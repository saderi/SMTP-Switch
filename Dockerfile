FROM python:3.12-slim AS base

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# Dependencies first for layer caching.
COPY pyproject.toml README.md ./
COPY smtp_switch ./smtp_switch
RUN pip install --no-cache-dir .

# Non-root; /data is the writable volume (sqlite db + spool + certs).
RUN useradd --system --uid 10001 --create-home switch \
    && mkdir -p /data/spool /data/certs \
    && chown -R switch:switch /data /app
USER switch

VOLUME ["/data"]
EXPOSE 2525 8080

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8080/healthz', timeout=3).status==200 else 1)"

# Config is provided at runtime (bind-mount or env). Defaults expect /data.
ENV SMTP_SWITCH_CONFIG=/data/config.yaml \
    SMTP_SWITCH_DATABASE__URL=sqlite+aiosqlite:////data/smtp_switch.db \
    SMTP_SWITCH_DISPATCH__SPOOL_DIR=/data/spool \
    SMTP_SWITCH_WEB__HOST=0.0.0.0

ENTRYPOINT ["smtp-switch"]
