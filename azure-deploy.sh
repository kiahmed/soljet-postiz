#!/usr/bin/env bash
# Provision an Azure VM and deploy the Postiz docker-compose stack onto it.
# Idempotent: re-running will reuse existing RG/IP/VM where possible.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

echo "=== Postiz Azure Deploy ==="

# --- Load config -------------------------------------------------------------
[ -f .env ] || { echo "ERROR: .env not found"; exit 1; }
set -a; source .env; set +a

if [ "${AZURE_ENABLED:-false}" != "true" ]; then
  echo "ERROR: AZURE_ENABLED is not 'true' in .env. Set AZURE_ENABLED=true to proceed."
  exit 1
fi

: "${AZURE_SUBSCRIPTION_ID:?AZURE_SUBSCRIPTION_ID is required}"
: "${AZURE_RESOURCE_GROUP:?AZURE_RESOURCE_GROUP is required}"
: "${AZURE_LOCATION:?AZURE_LOCATION is required}"
: "${AZURE_VM_NAME:?AZURE_VM_NAME is required}"
: "${AZURE_VM_SIZE:?AZURE_VM_SIZE is required}"
: "${AZURE_VM_ADMIN_USER:?AZURE_VM_ADMIN_USER is required}"
: "${AZURE_SSH_KEY_PATH:?AZURE_SSH_KEY_PATH is required}"
: "${AZURE_REMOTE_APP_DIR:?AZURE_REMOTE_APP_DIR is required}"

SSH_KEY_PATH="${AZURE_SSH_KEY_PATH/#\~/$HOME}"
if [ ! -f "$SSH_KEY_PATH" ]; then
  echo "ERROR: SSH public key not found at $SSH_KEY_PATH"
  echo "       Generate one with:  ssh-keygen -t ed25519 -f ~/.ssh/id_rsa -N ''"
  exit 1
fi

# --- Tooling check -----------------------------------------------------------
command -v az >/dev/null  || { echo "ERROR: az CLI not installed"; exit 1; }
command -v ssh >/dev/null || { echo "ERROR: ssh not installed"; exit 1; }
command -v scp >/dev/null || { echo "ERROR: scp not installed"; exit 1; }

# --- Azure auth --------------------------------------------------------------
echo "--- Verifying Azure subscription ---"
ACTIVE_SUB=$(az account show --query id -o tsv 2>/dev/null || true)
if [ -z "$ACTIVE_SUB" ]; then
  echo "ERROR: Not logged in to Azure. Run: az login"
  exit 1
fi
if [ "$ACTIVE_SUB" != "$AZURE_SUBSCRIPTION_ID" ]; then
  echo "Switching to subscription $AZURE_SUBSCRIPTION_ID..."
  az account set --subscription "$AZURE_SUBSCRIPTION_ID"
fi
echo "  Subscription: $(az account show --query name -o tsv) ($AZURE_SUBSCRIPTION_ID)"

# --- Resource group ----------------------------------------------------------
echo ""
echo "--- Resource group ---"
if az group show -n "$AZURE_RESOURCE_GROUP" &>/dev/null; then
  echo "  $AZURE_RESOURCE_GROUP exists, reusing"
else
  echo "  Creating $AZURE_RESOURCE_GROUP in $AZURE_LOCATION"
  az group create -n "$AZURE_RESOURCE_GROUP" -l "$AZURE_LOCATION" --tags app=postiz managed-by=azure-deploy.sh -o none
fi

# --- Cloud-init --------------------------------------------------------------
echo ""
echo "--- Preparing cloud-init ---"
[ -f cloud-init.yaml ] || { echo "ERROR: cloud-init.yaml not found"; exit 1; }
RENDERED_CLOUD_INIT="$(mktemp)"
trap 'rm -f "$RENDERED_CLOUD_INIT"' EXIT
sed -e "s|{{ADMIN_USER}}|$AZURE_VM_ADMIN_USER|g" \
    -e "s|{{APP_DIR}}|$AZURE_REMOTE_APP_DIR|g" \
    cloud-init.yaml > "$RENDERED_CLOUD_INIT"
echo "  Rendered to $RENDERED_CLOUD_INIT"

# --- VM ----------------------------------------------------------------------
echo ""
echo "--- Virtual machine ---"
DNS_NAME_OPT=()
if [ -n "${AZURE_DNS_LABEL:-}" ]; then
  DNS_NAME_OPT=(--public-ip-address-dns-name "$AZURE_DNS_LABEL")
fi

if az vm show -g "$AZURE_RESOURCE_GROUP" -n "$AZURE_VM_NAME" &>/dev/null; then
  echo "  VM $AZURE_VM_NAME exists, reusing (cloud-init NOT re-run on existing VMs)"
else
  echo "  Creating VM $AZURE_VM_NAME ($AZURE_VM_SIZE, $AZURE_VM_IMAGE)..."
  az vm create \
    --resource-group "$AZURE_RESOURCE_GROUP" \
    --name "$AZURE_VM_NAME" \
    --image "$AZURE_VM_IMAGE" \
    --size "$AZURE_VM_SIZE" \
    --admin-username "$AZURE_VM_ADMIN_USER" \
    --ssh-key-values "$SSH_KEY_PATH" \
    --os-disk-size-gb "${AZURE_VM_OS_DISK_GB:-64}" \
    --custom-data "$RENDERED_CLOUD_INIT" \
    --public-ip-sku Standard \
    "${DNS_NAME_OPT[@]}" \
    --nsg-rule NONE \
    --tags app=postiz \
    -o none
fi

# --- NSG: open configured ports ---------------------------------------------
echo ""
echo "--- Network security group rules ---"
NSG_NAME="${AZURE_VM_NAME}NSG"
if ! az network nsg show -g "$AZURE_RESOURCE_GROUP" -n "$NSG_NAME" &>/dev/null; then
  echo "ERROR: NSG $NSG_NAME not found (expected after VM create)"
  exit 1
fi

priority=1000
IFS=',' read -ra PORTS <<< "${AZURE_OPEN_PORTS:-22,80,443,4007,8080}"
for port in "${PORTS[@]}"; do
  port="${port// /}"
  [ -z "$port" ] && continue
  rule_name="allow-tcp-${port}"
  source_cidr="*"
  if [ "$port" = "22" ] && [ -n "${AZURE_SSH_SOURCE_CIDR:-}" ]; then
    source_cidr="$AZURE_SSH_SOURCE_CIDR"
  fi
  if az network nsg rule show -g "$AZURE_RESOURCE_GROUP" --nsg-name "$NSG_NAME" -n "$rule_name" &>/dev/null; then
    echo "  rule $rule_name exists"
  else
    echo "  creating rule $rule_name (source: $source_cidr)"
    az network nsg rule create \
      -g "$AZURE_RESOURCE_GROUP" --nsg-name "$NSG_NAME" -n "$rule_name" \
      --priority "$priority" --access Allow --direction Inbound --protocol Tcp \
      --source-address-prefixes "$source_cidr" --source-port-ranges '*' \
      --destination-address-prefixes '*' --destination-port-ranges "$port" \
      -o none
  fi
  priority=$((priority + 10))
done

# --- Resolve public address --------------------------------------------------
echo ""
echo "--- Public address ---"
PUBLIC_IP=$(az vm show -d -g "$AZURE_RESOURCE_GROUP" -n "$AZURE_VM_NAME" --query publicIps -o tsv)
PUBLIC_FQDN=$(az vm show -d -g "$AZURE_RESOURCE_GROUP" -n "$AZURE_VM_NAME" --query fqdns -o tsv)
PUBLIC_HOST="${PUBLIC_FQDN:-$PUBLIC_IP}"
echo "  IP:   $PUBLIC_IP"
echo "  FQDN: ${PUBLIC_FQDN:-<none>}"

# --- Wait for SSH ------------------------------------------------------------
echo ""
echo "--- Waiting for SSH (up to ~3 min) ---"
SSH_OPTS=(-o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -o ConnectTimeout=10)
for i in $(seq 1 18); do
  if ssh "${SSH_OPTS[@]}" "$AZURE_VM_ADMIN_USER@$PUBLIC_IP" "echo ok" &>/dev/null; then
    echo "  SSH ready"
    break
  fi
  [ "$i" -eq 18 ] && { echo "ERROR: SSH never came up"; exit 1; }
  sleep 10
done

# --- Wait for cloud-init -----------------------------------------------------
echo ""
echo "--- Waiting for cloud-init bootstrap (Docker install) ---"
for i in $(seq 1 30); do
  if ssh "${SSH_OPTS[@]}" "$AZURE_VM_ADMIN_USER@$PUBLIC_IP" \
       "test -f /var/lib/cloud/postiz-bootstrap-complete" &>/dev/null; then
    echo "  cloud-init complete"
    break
  fi
  [ "$i" -eq 30 ] && { echo "ERROR: cloud-init didn't finish in 5 min — check /var/log/cloud-init-output.log"; exit 1; }
  sleep 10
done

# --- Build remote .env (rewrite URLs to public host) -------------------------
echo ""
echo "--- Preparing remote .env ---"
REMOTE_ENV="$(mktemp)"
trap 'rm -f "$RENDERED_CLOUD_INIT" "$REMOTE_ENV"' EXIT
# Default to http on the configured Postiz port (TLS reverse proxy is a follow-up)
PUBLIC_BASE="http://${PUBLIC_HOST}:${POSTIZ_PORT:-4007}"
awk -v base="$PUBLIC_BASE" '
  BEGIN { OFS="=" }
  /^MAIN_URL=/                  { print "MAIN_URL=" base; next }
  /^FRONTEND_URL=/              { print "FRONTEND_URL=" base; next }
  /^NEXT_PUBLIC_BACKEND_URL=/   { print "NEXT_PUBLIC_BACKEND_URL=" base "/api"; next }
  { print }
' .env > "$REMOTE_ENV"
echo "  MAIN_URL/FRONTEND_URL rewritten to $PUBLIC_BASE"

# --- Copy app files to VM ----------------------------------------------------
echo ""
echo "--- Copying app files to $AZURE_REMOTE_APP_DIR ---"
SCP_OPTS=(-o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null)
ssh "${SSH_OPTS[@]}" "$AZURE_VM_ADMIN_USER@$PUBLIC_IP" \
    "sudo chown -R $AZURE_VM_ADMIN_USER:$AZURE_VM_ADMIN_USER $AZURE_REMOTE_APP_DIR && mkdir -p $AZURE_REMOTE_APP_DIR/dynamicconfig"
scp "${SCP_OPTS[@]}" docker-compose.yaml deploy.sh status.sh teardown.sh update.sh \
    "$AZURE_VM_ADMIN_USER@$PUBLIC_IP:$AZURE_REMOTE_APP_DIR/"
scp "${SCP_OPTS[@]}" "$REMOTE_ENV" \
    "$AZURE_VM_ADMIN_USER@$PUBLIC_IP:$AZURE_REMOTE_APP_DIR/.env"
scp "${SCP_OPTS[@]}" -r dynamicconfig/ \
    "$AZURE_VM_ADMIN_USER@$PUBLIC_IP:$AZURE_REMOTE_APP_DIR/"
ssh "${SSH_OPTS[@]}" "$AZURE_VM_ADMIN_USER@$PUBLIC_IP" \
    "chmod +x $AZURE_REMOTE_APP_DIR/*.sh"

# --- Run deploy.sh on VM -----------------------------------------------------
echo ""
echo "--- Running deploy.sh on the VM ---"
ssh "${SSH_OPTS[@]}" "$AZURE_VM_ADMIN_USER@$PUBLIC_IP" \
    "cd $AZURE_REMOTE_APP_DIR && ./deploy.sh"

echo ""
echo "=== Azure Deployment Complete ==="
echo "Postiz UI:    $PUBLIC_BASE"
echo "Temporal UI:  http://${PUBLIC_HOST}:8080  (close port 8080 in production!)"
echo "SSH:          ssh $AZURE_VM_ADMIN_USER@$PUBLIC_IP"
echo ""
echo "Next steps:"
echo "  - Front this with Caddy/Nginx + Let's Encrypt for HTTPS"
echo "  - Tighten NSG: remove 8080 and restrict 22 to AZURE_SSH_SOURCE_CIDR"
echo "  - Run ./azure-status.sh to check health later"
