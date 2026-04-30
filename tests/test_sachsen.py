"""Tests fuer den Sachsen / NetServer-Scraper - ohne Netzwerk."""
from __future__ import annotations

from scrapers.sachsen import SachsenScraper


LISTING_HTML = """
<html><body>
<table class="searchResults">
  <tbody>
    <tr class="publication">
      <td><a href="PublicationControllerServlet?function=ShowPublication&PublicationID=4711">
          Verfüllung Leitungsgraben mit Flüssigboden, Dresden</a></td>
      <td>Vergabestelle: Stadt Dresden | Erfüllungsort: Dresden, 01069 |
          Angebotsfrist: 12.06.2026 | Veröffentlichung: 01.05.2026</td>
    </tr>
    <tr class="publication">
      <td><a href="PublicationControllerServlet?function=ShowPublication&PublicationID=4712">
          Reinigung Verwaltungsgebäude Leipzig</a></td>
      <td>Vergabestelle: Stadt Leipzig | Erfüllungsort: Leipzig |
          Angebotsfrist: 30.05.2026</td>
    </tr>
    <tr class="publication">
      <td><a href="PublicationControllerServlet?function=ShowPublication&PublicationID=4713">
          Tiefbauarbeiten Fernwärmetrasse Chemnitz</a></td>
      <td>Vergabestelle: Stadtwerke Chemnitz | Erfüllungsort: Chemnitz |
          Angebotsfrist: 18.06.2026</td>
    </tr>
  </tbody>
</table>
</body></html>
"""

EMPTY_HTML = """
<html><body>
<table class="searchResults"><tbody></tbody></table>
<p>Es wurden keine Bekanntmachungen gefunden.</p>
</body></html>
"""

# Layout-Variante ohne erkennbare Tabelle - Fallback ueber Detail-Links.
LINK_ONLY_HTML = """
<html><body>
<div class="content">
  <p><a href="/NetServer/PublicationControllerServlet?function=ShowPublication&PublicationID=8001">
       Sanierung Trinkwasserleitung mit ZFSV - Bautzen</a></p>
  <p><a href="/NetServer/PublicationControllerServlet?function=ShowPublication&PublicationID=8002">
       Glasfaserausbau Goerlitz</a></p>
  <p><a href="/static/help.html">Hilfe</a></p>
</div>
</body></html>
"""


BASE = "https://www.evergabe.sachsen.de"


def test_parse_listing_extracts_rows():
    items = SachsenScraper.parse_listing(
        LISTING_HTML, base_url=BASE, portal_name="evergabe.sachsen.de",
    )
    assert len(items) == 3
    fluessig = next(it for it in items.values() if "Flüssigboden" in it.title)
    assert fluessig.url.startswith(f"{BASE}/")
    assert "PublicationID=4711" in fluessig.url
    assert fluessig.contracting_authority == "Stadt Dresden"
    assert fluessig.location and "Dresden" in fluessig.location
    assert fluessig.deadline is not None
    assert fluessig.deadline.year == 2026
    assert fluessig.deadline.month == 6
    assert fluessig.deadline.day == 12
    assert fluessig.publication_date is not None
    assert fluessig.publication_date.day == 1


def test_parse_listing_empty():
    items = SachsenScraper.parse_listing(
        EMPTY_HTML, base_url=BASE, portal_name="evergabe.sachsen.de",
    )
    assert items == {}


def test_parse_listing_link_fallback():
    items = SachsenScraper.parse_listing(
        LINK_ONLY_HTML, base_url=BASE, portal_name="evergabe.sachsen.de",
    )
    # Mindestens die zwei Publication-Links - die Hilfe-Seite nicht.
    assert len(items) >= 2
    assert all("Publication" in it.url for it in items.values())
    titles = {it.title for it in items.values()}
    assert any("Trinkwasserleitung" in t for t in titles)
    assert not any("Hilfe" in t for t in titles)


def test_search_url_with_term_and_page():
    s = SachsenScraper(base_url=BASE, name="evergabe.sachsen.de", config={})
    try:
        url1 = s._search_url(
            "/NetServer/PublicationSearchControllerServlet"
            "?function=SearchPublications&Category=InvitationToTender",
            term="Flüssigboden", page=1,
        )
        assert "Search.SearchPattern=Fl%C3%BCssigboden" in url1
        assert "Page=" not in url1

        url2 = s._search_url(
            "/NetServer/PublicationSearchControllerServlet"
            "?function=SearchPublications&Category=InvitationToTender",
            term="Tiefbau", page=2,
        )
        assert "Search.SearchPattern=Tiefbau" in url2
        assert "Page=2" in url2

        url3 = s._search_url(
            "/NetServer/PublicationSearchControllerServlet"
            "?function=SearchPublications&Category=InvitationToTender",
            term=None, page=1,
        )
        assert "Search.SearchPattern" not in url3
        assert "Page=" not in url3
    finally:
        s.close()
