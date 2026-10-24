# syntax=docker/dockerfile:1.7
#
# The tool, for using it. Two images are built from this file:
#   docker build --target full .   (the default) with the GTFS validator, which checks
#                                  every feed before it is converted
#   docker build --target lite .   without it: no Java, about 250 MB lighter
# For working on the code see Dockerfile.dev.

FROM python:3.12-slim AS base

# osmium-tool: cuts the OSM data to the area of the feed
RUN apt-get update \
    && apt-get install -y --no-install-recommends osmium-tool \
    && rm -rf /var/lib/apt/lists/*

COPY --from=ghcr.io/astral-sh/uv:0.13.5 /uv /usr/local/bin/uv

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

COPY --chmod=755 docker/gtfstoosm /usr/local/bin/gtfstoosm

ENTRYPOINT ["python", "-m", "gtfstoosm.cli"]
CMD ["--help"]


# Without the validator
FROM base AS lite


# With the validator (MobilityData, Apache-2.0): a Java program, run by the tool on
# each feed. The jar is the release's, checked against its published checksum.
FROM base AS full

RUN apt-get update \
    && apt-get install -y --no-install-recommends default-jre-headless \
    && rm -rf /var/lib/apt/lists/*

ADD --checksum=sha256:19293ddd9b6f954f216d4f12054bd8a3232921751c4484339e339764a91000e2 \
    https://github.com/MobilityData/gtfs-validator/releases/download/v8.0.1/gtfs-validator-8.0.1-cli.jar \
    /opt/gtfs-validator/gtfs-validator-cli.jar
ENV GTFS_VALIDATOR_JAR=/opt/gtfs-validator/gtfs-validator-cli.jar
