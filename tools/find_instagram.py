#!/usr/bin/env python3
"""
HalalBite – Instagram-Profile der Restaurants automatisch finden

Für jeden Ort (geprüft und aus OpenStreetMap) mit Website, aber ohne Instagram,
wird die Website (Startseite + Impressum/Kontakt) geladen und nach Links zu
instagram.com gesucht. Der passendste Name wird in Supabase eingetragen.
Instagram selbst wird dabei NICHT abgefragt (das verbieten deren Regeln).

Benutzung (Terminal):
    cd ~/Downloads/HalalBite2
    python3 tools/find_instagram.py --dry-run   # nur anzeigen, nichts speichern
    python3 tools/find_instagram.py             # eintragen (fragt nach Admin-Login)

Ergebnisse werden in tools/.instagram_cache.json gemerkt; Websites ohne Treffer
werden erst nach 60 Tagen erneut geprüft.
"""

import argparse
import difflib
import json
import re
import subprocess
import sys
import time
import urllib.parse
from collections import Counter
from concurrent.futures import ThreadPoolExecutor

from import_osm import ROOT, http, login, normalize, supabase_config

CACHE = ROOT / "tools" / ".instagram_cache.json"
BROWSER_AGENT = "Mozilla/5.0 (compatible; HalalBite/1.0; +https://halalbite.github.io/Halalbite/)"
RETRY_AFTER = 60 * 24 * 60 * 60  # Websites ohne Treffer: nach 60 Tagen erneut

# instagram.com/<name> – Unterseiten wie /p/, /reel/ sind keine Profile
INSTAGRAM_LINK = re.compile(r"instagram\.com\\?/(?:#!/)?([A-Za-z0-9._]{2,30})(?=[/?\"'#\s<]|$)", re.I)
NOT_PROFILES = {"p", "reel", "reels", "tv", "explore", "accounts", "stories", "direct", "about",
                "developer", "legal", "web", "share", "sharer", "instagram", "privacy", "embed.js", "policy",
                "muster", "beispiel", "example", "username", "yourname", "deinname", "name"}
# Unterseiten, auf denen Social-Links oft stehen
SUBPAGES = ["impressum", "kontakt", "contact"]


def load_cache():
    try:
        return json.loads(CACHE.read_text())
    except (OSError, json.JSONDecodeError):
        return {}


def fetch_page(url):
    """HTML einer Seite (folgt Weiterleitungen); bei Fehlern leer."""
    try:
        result = subprocess.run(["curl", "-sSL", "--max-time", "12", "--max-filesize", "3000000",
                                 "-A", BROWSER_AGENT, "-w", "\n%{http_code}", url], capture_output=True)
    except OSError:
        return ""
    raw, _, code = result.stdout.rpartition(b"\n")
    return raw.decode(errors="replace") if result.returncode == 0 and code == b"200" else ""


def normalized_site(website):
    site = website.strip()
    if not site.lower().startswith("http"):
        site = "https://" + site
    return site


def handles_on_site(website):
    """Alle Instagram-Namen, die auf der Website verlinkt sind (mit Häufigkeit)."""
    site = normalized_site(website)
    html = fetch_page(site)
    found = Counter(h.lower().rstrip(".") for h in INSTAGRAM_LINK.findall(html))
    if not found and html:
        # Impressum/Kontakt verlinkt von der Startseite oder unter Standard-Pfaden
        for sub in SUBPAGES:
            link = re.search(rf'href="([^"]*{sub}[^"]*)"', html, re.I)
            target = urllib.parse.urljoin(site, link.group(1)) if link else urllib.parse.urljoin(site + "/", sub)
            found.update(h.lower().rstrip(".") for h in INSTAGRAM_LINK.findall(fetch_page(target)))
            if found:
                break
    return {h: n for h, n in found.items() if h not in NOT_PROFILES}


def is_deep_link(website):
    """Website zeigt auf eine Unterseite fremder Seiten (Einkaufszentrum, Lieferdienst …)?"""
    path = urllib.parse.urlparse(normalized_site(website)).path.strip("/")
    return bool(re.sub(r"^(de|en|index\.html?|home)$", "", path, flags=re.I))


def similarity(a, b):
    return difflib.SequenceMatcher(None, a, b).ratio()


def matches_name(name, handle, ignore=""):
    """Passt der Instagram-Name zum Laden? Ähnlicher Text oder ein gemeinsames Wort (≥ 4 Zeichen)."""
    words = [w for w in normalize(name).split() if len(w) >= 4 and w != ignore]
    flat = re.sub(r"[._]", "", handle).replace(ignore, "")
    key = "".join(w for w in normalize(name).split() if w != ignore)
    return similarity(key, flat) >= 0.45 or any(w in flat for w in words)


def best_handle(name, website, handles, shared):
    """Wählt den Namen, der zum Laden passt. Namen, die auf vielen verschiedenen
    Websites vorkommen (Webagenturen, Lieferdienste), fallen raus."""
    candidates = {h: n for h, n in handles.items() if h not in shared}
    if not candidates:
        return None
    key = normalize(name).replace(" ", "")
    best = max(candidates, key=lambda h: similarity(key, re.sub(r"[._]", "", h)) * 3 + candidates[h])
    # Unterseite einer fremden Website (Einkaufszentrum …): deren Name zählt nicht als Treffer
    host = urllib.parse.urlparse(normalized_site(website)).hostname or ""
    domain = host.removeprefix("www.").split(".")[0] if is_deep_link(website) else ""
    if not matches_name(name, best, ignore=domain):
        return None
    return best


def fetch_all(url, headers, table, columns, extra=""):
    rows, offset = [], 0
    while True:
        status, page = http("GET", f"{url}/rest/v1/{table}?select={columns}{extra}&order=id&limit=1000&offset={offset}",
                            headers=headers)
        if status != 200:
            sys.exit(f"❌ {table} konnte nicht geladen werden ({status}): {page}")
        rows += page
        if len(page) < 1000:
            return rows
        offset += 1000


def main():
    parser = argparse.ArgumentParser(description="Instagram-Profile der Restaurants finden")
    parser.add_argument("--dry-run", action="store_true", help="nur anzeigen, nichts speichern")
    parser.add_argument("--limit", type=int, help="höchstens so viele Websites prüfen")
    args = parser.parse_args()

    url, key = supabase_config()
    token = None if args.dry_run else login(url, key)
    headers = {"apikey": key, "Authorization": f"Bearer {token or key}"}

    places = [("restaurants", p) for p in fetch_all(url, headers, "restaurants", "id,name,website,instagram")]
    places += [("osm_places", p) for p in fetch_all(url, headers, "osm_places", "id,name,website,instagram",
                                                    "&hidden=eq.false&promoted_restaurant_id=is.null")]
    todo = [(t, p) for t, p in places if (p.get("website") or "").strip() and not (p.get("instagram") or "").strip()]
    print(f"📋 {len(places)} Orte, davon {len(todo)} mit Website und ohne Instagram")

    cache = load_cache()
    now = time.time()
    fresh = [(t, p) for t, p in todo
             if not (cache.get(p["website"]) is not None
                     and (cache[p["website"]]["handles"] or now - cache[p["website"]]["checked"] < RETRY_AFTER))]
    if args.limit:
        fresh = fresh[:args.limit]

    if fresh:
        print(f"🌐 Prüfe {len(fresh)} Websites …")
        sites = list({p["website"] for _, p in fresh})
        with ThreadPoolExecutor(max_workers=8) as pool:
            for i, (site, handles) in enumerate(zip(sites, pool.map(handles_on_site, sites)), 1):
                cache[site] = {"handles": handles, "checked": now}
                if i % 25 == 0:
                    print(f"   … {i}/{len(sites)}")
        CACHE.write_text(json.dumps(cache, indent=1, ensure_ascii=False))

    # Namen, die auf mehr als 3 verschiedenen Websites auftauchen, gehören nicht einem Laden
    usage = Counter(h for entry in cache.values() for h in entry["handles"])
    shared = {h for h, n in usage.items() if n > 3}
    # Von tools/clean_instagram.py als ungültig entfernte Namen nicht wieder eintragen
    try:
        shared |= set(json.loads((ROOT / "tools" / ".instagram_invalid.json").read_text()))
    except (OSError, json.JSONDecodeError):
        pass

    found = []
    for table, p in todo:
        entry = cache.get(p["website"])
        handle = entry and best_handle(p["name"], p["website"], entry["handles"], shared)
        if handle:
            found.append((table, p, handle))

    for table, p, handle in found:
        print(f"   📸 {p['name']}: @{handle}")
    print(f"\n✅ {len(found)} Instagram-Profile gefunden")

    if args.dry_run or not found:
        print("ℹ️  Testlauf – nichts gespeichert." if args.dry_run else "")
        return

    write = {**headers, "Content-Type": "application/json", "Prefer": "return=minimal"}
    failed = 0
    for table, p, handle in found:
        status, body = http("PATCH", f"{url}/rest/v1/{table}?id=eq.{p['id']}&or=(instagram.is.null,instagram.eq.)",
                            headers=write, body={"instagram": handle})
        if status not in (200, 204):
            failed += 1
            print(f"   ⚠️ {p['name']}: {status} {body}")
    print(f"💾 {len(found) - failed} gespeichert" + (f", {failed} fehlgeschlagen" if failed else ""))


if __name__ == "__main__":
    main()
