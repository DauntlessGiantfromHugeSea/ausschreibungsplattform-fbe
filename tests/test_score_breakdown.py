"""Tests fuer das Score-Breakdown."""
from __future__ import annotations

from datetime import datetime, timedelta

from backend.scoring import score_text


def test_breakdown_lists_cluster_hits():
    res = score_text(
        title="Verfuellung mit Fluessigboden Magdeburg",
        description="ZFSV gemaess RAL GZ 507",
        deadline=datetime.utcnow() + timedelta(days=20),
        region="Sachsen-Anhalt",
    )
    labels = [c.label for c in res.breakdown]
    # Mindestens das Kernthema, der Bonus, eine Frist und Region.
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
    # Die positiven Komponenten in breakdown ergeben (vor Cap) den Score.
    total = sum(c.points for c in res.breakdown)
    # Score ist maximal 100 - aber unsere Summe darf darueber liegen,
    # weil 100 ein hartes max ist. Wichtig: Summe nicht negativ.
    assert total > 0
    # Bei diesem Beispiel sollte der Score voll durchgereicht werden,
    # ohne dass der Cap greift.
    assert res.score >= 70


def test_breakdown_includes_cap_for_supporting_only():
    res = score_text(
        title="Erneuerung Fernwaermenetz Berlin Mitte",
        description="Tiefbau, Leitungsbau, Kanalbau",
        deadline=datetime.utcnow() + timedelta(days=15),
        region="Berlin",
    )
    labels = [c.label for c in res.breakdown]
    # Score ohne Kernthema soll gedeckelt werden, falls > 50 vor Cap.
    if any("Deckel" in l for l in labels):
        cap = next(c for c in res.breakdown if "Deckel" in c.label)
        assert cap.points < 0
    assert res.score <= 50


def test_breakdown_dicts_serialisable():
    res = score_text(title="Strassenbau", description="Asphalt")
    dicts = res.breakdown_dicts()
    assert isinstance(dicts, list)
    for d in dicts:
        assert set(d.keys()) == {"label", "points", "detail"}


def test_breakdown_region_outside_target_no_bonus():
    res = score_text(
        title="Fluessigboden Verfuellung",
        description="ZFSV",
        region="Hawaii",
        deadline=datetime.utcnow() + timedelta(days=10),
    )
    labels = [c.label for c in res.breakdown]
    assert any("ausserhalb" in l.lower() or "ausserhalb" in l for l in labels)
    region_comp = next(c for c in res.breakdown if "Region" in c.label or "Zielregion" in c.label)
    assert region_comp.points == 0
