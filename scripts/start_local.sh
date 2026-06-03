#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────────────────
# start_local.sh — Start all 3 ingestion agents locally (no Docker)
# All Azure resources (Blob, Search, Service Bus, Foundry) are real Azure.
#
# Usage:
#   chmod +x scripts/start_local.sh
#   ./scripts/start_local.sh
# ─────────────────────────────────────────────────────────────────────────────
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

if [ ! -f .env ]; then
  echo "ERROR: .env not found. Copy .env.template → .env and fill in values."
  exit 1
fi

# Activate venv if present
if [ -d ".venv" ]; then source .venv/bin/activate; fi

echo "▶ Starting Ingestion Agent   :8010"
python -m uvicorn agents.ingestion_agent:app  --host 0.0.0.0 --port 8010 --reload &
PID1=$!
sleep 3

echo "▶ Starting Processing Agent  :8011"
python -m uvicorn agents.processing_agent:app --host 0.0.0.0 --port 8011 --reload &
PID2=$!
sleep 3

echo "▶ Starting Embedding Agent   :8012"
python -m uvicorn agents.embedding_agent:app  --host 0.0.0.0 --port 8012 --reload &
PID3=$!

echo ""
echo "══════════════════════════════════════════════════════"
echo "  All ingestion agents running."
echo ""
echo "  Health checks:"
echo "    curl http://localhost:8010/health"
echo "    curl http://localhost:8011/health"
echo "    curl http://localhost:8012/health"
echo ""
echo "  Manual ingest trigger:"
echo '    curl -X POST http://localhost:8010/ingest/folder \'
echo '      -H "Content-Type: application/json" \'
echo '      -d '"'"'{"site_id":"<id>","folder_path":"/Shared Documents/HR","domain":"hr"}'"'"
echo ""
echo "  Local file test (no SharePoint):"
echo "    python scripts/test_pipeline_local.py --file test_docs/policy.pdf --domain hr"
echo ""
echo "  Press Ctrl+C to stop all agents."
echo "══════════════════════════════════════════════════════"

trap "echo 'Stopping...'; kill $PID1 $PID2 $PID3 2>/dev/null; exit" INT TERM
wait
