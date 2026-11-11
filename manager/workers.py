#!/usr/bin/env python3
"""Tracks the running per-session inbound workers (started on bind, stopped on delete)."""

import threading

from .inbound import InboundWorker


class Workers:
    def __init__(self, registry, dispatcher, api_key, log):
        self.reg = registry
        self.dispatcher = dispatcher
        self.api_key = api_key
        self.log = log
        self._workers = {}
        self._lock = threading.Lock()

    def start(self, sid):
        with self._lock:
            if sid in self._workers:
                return
            w = InboundWorker(sid, self.reg, self.dispatcher, self.api_key, self.log)
            self._workers[sid] = w
            w.start()
        self.log(f"[workers] inbound started for {sid}")

    def stop(self, sid):
        with self._lock:
            w = self._workers.pop(sid, None)
        if w:
            w.stop()
            self.log(f"[workers] inbound stopped for {sid}")

    def start_bound(self):
        for row in self.reg.bound_sessions():
            self.start(row["id"])

    def stop_all(self):
        with self._lock:
            workers = list(self._workers.values())
            self._workers.clear()
        for w in workers:
            w.stop()
