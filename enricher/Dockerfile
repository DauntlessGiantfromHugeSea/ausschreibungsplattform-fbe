FROM mcr.microsoft.com/playwright/python:v1.60.0-jammy

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Alle Python-Module des Enrichers kopieren (enricher.py + portal_logins.py + ...)
COPY *.py ./

# Default-Volume fuer die MD-Dateien (host-seitig wird /srv/fbe-enrich gemountet).
RUN mkdir -p /data/enrich
ENV ENRICH_DIR=/data/enrich

# Kein Health-Check, kein Server - reiner Worker-Loop.
CMD ["python", "-u", "enricher.py"]
