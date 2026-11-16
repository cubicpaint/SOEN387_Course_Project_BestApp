"""Demonstrate safe authenticated booking contention against the running API.

Uses independent HTTP connections and a bounded start barrier. Run from the
repository root with development dependencies and application settings. This
script creates unique temporary fixtures, prints no credentials/tokens/keys,
and removes only its own fixture rows. It never enables an unsafe API mode.
"""

from concurrent.futures import ThreadPoolExecutor
import sys
from threading import Barrier

from scripts.phase4_probe import Phase4Probe, VerificationError, require


def together(*operations):
    ready = Barrier(len(operations))

    def call(operation):
        ready.wait(timeout=10)
        return operation()

    with ThreadPoolExecutor(max_workers=len(operations)) as pool:
        futures = [pool.submit(call, operation) for operation in operations]
        return [future.result(timeout=20) for future in futures]


def check_winner(results, code: str):
    require(sorted(reply.status for reply in results) == [201, 409], "Competing bookings did not produce one creation and one conflict.")
    winner = next(reply for reply in results if reply.status == 201)
    loser = next(reply for reply in results if reply.status == 409)
    require(loser.body["error"]["code"] == code, "A competing booking returned the wrong conflict.")
    return winner


def check_state(probe, before, *, allocations: int, outcomes: int) -> None:
    after = probe.state()
    require(after == {
        "appointments": before["appointments"] + 1,
        "allocations": before["allocations"] + allocations,
        "outcomes": before["outcomes"] + outcomes,
    }, "Committed database counts show duplicate appointments or partial allocation.")
    print(
        f"  Database: {after['appointments']} appointments, {after['allocations']} complete block allocations, "
        f"{after['outcomes']} completed outcomes (this run only)", flush=True,
    )


def demonstrate() -> None:
    with Phase4Probe() as probe:
        probe.check_publication()

        before = probe.state()
        results = together(
            lambda: probe.book(0, account=0, key="same-slot-one", status=(201, 409)),
            lambda: probe.book(0, account=1, key="same-slot-two", status=(201, 409)),
        )
        check_winner(results, "slot_unavailable")
        print("PASS: two customers, one barber slot -> one 201 creation and one 409 slot_unavailable", flush=True)
        check_state(probe, before, allocations=1, outcomes=2)

        before = probe.state()
        results = together(
            lambda: probe.book(4, service="both", account=0, key="combined-contender", status=(201, 409)),
            lambda: probe.book(5, service="haircut", account=1, key="single-contender", status=(201, 409)),
        )
        winner = check_winner(results, "slot_unavailable")
        print("PASS: 60 minutes versus 30 minutes sharing the second block -> one complete winner, no partial booking", flush=True)
        check_state(probe, before, allocations=winner.body["durationMinutes"] // 30, outcomes=2)

        before = probe.state()
        results = together(
            lambda: probe.book(8, barber=0, account=0, key="customer-one", status=(201, 409)),
            lambda: probe.book(8, barber=1, account=0, key="customer-two", status=(201, 409)),
        )
        check_winner(results, "customer_overlap")
        print("PASS: one customer, overlapping times with different barbers -> one creation and one 409 customer_overlap", flush=True)
        check_state(probe, before, allocations=1, outcomes=2)

        before = probe.state()
        results = together(
            lambda: probe.book(12, account=0, key="identical-retries"),
            lambda: probe.book(12, account=0, key="identical-retries"),
        )
        require(results[0].body == results[1].body, "Simultaneous identical retries returned different appointment results.")
        require(results[0].correlation_id != results[1].correlation_id, "Retries reused a request correlation ID.")
        print("PASS: simultaneous identical retries -> two original 201 responses with one appointment", flush=True)
        check_state(probe, before, allocations=1, outcomes=1)

        before = probe.state()
        results = together(
            lambda: probe.book(16, service="haircut", account=0, key="conflicting-retries", status=(201, 409)),
            lambda: probe.book(16, service="beard", account=0, key="conflicting-retries", status=(201, 409)),
        )
        winner = check_winner(results, "idempotency_key_conflict")
        require(probe.outcome("conflicting-retries") == winner.body, "Conflicting simultaneous reuse replaced the original outcome.")
        print("PASS: simultaneous changed bodies with one key -> one creation and one 409 idempotency_key_conflict", flush=True)
        check_state(probe, before, allocations=1, outcomes=1)
        print("PASS: authenticated concurrency demonstration complete; temporary fixtures will be removed", flush=True)


if __name__ == "__main__":
    try:
        demonstrate()
    except VerificationError as error:
        print(str(error), file=sys.stderr)
        raise SystemExit(1) from None
    except Exception:
        print("Booking concurrency demonstration failed. Inspect the API, database and configuration; no private values were printed.", file=sys.stderr)
        raise SystemExit(1) from None
