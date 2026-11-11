#!/usr/bin/env python3
"""Reaper: tears down sessions that no longer have a reason to run.

Chatwoot doesn't notify the inbox webhook when an inbox is deleted, so the manager reconciles on a
timer instead:
  - bound session  -> probe the inbox on Chatwoot's Viber endpoint; only an explicit
                      `exists: false` means the inbox was deleted, so stop the worker and remove the
                      container + volume. Probe errors (Chatwoot down, 401, 404, 5xx) are skipped.
                      A session whose Viber was unlinked on the phone, or whose database Viber
                      broke (unreadable for minutes), is reported to Chatwoot (the inbox asks for
                      reconnection, which links a fresh session) and removed.
  - unbound session older than UNBOUND_TTL -> abandoned onboarding (tab closed before linking or
                      before bind): remove it so idle Viber containers don't pile up.
"""

import threading
import time

from bridge.chatwoot import ViberInbox

from . import link

INTERVAL = 60  # seconds between passes
UNBOUND_TTL = 15 * 60  # an onboarding that hasn't bound within this window is abandoned


class Reaper:
    def __init__(self, registry, teardown, api_key, log):
        self.reg = registry
        self.api_key = api_key
        self.teardown = teardown
        self.log = log

    def start(self):
        threading.Thread(target=self._loop, daemon=True).start()

    def _loop(self):
        while True:
            self._pass()
            time.sleep(INTERVAL)

    def _pass(self):
        now = time.time()
        for row in self.reg.all():
            sid = row["id"]
            if row.get("inbox_id"):
                inbox = ViberInbox(row["chatwoot_url"], row["inbox_identifier"], self.api_key)
                try:
                    alive = inbox.exists()
                except Exception as e:  # noqa: BLE001 — any probe failure means "unknown", keep it
                    self.log(f"[reaper] {sid} inbox probe failed, keeping: {e}")
                    continue
                if not alive:
                    self.log(f"[reaper] {sid} inbox {row['inbox_id']} deleted -> teardown")
                    self.teardown(sid)
                elif reason := link.lost(row["container"]):
                    try:
                        inbox.report_unlinked()
                    except Exception as e:  # noqa: BLE001 — Chatwoot must hear it first; retry next pass
                        self.log(f"[reaper] {sid} {reason}, report failed, keeping: {e}")
                        continue
                    self.log(f"[reaper] {sid} Viber {reason} -> reported, teardown")
                    self.teardown(sid)
            elif now - row["created_at"] > UNBOUND_TTL:
                self.log(f"[reaper] {sid} unbound for {int(now - row['created_at'])}s -> teardown")
                self.teardown(sid)
