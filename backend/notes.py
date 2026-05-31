"""Sichere File-Operations fuer den Notes-/Wissens-Editor.

Erlaubt nur Pfade innerhalb der konfigurierten Wurzelverzeichnisse
(enrich_dir + knowledge_dir). Path-Traversal via '..' wird abgefangen.
"""
from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Optional

from .config import settings


# Sichtbare Roots im Notes-Editor. Slug -> (Anzeigename, Pfad, beschreibbar).
def get_roots() -> dict[str, tuple[str, Path, bool]]:
    return {
        "knowledge": ("Wissensbasis (KI-Kontext)", Path(settings.knowledge_dir), True),
        "enrichments": ("Tender-Anreicherungen", Path(settings.enrich_dir), True),
    }


def safe_path(root_key: str, name: str) -> Optional[Path]:
    """Liefert den abolut-aufgeloesten Pfad innerhalb der angegebenen Root
    oder None bei Traversal-Versuch / unbekannter Root.

    `name` darf nur ein .md-Dateiname sein - keine Unterverzeichnisse.
    """
    if not name or not name.endswith(".md"):
        return None
    if "/" in name or "\\" in name or ".." in name:
        return None
    roots = get_roots()
    if root_key not in roots:
        return None
    _, root, _ = roots[root_key]
    try:
        root.mkdir(parents=True, exist_ok=True)
        p = (root / name).resolve()
        p.relative_to(root.resolve())
        return p
    except (ValueError, OSError):
        return None


def list_files(root_key: str) -> list[dict]:
    """Listet alle .md-Dateien in der Root. Read-only, sortiert nach mtime DESC."""
    roots = get_roots()
    if root_key not in roots:
        return []
    _, root, _ = roots[root_key]
    if not root.is_dir():
        return []
    out = []
    for f in root.glob("*.md"):
        try:
            st = f.stat()
        except OSError:
            continue
        out.append({
            "name": f.name,
            "size_kb": round(st.st_size / 1024, 1),
            "modified_iso": datetime.utcfromtimestamp(st.st_mtime).isoformat(timespec="seconds"),
            "preview": _first_line(f),
        })
    out.sort(key=lambda x: x["modified_iso"], reverse=True)
    return out


def _first_line(path: Path, max_chars: int = 80) -> str:
    """Holt die erste nicht-leere Zeile (ohne #-Praefix), gekuerzt."""
    try:
        with path.open("r", encoding="utf-8", errors="replace") as fh:
            for line in fh:
                line = line.strip().lstrip("#").strip()
                if line:
                    return line[:max_chars]
    except OSError:
        return ""
    return ""


def read_file(root_key: str, name: str) -> Optional[str]:
    p = safe_path(root_key, name)
    if not p or not p.is_file():
        return None
    try:
        return p.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None


def write_file(root_key: str, name: str, content: str) -> bool:
    p = safe_path(root_key, name)
    if not p:
        return False
    roots = get_roots()
    _, _, writable = roots[root_key]
    if not writable:
        return False
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
        return True
    except OSError:
        return False


def delete_file(root_key: str, name: str) -> bool:
    p = safe_path(root_key, name)
    if not p or not p.is_file():
        return False
    try:
        p.unlink()
        return True
    except OSError:
        return False


def search(query: str, max_results: int = 30) -> list[dict]:
    """Volltextsuche ueber alle Roots. Liefert {root, name, snippet}."""
    q = (query or "").strip().lower()
    if not q:
        return []
    out = []
    for root_key, (label, root, _) in get_roots().items():
        if not root.is_dir():
            continue
        for f in root.glob("*.md"):
            try:
                text = f.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            tl = text.lower()
            if q not in tl and q not in f.name.lower():
                continue
            idx = tl.find(q)
            if idx >= 0:
                start = max(0, idx - 40)
                end = min(len(text), idx + len(q) + 80)
                snippet = text[start:end].replace("\n", " ")
            else:
                snippet = (text[:120] or "").replace("\n", " ")
            out.append({
                "root_key": root_key, "root_label": label,
                "name": f.name, "snippet": snippet,
            })
            if len(out) >= max_results:
                return out
    return out


def render_markdown(text: str) -> str:
    """Markdown -> HTML. Bevorzugt 'markdown'-Lib, fallback einfach <pre>."""
    try:
        import markdown as _md
        return _md.markdown(text, extensions=["fenced_code", "tables"])
    except ImportError:
        from html import escape
        return f"<pre style='white-space:pre-wrap'>{escape(text)}</pre>"
