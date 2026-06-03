# Single Dockerfile for all 3 ingestion agents.
# Build arg AGENT_MODULE selects which agent runs.
#
# Build examples:
#   docker build --build-arg AGENT_MODULE=agents.ingestion_agent  --build-arg AGENT_PORT=8010 -t rag-ingestion .
#   docker build --build-arg AGENT_MODULE=agents.processing_agent --build-arg AGENT_PORT=8011 -t rag-processing .
#   docker build --build-arg AGENT_MODULE=agents.embedding_agent  --build-arg AGENT_PORT=8012 -t rag-embedding .

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

COPY shared/   ./shared/
COPY tools/    ./tools/         2>/dev/null || true
COPY processors/ ./processors/
COPY agents/   ./agents/

RUN find . -name "*.pyc" -delete && \
    find . -name "__pycache__" -type d -exec rm -rf {} + 2>/dev/null || true

USER appuser

ARG AGENT_MODULE=agents.ingestion_agent
ARG AGENT_PORT=8010
ENV AGENT_MODULE=${AGENT_MODULE}
ENV AGENT_PORT=${AGENT_PORT}
ENV RUNNING_IN_AZURE=true
ENV PYTHONUNBUFFERED=1

EXPOSE ${AGENT_PORT}

HEALTHCHECK --interval=30s --timeout=10s --start-period=20s --retries=3 \
    CMD curl -f http://localhost:${AGENT_PORT}/health || exit 1

CMD ["sh", "-c", "python -m uvicorn ${AGENT_MODULE}:app --host 0.0.0.0 --port ${AGENT_PORT} --workers 1"]
