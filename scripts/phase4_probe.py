"""Private temporary fixtures and live contract checks shared by Phase 4 scripts."""

from dataclasses import dataclass
from datetime import UTC, datetime, time, timedelta
import json
from secrets import token_urlsafe
from threading import Lock
from uuid import uuid4

from jsonschema import Draft202012Validator, FormatChecker
from pwdlib import PasswordHash
from sqlalchemy import delete, func, insert, select
import yaml

from app.config import load_auth_settings, load_settings
from app.data.database import create_engine_from_settings
from app.data.models import Appointment, AvailabilityBlock, Barber, IdempotencyRecord, SlotAllocation, User
from app.services.tokens import TokenService
from scripts.verify_phase3_runtime import ROOT, TORONTO, VerificationError, http_response, require, resolve


@dataclass(frozen=True)
class LiveReply:
    status: int
    body: dict
    correlation_id: str


class Phase4Probe:
    """Create only uniquely named fixtures; remove only those fixtures on exit."""

    def __init__(self):
        self.document = yaml.safe_load((ROOT / "api" / "openapi.yaml").read_text(encoding="utf-8"))
        self.engine = create_engine_from_settings(load_settings())
        self.token_service = TokenService(load_auth_settings())
        self.identity = uuid4().hex
        self.password = token_urlsafe(24)
        self.users = [f"usr_p4_{self.identity}_{index}" for index in range(3)]
        self.emails = [f"phase4.{self.identity}.{index}@example.com" for index in range(3)]
        self.barbers = [f"bar_p4_{self.identity}_{index}" for index in range(2)]
        self.day = datetime.now(UTC).astimezone(TORONTO).date() + timedelta(days=1)
        self.start = datetime.combine(self.day, time(10), tzinfo=TORONTO).astimezone(UTC)
        self.tokens = {}
        self._correlations = set()
        self._correlation_lock = Lock()
        self._created = False

    def slot(self, barber: int = 0, index: int = 0) -> str:
        suffix = "notice" if index == -1 else str(index)
        return f"slot_p4_{self.identity}_{barber}_{suffix}"

    def __enter__(self):
        try:
            self.check("/health/live")
            self.check("/health/ready")
            password_hash = PasswordHash.recommended().hash(self.password)
            now = datetime.now(UTC)
            recent = now.replace(minute=(now.minute // 30) * 30, second=0, microsecond=0)
            with self.engine.begin() as connection:
                connection.execute(insert(User), [
                    {"id": user, "email": email, "role": "staff" if index == 2 else "customer", "password_hash": password_hash}
                    for index, (user, email) in enumerate(zip(self.users, self.emails))
                ])
                connection.execute(insert(Barber), [
                    {"id": barber, "display_name": f"Phase 4 temporary barber {index + 1}"}
                    for index, barber in enumerate(self.barbers)
                ])
                blocks = []
                for barber_index, barber in enumerate(self.barbers):
                    for index in range(24):
                        starts_at = self.start + timedelta(minutes=30 * index)
                        blocks.append({
                            "id": self.slot(barber_index, index), "barber_id": barber,
                            "starts_at": starts_at, "ends_at": starts_at + timedelta(minutes=30),
                            "enabled": index != 23,
                        })
                blocks.append({
                    "id": self.slot(0, -1), "barber_id": self.barbers[0],
                    "starts_at": recent, "ends_at": recent + timedelta(minutes=30), "enabled": True,
                })
                connection.execute(insert(AvailabilityBlock), blocks)
            self._created = True
            for account in range(3):
                self.login(account)
            return self
        except Exception:
            self.__exit__(None, None, None)
            raise

    def __exit__(self, *_):
        try:
            if self._created:
                self.engine.dispose()
                with self.engine.begin() as connection:
                    own_appointments = select(Appointment.id).where(Appointment.customer_id.in_(self.users))
                    connection.execute(delete(IdempotencyRecord).where(IdempotencyRecord.caller_id.in_(self.users)))
                    connection.execute(delete(SlotAllocation).where(SlotAllocation.appointment_id.in_(own_appointments)))
                    connection.execute(delete(Appointment).where(Appointment.customer_id.in_(self.users)))
                    connection.execute(delete(AvailabilityBlock).where(AvailabilityBlock.barber_id.in_(self.barbers)))
                    connection.execute(delete(Barber).where(Barber.id.in_(self.barbers)))
                    connection.execute(delete(User).where(User.id.in_(self.users)))
        finally:
            self.engine.dispose()

    def login(self, account: int = 0) -> None:
        reply = self.check("/auth/login", method="post", body={"email": self.emails[account], "password": self.password})
        require(reply.body["user"]["id"] == self.users[account], "Login returned another fixture identity.")
        require(self.token_service.verify(reply.body["accessToken"]) == self.users[account], "Login issued an invalid token.")
        self.tokens[account] = reply.body["accessToken"]

    def check(
        self, path: str, *, method: str = "get", query: str = "", body: dict | bytes | None = None,
        account: int | None = None, key: str | None = None,
        headers: list[tuple[str, str]] | None = None, media_type: str = "application/json",
        status: int | tuple[int, ...] = 200, code: str | None = None,
    ) -> LiveReply:
        request_headers = list(headers or [])
        if account is not None:
            request_headers.append(("Authorization", "Bearer " + self.tokens[account]))
        if key is not None:
            request_headers.append(("Idempotency-Key", key))
        if body is not None:
            request_headers.append(("Content-Type", media_type))
            body = body if isinstance(body, bytes) else json.dumps(body).encode("utf-8")
        actual_status, result, response_headers = http_response(
            path + ("?" + query if query else ""), method, body, request_headers
        )
        expected = (status,) if isinstance(status, int) else status
        require(actual_status in expected, "A live operation returned an unexpected HTTP status.")
        declaration = resolve(self.document, self.document["paths"][path][method]["responses"][str(actual_status)])
        schema = declaration["content"]["application/json"]["schema"]
        Draft202012Validator(
            {"allOf": [schema], "components": self.document["components"]}, format_checker=FormatChecker()
        ).validate(result)
        require(response_headers.get_content_type() == "application/json", "Unexpected response media type.")
        for name, reference in declaration["headers"].items():
            values = response_headers.get_all(name, [])
            require(len(values) == 1, "A required response header is missing or repeated.")
            header_schema = resolve(self.document, reference)["schema"]
            value = int(values[0]) if header_schema.get("type") == "integer" else values[0]
            Draft202012Validator(header_schema, format_checker=FormatChecker()).validate(value)
        correlation = response_headers["X-Correlation-ID"]
        with self._correlation_lock:
            require(correlation not in self._correlations, "A correlation identifier was reused.")
            self._correlations.add(correlation)
        if code is not None:
            require(result["error"]["code"] == code, "An operation returned an unexpected business error.")
        return LiveReply(actual_status, result, correlation)

    def book(
        self, index: int, *, barber: int = 0, service: str = "haircut", account: int = 0,
        key: str, status: int | tuple[int, ...] = 201, code: str | None = None,
    ) -> LiveReply:
        return self.check(
            "/appointments", method="post", body={"slotId": self.slot(barber, index), "service": service},
            account=account, key=key, status=status, code=code,
        )

    def state(self) -> dict:
        with self.engine.connect() as connection:
            own_appointments = select(Appointment.id).where(Appointment.customer_id.in_(self.users))
            return {
                "appointments": connection.scalar(select(func.count()).select_from(Appointment).where(Appointment.customer_id.in_(self.users))),
                "allocations": connection.scalar(select(func.count()).select_from(SlotAllocation).where(SlotAllocation.appointment_id.in_(own_appointments))),
                "outcomes": connection.scalar(select(func.count()).select_from(IdempotencyRecord).where(IdempotencyRecord.caller_id.in_(self.users))),
            }

    def outcome(self, key: str, *, account: int = 0) -> dict | None:
        with self.engine.connect() as connection:
            return connection.scalar(select(IdempotencyRecord.response_body).where(
                IdempotencyRecord.caller_id == self.users[account], IdempotencyRecord.key == key,
            ))

    def check_publication(self) -> None:
        from openapi_spec_validator import validate_spec

        status, document, _ = http_response("/openapi.json", "get", None, [])
        require(status == 200, "The running OpenAPI document is unavailable.")
        validate_spec(document)
        require(document["paths"] == self.document["paths"], "The running milestone operations differ from the authored contract.")
        require(document["components"] == self.document["components"], "The running milestone schemas differ from the authored contract.")
