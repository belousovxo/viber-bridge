#!/usr/bin/env python3
"""Native QR: the session's Viber login QR as a crisp PNG of just the code.

Inside the container: screenshot the login screen (`maim`), decode the QR from it (`zbarimg`) and
re-render the same payload (`qrencode`). The UI shows it as a plain <img> and refreshes it (the
Viber QR rotates), so no live desktop is ever exposed. Once linked the caller stops requesting it.
"""

from . import docker_ops

# `$(...)` strips zbarimg's trailing newline so the re-rendered payload is byte-identical.
RENDER = (
    "DISPLAY=:99 maim /tmp/qr-shot.png 2>/dev/null"
    " && code=$(zbarimg --raw -q /tmp/qr-shot.png)"
    " && qrencode -t PNG -s 10 -m 2 -o - \"$code\""
)


def qr_png(container):
    """PNG bytes of the login QR, or None while no QR is on screen yet."""
    ok, out = docker_ops.exec_sh(container, RENDER, binary=True, timeout=15)
    return out if ok and out else None
