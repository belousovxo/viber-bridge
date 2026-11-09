#!/usr/bin/env python3
"""Read-only: open viber.db through Viber's own sqlite plugin with the captured key.

Copies the DB, runs the viber_dump harness (linked at runtime against Viber's Qt libs and
sqlite plugin), and prints the table list + row counts of the tables that matter for the
bridge. Never prints the key.
"""

import glob
import os
import re
import shutil
import subprocess
import sys
import tempfile

HOME = os.path.expanduser("~")
KEY_FILE = os.environ.get("VIBER_KEY_OUT", "/tmp/viber-key.hex")
HARNESS = "/usr/local/bin/viber_dump"


def captured_key():
    try:
        blob = open(KEY_FILE, "r", errors="ignore").read()
    except OSError:
        return None
    m = re.search(r"[0-9a-fA-F]{64}", blob)
    return m.group(0) if m else None


def profile_db():
    for d in sorted(glob.glob(f"{HOME}/.ViberPC/*/")):
        if os.path.exists(d + "viber.db") and os.path.exists(d + "data.db"):
            return d + "viber.db"
    return None


def run_harness(db, key, *sql):
    env = dict(os.environ)
    env.update({
        "VIBER_DB": db,
        "VIBER_HEXKEY": key,
        "LD_LIBRARY_PATH": "/opt/viber/lib",
        "QT_PLUGIN_PATH": "/opt/viber/plugins",
        "QT_QPA_PLATFORM": "offscreen",
    })
    return subprocess.run([HARNESS, *sql], env=env, capture_output=True, text=True, timeout=60)


def main():
    key = captured_key()
    if not key:
        print("No 64-hex key in", KEY_FILE)
        return 2
    src = profile_db()
    if not src:
        print("viber.db not found")
        return 2

    tmp = tempfile.mkdtemp(prefix="plugindec_")
    for sfx in ("", "-wal", "-shm"):
        if os.path.exists(src + sfx):
            shutil.copy2(src + sfx, os.path.join(tmp, "viber.db" + sfx))
    db = os.path.join(tmp, "viber.db")

    r = run_harness(db, key)
    tables = [t.strip() for t in r.stdout.splitlines() if t.strip()]
    if not tables:
        print("Plugin open FAILED.")
        print("stderr:", r.stderr.strip()[:500])
        shutil.rmtree(tmp, ignore_errors=True)
        return 1

    print("[OPENED via Viber plugin]")
    print("tables:", " ".join(tables)[:600])
    # row counts for tables that look relevant to a messaging bridge
    wanted = [t for t in tables if re.search(r"(event|message|messag|contact|conversation|call|chat|recipient)", t, re.I)]
    for t in wanted[:12]:
        rc = run_harness(db, key, f'SELECT count(*) FROM "{t}"')
        n = rc.stdout.strip().splitlines()
        if n and n[-1].strip().isdigit():
            print(f"  {t}: {n[-1].strip()} rows")
    shutil.rmtree(tmp, ignore_errors=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
