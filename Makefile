# Postiz operator targets. Thin wrappers over the existing *.sh scripts,
# docker-compose, and the daily poster (bin/daily.py). Run `make` for the list.
.DEFAULT_GOAL := help
.PHONY: help deploy status update down clean clean-stopped clean-deep logs \
        ps restart heal heal-check check post post-preview regenerate manual-queue \
        scheduler-up scheduler-down scheduler-restart scheduler-logs scheduler-run

# ---- stack lifecycle (reuse existing scripts) ----------------------------
deploy:         ## Pull images + start the whole stack, wait for health
	./deploy.sh

update:         ## Pull latest images + recreate containers (self-heals backend)
	./update.sh

status:         ## docker compose ps + app/DB/Temporal health checks
	./status.sh

down:           ## Stop & remove containers (volumes preserved)
	./teardown.sh

clean:          ## Reclaim docker disk: dangling images + build cache (safe)
	./cleanup.sh

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

post-preview:   ## Compose today's posts for all enabled tiers, DO NOT publish
	python3 bin/daily.py

regenerate:     ## Re-compose + re-stage today's posts (discard staged content), no publish
	python3 bin/daily.py --regenerate

post:           ## Publish today's posts for all enabled tiers (the daily run)
	python3 bin/daily.py --push

manual-queue:   ## Show posts awaiting a hand-post (failed/stuck channels)
	@cat data/manual-post-queue.md 2>/dev/null || echo "(manual queue is empty)"

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

# ---- help ----------------------------------------------------------------
help:           ## Show this list
	@echo "Postiz operator targets:"
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) \
	  | sort | awk 'BEGIN{FS=":.*?## "}{printf "  \033[36m%-16s\033[0m %s\n", $$1, $$2}'
