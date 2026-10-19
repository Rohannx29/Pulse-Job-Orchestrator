# Pulse — Design Decisions

> **Distributed Job Scheduling & Orchestration System**
>
> This document records the major engineering decisions behind Pulse, the problems they solve, the alternatives considered, and the verification performed. It intentionally preserves implementation-specific reasoning rather than repeating the general architecture description.

---

## Table of Contents

1. [Celery + Redis instead of a hand-rolled queue](#1-celery--redis-instead-of-a-hand-rolled-queue)
2. [Retry backoff strategies](#2-retry-backoff-strategies)
3. [Dead-letter queue semantics](#3-dead-letter-queue-semantics)
4. [Scheduling on the same execution path](#4-scheduling-on-the-same-execution-path)
5. [Worker heartbeats via cluster inspection](#5-worker-heartbeats-via-cluster-inspection)
6. [Per-queue concurrency with Redis leases](#6-per-queue-concurrency-with-redis-leases)
7. [Priority as real queue isolation](#7-priority-as-real-queue-isolation)
8. [Internal housekeeping isolation](#8-internal-housekeeping-isolation)
9. [Queue pause semantics](#9-queue-pause-semantics)
10. [Queue-scoped idempotency](#10-queue-scoped-idempotency)
11. [Handler registry](#11-handler-registry)
12. [Alembic migrations instead of `create_all`](#12-alembic-migrations-instead-of-create_all)
13. [Job dependencies and `BLOCKED` state](#13-job-dependencies-and-blocked-state)
14. [Shared dispatch path](#14-shared-dispatch-path)
15. [Commit before dispatch](#15-commit-before-dispatch)
16. [Cookie and refresh-token authentication](#16-cookie-and-refresh-token-authentication)
17. [Multi-tenant authorization](#17-multi-tenant-authorization)
18. [DLQ retry state management](#18-dlq-retry-state-management)
19. [Worker registry and execution ownership](#19-worker-registry-and-execution-ownership)
20. [Queue priority inheritance](#20-queue-priority-inheritance)
21. [SSRF protection for HTTP jobs](#21-ssrf-protection-for-http-jobs)
22. [Ownership-aware concurrency leases](#22-ownership-aware-concurrency-leases)
23. [Cron dispatch locking](#23-cron-dispatch-locking)
24. [Global worker visibility](#24-global-worker-visibility)
25. [At-least-once execution semantics](#25-at-least-once-execution-semantics)
26. [JWT verification test hardening: a base64 boundary edge case](#26-jwt-verification-test-hardening-a-base64-boundary-edge-case)
27. [Known trade-offs and intentionally deferred scope](#27-known-trade-offs-and-intentionally-deferred-scope)
28. [Immutable audit logging for operator and control-plane actions](#28-immutable-audit-logging-for-operator-and-control-plane-actions)
29. [Server-Sent Events (SSE) with Redis Pub/Sub for real-time live updates](#29-server-sent-events-sse-with-redis-pubsub-for-real-time-live-updates)
30. [Sliding-window rate limiting and authentication hardening](#30-sliding-window-rate-limiting-and-authentication-hardening)
31. [Structured JSON logging and distributed correlation IDs](#31-structured-json-logging-and-distributed-correlation-ids)
32. [Prometheus metrics exposition and deep component healthchecks](#32-prometheus-metrics-exposition-and-deep-component-healthchecks)
33. [Multi-stage container builds and non-root process security](#33-multi-stage-container-builds-and-non-root-process-security)
34. [Batch data retention policies and table pruning](#34-batch-data-retention-policies-and-table-pruning)

---

## 1. Celery + Redis instead of a hand-rolled queue

### Decision

Pulse uses **Celery with Redis as the broker and result backend** rather than implementing its own worker-claiming and message-delivery system.

### Why

The hardest primitive in a distributed job scheduler is safe work claiming:

> When N workers are polling for work, how do we guarantee that one job is not simultaneously claimed by multiple workers or silently lost?

Redis list operations such as `BRPOP`/`LPUSH` provide atomic broker-level operations. Celery builds retry, ETA scheduling, acknowledgements, and periodic-task machinery on top of that.

A hand-rolled approach using database polling, for example:

```text
SELECT ... FOR UPDATE SKIP LOCKED
```

is possible, but would reproduce a large amount of infrastructure that a mature queue system already solves.

The project therefore spends its complexity budget on scheduler-specific behavior:

- retry policies
- dead-letter handling
- execution history
- per-queue concurrency
- priority isolation
- dependencies
- worker tracking

### Consequence

Pulse is intentionally **not** a message broker implementation. Celery/Redis provide the delivery primitive; Pulse provides the domain semantics around it.

---

## 2. Retry backoff strategies

### Decision

Pulse supports three retry-delay strategies:

| Strategy | Formula for attempt `N`, base `B` | Typical use |
|---|---|---|
| Fixed | `B` | External APIs with hard rate limits |
| Linear | `B × N` | Gradually easing pressure on a struggling dependency |
| Exponential | `B × 2^(N-1)` | Default; useful for transient failures and sustained outages |

The calculation is centralized in `_compute_backoff_seconds`.

### Why

Different failure classes need different retry behavior. A fixed delay is useful for predictable rate limits, while exponential backoff reduces pressure when a downstream service is persistently unavailable.

### Verification

A live fixed-backoff test with:

```text
strategy = fixed
base_delay_seconds = 3
max_retries = 2
```

observed approximately 3.5s and 3.6s between retries. The additional time is execution and scheduler overhead; the configured 3-second delay remained the floor.

The job entered the DLQ after the third failed attempt, matching:

```text
current_retry_count < max_retries
```

as the retry boundary.

### Implementation note

The current implementation uses the job's `max_retries` for retry exhaustion, while the selected retry policy provides the backoff strategy and base delay. The documentation should not describe `RetryPolicy.max_retries` as an independently enforced queue-level limit unless the implementation is changed to do so.

---

## 3. Dead-letter queue semantics

### Decision

A job is considered **terminally failed** only when its configured retry budget is exhausted. At that point it moves to `dead_letter`.

### Why

`failed` and `dead_letter` represent different states:

- `failed` — an attempt failed but the job may still retry.
- `dead_letter` — automatic retries are exhausted and the job requires manual intervention.

This distinction makes the operational dashboard meaningful.

### Manual retry

`POST /jobs/{id}/retry`:

1. resets the job's retry counter;
2. removes the current `DeadLetterJob` state;
3. re-dispatches the job.

The old `JobExecution` rows remain as historical evidence.

### Important design choice

`DeadLetterJob` represents the **current DLQ state**, not the historical failure log. `JobExecution` is already the execution audit trail.

This allows a job to follow:

```text
RUNNING
  ↓
DEAD_LETTER
  ↓
manual retry
  ↓
RUNNING
  ↓
DEAD_LETTER
```

without accumulating conflicting active DLQ rows.

---

## 4. Scheduling on the same execution path

### Decision

Recurring cron schedules do not have a separate execution engine.

`JobSchedule` stores the cron expression. Celery Beat periodically evaluates schedules with `croniter`; when one is due, Pulse creates an ordinary `Job` row and sends it through the same dispatch/execution pipeline as an ad-hoc job.

### Why

This gives scheduled jobs the same:

- retry behavior
- DLQ behavior
- concurrency limits
- priority routing
- execution history
- handler registry

as ordinary jobs.

There is one execution path to reason about and test instead of two.

### Current cadence

The Beat dispatch task runs on a fixed 30-second interval.

### Trade-off

The schedule is therefore interval-checked rather than triggered at exact wall-clock precision.

---

## 5. Worker heartbeats via cluster inspection

### Decision

Workers do not independently write heartbeat rows to PostgreSQL. The internal worker runs `sync_worker_heartbeats`, which uses Celery's `control.inspect()` to inspect the worker fleet and synchronizes the result into PostgreSQL.

### Why

This avoids:

- giving every worker direct database-heartbeat responsibilities;
- duplicating heartbeat logic across workers;
- adding another failure mode when an individual worker cannot reach PostgreSQL.

The internal worker provides one centralized observation path.

### Consequence

Worker health is derived from Celery cluster inspection rather than a worker-to-database heartbeat protocol.

---

## 6. Per-queue concurrency with Redis leases

### Decision

Pulse enforces `Queue.concurrency_limit` with a custom Redis-backed semaphore.

Celery's own:

```text
--concurrency
```

is a worker-process setting. It does not represent a logical Pulse queue limit.

### Why

Suppose a queue has:

```text
concurrency_limit = 1
```

while its worker pool has several execution slots. Pulse must still guarantee that only one job from that queue performs real work at a time.

### Behavior

Before doing real work:

```text
execute_job
    ↓
acquire Redis lease
    ↓
if capacity available → continue
if full → defer
```

When the queue is full, the task is rescheduled without incrementing the domain retry counter. Being temporarily unable to acquire capacity is not a job failure.

The lease is released in a `finally` block so normal success and failure paths both release capacity.

### Verification

A queue with:

```text
concurrency_limit = 1
```

was given three simultaneous simulated jobs while the worker fleet had six free execution slots. The jobs ran sequentially with no overlap in their execution windows.

---

## 7. Priority as real queue isolation

### Decision

Pulse uses three physical Celery queues:

```text
pulse.high
pulse.normal
pulse.low
```

with dedicated worker pools.

The application maps the effective job priority to one of those tiers.

### Why

Redis-backed Celery does not provide the same native per-task priority ordering semantics as a broker such as RabbitMQ.

A numeric priority field alone would therefore be misleading.

Dedicated worker pools provide a concrete guarantee:

> A flood of low-priority jobs cannot consume the execution capacity reserved for high-priority jobs.

### Verification

A priority-2 job executed only on `worker-high`, while a priority-9 job executed only on `worker-low`.

### Trade-off

This is **three-tier capacity isolation**, not a strict global ordering of every numeric priority value.

---

## 8. Internal housekeeping isolation

### Decision

Housekeeping tasks use a fourth queue:

```text
pulse.internal
```

with a dedicated worker.

It handles tasks such as:

- cron schedule dispatch
- worker heartbeat synchronization

### Why

User job traffic should not be able to starve the control-plane work that keeps the scheduler observable and schedules recurring jobs.

This makes the internal worker a small operational control plane separate from user workload capacity.

---

## 9. Queue pause semantics

### Decision

Pausing a queue stops execution, not merely intake.

`execute_job` checks the queue's paused state before doing real work. A task already handed to Celery can therefore defer itself when it reaches a paused queue.

### Why

A pause operation that only blocks new submissions is surprising to an operator: work already in the broker would continue to execute.

The chosen behavior makes:

```text
pause queue
```

mean:

> Do not perform user job work from this queue until it is resumed.

### Verification

A job scheduled for execution was paused after dispatch. It remained deferred beyond its ETA and completed only after the queue was resumed.

---

## 10. Queue-scoped idempotency

### Decision

`idempotency_key` is unique within a queue:

```text
(queue_id, idempotency_key)
```

rather than globally.

### Why

The same producer-facing key may legitimately be reused against two different queues without collision.

The queue is the natural scope of the job submission contract.

### Race handling

`create_job`:

1. checks whether the key already exists;
2. returns the existing job with `200` when it does;
3. catches the database uniqueness race if two requests arrive almost simultaneously.

This means idempotency is an actual API behavior, not merely a database constraint.

### Important distinction

Submission idempotency does **not** mean execution is exactly-once. See [At-least-once execution semantics](#25-at-least-once-execution-semantics).

---

## 11. Handler registry

### Decision

The actual job operation is selected through a handler registry keyed by:

```text
payload["type"]
```

Current handlers include:

- `simulate`
- `http_request`

### Why

Execution infrastructure should not need to know the implementation details of every job type.

The registry separates:

```text
scheduling / retries / DLQ / execution history
```

from:

```text
actual business operation
```

Adding a new handler therefore does not require rewriting the scheduler's retry and dead-letter machinery.

### Verification

`simulate` remains deterministic for testing. `http_request` was also verified with a real external HTTP request, rather than a mock.

---

## 12. Alembic migrations instead of `create_all`

### Decision

PostgreSQL schema evolution is managed with Alembic migrations under:

```text
backend/alembic/
```

rather than relying on:

```python
Base.metadata.create_all()
```

at startup.

### Why

Production databases need:

- reproducible schema history;
- upgrade paths;
- downgrade paths;
- reviewable schema changes;
- deployment-safe migration behavior.

### Migration issue discovered

A migration initially dropped tables but did not drop the separately-created PostgreSQL ENUM types:

```text
workerstatus
jobstatus
retrystrategy
loglevel
```

A full:

```text
upgrade → downgrade → upgrade
```

round trip then failed with a duplicate enum-type error.

The downgrade was corrected to explicitly drop the named enum types.

### Second enum issue

Adding `BLOCKED` exposed another PostgreSQL/Alembic edge case: autogenerate detects table/column changes but does not automatically generate new members for an existing PostgreSQL ENUM type.

The migration therefore had to explicitly add:

```sql
ALTER TYPE jobstatus ADD VALUE IF NOT EXISTS 'BLOCKED'
```

The first attempt used the wrong lowercase label. The live database showed that SQLAlchemy stores the Python enum **member name** (`BLOCKED`) in this schema.

The final migration was verified by checking the actual enum values after migration.

### Lesson

A generated migration is not automatically a verified migration.

---

## 13. Job dependencies and `BLOCKED` state

### Decision

Pulse supports one dependency per job using:

```text
Job.depends_on_job_id
```

plus the `BLOCKED` status.

### Why

A full DAG/workflow engine would add substantial complexity for the current assignment. A self-referential foreign key provides useful sequencing without introducing a separate scheduler.

### Positive path

For:

```text
A → B → C
```

the behavior is:

```text
A runs
 ↓
A completes
 ↓
B is released
 ↓
B runs
 ↓
B completes
 ↓
C is released
```

A live three-job chain executed in strict order with no overlapping execution windows.

### Failure path

For:

```text
X → Y → Z
```

where X permanently fails:

```text
X → DEAD_LETTER
       ↓
Y → DEAD_LETTER
       ↓
Z → DEAD_LETTER
```

`DeadLetterJob.reason` records the dependency chain so later failures remain diagnosable.

### Why recursion matters

Without recursive cascade failure, later jobs could remain `BLOCKED` forever after an upstream permanent failure.

### Cycle prevention

A true dependency cycle is structurally prevented because:

1. a job can only depend on an already-existing job;
2. the dependency field is set at creation;
3. the current API does not update that dependency afterward.

Therefore dependency edges point backward in creation time.

### Current limitation

The model supports **one dependency**, not multiple dependencies such as:

```text
A ─┐
   ├→ C
B ─┘
```

Supporting fan-in would require a dependency/join table and a "has every dependency completed?" check.

---

## 14. Shared dispatch path

### Decision

All paths that send a Pulse job to Celery use:

```text
app/dispatch.py
```

and its shared `dispatch_job()` function.

### Why

Jobs can be dispatched from several places:

- API job creation
- batch creation
- dependency release
- cron schedule spawning
- manual retry

Duplicating routing and ETA logic in each path would create drift.

Centralizing dispatch ensures that all paths use the same:

- priority-tier routing;
- ETA handling;
- Celery task submission behavior.

### Circular import consideration

`tasks.py` and `dispatch.py` depend on each other conceptually. The implementation uses a lazy import of `execute_job` inside the dispatch path to avoid a module-load-time circular import.

---

## 15. Commit before dispatch

### Decision

A job row must be committed to PostgreSQL **before** the corresponding Celery task is dispatched.

### Why

The original implementation dispatched first and committed second:

```text
dispatch
  ↓
commit
```

That creates a race:

```text
API
 ↓
send task to Redis
 ↓
worker receives task
 ↓
worker queries PostgreSQL
 ↓
job row is not committed yet
```

The worker could therefore find no job and return. The API would still believe it had successfully accepted the job.

### Correct ordering

```text
create Job
   ↓
commit
   ↓
dispatch to Celery
   ↓
persist any resulting status change
```

This ordering was applied consistently to job creation, batch creation, retry, dependency release, and scheduled dispatch.

### How it was discovered

This was not found by a failing automated test. The race window was too small for the earlier local testing loop. It was discovered during code review while implementing related functionality.

That is an important engineering lesson: tests and code review catch different classes of distributed-systems bugs.

---

## 16. Cookie and refresh-token authentication

### Decision

Authentication uses:

- httpOnly cookies;
- secure cookies;
- SameSite controls;
- short-lived access tokens;
- rotating refresh tokens;
- hashed refresh tokens in PostgreSQL;
- refresh-token families;
- reuse detection;
- explicit credential-aware CORS.

### Why

Keeping tokens out of JavaScript-accessible storage reduces the most common XSS-to-token-theft path.

Refresh tokens are stored as SHA-256 hashes rather than raw values.

Each refresh rotates the token:

```text
old refresh token
       ↓
revoked
       ↓
new refresh token
       ↓
same family
```

If an already-rotated token is replayed, the whole family is revoked.

### `/auth` cookie scope

The refresh cookie is scoped to `/auth`, so it is not sent with ordinary API requests.

### Delivery failure discovered

The authentication implementation initially added the `RefreshToken` model but omitted its Alembic migration. The feature therefore failed on the first real login against a fresh database with an undefined-table error.

A mocked unit test still passed because the database object was mocked and therefore could not reveal the missing table.

### Fix

The missing migration was added, followed by real-database verification of:

```text
register
→ login
→ me
→ refresh
→ replay old token
→ logout
```

An integration test using real PostgreSQL was also added.

### Lesson

For database-backed features:

> A green mocked test is not sufficient evidence that the feature works against a fresh database.

---

## 17. Multi-tenant authorization

### Decision

Resources are authorized through the ownership hierarchy backed by an explicit RBAC model:

```text
User ──── owns ────> Organization ──> Project ──> Queue ──> Job / Schedule
  │                        ▲
  └──── member of ─────────┘ (via OrganizationMember: admin | member)

Organization ── owns ──> InviteToken (single-use, SHA-256 hashed, 72h expiry)
```

The API does not trust a resource UUID by itself.

### Role model

Pulse implements a three-tier role system:
1. **`system_admin`** (Platform Operator): Bootstrapped from the initial installation account; possesses cross-tenant visibility for system-wide health monitoring, queue oversight, and user role management (`/admin/*`).
2. **`org_admin`** (Organization Administrator): Owner or designated administrator of an organization. Can configure queues, adjust concurrency limits, pause/resume queues, and invite team members.
3. **`member`** (Organization Member): Normal team member invited into the workspace. Can submit, monitor, and retry jobs within their company's projects.

### Onboarding & Invite Security

To prevent arbitrary self-promotion to admin in multi-company deployments:
- Registering a company workspace sets the creator as `org_admin`.
- Normal team members are added exclusively through invite tokens generated by an `org_admin` via `/orgs/me/invite`.
- Only the SHA-256 digest of the invite token is stored in the database.
- At `/auth/accept-invite`, tokens are checked for expiry, prior use, and matching email, creating an account with `member` privileges.

### Cross-tenant response

If a resource exists but belongs to another organization, the API returns:

```text
404 Not Found
```

rather than:

```text
403 Forbidden
```

This avoids confirming the existence of resources outside the caller's tenant.

### Verified cases

Cross-organization attempts against projects, queues, jobs, and schedules were denied.


---

## 18. DLQ retry state management

### Problem discovered

A manual DLQ retry originally reset the job's retry count and re-dispatched it but left the old `DeadLetterJob` row in place.

If the job failed again, insertion of a new DLQ row could collide with the existing unique `job_id`.

### Decision

`DeadLetterJob` is current-state data, so manual retry removes the stale DLQ record before re-dispatch.

### Verification

The same job was taken through multiple:

```text
fail → DLQ → retry → fail → DLQ
```

cycles.

The system maintained one current DLQ entry per dead-lettered state and generated a fresh failure timestamp each time.

---

## 19. Worker registry and execution ownership

### Problem discovered

The schema already contained:

```text
Job.claimed_by_worker_id
JobExecution.worker_id
JobStatus.CLAIMED
```

but the execution code was not actually populating them.

The job effectively went:

```text
QUEUED → RUNNING
```

with only a hostname in logs.

### Decision

A shared worker registry was introduced in:

```text
app/worker_registry.py
```

and used by both execution and heartbeat synchronization.

Execution now records:

```text
QUEUED
  ↓
CLAIMED + worker
  ↓
RUNNING
```

The worker is recorded on both the Job and JobExecution.

### API addition

A worker-specific jobs endpoint provides a persistent application-level query rather than requiring operators to infer state from Celery.

### Verification

During live execution:

- `Job.claimed_by_worker_id` pointed to a real worker;
- `JobExecution.worker_id` pointed to the same worker;
- the claim cleared after completion;
- cross-organization job data remained hidden.

---

## 20. Queue priority inheritance

### Problem discovered

Both `Queue.priority` and `Job.priority` existed, but the relationship was undefined. Only `Job.priority` was being used for routing.

This made the queue-level priority field effectively inert.

### Decision

`Job.priority` is optional at creation:

```text
None
  → inherit Queue.priority

explicit value
  → override Queue.priority
```

The effective priority is resolved once at job creation and stored on the Job.

### Why

Persisting the effective value means routing and display do not need to repeatedly join back to Queue just to determine the job's tier.

### Verification

A queue with priority `2` produced:

```text
job with no explicit priority → 2
job with explicit priority 9  → 9
```

The same behavior was applied to single creation, batch creation, and recurring schedule spawning.

---

## 21. SSRF protection for HTTP jobs

### Problem

The `http_request` handler accepts a caller-supplied URL, creating a classic server-side request forgery surface.

Without protection, a job could target:

```text
169.254.169.254
127.0.0.1
private/internal services
```

from inside the worker network.

### Decision

`app/url_safety.py` validates the destination before making the request.

Protections include:

- hostname resolution;
- blocking private addresses;
- blocking loopback addresses;
- blocking link-local addresses;
- blocking reserved addresses;
- safe HTTP-method allowlist;
- maximum timeout;
- streamed response-size cap;
- redirects disabled.

### Important limitation

This is not claimed to be a complete DNS-rebinding-proof implementation.

The hostname is resolved and validated before the HTTP connection, but the validated IP is not fully pinned to the eventual connection. A stronger design would bind the outbound connection to the validated destination.

### Verification

A request targeting a cloud metadata endpoint was blocked and reached the DLQ cleanly.

A legitimate external request completed normally.

---

## 22. Ownership-aware concurrency leases

### Problem discovered

The original semaphore used one Redis counter with a shared TTL.

That had two edge cases:

1. a long-running job could outlive the key's TTL and cause the entire counter to disappear;
2. an expired/recreated counter could allow a late release to decrement another job's capacity.

### Decision

The semaphore was rewritten as a Redis sorted set containing unique lease tokens:

```text
token → expiry timestamp
```

Each acquisition owns one token.

### Why

A task can now release only the lease token it was actually given.

A crashed worker's lease naturally expires and stops counting without another worker needing to detect the crash.

### Verification

Real concurrent load against a `concurrency_limit=1` queue showed exactly one active lease throughout execution, followed by a return to zero after completion.

A separate test intentionally slept past a short lease TTL to verify expiry behavior rather than mocking time.

---

## 23. Cron dispatch locking

### Problem

Celery Beat invokes `dispatch_due_schedules` every 30 seconds. If one invocation takes longer than the interval, another invocation could begin while the first is still running.

Both could observe the same schedule as due and create duplicate Jobs.

### Decision

The dispatch task acquires a short Redis mutex:

```text
SET ... NX EX 25
```

The lock expires slightly before the 30-second Beat interval so a crashed dispatcher cannot permanently wedge scheduling.

### Why

The mutex prevents ordinary overlapping dispatcher runs without introducing a permanently held distributed lock.

### Important limitation

The mutex is an overlap guard, not a complete exactly-once scheduling protocol. A stronger design could use a deterministic schedule-occurrence identifier or database uniqueness constraint for the specific scheduled occurrence.

---

## 24. Global worker visibility

### Decision

`GET /workers` exposes the shared worker fleet across organizations.

### Why

Workers are shared infrastructure:

```text
organization A job ─┐
organization B job ─┼→ shared worker fleet
organization C job ─┘
```

There are no per-tenant worker pools in the current architecture.

The worker's existence and hostname are treated as operational infrastructure information.

### Security boundary

Worker metadata may be globally visible, but job data remains tenant-scoped. `GET /workers/{id}/jobs` filters jobs to the caller's organization.

### Future isolated-tenancy model

True worker isolation per tenant would require architectural changes such as organization-specific Celery queues and worker pools. It is not merely an authorization patch.

---

## 25. At-least-once execution semantics

### Decision

Pulse provides **at-least-once execution**, not exactly-once execution.

### Why

`task_acks_late=True` means the Celery task is acknowledged only after execution completes.

If a worker crashes after performing an external side effect but before acknowledgement:

```text
external side effect succeeds
        ↓
worker crashes
        ↓
task is redelivered
        ↓
external side effect may happen again
```

This is the standard trade-off of reliable at-least-once task queues.

### Important distinction

Pulse's `idempotency_key` makes **submission** idempotent:

```text
duplicate POST
   ↓
same Job
```

It does not make arbitrary handler side effects idempotent.

Handlers that interact with external systems should use the downstream system's own idempotency mechanism where available.

For example, an HTTP payment request should carry an idempotency key understood by the payment API rather than relying on Pulse's submission key alone.

---

## 26. JWT verification test hardening: a base64 boundary edge case

### Problem discovered

`test_jwt_rejects_tampered_token` failed intermittently in full-suite runs while passing consistently in isolation.

A first investigation ran a 200,000-iteration fuzz test of the original tampering method (`token[:-2] + "xx"`) and found zero collisions, then reran the full suite 13 times with no further failures. That was treated as sufficient evidence, and the test was changed to guarantee the tampered string differed from the original token.

The failure recurred after that change.

### Root cause

A 32-byte HMAC-SHA256 signature base64url-encodes to 43 characters. 256 bits is not a multiple of 6, so the signature's last character carries only 4 meaningful bits; the remaining 2 bits are discarded on decode.

Several distinct base64 characters at that final position decode to identical underlying bytes. Tampering only the last character — even a version guaranteed to change the string — was never guaranteed to change the bytes signature verification actually checks.

### Decision

The test now tampers a character 5 positions from the end, inside the segment where every character's full 6 bits are significant.

### Verification

- 50,000-iteration fuzz test of the corrected tampering method: 0 failures.
- 20 consecutive full-suite runs: all passed.

### Lesson

A hypothesis supported by evidence is not a proven one. The first diagnosis ("environmental flake") was consistent with everything gathered at the time and still wrong. Only treating the second failure as new information, rather than noise around an already-accepted conclusion, surfaced the actual defect.

---

## 27. Known trade-offs and intentionally deferred scope

The following are intentionally not presented as solved features.

### 27.1 JWT storage / frontend authentication hardening

The current implementation should be treated according to the actual repository authentication flow. Any future authentication change must keep the README and design document synchronized with the code rather than describing a planned state as implemented.

### 27.2 Coarse priority tiers

Pulse deliberately uses:

```text
high
normal
low
```

instead of pretending to provide strict numeric ordering.

This gives a real, verifiable capacity-isolation guarantee.

### 27.3 Single dependency per job

The current schema supports:

```text
Job.depends_on_job_id
```

not arbitrary fan-in.

Multiple dependencies would require a join table and a completion predicate over all parents.

### 27.4 Rate limiting (Resolved in Decision 30)

API rate limiting and authentication hardening are now implemented with Redis sliding-window counters, account lockout, constant-time verification, and tenant quotas.

### 27.5 RBAC (Resolved in Decision 17)

Multi-tenant role-based access control (`system_admin`, `org_admin`, `member`) is now implemented with secure single-use invite tokens and organization membership joins.

### 27.6 Audit log (Resolved in Decision 28)

Control-plane audit logging is now implemented. Sensitive actions including queue pause/resume/edit, job manual retries, member invitations/removals, role modifications, and schedule toggling are captured in the immutable `audit_logs` table.

### 27.7 Observability (Resolved in Decisions 31 and 32)

Structured JSON logging and distributed correlation IDs from HTTP ingestion through Celery task execution are implemented (Decision 31). Standard Prometheus metrics exposition (`GET /metrics`), dynamic queue-depth gauges, worker availability monitoring, and decoupled deep readiness (`GET /health`) and liveness (`GET /health/live`) probes are fully implemented (Decision 32).

### 27.8 Production deployment hardening & retention (Resolved in Decisions 33 & 34)

Multi-stage Docker builds, compiler isolation, unprivileged non-root execution (`pulse:pulse` UID 10001, `nextjs:nodejs` UID 1001), `no-new-privileges` security options, scoped `.dockerignore` files, and container healthcheck dependency chains are implemented (Decision 33). Automated data retention policies and batch-scoped table pruning across heartbeats, logs, executions, and terminal jobs are implemented (Decision 34).

Remaining production-scale enhancements include:

- external secret management (e.g. HashiCorp Vault, AWS Secrets Manager);
- Kubernetes Helm charts and Horizontal Pod Autoscalers (HPA).

---

## 28. Immutable audit logging for operator and control-plane actions

### Decision

Pulse implements an immutable, append-only `AuditLog` model in PostgreSQL to record all sensitive control-plane and operator actions.

### Why

`JobExecution` and `JobLog` track automated worker attempts and task diagnostics, but they do not record *human operator* interventions. If a queue was paused, an idempotency or retry policy updated, a job retried out of dead-letter state, an employee invited/removed, or a schedule toggled, no historical actor record existed.

In a multi-tenant enterprise environment, operators require strict attribution, non-repudiation, and compliance visibility:
- **Attribution**: Knowing precisely which user account initiated a change.
- **Tenant scoping**: Ensuring audit records cannot leak across organization boundaries.
- **Non-repudiation & Immutability**: Preventing tampering, editing, or selective deletion of historical audit entries.

### Design and Guarantees

1. **Schema design**:
   - `id`: UUID primary key.
   - `org_id`: Foreign key to `Organization` (`ondelete="CASCADE"`) enforcing tenant isolation.
   - `user_id`: Foreign key to `User` (`ondelete="SET NULL"`). If a user account is deleted, the historical audit record is preserved with `user_id = NULL`.
   - `action`: Standard action identifier (e.g. `queue.pause`, `queue.resume`, `queue.update`, `job.retry`, `org.invite_member`, `org.remove_member`, `user.update_role`, `schedule.toggle`).
   - `resource_type` and `resource_id`: Identify the target entity (e.g. `queue`, `job`, `schedule`, `organization`, `user`).
   - `details`: JSONB dictionary capturing context-specific metadata and parameter diffs.
   - `created_at`: UTC timestamp indexed for efficient chronologically ordered retrieval.

2. **Index optimization**:
   Composite and single-column indexes on `(org_id, created_at DESC)`, `user_id`, `action`, and `resource_type` support fast dashboard filtering by date range, actor, action, and resource.

3. **Immutability guarantees**:
   The API exposes only a `GET /audit-logs` query endpoint. There are no `POST`, `PUT`, `PATCH`, or `DELETE` endpoints for audit records.

4. **Access control**:
   Audit logs are accessible strictly by authorized tenant administrators (`system_admin` or `org_admin`). Normal members cannot inspect audit records.

### Consequence

Every critical operator action is auditable and viewable via the dashboard explorer without external logging services or third-party dependencies.

---

## 29. Server-Sent Events (SSE) with Redis Pub/Sub for real-time live updates

### Decision

Pulse uses **Server-Sent Events (SSE)** over standard HTTP coupled with tenant-scoped Redis Pub/Sub channels (`pulse:events:{org_id}`) to push real-time execution state changes to the Next.js dashboard.

### Why

Polling creates an inherent trade-off between latency and server load:
- **Frequent polling** (e.g. 1–2s) floods the API and PostgreSQL with repetitive queries when queues are idle, generating unnecessary database load and connection churn.
- **Infrequent polling** (e.g. 10–15s) makes the operator experience feel sluggish and disconnected during active job executions or unexpected failures.

Pulse requires zero-latency observability into job transitions (`QUEUED` $\to$ `CLAIMED` $\to$ `RUNNING` $\to$ `COMPLETED` / `FAILED` / `DEAD_LETTER`) while maintaining strict multi-tenant boundaries and minimal operational overhead.

### Alternatives Considered and Trade-offs

1. **WebSockets**:
   - *Pros*: Full-duplex bidirectional communication over a single connection.
   - *Cons*: Requires protocol upgrade handshakes (`101 Switching Protocols`), custom frame parsing, complex heartbeat management, and stateful socket handling that complicates load balancing and reverse proxy configuration.
   - *Verdict*: Scheduler dashboard updates are strictly unidirectional (server $\to$ client). WebSockets introduce architectural complexity without providing value for a read-dominated operational UI.

2. **Server-Sent Events (SSE)**:
   - *Pros*: Unidirectional streaming over standard HTTP (`text/event-stream`), native browser `EventSource` API support with automatic reconnection, native support for HTTP/2 multiplexing, standard cookie authentication, and transparent traversal of reverse proxies and firewalls without protocol upgrade headers.
   - *Verdict*: Selected. Fits the application model with minimal moving parts.

3. **PostgreSQL LISTEN / NOTIFY vs. Redis Pub/Sub**:
   - PostgreSQL `LISTEN`/`NOTIFY` is bounded by an 8000-byte payload limit and consumes database connection slots for idle listeners.
   - Redis is already deployed in Pulse as the Celery broker. Its in-memory Pub/Sub mechanism routes messages with sub-millisecond latency, zero disk I/O, and negligible memory overhead.
   - *Verdict*: Redis Pub/Sub is the natural mechanism for ephemeral event distribution.

### Multi-Tenant Isolation

1. **Partitioned Channels**:
   Events are published to tenant-specific Redis channels:
   ```text
   pulse:events:{org_id}
   ```
2. **Access Control**:
   The `GET /events` FastAPI endpoint authenticates the client session, resolves the user's primary organization membership, and subscribes strictly to `pulse:events:{org_id}`. A user belonging to Organization A never receives messages from Organization B.
3. **Platform Administrator Stream**:
   A dedicated `pulse:events:global` channel allows `system_admin` platform operators to monitor cluster-wide events across all organizations without subscribing to arbitrary channels.

### Failure Resilience

Publishing failures in Celery workers or FastAPI routers are wrapped in non-blocking try/except guards and logged as non-fatal warnings. A transient Redis error will never abort a worker's job execution or roll back an API transaction.

---

## 30. Sliding-window rate limiting and authentication hardening

### Decision

Pulse implements an atomic Redis sliding-window rate limiter and multi-layered authentication defenses:
- Atomic Redis sorted-set sliding window Lua script tracking request timestamps.
- Tenant-scoped job creation (120/min) and retry quotas (30/min) preventing noisy neighbor resource exhaustion.
- Endpoint-specific IP rate limits on `/auth/login` (15/min), `/auth/register` (10/5min), and `/auth/accept-invite` (15/5min).
- Automatic 15-minute account lockout after 5 consecutive failed login attempts.
- Constant-time dummy password hash verification to eliminate user enumeration timing leaks.
- Password complexity enforcement on registration and onboarding.

### Why

In multi-tenant distributed environments, rate limiting and defensive authentication are critical infrastructure boundaries:
1. **Denial-of-Service & Noisy Neighbors**: An abusive or runaway script in Organization A could submit thousands of jobs in seconds, exhausting worker pools, Redis bandwidth, and PostgreSQL connection pools. Scoping job creation limits by `org_id` isolates tenants from mutual interference.
2. **Credential Stuffing & Brute Force**: Open `/auth/login` endpoints are vulnerable to distributed credential attacks. Rate limiting by IP alone is insufficient against botnets using rotating proxies; pairing IP limits with email-specific failed-attempt tracking and progressive lockout stops attacks regardless of proxy rotation.
3. **User Enumeration Timing Leaks**: Bcrypt is intentionally CPU-intensive. If an endpoint returns immediately when a user is not found but spends 80ms hashing when a user is found, attackers can identify registered corporate emails. Running dummy bcrypt verification on missing users guarantees identical response timing.

### Algorithm & Guarantees

1. **Sliding-window counter**:
   Unlike fixed-window counters that allow double-limit bursts across window boundaries (e.g. 100 requests at 00:59 and 100 requests at 01:00), the Redis sorted set tracks exact request timestamps:
   - `ZREMRANGEBYSCORE` cleans entries older than `now - window`.
   - `ZCARD` counts active entries in the rolling window.
   - If under capacity, `ZADD` inserts the timestamped request token with automatic key TTL (`window + 10s`).
   - If over capacity, the oldest entry's timestamp is queried to compute the exact `Retry-After` reset duration.
2. **RFC Headers**:
   Standard headers `X-RateLimit-Limit`, `X-RateLimit-Remaining`, and `X-RateLimit-Reset` accompany successful responses, while HTTP `429 Too Many Requests` returns `Retry-After`.
3. **Fail-open resilience**:
   If Redis experiences transient connectivity issues, the rate limiter fails open with warning logs, prioritizing core application availability.

---

## 31. Structured JSON logging and distributed correlation IDs

### Decision

Pulse adopts single-line structured JSON logging and distributed correlation ID tracing spanning HTTP requests, Celery task headers, and asynchronous background worker lifecycles.

### Why

In distributed systems, correlating logs between the API tier that accepted a request and the asynchronous worker that executed the resulting job is a common operational challenge:
- Traditional unstructured text logs (e.g., `[INFO] Job 123 claimed`) lack machine-parseable context, making querying in centralized aggregators (Datadog, Loki, Elasticsearch, CloudWatch) slow and regex-heavy.
- Without a shared correlation ID, diagnosing a failed job execution requires manually cross-referencing timestamps, job IDs, and worker process logs.
- Web request traces were disconnected from Celery task executions because task dispatch drops thread/coroutine context across Redis broker boundaries.

### Design and Guarantees

1. **ContextVar-driven Correlation Management**:
   The module `app/logging_config.py` manages execution context using Python's built-in `contextvars.ContextVar`.
   - When an incoming HTTP request arrives at `CorrelationIdMiddleware`, it checks for an existing `X-Correlation-ID` header or generates a fresh `UUIDv4`.
   - The correlation ID is bound to the context var, stamped on the HTTP response headers, and recorded in structured HTTP access logs along with method, path, client IP, status code, and latency in milliseconds.
2. **Celery Task Header Propagation**:
   When `dispatch_job` dispatches an execution via `execute_job.apply_async(...)`, the active correlation ID is injected into the Celery task message headers:
   ```python
   execute_job.apply_async(args=[job.id], queue=celery_queue, headers={"correlation_id": corr_id})
   ```
3. **Worker Signal Binding**:
   In `celery_app.py`, Celery signals bind the lifecycle:
   - `task_prerun`: Extracts `correlation_id` from the task headers (or falls back to Celery `task_id`) and binds it into the worker process's `ContextVar`.
   - Worker execution logs throughout `tasks.py` (job claiming, running, retrying, failing, DLQ transitions) automatically inherit and emit the identical correlation ID.
   - `task_postrun`: Safely resets the correlation ID context var to prevent bleed between re-used worker threads/processes.
4. **Structured JSON Formatter**:
   `JSONFormatter` converts all log records into single-line JSON with keys:
   `timestamp` (ISO UTC), `level`, `service`, `logger`, `module`, `line`, `correlation_id`, `message`, `exception` (if present), and any custom extra metadata.
   Configurable via `LOG_FORMAT="json"` or `"text"`, enabling human-friendly formatting in local terminal development and machine-ingestible JSON in containerized production deployments.

---

## 32. Prometheus metrics exposition and deep component healthchecks

### Decision

Pulse exposes standard Prometheus exposition format (`GET /metrics`) using a zero-dependency in-memory registry, coupled with decoupled deep readiness (`GET /health`) and process liveness (`GET /health/live`) probes.

### Why

Enterprise production deployments require standardized observability for Prometheus, Grafana, and Alertmanager:
1. **Dynamic Queue Depth Observability**: Operators need alerting on queue backlogs (`pulse_queue_depth`) before workers become completely overwhelmed or jobs exceed SLO deadlines.
2. **Worker Fleet Health**: Monitoring active worker heartbeats (`pulse_workers_active`) ensures automated alerts trigger if a worker node crashes or disconnects.
3. **Cardinality Management**: Naive Prometheus instrumentation that includes raw request URLs (e.g. `/jobs/550e8400-e29b-41d4-a716-446655440000/retry`) causes metric cardinality to grow indefinitely with every new job ID, consuming unbounded memory. Normalizing paths to `/:id` eliminates high-cardinality memory leaks.
4. **Resilient Scrapes**: If PostgreSQL or Redis suffers an outage, the `/metrics` endpoint must not return an HTTP 500. Instead, it must successfully return Prometheus exposition reporting `pulse_component_up{component="..."} 0`, allowing alerting rules to fire reliably.
5. **Readiness vs. Liveness**: Orchestrators (Kubernetes, Docker Compose, AWS ECS) require separate probes. Coupling DB readiness to container liveness causes orchestrators to kill and reboot application containers in a loop during transient database restarts, creating cascade outages.

### Design and Guarantees

1. **Zero-Dependency Exposition Engine**:
   `app/metrics.py` implements thread-safe `Counter`, `Gauge`, and `Histogram` primitives conforming to Prometheus text format version 0.0.4 (`text/plain; version=0.0.4; charset=utf-8`). This eliminates third-party library conflicts and enables native testing on any environment.
2. **Dynamic On-Scrape Probing**:
   When scraped, `collect_dynamic_metrics` runs efficient, indexed queries against PostgreSQL (`ix_jobs_queue_status`) to refresh queue depths, worker heartbeats, and schedule counts, as well as round-trip ping latencies for database and Redis.
3. **HTTP Latency Histogram**:
   `pulse_http_request_duration_seconds` tracks request durations across 11 standard buckets (5ms to 10s) with request path normalization.
4. **Security & Control-Plane Telemetry**:
   Counters record brute-force account lockouts (`pulse_account_lockouts_total`), failed logins (`pulse_failed_logins_total`), rate limit rejections (`pulse_rate_limited_requests_total`), and SSE pub/sub throughput (`pulse_events_published_total`).
5. **Decoupled Probes**:
   - `GET /health`: Deep readiness probe checking PostgreSQL `SELECT 1` and Redis `PING`. Returns HTTP 200 with component latencies or HTTP 503 if any required dependency fails.
   - `GET /health/live`: Lightweight process liveness probe returning HTTP 200 without touching dependencies.

---

## 33. Multi-stage container builds and non-root process security

### Decision

Pulse adopts multi-stage container builds (`builder` $\to$ `runtime` / `deps` $\to$ `builder` $\to$ `runner`), non-root system users (`pulse:pulse` UID 10001, `nextjs:nodejs` UID 1001), scoped `.dockerignore` context isolation, container healthcheck probes, and `no-new-privileges` privilege escalation prevention.

### Why

Monolithic single-stage Docker images and running processes as `root` create severe production risks:
1. **Attack Surface & Compilers in Production**: Compilers (`gcc`, `make`) and development header files (`libpq-dev`, `python3-dev`) installed to compile C-extensions (like `psycopg2`, `bcrypt`) remain inside the container if built in a single stage. If an attacker achieves remote code execution, having a C compiler and build tools in the container facilitates privilege escalation, reverse shell compilation, and memory corruption payloads.
2. **Container Escapes via Root**: If a container process runs as UID 0 (`root`), any container breakout or mounted host volume vulnerability can lead directly to root execution on the underlying Linux host or Kubernetes node.
3. **Image Size & Deployment Bandwidth**: Including development dependencies, build tools, package managers, and raw `node_modules` produces images exceeding 1.2 GB. Next.js `output: "standalone"` and Python wheel staging reduce image sizes by 80–90%, speeding up CI/CD deployments and autoscaling scale-up latencies.
4. **Context Invalidation**: Without `.dockerignore` files, local `__pycache__`, `.pytest_cache`, `.git`, and developer `.env` files are accidentally uploaded into the Docker daemon context, leaking secrets and busting Docker's layer cache on every build.

### Design and Guarantees

1. **Multi-Stage Backend (`backend/Dockerfile`)**:
   - `builder`: Compiles Python dependencies into an isolated `/install` prefix using `gcc` and `libpq-dev`.
   - `runtime`: Minimal `python:3.12-slim` image installing only runtime dynamic libraries (`libpq5`, `curl`). It creates system group and user `pulse` (UID/GID 10001), copies `/install` to `/usr/local`, copies application code with ownership `pulse:pulse`, and drops root privileges (`USER pulse`).
2. **Multi-Stage Frontend (`frontend/Dockerfile`)**:
   - `deps`: Caches `node_modules` from `package-lock.json` with `npm ci`.
   - `builder`: Compiles Next.js with `output: "standalone"`.
   - `runner`: Clean `node:20-alpine` image with unprivileged system user `nextjs` (UID 1001). Copies strictly `.next/standalone` and static assets, discarding build tools and devDependencies.
3. **Privilege Escalation Defenses**:
   All services in `docker-compose.yml` enforce `security_opt: [ "no-new-privileges:true" ]`, preventing processes from gaining additional privileges via setuid or capabilities.
4. **Dependency Health Orchestration**:
   Docker Compose uses `condition: service_healthy` to guarantee that API migrations and database readiness pass before Celery workers, Beat, and frontend containers initialize, preventing startup race conditions.

---

## 34. Batch data retention policies and table pruning

### Decision

Pulse implements configurable, batch-chunked automated retention policies (`WorkerHeartbeat` < 7d, `JobLog` < 30d, finished `JobExecution` < 30d, terminal `Job` < 60d, `AuditLog` < 365d) executed via periodic Celery Beat tasks on `pulse.internal` and on-demand administrator API triggers (`POST /admin/prune`).

### Why

High-throughput distributed schedulers generate millions of operational rows (heartbeats, logs, execution attempts, completed jobs) over time:
1. **Unbounded Table & Index Bloat**: Unchecked growth inflates PostgreSQL table and index sizes, degrading query planner efficiency, increasing memory consumption for hot pages, and causing sequential scans on unpruned tables.
2. **PostgreSQL WAL Spikes & Lock Contention**: Running a monolithic `DELETE FROM jobs WHERE created_at < NOW() - INTERVAL '60 days'` can lock millions of rows simultaneously, block concurrent inserts/updates, saturate the write-ahead log (WAL), and trigger replication lag or connection timeouts.
3. **Control-Plane Starvation**: If retention cleanup runs in the same worker pool as customer workloads, a large pruning job can block user tasks. Housekeeping must be segregated to internal worker capacity.
4. **Compliance & Auditability**: System pruning cannot be a silent, unmonitored background script. Operators must have auditable records of when data was deleted, how many rows were purged per table, and whether deletion quotas comply with enterprise retention SLAs.

### Design and Guarantees

1. **Chunked Batch Deletion (`app/retention.py`)**:
   Pruning queries fetch IDs in configurable batches (`PRUNE_BATCH_SIZE=1000`) and issue chunked deletions committed incrementally:
   ```python
   while True:
       batch_ids = db.query(Model.id).filter(...).limit(batch_size).all()
       if not batch_ids:
           break
       db.query(Model).filter(Model.id.in_([b[0] for b in batch_ids])).delete(synchronize_session=False)
       db.commit()
   ```
   This caps table lock duration to milliseconds per batch, allows concurrent transactions to proceed without locking waits, and distributes WAL emission smoothly.
2. **Dedicated Internal Queue**:
   The pruning task `prune_stale_data()` is assigned to `queue="pulse.internal"`, completely isolated from user job priority queues (`pulse.high`, `pulse.normal`, `pulse.low`).
3. **Distributed Mutex Lock**:
   To prevent overlapping runs if a retention sweep takes longer than the scheduled interval, the task acquires a Redis mutex lock (`pulse:lock:prune_stale_data`) with a 300-second TTL before executing.
4. **Cascading Referential Integrity**:
   Jobs are pruned only when in terminal states (`COMPLETED`, `DEAD_LETTER`). Foreign key constraints are safely respected because child logs and executions for those jobs fall under the shorter 30-day retention window and are pruned before or alongside parent job records.
5. **Observability and Audit Trail**:
   Every prune execution increments Prometheus counter `pulse_pruned_rows_total{table="..."}`, updates gauge `pulse_last_prune_timestamp_seconds`, and records an immutable entry in PostgreSQL `audit_logs` (`action="system.data_prune"`) detailing row counts, elapsed duration, and trigger actor.

---

## Decision summary

| Area | Chosen approach | Main reason |
|---|---|---|
| Task delivery | Celery + Redis | Proven delivery/retry primitives |
| Persistent state | PostgreSQL | Durable source of truth |
| Schema evolution | Alembic | Reproducible migrations |
| Retry | Fixed / Linear / Exponential | Different downstream failure patterns |
| DLQ | Persistent `DeadLetterJob` state | Human/manual recovery |
| Queue concurrency | Redis ownership-aware leases | True logical queue limits |
| Priority | Dedicated Celery queues/workers | Real capacity isolation |
| Internal tasks | `pulse.internal` | Protect scheduler control-plane work |
| Scheduling | Beat + croniter | Reuse normal job pipeline |
| Dependencies | Self-referential FK + `BLOCKED` | Useful sequencing without a DAG engine |
| Idempotency | `(queue_id, idempotency_key)` | Correct submission scope |
| Worker tracking | PostgreSQL worker registry | Persistent execution ownership |
| Auth | Cookie + rotating refresh tokens | Reduced token exposure + reuse detection |
| Tenant isolation | Ownership-chain queries | Prevent cross-organization access |
| HTTP jobs | SSRF validation | Protect worker network boundary |
| Execution semantics | At-least-once | Reliable redelivery after worker failure |
| Audit logging | Append-only PostgreSQL `AuditLog` + JSONB details | Non-repudiation, tenant attribution, and operator compliance |
| Real-time live updates | Server-Sent Events (SSE) + Redis Pub/Sub | Zero-latency reactive dashboard without WebSocket protocol overhead or polling churn |
| Rate limiting & auth hardening | Redis sliding-window Lua + account lockout + constant-time bcrypt | Protects against noisy neighbors, credential stuffing, and user enumeration |
| Structured logging & correlation | Single-line JSON formatter + ContextVar + Celery headers | End-to-end request-to-worker traceability across distributed log aggregators |
| Metrics & deep healthchecks | Prometheus format 0.0.4 (`/metrics`) + decoupled readiness/liveness (`/health`, `/health/live`) | Cluster telemetry, queue depth alerting, and cascade-proof orchestrator lifecycle management |
| Container security & multi-stage | Builder/runtime separation + non-root UID 10001/1001 + `no-new-privileges` | Eliminates compilers from runtime, reduces image size, and prevents root escalation |
| Data retention & pruning | Chunked batch deletes + `pulse.internal` + Redis lock + audit trail | Eliminates table bloat and WAL spikes without blocking concurrent workloads |

---

## Engineering principles demonstrated

The decisions above follow a few recurring principles:

1. **Use proven infrastructure for generic distributed primitives.**
2. **Keep PostgreSQL as the durable application source of truth.**
3. **Make concurrency and routing guarantees explicit rather than cosmetic.**
4. **Separate current state from immutable execution history.**
5. **Treat retries, DLQ, and dependencies as state-machine behavior.**
6. **Verify distributed behavior against real infrastructure, not only mocks.**
7. **Document limitations instead of claiming guarantees the implementation cannot provide.**
8. **Prefer one shared execution path over multiple subtly different paths.**
9. **Fix race conditions at the ordering/ownership boundary rather than adding arbitrary sleeps.**
10. **Treat migrations, security boundaries, and operational behavior as part of the feature—not as cleanup after implementation.**
11. **Treat early supporting evidence for a diagnosis as a hypothesis, not a conclusion — a second failure is new information, not noise.**
