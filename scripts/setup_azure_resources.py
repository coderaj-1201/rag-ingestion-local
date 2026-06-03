"""
setup_azure_resources.py
========================
Creates Azure resources needed by the ingestion pipeline:
  - Blob containers: raw-documents, processed-chunks
  - Service Bus queues: ingestion-tasks, processing-tasks, embedding-tasks

Run once before starting agents.
  cd ingestion-pipeline
  python scripts/setup_azure_resources.py

Auth: az login (Contributor access is enough for this)
"""
from __future__ import annotations

import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from azure.identity import AzureCliCredential
from azure.storage.blob import BlobServiceClient
from azure.servicebus.management import ServiceBusAdministrationClient

from shared.config import settings


def create_blob_containers():
    print("\n▶ Creating Blob containers...")
    credential = AzureCliCredential()
    client = BlobServiceClient(
        account_url=f"https://{settings.AZURE_STORAGE_ACCOUNT_NAME}.blob.core.windows.net",
        credential=credential,
    )
    for container in [settings.AZURE_STORAGE_CONTAINER_RAW, settings.AZURE_STORAGE_CONTAINER_PROCESSED]:
        try:
            client.create_container(container)
            print(f"  ✅ Created container: {container}")
        except Exception as e:
            if "ContainerAlreadyExists" in str(e):
                print(f"  ✓  Already exists: {container}")
            else:
                print(f"  ❌ Failed: {container} — {e}")


def create_service_bus_queues():
    print("\n▶ Creating Service Bus queues...")
    conn_str = (
        settings.AZURE_SERVICE_BUS_CONNECTION_STR.get_secret_value()
        if settings.AZURE_SERVICE_BUS_CONNECTION_STR
        else None
    )
    if not conn_str:
        print("  ⚠️  No connection string — skipping (use Managed Identity in production)")
        return

    mgmt = ServiceBusAdministrationClient.from_connection_string(conn_str)
    queues = [
        settings.SB_QUEUE_INGESTION,
        settings.SB_QUEUE_PROCESSING,
        settings.SB_QUEUE_EMBEDDING,
    ]
    for queue_name in queues:
        try:
            mgmt.create_queue(
                queue_name,
                max_delivery_count=3,
                dead_lettering_on_message_expiration=True,
            )
            print(f"  ✅ Created queue: {queue_name}")
        except Exception as e:
            if "409" in str(e) or "already exists" in str(e).lower():
                print(f"  ✓  Already exists: {queue_name}")
            else:
                print(f"  ❌ Failed: {queue_name} — {e}")


if __name__ == "__main__":
    create_blob_containers()
    create_service_bus_queues()
    print("\n✅ Setup complete. Run create_search_index.py next.")
