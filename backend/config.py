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

    target_regions: str = "Sachsen,Brandenburg,Berlin,Sachsen-Anhalt,Thüringen"
    high_relevance_threshold: int = 70

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

    @property
    def regions_list(self) -> List[str]:
        return [r.strip() for r in self.target_regions.split(",") if r.strip()]


settings = Settings()
