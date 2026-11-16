"""Check the running Phase 3 API using temporary, uniquely named fixtures.

Run from the repository root with development dependencies and a running API.
Uses the configured application database, preserves demonstration data, and
removes only this run's customer, barber, blocks and appointment on completion.
"""

from datetime import UTC, datetime, time, timedelta
from http.client import HTTPConnection
import json
from pathlib import Path
from secrets import token_urlsafe
import sys
from urllib.parse import urlencode
from uuid import uuid4
from zoneinfo import ZoneInfo

from jsonschema import Draft202012Validator, FormatChecker
from pwdlib import PasswordHash
from sqlalchemy import delete, insert, update
import yaml

from app.config import load_auth_settings, load_settings
from app.data.database import create_engine_from_settings
from app.data.models import Appointment, AvailabilityBlock, Barber, SlotAllocation, User
from app.services.tokens import TokenService


ROOT = Path(__file__).resolve().parents[1]
TORONTO = ZoneInfo("America/Toronto")


class VerificationError(RuntimeError):
    """A controlled diagnostic that excludes private fixture and request values."""


def require(condition: bool, message: str) -> None:
    if not condition:
        raise VerificationError(message)


def resolve(document: dict, value: dict) -> dict:
    while "$ref" in value:
        reference = value["$ref"]
        require(reference.startswith("#/"), "A contract reference is not local.")
        value = document
        for part in reference[2:].split("/"):
            value = value[part.replace("~1", "/").replace("~0", "~")]
    return value


def http_response(path: str, method: str, body: bytes | None, headers: list[tuple[str, str]]):
    # The low-level API preserves duplicate headers, unlike a header dictionary.
    connection = HTTPConnection("127.0.0.1", 8000, timeout=8)
    try:
        connection.putrequest(method.upper(), path)
        for name, value in headers:
            connection.putheader(name, value)
        if body is not None:
            connection.putheader("Content-Length", str(len(body)))
        connection.endheaders(body)
        response = connection.getresponse()
        return response.status, json.loads(response.read()), response.headers
    finally:
        connection.close()


def verify() -> None:
    document = yaml.safe_load((ROOT / "api" / "openapi.yaml").read_text(encoding="utf-8"))
    tokens = TokenService(load_auth_settings())
    engine = create_engine_from_settings(load_settings())
    identity = uuid4().hex
    customer_id = "usr_phase3_" + identity
    barber_id = "bar_phase3_" + identity
    appointment_id = "apt_phase3_" + identity
    slot_ids = [f"slot_phase3_{identity}_{index}" for index in range(4)]
    email = f"phase3.{identity}@example.com"
    password = token_urlsafe(24)
    day = datetime.now(UTC).astimezone(TORONTO).date() + timedelta(days=1)
    first_start = datetime.combine(day, time(10), tzinfo=TORONTO).astimezone(UTC)
    starts = [first_start + timedelta(minutes=30 * index) for index in range(4)]
    correlation_ids = set()
    fixtures_created = False

    def check(
        path: str,
        *,
        method: str = "get",
        query: str = "",
        body: bytes | None = None,
        media_type: str = "application/json",
        headers: list[tuple[str, str]] | None = None,
        status: int = 200,
        code: str | None = None,
    ) -> dict:
        request_headers = list(headers or [])
        if body is not None:
            request_headers.append(("Content-Type", media_type))
        actual_status, result, response_headers = http_response(
            path + ("?" + query if query else ""), method, body, request_headers
        )
        require(actual_status == status, "A live operation returned an unexpected HTTP status.")
        declaration = resolve(document, document["paths"][path][method]["responses"][str(status)])
        schema = declaration["content"]["application/json"]["schema"]
        Draft202012Validator(
            {"allOf": [schema], "components": document["components"]}, format_checker=FormatChecker()
        ).validate(result)
        require(response_headers.get("Content-Type", "").startswith("application/json"), "A response has an unexpected media type.")
        for name, reference in declaration["headers"].items():
            require(name in response_headers, "A required response header is missing.")
            header_schema = resolve(document, reference)["schema"]
            value = response_headers[name]
            if header_schema.get("type") == "integer":
                value = int(value)
            Draft202012Validator(header_schema, format_checker=FormatChecker()).validate(value)
        correlation_id = response_headers["X-Correlation-ID"]
        require(correlation_id not in correlation_ids, "A correlation identifier was reused.")
        correlation_ids.add(correlation_id)
        if code is not None:
            require(result["error"]["code"] == code, "An operation returned an unexpected error code.")
        return result

    def availability(service: str) -> dict:
        return check("/availability", query=urlencode({"barberId": barber_id, "date": day.isoformat(), "service": service}))

    try:
        check("/health/live")
        check("/health/ready")
        fixtures_created = True
        with engine.begin() as connection:
            connection.execute(insert(User).values(id=customer_id, email=email, role="customer", password_hash=PasswordHash.recommended().hash(password)))
            connection.execute(insert(Barber).values(id=barber_id, display_name="Phase 3 runtime verification"))
            connection.execute(insert(AvailabilityBlock), [
                {"id": slot_id, "barber_id": barber_id, "starts_at": start, "ends_at": start + timedelta(minutes=30), "enabled": True}
                for slot_id, start in zip(slot_ids, starts)
            ])
        credentials = json.dumps({"email": email, "password": password}).encode("utf-8")
        public_headers = [("Authorization", "invalid"), ("Authorization", "Bearer invalid")]
        login = check("/auth/login", method="post", body=credentials, headers=public_headers)
        require(login["user"] == {"id": customer_id, "role": "customer", "managedBarberIds": []}, "Login returned an unexpected caller.")
        require(tokens.verify(login["accessToken"]) == customer_id, "The issued token did not verify for its caller.")
        require(login["expiresIn"] == tokens.settings.jwt_ttl_seconds, "The configured token lifetime was not returned.")
        wrong_password = check("/auth/login", method="post", body=json.dumps({"email": email, "password": token_urlsafe(24)}).encode(), status=401, code="invalid_credentials")
        unknown_email = check("/auth/login", method="post", body=json.dumps({"email": f"missing.{identity}@example.com", "password": password}).encode(), status=401, code="invalid_credentials")
        require(wrong_password == unknown_email, "Failed login responses disclose account existence.")
        print("PASS: live login, generic credential failures, signed token and caller", flush=True)

        barbers = check("/barbers", headers=public_headers)["barbers"]
        require({"id": barber_id, "displayName": "Phase 3 runtime verification"} in barbers, "Barber listing omitted the temporary fixture.")
        for service in ("haircut", "beard", "both"):
            result = availability(service)
            duration = 60 if service == "both" else 30
            count = 3 if service == "both" else 4
            require(result["durationMinutes"] == duration, "Availability returned the wrong service duration.")
            require([slot["slotId"] for slot in result["slots"]] == slot_ids[:count], "Availability did not preserve ordered slot identities.")
            for index, slot in enumerate(result["slots"]):
                require(datetime.fromisoformat(slot["startTime"]) == starts[index], "Availability returned the wrong start instant.")
                require(datetime.fromisoformat(slot["endTime"]) == starts[index] + timedelta(minutes=duration), "Availability did not cover the complete service.")
        query = urlencode({"barberId": barber_id, "date": day.isoformat(), "service": "both"})
        check("/availability", query=query, headers=public_headers)
        print("PASS: public barber listing and complete 30/60-minute availability", flush=True)

        with engine.begin() as connection:
            connection.execute(insert(Appointment).values(id=appointment_id, customer_id=customer_id, barber_id=barber_id, service="haircut", starts_at=starts[1], ends_at=starts[1] + timedelta(minutes=30)))
            connection.execute(insert(SlotAllocation).values(slot_id=slot_ids[1], appointment_id=appointment_id))
            connection.execute(update(AvailabilityBlock).where(AvailabilityBlock.id == slot_ids[2]).values(enabled=False))
        require(availability("both")["slots"] == [], "Occupied or disabled continuation was advertised for a combined service.")
        require([slot["slotId"] for slot in availability("haircut")["slots"]] == [slot_ids[0], slot_ids[3]], "Occupied or disabled blocks were advertised.")
        print("PASS: occupied and disabled blocks exclude incomplete service options", flush=True)

        check("/availability", query=urlencode({"barberId": "bar_missing_" + identity, "date": day.isoformat(), "service": "haircut"}), status=404, code="barber_not_found")
        check("/barbers", query="unknown=1", status=422, code="validation_error")
        check("/availability", query=query + "&service=both", status=422, code="validation_error")
        check("/availability", query=urlencode({"barberId": barber_id, "date": "2026-02-30", "service": "haircut"}), status=422, code="validation_error")
        check("/auth/login", method="post", body=b"{", status=400, code="invalid_json")
        check("/auth/login", method="post", body=b'{"email":"first@example.com","email":"second@example.com","password":"unused"}', status=400, code="invalid_json")
        check("/auth/login", method="post", body=credentials, media_type="text/plain", status=415, code="unsupported_media_type")
        print("PASS: live negative cases conform to authored bodies, headers and errors", flush=True)
    finally:
        try:
            if fixtures_created:
                with engine.begin() as connection:
                    connection.execute(delete(SlotAllocation).where(SlotAllocation.appointment_id == appointment_id))
                    connection.execute(delete(Appointment).where(Appointment.id == appointment_id))
                    connection.execute(delete(AvailabilityBlock).where(AvailabilityBlock.id.in_(slot_ids)))
                    connection.execute(delete(Barber).where(Barber.id == barber_id))
                    connection.execute(delete(User).where(User.id == customer_id))
        finally:
            engine.dispose()


if __name__ == "__main__":
    try:
        verify()
    except VerificationError as error:
        print(str(error), file=sys.stderr)
        raise SystemExit(1) from None
    except Exception:
        print("Phase 3 runtime verification failed. Check the API, database and configuration; no private values were printed.", file=sys.stderr)
        raise SystemExit(1) from None
