"""Tests fuer das Score-Breakdown."""
from __future__ import annotations

from datetime import datetime, timedelta

from backend.scoring import score_text


def test_breakdown_lists_cluster_hits(monkeypatch):
    # Region-Bonus nur wenn TARGET_REGIONS gesetzt - hier explizit setzen.
    from backend.config import settings
    monkeypatch.setattr(settings, "target_regions", "Sachsen-Anhalt")
    res = score_text(
        title="Verfuellung mit Fluessigboden Magdeburg",
        description="ZFSV gemaess RAL GZ 507",
        deadline=datetime.utcnow() + timedelta(days=20),
        region="Sachsen-Anhalt",
    )
    labels = [c.label for c in res.breakdown]
    assert any("Themencluster" in l for l in labels)
    assert any("Kernthema-Bonus" in l for l in labels)
    assert any("Frist" in l for l in labels)
    assert any("Zielregion" in l for l in labels)


def test_breakdown_sums_to_score_for_typical_case():
    res = score_text(
        title="Verfuellung mit Fluessigboden Magdeburg",
        description="ZFSV",
        deadline=datetime.utcnow() + timedelta(days=20),
        region="Sachsen-Anhalt",
    )
    total = sum(c.points for c in res.breakdown)
    assert total > 0
    # Score sollte voll durchgereicht werden.
    assert res.score >= 70


def test_breakdown_no_region_bonus_without_target_regions():
    """Default-Verhalten ohne TARGET_REGIONS: kein Region-Bonus, kein
    'ausserhalb'-Eintrag. Bundesweite Behandlung."""
    res = score_text(
        title="Verfuellung Fluessigboden",
        description="ZFSV",
        deadline=datetime.utcnow() + timedelta(days=10),
        region="Sachsen-Anhalt",
    )
    labels = [c.label for c in res.breakdown]
    assert not any("Zielregion" in l for l in labels)
    assert not any("ausserhalb" in l.lower() for l in labels)


def test_tiefbau_only_now_scores_high():
    """Seit Tiefbau-Cluster auf high steht: 'Tiefbauarbeiten' allein scort
    bereits >= 50 (medium-high)."""
    res = score_text(
        title="Tiefbauarbeiten Innenstadt",
        description="Bauleistungen nach VOB",
        deadline=datetime.utcnow() + timedelta(days=20),
    )
    assert res.score >= 50
    assert res.level in {"medium", "high"}


def test_breakdown_dicts_serialisable():
    res = score_text(title="Strassenbau", description="Asphalt")
    dicts = res.breakdown_dicts()
    assert isinstance(dicts, list)
    for d in dicts:
        assert set(d.keys()) == {"label", "points", "detail"}


def test_new_clusters_match_relevant_titles():
    """Spundwand, DIN-18300, Pflasterarbeiten muessen jetzt scoren."""
    from datetime import datetime, timedelta
    base = {"deadline": datetime.utcnow() + timedelta(days=15)}

    # Spundwand-Cluster (high)
    res = score_text(
        title="Spundwandarbeiten Hafen Hamburg",
        description="Verbau und Spundwand Verfuellung",
        **base,
    )
    assert res.score >= 50
    matched = " ".join(res.matched_terms).lower()
    assert "spundwand" in matched

    # DIN-18300-Cluster (medium)
    res = score_text(
        title="Tiefbauarbeiten DIN 18300 18306 18315",
        description="Bauleistungen",
        **base,
    )
    assert res.score >= 30
    matched = " ".join(res.matched_terms).lower()
    assert any("din 1830" in t.lower() or "din 18306" in t.lower() or "din 18315" in t.lower()
               for t in res.matched_terms)

    # Pflasterarbeiten (im Tiefbau-Cluster)
    res = score_text(
        title="Pflasterarbeiten Hauptstraße",
        description="Auftrag zu Pflaster und Fugenarbeiten",
        **base,
    )
    assert res.score >= 20
    matched = " ".join(res.matched_terms).lower()
    assert "pflaster" in matched


def test_breakdown_region_outside_target_no_bonus(monkeypatch):
    from backend.config import settings
    monkeypatch.setattr(settings, "target_regions", "Sachsen,Brandenburg")
    res = score_text(
        title="Fluessigboden Verfuellung",
        description="ZFSV",
        region="Hawaii",
        deadline=datetime.utcnow() + timedelta(days=10),
    )
    labels = [c.label for c in res.breakdown]
    assert any("ausserhalb" in l.lower() for l in labels)
    region_comp = next(c for c in res.breakdown if "Region" in c.label)
    assert region_comp.points == 0
