"""
Azure client factories for the ingestion pipeline.
- Foundry + Blob + Service Bus: fully keyless (Managed Identity / CLI)
- Document Intelligence + AI Search: API key (keyless not universally available for DI)
"""
from __future__ import annotations

import os
from functools import lru_cache

from azure.ai.documentintelligence import DocumentIntelligenceClient
from azure.ai.projects import AIProjectClient
from azure.core.credentials import AzureKeyCredential
from azure.identity import AzureCliCredential, ManagedIdentityCredential
from azure.search.documents import SearchClient
from azure.search.documents.indexes import SearchIndexClient
from azure.servicebus import ServiceBusClient
from azure.storage.blob import BlobServiceClient
from openai import AzureOpenAI

from shared.config import settings


def _credential():
    if os.getenv("RUNNING_IN_AZURE"):
        return ManagedIdentityCredential()
    return AzureCliCredential()


@lru_cache(maxsize=1)
def get_foundry_client() -> AIProjectClient:
    return AIProjectClient(
        endpoint=str(settings.AZURE_FOUNDRY_PROJECT_ENDPOINT),
        credential=_credential(),
    )


@lru_cache(maxsize=1)
def get_openai_client() -> AzureOpenAI:
    return get_foundry_client().inference.get_azure_openai_client(
        api_version=settings.AZURE_OPENAI_API_VERSION
    )


@lru_cache(maxsize=1)
def get_document_intelligence_client() -> DocumentIntelligenceClient:
    return DocumentIntelligenceClient(
        endpoint=str(settings.AZURE_DOCUMENT_INTELLIGENCE_ENDPOINT),
        credential=AzureKeyCredential(
            settings.AZURE_DOCUMENT_INTELLIGENCE_KEY.get_secret_value()
        ),
    )


@lru_cache(maxsize=1)
def get_blob_service_client() -> BlobServiceClient:
    """Sync blob client for non-async contexts. Use AsyncBlobClient directly in agents."""
    return BlobServiceClient(
        account_url=f"https://{settings.AZURE_STORAGE_ACCOUNT_NAME}.blob.core.windows.net",
        credential=_credential(),
    )


@lru_cache(maxsize=1)
def get_search_client() -> SearchClient:
    return SearchClient(
        endpoint=str(settings.AZURE_SEARCH_ENDPOINT),
        index_name=settings.AZURE_SEARCH_INDEX,
        credential=AzureKeyCredential(
            settings.AZURE_SEARCH_API_KEY.get_secret_value()
        ),
    )


@lru_cache(maxsize=1)
def get_search_index_client() -> SearchIndexClient:
    return SearchIndexClient(
        endpoint=str(settings.AZURE_SEARCH_ENDPOINT),
        credential=AzureKeyCredential(
            settings.AZURE_SEARCH_API_KEY.get_secret_value()
        ),
    )


def get_service_bus_client() -> ServiceBusClient:
    """New instance per use — context manager. Prefers keyless auth."""
    if os.getenv("RUNNING_IN_AZURE"):
        return ServiceBusClient(
            fully_qualified_namespace=settings.AZURE_SERVICE_BUS_NAMESPACE,
            credential=ManagedIdentityCredential(),
        )
    if settings.AZURE_SERVICE_BUS_CONNECTION_STR:
        return ServiceBusClient.from_connection_string(
            settings.AZURE_SERVICE_BUS_CONNECTION_STR.get_secret_value()
        )
    return ServiceBusClient(
        fully_qualified_namespace=settings.AZURE_SERVICE_BUS_NAMESPACE,
        credential=AzureCliCredential(),
    )
