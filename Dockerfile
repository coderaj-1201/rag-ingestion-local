# ─────────────────────────────────────────────────────────────────────────────
# Ingestion Pipeline — Single Dockerfile, one image per agent via build args
#
# Build from REPO ROOT:
#   docker build \
#     --build-arg AGENT_MODULE=agents.ingestion_agent \
#     --build-arg AGENT_PORT=8010 \
#     -t rag-ingestion:latest .
#
#   docker build \
#     --build-arg AGENT_MODULE=agents.processing_agent \
#     --build-arg AGENT_PORT=8011 \
#     -t rag-processing:latest .
#
#   docker build \
#     --build-arg AGENT_MODULE=agents.embedding_agent \
#     --build-arg AGENT_PORT=8012 \
#     -t rag-embedding:latest .
#
# The reindex Job uses the ingestion image with a different CMD:
#   command: ["python", "scripts/run_reindex_job.py"]
# ─────────────────────────────────────────────────────────────────────────────

ARG PYTHON_VERSION=3.12
FROM python:${PYTHON_VERSION}-slim AS base

RUN useradd -m -u 1000 appuser && \
    apt-get update && \
    apt-get install -y --no-install-recommends curl && \
    rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir -r requirements.txt

COPY shared/      ./shared/
COPY processors/  ./processors/
COPY agents/      ./agents/
COPY scripts/     ./scripts/

RUN find . -name "*.pyc" -delete && \
    find . -name "__pycache__" -type d -exec rm -rf {} + 2>/dev/null || true

USER appuser

ARG AGENT_MODULE=agents.ingestion_agent
ARG AGENT_PORT=8010
ENV AGENT_MODULE=${AGENT_MODULE}
ENV AGENT_PORT=${AGENT_PORT}
ENV RUNNING_IN_AZURE=true
ENV PYTHONUNBUFFERED=1
ENV PYTHONDONTWRITEBYTECODE=1

EXPOSE ${AGENT_PORT}

HEALTHCHECK --interval=30s --timeout=10s --start-period=30s --retries=3 \
    CMD curl -f http://localhost:${AGENT_PORT}/health || exit 1

# workers=1 for processing/embedding (heavy per-task work, no benefit from multiple workers)
CMD ["sh", "-c", "python -m uvicorn ${AGENT_MODULE}:app --host 0.0.0.0 --port ${AGENT_PORT} --workers 1"]
