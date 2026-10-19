# Pulse

**Distributed Job Scheduling & Orchestration System**

Pulse is a portfolio-grade distributed job scheduling and orchestration system built with **FastAPI, PostgreSQL, Celery, Redis, and Next.js**. It provides one execution pipeline for immediate, delayed, recurring, and dependency-aware jobs, with retries, dead-letter handling, idempotent submission, per-queue concurrency control, priority isolation, execution history, and an operator dashboard.

> **Project status:** engineering prototype / internship portfolio project. The core scheduling and execution path is implemented. Production-hardening items are intentionally documented rather than presented as complete.

## Why Pulse?

A useful job system has to answer more than "was a message placed on a queue?" Pulse is designed around operational questions such as:

- What is the current state of a job?
- Which worker executed each attempt?
- What happened during a retry?
- Why did a job reach the dead-letter queue?
- Can one queue consume all of the concurrency available to the system?
- Can high-priority work remain isolated from a low-priority burst?
- What happens when one job depends on another?
- Can the same submission safely be repeated?
- Can recurring work use the same execution path as a normal job?

## Architecture at a glance

![Pulse system architecture](docs/diagrams/architecture.png)

**Execution model:** PostgreSQL is the durable source of truth; Redis carries Celery work and coordination state; the API chooses the priority tier; dedicated worker pools provide real high/normal/low isolation; Beat feeds housekeeping tasks; and recurring schedules create ordinary Pulse jobs before they enter the normal execution pipeline.

For the detailed design, see [Architecture](docs/architecture.md), [Design Decisions](docs/design-decisions.md), and [ER Diagram](docs/er-diagram.md).

## Core capabilities

| Capability | Status | Implementation note |
|---|---|---|
| REST API | ✅ | FastAPI + OpenAPI/Swagger |
| Cookie authentication | ✅ | httpOnly access/refresh cookies |
| Refresh-token rotation | ✅ | Hashed tokens + family-based reuse detection |
| Projects and queues | ✅ | Organization-owned resources |
| Immediate jobs | ✅ | Standard execution path |
| Delayed jobs | ✅ | Celery ETA / `run_at` |
| Recurring jobs | ✅ | 5-field cron evaluated by Beat |
| Retry/backoff | ✅ | Fixed, linear, exponential strategy |
| Dead-letter queue | ✅ | Manual retry supported |
| Idempotency | ✅ | `(queue_id, idempotency_key)` uniqueness |
| Job dependencies & Multi-Parent DAGs | ✅ | Arbitrary directed acyclic graphs (DAGs), multi-parent fan-in, multi-child fan-out, DFS cycle detection, non-blocking join resolution, recursive cascade failure |
| Queue pause/resume | ✅ | Already-dispatched work is also deferred |
| Per-queue concurrency | ✅ | Redis ownership-aware lease semaphore |
| Priority isolation | ✅ | Dedicated high/normal/low worker pools |
| Execution history | ✅ | Attempt and log records |
| Worker tracking | ✅ | Current claim + execution worker ID |
| Worker heartbeat sync | ✅ | Internal Celery housekeeping task |
| Queue statistics | ✅ | Counts, success rate, latency, throughput, concurrency |
| HTTP job handler | ✅ | SSRF-aware URL validation, timeout and response cap |
| Dashboard | ✅ | Next.js App Router |
| RBAC | ✅ | Multi-tenant RBAC: System Admin, Org Admin, Member roles with secure invite tokens |
| Audit log | ✅ | Immutable audit log table with user/actor tracking and filterable dashboard explorer |
| Real-time live updates | ✅ | Server-Sent Events (SSE) with Redis Pub/Sub scoped by tenant |
| Rate limiting & auth hardening | ✅ | Redis sliding-window rate limiter, account lockout, constant-time verification, and tenant quotas |
| Structured JSON logs & Correlation IDs | ✅ | Single-line JSON formatter with distributed X-Correlation-ID propagation across API and Celery |
| Prometheus metrics & deep healthchecks | ✅ | Prometheus format (0.0.4) exposition (`/metrics`), deep readiness probe (`/health`), and liveness probe (`/health/live`) |
| Production deployment hardening | ✅ | Multi-stage Docker builds, non-root users (`pulse:pulse`, `nextjs:nodejs`), `no-new-privileges`, healthchecks, restart policies |
| Data retention & table pruning | ✅ | Automated Celery Beat batch pruning (`pulse.internal`) & admin trigger (`POST /admin/prune`) for heartbeats, logs, executions, and terminal jobs |
| Outbound Webhooks & Secret Hardening | ✅ | Cryptographic HMAC-SHA256 (`whsec_...`), KMS encryption at rest (Fernet), dual-signing rotation grace window, automatic delivery redaction, SSRF protection, Celery delivery worker (`pulse.internal`), and strict Org-Admin RBAC boundary |
| Transactional Outbox & Race-Free Dispatch | ✅ | Atomic PostgreSQL transactional outbox (`outbox_events`), row-level locked claiming (`SKIP LOCKED`), idempotent publisher with retry bounds, atomic conditional DAG fan-in unblocking, and unique `(schedule_id, scheduled_fire_time)` occurrence deduplication |
| Network & Session Security | ✅ | Query-string JWT completely removed in favor of httpOnly cookies, short-lived single-use SSE connection tickets (`POST /auth/sse-ticket`), trusted proxy IP validation (`X-Forwarded-For`), fail-closed auth rate limiting on Redis outage, defense-in-depth SSRF client (custom resolver, IP validation, redirects forbidden, 1MB payload caps), CSRF protection middleware, and private `/metrics` authorization |
| Data Integrity & Keyset Pagination | ✅ | Strict cross-tenant `retry_policy_id` isolation, 256 KB JSON payload bounds, HTTP job URL/header/body limits, batch limits (100 jobs/batch), entity name length caps, timestamp keyset cursor pagination (`X-Has-More`, `X-Next-Cursor`), and compound indexes (`ix_jobs_queue_created`, `ix_jobs_status_created`, `ix_audit_logs_org_created`, `ix_webhook_deliveries_sub_delivered`) |
| Production Topology & Automated Backups | ✅ | Production compose topology (`docker-compose.prod.yml`) with zero published internal ports for Postgres/Redis, Redis password auth (`requirepass`), one-off migration gate (`migration`), dedicated Celery pools, non-root users (`10001:10001`, `1001:1001`), resource caps, network segmentation, and automated daily database backup service with 14-day retention |
| CI/CD & Security Audits | ✅ | GitHub Actions workflow with PostgreSQL 16 & Redis 7 service containers, migration round-trip tests (`upgrade -> downgrade -> upgrade`), Ruff linting, pip-audit, npm audit, Gitleaks secret scanning, Trivy image vulnerability & CycloneDX SBOM generation, Docker Compose validation, and multi-arch container delivery (`linux/amd64`, `linux/arm64`) |
| End-to-End Testing (Playwright) | ✅ | Automated browser test suite covering registration, auth lifecycle, project/queue management, multi-parent DAG workflow inspection, outbound webhooks, and audit logs |


## Repository structure

```text
pulse/
├── README.md
├── docker-compose.yml         # Local development compose configuration
├── docker-compose.prod.yml    # Hardened production compose configuration (segmented networks, resource caps, backups)
├── .env.production.example    # Production environment template & secret generation instructions
├── .github/
│   ├── dependabot.yml         # Automated dependency vulnerability scanning
│   └── workflows/
│       └── ci.yml             # Automated testing, linting, migration round-trips, security scans & multi-arch delivery
│
├── backend/
│   ├── Dockerfile
│   ├── requirements.txt
│   ├── alembic.ini
│   ├── alembic/
│   │   └── versions/          # database migrations
│   ├── app/
│   │   ├── main.py            # FastAPI application
│   │   ├── config.py          # environment-backed settings
│   │   ├── database.py        # SQLAlchemy engine/session
│   │   ├── models.py          # database models
│   │   ├── schemas.py         # Pydantic API schemas
│   │   ├── security.py        # password/token helpers
│   │   ├── kms.py             # KMS secret encryption at rest & masking
│   │   ├── redaction.py       # secret sanitization & delivery log redaction
│   │   ├── celery_app.py      # Celery configuration
│   │   ├── dispatch.py        # common job-dispatch path
│   │   ├── tasks.py           # execution/retry/DLQ/cron/heartbeat/webhooks
│   │   ├── priority.py        # priority → Celery queue mapping
│   │   ├── concurrency.py     # Redis queue semaphore
│   │   ├── worker_registry.py # worker identity/lifecycle
│   │   ├── url_safety.py      # SSRF protections
│   │   ├── audit.py           # immutable audit logging helper
│   │   ├── events.py          # Redis Pub/Sub real-time event publisher
│   │   ├── outbox.py          # Transactional outbox engine, atomic event persistence & locked publisher
│   │   ├── webhooks.py        # Outbound webhook signing, SSRF validation & delivery engine
│   │   ├── dag.py             # Multi-parent DAG engine, DFS cycle detection & join resolution
│   │   ├── rate_limiter.py    # Redis sliding-window rate limiter
│   │   ├── csrf.py            # CSRF protection middleware for state-modifying requests
│   │   ├── auth_hardening.py  # account lockout and brute-force defenses
│   │   ├── logging_config.py  # structured JSON logging & correlation IDs
│   │   ├── metrics.py         # Prometheus metrics registry, gauges & collectors
│   │   ├── retention.py       # automated batch data retention & table pruning
│   │   ├── services/          # domain services (invitations, provisioning)
│   │   │   └── invitations.py # centralized audited organization invitation service
│   │   └── routers/           # API route modules (jobs, queues, dlq, schedules, workers, projects, orgs, webhooks, admin, audit, events, health, auth)
│   └── tests/                 # unit + integration tests
│
├── frontend/
│   ├── app/                        # Next.js routes/pages (dashboard, jobs, queues, schedules, workers, dlq, webhooks, projects, team, admin, audit-logs, auth)
│   ├── e2e/                        # Playwright automated browser test specs (auth, queues, jobs-dag, webhooks, audit-logs)
│   ├── middleware.ts               # session verification route guard
│   ├── lib/api.ts                  # API client
│   ├── lib/useEvents.ts            # Server-Sent Events React hook
│   └── playwright.config.ts        # Playwright E2E browser test runner configuration
│
└── docs/
    ├── architecture.md
    ├── design-decisions.md
    ├── er-diagram.md
    ├── Pulse-Technical-Documentation.docx
    └── diagrams/                   # rendered PNGs referenced by the markdown docs above
        ├── architecture.png
        └── er-diagram.png
```

The documentation set is intentionally limited to these four documents under `docs/` (plus the rendered diagram images they reference); the technical documentation is the fifth submission artifact when the root `README.md` is included.

## Quick start — Docker

Docker Compose is the recommended way to run Pulse because the application is intentionally multi-process.

### 1. Start the stack

From the repository root:

```bash
docker compose up --build
```

The stack contains:

- PostgreSQL 16
- Redis 7
- FastAPI API
- one internal Celery worker pool
- high-priority worker pool
- normal-priority worker pool
- low-priority worker pool
- Celery Beat
- Flower
- Next.js dashboard

### 2. Verify the containers

```bash
docker compose ps
```

### 3. Open the services

| Service | URL | Purpose |
|---|---|---|
| Dashboard | `http://localhost:3000` | Operator UI |
| API docs | `http://localhost:8000/docs` | Swagger UI |
| OpenAPI | `http://localhost:8000/openapi.json` | API schema |
| Health | `http://localhost:8000/health` | Deep component readiness probe (PostgreSQL + Redis) |
| Liveness | `http://localhost:8000/health/live` | API process liveness probe |
| Prometheus | `http://localhost:8000/metrics` | Prometheus metrics exposition (format 0.0.4) |
| Flower | `http://localhost:5555` | Celery monitoring |

> The `/health` endpoint performs deep readiness checks against PostgreSQL and Redis, returning HTTP 503 if any required component is unreachable. Container orchestrators may use `/health/live` for process liveness.

### 4. Register a user

```bash
curl -X POST http://localhost:8000/auth/register \
  -H "Content-Type: application/json" \
  -d '{
    "email":"you@example.com",
    "password":"yourpassword123",
    "full_name":"You",
    "organization_name":"Your Org"
  }'
```

### 5. Log in

The login route uses `OAuth2PasswordRequestForm`, so the request is form-encoded rather than JSON:

```bash
curl -i -c cookies.txt -X POST http://localhost:8000/auth/login \
  -H "Content-Type: application/x-www-form-urlencoded" \
  --data "username=you@example.com&password=yourpassword123"
```

Pulse stores the session in httpOnly cookies. For command-line testing, keep the cookie jar:

```bash
curl -b cookies.txt http://localhost:8000/auth/me
```

### 6. Create a project

```bash
curl -b cookies.txt -X POST http://localhost:8000/projects \
  -H "Content-Type: application/json" \
  -d '{"name":"Demo Project","description":"Pulse smoke test"}'
```

Save the returned project ID as `PROJECT_ID`.

### 7. Create a queue

```bash
curl -b cookies.txt -X POST http://localhost:8000/projects/PROJECT_ID/queues \
  -H "Content-Type: application/json" \
  -d '{"name":"demo","priority":5,"concurrency_limit":2}'
```

Save the returned queue ID as `QUEUE_ID`.

Priority mapping is:

```text
1–3  → pulse.high
4–7  → pulse.normal
8–10 → pulse.low
```

This is **capacity isolation**, not strict global priority ordering.

### 8. Submit a test job

```bash
curl -b cookies.txt -X POST http://localhost:8000/projects/PROJECT_ID/queues/QUEUE_ID/jobs \
  -H "Content-Type: application/json" \
  -d '{
    "name":"smoke-test",
    "payload":{"type":"simulate","duration_seconds":2,"fail_rate":0},
    "max_retries":2
  }'
```

Save the returned job ID as `JOB_ID`, then inspect it:

```bash
curl -b cookies.txt http://localhost:8000/jobs/JOB_ID
```

## Recommended smoke-test sequence

For a complete demonstration, test these features in order:

1. Register and log in.
2. Create a project and queue.
3. Submit a successful simulated job.
4. Submit a guaranteed-failure job and observe retries.
5. Confirm the final dead-letter entry.
6. Manually retry the dead-lettered job.
7. Submit the same idempotency key twice and verify one Job row.
8. Submit high/normal/low priority jobs and inspect the corresponding worker pools.
9. Submit several long-running jobs against a small queue concurrency limit.
10. Create a dependency and verify that the child waits for the parent.
11. Fail the parent and verify dependency failure propagation.
12. Pause a queue and verify dispatched work defers instead of executing.
13. Create a cron schedule and observe Beat-generated Jobs.
14. Use Flower and worker logs to trace execution.

## Retry and DLQ demonstration

Create a guaranteed failure:

```json
{
  "name": "failure-demo",
  "payload": {
    "type": "simulate",
    "duration_seconds": 1,
    "fail_rate": 1.0
  },
  "max_retries": 2
}
```

The expected lifecycle is approximately:

```text
QUEUED → CLAIMED → RUNNING → FAILED
                         ↓
                    retry/backoff
                         ↓
                  CLAIMED → RUNNING
                         ↓
                       ...
                         ↓
                   DEAD_LETTER
```

Inspect the current DLQ:

```bash
curl -b cookies.txt http://localhost:8000/dead-letter-jobs
```

Retry the job:

```bash
curl -b cookies.txt -X POST http://localhost:8000/jobs/JOB_ID/retry
```

`JobExecution` preserves the attempt history; `DeadLetterJob` represents the current DLQ state.

## Development without Docker

Docker is recommended. Local execution requires PostgreSQL and Redis to be available separately and requires the API, workers, Beat, and frontend to run as separate processes.

Backend setup:

```bash
cd backend
python -m venv .venv
# activate .venv
pip install -r requirements.txt
pip install pytest
```

Set environment variables appropriate for your local services, then run:

```bash
alembic upgrade head
uvicorn app.main:app --reload --port 8000
```

Run workers in separate terminals, for example:

```bash
celery -A app.celery_app worker -Q pulse.internal --loglevel=info --concurrency=2 --hostname=worker-internal@%h
celery -A app.celery_app worker -Q pulse.high --loglevel=info --concurrency=4 --hostname=worker-high@%h
celery -A app.celery_app worker -Q pulse.normal --loglevel=info --concurrency=4 --hostname=worker-normal@%h
celery -A app.celery_app worker -Q pulse.low --loglevel=info --concurrency=2 --hostname=worker-low@%h
celery -A app.celery_app beat --loglevel=info
```

## Testing

The backend test suite contains **111 test cases across 22 test modules** in `backend/tests/`, covering retry math, priority mapping, concurrency, multi-parent DAG dependencies, transactional outbox race prevention, schedule deduplication, cookie authentication, network security, SSRF defense in depth, KMS webhook encryption, secret rotation with grace windows, delivery redaction, rate limiting, data integrity bounds, cross-tenant retry policy isolation, cursor pagination, and Alembic migration round-trips. In addition, the frontend includes 5 end-to-end automated browser test suites in `frontend/e2e/` powered by Playwright.

Run backend tests:

```bash
cd backend
python -m pytest tests/ -v
```

Targeted test suites:

```bash
python -m pytest tests/test_data_integrity.py -v
python -m pytest tests/test_migrations.py -v
python -m pytest tests/test_network_security.py -v
python -m pytest tests/test_outbox_concurrency.py -v
python -m pytest tests/test_webhook_rbac.py -v
python -m pytest tests/test_webhooks.py -v
python -m pytest tests/test_dag.py -v
python -m pytest tests/test_dependencies.py -v
python -m pytest tests/test_cookie_auth_integration.py -v
```

Integration tests require the dependencies specified by the project and, where applicable, real PostgreSQL/Redis services. A test that cannot collect because dependencies are absent is an environment/setup failure, not evidence that the application logic passed.

## Environment support matrix

Pulse supports distinct deployment profiles tailored for local evaluation, continuous integration, pre-production staging, and production:

| Capability / Tier | Local Demo (`docker-compose.yml`) | CI Pipeline (GitHub Actions) | Staging (`docker-compose.prod.yml`) | Scaled Production |
|---|---|---|---|---|
| **Intended Purpose** | Local developer testing & rapid feedback | Automated linting, test suites & container builds | Pre-production validation & performance testing | Highly available tenant workload orchestration |
| **Database** | PostgreSQL 16 (single container, exposed port) | PostgreSQL 16 (ephemeral service container) | PostgreSQL 16 (segmented network, no host port) | Managed PostgreSQL (RDS/Cloud SQL) with read replicas |
| **Cache & Broker** | Redis 7 (single container, unauthenticated) | Redis 7 (ephemeral service container) | Redis 7 (`requirepass`, AOF enabled, no host port) | Managed Redis cluster with TLS & password authentication |
| **Worker Pools** | Combined or concurrency-capped local workers | Ephemeral Celery test runners | Dedicated pools (`internal`, `high`, `normal`, `low`) | Autoscaling Kubernetes worker deployments per queue |
| **Authentication** | httpOnly cookies (`IS_DEV=true`) | Ephemeral test secret keys | httpOnly cookies + strict secure flag | Same-origin or custom domain with validated CORS & CSRF |
| **Perimeter & TLS** | Plain HTTP (`localhost:8000`, `:3000`) | Headless HTTP test server | Local reverse proxy TLS (Nginx / Caddy) | Cloud Load Balancer / API Gateway with WAF & TLS termination |
| **Database Backups** | Local Docker volume persistence | N/A (ephemeral) | Automated daily `pg_dump` with 14-day retention | Automated cloud snapshots with point-in-time recovery |

## Operational & execution semantics

### 1. Job execution and retry semantics
- **At-Least-Once Delivery**: Pulse uses at-least-once execution semantics. Celery late task acknowledgement (`task_acks_late=True`) ensures that if a worker crashes midway through execution, the task remains on the broker and is redelivered.
- **Submission Idempotency vs Side-Effects**: `(queue_id, idempotency_key)` uniqueness guarantees that repeated submissions of the same job return the existing record without duplicate queuing. However, downstream task handlers performing non-idempotent mutations (e.g. charging a payment gateway) must implement domain-level idempotency tokens.
- **Retry & DLQ State Machine**: Jobs fail with structured backoff (`fixed`, `linear`, `exponential`). When `current_retry_count >= max_retries`, the job transitions to `FAILED` or `DEAD_LETTER`, persisting execution history and failure logs for operator inspection.

### 2. Scheduler catch-up policy
- **Bounded Catch-Up**: Recurring schedules are evaluated by Celery Beat every 30 seconds via `croniter`.
- **Occurrence Uniqueness**: Every fire tick is durably recorded in `schedule_occurrences` keyed by `(schedule_id, scheduled_fire_time)` before the job is queued.
- **Downtime Catch-Up Policy**: If the scheduler is stopped or delayed, Beat evaluates only the current window; Pulse intentionally does *not* flood the system with hundreds of backlogged historical jobs upon restart.
- **Fail-Closed Malformed Cron**: If an invalid cron syntax is encountered in legacy data, the schedule is automatically disabled (`is_active=False`), marked invalid, and audited (`schedule.invalid_disabled`), allowing other healthy schedules to continue uninterrupted.

### 3. Transactional outbox delivery semantics
- **Atomic Database Persistence**: Every state transition that causes an external side effect (Celery job dispatch, real-time SSE broadcasts, or outbound webhooks) is written to `outbox_events` in the *same* database transaction as the aggregate change.
- **Row-Locked Polling Publisher**: A periodic Celery worker claims pending outbox rows using `SELECT ... FOR UPDATE SKIP LOCKED` on PostgreSQL, guaranteeing concurrent publishers never double-claim or race.
- **Worker Crash Recovery**: If a publisher crashes after dispatching to Celery but before updating the outbox row, the task is safely redelivered under at-least-once semantics with retry counts bounded to 5 attempts.

### 4. Data retention & table pruning
- **Automated Celery Beat Pruning**: Running on `pulse.internal`, the retention engine runs hourly batch deletions:
  - Job logs & execution history: purged after 30 days.
  - Completed jobs: purged after 60 days.
  - Worker heartbeats: purged after 7 days.
  - Immutable audit logs: retained for 365 days.
- **Chunked Batch Deletions**: Deletes are executed in bounded batches (1,000 rows/batch) to avoid database lock contention and transaction log bloat.

### 5. Webhook secret lifecycle & KMS encryption
- **Secret Generation & Storage**: Webhook secrets are generated with high entropy (`whsec_...`) and encrypted at rest in PostgreSQL via Fernet AES-128 (`enc:v1:...`).
- **Single Raw Secret Exposure**: The raw secret is returned in plaintext *exactly once* upon creation or rotation (`WebhookSubscriptionCreateOut`). All subsequent GET and LIST endpoints mask or omit the secret.
- **Dual-Signing Rotation Grace Window**: Non-destructive rotation creates a new active secret while preserving the previous secret and stamping `previous_secret_expires_at` (default 24 hours). Inbound delivery verification accepts signatures signed by either key until the window expires.
- **Org-Admin Access Boundary**: Creation, modification, deletion, secret rotation, and delivery log inspection are restricted to the `org_admin` or `system_admin` role. Regular members receive `403 Forbidden`.

### 6. Production deployment topology
- **Segmented Networks**: `pulse-internal` (internal: true) isolates PostgreSQL, Redis, and workers with zero exposed host ports. `pulse-ingress` exposes only API and UI behind audited reverse proxies.
- **One-Off Migration Gate**: `docker-compose.prod.yml` runs a dedicated `migration` container executing `alembic upgrade head`; API and workers depend on its successful completion before booting.
- **Least Privilege**: Containers execute as non-root users (`10001:10001` for backend, `1001:1001` for frontend) with `no-new-privileges:true`.

## Threat model & accepted risks

| Threat Vector | Mitigation Strategy | Remaining Accepted Risk / Production Recommendation |
|---|---|---|
| **Server-Side Request Forgery (SSRF)** | Integrated HTTP transport validating all resolved IPv4/IPv6 addresses, blocking RFC1918, loopback, link-local, and cloud metadata (`169.254.169.254`). Redirects strictly disabled (`follow_redirects=False`). Payload capped at 1MB. | Dedicated egress proxy or firewall recommended in enterprise clouds to defend against low-level hardware DNS rebinding. |
| **Token Theft & Credential Leakage** | Query-string JWT authentication completely removed. Enforced httpOnly access/refresh cookies with rotating tokens. Single-use 60s SSE connection tickets. | In cross-origin deployments without reverse proxying, secure cookie domain and CORS settings must be strictly configured. |
| **Rate-Limiter Spoofing & DoS** | Client IP extracted from `X-Forwarded-For` *only* when the direct socket connection matches `TRUSTED_PROXIES`. Sensitive auth routes fail closed (`503`) during Redis outages. 256KB payload caps and 100-job batch limits protect DB memory. | Trusted proxy list must be updated if cloud load balancer IP ranges change. |
| **Cross-Site Request Forgery (CSRF)** | `CSRFProtectionMiddleware` verifies `Origin` and `Referer` headers against `CORS_ORIGINS` for all state-modifying requests bearing cookies. | Custom client applications communicating via raw curl/Postman without cookies are unaffected. |
| **Cryptographic Key Compromise** | Webhook secrets encrypted at rest with Fernet AES-128. Single raw secret exposure on write. Redacted delivery logs. | Production deployments with strict compliance requirements should integrate hardware-backed keys (AWS KMS / GCP Cloud KMS). |
| **Database Schema Reversibility** | Explicit foreign key constraint naming and dialect-guarded DDL verified via automated `upgrade -> downgrade -> upgrade` CI tests. | Irreversible destructive schema changes (e.g. dropping data columns) must be deployed via multi-phase expand/contract patterns. |

## Documentation


The submission documentation is intentionally small:

| File | Purpose |
|---|---|
| `README.md` | Project overview, setup, usage, testing, limitations |
| `docs/architecture.md` | System components and execution flow |
| `docs/design-decisions.md` | Important engineering decisions and trade-offs |
| `docs/er-diagram.md` | Current database/entity model |
| `docs/Pulse-Technical-Documentation.docx` | Formal submission document |

## Editing the documentation safely

When the implementation changes, update documentation in this order:

1. **README.md** — change only user-facing behavior, commands, features, and repository structure.
2. **architecture.md** — update components or request/execution flow when service boundaries change.
3. **design-decisions.md** — add or revise a decision only when the implementation introduces a meaningful architectural trade-off.
4. **er-diagram.md** — update whenever models, relationships, fields, or constraints change.
5. **Pulse-Technical-Documentation.docx** — update the formal document last, using the four source documents and the code as the authority.

Do not describe a planned feature as implemented. If a guarantee is conditional, state the condition explicitly.

## License / submission note

This repository is prepared as an internship/portfolio submission. Add a project-specific license only if you intend to publish the source under one.
