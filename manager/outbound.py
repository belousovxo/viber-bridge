#!/usr/bin/env python3
"""Outbound dispatcher: Chatwoot -> Viber.

The shared webhook endpoint authenticates each Channel::Api payload, resolves its session and hands
both here. Outgoing messages are queued per container and sent in Chatwoot message order by the
in-container agent to the recipient number. Attachments are downloaded from their Chatwoot
`data_url`, copied into the container and sent as files (the text becomes the first file's caption).
The agent reports the Viber EventIDs it produced, so the inbound worker can tell these sends from
messages the owner typed on the phone (`sent_here`). Delivery-status updates are deferred, so this
path never calls back into the Chatwoot API.
"""

import json
import os
import queue
import re
import shutil
import tempfile
import threading
import time
from urllib.parse import unquote, urlparse

import requests

from . import docker_ops

AGENT = "/usr/local/bin/agent.py"
OUTBOX_PREFIX = "/tmp/outbox-"  # per-message dir inside the container holding files to send
OUTBOX_GLOB = "outbox-*"
REORDER_WINDOW = 0.3  # seconds to let shuffled webhooks of back-to-back replies arrive
PHONE_NUMBER = re.compile(r"\+[1-9]\d{6,14}")  # E.164; the agent puts it into a viber:// URI and SQL
SENT_EVENTS_CAP = 2000  # EventIDs of our own sends remembered per container

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


def _is_echo(payload):
    """Messages the owner sent from the phone, recorded back into Chatwoot: they're already in Viber."""
    return bool((payload.get("content_attributes") or {}).get("external_echo"))


class Dispatcher:
    def __init__(self, log):
        self.log = log
        self._seen = set()
        self._lock = threading.Lock()
        self._queues = {}
        self._idle = {}   # container -> Event, cleared while a send runs
        self._sent = {}   # container -> EventIDs our sends produced

    def _dedup(self, message_id, cap=2000):
        with self._lock:
            if message_id in self._seen:
                return False
            self._seen.add(message_id)
            if len(self._seen) > cap:
                self._seen = set(list(self._seen)[-cap:])
            return True

    def handle(self, session, payload):
        """Takes an authenticated webhook payload for `session`. Outgoing messages are queued per
        container: one Viber window means sends run one at a time, in Chatwoot message order."""
        if (payload.get("event") != "message_created" or not _is_outgoing(payload) or payload.get("private")
                or _is_echo(payload)):
            return
        message_id = payload.get("id")
        if message_id is None or not self._dedup(message_id):
            return
        self._queue(session["container"]).put((message_id, session, payload))

    def sent_here(self, container, event_id):
        """Whether an outgoing Viber event came from one of our sends. A send in flight is awaited
        first: its event can reach the log before the agent reports its id."""
        self._idle_event(container).wait()
        return event_id in self._sent.get(container, ())

    def _idle_event(self, container):
        with self._lock:
            idle = self._idle.get(container)
            if idle is None:
                idle = self._idle[container] = threading.Event()
                idle.set()
            return idle

    def _queue(self, container):
        with self._lock:
            q = self._queues.get(container)
            if q is None:
                q = self._queues[container] = queue.PriorityQueue()
                threading.Thread(target=self._drain, args=(container, q), daemon=True,
                                 name=f"out-{container}").start()
            return q

    def _remember_sent(self, container, event_ids):
        with self._lock:
            sent = self._sent.setdefault(container, [])
            sent.extend(event_ids)
            del sent[:-SENT_EVENTS_CAP]

    def _drain(self, container, q):
        """Sends the lowest message id first. Chatwoot delivers webhooks in parallel, so back-to-back
        replies arrive shuffled by milliseconds: wait a short window, then take the lowest id."""
        while True:
            q.put(q.get())  # block until there is work
            time.sleep(REORDER_WINDOW)
            _, session, payload = q.get()
            idle = self._idle_event(container)
            idle.clear()
            try:
                self._deliver(session, payload)
            except Exception as e:  # noqa: BLE001 — one bad message must not stop the queue
                self.log(f"[out] {session['id']} msg {payload.get('id')} error: {e}")
            finally:
                idle.set()

    def _deliver(self, session, payload):
        message_id = payload["id"]
        number = _number_from_payload(payload)
        content = payload.get("content")
        attachments = payload.get("attachments") or []
        if not number or not (content or attachments):
            self.log(f"[out] skip msg {message_id}: {'no number' if not number else 'empty message'}")
            return

        number = "+" + str(number).lstrip("+")
        if not PHONE_NUMBER.fullmatch(number):
            self.log(f"[out] skip msg {message_id}: recipient {number!r} is not a phone number")
            return
        container = session["container"]
        if attachments:
            res = self._send_files(container, number, attachments, content, message_id)
        else:
            res = self._agent(container, ["send", number, content])
        if res.get("ok"):
            self.log(f"[out] {session['id']} {number} delivered ({res.get('elapsed', 0):.2f}s) msg {message_id}")
        else:
            self.log(f"[out] {number} msg {message_id} FAILED: {res.get('reason')}")

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
                    res = json.loads(line)
                except json.JSONDecodeError:
                    break
                self._remember_sent(container, res.get("event_ids", []))
                return res
        return {"ok": False, "reason": f"agent exit {r.returncode}: {(r.stderr or '')[:200]}"}
