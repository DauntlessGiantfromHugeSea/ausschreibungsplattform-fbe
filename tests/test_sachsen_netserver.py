"""Realistischer NetServer-HTML-Test fuer den Sachsen-Parser.

NetServer (AI Informatics) rendert Detail-Links als JavaScript-onclick:
    onclick="OnPublicationClicked('PublicationID','12345ABC')"
und nicht als klassisches <a href>. Mein erster Parser hat das verfehlt
(0 Treffer trotz 215KB Antwort) - dieser Test sichert den Fix ab.
"""
from __future__ import annotations

from scrapers.sachsen import SachsenScraper


# Synthetisches NetServer-Listing. PublicationID wird in JS-onclick + in
# einem Hidden-Form-Feld gespeichert - genauso wie in der echten Antwort.
NETSERVER_HTML = """
<html><body>
<table class="searchResultTable">
  <tr class="searchResultRow"
      onclick="OnPublicationClicked('PublicationID','45678ABC')">
    <td>
      <span>Verfuellung Leitungsgraben Dresden mit Fluessigboden</span>
    </td>
    <td>Stadt Dresden</td>
    <td>12.06.2026</td>
  </tr>
  <tr class="searchResultRow"
      onclick="OnPublicationClicked('PublicationID','99887DEF')">
    <td><span>Tiefbauarbeiten Innenstadt Leipzig</span></td>
    <td>Stadt Leipzig</td>
    <td>30.05.2026</td>
  </tr>
</table>
<form method="post" action="/NetServer/PublicationControllerServlet">
  <input type="hidden" name="PublicationID" value="11122GHI">
  <input type="hidden" name="function" value="DisplayPublication">
</form>
<a class="resultLink" data-publication-id="55566JKL"
   onclick="OnPublicationClicked('PublicationID','55566JKL')">
  Glasfaserausbau Goerlitz
</a>
</body></html>
"""

NETSERVER_NO_RESULTS_HTML = """
<html><body>
<p>Es wurden keine Bekanntmachungen gefunden.</p>
<table class="searchResultTable"><tr><td>Keine Eintraege</td></tr></table>
</body></html>
"""


BASE = "https://www.evergabe.sachsen.de"


def test_extracts_publication_ids_from_js_onclick():
    items = SachsenScraper.parse_listing(
        NETSERVER_HTML, base_url=BASE, portal_name="Vergabe Sachsen",
    )
    # Mindestens die 4 IDs aus onclick + hidden-input + data-attr
    pids = {it.url.split("PublicationID=")[-1] for it in items.values()}
    assert "45678ABC" in pids
    assert "99887DEF" in pids
    assert "11122GHI" in pids
    assert "55566JKL" in pids
    assert len(items) >= 4


def test_extracts_titles_near_publication_ids():
    items = SachsenScraper.parse_listing(
        NETSERVER_HTML, base_url=BASE, portal_name="Vergabe Sachsen",
    )
    # Titel sollten aus dem umgebenden <tr> kommen
    titles = " ".join(it.title for it in items.values())
    assert "Fluessigboden" in titles or "Leitungsgraben" in titles
    assert "Tiefbau" in titles or "Innenstadt" in titles


def test_urls_are_absolute_and_contain_netserver_path():
    items = SachsenScraper.parse_listing(
        NETSERVER_HTML, base_url=BASE, portal_name="Vergabe Sachsen",
    )
    for it in items.values():
        assert it.url.startswith(BASE + "/NetServer/PublicationControllerServlet")
        assert "function=DisplayPublication" in it.url
        assert "PublicationID=" in it.url


def test_no_results_page_returns_empty():
    items = SachsenScraper.parse_listing(
        NETSERVER_NO_RESULTS_HTML, base_url=BASE, portal_name="Vergabe Sachsen",
    )
    assert items == {}
