"""Фейковый клиент Gmail API (users().messages().*().execute()) для тестов — без сети."""
import base64
import email
import email.policy
import itertools


class _Req:
    def __init__(self, fn):
        self._fn = fn

    def execute(self):
        return self._fn()


class _Messages:
    def __init__(self, svc):
        self.svc = svc

    def send(self, userId, body):
        def run():
            raw = base64.urlsafe_b64decode(body["raw"])
            self.svc.sent.append(raw)
            gid = f"sent{next(self.svc._ids)}"
            msg = email.message_from_bytes(raw, policy=email.policy.default)
            mid = self.svc.rewrite_message_id or str(msg["Message-ID"])
            self.svc.store[gid] = {"raw": raw, "labels": ["SENT"], "message_id": mid}
            return {"id": gid, "threadId": "t" + gid, "labelIds": ["SENT"]}
        return _Req(run)

    def list(self, userId, q, maxResults=100, pageToken=None):
        def run():
            self.svc.list_calls.append(q)
            ids = [gid for gid, m in self.svc.store.items() if "UNREAD" in m["labels"] and "INBOX" in m["labels"]]
            ids.reverse()  # как Gmail: новые первыми
            start = int(pageToken or 0)
            page = ids[start:start + self.svc.page_size]
            resp = {"messages": [{"id": i, "threadId": "t" + i} for i in page]}
            if start + self.svc.page_size < len(ids):
                resp["nextPageToken"] = str(start + self.svc.page_size)
            return resp
        return _Req(run)

    def get(self, userId, id, format="full", metadataHeaders=None):
        def run():
            m = self.svc.store[id]
            if format == "raw":
                return {"id": id, "labelIds": list(m["labels"]),
                        "raw": base64.urlsafe_b64encode(m["raw"]).decode().rstrip("=")}
            headers = [{"name": "Message-ID", "value": m["message_id"]}] if m.get("message_id") else []
            return {"id": id, "labelIds": list(m["labels"]), "payload": {"headers": headers}}
        return _Req(run)

    def batchModify(self, userId, body):
        def run():
            self.svc.modify_calls.append(body)
            for gid in body["ids"]:
                for label in body.get("removeLabelIds", []):
                    if label in self.svc.store[gid]["labels"]:
                        self.svc.store[gid]["labels"].remove(label)
            return None
        return _Req(run)


class _Users:
    def __init__(self, svc):
        self.svc = svc

    def messages(self):
        return _Messages(self.svc)


class FakeGmailService:
    def __init__(self):
        self.store: dict[str, dict] = {}
        self.sent: list[bytes] = []
        self.list_calls: list[str] = []
        self.modify_calls: list[dict] = []
        self.rewrite_message_id: str | None = None
        self.page_size = 100
        self._ids = itertools.count(1)

    def users(self):
        return _Users(self)

    def deliver(self, raw: bytes, labels=("INBOX", "UNREAD")) -> str:
        gid = f"in{next(self._ids)}"
        self.store[gid] = {"raw": raw, "labels": list(labels), "message_id": ""}
        return gid
