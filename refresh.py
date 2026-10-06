#!/usr/bin/env python3
# Daily command, from the repo root:
#   python3 refresh.py
# Rechecks homes already on the board and adds newly price-cut listings
# in those same cities. Each request is one Zillow price-reduced search
# list (the next page only when that list says there is one). It does not
# open listing pages. If that list is missing, it stops and writes nothing.
# A run that finds no price, score, or membership change does not rewrite
# listings.json, texas.json, or colorado.json.
#
# New homes are added only when their latest price cut is dated on or after
# the cutoff in refresh_state.json ("last_fetch_date", the America/New_York
# date of the last complete run). The cutoff day itself is included because
# Zillow dates cuts by day only, so a cut dated on the last run's day may have
# posted after that run. Homes already on the board are never added twice.
# A complete, written (not --dry-run, not limited) run moves the cutoff to
# today; commit refresh_state.json with the boards. Override with --since.
#
# Page caps: --max-pages per city (default 5) and --max-total-pages across
# all states (default 750). Every request, including a slug that 404s,
# counts toward the total. If the total cap is hit, the cities left are
# skipped and the cutoff is not moved, so the next run still covers them.
#
# Colorado starts from CO_SEED_CITIES when colorado.json has no cities yet.
#
# Colorado deepen (listing pages, same rules as FL/TX deep scores):
#   python3 deepen_colorado.py --limit 120
# Or after a CO list refresh:
#   python3 refresh.py --state CO --deepen-co 80
# deepen_colorado.py visits Redfin listing pages via Playwright (urllib hits WAF),
# scores multi-cuts / off-market / fall-through / assessment / remarks, and writes
# colorado.json. Priority: Denver, Aurora, Colorado Springs, Fort Collins, Boulder
# and current top shallow scores. Cap with --limit / --deepen-co.
#
# Smoke test, one city, no writes:
#   python3 refresh.py --city Eustis --state FL --max-pages 1 --dry-run

"""Refresh the Lowball boards from Zillow price-reduced city lists."""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path
from zoneinfo import ZoneInfo

from city_coords import sync_board_cities
from listing_rules import (
    address_street_number,
    is_non_home,
    prepare_board,
    url_street_number,
)

ROOT = Path(__file__).resolve().parent
FL_PATH = ROOT / "listings.json"
TX_PATH = ROOT / "texas.json"
CO_PATH = ROOT / "colorado.json"
STATE_PATH = ROOT / "refresh_state.json"
DEFAULT_MAX_PAGES = 5
DEFAULT_MAX_TOTAL_PAGES = 750
TZ = ZoneInfo("America/New_York")
UA = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
)
LAND_TYPES = {"LOT", "LAND", "VACANT_LAND", "VACANTLAND"}
KIND_STATE = {"fl": "FL", "tx": "TX", "co": "CO"}
HOME_FL = {
    "SINGLE_FAMILY": "Single family",
    "CONDO": "Condo",
    "TOWNHOUSE": "Townhouse",
    "MANUFACTURED": "Manufactured",
    "MULTI_FAMILY": "Multi-family",
}
HOME_TX = {
    "SINGLE_FAMILY": "single-family",
    "CONDO": "condo",
    "TOWNHOUSE": "townhouse",
    "MANUFACTURED": "manufactured",
    "MULTI_FAMILY": "multi-family",
}
# Same labels as Texas; Colorado cards use the TX-style schema.
HOME_CO = HOME_TX
# Used only when colorado.json has no cities yet (first seed / empty board).
CO_SEED_CITIES = [
    "Denver",
    "Aurora",
    "Colorado Springs",
    "Fort Collins",
    "Boulder",
    "Lakewood",
    "Thornton",
    "Arvada",
    "Westminster",
    "Pueblo",
    "Centennial",
    "Greeley",
    "Longmont",
    "Loveland",
    "Grand Junction",
    "Broomfield",
    "Castle Rock",
    "Commerce City",
    "Parker",
    "Littleton",
    "Northglenn",
    "Englewood",
    "Wheat Ridge",
    "Brighton",
    "Fountain",
    "Lafayette",
    "Louisville",
    "Erie",
    "Superior",
    "Golden",
    "Highlands Ranch",
    "Lone Tree",
    "Greenwood Village",
    "Cherry Hills Village",
    "Federal Heights",
    "Sheridan",
    "Edgewater",
    "Firestone",
    "Frederick",
    "Windsor",
    "Johnstown",
    "Evans",
    "Montrose",
    "Durango",
    "Steamboat Springs",
    "Aspen",
    "Vail",
    "Glenwood Springs",
    "Canon City",
    "Trinidad",
]
STREET = {
    "STREET": "ST",
    "AVENUE": "AVE",
    "DRIVE": "DR",
    "ROAD": "RD",
    "BOULEVARD": "BLVD",
    "LANE": "LN",
    "COURT": "CT",
    "PLACE": "PL",
    "CIRCLE": "CIR",
    "TERRACE": "TER",
    "TRAIL": "TRL",
    "PARKWAY": "PKWY",
}
POINT_RE = re.compile(
    r"\+\s*(\d+(?:\.\d+)?)(?:\s*of\s*\d+(?:\.\d+)?)?\.?\s*$"
)
DOM_RE = re.compile(
    r"\b\d+(?:\.\d+)?\s+days on (?:Zillow|Redfin|market)\b",
    re.I,
)
NEXT_RE = re.compile(
    r'<script id="__NEXT_DATA__" type="application/json">(.*?)</script>',
    re.S,
)


class ListBlocked(Exception):
    """The public search list was not returned. Do not fall back to listing pages."""


def round1(value: Decimal) -> Decimal:
    return value.quantize(Decimal("0.1"), rounding=ROUND_HALF_UP)


def money(n: int) -> str:
    return f"${n:,}"


def num_txt(n) -> str:
    if isinstance(n, float) and n.is_integer():
        return str(int(n))
    if isinstance(n, Decimal) and n == n.to_integral_value():
        return str(int(n))
    return str(n)


def today_iso() -> str:
    return datetime.now(TZ).date().isoformat()


def ms_to_iso(ms) -> str | None:
    if not isinstance(ms, (int, float)):
        return None
    return datetime.fromtimestamp(ms / 1000, tz=TZ).date().isoformat()


def ms_to_date(ms) -> str | None:
    if not isinstance(ms, (int, float)):
        return None
    dt = datetime.fromtimestamp(ms / 1000, tz=TZ)
    return f"{dt.strftime('%b')} {dt.day}, {dt.year}"


def norm_street(value: str) -> str:
    text = value.upper().replace("#", " ")
    text = re.sub(r"[^A-Z0-9 ]", " ", text)
    parts = [STREET.get(p, p) for p in text.split()]
    return " ".join(parts)


def norm_place(value: str) -> str:
    text = value.lower().replace(".", "")
    text = re.sub(r"\bsaint\b", "st", text)
    text = re.sub(r"\bfort\b", "ft", text)
    text = re.sub(r"\bmount\b", "mt", text)
    return re.sub(r"[^a-z0-9]+", " ", text).strip()


def street_of(address: str) -> str:
    return address.split(",")[0].strip()


def match_key(street: str, city: str, zip_code: str) -> tuple[str, str, str]:
    return (norm_street(street), norm_place(city), str(zip_code or "").strip())


def slug_token_sets(city: str) -> list[str]:
    raw = city.lower().replace("'", "").replace(".", "")
    words = [w for w in re.sub(r"[^a-z0-9]+", " ", raw).split() if w]
    options = {
        "st": ["st", "saint"],
        "saint": ["saint", "st"],
        "ft": ["ft", "fort"],
        "fort": ["fort", "ft"],
        "mt": ["mt", "mount"],
        "mount": ["mount", "mt"],
    }
    variants = [""]
    for word in words:
        choices = options.get(word, [word])
        nxt = []
        for prefix in variants:
            for choice in choices:
                nxt.append((prefix + "-" + choice).strip("-"))
        # Cap the combinations. The first choice is always the word itself.
        variants = nxt[:8]
    seen = []
    for slug in variants:
        if slug and slug not in seen:
            seen.append(slug)
    return seen


def reason_points(text: str) -> Decimal:
    match = POINT_RE.search(str(text).strip())
    if not match:
        return Decimal(0)
    return Decimal(match.group(1))


def score_from_reasons(reasons: list[str]) -> float:
    total = sum((reason_points(r) for r in reasons), Decimal(0))
    if total > 100:
        total = Decimal(100)
    return float(round1(total))


def score_from_points(points: dict) -> float:
    total = sum((Decimal(str(v)) for v in points.values()), Decimal(0))
    if total > 100:
        total = Decimal(100)
    return float(round1(total))


def cut_points(percent: Decimal) -> Decimal:
    raw = percent * Decimal("2.3")
    if raw > 25:
        raw = Decimal(25)
    return round1(raw)


def dom_points(days: int) -> Decimal:
    raw = Decimal(days) / Decimal(12)
    if raw > 10:
        raw = Decimal(10)
    return round1(raw)


def is_dom_reason(text: str) -> bool:
    return bool(DOM_RE.search(text)) and not text.lower().startswith("latest cut")


def is_cut_reason(text: str) -> bool:
    lowered = text.lower()
    return lowered.startswith("latest cut") or lowered.startswith("no price cut")


def replace_reason(reasons: list[str], predicate, new_text: str) -> list[str]:
    out = []
    placed = False
    for reason in reasons:
        if predicate(reason):
            if not placed:
                out.append(new_text)
                placed = True
            continue
        out.append(reason)
    if not placed:
        out.insert(0, new_text)
    return out


def fmt_points(points: Decimal, cap: int) -> str:
    return f"+{points:.1f} of {cap}"


def cut_reason(obs: dict, points: Decimal) -> str:
    when = obs.get("when")
    dated = f" on {when}" if when else ""
    tail = "" if when else " The list does not print the date."
    return (
        f"Latest cut is {obs['cut_display']}%, from {money(obs['previous'])} "
        f"to {money(obs['price'])}{dated}. {fmt_points(points, 25)}.{tail}"
    )


def dom_reason(days: int, points: Decimal) -> str:
    return f"{days} days on Zillow. {fmt_points(points, 10)}."


def one_cut_reason(obs: dict) -> str:
    when = obs.get("when")
    dated = f" on {when}" if when else ""
    return (
        f"The search list shows one price cut{dated}. It does not print earlier "
        f"cuts, so only this cut is counted. +5.0 of 15."
    )


def patch_summary(summary: str, obs: dict, cut_changed: bool, dom_changed: bool) -> str:
    text = summary or ""
    if cut_changed:
        piece = f"Latest cut {money(obs['cut_dollars'])} ({obs['cut_display']}%)"
        if text.startswith("Latest cut"):
            text = re.sub(
                r"^Latest cut \$[\d,]+ \([0-9.]+%\)",
                piece,
                text,
                count=1,
            )
        elif text.startswith("No price cut"):
            text = piece + (" · " + text if text else "")
        elif text:
            text = piece + " · " + text
        else:
            text = piece
    if dom_changed:
        piece = f"{obs['days']} days on Zillow"
        if re.search(r"\d+(?:\.\d+)? days on [^·]+", text):
            text = re.sub(r"\d+(?:\.\d+)? days on [^·]+", piece, text, count=1)
        else:
            text = (text + " · " if text else "") + piece
    return text


def excluded_reason(item: dict, info: dict) -> str | None:
    url = str(item.get("detailUrl") or "")
    if "/community/" in url.lower():
        return "community floor plan"
    status = str(info.get("homeStatus") or item.get("statusType") or "").upper()
    text = str(item.get("statusText") or "").lower()
    if any(word in text for word in ("pending", "contingent", "auction", "foreclos", "sold")):
        return "pending, contingent, auction, foreclosure, or sold"
    if status in {"PENDING", "CONTINGENT", "SOLD", "OFF_MARKET", "RECENTLY_SOLD"}:
        return status.lower().replace("_", " ")
    sub = info.get("listing_sub_type") or {}
    if isinstance(sub, dict) and any(
        sub.get(key)
        for key in ("is_foreclosure", "is_auction", "is_forAuction", "is_pending", "is_bankOwned")
    ):
        return "foreclosure, auction, or pending"
    if info.get("isPreforeclosureAuction"):
        return "auction"
    home_type = str(info.get("homeType") or "").upper()
    if home_type in LAND_TYPES or "LAND" in home_type:
        return "land"
    try:
        price = int(info.get("price") if info.get("price") is not None else item.get("unformattedPrice"))
    except (TypeError, ValueError):
        price = None
    if is_non_home(
        {
            "price": price,
            "address": str(info.get("streetAddress") or item.get("addressStreet") or ""),
            "sqft": info.get("livingArea", item.get("area")),
            "beds": info.get("bedrooms", item.get("beds")),
        }
    ):
        return "not a home"
    if status and status not in {"FOR_SALE", "FORSALE"}:
        return status.lower().replace("_", " ")
    return None


def observation_from_item(item: dict) -> dict | None:
    info = (item.get("hdpData") or {}).get("homeInfo") or {}
    if not isinstance(info, dict):
        info = {}
    price = info.get("price", item.get("unformattedPrice"))
    change = info.get("priceChange")
    try:
        price = int(price)
        change = int(change)
    except (TypeError, ValueError):
        return None
    if price <= 0 or change >= 0:
        return None
    previous = price - change
    if previous <= 0:
        return None
    percent = (Decimal(abs(change)) / Decimal(previous)) * Decimal(100)
    days = info.get("daysOnZillow")
    if isinstance(days, float) and days.is_integer():
        days = int(days)
    if not isinstance(days, int):
        days = None
    if days is not None and days < 0:
        days = None
    home_type = str(info.get("homeType") or "").upper() or None
    zpid = item.get("zpid") or info.get("zpid")
    url = item.get("detailUrl") or ""
    if url.startswith("/"):
        url = "https://www.zillow.com" + url
    street = str(info.get("streetAddress") or item.get("addressStreet") or "").strip()
    city = str(info.get("city") or item.get("addressCity") or "").strip()
    state = str(info.get("state") or item.get("addressState") or "").strip().upper()
    zip_code = str(info.get("zipcode") or item.get("addressZipcode") or "").strip()
    seen = {
        "price": price,
        "change": change,
        "cut_dollars": abs(change),
        "previous": previous,
        "percent": percent,
        "cut_display": f"{round1(percent):.1f}",
        "days": days,
        "when": ms_to_date(info.get("datePriceChanged")),
        "cut_date": ms_to_iso(info.get("datePriceChanged")),
        "home_type": home_type,
        "beds": info.get("bedrooms", item.get("beds")),
        "baths": info.get("bathrooms", item.get("baths")),
        "sqft": info.get("livingArea", item.get("area")),
        "zpid": str(zpid) if zpid is not None else "",
        "url": url,
        "street": street,
        "city": city,
        "state": state,
        "zip": zip_code,
        "exclude": excluded_reason(item, info),
    }
    if not seen["exclude"]:
        number = address_street_number(seen["street"])
        linked = url_street_number(seen["url"])
        if number and linked and number != linked:
            seen["exclude"] = "link does not match address"
    return seen


def parse_search_html(html: str, url: str) -> dict:
    match = NEXT_RE.search(html)
    lowered = html.lower()
    # A config key such as GOOGLE_CAPTCHA_PUBLIC_KEY is not a block.
    # Only stop when the list itself is missing or a challenge page replaced it.
    hard_block = any(
        marker in lowered
        for marker in ("px-captcha", "verify you are a human", "access to this page has been denied")
    )
    if hard_block and not match:
        raise ListBlocked(f"{url} returned a block page instead of a search list.")
    if not match:
        raise ListBlocked(
            f"{url} did not include a search list. "
            "This updater will not open individual listing pages."
        )
    try:
        data = json.loads(match.group(1))
        page = data["props"]["pageProps"]["searchPageState"]
        results = page["cat1"]["searchResults"]["listResults"]
        search_list = page["cat1"].get("searchList") or {}
    except (KeyError, TypeError, json.JSONDecodeError) as exc:
        raise ListBlocked(
            f"{url} did not include a readable search list ({exc}). "
            "This updater will not open individual listing pages."
        ) from exc
    if not isinstance(results, list):
        raise ListBlocked(
            f"{url} did not include a list of homes. "
            "This updater will not open individual listing pages."
        )
    return {
        "results": results,
        "region": (page.get("regionState") or {}).get("regionInfo") or [],
        "next_url": (search_list.get("pagination") or {}).get("nextUrl") or "",
        "total": (page.get("categoryTotals") or {}).get("cat1", {}).get("totalResultCount"),
    }


def region_matches(region_info, city: str, state: str) -> bool:
    want_city = norm_place(city)
    want_display = norm_place(f"{city} {state}")
    for region in region_info or []:
        if norm_place(str(region.get("regionName") or "")) == want_city:
            return True
        if norm_place(str(region.get("displayName") or "")) == want_display:
            return True
    return False


def fetch(url: str) -> tuple[int, str, str]:
    if "/homedetails/" in url:
        raise ListBlocked(f"Refusing listing page {url}.")
    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": UA,
            "Accept": "text/html,application/xhtml+xml",
            "Accept-Language": "en-US,en;q=0.9",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=40) as response:
            body = response.read().decode("utf-8", "replace")
            return response.status, response.geturl(), body
    except urllib.error.HTTPError as exc:
        if exc.code in (403, 429, 503):
            raise ListBlocked(
                f"{url} returned HTTP {exc.code}, so the search list is blocked."
            ) from exc
        if exc.code == 404:
            return 404, url, ""
        raise ListBlocked(
            f"{url} returned HTTP {exc.code}. "
            "This updater will not open individual listing pages."
        ) from exc


def fetch_city_lists(
    city: str, state: str, max_pages: int | None, delay: float, budget: dict
) -> list[dict] | None:
    last_error = None
    for slug in slug_token_sets(city):
        url = f"https://www.zillow.com/{slug}-{state.lower()}/price-reduced/"
        pages = []
        seen_urls = set()
        page_num = 0
        while url and url not in seen_urls:
            page_num += 1
            if max_pages is not None and page_num > max_pages:
                print(f"{city}, {state}: stopped at the {max_pages}-page city cap")
                break
            if budget["left"] <= 0:
                budget["hit"] = True
                print(f"{city}, {state}: stopped, total page cap reached")
                break
            budget["left"] -= 1
            budget["used"] += 1
            seen_urls.add(url)
            if pages and delay:
                time.sleep(delay)
            status, _final, html = fetch(url)
            if status == 404:
                last_error = f"HTTP 404 for {url}"
                pages = []
                break
            parsed = parse_search_html(html, url)
            if not region_matches(parsed["region"], city, state):
                names = [
                    str(r.get("displayName") or r.get("regionName") or "")
                    for r in parsed["region"]
                ]
                last_error = (
                    f"{url} came back as {', '.join(names) or 'another region'}, not {city}, {state}"
                )
                pages = []
                break
            pages.append(parsed)
            print(
                f"{city}, {state}: {url} "
                f"(page {page_num}, {len(parsed['results'])} rows, "
                f"{parsed.get('total')} price-reduced in the city)"
            )
            nxt = parsed["next_url"]
            if not nxt or "/price-reduced" not in nxt:
                break
            url = nxt if nxt.startswith("http") else "https://www.zillow.com" + nxt
        if pages:
            return pages
        if budget["left"] <= 0:
            budget["hit"] = True
            break
        if delay:
            time.sleep(min(delay, 0.5))
    print(
        f"Skipped {city}, {state}: no price-reduced search list matched that city"
        + (f" ({last_error})" if last_error else "")
        + ". Listing pages were not opened."
    )
    return None


def index_board(kind: str, listings: list[dict]) -> tuple[dict, dict]:
    by_zpid = {}
    by_addr = {}
    for listing in listings:
        zpid = str(listing.get("zpid") or "")
        if zpid:
            by_zpid[zpid] = listing
        if kind == "fl":
            street = street_of(str(listing.get("address") or ""))
        else:
            street = str(listing.get("address") or "")
        key = match_key(street, str(listing.get("city") or ""), str(listing.get("zip") or ""))
        by_addr.setdefault(key, listing)
    return by_zpid, by_addr


def find_listing(obs: dict, by_zpid: dict, by_addr: dict):
    if obs["zpid"] and obs["zpid"] in by_zpid:
        return by_zpid[obs["zpid"]]
    key = match_key(obs["street"], obs["city"], obs["zip"])
    return by_addr.get(key)


def cities_on_board(listings: list[dict], state: str) -> list[str]:
    found = []
    seen = set()
    for listing in listings:
        city = str(listing.get("city") or "").strip()
        if not city:
            continue
        marker = norm_place(city)
        if marker in seen:
            continue
        seen.add(marker)
        found.append(city)
    return found


def apply_to_existing(kind: str, listing: dict, obs: dict) -> str:
    if obs["exclude"]:
        return "remove"
    if kind == "fl":
        stored_price = listing.get("price")
        stored_cut = listing.get("latest_cut_percent")
        stored_dom = listing.get("dom")
    else:
        stored_price = listing.get("price")
        stored_cut = listing.get("cutPercent")
        stored_dom = listing.get("dom")
    try:
        price_changed = int(stored_price) != obs["price"]
    except (TypeError, ValueError):
        price_changed = True
    try:
        cut_changed = f"{Decimal(str(stored_cut)):.1f}" != obs["cut_display"]
    except Exception:
        cut_changed = True
    dom_changed = False
    if obs["days"] is not None:
        try:
            dom_changed = int(round(float(stored_dom))) != obs["days"]
        except (TypeError, ValueError):
            dom_changed = True
    if not price_changed and not cut_changed and not dom_changed:
        return "same"

    listing["price"] = obs["price"]
    reasons = list(listing.get("reasons") or [])
    if cut_changed:
        points = cut_points(obs["percent"])
        reasons = replace_reason(reasons, is_cut_reason, cut_reason(obs, points))
        if kind == "fl":
            listing["latest_cut_percent"] = float(obs["cut_display"])
            listing["latest_cut_dollars"] = obs["cut_dollars"]
        else:
            listing["cutPercent"] = float(obs["cut_display"])
            points_map = dict(listing.get("points") or {})
            points_map["latestCut"] = float(points)
            listing["points"] = points_map
    if dom_changed and obs["days"] is not None:
        points = dom_points(obs["days"])
        reasons = replace_reason(reasons, is_dom_reason, dom_reason(obs["days"], points))
        listing["dom"] = obs["days"]
        if kind == "fl":
            listing["dom_label"] = "days on Zillow"
        else:
            listing["domSource"] = "days on Zillow"
            points_map = dict(listing.get("points") or {})
            points_map["dom"] = float(points)
            listing["points"] = points_map
    listing["reasons"] = reasons
    if kind != "fl" and isinstance(listing.get("points"), dict):
        listing["score"] = score_from_points(listing["points"])
    else:
        listing["score"] = score_from_reasons(reasons)
    if kind == "fl":
        listing["cut_summary"] = patch_summary(
            listing.get("cut_summary") or "",
            obs,
            cut_changed,
            dom_changed,
        )
    listing["fetched"] = today_iso()
    listing.pop("verified_from", None)
    listing.pop("verifiedFrom", None)
    return "updated"


def new_listing(kind: str, obs: dict) -> dict:
    latest = cut_points(obs["percent"])
    cuts = Decimal("5.0")
    parts = [latest, cuts]
    reasons = [cut_reason(obs, latest), one_cut_reason(obs)]
    if obs["days"] is not None:
        dom_pts = dom_points(obs["days"])
        parts.append(dom_pts)
        reasons.append(dom_reason(obs["days"], dom_pts))
    total = sum(parts, Decimal(0))
    if total > 100:
        total = Decimal(100)
    score = float(round1(total))
    home_fl = HOME_FL.get(obs["home_type"] or "")
    home_tx = (HOME_CO if kind == "co" else HOME_TX).get(obs["home_type"] or "")
    beds = obs["beds"]
    baths = obs["baths"]
    sqft = obs["sqft"] if isinstance(obs["sqft"], (int, float)) and obs["sqft"] > 0 else None
    fetched = today_iso()
    if kind == "fl":
        street = obs["street"].title()
        bits = []
        if home_fl:
            bits.append(home_fl)
        if beds is not None:
            bits.append(f"{num_txt(beds)} bd")
        if baths is not None:
            bits.append(f"{num_txt(baths)} ba")
        if sqft is not None:
            bits.append(f"{int(sqft):,} sq ft")
        summary_bits = [
            f"Latest cut {money(obs['cut_dollars'])} ({obs['cut_display']}%)",
            "1 cut shown",
        ]
        if obs["days"] is not None:
            summary_bits.append(f"{obs['days']} days on Zillow")
        return {
            "address": f"{street}, {obs['city']}, FL {obs['zip']}",
            "city": obs["city"],
            "state": "FL",
            "zip": obs["zip"],
            "price": obs["price"],
            "beds": beds,
            "baths": baths,
            "sqft": int(sqft) if sqft is not None else None,
            "acres": None,
            "home_type": home_fl,
            "facts": " · ".join(bits),
            "status": "For sale",
            "latest_cut_dollars": obs["cut_dollars"],
            "latest_cut_percent": float(obs["cut_display"]),
            "cuts_counted": 1,
            "dom": obs["days"],
            "dom_label": "days on Zillow" if obs["days"] is not None else None,
            "cut_summary": " · ".join(summary_bits),
            "score": score,
            "reasons": reasons,
            "url": obs["url"],
            "source": "Zillow",
            "mls": None,
            "zpid": obs["zpid"],
            "fetched": fetched,
            "original": False,
        }
    points = {"latestCut": float(latest), "cuts": 5.0}
    if obs["days"] is not None:
        points["dom"] = float(dom_points(obs["days"]))
    return {
        "address": obs["street"].title(),
        "city": obs["city"].title() if obs["city"] else obs["city"],
        "zip": obs["zip"],
        "price": obs["price"],
        "beds": beds,
        "baths": baths,
        "sqft": int(sqft) if sqft is not None else None,
        "propertyType": home_tx,
        "status": "for sale",
        "cutPercent": float(obs["cut_display"]),
        "dom": obs["days"],
        "domSource": "days on Zillow" if obs["days"] is not None else None,
        "url": obs["url"],
        "source": "Zillow",
        "zpid": obs["zpid"],
        "reasons": reasons,
        "score": score,
        "points": points,
        "fetched": fetched,
    }


def refresh_board(
    kind: str,
    board: dict,
    city_filter: str | None,
    max_pages: int | None,
    delay: float,
    since: str,
    budget: dict,
):
    state = KIND_STATE[kind]
    listings = board["listings"]
    wanted = cities_on_board(listings, state)
    if not wanted and kind == "co":
        wanted = list(CO_SEED_CITIES)
        print(f"Colorado board has no cities yet; seeding from {len(wanted)} metro / major CO cities.")
    if city_filter:
        wanted = [city for city in wanted if norm_place(city) == norm_place(city_filter)]
        if not wanted and kind == "co":
            # Allow seeding a single named CO city even before the board exists.
            wanted = [city_filter]
        if not wanted:
            raise SystemExit(
                f"{city_filter} is not a {state} city already on the board. Nothing was requested."
            )
    by_zpid, by_addr = index_board(kind, listings)
    stats = {
        "cities": 0,
        "pages": 0,
        "rows": 0,
        "rechecked": 0,
        "updated": 0,
        "added": 0,
        "removed": 0,
        "skipped": 0,
        "old_cut": 0,
        "cities_capped_out": 0,
    }
    remove_ids = set()
    seen_new = set()
    stats["examples"] = []
    for city in wanted:
        if budget["left"] <= 0:
            budget["hit"] = True
            stats["cities_capped_out"] += 1
            continue
        pages = fetch_city_lists(city, state, max_pages, delay, budget)
        if not pages:
            stats["skipped"] += 1
            continue
        stats["cities"] += 1
        stats["pages"] += len(pages)
        if delay and wanted[-1] != city:
            time.sleep(delay)
        for page in pages:
            for item in page["results"]:
                stats["rows"] += 1
                obs = observation_from_item(item)
                if obs is None:
                    stats["skipped"] += 1
                    continue
                if norm_place(obs["city"]) != norm_place(city) or obs["state"] != state:
                    stats["skipped"] += 1
                    continue
                existing = find_listing(obs, by_zpid, by_addr)
                if existing is not None:
                    stats["rechecked"] += 1
                    outcome = apply_to_existing(kind, existing, obs)
                    if outcome == "remove":
                        if id(existing) not in remove_ids:
                            remove_ids.add(id(existing))
                            stats["removed"] += 1
                    elif outcome == "updated":
                        stats["updated"] += 1
                        if len(stats["examples"]) < 5:
                            stats["examples"].append(f"updated {obs['street'].title()} score {existing.get('score')}")
                    continue
                if obs["exclude"] or not obs["zpid"] or obs["zpid"] in seen_new:
                    stats["skipped"] += 1
                    continue
                # Only brand-new cuts become new cards. A row with no cut date,
                # or a cut dated before the cutoff, is left off.
                if not is_new_cut(obs, since):
                    stats["old_cut"] += 1
                    continue
                created = new_listing(kind, obs)
                listings.append(created)
                seen_new.add(obs["zpid"])
                by_zpid[obs["zpid"]] = created
                by_addr[match_key(obs["street"], obs["city"], obs["zip"])] = created
                stats["added"] += 1
                if len(stats["examples"]) < 5:
                    stats["examples"].append(
                        f"added {created.get('address')} cut {created.get('latest_cut_percent', created.get('cutPercent'))}% score {created.get('score')}"
                    )
    if remove_ids:
        board["listings"] = [row for row in listings if id(row) not in remove_ids]
    else:
        board["listings"] = listings
    return stats


def is_new_cut(obs: dict, since: str) -> bool:
    cut_date = obs.get("cut_date")
    return bool(cut_date) and cut_date >= since


def load_state() -> dict:
    if not STATE_PATH.exists():
        return {}
    try:
        data = json.loads(STATE_PATH.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise SystemExit(f"{STATE_PATH.name} is not valid JSON ({exc}). Nothing was requested.")
    return data if isinstance(data, dict) else {}


def resolve_since(arg_since: str | None, state: dict, *boards: dict) -> tuple[str, str]:
    if arg_since:
        return parse_day(arg_since), "--since"
    saved = state.get("last_fetch_date")
    if saved:
        return parse_day(saved), f"{STATE_PATH.name} last_fetch_date"
    # No state file yet: fall back to the older board fetch date, never wider.
    dates = [b.get("fetched") for b in boards if b.get("fetched")]
    if dates:
        return parse_day(min(dates)), "board fetched date (no state file)"
    return today_iso(), "today (no state file or board date)"


def parse_day(value: str) -> str:
    try:
        return datetime.strptime(str(value), "%Y-%m-%d").date().isoformat()
    except ValueError:
        raise SystemExit(f"Cutoff {value!r} is not YYYY-MM-DD. Nothing was requested.")


def save_state(state: dict, stats: dict) -> None:
    now = datetime.now(TZ)
    out = dict(state)
    out.update(
        {
            "timezone": "America/New_York",
            "last_fetch": now.isoformat(timespec="seconds"),
            "last_fetch_date": now.date().isoformat(),
            "rule": (
                "refresh.py adds a new home only when its latest Zillow price cut is dated "
                "on or after last_fetch_date. Updated after each complete, written run."
            ),
            "last_run": stats,
        }
    )
    temp = STATE_PATH.with_suffix(".json.tmp")
    temp.write_text(json.dumps(out, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    temp.replace(STATE_PATH)
    print(f"{STATE_PATH.name}: cutoff moved to {out['last_fetch_date']}")


def counts_changed(kind: str, board: dict) -> None:
    listings = board["listings"]
    board["count"] = len(listings)
    if kind == "fl":
        original = sum(1 for row in listings if row.get("original") is True)
        board["original_count"] = original
        board["new_count"] = board["count"] - original
    else:
        # Original Texas rows have no source. Rows this script adds are source Zillow.
        board["originalCount"] = sum(1 for row in listings if not row.get("source"))
        board["newCount"] = board["count"] - board["originalCount"]
    board["fetched"] = today_iso()


INTERNAL_BOARD_KEYS = ("note", "blocked", "leftOff")
# Published Texas rules used to say "days on Redfin" even though the rows came from Zillow.
TX_RULES_DOM_OLD = "otherwise days on Redfin."
TX_RULES_DOM_NEW = "otherwise days on Zillow. A few earlier Texas listings use days on Redfin."


def extract_fetched(text: str) -> str | None:
    match = re.search(r"\d{4}-\d{2}-\d{2}", text)
    if match:
        return match.group(0)
    match = re.search(
        r"(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\s+(\d{1,2}),\s+(\d{4})",
        text,
        re.I,
    )
    if not match:
        return None
    try:
        parsed = datetime.strptime(
            f"{match.group(1)[:3]} {int(match.group(2))} {match.group(3)}",
            "%b %d %Y",
        )
    except ValueError:
        return None
    return parsed.date().isoformat()


def scrub_published(board: dict) -> bool:
    """Drop scrape notes before a board is written to the public JSON files."""
    changed = False
    for key in INTERNAL_BOARD_KEYS:
        if key in board:
            board.pop(key, None)
            changed = True
    rules = board.get("rules")
    if isinstance(rules, str) and TX_RULES_DOM_OLD in rules:
        board["rules"] = rules.replace(TX_RULES_DOM_OLD, TX_RULES_DOM_NEW)
        changed = True
    for row in board.get("listings") or []:
        if not isinstance(row, dict):
            continue
        narrative = None
        for old in ("verified_from", "verifiedFrom"):
            if old in row:
                narrative = row.pop(old)
                changed = True
        if narrative and not row.get("fetched"):
            found = extract_fetched(str(narrative))
            if found:
                row["fetched"] = found
    kind = "tx" if board.get("state") == "TX" else "co" if board.get("state") == "CO" else "fl"
    before_rows = json.dumps(board.get("listings"), sort_keys=True, default=str)
    before_rules = board.get("rules")
    prepare_board(board, kind)
    if json.dumps(board.get("listings"), sort_keys=True, default=str) != before_rows or board.get("rules") != before_rules:
        changed = True
    return changed


def write_if_changed(path: Path, before: str, board: dict, dry_run: bool) -> bool:
    scrub_published(board)
    kind = "tx" if path == TX_PATH else "co" if path == CO_PATH else "fl"
    # Adds map coordinates for a new city when geo/ already knows it.
    sync_board_cities(board, kind, dry_run=dry_run)
    after = json.dumps(board, indent=2, ensure_ascii=False) + "\n"
    if after == before:
        print(f"{path.name}: no change, not rewritten")
        return False
    if dry_run:
        print(f"{path.name}: would rewrite ({len(board['listings'])} listings), dry run so not written")
        return False
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(after, encoding="utf-8")
    temp.replace(path)
    print(f"{path.name}: updated")
    return True


def top_home(kind: str, listings: list[dict]) -> str:
    if not listings:
        return "none"
    best = max(listings, key=lambda row: float(row.get("score") or 0))
    score = float(best.get("score") or 0)
    if kind == "fl":
        addr = best.get("address") or ""
    else:
        st = KIND_STATE.get(kind, "")
        addr = f"{best.get('address')}, {best.get('city')}, {st} {best.get('zip')}"
    return f"{addr} — {score:.1f}"


def load_board(path: Path) -> tuple[dict, str]:
    text = path.read_text(encoding="utf-8")
    return json.loads(text), text if text.endswith("\n") else text + "\n"


def snapshot(board: dict) -> str:
    return json.dumps(board, indent=2, ensure_ascii=False, sort_keys=True)



def _finish(
    args,
    boards,
    befores,
    changed,
    stats,
    since,
    since_src,
    budget,
    state,
) -> int:
    for kind in boards:
        if scrub_published(boards[kind]):
            changed[kind] = True
    labels = [("fl", "listings.json", "Florida"), ("tx", "texas.json", "Texas"), ("co", "colorado.json", "Colorado")]
    for kind, fname, _label in labels:
        if changed[kind]:
            write_if_changed({"fl": FL_PATH, "tx": TX_PATH, "co": CO_PATH}[kind], befores[kind], boards[kind], args.dry_run)
        else:
            print(f"{fname}: no change, not rewritten")
    rechecked = sum(stats[k]["rechecked"] for k in stats)
    added = sum(stats[k]["added"] for k in stats)
    removed = sum(stats[k]["removed"] for k in stats)
    updated = sum(stats[k]["updated"] for k in stats)
    print(f"rechecked {rechecked}, added {added}, removed {removed} (updated {updated})")
    old_cut = sum(stats[k].get("old_cut", 0) for k in stats)
    print(f"cutoff {since} ({since_src}): left off {old_cut} rows whose latest cut is older or undated")
    for kind, _fname, label in labels:
        examples = stats[kind].get("examples") or []
        if examples:
            print(f"{label} examples: " + " | ".join(examples[:5]))
    print(
        "lists: "
        + "; ".join(
            f"{KIND_STATE[kind]} {stats[kind]['cities']} cities / {stats[kind]['pages']} pages / {stats[kind]['rows']} rows"
            for kind, _fname, _label in labels
        )
    )
    print(f"pages requested {budget['used']} of the {budget['cap']} total cap; city cap {args.max_pages}")
    limited = bool(args.city or args.state or args.since)
    if limited:
        print("Limited run. Cities that were not named were not requested.")
    if budget["hit"]:
        skipped = sum(stats[k].get("cities_capped_out", 0) for k in stats)
        print(f"Total page cap reached; {skipped} cities were not requested.")
    if args.dry_run:
        print(f"{STATE_PATH.name}: dry run, cutoff not moved")
    elif limited or budget["hit"]:
        print(f"{STATE_PATH.name}: cutoff not moved (limited run or total page cap reached)")
    else:
        save_state(
            state,
            {
                "since": since,
                "pages": budget["used"],
                "added": added,
                "removed": removed,
                "updated": updated,
                "rechecked": rechecked,
                "left_off_old_cut": old_cut,
            },
        )
    for kind, _fname, label in labels:
        print(f"top {label}: {top_home(kind, boards[kind]['listings'])}")
    return 0


def empty_stats() -> dict:
    return {
        "cities": 0,
        "pages": 0,
        "rows": 0,
        "rechecked": 0,
        "updated": 0,
        "added": 0,
        "removed": 0,
        "skipped": 0,
        "old_cut": 0,
        "cities_capped_out": 0,
    }


def empty_co_board() -> dict:
    return {
        "state": "CO",
        "fetched": today_iso(),
        "timezone": "America/New_York",
        "scoreCap": 100,
        "count": 0,
        "originalCount": 0,
        "newCount": 0,
        "rules": (
            "Points only when the listing page shows the signal. Latest cut percent times 2.3, "
            "cap 25. Cuts on the current MLS number, 5 each, cap 15. DOM days/12, cap 10, using "
            "days on market when printed, otherwise days on Zillow. A listing checked on Redfin "
            "uses the day count that page printed. Drop from the first "
            "ask on the current listing, 1 point per percent, cap 10. Off the market then back, 12. "
            "Relisted under the prior ask, 8, only when that price is printed and lower. Pending or "
            "contingent then back, 12, not also off-then-back unless a separate removal is shown. "
            "Under the newest printed assessment or the last printed sale price, 6 once. Motivated "
            "remarks, 4. Half up to one decimal. Cap 100. Off the market, a relist under the prior "
            "ask, or a pending or contingent sale scores only when that event is dated within the "
            "12 months before this board's fetched date. Older history is ignored. A rent amount "
            "in the price history is not a price cut."
        ),
        "listings": [],
    }


def load_board_or_empty(path: Path, kind: str) -> tuple[dict, str]:
    if path.exists():
        return load_board(path)
    if kind != "co":
        raise SystemExit(f"{path.name} is missing. Nothing was requested.")
    board = empty_co_board()
    before = json.dumps(board, indent=2, ensure_ascii=False) + "\n"
    return board, before



def deepen_colorado_board(limit: int, delay: float = 1.0) -> int:
    """Deepen CO rows from Redfin listing pages (same rules as FL/TX).

    Runs deepen_colorado.py as a subprocess. Prefers the Playwright venv when
    present because Redfin listing HTML needs a real browser for the WAF.
    """
    if limit <= 0:
        return 0
    import subprocess

    script = ROOT / "deepen_colorado.py"
    if not script.exists():
        print(f"{script.name} is missing; skipped CO deepen.", file=sys.stderr)
        return 1
    candidates = [
        Path("/workspace/.venv-pw/bin/python"),
        ROOT / ".venv-pw" / "bin" / "python",
        Path(sys.executable),
    ]
    exe = next((str(p) for p in candidates if p.exists()), sys.executable)
    print(f"Deepening Colorado via {exe} {script.name} (limit {limit})...")
    proc = subprocess.run(
        [exe, str(script), "--limit", str(limit), "--delay", str(delay)],
        cwd=str(ROOT),
    )
    return int(proc.returncode)


def run(args) -> int:
    fl_board, fl_before = load_board(FL_PATH)
    tx_board, tx_before = load_board(TX_PATH)
    co_board, co_before = load_board_or_empty(CO_PATH, "co")
    boards = {"fl": fl_board, "tx": tx_board, "co": co_board}
    befores = {"fl": fl_before, "tx": tx_before, "co": co_before}
    snaps = {k: snapshot(boards[k]) for k in boards}
    state_filter = args.state.upper() if args.state else None
    state = load_state()
    since, since_src = resolve_since(args.since, state, fl_board, tx_board, co_board)
    print(f"Adding only homes whose latest cut is dated {since} or later ({since_src}).")
    budget = {"left": args.max_total_pages, "cap": args.max_total_pages, "used": 0, "hit": False}
    stats = {k: empty_stats() for k in boards}
    try:
        for kind, abbrev in (("fl", "FL"), ("tx", "TX"), ("co", "CO")):
            if state_filter in (None, abbrev):
                stats[kind] = refresh_board(
                    kind, boards[kind], args.city, args.max_pages, args.delay, since, budget
                )
    except ListBlocked as exc:
        print(f"Stopped. {exc} No files were written.", file=sys.stderr)
        return 2
    changed = {k: snapshot(boards[k]) != snaps[k] for k in boards}
    for kind in boards:
        if changed[kind]:
            counts_changed(kind, boards[kind])
    deepen_limit = getattr(args, "deepen_co", 0) or 0
    if deepen_limit and state_filter in (None, "CO") and not args.dry_run:
        # Reload board from disk only after list writes; deepen updates colorado.json itself.
        if changed["co"]:
            CO_PATH.write_text(json.dumps(boards["co"], indent=2, ensure_ascii=False) + "\n")
            befores["co"] = CO_PATH.read_text()
            changed["co"] = True
        code = deepen_colorado_board(deepen_limit, delay=args.delay)
        if code == 0 and CO_PATH.exists():
            boards["co"], befores["co"] = load_board(CO_PATH)
            changed["co"] = True
            counts_changed("co", boards["co"])
            stats["co"]["deepened"] = deepen_limit
        elif code not in (0, 2):
            print(f"Colorado deepen exited {code}; list refresh results are kept.", file=sys.stderr)
    return _finish(args, boards, befores, changed, stats, since, since_src, budget, state)



def self_test() -> int:
    html_missing = "<html><title>no list</title></html>"
    try:
        parse_search_html(html_missing, "https://example.test/price-reduced/")
    except ListBlocked as exc:
        print(f"self-test stop message: {exc}")
    else:
        print("self-test failed: missing list did not stop", file=sys.stderr)
        return 1

    obs = {
        "price": 255000,
        "change": -20000,
        "cut_dollars": 20000,
        "previous": 275000,
        "percent": (Decimal(20000) / Decimal(275000)) * Decimal(100),
        "cut_display": "7.3",
        "days": 28,
        "when": "Oct 1, 2026",
        "cut_date": "2026-10-01",
        "home_type": "SINGLE_FAMILY",
        "beds": 3,
        "baths": 2,
        "sqft": 1738,
        "zpid": "45313678",
        "url": "https://www.zillow.com/homedetails/example/45313678_zpid/",
        "street": "21 S MARY STREET",
        "city": "Eustis",
        "state": "FL",
        "zip": "32726",
        "exclude": None,
    }
    created = new_listing("fl", obs)
    again = new_listing("fl", obs)
    if created.get("verified_from") or created.get("verifiedFrom") or "note" in created:
        print("self-test failed: new listing published an internal note", file=sys.stderr)
        return 1
    if created.get("fetched") != today_iso():
        print("self-test failed: new listing missing fetched date", file=sys.stderr)
        return 1
    empty = empty_co_board()
    if any(key in empty for key in INTERNAL_BOARD_KEYS):
        print("self-test failed: empty Colorado board has an internal note", file=sys.stderr)
        return 1
    if created["score"] != again["score"] or created["latest_cut_percent"] != 7.3:
        print("self-test failed: new listing not stable", file=sys.stderr)
        return 1
    # 20000/275000*100*2.3 = 16.727... -> 16.7; DOM 28/12 = 2.333... -> 2.3; cuts 5.
    if created["score"] != 24.0:
        print(f"self-test failed: score {created['score']} != 24.0", file=sys.stderr)
        return 1
    board_row = json.loads(json.dumps(created))
    first = apply_to_existing("fl", board_row, obs)
    second = apply_to_existing("fl", board_row, obs)
    if first != "same" or second != "same":
        print(f"self-test failed: idempotent apply got {first}, {second}", file=sys.stderr)
        return 1
    land = dict(obs, exclude="land")
    if apply_to_existing("fl", board_row, land) != "remove":
        print("self-test failed: land was not removed", file=sys.stderr)
        return 1
    payload = {
        "props": {
            "pageProps": {
                "searchPageState": {
                    "regionState": {
                        "regionInfo": [{"regionName": "Eustis", "displayName": "Eustis FL"}]
                    },
                    "cat1": {
                        "searchResults": {
                            "listResults": [
                                {
                                    "zpid": "1",
                                    "detailUrl": "/homedetails/x/1_zpid/",
                                    "addressStreet": "1 MAIN ST",
                                    "addressCity": "Eustis",
                                    "addressState": "FL",
                                    "addressZipcode": "32726",
                                    "unformattedPrice": 100000,
                                    "hdpData": {
                                        "homeInfo": {
                                            "price": 100000,
                                            "priceChange": -10000,
                                            "daysOnZillow": 12,
                                            "homeType": "LOT",
                                            "homeStatus": "FOR_SALE",
                                            "city": "Eustis",
                                            "state": "FL",
                                            "zipcode": "32726",
                                            "streetAddress": "1 MAIN ST",
                                        }
                                    },
                                }
                            ]
                        },
                        "searchList": {"pagination": {}},
                    },
                    "categoryTotals": {"cat1": {"totalResultCount": 1}},
                }
            }
        }
    }
    html = '<script id="__NEXT_DATA__" type="application/json">' + json.dumps(payload) + "</script>"
    parsed = parse_search_html(html, "https://www.zillow.com/eustis-fl/price-reduced/")
    seen = observation_from_item(parsed["results"][0])
    if not seen or seen["exclude"] != "land":
        print(f"self-test failed: land list row {seen}", file=sys.stderr)
        return 1
    if not is_new_cut(obs, "2026-10-01") or not is_new_cut(obs, "2026-09-30"):
        print("self-test failed: cut on or after the cutoff was not new", file=sys.stderr)
        return 1
    if is_new_cut(obs, "2026-10-02") or is_new_cut(dict(obs, cut_date=None), "2026-09-01"):
        print("self-test failed: old or undated cut counted as new", file=sys.stderr)
        return 1
    if seen.get("cut_date") is not None:
        print("self-test failed: list row with no datePriceChanged got a cut date", file=sys.stderr)
        return 1
    community = excluded_reason(
        {"detailUrl": "/community/stonehaven/1_zpid/", "statusText": ""},
        {"homeStatus": "FOR_SALE", "homeType": "SINGLE_FAMILY", "price": 400000},
    )
    if community != "community floor plan":
        print(f"self-test failed: community row {community}", file=sys.stderr)
        return 1
    lot = excluded_reason(
        {"detailUrl": "/homedetails/x/4_zpid/", "statusText": ""},
        {
            "homeStatus": "FOR_SALE",
            "homeType": "MANUFACTURED",
            "price": 4999,
            "streetAddress": "7403 46th Ave N Lot 78",
            "livingArea": 960,
            "bedrooms": 2,
        },
    )
    if lot != "not a home":
        print(f"self-test failed: lot row {lot}", file=sys.stderr)
        return 1
    cheap_home = excluded_reason(
        {"detailUrl": "/homedetails/x/5_zpid/", "statusText": ""},
        {
            "homeStatus": "FOR_SALE",
            "homeType": "MANUFACTURED",
            "price": 7500,
            "streetAddress": "1280 Lakeview Rd 246",
            "livingArea": 672,
            "bedrooms": 2,
        },
    )
    if cheap_home is not None:
        print(f"self-test failed: manufactured home excluded ({cheap_home})", file=sys.stderr)
        return 1
    by_zpid = {created["zpid"]: created}
    by_addr = {match_key(obs["street"], obs["city"], obs["zip"]): created}
    if find_listing(dict(obs, zpid="999999"), by_zpid, by_addr) is not created:
        print("self-test failed: same address with a new zpid was not treated as a duplicate", file=sys.stderr)
        return 1
    mismatched = observation_from_item(
        {
            "zpid": "9",
            "detailUrl": "/homedetails/1408-Willow-Way-Windsor-CO-80550/9_zpid/",
            "hdpData": {
                "homeInfo": {
                    "price": 599900,
                    "priceChange": -15000,
                    "homeType": "SINGLE_FAMILY",
                    "homeStatus": "FOR_SALE",
                    "streetAddress": "201 Poudre Bay",
                    "city": "Windsor",
                    "state": "CO",
                    "zipcode": "80550",
                }
            },
        }
    )
    if not mismatched or mismatched["exclude"] != "link does not match address":
        print(f"self-test failed: mismatched link {mismatched}", file=sys.stderr)
        return 1
    if created.get("deepened"):
        print("self-test failed: new listing published deepened", file=sys.stderr)
        return 1
    print("self-test ok")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Refresh Lowball from Zillow price-reduced city lists.")
    parser.add_argument("--city", help="Only this city, and only if it is already on the board.")
    parser.add_argument("--state", choices=["FL", "TX", "CO", "fl", "tx", "co"], help="Only this state.")
    parser.add_argument(
        "--max-pages", type=int, default=DEFAULT_MAX_PAGES,
        help=f"Stop each city after this many list pages (default {DEFAULT_MAX_PAGES}).",
    )
    parser.add_argument(
        "--max-total-pages", type=int, default=DEFAULT_MAX_TOTAL_PAGES,
        help=f"Stop the whole run after this many list requests (default {DEFAULT_MAX_TOTAL_PAGES}).",
    )
    parser.add_argument(
        "--since",
        help="Add only cuts dated on or after this YYYY-MM-DD instead of refresh_state.json. Does not move the cutoff.",
    )
    parser.add_argument("--delay", type=float, default=1.0, help="Seconds between list requests.")
    parser.add_argument(
        "--deepen-co",
        type=int,
        default=0,
        metavar="N",
        help="After the CO list refresh, deepen up to N Colorado homes from Redfin listing pages "
        "(same multi-cut / off-market / remarks rules as FL/TX). 0 skips (default).",
    )
    parser.add_argument("--dry-run", action="store_true", help="Do not write JSON files.")
    parser.add_argument("--self-test", action="store_true", help="Run local checks and do not touch the boards.")
    args = parser.parse_args(argv)
    if args.self_test:
        return self_test()
    return run(args)


if __name__ == "__main__":
    sys.exit(main())
