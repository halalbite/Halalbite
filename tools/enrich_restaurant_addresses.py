#!/usr/bin/env python3
"""Fill missing restaurant addresses from unique exact matches in osm_places.

Only rows with an empty address are considered. Matching uses the normalized
restaurant name. If multiple distinct OSM addresses match, or the restaurant
name is duplicated in restaurants, the row is skipped rather than guessed.
"""

import argparse
from collections import Counter, defaultdict

from import_osm import fetch_all_rows, http, login, normalize, supabase_config


def clean(value):
    return str(value or "").strip()


def address_from_osm(row):
    street = clean(row.get("street"))
    postal_code = clean(row.get("postal_code"))
    city = clean(row.get("city"))

    if not street or not (postal_code or city):
        return None

    locality = " ".join(part for part in (postal_code, city) if part)
    return f"{street}, {locality}"


def fetch_rows(url, path, headers, label):
    status, rows = fetch_all_rows(url, path, headers)
    if status != 200 or not isinstance(rows, list):
        raise RuntimeError(f"{label} konnte nicht geladen werden: HTTP {status}, {rows}")
    return rows


def main():
    parser = argparse.ArgumentParser(
        description="Ergänzt fehlende Restaurantadressen aus eindeutigen OSM Treffern"
    )
    parser.add_argument("--dry-run", action="store_true", help="Nur anzeigen, nichts speichern")
    parser.add_argument("--limit", type=int, help="Höchstens so viele leere Adressen prüfen")
    args = parser.parse_args()

    url, key = supabase_config()
    headers = {"apikey": key, "Authorization": f"Bearer {key}"}
    if not args.dry_run:
        headers["Authorization"] = f"Bearer {login(url, key)}"

    restaurants = fetch_rows(
        url,
        "restaurants?select=id,name,address&or=(address.is.null,address.eq.)",
        headers,
        "restaurants",
    )
    osm_rows = fetch_rows(
        url,
        "osm_places?select=name,street,postal_code,city&hidden=is.false&promoted_restaurant_id=is.null",
        headers,
        "osm_places",
    )

    osm_by_name = defaultdict(dict)
    for place in osm_rows:
        name_key = normalize(clean(place.get("name")))
        address = address_from_osm(place)
        if name_key and address:
            address_key = normalize(address)
            osm_by_name[name_key][address_key] = address

    restaurant_name_counts = Counter(
        normalize(clean(row.get("name")))
        for row in fetch_rows(
            url,
            "restaurants?select=name",
            headers,
            "Restaurantnamen",
        )
        if normalize(clean(row.get("name")))
    )

    pending = restaurants[:args.limit] if args.limit else restaurants
    updated = 0
    unmatched = 0
    ambiguous = 0
    duplicate_names = 0
    write_headers = {
        **headers,
        "Content-Type": "application/json",
        "Prefer": "return=minimal",
    }

    for restaurant in pending:
        name = clean(restaurant.get("name"))
        name_key = normalize(name)

        if not name_key or restaurant_name_counts[name_key] > 1:
            duplicate_names += 1
            print(f"Übersprungen, Name mehrfach in restaurants: {name}")
            continue

        candidates = list(osm_by_name.get(name_key, {}).values())
        if len(candidates) == 0:
            unmatched += 1
            continue
        if len(candidates) > 1:
            ambiguous += 1
            print(f"Übersprungen, mehrere OSM Adressen für {name}: {candidates}")
            continue

        address = candidates[0]
        print(f"{name} → {address}")

        if args.dry_run:
            continue

        row_id = restaurant.get("id")
        status, body = http(
            "PATCH",
            f"{url}/rest/v1/restaurants?id=eq.{row_id}&or=(address.is.null,address.eq.)",
            write_headers,
            {"address": address},
        )
        if status not in (200, 204):
            print(f"Fehler beim Speichern für {name}: HTTP {status}, {body}")
            continue
        updated += 1

    print(
        f"Fertig. Gespeichert: {updated if not args.dry_run else 0}. "
        f"Ohne Treffer: {unmatched}. Mehrdeutig: {ambiguous}. "
        f"Doppelte Namen: {duplicate_names}."
    )
    if args.dry_run:
        print("Testlauf: Es wurden keine Daten geändert.")


if __name__ == "__main__":
    main()
