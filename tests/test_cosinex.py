"""Tests fuer den Cosinex VMP Satellite/Center-Scraper - ohne Netzwerk."""
from __future__ import annotations

from scrapers.cosinex import CosinexScraper


SATELLITE_LISTING_HTML = """
<html><body>
<table class="publications">
  <tbody>
    <tr class="publication">
      <td><a href="/VMPSatellite/notice/CXP4YDLXC1BSHE3X">
          Tiefbauarbeiten - Verfüllung mit Flüssigboden Potsdam</a></td>
      <td>Vergabestelle: Stadt Potsdam | Ort: Potsdam | Frist: 12.06.2026</td>
    </tr>
    <tr class="publication">
      <td><a href="/VMPSatellite/notice/CXP4YDLXC1BSHE3Y">
          Reinigung Verwaltungsgebaeude</a></td>
      <td>Vergabestelle: Land Brandenburg | Ort: Cottbus | Frist: 30.05.2026</td>
    </tr>
  </tbody>
</table>
</body></html>
"""

CENTER_LISTING_HTML = """
<html><body>
<ul class="notice-list">
  <li class="notice">
    <a href="/VMPCenter/notice/CXS4YA12345678">
       Sanierung Fernwärmetrasse - ZFSV-Verfüllung Düsseldorf</a>
    Vergabestelle: Stadt Düsseldorf | Ort: Düsseldorf |
    Angebotsfrist: 18.06.2026
  </li>
</ul>
</body></html>
"""

# Layout-Variante ohne Tabelle - Fallback ueber Detail-Links.
LINK_ONLY_HTML = """
<html><body>
<div class="content">
  <p><a href="/VMPCenter/notice/CXP4Y9999AABBCC">
       Glasfaserausbau Aachen</a></p>
  <p><a href="/static/help.html">Hilfe</a></p>
  <p><a href="javascript:void(0)">Inaktiv</a></p>
</div>
</body></html>
"""

EMPTY_HTML = "<html><body><p>Keine Bekanntmachungen gefunden.</p></body></html>"

BASE_SAT = "https://vergabemarktplatz.brandenburg.de"
BASE_CENTER = "https://www.evergabe.nrw.de"


def test_parse_satellite_listing():
    items = CosinexScraper.parse_listing(
        SATELLITE_LISTING_HTML, base_url=BASE_SAT, portal_name="brandenburg",
    )
    assert len(items) == 2
    fluessig = next(it for it in items.values() if "Flüssigboden" in it.title)
    assert fluessig.url.startswith(BASE_SAT + "/VMPSatellite/notice/CXP4Y")
    assert fluessig.contracting_authority == "Stadt Potsdam"
    assert fluessig.location == "Potsdam"
    assert fluessig.deadline is not None
    assert fluessig.deadline.year == 2026
    assert fluessig.deadline.month == 6
    assert fluessig.deadline.day == 12


def test_parse_center_listing():
    items = CosinexScraper.parse_listing(
        CENTER_LISTING_HTML, base_url=BASE_CENTER, portal_name="nrw",
    )
    assert len(items) == 1
    item = next(iter(items.values()))
    assert "Fernwärmetrasse" in item.title
    assert "/VMPCenter/notice/" in item.url
    assert item.contracting_authority == "Stadt Düsseldorf"
    assert item.deadline is not None
    assert item.deadline.day == 18


def test_parse_link_fallback_skips_non_notices():
    items = CosinexScraper.parse_listing(
        LINK_ONLY_HTML, base_url=BASE_CENTER, portal_name="nrw",
    )
    assert len(items) == 1
    item = next(iter(items.values()))
    assert "Glasfaser" in item.title
    titles = {it.title for it in items.values()}
    assert "Hilfe" not in titles
    assert "Inaktiv" not in titles


def test_parse_empty_listing():
    items = CosinexScraper.parse_listing(
        EMPTY_HTML, base_url=BASE_SAT, portal_name="brandenburg",
    )
    assert items == {}


def test_listing_paths_variant_resolution():
    sat = CosinexScraper(base_url=BASE_SAT, name="x", config={"variant": "satellite"})
    try:
        paths = sat._listing_paths()
        assert any("VMPSatellite" in p for p in paths)
        assert all("VMPCenter" not in p for p in paths)
    finally:
        sat.close()

    center = CosinexScraper(base_url=BASE_CENTER, name="x", config={"variant": "center"})
    try:
        paths = center._listing_paths()
        assert any("VMPCenter" in p for p in paths)
        assert all("VMPSatellite" not in p for p in paths)
    finally:
        center.close()


def test_listing_paths_explicit_override():
    s = CosinexScraper(
        base_url=BASE_SAT, name="x",
        config={"listing_paths": ["/custom/notice"]},
    )
    try:
        assert s._listing_paths() == ["/custom/notice"]
    finally:
        s.close()


def test_browser_user_agent_default():
    s = CosinexScraper(base_url=BASE_SAT, name="x", config={})
    try:
        ua = s._client.headers.get("User-Agent")
        assert "Mozilla" in ua
        assert "Firefox" in ua
    finally:
        s.close()


def test_user_agent_override():
    s = CosinexScraper(
        base_url=BASE_SAT, name="x",
        config={"user_agent": "MyCustomUA/1.0"},
    )
    try:
        assert s._client.headers.get("User-Agent") == "MyCustomUA/1.0"
    finally:
        s.close()
