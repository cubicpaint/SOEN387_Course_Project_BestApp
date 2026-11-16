"""Application construction; no migrations or demo data are run on HTTP startup."""

from contextlib import asynccontextmanager
from copy import deepcopy
from pathlib import Path

import yaml
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError

from app.config import AuthSettings, Settings, load_auth_settings, load_settings
from app.data.database import create_engine_from_settings, create_session_factory
from app.interface.errors import error_response
from app.interface.health import router
from app.interface.middleware import RequestContextMiddleware
from app.interface.validation import InputError
from app.logging import configure_logging
from app.services.health import ReadinessService
from app.services.errors import BarberNotFound, Forbidden, InvalidCredentials, ServiceUnavailable, Unauthenticated


def runtime_openapi() -> dict:
    source = Path(__file__).resolve().parents[1] / "api" / "openapi.yaml"
    document = deepcopy(yaml.safe_load(source.read_text(encoding="utf-8")))
    document["paths"] = {path: document["paths"][path] for path in ("/health/live", "/health/ready")}
    document["info"]["description"] = "Implemented Phase 2 health operations. Other milestone operations remain design-only."
    document["x-status"] = "phase-2-health-implemented"
    document["servers"] = [{"url": "/", "description": "Current service"}]
    document["tags"] = [{"name": "Health"}]
    return document


def create_app(settings: Settings | None = None, auth_settings: AuthSettings | None = None) -> FastAPI:
    settings = settings if settings is not None else load_settings()
    auth_settings = auth_settings if auth_settings is not None else load_auth_settings()
    configure_logging()
    engine = create_engine_from_settings(settings)

    @asynccontextmanager
    async def lifespan(application: FastAPI):
        yield
        engine.dispose()

    application = FastAPI(title="BestApp API", version="0.1.0", debug=False, lifespan=lifespan)
    application.state.settings = settings
    application.state.auth_settings = auth_settings
    application.state.engine = engine
    application.state.readiness = ReadinessService(engine)
    application.add_middleware(RequestContextMiddleware)
    application.include_router(router)

    @application.exception_handler(InputError)
    async def invalid_http_input(request: Request, error: InputError):
        return error_response(error.status, error.code, error.message)

    failures = {
        InvalidCredentials: (401, "invalid_credentials", "The email or password is incorrect."),
        Unauthenticated: (401, "unauthenticated", "Valid bearer authorization is required."),
        Forbidden: (403, "forbidden", "You are not permitted to perform this action."),
        BarberNotFound: (404, "barber_not_found", "The barber was not found."),
        ServiceUnavailable: (503, "service_unavailable", "The service is temporarily unavailable."),
    }

    async def use_case_failure(request: Request, error: Exception):
        status, code, message = failures[type(error)]
        return error_response(status, code, message, retryable=status == 503)

    for error_type in failures:
        application.add_exception_handler(error_type, use_case_failure)

    @application.exception_handler(RequestValidationError)
    async def invalid_input(request: Request, error: RequestValidationError):
        return error_response(422, "validation_error", "The request is invalid.")

    application.openapi = runtime_openapi
    return application
