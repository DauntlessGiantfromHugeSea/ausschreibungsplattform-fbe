from scrapers.bund import BundScraper
from scrapers.ted import TedScraper


BUND_HTML = """
<html><body>
  <ul>
    <li class="standard-teaser">
      <a href="/Content/DE/Ausschreibungen/Anzeige/12345.html">Tiefbau Flüssigboden Verfüllung Leipzig</a>
      <p>Vergabestelle: Stadt Leipzig | Ort: Leipzig, Sachsen | Frist: 12.06.2026 | Veröffentlichung: 01.05.2026</p>
    </li>
    <li class="standard-teaser">
      <a href="/Content/DE/Ausschreibungen/Anzeige/99999.html">Sanierung Fernwärmeleitung Dresden</a>
      <p>Vergabestelle: SachsenEnergie | Ort: Dresden | Frist: 30.07.2026</p>
    </li>
  </ul>
</body></html>
"""


def test_bund_parses_two_results():
    items = BundScraper.parse_search_html(BUND_HTML, base_url="https://www.service.bund.de")
    assert len(items) == 2
    titles = sorted(t.title for t in items.values())
    assert any("Flüssigboden" in t for t in titles)
    leipzig = next(i for i in items.values() if "Leipzig" in i.title)
    assert leipzig.contracting_authority == "Stadt Leipzig"
    assert "Leipzig" in leipzig.location
    assert leipzig.deadline is not None
    assert leipzig.deadline.year == 2026 and leipzig.deadline.month == 6
    assert leipzig.region == "Sachsen"


def test_ted_parses_notice_payload():
    notices = [
        {
            "publication-number": "2026/S 100-123456",
            "notice-title": {"deu": "Bauauftrag Verfüllung Leitungsgraben"},
            "buyer-name": {"deu": "Stadt Magdeburg"},
            "place-of-performance": {"deu": "Magdeburg"},
            "publication-date": "2026-04-15T00:00:00Z",
            "deadline-date-lot": "2026-06-01T12:00:00Z",
            "classification-cpv": ["45112000", "45232140"],
            "description-lot": {"deu": "Verfüllung mit ZFSV"},
            "links": {"html": {"DEU": "https://ted.europa.eu/de/notice/-/detail/123456-2026"}},
        }
    ]
    items = TedScraper.parse_results(notices)
    assert len(items) == 1
    item = next(iter(items.values()))
    assert "Verfüllung" in item.title
    assert item.contracting_authority == "Stadt Magdeburg"
    assert item.cpv_codes == ["45112000", "45232140"]
    assert item.deadline is not None
    assert item.publication_date is not None
    assert item.url.startswith("https://ted.europa.eu")
