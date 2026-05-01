"""Tests fuer den NetServer-Tabellen-Parser auf der echten DOM-Struktur
(class .tender / .tenderAuthority / .tenderDeadline aus dem Live-Dump)."""
from __future__ import annotations

from scrapers.sachsen import SachsenScraper


# Echte NetServer-Struktur aus dem Live-Dump auf evergabe.sachsen.de
# (.tableHorizontalHeader mit tr.tableRow + .tender/.tenderAuthority/.tenderDeadline)
NETSERVER_REAL_HTML = """
<html><body>
<table class="table table-responsive table-striped table-hover tableHorizontalHeader">
  <thead>
    <tr><th>Erschienen am</th><th>Ausschreibung</th><th>Vergabestelle</th>
        <th>Verfahrensart</th><th>Frist</th></tr>
  </thead>
  <tbody>
    <tr class="tableRow clickable-row">
      <td>15.05.2026</td>
      <td class="tender">
        <a href="PublicationSearchControllerServlet?function=DisplayPublication&PublicationID=ABC123XY">
          Verfuellung Leitungsgraben Dresden mit Fluessigboden
        </a>
      </td>
      <td class="tenderAuthority">Stadt Dresden, Tiefbauamt</td>
      <td class="tenderType">VOB</td>
      <td class="tenderDeadline">12.06.2026</td>
    </tr>
    <tr class="tableRow clickable-row">
      <td>10.05.2026</td>
      <td class="tender">
        <a href="PublicationSearchControllerServlet?function=DisplayPublication&PublicationID=DEF456ZZ">
          Tiefbauarbeiten Innenstadt Leipzig
        </a>
      </td>
      <td class="tenderAuthority">Stadt Leipzig</td>
      <td class="tenderType">VOL</td>
      <td class="tenderDeadline">30.05.2026</td>
    </tr>
  </tbody>
</table>
</body></html>
"""

BASE = "https://www.evergabe.sachsen.de"


def test_parses_real_netserver_rows():
    items = SachsenScraper.parse_listing(
        NETSERVER_REAL_HTML, base_url=BASE, portal_name="Vergabe Sachsen",
    )
    assert len(items) == 2

    fluessig = next(it for it in items.values() if "Fluessigboden" in it.title)
    assert fluessig.contracting_authority == "Stadt Dresden, Tiefbauamt"
    assert fluessig.deadline is not None
    assert fluessig.deadline.year == 2026
    assert fluessig.deadline.month == 6
    assert fluessig.deadline.day == 12
    assert "PublicationID=ABC123XY" in fluessig.url
    # Relative URL gegen NetServer-Base aufgeloest
    assert fluessig.url.startswith(BASE + "/NetServer/")

    leipzig = next(it for it in items.values() if "Leipzig" in it.title)
    assert leipzig.contracting_authority == "Stadt Leipzig"
    assert leipzig.deadline.day == 30
    assert "VOL" in (leipzig.description or "")


def test_real_netserver_takes_precedence_over_publication_id_regex():
    """Wenn die Tabelle direkt parsbar ist, brauchen wir den Regex-Fallback
    nicht - wir bekommen sofort strukturierte Daten."""
    items = SachsenScraper.parse_listing(
        NETSERVER_REAL_HTML, base_url=BASE, portal_name="x",
    )
    # Echte Authority-Strings statt 'Sachsen-Bekanntmachung XXX' aus Regex-Fallback.
    titles = [it.contracting_authority for it in items.values()]
    assert "Stadt Dresden, Tiefbauamt" in titles
    assert "Stadt Leipzig" in titles
