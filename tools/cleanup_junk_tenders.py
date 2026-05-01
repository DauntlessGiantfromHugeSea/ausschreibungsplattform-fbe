"""Bereinigt offensichtliche Junk-Tender (Footer-/Menue-Eintraege),
die durch zu gierigen Parser-Fallback in die DB gerutscht sind.

Aufruf:

    # 1. Vorschau (zeigt nur, loescht nichts)
    python tools/cleanup_junk_tenders.py

    # 2. Wirklich loeschen
    python tools/cleanup_junk_tenders.py --delete

    # 3. Statt loeschen: archivieren (Status='archiviert')
    python tools/cleanup_junk_tenders.py --archive

Was ist Junk?
- Titel matcht eines der bekannten Footer-/Menue-Patterns:
  Impressum, Datenschutz, Barrierefreiheit, AGB, Newsletter, Kontakt,
  Ministerium, Staatskanzlei, Home, Startseite, Login, Anmelden,
  Hilfe, FAQ, Sitemap.
- Oder URL endet auf typische CMS-Pfade.
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backend.database import SessionLocal  # noqa: E402
from backend.models import Tender, TenderStatus  # noqa: E402


JUNK_TITLE_RE = re.compile(
    r"(?xi)
    ^\s*
    (?:
      impressum
      | datenschutz(erkl[aä]rung)?
      | barrierefreiheit(serkl[aä]rung)?
      | erkl[aä]rung\s+zur\s+barrierefreiheit
      | a\s*g\s*b
      | newsletter
      | kontakt(formular)?
      | sitemap
      | hilfe
      | faq
      | suche
      | leichte\s+sprache
      | geb[aä]rdensprache
      | home(page)?
      | startseite
      | anmelden|login|anmeldung
      | registrieren|registrierung
      | abmelden|logout
      | passwort\s+vergessen
      | benutzername\s+vergessen
      | druckansicht
      | seite\s+drucken
      | weiterlesen
      | mehr(\s+erfahren|\s+anzeigen)?
      | english(\s+instructions)?
      | deutsch
      | sprache(n)?
      | suchen|search
      | (staatskanzlei.*ministerium|ministerium\s+(f[uü]r|der)\s+\w+)
    )
    [\s.…:]*$
    """
)

# Auch ganze URLs auf typische CMS-Pfade
JUNK_URL_RE = re.compile(
    r"(?xi)
    /(?:
      impressum
      | datenschutz
      | barrierefreiheit
      | agb
      | newsletter
      | kontakt
      | sitemap
      | hilfe
      | faq
      | login|logout|anmelden|registrieren
      | drucken|print
      | leichte-sprache|gebaerdensprache
      | suche
      | meta/(?:hinweise|barrierefreiheitserkl|kontaktformular|impressum)
      | ueber-uns|wir-ueber-uns
      | staatskanzlei
      | ministerium
    )(?:[/?]|$)
    """
)


def find_junk():
    db = SessionLocal()
    try:
        all_tenders = db.query(Tender).all()
        junk = []
        for t in all_tenders:
            title = (t.title or "").strip()
            url = (t.url or "").strip()
            if JUNK_TITLE_RE.match(title):
                junk.append((t, "title"))
            elif JUNK_URL_RE.search(url):
                junk.append((t, "url"))
        return junk
    finally:
        db.close()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    g = parser.add_mutually_exclusive_group()
    g.add_argument("--delete", action="store_true",
                   help="Wirklich aus DB loeschen.")
    g.add_argument("--archive", action="store_true",
                   help="Status auf 'archiviert' setzen statt loeschen.")
    args = parser.parse_args()

    junk = find_junk()
    if not junk:
        print("Keine Junk-Tender gefunden. Nichts zu tun.")
        return 0

    print("=" * 70)
    print("Gefundene Junk-Tender (Vorschau):")
    print("=" * 70)
    by_portal: dict[str, int] = {}
    for t, reason in junk:
        by_portal[t.portal] = by_portal.get(t.portal, 0) + 1
        print("  [{}]  {:8s}  '{}'".format(
            reason, (t.portal or "")[:25], (t.title or "")[:80]))
    print()
    print("Zusammenfassung:")
    for portal, n in sorted(by_portal.items(), key=lambda x: -x[1]):
        print("  {:3d}  {}".format(n, portal))
    print("  ---")
    print("  {:3d}  GESAMT".format(len(junk)))
    print()

    if not (args.delete or args.archive):
        print("Vorschau-Modus. Nichts geaendert.")
        print("  Zum Archivieren:  python tools/cleanup_junk_tenders.py --archive")
        print("  Zum Loeschen:     python tools/cleanup_junk_tenders.py --delete")
        return 0

    db = SessionLocal()
    try:
        ids = [t.id for t, _ in junk]
        if args.delete:
            from backend.models import Comment
            # Erst Kommentare zu den Tendern weg (FK-Schutz).
            db.query(Comment).filter(Comment.tender_id.in_(ids)).delete(
                synchronize_session=False)
            n = db.query(Tender).filter(Tender.id.in_(ids)).delete(
                synchronize_session=False)
            db.commit()
            print("==> {} Junk-Tender geloescht.".format(n))
        elif args.archive:
            n = db.query(Tender).filter(Tender.id.in_(ids)).update(
                {Tender.status: TenderStatus.ARCHIVIERT.value},
                synchronize_session=False)
            db.commit()
            print("==> {} Junk-Tender archiviert.".format(n))
    finally:
        db.close()

    return 0


if __name__ == "__main__":
    sys.exit(main())
