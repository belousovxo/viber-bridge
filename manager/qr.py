#!/usr/bin/env python3
"""Native QR: screenshot the session's Viber login screen and return PNG bytes.

The UI renders this as a plain <img> and refreshes it (the Viber QR rotates), so no live desktop
is ever exposed. Once the session is linked the caller stops requesting it.

Uses `maim` inside the container (added to the session image). The login screen fills the small
800x600 Xvfb, so the whole root is returned; cropping to the QR rectangle is a later refinement.
"""

from . import docker_ops


def screenshot_png(container):
    """Return PNG bytes of the container's screen, or None if it isn't capturable yet."""
    ok, out = docker_ops.exec_sh(container, "DISPLAY=:99 maim 2>/dev/null", binary=True, timeout=15)
    if ok and out:
        return out
    return None
