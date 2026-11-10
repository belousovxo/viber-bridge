#!/usr/bin/env python3
"""TEST HARNESS — send a text message on Viber by driving the desktop UI.

TEMPORARY: a manual CLI to prove outgoing send works end to end. NOT wired into supervisord.
Once the Chatwoot bridge lands, sending is driven by the agent's HTTP API and this file +
its COPY line get deleted. (No programmatic send exists for a personal Viber account — driving
the client UI is the only option, same as commercial services do.)

Viber does not expose its chat list / search results to accessibility (AT-SPI can't select or
even detect a chat row), so the whole flow is fixed-coordinate clicks on a pinned-size window:
  maximize -> click search, type number -> wait render -> click first result -> wait render ->
  click composer, paste, Enter.
The only tunable cost is the two render waits; they're constants below. Lower = faster but риск of
clicking before Viber has drawn. Re-measure coordinates if the pinned Viber build changes.

Usage: send.py <+E164number> <text>   (needs DISPLAY=:99)
Confirmation: the reader emits the resulting outgoing Event (Direction=1).
"""

import subprocess
import sys
import time

DISPLAY = ":99"
WINDOW_NAME = "Rakuten Viber"
WIN_W, WIN_H = 1280, 740

SEARCH_XY = (160, 26)
FIRST_RESULT_XY = (150, 180)
COMPOSER_XY = (500, 713)

T_SEARCH = 0.35     # wait for search results to filter
T_OPEN = 0.40       # wait for the conversation to load


def xdo(*args):
    subprocess.run(["xdotool", *args], env={"DISPLAY": DISPLAY})


def win_id():
    r = subprocess.run(["xdotool", "search", "--name", WINDOW_NAME],
                       env={"DISPLAY": DISPLAY}, capture_output=True, text=True)
    ids = r.stdout.split()
    return ids[-1] if ids else None


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

    # clipboard set up front (cheap), so sending is just paste + Enter
    subprocess.run(["xclip", "-selection", "clipboard"], input=text.encode("utf-8"),
                   env={"DISPLAY": DISPLAY}, check=True)

    # xdotool commands are batched per segment (one process instead of many) to cut spawn overhead.
    # pin the window to a known geometry so the fixed coordinates line up
    xdo("windowsize", wid, str(WIN_W), str(WIN_H), "windowmove", wid, "0", "0",
        "windowactivate", "--sync", wid)

    # 1) search for the number (type must be last in the chain — it consumes the trailing args)
    xdo("mousemove", str(SEARCH_XY[0]), str(SEARCH_XY[1]), "click", "1",
        "key", "--clearmodifiers", "ctrl+a", "key", "--clearmodifiers", "Delete",
        "type", "--clearmodifiers", number)
    time.sleep(T_SEARCH)

    # 2) open the single best-match result
    xdo("mousemove", str(FIRST_RESULT_XY[0]), str(FIRST_RESULT_XY[1]), "click", "1")
    time.sleep(T_OPEN)

    # 3) focus the composer, paste and send
    xdo("mousemove", str(COMPOSER_XY[0]), str(COMPOSER_XY[1]), "click", "1",
        "key", "--clearmodifiers", "ctrl+a", "key", "--clearmodifiers", "ctrl+v",
        "key", "Return")

    print(f"sent to {number} in {time.time() - t0:.2f}s")
    return 0


if __name__ == "__main__":
    sys.exit(main())
