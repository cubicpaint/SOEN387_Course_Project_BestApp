"""Explicit local acceptance check; briefly stops/recreates this Compose database.

Run from the repository root with the development dependencies installed.
Keeps the named volume and existing data, removing only its own unique probe.
"""

import json
import subprocess
import sys
import time
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import urlopen
from uuid import UUID, uuid4

from sqlalchemy import text

from app.config import load_settings
from app.data.database import create_engine_from_settings

ROOT = Path(__file__).resolve().parents[1]
BASE_URL = "http://127.0.0.1:8000"


class VerificationError(RuntimeError):
    """A verification diagnostic that contains no connection or credential values."""


def compose(*arguments: str) -> None:
    result = subprocess.run(
        ["docker", "compose", *arguments], cwd=ROOT,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        encoding="utf-8", errors="replace",
    )
    if result.returncode:
        raise VerificationError("Compose " + " ".join(arguments) + " failed; inspect docker compose status and logs.")


def response(path: str) -> tuple[int, dict, object]:
    try:
        reply = urlopen(BASE_URL + path, timeout=6)
    except HTTPError as error:
        reply = error
    with reply:
        return reply.status, json.load(reply), reply.headers


def require(condition: bool, message: str) -> None:
    if not condition:
        raise VerificationError(message)


def wait_ready() -> None:
    for _ in range(40):
        try:
            if response("/health/ready")[0] == 200:
                return
        except (URLError, TimeoutError, ConnectionError):
            pass
        time.sleep(1)
    raise VerificationError("API did not become ready within the verification window.")


def verify() -> None:
    engine = create_engine_from_settings(load_settings())
    probe_id = "bar_phase2_probe_" + uuid4().hex
    probe_created = False
    try:
        compose("up", "-d", "--build", "api")
        wait_ready()
        correlation_ids = []
        for path, expected in (("/health/live", "alive"), ("/health/ready", "ready")):
            status, body, headers = response(path)
            require(status == 200 and body == {"status": expected}, "Unexpected health response.")
            require(headers.get("Cache-Control") == "no-store", "Missing no-store header.")
            correlation_ids.append(UUID(headers["X-Correlation-ID"]))
        require(len(set(correlation_ids)) == 2, "Correlation IDs must be fresh.")
        status, document, _ = response("/openapi.json")
        require(status == 200 and set(document["paths"]) == {"/health/live", "/health/ready"},
                "Runtime contract must expose only implemented health paths.")
        status, body, _ = response("/health/live?unexpected=1&unexpected=2")
        require(status == 422 and body["error"]["code"] == "validation_error", "Invalid query was accepted.")
        print("PASS: running health responses, headers, validation, and published contract", flush=True)

        with engine.begin() as connection:
            connection.execute(text("INSERT INTO barbers (id, display_name) VALUES (:id, :name)"),
                               {"id": probe_id, "name": "Phase 2 persistence verification"})
        probe_created = True

        def check_persisted() -> None:
            engine.dispose()
            with engine.connect() as connection:
                require(connection.scalar(text("SELECT display_name FROM barbers WHERE id = :id"), {"id": probe_id})
                        == "Phase 2 persistence verification", "Persisted probe was lost.")
                require(connection.scalar(text("SELECT version_num FROM alembic_version")) == "0001_core",
                        "Migration revision was lost.")

        compose("restart", "db", "api")
        wait_ready()
        check_persisted()
        compose("up", "-d", "--force-recreate", "db", "migrate", "api")
        wait_ready()
        check_persisted()
        print("PASS: inserted data and migration revision survive restart and container recreation", flush=True)

        try:
            compose("stop", "db")
            require(response("/health/live")[:2] == (200, {"status": "alive"}), "Database outage broke liveness.")
            status, body, headers = response("/health/ready")
            require(status == 503 and body["error"]["code"] == "service_unavailable"
                    and body["error"]["retryable"] is True and headers.get("Retry-After") == "5",
                    "Database outage did not produce the documented readiness error.")
        finally:
            compose("start", "db")
        wait_ready()
        print("PASS: liveness during database outage, controlled readiness failure, and recovery", flush=True)
    finally:
        try:
            if probe_created:
                engine.dispose()
                with engine.begin() as connection:
                    connection.execute(text("DELETE FROM barbers WHERE id = :id"), {"id": probe_id})
        finally:
            engine.dispose()


if __name__ == "__main__":
    try:
        verify()
    except VerificationError as error:
        print(str(error), file=sys.stderr)
        raise SystemExit(1) from None
    except Exception:
        print("Phase 2 runtime verification failed. Inspect Compose status and logs; no secret values were printed.", file=sys.stderr)
        raise SystemExit(1) from None
