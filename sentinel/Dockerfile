FROM python:3.14-slim

WORKDIR /app
COPY pyproject.toml .
RUN pip install --no-cache-dir .

COPY . .
ENV PYTHONUNBUFFERED=1
CMD ["python", "."]
