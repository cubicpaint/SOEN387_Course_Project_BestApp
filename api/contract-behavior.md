# API behavior reference

This document explains the implemented milestone behavior specified by [openapi.yaml](openapi.yaml): login, barber listing, availability browsing, atomic booking, own upcoming appointments, and health checks. This reference alone is not runtime verification evidence. The README records workflows and verification, and the OpenAPI document defines request and response schemas.

## Operations and callers

| Operation | Allowed caller | Success |
| --- | --- | --- |
| `POST /auth/login` | Public; credentials must identify a predefined customer or staff account | `200` with a short-lived bearer token and caller information |
| `GET /barbers` | Public | `200` with barbers ordered by display name, then ID; an empty array is valid |
| `GET /availability` | Public | `200` with eligible starts for the requested barber, local date, and service; an empty array is valid |
| `POST /appointments` | Authenticated customer | `201` with the created appointment, or the recorded creation result on replay |
| `GET /appointments` | Authenticated customer | `200` with only that customer's upcoming confirmed appointments, ordered by start time, then ID |
| `GET /health/live` | Public | `200` when the HTTP process responds; no database or provider call |
| `GET /health/ready` | Public | `200` when required application and database/schema dependencies are available |

Public operations ignore `Authorization`; they do not establish a caller from it. Protected operations require exactly one valid `Authorization: Bearer <token>` header. Missing, duplicate, forged, expired, or otherwise invalid bearer authorization returns `401 unauthenticated`. A valid staff identity receives `403 forbidden` on customer-only operations.

Login matches the stored email exactly, including case, and does not trim or normalize either credential. Tokens use the configured issuer, audience, and lifetime (default 1800 seconds, maximum 3600). Trusted identities and staff assignments are reloaded from the database after token verification. Both deployed appointment operations enforce bearer authentication and customer permission, with independent authorization in the application service.

Booking requires exactly one `Idempotency-Key` header; a missing, duplicate, or malformed key returns `422 validation_error`. Unknown or repeated query parameters are rejected. Duplicate JSON object member names are malformed input, including duplicates inside nested objects.

Booking accepts only `slotId` and `service`. The server derives customer identity from the verified caller, barber and start from the slot, and duration from the service. Client-supplied customer, barber, duration, role, and notes fields are rejected. The appointment response confirms its stored state; it does not promise email delivery.

## Time, availability, and interval decisions

| Case | Required decision |
| --- | --- |
| Start less than two elapsed hours after the booking decision time | Reject with `422 booking_notice_violation` |
| Start exactly two elapsed hours after that time | Eligible if all other booking rules pass |
| Start more than two elapsed hours after that time | Eligible if all other booking rules pass |
| One appointment ends exactly when another begins | No overlap |
| Appointment intervals intersect, including appointments with different barbers for one customer | Reject the customer's conflicting booking |
| A 60-minute service has only one free 30-minute block | Reject with `409 slot_unavailable`; claim no partial interval |

Haircut and beard each require one 30-minute block; both requires two consecutive blocks belonging to the same barber. Check and claim the complete interval atomically. Barber conflicts and customer conflicts must remain prevented under simultaneous requests.

Intervals include their start and exclude their end. Two intervals overlap when `a.start < b.end` and `b.start < a.end`. The public availability operation checks barber availability and booking notice; it cannot exclude a particular customer's existing appointments. Booking rechecks both resources using the authenticated customer.

For a new booking decision, acquire the required concurrency protection before sampling the trusted server clock. Sample the decision time once and use that instant for the decision's notice checks. Do not use a client clock, or infer this decision time from the response's `createdAt` value.

Availability queries interpret `date` in `America/Toronto`. The date extends from one local midnight to the next, so daylight-saving transitions can produce 23- or 25-hour dates. Response timestamps are UTC instants ending in `Z`; distinct instants and slot IDs distinguish repeated local clock times. Starts follow the local `:00` / `:30` grid. A known barber with no eligible starts, including a past date, returns `200` with an empty array.

Within a retained database, a slot ID permanently identifies the same barber and base-block interval. It must never be rebound or reused for a different block, even after the original availability is retired.

The agreed customer change cutoff is at least 24 elapsed hours before an existing appointment starts. Before accepting a booking made at least two but less than 24 hours ahead, clients must explain that the customer cannot later cancel or reschedule it online. This contract exposes no change operation.

## Errors and retry decisions

Errors use the documented `error` object with `code`, a safe `message`, and `retryable`. Responses never expose stack traces or secrets. Every response carries a fresh `X-Correlation-ID` and `Cache-Control: no-store`.

| HTTP status | Error code and meaning | Client action |
| --- | --- | --- |
| `400` | `invalid_json`: malformed JSON or duplicate object member names | Correct the body |
| `401` | `invalid_credentials` at login; `unauthenticated` on protected operations | Supply correct credentials or valid bearer authorization |
| `403` | `forbidden`: a valid caller lacks customer permission | Use an authorized account |
| `404` | `barber_not_found` for availability; `slot_not_found` for booking | Refresh the selected resource |
| `409` | `slot_unavailable`, `customer_overlap`, or `idempotency_key_conflict` | Resolve the conflict; do not automatically create a new booking attempt |
| `415` | `unsupported_media_type`: a JSON-body operation received another media type | Send `application/json` |
| `422` | `validation_error`, or booking's `booking_notice_violation` | Correct input or choose an eligible start |
| `500` | `internal_error`: controlled unexpected failure | Bounded retry; booking retains its key and body |
| `503` | `service_unavailable`: temporary dependency or transaction-wait failure; also unready database/schema | Honor `Retry-After`; bounded retry with the same booking key and body |

Only `500` and `503` errors have `retryable: true`. A failed or lost HTTP response does not prove a booking transaction failed. Reuse the original key and body to discover its recorded outcome safely.

Input errors can be rejected before authentication, and multiple input failures have no fixed ordering. For protected operations, authenticate and authorize before evaluating booking business rules, replaying an outcome, or reading customer appointments. If several business rules then fail, any applicable documented business rejection may be returned.

## Idempotency decisions

Keys are opaque, case-sensitive, scoped to the authenticated caller, and bound to the HTTP operation and normalized validated request content. JSON property order and insignificant whitespace do not change that content; string values are not trimmed or case-folded. Different callers have independent key scopes.

| Booking result | Store as a completed outcome? |
| --- | --- |
| `201` appointment created | Yes; commit appointment, complete block allocation, and outcome together |
| `404 slot_not_found` | Yes; no appointment or block changes |
| `409 slot_unavailable` or `409 customer_overlap` | Yes; no appointment or block changes |
| `422 booking_notice_violation` | Yes; no appointment or block changes |
| `400`, `415`, or `422 validation_error`, including a bad key | No; input validation does not consume an otherwise unbound key |
| `401` or `403` | No; no stored outcome is disclosed |
| `409 idempotency_key_conflict` | No new outcome; preserve the first binding and result |
| `500` or `503` | No transient error outcome; retain any earlier completed result |

For each request:

1. Validate the applicable input, bearer authorization, customer permission, and key before replaying a result. Independent validation failures have no fixed global priority.
2. Find or coordinate the caller/key binding. A different normalized request under a bound key receives `409 idempotency_key_conflict` and never overwrites that binding.
3. For a matching completed binding, return the original HTTP status and identical JSON body before checking current availability, customer overlap, or notice. A replay remains valid after its original time boundary passes.
4. For a new decision, acquire the necessary concurrency protection, sample the server decision time once, and evaluate the full requested interval.
5. Persist a completed success or listed business rejection. A rejected or rolled-back booking leaves no appointment or partial block claim.

Concurrent identical requests serialize to one decision and replay its result. If waiting times out, return `503 service_unavailable` so the client can retry the same request. Keep completed bindings and outcomes for the persisted database's lifetime; no automatic expiry is defined.

The first completed outcome is immutable. Correcting malformed input may reuse a still-unbound key. A new attempt after a recorded business rejection requires a new unpredictable key. After an uncertain reply, use the original key and unchanged body rather than guessing whether the appointment exists.

Correlation headers are generated for each HTTP request and are outside the stored JSON body, so a replay can preserve its body while carrying a fresh log identifier.

## Required checks against a running service

The following checks are expected runtime evidence; schema and example validation alone cannot prove them:

- Accept a start exactly two hours after a controlled decision time; reject just inside the boundary, including time spent waiting for concurrency protection.
- Return documented empty arrays for no barbers, no eligible starts, and no own upcoming appointments.
- Reject invalid and duplicate bearer authorization; reject a valid staff caller on customer-only operations. Public operations remain public.
- Reject duplicate JSON members, duplicate idempotency headers, injected booking fields, and unknown or repeated query parameters with the documented errors.
- Reserve both blocks of a 60-minute service atomically under a competing booking; never leave a partial allocation or double-book a barber.
- Prevent concurrent customer overlaps across different barbers while permitting back-to-back intervals.
- Replay identical sequential and simultaneous requests, including after the notice boundary, with one appointment and the original status/body.
- Preserve the first result on conflicting key reuse; permit corrected invalid input to reuse an unbound key, but not replace a recorded business rejection.
- Recover the original booking after a lost reply or uncertain server error by retrying the same key and body; use fresh correlation headers.
- Verify distinct Toronto fallback instants, 23-/25-hour date queries, and permanent slot identity.
- Keep liveness independent of database failure while readiness returns a controlled `503` when the required database/schema is unavailable.
