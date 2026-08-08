# syntax=docker/dockerfile:1.7

# Release builds should update this digest with the exact approved Python base.
ARG PYTHON_IMAGE=python:3.12-slim-bookworm

FROM node:22-bookworm-slim AS web-build
WORKDIR /build/web
COPY web/package.json web/package-lock.json ./
RUN npm ci
COPY web/ ./
RUN npm run build

FROM ${PYTHON_IMAGE} AS python-build
ENV UV_LINK_MODE=copy \
    UV_COMPILE_BYTECODE=1
WORKDIR /opt/agent-hub
COPY --from=ghcr.io/astral-sh/uv:0.5.30 /uv /usr/local/bin/uv
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-editable
COPY alembic.ini ./alembic.ini
COPY alembic ./alembic
COPY src ./src

FROM ${PYTHON_IMAGE} AS runtime
ENV PATH="/opt/agent-hub/.venv/bin:${PATH}" \
    PYTHONUNBUFFERED=1 \
    AGENT_HUB_WEB_DIR=/opt/agent-hub/web
WORKDIR /opt/agent-hub
RUN groupadd --gid 10001 agent-hub \
    && useradd --uid 10001 --gid 10001 --home-dir /opt/agent-hub --shell /usr/sbin/nologin agent-hub \
    && mkdir -p /opt/agent-hub/web /var/lib/agent-hub /run/agent-hub \
    && chown -R 10001:10001 /opt/agent-hub /var/lib/agent-hub /run/agent-hub
COPY --from=python-build --chown=10001:10001 /opt/agent-hub/.venv ./.venv
COPY --from=python-build --chown=10001:10001 /opt/agent-hub/alembic.ini ./alembic.ini
COPY --from=python-build --chown=10001:10001 /opt/agent-hub/alembic ./alembic
COPY --from=python-build --chown=10001:10001 /opt/agent-hub/src ./src
COPY --from=web-build --chown=10001:10001 /build/web/dist ./web
COPY deploy/compose/healthcheck.sh /usr/local/bin/agent-hub-healthcheck
RUN chmod 0755 /usr/local/bin/agent-hub-healthcheck
USER 10001:10001
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 CMD ["agent-hub-healthcheck"]
CMD ["uvicorn", "agent_hub.app:app", "--host", "0.0.0.0", "--port", "8000"]
