#!/usr/bin/env python3
"""Per-session inbound worker: Viber -> Chatwoot.

Tails the session container's log (where the in-container reader prints normalized JSON events),
and posts each new 1:1 message (text + media) to Chatwoot's Viber endpoint with the other party's
number: incoming ones, and outgoing ones the owner sent from the phone (recorded as echoes). The ones
the bridge itself sent from Chatwoot are skipped. Media files are copied out with `docker cp`.
Dedup is by Viber EventID persisted in the session's registry row.
"""

import json
import mimetypes
import os
import tempfile
import threading
import time

from bridge.chatwoot import ViberInbox

from . import docker_ops


def _mime(path):
    return mimetypes.guess_type(path)[0] or "application/octet-stream"


# Viber wraps display names in Unicode bidi isolates (U+2066..U+2069); they'd end up invisible
# inside Chatwoot contact names and break search/matching.
_BIDI_ISOLATES = dict.fromkeys(map(ord, "⁦⁧⁨⁩"))


def _clean_name(name):
    return (name or "").translate(_BIDI_ISOLATES).strip() or None


def _shared_contact_text(card):
    """A shared Viber contact, as text: name and number on separate lines."""
    if not card:
        return None
    return "\n".join(v for v in (_clean_name(card.get("name")), card.get("number")) if v) or None


class InboundWorker(threading.Thread):
    def __init__(self, session_id, registry, dispatcher, api_key, log):
        super().__init__(daemon=True, name=f"inbound-{session_id}")
        self.sid = session_id
        self.reg = registry
        self.dispatcher = dispatcher
        self.api_key = api_key
        self.log = log
        self._stop = threading.Event()
        self._cw = None

    def stop(self):
        self._stop.set()

    def _row(self):
        return self.reg.get(self.sid)

    def _chatwoot(self, row):
        if self._cw is None:
            self._cw = ViberInbox(row["chatwoot_url"], row["inbox_identifier"], self.api_key)
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

    def _handle(self, row, evt):
        if evt.get("type") != "message":
            return
        event_id = int(evt.get("event_id") or 0)
        if event_id <= int(row.get("last_event") or 0):
            return
        outgoing = evt.get("direction") == "out"
        if outgoing and self.dispatcher.sent_here(row["container"], event_id):
            self.reg.update(self.sid, last_event=event_id)  # a reply from Chatwoot, already there
            return

        contact = evt.get("contact") or {}
        if not contact.get("number"):
            # group/community/bot messages carry no contact number; the bridge maps 1:1 chats only
            self.log(f"[in] {self.sid} event {event_id} skipped: no contact number (not a 1:1 chat)")
            self.reg.update(self.sid, last_event=event_id)
            return
        number = "+" + str(contact["number"]).lstrip("+")
        name = _clean_name(contact.get("name"))
        content = evt.get("text") or _shared_contact_text(evt.get("shared_contact"))
        attachments = self._attachments(row["container"], evt)
        # a real message (not a system event, type 0) we can't render, e.g. a sticker or media that
        # Viber didn't download: recorded as unsupported, like Instagram does, rather than dropped
        unsupported = not (content or attachments) and evt.get("message_type", 0) != 0

        if content or attachments or unsupported:
            self._chatwoot(row).post_message(number, outgoing, event_id, name=name, content=content,
                                             attachments=attachments, unsupported=unsupported)
            self.log(f"[in] {self.sid} event {event_id} {number}"
                     f"{' (sent from phone)' if outgoing else ''}{' +media' if attachments else ''}"
                     f"{' (unsupported type ' + str(evt.get('message_type')) + ')' if unsupported else ''}")
        self.reg.update(self.sid, last_event=event_id)

    # ─── log tail loop ────────────────────────────────────────────────────────
    def run(self):
        while not self._stop.is_set():
            row = self._row()
            if not row:
                return
            # Replay the whole log on every (re)attach: the EventID cursor drops what was already
            # posted, and nothing received while the manager was down is lost.
            proc = docker_ops.logs_follow(row["container"], tail="all")
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
