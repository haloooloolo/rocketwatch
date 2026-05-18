# syntax=docker/dockerfile:1

FROM python:3.14-slim AS build

# binutils provides `strip`; only needed in build stage.
RUN apt-get update && apt-get install -y --no-install-recommends binutils && \
    rm -rf /var/lib/apt/lists/*
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

COPY pyproject.toml pyproject.toml
RUN --mount=type=cache,target=/root/.cache/uv \
    uv venv /opt/venv && \
    VIRTUAL_ENV=/opt/venv uv pip install -r pyproject.toml

# Strip symbols from compiled extensions and drop bundled tests/__pycache__.
RUN find /opt/venv -type d \( -name tests -o -name test -o -name __pycache__ \) \
        -prune -exec rm -rf {} + && \
    find /opt/venv -name "*.so" -exec strip --strip-unneeded {} +


FROM python:3.14-slim AS runtime

# libopus0 is dlopen'd by discord.py for voice send/receive.
RUN apt-get update && apt-get install -y --no-install-recommends libopus0 && \
    rm -rf /var/lib/apt/lists/*

COPY --from=build /opt/venv /opt/venv
COPY rocketwatch/ /app/rocketwatch

ENV PATH="/opt/venv/bin:$PATH" PYTHONUNBUFFERED=1 PYTHONPATH=/app
WORKDIR /app/rocketwatch
CMD ["python", "."]
