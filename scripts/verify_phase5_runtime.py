"""Verify Phase 5 against the running API with isolated temporary shop data.

Run from the repository root with host development dependencies and matching
DATABASE_URL/JWT settings. The default leaves containers running; --restart also
checks retained-volume restart/recreation. Neither mode changes the server clock
or provisioned demonstration data. Run restart checks separately from test suites.
"""

import argparse
from dataclasses import dataclass
from datetime import UTC, datetime, time, timedelta
from http.client import HTTPConnection
import json
import re
import sys
from urllib.parse import urlencode

from jsonschema import Draft202012Validator, FormatChecker
from sqlalchemy import delete, insert, select

from app.data.models import Appointment, AvailabilityBlock, IdempotencyRecord, SlotAllocation, StaffBarber
from scripts.phase4_probe import LiveReply, Phase4Probe, TORONTO, VerificationError, require, resolve
from scripts.verify_phase2_runtime import wait_ready
from scripts.verify_phase4_runtime import compose


@dataclass(frozen=True)
class ManagementReply(LiveReply):
    raw: bytes
    etag: str | None


class Phase5Probe(Phase4Probe):
    """Reuse fixture lifecycle while handling templated paths and raw replays."""

    def __init__(self):
        super().__init__()
        self.users = [value.replace("usr_p4_", "usr_p5_", 1) for value in self.users]
        self.emails = [value.replace("phase4.", "phase5.", 1) for value in self.emails]
        self.barbers = [value.replace("bar_p4_", "bar_p5_", 1) for value in self.barbers]
        self.day = datetime.now(UTC).astimezone(TORONTO).date() + timedelta(days=3)
        self.start = datetime.combine(self.day, time(10), tzinfo=TORONTO).astimezone(UTC)

    def slot(self, barber: int = 0, index: int = 0) -> str:
        return super().slot(barber, index).replace("slot_p4_", "slot_p5_", 1)

    def __enter__(self):
        try:
            super().__enter__()
            now = datetime.now(UTC)
            short_start = now.replace(minute=(now.minute // 30) * 30, second=0, microsecond=0) + timedelta(hours=3)
            with self.engine.begin() as connection:
                connection.execute(insert(StaffBarber), {"staff_id": self.users[2], "barber_id": self.barbers[0]})
                connection.execute(insert(AvailabilityBlock), [{
                    "id": self.slot(0, 1000 + index), "barber_id": self.barbers[0],
                    "starts_at": short_start + timedelta(minutes=30 * index),
                    "ends_at": short_start + timedelta(minutes=30 * (index + 1)), "enabled": True,
                } for index in range(2)])
            self.login(2)
            return self
        except Exception:
            self.__exit__(None, None, None)
            raise

    def __exit__(self, *_):
        try:
            if self._created:
                with self.engine.begin() as connection:
                    connection.execute(delete(StaffBarber).where(StaffBarber.staff_id.in_(self.users)))
        finally:
            super().__exit__(*_)

    def check(
        self, path: str, *, method: str = "get", query: str = "", body: dict | bytes | None = None,
        account: int | None = None, key: str | None = None,
        headers: list[tuple[str, str]] | None = None, media_type: str = "application/json",
        status: int | tuple[int, ...] = 200, code: str | None = None,
    ) -> ManagementReply:
        request_headers = dict(headers or [])
        if account is not None:
            request_headers["Authorization"] = "Bearer " + self.tokens[account]
        if key is not None:
            request_headers["Idempotency-Key"] = key
        if body is not None:
            request_headers["Content-Type"] = media_type
            body = body if isinstance(body, bytes) else json.dumps(body).encode("utf-8")
        connection = HTTPConnection("127.0.0.1", 8000, timeout=8)
        try:
            connection.request(method.upper(), path + ("?" + query if query else ""), body, request_headers)
            response = connection.getresponse()
            raw = response.read()
            response_headers = response.headers
            actual_status = response.status
        finally:
            connection.close()
        expected = (status,) if isinstance(status, int) else status
        require(actual_status in expected, "A Phase 5 HTTP operation returned an unexpected status.")
        result = json.loads(raw)
        candidates = [template for template in self.document["paths"] if re.fullmatch(
            "/".join("[^/]+" if segment.startswith("{") else re.escape(segment) for segment in template.split("/")), path,
        )]
        require(len(candidates) == 1, "A live path does not resolve to exactly one authored operation.")
        declaration = resolve(self.document, self.document["paths"][candidates[0]][method]["responses"][str(actual_status)])
        Draft202012Validator(
            {"allOf": [declaration["content"]["application/json"]["schema"]], "components": self.document["components"]},
            format_checker=FormatChecker(),
        ).validate(result)
        require(response_headers.get_content_type() == "application/json", "Unexpected Phase 5 response media type.")
        for name, reference in declaration["headers"].items():
            values = response_headers.get_all(name, [])
            require(len(values) == 1, "A Phase 5 response header is missing or repeated.")
            schema = resolve(self.document, reference)["schema"]
            value = int(values[0]) if schema.get("type") == "integer" else values[0]
            Draft202012Validator(
                {"allOf": [schema], "components": self.document["components"]}, format_checker=FormatChecker(),
            ).validate(value)
        correlation = response_headers["X-Correlation-ID"]
        with self._correlation_lock:
            require(correlation not in self._correlations, "A Phase 5 correlation identifier was reused.")
            self._correlations.add(correlation)
        if code is not None:
            require(result["error"]["code"] == code, "A Phase 5 operation returned an unexpected error code.")
        return ManagementReply(actual_status, result, correlation, raw, response_headers.get("ETag"))

    def allocation_ids(self, appointment_id: str) -> set[str]:
        with self.engine.connect() as connection:
            return set(connection.scalars(select(SlotAllocation.slot_id).where(SlotAllocation.appointment_id == appointment_id)))

    def outcomes(self) -> tuple:
        """Snapshot every fixture-owned durable binding without printing keys."""
        with self.engine.connect() as connection:
            return tuple(tuple(row) for row in connection.execute(select(
                IdempotencyRecord.caller_id, IdempotencyRecord.key, IdempotencyRecord.operation,
                IdempotencyRecord.fingerprint, IdempotencyRecord.response_status, IdempotencyRecord.response_body,
            ).where(IdempotencyRecord.caller_id.in_(self.users)).order_by(
                IdempotencyRecord.caller_id, IdempotencyRecord.key,
            )))

    def change(self, appointment_id: str, etag: str, key: str, *, slot: int | None = None,
               account: int = 0, status: int = 200, code: str | None = None) -> ManagementReply:
        return self.check(
            f"/appointments/{appointment_id}", method="delete" if slot is None else "put",
            account=account, key=key, headers=[("If-Match", etag)],
            body=None if slot is None else {"slotId": self.slot(0, slot)}, status=status, code=code,
        )


def same_replay(original: ManagementReply, replay: ManagementReply) -> None:
    require(original.status == replay.status and original.raw == replay.raw and original.etag == replay.etag,
            "A completed operation did not preserve its exact recorded body, status and ETag.")
    require(original.correlation_id != replay.correlation_id, "Replay did not receive a fresh correlation identifier.")


def verify(*, restart: bool = False) -> None:
    with Phase5Probe() as probe:
        probe.check_publication()
        require(sum(method in {"get", "post", "put", "delete"} for item in probe.document["paths"].values() for method in item) == 14,
                "Phase 5 must publish the fourteen implemented operations.")
        require(probe.barbers[0] in {row["id"] for row in probe.check("/barbers").body["barbers"]}, "Temporary barber is missing.")
        booked = probe.book(0, service="both", key="original-booking")
        appointment_id = booked.body["id"]
        path = f"/appointments/{appointment_id}"
        original = probe.check(path, account=0)
        require(original.etag == '"1"' and original.body["version"] == 1, "Initial management version is incorrect.")
        probe.check(path, status=401, code="unauthenticated")
        probe.check(path, account=1, status=403, code="forbidden")
        probe.check(path, account=2, status=403, code="forbidden")
        replacement = probe.check(path + "/availability", account=0, query=urlencode({"date": probe.day.isoformat()}))
        require(probe.slot(0, 1) in {slot["slotId"] for slot in replacement.body["slots"]}, "Replacement browsing omitted the shared-block move.")
        probe.check(path, method="put", account=0, key="changed-barber", headers=[("If-Match", original.etag)],
                    body={"slotId": probe.slot(1, 0)}, status=409, code="barber_change_forbidden")
        probe.check(path, method="put", account=0, key="changed-service", headers=[("If-Match", original.etag)],
                    body={"slotId": probe.slot(0, 1), "service": "haircut"}, status=422, code="validation_error")
        blocker = probe.book(2, account=1, key="destination-blocker")
        rejected = probe.change(appointment_id, original.etag, "blocked-move", slot=1, status=409, code="slot_unavailable")
        require(probe.check(path, account=0).raw == original.raw and probe.allocation_ids(appointment_id) == {probe.slot(0, 0), probe.slot(0, 1)},
                "Failed rescheduling changed the original appointment or its complete allocation.")
        blocker_detail = probe.check(f"/appointments/{blocker.body['id']}", account=1)
        probe.change(blocker.body["id"], blocker_detail.etag, "cancel-blocker", account=1)
        moved = probe.change(appointment_id, original.etag, "move-forward", slot=1)
        require(moved.body["version"] == 2 and moved.body["barberId"] == booked.body["barberId"] and moved.body["service"] == "both",
                "Rescheduling changed barber/service or failed to advance the version.")
        require(probe.allocation_ids(appointment_id) == {probe.slot(0, 1), probe.slot(0, 2)}, "Shared-block move allocated the wrong blocks.")
        same_replay(rejected, probe.change(appointment_id, original.etag, "blocked-move", slot=1, status=409, code="slot_unavailable"))
        same_replay(booked, probe.book(0, service="both", key="original-booking"))
        same_replay(moved, probe.change(appointment_id, original.etag, "move-forward", slot=1))
        print("PASS: owner protection, fixed barber/service, failed-move rollback, 60-minute move by 30 minutes and exact historical replays", flush=True)

        short = probe.book(1000, service="both", key="short-notice")
        short_detail = probe.check(f"/appointments/{short.body['id']}", account=0)
        probe.change(short.body["id"], short_detail.etag, "short-cancel", status=422, code="change_cutoff_violation")
        probe.change(short.body["id"], short_detail.etag, "short-move", slot=10, status=422, code="change_cutoff_violation")
        require(len(probe.allocation_ids(short.body["id"])) == 2, "A cutoff rejection released short-notice blocks.")
        print("PASS: production change cutoff rejects short-notice cancellation and rescheduling without releasing blocks", flush=True)

        staff_path = f"/barbers/{probe.barbers[0]}"
        query = urlencode({"date": probe.day.isoformat()})
        staff_list = probe.check(staff_path + "/appointments", account=2)
        require(appointment_id in {row["id"] for row in staff_list.body["appointments"]}, "Authorized staff cannot see the confirmed appointment.")
        probe.check(f"/barbers/{probe.barbers[1]}/appointments", account=2, status=403, code="forbidden")
        probe.check(staff_path + "/availability", account=0, query=query, status=403, code="forbidden")
        configured = probe.check(staff_path + "/availability", account=2, query=query)
        occupied = probe.check(staff_path + "/availability", method="put", account=2, query=query, key="occupied-date",
                               headers=[("If-Match", configured.etag)], body={"ranges": []}, status=409, code="availability_occupied")
        require(probe.check(staff_path + "/availability", account=2, query=query).raw == configured.raw,
                "An occupied availability edit partially changed the schedule.")
        cancelled = probe.change(appointment_id, moved.etag, "cancel-main")
        require(cancelled.body["status"] == "cancelled" and cancelled.body["version"] == 3 and not probe.allocation_ids(appointment_id),
                "Cancellation failed to retain history or release every block.")
        same_replay(cancelled, probe.change(appointment_id, moved.etag, "cancel-main"))
        same_replay(moved, probe.change(appointment_id, original.etag, "move-forward", slot=1))
        same_replay(booked, probe.book(0, service="both", key="original-booking"))
        require(appointment_id not in {row["id"] for row in probe.check("/appointments", account=0).body["appointments"]},
                "Cancelled history remains in the upcoming customer list.")
        probe.change(appointment_id, moved.etag, "stale-cancel", status=412, code="stale_version")
        print("PASS: authorized staff views, whole-edit occupied rejection, cancellation release and immutable retry after later changes", flush=True)

        probe.check(staff_path + "/availability", method="put", account=2, query=query, key="stale-date",
                    headers=[("If-Match", configured.etag)], body={"ranges": []}, status=412, code="stale_version")
        fresh = probe.check(staff_path + "/availability", account=2, query=query)
        closed = probe.check(staff_path + "/availability", method="put", account=2, query=query, key="close-date",
                             headers=[("If-Match", fresh.etag)], body={"ranges": []})
        require(not closed.body["ranges"] and all(not row["enabled"] for row in closed.body["blocks"]), "Date closure left enabled blocks.")
        same_replay(closed, probe.check(staff_path + "/availability", method="put", account=2, query=query, key="close-date",
                                       headers=[("If-Match", fresh.etag)], body={"ranges": []}))
        public_query = urlencode({"barberId": probe.barbers[0], "date": probe.day.isoformat(), "service": "both"})
        require(probe.check("/availability", query=public_query).body["slots"] == [], "Closed availability remains publicly bookable.")
        ranges = [{"startTime": probe.start.astimezone(TORONTO).isoformat(),
                   "endTime": (probe.start + timedelta(hours=2)).astimezone(TORONTO).isoformat()}]
        reopened = probe.check(staff_path + "/availability", method="put", account=2, query=query, key="reopen-date",
                               headers=[("If-Match", closed.etag)], body={"ranges": ranges})
        require(probe.slot(0, 0) in {row["slotId"] for row in reopened.body["blocks"]}, "Availability edit replaced an existing slot identity.")
        same_replay(closed, probe.check(staff_path + "/availability", method="put", account=2, query=query, key="close-date",
                                       headers=[("If-Match", fresh.etag)], body={"ranges": []}))
        require(probe.check("/availability", query=public_query).body["slots"], "Historical closure replay disabled the current reopened schedule.")
        same_replay(occupied, probe.check(staff_path + "/availability", method="put", account=2, query=query, key="occupied-date",
                                         headers=[("If-Match", configured.etag)], body={"ranges": []}, status=409, code="availability_occupied"))
        print("PASS: stale staff preconditions, complete date closure/reopen, permanent slot identity and historical availability replay", flush=True)

        active_booking = probe.book(0, service="both", key="persistent-booking")
        active_id = active_booking.body["id"]
        active_path = f"/appointments/{active_id}"
        active_original = probe.check(active_path, account=0)
        active_move = probe.change(active_id, active_original.etag, "persistent-move", slot=1)
        require(probe.allocation_ids(active_id) == {probe.slot(0, 1), probe.slot(0, 2)},
                "The active persistence fixture did not retain and acquire the correct blocks.")
        same_replay(active_booking, probe.book(0, service="both", key="persistent-booking"))
        require(probe.check(path, account=0).raw == cancelled.raw and not probe.allocation_ids(appointment_id),
                "Reusing a cancelled appointment's old blocks changed its retained history.")

        if restart:
            expected_state = probe.state()
            expected_outcomes = probe.outcomes()
            expected_own = probe.check("/appointments", account=0).body
            expected_staff = probe.check(staff_path + "/appointments", account=2).body
            expected_date = probe.check(staff_path + "/availability", account=2, query=query)
            appointment_ids = (appointment_id, blocker.body["id"], short.body["id"], active_id)
            expected_allocations = {identifier: probe.allocation_ids(identifier) for identifier in appointment_ids}
            for arguments in (("restart", "db", "api"), ("up", "-d", "--force-recreate", "db", "migrate", "api")):
                probe.engine.dispose()
                compose(*arguments)
                wait_ready()
                for account in range(3):
                    probe.login(account)
                same_replay(cancelled, probe.check(path, account=0))
                same_replay(active_move, probe.check(active_path, account=0))
                require({identifier: probe.allocation_ids(identifier) for identifier in appointment_ids} == expected_allocations,
                        "Restart/recreation changed active or cancelled appointment allocations.")
                require(probe.check("/appointments", account=0).body == expected_own,
                        "Restart/recreation changed the customer's current upcoming appointments.")
                require(probe.check(staff_path + "/appointments", account=2).body == expected_staff,
                        "Restart/recreation changed staff appointment visibility or assignments.")
                same_replay(expected_date, probe.check(staff_path + "/availability", account=2, query=query))
                same_replay(booked, probe.book(0, service="both", key="original-booking"))
                same_replay(moved, probe.change(appointment_id, original.etag, "move-forward", slot=1))
                same_replay(cancelled, probe.change(appointment_id, moved.etag, "cancel-main"))
                same_replay(active_booking, probe.book(0, service="both", key="persistent-booking"))
                same_replay(active_move, probe.change(active_id, active_original.etag, "persistent-move", slot=1))
                same_replay(closed, probe.check(staff_path + "/availability", method="put", account=2, query=query, key="close-date",
                                               headers=[("If-Match", fresh.etag)], body={"ranges": []}))
                require(probe.outcomes() == expected_outcomes and probe.state() == expected_state,
                        "Restart/recreation or historical replay changed persisted bindings, counts, or current appointments.")
                require({identifier: probe.allocation_ids(identifier) for identifier in appointment_ids} == expected_allocations,
                        "Historical replay changed retained allocations after restart/recreation.")
                same_replay(cancelled, probe.check(path, account=0))
                same_replay(active_move, probe.check(active_path, account=0))
                same_replay(expected_date, probe.check(staff_path + "/availability", account=2, query=query))
            print("PASS: active reschedules, cancelled history, allocations, staff revisions and exact immutable outcomes survive retained-volume restart/recreation", flush=True)
        print("PASS: Phase 5 live verification complete; only temporary fixtures will be removed", flush=True)


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
        print("Phase 5 runtime verification failed. Inspect API/database configuration; no private values were printed.", file=sys.stderr)
        raise SystemExit(1) from None
