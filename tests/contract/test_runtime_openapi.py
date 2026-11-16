"""Check implemented schemas and HTTP responses against the authored contract."""

from pathlib import Path
from datetime import UTC, date, datetime
from unittest.mock import Mock

import pytest
import yaml
from fastapi.testclient import TestClient
from fastapi.openapi.utils import get_openapi
from jsonschema import Draft202012Validator, FormatChecker
from openapi_spec_validator import validate

from app.config import AuthSettings, Settings
from app.main import create_app
from app.domain.availability import AvailabilityResult, AvailableStart, BarberSummary
from app.domain.appointments import BookingOutcome
from app.domain.identity import Caller
from app.domain.types import Role, ServiceChoice
from app.services.authentication import LoginResult
from app.services.errors import BarberNotFound, Forbidden, InvalidCredentials, ServiceUnavailable, Unauthenticated
from app.services.health import ReadinessService


CONTRACT = Path(__file__).resolve().parents[2] / "api" / "openapi.yaml"
HEALTH_PATHS = {"/health/live", "/health/ready"}
IMPLEMENTED_PATHS = HEALTH_PATHS | {"/auth/login", "/barbers", "/availability", "/appointments"}
APPOINTMENT = {
    "id": "apt_one", "barberId": "bar_one", "slotId": "slot_one", "service": "both",
    "durationMinutes": 60, "startTime": "2030-01-15T15:00:00Z", "endTime": "2030-01-15T16:00:00Z",
    "status": "confirmed", "createdAt": "2030-01-14T15:00:00.123456Z",
}


@pytest.fixture(scope="module")
def baseline():
    return yaml.safe_load(CONTRACT.read_text(encoding="utf-8"))


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(ReadinessService, "is_ready", lambda self: True)
    settings = Settings(
        DATABASE_URL="postgresql+psycopg://bestapp:contract-test-secret@localhost:5432/bestapp",
        _env_file=None,
    )
    auth = AuthSettings(JWT_SECRET="private-contract-test-signing-key-only-12345", _env_file=None)
    with TestClient(create_app(settings, auth), raise_server_exceptions=False) as http:
        http.app.state.authentication = Mock()
        http.app.state.authentication.login.return_value = LoginResult("header.payload.signature", 1800, Caller("usr_one", Role.CUSTOMER))
        http.app.state.authentication.authenticate.return_value = Caller("usr_one", Role.CUSTOMER)
        http.app.state.appointments = Mock()
        http.app.state.appointments.book.return_value = BookingOutcome(201, APPOINTMENT)
        http.app.state.appointments.list_own.return_value = (APPOINTMENT,)
        http.app.state.browsing = Mock()
        http.app.state.browsing.list_barbers.return_value = (BarberSummary("bar_one", "Alex"),)
        http.app.state.browsing.list_availability.return_value = AvailabilityResult(
            "bar_one", date(2026, 11, 17), ServiceChoice.BOTH, 60,
            (AvailableStart("slot_one", datetime(2026, 11, 17, 15, tzinfo=UTC), datetime(2026, 11, 17, 16, tzinfo=UTC)),),
        )
        yield http


def resolve(spec, item):
    while "$ref" in item:
        item_reference = item["$ref"]
        assert item_reference.startswith("#/")
        item = spec
        for part in item_reference[2:].split("/"):
            item = item[part.replace("~1", "/").replace("~0", "~")]
    return item


def validate_response(baseline, path, response, method="get"):
    declared = resolve(baseline, baseline["paths"][path][method]["responses"][str(response.status_code)])
    schema = declared["content"]["application/json"]["schema"]
    validator = Draft202012Validator(
        {"allOf": [schema], "components": baseline["components"]},
        format_checker=FormatChecker(),
    )
    validator.validate(response.json())
    assert response.headers["content-type"].startswith("application/json")
    for name, header_reference in declared["headers"].items():
        header = resolve(baseline, header_reference)
        assert name in response.headers
        value = response.headers[name]
        if header["schema"].get("type") == "integer":
            value = int(value)
        Draft202012Validator(header["schema"], format_checker=FormatChecker()).validate(value)


def test_runtime_contract_publishes_only_implemented_operations(client, baseline):
    response = client.get("/openapi.json")

    assert response.status_code == 200
    runtime = response.json()
    validate(runtime)
    assert runtime["openapi"] == "3.1.0"
    assert set(runtime["paths"]) == IMPLEMENTED_PATHS
    assert runtime["paths"] == {path: baseline["paths"][path] for path in IMPLEMENTED_PATHS}
    for name in ("LivenessResponse", "ReadinessResponse", "Error"):
        assert runtime["components"]["schemas"][name] == baseline["components"]["schemas"][name]
    for name in ("ValidationError", "InternalError", "ServiceUnavailable"):
        assert runtime["components"]["responses"][name] == baseline["components"]["responses"][name]


def test_native_health_response_models_match_the_authored_schemas(client, baseline):
    native = get_openapi(title="BestApp API", version="0.1.0", routes=client.app.routes)

    def without_titles(value):
        if isinstance(value, dict):
            return {key: without_titles(item) for key, item in value.items() if key != "title"}
        if isinstance(value, list):
            return [without_titles(item) for item in value]
        return value

    assert set(native["paths"]) == IMPLEMENTED_PATHS
    for path in HEALTH_PATHS:
        generated = native["paths"][path]["get"]
        authored = baseline["paths"][path]["get"]
        assert generated["operationId"] == authored["operationId"]
        generated_schema = generated["responses"]["200"]["content"]["application/json"]["schema"]
        authored_schema = authored["responses"]["200"]["content"]["application/json"]["schema"]
        assert without_titles(resolve(native, generated_schema)) == resolve(baseline, authored_schema)


def schema_constraints(spec, value):
    """Compare validation constraints after expanding refs and removing prose."""
    if isinstance(value, dict):
        value = resolve(spec, value)
        return {
            key: schema_constraints(spec, item)
            for key, item in value.items()
            if key not in {"title", "description", "examples", "example"}
        }
    if isinstance(value, list):
        return [schema_constraints(spec, item) for item in value]
    return value


def test_native_login_models_match_the_authored_contract(client, baseline):
    native = get_openapi(title="BestApp API", version="0.1.0", routes=client.app.routes)
    generated = native["paths"]["/auth/login"]["post"]
    authored = baseline["paths"]["/auth/login"]["post"]
    assert generated["operationId"] == authored["operationId"]
    assert schema_constraints(native, generated["requestBody"]) == schema_constraints(baseline, authored["requestBody"])
    generated_schema = generated["responses"]["200"]["content"]["application/json"]["schema"]
    authored_schema = authored["responses"]["200"]["content"]["application/json"]["schema"]
    assert schema_constraints(native, generated_schema) == schema_constraints(baseline, authored_schema)


def test_native_appointment_models_headers_and_security_match_authored_contract(client, baseline):
    native = get_openapi(title="BestApp API", version="0.1.0", routes=client.app.routes)
    assert schema_constraints(native, native["components"]["securitySchemes"]["bearerAuth"]) == schema_constraints(
        baseline, baseline["components"]["securitySchemes"]["bearerAuth"],
    )
    for method, status in (("post", "201"), ("get", "200")):
        generated = native["paths"]["/appointments"][method]
        authored = baseline["paths"]["/appointments"][method]
        assert generated["operationId"] == authored["operationId"]
        assert generated["security"] == authored["security"]
        generated_schema = generated["responses"][status]["content"]["application/json"]["schema"]
        authored_schema = authored["responses"][status]["content"]["application/json"]["schema"]
        assert schema_constraints(native, generated_schema) == schema_constraints(baseline, authored_schema)
        generated_parameters = generated.get("parameters", [])
        authored_parameters = authored.get("parameters", [])
        assert len(generated_parameters) == len(authored_parameters)
        for actual, expected in zip(generated_parameters, authored_parameters):
            assert (actual["name"], actual["in"], actual["required"]) == (expected["name"], expected["in"], expected["required"])
            assert schema_constraints(native, actual["schema"]) == schema_constraints(baseline, expected["schema"])
    assert schema_constraints(native, native["paths"]["/appointments"]["post"]["requestBody"]) == schema_constraints(
        baseline, baseline["paths"]["/appointments"]["post"]["requestBody"],
    )


@pytest.mark.parametrize("method", ["get", "post"])
def test_appointment_success_responses_conform(client, baseline, method):
    response = client.request(method, "/appointments", headers={"Authorization": "Bearer token", "Idempotency-Key": "one"},
                              **({"json": {"slotId": "slot_one", "service": "both"}} if method == "post" else {}))
    assert response.status_code == (201 if method == "post" else 200)
    validate_response(baseline, "/appointments", response, method)


@pytest.mark.parametrize("status,code", [
    (404, "slot_not_found"), (409, "slot_unavailable"), (409, "customer_overlap"),
    (409, "idempotency_key_conflict"), (422, "booking_notice_violation"),
])
def test_recorded_booking_rejection_responses_conform(client, baseline, status, code):
    client.app.state.appointments.book.return_value = BookingOutcome(status, {
        "error": {"code": code, "message": "A safe business rejection.", "retryable": False},
    })
    response = client.post("/appointments", json={"slotId": "slot_one", "service": "both"},
                           headers={"Authorization": "Bearer token", "Idempotency-Key": "one"})
    assert response.status_code == status
    validate_response(baseline, "/appointments", response, "post")


@pytest.mark.parametrize("method", ["get", "post"])
@pytest.mark.parametrize("failure,status", [(Unauthenticated(), 401), (Forbidden(), 403),
                                           (ServiceUnavailable(), 503), (RuntimeError("private details"), 500)])
def test_appointment_failure_responses_conform(client, baseline, method, failure, status):
    operation = client.app.state.appointments.book if method == "post" else client.app.state.appointments.list_own
    operation.side_effect = failure
    response = client.request(method, "/appointments", headers={"Authorization": "Bearer token", "Idempotency-Key": "one"},
                              **({"json": {"slotId": "slot_one", "service": "both"}} if method == "post" else {}))
    assert response.status_code == status
    validate_response(baseline, "/appointments", response, method)


@pytest.mark.parametrize("body,media,status", [
    ("{", "application/json", 400), ("{}", "text/plain", 415), ("{}", "application/json", 422),
])
def test_appointment_input_error_responses_conform(client, baseline, body, media, status):
    response = client.post("/appointments", content=body, headers={
        "Content-Type": media, "Authorization": "Bearer token", "Idempotency-Key": "one",
    })
    assert response.status_code == status
    validate_response(baseline, "/appointments", response, "post")


@pytest.mark.parametrize("path", ["/barbers", "/availability"])
def test_native_browsing_schemas_and_queries_match_the_authored_contract(client, baseline, path):
    native = get_openapi(title="BestApp API", version="0.1.0", routes=client.app.routes)
    generated = native["paths"][path]["get"]
    authored = baseline["paths"][path]["get"]
    assert generated["operationId"] == authored["operationId"]
    generated_schema = generated["responses"]["200"]["content"]["application/json"]["schema"]
    authored_schema = authored["responses"]["200"]["content"]["application/json"]["schema"]
    assert schema_constraints(native, generated_schema) == schema_constraints(baseline, authored_schema)
    generated_parameters = {p["name"]: p for p in generated.get("parameters", [])}
    authored_parameters = {p["name"]: p for p in authored.get("parameters", [])}
    assert generated_parameters.keys() == authored_parameters.keys()
    for name, parameter in generated_parameters.items():
        expected = authored_parameters[name]
        assert (parameter["in"], parameter["required"]) == (expected["in"], expected["required"])
        assert schema_constraints(native, parameter["schema"]) == schema_constraints(baseline, expected["schema"])


@pytest.mark.parametrize("method,path,body", [
    ("post", "/auth/login", {"email": "customer@example.test", "password": "private test password"}),
    ("get", "/barbers", None),
    ("get", "/availability?barberId=bar_one&date=2026-11-17&service=both", None),
])
def test_login_and_browsing_success_http_responses_conform(client, baseline, method, path, body):
    response = client.request(method, path, json=body) if body is not None else client.get(path)
    assert response.status_code == 200
    validate_response(baseline, path.split("?")[0], response, method)


@pytest.mark.parametrize("path,component,operation,failure,status", [
    ("/auth/login", "authentication", "login", InvalidCredentials(), 401),
    ("/auth/login", "authentication", "login", ServiceUnavailable(), 503),
    ("/barbers", "browsing", "list_barbers", ServiceUnavailable(), 503),
    ("/availability", "browsing", "list_availability", BarberNotFound(), 404),
    ("/availability", "browsing", "list_availability", ServiceUnavailable(), 503),
    ("/availability", "browsing", "list_availability", RuntimeError("private error"), 500),
])
def test_login_and_browsing_service_errors_conform(client, baseline, path, component, operation, failure, status):
    getattr(getattr(client.app.state, component), operation).side_effect = failure
    if path == "/auth/login":
        response = client.post(path, json={"email": "customer@example.test", "password": "private test password"})
        method = "post"
    else:
        query = "?barberId=bar_one&date=2026-11-17&service=both" if path == "/availability" else ""
        response = client.get(path + query)
        method = "get"
    assert response.status_code == status
    validate_response(baseline, path, response, method)


@pytest.mark.parametrize("body,media,status", [
    ("{", "application/json", 400),
    ('{"email":"one@example.test","email":"two@example.test"}', "application/json", 400),
    ("{}", "text/plain", 415),
    ("{}", "application/json", 422),
])
def test_login_input_errors_conform(client, baseline, body, media, status):
    response = client.post("/auth/login", content=body, headers={"Content-Type": media})
    assert response.status_code == status
    validate_response(baseline, "/auth/login", response, "post")


@pytest.mark.parametrize("path", sorted(HEALTH_PATHS))
def test_successful_health_http_responses_conform_to_authored_contract(client, baseline, path):
    response = client.get(path)

    assert response.status_code == 200
    validate_response(baseline, path, response)


@pytest.mark.parametrize("path", sorted(HEALTH_PATHS))
def test_invalid_health_query_responses_conform_to_authored_contract(client, baseline, path):
    response = client.get(path + "?unknown=value")

    assert response.status_code == 422
    validate_response(baseline, path, response)


def test_unavailable_readiness_response_conforms_to_authored_contract(client, baseline, monkeypatch):
    monkeypatch.setattr(ReadinessService, "is_ready", lambda self: False)

    response = client.get("/health/ready")

    assert response.status_code == 503
    validate_response(baseline, "/health/ready", response)


def test_unexpected_health_error_conforms_to_authored_contract(client, baseline, monkeypatch):
    def fail(self):
        raise RuntimeError("A failure used only by this test")

    monkeypatch.setattr(ReadinessService, "is_ready", fail)

    response = client.get("/health/ready")

    assert response.status_code == 500
    validate_response(baseline, "/health/ready", response)
