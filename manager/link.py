#!/usr/bin/env python3
"""Link detection: is a session's Viber logged in, and to which number?

`linked` once BOTH hold inside the container:
  - the SQLCipher key was captured (/tmp/viber-key.hex is non-empty), and
  - a profile dir exists at ~/.ViberPC/<number>/ with a viber.db.

The profile folder name is the account's own E.164 digits, so the number comes for free.
"""

import re

from . import docker_ops


def lost(container):
    """Why a linked session can no longer work, or None:
      - "unlinked": unlinking on the phone makes Viber delete the profile dir and go back to the QR
        screen (the profile lives on the volume, so a restart never looks like this);
      - "unreadable": the reader has failed to open viber.db for minutes (session/reader.py).
    A stopped container is unknown (None)."""
    if not docker_ops.container_running(container):
        return None
    ok, out = docker_ops.exec_sh(
        container,
        "ls /home/viber/.ViberPC/*/viber.db >/dev/null 2>&1 || echo unlinked; "
        "test -e /tmp/viber-db-unreadable && echo unreadable; true")
    reasons = (out or "").split() if ok else []
    return reasons[0] if reasons else None


def probe(container):
    """Return (status, number). status in {starting, linking, linked}."""
    if not docker_ops.container_running(container):
        return "starting", None

    ok, out = docker_ops.exec_sh(
        container,
        # print the profile dir basename if a linked profile exists, else nothing
        "for d in /home/viber/.ViberPC/*/; do "
        "[ -f \"$d/viber.db\" ] && basename \"$d\"; done 2>/dev/null")
    profile = (out or "").strip().splitlines()
    number = None
    for name in profile:
        digits = re.sub(r"\D", "", name)
        if digits:
            number = "+" + digits
            break

    key_ok, _ = docker_ops.exec_sh(container, "test -s /tmp/viber-key.hex && echo ok")
    key_present = (_ or "").strip() == "ok"

    if number and key_present:
        return "linked", number
    return "linking", number
