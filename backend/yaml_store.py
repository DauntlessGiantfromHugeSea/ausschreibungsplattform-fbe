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
TERMS_PATH = PROJECT_ROOT / "config" / "search_terms.yaml"


def read_portals_raw() -> dict:
    return _load(PORTALS_PATH)


def read_terms_raw() -> dict:
    return _load(TERMS_PATH)


def write_portals(data: dict) -> None:
    _dump(PORTALS_PATH, data)
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


def _invalidate_portal_cache() -> None:
    from . import portal_config
    portal_config.load_portals.cache_clear()


def _invalidate_terms_cache() -> None:
    from . import search_terms
    search_terms.load_search_config.cache_clear()
