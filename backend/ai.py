"""KI-Anbindung an OpenAI fuer Tender-Analyse und Chat.

Wissensbasis liegt als Markdown in config/ai_knowledge.md und wird bei jeder
Anfrage als System-Prompt mitgeschickt. Editierbar ueber /admin/ai-knowledge.

Modell + API-Key kommen aus settings (.env).
"""
from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Iterable

from .config import settings, PROJECT_ROOT


log = logging.getLogger(__name__)

KNOWLEDGE_PATH = PROJECT_ROOT / "config" / "ai_knowledge.md"


# ---------------------------------------------------------------------------
def is_configured() -> bool:
    return bool(settings.openai_api_key)


def read_knowledge() -> str:
    try:
        return KNOWLEDGE_PATH.read_text(encoding="utf-8")
    except FileNotFoundError:
        return ""


def write_knowledge(text: str) -> None:
    KNOWLEDGE_PATH.parent.mkdir(parents=True, exist_ok=True)
    KNOWLEDGE_PATH.write_text(text, encoding="utf-8")


def _client():
    from openai import OpenAI
    kwargs = {"api_key": settings.openai_api_key}
    if settings.openai_base_url:
        kwargs["base_url"] = settings.openai_base_url
    return OpenAI(**kwargs)


# ---------------------------------------------------------------------------
SYSTEM_BASE = (
    "Du bist der interne Assistent der Fluessigboden Engineering (FBE). "
    "Du hilfst Mitarbeitenden bei der Analyse oeffentlicher Ausschreibungen "
    "im Hinblick auf den Einsatz von ZFSV (Fluessigboden). "
    "Antworte sachlich, auf Deutsch, knapp und in Stichpunkten wo moeglich. "
    "Erfinde niemals Zahlen oder Fakten - lieber konservativ schaetzen oder "
    "explizit sagen, dass Angaben fehlen."
)


def _system_prompt() -> str:
    kb = read_knowledge().strip()
    if kb:
        return SYSTEM_BASE + "\n\n# Wissensbasis FBE\n" + kb
    return SYSTEM_BASE


# ---------------------------------------------------------------------------
def analyze_tender(tender) -> dict:
    """Analyse einer Ausschreibung. Liefert strukturiertes Ergebnis als dict:
        {
          "fit_level":   "high" | "medium" | "low",
          "fit_reason":  kurzer Satz,
          "savings_pct": Prozent oder None,
          "savings_eur": Bandbreite-String oder None,
          "summary":     2-4 Stichpunkte,
          "caveats":     Hinweise / Annahmen
        }
    Bei Fehler wird {"error": "..."} zurueckgegeben.
    """
    if not is_configured():
        return {"error": "OPENAI_API_KEY nicht gesetzt"}

    payload = {
        "titel": tender.title or "",
        "auftraggeber": tender.contracting_authority or "",
        "ort": tender.location or "",
        "bundesland": tender.region or "",
        "frist": tender.deadline.isoformat() if tender.deadline else None,
        "portal": tender.portal or "",
        "cpv": tender.cpv_codes or "",
        "beschreibung": (tender.description or "")[:6000],
        "passende_begriffe": tender.matched_terms or "",
    }

    user_msg = (
        "Analysiere die folgende Ausschreibung in Hinblick auf den moeglichen "
        "Einsatz von Fluessigboden (ZFSV) der FBE. Gib das Ergebnis als JSON "
        "mit genau diesen Feldern zurueck:\n"
        "- fit_level: \"high\" | \"medium\" | \"low\" "
        "(wie gut passt die Ausschreibung zu FBE-Leistungen)\n"
        "- fit_reason: 1 Satz Begruendung\n"
        "- savings_pct: geschaetzte Einsparung in Prozent (Zahl 0-80, oder null wenn "
        "nicht belastbar)\n"
        "- savings_eur: Bandbreite als String, z.B. \"5.000 - 12.000 EUR\" "
        "(oder null)\n"
        "- summary: 2-4 Stichpunkte als Liste (Strings) - was die Ausschreibung "
        "verlangt und wo FBE einen Hebel hat\n"
        "- caveats: 1-2 Hinweise auf fehlende Angaben oder Annahmen (Strings)\n\n"
        "Ausschreibungsdaten:\n" + json.dumps(payload, ensure_ascii=False, indent=2)
    )

    try:
        client = _client()
        resp = client.chat.completions.create(
            model=settings.openai_model,
            response_format={"type": "json_object"},
            messages=[
                {"role": "system", "content": _system_prompt()},
                {"role": "user", "content": user_msg},
            ],
            temperature=0.2,
        )
        content = resp.choices[0].message.content or "{}"
        data = json.loads(content)
    except Exception as exc:
        log.exception("KI-Analyse fehlgeschlagen: %s", exc)
        return {"error": "KI-Anfrage fehlgeschlagen: " + str(exc)[:200]}

    # Defensive Normalisierung
    fit = (data.get("fit_level") or "").lower()
    if fit not in ("high", "medium", "low"):
        fit = "low"
    return {
        "fit_level": fit,
        "fit_reason": (data.get("fit_reason") or "").strip(),
        "savings_pct": data.get("savings_pct") if isinstance(data.get("savings_pct"), (int, float)) else None,
        "savings_eur": (data.get("savings_eur") or None),
        "summary": data.get("summary") or [],
        "caveats": data.get("caveats") or [],
    }


# ---------------------------------------------------------------------------
def chat(messages: list[dict]) -> str:
    """Freier Chat. messages = [{role, content}, ...].

    Wirft RuntimeError bei Fehler.
    """
    if not is_configured():
        raise RuntimeError("OPENAI_API_KEY nicht gesetzt")
    payload = [{"role": "system", "content": _system_prompt()}]
    payload += [m for m in messages if m.get("role") in ("user", "assistant")][-20:]
    client = _client()
    resp = client.chat.completions.create(
        model=settings.openai_model,
        messages=payload,
        temperature=0.4,
    )
    return resp.choices[0].message.content or ""
