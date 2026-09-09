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

ENV PYTHONPATH=/app PORT=8080
# --mode now: a live event => a live post. The engine only publishes real state
# changes, and the Firestore dedupe guard makes retries idempotent.
CMD ["python", "bin/simmer_poster.py", "--serve", "--mode", "now"]
