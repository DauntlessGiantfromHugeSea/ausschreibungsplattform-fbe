"""Sanity-Checks fuer Portal-Konfigurationen.

Einige Scraper sind fest an ein bestimmtes Portal gebunden (hartkodierte
Pfade / APIs). Wird so ein Scraper versehentlich einem fremden Portal
zugewiesen (z.B. ueber das Dropdown im Portal-Editor), liefert er nur
404s. Dieses Modul erkennt solche Fehlpaarungen und repariert sie -
beim App-Start und beim Speichern im Admin-UI.
"""
from __future__ import annotations

import logging
from urllib.parse import urlparse


log = logging.getLogger(__name__)

# Scraper -> Host-Suffixe, auf denen er ausschliesslich funktioniert.
HOST_BOUND_SCRAPERS: dict[str, tuple[str, ...]] = {
    "bund": ("service.bund.de", "bund.de"),
    "ted": ("ted.europa.eu",),
    "dtvp": ("dtvp.de",),
    "evergabe_de": ("evergabe.de",),
    "sachsen": ("evergabe.sachsen.de",),
    "oeffentlichevergabe_api": ("oeffentlichevergabe.de",),
}

# Host-Suffix -> der Scraper, der fuer dieses Portal der richtige ist.
HOST_DEFAULT_SCRAPER: dict[str, str] = {
    "dtvp.de": "dtvp",
    "service.bund.de": "bund",
    "ted.europa.eu": "ted",
    "oeffentlichevergabe.de": "oeffentlichevergabe_api",
    "evergabe.sachsen.de": "sachsen",
    "evergabe.de": "evergabe_de",
}


def _host_of(base_url: str) -> str:
    try:
        return (urlparse(base_url or "").hostname or "").lower()
    except Exception:
        return ""


def _host_matches(host: str, suffix: str) -> bool:
    return host == suffix or host.endswith("." + suffix)


def check_pairing(scraper: str, base_url: str) -> tuple[bool, str | None]:
    """Prueft, ob scraper+base_url zusammenpassen.

    Liefert (ok, empfohlener_scraper_oder_None).
    ok=False bedeutet: die Kombination ist sicher kaputt.
    Der empfohlene Scraper ist gesetzt, wenn wir den richtigen kennen.
    """
    scraper = (scraper or "").strip()
    host = _host_of(base_url)
    if not scraper or not host:
        return True, None

    bound = HOST_BOUND_SCRAPERS.get(scraper)
    if bound and not any(_host_matches(host, s) for s in bound):
        # Scraper gehoert zu einem anderen Portal -> kaputt.
        for suffix, correct in HOST_DEFAULT_SCRAPER.items():
            if _host_matches(host, suffix):
                return False, correct
        return False, None

    # Umgekehrt: Host hat einen dedizierten Scraper, aber ein generischer
    # ist eingestellt -> nicht kaputt (generic kann klappen), kein Eingriff.
    return True, None


def sanitize_portals() -> list[str]:
    """Repariert offensichtlich kaputte Scraper-Zuordnungen in portals.yaml.

    Wird beim App-Start aufgerufen. Liefert die Liste der Korrekturen
    (Klartext, fuer Logging).
    """
    from . import yaml_store
    fixes: list[str] = []
    try:
        raw = yaml_store.read_portals_raw()
    except Exception as exc:
        log.warning("portal_sanity: portals.yaml nicht lesbar: %s", exc)
        return fixes

    portals = raw.get("portals", [])
    changed = False
    for p in portals:
        if not isinstance(p, dict):
            continue
        scraper = (p.get("scraper") or "").strip()
        base_url = p.get("base_url") or ""
        ok, recommended = check_pairing(scraper, base_url)
        if ok:
            continue
        name = p.get("name", "?")
        if recommended:
            p["scraper"] = recommended
            changed = True
            msg = (f"'{name}': Scraper '{scraper}' passt nicht zu {base_url} "
                   f"- automatisch auf '{recommended}' korrigiert")
            fixes.append(msg)
            log.warning("portal_sanity: %s", msg)
        else:
            msg = (f"'{name}': Scraper '{scraper}' ist an ein anderes Portal "
                   f"gebunden und liefert auf {base_url} nur Fehler. "
                   f"Bitte im Portal-Editor einen passenden Scraper waehlen "
                   f"(z.B. generic_html/crawl_html/playwright_html).")
            fixes.append(msg)
            log.warning("portal_sanity: %s", msg)

    if changed:
        try:
            yaml_store.write_portals(raw)
            log.info("portal_sanity: %d Korrektur(en) in portals.yaml geschrieben", len(fixes))
        except Exception as exc:
            log.warning("portal_sanity: Schreiben fehlgeschlagen: %s", exc)
    return fixes
