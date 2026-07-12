# LinkedIn @mentions — how a real tag gets made

**Problem.** LinkedIn's post API renders a *real* mention (links the account,
notifies it, counts as a tag) only when the post text carries the exact token

```
@[Display Name](urn:li:organization:<numeric-id>)
```

A bare `@spiritai-robotics` is **dead text** — LinkedIn shows it literally and
tags nobody. (Postiz's LinkedIn provider `fixText()` matches exactly that token
and escapes everything else.) X is unaffected: its `@handle` works as-is.

**Division of labour.**

| Step | Who | Why |
|---|---|---|
| entity → verified vanity **slug** (e.g. `@spiritai-robotics`, *not* `@spirit-ai`) | **KG** (catalyst-knowledge-graph) | needs graph/sector context to pick the right company among same-named orgs — impossible at post time |
| slug → **organization URN** (`urn:li:organization:111476012`) + official name | **here** (`src/lib/linkedin_urn.py`) | authenticated LinkedIn API call, deterministic, cacheable — nothing to reason about |

The KG stores only the slug; the LinkedIn URL is derivable
(`linkedin.com/company/<slug>`), so it is not stored separately.

## What our side does

`channel_dispatch.entity_tags`, for the **LinkedIn** channel only, passes the
resolved slug through `linkedin_urn.to_mention()`:

1. `to_mention("@spiritai-robotics")` strips the `@`, calls LinkedIn's
   vanityName finder
   `GET /rest/organizations?q=vanityName&vanityName=<slug>`
   → `{id, localizedName}`.
2. Emits `@[Spirit AI](urn:li:organization:111476012)` (display name = the org's
   official `localizedName`, not our slug).
3. **Fail closed:** any miss (wrong/stale slug, throttle, no token) → returns
   `None` → the tag is dropped. Never a broken or dead mention.

Results cache to `data/linkedin_urn_cache.json` (positive forever — org ids are
stable; negative for 3 days so a corrected slug re-resolves). X keeps the plain
`@handle` — the two channels' text already diverges via `channel_parts`.

## Collision guard (wrong same-named org)

The fail-closed miss above can't catch a slug that resolves to the **wrong**
same-named org — KG once stored `foundation-group-inc` (a 501(c)(3) tax-services
firm) for a humanoid-robotics "Foundation." It resolved fine; it was just the
wrong company. So for **collision-prone** entity names we verify the resolved org
against the card before tagging:

- **Trigger (`_collision_prone`)** — only single bare **common-word** names
  (`Foundation`, `Figure`, `Bolt`, `Tesla`, `Siemens`). Multi-word names
  (`Boston Dynamics`), acronyms/all-caps (`NVIDIA`, `UBTECH`), and names with
  digits are distinctive enough to trust and skip the check entirely.
- **Check (`_context_confidence`)** — deterministic, no LLM, no extra API (reuses
  the `description` the finder already returns). Counts distinctive domain terms
  shared between the org's name+description and the card context (headline +
  entities + relationship verbs), **excluding the entity's own name** (a common
  word matches itself). ≥1 shared term → confident (0.9).
- **Zero overlap is not enough to drop.** A legit brand's About-text often uses
  different words than a specific card (Tesla's EV blurb vs an "Optimus humanoid"
  headline). With zero overlap we drop **only** if the org blurb carries an
  off-domain marker (`_OFF_DOMAIN`: nonprofit / 501(c)(3) / tax / legal / realty /
  staffing / restaurant / …) → 0.30. Otherwise keep at 0.75.
- **Threshold 0.7.** Below → drop the tag (logged to stderr with the reason);
  the rest of the post is unaffected. Better a missing tag than a wrong one.

Net effect: `Foundation → foundation-group-inc` (nonprofit) is dropped; `Tesla`,
`Siemens`, `Boston Dynamics`, `D-Robotics` are all kept. The guard only fires on
the clear-cut off-domain mismatch — the class KG's context re-audit can't always
catch, since a wrong slug still "resolves."

## The token

The finder needs the LinkedIn app token. We read the **live** token from Postiz's
own Postgres each run (Postiz owns the OAuth refresh, so it's always current):

```
select token from "Integration" where "providerIdentifier"='linkedin-page'
```

Override for headless/other setups via env (checked first):
`LINKEDIN_ACCESS_TOKEN`, or point the DB read elsewhere with
`POSTIZ_PG_CONTAINER` / `POSTIZ_PG_USER` / `POSTIZ_PG_DB` / `LINKEDIN_PROVIDER_ID`.

**Scope check (done):** the app token carries `rw_organization_admin`, and that
scope's vanityName finder resolves *arbitrary* third-party orgs (not just ones we
administer) — verified live against `spiritai-robotics` / `yfcapital`. No
`r_organization_lookup` / Community-Management product access is required.

## Config

Same gates as before, per tier.config: `HANDLE_INJECTION="true"` turns the whole
thing on; `MAX_ENTITY_TAGS`, `ENTITY_TAG_MODE` unchanged. The URN step is
automatic for the LinkedIn channel whenever handle injection is on — no new flag.
