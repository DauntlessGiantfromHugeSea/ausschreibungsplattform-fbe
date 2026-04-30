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
