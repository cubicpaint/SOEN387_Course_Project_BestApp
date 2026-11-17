"""Versioned owner-only appointment management HTTP representations."""

from datetime import date as calendar_date
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Header, Query, Request
from fastapi.routing import APIRoute
from fastapi.security import HTTPBearer
from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.domain.appointments import BookingOutcome
from app.domain.identity import Caller
from app.interface.appointments import AppointmentId, AppointmentResponse, BookingJSONResponse, KEY_PATTERN, current_customer
from app.interface.browsing import AvailabilityResponse, LocalDate, SlotId
from app.interface.validation import InputError, JSONRoute, validate_query


VERSION_MATCH_PATTERN = r'^"[1-9][0-9]*"$'


class ManagedAppointment(AppointmentResponse):
    status: Literal["confirmed", "cancelled"]
    version: int = Field(ge=1, strict=True)


class RescheduleRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    slotId: SlotId


class ReplacementQuery(BaseModel):
    model_config = ConfigDict(extra="forbid")
    date: LocalDate

    @field_validator("date")
    @classmethod
    def valid_calendar_date(cls, value: str) -> str:
        calendar_date.fromisoformat(value)
        return value


class BodylessRoute(APIRoute):
    def get_route_handler(self):
        handler = super().get_route_handler()

        async def validated(request: Request):
            if await request.body():
                raise InputError(422, "validation_error", "This operation does not accept a request body.")
            return await handler(request)

        return validated


def managed_response(outcome: BookingOutcome):
    headers = {}
    if outcome.status == 200:
        ManagedAppointment.model_validate(outcome.body)
        headers["ETag"] = f'"{outcome.body["version"]}"'
    return BookingJSONResponse(status_code=outcome.status, content=outcome.body, headers=headers)


def mutation_version(request: Request, match: str) -> int:
    if len(request.headers.getlist("idempotency-key")) != 1 or len(request.headers.getlist("if-match")) != 1:
        raise InputError(422, "validation_error", "Send exactly one valid Idempotency-Key and If-Match header.")
    try:
        return int(match[1:-1])
    except ValueError:
        raise InputError(422, "validation_error", "The appointment version is invalid.") from None


router = APIRouter(tags=["Appointments"], dependencies=[
    Depends(HTTPBearer(scheme_name="bearerAuth", bearerFormat="JWT", auto_error=False)),
])
mutation_router = APIRouter(route_class=JSONRoute)
cancel_router = APIRouter(route_class=BodylessRoute)


@router.get("/appointments/{appointmentId}", operation_id="getAppointment", response_model=ManagedAppointment)
def get_appointment(request: Request, appointmentId: AppointmentId, caller: Annotated[Caller, Depends(current_customer)]):
    validate_query(request, set())
    return managed_response(request.app.state.appointments.detail(caller, appointmentId))


@router.get("/appointments/{appointmentId}/availability", operation_id="listReplacementAvailability", response_model=AvailabilityResponse)
def replacement_availability(
    request: Request, appointmentId: AppointmentId, query: Annotated[ReplacementQuery, Query()],
    caller: Annotated[Caller, Depends(current_customer)],
):
    validate_query(request, {"date"})
    outcome = request.app.state.appointments.replacement_availability(caller, appointmentId, calendar_date.fromisoformat(query.date))
    if outcome.status == 200:
        AvailabilityResponse.model_validate(outcome.body)
    return BookingJSONResponse(status_code=outcome.status, content=outcome.body)


@mutation_router.put("/appointments/{appointmentId}", operation_id="rescheduleAppointment", response_model=ManagedAppointment)
def reschedule_appointment(
    request: Request, appointmentId: AppointmentId, body: RescheduleRequest,
    caller: Annotated[Caller, Depends(current_customer)],
    key: Annotated[str, Header(alias="Idempotency-Key", min_length=1, max_length=128, pattern=KEY_PATTERN)],
    match: Annotated[str, Header(alias="If-Match", pattern=VERSION_MATCH_PATTERN)],
):
    validate_query(request, set())
    version = mutation_version(request, match)
    return managed_response(request.app.state.appointments.reschedule(
        caller, appointmentId, slot_id=body.slotId, expected_version=version, key=key,
    ))


@cancel_router.delete("/appointments/{appointmentId}", operation_id="cancelAppointment", response_model=ManagedAppointment)
def cancel_appointment(
    request: Request, appointmentId: AppointmentId, caller: Annotated[Caller, Depends(current_customer)],
    key: Annotated[str, Header(alias="Idempotency-Key", min_length=1, max_length=128, pattern=KEY_PATTERN)],
    match: Annotated[str, Header(alias="If-Match", pattern=VERSION_MATCH_PATTERN)],
):
    validate_query(request, set())
    version = mutation_version(request, match)
    return managed_response(request.app.state.appointments.cancel(caller, appointmentId, expected_version=version, key=key))


router.include_router(mutation_router)
router.include_router(cancel_router)
