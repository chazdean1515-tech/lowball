#!/usr/bin/env python3
"""Coordinates for the city map.

The site loads cities.json and never calls a geocoder. Refresh uses the
files in geo/ to fill in a city that shows up later:

- geo/places.json is every 2024 Census Gazetteer place in Florida, Texas,
  and Colorado. The point is the Census internal point (public domain).
- geo/zcta.json is the Census ZIP Code Tabulation Area centroid, used only
  when two places in a state share a name.
- geo/communities.json is a one-time offline lookup for communities that
  are not census places. Those points were checked against the state.

A city that is still unknown is left off the map. The city list still
shows it. Run `python3 city_coords.py` to rebuild cities.json from the
boards and the geo files.
"""

from __future__ import annotations

import json
import math
import re
import unicodedata
from pathlib import Path

ROOT = Path(__file__).resolve().parent
CITIES_PATH = ROOT / "cities.json"
PLACES_PATH = ROOT / "geo" / "places.json"
ZCTA_PATH = ROOT / "geo" / "zcta.json"
COMMUNITIES_PATH = ROOT / "geo" / "communities.json"
BOARDS = {
    "fl": ROOT / "listings.json",
    "tx": ROOT / "texas.json",
    "co": ROOT / "colorado.json",
}
KIND_STATE = {"fl": "FL", "tx": "TX", "co": "CO"}

# Census place names that are the same community stay one point when their
# internal points are this close. Farther apart, they are different places.
SAME_PLACE_KM = 15
# A ZIP centroid farther than this from every same-named place is not a match.
MAX_ZIP_KM = 80


def strip_accents(value: str) -> str:
    text = unicodedata.normalize("NFKD", value or "")
    return "".join(ch for ch in text if not unicodedata.combining(ch))


def norm_city(value: str) -> str:
    """Fold a city string the same way the map groups dots."""
    text = strip_accents(value or "").lower().replace(".", "").replace("'", " ")
    text = re.sub(r"\bsaint\b", "st", text)
    text = re.sub(r"\bfort\b", "ft", text)
    text = re.sub(r"\bmount\b", "mt", text)
    text = re.sub(r"^pt\s+", "port ", text)
    text = re.sub(r"\s+pt$", " point", text)
    text = text.replace("crossroads", "cross roads")
    text = re.sub(r"\bmc\s+", "mc", text)
    return re.sub(r"[^a-z0-9]+", " ", text).strip()


def _km(a: list[float], b: list[float]) -> float:
    lat1, lon1 = math.radians(a[0]), math.radians(a[1])
    lat2, lon2 = math.radians(b[0]), math.radians(b[1])
    dlat = lat2 - lat1
    dlon = lon2 - lon1
    h = math.sin(dlat / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin(dlon / 2) ** 2
    return 2 * 6371.0 * math.asin(math.sqrt(h))


def _load(path: Path) -> dict:
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def _state_table(blob: dict, kind: str) -> dict:
    table = blob.get(kind) or blob.get(KIND_STATE[kind]) or {}
    return table if isinstance(table, dict) else {}


def choose_point(candidates: list[list[float]], zip_code: str, zcta: dict) -> list[float] | None:
    if not candidates:
        return None
    if len(candidates) == 1:
        return candidates[0]
    target = zcta.get(str(zip_code or "").strip())
    if not target:
        return None
    ranked = sorted(candidates, key=lambda point: _km(point, target))
    if _km(ranked[0], target) > MAX_ZIP_KM:
        return None
    if len(ranked) > 1 and _km(ranked[1], target) - _km(ranked[0], target) < SAME_PLACE_KM:
        return None
    return ranked[0]


def resolve_city(kind: str, city: str, zip_code: str, places: dict, zcta: dict, communities: dict) -> list[float] | None:
    key = norm_city(city)
    if not key or key == "unknown city":
        return None
    community = _state_table(communities, kind).get(key)
    if isinstance(community, list) and len(community) == 2:
        return [float(community[0]), float(community[1])]
    candidates = _state_table(places, kind).get(key) or []
    if isinstance(candidates, list) and candidates and isinstance(candidates[0], (int, float)):
        candidates = [candidates]
    cleaned = []
    for point in candidates:
        if isinstance(point, list) and len(point) >= 2:
            cleaned.append([float(point[0]), float(point[1])])
    return choose_point(cleaned, zip_code, zcta)


def cities_from_board(board: dict) -> dict[str, str]:
    """Exact city string -> most common ZIP on that city's rows."""
    counts: dict[str, dict[str, int]] = {}
    for row in board.get("listings") or []:
        if not isinstance(row, dict):
            continue
        city = str(row.get("city") or "").strip()
        if not city:
            continue
        zips = counts.setdefault(city, {})
        zip_code = str(row.get("zip") or "").strip()
        zips[zip_code] = zips.get(zip_code, 0) + 1
    chosen = {}
    for city, zips in counts.items():
        best = sorted(zips.items(), key=lambda item: (-item[1], item[0]))[0][0]
        chosen[city] = best
    return chosen


def sync_board_cities(board: dict, kind: str, dry_run: bool = False) -> list[str]:
    """Write coordinates for this board's cities into cities.json.

    Returns city names that still have no point. Does not call a geocoder.
    """
    if kind not in BOARDS:
        raise ValueError(f"unknown board {kind}")
    places = _load(PLACES_PATH)
    zcta = _load(ZCTA_PATH)
    communities = _load(COMMUNITIES_PATH)
    current = _load(CITIES_PATH)
    if not isinstance(current, dict):
        current = {}
    state_coords: dict[str, list[float]] = {}
    missing = []
    for city, zip_code in sorted(cities_from_board(board).items()):
        point = resolve_city(kind, city, zip_code, places, zcta, communities)
        if not point:
            missing.append(city)
            continue
        state_coords[city] = [round(point[0], 6), round(point[1], 6)]
    current[kind] = state_coords
    # Keep a short note so the file explains itself.
    current["note"] = (
        "Latitude, longitude for each city string on the boards. "
        "Census Gazetteer internal points, plus geo/communities.json "
        "for places that are not census places. No runtime geocoder."
    )
    text = json.dumps(current, indent=2, ensure_ascii=False) + "\n"
    if CITIES_PATH.exists() and CITIES_PATH.read_text(encoding="utf-8") == text:
        for city in missing:
            print(f"cities.json: no coordinates for {KIND_STATE[kind]} {city}")
        return missing
    if dry_run:
        print(f"cities.json: would update {kind} ({len(state_coords)} cities, {len(missing)} without a point)")
        return missing
    CITIES_PATH.write_text(text, encoding="utf-8")
    print(f"cities.json: updated {kind} ({len(state_coords)} cities, {len(missing)} without a point)")
    for city in missing:
        print(f"cities.json: no coordinates for {KIND_STATE[kind]} {city}")
    return missing


def sync_all(dry_run: bool = False) -> int:
    missing = 0
    for kind, path in BOARDS.items():
        board = json.loads(path.read_text(encoding="utf-8"))
        missing += len(sync_board_cities(board, kind, dry_run=dry_run))
    return missing


def main() -> int:
    missing = sync_all(dry_run=False)
    print(f"cities still off the map: {missing}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
