"""scripts/gmail_auth.py: параметры OAuth-потока и запись .env-строк (без реального браузера)."""
import importlib.util
import json
import os
from pathlib import Path
from types import SimpleNamespace

import google_auth_oauthlib.flow as flow_mod
import googleapiclient.discovery as discovery

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "gmail_auth.py"


def _load():
    spec = importlib.util.spec_from_file_location("gmail_auth", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_gmail_auth_writes_env_file(tmp_path, monkeypatch, capsys):
    client_json = tmp_path / "client.json"
    client_json.write_text(json.dumps({"installed": {"client_id": "cid", "client_secret": "S3CRET"}}))
    out = tmp_path / "gmail.env"
    seen = {}

    class FakeFlow:
        @classmethod
        def from_client_secrets_file(cls, path, scopes):
            seen["path"], seen["scopes"] = path, scopes
            return cls()

        def run_local_server(self, **kw):
            seen.update(kw)
            return SimpleNamespace(client_id="cid", client_secret="S3CRET", refresh_token="1//REFRESH")

    class FakeService:
        def users(self):
            return SimpleNamespace(getProfile=lambda userId: SimpleNamespace(
                execute=lambda: {"emailAddress": "support@example.com"}))

    monkeypatch.setattr(flow_mod, "InstalledAppFlow", FakeFlow)
    monkeypatch.setattr(discovery, "build", lambda *a, **k: FakeService())

    assert _load().main(["gmail_auth.py", str(client_json), str(out)]) == 0
    assert seen["scopes"] == ["https://www.googleapis.com/auth/gmail.modify"]
    assert seen["port"] == 0 and seen["open_browser"] is True
    assert seen["access_type"] == "offline" and seen["prompt"] == "consent"
    assert out.read_text().splitlines() == [
        "GMAIL_USER=support@example.com", "GMAIL_CLIENT_ID=cid",
        "GMAIL_CLIENT_SECRET=S3CRET", "GMAIL_REFRESH_TOKEN=1//REFRESH",
    ]
    assert oct(os.stat(out).st_mode & 0o777) == "0o600"
    printed = capsys.readouterr()
    assert "S3CRET" not in printed.out + printed.err and "1//REFRESH" not in printed.out + printed.err


def test_gmail_auth_usage():
    assert _load().main(["gmail_auth.py"]) == 2
