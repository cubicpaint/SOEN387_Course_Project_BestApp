"""Completed request outcomes, coordinated by the caller's transaction lock."""

from copy import deepcopy
from hashlib import sha256
import json

from sqlalchemy.orm import Session

from app.data.models import IdempotencyRecord
from app.domain.appointments import BookingOutcome
from app.domain.types import ServiceChoice


BOOK_OPERATION = "POST /appointments"


def booking_fingerprint(slot_id: str, service: ServiceChoice, *, operation: str = BOOK_OPERATION) -> str:
    # Hash validated content rather than its original JSON representation.
    # The caller is a separate database scope; values stay case-sensitive.
    normalized = {"operation": operation, "request": {"slotId": slot_id, "service": service.value}}
    encoded = json.dumps(normalized, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("utf-8")
    return sha256(encoded).hexdigest()


class IdempotencyRepository:
    def __init__(self, session: Session):
        self.session = session

    def find(self, caller_id: str, key: str) -> IdempotencyRecord | None:
        return self.session.get(IdempotencyRecord, (caller_id, key))

    def store(
        self, *, caller_id: str, key: str, operation: str, fingerprint: str, outcome: BookingOutcome
    ) -> None:
        self.session.add(IdempotencyRecord(
            caller_id=caller_id,
            key=key,
            operation=operation,
            fingerprint=fingerprint,
            response_status=outcome.status,
            response_body=deepcopy(outcome.body),
        ))
        self.session.flush()
