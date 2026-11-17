# BestApp

**Student Name:** Alain Yaacoub  
**Student ID:** 40260582  
**Course:** SOEN387  
**Semester:** Fall 2026

BestApp is a barbershop appointment-booking project. Customers can log in, browse service-aware availability, book a haircut, beard service, or both, and view their own upcoming appointments through a small browser interface or the API.

The runtime includes PostgreSQL, migrations, health checks, structured logs, demo provisioning, authenticated booking, atomic block allocation, and durable booking retries. Phase 4's booking and milestone-interface criteria are verified. Cancellation, rescheduling, staff management, email notifications, and reminders remain later-phase work.

Phase 5 implementation is approved and in progress. Rescheduling will change time only: the original barber and service remain fixed. Staff availability edits will reject the entire change if it would remove an occupied block. Appointment changes, staff routes, and their interface will be documented as implemented after verification; emails and reminders remain later phases.

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
| [scripts/verify_phase3_runtime.py](scripts/verify_phase3_runtime.py) | Verify live login, public browsing, occupied intervals, and contracted errors using temporary fixtures |
| [scripts/phase4_probe.py](scripts/phase4_probe.py) | Shared temporary fixtures, authenticated HTTP requests, and response-contract checks for Phase 4 scripts |
| [scripts/verify_phase4_runtime.py](scripts/verify_phase4_runtime.py) | Verify booking, durable outcomes, ownership, and live conformance; optional restart/recreation checks |
| [scripts/demo_booking_concurrency.py](scripts/demo_booking_concurrency.py) | Demonstrate authenticated contention and simultaneous retries against the running API |
| [app/static/](app/static/) | Packaged HTML, JavaScript, and CSS for the browser workflow |

The authored specification and running service publish all seven milestone operations across six API paths at `/openapi.json` and `/docs`: login, barber listing, availability, appointment creation, own-appointment listing, and the two health checks. The browser page at `/` and its fixed static assets are outside the OpenAPI operation list. Native FastAPI request/response models, bearer-security declarations, and HTTP responses are checked against the authored contract; publication alone does not prove runtime behavior.

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

The command creates demo barbers Alex and Sam, plus 30-minute blocks from 09:00 to 17:00 on the next seven Toronto dates (224 blocks for two barbers). It converts those local intervals to UTC instants.

Provisioning runs in one transaction. Rerunning it reuses existing users and slots, preserves password hashes, disabled blocks, versions, and bookings, and adds missing future blocks. It does not reset passwords or move existing slot identities. If an existing demo identity conflicts with the required setup, it reports a controlled failure and rolls back.

## Log in

Send `POST /auth/login` with `Content-Type: application/json` and an object containing `email` and `password`. Use a provisioned account and its privately supplied demo password. Email matching is exact and case-sensitive; input is neither trimmed nor normalized. The interactive `/docs` request form can send the JSON body.

A successful response contains `accessToken`, `tokenType: "Bearer"`, `expiresIn`, and `user` (ID, role, and managed barber IDs). Unknown email and incorrect password both return `401 invalid_credentials` with the same message. Staff can log in. Public operations ignore authorization headers. No refresh-token or registration operation is provided.

The reusable bearer dependency verifies the token and reloads current database identity and staff assignments. Service-level guards enforce customer role, ownership, and staff/barber permissions independently of HTTP. Booking and own-appointment listing require a verified customer: missing, invalid, or repeated authorization returns `401`, and a valid staff caller receives `403` before any booking outcome or appointment list is disclosed.

Malformed or duplicate JSON members return `400`; unsupported media returns `415`; invalid fields or query parameters return `422`. Passwords, hashes, and tokens are excluded from application logs. The browser keeps its bearer token only in memory and clears the password field after submitting login.

## Browse barbers and availability

`GET /barbers` returns public `id` and `displayName` values, ordered by display name and then ID. An empty shop returns `{"barbers": []}`.

`GET /availability?barberId=bar_demo_alex&date=2026-11-17&service=both` illustrates the query shape; replace the date with one of the future Toronto dates provisioned on your run. Required service choices are `haircut`, `beard` (30 minutes each), or `both` (60 minutes).

The response includes `barberId`, `date`, `timeZone`, `service`, `durationMinutes`, and `slots`. Each slot has the first block's stable `slotId`, UTC `startTime`, and the entire service's UTC `endTime`, ending in `Z`. A combined service needs two consecutive enabled, unallocated blocks for the same barber. Occupied, disabled, past, too-soon, incomplete, and off-grid starts are excluded. Exactly two elapsed hours of notice is eligible.

Dates mean `America/Toronto`, from one local midnight to the next. Daylight-saving dates may contain 23 or 25 hours; repeated local times remain distinct UTC instants and slot IDs. A start near midnight may use a continuation block on the next local date. Results are sorted by UTC start time. A known barber without eligible starts returns `200` with `slots: []`; an unknown barber returns `404 barber_not_found`. There is no additional booking horizon.

Both operations are public and ignore authorization headers. Browsing does not reserve blocks or filter a particular customer's existing appointments. Unknown/repeated query parameters and invalid values return `422 validation_error`; database outages return controlled `503` errors. Booking independently rechecks the complete interval and the authenticated customer's existing appointments inside its transaction.

## Book and view appointments

Send `POST /appointments` with exactly one `Authorization: Bearer <token>` header, exactly one `Idempotency-Key` header, and `Content-Type: application/json`. Its body contains only `slotId` and `service`, for example `{"slotId":"slot_example","service":"both"}` using a currently browsable slot. The key is case-sensitive, 1-128 characters, and matches `^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$`; generate a new unpredictable key for each new booking intent. The server derives the customer, barber, start, and duration. Extra customer, role, barber, duration, or notes fields are rejected.

A successful request returns `201` with the appointment and its full 30- or 60-minute interval. A combined service allocates two consecutive blocks together. Competing barber allocations return `409 slot_unavailable`; overlapping appointments for the same customer, including different barbers, return `409 customer_overlap`. Intervals include their start and exclude their end, so back-to-back appointments are allowed. A start inside the two-hour notice boundary returns `422 booking_notice_violation`; a missing starting slot returns `404 slot_not_found`.

Retry an uncertain booking with the **same key and unchanged body**. Matching sequential and simultaneous retries return the original HTTP status and JSON result, including after the original notice boundary passes. Booking responses serialize JSON keys deterministically so the first response and its stored replay also have identical raw body bytes; each HTTP request still receives a fresh correlation header. Changed validated content under an already bound key returns `409 idempotency_key_conflict`. Each caller has an independent key scope. Successful creations and the listed booking business rejections are retained for the database's lifetime; input and authorization failures do not consume an unbound key. A new intent after a recorded rejection requires a new key. Dependency failures return retryable `503` with `Retry-After`; unexpected failures return safe retryable `500`. An uncertain response can follow a committed transaction, so neither error justifies switching keys.

`GET /appointments` requires the same customer authorization and accepts no filters. It returns only that customer's confirmed appointments whose start is greater than or equal to the trusted server time, ordered by start and ID. An empty list is valid. Responses contain the full service interval and first block's `slotId`. Confirmation records a persisted appointment; it does not indicate that an email was sent.

## Browser interface

Open `http://localhost:8000/` after starting the API and provisioning demo accounts. The same-origin page provides login/logout, barber/service/date selection, available starts, booking confirmation, retrying a pending booking, and refreshing own appointments. Times display in `America/Toronto` with offsets to distinguish repeated daylight-saving clock times. The warning before confirmation explains that bookings less than 24 hours ahead cannot later be cancelled or rescheduled online. Online appointment changes are not implemented yet.

Tokens stay in page memory and are cleared on logout or reload. Only pending-booking metadata (customer ID, original body, retry key, and display summary) is kept in tab-scoped `sessionStorage`; passwords and tokens are not stored there. After reload or token expiry, log in as the same customer to recover a pending result. Other customers do not see that pending selection. While a result is uncertain, the page preserves the original selection/key and offers **Retry the same booking** instead of creating another intent. Keep the tab open until the result is resolved; if storage is unavailable, a reload cannot preserve its pending state.

The page uses plain HTML, CSS, and JavaScript served by FastAPI. Its assets are included in the installed Python package and Docker image. No frontend build, Node runtime, new backend dependency, or schema migration is required for Phase 4.

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

`app/interface` validates HTTP shapes/media/headers, serves the minimal page, and translates service results. `app/services` coordinates authentication, authorization, browsing, booking, own appointments, readiness, and provisioning. `app/domain` contains trusted caller/value types and pure duration, interval, notice, and timezone rules. `app/data` contains SQLAlchemy mappings, sessions, database probes, and focused repositories, including appointments and idempotency. `app/main.py` connects them. SQL and scheduling rules remain outside handlers; services own transactions.

The first migration creates `users`, `barbers`, `staff_barbers`, `availability_blocks`, `appointments`, `slot_allocations`, and `idempotency_records`. Foreign keys, service/duration checks, unique barber/start intervals, unique active block allocations, and caller-scoped key uniqueness provide foundational integrity. Timestamps are timezone-aware PostgreSQL values; slot IDs remain attached to their original barber/interval.

Booking uses the existing schema in one explicitly `READ COMMITTED` transaction. It first locks the customer's user row, serializing that customer's booking attempts across all barbers and keys. A fresh statement after acquiring that lock sees the preceding transaction's completed outcome and appointments. Each required block is claimed with a conditional update that checks its version, enabled state, and lack of allocation, then increments its version. Blocks are claimed in time order; another customer's stale claim fails instead of overwriting a winner. The unique allocation primary key remains a final database guard.

The booking decision runs inside a savepoint. If either block claim, notice, or customer-overlap check fails, that savepoint rolls back every claim and version change before the outer transaction stores the business rejection. For success, the appointment, all allocations, and the completed idempotency response commit together. A transient or unexpected failure rolls back the unfinished transaction rather than recording a transient outcome; retrying the original key discovers any outcome that committed before its reply was lost. The trusted decision clock is sampled once after customer and block waits. Outbox data and optional notes remain reserved for later genuine migrations; only `0001_core` is currently required.

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

The original authored-contract suite contains 52 tests. Additional runtime-contract checks compare native login, browsing, health, and appointment models, query/header constraints, and bearer-security declarations with the authored specification. They validate the seven-operation published contract and actual HTTP success/error bodies and headers using the ASGI test client. These use controlled dependencies; separate unit, real PostgreSQL, and live acceptance checks provide authentication, booking, concurrency, rollback, and retry evidence.

Example credentials are nonfunctional placeholders. Example dates illustrate request and response formats and are not live availability.

## Test the application

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

Integration fixtures require a database name ending in `_test`. Each test creates, migrates, and finally removes its own randomly named schema, without resetting application tables. Tests cover migrations/model consistency, database constraints and isolation, rollback, readiness, provisioning, real login/current permissions, availability, all booking services, ownership, sequential/concurrent retries, and competing transactions. Fault-injection checks verify no partial allocations or version changes survive rejection or persistence failure. Event/barrier checks exercise notice boundaries around customer/block protection; a separate genuine PostgreSQL lock-timeout check verifies retryable failures do not bind a key. Without `TEST_DATABASE_URL`, these tests explicitly skip; a passing offline suite is not PostgreSQL evidence.

The isolated unsafe control deliberately omits the customer lock and synchronizes two overlap reads. It demonstrates two overlapping customer appointments across different barbers with the existing schema; it does not claim to bypass the allocation primary key and double-sell one block. The protected counterpart allows one booking. Run it only against the configured test database:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/integration/test_booking_concurrency.py -k unsafe -q
```

Optional JavaScript state checks use an installed `node` executable through a small DOM/fetch substitute. They cover lost replies, same-key retry, pending-state restoration, account switches, stale availability/list responses, token expiry, and login while browsing. They are not full-browser automation. Without Node those cases explicitly skip, while Python asset/HTML/package checks still run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/unit/test_frontend.py -q
```

For the running-container acceptance check:

```powershell
.\.venv\Scripts\python.exe -m scripts.verify_phase2_runtime
```

Run this separately from the integration suite. It builds/starts this Compose stack, checks live health responses and the published contract, inserts a uniquely named temporary barber, verifies it survives restarts and container recreation with the same volume, and briefly stops PostgreSQL to verify liveness/readiness and recovery. It then removes only its own temporary record and leaves the API/database running. Existing application data is retained.

Phase 2 verification completed on November 15, 2026: all 118 cases passed (98 unit/contract checks and 20 real PostgreSQL integration checks), alongside the running-container restart, persistence, outage, and recovery checks. That record covers the foundation and health operations; Phase 3 extends the evidence with authentication and browsing checks below.

For Phase 3 live HTTP acceptance, start the API with the current build and run:

```powershell
docker compose up -d --build api
.\.venv\Scripts\python.exe -m scripts.verify_phase3_runtime
```

This command needs the host development dependencies and the same database/JWT configuration as the API. It inserts a uniquely named temporary customer, barber, four blocks, and an occupied interval; checks real login/token issuance, public browsing, complete service durations, and negative responses against OpenAPI; then removes only its own fixtures. It preserves demonstration data and never prints passwords or tokens. Run acceptance commands separately from PostgreSQL integration tests because the Phase 2 command restarts the database.

Token tests cover signature/algorithm/required claims, issuer/audience, exact expiry, and malformed/duplicate bearer headers. Service tests reject unauthorized callers without HTTP routing. Availability checks cover occupied/disabled/missing blocks, the exact two-hour boundary, UTC/local date differences, midnight continuation, both daylight-saving changes, stable slot IDs, and calendar limits.

Phase 3 verification completed on November 16, 2026: **311 tests passed**, comprising 274 unit/contract checks and 37 real PostgreSQL integration checks. OpenAPI validation and `pip check` passed. The rebuilt container passed both acceptance commands: persistence/restart/outage recovery and real HTTP login/browsing/error conformance. Temporary verification records were removed and the API/database were left running. One existing, nonfailing Starlette TestClient deprecation warning remains.

That Phase 3 record predates booking and the browser workflow. Phase 4 now exercises real protected appointment routes in addition to the earlier test-only authorization routes. Appointment changes and notification integration remain unimplemented.

For current Phase 4 acceptance and the authenticated concurrency demonstration:

```powershell
docker compose up -d --build api
.\.venv\Scripts\python.exe -m scripts.verify_phase4_runtime
.\.venv\Scripts\python.exe -m scripts.demo_booking_concurrency
```

Both scripts require the host development dependencies, a running current API, and matching private database/JWT configuration. Their shared `phase4_probe` creates uniquely named temporary customers, a staff identity, barbers, and blocks; it logs in through HTTP, validates responses against OpenAPI, and removes only its own fixtures. They preserve provisioned demo data and do not print passwords, tokens, or keys. The concurrency script uses independent authenticated HTTP requests and checks both responses and committed row counts for same-slot competition, 60/30-minute competition, cross-barber customer overlap, identical retries, and conflicting retries. Its bounded start barrier launches competitors together; isolated integration barriers provide the more controlled race reproduction.

To also verify appointments, allocations, and recorded outcomes across API/database restarts and container recreation with the retained named volume:

```powershell
.\.venv\Scripts\python.exe -m scripts.verify_phase4_runtime --restart
```

Run restart/outage acceptance separately from integration tests or other demos, because those checks restart shared containers. No script enables unsafe behavior in the deployed API.

The rebuilt container passed the default Phase 4 live acceptance command and all five authenticated concurrency demonstration scenarios. Real headless Edge checks exercised the visible page: login, combined-service availability, the 24-hour warning, discarding a real committed `201` reply, retrying with the original key/body, own listing, second-customer privacy, staff feedback, and logout. Database checks confirmed one appointment, two allocations, and one outcome for that retry workflow. Temporary verification fixtures were cleaned up. The optional Node state checks remain useful narrower checks, separate from this real-browser evidence.

Phase 4 verification on November 16, 2026 covered **444 unique passing tests: 363 unit/contract checks and 81 real PostgreSQL cases**. The full regression run passed 443 tests in 598.17 seconds before the additional isolation regression was collected; that new PostgreSQL case then passed alongside three already-counted database-settings checks, and collection confirms 444 cases. OpenAPI validation and `pip check` passed. After a stricter raw-response comparison exposed JSONB object-key reordering, deterministic booking serialization fixed the reproduced real HTTP/database failure; all 83 affected HTTP, native-contract, and real-database checks passed after the correction. The existing Starlette TestClient deprecation warning remains nonfailing.

The foundation acceptance command passed retained-data/revision checks across restart and container recreation, then an actual database outage with liveness `200`, readiness `503`, and recovery. Phase 4 acceptance with `--restart` also passed appointment, full-allocation, and immutable-outcome persistence across both restart and recreation. The final image was rebuilt after the serialization correction and passed Phase 4 restart/recreation acceptance again; a live raw-body check confirmed byte-identical creation/replay responses with fresh correlation IDs and one complete appointment. Earlier login/browsing acceptance also passed. Temporary verification records and isolated test schemas were removed, and the API/database were left healthy. Phase 4 is complete; the milestone demonstration is prepared. This does not claim the presentation has occurred.

## Five-step milestone demonstration

1. Start the current Compose build, check `/health/ready`, and run `docker compose run --rm provision`. Open `http://localhost:8000/` and log in as `customer.one@example.com` with the private provisioned password.
2. Choose a future Toronto date, barber, and **Haircut and beard**. Browse starts, read the change-cutoff warning, and confirm. Show the 60-minute appointment in the customer's upcoming list.
3. Log out and log in as `customer.two@example.com`. Show that the first customer's appointment is absent. Use `/docs` or the acceptance script to demonstrate a protected operation rejecting missing/forged authorization or a valid staff account.
4. Run `.\.venv\Scripts\python.exe -m scripts.demo_booking_concurrency` to show one winner under contention, full-duration allocation, identical replay, and conflicting key reuse. With `TEST_DATABASE_URL` configured, run the isolated `-k unsafe` command above to contrast the reproduced customer-overlap race with the protected API behavior.
5. Run `.\.venv\Scripts\python.exe -m scripts.verify_phase4_runtime --restart` to demonstrate persisted appointments and outcomes, then refresh the browser list. Identify cancellation, rescheduling, staff management, emails, and reminders as later work.
