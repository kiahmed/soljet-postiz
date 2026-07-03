# Daily posting — operator's manual

How the daily social posts happen, how to check them, and what to do when a
post needs posting by hand. Written so someone new can run this cold.

---

## What runs

One script, on a timer: **`bin/daily.py --push`**. No Claude, no always-on
process — just a cron / Windows Task Scheduler job firing once a day.

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
what blocked LinkedIn historically). Fix:

```bash
docker compose restart postiz      # workers re-register in ~1–3 min
python3 bin/daily.py --check        # should now say: workers polling YES
```

`daily.py` detects this itself: a post stuck in `QUEUE` past the timeout is left
**unposted** (so the next run retries it) and written to the manual queue with a
"restart postiz" note. Definitive failures (like X out of credits) are *not*
retried — retrying wouldn't help — they just go to the manual queue.

**X posts always fail.** Expected until the dev app has credits. Top up at
`developer.x.com`; no config change needed afterward.

**Where the real ids/keys live.** `.env` (gitignored). Never commit it.

---

## Scheduling it

Preview first (never publishes): `python3 bin/daily.py --since 3d`
Go live: add `--push`.

**Windows Task Scheduler (recommended — survives reboots, runs headless):**
Create a Basic Task, daily at your chosen time, action:

```
wsl.exe -e bash -lc "cd /mnt/c/soljet_dev/ai_stack_development/soljet-postiz && python3 bin/daily.py --push >> data/daily.log 2>&1"
```

**WSL cron (only fires while WSL is running):**

```cron
30 14 * * *  cd /mnt/c/soljet_dev/ai_stack_development/soljet-postiz && python3 bin/daily.py --push >> data/daily.log 2>&1
```

Both append to `data/daily.log`. The job exits non-zero only when something is
stuck (needs a human), so that's the line to grep in the log.

---

## Flags reference

| Command | What it does |
| --- | --- |
| `bin/daily.py` | Preview all enabled tiers. **No publishing.** |
| `bin/daily.py --push` | Publish. This is the scheduled command. |
| `bin/daily.py --tier arboryx --push` | Just one tier. |
| `bin/daily.py --check` | Health only: worker pollers + each tier's channels. |
| `bin/daily.py --since 7d` | Widen the "new items" lookback window (default 3d). |
