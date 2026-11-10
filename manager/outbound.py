#!/usr/bin/env python3
"""Outbound dispatcher: Chatwoot -> Viber.

The shared webhook endpoint hands each Channel::Api `message_created` payload here. On an outgoing
message we find the session by inbox id (registry), extract the recipient number, and run the
in-container send agent. Delivery-status updates are deferred, so this path never calls back into
the Chatwoot API.
"""

import json
import threading

from . import docker_ops

AGENT = "/usr/local/bin/agent.py"

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

        if not session:
            self.log(f"[out] no session for inbox {inbox_id}")
            return
        if not number or not content:
            self.log(f"[out] skip msg {message_id}: "
                     f"{'no number' if not number else 'empty/attachment-only content'}")
            return

        number = "+" + str(number).lstrip("+")
        res = self._send(session["container"], number, content)
        if res.get("ok"):
            self.log(f"[out] {self.sid_hint(session)} {number} delivered "
                     f"({res.get('elapsed', 0):.2f}s) msg {message_id}")
        else:
            self.log(f"[out] {number} msg {message_id} FAILED: {res.get('reason')}")

    @staticmethod
    def sid_hint(session):
        return session.get("id", "?")

    def _send(self, container, number, text):
        r = docker_ops.exec_argv(container, ["python3", AGENT, "send", number, text], env=AGENT_ENV)
        for line in reversed((r.stdout or "").splitlines()):
            line = line.strip()
            if line.startswith("{"):
                try:
                    return json.loads(line)
                except json.JSONDecodeError:
                    break
        return {"ok": False, "reason": f"agent exit {r.returncode}: {(r.stderr or '')[:200]}"}
