#!/usr/bin/env bash
# Mode-aware front-end for the scheduler make targets. Reads GCP_PROD_SCHEDULER
# from .env and dispatches:
#   disabled (default) -> local supercronic container (compose profile `scheduler`)
#   enabled            -> GCP Cloud Scheduler jobs + the trigger sidecar
#                         (compose profile `scheduler-gcp` + gcp-scheduler.sh)
#
# Usage: scheduler-ctl.sh <up|down|restart|logs|run|show>
# Set DRY=1 to print the commands instead of running them.
set -uo pipefail
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=ops/scheduler/lib.sh
source "$DIR/lib.sh"
cd "$REPO_ROOT"

CMD="${1:-}"
[ -n "$CMD" ] || { echo "usage: scheduler-ctl.sh <up|down|restart|logs|run|show>"; exit 2; }

MODE="$(sched_envget GCP_PROD_SCHEDULER)"; MODE="${MODE:-disabled}"
case "$MODE" in enabled|true|1) MODE=gcp;; *) MODE=local;; esac

run() {  # echo-or-exec
  if [ "${DRY:-}" = "1" ]; then printf '  + %s\n' "$*"; else "$@"; fi
}

local_compose() { run docker compose --profile scheduler "$@"; }

do_local() {
  case "$CMD" in
    up)      run "$DIR/gen-crontab.sh"; local_compose up -d --build scheduler ;;
    restart) run "$DIR/gen-crontab.sh"; local_compose restart scheduler ;;
    down)    local_compose rm -sf scheduler ;;
    logs)    local_compose logs -f scheduler ;;
    run)     local_compose exec scheduler /app/ops/scheduler/run-daily.sh ;;
    show)    echo "[scheduler] local crontab (ops/scheduler/crontab):"; grep -vE '^\s*#|^\s*$' "$REPO_ROOT/ops/scheduler/crontab" 2>/dev/null || echo "  (no crontab yet — run make scheduler-up)" ;;
    *) echo "unknown command: $CMD"; exit 2 ;;
  esac
}

gcp_compose() { run docker compose --profile scheduler-gcp "$@"; }

# Ensure the Cloudflare tunnel route (DNS + ingress) exists before the jobs can
# reach the sidecar. Idempotent. Skip with SKIP_CF_PROVISION=1 if you manage the
# hostname by hand.
cf_provision() { [ "${SKIP_CF_PROVISION:-}" = 1 ] && { echo "[scheduler] SKIP_CF_PROVISION=1 — not touching Cloudflare"; return 0; }; run "$DIR/cf-provision.sh" "$1"; }

do_gcp() {
  case "$CMD" in
    up)      gcp_compose up -d --build scheduler-trigger; cf_provision ensure; run "$REPO_ROOT/gcp-scheduler.sh" create ;;
    restart) gcp_compose up -d --build scheduler-trigger; cf_provision ensure; run "$REPO_ROOT/gcp-scheduler.sh" create ;;
    down)    run "$REPO_ROOT/gcp-scheduler.sh" delete; cf_provision remove; gcp_compose rm -sf scheduler-trigger ;;
    logs)    run "$REPO_ROOT/gcp-scheduler.sh" logs ;;
    run)     run "$REPO_ROOT/gcp-scheduler.sh" run ;;
    show)    "$REPO_ROOT/gcp-scheduler.sh" show ;;
    *) echo "unknown command: $CMD"; exit 2 ;;
  esac
}

echo "[scheduler] mode=$MODE cmd=$CMD"
[ "$MODE" = gcp ] && do_gcp || do_local
