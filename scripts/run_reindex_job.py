"""
run_reindex_job.py
==================
Entrypoint for the Azure Container Apps Job (manual trigger).
Reads REINDEX_SITE_ID, REINDEX_FOLDER_PATH, REINDEX_DOMAIN from env,
calls the ingestion workflow directly (no HTTP — same process).

Trigger via:
  az containerapp job start \
    --name rag-prod-reindex-job \
    --resource-group <rg> \
    --env-vars REINDEX_SITE_ID=<id> REINDEX_FOLDER_PATH="/Shared Documents/HR" REINDEX_DOMAIN=hr
"""
from __future__ import annotations

import asyncio
import os
import sys
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from shared.logging_config import configure_logging, get_logger
configure_logging("rag-reindex-job")
logger = get_logger(__name__)


async def main():
    site_id     = os.environ.get("REINDEX_SITE_ID", "")
    folder_path = os.environ.get("REINDEX_FOLDER_PATH", "/")
    domain      = os.environ.get("REINDEX_DOMAIN", "hr")
    recursive   = os.environ.get("REINDEX_RECURSIVE", "true").lower() == "true"

    if not site_id:
        logger.error("REINDEX_SITE_ID not set — exiting")
        sys.exit(1)

    logger.info("Reindex job starting site=%s folder=%s domain=%s", site_id, folder_path, domain)

    from shared.graph_client import graph_client
    from agents.ingestion_agent import _item_to_task, ingestion_workflow
    from shared.models import TriggerType

    items = await graph_client.list_folder_items(site_id, folder_path, recursive)
    logger.info("Found %d files to reindex", len(items))

    tasks = []
    for item in items:
        task = _item_to_task(item, domain, TriggerType.MANUAL, is_delete=False)
        if task:
            tasks.append(task)

    if not tasks:
        logger.info("No supported files found — job complete")
        return

    result_obj = await ingestion_workflow.run(tasks)
    outputs    = result_obj.get_outputs()
    result     = outputs[0] if outputs else {}
    logger.info("Reindex job complete: %s", result)


if __name__ == "__main__":
    asyncio.run(main())
