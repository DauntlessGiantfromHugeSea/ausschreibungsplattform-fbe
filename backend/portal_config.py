"""Portal-Konfiguration aus config/portals.yaml."""
from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import List

import yaml

from .config import PROJECT_ROOT


CONFIG_PATH = PROJECT_ROOT / "config" / "portals.yaml"
LOCAL_CONFIG_PATH = PROJECT_ROOT / "config" / "portals.local.yaml"


@dataclass
class PortalConfig:
    name: str
    enabled: bool
    scraper: str
    base_url: str
    strategy: str
    notes: str = ""
    config: dict = None  # type: ignore[assignment]

    def __post_init__(self):
        if self.config is None:
            self.config = {}


@lru_cache(maxsize=1)
def load_portals(path: Path | None = None) -> List[PortalConfig]:
    p = Path(path) if path else CONFIG_PATH
    with p.open("r", encoding="utf-8") as f:
        raw = yaml.safe_load(f) or {}

    # Lokale Overrides nur fuer den Default-Pfad anwenden (nicht in Tests
    # mit explizitem path-Parameter).
    if path is None and LOCAL_CONFIG_PATH.exists():
        try:
            with LOCAL_CONFIG_PATH.open("r", encoding="utf-8") as f:
                local = yaml.safe_load(f) or {}
            raw = _merge_local(raw, local)
        except Exception:  # pragma: no cover
            pass

    return [
        PortalConfig(
            name=item["name"],
            enabled=bool(item.get("enabled", True)),
            scraper=item["scraper"],
            base_url=item.get("base_url", ""),
            strategy=item.get("strategy", "scrape"),
            notes=item.get("notes", ""),
            config=item.get("config", {}) or {},
        )
        for item in raw.get("portals", [])
    ]


def _merge_local(base: dict, local: dict) -> dict:
    """Merge analog zu yaml_store._merge_portals - Override-Portale per
    name in die Liste ein-/draufpatchen, Reihenfolge der Base bewahren."""
    base_portals = list(base.get("portals", []) or [])
    local_portals = list(local.get("portals", []) or [])
    by_name = {p.get("name"): i for i, p in enumerate(base_portals) if p.get("name")}
    result = list(base_portals)
    for lp in local_portals:
        name = lp.get("name")
        if not name:
            continue
        if name in by_name:
            result[by_name[name]] = lp
        else:
            result.append(lp)
    out = dict(base)
    out["portals"] = result
    return out


def enabled_portals() -> List[PortalConfig]:
    return [p for p in load_portals() if p.enabled]
