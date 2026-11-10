# Viber Session Manager — the control plane. Talks to the host docker daemon (socket mounted)
# to spawn/drive per-session Viber containers, and serves the HTTP API.
FROM python:3.12-slim

# docker CLI (to drive sibling containers via the mounted socket)
RUN apt-get update && apt-get install -y --no-install-recommends docker.io \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY manager/requirements.txt /app/manager/requirements.txt
RUN pip install --no-cache-dir -r /app/manager/requirements.txt

# bridge/ provides the reused Chatwoot HTTP client (manager.inbound imports bridge.chatwoot)
COPY bridge /app/bridge
COPY manager /app/manager

ENV MANAGER_REGISTRY=/data/sessions.json
VOLUME ["/data"]
EXPOSE 8800
CMD ["python3", "-m", "manager.app"]
