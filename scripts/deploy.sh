#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────────────────
# scripts/deploy.sh — Build, push, and deploy ingestion pipeline to Azure
#
# Usage:
#   chmod +x scripts/deploy.sh
#   RESOURCE_GROUP=my-rg ENV=prod ./scripts/deploy.sh
# ─────────────────────────────────────────────────────────────────────────────
set -euo pipefail

RESOURCE_GROUP="${RESOURCE_GROUP:?Set RESOURCE_GROUP}"
ENV="${ENV:-prod}"
LOCATION="${LOCATION:-eastus}"
BICEP_FILE="infra/main.bicep"

echo "▶ Verifying Azure CLI login..."
az account show --output none || { echo "Run 'az login' first."; exit 1; }

echo "▶ Ensuring resource group '$RESOURCE_GROUP'..."
az group create --name "$RESOURCE_GROUP" --location "$LOCATION" --output none

echo "▶ Deploying infrastructure..."
DEPLOY_OUTPUT=$(az deployment group create \
  --resource-group "$RESOURCE_GROUP" \
  --template-file "$BICEP_FILE" \
  --parameters \
    environmentName="$ENV" \
    azureFoundryProjectEndpoint="$AZURE_FOUNDRY_PROJECT_ENDPOINT" \
    documentIntelligenceEndpoint="$AZURE_DOCUMENT_INTELLIGENCE_ENDPOINT" \
    documentIntelligenceKey="$AZURE_DOCUMENT_INTELLIGENCE_KEY" \
    searchEndpoint="$AZURE_SEARCH_ENDPOINT" \
    searchApiKey="$AZURE_SEARCH_API_KEY" \
    sharepointTenantId="$SHAREPOINT_TENANT_ID" \
    sharepointClientId="$SHAREPOINT_CLIENT_ID" \
    sharepointClientSecret="$SHAREPOINT_CLIENT_SECRET" \
    sharepointWebhookSecret="$SHAREPOINT_WEBHOOK_SECRET" \
    siteDomainMap="${SITE_DOMAIN_MAP:-}" \
  --name "ingest-deploy-$(date +%Y%m%d%H%M%S)" \
  --output json)

ACR=$(echo "$DEPLOY_OUTPUT" | jq -r '.properties.outputs.acrLoginServer.value')
INGESTION_FQDN=$(echo "$DEPLOY_OUTPUT" | jq -r '.properties.outputs.ingestionFqdn.value')
echo "  ACR: $ACR"

echo "▶ Logging into ACR..."
az acr login --name "${ACR%%.*}"

PREFIX="rag-${ENV}"
declare -A AGENTS=(
  ["rag-ingestion"]="agents.ingestion_agent:8010"
  ["rag-processing"]="agents.processing_agent:8011"
  ["rag-embedding"]="agents.embedding_agent:8012"
)

for IMAGE in "${!AGENTS[@]}"; do
  IFS=':' read -r MODULE PORT <<< "${AGENTS[$IMAGE]}"
  FULL_TAG="${ACR}/${IMAGE}:latest"
  echo "▶ Building ${IMAGE}..."
  docker build \
    --build-arg AGENT_MODULE="$MODULE" \
    --build-arg AGENT_PORT="$PORT" \
    --tag "$FULL_TAG" \
    --platform linux/amd64 .
  echo "▶ Pushing ${FULL_TAG}..."
  docker push "$FULL_TAG"
done

echo "▶ Updating Container Apps..."
for APP_SUFFIX in ingestion processing embedding; do
  az containerapp update \
    --name "${PREFIX}-${APP_SUFFIX}" \
    --resource-group "$RESOURCE_GROUP" \
    --image "${ACR}/rag-${APP_SUFFIX}:latest" \
    --output none
  echo "  Updated ${PREFIX}-${APP_SUFFIX}"
done

echo ""
echo "══════════════════════════════════════════════════════"
echo "  ✅ Ingestion pipeline deployed"
echo "  Ingestion Agent (webhook endpoint): https://${INGESTION_FQDN}"
echo ""
echo "  Register SharePoint webhook:"
echo "    curl -X POST https://${INGESTION_FQDN}/webhook/subscribe \\"
echo "      -H 'Content-Type: application/json' \\"
echo "      -d '{\"site_id\":\"<id>\",\"notification_url\":\"https://${INGESTION_FQDN}/webhook/sharepoint\"}'"
echo ""
echo "  Trigger manual reindex job:"
echo "    az containerapp job start --name ${PREFIX}-reindex-job --resource-group ${RESOURCE_GROUP} \\"
echo "      --env-vars REINDEX_SITE_ID=<id> REINDEX_FOLDER_PATH='/Shared Documents/HR' REINDEX_DOMAIN=hr"
echo "══════════════════════════════════════════════════════"
