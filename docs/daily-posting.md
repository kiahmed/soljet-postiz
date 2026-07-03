# Daily posting — operator's manual

How the daily social posts happen, how to check them, and what to do when a
post needs posting by hand. Written so someone new can run this cold.

---

## What runs

One script, on a timer: **`bin/daily.py --push`** (via `make post`). No Claude,
no always-on agent — just a cron job firing once a day. Recommended host is a
small Docker container in this same stack (see [Scheduling it](#scheduling-it));
Windows Task Scheduler / WSL cron also work.

Each run, for every **enabled tier** (a product or an enabled branch):

1. Picks the **newest unposted** item from that tier's primary feed
   (arboryx → Firestore findings; arboryx.robotics → robotics cards).
2. Composes the post text + image (same pipeline as `bin/post.py`).
3. Publishes to **each channel separately** (X and LinkedIn).
4. **Waits and confirms** each post reached `PUBLISHED` — never fire-and-forget.

A tier with nothing new is skipped. An item already posted is never reposted
(tracked in `data/posted_log.sqlite`).

> "Enabled" = the tier's channels resolve to at least one integration id.
> LinkedIn is gated by `LINKEDIN_ENABLED` in `.env`; branches are gated by
> `BRANCH_<NAME>_ENABLED` in the parent's `tier.config`.

---

## What's automatic vs. what needs you

- **LinkedIn** publishes automatically. Nothing to do.
- **X** currently fails (dev-app API credits are depleted). When that happens
  the job **catches it, keeps going, and never crashes** — the post is queued
  for you to post by hand. **When you top up X credits, it just works again —
  no code change, no toggle.**

So day to day you do nothing. You only step in for the X (or any failed) posts.

---

## Where to find posts you must post by hand

Two places, both passive — check whenever you drop in, not on a daily leash:

1. **Postiz UI** — a failed post shows on its day, flagged as errored.
   `https://dev.arboryx.ai` (or `http://localhost:4007`).
2. **`data/manual-post-queue.md`** — the job appends a block per failed post:

   ```
   ## 2026-07-03 15:52 UTC — arboryx — X
   Reason: Publish failed: ... — for X this is usually depleted API credits.
   Image: `assets/banner-x.jpg`      ← the picture to attach (or "(none)")
   Text:  ```<the exact post text>```
   ```

   Copy the **Text**, attach the **Image** if one is listed, post it on the
   platform, then delete that block from the file.

---

## The daily routine (for a human)

Normally you do **nothing** — it runs itself. When you feel like checking in:

1. `python3 bin/daily.py --check` — confirms workers are polling and shows each
   tier's channels. If it says workers are NOT polling, see Troubleshooting.
2. Open `data/manual-post-queue.md`. For each block: paste the text + attach the
   image on the platform (X, etc.), then delete the block.
3. Glance at the Postiz UI calendar for any red/errored posts (same items).

That's it.

### Posting one by hand, the easy way

For any manual post, this renders it and helps you paste:

```bash
python3 bin/post.py --recipe single --tier arboryx --source-id <ID> --copy --show
# --copy → text on your clipboard   --show → image opens in the viewer
```

Then paste into the platform and attach.

---

## Troubleshooting

**Posts stuck / nothing publishing.** Almost always the Temporal workers have
silently stopped polling (the app looks "up" but publishes nothing — this is
what blocked LinkedIn historically). One command diagnoses and fixes it:

```bash
make heal          # checks Temporal + workers; restarts only what's broken
make heal-check    # report only, no restart (exit 1 if unhealthy)
```

`make heal` restarts temporal and/or postiz to re-register the workers and waits
until they poll again. The docker scheduler runs it automatically before every
daily run, so this is mostly a manual escape hatch.

`daily.py` also detects this itself: a post stuck in `QUEUE` past the timeout is left
**unposted** (so the next run retries it) and written to the manual queue with a
"restart postiz" note. Definitive failures (like X out of credits) are *not*
retried — retrying wouldn't help — they just go to the manual queue.

**X posts always fail.** Expected until the dev app has credits. Top up at
`developer.x.com`; no config change needed afterward.

**Where the real ids/keys live.** `.env` (gitignored). Never commit it.

---

## Scheduling it

Preview first (never publishes): `make post-preview`. Go live: `make post`.

### Option 1 — Docker scheduler (recommended)

A small container (`postiz-scheduler`) runs the daily job on a cron **inside the
same Docker stack** you already keep up for Postiz. It heals connectivity, then
publishes — no host cron, no open terminal needed.

```bash
make scheduler-up        # build + start it (opt-in `scheduler` compose profile)
make scheduler-logs      # watch it
make scheduler-run       # fire one run right now (test)
make scheduler-down      # stop it
```

- **Schedule:** edit `ops/scheduler/crontab` (default `0 6 * * *` = 06:00), then
  `make scheduler-restart`. Times use `SCHEDULER_TZ` (default `America/New_York`,
  i.e. 6am Eastern year-round); override in `.env` for another zone.
- **What each run does:** `ops/scheduler/run-daily.sh` → `make heal` then
  `make post`, logged to `data/daily.log`.
- **How it reaches the stack:** the container mounts the repo (`.:/app`), the
  docker socket (to exec the other containers), your gcloud ADC read-only, and
  the sibling `catalyst-knowledge-graph` repo (robotics branch's `cards.json`).
  It needs no secrets of its own — it reads the same `.env`.
- **Survives reboots** *if Docker itself auto-starts* — set Docker Desktop to
  "Start on login." Then after a reboot the whole stack + scheduler come back
  (`restart: unless-stopped`). If Docker isn't running, nothing runs.

**Firestore credentials — required for the parent (arboryx) tier.** The parent
tier reads findings from Firestore, which needs a **service-account** key for
headless use. (A user ADC from `gcloud auth application-default login` does NOT
work unattended — Google forces periodic reauth: `503 Reauthentication is
needed`. Fine when you run `make post` yourself on the host, not in the container.)

**If `GOOGLE_APPLICATION_CREDENTIALS` is already set in your shell** to an SA key
with Firestore read (`roles/datastore.viewer`) — it is on this box — **nothing
to configure.** The scheduler reads that variable and identity-mounts the key at
the same path inside the container automatically. Verified: the parent tier
composes from Firestore headless.

If you ever need to set one up fresh: create an SA with `roles/datastore.viewer`
in the `GCP_PROJECT` project, download its JSON key anywhere on the host, and
`export GOOGLE_APPLICATION_CREDENTIALS=/path/to/key.json` before
`make scheduler-up`.

### Option 2 — Windows Task Scheduler (host-side, no extra container)

Survives reboots, runs headless. Basic Task, daily, action:

```
wsl.exe -e bash -lc "cd /mnt/c/soljet_dev/ai_stack_development/soljet-postiz && make heal && make post >> data/daily.log 2>&1"
```

### Option 3 — WSL cron (only fires while WSL is running)

```cron
30 14 * * *  cd /mnt/c/soljet_dev/ai_stack_development/soljet-postiz && make heal && make post >> data/daily.log 2>&1
```

All three append to `data/daily.log`. The job exits non-zero only when something
is stuck (needs a human), so that's the line to grep in the log.

---

## Make targets

Everything runs through `make` (run `make` alone for the full list):

| Target | What it does |
| --- | --- |
| **Stack** | |
| `make deploy` / `make update` | Start / update the whole Postiz stack |
| `make status` / `make ps` | Health checks / container list |
| `make down` | Stop containers (volumes preserved) |
| `make clean` \| `clean-stopped` \| `clean-deep` | Reclaim docker disk |
| **Connectivity** | |
| `make heal` | Check Temporal + workers, restart only what's broken |
| `make heal-check` | Report health only (no restart) |
| **Posting** | |
| `make check` | Worker pollers + each tier's channels |
| `make post-preview` | Compose today's posts, **no publish** |
| `make post` | Publish today's posts (the daily run) |
| `make manual-queue` | Show posts awaiting a hand-post |
| **Scheduler** | |
| `make scheduler-up` \| `-down` \| `-restart` \| `-logs` | Manage the cron container |
| `make scheduler-run` | Fire one daily run now (test) |

`bin/daily.py` flags (what `make post*` wraps): `--push` publish · `--tier <id>`
one tier · `--check` health only · `--since 7d` widen the lookback (default 3d).
