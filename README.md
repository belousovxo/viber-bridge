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

## Next steps

- Agent inside the container: read new messages and send them.
- Manager: API to create and remove sessions (one container per number), QR screenshot, status.
- Bridge to a Chatwoot API inbox.
