#!/usr/bin/env python3
"""TEST HARNESS — send a text message on Viber by driving the desktop UI, coordinate-free.

TEMPORARY: a manual CLI to prove outgoing send works end to end. NOT wired into supervisord.
Once the Chatwoot bridge lands, sending is driven by the agent's HTTP API and this file +
its COPY line get deleted. (No programmatic send exists for a personal Viber account — driving
the client UI is the only option.)

Flow — no mouse, no fixed coordinates, so the window size/resolution is irrelevant:
  1. snapshot the open conversation's message texts over AT-SPI (chat list isn't exposed, only
     the open chat's bubbles are), fire viber://chat?number=<E164>, wait until those messages
     change (target chat rendered) — event-driven, no guessed delay;
  2. find the composer (the empty editable text field, preferring the focused one) and grabFocus
     it over AT-SPI;
  3. paste from clipboard + Enter.
Only a window-activate (so keystrokes land in Viber) uses xdotool; everything else is AT-SPI.

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

READY_TIMEOUT = 6.0
GRACE = 1.2
POLL = 0.05


def xdo(*args):
    subprocess.run(["xdotool", *args], env={"DISPLAY": DISPLAY})


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


def snapshot():
    """Return (message_texts, composer_node).

    Viber marks both message bubbles and input fields as editable 'text' nodes. Non-empty ones
    are conversation messages (the chat list is not exposed); empty editable ones are the search
    box / composer. The composer is the focused empty field, else the last one in tree order
    (the conversation composer comes after the sidebar search). All resolution-independent."""
    app = viber_app()
    if not app:
        return (), None
    msgs = []
    composer = None
    composer_focused = None

    def walk(node):
        nonlocal composer, composer_focused
        try:
            if node.getRoleName() == "text":
                t = node.queryText()
                n = t.characterCount
                if n > 0:
                    s = t.getText(0, min(n, 200))
                    if s and s.strip():
                        msgs.append(s)
                else:
                    st = node.getState()
                    if st.contains(pyatspi.STATE_EDITABLE):
                        composer = node
                        if st.contains(pyatspi.STATE_FOCUSED):
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


def wait_for_switch(baseline):
    deadline = time.time() + READY_TIMEOUT
    grace_until = time.time() + GRACE
    last = None
    while time.time() < deadline:
        cur, _ = snapshot()
        if cur != baseline and cur == last and cur:
            return
        if time.time() > grace_until and cur == baseline:
            return
        last = cur
        time.sleep(POLL)


def main():
    if len(sys.argv) < 3:
        print("usage: send.py <+E164number> <text>", file=sys.stderr)
        return 2
    number, text = sys.argv[1], sys.argv[2]

    t0 = time.time()
    # make Viber the active X window so keystrokes land in it (single window in our Xvfb anyway)
    r = subprocess.run(["xdotool", "search", "--name", WINDOW_NAME],
                       env={"DISPLAY": DISPLAY}, capture_output=True, text=True)
    wids = r.stdout.split()
    if not wids:
        print("viber window not found", file=sys.stderr)
        return 3
    xdo("windowactivate", wids[-1])

    subprocess.run(["xclip", "-selection", "clipboard"], input=text.encode("utf-8"),
                   env={"DISPLAY": DISPLAY}, check=True)

    baseline, _ = snapshot()
    subprocess.Popen([VIBER, f"viber://chat?number={number}"], env={"DISPLAY": DISPLAY},
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    wait_for_switch(baseline)

    _, composer = snapshot()
    if composer is not None:
        try:
            composer.queryComponent().grabFocus()
            time.sleep(0.05)
        except Exception:
            pass

    xdo("key", "--clearmodifiers", "ctrl+a", "key", "--clearmodifiers", "ctrl+v", "key", "Return")
    print(f"sent to {number} in {time.time() - t0:.2f}s")
    return 0


if __name__ == "__main__":
    sys.exit(main())
