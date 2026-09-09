# simmer-snap — headless-Chromium screenshot service for Cloud Run.
FROM mcr.microsoft.com/playwright/python:v1.47.0-jammy

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY main.py .

ENV PORT=8080
# gunicorn: 1 worker (Chromium is heavy), a few threads, generous timeout.
CMD ["gunicorn", "-b", "0.0.0.0:8080", "-w", "1", "--threads", "4", "-t", "90", "main:app"]
