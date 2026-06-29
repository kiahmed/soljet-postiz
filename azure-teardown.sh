#!/usr/bin/env bash
# Delete the Azure resource group (releases VM, disk, IP, NSG, vnet — everything).
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

[ -f .env ] || { echo "ERROR: .env not found"; exit 1; }
set -a; source .env; set +a

: "${AZURE_RESOURCE_GROUP:?AZURE_RESOURCE_GROUP is required}"

# Guardrail — refuse to act on shared/system RGs
case "$AZURE_RESOURCE_GROUP" in
  ""|NetworkWatcherRG|DefaultResourceGroup*|MC_*|AzureBackup*)
    echo "ERROR: refusing to delete suspicious RG name: $AZURE_RESOURCE_GROUP"
    exit 1
    ;;
esac

echo "=== Postiz Azure Teardown ==="
echo "Subscription: ${AZURE_SUBSCRIPTION_ID:-<unset>}"
echo "Resource group: $AZURE_RESOURCE_GROUP"
echo ""

if [ -n "${AZURE_SUBSCRIPTION_ID:-}" ]; then
  az account set --subscription "$AZURE_SUBSCRIPTION_ID" 2>/dev/null || true
fi

if ! az group show -n "$AZURE_RESOURCE_GROUP" &>/dev/null; then
  echo "RG does not exist — nothing to do."
  exit 0
fi

echo "Resources that will be deleted:"
az resource list -g "$AZURE_RESOURCE_GROUP" --query "[].{Name:name, Type:type}" -o table || true
echo ""

read -p "Type the RG name '$AZURE_RESOURCE_GROUP' to confirm deletion: " confirm
if [ "$confirm" != "$AZURE_RESOURCE_GROUP" ]; then
  echo "Cancelled."
  exit 0
fi

echo "Deleting (this can take 5+ minutes)..."
az group delete -n "$AZURE_RESOURCE_GROUP" --yes --no-wait
echo "Delete request submitted (running async). Check with:"
echo "  az group show -n $AZURE_RESOURCE_GROUP"
