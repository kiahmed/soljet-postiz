# Daily posting scheduler

The daily poster runs on a schedule via one of two interchangeable backends,
chosen by `GCP_PROD_SCHEDULER` in `.env`:

| flag | backend | what runs the timer | what runs the posting |
|------|---------|---------------------|-----------------------|
| `disabled` (default) | **local** | supercronic in the `postiz-scheduler` container | same container |
| `enabled` | **GCP** | GCP Cloud Scheduler (one job per channel) | the `postiz-scheduler-trigger` sidecar, on the host where the stack lives |

Both backends read the **same** schedule: `ops/scheduler/channels.conf`. Both call
the **same** `ops/scheduler/run-daily.sh <channel> <count> <delay> <tier>`, which
runs `make heal` then `make post READY=1 OLDEST=1 …`. Nothing about the posting
logic differs between local and prod.

The same make targets drive either backend:

```
make scheduler-up       # start (local container, or GCP jobs + trigger sidecar)
make scheduler-down      # stop / remove
make scheduler-restart   # reload schedule from channels.conf
make scheduler-logs      # follow logs
make scheduler-run       # fire one run now (test)
```

## The schedule — `ops/scheduler/channels.conf`

One row per channel = one scheduler entry (`channel | count | delay | tier | cron`).
This is the cron translation of the two manual commands, and the only file to
edit to change cadence:

```
linkedin | 5 | 60m | arboryx.robotics | 0 6 * * *
x        | 5 | 70m | arboryx.robotics | 30 8 * * *
```

`delay` accepts `90` (seconds) or `60m` / `2h`. A row for a *disabled* channel is
harmless — `daily.py --channel <c>` finds no integration and no-ops — so you can
pre-list a channel and just flip its `*_ENABLED` flag later. Edit the file, then
`make scheduler-restart` (local) regenerates the crontab; `make scheduler-up`
(GCP) re-upserts the Cloud Scheduler jobs.

## Why GCP mode needs a trigger sidecar (not "inline")

GCP Cloud Scheduler can only make an HTTP call — it has no runtime for what
`daily.py` actually needs: `docker exec` into `postiz-postgres`/`temporal-admin-tools`
to confirm each post, the local `posted_log.sqlite` dedup DB, the KG card PNGs,
and Firestore ADC. So Cloud Scheduler is **only the timer**. Each job POSTs the
`postiz-scheduler-trigger` sidecar (through the Cloudflare tunnel); the sidecar
runs `run-daily.sh` on the host where the stack + data live. A real run can take
hours (60m between cards), so the trigger returns `202` immediately and runs the
job detached — never blocking the HTTP call past Cloud Scheduler's deadline.

```
GCP Cloud Scheduler  ──HTTPS POST /run──▶  CF tunnel  ──▶  scheduler-trigger  ──▶  run-daily.sh
  (one job per channel,                    (dashboard        (validates token +      (make heal;
   fires on cron)                           ingress rule)     channel whitelist)       make post …)
```

## Enabling GCP mode (manual prod steps)

These require live credentials and are not run by CI. Add to `.env`:

```
GCP_PROD_SCHEDULER=enabled
GCP_PROJECT=marketresearch-agents      # already set (Firestore)
GCP_SCHEDULER_REGION=us-central1
GCP_SCHEDULER_TZ=Etc/UTC
SCHEDULER_TRIGGER_URL=https://trigger.arboryx.ai/run
SCHEDULER_TRIGGER_TOKEN=<a long random string>
SCHEDULER_TRIGGER_PORT=8090
```

Then, once:
1. `gcloud auth login` (and set the project) so `gcp-scheduler.sh` can create jobs.
2. In the **Cloudflare dashboard**, add a tunnel ingress rule routing
   `trigger.arboryx.ai` → `http://postiz-scheduler-trigger:8090` (the tunnel is
   token-managed, so ingress lives in the dashboard, not a repo file). The
   sidecar is on `postiz-network`, reachable by `cloudflared`.
3. `make scheduler-up` — builds/starts the trigger sidecar and upserts one Cloud
   Scheduler job per channel.

Verify without spending: `DRY=1 ./gcp-scheduler.sh create` prints the exact
`gcloud` calls; `SCHEDULER_DRY_RUN=1` makes the trigger echo the resolved
`make post` line instead of posting.

## Security

- The trigger rejects any `/run` without the exact bearer `SCHEDULER_TRIGGER_TOKEN`
  (constant-time compare) and only executes a channel present in `channels.conf`
  (no arbitrary channel string reaches the shell). `count`/`delay`/`tier` are
  format-validated.
- The trigger port is `expose`d to the compose network only — not published to
  the host. Public reach is solely via the authenticated CF tunnel ingress rule.
