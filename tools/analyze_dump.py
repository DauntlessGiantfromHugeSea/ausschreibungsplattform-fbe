"""HTML-Struktur-Analyse fuer Debug-Dumps.

Erzeugt einen kompakten Bericht pro HTML-Datei mit:
- Top-CSS-Klassen, IDs, Tags
- Anchor-Pattern (URL-Praefixe + Beispiel-Texte)
- PublicationID-/CXP-IDs/onclick-Handler
- Form-Aktionen + Felder
- Visible-Text-Schnipsel (zur Identifikation des Inhalts)

Aufruf:
    python tools/analyze_dump.py /tmp/portal-debug-extracted/

Output:
    /tmp/portal-analysis.txt   - Bericht zum Pasten in den Chat
"""
from __future__ import annotations

import re
import sys
from collections import Counter
from pathlib import Path

from bs4 import BeautifulSoup


def analyze(path: Path) -> str:
    raw = path.read_bytes()
    if raw.startswith(b"FEHLER:"):
        return "(FEHLER-Eintrag, kein HTML)\n"

    try:
        text = raw.decode("utf-8", errors="replace")
    except Exception:
        text = raw.decode("latin-1", errors="replace")

    out = []
    out.append("Bytes: {}".format(len(raw)))

    soup = BeautifulSoup(text, "lxml")
    body_text = soup.get_text(" ", strip=True)
    out.append("Visible-Text: {} chars".format(len(body_text)))

    title = soup.find("title")
    if title:
        out.append("Title: {}".format((title.get_text(strip=True) or "")[:120]))

    # Top-Tags
    tags = Counter(t.name for t in soup.find_all(True))
    top_tags = ", ".join("{}:{}".format(n, c) for n, c in tags.most_common(8))
    out.append("Top-Tags: {}".format(top_tags))

    # Top-CSS-Klassen (gruppiert)
    classes = Counter()
    for el in soup.find_all(class_=True):
        for c in el.get("class", []):
            classes[c] += 1
    out.append("Top-Klassen:")
    for cls, n in classes.most_common(15):
        out.append("  {:3d}x .{}".format(n, cls))

    # Top-IDs (nicht-numerisch)
    ids = [el.get("id") for el in soup.find_all(id=True) if el.get("id")]
    ids = [i for i in ids if not i.isdigit()]
    out.append("IDs ({}): {}".format(
        len(ids), ", ".join("#" + i for i in ids[:15])))

    # Anchors gruppieren nach Pfad-Praefix
    anchors = []
    for a in soup.find_all("a", href=True):
        href = a["href"].strip()
        if not href or href.startswith(("#", "javascript:", "mailto:", "tel:")):
            continue
        txt = a.get_text(" ", strip=True)
        anchors.append((href, txt))
    out.append("Anchors: {}".format(len(anchors)))

    # Pfad-Praefixe (bis zum 2. Slash)
    prefix_counter = Counter()
    for href, _ in anchors:
        # Praefix: erste 2 Pfad-Komponenten
        m = re.match(r"^(?:https?://[^/]+)?(/[^?#]+)", href)
        path = m.group(1) if m else href
        parts = path.split("/")
        prefix = "/".join(parts[:3]) if len(parts) > 2 else path
        prefix_counter[prefix[:60]] += 1
    out.append("Top-URL-Praefixe (Anchors):")
    for pfx, n in prefix_counter.most_common(10):
        out.append("  {:3d}x {}".format(n, pfx))

    # Beispiel-Anchors mit langem Text
    out.append("Anchor-Beispiele (Text >= 20 Zeichen):")
    seen_texts = set()
    cnt = 0
    for href, txt in anchors:
        if len(txt) < 20 or txt in seen_texts:
            continue
        seen_texts.add(txt)
        out.append("  href={}".format(href[:90]))
        out.append("       text={}".format(txt[:120]))
        cnt += 1
        if cnt >= 8:
            break

    # Tabellen
    tables = soup.find_all("table")
    out.append("Tabellen: {}".format(len(tables)))
    for i, tbl in enumerate(tables[:3]):
        cls = " ".join(tbl.get("class") or [])
        rows = tbl.find_all("tr")
        out.append("  Tabelle {} class='{}' rows={}".format(i, cls, len(rows)))
        # Erste Zeile
        if rows:
            first = rows[0]
            tds = first.find_all(["td", "th"])
            if tds:
                out.append("    Erste-Row-Felder: " + " | ".join(
                    (td.get_text(" ", strip=True) or "")[:30] for td in tds[:5]))

    # PublicationID + onclick + CXP-IDs im Roh-HTML
    pub_ids = re.findall(r"PublicationID[\"'\s]*[=:,]\s*[\"']?([A-Za-z0-9_\-]{4,})", text)
    onclick = re.findall(r"onclick\s*=\s*['\"]([^'\"]{1,80})", text)
    cxp_ids = re.findall(r"\b(CXP[A-Z0-9]{6,20})\b", text)
    notice_ids = re.findall(r"\b([A-Z]{4,8}-[A-Z0-9]{4,12})\b", text)

    out.append("PublicationID-Treffer: {}".format(len(pub_ids)))
    if pub_ids:
        out.append("  Beispiele: {}".format(", ".join(set(pub_ids[:8]))))

    out.append("onclick-Handler: {}".format(len(onclick)))
    if onclick:
        unique_onclicks = list(set(onclick))[:5]
        for oc in unique_onclicks:
            out.append("  {}".format(oc[:120]))

    out.append("CXP-IDs (Cosinex): {}".format(len(set(cxp_ids))))
    if cxp_ids:
        out.append("  Beispiele: {}".format(", ".join(list(set(cxp_ids))[:5])))

    out.append("Notice-IDs (Pattern X-XXXX): {}".format(len(set(notice_ids))))
    if notice_ids:
        out.append("  Beispiele: {}".format(", ".join(list(set(notice_ids))[:5])))

    # Forms
    forms = soup.find_all("form")
    out.append("Forms: {}".format(len(forms)))
    for i, f in enumerate(forms[:3]):
        action = f.get("action", "")
        method = f.get("method", "GET")
        inputs = [(inp.get("name"), inp.get("value", "")[:40])
                  for inp in f.find_all("input") if inp.get("name")]
        out.append("  Form {} {} action={}".format(i, method.upper(), action[:60]))
        for name, val in inputs[:5]:
            out.append("    input name={} value={}".format(name, val[:50]))

    # Erste 500 Zeichen des sichtbaren Texts (zur Inhaltsidentifikation)
    out.append("Visible-Text-Anfang:")
    out.append("  " + body_text[:300].replace("\n", " "))

    return "\n".join(out) + "\n"


def main(target_dir: str) -> int:
    target = Path(target_dir)
    if not target.is_dir():
        print("Verzeichnis nicht gefunden: {}".format(target), file=sys.stderr)
        return 1

    out_lines = ["Portal-HTML-Analyse · {}".format(target), ""]
    files = sorted(target.glob("*.html"))
    print("Analysiere {} Dateien aus {}".format(len(files), target))

    for f in files:
        out_lines.append("=" * 70)
        out_lines.append("FILE: {}".format(f.name))
        out_lines.append("=" * 70)
        try:
            out_lines.append(analyze(f))
        except Exception as exc:
            out_lines.append("ANALYSE-FEHLER: {}: {}".format(
                type(exc).__name__, str(exc)[:200]))
            out_lines.append("")

    report = "\n".join(out_lines)
    out_path = Path("/tmp/portal-analysis.txt")
    out_path.write_text(report, encoding="utf-8")
    print("Bericht: {} ({} KB)".format(out_path, len(report) // 1024))
    print()
    print("Zum Pasten in den Chat:")
    print("  cat {}".format(out_path))
    return 0


if __name__ == "__main__":
    target = sys.argv[1] if len(sys.argv) > 1 else "/tmp/portal-debug-extracted"
    sys.exit(main(target))
