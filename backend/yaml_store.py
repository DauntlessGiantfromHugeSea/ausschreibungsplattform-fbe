"""Lese-/Schreib-Layer fuer die YAML-Konfigs (Portale, Suchbegriffe).

- Schreibt atomar (tmp -> rename) und legt vorher ein .bak an.
- Validiert via yaml.safe_load.
- Invalidiert die @lru_cache der Reader (portal_config, search_terms).
"""
from __future__ import annotations

import logging
import shutil
import tempfile
from pathlib import Path
from typing import Any

import yaml

from .config import PROJECT_ROOT


log = logging.getLogger(__name__)

PORTALS_PATH = PROJECT_ROOT / "config" / "portals.yaml"
PORTALS_LOCAL_PATH = PROJECT_ROOT / "config" / "portals.local.yaml"
TERMS_PATH = PROJECT_ROOT / "config" / "search_terms.yaml"


def read_portals_raw() -> dict:
    """Liest portals.yaml. Falls portals.local.yaml existiert, mergt es
    drueber. So bleiben Admin-UI-Edits aus dem git fern und kollidieren
    nicht mit git pulls."""
    base = _load(PORTALS_PATH)
    local = _load(PORTALS_LOCAL_PATH) if PORTALS_LOCAL_PATH.exists() else {}
    if local:
        return _merge_portals(base, local)
    return base


def read_terms_raw() -> dict:
    return _load(TERMS_PATH)


def write_portals(data: dict) -> None:
    """Admin-UI schreibt seine Aenderungen in portals.local.yaml.

    Damit bleibt die im git getrackte portals.yaml unveraendert und
    'git pull' kollidiert nicht mehr mit UI-Toggles. Vor dem ersten
    Schreiben wird der lokale Override aus dem Diff Base->Daten gebaut.
    """
    base = _load(PORTALS_PATH)
    local_only = _diff_portals(base, data)
    if local_only.get("portals"):
        _dump(PORTALS_LOCAL_PATH, local_only)
    elif PORTALS_LOCAL_PATH.exists():
        # Wenn alle Aenderungen wieder mit Base identisch sind, Override entfernen.
        PORTALS_LOCAL_PATH.unlink()
    _invalidate_portal_cache()


def write_terms(data: dict) -> None:
    _dump(TERMS_PATH, data)
    _invalidate_terms_cache()


def parse_yaml_string(text: str) -> dict:
    """Validiert und parst einen YAML-Text."""
    data = yaml.safe_load(text)
    if data is None:
        return {}
    if not isinstance(data, dict):
        raise ValueError("YAML-Dokument muss ein Mapping (Top-Level dict) sein.")
    return data


def _load(path: Path) -> dict:
    if not path.exists():
        return {}
    with path.open("r", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def _dump(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        backup = path.with_suffix(path.suffix + ".bak")
        try:
            shutil.copy2(path, backup)
        except OSError as exc:  # pragma: no cover
            log.warning("Konnte Backup %s nicht anlegen: %s", backup, exc)

    fd, tmp_path_str = tempfile.mkstemp(prefix=path.name + ".", dir=str(path.parent))
    tmp_path = Path(tmp_path_str)
    try:
        with open(fd, "w", encoding="utf-8") as f:
            yaml.safe_dump(
                data, f,
                allow_unicode=True,
                sort_keys=False,
                default_flow_style=False,
                width=120,
            )
        tmp_path.replace(path)
    except Exception:
        if tmp_path.exists():
            tmp_path.unlink(missing_ok=True)
        raise


def _merge_portals(base: dict, local: dict) -> dict:
    """Ueberlagert local-Portale auf base. Match per name. Fehlende Portale
    in base werden hinzugefuegt; existierende werden ueberschrieben (komplette
    Portal-Konfig, nicht feldweise gemergt - Reihenfolge wie in base bewahrt).
    """
    base_portals = list(base.get("portals", []) or [])
    local_portals = list(local.get("portals", []) or [])
    by_name = {p.get("name"): i for i, p in enumerate(base_portals) if p.get("name")}

    result_list = list(base_portals)  # copy
    for lp in local_portals:
        name = lp.get("name")
        if not name:
            continue
        if name in by_name:
            result_list[by_name[name]] = lp
        else:
            result_list.append(lp)
    out = dict(base)
    out["portals"] = result_list
    return out


def _diff_portals(base: dict, full: dict) -> dict:
    """Ermittelt, welche Portale sich vom Base-Stand unterscheiden, und
    liefert NUR die geaenderten/zusaetzlichen als 'portals'-Liste fuer das
    local-Override-File."""
    base_portals = list(base.get("portals", []) or [])
    full_portals = list(full.get("portals", []) or [])
    by_name = {p.get("name"): p for p in base_portals if p.get("name")}

    diff: list[dict] = []
    for fp in full_portals:
        name = fp.get("name")
        if not name:
            continue
        bp = by_name.get(name)
        if bp != fp:
            diff.append(fp)
    return {"portals": diff} if diff else {}


def _invalidate_portal_cache() -> None:
    from . import portal_config
    portal_config.load_portals.cache_clear()


def _invalidate_terms_cache() -> None:
    from . import search_terms
    search_terms.load_search_config.cache_clear()
