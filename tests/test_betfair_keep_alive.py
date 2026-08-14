from __future__ import annotations

from scripts import betfair_keep_alive


class FakeResponse:
    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict[str, str]:
        return {"status": "SUCCESS"}


def test_keep_alive_uses_delayed_key_first(monkeypatch, capsys) -> None:
    captured = {}

    def fake_post(url, *, headers, timeout):
        captured["url"] = url
        captured["headers"] = headers
        captured["timeout"] = timeout
        return FakeResponse()

    monkeypatch.setenv("BETFAIR_APP_KEY_DELAYED", "delayed-key")
    monkeypatch.setenv("BETFAIR_APP_KEY", "bad")
    monkeypatch.setenv("BETFAIR_SESSION_TOKEN", "session-token")
    monkeypatch.delenv("BETFAIR_USERNAME", raising=False)
    monkeypatch.delenv("BETFAIR_PASSWORD", raising=False)
    monkeypatch.delenv("BETFAIR_CERT_FILE", raising=False)
    monkeypatch.delenv("BETFAIR_KEY_FILE", raising=False)
    monkeypatch.setattr(betfair_keep_alive, "load_dotenv", lambda: None)
    monkeypatch.setattr(betfair_keep_alive.httpx, "post", fake_post)

    betfair_keep_alive.main()

    assert captured["headers"]["X-Application"] == "delayed-key"
    assert captured["headers"]["X-Authentication"] == "session-token"
    assert "succeeded" in capsys.readouterr().out


def test_keep_alive_skips_when_certificate_login_is_blocked(monkeypatch, capsys) -> None:
    def blocked_login(**kwargs):
        raise RuntimeError("restricted location")

    monkeypatch.setenv("BETFAIR_APP_KEY_DELAYED", "delayed-key")
    monkeypatch.setenv("BETFAIR_USERNAME", "user")
    monkeypatch.setenv("BETFAIR_PASSWORD", "pass")
    monkeypatch.setenv("BETFAIR_CERT_FILE", "/tmp/client.crt")
    monkeypatch.setenv("BETFAIR_KEY_FILE", "/tmp/client.key")
    monkeypatch.delenv("BETFAIR_SESSION_TOKEN", raising=False)
    monkeypatch.setattr(betfair_keep_alive, "load_dotenv", lambda: None)
    monkeypatch.setattr(betfair_keep_alive, "certificate_login", blocked_login)

    betfair_keep_alive.main()

    output = capsys.readouterr().out
    assert "keepAlive skipped" in output
    assert "not configured" in output
