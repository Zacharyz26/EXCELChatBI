# syntax=docker/dockerfile:1.7

ARG PYTHON_IMAGE=python:3.11.15-slim-bookworm
ARG UV_IMAGE=ghcr.io/astral-sh/uv:0.11.24

FROM ${UV_IMAGE} AS uv-bin

FROM ${PYTHON_IMAGE} AS python-builder

COPY --from=uv-bin /uv /uvx /bin/
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=0
WORKDIR /app

# Cache third-party dependencies independently from application source.
COPY pyproject.toml uv.lock README.md ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --no-install-project --no-editable \
      --extra stats --extra report --extra rag-store --extra mcp

COPY apps ./apps
COPY mcp_servers ./mcp_servers
COPY packages ./packages
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --no-dev --no-editable \
      --extra stats --extra report --extra rag-store --extra mcp


FROM ${PYTHON_IMAGE} AS api

ARG APP_UID=10001
ARG APP_GID=10001
ARG BUILD_DATE=unknown
ARG VCS_REF=unknown
LABEL org.opencontainers.image.title="ChatBI API" \
      org.opencontainers.image.version="0.1.0" \
      org.opencontainers.image.created="${BUILD_DATE}" \
      org.opencontainers.image.revision="${VCS_REF}"
RUN apt-get update \
    && apt-get install --no-install-recommends -y \
      libffi8 \
      libharfbuzz-subset0 \
      libjpeg62-turbo \
      libopenjp2-7 \
      libpango-1.0-0 \
      libpangoft2-1.0-0 \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --gid "${APP_GID}" chatbi \
    && useradd --uid "${APP_UID}" --gid "${APP_GID}" --no-create-home chatbi \
    && mkdir -p \
      /app/config \
      /app/docs/kb_samples \
      /var/lib/chatbi/db \
      /var/lib/chatbi/uploads \
      /var/lib/chatbi/datasets \
      /var/lib/chatbi/artifacts \
      /var/lib/chatbi/kb \
    && chown -R "${APP_UID}:${APP_GID}" /var/lib/chatbi

WORKDIR /app
COPY --from=python-builder /app/.venv /app/.venv
COPY config ./config
COPY docs/kb_samples ./docs/kb_samples

ENV PATH=/app/.venv/bin:$PATH \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    APP_ENV=production \
    MODEL_REGISTRY_PATH=config/models.example.yaml \
    RAG_EMBEDDER=hashing \
    RAG_RERANKER=lexical \
    RAG_STORE=local \
    CHAT_DB_PATH=/var/lib/chatbi/db/chatbi.db \
    UPLOAD_DIR=/var/lib/chatbi/uploads \
    DATASET_DIR=/var/lib/chatbi/datasets \
    REPORT_DIR=/var/lib/chatbi/artifacts \
    KB_INDEX_DIR=/var/lib/chatbi/kb/index \
    KB_BACKUP_DIR=/var/lib/chatbi/kb/backups

USER ${APP_UID}:${APP_GID}
EXPOSE 8000
HEALTHCHECK --interval=15s --timeout=3s --start-period=20s --retries=3 \
  CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=2).read()"]
CMD ["uvicorn", "apps.api.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1"]
