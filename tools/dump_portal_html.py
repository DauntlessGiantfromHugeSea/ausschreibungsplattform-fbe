"""HTML-Dump aller konfigurierten Portale fuer Debug-Zwecke.

Aufruf auf dem VPS:

    cd /root/ausschreibungsplattform-fbe
    source .venv/bin/activate
    python tools/dump_portal_html.py

Effekt:
    /tmp/portal-debug/<portal-slug>__<path>.html  - rohe HTML-Antworten
    /tmp/portal-debug/_summary.txt                - Status, Groesse, Fehler
    /tmp/portal-debug.zip                         - alles gepackt zum Versenden

Schickst du mir das ZIP, sehe ich genau was die Server liefern, und kann
passgenaue Selektoren / Parser bauen.
"""
from __future__ import annotations

import re
import shutil
import sys
from datetime import datetime
from pathlib import Path
from urllib.parse import urljoin, urlsplit

# Projekt-Root in sys.path
ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backend.portal_config import load_portals  # noqa: E402
from scrapers.base import BaseScraper  # noqa: E402


OUT_DIR = Path("/tmp/portal-debug")
ZIP_PATH = Path("/tmp/portal-debug.zip")


def _slug(name: str) -> str:
    return re.sub(r"[^a-zA-Z0-9_.-]+", "-", name).strip("-").lower()


def _path_slug(url: str) -> str:
    p = urlsplit(url)
    s = (p.path + ("_" + p.query if p.query else "")) or "_root"
    return re.sub(r"[^a-zA-Z0-9_.-]+", "_", s).strip("_")[:80] or "_root"


def _candidate_urls(portal) -> list[str]:
    """URLs, die wir pro Portal anhitten - aus listing_paths oder search_path."""
    from urllib.parse import urlencode
    cfg = portal.config or {}
    out: list[str] = []
    for path in (cfg.get("listing_paths") or []):
        out.append(urljoin(portal.base_url + "/", path.lstrip("/")))
    sp = cfg.get("search_path")
    if sp:
        out.append(urljoin(portal.base_url + "/", sp.replace("{term}", "Tiefbau").lstrip("/")))
    # bund-Scraper baut die URL aus listing_params - hier nachbilden, damit
    # wir die echte gefilterte Bauleistungs-URL dumpen, nicht nur die Landingpage.
    if portal.scraper == "bund" and cfg.get("listing_params"):
        params = {k: str(v) for k, v in cfg["listing_params"].items()}
        out.append("{}{}?{}".format(
            portal.base_url,
            "/Content/DE/Ausschreibungen/Suche/Formular.html",
            urlencode(params),
        ))
    if not out:
        out.append(portal.base_url + "/")
    return out[:3]  # max 3 URLs pro Portal


def main() -> int:
    if OUT_DIR.exists():
        shutil.rmtree(OUT_DIR)
    OUT_DIR.mkdir(parents=True)

    portals = [p for p in load_portals() if p.enabled]
    summary_lines = [
        "Portal-HTML-Dump · {}".format(datetime.utcnow().isoformat()),
        "Insgesamt {} aktive Portale".format(len(portals)),
        "",
    ]

    for portal in portals:
        slug = _slug(portal.name)
        summary_lines.append("=" * 70)
        summary_lines.append("PORTAL: {}".format(portal.name))
        summary_lines.append("  scraper:  {}".format(portal.scraper))
        summary_lines.append("  base_url: {}".format(portal.base_url))

        try:
            scraper = BaseScraper(
                base_url=portal.base_url, name=portal.name, config=portal.config,
            )
        except Exception as exc:
            summary_lines.append("  INIT-FEHLER: {}".format(exc))
            continue

        try:
            for url in _candidate_urls(portal):
                fname = "{}__{}.html".format(slug, _path_slug(url))
                fpath = OUT_DIR / fname
                summary_lines.append("")
                summary_lines.append("  GET {}".format(url))
                summary_lines.append("      -> {}".format(fname))
                try:
                    resp = scraper.get(url)
                    fpath.write_bytes(resp.content)
                    summary_lines.append("      HTTP {} · {} bytes · final={}".format(
                        resp.status_code,
                        len(resp.content),
                        str(resp.url) if str(resp.url) != url else "(unchanged)",
                    ))
                    if resp.headers.get("content-type"):
                        summary_lines.append("      Content-Type: {}".format(
                            resp.headers["content-type"]))
                except Exception as exc:
                    summary_lines.append("      FEHLER: {}: {}".format(
                        type(exc).__name__, str(exc)[:200]))
                    fpath.write_text(
                        "FEHLER: {}: {}".format(type(exc).__name__, exc),
                        encoding="utf-8",
                    )
        finally:
            scraper.close()

    summary = "\n".join(summary_lines) + "\n"
    (OUT_DIR / "_summary.txt").write_text(summary, encoding="utf-8")
    print(summary)

    # ZIP erstellen
    if ZIP_PATH.exists():
        ZIP_PATH.unlink()
    shutil.make_archive(str(ZIP_PATH.with_suffix("")), "zip", str(OUT_DIR))
    size_kb = ZIP_PATH.stat().st_size // 1024
    print("=" * 70)
    print("FERTIG. {} portale, {} dateien.".format(
        len(portals),
        len(list(OUT_DIR.glob("*.html"))),
    ))
    print("Archiv: {} ({} KB)".format(ZIP_PATH, size_kb))
    print()
    print("Zum Versenden:")
    print("  scp root@<VPS>:{}  ./".format(ZIP_PATH))
    print("oder direkt vom VPS aus:")
    print("  base64 {} | head -c 80".format(ZIP_PATH))
    return 0


if __name__ == "__main__":
    sys.exit(main())
