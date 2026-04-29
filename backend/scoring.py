"""Relevanz-Scoring fuer Ausschreibungen.

Skala 0-100. Drei Stufen:
    high   >= 70
    medium 40-69
    low    <  40

Faktoren:
- Treffer pro Themencluster (Gewichtung high=15, medium=8, low=3)
- Mehrfachtreffer in einem Cluster begrenzt (max 2x pro Cluster)
- Frist in der Zukunft: +5 (Frist > 7d sogar +10)
- Zielregion getroffen: +10
- CPV-Code-Match: +5 pro Match (max 10)
- Kein Treffer im high-Cluster → Score auf max. 50 gedeckelt
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Iterable, List, Tuple

from .config import settings
from .search_terms import SearchConfig, load_search_config


WEIGHTS = {"high": 15, "medium": 8, "low": 3}
MAX_HITS_PER_CLUSTER = 2


@dataclass
class ScoreResult:
    score: float
    level: str  # high | medium | low
    matched_terms: List[str]


def _haystack(*parts: str | None) -> str:
    return " ".join(p for p in parts if p).lower()


def score_text(
    title: str | None,
    description: str | None,
    cpv_codes: Iterable[str] | None = None,
    region: str | None = None,
    deadline: datetime | None = None,
    config: SearchConfig | None = None,
) -> ScoreResult:
    cfg = config or load_search_config()
    text = _haystack(title, description)
    matched: List[str] = []
    score = 0.0

    has_high_match = False
    for cluster in cfg.clusters:
        weight = WEIGHTS.get(cluster.weight, 1)
        hits = 0
        for term in cluster.terms:
            if _term_matches(term, text):
                if term not in matched:
                    matched.append(term)
                hits += 1
                if hits >= MAX_HITS_PER_CLUSTER:
                    break
        if hits > 0:
            score += weight * hits
            if cluster.weight == "high":
                has_high_match = True

    # Baseline-Bonus, wenn das Kernthema (Fluessigboden / ZFSV / Verfuellung) getroffen ist.
    if has_high_match:
        score += 25

    # CPV-Codes
    cpv_set = {str(c).strip() for c in (cpv_codes or []) if c}
    cpv_hits = sum(1 for c in cpv_set if c in cfg.cpv_codes)
    if cpv_hits:
        score += min(cpv_hits, 2) * 5

    # Frist
    if deadline:
        days_left = (deadline - datetime.utcnow()).days
        if days_left > 7:
            score += 10
        elif days_left > 0:
            score += 5

    # Zielregion
    if region:
        region_l = region.lower()
        if any(r.lower() == region_l for r in settings.regions_list):
            score += 10

    # Cap ohne high-cluster-Treffer
    if not has_high_match:
        score = min(score, 50)

    score = max(0.0, min(100.0, score))
    level = _level_for(score)
    return ScoreResult(score=score, level=level, matched_terms=matched)


def _term_matches(term: str, text: str) -> bool:
    """Case-insensitiver Match auf Wortgrenzen, mit Toleranz bei Bindestrichen."""
    pattern = re.escape(term.lower()).replace(r"\ ", r"[\s\-]+")
    return re.search(rf"(?<![\w]){pattern}(?![\w])", text) is not None


def _level_for(score: float) -> str:
    if score >= settings.high_relevance_threshold:
        return "high"
    if score >= 40:
        return "medium"
    return "low"
