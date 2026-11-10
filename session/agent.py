#!/usr/bin/env python3
"""Viber send agent — stateful, safety-layered outgoing send over the desktop UI.

Guarantees, in order of strength:
  1. Addressing: viber://chat?number=<E164> opens the chat by number (Viber's own routing).
  2. Memory: the agent remembers the last CONFIRMED-open chat, so a reply to the same chat sends
     directly with no deep link and no wait.
  3. Render wait: for a new chat, after the deep link it waits on an AT-SPI children-changed event
     (push, not a guessed delay); no event within the timeout == the switch didn't happen -> abort.
  4. Extra text check: the target chat's last message (from the decrypted DB) must be visible in
     the open conversation (belt-and-suspenders; text alone isn't identity, so it only gates when
     the DB last message is non-empty text).
  5. Ground truth: after sending, the newest outgoing Event's ChatID->number (from the DB) must
     equal the target. Only then is the transition confirmed and remembered; a mismatch is a
     misroute -> not delivered, alert, memory left untouched so the next send re-deep-links.

State (confirmed number of the open chat) persists to a file so it survives across CLI calls and
process restarts. In the Chatwoot bridge this is just the agent's in-memory state.

CLI: agent.py send <+E164number> <text>
     agent.py send-file <+E164number> <path> [caption]
Needs DISPLAY=:99 and the shared DBUS_SESSION_BUS_ADDRESS.
"""

import glob
import json
import mimetypes
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import urllib.parse

import pyatspi

DISPLAY = ":99"
VIBER = "/opt/viber/Viber"
WINDOW_NAME = "Rakuten Viber"
HOME = os.path.expanduser("~")
KEY_FILE = os.environ.get("VIBER_KEY_OUT", "/tmp/viber-key.hex")
HARNESS = "/usr/local/bin/viber_dump"
STATE_FILE = os.path.join(HOME, ".viber-bridge", "open_chat")

RENDER_TIMEOUT = 2.5      # cap waiting for the switch's render event (a real switch fires ~0.35s)
POST_SETTLE = 0.05
MEDIA_PREVIEW_SETTLE = 1.0  # pasted image -> media preview with a focused caption field
PREVIEW_IMAGE_MIMES = {"image/png", "image/jpeg"}  # pasted as image data (anything else: as a file)


# ─── low-level X / clipboard ────────────────────────────────────────────────
def xdo(*args):
    subprocess.run(["xdotool", *args], env={"DISPLAY": DISPLAY})


def set_clipboard(text):
    subprocess.run(["xclip", "-selection", "clipboard"], input=text.encode("utf-8"),
                   env={"DISPLAY": DISPLAY}, check=True)


def activate_window():
    r = subprocess.run(["xdotool", "search", "--name", WINDOW_NAME],
                       env={"DISPLAY": DISPLAY}, capture_output=True, text=True)
    wids = r.stdout.split()
    if not wids:
        return False
    xdo("windowactivate", wids[-1])
    return True


# ─── decrypted-DB access (via Viber's own sqlite plugin) ────────────────────
def _key():
    try:
        m = re.search(r"[0-9a-fA-F]{64}", open(KEY_FILE, "r", errors="ignore").read())
        return m.group(0) if m else None
    except OSError:
        return None


def _profile():
    for d in sorted(glob.glob(f"{HOME}/.ViberPC/*/")):
        if os.path.exists(d + "viber.db") and os.path.exists(d + "data.db"):
            return d
    return None


def db_query(sql):
    """Run one SELECT against a snapshot of viber.db; return a list of dict rows."""
    key, profile = _key(), _profile()
    if not key or not profile:
        return []
    tmp = tempfile.mkdtemp(prefix="agentdb_")
    try:
        for sfx in ("", "-wal", "-shm"):
            src = profile + "viber.db" + sfx
            if os.path.exists(src):
                shutil.copy2(src, os.path.join(tmp, "viber.db" + sfx))
        env = dict(os.environ)
        env.update({"VIBER_DB": os.path.join(tmp, "viber.db"), "VIBER_HEXKEY": key,
                    "VIBER_JSON": "1", "LD_LIBRARY_PATH": "/opt/viber/lib",
                    "QT_PLUGIN_PATH": "/opt/viber/plugins", "QT_QPA_PLATFORM": "offscreen"})
        r = subprocess.run([HARNESS, sql], env=env, capture_output=True, text=True, timeout=60)
        rows = []
        for line in r.stdout.splitlines():
            line = line.strip()
            if line.startswith("{"):
                try:
                    rows.append(json.loads(line))
                except json.JSONDecodeError:
                    pass
        return rows
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def target_last_texts(number, limit=5):
    """Recent non-empty message texts of the 1:1 chat for a number (for the extra check)."""
    num = number.lstrip("+")
    rows = db_query(
        "SELECT e.Body FROM Events e "
        "JOIN ChatRelation cr ON cr.ChatID = e.ChatID "
        "JOIN Contact c ON c.ContactID = cr.ContactID "
        f"WHERE replace(c.Number,'+','') = '{num}' AND e.Body IS NOT NULL AND e.Body <> '' "
        f"ORDER BY e.EventID DESC LIMIT {int(limit)}")
    return [r.get("Body", "") for r in rows if r.get("Body")]


def last_outgoing_event_id():
    """EventID of the newest outgoing message (0 if none) — the baseline taken before a send."""
    rows = db_query("SELECT MAX(EventID) AS id FROM Events WHERE Direction = 1")
    return int(rows[0].get("id") or 0) if rows else 0


def recipient_of_last_outgoing(after_event_id):
    """The OTHER party (recipient) of the newest outgoing message NEWER than `after_event_id`, by
    number. Only a new event proves this send happened: an older outgoing message to the same chat
    must not count (a swallowed Enter would otherwise be reported as delivered)."""
    rows = db_query(
        "SELECT e.EventID, e.ChatID FROM Events e WHERE e.Direction = 1 "
        f"AND e.EventID > {int(after_event_id)} ORDER BY e.EventID DESC LIMIT 1")
    if not rows:
        return None
    chat_id = rows[0].get("ChatID")
    # self is ContactID 1 in this profile; the recipient is the chat's non-self member
    members = db_query(
        "SELECT c.Number AS number FROM ChatRelation cr "
        "JOIN Contact c ON c.ContactID = cr.ContactID "
        f"WHERE cr.ChatID = {int(chat_id)} AND c.Number IS NOT NULL AND c.Number <> '' "
        "AND cr.ContactID <> 1")
    return members[0]["number"] if members else None


# ─── AT-SPI UI access ───────────────────────────────────────────────────────
def viber_app():
    d = pyatspi.Registry.getDesktop(0)
    for i in range(d.childCount):
        try:
            a = d.getChildAtIndex(i)
        except Exception:
            continue
        if a and "iber" in (a.name or "").lower():
            return a
    return None


def visible_messages_and_composer(app=None):
    app = app or viber_app()
    if not app:
        return (), None
    msgs, composer, composer_focused = [], None, None

    def walk(node):
        nonlocal composer, composer_focused
        try:
            if node.getRoleName() == "text":
                t = node.queryText()
                n = t.characterCount
                if n > 0:
                    s = t.getText(0, min(n, 300))
                    if s and s.strip():
                        msgs.append(s)
                elif node.getState().contains(pyatspi.STATE_EDITABLE):
                    composer = node
                    if node.getState().contains(pyatspi.STATE_FOCUSED):
                        composer_focused = node
        except Exception:
            pass
        try:
            for i in range(node.childCount):
                walk(node.getChildAtIndex(i))
        except Exception:
            pass

    walk(app)
    return tuple(msgs), (composer_focused or composer)


def wait_render_event(timeout=RENDER_TIMEOUT):
    """Block until Viber fires a children-changed event (the conversation repopulated on switch),
    or timeout. Push-based (AT-SPI event), not polling/text-compare."""
    from gi.repository import GLib
    fired = {"v": False}

    def on_event(e):
        try:
            app = e.host_application
            if app and "iber" in (app.name or "").lower():
                fired["v"] = True
                pyatspi.Registry.stop()
        except Exception:
            pass

    pyatspi.Registry.registerEventListener(on_event, "object:children-changed")
    tid = GLib.timeout_add(int(timeout * 1000),
                           lambda: (pyatspi.Registry.stop(), False)[1])
    try:
        pyatspi.Registry.start()
    finally:
        GLib.source_remove(tid) if fired["v"] else None
        try:
            pyatspi.Registry.deregisterEventListener(on_event, "object:children-changed")
        except Exception:
            pass
    return fired["v"]


# ─── state ──────────────────────────────────────────────────────────────────
def load_open_chat():
    try:
        return open(STATE_FILE).read().strip() or None
    except OSError:
        return None


def save_open_chat(number):
    os.makedirs(os.path.dirname(STATE_FILE), exist_ok=True)
    tmp = STATE_FILE + ".tmp"
    with open(tmp, "w") as f:
        f.write(number or "")
    os.replace(tmp, STATE_FILE)


# ─── send ───────────────────────────────────────────────────────────────────
def norm(n):
    return "+" + n.lstrip("+")


def last_text_visible(number, msgs):
    """Extra check: is the target chat's last DB message currently on screen?
    Returns True/False, or None when the DB last message is empty/media (check skipped)."""
    texts = target_last_texts(number, limit=3)
    texts = [t for t in texts if t and t.strip()]
    if not texts:
        return None  # nothing text-based to compare against
    joined = "\u0001".join(msgs)
    return any(t in joined for t in texts)


def focus(composer):
    if composer is not None:
        try:
            composer.queryComponent().grabFocus()
            time.sleep(POST_SETTLE)
        except Exception:
            pass


def do_paste_send(composer):
    focus(composer)
    xdo("key", "--clearmodifiers", "ctrl+a", "key", "--clearmodifiers", "ctrl+v", "key", "Return")


def open_chat(number):
    """Bring the target's chat to the front. Returns (composer, None) or (None, failure_reason)."""
    same_chat = (load_open_chat() == number)

    if same_chat:
        msgs, composer = visible_messages_and_composer()
        # extra text check for the already-open case
        chk = last_text_visible(number, msgs)
        if chk is False:
            # on-screen chat doesn't show the target's last message -> don't trust memory, re-open
            same_chat = False

    if not same_chat:
        msgs, composer = visible_messages_and_composer()
        if last_text_visible(number, msgs) is True:
            # target already open though memory didn't know it -> send directly, no deep link/wait
            pass
        else:
            # genuine switch: deep link + wait for the render event (push, not a guessed delay)
            subprocess.Popen([VIBER, f"viber://chat?number={number}"], env={"DISPLAY": DISPLAY},
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            switched = wait_render_event()
            msgs, composer = visible_messages_and_composer()
            chk = last_text_visible(number, msgs)
            if not switched:
                # no render event: already-open (no-op) only acceptable if its last msg shows now
                if chk is not True:
                    return None, "no_switch"
            elif chk is False:
                return None, "wrong_chat_text"
    else:
        msgs, composer = visible_messages_and_composer()
    return composer, None


def confirm(number, baseline, t0):
    """Ground-truth post-send verification: a NEW outgoing event addressed to the target."""
    recipient = None
    for _ in range(50):
        recipient = recipient_of_last_outgoing(baseline)
        if recipient:
            break
        time.sleep(0.1)
    confirmed = recipient is not None and norm(recipient) == number
    if confirmed:
        save_open_chat(number)
        return {"ok": True, "reason": "delivered", "elapsed": time.time() - t0,
                "confirmed_recipient": recipient}
    # misroute (or couldn't verify): do NOT trust the open-chat memory
    save_open_chat("")
    return {"ok": False, "reason": "verify_mismatch", "elapsed": time.time() - t0,
            "db_recipient": recipient, "target": number}


def send(number, text):
    """Return a result dict: {ok, reason, elapsed, confirmed}."""
    number = norm(number)
    t0 = time.time()
    if not activate_window():
        return {"ok": False, "reason": "no_window"}
    composer, err = open_chat(number)
    if err:
        return {"ok": False, "reason": err, "elapsed": time.time() - t0}
    set_clipboard(text)
    baseline = last_outgoing_event_id()
    do_paste_send(composer)
    return confirm(number, baseline, t0)


def send_file(number, path, caption=""):
    """Send a local file. Viber takes both from the clipboard, but differently:
      - png/jpeg as image data -> opens the media preview with its caption field focused; the
        caption is pasted there and Return sends photo + caption as one message;
      - anything else as a file:// uri-list -> sent immediately as a file, no preview, so a caption
        follows as its own text message."""
    number = norm(number)
    t0 = time.time()
    if not activate_window():
        return {"ok": False, "reason": "no_window"}
    composer, err = open_chat(number)
    if err:
        return {"ok": False, "reason": err, "elapsed": time.time() - t0}
    mime = mimetypes.guess_type(path)[0] or ""
    baseline = last_outgoing_event_id()
    focus(composer)
    if mime in PREVIEW_IMAGE_MIMES:
        subprocess.run(["xclip", "-selection", "clipboard", "-t", mime, "-i", path],
                       env={"DISPLAY": DISPLAY}, check=True)
        xdo("key", "--clearmodifiers", "ctrl+v")
        time.sleep(MEDIA_PREVIEW_SETTLE)
        if caption:
            set_clipboard(caption)
            xdo("key", "--clearmodifiers", "ctrl+v")
        xdo("key", "Return")
        return confirm(number, baseline, t0)
    uri = "file://" + urllib.parse.quote(os.path.abspath(path)) + "\r\n"
    subprocess.run(["xclip", "-selection", "clipboard", "-t", "text/uri-list"],
                   input=uri.encode(), env={"DISPLAY": DISPLAY}, check=True)
    xdo("key", "--clearmodifiers", "ctrl+v")
    res = confirm(number, baseline, t0)
    if res.get("ok") and caption:
        cap = send(number, caption)
        if not cap.get("ok"):
            return {**cap, "reason": f"file sent, caption failed: {cap.get('reason')}"}
    return res


def main():
    args = sys.argv[1:]
    if len(args) >= 3 and args[0] == "send":
        res = send(args[1], args[2])
    elif len(args) >= 3 and args[0] == "send-file":
        res = send_file(args[1], args[2], args[3] if len(args) >= 4 else "")
    else:
        print("usage: agent.py send <+E164number> <text>\n"
              "       agent.py send-file <+E164number> <path> [caption]", file=sys.stderr)
        return 2
    print(json.dumps(res, ensure_ascii=False))
    return 0 if res.get("ok") else 1


if __name__ == "__main__":
    sys.exit(main())
