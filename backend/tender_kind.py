"""Klassifikation: Bauvorhaben vs. Planungsleistung.

Wird beim Speichern eines Tenders berechnet (Tender.kind) und treibt
den Art-Filter im Dashboard ('Bau' / 'Planung' / 'Beide').

Heuristik:
  1. CPV 71xxx  -> Planung (EU-Kategorie Ingenieur-/Planungsleistungen)
  2. Planungs-Vokabular in Titel/Beschreibung -> Planung
  3. sonst -> Bau
"""
from __future__ import annotations

import re


KIND_BAU = "bau"
KIND_PLANUNG = "planung"

_PLANNING_RE = re.compile(
    r"(planungsleistung|objektplanung|fachplanung|generalplan|tragwerksplanung|"
    r"entwurfsplanung|genehmigungsplanung|ausf(ü|ue)hrungsplanung|"
    r"leistungsphase|hoai|ingenieurleistung|ingenieurvertrag|"
    r"trassenplanung|leitungsplanung|kanalplanung|verkehrsanlagenplanung|"
    r"bau(ü|ue)berwachung|bauoberleitung|baugrundgutachten|geotechnisch|"
    r"machbarkeitsstudie|variantenuntersuchung|vgv-verfahren|sigeko|"
    r"sicherheits-\s*und\s*gesundheitsschutz|freiberufliche\s+leistung|"
    r"vermessungsleistung|planungswettbewerb|architektenleistung|"
    r"gutachten|studie\b)",
    re.IGNORECASE,
)


def classify_kind(title: str | None, description: str | None,
                  cpv_codes: list | str | None) -> str:
    """Liefert 'planung' oder 'bau'."""
    codes: list[str] = []
    if isinstance(cpv_codes, str):
        codes = [c.strip() for c in cpv_codes.split(";") if c.strip()]
    elif cpv_codes:
        codes = [str(c).strip() for c in cpv_codes if str(c).strip()]
    for c in codes:
        if c.startswith("71"):
            return KIND_PLANUNG
    hay = f"{title or ''} {description or ''}"
    if _PLANNING_RE.search(hay):
        return KIND_PLANUNG
    return KIND_BAU
