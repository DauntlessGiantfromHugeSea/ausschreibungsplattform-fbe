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
class ScoreComponent:
    label: str
    points: float
    detail: str

    def to_dict(self) -> dict:
        return {"label": self.label, "points": self.points, "detail": self.detail}


@dataclass
class ScoreResult:
    score: float
    level: str  # high | medium | low
    matched_terms: List[str]
    breakdown: List[ScoreComponent]

    def breakdown_dicts(self) -> List[dict]:
        return [c.to_dict() for c in self.breakdown]


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
    breakdown: List[ScoreComponent] = []
    score = 0.0

    has_high_match = False
    for cluster in cfg.clusters:
        weight = WEIGHTS.get(cluster.weight, 1)
        hits = 0
        cluster_terms: List[str] = []
        for term in cluster.terms:
            if _term_matches(term, text):
                if term not in matched:
                    matched.append(term)
                cluster_terms.append(term)
                hits += 1
                if hits >= MAX_HITS_PER_CLUSTER:
                    break
        if hits > 0:
            pts = weight * hits
            score += pts
            breakdown.append(ScoreComponent(
                label="Themencluster '{}' ({})".format(cluster.name, cluster.weight),
                points=pts,
                detail="{}x Treffer * {} Punkte je Treffer - Begriffe: {}".format(
                    hits, weight, ", ".join(cluster_terms),
                ),
            ))
            if cluster.weight == "high":
                has_high_match = True

    # Baseline-Bonus, wenn das Kernthema (Fluessigboden / ZFSV / Verfuellung) getroffen ist.
    if has_high_match:
        score += 25
        breakdown.append(ScoreComponent(
            label="Kernthema-Bonus",
            points=25,
            detail="Mindestens ein Treffer im high-Cluster (Fluessigboden/ZFSV/...)",
        ))

    # CPV-Codes
    cpv_set = {str(c).strip() for c in (cpv_codes or []) if c}
    cpv_hits = sum(1 for c in cpv_set if c in cfg.cpv_codes)
    if cpv_hits:
        pts = min(cpv_hits, 2) * 5
        score += pts
        matched_cpvs = [c for c in cpv_set if c in cfg.cpv_codes]
        breakdown.append(ScoreComponent(
            label="CPV-Code-Treffer",
            points=pts,
            detail="{}x CPV-Match (max. 2 gewertet) - Codes: {}".format(
                cpv_hits, ", ".join(matched_cpvs),
            ),
        ))

    # Frist
    if deadline:
        days_left = (deadline - datetime.utcnow()).days
        if days_left > 7:
            score += 10
            breakdown.append(ScoreComponent(
                label="Frist > 7 Tage",
                points=10,
                detail="Angebotsfrist in {} Tagen - genug Zeit zur Bearbeitung".format(days_left),
            ))
        elif days_left > 0:
            score += 5
            breakdown.append(ScoreComponent(
                label="Frist innerhalb 7 Tage",
                points=5,
                detail="Angebotsfrist in {} Tagen - knapp".format(days_left),
            ))
        else:
            breakdown.append(ScoreComponent(
                label="Frist abgelaufen",
                points=0,
                detail="Frist liegt {} Tage in der Vergangenheit - kein Bonus".format(-days_left),
            ))

    # Zielregion
    if region:
        region_l = region.lower()
        if any(r.lower() == region_l for r in settings.regions_list):
            score += 10
            breakdown.append(ScoreComponent(
                label="Zielregion getroffen",
                points=10,
                detail="Bundesland '{}' steht in TARGET_REGIONS".format(region),
            ))
        else:
            breakdown.append(ScoreComponent(
                label="Region ausserhalb Zielgebiet",
                points=0,
                detail="'{}' ist nicht in TARGET_REGIONS ({})".format(
                    region, ", ".join(settings.regions_list),
                ),
            ))

    # Cap ohne high-cluster-Treffer
    if not has_high_match and score > 50:
        capped_from = score
        score = 50
        breakdown.append(ScoreComponent(
            label="Deckel (kein Kernthema-Treffer)",
            points=-(capped_from - 50),
            detail="Ohne Treffer im high-Cluster wird der Score auf 50 gedeckelt "
                   "(vorher {:.0f})".format(capped_from),
        ))

    score = max(0.0, min(100.0, score))
    level = _level_for(score)
    return ScoreResult(
        score=score, level=level, matched_terms=matched, breakdown=breakdown,
    )


def _term_matches(term: str, text: str) -> bool:
    """Case-insensitiver Match mit linker Wortgrenze, rechts kompositions-tolerant.

    Beispiel: Term 'Tiefbau' matcht 'Tiefbau', 'Tiefbauarbeiten',
    'Tiefbau-Arbeiten' und 'Tiefbau & Erdbau', NICHT aber 'Untertiefbau'
    (linke Grenze fehlt). Damit funktionieren deutsche Komposita.
    Bindestriche im Term werden zu [\\s\\-]+ - 'Flüssig boden' matcht.
    """
    pattern = re.escape(term.lower()).replace(r"\ ", r"[\s\-]+")
    # Linke Grenze: kein Buchstabe/Ziffer davor.
    # Rechts: keine zusaetzliche Restriktion - Komposita erlaubt.
    return re.search(rf"(?<![\w]){pattern}", text) is not None


def _level_for(score: float) -> str:
    if score >= settings.high_relevance_threshold:
        return "high"
    if score >= 40:
        return "medium"
    return "low"
