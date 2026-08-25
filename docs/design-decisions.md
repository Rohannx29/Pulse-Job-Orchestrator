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
26. [Known trade-offs and intentionally deferred scope](#26-known-trade-offs-and-intentionally-deferred-scope)

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

Resources are authorized through the ownership hierarchy:

```text
Organization
   ↓
Project
   ↓
Queue
   ↓
Job / Schedule
```

The API does not trust a resource UUID by itself.

### Why

A resource identifier is not an authorization decision.

Every owned-resource lookup verifies that the resource belongs to the authenticated user's organization.

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

## 26. Known trade-offs and intentionally deferred scope

The following are intentionally not presented as solved features.

### 26.1 JWT storage / frontend authentication hardening

The current implementation should be treated according to the actual repository authentication flow. Any future authentication change must keep the README and design document synchronized with the code rather than describing a planned state as implemented.

### 26.2 Coarse priority tiers

Pulse deliberately uses:

```text
high
normal
low
```

instead of pretending to provide strict numeric ordering.

This gives a real, verifiable capacity-isolation guarantee.

### 26.3 Single dependency per job

The current schema supports:

```text
Job.depends_on_job_id
```

not arbitrary fan-in.

Multiple dependencies would require a join table and a completion predicate over all parents.

### 26.4 Rate limiting

API rate limiting is a production-hardening item, particularly for authentication and externally exposed endpoints.

### 26.5 RBAC

The current organization model is intentionally simple. Role-based permissions such as:

```text
admin
operator
viewer
```

are future scope.

### 26.6 Audit log

`JobExecution` provides job execution history, but sensitive control-plane actions such as queue pause/resume, manual DLQ retry, and schedule toggling would benefit from a dedicated audit log.

### 26.7 Observability

Future production hardening should include:

- structured JSON logging;
- correlation IDs;
- Prometheus metrics;
- queue-depth metrics;
- worker utilization;
- job latency and retry metrics.

### 26.8 Production deployment hardening

The Docker Compose setup is a development/demo environment. Production deployment should add:

- external secret management;
- non-root multi-stage images;
- resource limits;
- readiness/liveness checks;
- controlled shutdown behavior;
- retention policies for execution logs and heartbeats;
- CI/CD validation.

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
