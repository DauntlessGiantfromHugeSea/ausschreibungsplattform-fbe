"""Zentrale Settings, geladen aus .env / Environment-Variablen."""
from __future__ import annotations

from pathlib import Path
from typing import List

from pydantic_settings import BaseSettings, SettingsConfigDict


PROJECT_ROOT = Path(__file__).resolve().parent.parent


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=str(PROJECT_ROOT / ".env"),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    database_url: str = f"sqlite:///{PROJECT_ROOT / 'data' / 'tenders.db'}"
    # Default ist Continuous-Mode: alle 60 Minuten ein Lauf.
    # Auf 0 setzen, um stattdessen den taeglichen Cron (siehe scheduler_hour)
    # zu verwenden.
    scheduler_interval_minutes: int = 60
    scheduler_hour: int = 7
    scheduler_minute: int = 0

    notify_email: str = ""
    smtp_host: str = ""
    smtp_port: int = 587
    smtp_user: str = ""
    smtp_password: str = ""
    smtp_from: str = "ausschreibungsbot@example.com"
    # Tageszusammenfassung: Versandzeit (lokal). Default 7:00.
    summary_hour: int = 7
    summary_minute: int = 0

    # Firmendaten fuer den Mail-Footer + Tender-Compose-Mail.
    # Werden im HTML-Mail-Footer und im Detail-Compose-Vorschau-Header
    # verwendet. Alle Felder optional - bleiben sie leer, faellt der
    # Mail-Footer auf einen generischen Hinweis zurueck.
    company_name: str = "Flüssigboden Akademie"
    company_address: str = ""
    company_phone: str = ""
    company_email: str = ""
    company_web: str = "https://fb-akademie.de"
    # Logo-URL fuer Mail-Templates (HTML-img). Muss oeffentlich erreichbar sein.
    company_logo_url: str = "https://fb-akademie.de/wp-content/uploads/2024/12/fba-logo.png"
    # Rechtliche Footer-Links (in jedem Page-Footer + Mail-Footer).
    legal_impressum_url: str = "https://fb-akademie.de/impressum"
    legal_datenschutz_url: str = "https://fb-akademie.de/datenschutz"

    # Default: leer = bundesweit, kein Region-Bonus/Strafe im Scoring.
    # Per .env auf Bundeslaender-Liste setzen, wenn man Region-Praeferenz
    # haben will (z.B. 'Sachsen,Sachsen-Anhalt,Thüringen').
    target_regions: str = ""
    # Schwelle ab der ein Tender als 'high' eingestuft wird. 60 erlaubt es,
    # dass ein einzelner Kernthema-Treffer (z.B. nur 'Tiefbau' im Titel,
    # ohne ZFSV/Fluessigboden) bereits HIGH-relevant wird.
    high_relevance_threshold: int = 60

    # ---- Auth ---------------------------------------------------------
    admin_username: str = "admin"
    admin_password: str = "fbe-admin-bitte-aendern"
    # Wenn leer wird beim Start ein zufaelliger Wert gesetzt (Sessions
    # ueberleben dann allerdings keinen Restart).
    session_secret: str = ""
    # True sobald HTTPS aktiv ist - Cookies werden dann nur noch ueber HTTPS
    # gesendet. Vor dem ersten Cert auf False lassen, sonst sperrt man
    # sich aus.
    session_https_only: bool = False

    scraper_user_agent: str = (
        "FBE-Ausschreibungsbot/1.0 (+kontakt@fluessigboden-engineering.de)"
    )
    http_timeout: int = 30

    # robots.txt global ignorieren. Default False (rechtssicher).
    # Wenn True, wird auf KEINEM Portal mehr robots.txt geprueft.
    # Empfohlen: lieber pro Portal in portals.yaml setzen
    #   config: { ignore_robots: true }
    # Damit nur dort umgangen wird, wo bewusst entschieden.
    ignore_robots_global: bool = False

    # ---- KI-Anbindung (OpenAI) ---------------------------------------
    openai_api_key: str = ""
    openai_model: str = "gpt-4o-mini"
    # Optional: eigener Base-URL (z.B. fuer Azure, lokale Proxies).
    openai_base_url: str = ""
    # Cache: Wenn True wird die Tender-Analyse beim ersten Oeffnen einer
    # Detailseite einmal ausgefuehrt und gespeichert - jeder weitere
    # Aufruf kostet nichts.
    ai_auto_analyze: bool = True

    # ---- KI-Anbindung (Claude / Anthropic) ---------------------------
    # Tiefe Tender-Analyse: laeuft auf Knopfdruck im Detail-View und
    # erzeugt Einsparungspotenzial + Fluessigboden-Eignung. Nutzt eine
    # andere Engine als die Enricher-Anreicherung, damit man zwei
    # unabhaengige Quellen vergleichen kann.
    anthropic_api_key: str = ""
    anthropic_model: str = "claude-sonnet-4-6"

    # ---- Enricher (separater Docker-Container) -----------------------
    # Shared-Secret fuer /api/internal/* - der Enricher schickt diesen
    # Token im Header X-Internal-Token. Wenn leer, sind die Endpoints
    # deaktiviert (= kein Enricher angebunden).
    enricher_token: str = ""
    # Verzeichnis fuer Markdown-Anreicherungen. Wird vom Enricher
    # geschrieben und von fbe-tender beim Detail-View gelesen.
    enrich_dir: str = "/srv/fbe-enrich"
    # Verzeichnis fuer redaktionelle Notizen + KI-Wissensbasis.
    # Alles aus diesem Ordner kann der Admin im Notes-Editor bearbeiten,
    # und alle Dateien werden dem Enricher als zusaetzlicher Kontext
    # an das LLM uebergeben.
    knowledge_dir: str = "/srv/fbe-knowledge"
    # Vom Enricher heruntergeladene Vergabeunterlagen (PDF/DOCX/XLSX/ZIP).
    # Unterordner pro Tender-ID.
    attachment_dir: str = "/srv/fbe-attachments"

    @property
    def regions_list(self) -> List[str]:
        return [r.strip() for r in self.target_regions.split(",") if r.strip()]


settings = Settings()
