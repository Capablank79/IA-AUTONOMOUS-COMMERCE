# Multi-stage deterministic, non-root Dockerfile for AI Autonomous Commerce Platform (O.13)
# Base stage: Python 3.10 slim
FROM python:3.10-slim AS builder

WORKDIR /app

# Prevent python from writing bytecode and buffer stdout
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

# Install build dependencies
RUN apt-get update && \
    apt-get install -y --no-install-recommends build-essential && \
    rm -rf /var/lib/apt/lists/*

# Copy pyproject.toml to install dependencies
COPY pyproject.toml .

# Install dependencies into wheels directory
RUN pip install --no-cache-dir --upgrade pip && \
    pip wheel --no-cache-dir --wheel-dir /app/wheels -e . || \
    pip wheel --no-cache-dir --wheel-dir /app/wheels .

# Final stage: Runtime with unprivileged user
FROM python:3.10-slim AS runner

# Security: Non-root user with UID/GID 10001
ARG USERNAME=appuser
ARG USER_UID=10001
ARG USER_GID=10001

RUN groupadd --gid ${USER_GID} ${USERNAME} && \
    useradd --uid ${USER_UID} --gid ${USER_GID} -m -s /bin/bash ${USERNAME}

WORKDIR /app

# Runtime environment settings
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app \
    ENVIRONMENT=production \
    HOST=0.0.0.0 \
    PORT=8000 \
    DATA_DIR=/app/data \
    LOG_LEVEL=INFO

# Install wheels from builder stage
COPY --from=builder /app/wheels /wheels
RUN pip install --no-cache-dir /wheels/* && \
    rm -rf /wheels

# Create isolated data directory for persistence and assign ownership
RUN mkdir -p /app/data && \
    chown -R ${USERNAME}:${USERNAME} /app

# Copy application source code and entrypoint
COPY --chown=${USERNAME}:${USERNAME} src/ /app/src/
COPY --chown=${USERNAME}:${USERNAME} oauth/ /app/oauth/
COPY --chown=${USERNAME}:${USERNAME} scripts/ /app/scripts/
COPY --chown=${USERNAME}:${USERNAME} pyproject.toml /app/

# Switch to non-root user
USER ${USERNAME}

# Expose canonical platform port
EXPOSE 8000

# Health check probe using python stdlib (no curl/wget dependency needed)
HEALTHCHECK --interval=30s --timeout=5s --start-period=5s --retries=3 \
    CMD python -c "import os, urllib.request; urllib.request.urlopen('http://127.0.0.1:' + str(os.environ.get('PORT', 8000)) + '/health').read()" || exit 1

# Canonical entrypoint
ENTRYPOINT ["python", "scripts/entrypoint.py"]
