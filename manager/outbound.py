#!/usr/bin/env python3
"""Outbound dispatcher: Chatwoot -> Viber.

The shared webhook endpoint hands each Channel::Api `message_created` payload here. On an outgoing
message we find the session by inbox id (registry), extract the recipient number, and run the
in-container send agent. Attachments are downloaded from their Chatwoot `data_url`, copied into the
container and sent as files (the text becomes the first file's caption). Delivery-status updates
are deferred, so this path never calls back into the Chatwoot API.
"""

import json
import os
import shutil
import tempfile
import threading
from urllib.parse import unquote, urlparse

import requests

from . import docker_ops

AGENT = "/usr/local/bin/agent.py"
OUTBOX_PREFIX = "/tmp/outbox-"  # per-message dir inside the container holding files to send
OUTBOX_GLOB = "outbox-*"

# agent.py drives the Viber UI via xdotool + AT-SPI, so `docker exec` must join the container's
# running session: its X display and the shared D-Bus session bus (where at-spi-bus-launcher
# registers). These match session/supervisord.conf; without them pyatspi fails to reach the a11y bus.
AGENT_ENV = {
    "DISPLAY": ":99",
    "DBUS_SESSION_BUS_ADDRESS": "unix:path=/tmp/dbus-session",
}


def _number_from_payload(payload):
    conv = payload.get("conversation") or {}
    ci = conv.get("contact_inbox") or {}
    if ci.get("source_id"):
        return ci["source_id"]
    sender = ((conv.get("meta") or {}).get("sender")) or {}
    return sender.get("phone_number")


def _is_outgoing(payload):
    return payload.get("message_type") in ("outgoing", 1)


class Dispatcher:
    def __init__(self, registry, log):
        self.reg = registry
        self.log = log
        self._seen = set()
        self._lock = threading.Lock()
        self._ui_locks = {}

    def _dedup(self, message_id, cap=2000):
        with self._lock:
            if message_id in self._seen:
                return False
            self._seen.add(message_id)
            if len(self._seen) > cap:
                self._seen = set(list(self._seen)[-cap:])
            return True

    def handle(self, payload):
        if payload.get("event") != "message_created" or not _is_outgoing(payload) or payload.get("private"):
            return
        message_id = payload.get("id")
        if message_id is None or not self._dedup(message_id):
            return

        inbox_id = (payload.get("inbox") or {}).get("id") or (payload.get("conversation") or {}).get("inbox_id")
        session = self.reg.by_inbox(inbox_id) if inbox_id else None
        number = _number_from_payload(payload)
        content = payload.get("content")
        attachments = payload.get("attachments") or []

        if not session:
            self.log(f"[out] no session for inbox {inbox_id}")
            return
        if not number or not (content or attachments):
            self.log(f"[out] skip msg {message_id}: {'no number' if not number else 'empty message'}")
            return

        number = "+" + str(number).lstrip("+")
        container = session["container"]
        # One Viber window per container: sends must not interleave their clicks/pastes.
        with self._ui_lock(container):
            if attachments:
                res = self._send_files(container, number, attachments, content, message_id)
            else:
                res = self._agent(container, ["send", number, content])
        if res.get("ok"):
            self.log(f"[out] {self.sid_hint(session)} {number} delivered "
                     f"({res.get('elapsed', 0):.2f}s) msg {message_id}")
        else:
            self.log(f"[out] {number} msg {message_id} FAILED: {res.get('reason')}")

    @staticmethod
    def sid_hint(session):
        return session.get("id", "?")

    def _ui_lock(self, container):
        with self._lock:
            return self._ui_locks.setdefault(container, threading.Lock())

    def _send_files(self, container, number, attachments, caption, message_id):
        """Download the Chatwoot attachments, copy them into the container, send each one; the
        message text rides as the first file's caption."""
        outbox = f"{OUTBOX_PREFIX}{message_id}"
        # Viber may still be reading a just-sent file, so outboxes are swept later, not right away.
        docker_ops.exec_sh(container, f"find /tmp -maxdepth 1 -name '{OUTBOX_GLOB}' -mmin +60 "
                                      "-exec rm -rf {} +")
        local = tempfile.mkdtemp(prefix="viber-out-")
        try:
            os.chmod(local, 0o755)  # docker cp keeps the mode; the viber user must read it
            names = []
            for att in attachments:
                url = att["data_url"]
                name = os.path.basename(unquote(urlparse(url).path)) or f"file.{att.get('extension') or 'bin'}"
                r = requests.get(url, timeout=60)
                r.raise_for_status()
                with open(os.path.join(local, name), "wb") as f:
                    f.write(r.content)
                names.append(name)
            if not docker_ops.cp_in(container, local, outbox):
                return {"ok": False, "reason": "docker cp into container failed"}
        except requests.RequestException as e:
            return {"ok": False, "reason": f"attachment download failed: {e}"}
        finally:
            shutil.rmtree(local, ignore_errors=True)

        res = {"ok": False, "reason": "no attachments"}
        for i, name in enumerate(names):
            res = self._agent(container, ["send-file", number, f"{outbox}/{name}",
                                          (caption or "") if i == 0 else ""])
            if not res.get("ok"):
                return {**res, "reason": f"{name}: {res.get('reason')}"}
        return res

    def _agent(self, container, args):
        r = docker_ops.exec_argv(container, ["python3", AGENT, *args], env=AGENT_ENV)
        for line in reversed((r.stdout or "").splitlines()):
            line = line.strip()
            if line.startswith("{"):
                try:
                    return json.loads(line)
                except json.JSONDecodeError:
                    break
        return {"ok": False, "reason": f"agent exit {r.returncode}: {(r.stderr or '')[:200]}"}
