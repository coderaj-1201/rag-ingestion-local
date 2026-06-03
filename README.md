# RAG Ingestion Pipeline

SharePoint → Ingestion Agent → Processing Agent → Embedding Agent → AI Search

## Architecture

```
SharePoint
  │
  ├── Webhook trigger   POST /webhook/sharepoint  (file created/updated/deleted)
  └── Manual trigger    POST /ingest/folder       (scan entire folder)
        │
        ▼
  Ingestion Agent  (port 8010)
  ├── Downloads file via Graph API
  ├── Stores raw bytes → Blob: raw-documents/<domain>/<filename>
  └── Queues IngestionTask → SB: processing-tasks
        │
        ▼
  Processing Agent  (port 8011)
  ├── Downloads raw file from Blob
  ├── Routes to parser: PDF / DOCX / XLSX / PPTX
  │     PDF  → Azure Document Intelligence (layout model) + light LLM page cleaning
  │     DOCX → python-docx (native heading styles H1/H2/H3, tables)
  │     XLSX → openpyxl (named tables, sheet regions)
  │     PPTX → python-pptx (slide title, body, tables)
  ├── Parent-child chunking:
  │     Parent = full section (~1000 tokens) — stored for context
  │     Child  = paragraph / table (~200 tokens) — embedded + searched
  │     Table  = NL summary (embedded) + markdown raw (passed to LLM)
  ├── Stores processed JSON → Blob: processed-chunks/<domain>/<filename>.json
  └── Queues ProcessingTask → SB: embedding-tasks
        │
        ▼
  Embedding Agent  (port 8012)
  ├── Downloads processed chunks JSON from Blob
  ├── Embeds child chunks (batches of 16) via text-embedding-ada-002
  ├── Uploads to AI Search index (idx-rag):
  │     - Children: with content_vector
  │     - Parents: no vector, fetched by parent_id at query time
  └── On delete: removes all chunks for doc_name from index
```

## AI Search Index Schema

Single index `idx-rag` shared with the retrieval pipeline.

| Field | Type | Purpose |
|---|---|---|
| `id` (key) | String | chunk_id |
| `parent_id` | String | links child → parent |
| `chunk_type` | String | title / heading / paragraph / table |
| `domain` | String | hr / legal / it — OData filter |
| `doc_name` | String | filename |
| `source` | String | alias for doc_name — used by retrieval |
| `doc_url` | String | SharePoint URL |
| `file_type` | String | pdf / docx / xlsx / pptx |
| `page_number` | Int32 | page or slide number |
| `title` | String | document-level title (searchable) |
| `section_heading` | String | H1/H2 — semantic ranker keyword |
| `section_subheading` | String | H3/H4 — semantic ranker keyword |
| `content` | String | NL text — embedded + BM25 searched |
| `table_raw` | String | original markdown table — passed to LLM |
| `content_vector` | Vector(1536) | ada-002 embedding of `content` |
| `ingested_at` | String | ISO 8601 timestamp |
| `is_deleted` | Boolean | soft delete flag |

## Setup (first time)

```bash
# 1. Install
cd ingestion-pipeline
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

# 2. Configure
cp .env.template .env
# Fill in: Foundry, Document Intelligence, Blob, Search, Service Bus, SharePoint Graph

# 3. Create Azure resources
python scripts/setup_azure_resources.py   # creates Blob containers + SB queues

# 4. Create Search index
python scripts/create_search_index.py     # creates idx-rag with full schema

# 5. Test with a local file (no SharePoint needed)
mkdir test_docs
cp /path/to/your.pdf test_docs/
python scripts/test_pipeline_local.py --file test_docs/your.pdf --domain hr --query "what is this about"
```

## Run agents (3 terminals)

```bash
# Terminal 1 — Ingestion Agent (webhook + manual trigger)
python -m uvicorn agents.ingestion_agent:app --port 8010 --reload

# Terminal 2 — Processing Agent (SB listener)
python -m uvicorn agents.processing_agent:app --port 8011 --reload

# Terminal 3 — Embedding Agent (SB listener)
python -m uvicorn agents.embedding_agent:app --port 8012 --reload
```

## Trigger ingestion manually

```bash
# Ingest all files in a SharePoint folder
curl -X POST http://localhost:8010/ingest/folder \
  -H "Content-Type: application/json" \
  -d '{"site_id": "<your-site-id>", "folder_path": "/Shared Documents/HR Policies", "domain": "hr", "recursive": true}'
```

## Set up SharePoint webhook

```bash
# 1. Your ingestion agent must be publicly reachable (use ngrok for local dev)
ngrok http 8010

# 2. Register webhook
curl -X POST http://localhost:8010/webhook/subscribe \
  -H "Content-Type: application/json" \
  -d '{"site_id": "<site-id>", "notification_url": "https://<ngrok-url>/webhook/sharepoint"}'

# SharePoint will call your endpoint on every file create/update/delete
```

## SharePoint App Registration (Graph API)

In Azure Portal → Entra ID → App registrations:
1. New registration → name it "RAG Ingestion"
2. API permissions → Add → Microsoft Graph → Application permissions:
   - `Sites.Read.All` (read SharePoint files)
   - `Files.Read.All` (download files)
3. Grant admin consent
4. Certificates & secrets → New client secret → copy to `.env`
