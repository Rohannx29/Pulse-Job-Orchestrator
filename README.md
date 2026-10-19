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
| CI/CD & Multi-Arch Container Builds | ✅ | GitHub Actions workflow with PostgreSQL 16 & Redis 7 service containers, Ruff linting, coverage reporting, Next.js build verification, Docker Compose smoke tests, and Buildx multi-arch images (`linux/amd64`, `linux/arm64`) |
| End-to-End Testing (Playwright) | ✅ | Automated browser test suite covering registration, auth lifecycle, project/queue management, multi-parent DAG workflow inspection, outbound webhooks, and audit logs |


## Repository structure

```text
pulse/
├── README.md
├── docker-compose.yml
├── .github/
│   ├── dependabot.yml         # Automated dependency vulnerability scanning
│   └── workflows/
│       └── ci.yml             # Automated testing, linting, Next.js build & multi-arch container delivery
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

The backend test suite contains **99 test cases across 20 test modules** in `backend/tests/`, covering retry math, priority mapping, concurrency, multi-parent DAG dependencies, transactional outbox race prevention, schedule deduplication, cookie authentication, network security, SSRF defense in depth, KMS webhook encryption, secret rotation with grace windows, delivery redaction, rate limiting, and retention pruning. In addition, the frontend includes 5 end-to-end automated browser test suites in `frontend/e2e/` powered by Playwright.

Run backend tests:

```bash
cd backend
python -m pytest tests/ -v
```

Targeted test suites:

```bash
python -m pytest tests/test_network_security.py -v
python -m pytest tests/test_outbox_concurrency.py -v
python -m pytest tests/test_webhook_rbac.py -v
python -m pytest tests/test_webhooks.py -v
python -m pytest tests/test_dag.py -v
python -m pytest tests/test_dependencies.py -v
python -m pytest tests/test_cookie_auth_integration.py -v
```

Integration tests require the dependencies specified by the project and, where applicable, real PostgreSQL/Redis services. A test that cannot collect because dependencies are absent is an environment/setup failure, not evidence that the application logic passed.

## Security model

Pulse currently provides:

- password hashing (Argon2 / bcrypt)
- httpOnly cookie-based access/refresh authentication (query-string JWT rejected across all APIs)
- short-lived, single-use SSE connection tickets (`POST /auth/sse-ticket`) with atomic consumption
- refresh-token hashing at rest and family-based reuse detection
- role-based access control (System Admin, Org Admin, Member)
- single-use, hashed invite tokens for team onboarding
- explicit CORS origin and same-origin `/api` Next.js reverse proxy configuration
- Cross-Site Request Forgery (CSRF) protection middleware on state-modifying requests bearing cookies
- trusted proxy client IP extraction preventing `X-Forwarded-For` header spoofing
- fail-closed sliding-window rate limiting on sensitive endpoints (`/auth/login`, `/auth/register`) during Redis outages
- defense-in-depth SSRF HTTP client (IPv4/IPv6 address validation, cloud metadata blocking, disabled redirects, bounded timeouts and capped 1MB payloads)
- KMS secret encryption at rest (Fernet AES-128) for outbound webhook secrets with non-destructive rotation grace windows
- automatic delivery log sanitization and credential redaction
- organization-scoped resource authorization with 404 responses for cross-tenant access
- private `/metrics` protection via private network restriction and Bearer tokens

The HTTP handler is hardened against common private/loopback/link-local targets, but DNS rebinding is a deeper network-level problem. Production deployment should add egress controls or a dedicated proxy.

## Execution semantics

Pulse uses **at-least-once execution semantics**.

With late task acknowledgement, a worker crash can cause Celery to redeliver a task. If an external side effect occurred before the crash, that side effect may happen again.

Therefore:

> `idempotency_key` makes **job submission** idempotent; it does not make arbitrary external side effects exactly-once.

## Current limitations

The following are intentionally outside the current implementation scope:

- Dedicated egress proxy / network firewall for hardware-level DNS rebinding defense
- Active lease heartbeat renewal for jobs exceeding 30-minute durations
- Cloud KMS / HSM-backed hardware keystore (currently software Fernet AES-128 master key)
- Distributed OpenTelemetry tracing collector export (currently structured single-line JSON logs with correlation IDs)

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
