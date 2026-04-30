"""Tests fuer den Next.js-Scraper."""
from __future__ import annotations

import json
from datetime import datetime

from scrapers.nextjs import (
    NextjsScraper,
    extract_next_data,
    _find_tender_list,
    _parse_any_date,
)


# Realistisches Next.js-HTML: Hülle + __NEXT_DATA__ JSON mit Tendern
def _nextjs_html(payload: dict) -> str:
    return f"""
<!DOCTYPE html>
<html><head><title>evergabe.de</title></head>
<body>
  <div id="__next"></div>
  <script id="__NEXT_DATA__" type="application/json">{json.dumps(payload, ensure_ascii=False)}</script>
</body></html>
"""


SAMPLE_PAYLOAD = {
    "props": {
        "pageProps": {
            "results": {
                "items": [
                    {
                        "title": "Verfüllung Leitungsgraben mit Flüssigboden Magdeburg",
                        "slug": "verfuellung-magdeburg-12345",
                        "submission_deadline": "2026-06-12T08:00:00Z",
                        "place_of_performance": {"name": "39104 Magdeburg"},
                        "buyer_name": "Stadt Magdeburg",
                        "description": "ZFSV nach RAL GZ 507",
                        "published_at": "2026-04-15T00:00:00Z",
                    },
                    {
                        "title": "Bahnhof Markranstädt - Aufzugschacht",
                        "slug": "bahnhof-markranstaedt-67890",
                        "submission_deadline": "07.05.2026",
                        "place_of_performance": {"name": "04420 Markranstädt"},
                        "buyer_name": "DB Station&Service",
                        "description": "bauzeitlicher Rückbau, Baugrubensicherung",
                    },
                ],
                "pagination": {"page": 1, "total": 2}
            }
        },
    },
    "page": "/auftraege/auftrag-suchen",
    "query": {"search[query]": "tiefbau"},
    "buildId": "abc123",
}


def test_extract_next_data_returns_parsed_json():
    html = _nextjs_html(SAMPLE_PAYLOAD)
    data = extract_next_data(html)
    assert data is not None
    assert data["buildId"] == "abc123"


def test_find_tender_list_walks_into_pageProps():
    found = _find_tender_list(SAMPLE_PAYLOAD)
    assert found is not None
    assert len(found) == 2
    assert found[0]["title"].startswith("Verfüllung")


def test_parse_html_maps_fields_correctly():
    html = _nextjs_html(SAMPLE_PAYLOAD)
    items = NextjsScraper.parse_html(
        html, base_url="https://www.evergabe.de",
        portal_name="evergabe.de", config={},
    )
    assert len(items) == 2
    fluess = next(i for i in items.values() if "Flüssigboden" in i.title)
    assert fluess.contracting_authority == "Stadt Magdeburg"
    assert fluess.location == "39104 Magdeburg"
    assert fluess.deadline is not None
    assert fluess.deadline.year == 2026 and fluess.deadline.month == 6
    assert fluess.publication_date is not None
    # URL wurde aus slug + auftraege/-Default gebaut
    assert "verfuellung-magdeburg-12345" in fluess.url
    assert fluess.url.startswith("https://www.evergabe.de/")


def test_parse_html_handles_de_dates():
    html = _nextjs_html(SAMPLE_PAYLOAD)
    items = NextjsScraper.parse_html(
        html, base_url="https://www.evergabe.de",
        portal_name="evergabe.de", config={},
    )
    bahn = next(i for i in items.values() if "Markranstädt" in i.title)
    assert bahn.deadline is not None
    assert bahn.deadline.day == 7 and bahn.deadline.month == 5


def test_parse_any_date_handles_iso_and_de_formats():
    assert _parse_any_date("2026-06-12T08:00:00Z").year == 2026
    assert _parse_any_date("12.06.2026").month == 6
    assert _parse_any_date(None) is None
    assert _parse_any_date({"date": "2026-06-12T08:00:00Z"}).year == 2026


def test_parse_html_returns_empty_when_no_next_data():
    items = NextjsScraper.parse_html(
        "<html><body>Kein NextData</body></html>",
        base_url="https://example.org", portal_name="x", config={},
    )
    assert items == {}


def test_parse_html_handles_url_template():
    payload = {"props": {"pageProps": {"items": [
        {"title": "Test", "id": "abc-1", "deadline": "2026-06-01"}
    ]}}}
    html = _nextjs_html(payload)
    items = NextjsScraper.parse_html(
        html, base_url="https://x.de", portal_name="x", config={
            "url_template": "/notice/{id}",
            "field_map": {"slug": ["id"]},
        },
    )
    item = next(iter(items.values()))
    assert item.url == "https://x.de/notice/abc-1"
