"""Appointment HTTP representations matching the authored milestone contract."""

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field

from app.domain.types import ServiceChoice
from app.interface.browsing import BarberId, SlotId, UTCDateTime


AppointmentId = Annotated[str, Field(pattern=r"^apt_[A-Za-z0-9_-]+$", max_length=64)]
KEY_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$"


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
