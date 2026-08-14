FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

RUN addgroup --system app && adduser --system --ingroup app app

COPY pyproject.toml README.md ./
COPY kraken_telegram_gateway ./kraken_telegram_gateway

RUN pip install .

RUN mkdir -p /data && chown -R app:app /app /data
USER app

EXPOSE 8000

CMD ["uvicorn", "kraken_telegram_gateway.gateway.app:app", "--host", "0.0.0.0", "--port", "8000"]
