# Superuser Impersonation Support Plan (TDD-First, Updated)

## What was incomplete or incorrect in the previous plan

This update keeps scope tight and focused on completing the issue.

- Removed allowlist and default-user design; both are out of scope.
- Removed session identity model (`set_identity`) to avoid split identity state.
- Standardized on per-call `as_user` for all P4-backed tools when impersonation is enabled.
- Locked connection strategy to per-request fresh `P4` for correctness under concurrency.
- Reframed testing to completion-focused checks only.

---

## Issue

The MCP server currently behaves as a single-user integration:

- `src/core/connection.py` reuses a mutable long-lived `P4` object.
- `p4.user` can become unsafe under concurrent multi-session usage.
- Permission property caching in `src/middleware/check_permission.py` is not user-scoped.
- Audit logs do not consistently carry both actor and effective identity.

With superuser credentials, this creates correctness and audit risk unless identity is explicit per request and fully traceable.

---

## Goal

Enable robust superuser-backed impersonation for multi-user operations with explicit request identity.

Required final behavior:

1. Server authenticates with a superuser account.
2. When impersonation is enabled, each P4-backed tool call must include `as_user`.
3. If impersonation is disabled and `as_user` is provided, request is denied with policy reason.
4. Permission/property checks execute in effective-user context.
5. Every tool call logs actor and effective identity plus outcome.
6. Concurrency is safe (no shared mutable `p4.user` leakage).

---

## Decided policy and contracts

### Identity and request contract

- No session identity API in v1.
- No `set_identity` tool in v1.
- Identity source for impersonation is request-only: `as_user`.
- `as_user` is required for all P4-backed tools when impersonation is enabled.
- When impersonation is disabled, existing behavior stays unchanged for legacy clients; explicit `as_user` hard-fails.

### Configuration contract

Keep configuration minimal:

- `MCP_IMPERSONATION_ENABLED` (bool)
- `MCP_IMPERSONATION_SUPERUSER_MODE` (bool)

Removed from scope:

- `MCP_IMPERSONATION_ALLOWED_USERS`
- `MCP_IMPERSONATION_DEFAULT_USER`

### Error handling contract

- Startup fail-fast when impersonation is enabled but superuser-mode or auth preconditions are not satisfied.
- Policy denials return structured machine-readable `reason_code` values.
- If request input is contradictory (for example legacy session identity metadata conflicts with `as_user`), return `400`.
- If `as_user` does not exist or cannot execute, pass through underlying P4 error.

### Audit logging contract

- Single summary event per tool invocation.
- Include: `request_id`, `session_id` (if present), `tool`, `actor_user`, `effective_user`, `outcome`, `reason_code` (for denies/errors).
- If actor cannot be resolved from auth state, log `actor_user=unknown_actor` and include warning marker.
- Include sanitized policy context only (no secrets/credentials).

---

## Architecture

### Connection model

- Use per-request fresh `P4` connection.
- Set `p4.user = as_user` before command execution.
- Always cleanup connection on success/failure.

#### Security level and ticket-based auth

Perforce servers with security level >= 3 (configurable via `p4 configure set
security=N`) require **ticket-based authentication** — plaintext passwords in
`P4PASSWD` are rejected.  At level 4, SSL is additionally mandatory.

| Level | Auth requirement |
|-------|-----------------|
| 0–2   | Password (plaintext or strong) accepted |
| 3     | Ticket required (`p4 login`); plaintext `P4PASSWD` rejected |
| 4     | Ticket required + SSL mandatory for all connections |

**Impact on impersonation:** simply setting `p4.user = <effective_user>` and
connecting is insufficient at level >= 3 because the impersonated user has no
ticket of their own.  The server rejects all authenticated commands (everything
except `p4 info`) with `Perforce password (P4PASSWD) invalid or unset.`

**Solution:** before each impersonated request, the superuser authenticates and
issues a ticket on behalf of the target user:

1. Connect a temporary `P4` object as the configured superuser.
2. `p4.run_login()` — authenticate the superuser.
3. `p4.run("login", effective_user)` — superuser issues a ticket for the
   target user (stored in the shared ticket file).
4. Disconnect the temporary object.
5. Create the per-request `P4` with `p4.user = effective_user`; the ticket
   from step 3 is now available in the ticket file.

This is implemented in `P4ConnectionManager._login_for_user()`.

### Permission and property evaluation

- Middleware resolves effective user from `as_user`.
- Property cache key must include effective user identity.
- Keep current TTL behavior, now keyed per user.
- Preserve existing property precedence logic.

### Service plumbing

- Pass effective user through handler/service boundaries.
- Ensure all P4-backed services use identity-aware connection acquisition.
- Roll out to all P4-backed tools in one implementation pass.

### Review service scope

- Review service operations remain enabled under impersonation in v1.

---

## Implementation plan (files and concrete updates)

### 1) Config updates

**File:** `src/core/config.py`

- Add `impersonation_enabled: bool = False`.
- Add `impersonation_superuser_mode: bool = False`.
- Load and safely parse:
  - `MCP_IMPERSONATION_ENABLED`
  - `MCP_IMPERSONATION_SUPERUSER_MODE`
- Update `to_dict()` with these fields.

### 2) Startup precondition enforcement

**Files:** `src/main.py`, `src/server.py`

- On startup, if impersonation is enabled:
  - validate superuser mode is enabled,
  - validate auth state meets superuser-backed preconditions.
- Fail fast with clear startup error when invalid.

### 3) Connection manager redesign

**File:** `src/core/connection.py`

- Implement identity-aware acquisition API:
  - `get_connection(effective_user: str | None = None)`
- Use fresh request-scoped `P4` object per call.
- Set `p4.user` from effective user for impersonated calls.
- Add helper for actor resolution from authenticated base state.
- Add `_login_for_user(effective_user)` — connects as the configured
  superuser, authenticates, runs `p4 login <effective_user>` to issue a
  ticket for the target user, then disconnects.  Called before every
  impersonated request to ensure the ticket file contains a valid entry
  for the effective user (required at security level >= 3).
- Remove `login -s` check from the impersonation path — the impersonated
  user's ticket is issued by `_login_for_user`, not pre-existing.

### 4) Middleware policy enforcement

**File:** `src/middleware/check_permission.py`

- Enforce policy:
  - impersonation enabled + P4-backed tool => `as_user` required.
  - impersonation disabled + `as_user` present => deny.
- Return structured reason codes, for example:
  - `IMPERSONATION_DISABLED`
  - `AS_USER_REQUIRED`
  - `SUPERUSER_MODE_REQUIRED`
- Use user-scoped property cache keys.
- Emit policy decision details for audit summary event.

### 5) Tool contract updates

**File:** `src/server.py`

- Add uniform optional `as_user` argument to every P4-backed tool signature.
- Enforce required-at-runtime behavior when impersonation is enabled.
- Remove or avoid introducing `set_identity` in v1.

### 6) Handler and service propagation

**Files:**

- `src/handlers/handlers.py`
- `src/services/server_services.py`
- `src/services/workspace_services.py`
- `src/services/file_services.py`
- `src/services/changelist_services.py`
- `src/services/shelve_services.py`
- `src/services/job_services.py`
- `src/services/review_services.py`

Changes:

- Thread `effective_user` through handler/service interfaces.
- Ensure all P4 execution paths call identity-aware connection APIs.

### 7) Logging and audit event shape

**Files:** `src/logging/session_logging.py`, `src/server.py`

- Add `request_id` generation/propagation.
- Record actor/effective identity and standardized outcome fields.
- Add sanitized deny/error reason context.

### 8) Documentation

**File:** `README.md`

- Document env flags and fail-fast startup behavior.
- Document per-tool `as_user` requirement when impersonation is enabled.
- Document denial reason code behavior and audit fields.

---

## Test strategy (TDD-first, completion-focused)

Create:

- `tests/conftest.py`
- `tests/unit/test_config_impersonation.py`
- `tests/unit/test_connection_impersonation.py`
- `tests/unit/test_permission_middleware_impersonation.py`
- `tests/unit/test_server_identity_flow.py`
- `tests/unit/test_logging_impersonation_fields.py`

Use mocks/fakes for `P4`, middleware context, and tool context.

### Required test cases

1. Config loads enabled/superuser flags correctly and secure defaults hold.
2. Startup fails fast when enabled but superuser preconditions fail.
3. Per-request connection sets `p4.user` from `as_user` and isolates concurrent requests.
4. Connection cleanup executes on exceptions.
5. Middleware denies `as_user` when impersonation is disabled (`reason_code` asserted).
6. Middleware requires `as_user` for P4-backed tools when enabled (`reason_code` asserted).
7. Property cache key includes effective user and preserves TTL behavior per user.
8. Tool signatures accept `as_user`; services receive and apply it.
9. Non-impersonation behavior remains unchanged when feature is disabled and no `as_user` is supplied.
10. Audit summary event includes `request_id`, actor, effective, tool, outcome, and sanitized reason context.
11. Actor fallback logs `unknown_actor` with warning marker when unresolved.
12. Invalid/nonexistent `as_user` behavior is surfaced as pass-through P4 error.

---

## Execution phases

Each phase is a self-contained deliverable (one PR per phase).

### Phase A — Foundation and Safety Rails

**Scope:** Config surface, startup validation, and policy reason-code contract.

**Changes:**

- `src/core/config.py`: add `impersonation_enabled`, `impersonation_superuser_mode` fields, env loading, `to_dict()`.
- `src/main.py` / `src/server.py`: startup fail-fast when impersonation enabled but superuser preconditions unmet.
- Define standardized `reason_code` constants (`IMPERSONATION_DISABLED`, `AS_USER_REQUIRED`, `SUPERUSER_MODE_REQUIRED`) in permission path.

**Tests (write first, then implement):**

- Config loading + secure defaults.
- Startup fail-fast behavior.
- Reason-code contract for basic policy failures.

**Exit criteria:** Server cannot start in invalid impersonation mode; config and policy error contracts are stable.

---

### Phase B — Identity-Correct Execution Path

**Scope:** Per-request connection model, identity isolation, and service plumbing.

**Changes:**

- `src/core/connection.py`: refactor to per-request fresh `P4` connection; implement `get_connection(effective_user=...)` API; add actor resolution helper.
- `src/handlers/handlers.py`: thread `effective_user` through handler boundaries.
- All P4-backed service modules (`src/services/server_services.py`, `workspace_services.py`, `file_services.py`, `changelist_services.py`, `shelve_services.py`, `job_services.py`, `review_services.py`): accept and forward `effective_user`; use identity-aware connection acquisition.

**Tests (write first, then implement):**

- Per-request `p4.user` assignment from `as_user`.
- Concurrent requests for different users do not leak identity.
- Connection cleanup on exceptions.
- Pass-through backend errors for invalid/nonexistent `as_user`.

**Exit criteria:** Identity isolation is correct under concurrency; all service plumbing is complete.

---

### Phase C — Tool Contract and Middleware Enforcement

**Scope:** Tool signatures, middleware policy, and property cache keying.

**Changes:**

- `src/server.py`: add uniform optional `as_user` argument to every P4-backed tool signature; enforce required-at-runtime when impersonation enabled; no `set_identity` in v1.
- `src/middleware/check_permission.py`: enforce policy matrix (enabled + missing `as_user` => deny; disabled + `as_user` present => deny); user-scoped property cache keys with existing TTL behavior preserved.

**Tests (write first, then implement):**

- `as_user` required/denied policy matrix.
- Property cache keyed per effective user.
- Legacy behavior preserved when impersonation disabled and no `as_user` supplied.

**Exit criteria:** Policy behavior is deterministic and contract-complete for all P4-backed tools.

---

### Phase D — Audit and Documentation

**Scope:** Audit event shape, request ID, actor fallback, and operator docs.

**Changes:**

- `src/logging/session_logging.py`, `src/server.py`: add `request_id` generation/propagation; single summary audit event per tool invocation with `request_id`, `session_id`, `tool`, `actor_user`, `effective_user`, `outcome`, `reason_code`; `unknown_actor` + warning marker fallback; sanitized context only.
- `README.md`: document env flags, `as_user` semantics, fail-fast startup, reason codes, audit fields.

**Tests (write first, then implement):**

- Audit field completeness (`request_id`, actor, effective, tool, outcome, reason context).
- `unknown_actor` fallback with warning marker.
- No sensitive info leakage in logged payloads.

**Exit criteria:** Observability and operator docs are production-ready.

---

### Phase E — Final Hardening and Merge Readiness

**Scope:** Full suite validation and Definition of Done sign-off.

**Steps:**

1. Run full test suite; fix any contract mismatches.
2. Targeted smoke checks for in-scope behavior only (no extra perf/regression expansion).
3. Validate all Definition of Done items below.

**Exit criteria:** All completion-focused tests pass; all DoD items satisfied.

---

## Definition of done

Plan is complete when:

1. All completion-focused tests are implemented and passing.
2. Per-call impersonation (`as_user`) is enforced exactly as specified.
3. Concurrency safety is validated for effective-user isolation.
4. Audit logs include required identity and reason fields without secrets.
5. Startup precondition checks fail fast when misconfigured.
6. Legacy behavior remains intact when impersonation is disabled and `as_user` is not used.
