"""JSON login and a reusable bearer dependency for protected operations."""

from email.headerregistry import Address
from email.errors import MessageError
from typing import Annotated, Literal

from fastapi import APIRouter, Request
from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.domain.identity import Caller
from app.domain.types import Role
from app.interface.validation import JSONRoute, validate_query


router = APIRouter(tags=["Authentication"], route_class=JSONRoute)
UserId = Annotated[str, Field(pattern=r"^usr_[A-Za-z0-9_-]+$", max_length=64)]
BarberId = Annotated[str, Field(pattern=r"^bar_[A-Za-z0-9_-]+$", max_length=64)]


class LoginRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    email: str = Field(max_length=254, json_schema_extra={"format": "email"})
    password: str = Field(min_length=1, max_length=1024, repr=False, json_schema_extra={"writeOnly": True})

    @field_validator("email")
    @classmethod
    def valid_email(cls, value: str) -> str:
        # Syntax only; keep case and whitespace unchanged for exact DB matching.
        try:
            address = Address(addr_spec=value)
            if not address.username or not address.domain or "\r" in value or "\n" in value:
                raise ValueError()
        except (ValueError, IndexError, MessageError):
            raise ValueError("Use an email address.") from None
        return value


class CallerResponse(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        json_schema_extra={"allOf": [{
            "if": {"properties": {"role": {"const": "customer"}}},
            "then": {"properties": {"managedBarberIds": {"maxItems": 0}}},
        }]},
    )
    id: UserId
    role: Role
    managedBarberIds: list[BarberId] = Field(json_schema_extra={"uniqueItems": True})


class LoginResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    accessToken: str = Field(pattern=r"^[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+$")
    tokenType: Literal["Bearer"]
    expiresIn: int = Field(ge=1, le=3600)
    user: CallerResponse


@router.post("/auth/login", operation_id="login", response_model=LoginResponse)
def login(request: Request, credentials: LoginRequest):
    validate_query(request, set())
    result = request.app.state.authentication.login(credentials.email, credentials.password)
    return LoginResponse(
        accessToken=result.access_token,
        tokenType="Bearer",
        expiresIn=result.expires_in,
        user=CallerResponse(
            id=result.caller.id,
            role=result.caller.role,
            managedBarberIds=list(result.caller.managed_barber_ids),
        ),
    )
