# FBE Enricher

Separater Docker-Container, der Tender-Detailseiten mit Chromium rendert,
durch OpenAI strukturiert extrahiert und das Ergebnis als Markdown +
DB-Eintrag zurueck an die Plattform liefert.

Kein direkter LLM-Zugriff fuer Plattform-User — der API-Key liegt
ausschliesslich in diesem Container.

## Setup auf dem Server

```bash
# 1. Shared-Volume anlegen
sudo mkdir -p /srv/fbe-enrich
sudo chown $(whoami) /srv/fbe-enrich

# 2. Secret erzeugen + in beide .envs eintragen
ENRICHER_TOKEN=$(openssl rand -hex 32)
echo "ENRICHER_TOKEN=$ENRICHER_TOKEN" | sudo tee -a /root/ausschreibungsplattform-fbe/.env
# fbe-tender muss diesen Token kennen -> Restart
sudo systemctl restart fbe-tender

# 3. Enricher-Image bauen
cd /root/ausschreibungsplattform-fbe/enricher
docker build -t fbe-enricher .

# 4. .env fuer den Container
cp .env.example .env
# .env editieren: ENRICHER_TOKEN (aus 2), OPENAI_API_KEY

# 5. Container starten (network=host, damit er auf localhost:8000 erreicht)
docker run -d \
  --name fbe-enricher \
  --restart unless-stopped \
  --network host \
  --env-file /root/ausschreibungsplattform-fbe/enricher/.env \
  -v /srv/fbe-enrich:/data/enrich \
  fbe-enricher

# 6. Logs checken
docker logs -f fbe-enricher
```

## Loop

Alle `POLL_INTERVAL_S` Sekunden (Default 600 = 10 min):
1. `GET /api/internal/tenders-to-enrich?limit=5` mit `X-Internal-Token`
2. Pro Tender:
   - Chromium rendert `tender.url`, wartet bis `networkidle`
   - HTML wird zu Plain-Text reduziert (max 18.000 Zeichen)
   - OpenAI extrahiert strukturiert (Eckdaten, Leistungsbeschreibung, Gewerke, FBE-Bewertung)
   - `.md` -> `/data/enrich/tender-<id>.md` (= `/srv/fbe-enrich/tender-<id>.md` am Host)
   - `POST /api/internal/tenders/<id>/enriched` mit dem Markdown + Summary
3. Fehler werden in der DB als `enrich-error: <reason>` markiert, damit das
   gleiche Tender nicht endlos rotiert.

## Update / Rebuild

```bash
cd /root/ausschreibungsplattform-fbe
git pull
cd enricher
docker build -t fbe-enricher .
docker restart fbe-enricher
```

## Wissensbasis (KI-Kontext)

Alle Markdown-Dateien aus `/srv/fbe-knowledge/` (vom Admin gepflegt unter
`/admin/notes`) werden bei jedem Anreicherungs-Lauf als System-Kontext
an das LLM gegeben. So kann man dem Bot mitgeben, was fuer FBE wichtig
ist (z.B. „Fluessigboden ist ZFSV", „nur Bauleistungen, keine
Planungsleistungen", typische Wettbewerber, etc.).

Der Container muss dazu nichts mounten — der Plattform-Endpoint
`/api/internal/knowledge` liefert die Dateien direkt aus.

## Portal-Logins (Optional)

Wenn ein Portal Login-pflichtig ist, kann der Enricher sich
einmalig pro Container-Lifetime einloggen. Cookies bleiben im
Browser-Context.

Konfiguration via ENV `PORTAL_LOGINS_JSON` (JSON-String in der .env)
oder `PORTAL_LOGINS_FILE` (Pfad zu einer JSON-Datei).

Siehe `enricher/.env.example` fuer das Format.

Praktisches Vorgehen pro Portal:
1. Browser-DevTools auf der Login-Seite oeffnen, Username/Password-Feld
   inspizieren, deren CSS-Selektor notieren
2. Submit-Button-Selektor notieren
3. Einen Selektor finden, der NUR im eingeloggten Zustand existiert
   (z.B. `a[href*='logout']`) — das wird der `success_selector`
4. Konfig in die .env eintragen, Container neustarten

## Backfill

Wenn du alle bestehenden Tender neu anreichern willst (z.B. nach Prompt-
Anpassung), kannst du in der Plattform-DB die `ai_analysis`-Spalte leeren:

```bash
sqlite3 /root/ausschreibungsplattform-fbe/data/tenders.db \
  "UPDATE tenders SET ai_analysis = NULL, ai_analyzed_at = NULL;"
```

Beim naechsten Poll holt der Enricher die Tender wieder ab.
