FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

# tzdata so TZ= works; curl for healthcheck. nvidia-smi is injected by the
# NVIDIA container runtime when the service is started with GPU reservations.
RUN apt-get update \
 && apt-get install -y --no-install-recommends tzdata curl ca-certificates \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install -r requirements.txt

COPY app/ ./app/
COPY config/ ./config/

EXPOSE 8791
HEALTHCHECK --interval=60s --timeout=5s --start-period=15s --retries=3 \
  CMD curl -fsS http://127.0.0.1:8791/health || exit 1

CMD ["uvicorn", "app.main:api", "--host", "0.0.0.0", "--port", "8791", "--log-level", "info"]
