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


# Card-Layout im evergabe.de-Stil: Felder als 'Label: Wert' im Karten-Volltext,
# kein klickbarer Detail-Link (Login erforderlich).
EVERGABE_CARD_FIXTURE = """
<html><body>
<div>
  <article class="tender-card">
    <h2>Bahnhof Markranstädt Bauleistungen zur Herstellung eines Aufzugschachtes auf Mittelbahnsteig</h2>
    <p>bauzeitlichen Rückbau des Bahnsteigdachs, Baugrubensicherung mit einer Wand aus Spundwand mit Verfüllung der Baugrube</p>
    <span>Aufzüge/Rolltreppen</span>
    Angebotsfrist: 07.05.2026 08:00 Uhr
    Ausführungsort: 04420 Markranstädt
    Auftraggeber: Nach Freischalten sichtbar
    Leistungszeit: Nach Freischalten sichtbar
    Auftragsart: Privatrecht (BGB)
  </article>

  <article class="tender-card">
    <h2>Reinigung Bürogebäude</h2>
    <p>Allgemeine Reinigungsleistungen für ein Verwaltungsgebäude.</p>
    Angebotsfrist: 30.06.2026
    Ausführungsort: 10115 Berlin
    Auftraggeber: Nach Freischalten sichtbar
  </article>
</div>
</body></html>
"""


def test_listing_extracts_via_labels_and_skips_hidden_fields():
    cfg = {
        "result_selector": "article.tender-card",
        "title_selector": "h2",
    }
    items = GenericHtmlScraper.parse_html(
        EVERGABE_CARD_FIXTURE,
        base_url="https://www.evergabe.de",
        portal_name="evergabe.de",
        config=cfg,
    )
    assert len(items) == 2
    bahn = next(i for i in items.values() if "Bahnhof" in i.title)
    # Label-Extraktion zieht Frist + Ort aus dem Volltext
    assert bahn.deadline is not None
    assert bahn.deadline.year == 2026 and bahn.deadline.month == 5 and bahn.deadline.day == 7
    assert bahn.location and "Markranstädt" in bahn.location
    # Hidden-Werte ("Nach Freischalten sichtbar") muessen rausgefiltert werden
    assert bahn.contracting_authority is None
    # URL ist synthetisch (kein <a>) aber stabil
    assert bahn.url.startswith("https://www.evergabe.de#card-")


def test_listing_filter_by_terms_keeps_only_matches():
    cfg = {
        "result_selector": "article.tender-card",
        "title_selector": "h2",
        "filter_by_terms": True,
    }
    items = GenericHtmlScraper.parse_html(
        EVERGABE_CARD_FIXTURE,
        base_url="https://www.evergabe.de",
        portal_name="evergabe.de",
        config=cfg,
    )
    # Wir filtern nicht in parse_html selbst (das macht fetch), aber wir
    # pruefen die helper-Funktion separat:
    from scrapers.generic_html import _matches_any
    bahn = next(i for i in items.values() if "Bahnhof" in i.title)
    reinigung = next(i for i in items.values() if "Reinigung" in i.title)
    assert _matches_any(bahn, ["Baugrube", "Verfüllung"]) is True
    assert _matches_any(reinigung, ["Baugrube", "Verfüllung"]) is False
