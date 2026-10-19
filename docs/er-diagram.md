# Pulse — Entity Relationship Diagram

This document focuses on the important keys and relationships so the diagram remains readable on GitHub. For exact column definitions and constraints, `backend/app/models.py` and the Alembic migrations are authoritative.

## Current database model

![Pulse entity-relationship diagram](diagrams/er-diagram.png)

*17 entities across auth, tenancy, RBAC, audit logging, scheduling, and execution. The self-referential `Job.depends_on_job_id` relationship is omitted from the rendered image for visual clarity — see [Dependencies](#dependencies) below.*

## How the model is organized

### Tenancy and access hierarchy

```text
User ──── owns ────> Organization ──> Project ──> Queue
  │                        ▲
  └──── member of ─────────┘ (via OrganizationMember: admin | member)

Organization ── owns ──> InviteToken (single-use hashed token for employee onboarding)
Organization ── owns ──> AuditLog (immutable trail of operator actions and mutations)
```

Tenant-owned API resources follow this chain. Access is governed by the `OrganizationMember` authority table:
- **`system_admin`** (platform operator): manages all organizations and users.
- **`org_admin`** (organization admin): owns the org, invites members, manages projects and queues.
- **`member`** (team member): submits, views, and explores jobs within the organization.

### Audit logging

```text
AuditLog
 ├── user_id (nullable actor reference)
 ├── org_id (tenant boundary)
 ├── action & resource_type
 └── details (JSONB metadata)
```

Sensitive operator mutations (queue pause/resume, queue configuration edits, job retries, member invitations and removals, user role changes, schedule toggling) write an immutable record to `audit_logs`.


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
- `(org_id, user_id)` is unique in `organization_members`.
- `invite_tokens.token_hash` is unique.
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
