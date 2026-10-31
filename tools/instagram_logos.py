#!/usr/bin/env python3
"""
HalalBite – Logos aus Instagram-Profilbildern vorschlagen

Für jeden Ort (geprüft und aus OpenStreetMap) mit Instagram-Link, aber ohne Logo,
wird über die OFFIZIELLE Instagram Graph API („Business Discovery“) das Profilbild
geholt, in Supabase Storage (Bucket restaurant-logos) kopiert und als Vorschlag in
die Tabelle logo_candidates eingetragen. In der App erscheint es erst, wenn du es
unter Einstellungen → Admin → Prüfen → „Logos“ freigibst.

Funktioniert nur für Business- und Creator-Konten (bei Restaurants fast immer).
Private/persönliche Konten liefert die API nicht – die werden übersprungen.

Voraussetzungen (einmalig, siehe AppStore/05_Instagram-Logos.md):
  • @halalbiteee ist ein Business-/Creator-Konto und mit einer Facebook-Seite verknüpft
  • Meta-Entwickler-App + Zugriffsschlüssel (Token) aus dem Graph API Explorer
  • SQL-Skript supabase/migrations/20261106_instagram_logos.sql ausgeführt

Benutzung (Terminal):
    cd ~/Downloads/HalalBite2
    export META_ACCESS_TOKEN="EAAG…"                 # Token aus dem Graph API Explorer
    python3 tools/instagram_logos.py --dry-run       # nur anzeigen, nichts speichern
    python3 tools/instagram_logos.py                 # Vorschläge anlegen (fragt nach Admin-Login)
"""

import argparse
import os
import re
import subprocess
import sys
import time
import urllib.parse
from pathlib import Path

from import_osm import USER_AGENT, fetch_all_rows, http, login, supabase_config

GRAPH = "https://graph.facebook.com/v23.0"
# Benutzername aus „@name“, „name“ oder „https://www.instagram.com/name/…“
HANDLE = re.compile(r"^(?:@|(?:https?://)?(?:www\.)?instagram\.com/)?([A-Za-z0-9._]{1,30})/?(?:[?#].*)?$", re.I)


def handle_of(value):
    match = HANDLE.match((value or "").strip())
    return match.group(1).lower().rstrip(".") if match else None


def graph(path, token, **params):
    query = urllib.parse.urlencode({**params, "access_token": token})
    status, data = http("GET", f"{GRAPH}/{path}?{query}", timeout=30)
    if status != 200 or not isinstance(data, dict):
        message = data.get("error", {}).get("message") if isinstance(data, dict) else str(data)[:200]
        return None, message or f"HTTP {status}"
    return data, None


def fail(message):
    """Abbrechen. Auf GitHub als gelbe Warnung (die anderen Schritte laufen weiter), sonst als Fehler."""
    if os.environ.get("GITHUB_ACTIONS"):
        print(f"::warning title=Instagram-Logos übersprungen::{message.splitlines()[0]}")
        print(message)
        sys.exit(0)
    sys.exit(f"❌ {message}")


def warn_if_expiring(token, days=10):
    """Hinweis, wenn der Token bald abläuft (langlebige Tokens gelten 60 Tage)"""
    data, error = graph("debug_token", token, input_token=token)
    expires = (data or {}).get("data", {}).get("expires_at")
    if error or not expires:  # 0 = läuft nicht ab (z. B. Seiten-Token)
        return
    left = (expires - time.time()) / 86400
    if left < days:
        message = (f"Der Meta-Token läuft in {max(left, 0):.0f} Tagen ab – bitte erneuern "
                   "(AppStore/05_Instagram-Logos.md, „Token für GitHub“).")
        print(f"::warning title=Meta-Token läuft bald ab::{message}" if os.environ.get("GITHUB_ACTIONS")
              else f"⚠️  {message}")


def own_instagram_id(token):
    """ID des eigenen Instagram-Business-Kontos – mit Nutzer-Token über die verknüpfte
    Facebook-Seite, mit Seiten-Token direkt"""
    data, error = graph("me/accounts", token, fields="name,instagram_business_account{id,username}")
    pages = (data or {}).get("data", [])
    if error or not pages:
        # Seiten-Token: "me" ist dann die Facebook-Seite selbst
        page, page_error = graph("me", token, fields="name,instagram_business_account{id,username}")
        if page and page.get("instagram_business_account"):
            pages, error = [page], None
        elif error or page_error:
            fail(f"Meta-Token ungültig oder abgelaufen: {error or page_error}\n"
                 "   → Neuen Token holen: AppStore/05_Instagram-Logos.md")
    for page in pages:
        account = page.get("instagram_business_account")
        if account:
            print(f"✅ Abfrage über @{account.get('username')} (Facebook-Seite „{page.get('name')}“)")
            return account["id"], account.get("username")
    fail("Keine Facebook-Seite mit verknüpftem Instagram-Business-Konto gefunden.\n"
         "   → Anleitung: AppStore/05_Instagram-Logos.md, Schritt 1–2")


# Fehler, die nicht am einzelnen Restaurant liegen, sondern an App/Token → sofort abbrechen
GLOBAL_ERRORS = ("(#10)", "(#190)", "(#200)", "(#3)")
SETUP_HINT = """
   Die Meta-App darf „Business Discovery“ noch nicht nutzen. So behebst du das:
   1. developers.facebook.com → deine App → links „Anwendungsfälle“ (Use cases)
      → „Anwendungsfall hinzufügen“ → Instagram-Anwendungsfall („Nachrichten und Inhalte
      auf Instagram verwalten“) → bei „API-Einrichtung mit Facebook Login“ auf „Anpassen“.
      Dort die Berechtigungen instagram_basic, instagram_manage_insights,
      pages_read_engagement und business_management hinzufügen.
   2. Graph API Explorer: NEUEN Token erzeugen und dabei alle Berechtigungen anhaken:
      instagram_basic, instagram_manage_insights, pages_show_list,
      pages_read_engagement, business_management
      → beim Facebook-Dialog Seite UND Instagram-Konto auswählen.
   3. export META_ACCESS_TOKEN="…neuer Token…" und das Skript erneut starten.
   Details: AppStore/05_Instagram-Logos.md"""


def profile_of(handle, own_id, token):
    fields = f"business_discovery.username({handle}){{username,name,profile_picture_url,followers_count}}"
    data, error = graph(own_id, token, fields=fields)
    return (data or {}).get("business_discovery"), error


def places_without_logo(url, headers):
    common = "instagram=not.is.null&logo_url=is.null&order=id.asc&limit=5000"
    queries = {
        "restaurants": f"select=id,name,instagram&{common}",
        # ausgeblendete und bereits als geprüfter Ort übernommene OSM-Orte überspringen
        "osm_places": f"select=id,name,city,instagram&hidden=is.false&promoted_restaurant_id=is.null&{common}",
    }
    rows = []
    for table, query in queries.items():
        status, data = fetch_all_rows(url, f"{table}?{query}", headers)
        if status != 200:
            sys.exit(f"❌ {table} konnte nicht geladen werden ({status}): {data}")
        rows += [{**r, "table": table} for r in data if handle_of(r.get("instagram"))]
    return rows


def existing_candidates(url, headers):
    status, data = fetch_all_rows(url, "logo_candidates?select=place_table,place_id", headers)
    if status != 200:
        sys.exit("❌ Tabelle logo_candidates fehlt – bitte zuerst "
                 "supabase/migrations/20261106_instagram_logos.sql im SQL Editor ausführen.")
    return {(r["place_table"], r["place_id"]) for r in data}


def upload_logo(row, picture_url, url, headers):
    """Instagram-Bildlinks laufen ab → Bild herunterladen und dauerhaft in Supabase ablegen.
    Gibt (öffentlicher Link, None) oder (None, Fehlerbeschreibung) zurück."""
    tmp = Path("/tmp") / f"ig_{row['table']}_{row['id']}.jpg"
    download = subprocess.run(["curl", "-sSL", "--max-time", "30", "-A", USER_AGENT,
                               "-w", "%{http_code} %{content_type}", "-o", str(tmp), picture_url],
                              capture_output=True, text=True)
    code, _, content_type = download.stdout.partition(" ")
    if download.returncode != 0 or code != "200" or not tmp.exists() or tmp.stat().st_size == 0:
        tmp.unlink(missing_ok=True)
        return None, f"Download von Instagram fehlgeschlagen (HTTP {code or '-'}) {download.stderr.strip()[:80]}"
    content_type = content_type.split(";")[0].strip() or "image/jpeg"
    image, error = upload_to_bucket(tmp, f"instagram/{row['table']}_{row['id']}.jpg", content_type, url, headers)
    tmp.unlink(missing_ok=True)
    return image, error


def upload_to_bucket(local_file, path, content_type, url, headers):
    """Datei in den Bucket restaurant-logos legen → (öffentlicher Link, None) oder (None, Fehler)"""
    upload = subprocess.run(["curl", "-sS", "-w", "\n%{http_code}", "-X", "POST",
                             f"{url}/storage/v1/object/restaurant-logos/{path}",
                             "-H", f"apikey: {headers['apikey']}", "-H", f"Authorization: {headers['Authorization']}",
                             "-H", f"Content-Type: {content_type}",
                             "--data-binary", f"@{local_file}"], capture_output=True, text=True)
    body, _, code = upload.stdout.rpartition("\n")
    # 409 = Datei liegt schon da (z. B. von einem abgebrochenen Lauf) → weiterverwenden.
    # Bewusst ohne „x-upsert“: Überschreiben bräuchte zusätzlich eine Lese-Regel auf storage.objects.
    if code not in ("200", "201") and not (code == "400" and "Duplicate" in body) and code != "409":
        return None, f"Upload zu Supabase fehlgeschlagen (HTTP {code or '-'}, {content_type}): {body.strip()[:160]}"
    # Zeitstempel im Link, damit die App ein ersetztes Bild nicht aus dem Cache zeigt
    return f"{url}/storage/v1/object/public/restaurant-logos/{path}?v={int(time.time())}", None


def main():
    parser = argparse.ArgumentParser(description="Logos aus Instagram-Profilbildern vorschlagen")
    parser.add_argument("--dry-run", action="store_true", help="Nur anzeigen, nichts speichern")
    parser.add_argument("--limit", type=int, help="Höchstens so viele Orte abfragen")
    parser.add_argument("--token", default=os.environ.get("META_ACCESS_TOKEN"),
                        help="Meta-Zugriffsschlüssel (oder Umgebungsvariable META_ACCESS_TOKEN)")
    args = parser.parse_args()
    if not args.token:
        fail("Kein Meta-Token. Lokal: export META_ACCESS_TOKEN=\"…\" – auf GitHub: Secret META_ACCESS_TOKEN "
             "anlegen (siehe AppStore/05_Instagram-Logos.md)")

    url, key = supabase_config()
    own_id, own_username = own_instagram_id(args.token)
    # Vorabtest am eigenen Konto: klappt das nicht, klappt es bei keinem Restaurant
    _, error = profile_of(own_username, own_id, args.token)
    if error:
        fail(f"Testabfrage für @{own_username} fehlgeschlagen: {error}{SETUP_HINT}")
    print("✅ Business Discovery funktioniert")
    warn_if_expiring(args.token)

    headers = {"apikey": key, "Authorization": f"Bearer {key}"}
    if not args.dry_run:
        headers["Authorization"] = f"Bearer {login(url, key)}"

    rows = places_without_logo(url, headers)
    if not args.dry_run:
        done = existing_candidates(url, headers)
        rows = [r for r in rows if (r["table"], r["id"]) not in done]
    if args.limit:
        rows = rows[:args.limit]
    print(f"🔎 {len(rows)} Orte mit Instagram, aber ohne Logo\n")

    stats = {"neu": 0, "kein Business-Konto": 0, "ohne Bild": 0, "Fehler": 0}
    failed_uploads = 0  # mehrere Upload-Fehler hintereinander → Problem mit Speicher/Rechten
    for i, row in enumerate(rows, 1):
        handle = handle_of(row["instagram"])
        profile, error = profile_of(handle, own_id, args.token)
        label = f"[{i}/{len(rows)}] {row['name']} (@{handle})"
        if error and error.startswith(GLOBAL_ERRORS):
            fail(f"{label}: {error}{SETUP_HINT}")
        if error:
            # Business Discovery kennt nur Business-/Creator-Konten
            key_ = "kein Business-Konto" if "business" in error.lower() or "user" in error.lower() else "Fehler"
            stats[key_] += 1
            print(f"   ⏭  {label}: {error[:90]}")
            if "limit" in error.lower():
                print("\n⏸  Meta-Abfragelimit erreicht – in einer Stunde erneut starten (bereits Gefundenes bleibt).")
                break
        elif not profile.get("profile_picture_url"):
            stats["ohne Bild"] += 1
            print(f"   ⏭  {label}: kein Profilbild")
        elif args.dry_run:
            stats["neu"] += 1
            print(f"   ✅ {label} → „{profile.get('name') or ''}“, {profile.get('followers_count', '?')} Follower")
        else:
            image, upload_error = upload_logo(row, profile["profile_picture_url"], url, headers)
            candidate = {
                "place_table": row["table"], "place_id": row["id"], "place_name": row["name"],
                "city": row.get("city"), "instagram": handle, "image_url": image,
                "ig_name": profile.get("name"), "ig_followers": profile.get("followers_count"),
            }
            status, data = (0, upload_error) if not image else http(
                "POST", f"{url}/rest/v1/logo_candidates",
                {**headers, "Content-Type": "application/json", "Prefer": "return=minimal"}, candidate)
            if status in (200, 201):
                stats["neu"] += 1
                failed_uploads = 0
                print(f"   ✅ {label}")
            else:
                stats["Fehler"] += 1
                failed_uploads += 1
                print(f"   ❌ {label}: {str(data)[:200]}")
                if failed_uploads >= 3:
                    sys.exit("\n⏹  3 Fehler hintereinander – abgebrochen. Bitte die Meldung oben an Claude schicken.")
        time.sleep(1)  # Meta erlaubt ca. 200 Abfragen pro Stunde

    print("\n" + " · ".join(f"{k}: {v}" for k, v in stats.items()))
    if stats["neu"] and not args.dry_run:
        print("👉 Jetzt in der App: Einstellungen → Admin → Prüfen → „Logos“ durchsehen und freigeben.")


if __name__ == "__main__":
    main()
