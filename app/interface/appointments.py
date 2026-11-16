"""Appointment HTTP representations matching the authored milestone contract."""

import json
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Header, Request
from fastapi.responses import JSONResponse
from fastapi.security import HTTPBearer
from pydantic import BaseModel, ConfigDict, Field

from app.domain.identity import Caller
from app.domain.types import ServiceChoice
from app.interface.auth import current_caller
from app.interface.browsing import BarberId, SlotId, UTCDateTime
from app.interface.validation import InputError, JSONRoute, validate_query
from app.services.authorization import require_customer


AppointmentId = Annotated[str, Field(pattern=r"^apt_[A-Za-z0-9_-]+$", max_length=64)]
KEY_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$"


class BookingJSONResponse(JSONResponse):
    def render(self, content: object) -> bytes:
        # JSONB can reorder object keys. Serialize both the first response and
        # its replay canonically so the raw HTTP bodies also remain identical.
        return json.dumps(
            content, ensure_ascii=False, allow_nan=False, sort_keys=True, separators=(",", ":"),
        ).encode("utf-8")


class BookingRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    slotId: SlotId
    service: ServiceChoice


class AppointmentResponse(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        json_schema_extra={"allOf": [{
            "if": {"properties": {"service": {"enum": ["haircut", "beard"]}}},
            "then": {"properties": {"durationMinutes": {"const": 30}}},
            "else": {"properties": {"durationMinutes": {"const": 60}}},
        }]},
    )
    id: AppointmentId
    barberId: BarberId
    slotId: SlotId
    service: ServiceChoice
    durationMinutes: Literal[30, 60]
    startTime: UTCDateTime
    endTime: UTCDateTime
    status: Literal["confirmed"]
    createdAt: UTCDateTime


class AppointmentListResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    appointments: list[AppointmentResponse]


def current_customer(request: Request) -> Caller:
    # The application service repeats this permission check for non-HTTP callers.
    return require_customer(current_caller(request))


# Declare bearer security for FastAPI's native schema. current_caller still
# performs strict verification, including rejection of repeated headers.
router = APIRouter(tags=["Appointments"], dependencies=[
    Depends(HTTPBearer(scheme_name="bearerAuth", bearerFormat="JWT", auto_error=False)),
])
booking_router = APIRouter(route_class=JSONRoute)


@booking_router.post(
    "/appointments", operation_id="bookAppointment", status_code=201,
    response_model=AppointmentResponse,
)
def book_appointment(
    request: Request,
    body: BookingRequest,
    caller: Annotated[Caller, Depends(current_customer)],
    key: Annotated[str, Header(alias="Idempotency-Key", min_length=1, max_length=128, pattern=KEY_PATTERN)],
):
    validate_query(request, set())
    if len(request.headers.getlist("idempotency-key")) != 1:
        raise InputError(422, "validation_error", "Send exactly one valid Idempotency-Key header.")
    outcome = request.app.state.appointments.book(caller, slot_id=body.slotId, service=body.service, key=key)
    if outcome.status == 201:
        AppointmentResponse.model_validate(outcome.body)
    # Replays preserve their stored JSON representation, including timestamps.
    return BookingJSONResponse(status_code=outcome.status, content=outcome.body)


router.include_router(booking_router)


@router.get("/appointments", operation_id="listOwnAppointments", response_model=AppointmentListResponse)
def list_own_appointments(request: Request, caller: Annotated[Caller, Depends(current_customer)]):
    validate_query(request, set())
    return AppointmentListResponse(appointments=list(request.app.state.appointments.list_own(caller)))
