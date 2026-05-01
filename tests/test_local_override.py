"""Tests fuer den Local-Override-Mechanismus von portals.yaml."""
from __future__ import annotations

import yaml
from pathlib import Path

from backend import yaml_store, portal_config


def test_merge_portals_overrides_existing_by_name():
    base = {"portals": [
        {"name": "A", "enabled": True, "scraper": "x"},
        {"name": "B", "enabled": True, "scraper": "y"},
    ]}
    local = {"portals": [
        {"name": "B", "enabled": False, "scraper": "y"},  # toggle
    ]}
    result = yaml_store._merge_portals(base, local)
    assert len(result["portals"]) == 2
    assert result["portals"][0]["name"] == "A"  # Reihenfolge bleibt
    assert result["portals"][1]["name"] == "B"
    assert result["portals"][1]["enabled"] is False


def test_merge_portals_appends_new_names():
    base = {"portals": [{"name": "A", "enabled": True, "scraper": "x"}]}
    local = {"portals": [{"name": "Z", "enabled": True, "scraper": "z"}]}
    result = yaml_store._merge_portals(base, local)
    assert [p["name"] for p in result["portals"]] == ["A", "Z"]


def test_diff_portals_returns_only_changed():
    base = {"portals": [
        {"name": "A", "enabled": True, "scraper": "x"},
        {"name": "B", "enabled": True, "scraper": "y"},
    ]}
    full = {"portals": [
        {"name": "A", "enabled": True, "scraper": "x"},  # unchanged
        {"name": "B", "enabled": False, "scraper": "y"},  # toggled
    ]}
    diff = yaml_store._diff_portals(base, full)
    assert len(diff["portals"]) == 1
    assert diff["portals"][0]["name"] == "B"
    assert diff["portals"][0]["enabled"] is False


def test_diff_portals_empty_when_identical():
    base = {"portals": [{"name": "A", "enabled": True, "scraper": "x"}]}
    full = base
    diff = yaml_store._diff_portals(base, full)
    assert diff == {}


def test_load_portals_applies_local_override(tmp_path, monkeypatch):
    """Wenn portals.local.yaml existiert, wird sie ueber portals.yaml gelegt."""
    base = tmp_path / "portals.yaml"
    local = tmp_path / "portals.local.yaml"
    base.write_text(yaml.safe_dump({"portals": [
        {"name": "A", "enabled": True, "scraper": "x", "base_url": "https://a"},
        {"name": "B", "enabled": True, "scraper": "y", "base_url": "https://b"},
    ]}))
    local.write_text(yaml.safe_dump({"portals": [
        {"name": "B", "enabled": False, "scraper": "y", "base_url": "https://b"},
    ]}))

    monkeypatch.setattr(portal_config, "CONFIG_PATH", base)
    monkeypatch.setattr(portal_config, "LOCAL_CONFIG_PATH", local)
    portal_config.load_portals.cache_clear()

    portals = portal_config.load_portals()
    assert len(portals) == 2
    a = next(p for p in portals if p.name == "A")
    b = next(p for p in portals if p.name == "B")
    assert a.enabled is True
    assert b.enabled is False  # Override hat gegriffen


def test_write_portals_creates_local_only_for_diff(tmp_path, monkeypatch):
    """Beim Schreiben darf NUR die Diff in local landen, base bleibt unangetastet."""
    base = tmp_path / "portals.yaml"
    local = tmp_path / "portals.local.yaml"
    base.write_text(yaml.safe_dump({"portals": [
        {"name": "A", "enabled": True, "scraper": "x", "base_url": "a"},
        {"name": "B", "enabled": True, "scraper": "y", "base_url": "b"},
    ]}))

    monkeypatch.setattr(yaml_store, "PORTALS_PATH", base)
    monkeypatch.setattr(yaml_store, "PORTALS_LOCAL_PATH", local)
    monkeypatch.setattr(yaml_store, "_invalidate_portal_cache", lambda: None)

    full = {"portals": [
        {"name": "A", "enabled": True, "scraper": "x", "base_url": "a"},  # unchanged
        {"name": "B", "enabled": False, "scraper": "y", "base_url": "b"},  # toggled
    ]}
    yaml_store.write_portals(full)

    # Base unveraendert
    base_after = yaml.safe_load(base.read_text())
    assert base_after["portals"][1]["enabled"] is True
    # Local enthaelt nur das geaenderte Portal B
    local_after = yaml.safe_load(local.read_text())
    assert len(local_after["portals"]) == 1
    assert local_after["portals"][0]["name"] == "B"
    assert local_after["portals"][0]["enabled"] is False


def test_write_portals_removes_local_when_back_to_base(tmp_path, monkeypatch):
    """Wenn der User Aenderungen wieder zurueckdreht, soll local geloescht werden."""
    base = tmp_path / "portals.yaml"
    local = tmp_path / "portals.local.yaml"
    base.write_text(yaml.safe_dump({"portals": [
        {"name": "A", "enabled": True, "scraper": "x"},
    ]}))
    local.write_text(yaml.safe_dump({"portals": [
        {"name": "A", "enabled": False, "scraper": "x"},
    ]}))

    monkeypatch.setattr(yaml_store, "PORTALS_PATH", base)
    monkeypatch.setattr(yaml_store, "PORTALS_LOCAL_PATH", local)
    monkeypatch.setattr(yaml_store, "_invalidate_portal_cache", lambda: None)

    # Schreibe wieder den Base-Stand
    yaml_store.write_portals({"portals": [{"name": "A", "enabled": True, "scraper": "x"}]})
    assert not local.exists()
