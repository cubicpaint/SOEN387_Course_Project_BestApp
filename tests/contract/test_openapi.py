"""Validate the authored contract and its examples without running an API."""

from copy import deepcopy
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
import yaml
from jsonschema import Draft202012Validator, FormatChecker, ValidationError
from openapi_spec_validator import OpenAPIV31SpecValidator


# Load this module's reporting hooks when pytest collects the tests.
pytest_plugins = [__name__]

CONTRACT = Path(__file__).resolve().parents[2] / "api" / "openapi.yaml"
SUMMARY_PROPERTY = "bestapp_contract_summary"
TEST_FILE = Path(__file__)
OUTCOME_PRIORITY = {"PASS": 0, "SKIP": 1, "FAIL": 2}
METHODS = {"get", "post", "put", "patch", "delete", "options", "head", "trace"}
CUSTOMER_OPERATIONS = {("/appointments", "get"), ("/appointments", "post")}
CONTRACT_OPERATIONS = CUSTOMER_OPERATIONS | {
    ("/auth/login", "post"),
    ("/barbers", "get"),
    ("/availability", "get"),
    ("/health/live", "get"),
    ("/health/ready", "get"),
}


def pytest_collection_modifyitems(items):
    for item in items:
        if item.path != TEST_FILE:
            continue
        if hasattr(item, "callspec"):
            description = item.callspec.id
        else:
            description = (item.obj.__doc__ or item.name).strip().splitlines()[0]
        item.user_properties.append((SUMMARY_PROPERTY, description))


def pytest_terminal_summary(terminalreporter):
    results = {}
    for reports in terminalreporter.stats.values():
        for report in reports:
            if not hasattr(report, "when"):
                continue
            description = dict(getattr(report, "user_properties", [])).get(SUMMARY_PROPERTY)
            if description is None:
                continue
            if report.when != "call" and not report.failed and not report.skipped:
                continue
            status = "FAIL" if report.failed else "SKIP" if report.skipped else "PASS"
            previous = results.get(report.nodeid)
            if previous is None or OUTCOME_PRIORITY[status] > OUTCOME_PRIORITY[previous[0]]:
                results[report.nodeid] = (status, description)

    if results:
        terminalreporter.write_sep("=", "Contract test results")
        for status, description in results.values():
            terminalreporter.write_line(f"{status} - {description}")


@pytest.fixture(scope="module")
def spec():
    return yaml.safe_load(CONTRACT.read_text(encoding="utf-8"))


def walk(value, path=()):
    if isinstance(value, dict):
        yield path, value
        for key, child in value.items():
            yield from walk(child, (*path, str(key)))
    elif isinstance(value, list):
        for index, child in enumerate(value):
            yield from walk(child, (*path, str(index)))


def resolve(spec, value):
    while isinstance(value, dict) and "$ref" in value:
        reference = value["$ref"]
        assert reference.startswith("#/"), f"Unexpected external reference: {reference}"
        value = spec
        for part in reference[2:].split("/"):
            value = value[part.replace("~1", "/").replace("~0", "~")]
    return value


def validator(spec, schema):
    # Keep local JSON Pointers relative to the complete components collection.
    root = {"allOf": [schema], "components": spec["components"]}
    return Draft202012Validator(root, format_checker=FormatChecker())


def examples(spec):
    """Yield schema, parameter/header, and request/response media examples."""
    for path, node in walk(spec):
        label = "/".join(path)
        if isinstance(node.get("examples"), list):
            for index, example in enumerate(node["examples"]):
                yield f"{label}/examples/{index}", node, example
        if "schema" not in node:
            continue
        if "example" in node:
            yield f"{label}/example", node["schema"], node["example"]
        if isinstance(node.get("examples"), dict):
            for name, entry in node["examples"].items():
                example = resolve(spec, entry)
                assert "value" in example, f"Example must be local: {label}/{name}"
                yield f"{label}/examples/{name}", node["schema"], example["value"]


def operations(spec):
    for path, item in spec["paths"].items():
        for method, operation in item.items():
            if method in METHODS:
                yield path, method, operation


def test_openapi_document_is_valid(spec):
    """The authored API document is valid OpenAPI 3.1."""
    assert spec["openapi"] == "3.1.0"
    OpenAPIV31SpecValidator(spec).validate()


def test_all_references_resolve_locally(spec):
    """Every contract reference resolves within the same document."""
    references = [node for _, node in walk(spec) if "$ref" in node]
    assert references, "Reference validation must exercise actual references"
    for reference in references:
        resolve(spec, reference)


def test_every_declared_example_matches_its_schema(spec):
    """Every declared example matches its schema and format constraints."""
    declared = list(examples(spec))
    assert declared, "The contract must contain examples"
    for label, schema, example in declared:
        errors = list(validator(spec, schema).iter_errors(example))
        assert not errors, f"{label}: {errors[0].message if errors else ''}"


def test_request_and_response_media_have_examples(spec):
    """Every request and response representation includes a schema and example."""
    for path, method, operation in operations(spec):
        objects = list(operation["responses"].values())
        if "requestBody" in operation:
            objects.append(operation["requestBody"])
        for item in objects:
            for media in resolve(spec, item).get("content", {}).values():
                assert "schema" in media, f"Missing schema: {method} {path}"
                assert "example" in media or media.get("examples"), (
                    f"Missing example: {method} {path}"
                )


def test_only_documented_operations_and_explicit_permissions_are_declared(spec):
    """Only documented operations and their required permissions are declared."""
    actual = {(path, method) for path, method, _ in operations(spec)}
    assert actual == CONTRACT_OPERATIONS
    for path, method, operation in operations(spec):
        expected = [{"bearerAuth": []}] if (path, method) in CUSTOMER_OPERATIONS else []
        assert operation["security"] == expected, f"{method} {path}"
    assert spec["components"]["securitySchemes"]["bearerAuth"]["scheme"] == "bearer"
    assert "503" in spec["paths"]["/health/ready"]["get"]["responses"]


def test_booking_key_and_response_correlation_are_documented(spec):
    """Booking keys, correlation IDs, and authentication challenges are documented."""
    booking = spec["paths"]["/appointments"]["post"]
    parameters = [resolve(spec, item) for item in booking["parameters"]]
    key = next(item for item in parameters if item["name"] == "Idempotency-Key")
    assert key["in"] == "header" and key["required"] is True
    for path, method, operation in operations(spec):
        for status, response in operation["responses"].items():
            headers = resolve(spec, response).get("headers", {})
            names = {name.lower() for name in headers}
            assert "x-correlation-id" in names, (
                f"Missing correlation header: {method} {path}"
            )
            if status == "401":
                assert "www-authenticate" in names, f"Missing 401 challenge: {method} {path}"


@pytest.mark.parametrize(
    "field,value",
    [
        pytest.param("customerId", "usr_999", id="Booking rejects a client-supplied customer ID."),
        pytest.param("barberId", "bar_999", id="Booking rejects a client-supplied barber ID."),
        pytest.param("durationMinutes", 1, id="Booking rejects a client-supplied duration in minutes."),
        pytest.param("duration", 1, id="Booking rejects a client-supplied duration field."),
        pytest.param("role", "staff", id="Booking rejects a client-supplied role."),
        pytest.param(
            "notes", "Reserved for a later compatible contract change",
            id="Booking rejects the future notes field.",
        ),
    ],
)
def test_booking_rejects_untrusted_or_future_fields(spec, field, value):
    """The booking schema rejects untrusted and future request fields."""
    request = {"slotId": "slot_205", "service": "haircut", field: value}
    schema = spec["components"]["schemas"]["BookingRequest"]
    with pytest.raises(ValidationError):
        validator(spec, schema).validate(request)


@pytest.mark.parametrize(
    "payload",
    [
        pytest.param({}, id="Booking rejects an empty request."),
        pytest.param({"slotId": "slot_205"}, id="Booking requires a service choice."),
        pytest.param({"service": "haircut"}, id="Booking requires a starting slot."),
        pytest.param(
            {"slotId": "slot_205", "service": "massage"},
            id="Booking rejects an unsupported service.",
        ),
        pytest.param(
            {"slotId": 205, "service": "haircut"},
            id="Booking rejects a numeric slot ID.",
        ),
    ],
)
def test_booking_rejects_missing_or_invalid_fields(spec, payload):
    """The booking schema rejects missing or invalid input."""
    with pytest.raises(ValidationError):
        validator(spec, spec["components"]["schemas"]["BookingRequest"]).validate(payload)


@pytest.mark.parametrize(
    "service",
    [
        pytest.param("haircut", id="Booking accepts the haircut service."),
        pytest.param("beard", id="Booking accepts the beard service."),
        pytest.param("both", id="Booking accepts the combined service."),
    ],
)
def test_all_agreed_services_are_accepted(spec, service):
    """The booking schema accepts every supported service."""
    schema = spec["components"]["schemas"]["BookingRequest"]
    validator(spec, schema).validate({"slotId": "slot_205", "service": service})


@pytest.mark.parametrize(
    "service,minutes",
    [
        pytest.param("haircut", 30, id="Haircut appointments require 30 minutes."),
        pytest.param("beard", 30, id="Beard appointments require 30 minutes."),
        pytest.param("both", 60, id="Combined appointments require 60 minutes."),
    ],
)
def test_appointment_schema_enforces_service_duration(spec, service, minutes):
    """Appointment schemas enforce the duration of each service."""
    appointment = next(
        deepcopy(item)
        for _, _, example in examples(spec)
        for _, item in walk(example)
        if {"service", "durationMinutes", "startTime", "createdAt"} <= item.keys()
    )
    appointment["service"] = service
    appointment["durationMinutes"] = minutes
    start = datetime.fromisoformat(appointment["startTime"])
    appointment["endTime"] = (start + timedelta(minutes=minutes)).isoformat().replace("+00:00", "Z")
    check = validator(spec, spec["components"]["schemas"]["Appointment"])
    check.validate(appointment)
    appointment["durationMinutes"] = 60 if minutes == 30 else 30
    with pytest.raises(ValidationError):
        check.validate(appointment)


@pytest.mark.parametrize(
    "name,value",
    [
        pytest.param(
            "UTCDateTime", "2026-11-17T15:00:00",
            id="UTC timestamps reject missing timezone information.",
        ),
        pytest.param(
            "UTCDateTime", "2026-11-17T10:00:00-05:00",
            id="UTC timestamps reject non-UTC offsets.",
        ),
        pytest.param(
            "UTCDateTime", "2026-02-30T15:00:00Z",
            id="UTC timestamps reject impossible calendar dates.",
        ),
        pytest.param(
            "LocalDate", "2026-02-30",
            id="Date queries reject impossible calendar dates.",
        ),
        pytest.param(
            "LocalDate", "2026-11-17T15:00:00Z",
            id="Date queries reject full timestamps.",
        ),
        pytest.param("IdempotencyKey", "", id="Idempotency keys reject empty values."),
        pytest.param(
            "IdempotencyKey", "contains spaces",
            id="Idempotency keys reject spaces.",
        ),
        pytest.param(
            "IdempotencyKey", "non-ascii-\u00e9",
            id="Idempotency keys reject non-ASCII characters.",
        ),
        pytest.param(
            "IdempotencyKey", "a" * 129,
            id="Idempotency keys reject values longer than 128 characters.",
        ),
    ],
)
def test_invalid_dates_times_and_keys_are_rejected(spec, name, value):
    """Schemas reject invalid dates, timestamps, and idempotency keys."""
    with pytest.raises(ValidationError):
        validator(spec, spec["components"]["schemas"][name]).validate(value)


def test_duration_and_time_examples_are_consistent(spec):
    """Example appointment times match service durations and the booking grid."""
    checked = 0
    durations = {"haircut": 30, "beard": 30, "both": 60}
    toronto = ZoneInfo("America/Toronto")
    for label, _, example in examples(spec):
        for _, item in walk(example):
            if "service" not in item or "durationMinutes" not in item:
                continue
            duration = durations[item["service"]]
            assert item["durationMinutes"] == duration, label
            intervals = item.get("slots", [item])
            for interval in intervals:
                start = datetime.fromisoformat(interval["startTime"])
                end = datetime.fromisoformat(interval["endTime"])
                assert end - start == timedelta(minutes=duration), label
                local = start.astimezone(toronto)
                assert local.minute in (0, 30) and local.second == 0, label
                checked += 1
    assert checked, "Duration checks must exercise actual interval examples"


def test_credential_examples_are_explicit_placeholders(spec):
    """Credential examples use explicit placeholder values."""
    seen = set()
    for label, _, example in examples(spec):
        for _, item in walk(example):
            if "password" in item:
                assert "REPLACE_" in item["password"], label
                seen.add("password")
            if "accessToken" in item:
                assert item["accessToken"].lower().startswith("example"), label
                seen.add("accessToken")
    assert seen == {"password", "accessToken"}


ERROR_CODES = {
    "invalid_json", "invalid_credentials", "unauthenticated", "forbidden",
    "barber_not_found", "slot_not_found", "slot_unavailable", "customer_overlap",
    "idempotency_key_conflict", "unsupported_media_type", "validation_error",
    "booking_notice_violation", "internal_error", "service_unavailable",
}


@pytest.mark.parametrize(
    "response_name,http_status,allowed_codes,retryable",
    [
        pytest.param(
            "InvalidJson", "400", {"invalid_json"}, False,
            id="Invalid JSON responses allow only their documented error and forbid automatic retries.",
        ),
        pytest.param(
            "InvalidCredentials", "401", {"invalid_credentials"}, False,
            id="Invalid credential responses allow only their documented error and forbid automatic retries.",
        ),
        pytest.param(
            "Unauthenticated", "401", {"unauthenticated"}, False,
            id="Unauthenticated responses allow only their documented error and forbid automatic retries.",
        ),
        pytest.param(
            "Forbidden", "403", {"forbidden"}, False,
            id="Forbidden responses allow only their documented error and forbid automatic retries.",
        ),
        pytest.param(
            "BarberNotFound", "404", {"barber_not_found"}, False,
            id="Missing barber responses allow only their documented error and forbid automatic retries.",
        ),
        pytest.param(
            "SlotNotFound", "404", {"slot_not_found"}, False,
            id="Missing slot responses allow only their documented error and forbid automatic retries.",
        ),
        pytest.param(
            "BookingConflict", "409",
            {"slot_unavailable", "customer_overlap", "idempotency_key_conflict"}, False,
            id="Booking conflict responses restrict their error codes and forbid automatic retries.",
        ),
        pytest.param(
            "UnsupportedMediaType", "415", {"unsupported_media_type"}, False,
            id="Unsupported media responses allow only their documented error and forbid automatic retries.",
        ),
        pytest.param(
            "ValidationError", "422", {"validation_error"}, False,
            id="Validation responses allow only their documented error and forbid automatic retries.",
        ),
        pytest.param(
            "BookingUnprocessable", "422",
            {"validation_error", "booking_notice_violation"}, False,
            id="Unprocessable booking responses restrict their error codes and forbid automatic retries.",
        ),
        pytest.param(
            "InternalError", "500", {"internal_error"}, True,
            id="Internal failure responses allow only their documented error and permit retries.",
        ),
        pytest.param(
            "ServiceUnavailable", "503", {"service_unavailable"}, True,
            id="Unavailable service responses allow only their documented error and permit retries.",
        ),
    ],
)
def test_error_responses_restrict_codes_retryability_and_http_status(
    spec, response_name, http_status, allowed_codes, retryable
):
    """Each error response enforces its codes, retry advice, and HTTP status."""
    schema = spec["components"]["responses"][response_name]["content"]["application/json"]["schema"]
    check = validator(spec, schema)
    for code in allowed_codes:
        payload = {"error": {"code": code, "message": "Example error.", "retryable": retryable}}
        check.validate(payload)
        payload["error"]["retryable"] = not retryable
        with pytest.raises(ValidationError):
            check.validate(payload)
    for code in ERROR_CODES - allowed_codes:
        payload = {"error": {"code": code, "message": "Example error.", "retryable": retryable}}
        with pytest.raises(ValidationError):
            check.validate(payload)
    reference = f"#/components/responses/{response_name}"
    statuses = {
        status
        for _, _, operation in operations(spec)
        for status, response in operation["responses"].items()
        if response.get("$ref") == reference
    }
    assert statuses == {http_status}


@pytest.mark.parametrize(
    "service,minutes",
    [
        pytest.param("haircut", 30, id="Haircut availability requires a 30-minute duration."),
        pytest.param("beard", 30, id="Beard availability requires a 30-minute duration."),
        pytest.param("both", 60, id="Combined availability requires a 60-minute duration."),
    ],
)
def test_availability_schema_enforces_service_duration(spec, service, minutes):
    """Availability schemas enforce the duration of each service."""
    availability = next(
        deepcopy(item)
        for _, _, example in examples(spec)
        for _, item in walk(example)
        if {"barberId", "date", "timeZone", "service", "durationMinutes", "slots"} <= item.keys()
    )
    availability["service"] = service
    availability["durationMinutes"] = minutes
    for slot in availability["slots"]:
        start = datetime.fromisoformat(slot["startTime"])
        slot["endTime"] = (start + timedelta(minutes=minutes)).isoformat().replace("+00:00", "Z")
    check = validator(spec, spec["components"]["schemas"]["AvailabilityResponse"])
    check.validate(availability)
    availability["durationMinutes"] = 60 if minutes == 30 else 30
    with pytest.raises(ValidationError):
        check.validate(availability)


def test_availability_examples_use_the_requested_local_date_and_order(spec):
    """Availability examples contain ordered, distinct slots on the requested Toronto date."""
    checked = 0
    toronto = ZoneInfo("America/Toronto")
    for label, _, example in examples(spec):
        for _, item in walk(example):
            if not {"date", "timeZone", "slots"} <= item.keys():
                continue
            starts = []
            slot_ids = []
            for slot in item["slots"]:
                start = datetime.fromisoformat(slot["startTime"])
                assert start.astimezone(toronto).date().isoformat() == item["date"], label
                starts.append(start)
                slot_ids.append(slot["slotId"])
                checked += 1
            assert starts == sorted(starts), label
            assert len(slot_ids) == len(set(slot_ids)), label
    assert checked, "Availability checks must exercise actual slot examples"


@pytest.mark.parametrize(
    "length",
    [
        pytest.param(1, id="Idempotency keys accept one character."),
        pytest.param(128, id="Idempotency keys accept 128 characters."),
    ],
)
def test_idempotency_key_length_boundaries_are_accepted(spec, length):
    """Idempotency keys accept both documented length boundaries."""
    validator(spec, spec["components"]["schemas"]["IdempotencyKey"]).validate("a" * length)
