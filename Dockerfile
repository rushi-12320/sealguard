FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    SEALGUARD_DB=/tmp/sealguard.db \
    SEALGUARD_READ_ONLY=true

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt \
    && useradd --create-home --uid 10001 app

COPY sealguard ./sealguard
COPY static ./static

USER app
EXPOSE 8080

CMD ["sh", "-c", "exec python -m sealguard --host 0.0.0.0 --port \"${PORT:-8080}\""]
