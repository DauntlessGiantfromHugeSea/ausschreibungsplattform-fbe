FROM mcr.microsoft.com/playwright/python:v1.46.0-jammy

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY enricher.py .

# Default-Volume fuer die MD-Dateien (host-seitig wird /srv/fbe-enrich gemountet).
RUN mkdir -p /data/enrich
ENV ENRICH_DIR=/data/enrich

# Kein Health-Check, kein Server - reiner Worker-Loop.
CMD ["python", "-u", "enricher.py"]
