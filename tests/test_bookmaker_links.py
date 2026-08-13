from __future__ import annotations

from urllib.parse import quote

import httpx

from exchange_scanner.bookmaker_links import resolve_event_page


def test_resolves_bookmaker_event_page_from_search_result() -> None:
    event_url = "https://www.paddypower.com/football/notts-county-v-leicester-city-123"
    html = f"""
    <html>
      <body>
        <a class="result__a" href="/l/?uddg={quote(event_url, safe='')}">
          Notts County v Leicester City Betting Odds | Paddy Power
        </a>
      </body>
    </html>
    """

    client = _mock_search_client(html)
    resolution = resolve_event_page(
        bookmaker="Paddy Power",
        event_name="Notts County v Leicester City",
        selection="Notts County",
        client=client,
    )

    assert resolution.status == "resolved"
    assert resolution.url == event_url


def test_returns_unverified_bookmaker_result_when_event_tokens_do_not_match() -> None:
    event_url = "https://www.betvictor.com/sports/football"
    html = f"""
    <html>
      <body>
        <a class="result__a" href="/l/?uddg={quote(event_url, safe='')}">
          Football Betting Odds | BetVictor
        </a>
      </body>
    </html>
    """

    client = _mock_search_client(html)
    resolution = resolve_event_page(
        bookmaker="Bet Victor",
        event_name="Cruz Azul v Chicago Fire",
        selection="Draw",
        client=client,
    )

    assert resolution.status == "bookmaker_result_unverified"
    assert resolution.url == event_url


def test_unsupported_bookmaker_does_not_search() -> None:
    resolution = resolve_event_page(
        bookmaker="Unknown Book",
        event_name="Notts County v Leicester City",
    )

    assert resolution.status == "unsupported_bookmaker"


def _mock_search_client(html: str) -> httpx.Client:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, request=request, text=html)

    return httpx.Client(transport=httpx.MockTransport(handler))
