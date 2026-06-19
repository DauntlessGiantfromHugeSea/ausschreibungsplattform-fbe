"""Claude-Agent mit Tool-Use fuer tiefe Tender-Analyse.

Statt One-Shot-Anfrage laeuft hier ein echter Agent-Loop:
Claude bekommt ein Set Tools und entscheidet selbstaendig, welche
Informationen es braucht (Wissensbasis durchsuchen, Anhaenge lesen,
aehnliche Tender vergleichen). Erst wenn es genug Material hat,
schreibt es das finale Markdown.

Limits:
- Max 8 Tool-Use-Iterationen pro Analyse (Schutz gegen Endlos-Loop)
- Max 4096 Output-Tokens
- Modell aus settings.anthropic_model
"""
from __future__ import annotations

import json
import logging
from datetime import datetime
from pathlib import Path
from typing import Any

from .config import settings
from .database import SessionLocal
from .models import Tender, TenderAttachment


log = logging.getLogger(__name__)


SYSTEM_PROMPT = """Du bist Spezialberater fuer Flüssigboden Akademie (FBA),
spezialisiert auf ZFSV (Zeitweise fliessfaehige selbstverdichtende
Verfuellbaustoffe nach RAL GZ 507), Verfuellung von Leitungsgraeben,
Kabelgraeben, Schaechten, Baugruben, Rohrleitungs-Bettungen.

Vor der Antwort: nutze die Tools, um genug Kontext zu sammeln. Geh
nicht einfach raten - frag Tools nach Wissensbasis-Eintraegen,
Anhaengen, aehnlichen vergangenen Tendern. Erst wenn du genug
Substanz hast, schreibst du das finale Markdown.

Finale Antwort = reines Markdown, exakt diese 5 Abschnitte:

## Eckdaten
- **Auftraggeber:**
- **Ausfuehrungsort:**
- **Submission-Frist:**
- **Geschaetzter Wert:**
- **Verfahrensart:**
- **Vergabestelle / Ansprechpartner:**

## Anhaenge / Vergabeunterlagen
Liste der gefundenen Anhaenge mit Name + Hinweis, was drin steht.

## Einsparungspotenzial bei Einsatz von Flüssigboden
Konkrete Schaetzung in EUR bzw. % gegenueber klassischer Verfuellung.
Konservative Annahmen (typisch 30-50% bei Leitungsgraeben).

## Flüssigboden-Eignung
**Bewertung:** {hoch | mittel | gering | nicht anwendbar}
**Begruendung:** 2-4 Saetze, was im Material dafuer/dagegen spricht.

## Empfehlung
Maximal 3 Saetze: bieten ja/pruefen/nein + Hebel.

Wenn das Material zu duenn ist (z.B. nur Login-Wall, keine PDFs),
sag das ehrlich in einem 'Hinweis'-Abschnitt statt zu phantasieren.
"""


# Tools die Claude aufrufen kann.
TOOLS_SPEC = [
    {
        "name": "search_knowledge",
        "description": (
            "Durchsucht die Admin-Wissensbasis (vom Admin gepflegte Markdown-"
            "Notizen in /srv/fbe-knowledge) nach einem Begriff. Liefert die "
            "ersten N Treffer mit Dateinamen + Snippet."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Suchbegriff (Substring-Match, case-insensitive)"}
            },
            "required": ["query"],
        },
    },
    {
        "name": "list_attachments",
        "description": (
            "Listet die Vergabeunterlagen/Anhaenge dieser Ausschreibung "
            "(PDFs, DOCX, etc.), die der Enricher-Bot heruntergeladen hat. "
            "Liefert pro Anhang: filename, source_url, size_bytes, "
            "extracted_text (erste ~2000 Zeichen)."
        ),
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "find_similar_tenders",
        "description": (
            "Findet aehnliche Tender in der Datenbank per Volltextsuche "
            "(Titel + Beschreibung + matched_terms). Liefert max. 5 Treffer "
            "mit Titel + Portal + Score + ggf. claude_analysis-Zusammenfassung."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "keyword": {"type": "string", "description": "Suchbegriff"}
            },
            "required": ["keyword"],
        },
    },
    {
        "name": "read_enrichment",
        "description": (
            "Liefert die vom OpenAI-Enricher-Bot erzeugte Vor-Analyse fuer "
            "DIESEN Tender (Chromium-gerendert + PDF-extracted). Klassisch "
            "der erste Schritt zum Material-Check."
        ),
        "input_schema": {"type": "object", "properties": {}},
    },
]


def is_configured() -> bool:
    return bool(settings.anthropic_api_key)


# --- Tool-Implementierung -----------------------------------------------

def _tool_search_knowledge(query: str) -> str:
    q = (query or "").strip().lower()
    if not q:
        return "Leere Anfrage."
    d = Path(settings.knowledge_dir)
    if not d.is_dir():
        return "Wissensbasis-Verzeichnis existiert nicht."
    hits = []
    for f in sorted(d.glob("*.md")):
        try:
            text = f.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        idx = text.lower().find(q)
        if idx < 0 and q not in f.name.lower():
            continue
        if idx >= 0:
            start = max(0, idx - 80)
            end = min(len(text), idx + len(q) + 240)
            snippet = text[start:end].strip()
        else:
            snippet = text[:240].strip()
        hits.append(f"### {f.name}\n{snippet}")
        if len(hits) >= 5:
            break
    if not hits:
        return f"Keine Treffer fuer '{query}' in der Wissensbasis."
    return "\n\n".join(hits)


def _tool_list_attachments(tender_id: int) -> str:
    db = SessionLocal()
    try:
        items = (
            db.query(TenderAttachment)
            .filter(TenderAttachment.tender_id == tender_id)
            .order_by(TenderAttachment.created_at.desc())
            .all()
        )
        if not items:
            return "Keine Anhaenge gefunden. Enricher hat fuer diesen Tender noch keine PDFs/Vergabeunterlagen heruntergeladen."
        out = []
        for a in items:
            block = [
                f"### {a.filename}",
                f"- Quelle: {a.source_url}",
                f"- Groesse: {a.size_bytes or '?'} bytes",
                f"- Content-Type: {a.content_type or '?'}",
            ]
            if a.extracted_text:
                block.append("- Inhaltsauszug (max 2000 Zeichen):")
                block.append(a.extracted_text[:2000])
            out.append("\n".join(block))
        return "\n\n".join(out)
    finally:
        db.close()


def _tool_find_similar_tenders(keyword: str, exclude_id: int) -> str:
    k = (keyword or "").strip()
    if not k:
        return "Leere Anfrage."
    like = f"%{k}%"
    db = SessionLocal()
    try:
        from sqlalchemy import or_ as _or
        rows = (
            db.query(Tender)
            .filter(Tender.id != exclude_id)
            .filter(_or(
                Tender.title.ilike(like),
                Tender.description.ilike(like),
                Tender.matched_terms.ilike(like),
            ))
            .order_by(Tender.relevance_score.desc())
            .limit(5)
            .all()
        )
        if not rows:
            return f"Keine aehnlichen Tender zu '{keyword}' in der DB."
        out = []
        for t in rows:
            summary = (t.claude_analysis or t.ai_analysis or t.description or "")[:300]
            out.append(
                f"### #{t.id} {t.title}\n"
                f"- Portal: {t.portal}, Score: {t.relevance_score}\n"
                f"- {summary}"
            )
        return "\n\n".join(out)
    finally:
        db.close()


def _tool_read_enrichment(tender_id: int) -> str:
    db = SessionLocal()
    try:
        t = db.get(Tender, tender_id)
        if not t:
            return "Tender nicht gefunden."
        if not t.ai_analysis:
            return "Noch keine Enricher-Anreicherung vorhanden. Der Enricher-Bot hat diesen Tender noch nicht bearbeitet."
        if t.ai_analysis.startswith("enrich-error:"):
            return f"Enricher hat fuer diesen Tender einen Fehler hinterlegt: {t.ai_analysis}"
        return t.ai_analysis
    finally:
        db.close()


def _dispatch_tool(name: str, args: dict, tender_id: int) -> str:
    """Fuehrt ein Tool aus, gibt String als Ergebnis."""
    try:
        if name == "search_knowledge":
            return _tool_search_knowledge(args.get("query", ""))
        if name == "list_attachments":
            return _tool_list_attachments(tender_id)
        if name == "find_similar_tenders":
            return _tool_find_similar_tenders(args.get("keyword", ""), exclude_id=tender_id)
        if name == "read_enrichment":
            return _tool_read_enrichment(tender_id)
    except Exception as exc:
        log.exception("Tool '%s' fehlgeschlagen", name)
        return f"Tool-Fehler: {type(exc).__name__}: {exc}"
    return f"Unbekanntes Tool: {name}"


# --- Agent-Loop --------------------------------------------------------

MAX_ITERATIONS = 8


def analyze_tender(tender: Tender) -> tuple[bool, str, list[dict]]:
    """Fuehrt die Agent-Analyse aus.

    Liefert (ok, markdown_oder_fehler, trace).
    trace ist eine Liste von dict mit {tool, args, result_excerpt}.
    """
    if not is_configured():
        return False, "Anthropic-API-Key nicht konfiguriert (ANTHROPIC_API_KEY in .env).", []

    try:
        from anthropic import Anthropic
    except ImportError:
        return False, "anthropic-Paket nicht installiert. pip install anthropic", []

    now = datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC")
    initial_user = (
        f"# Tender #{tender.id}: {tender.title or '(ohne Titel)'}\n\n"
        f"**Portal:** {tender.portal or '-'}\n"
        f"**URL:** {tender.url or '-'}\n"
        f"**Stand:** {now}\n"
        f"**Score:** {tender.relevance_score}\n"
    )
    if tender.contracting_authority:
        initial_user += f"**Auftraggeber (DB):** {tender.contracting_authority}\n"
    if tender.location:
        initial_user += f"**Ort (DB):** {tender.location}\n"
    if tender.deadline:
        initial_user += f"**Frist (DB):** {tender.deadline.strftime('%d.%m.%Y')}\n"
    if tender.description:
        initial_user += f"\n**Beschreibung:**\n{tender.description}\n"

    initial_user += (
        "\nDeine Aufgabe: nutze deine Tools, sammle relevante Infos "
        "(Enricher-Ergebnis, Anhaenge, Wissensbasis, aehnliche Tender), "
        "und liefere dann die strukturierte FBA-Bewertung als Markdown."
    )

    client = Anthropic(api_key=settings.anthropic_api_key)
    messages: list[dict[str, Any]] = [{"role": "user", "content": initial_user}]
    trace: list[dict] = []

    for iteration in range(MAX_ITERATIONS):
        try:
            resp = client.messages.create(
                model=settings.anthropic_model,
                max_tokens=4096,
                system=SYSTEM_PROMPT,
                tools=TOOLS_SPEC,
                messages=messages,
            )
        except Exception as exc:
            log.exception("Claude-Agent-Aufruf fehlgeschlagen")
            return False, f"Claude-API-Fehler: {type(exc).__name__}: {str(exc)[:200]}", trace

        stop_reason = getattr(resp, "stop_reason", None)
        content_blocks = resp.content or []

        # Wenn Claude tool_use-Blocks zurueckgibt -> Tools ausfuehren
        if stop_reason == "tool_use":
            # Assistant-Antwort als Block in den Dialog aufnehmen
            messages.append({"role": "assistant", "content": [
                _block_to_dict(b) for b in content_blocks
            ]})
            tool_results = []
            for b in content_blocks:
                if getattr(b, "type", None) == "tool_use":
                    tool_name = b.name
                    tool_args = b.input or {}
                    log.info("[claude-agent #%d] tool=%s args=%s", tender.id, tool_name, str(tool_args)[:120])
                    result = _dispatch_tool(tool_name, tool_args, tender.id)
                    trace.append({
                        "iteration": iteration + 1,
                        "tool": tool_name,
                        "args": tool_args,
                        "result_excerpt": (result or "")[:600],
                    })
                    tool_results.append({
                        "type": "tool_result",
                        "tool_use_id": b.id,
                        "content": (result or "")[:8000],
                    })
            messages.append({"role": "user", "content": tool_results})
            continue

        # Endgueltige Antwort
        text_out = []
        for b in content_blocks:
            if getattr(b, "type", None) == "text":
                text_out.append(b.text)
        markdown = "\n".join(text_out).strip()
        if not markdown:
            return False, "Claude lieferte keine Text-Antwort.", trace
        return True, markdown, trace

    return False, f"Agent-Loop-Limit erreicht ({MAX_ITERATIONS} Iterationen). Tender ist zu komplex oder die Tools liefern unzureichende Daten.", trace


def _block_to_dict(b) -> dict:
    """Wandelt ein Anthropic-Content-Block in ein dict um, das beim
    nachsten messages.create wieder akzeptiert wird."""
    t = getattr(b, "type", None)
    if t == "text":
        return {"type": "text", "text": b.text}
    if t == "tool_use":
        return {"type": "tool_use", "id": b.id, "name": b.name, "input": b.input}
    return {"type": t}


def run_and_store(db, tender: Tender) -> tuple[bool, str, list[dict]]:
    """Wrapper: fuehrt Analyse aus + speichert Ergebnis + Trace."""
    ok, content, trace = analyze_tender(tender)
    if not ok:
        return False, content, trace
    tender.claude_analysis = content[:64000]
    tender.claude_analyzed_at = datetime.utcnow()
    db.commit()
    return True, content, trace
