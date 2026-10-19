# Pulse — System Architecture

## 1. Purpose

Pulse is a distributed job scheduling and orchestration system. The architecture separates **API/state management**, **message delivery**, **distributed coordination**, and **job execution** so that each responsibility has an appropriate mechanism.

The key design principle is:

> PostgreSQL owns durable application state; Redis/Celery owns task delivery and short-lived coordination; workers perform execution.

## 2. High-level architecture

![Pulse system architecture](diagrams/architecture.png)

*Request flow, data stores, and the priority-tier worker fleet. Rendered from the source topology described below — regenerate from `docs/diagrams/` if the component graph changes.*

## 3. Components

### 3.1 Next.js dashboard

The frontend provides the operator-facing interface for authentication, queue/job exploration, queue statistics, and job details.

It communicates with the FastAPI API, relies on browser cookies for authentication rather than storing access tokens in JavaScript-accessible storage, and subscribes to the Server-Sent Events (SSE) stream (`GET /events`) to render job state transitions and queue mutations live with zero-latency.

### 3.2 FastAPI API

The API is the application boundary. It:

- validates requests with Pydantic schemas;
- authenticates users;
- enforces organization ownership;
- creates and updates durable PostgreSQL state;
- maps job priority to a Celery queue;
- dispatches jobs after the durable state required by the worker exists;
- exposes operational and execution data to the dashboard;
- streams real-time updates over Server-Sent Events (SSE) scoped by tenant organization;
- enforces sliding-window rate limits and brute-force defenses across API routes.

Important modules include:

```text
app/main.py
app/routers/*.py
app/dispatch.py
app/events.py
app/rate_limiter.py
app/auth_hardening.py
app/schemas.py
app/deps.py
app/security.py
app/audit.py
app/logging_config.py
```

### 3.3 PostgreSQL

PostgreSQL is the **system of record** for durable state, including:

- users, roles, and refresh-token sessions;
- organizations, members, and single-use invitations;
- queues and retry policies;
- schedules;
- jobs;
- execution attempts and logs;
- workers and heartbeats;
- current dead-letter state;
- audit log records for operator actions and resource mutations.

Schema changes are managed through Alembic migrations. The application does not silently create the schema with `create_all()` at startup.

### 3.4 Redis

Redis has three roles:

1. Celery broker/result-backend infrastructure.
2. Backing store for Pulse's custom short-lived coordination primitives.
3. High-throughput message broker for tenant-scoped real-time event streaming (Pub/Sub).

The custom coordination primitives are:

- per-queue concurrency leases using a sorted set and atomic Lua operations;
- a short-lived cron-dispatch mutex;
- sliding-window rate limiter counters and account lockout locks.

### 3.5 Celery worker pools

There are four worker pools in Compose:

| Pool | Queue | Purpose |
|---|---|---|
| Internal | `pulse.internal` | cron dispatch and heartbeat synchronization |
| High | `pulse.high` | priorities 1–3 |
| Normal | `pulse.normal` | priorities 4–7 |
| Low | `pulse.low` | priorities 8–10 |

The dedicated pools provide **capacity isolation**. A burst of low-priority jobs cannot consume the high-priority worker pool.

This is deliberately not described as strict global priority ordering.

### 3.6 Celery Beat

Beat runs periodic housekeeping tasks. The current system uses it for:

- evaluating active cron schedules and creating ordinary Job rows;
- synchronizing worker heartbeat information.

Recurring schedules therefore reuse the normal job execution pipeline rather than introducing a second execution subsystem.

### 3.7 Flower

Flower is a development/operations aid for observing Celery worker and task activity. It is supplementary to the application's PostgreSQL execution history.

## 4. Job lifecycle

A normal job follows this conceptual path:

```text
Client
  ↓
POST job
  ↓
Validate + authorize
  ↓
Create durable Job row
  ↓
Resolve effective priority
  ↓
Dispatch to pulse.high / pulse.normal / pulse.low
  ↓
Celery worker receives task
  ↓
Acquire queue concurrency lease
  ↓
Check queue pause state
  ↓
CLAIMED → RUNNING
  ↓
Create JobExecution + logs
  ↓
Run handler
  ├── success → COMPLETED
  └── failure → retry/backoff OR DEAD_LETTER
  ↓
Release lease
```

### Why the commit happens before dispatch

Dispatching before the Job row is committed creates a race:

```text
publish Celery task
        ↓
worker queries PostgreSQL
        ↓
Job row is not visible yet
        ↓
worker can skip the job
```

Pulse therefore makes the durable database write available before handing the task to the worker.

## 5. Priority routing

The queue and job priority fields use the range 1–10:

```text
1–3  → pulse.high
4–7  → pulse.normal
8–10 → pulse.low
```

A queue has a default priority. A job can explicitly override it. The effective priority is stored on the Job row so the dispatch layer does not need to re-resolve the queue default later.

The result is **worker-pool isolation**, not a claim that every high-priority job globally executes before every lower-priority job.

## 6. Per-queue concurrency

Celery's `--concurrency` controls parallelism within a worker process. It does not enforce a logical limit across all workers serving a Pulse queue.

Pulse therefore uses Redis leases:

```text
pulse:concurrency:{queue_id}

worker token → expiry timestamp
```

Acquisition and release use atomic Redis/Lua operations. Each execution receives a unique ownership token, so a worker can only release the lease it owns.

If a worker crashes, its lease eventually expires and stops counting against the queue.

The current implementation assumes execution remains within a bounded lease lifetime. Long-running handlers would require lease renewal/extension.

## 7. Retry and dead-letter flow

A failure can produce either:

```text
failure → retry → backoff → worker
```

or, once the Job's retry limit is exhausted:

```text
failure → DEAD_LETTER + DeadLetterJob
```

Retry strategy is selected from the optional `RetryPolicy` attached to the Job. The current retry exhaustion limit is stored on the Job itself as `max_retries`; `RetryPolicy.max_retries` exists in the schema but is not currently used to override the Job value.

This distinction is intentional in the documentation because it reflects the current implementation rather than the earlier planned model.

## 8. Dependency execution

The current model supports one dependency per Job:

```text
A → B → C
```

If B depends on A and A is incomplete, B is stored as `BLOCKED` and is not dispatched.

When A completes, B is released through the normal dispatch path.

If A reaches the dead-letter state, dependents are recursively propagated to dead-letter state so a dependency chain does not remain permanently blocked.

The current API does not support fan-in such as `C waits for A AND B`.

## 9. Scheduling

A `JobSchedule` contains a standard five-field cron expression and a payload template.

Beat periodically evaluates active schedules. When a schedule is due, it creates an ordinary Job and routes that Job through the same priority/retry/execution machinery used for ad-hoc jobs.

A short Redis mutex prevents overlapping cron-dispatch runs from both processing the same due schedule at the same time. The lock is a best-effort overlap guard; it is not a distributed exactly-once scheduling protocol.

## 10. Authentication and tenant isolation

The ownership hierarchy is:

```text
User
  ↓
Organization
  ↓
Project
  ↓
Queue
  ↓
Job / Schedule
```

Resource lookups enforce that hierarchy. A resource belonging to another organization is not returned merely because its ID is known.

Browser authentication uses httpOnly cookies. Refresh tokens are stored as hashes in PostgreSQL and rotated through token families.

Sensitive control-plane actions (queue pause/resume/edit, job manual retries, member invitations and removals, role modifications, and schedule toggling) are recorded into an append-only `AuditLog` table. Each audit entry records the tenant `org_id`, actor `user_id`, action verb, target resource type and ID, and metadata diff in `details` (JSONB), enabling complete compliance tracking across tenant boundaries.

## 11. Execution semantics

Pulse provides **at-least-once execution semantics**.

With late acknowledgement, a worker crash can result in task redelivery. If an external side effect occurred before the crash, that side effect can happen again.

Therefore:

- submission idempotency is handled by the `(queue_id, idempotency_key)` constraint;
- exactly-once external side effects are not guaranteed by Pulse;
- handlers interacting with external systems must use their own idempotency strategy where necessary.

## 12. Architecture boundaries

The most important boundaries are:

```text
FastAPI       → durable application state + authorization + SSE streaming
PostgreSQL    → durable state/history
Redis/Celery  → task delivery + coordination + Pub/Sub events
Workers       → actual job execution + event emission
Next.js       → operator experience + real-time reactive UI
Flower        → Celery-level observation
```

This keeps the scheduler's domain logic explicit without reimplementing the reliable task-delivery machinery already provided by Celery/Redis.

## 13. Real-time live updates (Server-Sent Events & Redis Pub/Sub)

Pulse uses Server-Sent Events (SSE) backed by Redis Pub/Sub to provide real-time updates without polling:

```text
Worker / API Action
        ↓
publish_event(org_id, event_name, payload)
        ↓
Redis Pub/Sub: pulse:events:{org_id}
        ↓
FastAPI SSE endpoint: GET /events (aioredis stream)
        ↓
Next.js useEvents hook (native browser EventSource)
        ↓
Instant UI badge, count, and status update
```

### Key properties

- **Unidirectional simplicity**: Dashboard observability is strictly server-to-client; SSE uses standard HTTP without the protocol upgrade, framing overhead, and bidirectional state maintenance of WebSockets.
- **Tenant isolation**: Normal users and organization administrators subscribe strictly to their own organization's channel `pulse:events:{org_id}`. Platform `system_admin` users can monitor `pulse:events:global` or filter to specific tenants.
- **Graceful degradation**: In the event of a network disruption or proxy timeout, the client automatically reconnects with exponential backoff. Periodic keepalive pings (`: keepalive\n\n`) prevent proxy timeouts.
- **Resilience**: Redis Pub/Sub publication failures in background workers or API routers are non-blocking and caught safely, ensuring event publishing never disrupts primary job execution or database transactions.

## 14. API rate limiting and authentication hardening

To protect multi-tenant infrastructure against denial-of-service, noisy neighbors, and credential-stuffing brute-force attacks, Pulse enforces layered protections:

### 14.1 Sliding-window rate limiting

Rate limits are evaluated using Redis sorted sets (`ZADD`, `ZREMRANGEBYSCORE`, `ZCARD`) within an atomic Lua script:

- **Auth endpoints**: `/auth/login` (15 req/min per IP), `/auth/register` (10 req/5min per IP), `/auth/accept-invite` (15 req/5min per IP).
- **Tenant job quotas**: `POST /jobs` (120 req/min per tenant organization), preventing a single tenant from starving shared scheduler resources.
- **Retry quotas**: `POST /jobs/{id}/retry` (30 req/min per organization).
- **Standard headers**: RFC-compliant `X-RateLimit-Limit`, `X-RateLimit-Remaining`, `X-RateLimit-Reset`, and `Retry-After` headers are returned with every response.

### 14.2 Account lockout and brute-force defenses

- **Account lockout**: Repeated failed login attempts for an email account are tracked in Redis. Upon 5 consecutive failures, the account is temporarily locked for 15 minutes (`HTTP 429 Too Many Requests`).
- **Constant-time verification**: If an unrecognized email attempts login, bcrypt verification is executed against a dummy hash to consume identical CPU cycles and defeat user-enumeration timing attacks.
- **Password complexity**: User registration and invite acceptance enforce minimum 8 characters with alphanumeric and symbol complexity requirements.

## 15. Structured JSON logging and distributed correlation IDs

Pulse implements end-to-end distributed tracing and structured telemetry to simplify debugging across asynchronous boundaries:

```text
HTTP Request (Client)
        ↓  (Optional 'X-Correlation-ID' header or auto-generated UUIDv4)
CorrelationIdMiddleware (FastAPI)
        ↓  (Sets ContextVar; sets 'X-Correlation-ID' response header)
FastAPI Router / Service Layer
        ↓  (Structured JSON log: timestamp, level, service, correlation_id, path, duration_ms)
Celery Task Dispatch (`dispatch.py`)
        ↓  (Injects correlation_id into Celery task headers: `execute_job.apply_async(..., headers={...})`)
Redis Broker (Task Queue)
        ↓
Celery Worker (`task_prerun` signal in `celery_app.py`)
        ↓  (Extracts correlation_id from task headers; re-binds into worker ContextVar)
Job Execution & State Machine (`tasks.py`)
        ↓  (Worker logs emit identical correlation_id for claiming, execution, retries, and failures)
Worker Completion (`task_postrun` signal resets ContextVar)
```

### Key properties

- **Unified Context Propagation**: Asynchronous job submission and background worker execution are correlated using the same ID, allowing operators to track a single workflow from frontend HTTP submission through queueing and worker execution to completion.
- **Single-Line Structured JSON**: In production (`LOG_FORMAT=json`), all log messages are formatted as single-line JSON with standardized keys (`timestamp`, `level`, `service`, `logger`, `module`, `line`, `correlation_id`, `message`, `exception`, and contextual `extra` fields), ready for ingestion by ELK, Datadog, Loki, or CloudWatch.
- **Zero-Dependency Core**: Context propagation leverages Python's built-in `contextvars`, and the JSON formatter uses the standard library `json` module, maintaining zero external runtime dependencies for logging infrastructure.

## 16. Prometheus metrics and deep healthchecks

Pulse exposes industry-standard observability endpoints for platform telemetry, cluster alerting, and orchestrator lifecycle management:

```text
Prometheus Scraper
        ↓ (GET /metrics every 15s)
FastAPI Metrics Router (`app/routers/health.py`)
        ↓
Metrics Collector (`app/metrics.py`)
        ├── 1. Probes PostgreSQL (latency + 'SELECT 1')
        ├── 2. Probes Redis (latency + 'PING')
        ├── 3. Queries active queue depths (`ix_jobs_queue_status`)
        ├── 4. Counts active worker heartbeats (last 60s)
        └── 5. Renders Prometheus exposition text (0.0.4)
        ↓
Prometheus Engine / Grafana Dashboard / Alertmanager
```

### 16.1 Prometheus metrics exposition (`GET /metrics`)

- **Standard Text Format**: Outputs text conforming strictly to Prometheus exposition format 0.0.4 (`text/plain; version=0.0.4; charset=utf-8`).
- **Dynamic Queue Depths**: Aggregates job counts by queue and status (`pulse_queue_depth{queue="default",status="queued"}`) on scrape, providing immediate visibility into queue backlogs and worker starvation.
- **Worker Availability**: Measures active cluster workers (`pulse_workers_active`) based on the 60-second rolling heartbeat window.
- **Low Cardinality HTTP Telemetry**: HTTP request counts and latency histograms normalize dynamic path parameters (replacing UUIDs and entity IDs with `/:id`) to prevent high-cardinality memory exhaustion in long-running deployments.
- **Security & Error Counters**: Tracks brute-force lockouts (`pulse_account_lockouts_total`), failed authentications (`pulse_failed_logins_total`), rate limit rejections (`pulse_rate_limited_requests_total`), and SSE pub/sub throughput (`pulse_events_published_total`).
- **Fail-Safe Scrapes**: If PostgreSQL or Redis is down, `/metrics` does not crash with HTTP 500; it reports `pulse_component_up{component="..."} 0` with `pulse_up 1`, allowing Prometheus to alert on dependency failure without losing scrape telemetry.

### 16.2 Deep readiness probe vs. process liveness

Pulse decouples readiness and liveness to satisfy cloud-native orchestration standards (Kubernetes, AWS ECS, Docker Compose):

| Endpoint | Probe Type | Verification | Success | Failure |
|---|---|---|---|---|
| `GET /health` | Readiness | PostgreSQL ping (`SELECT 1`) + Redis ping (`PING`) with latencies | `HTTP 200 OK` | `HTTP 503 Service Unavailable` |
| `GET /health/live` | Liveness | FastAPI ASGI process responsiveness | `HTTP 200 OK` | Process terminated / deadlocked |

This separation prevents cascade restart storms: during external database maintenance or brief network partitions, orchestrators avoid killing and restarting application containers while waiting for the database to resume.

## 17. Production deployment hardening and container architecture

Pulse implements production-grade container security, multi-stage builds, and orchestrator lifecycle management:

```text
Backend Build Pipeline:
[Stage 1: builder (python:3.12-slim)]
  ├── Installs build tools: gcc, libpq-dev
  └── Compiles dependencies into /install
             ↓ (COPY --from=builder /install /usr/local)
[Stage 2: runtime (python:3.12-slim)]
  ├── Installs runtime libraries only: libpq5, curl
  ├── Drops compiler tools and development headers
  ├── Creates unprivileged user: pulse (UID 10001)
  ├── Copies app code with --chown=pulse:pulse
  └── Runs as USER pulse with HEALTHCHECK

Frontend Build Pipeline:
[Stage 1: deps] → [Stage 2: builder (Next.js standalone)]
             ↓ (COPY standalone output)
[Stage 3: runner (node:20-alpine)]
  ├── Drops devDependencies (90% size reduction)
  ├── Creates unprivileged user: nextjs (UID 1001)
  └── Runs as USER nextjs with HEALTHCHECK
```

### 17.1 Security boundaries & non-root execution

- **Unprivileged Users**: Neither the API nor Celery workers run as `root`. System user `pulse:pulse` (UID/GID 10001) runs all backend and worker workloads, while `nextjs:nodejs` (UID/GID 1001) runs the frontend.
- **Privilege Escalation Prevention**: All containers enforce `security_opt: [ "no-new-privileges:true" ]`, preventing binaries inside containers from acquiring additional privileges via setuid or capabilities.
- **Compiler Exclusion**: Compilers (`gcc`) and development header files (`libpq-dev`) are isolated strictly to builder stages and never exist in runtime images, mitigating remote code execution escalation vectors.

### 17.2 Context optimization & standalone output

- **Layer Cache Hygiene**: Scoped `.dockerignore` files prevent local `__pycache__`, `.pytest_cache`, `.git`, and `node_modules` from polluting the Docker daemon build context or invalidating build cache layers.
- **Standalone Next.js**: The frontend uses `output: "standalone"` to bundle only the exact files and node modules required for server-side rendering, drastically shrinking production container footprint.

### 17.3 Orchestrator lifecycle & healthcheck chain

Containers declare explicit healthchecks and start sequences:
1. `postgres` (`pg_isready`) and `redis` (`redis-cli ping`) reach healthy status.
2. `api` runs `alembic upgrade head` and enters `service_healthy` after passing `/health` readiness probes.
3. `worker-internal`, `worker-high`, `worker-normal`, `worker-low`, `beat`, and `frontend` start safely after dependencies verify readiness.
4. `restart: unless-stopped` ensures automated container recovery across machine restarts.

## 18. Automated data retention and table pruning

High-throughput distributed schedulers generate millions of operational records over time. Without automated pruning, unbounded table and index growth degrades query performance, inflates PostgreSQL WAL logs, and increases storage costs:

```text
Celery Beat Scheduler (hourly) OR Admin Operator (`POST /admin/prune`)
                       ↓
Redis Lock (`pulse:lock:prune_stale_data`, TTL 300s)
                       ↓
`run_retention_prune` (`app/retention.py` on queue `pulse.internal`)
  ├── 1. WorkerHeartbeat (< 7 days, batch=1000)
  ├── 2. JobLog (< 30 days, batch=1000)
  ├── 3. JobExecution (< 30 days, finished, batch=1000)
  ├── 4. Job (< 60 days, terminal COMPLETED/DEAD_LETTER, batch=1000)
  └── 5. AuditLog (< 365 days if enabled, batch=1000)
                       ↓
Metrics & Audit Trail:
  ├── Emit Prometheus counter `pulse_pruned_rows_total{table="..."}`
  ├── Update gauge `pulse_last_prune_timestamp_seconds`
  └── Write immutable `AuditLog` record (`action="system.data_prune"`)
```

### 18.1 Key operational safeguards

- **Batch Deletion**: Instead of executing large, monolithic `DELETE FROM table WHERE ...` statements that lock entire tables and generate gigabytes of WAL spikes, Pulse deletes in configurable chunks (`PRUNE_BATCH_SIZE=1000`) committing each batch incrementally.
- **Queue Isolation**: The periodic pruning task runs strictly on `pulse.internal`. High volumes of user job dispatches will never starve or delay scheduled housekeeping.
- **Concurrency Locking**: Periodic Beat dispatch is guarded by a Redis mutex lock (`pulse:lock:prune_stale_data`). If a large prune takes longer than expected, overlapping executions are safely skipped.
- **Compliance Auditability**: System-level prune operations are permanently recorded in PostgreSQL audit logs, documenting exact deleted row counts across tables, execution duration in milliseconds, and the actor (automated Beat or system admin).

## 19. Database query efficiency and connection pooling

High-throughput distributed systems require predictable database latency under heavy concurrent request and worker volume:

### 19.1 N+1 query elimination
- **Job Details & Execution Logs**: The `/jobs/{job_id}` endpoint utilizes SQLAlchemy `selectinload(Job.executions).selectinload(JobExecution.logs)` combined with `joinedload(Job.queue).joinedload(Queue.project)`. This consolidates nested parent-child-grandchild relationships and organization boundary authorization into single optimized queries, completely eliminating N+1 round-trips.
- **Organization Members**: The `/orgs/me/members` endpoint uses `joinedload(OrganizationMember.user)` to fetch member records and associated user profile details in a single SQL join rather than querying individual users iteratively.
- **Platform-Wide Aggregation**: The `/admin/organizations` endpoint computes organization member counts using a single grouped query (`SELECT org_id, count(id) FROM organization_members GROUP BY org_id`) mapped via in-memory dictionary lookup, avoiding per-organization count queries.

### 19.2 Connection pool hygiene
- **Pre-Ping & Recycling**: Database engines are configured with `pool_pre_ping=True` and connection recycling (`pool_recycle=1800`) to detect and discard stale or severed TCP sockets before handing connections to request handlers.
- **Fail-Open Resilience**: Operational dependencies (Redis rate limiters, pub/sub event emitters, and concurrency semaphores) are isolated within fail-open try/except blocks to guarantee that transient caching or message broker blips never cascade into API 500 downtime.
