# Viber Session Manager — the control plane. Talks to the host docker daemon (socket mounted)
# to spawn/drive per-session Viber containers, and serves the HTTP API.
FROM python:3.12-slim

# docker CLI only (drives sibling containers via the mounted socket; the daemon is the host's)
RUN apt-get update && apt-get install -y --no-install-recommends docker-cli \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY manager/requirements.txt /app/manager/requirements.txt
RUN pip install --no-cache-dir -r /app/manager/requirements.txt

# bridge/ provides the reused Chatwoot HTTP client (manager.inbound imports bridge.chatwoot)
COPY bridge /app/bridge
COPY manager /app/manager

ENV MANAGER_REGISTRY=/data/sessions.json PYTHONUNBUFFERED=1
VOLUME ["/data"]
EXPOSE 8800
# Exactly ONE worker: the registry, send queues and per-session threads live in this process.
# Threads serve concurrent requests (QR polling, webhooks).
CMD ["gunicorn", "--workers", "1", "--threads", "16", "--bind", "0.0.0.0:8800", \
     "--access-logfile", "-", "manager.app:create_app()"]
