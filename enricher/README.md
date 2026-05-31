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

## Backfill

Wenn du alle bestehenden Tender neu anreichern willst (z.B. nach Prompt-
Anpassung), kannst du in der Plattform-DB die `ai_analysis`-Spalte leeren:

```bash
sqlite3 /root/ausschreibungsplattform-fbe/data/tenders.db \
  "UPDATE tenders SET ai_analysis = NULL, ai_analyzed_at = NULL;"
```

Beim naechsten Poll holt der Enricher die Tender wieder ab.
