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
    monkeypatch.setattr(betfair_keep_alive.httpx, "post", fake_post)

    betfair_keep_alive.main()

    assert captured["headers"]["X-Application"] == "delayed-key"
    assert captured["headers"]["X-Authentication"] == "session-token"
    assert "succeeded" in capsys.readouterr().out
