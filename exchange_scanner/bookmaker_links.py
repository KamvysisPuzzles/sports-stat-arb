from __future__ import annotations

from dataclasses import dataclass
from html.parser import HTMLParser
from urllib.parse import parse_qs, quote_plus, unquote, urlparse

import httpx

BOOKMAKER_DOMAINS = {
    "bet365": ("bet365.com",),
    "betfred": ("betfred.com",),
    "betvictor": ("betvictor.com",),
    "betway": ("betway.com",),
    "boylesports": ("boylesports.com",),
    "coral": ("coral.co.uk",),
    "grosvenor": ("grosvenorcasinos.com",),
    "ladbrokes": ("ladbrokes.com",),
    "ladbrokesuk": ("ladbrokes.com",),
    "livescorebet": ("livescorebet.com",),
    "paddypower": ("paddypower.com",),
    "skybet": ("skybet.com",),
    "sport888": ("888sport.com",),
    "unibetuk": ("unibet.co.uk",),
    "unibet_uk": ("unibet.co.uk",),
    "virginbet": ("virginbet.com",),
    "williamhill": ("williamhill.com",),
}


@dataclass(frozen=True)
class EventPageResolution:
    status: str
    url: str = ""
    reason: str = ""


class DuckDuckGoResultParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.results: list[tuple[str, str]] = []
        self._capture_href: str | None = None
        self._capture_text: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag != "a":
            return
        attr_map = dict(attrs)
        classes = attr_map.get("class", "")
        href = attr_map.get("href")
        if href and ("result__a" in classes or "result-link" in classes):
            self._capture_href = href
            self._capture_text = []

    def handle_data(self, data: str) -> None:
        if self._capture_href:
            self._capture_text.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag == "a" and self._capture_href:
            title = " ".join(part.strip() for part in self._capture_text if part.strip())
            self.results.append((title, _unwrap_duckduckgo_url(self._capture_href)))
            self._capture_href = None
            self._capture_text = []


def resolve_event_page(
    *,
    bookmaker: str,
    event_name: str,
    selection: str = "",
    client: httpx.Client | None = None,
    timeout: float = 8.0,
) -> EventPageResolution:
    domains = _bookmaker_domains(bookmaker)
    if not domains:
        return EventPageResolution("unsupported_bookmaker", reason="No known bookmaker domain.")

    query = _query(bookmaker=bookmaker, event_name=event_name, selection=selection)
    owns_client = client is None
    http = client or httpx.Client(timeout=timeout, follow_redirects=True)
    try:
        response = http.get(
            "https://duckduckgo.com/html/",
            params={"q": query},
            headers={"User-Agent": "Mozilla/5.0"},
        )
        response.raise_for_status()
    except httpx.HTTPError as exc:
        return EventPageResolution("search_failed", reason=str(exc))
    finally:
        if owns_client:
            http.close()

    parser = DuckDuckGoResultParser()
    parser.feed(response.text)
    event_tokens = _event_tokens(event_name)
    for title, url in parser.results:
        if not _domain_matches(url, domains):
            continue
        haystack = f"{title} {url}".casefold()
        if not all(token in haystack for token in event_tokens):
            continue
        return EventPageResolution("resolved", url=url, reason="Search result matched bookmaker and event tokens.")

    for title, url in parser.results:
        if _domain_matches(url, domains):
            return EventPageResolution(
                "bookmaker_result_unverified",
                url=url,
                reason="Found bookmaker result, but could not verify all event tokens.",
            )

    return EventPageResolution("not_found", reason="No bookmaker-owned event result found.")


def _query(*, bookmaker: str, event_name: str, selection: str) -> str:
    parts = [bookmaker, event_name]
    if selection and selection.casefold() != "draw":
        parts.append(selection)
    return " ".join(parts)


def _bookmaker_domains(bookmaker: str) -> tuple[str, ...]:
    key = _bookmaker_key(bookmaker)
    return BOOKMAKER_DOMAINS.get(key, ())


def _bookmaker_key(bookmaker: str) -> str:
    return (
        bookmaker.casefold()
        .replace(" ", "")
        .replace("(", "")
        .replace(")", "")
        .replace("-", "")
        .replace("_", "")
    )


def _domain_matches(url: str, domains: tuple[str, ...]) -> bool:
    host = urlparse(url).netloc.casefold()
    return any(host == domain or host.endswith(f".{domain}") for domain in domains)


def _event_tokens(event_name: str) -> list[str]:
    return [
        token.casefold()
        for token in event_name.replace(" v ", " ").replace(" vs ", " ").split()
        if len(token) >= 4
    ][:6]


def _unwrap_duckduckgo_url(url: str) -> str:
    parsed = urlparse(url)
    if parsed.path.startswith("/l/"):
        uddg = parse_qs(parsed.query).get("uddg")
        if uddg:
            return unquote(uddg[0])
    if parsed.netloc.endswith("duckduckgo.com"):
        uddg = parse_qs(parsed.query).get("uddg")
        if uddg:
            return unquote(uddg[0])
    if url.startswith("//duckduckgo.com/l/"):
        uddg = parse_qs(urlparse(f"https:{url}").query).get("uddg")
        if uddg:
            return unquote(uddg[0])
    return url


def event_search_url(bookmaker: str, event_name: str, selection: str = "") -> str:
    return f"https://duckduckgo.com/?q={quote_plus(_query(bookmaker=bookmaker, event_name=event_name, selection=selection))}"
