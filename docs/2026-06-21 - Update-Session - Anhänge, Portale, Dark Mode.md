# 2026-06-21 — Update-Session: Anhänge, Portale, Dark Mode

> Alle Änderungen aus der Session vom 21.06.2026 an der Flüssigboden Akademie · Ausschreibungsplattform.
> Branch: `claude/multi-platform-integration-9wKvE`. Auf dem Server per `git pull` + `systemctl restart fbe-tender` (und ggf. `docker build`/`restart fbe-enricher`) ausrollen.

---

## Auf einen Blick

| Bereich | Was ist neu |
|---|---|
| **Changelog** | Update-Verlauf ist jetzt direkt im Admin-Bereich pflegbar; erscheint sofort auf `/hilfe`. Historische Einträge V0.1.0 – V1.0.2 sind in der DB geseedet. |
| **Broadcast-Mail** | User-freundlich umgeschrieben — verweist auf `/feedback` und `/hilfe`, keine „Admin-Hinweise" mehr. |
| **evergabe-online** | Umgestellt auf offiziellen RSS-Feed; Enricher klickt automatisch auf `Ausschreibungsunterlagen einsehen` und lädt alle Vergabeunterlagen als Anhänge in die DB. |
| **oeffentlichevergabe.de (DOEE)** | Dedizierter API-Scraper via OpenData-Bulk-Export (`/api/notice-exports`). Filtert client-seitig auf die FBE-Cluster-Keywords. |
| **Portale** | Neuer 🧠-Button („Bulk-Claude-Analyse aller Tender eines Portals inkl. Anhänge") + ♻ Recrawl + 🧹 Purge. |
| **User → Portale** | Benutzer können auf bestimmte Portale beschränkt werden (Whitelist im User-Edit). Leer lassen = alle Portale erlaubt. |
| **Konto** | User kann sein Passwort selbst ändern (ohne altes Passwort) unter *Meine Benachrichtigungen*. |
| **Branding** | Login-Texte (Pill, Headline, Untertitel) im Admin editierbar. Favicon wird automatisch aus dem Logo-Upload generiert (mit Cache-Buster). |
| **System** | Neuer Neustart-Button (Plattform + Enricher) unter *Einstellungen → Allgemein*. |
| **Dark Mode** | Komplett überarbeitet mit semantischen CSS-Variablen — alle Komponenten, Farben, Pills und Tabellen sind konsistent. |

---

## 1. Update-Verlauf editierbar

**Was:** Der Changelog ist nicht mehr statisch im Template, sondern lebt in der DB und wird über das Admin-UI gepflegt.

**Wo:**
- **Einstellungen → Update-Verlauf** — Formular mit Version, Titel, Markdown-Body und „veröffentlicht"-Checkbox. Versionsfeld wird automatisch aus dem letzten Eintrag hochgezählt (z. B. `V1.0.2` → `V1.0.3`).
- Öffentlich sichtbar unter `/hilfe#changelog`.

**Details:**
- Neue Tabelle `changelog_entries` (id, version, title, body_md, is_published, created_at, author).
- Beim ersten Start werden **11 historische Einträge** (V0.1.0 → V1.0.2) automatisch geseedet — der statische `_changelog.html`-Include ist nur noch Fallback.
- Öffentliche Seite: `hilfe.html` rendert `changelog_db` oberhalb des Fallback-Includes.

**Neue Routen (Admin):**
- `GET  /admin/changelog` — Liste + Formular
- `POST /admin/changelog/save` — anlegen/aktualisieren
- `POST /admin/changelog/{eid}/delete`

---

## 2. Broadcast-Mail user-freundlich

Die automatischen Broadcast-Mails an alle User (unter *Einstellungen → Broadcast*) waren mit „Bei Fragen wende dich an den Administrator" abgeschlossen — jetzt stehen dort **klare Verweise auf die Plattform-Ressourcen**:

- Verlinkt auf `https://app.fb-akademie.de/hilfe` (Update-Verlauf, FAQ, Anleitungen)
- Verlinkt auf `https://app.fb-akademie.de/feedback` (Wünsche, Bugs, Suchprofil-Anfragen)
- Deutlicher Hinweis „diese Mail ist automatisch — Antworten werden nicht gelesen"
- HTML + Plaintext-Variante sind synchron

---

## 3. evergabe-online.de (BMI) — RSS + tiefe Anhang-Erfassung

### Listing über RSS

Statt 20+ Playwright-Suchen pro Keyword nutzt das Portal jetzt den **offiziellen News-Feed**:

```
https://www.evergabe-online.info/SiteGlobals/Functions/RSSFeed/DE/eVergabe/RSSNewsfeed/RSSNewsfeed.xml?nn=1063186
```

- Scraper: `rss_generic` mit `filter_by_terms: true` — nur Treffer, die einen FBE-Cluster-Begriff (Tiefbau, Verfüllung, Spundwand, Leitungsbau, Flüssigboden, ZFSV etc.) enthalten, landen in der DB.
- Massiv schneller und stabiler als das UI-Scraping.

### Anhänge automatisch ziehen (Enricher-Bot)

Der Bot klickt jetzt auf **„AUSSCHREIBUNGSUNTERLAGEN EINSEHEN"** (Apache-Wicket-Link, `a.btn.btn-primary`), navigiert per Wicket-Session auf `tenderdocuments.html?id=NNN&cookieCheck` und lädt dort alle Dateien einzeln.

Sammelt PDF, DOCX, XLSX, ZIP, TXT usw. — je Tender bis zu **50 Dateien** (`MAX_PDFS` in `enricher/.env`).

**Fallstricke, die gefixt sind:**
- Nur **ein einziger** Klick, keine Pattern-Schleife mehr — Mehrfach-Klicks führten zu Wicket-`internalerror.html`.
- Wicket-Download-URLs (`?...downloadLink...`) sind Datei-Downloads, kein Sub-Pages — der Bot versucht kein `page.goto()` mehr auf sie („Download is starting"-Crash).
- `_find_pdf_links` matched jetzt auch `downloadLink`, `/files/`, `getFile`, `attachment`, `fileservlet`, `<a download="">`.
- Blocklist gegen `/downloads/installer/oba` etc. (das sind Marketing-Downloads, keine Vergabeunterlagen).
- `Content-Disposition: attachment` wird auch akzeptiert, wenn der Server `text/html` als Content-Type schickt (Wicket-Quirk).

### Anhänge im Dashboard sehen

Auf jeder Tender-Detailseite unter `/tender/<id>` gibt es unten den Block **📎 Anhänge** mit:
- Dateiname
- Größe in KB
- Direkter Download-Button
- Link zur Original-URL beim Portal

---

## 4. oeffentlichevergabe.de (DOEE) — OpenData-API

Statt UI-Scraping wird der offizielle **Datenservice-Öffentlicher-Einkauf-Endpoint** genutzt:

```
GET /api/notice-exports?pubDay=YYYY-MM-DD&format=csv.zip
```

- Liefert ein ZIP mit allen Bekanntmachungen des Tages im CSV-, OCDS-JSON- oder eForms-XML-Format
- Scraper `oeffentlichevergabe_api` lädt die letzten `lookback_days` (Default 14) Tage, entpackt ZIPs, parst CSV und mapped robust auf `TenderItem` (mehrere Alias-Spaltennamen — CSV-Header kann variieren).
- Client-seitiger Filter auf die FBE-Cluster-Keywords.

**Neue Datei:** `scrapers/oeffentlichevergabe_api.py`
**Portal-Config:** `config/portals.yaml` → `scraper: oeffentlichevergabe_api`, `strategy: api`

---

## 5. Portal-Admin: neue Buttons pro Portal

Auf **Einstellungen → Portale** hat jede Zeile jetzt vier Aktions-Icons:

| Icon | Bedeutung |
|---|---|
| 🔍 | Portal-Probe (Suchtest ohne DB-Änderung) |
| ✎ | Portal-Konfig bearbeiten |
| 🧠 | **NEU** — Bulk-Claude-Analyse aller Tender dieses Portals |
| ♻ | Komplett neu crawlen (Purge + Pipeline-Lauf + Enricher zieht Anhänge nach) |
| 🧹 | Alte DB-Einträge dieses Portals löschen |
| 🗑 | Portal-Konfig löschen |

### 🧠 Bulk-Claude-Analyse

- Klick → bestätigen → Hintergrund-Job startet
- Iteriert alle Tender des Portals (Default: nur die ohne bestehende `claude_analysis`)
- Nutzt den existierenden Claude-Agent (Anthropic Sonnet 4.6 mit Tool-Use)
- Agent hat automatisch Zugriff auf `list_attachments`, `read_enrichment`, `search_knowledge`, `find_similar_tenders`
- Ergebnis: Einsparungspotenzial (%), Flüssigboden-Eignung (hoch/mittel/gering), Empfehlung + Begründung
- Läuft im Hintergrund — Live-Log via `journalctl -u fbe-tender -f | grep "Bulk-Claude"`

---

## 6. User → Portale-Whitelist

**Was:** Restricted-User (`role=user`) können auf bestimmte Portale beschränkt werden — zusätzlich zur bereits vorhandenen Suchprofil-Zuweisung.

**Wo:** *Einstellungen → Benutzer → User bearbeiten* → neuer Block **Erlaubte Portale** mit Checkbox-Liste aller Portale.

**Regeln:**
- **Leer lassen** → alle Portale erlaubt (Standard, keine Änderung zum vorherigen Verhalten)
- **Mindestens ein Haken** → strikte Whitelist, User sieht nur Tender aus diesen Portalen
- Wirkt nur für Rolle `user`; Admin sieht alles

**Datenbank:** Neue Tabelle `user_portals(user_id, portal)` — leere Zeile-Menge = keine Restriktion.

**Filter greift in:** `_enforce_restricted()` in `backend/api.py`.

---

## 7. Passwort selbst ändern (Eigenservice)

**Wo:** *Mein Konto → Meine Benachrichtigungen* → ganz unten Block **🔐 Passwort ändern**

- Neues Passwort 2× eingeben (Bestätigung)
- **Kein altes Passwort abgefragt** — vertraut auf die laufende Session
- Mindestens 6 Zeichen
- Gilt sofort
- Route: `POST /me/change-password`

---

## 8. Branding — Login-Texte editierbar + Favicon

### Login-Texte

*Einstellungen → Logo & Branding* → neuer Block **Login-Texte (Brand-Panel)** mit drei Feldern:

| Feld | Beispiel |
|---|---|
| **Pill-Label** | `AUSSCHREIBUNGEN` (leer = ausblenden) |
| **Headline** | `Tiefbau.\nVerfüllung.\nFlüssigboden.` (Zeilenumbrüche = neue Zeile) |
| **Beschreibung** | `Die zentrale Plattform der Flüssigboden Akademie …` |

Änderungen sind sofort auf `/login` sichtbar. Persistiert in `backend/static/uploaded/login-texts.json`.

### Favicon

- Wird automatisch aus dem hochgeladenen **Mark** oder **Light**-Logo generiert (nur PNG/JPG/WEBP-Uploads — SVG geht nicht als Favicon).
- Dynamische Route `/favicon.png` liefert die aktuelle Datei.
- URL im HTML enthält `?v=<mtime>` — Cache-Buster, Browser laden das neue Icon sofort.
- Notfall-Button „**Favicon neu generieren**" auf der Branding-Seite (spiegelt Upload manuell auf `favicon.png`).

---

## 9. Neustart-Button (Plattform + Enricher)

**Wo:** *Einstellungen → Allgemein* → roter Button **↻ System neu starten (Plattform + Enricher)**

**Was passiert bei Klick:**
1. Redirect + Flash-Message werden sofort geliefert (Browser bleibt konsistent)
2. 2 Sekunden Verzögerung (damit die HTTP-Antwort noch beim Client ankommt)
3. `docker restart fbe-enricher`
4. `systemctl restart fbe-tender` (killt sich selbst, systemd startet die App neu)

Nach 10–15 Sekunden ist die Seite wieder da. **Voraussetzung:** Der `fbe-tender`-Service läuft als root — dann funktionieren `systemctl` und `docker` ohne sudo-Prompt.

---

## 10. Dark Mode — komplette Überarbeitung

Der Dark Mode war fragmentiert (viele Einzelfixes für spezifische Tailwind-Klassen). Er ist jetzt sauber auf **semantische CSS-Variablen** umgestellt.

### CSS-Variablen-Schema

```css
:root {
  --surface:      #ffffff;    /* Karten, Inputs, Buttons */
  --surface-2:    #f5f7f8;    /* Hover-Fill, Tabellen-Header */
  --surface-3:    #f3f4f6;    /* Striping */
  --border:       rgba(15,23,42,0.12);
  --border-strong: rgba(0,50,51,0.18);
  --text:         #0f172a;
  --text-muted:   #475569;
  --text-faint:   #94a3b8;
  --accent:       #d4f561;    /* Lime CTA */
  --accent-ink:   rgb(0,50,51);
}
html.dark {
  --surface:    #18181b;
  --surface-2:  #27272a;
  --surface-3:  #3f3f46;
  --border:     rgba(255,255,255,0.10);
  --text:       #f4f4f5;
  --text-muted: #a1a1aa;
  --text-faint: #71717a;
  /* ... */
}
```

### Was das bedeutet

- Cards, Buttons (alle Varianten inkl. `btn-secondary`), Inputs, Chips, Tabellen und Dropdown-Menüs lesen aus den Tokens → **keine eigene Dark-Variante** mehr nötig.
- Farb-Tints (emerald, rose, amber, sky, violet, indigo, brand) sind vollständig gemappt — inklusive `-100`-Varianten und `text-…-900`-Klassen.
- Sticky-Spalten und Pills mit hellem Standard-Hintergrund werden mit umgeschaltet.
- **Topbar bleibt in beiden Modi dunkel-teal** — Brand-Konsistenz.
- **Theme-Toggle-Button** unten rechts (dunkles Teal, statt vorher hellblau): Sonne im Light-Mode, Mond im Dark-Mode. Nutzt eigene CSS-Klassen statt `dark:hidden` (das war fragil).

---

## 11. Portal-Restrictions in `_enforce_restricted()`

Der bestehende Filter (Suchprofile) wurde erweitert:

```python
# Portal-Whitelist (leer = alle erlaubt)
uid = request.session.get("user_id")
if uid:
    allowed_portals = _user_allowed_portals(db, uid)
    if allowed_portals:
        base_query = base_query.filter(Tender.portal.in_(allowed_portals))
```

Wirkt auf **jede** Query, die durch `_enforce_restricted()` läuft — Dashboard, Detail-Seite, Suche, Export.

---

## 12. Deployment-Cheat-Sheet

```bash
# Auf dem Server
cd /root/ausschreibungsplattform-fbe
git pull origin claude/multi-platform-integration-9wKvE
systemctl restart fbe-tender

# Wenn der Enricher-Code angefasst wurde (dieses Session: ja):
cd enricher
docker build -t fbe-enricher .
docker stop fbe-enricher && docker rm fbe-enricher
docker run -d \
  --name fbe-enricher \
  --restart unless-stopped \
  --network host \
  --env-file /root/ausschreibungsplattform-fbe/enricher/.env \
  -v /srv/fbe-enrich:/data/enrich \
  -v /srv/fbe-attachments:/data/attachments \
  fbe-enricher
docker logs -f --tail=30 fbe-enricher
```

### Enricher-Konfig (empfohlene Werte)

`/root/ausschreibungsplattform-fbe/enricher/.env`:

```env
MAX_PDFS=50
MAX_ATTACHMENT_MB=50
MAX_SUBPAGES=6
BATCH_LIMIT=10
POLL_INTERVAL_S=300
ATTACHMENT_EXTENSIONS=.pdf,.docx,.doc,.xlsx,.xls,.zip,.txt,.rtf,.odt,.ods,.csv
```

---

## 13. Neue Dateien

- `scrapers/oeffentlichevergabe_api.py` — OpenData-Bulk-Scraper
- `backend/templates/changelog_admin.html` — Editor für Update-Verlauf
- `docs/2026-06-21 - Update-Session - Anhänge, Portale, Dark Mode.md` — dieses Dokument

## Wesentliche Änderungen an bestehenden Dateien

- `backend/api.py` — Portal-Recrawl, Purge, Bulk-Claude, Neustart, Passwort ändern, User-Portal-Whitelist, Login-Text-Save, Favicon-Route
- `backend/branding.py` — `get_login_texts`, `save_login_texts`, `favicon_url`, `favicon_path`, `_refresh_favicon`
- `backend/models.py` — `UserPortal`, `ChangelogEntry`
- `backend/migrations.py` — `create_changelog_entries_table`, `seed_changelog_entries`, `create_user_portals_table`
- `enricher/enricher.py` — Reveal-Klick (einmalig), erweiterter Download-Sammler, Portal-spezifische Sub-Pages, Content-Disposition-Handling, Blocklist gegen Installer
- `config/portals.yaml` — evergabe-online auf RSS, oeffentlichevergabe auf API
- `backend/templates/base.html` — komplette Dark-Mode-Überarbeitung mit semantischen Tokens
- `backend/templates/portals.html` — 🧠, ♻, 🧹-Buttons
- `backend/templates/user_edit.html` — Portal-Whitelist-Checkbox-Liste
- `backend/templates/branding.html` — Login-Text-Formular + Favicon-Refresh-Button
- `backend/templates/settings.html` — Neustart-Button
- `backend/templates/me_notifications.html` — Passwort-ändern-Block
- `backend/templates/login.html` — dynamische `login_texts`

---

## Offene Punkte / nächste Schritte

- **DOEE-CSV-Spaltennamen**: der Scraper mappt mehrere Aliase (`title`, `Titel`, `noticeTitle`), aber falls die reale Response Spalten hat, die keiner davon matcht, siehst du `0 Treffer` im Log — dann Log-Auszug schicken, ich passe das Mapping in einer Zeile an.
- **Bulk-Claude ist teuer**: Anthropic-API kostet je Tender ~5–15 Cent. Bei 100 Tendern ~10–15 €. Beobachte den Verbrauch, wenn du das über viele Portale hinweg auslöst.
- **Enricher-Session-Persistenz**: bei evergabe-online werden die Wicket-Cookies via Playwright `storage_state` gespeichert — die Session hält typischerweise mehrere Stunden, bevor sie neu aufgebaut werden muss.
