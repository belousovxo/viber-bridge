#!/usr/bin/env python3
"""Per-session inbound worker: Viber -> Chatwoot.

Tails the session container's log (where the in-container reader prints normalized JSON events),
and for each new incoming message resolves the contact + conversation and posts it (text + media)
via the Chatwoot Public Inbox API (keyed by the session's inbox_identifier; source_id = the
sender's number, so threads never duplicate). Media files are copied out with `docker cp`.
Dedup is by Viber EventID persisted in the session's registry row; the per-sender conversation id
is cached there too.
"""

import json
import mimetypes
import os
import tempfile
import threading
import time

from bridge.chatwoot import PublicInbox

from . import docker_ops


def _mime(path):
    return mimetypes.guess_type(path)[0] or "application/octet-stream"


class InboundWorker(threading.Thread):
    def __init__(self, session_id, registry, log):
        super().__init__(daemon=True, name=f"inbound-{session_id}")
        self.sid = session_id
        self.reg = registry
        self.log = log
        self._stop = threading.Event()
        self._cw = None

    def stop(self):
        self._stop.set()

    def _row(self):
        return self.reg.get(self.sid)

    def _chatwoot(self, row):
        if self._cw is None:
            self._cw = PublicInbox(row["chatwoot_url"], row["inbox_identifier"])
        return self._cw

    # ─── media ───────────────────────────────────────────────────────────────
    def _copy_media(self, container, container_path):
        fd, tmp = tempfile.mkstemp(prefix="vbmedia_")
        os.close(fd)
        try:
            if not docker_ops.cp_out(container, container_path, tmp) or not os.path.getsize(tmp):
                return None
            with open(tmp, "rb") as f:
                return (os.path.basename(container_path), f.read(), _mime(container_path))
        finally:
            os.unlink(tmp)

    def _attachments(self, container, evt):
        media = evt.get("media") or {}
        for key in ("payload", "thumbnail"):
            entry = media.get(key)
            if entry and entry.get("exists") and entry.get("path"):
                got = self._copy_media(container, entry["path"])
                if got:
                    return [got]
        return []

    # ─── conversation resolution (per-sender, cached in the registry row) ──────
    def _conversation_id(self, row, number, name):
        contacts = dict(row.get("contacts") or {})
        if contacts.get(number):
            return contacts[number]
        cw = self._chatwoot(row)
        cw.ensure_contact(number, name)          # source_id = number (idempotent)
        conv = cw.create_conversation(number)
        contacts[number] = conv
        self.reg.update(self.sid, contacts=contacts)
        return conv

    def _handle(self, row, evt):
        if evt.get("type") != "message" or evt.get("direction") != "in":
            return
        event_id = int(evt.get("event_id") or 0)
        if event_id <= int(row.get("last_event") or 0):
            return

        contact = evt.get("contact") or {}
        number = "+" + str(contact.get("number") or row.get("number") or "").lstrip("+")
        name = contact.get("name")
        content = evt.get("text") or None
        attachments = self._attachments(row["container"], evt)

        if content or attachments:
            conv = self._conversation_id(row, number, name)
            self._chatwoot(row).create_incoming(number, conv, content=content, attachments=attachments)
            self.log(f"[in] {self.sid} event {event_id} -> conv {conv}"
                     f"{' +media' if attachments else ''}")
        self.reg.update(self.sid, last_event=event_id)

    # ─── log tail loop ────────────────────────────────────────────────────────
    def run(self):
        while not self._stop.is_set():
            row = self._row()
            if not row:
                return
            proc = docker_ops.logs_follow(row["container"])
            try:
                for line in proc.stdout:
                    if self._stop.is_set():
                        break
                    line = line.strip()
                    if not line.startswith("{"):
                        continue
                    try:
                        evt = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    try:
                        self._handle(self._row(), evt)
                    except Exception as e:  # noqa: BLE001 - keep the tail alive
                        self.log(f"[in] {self.sid} error: {e}")
            finally:
                proc.terminate()
            if not self._stop.is_set():
                time.sleep(2)  # container restarted / stream ended -> reattach
