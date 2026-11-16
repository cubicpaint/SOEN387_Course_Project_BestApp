"""Canonical validated requests keep stable, operation-bound fingerprints."""

import json

from app.data.idempotency import BOOK_OPERATION, booking_fingerprint
from app.domain.types import ServiceChoice


def test_json_whitespace_and_property_order_do_not_change_fingerprint():
    first = json.loads('{"slotId":"slot_test","service":"both"}')
    retry = json.loads('{ "service" : "both", "slotId" : "slot_test" }')
    assert booking_fingerprint(first["slotId"], ServiceChoice(first["service"])) == booking_fingerprint(
        retry["slotId"], ServiceChoice(retry["service"])
    )


def test_fingerprint_binds_slot_service_and_operation_without_normalizing_values():
    original = booking_fingerprint("slot_test", ServiceChoice.HAIRCUT)
    changed = (
        booking_fingerprint("slot_other", ServiceChoice.HAIRCUT),
        booking_fingerprint("slot_TEST", ServiceChoice.HAIRCUT),
        booking_fingerprint("slot_test", ServiceChoice.BEARD),
        booking_fingerprint("slot_test", ServiceChoice.HAIRCUT, operation="POST /another-operation"),
    )
    assert all(value != original for value in changed)
    assert len(original) == 64
    assert all(character in "0123456789abcdef" for character in original)
    assert BOOK_OPERATION == "POST /appointments"
