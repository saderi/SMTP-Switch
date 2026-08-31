# syntax=docker/dockerfile:1

# Bump the base here; `docker buildx imagetools inspect python:3.12-slim`
# prints a fresh digest when you change the tag. Let Renovate/Dependabot
# keep the digest current instead of hand-editing it.
ARG PYTHON_VERSION=3.12-slim
ARG PYTHON_DIGEST=sha256:09f7da3bc104798d0afb40bc08d23ab2da20a76130cec1f2ef170848f5d85217

# ---------------------------------------------------------------------------
# build: install the app + its dependencies into a self-contained virtualenv
# ---------------------------------------------------------------------------
FROM python:${PYTHON_VERSION}@${PYTHON_DIGEST} AS build

ENV PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_ROOT_USER_ACTION=ignore

RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

WORKDIR /app
# setuptools builds the wheel from source, so the source has to be present at
# install time; the pip cache mount (not COPY ordering) is what keeps repeat
# builds fast.
COPY pyproject.toml README.md ./
COPY smtp_switch ./smtp_switch
RUN --mount=type=cache,target=/root/.cache/pip \
    pip install .

# ---------------------------------------------------------------------------
# runtime: only the venv + the files alembic needs, running as a non-root user
# ---------------------------------------------------------------------------
FROM python:${PYTHON_VERSION}@${PYTHON_DIGEST} AS runtime

LABEL org.opencontainers.image.title="smtp-switch" \
      org.opencontainers.image.description="SMTP switch fronting multiple email providers with health- and rate-aware routing" \
      org.opencontainers.image.source="https://github.com/saderi/SMTP-Switch" \
      org.opencontainers.image.licenses="MIT"

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PATH="/opt/venv/bin:$PATH"

COPY --from=build /opt/venv /opt/venv

WORKDIR /app
# alembic.ini resolves script_location relative to its own path, so the
# migrations tree has to sit next to it for `alembic upgrade head` to work
# in deployments that set database.auto_create: false.
COPY alembic.ini ./
COPY smtp_switch ./smtp_switch

# Non-root. /data is created and owned here (before VOLUME) so a *named*
# volume inherits uid 10001 ownership. A bind mount keeps host permissions:
# the host directory must be writable by uid 10001 - see README.
RUN useradd --uid 10001 --create-home switch \
    && mkdir -p /data/spool /data/certs \
    && chown -R switch:switch /data
# Numeric so runtimes that enforce runAsNonRoot don't need to resolve the name.
USER 10001

VOLUME ["/data"]
EXPOSE 2525 8080

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD ["python", "-c", "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8080/healthz', timeout=3).status==200 else 1)"]

# Config is provided at runtime (bind-mount or env). Defaults expect /data.
ENV SMTP_SWITCH_CONFIG=/data/config.yaml \
    SMTP_SWITCH_DATABASE__URL=sqlite+aiosqlite:////data/smtp_switch.db \
    SMTP_SWITCH_DISPATCH__SPOOL_DIR=/data/spool \
    SMTP_SWITCH_WEB__HOST=0.0.0.0

ENTRYPOINT ["smtp-switch"]
