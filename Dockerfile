# syntax=docker/dockerfile:1

FROM python:3.12-slim AS base

# osmium-tool: cut and filter OSM extracts (osmium extract / tags-filter)
RUN apt-get update \
    && apt-get install -y --no-install-recommends osmium-tool \
    && rm -rf /var/lib/apt/lists/*

COPY --from=ghcr.io/astral-sh/uv:0.8 /uv /usr/local/bin/uv

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PROJECT_ENVIRONMENT=/opt/venv \
    PYTHONPATH=/app \
    GTFSTOOSM_CACHE=/data/cache \
    PATH="/opt/venv/bin:$PATH"

WORKDIR /app

# Dependencies first so they stay cached when only the source changes
COPY pyproject.toml uv.lock README.md ./
RUN uv sync --frozen --no-install-project --no-dev

COPY gtfstoosm ./gtfstoosm
RUN uv sync --frozen --no-dev

# Development image: tests and linting, source is bind-mounted by compose
FROM base AS dev
RUN uv pip install --python /opt/venv/bin/python pytest ruff
COPY tests ./tests
CMD ["pytest", "-m", "not slow"]

# Default image: the converter CLI
FROM base AS runtime
ENTRYPOINT ["python", "-m", "gtfstoosm.cli"]
CMD ["--help"]
