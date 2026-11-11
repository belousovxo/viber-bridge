#!/usr/bin/env python3
"""Client for Chatwoot's Viber endpoint, the manager's only way into Chatwoot:

  POST /webhooks/viber/:inbox_identifier   a message of the session (incoming from a contact, or
                                           outgoing when the owner sent it from the phone)
  POST /webhooks/viber/:inbox_identifier/messages/:id   result of sending an outgoing message:
                                           {source_id} (its Viber EventID) or {error}
  POST /webhooks/viber/:inbox_identifier/unlinked      the session lost its Viber login
  GET  /webhooks/viber/:inbox_identifier   {"exists": bool}

Authenticated with the manager API key (`Authorization: Bearer`), the same one Chatwoot uses to call
the manager. Chatwoot keys the contact by phone number (source_id) and picks the conversation itself,
so the manager keeps no contact or conversation state.
"""

import requests


class ChatwootError(RuntimeError):
    pass


class ViberInbox:
    def __init__(self, base_url, inbox_identifier, api_key, timeout=20):
        self.url = f"{base_url}/webhooks/viber/{inbox_identifier}"
        self.timeout = timeout
        self.s = requests.Session()
        self.s.headers["Authorization"] = f"Bearer {api_key}"

    def exists(self):
        """False only when Chatwoot answers `exists: false`. Anything else raises (network errors, 401,
        404 from a wrong URL or an older Chatwoot, 5xx, non-JSON), so it's never mistaken for a
        deletion."""
        r = self.s.get(self.url, timeout=self.timeout)
        if not r.ok:
            raise ChatwootError(f"GET inbox -> {r.status_code}")
        try:
            return bool(r.json()["exists"])
        except (ValueError, KeyError) as e:
            raise ChatwootError(f"GET inbox -> unexpected body: {r.text[:200]}") from e

    def post_message(self, number, outgoing, event_id, name=None, content=None, attachments=None,
                     unsupported=False):
        """`attachments` is a list of (filename, bytes, mime). `unsupported` marks a message whose type
        Chatwoot can't show (it then needs neither content nor attachments)."""
        data = {"number": number, "direction": "outgoing" if outgoing else "incoming", "source_id": event_id}
        if unsupported:
            data["unsupported"] = "true"
        if name:
            data["name"] = name
        if content:
            data["content"] = content
        files = [("attachments[]", att) for att in attachments or []]
        r = self.s.post(self.url, data=data, files=files or None, timeout=self.timeout)
        if not r.ok:
            raise ChatwootError(f"POST message -> {r.status_code}: {r.text[:300]}")

    def report_unlinked(self):
        r = self.s.post(f"{self.url}/unlinked", timeout=self.timeout)
        if not r.ok:
            raise ChatwootError(f"POST unlinked -> {r.status_code}: {r.text[:300]}")

    def message_result(self, message_id, source_id=None, error=None):
        """Outcome of sending Chatwoot message `message_id`: its Viber EventID, or why it failed."""
        data = {"error": error} if error else {"source_id": source_id}
        r = self.s.post(f"{self.url}/messages/{message_id}", data=data, timeout=self.timeout)
        if not r.ok:
            raise ChatwootError(f"POST message result -> {r.status_code}: {r.text[:300]}")
