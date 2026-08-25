from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.config import settings
from app.routers import auth, dlq, jobs, projects, queues, schedules, workers

# Schema is managed by Alembic (see backend/alembic/), not created here.
# Run `alembic upgrade head` before starting the app — docker-compose's
# api service does this automatically via its entrypoint.

app = FastAPI(title="Pulse", description="Distributed job scheduling & orchestration API", version="0.1.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.CORS_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(auth.router)
app.include_router(projects.router)
app.include_router(queues.router)
app.include_router(jobs.router)
app.include_router(schedules.router)
app.include_router(workers.router)
app.include_router(dlq.router)


@app.get("/health")
def health():
    return {"status": "ok"}
