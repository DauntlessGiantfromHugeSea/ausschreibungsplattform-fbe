# Projekt-Kontext für Claude

## Obsidian-Vault des Users

Der User (`d.model@fb-eng.de`) pflegt sein Obsidian-Vault unter:

```
C:\Users\dm\OneDrive - Flüssigboden Engineering GmbH\Desktop\FBE
```

Auf einem Windows-Rechner, synchronisiert über OneDrive.

**Wenn er sagt „schreib das in mein Vault" o.ä.:**
- Markdown-Datei in `docs/` im Repo anlegen (Obsidian-taugliche Formatierung: keine Frontmatter zwingend, `##`-Überschriften, Tabellen erlaubt, Code-Blöcke mit Sprache getaggt)
- Sinnvollen Dateinamen mit Datum-Prefix wählen: `YYYY-MM-DD - Titel.md`
- Commit + Push
- Ihm den Windows-Pfad und einen Copy-Snippet nennen, wie er die Datei aus dem Repo in sein Vault holt

## Server-Setup

- Plattform läuft auf VPS als `root`, systemd-Service `fbe-tender`
- Projekt-Pfad auf Server: `/root/ausschreibungsplattform-fbe`
- Enricher-Container: `fbe-enricher` (Docker, `--network host`, `--env-file`)
- Attachment-Volume: `/srv/fbe-attachments`
- Enrich-Volume: `/srv/fbe-enrich`
- Wissensbasis-Volume: `/srv/fbe-knowledge`
- Domain: `https://ausschreibung.rss-fb.com` (und `https://app.fb-akademie.de`)

## Aktueller Branch

`claude/multi-platform-integration-9wKvE` — Feature-Branch für alle Änderungen.
Push-Regel: nie auf `main` direkt.

## Wichtige Commit-Regeln

- Kein "Claude"-Modell-Identifier in Commit-Messages
- Deutsch für User-facing Text, Englisch für Code-Kommentare wo sinnvoll
- Umlaute in Commit-Messages vermeiden (ae/oe/ue), im Code sind sie ok
