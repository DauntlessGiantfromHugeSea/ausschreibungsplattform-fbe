"""Zusaetzliche Tests fuer den service.bund.de-Scraper."""
from __future__ import annotations

from urllib.parse import parse_qs, urlsplit

from scrapers.bund import BundScraper


BASE = "https://www.service.bund.de"


# ---------------------------------------------------------------------------
def test_listing_url_default_params(monkeypatch):
    """Default-Listing schickt resultsPerPage=100 + sortOrder=dateOfIssue_dt desc."""
    captured: list[str] = []

    class FakeResp:
        status_code = 200
        text = "<html></html>"

    s = BundScraper(base_url=BASE, name="bund.de", config={})
    try:
        # robots umgehen + get mocken
        s._robots = type("R", (), {"can_fetch": lambda *a: True})()
        monkeypatch.setattr(s, "get", lambda url: captured.append(url) or FakeResp())
        s._fetch_listing(page=1)
        assert len(captured) == 1
        u = urlsplit(captured[0])
        params = parse_qs(u.query)
        assert params["resultsPerPage"] == ["100"]
        assert "dateOfIssue_dt" in params["sortOrder"][0]
        assert "desc" in params["sortOrder"][0]
        assert "GP" not in params  # erste Seite ohne GP
        # Sanity: Pfad stimmt
        assert u.path == "/Content/DE/Ausschreibungen/Suche/Formular.html"
    finally:
        s.close()


def test_listing_url_pagination(monkeypatch):
    captured: list[str] = []

    class FakeResp:
        status_code = 200
        text = "<html></html>"

    s = BundScraper(base_url=BASE, name="bund.de", config={})
    try:
        s._robots = type("R", (), {"can_fetch": lambda *a: True})()
        monkeypatch.setattr(s, "get", lambda url: captured.append(url) or FakeResp())
        s._fetch_listing(page=2)
        assert "GP=2" in captured[0]
    finally:
        s.close()


def test_listing_params_yaml_override(monkeypatch):
    captured: list[str] = []

    class FakeResp:
        status_code = 200
        text = "<html></html>"

    cfg = {"listing_params": {"resultsPerPage": 50, "nn": "4641482"}}
    s = BundScraper(base_url=BASE, name="bund.de", config=cfg)
    try:
        s._robots = type("R", (), {"can_fetch": lambda *a: True})()
        monkeypatch.setattr(s, "get", lambda url: captured.append(url) or FakeResp())
        s._fetch_listing(page=1)
        u = urlsplit(captured[0])
        params = parse_qs(u.query)
        assert params["resultsPerPage"] == ["50"]
        assert params["nn"] == ["4641482"]
    finally:
        s.close()


# ---------------------------------------------------------------------------
def test_parser_prefers_anzeige_link_over_first_link():
    """In modernen Teasern liegt der Titel-Link nach einem 'merken'-Icon -
    wir sollen NICHT auf 'merken' landen."""
    html = """
    <li class="standard-teaser">
      <a href="#merken" class="teaser-action">merken</a>
      <a href="/Content/DE/Ausschreibungen/Anzeige/4711.html">
         Verfüllung mit Flüssigboden Magdeburg
      </a>
      <p>Vergabestelle: Stadt Magdeburg | Ort: Magdeburg | Frist: 01.07.2026</p>
    </li>
    """
    items = BundScraper.parse_search_html(html, base_url=BASE)
    assert len(items) == 1
    item = next(iter(items.values()))
    assert item.url.endswith("/Anzeige/4711.html")
    assert "Flüssigboden" in item.title
    assert "merken" not in item.title.lower()


def test_parser_fallback_when_no_teaser_class():
    """Layout ohne erkennbare Teaser-Klasse - wir suchen Anzeige-Links und
    nehmen deren naechsten Container als Treffer-Karte."""
    html = """
    <html><body>
    <main>
      <div class="result-row">
        <a href="/Content/DE/Ausschreibungen/Anzeige/8001.html">
           ZFSV-Verfüllung Leitungsgraben Berlin</a>
        <span>Vergabestelle: Senat Berlin | Ort: Berlin | Frist: 15.06.2026</span>
      </div>
      <div class="result-row">
        <a href="/Content/DE/Ausschreibungen/Anzeige/8002.html">
           Tiefbau Hauptstraße Potsdam</a>
        <span>Vergabestelle: Stadt Potsdam | Ort: Potsdam | Frist: 20.06.2026</span>
      </div>
    </main>
    </body></html>
    """
    items = BundScraper.parse_search_html(html, base_url=BASE)
    assert len(items) == 2
    urls = {it.url for it in items.values()}
    assert any("/Anzeige/8001" in u for u in urls)
    assert any("/Anzeige/8002" in u for u in urls)


def test_parser_skips_junk_links():
    """'merken' / 'drucken' / Pagination-Links ohne Anzeige-Pfad raus."""
    html = """
    <ul>
      <li class="standard-teaser">
        <a href="#print">drucken</a>
        <a href="?GP=2">weiter</a>
        <a href="/Content/DE/Ausschreibungen/Anzeige/9999.html">
           Sanierung Trinkwasserleitung mit Flüssigboden</a>
      </li>
    </ul>
    """
    items = BundScraper.parse_search_html(html, base_url=BASE)
    assert len(items) == 1
    item = next(iter(items.values()))
    assert "Trinkwasserleitung" in item.title


def test_parser_handles_empty_html():
    items = BundScraper.parse_search_html("<html></html>", base_url=BASE)
    assert items == {}


# ---------------------------------------------------------------------------
RSS_HTML = b"""<?xml version="1.0" encoding="utf-8"?>
<rss version="2.0">
  <channel>
    <title>service.bund.de - Ausschreibungen Bauleistungen</title>
    <item>
      <title>Verfuellung Leitungsgraben Magdeburg</title>
      <link>https://www.service.bund.de/Content/DE/Ausschreibungen/Anzeige/4711.html</link>
      <description>Vergabestelle: Stadt Magdeburg | Ort: Magdeburg, Sachsen-Anhalt | Frist: 12.06.2026</description>
      <pubDate>Mon, 01 May 2026 10:00:00 +0200</pubDate>
    </item>
    <item>
      <title>Tiefbauarbeiten B5 Berlin</title>
      <link>https://www.service.bund.de/Content/DE/Ausschreibungen/Anzeige/9999.html</link>
      <description>Vergabestelle: Senat Berlin | Ort: Berlin | Frist: 30.06.2026</description>
      <pubDate>Mon, 01 May 2026 09:00:00 +0200</pubDate>
    </item>
  </channel>
</rss>
"""


def test_parse_rss_extracts_items():
    items = BundScraper.parse_rss(
        RSS_HTML, base_url=BASE, portal_name="bund.de Service-Portal",
    )
    assert len(items) == 2
    magde = next(it for it in items.values() if "Magdeburg" in it.title)
    assert magde.url.endswith("/Anzeige/4711.html")
    assert magde.contracting_authority == "Stadt Magdeburg"
    assert "Magdeburg" in magde.location
    assert magde.region == "Sachsen-Anhalt"
    assert magde.deadline is not None
    assert magde.deadline.year == 2026
    assert magde.deadline.month == 6
    assert magde.deadline.day == 12
    assert magde.publication_date is not None
    assert magde.portal == "bund.de Service-Portal"


def test_looks_like_rss_via_content_type():
    from scrapers.bund import _looks_like_rss

    class R:
        headers = {"content-type": "application/rss+xml; charset=utf-8"}
        content = b"<?xml..."
    assert _looks_like_rss(R) is True


def test_looks_like_rss_via_body_sniff():
    from scrapers.bund import _looks_like_rss

    class R:
        headers = {"content-type": "text/plain"}
        content = b'<?xml version="1.0"?><rss>...'
    assert _looks_like_rss(R) is True


def test_looks_like_rss_returns_false_for_html():
    from scrapers.bund import _looks_like_rss

    class R:
        headers = {"content-type": "text/html; charset=utf-8"}
        content = b"<!DOCTYPE html><html>..."
    assert _looks_like_rss(R) is False


def test_fetch_listing_routes_rss_to_rss_parser(monkeypatch):
    """Wenn der Server RSS zurueckgibt (jobsrss=true), wird parse_rss genutzt
    und nicht der HTML-Parser."""

    class FakeResp:
        status_code = 200
        headers = {"content-type": "application/rss+xml"}
        content = RSS_HTML
        text = RSS_HTML.decode("utf-8")

    s = BundScraper(base_url=BASE, name="bund.de Service-Portal", config={})
    try:
        s._robots = type("R", (), {"can_fetch": lambda *a: True})()
        monkeypatch.setattr(s, "get", lambda url: FakeResp())
        items = s._fetch_listing(page=1)
        assert len(items) == 2
        # Echter RSS-pubDate-Parse wirkt
        any_pub = next(iter(items.values())).publication_date
        assert any_pub is not None
    finally:
        s.close()


def test_filter_by_terms_excludes_off_topic(monkeypatch):
    """Listing liefert 3 Items, nur 1 enthaelt einen Cluster-Begriff -
    nach client-Filter bleibt 1 uebrig."""
    html = """
    <li class="standard-teaser">
      <a href="/Content/DE/Ausschreibungen/Anzeige/1.html">Verfüllung mit Flüssigboden Magdeburg</a>
      <p>Stadt Magdeburg</p>
    </li>
    <li class="standard-teaser">
      <a href="/Content/DE/Ausschreibungen/Anzeige/2.html">Reinigung Bürogebäude Köln</a>
      <p>Stadt Köln</p>
    </li>
    <li class="standard-teaser">
      <a href="/Content/DE/Ausschreibungen/Anzeige/3.html">Catering für Kantine</a>
      <p>Bundesamt</p>
    </li>
    """
    class FakeResp:
        status_code = 200
        text = html

    cfg = {"max_pages": 1, "enable_keyword_search": False,
           "match_terms": ["Flüssigboden", "ZFSV"], "filter_by_terms": True}
    s = BundScraper(base_url=BASE, name="bund.de", config=cfg)
    try:
        s._robots = type("R", (), {"can_fetch": lambda *a: True})()
        monkeypatch.setattr(s, "get", lambda url: FakeResp())
        items = s.fetch(["Flüssigboden"])
        assert len(items) == 1
        assert "Flüssigboden" in items[0].title
    finally:
        s.close()
