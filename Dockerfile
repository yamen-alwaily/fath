# Multi-stage Dockerfile for Fath (فتح) — Saudi Open Banking Sandbox
# Stage 1: Build Dependencies
FROM python:3.12-slim AS builder

WORKDIR /install

RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir --prefix=/install -r requirements.txt

# Stage 2: Final Minimal Runtime
FROM python:3.12-slim AS runner

# Create non-root system user
RUN groupadd -r fath && useradd -r -g fath -d /app -s /sbin/nologin fath

WORKDIR /app

# Copy installed Python packages from builder
COPY --from=builder /install /usr/local

# Copy application codebase
COPY . .

# Ensure app directory and sqlite directory are writable by non-root user
RUN chown -R fath:fath /app

USER fath

ENV PORT=5000 \
    HOST=0.0.0.0 \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    FATH_DB_PATH=/app/data_volume/fath.db

EXPOSE 5000

HEALTHCHECK --interval=30s --timeout=5s --start-period=5s --retries=3 \
  CMD python3 -c "import urllib.request; sys_exit = 0 if urllib.request.urlopen('http://localhost:5000/health').getcode() == 200 else 1; exit(sys_exit)"

# Launch Gunicorn with gthread worker supporting WebSockets and Flask-SocketIO
CMD ["gunicorn", "--worker-class", "gthread", "--workers", "2", "--threads", "4", "--bind", "0.0.0.0:5000", "app:app"]
