# Postiz operator targets. Thin wrappers over the existing *.sh scripts,
# docker-compose, and the daily poster (bin/daily.py). Run `make` for the list.
.DEFAULT_GOAL := help
.PHONY: help deploy status update down clean clean-stopped clean-deep logs \
        ps restart heal heal-check check post post-preview regenerate manual-queue \
        social-cache social-cache-list social-cache-clean social-cache-update \
        scheduler-up scheduler-down scheduler-restart scheduler-logs scheduler-run \
        worktree-clean _notmain commit push pr ship

# ---- typo guard: reject unknown KEY=val on the command line ---------------
# `make post-preview OLDERST=1` silently ignored the typo and posted the NEWEST
# card. Catch it: any command-line variable not in this allowlist aborts.
KNOWN_VARS := OLDEST CHANNEL TIER FORCE m
_cmdline_vars := $(foreach kv,$(MAKEOVERRIDES),$(firstword $(subst =, ,$(kv))))
_unknown_vars := $(filter-out $(KNOWN_VARS),$(_cmdline_vars))
ifneq ($(_unknown_vars),)
$(error unknown option(s): $(_unknown_vars) — valid knobs are: $(KNOWN_VARS). Check spelling (e.g. OLDEST, not OLDERST))
endif

# ---- stack lifecycle (reuse existing scripts) ----------------------------
deploy:         ## Pull images + start the whole stack, wait for health
	./deploy.sh

update:         ## Pull latest images + recreate containers (self-heals backend)
	./update.sh

status:         ## docker compose ps + app/DB/Temporal health checks
	./status.sh

down:           ## Stop & remove containers (volumes preserved)
	./teardown.sh

clean:          ## Reclaim docker disk + drop staged content_cache card JSON
	./cleanup.sh
	@find data/content_cache -mindepth 2 -name '*.json' -type f -print -delete 2>/dev/null || true

clean-stopped:  ## clean + also remove stopped containers
	./cleanup.sh --stopped

clean-deep:     ## clean + prompt to remove unused tagged images
	./cleanup.sh --deep

ps:             ## Raw container list
	docker compose ps

logs:           ## Follow the postiz container log
	docker compose logs -f postiz

restart:        ## Restart just the postiz container
	docker compose restart postiz

# ---- connectivity healing (Temporal <-> Postiz workers) ------------------
heal:           ## Check Temporal+worker health, re-register if broken
	./bin/heal.sh

heal-check:     ## Report Temporal+worker health only (no restart); exit 1 if unhealthy
	./bin/heal.sh --check

# ---- daily posting -------------------------------------------------------
check:          ## Daily poster's view: worker pollers + each tier's channels
	python3 bin/daily.py --check

# Optional knobs for ALL post targets below:
#   OLDEST=1               oldest unposted entry instead of newest
#   CHANNEL=linkedin|x     one channel only (default: all the tier's channels)
#   TIER=arboryx.robotics  one tier only (default: all enabled tiers)
_POSTOPTS = $(if $(OLDEST),--oldest) $(if $(CHANNEL),--channel $(CHANNEL)) $(if $(TIER),--tier $(TIER))

post-preview:   ## Compose posts, DO NOT publish [OLDEST=1] [CHANNEL=] [TIER=]
	python3 bin/daily.py $(_POSTOPTS)

regenerate:     ## Re-compose + re-stage (discard staged), no publish [OLDEST=1] [CHANNEL=] [TIER=]
	python3 bin/daily.py --regenerate $(_POSTOPTS)

post:           ## Publish posts [OLDEST=1] [CHANNEL=linkedin] [TIER=arboryx.robotics]
	python3 bin/daily.py --push $(_POSTOPTS)

manual-queue:   ## Show posts awaiting a hand-post (failed/stuck channels)
	@cat data/manual-post-queue.md 2>/dev/null || echo "(manual queue is empty)"

# Handle→URN cache tools. The operation is in the target NAME (not a positional
# word) on purpose: a bare `update` goal would collide with the `make update`
# stack target. Args are <channel> then a free-form <entity...> (case-insensitive).
social-cache:        ## Handle→URN cache tools — see social-cache-{list,clean,update}
	@python3 bin/social-cache.py

social-cache-list:   ## List cached handle→URN entries (usage: make social-cache-list <channel> [entity...])
	@python3 bin/social-cache.py list $(filter-out $@,$(MAKECMDGOALS))

social-cache-clean:  ## Drop matching entries so they re-resolve (usage: make social-cache-clean <channel> <entity...>)
	@python3 bin/social-cache.py delete $(filter-out $@,$(MAKECMDGOALS))

social-cache-update: ## Re-resolve matching entries live (usage: make social-cache-update <channel> <entity...>)
	@python3 bin/social-cache.py update $(filter-out $@,$(MAKECMDGOALS))

# ---- docker scheduler (opt-in `scheduler` compose profile) ---------------
scheduler-up:       ## Build & start the daily scheduler container
	docker compose --profile scheduler up -d --build scheduler

scheduler-down:     ## Stop & remove the scheduler container
	docker compose --profile scheduler rm -sf scheduler

scheduler-restart:  ## Restart the scheduler (after editing its crontab)
	docker compose --profile scheduler restart scheduler

scheduler-logs:     ## Follow the scheduler container log
	docker compose --profile scheduler logs -f scheduler

scheduler-run:      ## Fire one daily run NOW inside the scheduler (test/manual)
	docker compose --profile scheduler exec scheduler /app/ops/scheduler/run-daily.sh

# ---- worktree cleanup ----------------------------------------------------
worktree-clean:     ## Remove a merged worktree, or (no name) return to main + delete branch (usage: make worktree-clean [<name>] [FORCE=1])
	@./bin/worktree-clean.sh "$(filter-out $@,$(MAKECMDGOALS))" "FORCE=$(FORCE)"

# Let args be positional (`make worktree-clean <name>`, `make ship <name>`,
# `make social-cache <op> <channel> <entity...>`): turn the trailing words into
# no-op goals so make doesn't error on them. Scoped to these targets only, so it
# never masks typos in other targets.
_POSGOAL_TARGETS := worktree-clean ship social-cache-list social-cache-clean social-cache-update
ifneq (,$(filter $(_POSGOAL_TARGETS),$(firstword $(MAKECMDGOALS))))
$(if $(filter-out $(_POSGOAL_TARGETS),$(MAKECMDGOALS)),\
     $(eval $(filter-out $(_POSGOAL_TARGETS),$(MAKECMDGOALS)):;@:))
endif

# ---- git workflow (run inside a worktree branch) -------------------------
# Guard: never commit/push/PR straight onto main.
_notmain:
	@test "$$(git rev-parse --abbrev-ref HEAD)" != main \
	  || { echo "refusing: you're on main — switch to a worktree branch"; exit 1; }

# commit/push/pr act on the current branch and refuse on main. `ship` is exempt:
# with no name it cuts its OWN ship/<stamp> branch from main first.
commit push pr: _notmain

commit:     ## Stage all + commit (usage: make commit m="message")
	@test -n "$(m)" || { echo 'usage: make commit m="your message"'; exit 2; }
	git add -A && git commit -m "$(m)"

push:       ## Push the current branch to origin (sets upstream)
	git push -u origin $$(git rev-parse --abbrev-ref HEAD)

pr:         ## Open a PR from the current branch (title/body auto-filled from commits)
	gh pr create --fill --base main --head $$(git rev-parse --abbrev-ref HEAD)

ship:       ## Push + open PR; from main cuts ship/<stamp> for you (usage: make ship m="message" | make ship <name>)
	@./bin/ship.sh "$(filter-out $@,$(MAKECMDGOALS))" "$(m)"

# ---- help ----------------------------------------------------------------
help:           ## Show this list
	@echo "Postiz operator targets:"
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) \
	  | sort | awk 'BEGIN{FS=":.*?## "}{ \
	      d=$$2; \
	      gsub(/\[[^]]*\]|<[^>]*>|[A-Za-z_]+="[^"]*"/, "\033[38;5;208m&\033[0m", d); \
	      printf "  \033[36m%-16s\033[0m %s\n", $$1, d }'
