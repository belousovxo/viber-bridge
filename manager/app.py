#!/usr/bin/env python3
"""Viber Session Manager — HTTP API + orchestrator.

Control plane for the Viber<->Chatwoot bridge. Provisions a container per number, serves the
login QR, detects linking, holds the dynamic session registry, and runs per-session inbound
workers + the shared outbound webhook. Standalone service; Chatwoot reaches it at a configured URL.

Env:
  VIBER_SESSION_IMAGE   image to run per session           (default: viber-session:latest)
  MANAGER_REGISTRY      session store path                 (default: sessions.json)
  MANAGER_HOST          bind host                          (default: 0.0.0.0)
  MANAGER_PORT          bind port                          (default: 8800)

API:
  POST   /sessions                 -> {session_id, status}
  GET    /sessions                 -> [session, ...]
  GET    /sessions/<id>            -> {status, number}
  GET    /sessions/<id>/qr         -> image/png (login QR) | 404 once linked
  POST   /sessions/<id>/bind       {inbox_id, inbox_identifier, chatwoot_url, account_id?}
  DELETE /sessions/<id>            -> {ok}
  POST   /chatwoot/webhook         Channel::Api webhook -> Viber send

Background: a reaper (manager/reaper.py) removes sessions whose inbox was deleted in Chatwoot and
onboardings abandoned before bind.
"""

import os
import threading

from flask import Flask, Response, jsonify, request

from .outbound import Dispatcher
from .qr import screenshot_png
from .reaper import Reaper
from .registry import Registry
from .sessions import SessionManager
from .workers import Workers


def log(msg):
    print(msg, flush=True)


def create_app():
    image = os.environ.get("VIBER_SESSION_IMAGE", "viber-session:latest")
    registry = Registry(os.environ.get("MANAGER_REGISTRY", "sessions.json"))
    sessions = SessionManager(registry, image, log)
    workers = Workers(registry, log)
    dispatcher = Dispatcher(registry, log)

    def teardown(sid):
        workers.stop(sid)
        return sessions.delete(sid)

    workers.start_bound()  # resume bound sessions after a restart
    Reaper(registry, teardown, log).start()  # drop sessions of deleted inboxes / abandoned onboarding

    app = Flask(__name__)

    @app.after_request
    def cors(resp):
        resp.headers["Access-Control-Allow-Origin"] = "*"
        resp.headers["Access-Control-Allow-Headers"] = "Content-Type"
        resp.headers["Access-Control-Allow-Methods"] = "GET, POST, DELETE, OPTIONS"
        return resp

    @app.route("/sessions", methods=["POST", "OPTIONS"])
    def create_session():
        if request.method == "OPTIONS":
            return ("", 204)
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
        png = screenshot_png(row["container"])
        if not png:
            return jsonify({"status": row["status"]}), 202  # not capturable yet
        return Response(png, mimetype="image/png")

    @app.route("/sessions/<sid>/bind", methods=["POST", "OPTIONS"])
    def bind_session(sid):
        if request.method == "OPTIONS":
            return ("", 204)
        if not registry.get(sid):
            return jsonify({"error": "not found"}), 404
        body = request.get_json(force=True, silent=True) or {}
        required = ("inbox_id", "inbox_identifier", "chatwoot_url")
        missing = [k for k in required if not body.get(k)]
        if missing:
            return jsonify({"error": f"missing: {', '.join(missing)}"}), 422
        registry.update(sid, inbox_id=int(body["inbox_id"]), inbox_identifier=body["inbox_identifier"],
                        chatwoot_url=body["chatwoot_url"].rstrip("/"),
                        account_id=body.get("account_id"))
        workers.start(sid)
        log(f"[bind] {sid} -> inbox {body['inbox_id']} ({body['inbox_identifier']})")
        return jsonify({"ok": True})

    @app.route("/sessions/<sid>", methods=["DELETE", "OPTIONS"])
    def delete_session(sid):
        if request.method == "OPTIONS":
            return ("", 204)
        ok = teardown(sid)
        return jsonify({"ok": ok}), (200 if ok else 404)

    @app.route("/chatwoot/webhook", methods=["POST"])
    def webhook():
        payload = request.get_json(force=True, silent=True) or {}
        threading.Thread(target=dispatcher.handle, args=(payload,), daemon=True).start()
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
