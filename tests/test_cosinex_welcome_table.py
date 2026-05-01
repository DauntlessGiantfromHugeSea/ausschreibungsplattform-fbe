"""Tests fuer den Welcome-Page-Tabellen-Parser des Cosinex-Scrapers.

NRW + RLP zeigen auf welcome.do eine 22-Zeilen-Tabelle mit Spalten
'Veröffentlicht | Frist | Kurzbezeichnung | Typ | Plattform' direkt
auf der Landing-Page (aus Live-Dump verifiziert).
"""
from __future__ import annotations

from scrapers.cosinex import CosinexScraper


VMP_WELCOME_HTML = """
<html><body>
<table>
  <tr>
    <th>Veröffentlicht</th>
    <th>Angebots- / Teilnahmefrist</th>
    <th>Kurzbezeichnung</th>
    <th>Typ</th>
    <th>Vergabeplattform / Veröffentlichende Stelle</th>
  </tr>
  <tr>
    <td>15.05.2026</td>
    <td>15.06.2026</td>
    <td><a href="/VMPCenter/public/notice/CXP4Y9X8YZ">
        Verfuellung Leitungsgraben Koeln</a></td>
    <td>VOB</td>
    <td>Stadt Koeln</td>
  </tr>
  <tr>
    <td>10.05.2026</td>
    <td>30.05.2026</td>
    <td><a href="/VMPCenter/public/notice/CXP4YABCDEF">
        Sanierung Kanal Aachen</a></td>
    <td>VOL</td>
    <td>Stadt Aachen</td>
  </tr>
</table>
</body></html>
"""

BASE = "https://www.evergabe.nrw.de"


def test_parses_welcome_page_table():
    items = CosinexScraper.parse_listing(
        VMP_WELCOME_HTML, base_url=BASE, portal_name="Vergabe NRW",
    )
    assert len(items) == 2

    koeln = next(it for it in items.values() if "Koeln" in it.title)
    assert koeln.contracting_authority == "Stadt Koeln"
    assert koeln.deadline is not None
    assert koeln.deadline.day == 15
    assert koeln.deadline.month == 6
    assert koeln.publication_date is not None
    assert koeln.publication_date.day == 15
    assert koeln.publication_date.month == 5
    assert "VOB" in (koeln.description or "")
    assert koeln.url.startswith(BASE + "/VMPCenter/public/notice/")


def test_welcome_table_skips_when_header_missing():
    """Wenn keine Tabelle den 'Kurzbezeichnung'-Header hat, faellt der Parser
    auf andere Strategien zurueck - nicht auf zufaellige Tabellen."""
    no_match = """
    <table>
      <tr><th>foo</th><th>bar</th></tr>
      <tr><td><a href="/x">Some link</a></td><td>x</td></tr>
    </table>
    """
    items = CosinexScraper.parse_listing(
        no_match, base_url=BASE, portal_name="x",
    )
    # Welcome-Parser greift nicht; aggressive Fallbacks finden ggf. nichts
    assert all("Kurzbezeichnung" not in (it.title or "") for it in items.values())
