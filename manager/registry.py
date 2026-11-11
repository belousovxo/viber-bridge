#!/usr/bin/env python3
"""Persisted session registry — the manager's dynamic replacement for the static registry.yml.

One row per Viber session, holding the container identity, link state, and (after bind) the
Chatwoot wiring. Durable across manager restarts (JSON file, atomic writes, lock-guarded) so
bound sessions resume their workers on boot.

Session row:
  id            short session id (also used in container/volume names)
  container     docker container name (viber-session-<id>)
  volume        docker volume name    (viber-<id>)
  status        starting | linking | linked | error
  number        linked Viber E.164 number (once status == linked)
  account_id       Chatwoot account id         (after bind, informational)
  inbox_id         Chatwoot Channel::Api inbox  (after bind; routes outgoing webhooks)
  inbox_identifier Channel::Api identifier       (after bind; keys Chatwoot's Viber endpoint)
  webhook_secret   Channel::Api secret           (after bind; verifies the inbox's webhook signature)
  chatwoot_url     base url of that Chatwoot     (after bind)
  last_event       inbound dedup cursor (Viber EventID)
"""

import json
import os
import threading
import time


class Registry:
    def __init__(self, path):
        self.path = path
        self._lock = threading.RLock()
        self._rows = self._load()

    def _load(self):
        try:
            with open(self.path) as f:
                return json.load(f)
        except (OSError, ValueError):
            return {}

    def _flush(self):
        tmp = self.path + ".tmp"
        with open(tmp, "w") as f:
            json.dump(self._rows, f, ensure_ascii=False, indent=2)
        os.replace(tmp, self.path)

    def create(self, session_id, container, volume):
        with self._lock:
            self._rows[session_id] = {
                "id": session_id,
                "container": container,
                "volume": volume,
                "status": "starting",
                "number": None,
                "account_id": None,
                "inbox_id": None,
                "inbox_identifier": None,
                "chatwoot_url": None,
                "last_event": 0,
                "created_at": int(time.time()),
            }
            self._flush()
            return dict(self._rows[session_id])

    def get(self, session_id):
        with self._lock:
            row = self._rows.get(session_id)
            return dict(row) if row else None

    def all(self):
        with self._lock:
            return [dict(r) for r in self._rows.values()]

    def update(self, session_id, **kv):
        with self._lock:
            row = self._rows.get(session_id)
            if not row:
                return None
            row.update(kv)
            self._flush()
            return dict(row)

    def delete(self, session_id):
        with self._lock:
            row = self._rows.pop(session_id, None)
            if row:
                self._flush()
            return row

    def by_inbox(self, inbox_id):
        inbox_id = int(inbox_id)
        with self._lock:
            for r in self._rows.values():
                if r.get("inbox_id") == inbox_id:
                    return dict(r)
        return None

    def bound_sessions(self):
        """Sessions that have Chatwoot wiring and should have a running inbound worker."""
        with self._lock:
            return [dict(r) for r in self._rows.values() if r.get("inbox_id")]
