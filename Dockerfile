# Alternative to the systemd setup: docker compose up -d   (see docs/12-deployment.md)
FROM python:3.12-slim
ENV PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1 ABG_DATA_DIR=/data ABG_CACHE_DIR=/data/cache
WORKDIR /app
COPY pyproject.toml README.md NOTICE ./
COPY abg ./abg
RUN pip install ".[yfinance,fast,charts]" && useradd -m -u 1000 abg && mkdir -p /data && chown abg /data
USER abg
EXPOSE 8000
HEALTHCHECK --interval=60s --timeout=5s --start-period=30s CMD python -c "import urllib.request;urllib.request.urlopen('http://127.0.0.1:8000/api/health')"
CMD ["abg", "serve", "--host", "0.0.0.0", "--port", "8000"]
