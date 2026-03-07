# syntax=docker/dockerfile:1
FROM python:3.14.3

COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

COPY pyproject.toml pyproject.toml
RUN uv pip install --system --no-cache -r pyproject.toml

COPY rocketwatch/ /app
ENV PYTHONUNBUFFERED=1
WORKDIR /app
CMD [ "python", "." ]
