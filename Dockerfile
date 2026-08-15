# ─── ClickHouse-Console — multi-user admin console ──────────────────────────
FROM python:3.11-slim AS base
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
RUN apt-get update && apt-get install -y --no-install-recommends \
    coreutils ca-certificates curl && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY . .
RUN mkdir -p /app/data && chmod 750 /app/data

EXPOSE 5000
ENV APP_DB=/app/data/app.db \
    LICENSE_FILE=/app/data/license.lic

# Default: gunicorn, 4 workers × 8 threads. Override CMD if you need different.
CMD ["gunicorn", "-w", "4", "-k", "gthread", "--threads", "8", \
     "--access-logfile", "-", "--capture-output", \
     "-b", "0.0.0.0:5000", "--timeout", "120", "app:app"]
