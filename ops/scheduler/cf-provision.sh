#!/usr/bin/env bash
# Ensure the Cloudflare tunnel public hostname for the GCP-mode trigger exists
# and is active, creating it if missing. GCP Cloud Scheduler POSTs to
# https://<trigger-host>/run; that host must resolve (DNS CNAME -> the tunnel)
# AND the tunnel must have an ingress rule routing it to the sidecar. Both live
# in Cloudflare (the tunnel is token/dashboard-managed), so we provision them
# via the Cloudflare API instead of a manual dashboard step.
#
# Idempotent: run it as many times as you like — it only writes when something
# is missing or wrong. `make scheduler-up` (gcp mode) calls `ensure` before
# creating the Scheduler jobs; `make scheduler-down` calls `remove`.
#
# Usage: cf-provision.sh <ensure|verify|remove>
#   ensure  create-or-update DNS CNAME + tunnel ingress, then report
#   verify  read-only: token scope, resolved ids, and whether both already exist
#   remove  delete the DNS record + drop the ingress rule (leaves other rules)
# Set DRY=1 to print mutating calls without executing (reads still run).
#
# Inputs (auto-discovered — nothing hardcoded):
#   hostname   <- SCHEDULER_TRIGGER_URL in .env         (e.g. trigger.arboryx.ai)
#   zone       <- hostname minus first label            (arboryx.ai)
#   tunnel id  <- CF_TUNNEL_ID, else decoded from auth/cloudflare.yaml run token
#   API token  <- first active cfut_/cfk_ token in auth/cloudflare.yaml
#   account/zone ids <- fetched from the CF API
set -uo pipefail
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=ops/scheduler/lib.sh
source "$DIR/lib.sh"
cd "$REPO_ROOT"

CMD="${1:-ensure}"
API="https://api.cloudflare.com/client/v4"
AUTH_FILE="$REPO_ROOT/auth/cloudflare.yaml"
SERVICE="http://postiz-scheduler-trigger:8090"   # sidecar on postiz-network

# ---- derive hostname / zone / tunnel id -------------------------------------
TRIGGER_URL="$(sched_envget SCHEDULER_TRIGGER_URL)"
HOST="$(printf '%s' "$TRIGGER_URL" | sed -E 's#^https?://##; s#/.*$##')"
[ -n "$HOST" ] || { echo "ERROR: cannot derive hostname from SCHEDULER_TRIGGER_URL='$TRIGGER_URL'" >&2; exit 1; }
ZONE_NAME="${HOST#*.}"                            # trigger.arboryx.ai -> arboryx.ai

TUNNEL_ID="$(sched_envget CF_TUNNEL_ID)"
if [ -z "$TUNNEL_ID" ] && [ -f "$AUTH_FILE" ]; then
  TUNNEL_ID="$(grep -oE 'run --token [A-Za-z0-9]+' "$AUTH_FILE" | awk 'NR==1{print $3}' \
    | python3 -c "import sys,base64,json
t=sys.stdin.read().strip()
print(json.loads(base64.urlsafe_b64decode(t+'='*(-len(t)%4))).get('t','')) if t else print('')" 2>/dev/null)"
fi
[ -n "$TUNNEL_ID" ] || { echo "ERROR: no tunnel id (set CF_TUNNEL_ID, or keep the run token in $AUTH_FILE)" >&2; exit 1; }
CNAME_TARGET="${TUNNEL_ID}.cfargotunnel.com"

# ---- pick a working Cloudflare API token ------------------------------------
[ -f "$AUTH_FILE" ] || { echo "ERROR: $AUTH_FILE not found — no Cloudflare API token to provision with" >&2; exit 1; }
TOKEN=""
for _t in $(grep -oE '(cfut_|cfk_)[A-Za-z0-9]+' "$AUTH_FILE" | sort -u); do
  if curl -sS -m 20 "$API/user/tokens/verify" -H "Authorization: Bearer $_t" \
       | python3 -c "import sys,json;sys.exit(0 if json.load(sys.stdin).get('success') else 1)" 2>/dev/null; then
    TOKEN="$_t"; break
  fi
done
[ -n "$TOKEN" ] || { echo "ERROR: no active Cloudflare API token in $AUTH_FILE (all failed /tokens/verify)" >&2; exit 1; }
AUTH=(-H "Authorization: Bearer $TOKEN")

# ---- HTTP helper: cf METHOD PATH [json] -> body on stdout, honors DRY --------
cf() {
  local method="$1" path="$2" body="${3:-}"
  if [ "$method" != GET ] && [ "${DRY:-}" = "1" ]; then
    printf '  + [DRY] %s %s%s\n' "$method" "$path" "${body:+  $body}" >&2
    printf '{"success":true,"result":{},"_dry":true}'; return 0
  fi
  if [ -n "$body" ]; then
    curl -sS -m 30 -X "$method" "$API$path" "${AUTH[@]}" -H "Content-Type: application/json" --data "$body"
  else
    curl -sS -m 30 -X "$method" "$API$path" "${AUTH[@]}"
  fi
}

# Abort with the CF error if a response isn't success (skips DRY placeholders).
cf_ok() {
  python3 -c "import sys,json
d=json.load(sys.stdin)
if d.get('_dry'): sys.exit(0)
if not d.get('success'):
    sys.stderr.write('CF API error: '+json.dumps(d.get('errors'))+'\n'); sys.exit(1)" || {
    echo "ERROR: Cloudflare API call failed (see above). Token may lack the needed scope." >&2; exit 1; }
}

# ---- resolve zone_id + account_id -------------------------------------------
resolve_ids() {
  local resp; resp="$(cf GET "/zones?name=$ZONE_NAME")"
  printf '%s' "$resp" | python3 -c "import sys,json
d=json.load(sys.stdin)
if not d.get('success'):
    sys.stderr.write('CF error listing zones: '+json.dumps(d.get('errors'))+'\n'); sys.exit(1)
r=d.get('result') or []
if not r:
    sys.stderr.write('ERROR: zone \"$ZONE_NAME\" not found for this token (need Zone:Read on it)\n'); sys.exit(1)
print(r[0]['id']); print((r[0].get('account') or {}).get('id',''))"
}

# ---- DNS: ensure trigger host CNAME -> <tunnel>.cfargotunnel.com (proxied) ---
ensure_dns() {
  local zone_id="$1" resp action
  resp="$(cf GET "/zones/$zone_id/dns_records?type=CNAME&name=$HOST")"; printf '%s' "$resp" | cf_ok <<<"$resp"
  action="$(printf '%s' "$resp" | python3 -c "import sys,json
d=json.load(sys.stdin); res=d.get('result') or []
m=[r for r in res if r.get('name')=='$HOST']
if not m: print('CREATE'); raise SystemExit
r=m[0]
print('NOCHANGE '+r['id'] if (r.get('content')=='$CNAME_TARGET' and r.get('proxied') is True) else 'UPDATE '+r['id'])")"
  local body="{\"type\":\"CNAME\",\"name\":\"$HOST\",\"content\":\"$CNAME_TARGET\",\"proxied\":true,\"ttl\":1,\"comment\":\"postiz scheduler trigger (auto)\"}"
  case "$action" in
    CREATE)      echo "  DNS: creating CNAME $HOST -> $CNAME_TARGET (proxied)"; cf POST "/zones/$zone_id/dns_records" "$body" | cf_ok ;;
    UPDATE*)     echo "  DNS: updating CNAME $HOST -> $CNAME_TARGET"; cf PATCH "/zones/$zone_id/dns_records/${action#UPDATE }" "$body" | cf_ok ;;
    NOCHANGE*)   echo "  DNS: already correct ($HOST -> $CNAME_TARGET)" ;;
  esac
}

# ---- Tunnel: ensure an ingress rule host -> sidecar, keep the rest ----------
ensure_ingress() {
  local acct="$1" resp put
  resp="$(cf GET "/accounts/$acct/cfd_tunnel/$TUNNEL_ID/configurations")"; printf '%s' "$resp" | cf_ok <<<"$resp"
  put="$(printf '%s' "$resp" | HOST="$HOST" SERVICE="$SERVICE" python3 -c "import sys,os,json
host,svc=os.environ['HOST'],os.environ['SERVICE']
d=json.load(sys.stdin)
cfg=(d.get('result') or {}).get('config') or {}
ingress=cfg.get('ingress') or []
named=[r for r in ingress if r.get('hostname')]
catch=[r for r in ingress if not r.get('hostname')]
if any(r.get('hostname')==host and r.get('service')==svc for r in named) and catch:
    print('NOCHANGE'); raise SystemExit
new=[]
seen=False
for r in named:
    if r.get('hostname')==host: r={'hostname':host,'service':svc}; seen=True
    new.append(r)
if not seen: new.append({'hostname':host,'service':svc})
if not catch: catch=[{'service':'http_status:404'}]
cfg['ingress']=new+catch
print('PUT '+json.dumps({'config':cfg}))")"
  if [ "$put" = NOCHANGE ]; then
    echo "  ingress: already routes $HOST -> $SERVICE"
  else
    echo "  ingress: routing $HOST -> $SERVICE on tunnel $TUNNEL_ID"
    cf PUT "/accounts/$acct/cfd_tunnel/$TUNNEL_ID/configurations" "${put#PUT }" | cf_ok
  fi
}

# ---- remove (for scheduler-down) --------------------------------------------
remove_all() {
  local zone_id="$1" acct="$2" resp id put
  # DNS
  resp="$(cf GET "/zones/$zone_id/dns_records?type=CNAME&name=$HOST")"
  id="$(printf '%s' "$resp" | python3 -c "import sys,json
r=(json.load(sys.stdin).get('result') or [])
m=[x for x in r if x.get('name')=='$HOST']
print(m[0]['id'] if m else '')")"
  if [ -n "$id" ]; then echo "  DNS: deleting CNAME $HOST"; cf DELETE "/zones/$zone_id/dns_records/$id" | cf_ok
  else echo "  DNS: no $HOST record to delete"; fi
  # ingress
  resp="$(cf GET "/accounts/$acct/cfd_tunnel/$TUNNEL_ID/configurations")"
  put="$(printf '%s' "$resp" | HOST="$HOST" python3 -c "import sys,os,json
host=os.environ['HOST']
cfg=(json.load(sys.stdin).get('result') or {}).get('config') or {}
ing=cfg.get('ingress') or []
if not any(r.get('hostname')==host for r in ing): print('NOCHANGE'); raise SystemExit
cfg['ingress']=[r for r in ing if r.get('hostname')!=host] or [{'service':'http_status:404'}]
print('PUT '+json.dumps({'config':cfg}))")"
  if [ "$put" = NOCHANGE ]; then echo "  ingress: no $HOST rule to remove"
  else echo "  ingress: removing $HOST rule"; cf PUT "/accounts/$acct/cfd_tunnel/$TUNNEL_ID/configurations" "${put#PUT }" | cf_ok; fi
}

echo "[cf-provision] host=$HOST zone=$ZONE_NAME tunnel=$TUNNEL_ID token=${TOKEN:0:9}… cmd=$CMD"
IDS="$(resolve_ids)"; ZONE_ID="$(printf '%s\n' "$IDS" | sed -n 1p)"; ACCOUNT_ID="$(printf '%s\n' "$IDS" | sed -n 2p)"
[ -n "$ZONE_ID" ] && [ -n "$ACCOUNT_ID" ] || { echo "ERROR: could not resolve zone/account id (token scope?)" >&2; exit 1; }
echo "[cf-provision] zone_id=$ZONE_ID account_id=$ACCOUNT_ID"

case "$CMD" in
  ensure)
    ensure_dns "$ZONE_ID"
    ensure_ingress "$ACCOUNT_ID"
    echo "[cf-provision] ensured: https://$HOST/run -> $SERVICE" ;;
  verify)
    dns="$(cf GET "/zones/$ZONE_ID/dns_records?type=CNAME&name=$HOST" | python3 -c "import sys,json;r=(json.load(sys.stdin).get('result') or []);print('present' if any(x.get('name')=='$HOST' for x in r) else 'MISSING')")"
    ing="$(cf GET "/accounts/$ACCOUNT_ID/cfd_tunnel/$TUNNEL_ID/configurations" | HOST="$HOST" python3 -c "import sys,os,json;host=os.environ['HOST'];c=(json.load(sys.stdin).get('result') or {}).get('config') or {};print('present' if any(r.get('hostname')==host for r in (c.get('ingress') or [])) else 'MISSING')")"
    echo "  token: active | DNS CNAME: $dns | tunnel ingress: $ing"
    [ "$dns" = present ] && [ "$ing" = present ] && echo "  => route is fully provisioned" || echo "  => run 'ensure' to create the missing piece(s)" ;;
  remove)
    remove_all "$ZONE_ID" "$ACCOUNT_ID"
    echo "[cf-provision] removed $HOST route" ;;
  *) echo "usage: cf-provision.sh <ensure|verify|remove>" >&2; exit 2 ;;
esac
