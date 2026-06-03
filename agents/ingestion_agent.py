"""
Ingestion Agent
===============
MAF Functional Workflow (@workflow / @step).

Two entry points:
  1. POST /webhook/sharepoint  — SharePoint change notification
     → validates clientState secret
     → uses Graph delta API to identify created/updated/deleted files
     → queues IngestionTask per file to SB ingestion-tasks queue

  2. POST /ingest/folder       — manual folder scan
     → lists all files in folder (recursive)
     → queues IngestionTask per file

For each IngestionTask:
  → downloads file bytes via Graph API
  → uploads raw bytes to Blob Storage (raw-documents/<domain>/<filename>)
  → sends ProcessingTask to SB processing-tasks queue
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
from contextlib import asynccontextmanager
from dataclasses import asdict
from datetime import datetime, timezone

import uvicorn
from agent_framework import step, workflow
from azure.storage.blob.aio import BlobServiceClient as AsyncBlobClient
from fastapi import FastAPI, HTTPException, Query, Request, Response
from fastapi.responses import PlainTextResponse

from shared.azure_clients import get_service_bus_client
from shared.config import settings
from shared.graph_client import graph_client
from shared.logging_config import configure_logging, get_logger
from shared.models import (
    IngestionTask,
    ManualIngestRequest,
    ProcessingTask,
    TriggerType,
    WebhookNotification,
)
from shared.service_bus import send_to_queue

configure_logging("rag-ingestion")
logger = get_logger(__name__)

_SUPPORTED_EXTENSIONS = {".pdf", ".docx", ".doc", ".xlsx", ".xls", ".pptx", ".ppt"}

# Persisted delta tokens per (site_id, drive_id) — in production store in Azure Table Storage
_delta_tokens: dict[str, str] = {}


# ── Blob helper ───────────────────────────────────────────────────────────────

async def _upload_to_blob(blob_path: str, data: bytes) -> None:
    """Upload raw file bytes to raw-documents container."""
    from azure.identity.aio import AzureCliCredential, ManagedIdentityCredential

    credential = (
        ManagedIdentityCredential() if os.getenv("RUNNING_IN_AZURE")
        else AzureCliCredential()
    )
    async with AsyncBlobClient(
        account_url=f"https://{settings.AZURE_STORAGE_ACCOUNT_NAME}.blob.core.windows.net",
        credential=credential,
    ) as blob_service:
        container = blob_service.get_container_client(settings.AZURE_STORAGE_CONTAINER_RAW)
        blob_client = container.get_blob_client(blob_path)
        await blob_client.upload_blob(data, overwrite=True)
        logger.debug("Uploaded blob: %s (%d bytes)", blob_path, len(data))


# ── Step: process one file ────────────────────────────────────────────────────

@step
async def ingest_one_file(task: IngestionTask) -> ProcessingTask:
    """
    Download file → upload to Blob → send ProcessingTask to Service Bus.
    """
    if task.is_delete:
        # For deletes we don't download — just forward the delete signal
        processing_task = ProcessingTask(
            ingestion_task_id = task.task_id,
            domain            = task.domain,
            doc_name          = task.doc_name,
            doc_url           = task.doc_url,
            file_type         = task.file_type,
            processed_blob_path = "",
            is_delete         = True,
        )
        await send_to_queue(settings.SB_QUEUE_PROCESSING, asdict(processing_task))
        logger.info("Delete signal queued for doc_name=%s", task.doc_name,
                    extra={"task_id": task.task_id, "doc_name": task.doc_name})
        return processing_task

    # Download from SharePoint
    logger.info("Downloading doc_name=%s", task.doc_name,
                extra={"task_id": task.task_id, "doc_name": task.doc_name, "domain": task.domain})
    file_bytes = await graph_client.download_file(task.site_id, task.drive_id, task.item_id)

    # Upload to Blob
    await _upload_to_blob(task.blob_path, file_bytes)

    # Queue processing task
    processing_task = ProcessingTask(
        ingestion_task_id   = task.task_id,
        domain              = task.domain,
        doc_name            = task.doc_name,
        doc_url             = task.doc_url,
        file_type           = task.file_type,
        processed_blob_path = "",   # Processing Agent fills this
        is_delete           = False,
    )
    await send_to_queue(settings.SB_QUEUE_PROCESSING, asdict(processing_task),
                        correlation_id=task.task_id)
    logger.info("Queued processing task for doc_name=%s", task.doc_name)
    return processing_task


# ── Workflow: ingest a list of tasks ─────────────────────────────────────────

@workflow(name="ingestion_workflow")
async def ingestion_workflow(tasks: list[IngestionTask]) -> dict:
    """Fan-out: ingest all files in parallel (up to 10 at a time)."""
    semaphore = asyncio.Semaphore(10)

    async def _bounded(task: IngestionTask):
        async with semaphore:
            return await ingest_one_file(task)

    results = await asyncio.gather(*[_bounded(t) for t in tasks], return_exceptions=True)
    success = sum(1 for r in results if not isinstance(r, Exception))
    failed  = sum(1 for r in results if isinstance(r, Exception))
    for i, r in enumerate(results):
        if isinstance(r, Exception):
            logger.error("Failed to ingest task %s: %s", tasks[i].task_id, r)
    return {"total": len(tasks), "success": success, "failed": failed}


# ── Helpers: build IngestionTask from Graph item ──────────────────────────────

def _item_to_task(item: dict, domain: str, trigger_type: str, is_delete: bool = False) -> IngestionTask | None:
    doc_name  = item.get("name", "")
    ext       = "." + doc_name.lower().rsplit(".", 1)[-1] if "." in doc_name else ""
    if ext not in _SUPPORTED_EXTENSIONS:
        return None

    file_type = ext.lstrip(".")
    drive_id  = item.get("parentReference", {}).get("driveId", "")
    site_id   = item.get("parentReference", {}).get("siteId", "")

    blob_path = f"{domain}/{doc_name}"

    return IngestionTask(
        domain        = domain,
        file_type     = file_type,
        doc_name      = doc_name,
        doc_url       = item.get("webUrl", ""),
        blob_path     = blob_path,
        site_id       = site_id,
        drive_id      = drive_id,
        item_id       = item.get("id", ""),
        trigger_type  = trigger_type,
        is_delete     = is_delete,
    )


# ── FastAPI ───────────────────────────────────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("Ingestion Agent started.")
    yield
    logger.info("Ingestion Agent stopped.")


app = FastAPI(title="RAG Ingestion Agent", lifespan=lifespan)


@app.get("/health")
async def health() -> dict:
    return {"status": "healthy", "agent": "ingestion"}


@app.post("/webhook/sharepoint")
async def sharepoint_webhook(
    req: Request,
    validationToken: str = Query(default=""),
) -> Response:
    """
    SharePoint webhook endpoint.

    Two cases:
      1. Validation handshake (GET/POST with ?validationToken=...) → echo it back as text/plain
      2. Change notification → extract changed items and queue ingestion tasks
    """
    # Validation handshake
    if validationToken:
        return PlainTextResponse(content=validationToken, status_code=200)

    body = await req.json()

    # Validate clientState secret
    for notification in body.get("value", []):
        if notification.get("clientState") != settings.SHAREPOINT_WEBHOOK_SECRET:
            logger.warning("Invalid clientState in webhook notification — ignoring")
            raise HTTPException(status_code=401, detail="Invalid clientState")

    # Use delta API to get actual changed items per subscription/site
    tasks: list[IngestionTask] = []

    for notification in body.get("value", []):
        resource    = notification.get("resource", "")
        site_id     = settings.SHAREPOINT_DEFAULT_SITE_ID

        # Extract site_id from resource path if available
        # resource format: /sites/<site-id>/drive/root
        parts = resource.split("/")
        if "sites" in parts:
            idx = parts.index("sites")
            if idx + 1 < len(parts):
                site_id = parts[idx + 1]

        if not site_id:
            continue

        # Get drive id
        try:
            import httpx
            async with httpx.AsyncClient(timeout=15) as client:
                resp = await client.get(
                    f"https://graph.microsoft.com/v1.0/sites/{site_id}/drive",
                    headers=await graph_client._headers(),
                )
                resp.raise_for_status()
                drive_id = resp.json()["id"]
        except Exception as exc:
            logger.error("Failed to get drive for site=%s: %s", site_id, exc)
            continue

        delta_key = f"{site_id}:{drive_id}"
        delta_token = _delta_tokens.get(delta_key)

        changed_items, new_token = await graph_client.get_changed_items(site_id, drive_id, delta_token)
        _delta_tokens[delta_key] = new_token

        # Determine domain from site or default
        # Map site_id → domain via SITE_DOMAIN_MAP env var or default to 'hr'
        # Format in .env: SITE_DOMAIN_MAP=site-id-1:hr,site-id-2:legal,site-id-3:it
        import os
        site_map = {}
        for pair in os.getenv("SITE_DOMAIN_MAP", "").split(","):
            if ":" in pair:
                k, v = pair.strip().split(":", 1)
                site_map[k.strip()] = v.strip()
        domain = site_map.get(site_id, "hr")

        for item in changed_items:
            is_delete = "deleted" in item
            task = _item_to_task(item, domain, TriggerType.WEBHOOK, is_delete)
            if task:
                tasks.append(task)

    if tasks:
        await ingestion_workflow.run(tasks)
        logger.info("Webhook triggered ingestion of %d files", len(tasks))

    return Response(status_code=202)


@app.post("/ingest/folder")
async def ingest_folder(req: ManualIngestRequest) -> dict:
    """
    Manually trigger ingestion of all files in a SharePoint folder.
    POST body: {site_id, folder_path, domain, recursive}
    """
    logger.info("Manual ingest triggered site=%s folder=%s domain=%s",
                req.site_id, req.folder_path, req.domain)

    items = await graph_client.list_folder_items(req.site_id, req.folder_path, req.recursive)

    tasks: list[IngestionTask] = []
    for item in items:
        task = _item_to_task(item, req.domain, TriggerType.MANUAL, is_delete=False)
        if task:
            tasks.append(task)

    if not tasks:
        return {"status": "no_supported_files", "total": 0}

    result_obj = await ingestion_workflow.run(tasks)
    outputs    = result_obj.get_outputs()
    result     = outputs[0] if outputs else {}

    logger.info("Manual ingest complete: %s", result)
    return {"status": "queued", **result}


@app.post("/webhook/subscribe")
async def subscribe_webhook(site_id: str, notification_url: str) -> dict:
    """Create a new SharePoint webhook subscription."""
    sub = await graph_client.create_subscription(site_id, notification_url)
    return {"subscription_id": sub["id"], "expires": sub["expirationDateTime"]}


@app.post("/webhook/renew")
async def renew_webhook(subscription_id: str) -> dict:
    """Renew an expiring webhook subscription."""
    sub = await graph_client.renew_subscription(subscription_id)
    return {"subscription_id": sub["id"], "expires": sub["expirationDateTime"]}


if __name__ == "__main__":
    uvicorn.run("agents.ingestion_agent:app", host="0.0.0.0", port=8010, reload=False)
