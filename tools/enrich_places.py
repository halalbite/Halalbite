#!/usr/bin/env python3
"""
HalalBite – fehlende Angaben bei vorhandenen Orten nachtragen

Für alle Orte (geprüft und aus OpenStreetMap), bei denen Öffnungszeiten, Telefon oder Website
fehlen, sucht das Skript in zwei erlaubten Quellen:
  1. Website des Restaurants: maschinenlesbare Angaben für Suchmaschinen (schema.org –
     „openingHours“, „openingHoursSpecification“, „telephone“)
  2. OpenStreetMap: derselbe Laden in der Nähe (≤ 60 m, ähnlicher Name), der dort ohne
     Halal-Kennzeichnung, aber mit Zeiten/Telefon/Website eingetragen ist
     (nur für geprüfte Orte – OSM-Orte stammen ja schon aus OpenStreetMap)

Es werden NUR leere Felder gefüllt – vorhandene Angaben werden nie überschrieben.
Läuft auch automatisch nach dem OSM-Import (GitHub Actions).

Benutzung (Terminal):
    cd ~/Downloads/HalalBite2
    python3 tools/enrich_places.py --dry-run    # nur anzeigen, was gefunden wird
    python3 tools/enrich_places.py              # nachtragen (fragt nach Admin-Login)
"""

import argparse
import json
import math
import re
import sys
import time
import urllib.parse
from collections import Counter
from concurrent.futures import ThreadPoolExecutor

from find_instagram import fetch_page, matches_name, normalized_site
from import_osm import (DAY_NAMES, OVERPASS_URLS, fetch_all_rows, http, login, normalize,
                        parse_opening_hours, supabase_config)
from website_logos import DIRECTORY_DOMAINS

FIELDS = ("opening_hours", "phone", "website")
SCHEMA_DAYS = {name.lower(): name for name in DAY_NAMES}


def is_empty(value):
    if value in (None, "", {}):
        return True
    if isinstance(value, dict):
        return not any((value.get("hours") or {}).values())
    return isinstance(value, str) and not value.strip()


# ---------------------------------------------------------------------------
# Quelle 1: schema.org-Angaben auf der Website
# ---------------------------------------------------------------------------

def json_ld_objects(page):
    """Alle JSON-LD-Objekte einer Seite (auch verschachtelt in @graph oder Listen)"""
    found = []

    def walk(node):
        if isinstance(node, list):
            for item in node:
                walk(item)
        elif isinstance(node, dict):
            found.append(node)
            for value in node.values():
                if isinstance(value, (dict, list)):
                    walk(value)

    for block in re.findall(r'<script[^>]+application/ld\+json[^>]*>(.*?)</script>', page, re.I | re.S):
        try:
            walk(json.loads(block.strip()))
        except json.JSONDecodeError:
            continue
    return found


def to_24h(text):
    """"17:00", "17:00:00", "5:00 PM", "05:00PM" → "17:00" (None, wenn unlesbar)"""
    m = re.fullmatch(r"\s*(\d{1,2}):(\d{2})(?::\d{2})?\s*([AaPp][Mm])?\s*", str(text or ""))
    if not m:
        return None
    hour, minute, ampm = int(m.group(1)), int(m.group(2)), (m.group(3) or "").lower()
    if ampm == "pm" and hour < 12:
        hour += 12
    elif ampm == "am" and hour == 12:
        hour = 0
    return f"{hour:02d}:{minute:02d}" if hour <= 24 and minute < 60 else None


def hours_from_specification(specs):
    """openingHoursSpecification → {"hours": {"Monday": "11:00-22:00", …}}"""
    week = {}
    for spec in specs if isinstance(specs, list) else [specs]:
        if not isinstance(spec, dict):
            continue
        opens, closes = to_24h(spec.get("opens")), to_24h(spec.get("closes"))
        if not opens or not closes:
            continue
        days = spec.get("dayOfWeek") or []
        for day in days if isinstance(days, list) else [days]:
            name = SCHEMA_DAYS.get(str(day).rsplit("/", 1)[-1].lower())
            if name:
                slot = f"{opens}-{closes}"
                week.setdefault(name, []).append(slot)
    if not week:
        return None
    return {"hours": {day: ", ".join(sorted(set(week.get(day, [])))) for day in DAY_NAMES}}


# Telefonnummer im Text: "Tel.: 040 123456", "Telefon 0176/1234567", "☎ +49 30 123 45 67"
PHONE_TEXT = re.compile(r"(?:Tel(?:efon)?|Fon|Phone|☎|📞)\.?\s*[:.]?\s*(\+?\d[\d\s/().\-]{5,22}\d)", re.I)
SUBPAGES = ("impressum", "kontakt", "contact", "imprint")


def phone_in(page):
    """Telefonnummer aus tel:-Links oder dem Text (Fax-Nummern werden übersprungen)"""
    for link in re.findall(r'href=["\']tel:([^"\']+)["\']', page, re.I):
        number = urllib.parse.unquote(link).strip()
        if len(re.sub(r"\D", "", number)) >= 6:
            return number
    text = re.sub(r"<[^>]+>", " ", page)
    for match in PHONE_TEXT.finditer(text):
        before = text[max(0, match.start() - 12):match.start()].lower()
        if "fax" not in before and len(re.sub(r"\D", "", match.group(1))) >= 6:
            return re.sub(r"\s+", " ", match.group(1)).strip()
    return None


# Straßenwörter, die allein nichts über die Filiale aussagen
GENERIC_STREET_WORDS = {"strae", "strasse", "str", "platz", "allee", "weg", "gasse", "damm", "ring", "markt",
                        "hauptstrae", "bahnhofstrae", "bahnhofplatz", "deutschland"}


def address_keys(row):
    """PLZ, Straßenwörter und Stadt eines Ortes (normalisiert) für den Abgleich mit Filialangaben"""
    text = row.get("address") or " ".join(x for x in (row.get("street"), row.get("postal_code"), row.get("city")) if x)
    postcode = re.search(r"\b\d{5}\b", text or "")
    street = row.get("street") or (text or "").split(",")[0]
    words = [w for w in normalize(re.sub(r"\d.*$", "", street)).split() if len(w) >= 5 and w not in GENERIC_STREET_WORDS]
    parts = [p for p in (text or "").split(",")[1:] if normalize(p) not in ("", "deutschland", "germany")]
    city = normalize(row.get("city") or (parts[-1] if parts else ""))
    city = re.sub(r"^\d{5} ", "", city)
    return postcode.group(0) if postcode else None, words, city


def mentions_address(text, keys):
    """Passt ein Text (Filialadresse, Link, Seite) zu genau diesem Ort?
    PLZ reicht; sonst ein markantes Straßenwort – plus Stadt, falls bekannt."""
    postcode, words, city = keys
    flat = normalize(text)
    if postcode and re.search(rf"\b{postcode}\b", flat):
        return True
    if words and any(w in flat for w in words):
        return not city or city in flat
    return False


def extract(objects, row, keys, need_address):
    """Öffnungszeiten/Telefon aus schema.org-Objekten. Hat ein Objekt eine Adresse, muss sie
    zum Ort passen; Objekte ohne Adresse zählen nur, wenn need_address False ist."""
    found = {}
    for obj in objects:
        address = obj.get("address")
        if address:
            if not mentions_address(json.dumps(address, ensure_ascii=False), keys):
                continue  # andere Filiale
        elif need_address:
            continue
        if is_empty(row.get("opening_hours")) and "opening_hours" not in found:
            hours = None
            if obj.get("openingHoursSpecification"):
                hours = hours_from_specification(obj["openingHoursSpecification"])
            elif obj.get("openingHours"):
                spec = obj["openingHours"]
                # schema.org nutzt dieselbe Schreibweise wie OSM ("Mo-Fr 11:00-22:00")
                hours = parse_opening_hours("; ".join(spec) if isinstance(spec, list) else str(spec))
            if hours:
                found["opening_hours"] = hours
        phone = obj.get("telephone")
        if is_empty(row.get("phone")) and "phone" not in found and isinstance(phone, str) \
                and len(re.sub(r"\D", "", phone)) >= 6:
            found["phone"] = phone.strip()
    return found


def branch_links(page, site, keys):
    """Links auf der Startseite, die zur Filiale führen (Linktext oder Adresse nennt PLZ/Straße)"""
    links = []
    for href, label in re.findall(r'<a\b[^>]*href=["\']([^"\'#]+)["\'][^>]*>(.*?)</a>', page, re.I | re.S):
        if mentions_address(urllib.parse.unquote(href) + " " + re.sub(r"<[^>]+>", " ", label), keys):
            link = urllib.parse.urljoin(site, href)
            if link not in links and urllib.parse.urlparse(link).netloc == urllib.parse.urlparse(site).netloc:
                links.append(link)
    return links[:3]


def missing(row, found):
    return [f for f in ("opening_hours", "phone") if is_empty(row.get(f)) and f not in found]


def from_website(row, is_chain=False):
    """Sucht Öffnungszeiten/Telefon auf der Website → {feld: wert}.
    Bei Ketten (dieselbe Website bei mehreren Orten) nur Angaben, die nachweislich zur
    Adresse dieser Filiale gehören – sonst bekämen alle Filialen die Zeiten einer einzigen."""
    site = normalized_site(row["website"])
    host = urllib.parse.urlparse(site).netloc.lower()
    if any(domain in host for domain in DIRECTORY_DOMAINS):
        return {}
    page = fetch_page(site)
    if not page:
        return {}
    keys = address_keys(row)
    found = extract(json_ld_objects(page), row, keys, need_address=is_chain)

    if is_chain:
        # Filialseite suchen und nur nutzen, wenn dort die Adresse dieser Filiale steht
        for link in branch_links(page, site, keys) if missing(row, found) else []:
            subpage = fetch_page(link)
            if not subpage or not mentions_address(re.sub(r"<[^>]+>", " ", subpage), keys):
                continue
            for field, value in extract(json_ld_objects(subpage), row, keys, need_address=False).items():
                found.setdefault(field, value)
            if "phone" in missing(row, found) and (number := phone_in(subpage)):
                found["phone"] = number
            if not missing(row, found):
                break
        return found  # Impressum-Nummer einer Kette ist meist die der Zentrale → nicht übernehmen

    # Telefon fehlt noch → Startseite, dann Impressum/Kontakt (in Deutschland Pflichtangabe)
    if "phone" in missing(row, found):
        number = phone_in(page)
        if not number:
            for sub in SUBPAGES:
                link = re.search(rf'href="([^"]*{sub}[^"]*)"', page, re.I)
                target = urllib.parse.urljoin(site, link.group(1)) if link else urllib.parse.urljoin(site + "/", sub)
                subpage = fetch_page(target)
                if subpage and (number := phone_in(subpage)):
                    break
        if number:
            found["phone"] = number
    return found


# ---------------------------------------------------------------------------
# Quelle 2: derselbe Laden in OpenStreetMap (für geprüfte Orte)
# ---------------------------------------------------------------------------

def metres(lat1, lon1, lat2, lon2):
    dy = (lat1 - lat2) * 111320
    dx = (lon1 - lon2) * 111320 * math.cos(math.radians(lat1))
    return math.hypot(dx, dy)


def osm_neighbours(rows):
    """Gastronomie in OSM im Umkreis von 60 m um jeden Ort (eine Abfrage je 40 Orte)"""
    elements = []
    for i in range(0, len(rows), 40):
        around = "\n".join(f'  nwr(around:60,{r["latitude"]},{r["longitude"]})["amenity"~"restaurant|fast_food|cafe|ice_cream"]["name"];'
                           for r in rows[i:i + 40])
        query = f"[out:json][timeout:120];\n(\n{around}\n);\nout tags center;"
        body = urllib.parse.urlencode({"data": query}).encode()
        for server in OVERPASS_URLS:
            status, data = http("POST", server, timeout=150,
                                headers={"Content-Type": "application/x-www-form-urlencoded"}, body=body)
            if status == 200 and isinstance(data, dict) and "elements" in data and not data.get("remark"):
                elements += data["elements"]
                break
        else:
            print("   ⚠️ OpenStreetMap gerade nicht erreichbar – dieser Teil wird beim nächsten Lauf nachgeholt.")
            return elements
        time.sleep(2)
    return elements


def from_osm(row, elements):
    best = None
    for element in elements:
        tags = element.get("tags", {})
        lat = element.get("lat") or element.get("center", {}).get("lat")
        lon = element.get("lon") or element.get("center", {}).get("lon")
        if lat is None or not matches_name(row["name"], normalize(tags.get("name", "")).replace(" ", "")):
            continue
        distance = metres(row["latitude"], row["longitude"], lat, lon)
        if distance <= 60 and (best is None or distance < best[0]):
            best = (distance, tags)
    if not best:
        return {}
    tags, found = best[1], {}
    hours = parse_opening_hours(tags.get("opening_hours"))
    if is_empty(row.get("opening_hours")) and hours:
        found["opening_hours"] = hours
    phone = tags.get("phone") or tags.get("contact:phone")
    if is_empty(row.get("phone")) and phone:
        found["phone"] = phone.split(";")[0].strip()
    website = tags.get("website") or tags.get("contact:website")
    if is_empty(row.get("website")) and website:
        found["website"] = website
    return found


# ---------------------------------------------------------------------------

def run(url, headers, dry_run=False, limit=None):
    tables = {
        "restaurants": "",
        "osm_places": "&hidden=is.false&promoted_restaurant_id=is.null",
    }
    address_columns = {"restaurants": "address", "osm_places": "street,postal_code,city"}
    all_rows = []
    for table, extra in tables.items():
        columns = f"id,name,latitude,longitude,{address_columns[table]},{','.join(FIELDS)}"
        status, data = fetch_all_rows(url, f"{table}?select={columns}{extra}", headers)
        if status != 200:
            print(f"   ⚠️ {table} konnte nicht geladen werden ({status}) – übersprungen")
            continue
        all_rows += [{**r, "table": table} for r in data]
    rows = [r for r in all_rows if any(is_empty(r.get(f)) for f in FIELDS)]

    # Ketten: dieselbe Website bei mehreren Orten (z. B. back-lava.de für drei Filialen)
    def host_of(row):
        return urllib.parse.urlparse(normalized_site(row["website"])).netloc.lower().removeprefix("www.")
    host_count = Counter(host_of(r) for r in all_rows if not is_empty(r.get("website")))
    chain_hosts = {h for h, n in host_count.items() if n > 1}
    if limit:
        rows = rows[:limit]
    print(f"🔎 {len(rows)} Orte mit fehlenden Angaben (Öffnungszeiten, Telefon oder Website)")

    updates = {}
    with_site = [r for r in rows if not is_empty(r.get("website"))
                 and (is_empty(r.get("opening_hours")) or is_empty(r.get("phone")))]
    print(f"🌐 Prüfe {len(with_site)} Websites ({sum(host_of(r) in chain_hosts for r in with_site)} davon Ketten-Filialen) …")
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = pool.map(lambda r: from_website(r, is_chain=host_of(r) in chain_hosts), with_site)
        for row, found in zip(with_site, results):
            if found:
                updates[(row["table"], row["id"])] = (row, {**found, "_source": "Website"})

    verified = [r for r in rows if r["table"] == "restaurants" and r.get("latitude") is not None
                and any(is_empty(r.get(f)) and f not in updates.get((r["table"], r["id"]), (None, {}))[1] for f in FIELDS)]
    if verified:
        print(f"🗺  Suche {len(verified)} geprüfte Orte in OpenStreetMap …")
        elements = osm_neighbours(verified)
        for row in verified:
            found = from_osm(row, elements)
            key = (row["table"], row["id"])
            if found:
                old = updates.get(key, (row, {}))[1]
                updates[key] = (row, {**found, **old, "_source": "Website + OSM" if old else "OpenStreetMap"})

    stats = Counter()
    write = {**headers, "Content-Type": "application/json", "Prefer": "return=minimal"}
    for (table, place_id), (row, found) in updates.items():
        source = found.pop("_source")
        labels = {"opening_hours": "Öffnungszeiten", "phone": "Telefon", "website": "Website"}
        print(f"   ✅ {row['name']}: {', '.join(labels[f] for f in found)} ({source})")
        for field in found:
            stats[labels[field]] += 1
        if dry_run:
            continue
        # Nur leere Felder schreiben – Bedingung direkt in der Abfrage, falls sich etwas geändert hat
        for field, value in found.items():
            # (Öffnungszeiten können auch als leeres {"hours": {}} gespeichert sein – das prüft is_empty vorher)
            condition = "" if field == "opening_hours" else f"&or=({field}.is.null,{field}.eq.)"
            status, body = http("PATCH", f"{url}/rest/v1/{table}?id=eq.{place_id}{condition}", write, {field: value})
            if status not in (200, 204):
                print(f"      ⚠️ {field}: {status} {str(body)[:120]}")
    summary = " · ".join(f"{k}: {v}" for k, v in stats.most_common()) or "nichts gefunden"
    print(f"\n📋 Nachgetragen{' (Testlauf, nichts gespeichert)' if dry_run else ''}: {summary}")


def main():
    parser = argparse.ArgumentParser(description="Fehlende Öffnungszeiten, Telefon und Website nachtragen")
    parser.add_argument("--dry-run", action="store_true", help="Nur anzeigen, nichts speichern")
    parser.add_argument("--limit", type=int, help="Höchstens so viele Orte prüfen")
    args = parser.parse_args()

    url, key = supabase_config()
    headers = {"apikey": key, "Authorization": f"Bearer {key}"}
    if not args.dry_run:
        headers["Authorization"] = f"Bearer {login(url, key)}"
    run(url, headers, dry_run=args.dry_run, limit=args.limit)


if __name__ == "__main__":
    main()
