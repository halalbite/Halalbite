#!/usr/bin/env python3
"""
HalalBite – Halal-Orte aus OpenStreetMap importieren (deutschlandweit)

Lädt alle Restaurants, Imbisse, Cafés und Eisdielen mit diet:halal=yes/only
aus OpenStreetMap, erkennt Duplikate zu euren geprüften Orten und schreibt sie
in die Supabase-Tabelle `osm_places`. Kann beliebig oft wiederholt werden:
neue Orte kommen dazu, geänderte werden aktualisiert, aus OSM gelöschte entfernt.
Von euch übernommene oder ausgeblendete Orte bleiben unangetastet.

Benutzung (Terminal):
    cd ~/Downloads/HalalBite2
    python3 tools/import_osm.py --dry-run     # nur anzeigen, nichts hochladen
    python3 tools/import_osm.py               # importieren (fragt nach Admin-Login)

Daten: © OpenStreetMap-Mitwirkende, Lizenz ODbL – https://www.openstreetmap.org/copyright
"""

import argparse
import os
import difflib
import getpass
import json
import math
import re
import subprocess
import sys
import time
import unicodedata
import urllib.parse
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
# Mehrere öffentliche Overpass-Server – fällt einer aus, wird der nächste probiert
OVERPASS_URLS = [
    "https://overpass-api.de/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
    "https://overpass.private.coffee/api/interpreter",
]
USER_AGENT = "HalalBite-Import/1.0 (halalbiteapp@gmail.com)"

# Nur Gastronomie – ohne Supermärkte und Metzger
AMENITIES = ["restaurant", "fast_food", "cafe", "ice_cream", "food_court"]
# Große Ketten, die HalalBite bewusst nicht zeigt (Name oder Marke)
EXCLUDED_CHAINS = ["mcdonald", "burgerking"]

# Bewusst einfache Abfrage (läuft schnell); Gastronomie wird danach in Python gefiltert
OVERPASS_QUERY = """
[out:json][timeout:180];
area["ISO3166-1"="DE"][admin_level=2]->.de;
nwr["diet:halal"~"^(yes|only)$"](area.de);
out tags center;
"""

# OSM-"cuisine" → HalalBite-Kategorie (PlaceCategory.rawValue in der App)
CUISINE_MAP = [
    (("burger",), "Burger"),
    (("kebab", "turkish", "doner", "döner", "lahmacun", "pide"), "Türkisch"),
    (("arab", "lebanese", "syrian", "middle_eastern", "falafel", "persian", "iranian", "afghan",
      "moroccan", "egyptian", "iraqi", "yemeni", "shawarma", "mediterranean"), "Arabisch / Orientalisch"),
    (("asian", "chinese", "thai", "vietnamese", "indian", "pakistani", "japanese", "sushi", "korean",
      "malaysian", "indonesian", "bangladeshi", "noodle", "ramen", "curry"), "Asiatisch"),
    (("breakfast", "coffee_shop", "bakery", "brunch"), "Frühstück / Café"),
    (("pizza", "italian", "pasta"), "Pizza / Pasta"),
    (("steak_house", "steak", "grill", "barbecue", "bbq", "chicken"), "Grill / Steak"),
    (("ice_cream", "dessert", "cake", "waffle", "crepe", "donut", "chocolate"), "Dessert / Süßes"),
]

# Adressen für Orte ohne Adresse: Nominatim (Geocoder von OpenStreetMap).
# Nutzungsregeln: max. 1 Anfrage/Sekunde, eigener User-Agent, Ergebnisse cachen.
NOMINATIM_URL = "https://nominatim.openstreetmap.org/reverse"
GEOCODE_CACHE = ROOT / "tools" / ".geocode_cache.json"
OSM_CACHE = ROOT / "tools" / ".osm_cache.json"  # letzter erfolgreicher Overpass-Download
MEDIA_CACHE = ROOT / "tools" / ".media_cache.json"
MEDIA_FIELDS = ["logo_url", "logo_credit", "image_url", "image_credit", "image_source", "image_status"]

DAYS = ["Mo", "Tu", "We", "Th", "Fr", "Sa", "Su"]
DAY_NAMES = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]


# ---------------------------------------------------------------------------
# Hilfsfunktionen
# ---------------------------------------------------------------------------

def http(method, url, headers=None, body=None, timeout=200):
    """HTTP über curl (auf jedem Mac vorhanden) – das mitgelieferte Python hat
    teils veraltete SSL-Bibliotheken, die manche Server ablehnen."""
    data = None if body is None else (body if isinstance(body, bytes) else json.dumps(body).encode())
    cmd = ["curl", "-sS", "-X", method, "--max-time", str(timeout), "-A", USER_AGENT,
           "-w", "\n%{http_code}", url]
    for k, v in (headers or {}).items():
        cmd += ["-H", f"{k}: {v}"]
    if data is not None:
        cmd += ["--data-binary", "@-"]
    result = subprocess.run(cmd, input=data, capture_output=True)
    if result.returncode != 0:
        return 0, result.stderr.decode(errors="replace")
    raw, _, code = result.stdout.rpartition(b"\n")
    status = int(code or 0)
    try:
        return status, (json.loads(raw) if raw.strip() else None)
    except json.JSONDecodeError:
        return status, raw.decode(errors="replace")


def supabase_config():
    """URL und anon-Key: aus Umgebungsvariablen (GitHub Actions) oder aus der App."""
    if os.environ.get("SUPABASE_URL") and os.environ.get("SUPABASE_ANON_KEY"):
        return os.environ["SUPABASE_URL"].rstrip("/"), os.environ["SUPABASE_ANON_KEY"]
    swift = (ROOT / "HalalBite2" / "SupabaseManager.swift").read_text()
    url = re.search(r'supabaseURL = URL\(string: "([^"]+)"\)', swift).group(1)
    key = re.search(r'anonKey = "([^"]+)"', swift).group(1)
    return url, key


def normalize(text):
    text = unicodedata.normalize("NFKD", text or "").encode("ascii", "ignore").decode().lower()
    return re.sub(r"[^a-z0-9]+", " ", text).strip()


def distance_m(lat1, lon1, lat2, lon2):
    r = 6371000
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def is_excluded_chain(tags):
    text = " ".join(tags.get(k, "") for k in ("name", "brand", "operator"))
    key = re.sub(r"[^a-z]", "", unicodedata.normalize("NFKD", text.lower()))
    return any(chain in key for chain in EXCLUDED_CHAINS)


def category_for(tags):
    cuisines = [c.strip().lower() for c in re.split(r"[;,]", tags.get("cuisine", "")) if c.strip()]
    for keys, category in CUISINE_MAP:
        if any(any(k in c for k in keys) for c in cuisines):
            return category
    amenity = tags.get("amenity")
    if amenity == "cafe":
        return "Frühstück / Café"
    if amenity == "ice_cream":
        return "Dessert / Süßes"
    return None


def yes_no(value):
    if value is None:
        return None
    v = value.lower()
    if v in ("yes", "only", "designated"):
        return True
    if v == "no":
        return False
    return None


# ---------------------------------------------------------------------------
# Öffnungszeiten: OSM-Format → HalalBite-Format
# Unterstützt die häufigen Fälle ("Mo-Fr 11:00-22:00; Sa,Su 12:00-23:00", "24/7").
# Kompliziertere Angaben (Feiertage, Wochen, Monate) werden übersprungen.
# ---------------------------------------------------------------------------

TIME_RANGE = re.compile(r"^(\d{1,2}):(\d{2})-(\d{1,2}):(\d{2})\+?$")


def parse_days(spec):
    days = set()
    for part in spec.split(","):
        part = part.strip()
        if "-" in part:
            a, b = part.split("-", 1)
            if a not in DAYS or b not in DAYS:
                return None
            i, j = DAYS.index(a), DAYS.index(b)
            rng = range(i, j + 1) if i <= j else list(range(i, 7)) + list(range(0, j + 1))
            days.update(rng)
        elif part in DAYS:
            days.add(DAYS.index(part))
        elif part == "PH":
            continue  # Feiertage ignorieren
        else:
            return None
    return days


def parse_times(spec):
    if spec.strip().lower() in ("off", "closed"):
        return []
    slots = []
    for part in spec.split(","):
        m = TIME_RANGE.match(part.strip())
        if not m:
            return None
        h1, m1, h2, m2 = map(int, m.groups())
        if h1 > 24 or h2 > 48 or m1 > 59 or m2 > 59:
            return None
        h2 = h2 % 24 if h2 > 24 else h2  # "02:00+" / "26:00" → 02:00
        slots.append(f"{h1:02d}:{m1:02d}-{h2:02d}:{m2:02d}")
    return slots


def parse_opening_hours(raw):
    if not raw:
        return None
    raw = raw.strip()
    if raw == "24/7":
        return {"hours": {d: "00:00-24:00" for d in DAY_NAMES}}

    week = {}
    for rule in [r.strip() for r in raw.split(";") if r.strip()]:
        tokens = rule.split(" ", 1)
        if len(tokens) == 2 and re.match(r"^[A-Za-z,\-]+$", tokens[0]):
            days, times = parse_days(tokens[0]), parse_times(tokens[1])
        elif TIME_RANGE.match(rule.split(",")[0].strip()):
            days, times = set(range(7)), parse_times(rule)  # Uhrzeit ohne Tage = täglich
        else:
            return None
        if days is None or times is None:
            return None  # unbekanntes Format → lieber gar keine Zeiten als falsche
        for d in days:
            week[d] = times  # spätere Regeln überschreiben frühere (wie in OSM)

    if not week:
        return None
    return {"hours": {DAY_NAMES[d]: ", ".join(week.get(d, [])) for d in range(7)}}


# ---------------------------------------------------------------------------
# Import
# ---------------------------------------------------------------------------

def fetch_osm():
    print("⏳ Lade Halal-Orte aus OpenStreetMap (kann 1–3 Minuten dauern) …")
    body = urllib.parse.urlencode({"data": OVERPASS_QUERY}).encode()
    last_error = ""
    for attempt in range(1):
        for server in OVERPASS_URLS:
            status, data = http("POST", server, timeout=200,
                                headers={"Content-Type": "application/x-www-form-urlencoded"}, body=body)
            if status == 200 and isinstance(data, dict) and "elements" in data:
                OSM_CACHE.write_text(json.dumps(data["elements"]))
                return data["elements"]
            last_error = f"{server}: {status} {str(data)[:150]}"
            print(f"   ⚠️ {server.split('/')[2]} nicht erreichbar – versuche nächsten Server …")
    if OSM_CACHE.exists():
        age_days = (time.time() - OSM_CACHE.stat().st_mtime) / 86400
        print(f"   ↩️  Server überlastet – nutze den letzten Download (vor {age_days:.0f} Tagen).")
        return json.loads(OSM_CACHE.read_text())
    sys.exit(f"❌ Kein Overpass-Server erreichbar (überlastet). Bitte in 10–30 Minuten erneut versuchen.\n"
             f"   Letzter Fehler: {last_error[:200]}")


def to_row(element):
    tags = element.get("tags", {})
    lat = element.get("lat") or element.get("center", {}).get("lat")
    lon = element.get("lon") or element.get("center", {}).get("lon")
    name = tags.get("name", "").strip()
    if not name or lat is None or lon is None:
        return None

    street = " ".join(x for x in [tags.get("addr:street"), tags.get("addr:housenumber")] if x).strip()
    website = tags.get("website") or tags.get("contact:website") or tags.get("url")
    instagram = tags.get("contact:instagram") or tags.get("instagram")
    raw_hours = tags.get("opening_hours")

    return {
        "osm_key": f"{element['type']}/{element['id']}",
        "name": name[:120],
        "street": street or None,
        "postal_code": tags.get("addr:postcode"),
        "city": tags.get("addr:city"),
        "latitude": lat,
        "longitude": lon,
        "phone": tags.get("phone") or tags.get("contact:phone"),
        "website": website,
        "instagram": instagram,
        "opening_hours": parse_opening_hours(raw_hours),
        "opening_hours_raw": raw_hours,
        "category": category_for(tags),
        "halal": tags.get("diet:halal"),
        "has_prayer_room": yes_no(tags.get("prayer_room")),
        "vegetarian_options": yes_no(tags.get("diet:vegetarian")),
        "delivery": yes_no(tags.get("delivery")),
        "_tags": tags,  # nur intern (für die Bildsuche), wird nicht hochgeladen
    }


def fill_missing_addresses(rows, limit=None):
    """Ergänzt Straße, PLZ und Stadt per Reverse-Geocoding (nur wo die Stadt fehlt).
    Ergebnisse werden in tools/.geocode_cache.json gespeichert → spätere Importe sind schnell."""
    cache = json.loads(GEOCODE_CACHE.read_text()) if GEOCODE_CACHE.exists() else {}
    todo = [r for r in rows if not r["city"]]
    if limit is not None:
        todo = todo[:limit]
    uncached = [r for r in todo if r["osm_key"] not in cache]
    if uncached:
        minutes = max(1, round(len(uncached) * 1.1 / 60))
        print(f"📍 Ergänze Adressen für {len(uncached)} Orte ohne Stadt (ca. {minutes} Min., einmalig) …")

    for i, r in enumerate(todo):
        key = r["osm_key"]
        if key not in cache:
            query = urllib.parse.urlencode({
                "lat": r["latitude"], "lon": r["longitude"],
                "format": "jsonv2", "zoom": 18, "addressdetails": 1, "accept-language": "de",
            })
            status, data = http("GET", f"{NOMINATIM_URL}?{query}", timeout=30)
            cache[key] = data.get("address", {}) if status == 200 and isinstance(data, dict) else {}
            time.sleep(1.1)  # Nominatim-Regel: höchstens 1 Anfrage pro Sekunde
            if (i + 1) % 50 == 0:
                GEOCODE_CACHE.write_text(json.dumps(cache))
                print(f"   {i + 1}/{len(todo)}")

        a = cache[key]
        city = a.get("city") or a.get("town") or a.get("village") or a.get("municipality")
        if city:
            r["city"] = city
        if not r["postal_code"] and a.get("postcode"):
            r["postal_code"] = a["postcode"]
        if not r["street"] and a.get("road"):
            r["street"] = " ".join(x for x in [a.get("road"), a.get("house_number")] if x)

    GEOCODE_CACHE.write_text(json.dumps(cache))
    return sum(1 for r in todo if r["city"])


def is_duplicate(row, verified):
    """Gleicher Ort wie ein geprüfter? (nah beieinander und ähnlicher Name)"""
    name = normalize(row["name"])
    for v in verified:
        if v["latitude"] is None or v["longitude"] is None:
            continue
        d = distance_m(row["latitude"], row["longitude"], v["latitude"], v["longitude"])
        if d > 150:
            continue
        other = normalize(v["name"])
        similar = difflib.SequenceMatcher(None, name, other).ratio() >= 0.6 or name in other or other in name
        if similar or d < 15:
            return True
    return False



# ---------------------------------------------------------------------------
# Bilder – nur frei lizenzierte Quellen, immer mit Bildnachweis
#   • Wikimedia Commons (in OSM verlinkte Fotos)          → sofort freigegeben
#   • Wikidata (Logos von Ketten, Fotos von Orten)        → sofort freigegeben
#   • Mapillary (Straßenfotos, CC BY-SA 4.0, braucht Token) → erst nach eurer Freigabe
# ---------------------------------------------------------------------------

def strip_html(text):
    return re.sub(r"<[^>]+>", "", text or "").strip()


def commons_file(value):
    """'File:X.jpg', 'https://commons.wikimedia.org/wiki/File:X.jpg' → 'X.jpg'"""
    if not value:
        return None
    value = urllib.parse.unquote(value)
    m = re.search(r"(?:File|Datei):(.+)$", value)
    return m.group(1).strip() if m else None


def commons_image(filename, width):
    """Direkte Bild-URL + Bildnachweis (Urheber, Lizenz) von Wikimedia Commons"""
    q = urllib.parse.urlencode({
        "action": "query", "format": "json", "prop": "imageinfo",
        "iiprop": "url|extmetadata", "iiurlwidth": width, "titles": f"File:{filename}",
    })
    status, data = http("GET", f"https://commons.wikimedia.org/w/api.php?{q}", timeout=30)
    if status != 200 or not isinstance(data, dict):
        return None
    for page in data.get("query", {}).get("pages", {}).values():
        info = (page.get("imageinfo") or [None])[0]
        if not info:
            continue
        meta = info.get("extmetadata", {})
        artist = strip_html(meta.get("Artist", {}).get("value")) or "unbekannt"
        license_name = strip_html(meta.get("LicenseShortName", {}).get("value")) or "freie Lizenz"
        return info.get("thumburl") or info.get("url"), f"{artist[:80]}, {license_name} – Wikimedia Commons"
    return None


def wikidata_file(qid, prop):
    """Dateiname einer Wikidata-Eigenschaft (P154 = Logo, P18 = Bild)"""
    if not qid or not re.match(r"^Q\d+$", qid):
        return None
    status, data = http("GET", f"https://www.wikidata.org/wiki/Special:EntityData/{qid}.json", timeout=30)
    if status != 200 or not isinstance(data, dict):
        return None
    claims = data.get("entities", {}).get(qid, {}).get("claims", {}).get(prop, [])
    for claim in claims:
        value = claim.get("mainsnak", {}).get("datavalue", {}).get("value")
        if isinstance(value, str):
            return value
    return None


def bearing(lat1, lon1, lat2, lon2):
    y = math.sin(math.radians(lon2 - lon1)) * math.cos(math.radians(lat2))
    x = math.cos(math.radians(lat1)) * math.sin(math.radians(lat2)) - \
        math.sin(math.radians(lat1)) * math.cos(math.radians(lat2)) * math.cos(math.radians(lon2 - lon1))
    return (math.degrees(math.atan2(y, x)) + 360) % 360


def mapillary_candidate(row, token):
    """Nächstes Straßenfoto in ca. 40 m, dessen Kamera Richtung Laden zeigt"""
    lat, lon, d = row["latitude"], row["longitude"], 0.0004
    q = urllib.parse.urlencode({
        "access_token": token,
        "fields": "id,thumb_1024_url,computed_geometry,compass_angle,creator",
        "bbox": f"{lon - d},{lat - d},{lon + d},{lat + d}", "limit": 30,
    })
    status, data = http("GET", f"https://graph.mapillary.com/images?{q}", timeout=30)
    if status != 200 or not isinstance(data, dict):
        return None
    best = None
    for img in data.get("data", []):
        coords = img.get("computed_geometry", {}).get("coordinates")
        if not coords or img.get("compass_angle") is None or not img.get("thumb_1024_url"):
            continue
        ilon, ilat = coords
        dist = distance_m(ilat, ilon, lat, lon)
        if dist < 5 or dist > 40:
            continue
        diff = abs((bearing(ilat, ilon, lat, lon) - img["compass_angle"] + 180) % 360 - 180)
        if diff > 40:
            continue  # Kamera schaut nicht zum Laden
        score = dist + diff * 0.5
        if best is None or score < best[0]:
            best = (score, img)
    if not best:
        return None
    img = best[1]
    creator = (img.get("creator") or {}).get("username") or "Mapillary-Mitwirkende"
    return img["id"], img["thumb_1024_url"], f"{creator} / Mapillary, CC BY-SA 4.0"


def upload_mapillary(image_id, thumb_url, url, headers):
    """Mapillary-Links laufen ab → Bild herunterladen und in Supabase Storage ablegen"""
    tmp = Path("/tmp") / f"mly_{image_id}.jpg"
    if subprocess.run(["curl", "-sS", "-L", "--max-time", "60", "-o", str(tmp), thumb_url]).returncode != 0:
        return None
    path = f"osm/mapillary_{image_id}.jpg"
    cmd = ["curl", "-sS", "-o", "/dev/null", "-w", "%{http_code}", "-X", "POST",
           f"{url}/storage/v1/object/restaurant-cover/{path}",
           "-H", f"apikey: {headers['apikey']}", "-H", f"Authorization: {headers['Authorization']}",
           "-H", "Content-Type: image/jpeg", "--data-binary", f"@{tmp}"]
    code = subprocess.run(cmd, capture_output=True).stdout.decode()
    tmp.unlink(missing_ok=True)
    return f"{url}/storage/v1/object/public/restaurant-cover/{path}" if code in ("200", "201") else None


def find_media(rows, mapillary_token=None, url=None, headers=None, limit=None):
    """Sucht Logos und Fotos. Ergebnisse werden gecacht (tools/.media_cache.json)."""
    cache = json.loads(MEDIA_CACHE.read_text()) if MEDIA_CACHE.exists() else {}
    stats = {"logo": 0, "commons": 0, "mapillary": 0}
    todo = rows if limit is None else rows[:limit]
    print(f"🖼️  Suche frei lizenzierte Bilder für {len(todo)} Orte …")

    for i, r in enumerate(todo):
        tags, key = r["_tags"], r["osm_key"]
        entry = cache.get(key)
        if entry is None:
            entry = {}
            # 1) Logo der Kette (Wikidata P154)
            logo_file = wikidata_file(tags.get("brand:wikidata"), "P154")
            if logo_file and (logo := commons_image(logo_file, 400)):
                entry["logo_url"], entry["logo_credit"] = logo
            # 2) Foto des Ortes: OSM image/wikimedia_commons, sonst Wikidata P18
            photo_file = commons_file(tags.get("wikimedia_commons")) or commons_file(tags.get("image")) \
                or wikidata_file(tags.get("wikidata"), "P18")
            if photo_file and (photo := commons_image(photo_file, 1200)):
                entry["image_url"], entry["image_credit"] = photo
                entry["image_source"], entry["image_status"] = "commons", "approved"
            cache[key] = entry
            if (i + 1) % 100 == 0:
                MEDIA_CACHE.write_text(json.dumps(cache))
                print(f"   {i + 1}/{len(todo)}")

        # 3) Mapillary-Straßenfoto (nur mit Token und nur ohne anderes Foto)
        if mapillary_token and not entry.get("image_url") and not entry.get("mapillary_checked"):
            candidate = mapillary_candidate(r, mapillary_token)
            entry["mapillary_checked"] = True
            if candidate and url and headers:  # Hochladen nur im echten Import
                image_id, thumb, credit = candidate
                if public := upload_mapillary(image_id, thumb, url, headers):
                    entry["image_url"], entry["image_credit"] = public, credit
                    entry["image_source"], entry["image_status"] = "mapillary", "pending"
            elif candidate:
                entry["mapillary_preview"] = True  # Dry-Run: nur zählen
                entry["mapillary_checked"] = False
            cache[key] = entry

        for field in MEDIA_FIELDS:
            r[field] = entry.get(field)
        stats["logo"] += bool(entry.get("logo_url"))
        stats["commons"] += entry.get("image_source") == "commons"
        stats["mapillary"] += entry.get("image_source") == "mapillary" or bool(entry.get("mapillary_preview"))

    MEDIA_CACHE.write_text(json.dumps(cache))
    return stats


def keep_media_decisions(rows, url, headers):
    """Bereits von euch freigegebene/abgelehnte Bilder nicht überschreiben"""
    existing, offset = {}, 0
    while True:
        status, page = http("GET", f"{url}/rest/v1/osm_places?select=osm_key,{','.join(MEDIA_FIELDS)}"
                                   f"&order=id.asc&limit=1000&offset={offset}", headers=headers)
        if status != 200 or not isinstance(page, list):
            break
        existing.update({p["osm_key"]: p for p in page})
        if len(page) < 1000:
            break
        offset += 1000
    for r in rows:
        old = existing.get(r["osm_key"])
        if old and old.get("image_status") in ("approved", "rejected", "pending") and old.get("image_url"):
            for field in ["image_url", "image_credit", "image_source", "image_status"]:
                r[field] = old.get(field)


def login(url, key):
    # Automatischer Betrieb (GitHub Actions): Zugangsdaten aus Umgebungsvariablen
    email = os.environ.get("HALALBITE_ADMIN_EMAIL")
    password = os.environ.get("HALALBITE_ADMIN_PASSWORD")
    if not (email and password):
        print("\n🔐 Admin-Login (derselbe wie in der App)")
        email = input("E-Mail: ").strip()
        password = getpass.getpass("Passwort: ")
    status, data = http("POST", f"{url}/auth/v1/token?grant_type=password",
                        headers={"apikey": key, "Content-Type": "application/json"},
                        body={"email": email, "password": password})
    if status != 200:
        sys.exit(f"❌ Login fehlgeschlagen ({status}). E-Mail/Passwort prüfen.")
    return data["access_token"]


def main():
    parser = argparse.ArgumentParser(description="Halal-Orte aus OpenStreetMap importieren")
    parser.add_argument("--dry-run", action="store_true", help="Nur anzeigen, nichts hochladen")
    parser.add_argument("--no-geocode", action="store_true", help="Fehlende Adressen nicht ergänzen")
    parser.add_argument("--no-images", action="store_true", help="Keine Logos/Fotos suchen")
    parser.add_argument("--mapillary-token", default=os.environ.get("MAPILLARY_TOKEN"),
                        help="Mapillary-Zugangsschlüssel (oder Umgebungsvariable MAPILLARY_TOKEN)")
    args = parser.parse_args()

    url, key = supabase_config()
    anon_headers = {"apikey": key, "Authorization": f"Bearer {key}"}

    # Login zuerst, damit der Import danach ohne Rückfragen durchläuft
    headers = None
    if not args.dry_run:
        token = login(url, key)
        headers = {"apikey": key, "Authorization": f"Bearer {token}", "Content-Type": "application/json"}

    elements = [e for e in fetch_osm()
                if e.get("tags", {}).get("amenity") in AMENITIES and not is_excluded_chain(e["tags"])]
    rows = [r for r in (to_row(e) for e in elements) if r]

    # Doppelte OSM-Einträge (gleicher Name an gleicher Stelle) zusammenfassen
    unique = {}
    for r in rows:
        unique.setdefault((normalize(r["name"]), round(r["latitude"], 4), round(r["longitude"], 4)), r)
    rows = list(unique.values())

    status, verified = http("GET", f"{url}/rest/v1/restaurants?select=name,latitude,longitude", headers=anon_headers)
    verified = verified if status == 200 else []
    new_rows = [r for r in rows if not is_duplicate(r, verified)]

    if not args.no_geocode:
        # Im Dry-Run nur 5 Beispiele, damit die Vorschau schnell bleibt
        filled = fill_missing_addresses(new_rows, limit=5 if args.dry_run else None)
        print(f"   ✅ Adresse ergänzt für {filled} Orte")

    media = {"logo": 0, "commons": 0, "mapillary": 0}
    if not args.no_images:
        media = find_media(new_rows, args.mapillary_token, url if headers else None, headers,
                           limit=40 if args.dry_run else None)
    else:
        for r in new_rows:
            for field in MEDIA_FIELDS:
                r[field] = None

    with_hours = sum(1 for r in new_rows if r["opening_hours"])
    with_cat = sum(1 for r in new_rows if r["category"])
    cities = {}
    for r in new_rows:
        cities[r["city"] or "(ohne Stadt)"] = cities.get(r["city"] or "(ohne Stadt)", 0) + 1
    print(f"\n✅ {len(elements)} Treffer in OSM → {len(rows)} eindeutige Orte")
    print(f"   {len(rows) - len(new_rows)} davon gibt es schon bei HalalBite (werden übersprungen)")
    print(f"   {len(new_rows)} zum Import – {with_hours} mit Öffnungszeiten, {with_cat} mit Kategorie")
    print(f"   Bilder: {media['logo']} Logos, {media['commons']} Fotos (Wikimedia), "
          f"{media['mapillary']} Straßenfotos (Mapillary{'' if args.mapillary_token else ' – ohne Token übersprungen'})")
    print("   Top-Städte:", ", ".join(f"{c} ({n})" for c, n in sorted(cities.items(), key=lambda x: -x[1])[:8]))

    if args.dry_run:
        print("\n🔎 Beispiele mit Bild:")
        for r in [r for r in new_rows if r.get("logo_url") or r.get("image_url")][:5]:
            print(f"   • {r['name']} – Logo: {'ja' if r.get('logo_url') else 'nein'}, Foto: {r.get('image_source') or 'nein'}"
                  f" | {r.get('logo_credit') or r.get('image_credit')}")
        print("\nDry-Run – nichts hochgeladen.")
        return

    keep_media_decisions(new_rows, url, headers)
    run_started = datetime.now(timezone.utc).isoformat()

    print("\n⬆️  Lade hoch …")
    for i in range(0, len(new_rows), 200):
        batch = [{**{k: v for k, v in r.items() if not k.startswith("_")}, "last_seen_at": run_started}
                 for r in new_rows[i:i + 200]]
        status, body = http("POST", f"{url}/rest/v1/osm_places?on_conflict=osm_key",
                            headers={**headers, "Prefer": "resolution=merge-duplicates,return=minimal"},
                            body=batch)
        if status not in (200, 201, 204):
            sys.exit(f"❌ Upload-Fehler {status}: {str(body)[:400]}")
        print(f"   {min(i + 200, len(new_rows))}/{len(new_rows)}")

    # Aus OSM verschwundene Orte entfernen (übernommene und ausgeblendete bleiben)
    stale = urllib.parse.quote(run_started)
    status, _ = http("DELETE",
                     f"{url}/rest/v1/osm_places?last_seen_at=lt.{stale}&promoted_restaurant_id=is.null&hidden=eq.false",
                     headers={**headers, "Prefer": "return=minimal"})
    print("🧹 Veraltete Einträge entfernt." if status in (200, 204) else f"⚠️ Aufräumen fehlgeschlagen ({status}).")
    print(f"\n🎉 Fertig! {len(new_rows)} Orte sind jetzt in der App sichtbar (als „Laut Community halal“).")
    if media["mapillary"]:
        print(f"   📸 {media['mapillary']} Straßenfotos warten in der App auf deine Freigabe (Prüfen → Straße).")


if __name__ == "__main__":
    main()
