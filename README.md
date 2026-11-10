# viber-bridge

Connect a regular (numbered) Viber account to Chatwoot. Each number runs its own Viber Desktop in a
Docker container, linked as a secondary device by QR code. In Chatwoot it shows up as a **Viber**
tile under *Add Inbox*: scan the QR with the phone and the number becomes an inbox.

## How it works

Two parts:

- **Session manager** (`manager/`, this repo): one `viber-session-<id>` container + volume per
  number (image built from `session/`), the login QR, link detection, the session registry and the
  per-session message bridge.
- **Chatwoot** (the `xotravel/chatwoot` fork): the Viber tile, and a server-side proxy
  (`/api/v1/accounts/:id/viber/sessions`) to the manager. Browsers never talk to the manager.

```
browser ── Chatwoot (admin only) ──Bearer key──▶ manager ──docker──▶ viber-session-<id>
             POST   viber/sessions                POST /sessions        boots Viber
             GET    viber/sessions/:id/qr         GET  /sessions/:id/qr  QR decoded + re-rendered
             GET    viber/sessions/:id            GET  /sessions/:id     starting → linking → linked
             POST   viber/sessions/:id/inbox      creates a Channel::Api inbox, then
                                                  POST /sessions/:id/bind {inbox, identifier, secret}

incoming:  viber-session log ─▶ inbound worker ─▶ Chatwoot Public Inbox API (no token)
outgoing:  Chatwoot inbox webhook (HMAC-signed) ─▶ POST /chatwoot/webhook ─▶ agent.py in the container
```

- **Incoming** messages go through the token-free **Public Inbox API**, keyed by the inbox
  identifier, with `source_id` = the sender's number so contacts and threads never duplicate. Media
  is copied out with `docker cp`. Only 1:1 chats are bridged (group/community messages carry no
  sender number and are skipped). The worker replays the container log on every attach and drops
  what its EventID cursor already posted, so nothing received while the manager is down is lost.
- **Outgoing** replies arrive on the inbox webhook. The manager verifies the signature with the
  inbox secret it got at bind time, queues sends per container in Chatwoot message order, and the
  in-container agent types them into Viber: text, png/jpeg through the media preview with the text
  as caption, other files as files with the text following. Before each send the agent closes
  Viber's overlays (promos, info panels, the "Sync chat history" prompt shown on every start). A
  send counts as delivered only when a new outgoing event to the target appears in the Viber DB.
- **Cleanup**: once a minute a reaper removes the container + volume of a session whose inbox was
  deleted in Chatwoot (Public API returns 404; probe errors never count as a deletion) and of
  onboardings not completed within 15 minutes. Leaving the tile page before linking drops the
  session right away.
- Session containers run with `--restart unless-stopped` and a memory limit, and keep their login
  on the volume, so a crash or host reboot doesn't need a new QR scan.

## Production deploy

The manager needs the Docker daemon that will run the Viber containers (it mounts its socket) and
outbound internet for Viber. It can share the host with Chatwoot or run on its own. Each linked
number takes ~350 MB RAM (limit per container: `SESSION_MEMORY`, default `1g`).

**1. Build the session image** on that Docker host. The pinned Viber `.deb` is not in git:

```bash
git clone <this repo> viber-bridge && cd viber-bridge
curl -fsSL -o session/vendor/viber.deb https://download.cdn.viber.com/cdn/desktop/Linux/viber.deb
dpkg-deb -f session/vendor/viber.deb Version     # must be 29.4.0.7, see session/vendor/README.md
docker compose build                             # tags viber-session:latest
```

**2. Run the manager:**

```bash
export MANAGER_API_KEY=$(openssl rand -base64 32)    # keep it: Chatwoot needs the same value
cd manager
DOCKER_SOCK=/var/run/docker.sock docker compose up -d --build   # rootless: /run/user/<uid>/docker.sock
curl -s localhost:8800/health                                  # {"ok":true}
```

The registry lives in the `manager-data` volume. Keep port 8800 reachable **only from Chatwoot**
(web + sidekiq), e.g. a private network or firewall rule. If that traffic crosses the internet, put
the manager behind TLS.

| Manager env | |
| --- | --- |
| `MANAGER_API_KEY` | required; the manager refuses to start without it |
| `DOCKER_SOCK` | host path of the Docker socket (compose only) |
| `SESSION_MEMORY` | memory limit per Viber container, default `1g` |
| `VIBER_SESSION_IMAGE` | default `viber-session:latest` |
| `MANAGER_PORT` | published port, default `8800` |

**3. Configure Chatwoot** (web and sidekiq), then restart both:

| Chatwoot env | |
| --- | --- |
| `VIBER_MANAGER_URL` | the manager as Chatwoot's servers reach it, e.g. `http://viber-manager:8800`. Also enables the Viber tile |
| `VIBER_MANAGER_API_KEY` | the same value as `MANAGER_API_KEY` |
| `SAFE_FETCH_ALLOW_PRIVATE_NETWORK=true` | only if `VIBER_MANAGER_URL` is a private address (Chatwoot's webhook delivery refuses private IPs otherwise) |

The manager reaches Chatwoot back at its `FRONTEND_URL` (Public API and attachment downloads), so
that URL must be reachable from the manager.

**4. Check:** *Settings → Inboxes → Add Inbox → Viber* shows a QR, scanning it with
*Viber → More → QR scanner* creates the inbox `Viber (+number)` and continues to *Add Agents*.

**Upgrading:** a rebuilt `viber-session` image is used by new sessions only. Running containers keep
their image until recreated with the same name and volume (no tooling for that yet). Rebuild and
`docker compose up -d --build` in `manager/` to upgrade the manager itself; bound sessions resume.

## Local development

```bash
docker compose build                                   # session image (needs session/vendor/viber.deb)
python3 -m venv .venv && . .venv/bin/activate
pip install -r manager/requirements.txt
MANAGER_API_KEY=<key> python3 -m manager.app           # :8800, uses the host docker CLI
```

Running the manager on the host is the simplest with a dockerized local Chatwoot: the manager
reaches Chatwoot at `http://localhost:3000`. Chatwoot's containers reach the manager at the host's
IP, so in Chatwoot's `.env` set `VIBER_MANAGER_URL=http://<host-ip>:8800`, the same
`VIBER_MANAGER_API_KEY` and `SAFE_FETCH_ALLOW_PRIVATE_NETWORK=true`, then recreate `rails` and
`sidekiq`. With rootless Docker `host.docker.internal` doesn't reach host processes; use the
host's LAN/WSL address (`ip -4 addr`), which can change after a reboot.

`docker compose up` at the repo root still starts a single standalone session with its screen at
http://localhost:6080, which is handy for poking at Viber by hand.

## Manager API

All routes except the webhook and `/health` require `Authorization: Bearer <MANAGER_API_KEY>`.

| Method | Path | Purpose |
| --- | --- | --- |
| POST | `/sessions` | provision a container, returns `session_id` |
| GET | `/sessions` | all sessions (ops/debugging) |
| GET | `/sessions/:id` | `{status, number}` (starting → linking → linked) |
| GET | `/sessions/:id/qr` | login QR as `image/png`; 202 while not on screen, 409 once linked |
| POST | `/sessions/:id/bind` | `{inbox_id, inbox_identifier, webhook_secret, chatwoot_url, account_id?}`, starts the bridge |
| DELETE | `/sessions/:id` | stop + remove the container and volume |
| POST | `/chatwoot/webhook` | Channel::Api webhook, verified by `X-Chatwoot-Signature` |
| GET | `/health` | liveness |

## Next steps

- Mirror messages sent from the phone as outgoing messages in Chatwoot.
- Reflect `delivered`/`read` from the Viber DB back to Chatwoot message status.
- Recreate session containers on a new image without a new QR scan.
