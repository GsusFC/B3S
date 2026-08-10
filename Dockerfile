FROM python:3.11-slim

ARG B3S_BUILD_SHA=unknown

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PLAYWRIGHT_BROWSERS_PATH=/ms-playwright \
    PYTHONPATH=/app \
    B3S_BUILD_SHA=${B3S_BUILD_SHA}

LABEL org.opencontainers.image.revision="${B3S_BUILD_SHA}"

WORKDIR /app

COPY . .

RUN pip install --no-cache-dir . \
    && python -m playwright install --with-deps chromium \
    && apt-get update \
    && apt-get install -y --no-install-recommends gosu \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --gid 10001 b3s \
    && groupadd --gid 10002 b3s-acquisition \
    && useradd --create-home --uid 10001 --gid b3s --shell /usr/sbin/nologin b3s \
    && useradd --no-create-home --home-dir /nonexistent --uid 10002 \
        --gid b3s-acquisition --shell /usr/sbin/nologin b3s-worker \
    && usermod --append --groups b3s-acquisition b3s \
    && mkdir -p \
        /app/data/reports \
        /app/data/screenshots \
        /data/reports \
        /data/screenshots \
        /usr/local/lib/b3s \
    && chown -R b3s:b3s /app/data /data /ms-playwright

COPY deploy/fly_entrypoint.sh /usr/local/bin/b3s-entrypoint
COPY deploy/prepare_vault_volume.py /usr/local/lib/b3s/prepare_vault_volume.py
COPY deploy/vault_worker_supervisor.py /usr/local/lib/b3s/vault_worker_supervisor.py
RUN chmod 755 /usr/local/bin/b3s-entrypoint \
    && chmod 644 \
        /usr/local/lib/b3s/prepare_vault_volume.py \
        /usr/local/lib/b3s/vault_worker_supervisor.py \
    && chown root:root \
        /usr/local/bin/b3s-entrypoint \
        /usr/local/lib/b3s/prepare_vault_volume.py \
        /usr/local/lib/b3s/vault_worker_supervisor.py

EXPOSE 8080

ENTRYPOINT ["/usr/local/bin/b3s-entrypoint"]
CMD ["python", "-m", "uvicorn", "web.app:app", "--host", "0.0.0.0", "--port", "8080", "--workers", "1"]
