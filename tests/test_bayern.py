"""Tests fuer den Bayern-WebTicker-Parser auf der echten DOM-Struktur."""
from __future__ import annotations

from scrapers.bayern import BayernScraper


# Auszug aus dem Live-Dump www.auftraege.bayern.de/
WEBTICKER_HTML = """
<html><body>
<div class="row">
  <ul id="webTicker">
    <li class='itemTicker'>
      <i class='fa fa-exclamation-circle fa-2x'></i>
      <b>Sonderpaedagogisches Foerderzentrum Vohenstrauss - Abbrucharbeiten</b>
      (Landratsamt Neustadt a.d.Waldnaab)
    </li>
    <li class='itemTicker'>
      <i class='fa fa-exclamation-circle fa-2x'></i>
      <b>Tiefbauarbeiten Hauptstrasse Augsburg</b>
      (Stadt Augsburg)
    </li>
    <li class='itemTicker'>
      <i class='fa fa-exclamation-circle fa-2x'></i>
      <b>Verfuellung Leitungsgraben mit Fluessigboden Muenchen</b>
      (Landeshauptstadt Muenchen)
    </li>
  </ul>
</div>
</body></html>
"""

BASE = "https://www.auftraege.bayern.de"


def test_extracts_three_tickers():
    items = BayernScraper.parse_webticker(
        WEBTICKER_HTML, base_url=BASE, portal_name="Auftragsbörse Bayern",
        landing_url=BASE + "/",
    )
    assert len(items) == 3
    titles = [it.title for it in items.values()]
    assert any("Foerderzentrum" in t for t in titles)
    assert any("Tiefbauarbeiten" in t for t in titles)
    assert any("Fluessigboden" in t for t in titles)


def test_extracts_authority_from_parens():
    items = BayernScraper.parse_webticker(
        WEBTICKER_HTML, base_url=BASE, portal_name="x", landing_url=BASE + "/",
    )
    fluessig = next(it for it in items.values() if "Fluessigboden" in it.title)
    assert fluessig.contracting_authority == "Landeshauptstadt Muenchen"
    assert fluessig.region == "Bayern"


def test_url_is_stable_hash_not_random():
    """Beim erneuten Parsen muss derselbe Eintrag dieselbe URL bekommen,
    sonst zerschiesst sich die Dedup-Logik (fingerprint) zwischen Laeufen."""
    a = BayernScraper.parse_webticker(WEBTICKER_HTML, BASE, "x", BASE + "/")
    b = BayernScraper.parse_webticker(WEBTICKER_HTML, BASE, "x", BASE + "/")
    assert set(a.keys()) == set(b.keys())


def test_skips_items_without_title():
    html = """
    <ul id="webTicker">
      <li class='itemTicker'>
        <i class='fa fa-exclamation-circle fa-2x'></i>
        (Anonyme Vergabestelle)
      </li>
      <li class='itemTicker'>
        <b>Echte Ausschreibung mit substantiellem Titel</b>
        (Stadt Bayern)
      </li>
    </ul>
    """
    items = BayernScraper.parse_webticker(html, BASE, "x", BASE + "/")
    assert len(items) == 1
    assert "Echte Ausschreibung" in next(iter(items.values())).title


def test_falls_back_to_class_when_id_webticker_missing():
    html = """
    <ul class="some-other-list">
      <li class='itemTicker'>
        <b>Ein Titel mindestens zwanzig Zeichen lang fuer Akzeptanz</b>
        (Bayerisches Amt)
      </li>
    </ul>
    """
    items = BayernScraper.parse_webticker(html, BASE, "x", BASE + "/")
    assert len(items) == 1


# ---------------------------------------------------------------------------
DASHBOARD_HTML = """
<html><body>
<table class="results">
  <thead>
    <tr><th>Datum</th><th>Auftrag</th><th>Vergabestelle</th><th>Frist</th></tr>
  </thead>
  <tbody>
    <tr>
      <td>01.05.2026</td>
      <td><a href="/auftrag/12345-tiefbau-passau">Tiefbauarbeiten Passau Hauptstrasse</a></td>
      <td>Stadt Passau</td>
      <td>22.05.2026</td>
    </tr>
    <tr>
      <td>02.05.2026</td>
      <td><a href="/auftrag/22222-spundwand-augsburg">Spundwandarbeiten Hafen Augsburg</a></td>
      <td>Bayerische Staatsforsten AöR</td>
      <td>30.05.2026</td>
    </tr>
    <tr>
      <td>03.05.2026</td>
      <td><a href="#sortable">Sortieren</a></td>
      <td>—</td>
      <td>—</td>
    </tr>
  </tbody>
</table>
</body></html>
"""


def test_parse_dashboard_extracts_table_rows():
    items = BayernScraper.parse_dashboard(
        DASHBOARD_HTML, base_url=BASE, portal_name="Auftragsbörse Bayern",
        landing_url=BASE + "/Dashboards/Dashboard_off?BL=09",
    )
    assert len(items) == 2
    titles = [it.title for it in items.values()]
    assert any("Tiefbauarbeiten Passau" in t for t in titles)
    assert any("Spundwandarbeiten" in t for t in titles)
    # Sortier-Link wurde abgewiesen
    assert not any(t == "Sortieren" for t in titles)


def test_parse_dashboard_extracts_authority_from_buyer_column():
    items = BayernScraper.parse_dashboard(
        DASHBOARD_HTML, BASE, "x", BASE + "/Dashboards/Dashboard_off?BL=09",
    )
    passau = next(it for it in items.values() if "Passau" in it.title)
    assert passau.contracting_authority == "Stadt Passau"
    assert passau.region == "Bayern"


def test_parse_dashboard_extracts_first_date_as_deadline():
    items = BayernScraper.parse_dashboard(
        DASHBOARD_HTML, BASE, "x", BASE + "/Dashboards/Dashboard_off?BL=09",
    )
    passau = next(it for it in items.values() if "Passau" in it.title)
    assert passau.deadline is not None
    # Erstes Datum in der Zeile = Veroeffentlichung 01.05., dieses
    # wird heuristisch als Frist gespeichert (User kann das ggf. besser
    # mappen sobald wir ein Live-DOM haben).
    assert passau.deadline.year == 2026


def test_parse_dashboard_fallback_when_no_table():
    """Wenn keine Tabelle vorhanden ist, sammeln wir alle plausiblen
    Anchors als Karten."""
    html = """
    <main>
      <a href="/auftrag/9876">Verfuellung Leitungsgraben Muenchen Innenstadt</a>
      <a href="/help">Hilfe</a>
      <a href="/auftrag/1234">Tiefbauarbeiten Westring Nuernberg</a>
    </main>
    """
    items = BayernScraper.parse_dashboard(html, BASE, "x")
    assert len(items) == 2
    assert any("Verfuellung Leitungsgraben" in it.title for it in items.values())
    assert not any("Hilfe" in it.title for it in items.values())
