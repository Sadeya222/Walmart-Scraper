#!/usr/bin/env bash
# Build, push and run the scraper on Azure Container Instances.
#
#   ./azure/aci/deploy-aci.sh <resource-group> <registry-name> [storage-account]
#
# ACI has no built-in schedule.  Run this from:
#   * an Azure Function timer trigger (see azure/function-app), or
#   * an Azure Logic App recurrence, or
#   * a GitHub Actions schedule (see .github/workflows/azure-deploy.yml).
set -euo pipefail

RG="${1:?usage: deploy-aci.sh <resource-group> <registry-name> [storage-account]}"
REGISTRY="${2:?usage: deploy-aci.sh <resource-group> <registry-name> [storage-account]}"
STORAGE="${3:-wmscraperdata}"
IMAGE="${REGISTRY}.azurecr.io/walmart-scraper:latest"
SHARE="walmart-scraper"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

echo "==> checking prerequisites"
command -v az >/dev/null || { echo "az CLI not found"; exit 1; }
az account show >/dev/null || { echo "not logged in to Azure"; exit 1; }

echo "==> ensuring storage account ${STORAGE} and file share ${SHARE}"
az storage account create --name "${STORAGE}" --resource-group "${RG}" \
    --sku Standard_LRS --kind StorageV2 --only-show-errors >/dev/null || true
az storage share create --name "${SHARE}" \
    --connection-string "$(az storage account show-connection-string -n "${STORAGE}" -g "${RG}" --query connectionString -o tsv)" \
    >/dev/null || true
STORAGE_KEY="$(az storage account keys list -n "${STORAGE}" -g "${RG}" --query '[0].value' -o tsv)"

echo "==> building and pushing ${IMAGE}"
az acr build --registry "${REGISTRY}" --image walmart-scraper:latest \
    --file "${HERE}/../../Dockerfile" "${HERE}/../.."

echo "==> deploying container group"
az deployment group create \
    --resource-group "${RG}" \
    --template-file "${HERE}/aci-template.json" \
    --parameters \
        imageName="${IMAGE}" \
        storageAccountName="${STORAGE}" \
        storageAccountKey="${STORAGE_KEY}" \
        fileShareName="${SHARE}" \
        azureStorageConnectionString="$(az storage account show-connection-string -n "${STORAGE}" -g "${RG}" --query connectionString -o tsv)" \
    --no-wait

echo "==> deployed. Follow the run with:"
echo "    az container logs -g ${RG} -n walmart-scraper --follow"
