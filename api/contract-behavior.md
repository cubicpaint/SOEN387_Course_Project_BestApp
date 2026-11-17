# API behavior reference

This document explains the implemented booking and Phase 5 management behavior specified by [openapi.yaml](openapi.yaml). The README records verification evidence; this reference alone is not runtime verification evidence. The OpenAPI document defines request and response schemas.

## Operations and callers

| Operation | Allowed caller | Success |
| --- | --- | --- |
| `POST /auth/login` | Public; credentials must identify a predefined customer or staff account | `200` with a short-lived bearer token and caller information |
| `GET /barbers` | Public | `200` with barbers ordered by display name, then ID; an empty array is valid |
| `GET /availability` | Public | `200` with eligible starts for the requested barber, local date, and service; an empty array is valid |
| `POST /appointments` | Authenticated customer | `201` with the created appointment, or the recorded creation result on replay |
| `GET /appointments` | Authenticated customer | `200` with only that customer's upcoming confirmed appointments, ordered by start time, then ID |
| `GET /appointments/{appointmentId}` | Authenticated owner | `200` with current schedule, confirmed/cancelled status, version, and strong ETag |
| `PUT /appointments/{appointmentId}` | Authenticated owner | `200` after a time-only reschedule; service and barber stay fixed |
| `DELETE /appointments/{appointmentId}` | Authenticated owner | `200` with cancelled history after releasing all active allocations |
| `GET /appointments/{appointmentId}/availability` | Authenticated owner | `200` with eligible replacements using the original barber/service and reusable own blocks |
| `GET /barbers/{barberId}/appointments` | Staff assigned to that barber | `200` with upcoming confirmed appointments and customer email |
| `GET /barbers/{barberId}/availability` | Staff assigned to that barber | `200` with the configured Toronto date, blocks, occupancy, and revision ETag |
| `PUT /barbers/{barberId}/availability` | Staff assigned to that barber | `200` after replacing that date's complete desired ranges |
| `GET /health/live` | Public | `200` when the HTTP process responds; no database or provider call |
| `GET /health/ready` | Public | `200` when required application and database/schema dependencies are available |

Public operations ignore `Authorization`; they do not establish a caller from it. Protected operations require exactly one valid `Authorization: Bearer <token>` header. Missing, duplicate, forged, expired, or otherwise invalid bearer authorization returns `401 unauthenticated`. A valid staff identity receives `403 forbidden` on customer-only operations.

Login matches the stored email exactly, including case, and does not trim or normalize either credential. Tokens use the configured issuer, audience, and lifetime (default 1800 seconds, maximum 3600). Trusted identities and staff assignments are reloaded from the database after token verification. Customer appointment operations enforce bearer authentication and customer permission, with independent authorization in the application service. Owner-only management operations also enforce ownership; staff operations check the requested barber against the current assignment mapping.

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

The agreed customer change cutoff is at least 24 elapsed hours before an existing appointment starts. Before accepting a booking made at least two but less than 24 hours ahead, clients must explain that the customer cannot later cancel or reschedule it online. Rescheduling changes time only. The customer explicitly approved retaining the original service and barber; a destination belonging to another barber is rejected.

## Errors and retry decisions

Errors use the documented `error` object with `code`, a safe `message`, and `retryable`. Responses never expose stack traces or secrets. Every response carries a fresh `X-Correlation-ID` and `Cache-Control: no-store`.

| HTTP status | Error code and meaning | Client action |
| --- | --- | --- |
| `400` | `invalid_json`: malformed JSON or duplicate object member names | Correct the body |
| `401` | `invalid_credentials` at login; `unauthenticated` on protected operations | Supply correct credentials or valid bearer authorization |
| `403` | `forbidden`: a valid caller lacks the required role, ownership, or barber assignment | Use an authorized account |
| `404` | `barber_not_found`, `slot_not_found`, or `appointment_not_found` | Refresh the selected resource |
| `409` | `slot_unavailable`, `customer_overlap`, `idempotency_key_conflict`, `appointment_not_confirmed`, `barber_change_forbidden`, `availability_occupied`, or `availability_conflict` | Resolve the documented conflict before starting a new intent |
| `412` | `stale_version`: the supplied appointment version or date revision is stale | Refresh the resource before starting a new intent; preserve the precondition on an uncertain retry |
| `415` | `unsupported_media_type`: a JSON-body operation received another media type | Send `application/json` |
| `422` | `validation_error`, `booking_notice_violation`, or `change_cutoff_violation` | Correct input or choose an eligible start |
| `500` | `internal_error`: controlled unexpected failure | Bounded retry with the unchanged mutation intent |
| `503` | `service_unavailable`: temporary dependency or transaction-wait failure; also unready database/schema | Honor `Retry-After`; bounded retry with the unchanged mutation intent |

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

## Customer appointment management

The existing booking and own-upcoming-list representations remain unchanged: they report confirmed appointments and do not gain a required version. A separate owner-only management read exposes the current version and either confirmed or cancelled status, even when the upcoming list no longer includes the appointment. Cancellation preserves the appointment row, historical schedule, and permanent slot identities.

For a new reschedule or cancellation intent, read the management representation and send its strong quoted ETag in exactly one `If-Match` header, together with exactly one valid `Idempotency-Key`. Weak tags, wildcard tags, and missing, repeated, or malformed preconditions are `422 validation_error`. A valid but stale version produces `412 stale_version`. A fresh request cannot change a cancelled appointment.

Rescheduling accepts only `slotId`. Service, barber, customer, duration, and optional notes fields are rejected. The destination must belong to the existing barber; another barber returns `409 barber_change_forbidden`. The same appointment ID and original creation time survive a successful move.

Cancellation and rescheduling require the current existing start to be at least 24 elapsed hours after one trusted server decision time. The replacement must start at least two elapsed hours after that same time. Exactly 24 and two hours are eligible. UTC elapsed duration governs both rules across daylight-saving changes. The clock is sampled after customer, appointment, and block waits; browser time never authorizes a change.

The replacement requires complete enabled duration and no customer conflict, excluding only the appointment being moved. The protected replacement-availability operation derives barber and service from the owned appointment and accepts only the local date query. It treats that appointment's own blocks as reusable; public availability continues to hide all occupied blocks. Both browsing and mutation remain subject to eligibility, and browsing does not hold a slot.

For a 60-minute appointment occupying 10:00 and 10:30 blocks, a move to 10:30 retains the shared 10:30 block, acquires 11:00, and releases 10:00. Protect the union of source and destination blocks in a common barber/start/ID order. If any claim, rule, or persistence step fails, restore the original appointment, all allocations, and all affected versions.

Cancellation releases every allocation and advances the appointment version once in the same transaction. Replaying the completed cancellation does not release blocks or advance versions again. Before confirming a replacement made less than 24 hours ahead, show the same online-change restriction as short-notice booking.

## Assigned-barber staff management

Staff can read upcoming confirmed appointments only for assigned barbers, ordered by start and appointment ID, with customer email for shop contact. The all-barber shop account uses the existing staff role and assignment rows. The staff view does not confer customer appointment-editing privileges.

The protected staff availability GET and PUT require one `date` query interpreted in `America/Toronto`. GET includes all configured blocks, enabled/disabled state, occupancy, normalized enabled ranges, and a revision. It does not apply the customer booking-notice filter.

PUT replaces one complete date's desired ranges. `{"ranges": []}` closes that date. Each range supplies positive, nonoverlapping boundaries on the half-hour grid with explicit numeric offsets matching Toronto at those instants. The last endpoint may be the following local midnight. This distinguishes the repeated autumn hour and rejects nonexistent spring times and unsupported calendar conversions. Responses use Toronto-offset ranges so they can be submitted again; individual blocks use UTC timestamps ending in Z.

Materialize each elapsed half-hour between separately converted Toronto midnights: 46, 48, or 50 blocks. Preserve existing identities, create missing blocks, enable desired intervals, and disable omitted ones. Never move or rebind a slot ID. Reject the entire edit with `409 availability_occupied` if it would disable any occupied block, including one half of an appointment crossing midnight. No automatic appointment move or cancellation is implied.

A staff date read returns a strong quoted revision ETag; a new PUT requires it as `If-Match` and requires an idempotency key. A stale revision returns `412 stale_version`. A conflicting block write returns `409 availability_conflict`. Serialize same-barber schedule writers and missing-block creation with a short barber lock; changed blocks also participate in version checks used by booking. Demo provisioning takes the same barber lock and preserves disabled blocks, so rerunning it cannot reopen a staff-closed gap.

## Management retries and current state

Management fingerprints bind caller scope, HTTP operation, resource path, normalized request content, and precondition. Authentication and current permissions precede replay. In particular, a staff account whose assignment was revoked cannot replay protected schedule data.

Matching completed requests return their original HTTP status and deterministic JSON body before rechecking current state, version, cutoff, or availability. The ETag on such a response identifies that stored snapshot, not necessarily the resource's latest state. Generate a fresh correlation ID for each reply.

Persist successful changes and completed business rejections atomically with the decision. Input, authentication, and authorization errors do not bind a key. Conflicting key reuse preserves the first binding. Transient `500`/`503` outcomes do not intentionally complete an unfinished key; retry the unchanged method, path, body, precondition, and key after an uncertain reply.

An original booking replay after a later reschedule or cancellation still returns the original creation response without changing the current appointment. A prior reschedule replay likewise never reverses a later change. Clients should refresh the current management/list view after resolving a pending intent rather than rendering an old snapshot as current state.
