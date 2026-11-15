# BestApp

**Student Name:** Alain Yaacoub  
**Student ID:** 40260582  
**Course:** SOEN387  
**Semester:** Fall 2026

This repository contains the OpenAPI specification and Python validation tests for BestApp, a barbershop appointment-booking API.

## Repository contents

| File | Purpose |
| --- | --- |
| [api/openapi.yaml](api/openapi.yaml) | OpenAPI 3.1 specification, version `0.1.0`, with request/response schemas and examples |
| [api/contract-behavior.md](api/contract-behavior.md) | Design reference for caller permissions, booking boundaries, errors, retry decisions, and required runtime checks |
| [tests/contract/test_openapi.py](tests/contract/test_openapi.py) | Tests for the specification, examples, and schema constraints |
| [requirements-dev.txt](requirements-dev.txt) | Pinned direct dependencies for the validation tools |

The specification describes the API design. The executable code in this repository validates that specification; it does not implement or start an HTTP service.

## Setup

Use Python 3.11. Run these commands from the repository root in PowerShell:

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements-dev.txt
.\.venv\Scripts\python.exe -m pip check
```

Calling the virtual environment's Python directly avoids requiring shell activation or a PowerShell execution-policy change. The `.venv` directory is ignored by Git.

The validation tools use PyYAML, jsonschema, openapi-spec-validator, and pytest. The `tzdata` package supplies the IANA timezone data used by the tests for `America/Toronto`, including on Windows. Direct dependency versions are pinned; transitive dependencies are not fully locked.

## Validate the contract

For a quick check that `api/openapi.yaml` is a valid OpenAPI document, run:

```powershell
.\.venv\Scripts\python.exe -m openapi_spec_validator api/openapi.yaml
```

The tests in `tests/contract/test_openapi.py` run offline against `api/openapi.yaml`. They check the YAML document's structure, schemas, references, examples, and declared constraints. They do not send HTTP requests or contact a live server or database. No running API server is required.

To run the full document and schema test suite:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/contract -q
```

The test suite includes OpenAPI validation, so you can run the pytest command on its own for all document and schema checks. It also checks:

- Local references and declared examples, including date/time formats.
- Operation and authentication declarations in the contract.
- Required fields and rejection of unsupported booking fields.
- Service choices, duration constraints, and consistency of example intervals.
- Response-specific error codes, retry flags, and HTTP status declarations.
- Availability example ordering, distinct slot IDs, and Toronto local dates across timezone transitions.
- Idempotency-key format, response-header declarations, and placeholder credentials.

The suite contains 52 tests. These are specification and schema checks, not evidence that authentication, booking, persistence, concurrency, or request retries work in a running service.

Example credentials are nonfunctional placeholders. Example dates illustrate request and response formats and are not live availability.
