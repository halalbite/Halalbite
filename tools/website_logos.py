#!/usr/bin/env python3
"""
HalalBite – Logos von den Websites der Restaurants vorschlagen

Für jeden Ort (geprüft und aus OpenStreetMap) mit Website, aber ohne Logo, wird die
Startseite geladen und nach dem Logo gesucht – in dieser Reihenfolge:
  1. App-Symbol der Website (apple-touch-icon, quadratisch – passt am besten in den Kreis)
  2. Logo aus den strukturierten Daten (schema.org "logo", og:logo)
  3. <img>-Bild mit "logo" im Namen/Klasse/Alt-Text
  4. großes Website-Symbol (icon ≥ 96 px)
Das Bild wird in PNG umgewandelt, in Supabase Storage (Bucket restaurant-logos) gelegt und
als Vorschlag in logo_candidates eingetragen. In der App erscheint es erst nach deiner
Freigabe unter Einstellungen → Admin → Prüfen → „Logos“.

Übersprungen werden:
  • Verzeichnis- und Lieferseiten (golocal, Lieferando, Einkaufszentren …) – deren Logo wäre falsch
  • Bilder, die bei mehreren Orten identisch sind (z. B. Standard-Symbol eines Baukastens)
  • zu kleine Bilder und SVG-Dateien (kann die App nicht anzeigen)

Voraussetzung: SQL-Skript supabase/migrations/20261108_logo_candidates_website.sql ausgeführt.
Bilder werden auf dem Mac mit „sips“ umgewandelt, sonst (z. B. GitHub Actions) mit Pillow
(pip install pillow).

Benutzung (Terminal):
    cd ~/Downloads/HalalBite2
    python3 tools/website_logos.py --dry-run    # nur anzeigen, nichts speichern
    python3 tools/website_logos.py              # Vorschläge anlegen (fragt nach Admin-Login)
"""

import argparse
import hashlib
import html as html_lib
import re
import shutil
import subprocess
import sys
import time
import urllib.parse
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from find_instagram import BROWSER_AGENT, fetch_page, normalized_site
from import_osm import fetch_all_rows, http, login, supabase_config
from instagram_logos import upload_to_bucket

# Seiten, die viele Restaurants auflisten – das Logo dort gehört nicht zum Restaurant
DIRECTORY_DOMAINS = (
    "golocal.", "westfield.", "lieferando.", "wolt.", "ubereats.", "foodora.", "pizza.de",
    "facebook.", "instagram.", "google.", "goo.gl", "yelp.", "tripadvisor.", "gelbeseiten.",
    "dasoertliche.", "meinestadt.", "linktr.ee", "speisekarte.", "restaurantguru.", "thefork.",
    "opentable.", "quandoo.", "tiktok.", "youtube.", "ece.de", "mall", "center.de", "galeria.",
    "bahnhof.de", "flughafen", "airport", "foursquare.", "branchenbuch", "11880.", "werkenntdenbesten.",
)
MIN_SIDE = 64      # kürzere Bildseite in Pixeln
MIN_LONG_SIDE = 96 # längere Bildseite in Pixeln
WORK = Path("/tmp/halalbite_website_logos")


def attr(tag, name):
    match = re.search(rf'\b{name}\s*=\s*(["\'])(.*?)\1', tag, re.I | re.S)
    return html_lib.unescape(match.group(2).strip()) if match else ""


def candidate_urls(page, base):
    """Mögliche Logo-Links einer Seite, beste zuerst"""
    base_tag = re.search(r"<base\b[^>]*>", page, re.I)
    if base_tag and attr(base_tag.group(0), "href"):
        base = urllib.parse.urljoin(base, attr(base_tag.group(0), "href"))
    found = []

    def add(priority, link):
        link = (link or "").strip().split(" ")[0]  # srcset: "bild.png 2x" → "bild.png"
        if link and not link.startswith("data:"):
            found.append((priority, urllib.parse.urljoin(base, link)))

    for tag in re.findall(r"<link\b[^>]*>", page, re.I):
        rel, href, sizes = attr(tag, "rel").lower(), attr(tag, "href"), attr(tag, "sizes")
        if "apple-touch-icon" in rel:
            add(1, href)
        elif "icon" in rel:
            size = max((int(n) for n in re.findall(r"\d+", sizes)), default=0)
            if size >= MIN_LONG_SIDE or href.lower().endswith(".png"):
                add(4, href)
    for match in re.finditer(r'"logo"\s*:\s*(?:"([^"]+)"|\{[^{}]*?"(?:url|contentUrl)"\s*:\s*"([^"]+)")', page):
        add(2, (match.group(1) or match.group(2)).replace("\\/", "/"))
    for tag in re.findall(r"<meta\b[^>]*>", page, re.I):
        if attr(tag, "property").lower() == "og:logo" or attr(tag, "itemprop").lower() == "logo":
            add(2, attr(tag, "content"))
    for tag in re.findall(r"<img\b[^>]*>", page, re.I):
        if re.search(r"logo", tag, re.I) and not re.search(r"payment|paypal|visa|partner|sponsor|award", tag, re.I):
            add(3, attr(tag, "src") or attr(tag, "data-src") or attr(tag, "srcset"))

    seen, ordered = set(), []
    for _, link in sorted(found, key=lambda item: item[0]):
        if link not in seen:
            seen.add(link)
            ordered.append(link)
    return ordered[:6]


def convert_with_pillow(source, target):
    """Wie sips: in PNG umwandeln, längste Seite max. 512 px → (Breite, Höhe) oder None"""
    try:
        from PIL import Image
    except ImportError:
        sys.exit("❌ Pillow fehlt: pip install pillow")
    try:
        with Image.open(source) as image:
            image = image.convert("RGBA")
            image.thumbnail((512, 512))
            image.save(target, "PNG")
            return image.size
    except Exception:
        return None


def image_size(file):
    result = subprocess.run(["sips", "-g", "pixelWidth", "-g", "pixelHeight", str(file)],
                            capture_output=True, text=True)
    numbers = re.findall(r"pixel(?:Width|Height):\s*(\d+)", result.stdout)
    return (int(numbers[0]), int(numbers[1])) if len(numbers) == 2 else (0, 0)


def download_as_png(link, target):
    """Bild laden und in PNG (max. 512 px) umwandeln → (Breite, Höhe) oder None"""
    raw = target.with_suffix(".download")
    result = subprocess.run(["curl", "-sSL", "--max-time", "15", "--max-filesize", "5000000",
                             "-A", BROWSER_AGENT, "-w", "%{http_code} %{content_type}", "-o", str(raw), link],
                            capture_output=True, text=True)
    code, _, content_type = result.stdout.partition(" ")
    try:
        if result.returncode != 0 or code != "200" or "svg" in content_type or link.lower().split("?")[0].endswith(".svg"):
            return None
        if shutil.which("sips"):
            converted = subprocess.run(["sips", "-s", "format", "png", "-Z", "512", str(raw), "--out", str(target)],
                                       capture_output=True)
            if converted.returncode != 0 or not target.exists():
                return None
            width, height = image_size(target)
        else:
            size = convert_with_pillow(raw, target)
            if not size:
                return None
            width, height = size
        if min(width, height) < MIN_SIDE or max(width, height) < MIN_LONG_SIDE:
            return None
        return width, height
    finally:
        raw.unlink(missing_ok=True)


def find_logo(row):
    """Sucht das Logo einer Website → row ergänzt um _file/_source/_size oder _skip (Grund)"""
    site = normalized_site(row["website"])
    host = urllib.parse.urlparse(site).netloc.lower()
    if any(domain in host for domain in DIRECTORY_DOMAINS):
        return {**row, "_skip": "Verzeichnis-/Lieferseite"}
    page = fetch_page(site)
    if not page:
        return {**row, "_skip": "Website nicht erreichbar"}
    # Rückfall: viele Websites haben das App-Symbol an der Standard-Adresse, ohne es zu verlinken
    links = candidate_urls(page, site) + [urllib.parse.urljoin(site, "/apple-touch-icon.png")]
    target = WORK / f"{row['table']}_{row['id']}.png"
    for link in links:
        size = download_as_png(link, target)
        if size:
            digest = hashlib.sha1(target.read_bytes()).hexdigest()
            return {**row, "_file": target, "_source": link, "_size": size, "_hash": digest, "_page": site}
    target.unlink(missing_ok=True)
    return {**row, "_skip": "kein passendes Logo gefunden"}


def places_without_logo(url, headers):
    common = "website=not.is.null&logo_url=is.null&order=id.asc&limit=5000"
    queries = {
        "restaurants": f"select=id,name,website&{common}",
        "osm_places": f"select=id,name,city,website&hidden=is.false&promoted_restaurant_id=is.null&{common}",
    }
    rows = []
    for table, query in queries.items():
        status, data = fetch_all_rows(url, f"{table}?{query}", headers)
        if status != 200:
            sys.exit(f"❌ {table} konnte nicht geladen werden ({status}): {data}")
        rows += [{**r, "table": table} for r in data if (r.get("website") or "").strip()]
    return rows


def existing_candidates(url, headers):
    """Orte, für die es schon einen Vorschlag gibt (egal aus welcher Quelle)"""
    status, data = fetch_all_rows(url, "logo_candidates?select=place_table,place_id,source", headers)
    if status != 200:
        sys.exit("❌ Spalte „source“ fehlt – bitte zuerst supabase/migrations/20261108_logo_candidates_website.sql "
                 "im SQL Editor ausführen.")
    return {(r["place_table"], r["place_id"]) for r in data}


def main():
    parser = argparse.ArgumentParser(description="Logos von den Websites der Restaurants vorschlagen")
    parser.add_argument("--dry-run", action="store_true", help="Nur anzeigen, nichts speichern")
    parser.add_argument("--limit", type=int, help="Höchstens so viele Orte prüfen")
    args = parser.parse_args()

    url, key = supabase_config()
    headers = {"apikey": key, "Authorization": f"Bearer {key}"}
    if not args.dry_run:
        headers["Authorization"] = f"Bearer {login(url, key)}"

    rows = places_without_logo(url, headers)
    if not args.dry_run:
        done = existing_candidates(url, headers)
        rows = [r for r in rows if (r["table"], r["id"]) not in done]
    if args.limit:
        rows = rows[:args.limit]
    print(f"🔎 {len(rows)} Orte mit Website, aber ohne Logo – lade Websites …\n")

    WORK.mkdir(parents=True, exist_ok=True)
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(find_logo, rows))

    # Dasselbe Bild bei mehreren Orten = Standard-Symbol eines Baukastens/Verzeichnisses
    shared = {h for h, n in Counter(r.get("_hash") for r in results if r.get("_hash")).items() if n >= 3}
    stats = Counter()
    for r in results:
        label = f"{r['name']} ({urllib.parse.urlparse(normalized_site(r['website'])).netloc})"
        if r.get("_hash") in shared:
            r["_skip"] = "gleiches Bild wie bei anderen Orten (Standard-Symbol)"
        if r.get("_skip"):
            stats[r["_skip"]] += 1
            print(f"   ⏭  {label}: {r['_skip']}")
            continue
        width, height = r["_size"]
        if args.dry_run:
            stats["gefunden"] += 1
            print(f"   ✅ {label}: {width}×{height} px ← {r['_source'][:90]}")
            continue
        image, error = upload_to_bucket(r["_file"], f"website/{r['table']}_{r['id']}.png", "image/png", url, headers)
        candidate = {
            "place_table": r["table"], "place_id": r["id"], "place_name": r["name"], "city": r.get("city"),
            "source": "website", "source_url": r["_page"], "image_url": image,
        }
        status, data = (0, error) if not image else http(
            "POST", f"{url}/rest/v1/logo_candidates",
            {**headers, "Content-Type": "application/json", "Prefer": "return=minimal"}, candidate)
        if status in (200, 201):
            stats["gefunden"] += 1
            print(f"   ✅ {label}: {width}×{height} px")
        else:
            stats["Fehler"] += 1
            print(f"   ❌ {label}: {str(data)[:200]}")
        time.sleep(0.2)

    for r in results:
        if r.get("_file"):
            Path(r["_file"]).unlink(missing_ok=True)
    print("\n" + " · ".join(f"{k}: {v}" for k, v in stats.most_common()))
    if stats["gefunden"] and not args.dry_run:
        print("👉 Jetzt in der App: Einstellungen → Admin → Prüfen → „Logos“ durchsehen und freigeben.")


if __name__ == "__main__":
    main()
