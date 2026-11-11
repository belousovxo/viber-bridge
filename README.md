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

incoming + sent from phone:
           viber-session log ─▶ inbound worker ─▶ POST /webhooks/viber/:identifier (Bearer key)
outgoing:  Chatwoot inbox webhook (HMAC-signed) ─▶ POST /chatwoot/webhook ─▶ agent.py in the container
```

- **Incoming** messages are posted to Chatwoot's `POST /webhooks/viber/:inbox_identifier`,
  authenticated with the manager API key, the way Instagram messages arrive on Meta-signed webhooks.
  Chatwoot keys the contact by phone number (`source_id`) and picks the conversation like its other
  channels (a resolved one stays closed unless the inbox is locked to a single conversation). Media
  is copied out with `docker cp`. Only 1:1 chats are bridged (group/community messages carry no
  number and are skipped). The worker replays the container log on every attach and drops what its
  EventID cursor already posted, so nothing received while the manager is down is lost.
  Text, images, video, voice and files arrive as such; links and GIFs as their URL, shared contacts
  as name + number; other types (stickers, media Viber didn't download) as an "unsupported message",
  like Instagram's. Like Instagram, replies link to the message they quote, a message deleted for
  everyone is marked deleted, outgoing messages turn `read` when the contact reads them (Viber's
  `ChatsMetaData` read marker), and the contact's Viber photo becomes their avatar.
- **Sent from the phone**: what the owner writes in Viber itself (phone or desktop) goes the same
  way and is recorded as an outgoing echo (`external_echo`, like Instagram/WhatsApp echoes).
  Replies sent from Chatwoot are not echoed back: the agent reports the Viber EventIDs it produced
  and the worker skips them.
- **Access**: Chatwoot's token-free Public Inbox API is closed for Viber inboxes (404), since their
  contacts are keyed by guessable phone numbers. Session containers share the `viber-sessions`
  network with container-to-container traffic disabled, so nothing else on the host network can
  reach a session's screen (noVNC).
- **Outgoing** replies arrive on the inbox webhook. The manager verifies the signature with the
  inbox secret it got at bind time, queues sends per container in Chatwoot message order, and the
  in-container agent types them into Viber: text, png/jpeg through the media preview with the text
  as caption, other files as files with the text following. Before each send the agent closes
  Viber's overlays (promos, info panels, the "Sync chat history" prompt shown on every start). A
  send counts as delivered only when a new outgoing event to the target appears in the Viber DB.
  The outcome goes back to Chatwoot: the message gets its Viber EventID as `source_id`, or is marked
  failed with the reason.
- **Unlinked on the phone**: Viber deletes the profile and returns to its QR screen. The reaper
  notices (a running container without a profile), Chatwoot flags the inbox for reauthorization
  like an expired Instagram (sidebar warning, settings banner, email to admins), and the dead
  session is removed. The banner's reconnect runs the QR flow again and binds the new session to
  the same inbox; only the inbox's own number is accepted. A session whose database Viber can no
  longer open (it shows "Something went wrong", seen once after a hard kill) is handled the same
  way: the reader marks the DB unreadable after 3 minutes of failed reads and the reaper reports it.
- **Cleanup**: once a minute a reaper removes the container + volume of a session whose inbox was
  deleted in Chatwoot (only an explicit `exists: false` from the Viber endpoint counts; any probe
  error, 404 included, keeps the session) and of
  onboardings not completed within 15 minutes. Leaving the tile page before linking drops the
  session right away.
- Session containers run with `--restart unless-stopped` and a memory limit, and keep their login
  on the volume, so a crash or host reboot doesn't need a new QR scan. On start the manager
  recreates containers that run an older build of the session image on the same volume, so a
  rebuilt image reaches running sessions without a new QR scan: rebuild, then restart the manager.

## Production deploy

The manager needs the Docker daemon that will run the Viber containers (it mounts its socket) and
outbound internet for Viber. It can share the host with Chatwoot or run on its own. Each linked
number takes ~350 MB RAM (limit per container: `SESSION_MEMORY`, default `1g`).

**1. Build the session image** on that Docker host. The pinned Viber `.deb` is not in git, and the
CDN only serves the latest build: once Viber moves past the pin, the `curl` below gets the wrong
version. Keep a copy of the pinned `.deb` (or build the image once and push it to a private
registry) instead of relying on the CDN:

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

The manager reaches Chatwoot back at its `FRONTEND_URL` (the Viber endpoint and attachment downloads), so
that URL must be reachable from the manager.

**4. Check:** *Settings → Inboxes → Add Inbox → Viber* shows a QR, scanning it with
*Viber → More → QR scanner* creates the inbox `Viber (+number)` and continues to *Add Agents*.

Chatwoot also needs Sidekiq running (read statuses go through a job) and SMTP configured, so admins
get the email when a Viber inbox loses its connection.

**Upgrading:** rebuild the session image (step 1), then restart the manager
(`docker compose up -d --build` in `manager/`, which also upgrades the manager itself). On start it
recreates every session container that runs an older image, on the same volume, so they stay
linked. Viber ignores SIGTERM, so this kills it hard; that once left a database Viber refused to
open, which the bridge then reports like an unlinked session (the inbox asks for a QR reconnect).
Upgrade at a quiet time.

**Operating notes:**

- Every QR link activates a new Viber desktop for that number. Viber refuses activations after too
  many in a short time ("Please contact support"), for hours. Don't churn sessions, and remove
  stale devices on the phone (*Settings → Account → Computers and tablets*).
- Back up the `manager-data` volume: the registry holds each session's inbox binding and webhook
  secret. Without it the containers keep running but their inboxes silently stop receiving.
- The mounted Docker socket gives the manager root-equivalent access to its host: run it where that
  is acceptable (ideally a host or VM of its own).

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

- When Viber refuses to activate a new desktop (too many activations of a number), the session shows
  Viber's error dialog and the Chatwoot QR page keeps spinning. Surface that error instead.

Out of scope: group and community chats, contacts without a phone number.
