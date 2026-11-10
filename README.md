# viber-bridge

Connect a regular (numbered) Viber account to Chatwoot. Each number runs its own Viber Desktop in a Docker container, linked as a secondary device by QR code.

## v0: one session, manual check

Goal: confirm that Viber Desktop runs in a container, links by QR, and receives and sends messages.

```bash
docker compose up --build
```

Open http://localhost:6080 → **Connect**. You'll see the Viber screen with a QR code. Scan it from your phone (Viber → More → QR scanner) and confirm the login.

The profile is stored in the `viber-profile` volume, so restarting the container doesn't require a new QR scan:

```bash
docker compose restart
```

Look inside the profile (where Viber keeps its data):

```bash
docker compose exec viber-session ls -la /home/viber/.ViberPC
```

Full reset (logs the session out):

```bash
docker compose down -v
```

## Session Manager (`manager/`) — the service

A standalone control plane that makes Viber a self-serve Chatwoot channel: it provisions a Viber
container per number on demand, serves the login **QR** natively, detects linking + the number,
holds a dynamic session registry, and runs the per-session inbound/outbound bridge automatically.
The Chatwoot side is a thin **Viber tile** (a `Channel::Api` inbox) that drives this API and binds
the resulting inbox + an AgentBot token to a session.

```
Chatwoot "Viber" tile ──POST /sessions──▶ manager boots a container
                      ◀─GET  /qr──────── native QR (screenshot), scanned by the user's phone
                      ◀─GET  /sessions/:id─ status → linked + number
                      ──POST /bind────▶ {account_id, inbox_id, chatwoot_url, bot token}
Viber container ─docker logs▶ inbound worker ─▶ Chatwoot (incoming + media, bot token)
Chatwoot webhook ─▶ POST /chatwoot/webhook ─docker exec▶ agent.py send (outgoing)
```

Internals (`bridge/` provides the reused Chatwoot HTTP client): incoming are posted **token-free**
via the **Public Inbox API** (keyed by the channel `inbox_identifier`), with `source_id` = the
sender's number so contacts/threads never duplicate; media pulled out with `docker cp`. Outgoing
agent replies arrive on the Channel::Api `webhook_url` and are sent by the in-container agent.
Delivery statuses are deferred (the Public API doesn't update message status).

### Run (local)

```bash
# 1. build + tag the per-session image
docker compose build                     # tags viber-session:latest

# 2. run the manager ON THE HOST (simplest networking for a local, dockerized Chatwoot)
pip install -r manager/requirements.txt
python3 -m manager.app                    # listens on :8800, uses the host docker CLI

# 3. drive it by hand to prove the flow
curl -s -XPOST localhost:8800/sessions                 # -> {"session_id": "..."}
curl -s localhost:8800/sessions/<id>/qr -o qr.png      # scan from a phone
curl -s localhost:8800/sessions/<id>                   # -> {"status":"linked","number":"+..."}
```

(`manager/docker-compose.yml` runs it as a container with the docker socket mounted — better for a
hosted deployment. Locally the host run is simpler: the manager reaches a dockerized Chatwoot at
`http://localhost:3000` directly.)

**Chatwoot env** — the browser and the Rails server reach the manager at different addresses when
both are local behind Docker, so there are two knobs:

| ENV | Who uses it | Local value |
| --- | --- | --- |
| `VIBER_MANAGER_URL` | the browser (onboarding fetches + QR) | `http://localhost:8800` |
| `VIBER_MANAGER_WEBHOOK_URL` | the Rails container (inbox webhook) | `http://host.docker.internal:8800/chatwoot/webhook` |

Also run local Chatwoot with `SAFE_FETCH_ALLOW_PRIVATE_NETWORK=true` so the webhook can reach the
manager. In production with a real manager hostname, set only `VIBER_MANAGER_URL` (the webhook URL
defaults to `<VIBER_MANAGER_URL>/chatwoot/webhook`).

The Chatwoot "Viber" tile then automates everything: it creates the session, shows the QR, creates
the inbox on link, and binds it.

### API

| Method | Path | Purpose |
| --- | --- | --- |
| POST | `/sessions` | provision a container, returns `session_id` |
| GET | `/sessions/:id` | `{status, number}` (starting → linking → linked) |
| GET | `/sessions/:id/qr` | login QR as `image/png` (409 once linked) |
| POST | `/sessions/:id/bind` | attach `{inbox_id, inbox_identifier, chatwoot_url}` + start workers |
| DELETE | `/sessions/:id` | stop + remove container/volume |
| POST | `/chatwoot/webhook` | Channel::Api webhook → Viber send |

## Next steps

- Mirror messages the user sends from Viber directly as outgoing in Chatwoot.
- Reflect `delivered`/`read` from the Viber DB back to Chatwoot message status.
- Crop the QR screenshot to the code; optionally decode + re-render crisply.
