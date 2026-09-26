FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /app
COPY service_09261_003 ./service_09261_003
COPY tests ./tests
COPY README.md ./

RUN useradd --create-home appuser \
    && mkdir -p /data \
    && chown appuser:appuser /data
USER appuser

ENV HOST=0.0.0.0 \
    PORT=8080 \
    DB_PATH=/data/cases.db
EXPOSE 8080
VOLUME ["/data"]

HEALTHCHECK --interval=30s --timeout=3s --start-period=5s \
  CMD python -c "import urllib.request;urllib.request.urlopen('http://127.0.0.1:8080/healthz',timeout=2)"

CMD ["python", "-m", "service_09261_003"]
