#!/usr/bin/env python3
"""Viber message reader.

Watches viber.db-wal via inotify (event-driven, not a busy loop), debounces write bursts,
then reads new Events from viber.db through Viber's own sqlite plugin (viber_dump) and emits
one normalized JSON event per new message/call. Keeps the last processed EventID in the volume
so it never re-emits and survives restarts. A slow safety poll covers any missed inotify signal.

This step just prints events to stdout (container log) so we can verify text + sender number +
media are captured. Sending and the Chatwoot bridge come next.
"""

import glob
import json
import os
import selectors
import shutil
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone

HOME = os.path.expanduser("~")
KEY_FILE = os.environ.get("VIBER_KEY_OUT", "/tmp/viber-key.hex")
HARNESS = "/usr/local/bin/viber_dump"
STATE_DIR = os.path.join(HOME, ".viber-bridge")
STATE_FILE = os.path.join(STATE_DIR, "last_event")

DEBOUNCE = 0.3          # seconds of quiet after a write burst before reading
SAFETY_POLL = 30.0      # seconds; fallback in case an inotify event is missed

EVENT_COLUMNS = (
    "e.EventID, e.TimeStamp, e.Direction, e.Type, e.MessageType, e.MessageStatus, "
    "e.ChatID, e.ContactID, e.Body, e.PayloadPath, e.ThumbnailPath, e.Duration, "
    "e.CallType, e.CallStatus, "
    "c.Name AS ContactName, c.ClientName AS ContactClientName, "
    "c.Number AS ContactNumber, c.MID AS ContactMID"
)


def log(obj):
    print(json.dumps(obj, ensure_ascii=False), flush=True)


def captured_key():
    try:
        blob = open(KEY_FILE, "r", errors="ignore").read()
    except OSError:
        return None
    import re
    m = re.search(r"[0-9a-fA-F]{64}", blob)
    return m.group(0) if m else None


def profile_dir():
    for d in sorted(glob.glob(f"{HOME}/.ViberPC/*/")):
        if os.path.exists(d + "viber.db") and os.path.exists(d + "data.db"):
            return d
    return None


def read_state():
    try:
        return int(open(STATE_FILE).read().strip())
    except (OSError, ValueError):
        return 0


def write_state(last_id):
    os.makedirs(STATE_DIR, exist_ok=True)
    tmp = STATE_FILE + ".tmp"
    with open(tmp, "w") as f:
        f.write(str(last_id))
    os.replace(tmp, STATE_FILE)


def query_new(profile, key, last_id):
    """Snapshot the DB and return new Events (as dicts) with EventID > last_id."""
    tmp = tempfile.mkdtemp(prefix="viberread_")
    try:
        for sfx in ("", "-wal", "-shm"):
            src = profile + "viber.db" + sfx
            if os.path.exists(src):
                shutil.copy2(src, os.path.join(tmp, "viber.db" + sfx))
        db = os.path.join(tmp, "viber.db")
        sql = (
            f"SELECT {EVENT_COLUMNS} FROM Events e "
            # the other party: the sender of an incoming event; for an outgoing one (whose ContactID
            # is self, ContactID 1) the chat's only other member, so group chats get no contact
            f"LEFT JOIN Contact c ON c.ContactID = CASE WHEN e.Direction = 1 THEN "
            f"(SELECT CASE WHEN COUNT(*) = 1 THEN MAX(cr.ContactID) END FROM ChatRelation cr "
            f"WHERE cr.ChatID = e.ChatID AND cr.ContactID <> 1) ELSE e.ContactID END "
            f"WHERE e.EventID > {int(last_id)} ORDER BY e.EventID"
        )
        env = dict(os.environ)
        env.update({
            "VIBER_DB": db,
            "VIBER_HEXKEY": key,
            "VIBER_JSON": "1",
            "LD_LIBRARY_PATH": "/opt/viber/lib",
            "QT_PLUGIN_PATH": "/opt/viber/plugins",
            "QT_QPA_PLATFORM": "offscreen",
        })
        r = subprocess.run([HARNESS, sql], env=env, capture_output=True, text=True, timeout=60)
        rows = []
        for line in r.stdout.splitlines():
            line = line.strip()
            if line.startswith("{"):
                try:
                    rows.append(json.loads(line))
                except json.JSONDecodeError:
                    pass
        if r.returncode != 0 and not rows:
            log({"type": "reader_error", "stderr": r.stderr.strip()[:300]})
        return rows
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def media_entry(path):
    """ThumbnailPath/PayloadPath are stored as absolute paths to the real files.
    The image file can land a moment after the DB row, so report `exists` too — the
    bridge waits briefly for it before forwarding.
    """
    if not path:
        return None
    return {"path": path, "exists": os.path.exists(path)}


def normalize(row):
    ts = int(row.get("TimeStamp") or 0)
    iso = datetime.fromtimestamp(ts / 1000, tz=timezone.utc).isoformat() if ts else None
    mtype = int(row.get("MessageType") or -1)
    body = row.get("Body") or ""
    thumb = media_entry(row.get("ThumbnailPath"))
    payload = media_entry(row.get("PayloadPath"))
    evt = {
        "type": "message",
        "event_id": int(row.get("EventID") or 0),
        "ts": iso,
        "direction": "out" if str(row.get("Direction")) == "1" else "in",
        "message_type": mtype,
        "status": row.get("MessageStatus"),
        "chat_id": row.get("ChatID"),
        "contact_id": row.get("ContactID"),
        "contact": {
            # The other party (sender of incoming, recipient of outgoing). Number is present for
            # saved/number-sharing users; bots & some unsaved users have only a stable MID. Name
            # falls back to the Viber-side ClientName.
            "name": row.get("ContactName") or row.get("ContactClientName"),
            "number": row.get("ContactNumber") or None,
            "mid": row.get("ContactMID") or None,
        },
        "text": body if mtype in (1, 2, 15) else None,
        "body_raw": body,
    }
    if thumb or payload:
        evt["media"] = {"thumbnail": thumb, "payload": payload}
    if row.get("CallType") or row.get("CallStatus"):
        evt["call"] = {"type": row.get("CallType"), "status": row.get("CallStatus"),
                       "duration": row.get("Duration")}
    return evt


def process(profile, key):
    last = read_state()
    rows = query_new(profile, key, last)
    new_last = last
    for row in rows:
        log(normalize(row))
        new_last = max(new_last, int(row.get("EventID") or 0))
    if new_last != last:
        write_state(new_last)


def main():
    # Viber writes the key at startup via the memcpy hook; wait for it rather than crash-looping
    key = captured_key()
    profile = profile_dir()
    while not key or not profile:
        time.sleep(2)
        key = captured_key()
        profile = profile_dir()

    log({"type": "reader_start", "profile": profile, "last_event": read_state()})
    process(profile, key)  # initial catch-up

    proc = subprocess.Popen(
        ["inotifywait", "-m", "-q", "-e", "modify,create,close_write", "--format", "%f", profile],
        stdout=subprocess.PIPE, text=True,
    )
    sel = selectors.DefaultSelector()
    sel.register(proc.stdout, selectors.EVENT_READ)

    pending = False
    last_change = 0.0
    last_safety = time.time()
    try:
        while True:
            for _key, _mask in sel.select(timeout=1.0):
                line = proc.stdout.readline()
                if not line:
                    continue
                if "viber.db" in line:
                    pending = True
                    last_change = time.time()
            now = time.time()
            if pending and now - last_change >= DEBOUNCE:
                process(profile, key)
                pending = False
            if now - last_safety >= SAFETY_POLL:
                process(profile, key)
                last_safety = now
    finally:
        proc.terminate()


if __name__ == "__main__":
    sys.exit(main())
