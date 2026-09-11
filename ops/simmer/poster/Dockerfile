# simmer-poster — Pub/Sub PUSH subscriber, publishes to Postiz. Cloud Run.
# Build context = repo root:  docker build -f ops/simmer/poster/Dockerfile .
FROM python:3.12-slim

WORKDIR /app
RUN pip install --no-cache-dir \
      requests flask gunicorn \
      google-cloud-pubsub google-cloud-firestore google-cloud-storage Pillow

# The poster imports src/lib/* and bin/_common — copy the tree it needs.
COPY bin/ bin/
COPY src/ src/
COPY products/ products/

ENV PYTHONPATH=/app PORT=8080 SIMMER_POSTER_MODE=now
# gunicorn (not Flask's dev server) so app logs reach Cloud Logging on stdout.
# 1 worker (Firestore/dedupe state is per-process), threads for concurrent
# pushes, long timeout because a cold simmer-snap call can take ~30s.
# SIMMER_POSTER_MODE=now: a live event => a live post; the Firestore dedupe guard
# makes Pub/Sub retries idempotent.
CMD ["gunicorn", "-b", "0.0.0.0:8080", "-w", "1", "--threads", "8", "-t", "300", \
     "--access-logfile", "-", "--error-logfile", "-", \
     "bin.simmer_poster:create_app()"]
