# reflex-poster — Pub/Sub PUSH subscriber, publishes Reflex's finished posts to
# Postiz. Cloud Run. No snap/render step: the image comes from news-reactor's DB.
# Build context = repo root:  docker build -f ops/reflex/poster/Dockerfile .
FROM python:3.12-slim

WORKDIR /app
RUN pip install --no-cache-dir \
      requests flask gunicorn psycopg2-binary \
      google-cloud-pubsub google-cloud-firestore google-cloud-storage Pillow

# The poster imports src/lib/* and bin/_common — copy the tree it needs.
COPY bin/ bin/
COPY src/ src/
COPY products/ products/

ENV PYTHONPATH=/app PORT=8080 REFLEX_POSTER_MODE=now PYTHONUNBUFFERED=1
# gunicorn (not Flask's dev server) so app logs reach Cloud Logging on stdout.
# 1 worker (dedupe state is per-process), threads for concurrent pushes; a
# concurrent redelivery of the same post is stopped by the row lock
# (select ... for update skip locked) as well as the Firestore dedupe guard.
# REFLEX_POSTER_MODE=now: a live event => a live post + posted=true.
CMD ["gunicorn", "-b", "0.0.0.0:8080", "-w", "1", "--threads", "8", "-t", "300", \
     "--access-logfile", "-", "--error-logfile", "-", "--capture-output", \
     "bin.reflex_poster:create_app()"]
