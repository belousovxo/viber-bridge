# Vendored Viber build (pinned)

The bridge is tied to one Viber Desktop build: the reader, the key capture and the UI-driven
sending are verified on it only.

**Pinned version: `29.4.0.7` (amd64)**, released 2026-11-05,
sha256 `34b3c18f5d441a9813066811d037e971e1207d439c246766abd28e185a7ecd37`.

`docker compose build` takes the `.deb` from here (`session/vendor/viber.deb`, gitignored, 146 MB)
if it is present, otherwise downloads it from the Viber CDN. Either way the build checks the sha256
and fails if it doesn't match, so a rebuild never silently installs another Viber version.

The CDN only serves the latest build. Once Viber releases a newer one, the build fails with
"viber.deb is not Viber 29.4.0.7". Then either:

- put a saved copy of the pinned `.deb` here as `session/vendor/viber.deb` and build again, or
- verify the bridge on the new build, then update `VIBER_VERSION` and `VIBER_SHA256` in
  `session/Dockerfile` and this file.

Keep a copy of the pinned `.deb` outside the server (any private storage) for the first case.
