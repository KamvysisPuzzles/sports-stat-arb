from __future__ import annotations

import pytest

from exchange_scanner import betfair_auth


class FakeResponse:
    def __init__(self, payload):
        self.payload = payload

    def raise_for_status(self) -> None:
        return None

    def json(self):
        return self.payload


class FakeClient:
    captured = {}

    def __init__(self, *, cert, timeout):
        FakeClient.captured["cert"] = cert
        FakeClient.captured["timeout"] = timeout

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        return None

    def post(self, url, *, data, headers):
        FakeClient.captured["url"] = url
        FakeClient.captured["data"] = data
        FakeClient.captured["headers"] = headers
        return FakeResponse({"loginStatus": "SUCCESS", "sessionToken": "token-123"})


def test_certificate_login_returns_session_token(monkeypatch, tmp_path) -> None:
    cert = tmp_path / "client.crt"
    key = tmp_path / "client.key"
    cert.write_text("cert", encoding="utf-8")
    key.write_text("key", encoding="utf-8")
    monkeypatch.setattr(betfair_auth.httpx, "Client", FakeClient)

    token = betfair_auth.certificate_login(
        username="user",
        password="pass",
        app_key="app",
        cert_file=cert,
        key_file=key,
    )

    assert token == "token-123"
    assert FakeClient.captured["cert"] == (str(cert), str(key))
    assert FakeClient.captured["headers"]["X-Application"] == "app"


def test_certificate_login_raises_on_failed_login(monkeypatch, tmp_path) -> None:
    class FailingClient(FakeClient):
        def post(self, url, *, data, headers):
            return FakeResponse({"loginStatus": "CERT_AUTH_REQUIRED"})

    cert = tmp_path / "client.crt"
    key = tmp_path / "client.key"
    cert.write_text("cert", encoding="utf-8")
    key.write_text("key", encoding="utf-8")
    monkeypatch.setattr(betfair_auth.httpx, "Client", FailingClient)

    with pytest.raises(RuntimeError, match="CERT_AUTH_REQUIRED"):
        betfair_auth.certificate_login(
            username="user",
            password="pass",
            app_key="app",
            cert_file=cert,
            key_file=key,
        )
