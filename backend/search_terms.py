"""Lädt und kapselt die Suchbegriffe aus config/search_terms.yaml."""
from __future__ import annotations

from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import List

import yaml

from .config import PROJECT_ROOT


CONFIG_PATH = PROJECT_ROOT / "config" / "search_terms.yaml"
LOCAL_CONFIG_PATH = PROJECT_ROOT / "config" / "search_terms.local.yaml"


@dataclass
class TermCluster:
    name: str
    weight: str  # high | medium | low
    terms: List[str] = field(default_factory=list)


@dataclass
class SearchConfig:
    clusters: List[TermCluster]
    query_terms: List[str]
    cpv_codes: List[str]

    def all_terms(self) -> List[str]:
        out: List[str] = []
        for c in self.clusters:
            out.extend(c.terms)
        return out

    def cluster_for(self, term: str) -> TermCluster | None:
        term_l = term.lower()
        for c in self.clusters:
            if any(t.lower() == term_l for t in c.terms):
                return c
        return None


@lru_cache(maxsize=1)
def load_search_config(path: Path | None = None) -> SearchConfig:
    # Bei Default-Pfad: erst search_terms.local.yaml pruefen, sonst base.
    # Damit kann der User per Admin-UI eigene Suchbegriffe anlegen, ohne
    # dass git pull konflikten.
    if path is None and LOCAL_CONFIG_PATH.exists():
        p = LOCAL_CONFIG_PATH
    else:
        p = Path(path) if path else CONFIG_PATH
    with p.open("r", encoding="utf-8") as f:
        raw = yaml.safe_load(f) or {}
    clusters = [
        TermCluster(name=c["name"], weight=c.get("weight", "low"), terms=list(c.get("terms", [])))
        for c in raw.get("clusters", [])
    ]
    return SearchConfig(
        clusters=clusters,
        query_terms=list(raw.get("query_terms", [])),
        cpv_codes=[str(c) for c in raw.get("cpv_codes", [])],
    )
