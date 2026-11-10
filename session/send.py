#!/usr/bin/env python3
"""TEST HARNESS — send a text message on Viber by driving the desktop UI.

TEMPORARY: a manual CLI to prove outgoing send works end to end. NOT wired into supervisord.
Once the Chatwoot bridge lands, sending is driven by the agent's HTTP API and this file +
its COPY line get deleted. (No programmatic send exists for a personal Viber account — driving
the client UI is the only option, same as commercial services do.)

Chat selection uses Viber's own `viber://chat?number=<E164>` deep link (verified working for
personal accounts on Linux), forwarded to the running instance. Instead of a guessed delay, it
waits event-driven over AT-SPI: it snapshots the open conversation's visible messages, fires the
deep link, then proceeds the instant the middle-column messages change (the target chat rendered).
A short grace covers the case where the target chat was already open.

Usage: send.py <+E164number> <text>
Needs DISPLAY=:99 and the shared DBUS_SESSION_BUS_ADDRESS (for the AT-SPI tree).
Confirmation: the reader emits the resulting outgoing Event (Direction=1).
"""

import subprocess
import sys
import time

import pyatspi

DISPLAY = ":99"
VIBER = "/opt/viber/Viber"
WINDOW_NAME = "Rakuten Viber"
WIN_W, WIN_H = 1280, 740

COMPOSER_XY = (500, 713)
# middle conversation column (excludes left chat list and the docked right ChatGPT panel)
MID_X_MIN, MID_X_MAX = 310, 880
MID_Y_MIN, MID_Y_MAX = 100, 690

READY_TIMEOUT = 6.0     # hard cap waiting for the chat to render
GRACE = 1.2             # if messages don't change by now, assume the target chat was already open
POLL = 0.05


def xdo(*args):
    subprocess.run(["xdotool", *args], env={"DISPLAY": DISPLAY})


def win_id():
    r = subprocess.run(["xdotool", "search", "--name", WINDOW_NAME],
                       env={"DISPLAY": DISPLAY}, capture_output=True, text=True)
    ids = r.stdout.split()
    return ids[-1] if ids else None


def viber_app():
    desktop = pyatspi.Registry.getDesktop(0)
    for i in range(desktop.childCount):
        try:
            app = desktop.getChildAtIndex(i)
        except Exception:
            continue
        if app and "iber" in (app.name or "").lower():
            return app
    return None


def middle_messages():
    """Visible message texts in the middle conversation column, as a tuple (order-stable)."""
    app = viber_app()
    if not app:
        return ()
    out = []

    def walk(node):
        try:
            if node.getRoleName() == "text":
                t = node.queryText()
                s = t.getText(0, min(t.characterCount, 200))
                if s and s.strip():
                    ex = node.queryComponent().getExtents(pyatspi.DESKTOP_COORDS)
                    if MID_X_MIN <= ex.x <= MID_X_MAX and MID_Y_MIN <= ex.y <= MID_Y_MAX:
                        out.append(s)
        except Exception:
            pass
        try:
            for i in range(node.childCount):
                walk(node.getChildAtIndex(i))
        except Exception:
            pass

    walk(app)
    return tuple(out)


def wait_for_switch(baseline):
    """Return when the middle messages change and settle, or on grace/timeout."""
    deadline = time.time() + READY_TIMEOUT
    grace_until = time.time() + GRACE
    last = None
    while time.time() < deadline:
        cur = middle_messages()
        if cur != baseline and cur == last and cur:
            return  # changed and stable → target chat rendered
        if time.time() > grace_until and cur == baseline:
            return  # unchanged past grace → target chat was already open
        last = cur
        time.sleep(POLL)


def main():
    if len(sys.argv) < 3:
        print("usage: send.py <+E164number> <text>", file=sys.stderr)
        return 2
    number, text = sys.argv[1], sys.argv[2]

    t0 = time.time()
    wid = win_id()
    if not wid:
        print("viber window not found", file=sys.stderr)
        return 3

    subprocess.run(["xclip", "-selection", "clipboard"], input=text.encode("utf-8"),
                   env={"DISPLAY": DISPLAY}, check=True)
    xdo("windowsize", wid, str(WIN_W), str(WIN_H), "windowmove", wid, "0", "0",
        "windowactivate", "--sync", wid)

    baseline = middle_messages()
    subprocess.Popen([VIBER, f"viber://chat?number={number}"], env={"DISPLAY": DISPLAY},
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    wait_for_switch(baseline)

    xdo("mousemove", str(COMPOSER_XY[0]), str(COMPOSER_XY[1]), "click", "1",
        "key", "--clearmodifiers", "ctrl+a", "key", "--clearmodifiers", "ctrl+v",
        "key", "Return")

    print(f"sent to {number} in {time.time() - t0:.2f}s")
    return 0


if __name__ == "__main__":
    sys.exit(main())
