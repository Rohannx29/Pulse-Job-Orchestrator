# Multi-stage hardened production Dockerfile for Pulse Backend & Workers

# Stage 1: Build dependencies & compile binary wheels
FROM python:3.12-slim AS builder

WORKDIR /build

RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    libpq-dev \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir --prefix=/install -r requirements.txt

# Stage 2: Minimal unprivileged runtime image
FROM python:3.12-slim AS runtime

WORKDIR /app

# Install runtime shared libraries and curl for health checks (no compilers)
RUN apt-get update && apt-get install -y --no-install-recommends \
    libpq5 \
    curl \
    && rm -rf /var/lib/apt/lists/*

# Create unprivileged system user and group (UID/GID 10001)
RUN groupadd -g 10001 pulse && \
    useradd -u 10001 -g pulse -s /bin/sh -d /app -m pulse

# Copy compiled Python packages and CLI entrypoints from builder
COPY --from=builder /install /usr/local

# Copy application code and ensure unprivileged ownership
COPY --chown=pulse:pulse . /app

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PATH="/usr/local/bin:$PATH"

USER pulse

EXPOSE 8000

# Container liveness check
HEALTHCHECK --interval=15s --timeout=5s --start-period=15s --retries=3 \
    CMD curl -f http://localhost:8000/health/live || exit 1

# Default command runs the API; docker-compose overrides for workers, beat, and flower
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
