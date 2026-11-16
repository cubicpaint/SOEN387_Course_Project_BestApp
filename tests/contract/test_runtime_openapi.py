"""Check the implemented health contract and HTTP responses against the baseline."""

from pathlib import Path

import pytest
import yaml
from fastapi.testclient import TestClient
from fastapi.openapi.utils import get_openapi
from jsonschema import Draft202012Validator, FormatChecker
from openapi_spec_validator import validate

from app.config import Settings
from app.main import create_app
from app.services.health import ReadinessService


CONTRACT = Path(__file__).resolve().parents[2] / "api" / "openapi.yaml"
HEALTH_PATHS = {"/health/live", "/health/ready"}


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
    with TestClient(create_app(settings), raise_server_exceptions=False) as http:
        yield http


def resolve(spec, item):
    while "$ref" in item:
        item_reference = item["$ref"]
        assert item_reference.startswith("#/")
        item = spec
        for part in item_reference[2:].split("/"):
            item = item[part.replace("~1", "/").replace("~0", "~")]
    return item


def validate_response(baseline, path, response):
    declared = resolve(baseline, baseline["paths"][path]["get"]["responses"][str(response.status_code)])
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


def test_runtime_contract_publishes_only_implemented_health_operations(client, baseline):
    response = client.get("/openapi.json")

    assert response.status_code == 200
    runtime = response.json()
    validate(runtime)
    assert runtime["openapi"] == "3.1.0"
    assert set(runtime["paths"]) == HEALTH_PATHS
    assert runtime["paths"] == {path: baseline["paths"][path] for path in HEALTH_PATHS}
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

    assert set(native["paths"]) == HEALTH_PATHS
    for path in HEALTH_PATHS:
        generated = native["paths"][path]["get"]
        authored = baseline["paths"][path]["get"]
        assert generated["operationId"] == authored["operationId"]
        generated_schema = generated["responses"]["200"]["content"]["application/json"]["schema"]
        authored_schema = authored["responses"]["200"]["content"]["application/json"]["schema"]
        assert without_titles(resolve(native, generated_schema)) == resolve(baseline, authored_schema)


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
