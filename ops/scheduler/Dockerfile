# Daily-poster scheduler. A tiny cron container that fires bin/daily.py once a
# day. It talks to the other stack containers through the mounted docker socket
# (so daily.py / heal.sh run unchanged) and reads the repo + .env via a bind
# mount at /app. Build context is the repo root (see docker-compose.yaml).
FROM python:3.12-slim

ARG DOCKER_CLI_VERSION=27.5.1
ARG SUPERCRONIC_VERSION=v0.2.33

# make (Makefile targets) + the docker CLI (host daemon via socket) +
# supercronic (a container-friendly cron) + tzdata for local schedule times.
RUN apt-get update \
 && apt-get install -y --no-install-recommends make curl ca-certificates tzdata \
 && curl -fsSL "https://download.docker.com/linux/static/stable/x86_64/docker-${DOCKER_CLI_VERSION}.tgz" \
      | tar -xz -C /usr/local/bin --strip-components=1 docker/docker \
 && curl -fsSL -o /usr/local/bin/supercronic \
      "https://github.com/aptible/supercronic/releases/download/${SUPERCRONIC_VERSION}/supercronic-linux-amd64" \
 && chmod +x /usr/local/bin/docker /usr/local/bin/supercronic \
 && rm -rf /var/lib/apt/lists/*

# Python deps baked in; the code itself is bind-mounted at /app at runtime.
COPY requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir -r /tmp/requirements.txt

WORKDIR /app
# Crontab lives in the bind-mounted repo so you can edit the schedule without
# rebuilding — just `make scheduler-restart`.
CMD ["supercronic", "/app/ops/scheduler/crontab"]
