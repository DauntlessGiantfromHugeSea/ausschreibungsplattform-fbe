"""Bundesland aus Ort/PLZ-Text ableiten.

Strategie:
  1. Wenn schon ein Bundesland gesetzt ist -> nehmen.
  2. PLZ aus dem Ort-String extrahieren (5-stellig) -> ueber 2-stelliges
     Praefix das Bundesland nachschlagen.
  3. Fallback: Ort-String nach Bundeslandnamen durchsuchen.

Mapping ist auf den ersten 2 Stellen der PLZ pragmatisch:
- Grenzfaelle werden dem dominanten Bundesland zugeordnet.
- Stand: aktuelle deutsche Postleitzahlen-Bereiche.
"""
from __future__ import annotations

import re


# Reihenfolge: laengere Namen zuerst (damit "Sachsen-Anhalt" vor "Sachsen" matched)
GERMAN_STATES = [
    "Baden-Württemberg",
    "Mecklenburg-Vorpommern",
    "Nordrhein-Westfalen",
    "Rheinland-Pfalz",
    "Sachsen-Anhalt",
    "Schleswig-Holstein",
    "Bayern",
    "Berlin",
    "Brandenburg",
    "Bremen",
    "Hamburg",
    "Hessen",
    "Niedersachsen",
    "Saarland",
    "Sachsen",
    "Thüringen",
    "NRW",            # Aliase
]
STATE_ALIASES = {"NRW": "Nordrhein-Westfalen"}

# 2-stelliges PLZ-Praefix -> Bundesland
PLZ_PREFIX_TO_STATE: dict[str, str] = {
    # 0X
    "01": "Sachsen", "02": "Sachsen", "03": "Brandenburg",
    "04": "Sachsen", "05": "Sachsen-Anhalt", "06": "Sachsen-Anhalt",
    "07": "Thüringen", "08": "Sachsen", "09": "Sachsen",
    # 1X
    "10": "Berlin", "11": "Berlin", "12": "Berlin", "13": "Berlin",
    "14": "Brandenburg", "15": "Brandenburg", "16": "Brandenburg",
    "17": "Mecklenburg-Vorpommern",
    "18": "Mecklenburg-Vorpommern",
    "19": "Mecklenburg-Vorpommern",
    # 2X
    "20": "Hamburg", "21": "Hamburg", "22": "Hamburg",
    "23": "Schleswig-Holstein", "24": "Schleswig-Holstein", "25": "Schleswig-Holstein",
    "26": "Niedersachsen", "27": "Niedersachsen",
    "28": "Bremen",
    "29": "Niedersachsen",
    # 3X
    "30": "Niedersachsen", "31": "Niedersachsen",
    "32": "Nordrhein-Westfalen", "33": "Nordrhein-Westfalen",
    "34": "Hessen", "35": "Hessen", "36": "Hessen",
    "37": "Niedersachsen",
    "38": "Niedersachsen",
    "39": "Sachsen-Anhalt",
    # 4X
    "40": "Nordrhein-Westfalen", "41": "Nordrhein-Westfalen",
    "42": "Nordrhein-Westfalen", "43": "Nordrhein-Westfalen",
    "44": "Nordrhein-Westfalen", "45": "Nordrhein-Westfalen",
    "46": "Nordrhein-Westfalen", "47": "Nordrhein-Westfalen",
    "48": "Nordrhein-Westfalen",
    "49": "Niedersachsen",
    # 5X
    "50": "Nordrhein-Westfalen", "51": "Nordrhein-Westfalen",
    "52": "Nordrhein-Westfalen", "53": "Nordrhein-Westfalen",
    "54": "Rheinland-Pfalz", "55": "Rheinland-Pfalz",
    "56": "Rheinland-Pfalz",
    "57": "Nordrhein-Westfalen", "58": "Nordrhein-Westfalen",
    "59": "Nordrhein-Westfalen",
    # 6X
    "60": "Hessen", "61": "Hessen", "62": "Hessen", "63": "Hessen",
    "64": "Hessen", "65": "Hessen",
    "66": "Saarland",
    "67": "Rheinland-Pfalz",
    "68": "Baden-Württemberg", "69": "Baden-Württemberg",
    # 7X
    "70": "Baden-Württemberg", "71": "Baden-Württemberg",
    "72": "Baden-Württemberg", "73": "Baden-Württemberg",
    "74": "Baden-Württemberg", "75": "Baden-Württemberg",
    "76": "Baden-Württemberg", "77": "Baden-Württemberg",
    "78": "Baden-Württemberg", "79": "Baden-Württemberg",
    # 8X
    "80": "Bayern", "81": "Bayern", "82": "Bayern", "83": "Bayern",
    "84": "Bayern", "85": "Bayern", "86": "Bayern", "87": "Bayern",
    "88": "Baden-Württemberg", "89": "Bayern",
    # 9X
    "90": "Bayern", "91": "Bayern", "92": "Bayern", "93": "Bayern",
    "94": "Bayern", "95": "Bayern", "96": "Bayern", "97": "Bayern",
    "98": "Thüringen", "99": "Thüringen",
}


_PLZ_RE = re.compile(r"\b(\d{5})\b")


def infer_region(location: str | None, current_region: str | None = None) -> str | None:
    """Liefert das Bundesland fuer einen Tender.

    Reihenfolge der Heuristiken:
      1. current_region (falls gesetzt) - normalisiert (NRW -> Nordrhein-Westfalen).
      2. PLZ im location-Feld -> 2-stelliges Praefix lookup.
      3. Bundesland-Name im location-Feld.
    """
    if current_region:
        normalized = STATE_ALIASES.get(current_region.strip(), current_region.strip())
        if normalized:
            return normalized

    if not location:
        return None

    text = str(location)

    # PLZ-Lookup
    m = _PLZ_RE.search(text)
    if m:
        plz = m.group(1)
        state = PLZ_PREFIX_TO_STATE.get(plz[:2])
        if state:
            return state

    # Bundesland-Name im Text
    text_low = text.lower()
    for state in GERMAN_STATES:
        if state.lower() in text_low:
            return STATE_ALIASES.get(state, state)

    return None
