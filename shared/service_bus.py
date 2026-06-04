"""Service Bus send helpers — LOCAL DEV version. Always uses AzureCliCredential."""
from __future__ import annotations

import json
import logging

from azure.servicebus import ServiceBusMessage

logger = logging.getLogger(__name__)


async def send_to_queue(queue_name: str, payload: dict, correlation_id: str = "") -> None:
    """Send a single JSON message to a Service Bus queue."""
    from azure.identity.aio import AzureCliCredential
    from azure.servicebus.aio import ServiceBusClient as AsyncSBClient
    from shared.config import settings

    if settings.AZURE_SERVICE_BUS_CONNECTION_STR:
        sb = AsyncSBClient.from_connection_string(
            settings.AZURE_SERVICE_BUS_CONNECTION_STR.get_secret_value()
        )
    else:
        sb = AsyncSBClient(
            fully_qualified_namespace=settings.AZURE_SERVICE_BUS_NAMESPACE,
            credential=AzureCliCredential(),
        )

    async with sb:
        async with sb.get_queue_sender(queue_name) as sender:
            msg = ServiceBusMessage(
                body=json.dumps(payload),
                correlation_id=correlation_id,
                content_type="application/json",
            )
            await sender.send_messages(msg)
            logger.debug("Sent to queue=%s correlation_id=%s", queue_name, correlation_id)
