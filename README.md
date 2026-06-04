# RAG Ingestion Pipeline — Local Dev

Same code as the ACA production pipeline. Only differences:
- **Auth**: `AzureCliCredential` instead of Managed Identity — run `az login` once
- **Logging**: human-readable stdout instead of JSON

Document Intelligence and AI Search still use API keys (keyless not universally available for DI).
All other services (Blob Storage, Service Bus, Foundry) use `az login`.

---

## Prerequisites

```bash
pip install azure-cli
az login
az account set --subscription <your-subscription-id>
```

Assign yourself these roles (one-time):
```bash
# Blob Storage
az role assignment create \
  --role "Storage Blob Data Contributor" \
  --assignee $(az ad signed-in-user show --query id -o tsv) \
  --scope /subscriptions/<sub>/resourceGroups/<rg>/providers/Microsoft.Storage/storageAccounts/<storage-name>

# Service Bus (if not using connection string)
az role assignment create \
  --role "Azure Service Bus Data Owner" \
  --assignee $(az ad signed-in-user show --query id -o tsv) \
  --scope /subscriptions/<sub>/resourceGroups/<rg>/providers/Microsoft.ServiceBus/namespaces/<sb-namespace>
```

---

## Setup

```bash
cp .env.template .env
# Fill in .env — Document Intelligence key + AI Search key required; rest use az login
pip install -r requirements.txt
```

---

## Running (3 terminals)

```bash
# Terminal 1 — Ingestion Agent (receives SharePoint webhooks + manual triggers)
uvicorn agents.ingestion_agent:app --port 8010 --reload

# Terminal 2 — Processing Agent (SB listener: downloads → parses → chunks)
uvicorn agents.processing_agent:app --port 8011 --reload

# Terminal 3 — Embedding Agent (SB listener: embeds → uploads to AI Search)
uvicorn agents.embedding_agent:app --port 8012 --reload
```

---

## Test: Ingest a local folder from your laptop

```bash
# Point at any folder on your machine — no SharePoint needed
curl -X POST http://localhost:8010/ingest/local \
  -H "Content-Type: application/json" \
  -d '{
    "folder_path": "C:/Users/rajdeepc/Documents/test-docs",
    "domain": "hr",
    "recursive": true
  }'
# Linux/Mac path example: "/home/raj/docs/hr-policies"
```

Supported file types: `.pdf`, `.docx`, `.doc`, `.xlsx`, `.xls`, `.pptx`, `.ppt`

The files are uploaded to your Azure Blob Storage (`raw-documents/hr/`) and then
processed through the normal Processing → Embedding pipeline via Service Bus.
Check Terminal 2 and Terminal 3 for live processing logs.

---

## Test: Manual SharePoint folder ingest

```bash
# Trigger ingest of a SharePoint folder
curl -X POST http://localhost:8010/ingest/folder \
  -H "Content-Type: application/json" \
  -d '{
    "site_id": "<your-sharepoint-site-id>",
    "folder_path": "/Shared Documents/HR",
    "domain": "hr",
    "recursive": true
  }'

# Health checks
curl http://localhost:8010/health
curl http://localhost:8011/health
curl http://localhost:8012/health
```

## Test: Reindex job (runs in-process, no ACA needed)

```bash
REINDEX_SITE_ID=<site-id> \
REINDEX_FOLDER_PATH="/Shared Documents/HR" \
REINDEX_DOMAIN=hr \
python scripts/run_reindex_job.py
```

---

## Swagger UI

http://localhost:8010/docs — ingestion agent API (webhook + manual ingest endpoints).
