# Pulse — Entity Relationship Diagram

This document focuses on the important keys and relationships so the diagram remains readable on GitHub. For exact column definitions and constraints, `backend/app/models.py` and the Alembic migrations are authoritative.

## Current database model

```mermaid
erDiagram
    USER ||--o{ ORGANIZATION : owns
    USER ||--o{ REFRESH_TOKEN : has

    ORGANIZATION ||--o{ PROJECT : contains
    PROJECT ||--o{ QUEUE : contains

    QUEUE ||--o{ RETRY_POLICY : defines
    QUEUE ||--o{ JOB_SCHEDULE : schedules
    QUEUE ||--o{ JOB : contains

    RETRY_POLICY ||--o{ JOB : selected_by
    JOB_SCHEDULE ||--o{ JOB : spawns
    JOB ||--o{ JOB_EXECUTION : attempts
    JOB ||--o| DEAD_LETTER_JOB : has_current_dlq
    JOB ||--o{ JOB : has_dependents

    WORKER ||--o{ JOB_EXECUTION : executes
    WORKER ||--o{ WORKER_HEARTBEAT : reports
    WORKER ||--o{ JOB : claims
    JOB_EXECUTION ||--o{ JOB_LOG : produces

    USER {
        UUID id PK
        string email UK
        string hashed_password
        string full_name
    }

    REFRESH_TOKEN {
        UUID id PK
        UUID user_id FK
        UUID family_id
        string token_hash UK
        datetime expires_at
        datetime revoked_at
    }

    ORGANIZATION {
        UUID id PK
        UUID owner_id FK
        string name
    }

    PROJECT {
        UUID id PK
        UUID org_id FK
        string name
        text description
    }

    QUEUE {
        UUID id PK
        UUID project_id FK
        string name
        int priority
        int concurrency_limit
        boolean is_paused
    }

    RETRY_POLICY {
        UUID id PK
        UUID queue_id FK
        string strategy
        int max_retries
        int base_delay_seconds
    }

    JOB_SCHEDULE {
        UUID id PK
        UUID queue_id FK
        string cron_expression
        boolean is_active
        datetime last_dispatched_at
    }

    JOB {
        UUID id PK
        UUID queue_id FK
        UUID schedule_id FK
        UUID retry_policy_id FK
        UUID depends_on_job_id FK
        UUID claimed_by_worker_id FK
        string status
        int priority
        datetime run_at
        string idempotency_key
        int max_retries
        int current_retry_count
        string celery_task_id
    }

    JOB_EXECUTION {
        UUID id PK
        UUID job_id FK
        UUID worker_id FK
        int attempt_number
        string status
        datetime started_at
        datetime finished_at
    }

    JOB_LOG {
        UUID id PK
        UUID job_execution_id FK
        datetime timestamp
        string level
        text message
    }

    WORKER {
        UUID id PK
        string hostname UK
        string celery_worker_id
        string status
        datetime last_heartbeat_at
    }

    WORKER_HEARTBEAT {
        UUID id PK
        UUID worker_id FK
        datetime timestamp
        int active_jobs_count
    }

    DEAD_LETTER_JOB {
        UUID id PK
        UUID job_id FK
        text reason
        int retry_count_at_failure
        datetime failed_at
    }
```

## How the model is organized

### Ownership hierarchy

```text
User → Organization → Project → Queue
```

Tenant-owned API resources follow this chain.

### Job state and execution history

```text
Job
 ├── JobExecution × N
 │     └── JobLog × N
 └── DeadLetterJob (0..1 current record)
```

`Job` stores current state. `JobExecution` records attempts. `JobLog` records attempt-level messages. `DeadLetterJob` represents the job's current terminal DLQ state rather than a second execution history.

### Dependencies

`Job.depends_on_job_id` is a nullable self-reference:

```text
Job A → Job B → Job C
```

The current API creates a dependency against an already-existing job. The implemented scope supports **one dependency per job**, not arbitrary DAG/fan-in workflows.

### Retry policies

A Queue can own multiple retry policies and a Job can optionally reference one through `retry_policy_id`. In the current execution path, the selected policy supplies the retry strategy/base delay while `Job.max_retries` controls retry exhaustion.

### Workers

Workers are shared execution infrastructure. A worker can execute many attempts, report heartbeats, and temporarily appear as the current claimant of jobs. Historical execution records remain if a worker is removed; worker foreign keys use `SET NULL` where appropriate.

## Important constraints

- `users.email` is unique.
- Project names are unique within an organization.
- Queue names are unique within a project.
- Worker hostnames are unique.
- Refresh-token hashes are unique.
- A Job has at most one current `DeadLetterJob` row.
- Job submission idempotency is scoped by `(queue_id, idempotency_key)`.
- `(queue_id, status)` is indexed for queue/job filtering.
- Celery task IDs and other operational lookup fields are indexed.

## Updating the diagram

When the database changes, update this diagram **after** updating `backend/app/models.py` and the corresponding Alembic migration. The code and migration history remain the source of truth.
