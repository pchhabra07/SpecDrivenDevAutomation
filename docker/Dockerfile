FROM python:3.12-slim

LABEL maintainer="spec-driven-pipeline"
LABEL description="Ephemeral orchestrator for the JIRA-to-GitLab-PR automation pipeline"

WORKDIR /app

# Install dependencies first (layer caching).
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application source.
COPY orchestrator/ ./orchestrator/

ENV PYTHONPATH=/app
ENV PYTHONUNBUFFERED=1

# No credentials in image — inject via --env-file at runtime.
CMD ["python", "-m", "orchestrator.main"]
