"""Tests fuer den dedizierten evergabe.de-Scraper.

Anchor-zentriert: jeder /auftrag/-Link ist eine Card. Wir wandern hoch
zum Eltern-Container mit h2/h3 als Card-Wrapper.
"""
from __future__ import annotations

from scrapers.evergabe_de import EvergabeDeScraper


BASE = "https://www.evergabe.de"


# Echtes Tailwind4-Markup wie aus dem User-Screenshot
TAILWIND4_HTML = """
<html><body>
<div class="tw:flex tw:flex-col tw:gap-md">
  <div class="tw:flex tw:flex-col tw:gap-lg tw:pb-sm tw:px-sm">
    <a class="tw:outline-hidden tw:after:absolute" href="/auftrag/12345"></a>
    <h2>Landschaftsbau / Außenanlagen in 02694 Malschwitz OT Dubrauke</h2>
    <p>als gewerblicher Auftrag werden Leistungen zu Pflaster und Fugenarbeiten zum Bauvorhaben...</p>
    <span>Angebotsfrist: 04.05.2026 12:00 Uhr</span>
  </div>
  <div class="tw:flex tw:flex-col tw:gap-lg tw:pb-sm tw:px-sm">
    <a class="tw:outline-hidden tw:after:absolute" href="/auftrag/22222"></a>
    <h2>Tiefbauarbeiten Erfurt mit ZFSV-Verfüllung</h2>
    <p>Leitungsgrabenverfüllung gemäß DIN 18300</p>
    <span>Angebotsfrist: 15.05.2026</span>
  </div>
  <div class="tw:flex tw:flex-col tw:gap-lg tw:pb-sm tw:px-sm">
    <a class="tw:outline-hidden tw:after:absolute" href="/auftrag/33333"></a>
    <h2>Spundwandarbeiten Hafen 67435 Neustadt</h2>
    <p>Spundwand und Verbau für Hafenmodernisierung.</p>
  </div>
</div>
</body></html>
"""


# Layout ohne Tailwind (Fallback-Test - falls evergabe.de mal wieder
# auf SSR umstellt oder etwas Eigenes macht)
GENERIC_HTML = """
<html><body>
<main>
  <article>
    <h2><a href="/auftrag/777">Verfüllung Leitungsgraben</a></h2>
    <p>ZFSV-Einbau in 12345 Berlin</p>
  </article>
  <article>
    <h2>Tiefbau Innenstadt</h2>
    <p>DIN 18300 nach Vergabeordnung</p>
    <a href="/auftrag/888">Details ansehen</a>
  </article>
  <a href="/static/main.css">style</a>
  <a href="/help">Hilfe</a>
</main>
</body></html>
"""


def test_parses_tailwind4_cards():
    items = EvergabeDeScraper.parse_rendered_html(
        TAILWIND4_HTML, base_url=BASE, portal_name="evergabe.de",
    )
    assert len(items) == 3
    titles = [it.title for it in items.values()]
    assert any("Landschaftsbau" in t for t in titles)
    assert any("Tiefbauarbeiten Erfurt" in t for t in titles)
    assert any("Spundwandarbeiten" in t for t in titles)


def test_extracts_urls_correctly():
    items = EvergabeDeScraper.parse_rendered_html(
        TAILWIND4_HTML, BASE, "x",
    )
    urls = list(items.keys())
    assert all(u.startswith(BASE + "/auftrag/") for u in urls)
    assert any(u.endswith("/12345") for u in urls)
    assert any(u.endswith("/22222") for u in urls)
    assert any(u.endswith("/33333") for u in urls)


def test_extracts_deadline_from_angebotsfrist():
    items = EvergabeDeScraper.parse_rendered_html(
        TAILWIND4_HTML, BASE, "x",
    )
    landschaft = next(it for it in items.values() if "Landschaftsbau" in it.title)
    assert landschaft.deadline is not None
    assert landschaft.deadline.year == 2026
    assert landschaft.deadline.month == 5
    assert landschaft.deadline.day == 4


def test_extracts_location_from_plz_in_title():
    items = EvergabeDeScraper.parse_rendered_html(
        TAILWIND4_HTML, BASE, "x",
    )
    landschaft = next(it for it in items.values() if "Landschaftsbau" in it.title)
    assert landschaft.location is not None
    assert "02694" in landschaft.location
    spundwand = next(it for it in items.values() if "Spundwand" in it.title)
    assert "67435" in (spundwand.location or "")


def test_dedup_per_url():
    """Wenn dieselbe /auftrag/-URL mehrfach im DOM steht (z.B. zwei
    Anchors in einer Card), bekommen wir nur eine TenderItem zurueck."""
    html = """
    <div>
      <a href="/auftrag/SAME"></a>
      <h2>Gleiche Card</h2>
      <a href="/auftrag/SAME">noch ein Link</a>
    </div>
    """
    items = EvergabeDeScraper.parse_rendered_html(html, BASE, "x")
    assert len(items) == 1


def test_skips_anchors_without_h2_in_ancestors():
    """/auftrag/-Anchor ohne h2/h3 im Eltern-Pfad wird ignoriert."""
    html = """
    <body>
      <a href="/auftrag/no-card">Steht nackt im Body</a>
    </body>
    """
    items = EvergabeDeScraper.parse_rendered_html(html, BASE, "x")
    assert len(items) == 0


def test_fallback_when_anchor_has_text_but_no_h2():
    """Wenn Anchor selbst sichtbaren Text hat und ein h2 ueber ihm liegt,
    wird der Card-Container gefunden."""
    items = EvergabeDeScraper.parse_rendered_html(
        GENERIC_HTML, BASE, "x",
    )
    assert len(items) == 2
    titles = [it.title for it in items.values()]
    assert any("Verfüllung" in t for t in titles)
    assert any("Tiefbau Innenstadt" in t for t in titles)
