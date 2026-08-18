# syntax=docker/dockerfile:1

FROM python:3.12-slim-bookworm

LABEL org.opencontainers.image.title="polymarket-papertrader" \
      org.opencontainers.image.description="Polymarket weather paper trader + dashboard"

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates curl \
    && rm -rf /var/lib/apt/lists/*

COPY pyproject.toml README.md ./
COPY config ./config
COPY src ./src
COPY docker/entrypoint.sh /entrypoint.sh

RUN pip install --no-cache-dir --upgrade pip \
    && pip install --no-cache-dir -e ".[dashboard,live,momentum]" \
    && chmod +x /entrypoint.sh

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PAPERTRADER_DATA_DIR=/data \
    PORT=8787 \
    SERVICE=both \
    STRATEGY=both

VOLUME ["/data"]

EXPOSE 8787

HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 \
  CMD curl -fsS "http://127.0.0.1:${PORT:-8787}/health" || exit 1

ENTRYPOINT ["/entrypoint.sh"]
