# Ausschreibungsplattform Flüssigboden Engineering (FBE)

Webbasiertes Tool zur automatisierten Recherche, Bewertung und Verwaltung
öffentlicher Ausschreibungen mit Bezug zu **Flüssigboden, ZFSV, Tiefbau,
Leitungsbau, Fernwärme** und verwandten Anwendungen.

Dieses Repository enthält ein **lauffähiges MVP** mit zwei produktiven
Scrapern (`bund.de` Service-Portal Suche und TED – Tenders Electronic
Daily), Scoring-Logik, Dublettenprüfung, Scheduler, Web-Dashboard
(FastAPI + Jinja), CSV/Excel-Export und Tests.

---

## Inhaltsverzeichnis

1. [Architektur](#architektur)
2. [Ordnerstruktur](#ordnerstruktur)
3. [Installation](#installation)
4. [Start](#start)
5. [Konfiguration](#konfiguration)
6. [Portal-Hinweise (API / Scraping / Playwright)](#portal-hinweise)
7. [Tests](#tests)
8. [Erweiterung](#erweiterung)

---

## Architektur

```
                    ┌──────────────────────────┐
                    │  Scheduler (APScheduler) │
                    │   tägliche Suche 07:00   │
                    └────────────┬─────────────┘
                                 │
              ┌──────────────────▼──────────────────┐
              │           Scraper-Pipeline          │
              │  bund.de · TED · (weitere Quellen)  │
              └──────┬─────────────────────┬────────┘
                     │ TenderItem-Objekte  │
                     ▼                     ▼
              ┌─────────────┐       ┌─────────────┐
              │  Scoring    │──────▶│  Dedup      │
              └─────────────┘       └──────┬──────┘
                                           │
                                  ┌────────▼────────┐
                                  │  SQLite (SQLA)  │
                                  └────────┬────────┘
                                           │
                                ┌──────────▼──────────┐
                                │  FastAPI + Jinja    │
                                │  Dashboard / Export │
                                └─────────────────────┘
```

* **Backend:** Python 3.11, FastAPI, SQLAlchemy, APScheduler.
* **Scraping:** `httpx` + `BeautifulSoup`. Playwright optional – aktuell nicht
  benötigt, da bund.de und TED ohne Headless-Browser funktionieren.
* **Datenbank:** SQLite (`data/tenders.db`). Migration zu PostgreSQL über
  `DATABASE_URL` in `.env` möglich – das Schema ist neutral.
* **Frontend:** Jinja2-Templates + Vanilla JS / Bootstrap-CSS.
* **Export:** `pandas` + `openpyxl` (Excel) bzw. `csv` (CSV).
* **Konfiguration:** YAML in `config/` für Portale und Suchbegriffe.

---

## Ordnerstruktur

```
ausschreibungsplattform-fbe/
├── backend/
│   ├── __init__.py
│   ├── api.py                 # FastAPI-Routen (Dashboard, REST, Export)
│   ├── database.py            # SQLAlchemy-Engine + Session
│   ├── models.py              # ORM-Modell Tender
│   ├── scoring.py             # Relevanz-Score
│   ├── dedup.py               # Dublettenprüfung
│   ├── scheduler.py           # APScheduler – tägliche Läufe
│   ├── export.py              # CSV/Excel-Export
│   ├── pipeline.py            # Orchestriert Scraper → Scoring → Dedup → DB
│   ├── search_terms.py        # Lädt config/search_terms.yaml
│   ├── portal_config.py       # Lädt config/portals.yaml
│   ├── notify.py              # E-Mail-Benachrichtigung (optional)
│   └── templates/             # Jinja2-Templates
│       ├── base.html
│       ├── index.html
│       └── detail.html
├── scrapers/
│   ├── __init__.py
│   ├── base.py                # BaseScraper + TenderItem
│   ├── bund.py                # Service-Portal bund.de
│   └── ted.py                 # TED Search API v3
├── config/
│   ├── portals.yaml
│   └── search_terms.yaml
├── data/                      # SQLite-DB landet hier (gitignored)
├── exports/                   # CSV/XLSX-Exporte (gitignored)
├── tests/
│   ├── test_scoring.py
│   ├── test_dedup.py
│   └── test_parser.py
├── .env.example
├── .gitignore
├── requirements.txt
├── run.py                     # Startpunkt: uvicorn + Scheduler
└── README.md
```

---

## Installation

```bash
# 1. Repo klonen & ins Verzeichnis wechseln
git clone <repo-url> ausschreibungsplattform-fbe
cd ausschreibungsplattform-fbe

# 2. Virtuelles Environment
python3.11 -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate

# 3. Abhängigkeiten installieren
pip install --upgrade pip
pip install -r requirements.txt

# 4. .env anlegen
cp .env.example .env
# Werte nach Bedarf anpassen (SMTP für Mailbenachrichtigung, Zielregionen…)
```

---

## Start

```bash
python run.py
```

Damit startet:

* der **Webserver** auf <http://localhost:8000>
* der **Scheduler** mit täglicher Suche um 07:00 Uhr (lokale Zeit)

Im Dashboard können Sie über den Button **„Suche starten"** zusätzlich
manuell einen Lauf triggern.

### Wichtige URLs

| URL                                  | Funktion                       |
|--------------------------------------|--------------------------------|
| `/`                                  | Dashboard mit Tabelle & Filter |
| `/tender/{id}`                       | Detailansicht                  |
| `/tender/{id}/status`                | Status setzen (POST)           |
| `/run-search`                        | Suche manuell starten (POST)   |
| `/export/csv`                        | CSV-Export                     |
| `/export/xlsx`                       | Excel-Export                   |
| `/api/tenders`                       | JSON-Liste                     |
| `/docs`                              | OpenAPI / Swagger              |

---

## Konfiguration

### `config/search_terms.yaml`

Themencluster mit Gewichtung. Jede Gruppe definiert `weight` (high / medium /
low) und eine Liste an Synonymen. Die Datei ist die einzige Stelle, an der
Begriffe gepflegt werden müssen – Scoring und Scraper lesen automatisch.

### `config/portals.yaml`

Liste der konfigurierten Quellen. Felder pro Eintrag:

```yaml
- name: "bund.de"
  enabled: true
  scraper: "bund"            # Modulname unter scrapers/
  base_url: "https://www.service.bund.de"
  strategy: "search_url"     # api | rss | search_url | scrape
  notes: "Service-Portal des Bundes – HTML-Suche, ohne API"
```

### `.env`

```
DATABASE_URL=sqlite:///./data/tenders.db
SCHEDULER_HOUR=7
SCHEDULER_MINUTE=0
NOTIFY_EMAIL=                 # leer = keine Mails
SMTP_HOST=
SMTP_PORT=587
SMTP_USER=
SMTP_PASSWORD=
TARGET_REGIONS=Sachsen,Brandenburg,Berlin,Sachsen-Anhalt,Thüringen
HIGH_RELEVANCE_THRESHOLD=70
```

---

## Portal-Hinweise

| Portal                      | Empfohlene Strategie | Aufwand | Bemerkung                                                                                              |
|-----------------------------|----------------------|---------|--------------------------------------------------------------------------------------------------------|
| **bund.de** (Service-Portal)| `search_url` + HTML  | gering  | Implementiert. Stabile Such-URL, Server-rendered HTML.                                                 |
| **TED** (EU)                | offizielle Search-API| gering  | Implementiert. JSON-API `https://api.ted.europa.eu/v3/notices/search`, Volltext + CPV-Filter.          |
| **eVergabe-online (BMI)**   | `search_url` + HTML  | mittel  | HTML stabil, aber Sessions/Cookies nötig. Über `httpx.Client` mit Cookies möglich.                     |
| **Deutsche eVergabe**       | API (registriert)    | mittel  | Bietet REST-API – Registrierung erforderlich.                                                          |
| **Vergabe24**               | HTML-Scrape          | mittel  | JS-leicht, mit BeautifulSoup machbar.                                                                  |
| **Subreport ELViS**         | HTML-Scrape          | hoch    | Login-Schranke für Volltext, freie Suche eingeschränkt.                                                |
| **bi-medien**               | RSS / HTML           | gering  | RSS-Feeds für Bauausschreibungen verfügbar.                                                            |
| **DB Vergabeportal**        | HTML-Scrape (JS)     | hoch    | JS-rendered → **Playwright empfohlen**.                                                                |
| **Vergabe.NRW / Bayern …**  | HTML-Scrape          | mittel  | Heterogen, teils RSS, teils Such-URL, einzelne Länder JS-rendered.                                     |

> **Faustregel:** Zuerst `requests/httpx + BeautifulSoup` versuchen. Erst wenn
> Inhalte clientseitig per JS nachgeladen werden (DB Vergabeportal, einige
> kommunale Portale), Playwright einsetzen. `playwright` ist in
> `requirements-optional.txt` separat aufgeführt.

---

## Tests

```bash
pytest -q
```

Abgedeckt sind Scoring, Dublettenprüfung und Parser (HTML-Fixture für
bund.de, JSON-Fixture für TED).

---

## Erweiterung

Neuen Scraper anlegen:

1. `scrapers/<portal>.py` mit Klasse `class FoobarScraper(BaseScraper)` und
   Methode `fetch(self, terms: list[str]) -> list[TenderItem]`.
2. Eintrag in `config/portals.yaml` ergänzen (`scraper: foobar`).
3. Pipeline lädt das Modul automatisch (`importlib`).

Lizenz / robots.txt: Jeder Scraper berücksichtigt `robots.txt` und einen
konfigurierbaren `User-Agent`. Vor produktivem Einsatz die AGB der jeweiligen
Portale prüfen.
