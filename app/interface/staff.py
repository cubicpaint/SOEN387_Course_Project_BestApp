"""Staff HTTP models and translation; scheduling policy stays in services."""

from datetime import date as calendar_date, datetime
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Header, Query, Request
from fastapi.security import HTTPBearer
from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.domain.identity import Caller
from app.domain.staff_availability import AvailabilityRange, date_blocks
from app.domain.types import ServiceChoice
from app.interface.appointments import AppointmentId, BookingJSONResponse, KEY_PATTERN
from app.interface.auth import current_caller
from app.interface.browsing import BarberId, LocalDate, SlotId, UTCDateTime
from app.interface.validation import InputError, JSONRoute, validate_query


REVISION_PATTERN = r"^[0-9a-f]{64}$"
SCHEDULE_MATCH_PATTERN = r'^"[0-9a-f]{64}"$'
OffsetDateTime = Annotated[datetime, Field(json_schema_extra={"pattern": r"[+-][0-9]{2}:[0-9]{2}$"})]


class StaffRange(BaseModel):
    model_config = ConfigDict(extra="forbid")
    startTime: OffsetDateTime
    endTime: OffsetDateTime

    @field_validator("startTime", "endTime", mode="before")
    @classmethod
    def timestamp_string(cls, value):
        if not isinstance(value, str):
            raise ValueError("Availability timestamps must be ISO strings with Toronto offsets.")
        return value


class StaffAvailabilityRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    ranges: list[StaffRange] = Field(max_length=50)


class StaffBlockResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    slotId: SlotId
    startTime: UTCDateTime
    endTime: UTCDateTime
    enabled: bool
    occupied: bool


class StaffAvailabilityResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    barberId: BarberId
    date: LocalDate
    timeZone: Literal["America/Toronto"]
    ranges: list[StaffRange] = Field(max_length=50)
    blocks: list[StaffBlockResponse] = Field(max_length=50)
    revision: str = Field(min_length=64, max_length=64, pattern=REVISION_PATTERN)


class StaffAppointmentResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", json_schema_extra={"allOf": [{
        "if": {"properties": {"service": {"enum": ["haircut", "beard"]}}},
        "then": {"properties": {"durationMinutes": {"const": 30}}},
        "else": {"properties": {"durationMinutes": {"const": 60}}},
    }]})
    id: AppointmentId
    barberId: BarberId
    service: ServiceChoice
    durationMinutes: Literal[30, 60]
    startTime: UTCDateTime
    endTime: UTCDateTime
    status: Literal["confirmed"]
    createdAt: UTCDateTime
    customerEmail: str = Field(min_length=1, max_length=254)


class StaffAppointmentListResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    appointments: list[StaffAppointmentResponse]


class StaffDateQuery(BaseModel):
    model_config = ConfigDict(extra="forbid")
    date: LocalDate

    @field_validator("date")
    @classmethod
    def valid_date(cls, value: str) -> str:
        date_blocks(calendar_date.fromisoformat(value))
        return value


router = APIRouter(tags=["Staff"], dependencies=[
    Depends(HTTPBearer(scheme_name="bearerAuth", bearerFormat="JWT", auto_error=False)),
])
mutation_router = APIRouter(route_class=JSONRoute)


@router.get("/barbers/{barberId}/appointments", operation_id="listStaffAppointments", response_model=StaffAppointmentListResponse)
def list_staff_appointments(request: Request, barberId: BarberId, caller: Annotated[Caller, Depends(current_caller)]):
    validate_query(request, set())
    return StaffAppointmentListResponse(appointments=list(request.app.state.staff.list_appointments(caller, barberId)))


@router.get("/barbers/{barberId}/availability", operation_id="getStaffAvailability", response_model=StaffAvailabilityResponse)
def get_staff_availability(
    request: Request, barberId: BarberId, query: Annotated[StaffDateQuery, Query()], caller: Annotated[Caller, Depends(current_caller)]
):
    validate_query(request, {"date"})
    body = request.app.state.staff.availability(caller, barberId, calendar_date.fromisoformat(query.date))
    StaffAvailabilityResponse.model_validate(body)
    return BookingJSONResponse(content=body, headers={"ETag": '"' + body["revision"] + '"'})


@mutation_router.put("/barbers/{barberId}/availability", operation_id="replaceStaffAvailability", response_model=StaffAvailabilityResponse)
def replace_staff_availability(
    request: Request, barberId: BarberId, query: Annotated[StaffDateQuery, Query()], body: StaffAvailabilityRequest,
    caller: Annotated[Caller, Depends(current_caller)],
    key: Annotated[str, Header(alias="Idempotency-Key", min_length=1, max_length=128, pattern=KEY_PATTERN)],
    match: Annotated[str, Header(alias="If-Match", min_length=66, max_length=66, pattern=SCHEDULE_MATCH_PATTERN)],
):
    validate_query(request, {"date"})
    if len(request.headers.getlist("idempotency-key")) != 1 or len(request.headers.getlist("if-match")) != 1:
        raise InputError(422, "validation_error", "Send exactly one valid Idempotency-Key and If-Match header.")
    try:
        outcome = request.app.state.staff.replace_availability(
            caller, barberId, calendar_date.fromisoformat(query.date),
            tuple(AvailabilityRange(value.startTime, value.endTime) for value in body.ranges), revision=match[1:-1], key=key,
        )
    except ValueError as error:
        raise InputError(422, "validation_error", str(error)) from None
    headers = {}
    if outcome.status == 200:
        StaffAvailabilityResponse.model_validate(outcome.body)
        headers["ETag"] = '"' + outcome.body["revision"] + '"'
    return BookingJSONResponse(status_code=outcome.status, content=outcome.body, headers=headers)


router.include_router(mutation_router)
