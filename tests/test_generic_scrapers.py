"""Tests fuer rss_generic und generic_html – ohne Netzwerk."""
from __future__ import annotations

from scrapers.rss_generic import RssGenericScraper
from scrapers.generic_html import GenericHtmlScraper


RSS_FIXTURE = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0">
  <channel>
    <title>bi-medien Ausschreibungen</title>
    <link>https://example.org</link>
    <item>
      <title>Verfüllung Leitungsgraben mit Flüssigboden Magdeburg</title>
      <link>https://example.org/aus/123</link>
      <description>ZFSV nach RAL GZ 507, Stadt Magdeburg, Frist 12.06.2026</description>
      <pubDate>Mon, 27 Apr 2026 09:00:00 +0200</pubDate>
    </item>
    <item>
      <title>Sanierung Spielplatz</title>
      <link>https://example.org/aus/124</link>
      <description>Reine Spielgeraete, kein Tiefbau</description>
      <pubDate>Mon, 27 Apr 2026 09:30:00 +0200</pubDate>
    </item>
  </channel>
</rss>
""".encode("utf-8")


def test_rss_filter_by_terms_keeps_only_matches():
    items = RssGenericScraper.parse_feed(
        RSS_FIXTURE,
        portal_name="bi-medien",
        terms_l=["flüssigboden", "zfsv"],
        filter_by_terms=True,
    )
    assert len(items) == 1
    item = next(iter(items.values()))
    assert "Flüssigboden" in item.title
    assert item.publication_date is not None


def test_rss_no_filter_returns_all():
    items = RssGenericScraper.parse_feed(
        RSS_FIXTURE,
        portal_name="bi-medien",
        terms_l=[],
        filter_by_terms=False,
    )
    assert len(items) == 2


HTML_FIXTURE = """
<html><body>
<ul>
  <li class="result">
    <a class="title" href="/notice/abc-1">Tiefbau Verfüllung mit ZFSV Sachsen</a>
    <span class="buyer">Stadt Dresden</span>
    <span class="place">Dresden</span>
    <span class="deadline">Angebotsfrist: 30.06.2026</span>
    <span class="excerpt">Lieferung und Einbau von ZFSV.</span>
  </li>
  <li class="result">
    <a class="title" href="https://other.example/notice/abc-2">Strassenbau Ortsdurchfahrt</a>
    <span class="buyer">Gemeinde Y</span>
    <span class="place">Y</span>
    <span class="deadline">Frist: 01.07.2026</span>
  </li>
</ul>
</body></html>
"""


def test_generic_html_parser_extracts_records():
    cfg = {
        "result_selector": "li.result",
        "title_selector": "a.title",
        "authority_selector": ".buyer",
        "location_selector": ".place",
        "deadline_selector": ".deadline",
        "description_selector": ".excerpt",
    }
    items = GenericHtmlScraper.parse_html(
        HTML_FIXTURE, base_url="https://example.org", portal_name="DemoPortal", config=cfg,
    )
    assert len(items) == 2
    first = next(i for i in items.values() if "ZFSV" in i.title)
    assert first.contracting_authority == "Stadt Dresden"
    assert first.location == "Dresden"
    assert first.deadline is not None
    assert first.deadline.year == 2026 and first.deadline.month == 6
    # Relativer Link wurde absolut aufgeloest:
    assert first.url.startswith("https://example.org/")
    # Externer Link blieb absolut:
    other = next(i for i in items.values() if "Strassenbau" in i.title)
    assert other.url.startswith("https://other.example/")
