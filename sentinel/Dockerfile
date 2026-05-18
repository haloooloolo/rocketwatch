FROM python:3.14-slim

WORKDIR /app
COPY pyproject.toml .
RUN --mount=type=cache,target=/root/.cache/pip pip install .

COPY . /app/sentinel
ENV PYTHONUNBUFFERED=1 PYTHONPATH=/app
WORKDIR /app/sentinel
CMD ["python", "."]
