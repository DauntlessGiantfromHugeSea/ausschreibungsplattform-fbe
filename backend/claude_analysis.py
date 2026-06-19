"""Tiefe Tender-Analyse mit Anthropic Claude.

Liefert eine strukturierte Markdown-Auswertung pro Tender mit Fokus auf:
- Eckdaten (Auftraggeber, Frist, Ort, Wert)
- Anhaenge (PDFs, Vergabeunterlagen) - Namen + Hint auf Pruefung
- Einsparungspotenzial (Schaetzung bei FBE-Verfahren)
- Fluessigboden-Eignung (ja/nein/unklar + konkrete Begruendung)
- Gesamteinschaetzung fuer FBE

Nutzt zwei Datenquellen:
- tender.title/description/contracting_authority/location (DB)
- tender.ai_analysis (Enricher-Output mit Deep-Crawl-Daten)
- knowledge_dir (vom Admin gepflegte FBE-Wissensbasis)
"""
from __future__ import annotations

import logging
from datetime import datetime
from pathlib import Path
from typing import Optional

from .config import settings
from .models import Tender


log = logging.getLogger(__name__)


SYSTEM_PROMPT = """Du bist Spezialberater fuer Fluessigboden Engineering GmbH (FBE), spezialisiert auf:
- ZFSV (Zeitweise fliessfaehige selbstverdichtende Verfuellbaustoffe nach RAL GZ 507)
- Verfuellung von Leitungsgraeben, Kabelgraeben, Schaechten, Baugruben, Rohrleitungs-Bettungen
- Tiefbau-Massnahmen wo klassische Sand-/Bodenverfuellung durch Fluessigboden ersetzt werden kann

Deine Aufgabe: aus dem mitgelieferten Ausschreibungs-Material eine strukturierte Bewertung erzeugen.
Ausgabe-Format: reines Markdown, exakt diese Abschnitte (auch wenn ein Feld leer bleibt - schreib dann '—'):

## Eckdaten
- **Auftraggeber:**
- **Ausfuehrungsort:**
- **Submission-Frist:**
- **Geschaetzter Wert:**
- **Verfahrensart:**
- **Vergabestelle / Ansprechpartner:**

## Anhaenge / Vergabeunterlagen
Liste der im Material gefundenen Anhaenge/PDFs mit Name + ggf. URL. Wenn nichts erkennbar: '— keine Anhaenge im verfuegbaren Material gefunden, Originalausschreibung pruefen.'

## Einsparungspotenzial bei Einsatz von Fluessigboden
Konkrete Schaetzung in EUR bzw. % gegenueber klassischer Verfuellung. Bei unklarer Mengenangabe explizit nennen, welche Annahmen du triffst. Wenn keine relevanten Mengenangaben: '— keine belastbare Schaetzung mit den vorhandenen Daten moeglich.'
Halte dich an konservative Annahmen (typisch: Fluessigboden spart 30-50% gegenueber Lagern-Wiedereinbau bei Leitungsgraeben durch Wegfall von Verdichtungs-/Lagerflaechen).

## Fluessigboden-Eignung
**Bewertung:** {hoch | mittel | gering | nicht anwendbar}
**Begruendung:** 2-4 Saetze - was im Leistungsverzeichnis spricht dafuer/dagegen. Konkrete Hinweise auf Gewerke: Leitungsbau, Kabelbau, Schachtbau, Strassenbau-Querungen sind typisch hoch. Reine Hochbau-/Trockenbau-/Planungs-Ausschreibungen sind nicht anwendbar.

## Empfehlung
Eine konkrete Handlungsempfehlung in maximal 3 Saetzen:
- Soll FBE bieten? (ja / pruefen / nein)
- Falls ja: welcher Hebel sollte im Angebot betont werden?
- Wenn nein: warum nicht in 1 Satz.

Wenn das Material offensichtlich unvollstaendig ist (z.B. nur Login-Wall), schreib stattdessen:
## Hinweis
Material unvollstaendig - bitte erst Enricher-Anreicherung abwarten oder Original aufrufen.
"""


def is_configured() -> bool:
    return bool(settings.anthropic_api_key)


def _load_knowledge() -> str:
    """Liest die Notes aus dem knowledge_dir und haengt sie als Kontext an."""
    try:
        d = Path(settings.knowledge_dir)
        if not d.is_dir():
            return ""
        parts = []
        for f in sorted(d.glob("*.md")):
            try:
                content = f.read_text(encoding="utf-8", errors="replace").strip()
                if content:
                    parts.append(f"### {f.name}\n{content}")
            except OSError:
                continue
        if not parts:
            return ""
        return "\n\n--- ADMIN-WISSENSBASIS ---\n" + "\n\n".join(parts)
    except Exception:  # pragma: no cover
        return ""


def analyze_tender(tender: Tender) -> tuple[bool, str]:
    """Fuehrt die Claude-Analyse aus. Liefert (ok, markdown_oder_fehler)."""
    if not is_configured():
        return False, "Anthropic-API-Key nicht konfiguriert (ANTHROPIC_API_KEY in .env setzen)."

    try:
        from anthropic import Anthropic
    except ImportError:
        return False, "anthropic-Paket nicht installiert. pip install anthropic"

    # Material zusammenstellen
    enrich = (tender.ai_analysis or "").strip()
    knowledge = _load_knowledge()

    parts = []
    parts.append(f"# Tender #{tender.id}: {tender.title or '(ohne Titel)'}")
    parts.append("")
    parts.append(f"**Portal:** {tender.portal or '-'}")
    parts.append(f"**URL:** {tender.url or '-'}")
    if tender.contracting_authority:
        parts.append(f"**Auftraggeber (laut DB):** {tender.contracting_authority}")
    if tender.location:
        parts.append(f"**Ort (laut DB):** {tender.location}")
    if tender.deadline:
        parts.append(f"**Frist (laut DB):** {tender.deadline.strftime('%d.%m.%Y')}")
    if tender.relevance_score is not None:
        parts.append(f"**Plattform-Score:** {tender.relevance_score}")
    if tender.description:
        parts.append("")
        parts.append("**Beschreibung:**")
        parts.append(tender.description)
    if enrich:
        parts.append("")
        parts.append("--- Enricher-Anreicherung (Chromium-Render + PDFs) ---")
        parts.append(enrich)
    if knowledge:
        parts.append(knowledge)
    user_msg = "\n".join(parts)

    client = Anthropic(api_key=settings.anthropic_api_key)
    try:
        resp = client.messages.create(
            model=settings.anthropic_model,
            max_tokens=4096,
            system=SYSTEM_PROMPT,
            messages=[{"role": "user", "content": user_msg}],
        )
    except Exception as exc:
        log.exception("Claude-Analyse fehlgeschlagen")
        return False, f"Claude-API-Fehler: {type(exc).__name__}: {str(exc)[:200]}"

    # Antwort zusammenfuegen
    text_parts = []
    for block in resp.content:
        if getattr(block, "type", None) == "text":
            text_parts.append(block.text)
        elif hasattr(block, "text"):
            text_parts.append(block.text)
    markdown = "\n".join(text_parts).strip()
    if not markdown:
        return False, "Claude lieferte eine leere Antwort."
    return True, markdown


def run_and_store(db, tender: Tender) -> tuple[bool, str]:
    """Wrapper: fuehrt Analyse aus und speichert das Ergebnis am Tender."""
    ok, content = analyze_tender(tender)
    if not ok:
        return False, content
    tender.claude_analysis = content[:64000]
    tender.claude_analyzed_at = datetime.utcnow()
    db.commit()
    return True, content
