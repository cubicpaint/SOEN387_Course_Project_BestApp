"""Public barber listing."""

from typing import Annotated
from fastapi import APIRouter, Request
from pydantic import BaseModel, ConfigDict, Field
from app.interface.validation import validate_query

router = APIRouter()
BarberId = Annotated[str, Field(pattern=r"^bar_[A-Za-z0-9_-]+$", max_length=64)]


class Barber(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: BarberId
    displayName: str = Field(min_length=1, max_length=100)



class BarberListResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    barbers: list[Barber]



@router.get("/barbers", operation_id="listBarbers", tags=["Barbers"], response_model=BarberListResponse)
def list_barbers(request: Request):
    validate_query(request, set())
    return BarberListResponse(
        barbers=[Barber(id=barber.id, displayName=barber.display_name) for barber in request.app.state.browsing.list_barbers()]
    )
