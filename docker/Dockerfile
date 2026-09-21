FROM python:3.12-slim AS runtime

LABEL maintainer="spec-driven-pipeline"
LABEL description="Production container for the Spec-Driven JIRA-to-GitLab-PR automation pipeline"

# Security: Install curl for healthcheck and create non-root app user
RUN apt-get update && \
    apt-get install -y --no-install-recommends curl && \
    rm -rf /var/lib/apt/lists/* && \
    groupadd -g 10001 appgroup && \
    useradd -u 10001 -g appgroup -s /bin/bash -m appuser

WORKDIR /app

# Install dependencies first (leveraging Docker layer caching)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application source modules
COPY orchestrator/ ./orchestrator/
COPY webhook/ ./webhook/
COPY ci_templates/ ./ci_templates/

# Ensure runtime directories exist with non-root ownership
RUN mkdir -p /app/checkpoints /app/audit && \
    chown -R appuser:appgroup /app

USER appuser

ENV PYTHONPATH=/app \
    PYTHONUNBUFFERED=1

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD curl -f http://localhost:8000/health || exit 1

# Default command: launch the webhook server and serial queue worker
CMD ["python", "-m", "orchestrator.main", "--server"]
