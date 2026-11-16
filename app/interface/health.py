"""The only implemented business API operations in Phase 2."""

from typing import Literal

from fastapi import APIRouter, Request
from pydantic import BaseModel, ConfigDict

from app.interface.errors import error_response

router = APIRouter(tags=["Health"])


class LivenessResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    status: Literal["alive"]


class ReadinessResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    status: Literal["ready"]


@router.get("/health/live", operation_id="checkLiveness", response_model=LivenessResponse)
def check_liveness(request: Request):
    if request.query_params:
        return error_response(422, "validation_error", "Query parameters are not supported.")
    return LivenessResponse(status="alive")


@router.get("/health/ready", operation_id="checkReadiness", response_model=ReadinessResponse)
def check_readiness(request: Request):
    if request.query_params:
        return error_response(422, "validation_error", "Query parameters are not supported.")
    if not request.app.state.readiness.is_ready():
        return error_response(503, "service_unavailable", "The service is temporarily unavailable.", retryable=True)
    return ReadinessResponse(status="ready")
