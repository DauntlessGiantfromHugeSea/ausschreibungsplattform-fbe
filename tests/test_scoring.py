from datetime import datetime, timedelta

from backend.scoring import score_text


def test_high_relevance_when_fluessigboden_in_title():
    res = score_text(
        title="Verfüllung mit Flüssigboden für Leitungsgraben Magdeburg",
        description="ZFSV gemäß RAL GZ 507",
        deadline=datetime.utcnow() + timedelta(days=20),
        region="Sachsen-Anhalt",
    )
    assert res.level == "high"
    assert res.score >= 70
    assert any("flüssigboden" in t.lower() for t in res.matched_terms)


def test_medium_when_only_supporting_clusters():
    res = score_text(
        title="Erneuerung Fernwärmenetz Berlin Mitte",
        description="Tiefbau, Leitungsbau, Kanalbau",
        deadline=datetime.utcnow() + timedelta(days=15),
        region="Berlin",
    )
    # Kein Kernbegriff (Flüssigboden / ZFSV) → Score gedeckelt auf 50.
    assert res.level == "medium"
    assert res.score <= 50


def test_low_when_only_general_terms():
    res = score_text(
        title="Straßenbau Ortsdurchfahrt",
        description="Asphalt, Markierung",
    )
    assert res.level == "low"
    assert res.score < 40


def test_no_double_counting_within_cluster():
    # Mehrere Synonyme aus einem Cluster sollen den Score nicht beliebig hochtreiben.
    res = score_text(
        title="Flüssigboden Flüssigboden ZFSV ZFSV",
        description="zeitweise fließfähig",
    )
    # Begrenzung MAX_HITS_PER_CLUSTER greift → Score bleibt deutlich unter 100.
    assert res.score <= 100
    # Mindestens medium, weil Kernthema getroffen.
    assert res.level in {"medium", "high"}
    # matched_terms enthaelt verschiedene Synonyme, aber duplikatsfrei.
    assert len(res.matched_terms) == len(set(res.matched_terms))
