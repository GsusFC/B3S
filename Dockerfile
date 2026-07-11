FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PLAYWRIGHT_BROWSERS_PATH=/ms-playwright \
    PYTHONPATH=/app

WORKDIR /app

COPY . .

RUN pip install --no-cache-dir . \
    && python -m playwright install --with-deps chromium \
    && apt-get update \
    && apt-get install -y --no-install-recommends gosu \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --create-home --uid 10001 --shell /usr/sbin/nologin b3s \
    && mkdir -p /data/reports /data/screenshots \
    && chown -R b3s:b3s /app /data /ms-playwright

COPY deploy/fly_entrypoint.sh /usr/local/bin/b3s-entrypoint
RUN chmod 755 /usr/local/bin/b3s-entrypoint

EXPOSE 8080

ENTRYPOINT ["/usr/local/bin/b3s-entrypoint"]
CMD ["python", "-m", "uvicorn", "web.app:app", "--host", "0.0.0.0", "--port", "8080", "--workers", "1"]
