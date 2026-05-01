"""Tests fuer den Crawl-Scraper - ohne Netzwerk via Fixtures."""
from __future__ import annotations

from scrapers.crawl_html import CrawlHtmlScraper


LISTING_HTML = """
<html><body>
  <main>
    <ul class="notices">
      <li><a href="/auftrag/1001">Strassenbau Ortsdurchfahrt</a></li>
      <li><a href="/auftrag/1002">Verfuellung Leitungsgraben - Fluessigboden</a></li>
      <li><a href="/auftrag/1003">Reinigung Buerogebaeude</a></li>
      <li><a href="/anderes/page">Nicht-Auftrag</a></li>
      <li><a href="javascript:void(0)">Skip</a></li>
    </ul>
  </main>
</body></html>
"""

DETAIL_FLUESSIG = """
<html><body>
  <h1>Verfüllung Leitungsgraben - Flüssigboden Magdeburg</h1>
  <span class="vergabestelle">Stadt Magdeburg</span>
  <span class="ort">Magdeburg, Sachsen-Anhalt</span>
  <span class="frist">Frist: 12.06.2026</span>
  <p>Wir suchen Anbieter fuer die Verfüllung mit ZFSV gemaess RAL GZ 507 …</p>
</body></html>
"""

DETAIL_OHNE_KEYWORD = """
<html><body>
  <h1>Reinigung Buerogebaeude</h1>
  <span class="vergabestelle">Stadt X</span>
  <p>Allgemeine Reinigungsleistungen.</p>
</body></html>
"""


def test_extract_links_filters_pattern():
    cfg = {
        "link_selector": "a[href*='/auftrag/']",
        "link_pattern": r"/auftrag/[0-9]+",
    }
    links = CrawlHtmlScraper._extract_links(
        LISTING_HTML, base_url="https://example.org", config=cfg,
    )
    assert len(links) == 3
    assert all("/auftrag/" in u for u in links)
    assert all(u.startswith("https://example.org/") for u in links)


def test_aggressive_fallback_when_selector_misses():
    """Selektor passt nicht, aber Fallback findet Bekanntmachungs-Links."""
    html = """
    <html><body>
    <main>
      <table class="searchResults">
        <tr><td><a href="/notice.html?id=ABC1234">Bekanntmachung 1</a></td></tr>
        <tr><td><a href="/announcement/56789">Bekanntmachung 2</a></td></tr>
        <tr><td><a href="/static/style.css">Stylesheet</a></td></tr>
        <tr><td><a href="https://external.example/x">External</a></td></tr>
      </table>
    </main>
    </body></html>
    """
    cfg = {
        # absichtlich passt der Selektor NICHT auf die Anchor-Klassen
        "link_selector": "a.does-not-match",
    }
    links = CrawlHtmlScraper._extract_links(
        html, base_url="https://www.evergabe-online.de", config=cfg,
    )
    # Fallback findet die internen Bekanntmachungs-Links
    assert any("/notice.html?id=ABC1234" in u for u in links)
    assert any("/announcement/56789" in u for u in links)
    assert not any("style.css" in u for u in links)
    assert not any("external.example" in u for u in links)


def test_aggressive_fallback_can_be_disabled():
    html = """
    <a href="/notice.html?id=999">x</a>
    """
    cfg = {
        "link_selector": "a.does-not-match",
        "aggressive_fallback": False,
    }
    links = CrawlHtmlScraper._extract_links(
        html, base_url="https://www.evergabe-online.de", config=cfg,
    )
    assert links == []


def test_aggressive_fallback_skips_nav_and_footer_links():
    """Echtes Symptom aus dem Sachsen-Anhalt-Dump: footer-/menue-Links wie
    'Impressum', 'Datenschutz', 'Ministerium fuer Inneres und Sport' wurden
    als Tender erkannt, weil ihr Linktext lang genug war ODER ihr Pfad
    /vergabestellen/... enthielt. Der Nav-Context-Filter muss die abweisen."""
    html = """
    <html><body>
    <nav class="main-menu">
      <a href="/staatskanzlei-und-ministerium-fuer-kultur">
        Staatskanzlei und Ministerium fuer Kultur</a>
      <a href="/ministerium-der-finanzen">Ministerium der Finanzen</a>
    </nav>
    <header>
      <a href="/home">Home</a>
      <a href="/vergabestellen/informationen-fuer-vergabestellen">
        Informationen fuer Vergabestellen</a>
    </header>
    <footer class="footer">
      <a href="/impressum">Impressum</a>
      <a href="/datenschutz">Datenschutz</a>
      <a href="/barrierefreiheit">Erklaerung zur Barrierefreiheit</a>
    </footer>
    <main>
      <a href="/recherche-aktueller-vergaben/show?id=ABC123">
        Verfuellung Leitungsgraben Magdeburg mit Fluessigboden</a>
      <a href="/recherche-aktueller-vergaben/show?id=DEF456">
        Tiefbauarbeiten Hauptstrasse Halle</a>
    </main>
    </body></html>
    """
    cfg = {"link_selector": "a.does-not-match"}
    links = CrawlHtmlScraper._extract_links(
        html, base_url="https://www.evergabe.sachsen-anhalt.de", config=cfg,
    )
    # Nav/Header/Footer raus
    assert not any("staatskanzlei" in u.lower() for u in links)
    assert not any("/impressum" in u for u in links)
    assert not any("/datenschutz" in u for u in links)
    assert not any("barrierefreiheit" in u for u in links)
    assert not any("/home" in u for u in links)
    assert not any("/ministerium" in u for u in links)
    # Vergabestellen-Pfad ist Verwaltungs-Seite, nicht Tender
    assert not any("/vergabestellen" in u for u in links)
    # Echte Tender bleiben drin
    assert any("recherche-aktueller-vergaben/show?id=ABC123" in u for u in links)
    assert any("recherche-aktueller-vergaben/show?id=DEF456" in u for u in links)


def test_aggressive_fallback_accepts_long_titles_without_url_match():
    """Wenn der URL-Pfad nicht nach Bekanntmachung aussieht, aber der Linktext
    substantiell ist (>= 25 Zeichen), wird der Link trotzdem als Treffer
    akzeptiert. Deckt Portale ab, die seltsame URL-Strukturen verwenden
    aber sprechende Linktitel haben."""
    html = """
    <html><body>
    <main>
      <a href="/x/y/z?p=1">Verfuellung Leitungsgraben Ortsdurchfahrt Magdeburg</a>
      <a href="/x/y/z?p=2">Tiefbauarbeiten und Asphaltierung Hauptstrasse Berlin</a>
      <a href="/?menu=home">Startseite</a>
      <a href="/login">Login</a>
      <a href="/static/main.css">stylesheet-link-laenger-als-25-zeichen</a>
      <a href="https://external.example/x">Externer Link mit langem Titel</a>
    </main>
    </body></html>
    """
    cfg = {"link_selector": "a.does-not-match"}
    links = CrawlHtmlScraper._extract_links(
        html, base_url="https://example.de", config=cfg,
    )
    assert any("/x/y/z?p=1" in u for u in links)
    assert any("/x/y/z?p=2" in u for u in links)
    # Junk muss raus: kurze Titel, Login, Asset-Endung, externer Link
    assert not any("/?menu=home" in u for u in links)
    assert not any("/login" in u for u in links)
    assert not any("main.css" in u for u in links)
    assert not any("external.example" in u for u in links)


def test_parse_detail_keeps_only_matching():
    cfg = {
        "detail_title_selector": "h1",
        "detail_authority_selector": ".vergabestelle",
        "detail_location_selector": ".ort",
        "detail_deadline_selector": ".frist",
    }
    item = CrawlHtmlScraper._parse_detail(
        DETAIL_FLUESSIG,
        url="https://example.org/auftrag/1002",
        portal="evergabe.de",
        config=cfg,
        match_terms=["Flüssigboden", "ZFSV"],
    )
    assert item is not None
    assert "Flüssigboden" in item.title
    assert item.contracting_authority == "Stadt Magdeburg"
    assert item.deadline is not None and item.deadline.year == 2026


def test_parse_detail_skips_non_matching():
    item = CrawlHtmlScraper._parse_detail(
        DETAIL_OHNE_KEYWORD,
        url="https://example.org/auftrag/1003",
        portal="evergabe.de",
        config={"detail_title_selector": "h1"},
        match_terms=["Flüssigboden", "ZFSV"],
    )
    assert item is None


def test_extract_links_skips_anchors_and_js():
    cfg = {"link_selector": "a"}
    links = CrawlHtmlScraper._extract_links(
        '<a href="#anchor">x</a><a href="javascript:void(0)">y</a><a href="/real">z</a>',
        base_url="https://example.org",
        config=cfg,
    )
    assert links == ["https://example.org/real"]
