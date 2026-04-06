FROM python:3.14-slim

WORKDIR /app
COPY pyproject.toml .
RUN pip install --no-cache-dir .

COPY . /app/sentinel
ENV PYTHONUNBUFFERED=1 PYTHONPATH=/app
WORKDIR /app/sentinel
CMD ["python", "."]
