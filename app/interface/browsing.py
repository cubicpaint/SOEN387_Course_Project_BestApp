"""Public browsing routes: validate HTTP input and translate service results."""

from datetime import date as calendar_date, datetime
from typing import Annotated, Literal

from fastapi import APIRouter, Query, Request
from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.domain.types import ServiceChoice
from app.interface.validation import validate_query


router = APIRouter()
BarberId = Annotated[str, Field(pattern=r"^bar_[A-Za-z0-9_-]+$", max_length=64)]
SlotId = Annotated[str, Field(pattern=r"^slot_[A-Za-z0-9_-]+$", max_length=64)]
LocalDate = Annotated[str, Field(pattern=r"^\d{4}-\d{2}-\d{2}$", json_schema_extra={"format": "date"})]
UTCDateTime = Annotated[datetime, Field(json_schema_extra={"pattern": "Z$"})]


class Barber(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: BarberId
    displayName: str = Field(min_length=1, max_length=100)


class BarberListResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    barbers: list[Barber]


class AvailabilityQuery(BaseModel):
    model_config = ConfigDict(extra="forbid")
    barberId: BarberId
    date: LocalDate
    service: ServiceChoice

    @field_validator("date")
    @classmethod
    def valid_calendar_date(cls, value: str) -> str:
        calendar_date.fromisoformat(value)
        return value


class AvailableStart(BaseModel):
    model_config = ConfigDict(extra="forbid")
    slotId: SlotId
    startTime: UTCDateTime
    endTime: UTCDateTime


class AvailabilityResponse(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        json_schema_extra={
            "allOf": [{
                "if": {"properties": {"service": {"enum": ["haircut", "beard"]}}},
                "then": {"properties": {"durationMinutes": {"const": 30}}},
                "else": {"properties": {"durationMinutes": {"const": 60}}},
            }],
        },
    )
    barberId: BarberId
    date: LocalDate
    timeZone: Literal["America/Toronto"]
    service: ServiceChoice
    durationMinutes: Literal[30, 60]
    slots: list[AvailableStart]


@router.get("/barbers", operation_id="listBarbers", tags=["Barbers"], response_model=BarberListResponse)
def list_barbers(request: Request):
    validate_query(request, set())
    return BarberListResponse(
        barbers=[Barber(id=barber.id, displayName=barber.display_name) for barber in request.app.state.browsing.list_barbers()]
    )


@router.get("/availability", operation_id="listAvailability", tags=["Availability"], response_model=AvailabilityResponse)
def list_availability(request: Request, query: Annotated[AvailabilityQuery, Query()]):
    validate_query(request, {"barberId", "date", "service"})
    result = request.app.state.browsing.list_availability(query.barberId, calendar_date.fromisoformat(query.date), query.service)
    return AvailabilityResponse(
        barberId=result.barber_id,
        date=result.local_date.isoformat(),
        timeZone="America/Toronto",
        service=result.service,
        durationMinutes=result.duration_minutes,
        slots=[AvailableStart(slotId=slot.slot_id, startTime=slot.starts_at, endTime=slot.ends_at) for slot in result.slots],
    )
