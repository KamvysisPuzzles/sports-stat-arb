from __future__ import annotations

from scripts import enrich_betfair_liquidity


def test_session_token_returns_empty_when_certificate_login_is_blocked(monkeypatch, capsys) -> None:
    def blocked_login(**kwargs):
        raise RuntimeError("restricted location")

    monkeypatch.setenv("BETFAIR_USERNAME", "user")
    monkeypatch.setenv("BETFAIR_PASSWORD", "pass")
    monkeypatch.setenv("BETFAIR_CERT_FILE", "/tmp/client.crt")
    monkeypatch.setenv("BETFAIR_KEY_FILE", "/tmp/client.key")
    monkeypatch.delenv("BETFAIR_SESSION_TOKEN", raising=False)
    monkeypatch.setattr(enrich_betfair_liquidity, "certificate_login", blocked_login)

    assert enrich_betfair_liquidity._session_token("app-key") == ""
    assert "betfair_not_configured" in capsys.readouterr().err
