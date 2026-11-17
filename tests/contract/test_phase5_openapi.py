"""Validate management representations without weakening the booking baseline."""

from copy import deepcopy
from pathlib import Path

import pytest
import yaml
from jsonschema import Draft202012Validator, FormatChecker, ValidationError


CONTRACT = Path(__file__).resolve().parents[2] / "api" / "openapi.yaml"


@pytest.fixture(scope="module")
def document():
    return yaml.safe_load(CONTRACT.read_text(encoding="utf-8"))


def validator(document, name):
    return Draft202012Validator(
        {"allOf": [{"$ref": "#/components/schemas/" + name}], "components": document["components"]},
        format_checker=FormatChecker(),
    )


@pytest.mark.parametrize("field,value", [("barberId", "bar_other"), ("service", "beard"), ("customerId", "usr_other")])
def test_rescheduling_cannot_change_barber_service_or_customer(document, field, value):
    schema = validator(document, "RescheduleRequest")
    schema.validate({"slotId": "slot_next"})
    with pytest.raises(ValidationError):
        schema.validate({"slotId": "slot_next", field: value})


@pytest.mark.parametrize("invalid", ["1", 'W/"1"', "*", '"0"', '"-1"', '"1", "2"'])
def test_appointment_precondition_requires_one_strong_positive_version(document, invalid):
    schema = validator(document, "VersionMatch")
    schema.validate('"1"')
    with pytest.raises(ValidationError):
        schema.validate(invalid)


@pytest.mark.parametrize("invalid", ["a" * 64, 'W/"' + "a" * 64 + '"', '"' + "A" * 64 + '"', '"abc"', "*"])
def test_staff_precondition_requires_one_strong_revision(document, invalid):
    schema = validator(document, "RevisionMatch")
    schema.validate('"' + "a" * 64 + '"')
    with pytest.raises(ValidationError):
        schema.validate(invalid)


def test_management_status_and_version_do_not_change_original_booking_shape(document):
    schemas = document["components"]["schemas"]
    assert schemas["Appointment"]["properties"]["status"]["const"] == "confirmed"
    assert "version" not in schemas["Appointment"]["properties"]
    assert schemas["Appointment"]["additionalProperties"] is False
    assert schemas["ManagedAppointment"]["properties"]["status"]["enum"] == ["confirmed", "cancelled"]
    assert "version" in schemas["ManagedAppointment"]["required"]


def test_staff_view_contains_contact_email_without_credentials(document):
    schema = document["components"]["schemas"]["StaffAppointment"]
    assert "customerEmail" in schema["required"]
    assert "slotId" not in schema["properties"]
    assert not {"password", "passwordHash", "accessToken", "idempotencyKey"} & set(schema["properties"])


def test_staff_ranges_require_explicit_offsets_and_allow_empty_date_closure(document):
    schema = validator(document, "AvailabilityChangeRequest")
    schema.validate({"ranges": []})
    valid = {"ranges": [{"startTime": "2030-01-15T09:00:00-05:00", "endTime": "2030-01-15T17:00:00-05:00"}]}
    schema.validate(valid)
    missing_offset = deepcopy(valid)
    missing_offset["ranges"][0]["startTime"] = "2030-01-15T09:00:00"
    with pytest.raises(ValidationError):
        schema.validate(missing_offset)


def test_staff_availability_keeps_timezone_and_occupancy_explicit(document):
    schema = document["components"]["schemas"]["StaffAvailability"]
    assert schema["properties"]["timeZone"]["const"] == "America/Toronto"
    assert {"blocks", "ranges", "revision"} <= set(schema["required"])
    block = document["components"]["schemas"]["StaffAvailabilityBlock"]
    assert {"enabled", "occupied", "slotId"} <= set(block["required"])


@pytest.mark.parametrize("method", ["get", "put", "delete"])
def test_management_operations_require_authentication(document, method):
    operation = document["paths"]["/appointments/{appointmentId}"][method]
    assert operation["security"] == [{"bearerAuth": []}]
    assert {"401", "403", "404", "422", "500", "503"} <= set(operation["responses"])
    assert operation["responses"]["200"]["content"]["application/json"]["schema"] == {
        "$ref": "#/components/schemas/ManagedAppointment",
    }


@pytest.mark.parametrize("method", ["put", "delete"])
def test_management_mutations_bind_keys_to_versioned_intents(document, method):
    operation = document["paths"]["/appointments/{appointmentId}"][method]
    headers = {parameter["name"]: parameter for parameter in operation["parameters"] if parameter["in"] == "header"}
    assert set(headers) == {"If-Match", "Idempotency-Key"}
    assert all(header["required"] for header in headers.values())
    assert headers["If-Match"]["schema"] == {"$ref": "#/components/schemas/VersionMatch"}
    assert "412" in operation["responses"]
    assert operation["responses"]["200"]["headers"]["ETag"]["required"] is True


def test_replacement_browsing_cannot_choose_another_barber_or_service(document):
    operation = document["paths"]["/appointments/{appointmentId}/availability"]["get"]
    assert operation["security"] == [{"bearerAuth": []}]
    assert [parameter["name"] for parameter in operation["parameters"] if parameter["in"] == "query"] == ["date"]
    assert "own allocations" in operation["description"]
    assert "no barberId or service" in operation["description"]


def test_cancellation_does_not_advertise_a_request_body(document):
    assert "requestBody" not in document["paths"]["/appointments/{appointmentId}"]["delete"]
