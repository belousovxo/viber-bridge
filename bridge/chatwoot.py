#!/usr/bin/env python3
"""Chatwoot **Public Inbox API** client for a Channel::Api inbox.

This is the token-free, contact-facing path designed for custom channels: everything is keyed by
the channel's `inbox_identifier` (handed to the manager at bind time) plus a per-sender `source_id`.
We set `source_id` = the sender's E.164 number, and ContactInboxWithContactBuilder is idempotent on
it, so contacts/threads never duplicate. No agent/bot token is needed for incoming (the AgentBot
path can't create contacts anyway); outgoing is received separately on the Channel::Api webhook.

Liveness probe (used by the manager's reaper): GET /public/api/v1/inboxes/:identifier -> 404 once
the inbox is deleted.

Endpoints (POST):
  /public/api/v1/inboxes/:identifier/contacts                                   -> create contact
  /public/api/v1/inboxes/:identifier/contacts/:source_id/conversations          -> create conversation
  /public/api/v1/inboxes/:identifier/contacts/:source_id/conversations/:id/messages
"""

import requests


class ChatwootError(RuntimeError):
    pass


class PublicInbox:
    def __init__(self, base_url, inbox_identifier, timeout=20):
        self.base = f"{base_url}/public/api/v1/inboxes/{inbox_identifier}"
        self.timeout = timeout
        self.s = requests.Session()

    def _post(self, path, **kw):
        r = self.s.post(f"{self.base}{path}", timeout=self.timeout, **kw)
        if not r.ok:
            raise ChatwootError(f"POST {path} -> {r.status_code}: {r.text[:300]}")
        return r.json() if r.content else {}

    def exists(self):
        """False only on an explicit 404 (inbox/channel deleted). Network errors and 5xx raise, so a
        Chatwoot outage is never mistaken for a deletion."""
        r = self.s.get(self.base, timeout=self.timeout)
        if r.status_code == 404:
            return False
        if not r.ok:
            raise ChatwootError(f"GET inbox -> {r.status_code}")
        return True

    def ensure_contact(self, number, name=None):
        """Idempotent on source_id = number. Returns the source_id (= number)."""
        payload = {"source_id": number, "identifier": number, "phone_number": number}
        if name:
            payload["name"] = name
        self._post("/contacts", json=payload)
        return number

    def create_conversation(self, source_id):
        """Returns the conversation display id (used in the messages URL)."""
        data = self._post(f"/contacts/{source_id}/conversations", json={})
        return data["id"]

    def create_incoming(self, source_id, conversation_id, content=None, attachments=None):
        """Post an incoming message. `attachments` is a list of (filename, bytes, mime)."""
        path = f"/contacts/{source_id}/conversations/{conversation_id}/messages"
        if attachments:
            files = [("attachments[]", (fn, blob, mime)) for (fn, blob, mime) in attachments]
            data = {"content": content} if content else {}
            res = self._post(path, data=data, files=files)
        else:
            res = self._post(path, json={"content": content or ""})
        return res.get("id")
