# BestApp

**Student Name:** Alain Yaacoub  
**Student ID:** 40260582  
**Course:** SOEN387  
**Semester:** Fall 2026

BestApp is a barbershop appointment-booking project. Phase 2 provides the HTTP runtime, PostgreSQL schema, migrations, health checks, structured logs, and explicit demo provisioning.

Only the two health operations are implemented. Login, availability browsing, booking, appointment management, frontend workflows, and notifications remain later-phase work.

## Repository contents

| File | Purpose |
| --- | --- |
| [api/openapi.yaml](api/openapi.yaml) | OpenAPI 3.1 specification, version `0.1.0`, with request/response schemas and examples |
| [api/contract-behavior.md](api/contract-behavior.md) | Design reference for caller permissions, booking boundaries, errors, retry decisions, and required runtime checks |
| [tests/contract/test_openapi.py](tests/contract/test_openapi.py) | Tests for the specification, examples, and schema constraints |
| [requirements-dev.txt](requirements-dev.txt) | Development installation and pinned contract/HTTP test tools |
| [pyproject.toml](pyproject.toml) | Application metadata and pinned direct runtime dependencies |
| [app/](app/) | HTTP interface, services, domain types, and SQLAlchemy data access |
| [migrations/](migrations/) | Explicit Alembic migration history; initial revision `0001_core` |
| [compose.yaml](compose.yaml) | PostgreSQL, a one-off migration job, API, and opt-in provisioning command |
| [scripts/provision_demo.py](scripts/provision_demo.py) | Add demo users, barbers, staff assignments, and future availability |
| [scripts/verify_phase2_runtime.py](scripts/verify_phase2_runtime.py) | Verify live health behavior, restart persistence, and database-outage recovery |

The seven-operation authored specification remains the milestone design baseline. The running service publishes a subset at `/openapi.json` and `/docs` containing only the implemented health operations. Publishing that subset does not establish conformance for unimplemented operations.

## Start with Docker Compose

Install Docker with Compose and use Linux containers. Run commands from this repository root; no files from a parent workspace are required.

Copy the placeholder configuration:

```powershell
Copy-Item .env.example .env
```

Edit `.env`: replace both database occurrences of `replace_me` with the same randomly generated URL-safe database password. Use letters, digits, `_`, and `-` so the password is safe inside the connection URL. Set `JWT_SECRET` to a separate private secret generated from at least 32 random bytes, for example with Python's `secrets.token_urlsafe(32)`. Keep `.env` private and uncommitted. There are no working credentials in `.env.example`.

```powershell
docker compose up -d --build api
docker compose ps -a
Invoke-RestMethod http://localhost:8000/health/live
Invoke-RestMethod http://localhost:8000/health/ready
```

Startup waits for healthy PostgreSQL, runs `alembic upgrade head` in a one-off migration container, then starts the API. A failed migration prevents API startup. The API itself neither creates tables nor provisions data. The container runs as a non-root user with FastAPI debug disabled and without Uvicorn reload mode.

The images use Python 3.11.18 and PostgreSQL 17.12. Host ports 8000 and 5432 bind to localhost. If either is already occupied, stop the conflicting local service before starting this stack. If Docker is not found in an older terminal, reopen the terminal after installation.

```powershell
docker compose logs api migrate
docker compose down
docker compose up -d api
```

The `postgres_data` named volume retains data when containers stop or are recreated. **`docker compose down -v` deletes that database volume.** Changing `POSTGRES_PASSWORD` in `.env` does not change a password already stored in an initialized PostgreSQL volume.

For a manual migration or revision check:

```powershell
docker compose run --rm migrate
docker compose run --rm migrate python -m alembic current
```

## Configuration

| Setting | Purpose |
| --- | --- |
| `POSTGRES_USER`, `POSTGRES_PASSWORD`, `POSTGRES_DB` | Compose database initialization and the container connection URL |
| `DATABASE_URL` | Required by host Python commands; `postgresql+psycopg://user:password@localhost:5432/database` |
| `DB_CONNECT_TIMEOUT` | Connection timeout in seconds; default 3, range 1–30 |
| `DB_QUERY_TIMEOUT_MS` | Database statement timeout in milliseconds; default 2000, range 1–30000 |
| `DB_POOL_TIMEOUT` | Connection-pool wait in seconds; default 3, range 1–30 |
| `JWT_SECRET` | Required at API startup; separate private random signing secret of at least 32 bytes; placeholders are rejected |
| `JWT_ISSUER` | Expected token issuer; default `bestapp` |
| `JWT_AUDIENCE` | Expected token audience; default `bestapp-api` |
| `JWT_TTL_SECONDS` | Token lifetime in seconds; default 1800, range 1–3600 |
| `BESTAPP_DEMO_PASSWORD` | Optional password for new demo users; Compose reads it from `.env` or the process environment, local provisioning reads the process environment; otherwise prompted privately |

Host Python settings read `.env`; process environment values take precedence. Compose constructs its own `DATABASE_URL` using the `db` hostname. Use matching credentials in both places. Missing/invalid required application configuration stops startup with a field-name diagnostic, without printing its value. JWT settings are validated only when the API is constructed; database migrations and demo provisioning do not require a signing secret. Email-provider settings remain later-phase work.

Tokens use HS256 with an explicitly allowed algorithm and required signature, issuer, audience, subject, issued-at time, and expiration checks. The signing secret is never generated again at API startup: retain the same private configuration across restarts. Changing it invalidates existing tokens.

## Provision demonstration data

After startup, run the explicit command and enter a private password of at least 12 characters:

```powershell
docker compose run --rm provision
```

For noninteractive provisioning, supply `BESTAPP_DEMO_PASSWORD` through the process environment or the ignored `.env` file. Do not pass passwords as command-line arguments. The command prints public account identifiers and counts, never passwords or hashes.

| Demo email | Role and assignment |
| --- | --- |
| `customer.one@example.com` | Customer |
| `customer.two@example.com` | Customer |
| `shop.staff@example.com` | Staff assigned to every current barber |
| `barber.staff@example.com` | Staff assigned to demo barber Alex |

The command creates demo barbers Alex and Sam, plus 30-minute blocks from 09:00 to 17:00 on the next seven Toronto dates (224 blocks for two barbers). It converts those local intervals to UTC instants. These are demonstration records; Phase 2 has no login or availability HTTP operation.

Provisioning runs in one transaction. Rerunning it reuses existing users and slots, preserves password hashes, disabled blocks, versions, and bookings, and adds missing future blocks. It does not reset passwords or move existing slot identities. If an existing demo identity conflicts with the required setup, it reports a controlled failure and rolls back.

## Local Python development

Use Python 3.11. Run these commands from the repository root in PowerShell:

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements-dev.txt
.\.venv\Scripts\python.exe -m pip check
```

Calling the virtual environment's Python directly avoids requiring shell activation or a PowerShell execution-policy change. The `.venv` directory is ignored by Git.

Direct dependencies are pinned in `pyproject.toml` and `requirements-dev.txt`; transitive dependencies are not fully locked. The `tzdata` package supplies `America/Toronto` timezone data, including on Windows.

To use PostgreSQL in Docker while running the API locally:

```powershell
docker compose up -d --wait db
.\.venv\Scripts\python.exe -m alembic upgrade head
.\.venv\Scripts\python.exe -m uvicorn app.main:create_app --factory --host 127.0.0.1 --port 8000 --no-access-log
```

Stop the Compose API first if it already occupies port 8000. For local provisioning, run `.\.venv\Scripts\python.exe -m scripts.provision_demo`; its optional password environment variable comes from the process environment, otherwise it prompts privately.

## Layers and durable data

`app/interface` handles HTTP validation and response translation. `app/services` coordinates readiness and demo provisioning, including provisioning's transaction boundary. `app/domain` contains plain Python value types. `app/data` contains SQLAlchemy mappings, session construction, database probes, and the focused demo repository. `app/main.py` connects these components. Handlers contain no booking rules.

The first migration creates `users`, `barbers`, `staff_barbers`, `availability_blocks`, `appointments`, `slot_allocations`, and `idempotency_records`. Foreign keys, service/duration checks, unique barber/start intervals, unique active block allocations, and caller-scoped key uniqueness provide foundational integrity. Timestamps are timezone-aware PostgreSQL values; slot IDs remain attached to their original barber/interval.

Version columns and allocation/idempotency tables prepare for later use cases. They do not yet implement optimistic booking writes, customer-overlap locking, or replay behavior. Outbox data and optional notes are reserved for later genuine migrations.

## Health, errors, and logs

| Operation | Behavior |
| --- | --- |
| `GET /health/live` | `200 {"status":"alive"}`; no database or provider call |
| `GET /health/ready` | `200 {"status":"ready"}` when PostgreSQL, revision `0001_core`, and required tables/columns are present; otherwise controlled `503` |

Both operations are public and ignore authorization headers. Query parameters are rejected with `422 validation_error`. Responses carry a fresh UUID `X-Correlation-ID` and `Cache-Control: no-store`. Dependency failures use the contract's `service_unavailable` error with `retryable: true` and `Retry-After: 5`; unexpected failures return a safe `500 internal_error` without a traceback.

Application request logs are JSON records containing an event, correlation ID, method, route, HTTP status, and elapsed time. They exclude request bodies, query values, authorization headers, passwords, and database URLs. Uvicorn's separate access log is disabled by the documented run commands.

To demonstrate an outage after startup:

```powershell
docker compose stop db
curl.exe -i http://localhost:8000/health/live
curl.exe -i http://localhost:8000/health/ready
docker compose start db
```

Liveness stays `200`, readiness returns `503`, and readiness recovers when PostgreSQL is available again.

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

The original authored-contract suite contains 52 tests. Additional runtime-contract tests check the health-only published schema, native response-model consistency, and HTTP health responses. None establishes that authentication, booking, concurrency, or booking retries work.

Example credentials are nonfunctional placeholders. Example dates illustrate request and response formats and are not live availability.

## Test the Phase 2 foundation

Run checks that do not require PostgreSQL:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/unit tests/contract -q
```

For real PostgreSQL integration checks, wait for `db` and create a separate test database once:

```powershell
docker compose up -d --wait db
docker compose exec db sh -c 'createdb -U "$POSTGRES_USER" bestapp_test'
```

Set `TEST_DATABASE_URL` using the same local credentials as `DATABASE_URL`, but with database name `bestapp_test`. For example, after copying the actual private connection URL into a PowerShell variable:

```powershell
$env:TEST_DATABASE_URL = 'postgresql+psycopg://bestapp:YOUR_PRIVATE_PASSWORD@localhost:5432/bestapp_test'
.\.venv\Scripts\python.exe -m pytest tests -q
Remove-Item Env:TEST_DATABASE_URL
```

Integration fixtures require a database name ending in `_test`. Each test creates, migrates, and finally removes its own randomly named schema, without resetting application tables. Tests cover migrations/model consistency, database constraints, rollback, schema readiness, and safe additive provisioning. Without `TEST_DATABASE_URL`, these tests explicitly skip; a passing offline suite is not PostgreSQL evidence.

For the running-container acceptance check:

```powershell
.\.venv\Scripts\python.exe -m scripts.verify_phase2_runtime
```

Run this separately from the integration suite. It builds/starts this Compose stack, checks live health responses and the published contract, inserts a uniquely named temporary barber, verifies it survives restarts and container recreation with the same volume, and briefly stops PostgreSQL to verify liveness/readiness and recovery. It then removes only its own temporary record and leaves the API/database running. Existing application data is retained.

Phase 2 verification completed on November 15, 2026: all 118 cases passed (98 unit/contract checks and 20 real PostgreSQL integration checks), alongside the running-container restart, persistence, outage, and recovery checks. This evidence covers the foundation and health operations; later business workflows remain unimplemented.
