"""Portal-Konfiguration aus config/portals.yaml."""
from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import List

import yaml

from .config import PROJECT_ROOT


CONFIG_PATH = PROJECT_ROOT / "config" / "portals.yaml"


@dataclass
class PortalConfig:
    name: str
    enabled: bool
    scraper: str
    base_url: str
    strategy: str
    notes: str = ""


@lru_cache(maxsize=1)
def load_portals(path: Path | None = None) -> List[PortalConfig]:
    p = Path(path) if path else CONFIG_PATH
    with p.open("r", encoding="utf-8") as f:
        raw = yaml.safe_load(f) or {}
    return [
        PortalConfig(
            name=item["name"],
            enabled=bool(item.get("enabled", True)),
            scraper=item["scraper"],
            base_url=item.get("base_url", ""),
            strategy=item.get("strategy", "scrape"),
            notes=item.get("notes", ""),
        )
        for item in raw.get("portals", [])
    ]


def enabled_portals() -> List[PortalConfig]:
    return [p for p in load_portals() if p.enabled]
