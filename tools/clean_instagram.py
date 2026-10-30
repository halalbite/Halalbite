#!/usr/bin/env python3
"""
HalalBite – Ungültige Instagram-Links aufräumen

1. Falsch geschriebene Einträge werden korrigiert, z. B. „/name“ oder ein Link auf einen
   Beitrag/Reel („instagram.com/name/reel/…“) → „name“.
2. Mit Meta-Token (wie bei tools/instagram_logos.py) wird jedes Konto über die offizielle
   Graph API nachgeschlagen. Findet Meta das Konto nicht, wird der Link entfernt und der
   Name in tools/.instagram_invalid.json gemerkt – tools/find_instagram.py trägt ihn dann
   nicht wieder ein.

Achtung: Die API kennt nur Business- und Creator-Konten. Ein PRIVATES/persönliches Konto
meldet sie genauso als „nicht gefunden“ wie ein gelöschtes. Darum erst --dry-run ansehen
und Konten, die es doch gibt, mit --keep behalten.

Benutzung (Terminal):
    cd ~/Downloads/HalalBite2
    export META_ACCESS_TOKEN="EAA…"
    python3 tools/clean_instagram.py --dry-run               # nur anzeigen
    python3 tools/clean_instagram.py --keep name1,name2      # aufräumen (fragt nach Admin-Login)
"""

import argparse
import json
import os
import re
import sys
import time
import urllib.parse

from find_instagram import NOT_PROFILES
from import_osm import ROOT, fetch_all_rows, http, login, supabase_config
from instagram_logos import GLOBAL_ERRORS, SETUP_HINT, handle_of, own_instagram_id, profile_of

INVALID_FILE = ROOT / "tools" / ".instagram_invalid.json"


def clean_handle(value):
    """Instagram-Name aus allen üblichen Schreibweisen – None, wenn keiner erkennbar ist"""
    text = (value or "").strip()
    if not text:
        return None
    if "instagram.com" in text.lower():
        path = urllib.parse.urlparse(text if "://" in text else "https://" + text).path
    else:
        path = text
    for part in path.replace("@", "/").split("/"):
        part = part.strip().lower().rstrip(".")
        if re.fullmatch(r"[a-z0-9._]{1,30}", part) and part not in NOT_PROFILES:
            return part
    return None


def load_invalid():
    try:
        return set(json.loads(INVALID_FILE.read_text()))
    except (OSError, json.JSONDecodeError):
        return set()


def main():
    parser = argparse.ArgumentParser(description="Ungültige Instagram-Links aufräumen")
    parser.add_argument("--dry-run", action="store_true", help="Nur anzeigen, nichts speichern")
    parser.add_argument("--keep", default="", help="Diese Namen trotzdem behalten (Komma-getrennt)")
    parser.add_argument("--token", default=os.environ.get("META_ACCESS_TOKEN"),
                        help="Meta-Zugriffsschlüssel (oder Umgebungsvariable META_ACCESS_TOKEN)")
    args = parser.parse_args()
    keep = {h.strip().lstrip("@").lower() for h in args.keep.split(",") if h.strip()}

    url, key = supabase_config()
    headers = {"apikey": key, "Authorization": f"Bearer {key}"}
    if not args.dry_run:
        headers["Authorization"] = f"Bearer {login(url, key)}"

    places = []
    for table, extra in (("restaurants", ""), ("osm_places", "&hidden=is.false&promoted_restaurant_id=is.null")):
        status, data = fetch_all_rows(url, f"{table}?select=id,name,instagram"
                                           f"&instagram=not.is.null&order=id.asc{extra}", headers)
        if status != 200:
            sys.exit(f"❌ {table} konnte nicht geladen werden ({status}): {data}")
        places += [{**p, "table": table} for p in data if (p.get("instagram") or "").strip()]
    print(f"📋 {len(places)} Orte mit Instagram-Link\n")

    fixes, removals = [], []   # (Ort, neuer Wert)
    for p in places:
        handle = clean_handle(p["instagram"])
        if handle is None:
            removals.append((p, "nicht lesbar"))
        elif handle_of(p["instagram"]) is None:
            # Schreibweise, mit der App und Skripte nichts anfangen können → nur den Namen speichern
            fixes.append((p, handle))
        p["_handle"] = handle

    if fixes:
        print("✏️  Korrigieren:")
        for p, handle in fixes:
            print(f"   {p['name']}: „{p['instagram']}“ → {handle}")

    if args.token:
        own_id, own_username = own_instagram_id(args.token)
        print(f"\n🔎 Prüfe {len(places)} Konten über die Meta-API …")
        for p in places:
            handle = p["_handle"]
            if not handle or handle in keep:
                continue
            _, error = profile_of(handle, own_id, args.token)
            if error and error.startswith(GLOBAL_ERRORS):
                sys.exit(f"❌ {error}{SETUP_HINT}")
            if error and "limit" in error.lower():
                print("⏸  Meta-Abfragelimit erreicht – in einer Stunde erneut starten.")
                break
            # (#110) bzw. „Invalid user id“ = Meta kennt kein Business-/Creator-Konto mit dem Namen
            if error and ("(#110)" in error or "invalid user" in error.lower() or "cannot find" in error.lower()):
                removals.append((p, "nicht gefunden"))
            time.sleep(1)  # Meta erlaubt ca. 200 Abfragen pro Stunde
    else:
        print("\nℹ️  Ohne META_ACCESS_TOKEN werden nur Schreibfehler korrigiert, Konten nicht geprüft.")

    if removals:
        print("\n🗑  Entfernen (Konto nicht gefunden – privat oder gelöscht):")
        for p, reason in removals:
            print(f"   {p['name']}: {p['instagram']} ({reason})")
        print("   → Gibt es eins davon doch? Dann beim echten Lauf mit --keep name1,name2 behalten.")

    if args.dry_run:
        print(f"\nDry-Run – nichts gespeichert. {len(fixes)} Korrekturen, {len(removals)} zum Entfernen.")
        return

    write = {**headers, "Content-Type": "application/json", "Prefer": "return=minimal"}
    invalid = load_invalid()
    failed = 0
    for p, value in fixes + [(p, None) for p, _ in removals]:
        status, body = http("PATCH", f"{url}/rest/v1/{p['table']}?id=eq.{p['id']}", write, {"instagram": value})
        if status not in (200, 204):
            failed += 1
            print(f"   ⚠️ {p['name']}: {status} {body}")
        elif value is None and p["_handle"]:
            invalid.add(p["_handle"])
    INVALID_FILE.write_text(json.dumps(sorted(invalid), indent=1))
    print(f"\n💾 {len(fixes)} korrigiert, {len(removals)} entfernt" + (f", {failed} fehlgeschlagen" if failed else ""))


if __name__ == "__main__":
    main()
