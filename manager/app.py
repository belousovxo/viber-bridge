#!/usr/bin/env python3
"""Viber Session Manager — HTTP API + orchestrator.

Control plane for the Viber<->Chatwoot bridge. Provisions a container per number, serves the
login QR, detects linking, holds the dynamic session registry, and runs per-session inbound
workers + the shared outbound webhook. Standalone service called server-to-server by Chatwoot
(never by browsers).

Env:
  MANAGER_API_KEY       required; Chatwoot sends it as `Authorization: Bearer <key>`, and the manager
                        sends it back when recording messages sent from the phone
  VIBER_SESSION_IMAGE   image to run per session           (default: viber-session:latest)
  SESSION_MEMORY        memory limit per session container (default: 1g)
  MANAGER_REGISTRY      session store path                 (default: sessions.json)
  MANAGER_HOST          bind host                          (default: 0.0.0.0)
  MANAGER_PORT          bind port                          (default: 8800)

API (Bearer key):
  POST   /sessions                 -> {session_id, status}
  GET    /sessions                 -> [session, ...]
  GET    /sessions/<id>            -> {status, number}
  GET    /sessions/<id>/qr         -> image/png (login QR) | 202 not on screen yet | 409 once linked
  POST   /sessions/<id>/bind       {inbox_id, inbox_identifier, webhook_secret, chatwoot_url, account_id?}
  DELETE /sessions/<id>            -> {ok}
Open:
  POST   /chatwoot/webhook         Channel::Api webhook (HMAC-signed with the inbox secret) -> Viber
  GET    /health

Background: a reaper (manager/reaper.py) removes sessions whose inbox was deleted in Chatwoot and
onboardings abandoned before bind.
"""

import hashlib
import hmac
import os
import time

from flask import Flask, Response, jsonify, request

from . import docker_ops
from .outbound import Dispatcher
from .qr import qr_png
from .reaper import Reaper
from .registry import Registry
from .sessions import SessionManager
from .workers import Workers

WEBHOOK_MAX_AGE = 300  # seconds; older signed deliveries are treated as replays
OPEN_ENDPOINTS = {"webhook", "health"}  # authenticated by signature / public


def log(msg):
    print(msg, flush=True)


def inbox_id_of(payload):
    """message_* events carry `inbox.id`, typing events `conversation.inbox_id`, conversation_* events
    are the conversation itself (top-level `inbox_id`)."""
    return ((payload.get("inbox") or {}).get("id") or (payload.get("conversation") or {}).get("inbox_id")
            or payload.get("inbox_id"))


def signature_valid(secret, headers, body):
    """Chatwoot signs `<timestamp>.<raw body>` with the Channel::Api secret (Webhooks::Trigger)."""
    ts = headers.get("X-Chatwoot-Timestamp", "")
    if not secret or not ts.isdigit() or abs(time.time() - int(ts)) > WEBHOOK_MAX_AGE:
        return False
    expected = "sha256=" + hmac.new(secret.encode(), ts.encode() + b"." + body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, headers.get("X-Chatwoot-Signature", ""))


def create_app():
    api_key = os.environ["MANAGER_API_KEY"]
    registry = Registry(os.environ.get("MANAGER_REGISTRY", "sessions.json"))
    sessions = SessionManager(registry, os.environ.get("VIBER_SESSION_IMAGE", "viber-session:latest"),
                              os.environ.get("SESSION_MEMORY", "1g"), log)
    dispatcher = Dispatcher(log)
    workers = Workers(registry, dispatcher, api_key, log)

    def teardown(sid):
        workers.stop(sid)
        return sessions.delete(sid)

    docker_ops.ensure_session_network()
    workers.start_bound()  # resume bound sessions after a restart
    Reaper(registry, teardown, api_key, log).start()  # drop sessions of deleted inboxes / abandoned onboarding

    app = Flask(__name__)

    @app.before_request
    def require_api_key():
        if request.endpoint in OPEN_ENDPOINTS:
            return None
        if not hmac.compare_digest(request.headers.get("Authorization", ""), f"Bearer {api_key}"):
            return jsonify({"error": "unauthorized"}), 401
        return None

    @app.route("/health", methods=["GET"])
    def health():
        return jsonify({"ok": True})

    @app.route("/sessions", methods=["POST"])
    def create_session():
        row = sessions.create()
        return jsonify({"session_id": row["id"], "status": row["status"]}), 201

    @app.route("/sessions", methods=["GET"])
    def list_sessions():
        return jsonify(registry.all())

    @app.route("/sessions/<sid>", methods=["GET"])
    def get_session(sid):
        row = sessions.refresh(sid)
        if not row:
            return jsonify({"error": "not found"}), 404
        return jsonify({"status": row["status"], "number": row["number"], "id": row["id"]})

    @app.route("/sessions/<sid>/qr", methods=["GET"])
    def get_qr(sid):
        row = sessions.refresh(sid)
        if not row:
            return jsonify({"error": "not found"}), 404
        if row["status"] == "linked":
            return jsonify({"status": "linked", "number": row["number"]}), 409
        png = qr_png(row["container"])
        if not png:
            return jsonify({"status": row["status"]}), 202  # login QR not on screen yet
        return Response(png, mimetype="image/png")

    @app.route("/sessions/<sid>/bind", methods=["POST"])
    def bind_session(sid):
        if not registry.get(sid):
            return jsonify({"error": "not found"}), 404
        body = request.get_json(silent=True) or {}
        required = ("inbox_id", "inbox_identifier", "webhook_secret", "chatwoot_url")
        missing = [k for k in required if not body.get(k)]
        if missing:
            return jsonify({"error": f"missing: {', '.join(missing)}"}), 422
        registry.update(sid, inbox_id=int(body["inbox_id"]), inbox_identifier=body["inbox_identifier"],
                        webhook_secret=body["webhook_secret"], chatwoot_url=body["chatwoot_url"].rstrip("/"),
                        account_id=body.get("account_id"))
        workers.start(sid)
        log(f"[bind] {sid} -> inbox {body['inbox_id']} ({body['inbox_identifier']})")
        return jsonify({"ok": True})

    @app.route("/sessions/<sid>", methods=["DELETE"])
    def delete_session(sid):
        ok = teardown(sid)
        return jsonify({"ok": ok}), (200 if ok else 404)

    @app.route("/chatwoot/webhook", methods=["POST"])
    def webhook():
        raw = request.get_data()
        payload = request.get_json(silent=True) or {}
        inbox_id = inbox_id_of(payload)
        session = registry.by_inbox(inbox_id) if inbox_id else None
        if not session or not signature_valid(session.get("webhook_secret"), request.headers, raw):
            return jsonify({"error": "unauthorized"}), 401
        dispatcher.handle(session, payload)  # queues the send; returns at once
        return ("", 200)

    return app


def main():
    app = create_app()
    host = os.environ.get("MANAGER_HOST", "0.0.0.0")
    port = int(os.environ.get("MANAGER_PORT", "8800"))
    log(f"[manager] listening on {host}:{port}")
    app.run(host=host, port=port, threaded=True)


if __name__ == "__main__":
    main()
