from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.config import settings
from app.csrf import CSRFProtectionMiddleware
from app.logging_config import CorrelationIdMiddleware, setup_logging
from app.routers import admin, audit, auth, dlq, events, health, jobs, orgs, projects, queues, schedules, webhooks, workers

# Initialize structured JSON logging
if settings.LOG_FORMAT == "json":
    setup_logging(service_name=settings.SERVICE_NAME, level=settings.LOG_LEVEL)

# Schema is managed by Alembic (see backend/alembic/), not created here.
# Run `alembic upgrade head` before starting the app — docker-compose's
# api service does this automatically via its entrypoint.

app = FastAPI(title="Pulse", description="Distributed job scheduling & orchestration API", version="0.1.0")

app.add_middleware(CorrelationIdMiddleware)
app.add_middleware(CSRFProtectionMiddleware)
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.CORS_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
    expose_headers=["X-Has-More", "X-Next-Cursor", "X-Correlation-ID"],
)

app.include_router(health.router)
app.include_router(auth.router)
app.include_router(orgs.router)
app.include_router(admin.router)
app.include_router(audit.router)
app.include_router(events.router)
app.include_router(projects.router)
app.include_router(queues.router)
app.include_router(jobs.router)
app.include_router(schedules.router)
app.include_router(workers.router)
app.include_router(dlq.router)
app.include_router(webhooks.router)

