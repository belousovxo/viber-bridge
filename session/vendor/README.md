# Vendored Viber build (pinned)

The image installs Viber from `session/vendor/viber.deb` instead of pulling "latest"
from the CDN, so a rebuild never silently changes the Viber version. The reader module
is tied to a specific build, so the version must not drift.

**Pinned version: `29.4.0.7` (amd64)** — released 2026-11-05.

The `.deb` is gitignored (146 MB). To populate it on a fresh checkout:

```bash
curl -fsSL -o session/vendor/viber.deb https://download.cdn.viber.com/cdn/desktop/Linux/viber.deb
# verify the version matches the pin:
dpkg-deb -f session/vendor/viber.deb Version   # expect 29.4.0.7
```

If the CDN has moved past this version, source the exact `.deb` from an archive.
Do not upgrade the pin without re-checking the reader against the new build.
