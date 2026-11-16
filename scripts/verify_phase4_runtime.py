"""Check real Phase 4 HTTP workflows; --restart also verifies retained-volume persistence.

Run after building/starting the API, from the repository root with development
dependencies and application DATABASE_URL/JWT settings. The default does not
restart containers. Both modes create and clean only unique temporary fixtures.
"""

import argparse
import subprocess
import sys
from urllib.parse import urlencode

from scripts.phase4_probe import Phase4Probe, ROOT, VerificationError, require
from scripts.verify_phase2_runtime import wait_ready


def compose(*arguments: str) -> None:
    result = subprocess.run(
        ["docker", "compose", *arguments], cwd=ROOT,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        encoding="utf-8", errors="replace", timeout=120,
    )
    require(result.returncode == 0, "A requested container restart/recreation failed; inspect Compose status and logs.")


def verify(*, restart: bool = False) -> None:
    with Phase4Probe() as probe:
        probe.check_publication()
        require(probe.check("/appointments", account=0).body == {"appointments": []}, "A new customer has appointments.")
        for service in ("haircut", "beard", "both"):
            reply = probe.check("/availability", query=urlencode({
                "barberId": probe.barbers[0], "date": probe.day.isoformat(), "service": service,
            }))
            ids = [slot["slotId"] for slot in reply.body["slots"]]
            require(probe.slot(0, 0) in ids, "A complete fixture service was not browsable.")
            require(probe.slot(0, 23) not in ids, "Disabled availability was advertised.")
            if service == "both":
                require(probe.slot(0, 22) not in ids, "An incomplete combined service was advertised.")

        first = probe.book(0, key="first-booking")
        beard = probe.book(2, service="beard", key="beard-booking")
        combined = probe.book(4, service="both", key="combined-booking")
        for reply, duration in ((first, 30), (beard, 30), (combined, 60)):
            require(reply.body["durationMinutes"] == duration, "A booked service has the wrong duration.")
        require(probe.state() == {"appointments": 3, "allocations": 4, "outcomes": 3}, "A service did not allocate its complete duration.")
        retry = probe.check(
            "/appointments", method="post", account=0, key="first-booking", status=201,
            body=(f'{{ "service" : "haircut", "slotId" : "{probe.slot()}" }}').encode(),
        )
        require(retry.body == first.body and retry.correlation_id != first.correlation_id, "Replay did not preserve the result with a fresh correlation ID.")
        probe.book(0, service="beard", key="first-booking", status=409, code="idempotency_key_conflict")
        require(probe.outcome("first-booking") == first.body, "Conflicting reuse changed the original outcome.")
        probe.book(8, account=1, key="first-booking")
        print("PASS: login, all three services, complete allocations, sequential replay and caller-scoped keys", flush=True)

        def durable_rejection(key, operation, *, account=0):
            before = probe.state()
            original = operation(key)
            retry = operation(key)
            require(original.body == retry.body and original.status == retry.status, "A business rejection did not replay identically.")
            require(probe.outcome(key, account=account) == original.body, "A business rejection was not stored.")
            after = probe.state()
            require(after == {**before, "outcomes": before["outcomes"] + 1}, "A rejected booking changed appointments or partial allocations.")

        durable_rejection("missing", lambda key: probe.check(
            "/appointments", method="post", account=0, key=key,
            body={"slotId": "slot_missing_" + probe.identity, "service": "haircut"}, status=404, code="slot_not_found",
        ))
        durable_rejection("occupied", lambda key: probe.book(0, account=1, key=key, status=409, code="slot_unavailable"), account=1)
        durable_rejection("incomplete", lambda key: probe.book(22, service="both", key=key, status=409, code="slot_unavailable"))
        durable_rejection("overlap", lambda key: probe.book(0, barber=1, key=key, status=409, code="customer_overlap"))
        durable_rejection("notice", lambda key: probe.book(-1, key=key, status=422, code="booking_notice_violation"))
        print("PASS: durable missing-slot, unavailable-duration, customer-overlap and notice rejections without partial writes", flush=True)

        before_invalid = probe.state()
        valid_body = {"slotId": probe.slot(0, 10), "service": "haircut"}
        for key in (None, "bad key"):
            probe.check("/appointments", method="post", account=0, key=key, body=valid_body, status=422, code="validation_error")
        probe.check("/appointments", method="post", account=0, key="duplicate", headers=[("Idempotency-Key", "duplicate")], body=valid_body, status=422, code="validation_error")
        probe.check("/appointments", method="post", account=0, key="correctable", body={**valid_body, "customerId": probe.users[1]}, status=422, code="validation_error")
        probe.check("/appointments", method="post", account=0, key="malformed", body=b"{", status=400, code="invalid_json")
        probe.check("/appointments", method="post", account=0, key="duplicate-json", body=b'{"slotId":"slot_a","slotId":"slot_b","service":"haircut"}', status=400, code="invalid_json")
        probe.check("/appointments", method="post", account=0, key="wrong-media", body=valid_body, media_type="text/plain", status=415, code="unsupported_media_type")
        probe.check("/appointments", account=0, query="customerId=ignored", status=422, code="validation_error")
        probe.check("/appointments", method="post", key="anonymous", body=valid_body, status=401, code="unauthenticated")
        probe.check("/appointments", headers=[("Authorization", "Bearer invalid")], status=401, code="unauthenticated")
        probe.check("/appointments", account=0, headers=[("Authorization", "Bearer invalid")], status=401, code="unauthenticated")
        probe.check("/appointments", account=2, status=403, code="forbidden")
        probe.check("/appointments", method="post", account=2, key="staff", body=valid_body, status=403, code="forbidden")
        require(probe.state() == before_invalid, "An input or authorization failure consumed a key or changed a booking.")
        probe.book(10, key="correctable")
        own = probe.check("/appointments", account=0).body["appointments"]
        other = probe.check("/appointments", account=1).body["appointments"]
        require(len(own) == 4 and len(other) == 1, "Own-appointment listing returned the wrong owner or count.")
        require({row["id"] for row in own}.isdisjoint(row["id"] for row in other), "Another customer's appointment was exposed.")
        require(own == sorted(own, key=lambda row: (row["startTime"], row["id"])), "Appointments are not ordered by start and ID.")
        print("PASS: strict input/key validation, authorization, corrected unbound key, ownership and live contract conformance", flush=True)

        if restart:
            expected_state = probe.state()
            expected_own = own
            for arguments in (("restart", "db", "api"), ("up", "-d", "--force-recreate", "db", "migrate", "api")):
                probe.engine.dispose()
                compose(*arguments)
                wait_ready()
                probe.login(0)
                replay = probe.book(0, key="first-booking")
                require(replay.body == first.body, "Restart lost or changed the original booking outcome.")
                require(probe.check("/appointments", account=0).body["appointments"] == expected_own, "Restart lost customer appointments.")
                require(probe.state() == expected_state, "Restart/recreation changed persisted booking, allocation or outcome counts.")
                require(probe.outcome("first-booking") == first.body, "Restart lost the durable idempotency snapshot.")
                probe.book(-1, key="notice", status=422, code="booking_notice_violation")
            print("PASS: appointments, complete allocations and immutable outcomes survive restart and container recreation with the retained volume", flush=True)
        print("PASS: Phase 4 live verification complete; temporary fixtures will be removed", flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--restart", action="store_true", help="Also restart/recreate API and database containers, retaining the named volume.")
    arguments = parser.parse_args()
    try:
        verify(restart=arguments.restart)
    except VerificationError as error:
        print(str(error), file=sys.stderr)
        raise SystemExit(1) from None
    except Exception:
        print("Phase 4 runtime verification failed. Inspect the API, database and configuration; no private values were printed.", file=sys.stderr)
        raise SystemExit(1) from None
