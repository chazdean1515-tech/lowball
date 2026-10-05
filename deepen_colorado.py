#!/usr/bin/env python3
"""Deepen Colorado Lowball scores from Redfin listing pages (same rules as FL/TX).

Uses Playwright so Redfin's WAF challenge is satisfied. Resolves each CO home
to a Redfin URL via location-autocomplete (UI search fallback), loads the
listing HTML, and scores multi-cuts, drop from first ask, off-market/relist,
pending fall-through, assessment/sale-under, and motivated remarks.

Default target set: current top-score homes plus Denver / Aurora /
Colorado Springs / Fort Collins / Boulder, capped (see --limit).

  python3 deepen_colorado.py
  python3 deepen_colorado.py --limit 80 --delay 1.5
  python3 deepen_colorado.py --dry-run --limit 5
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
from datetime import datetime
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path
from zoneinfo import ZoneInfo
from urllib.parse import quote

ROOT = Path(__file__).resolve().parent
CO_PATH = ROOT / "colorado.json"
TZ = ZoneInfo("America/New_York")
PRIORITY_CITIES = {
    "Denver",
    "Aurora",
    "Colorado Springs",
    "Fort Collins",
    "Boulder",
}
UA = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
)
MOTIVATED_RE = re.compile(
    r"\b("
    r"motivated|bring\s+(?:all\s+)?offers?|priced?\s+to\s+sell|price\s+improved|"
    r"seller\s+financ|rent[\s-]?to[\s-]?own|must\s+sell|make\s+an?\s+offer|"
    r"offers?\s+welcome|vacant|already\s+moved|tenant|estate\s+sale|divorce|"
    r"relocati|reduced\s+\$?"
    r")\b",
    re.I,
)
PW_PYTHON = Path("/workspace/.venv-pw/bin/python")


def round1(value: Decimal) -> Decimal:
    return value.quantize(Decimal("0.1"), rounding=ROUND_HALF_UP)


def money(n: int) -> str:
    return f"${n:,}"


def today_iso() -> str:
    return datetime.now(TZ).date().isoformat()


def parse_price(text: str | None) -> int | None:
    if not text:
        return None
    m = re.search(r"\$([0-9,]+)", text)
    if not m:
        return None
    return int(m.group(1).replace(",", ""))


def unescape_js(s: str) -> str:
    try:
        return bytes(s, "utf-8").decode("unicode_escape")
    except Exception:
        return s.replace("\\n", " ").replace('\\"', '"')


def parse_redfin_html(html: str, url: str) -> dict:
    rows = re.findall(
        r'BasicTable__col date">(.*?)</div>'
        r'<div class="BasicTable__col event">(.*?)</div>'
        r'<div class="BasicTable__col price">(.*?)</div>',
        html,
        re.S,
    )
    events = []
    for date_h, event_h, price_h in rows:
        date = re.sub("<.*?>", "", date_h).strip()
        event = re.sub("<.*?>", "", event_h).strip()
        price_txt = re.sub(r"\s+", " ", re.sub("<.*?>", "", price_h)).strip()
        events.append(
            {
                "date": date,
                "event": event,
                "price": parse_price(price_txt),
                "raw_price": price_txt,
            }
        )

    desc = None
    for pat in (
        r'"listingRemarks":"(.*?)"',
        r'"marketingRemark":"(.*?)"',
        r'"remarks":"(.*?)"',
    ):
        m = re.search(pat, html)
        if m:
            desc = unescape_js(m.group(1))
            break
    if not desc:
        m = re.search(
            r'data-rf-test-id="listingRemarks"[^>]*>(.*?)</div>', html, re.S | re.I
        )
        if m:
            desc = re.sub("<.*?>", "", m.group(1))
            desc = re.sub(r"\s+", " ", desc).strip()

    assessments = [
        int(x.replace(",", "")) for x in re.findall(r'assessment">\$([0-9,]+)', html)
    ]
    assessment = assessments[0] if assessments else None
    assessment_year = None
    m = re.search(r'rollYear":(\d{4}).{0,80}taxableLandValue', html)
    if m:
        assessment_year = int(m.group(1))
    else:
        m = re.search(r'(20\d{2}).{0,40}assessment">\$', html)
        if m:
            assessment_year = int(m.group(1))

    dom = None
    dom_src = None
    m = re.search(r"(\d+)\s+days on Redfin", html, re.I)
    if m:
        dom = int(m.group(1))
        dom_src = "days on Redfin"
    if dom is None:
        m = re.search(r"(\d+)\s+days on market", html, re.I)
        if m:
            dom = int(m.group(1))
            dom_src = "days on market"

    price = None
    m = re.search(r'"priceInfo":\{"amount":(\d+)', html)
    if m:
        price = int(m.group(1))
    if price is None:
        m = re.search(r'itemprop="price"[^>]*content="(\d+)"', html)
        if m:
            price = int(m.group(1))
    if price is None:
        m = re.search(r'"price":(\d{5,})', html)
        if m:
            price = int(m.group(1))

    mls = None
    m = re.search(r"MLS#\s*([A-Za-z0-9-]+)", html)
    if m:
        mls = m.group(1)

    status = "for sale"
    if re.search(r'"searchStatus":\s*2\b', html) or re.search(
        r"\bPending\b", html[:80000]
    ):
        # only flip when header says pending
        m = re.search(r'data-rf-test-id="abp-status"[^>]*>(.*?)<', html, re.S)
        if m:
            st = re.sub("<.*?>", "", m.group(1)).strip().lower()
            if st:
                status = st

    return {
        "url": url,
        "events": events,
        "description": desc,
        "assessment": assessment,
        "assessmentYear": assessment_year,
        "dom": dom,
        "domSource": dom_src,
        "price": price,
        "mls": mls,
        "status": status,
        "ok": bool(events) or price is not None,
    }


def score_listing(listing: dict, page: dict) -> dict:
    """Apply FL/TX deepen rules to a CO listing from a Redfin page parse."""
    events = page["events"]
    list_price = page.get("price") or listing.get("price")
    reasons: list[str] = []
    points: dict = {}

    current: list[dict] = []
    prior: list[dict] = []
    past_current = False
    sold_price = None
    sold_date = None
    for ev in events:
        el = ev["event"].lower()
        if "sold" in el and sold_price is None and ev["price"]:
            sold_price = ev["price"]
            sold_date = ev["date"]
        if not past_current:
            if "listing removed" in el or el == "expired":
                past_current = True
                continue
            if "sold" in el:
                # public-records sold ends the current marketing history
                past_current = True
                continue
            current.append(ev)
        else:
            prior.append(ev)

    priced = [
        ev
        for ev in current
        if ev["price"]
        and any(
            w in ev["event"].lower()
            for w in ("price changed", "listed", "relisted", "price reduced")
        )
    ]
    chrono = list(reversed(priced))
    cuts = []
    first_ask = None
    first_ask_date = None
    for i, ev in enumerate(chrono):
        if first_ask is None:
            first_ask = ev["price"]
            first_ask_date = ev["date"]
        if "listed" in ev["event"].lower() and first_ask == ev["price"]:
            first_ask_date = ev["date"]
        if i == 0:
            continue
        prev = chrono[i - 1]["price"]
        if ev["price"] < prev:
            cuts.append({"from": prev, "to": ev["price"], "date": ev["date"]})

    # Latest cut
    if cuts:
        latest = cuts[-1]
        pct = (Decimal(latest["from"] - latest["to"]) / Decimal(latest["from"])) * Decimal(
            100
        )
        latest_pts = min(Decimal(25), round1(pct * Decimal("2.3")))
        points["latestCut"] = float(latest_pts)
        listing["cutPercent"] = float(round1(pct))
        reasons.append(
            f"Latest cut is {float(round1(pct))}%, from {money(latest['from'])} to "
            f"{money(latest['to'])} on {latest['date']}. +{float(latest_pts)} of 25."
        )
    elif listing.get("cutPercent"):
        pct = Decimal(str(listing["cutPercent"]))
        latest_pts = min(Decimal(25), round1(pct * Decimal("2.3")))
        points["latestCut"] = float(latest_pts)
        reasons.append(
            f"Latest cut is {float(round1(pct))}%. Listing page did not print a newer "
            f"decrease line, so the search-list cut is kept. +{float(latest_pts)} of 25."
        )

    n_cuts = len(cuts) if cuts else (1 if listing.get("cutPercent") else 0)
    cut_pts = min(Decimal(15), Decimal(5) * n_cuts) if n_cuts else Decimal(0)
    if n_cuts:
        points["cuts"] = float(cut_pts)
        mls = page.get("mls")
        mls_bit = f" on MLS #{mls}" if mls else " on this listing"
        if n_cuts == 1:
            when = f" on {cuts[0]['date']}" if cuts else ""
            reasons.append(
                f"The listing page shows one price decrease{when}. +5.0 of 15."
                if cuts
                else f"One cut{mls_bit}. +5.0 of 15."
            )
        else:
            trail = ""
            if cuts:
                trail = (
                    " ("
                    + ", ".join(f"{money(c['from'])} to {money(c['to'])}" for c in cuts)
                    + ")"
                )
            reasons.append(
                f"{n_cuts} cuts{mls_bit}{trail}. +{float(cut_pts)} of 15."
            )

    dom = page["dom"] if page.get("dom") is not None else listing.get("dom")
    dom_src = page.get("domSource") or listing.get("domSource") or "days on Zillow"
    if dom is not None:
        dom_pts = min(Decimal(10), round1(Decimal(dom) / Decimal(12)))
        points["dom"] = float(dom_pts)
        reasons.append(f"{dom} {dom_src}. +{float(dom_pts)} of 10.")
        listing["dom"] = dom
        listing["domSource"] = dom_src

    if first_ask and list_price and list_price < first_ask:
        drop_pct = (Decimal(first_ask - list_price) / Decimal(first_ask)) * Decimal(100)
        drop_pts = min(Decimal(10), round1(drop_pct))
        points["dropFromFirstAsk"] = float(drop_pts)
        when = f"the {first_ask_date} ask" if first_ask_date else "the first ask"
        reasons.append(
            f"Down from {when} of {money(first_ask)}. That is {float(round1(drop_pct))}% "
            f"off the original ask. +{float(drop_pts)} of 10."
        )

    # Off-market then back
    removals = [
        ev
        for ev in events
        if "listing removed" in ev["event"].lower() or ev["event"].lower() == "expired"
    ]
    current_listed = [ev for ev in current if "listed" in ev["event"].lower() and ev["price"]]
    prior_priced = [
        ev
        for ev in prior
        if ev["price"]
        and any(w in ev["event"].lower() for w in ("listed", "price changed"))
    ]

    # Fall-through detection (pending/contingent then back)
    chrono_all = list(reversed(events))
    saw_pending = False
    pending_dates: list[str] = []
    fell = False
    for ev in chrono_all:
        el = ev["event"].lower()
        if any(w in el for w in ("pending", "contingent")):
            saw_pending = True
            pending_dates.append(ev["date"])
        elif saw_pending and any(
            w in el for w in ("listed", "price changed", "relisted", "active")
        ):
            fell = True
            break

    off_then_back = bool(removals and current_listed and prior_priced)
    if fell:
        points["fellThrough"] = 12
        extra = f" ({', '.join(pending_dates)})" if pending_dates else ""
        reasons.append(
            f"Pending or contingent, then back on the market{extra}. +12."
        )
        # Fall-through is not also off-then-back unless a separate removal is shown
        if off_then_back and removals:
            # separate removal exists — keep both per rules
            pass
        else:
            off_then_back = False

    if off_then_back:
        points["offThenBack"] = 12
        rem_date = removals[0]["date"]
        # current listed is newest-first; take the chronological first Listed
        cur = list(reversed(current_listed))[0]
        prior_ask = prior_priced[0]["price"]  # newest prior ask
        reasons.append(
            f"Prior listing removed {rem_date}, then this one started {cur['date']}. +12."
        )
        if cur["price"] and prior_ask and cur["price"] < prior_ask:
            points["relistedLower"] = 8
            reasons.append(
                f"Came back at {money(cur['price'])}, under the prior {money(prior_ask)} ask. +8."
            )
        elif cur["price"] and prior_ask:
            reasons.append(
                f"Came back at {money(cur['price'])}, not under the prior {money(prior_ask)} "
                "ask. Not lower, so the extra relist points are not added."
            )
        else:
            reasons.append(
                "The relist line has no comparable prior ask price, so the extra "
                "relisted-lower points are not added."
            )

    under_bits = []
    if page.get("assessment") and list_price and list_price < page["assessment"]:
        yr = f"{page['assessmentYear']} " if page.get("assessmentYear") else ""
        under_bits.append(f"the {yr}assessment of {money(page['assessment'])}")
    if sold_price and list_price and list_price < sold_price:
        under_bits.append(f"the {sold_date} sale of {money(sold_price)}")
    if under_bits:
        points["underAssessmentOrSale"] = 6
        reasons.append(f"List price is under {' and '.join(under_bits)}. +6.")
    elif (
        sold_price
        and page.get("assessment")
        and list_price
        and list_price >= page["assessment"]
        and list_price < sold_price * 2  # only annotate when close/relevant
        and False
    ):
        pass

    desc = page.get("description") or ""
    if desc:
        m = MOTIVATED_RE.search(desc)
        if m:
            start = max(0, m.start() - 30)
            snippet = re.sub(r"\s+", " ", desc[start : m.end() + 70]).strip()
            points["motivatedRemarks"] = 4
            reasons.append(f'Remarks say "{snippet[:160]}". +4.')

    total = sum(Decimal(str(v)) for v in points.values())
    if total > 100:
        total = Decimal(100)
    listing = dict(listing)
    listing["score"] = float(round1(total))
    listing["points"] = points
    listing["reasons"] = reasons
    if list_price:
        listing["price"] = list_price
    listing["url"] = page["url"]
    listing["source"] = "Redfin"
    if page.get("mls"):
        listing["mls"] = page["mls"]
    if page.get("status"):
        listing["status"] = page["status"]
    listing.pop("verifiedFrom", None)
    listing.pop("verified_from", None)
    listing["fetched"] = today_iso()
    listing["deepened"] = True
    return listing


def select_targets(listings: list[dict], limit: int) -> list[dict]:
    """Prefer unscored-deep homes: top scores + priority metros."""
    def already(h):
        return bool(h.get("deepened")) or h.get("source") == "Redfin"

    remaining = [h for h in listings if not already(h)]
    top_score = sorted(remaining, key=lambda h: (-float(h.get("score") or 0), -(h.get("dom") or 0)))
    # Tier 1: score == max shallow (40) anywhere
    max_shallow = max((float(h.get("score") or 0) for h in remaining), default=0)
    tier1 = [h for h in top_score if float(h.get("score") or 0) >= max_shallow - 0.05]
    # Tier 2: priority metros by score
    tier2 = [
        h
        for h in top_score
        if h.get("city") in PRIORITY_CITIES and h not in tier1
    ]
    # Tier 3: everyone else by score
    tier3 = [h for h in top_score if h not in tier1 and h not in tier2]
    ordered = tier1 + tier2 + tier3
    # stable unique by id
    seen = set()
    out = []
    for h in ordered:
        key = h.get("zpid") or h.get("url") or (h.get("address"), h.get("zip"))
        if key in seen:
            continue
        seen.add(key)
        out.append(h)
        if len(out) >= limit:
            break
    return out


def deepen_with_playwright(targets: list[dict], delay: float, dry_run: bool) -> dict:
    from playwright.sync_api import sync_playwright

    stats = {
        "attempted": 0,
        "deepened": 0,
        "failed_resolve": 0,
        "failed_fetch": 0,
        "skipped": 0,
        "errors": [],
        "examples": [],
    }
    updates: dict[str, dict] = {}

    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=True, args=["--disable-blink-features=AutomationControlled"]
        )
        ctx = browser.new_context(
            user_agent=UA, viewport={"width": 1280, "height": 900}
        )
        page = ctx.new_page()
        page.goto("https://www.redfin.com/", wait_until="domcontentloaded", timeout=60000)
        page.wait_for_timeout(1500)

        def resolve(home: dict) -> str | None:
            addr = home.get("address") or ""
            city = home.get("city") or ""
            zipc = home.get("zip") or ""
            q = f"{addr}, {city}, CO {zipc}".strip()
            ac_url = (
                "https://www.redfin.com/stingray/do/location-autocomplete?"
                f"location={quote(q)}&v=2"
            )
            try:
                resp = page.request.get(ac_url, timeout=20000)
                if resp.status == 200:
                    text = resp.text()
                    if text.startswith("{}&&"):
                        text = text[4:]
                    data = json.loads(text)
                    em = (data.get("payload") or {}).get("exactMatch") or {}
                    path = em.get("url") or em.get("urlV2")
                    if path:
                        return "https://www.redfin.com" + path
                    sections = (data.get("payload") or {}).get("sections") or []
                    for sec in sections:
                        for row in sec.get("rows") or []:
                            if row.get("url"):
                                return "https://www.redfin.com" + row["url"]
            except Exception as exc:
                stats["errors"].append(f"autocomplete {q}: {exc}")

            # UI search fallback
            try:
                page.goto(
                    "https://www.redfin.com/", wait_until="domcontentloaded", timeout=60000
                )
                box = page.query_selector("#search-box-input")
                if not box:
                    return None
                box.click()
                box.fill(q)
                page.keyboard.press("Enter")
                page.wait_for_timeout(3500)
                if "/home/" in page.url:
                    return page.url.split("#")[0]
            except Exception as exc:
                stats["errors"].append(f"ui search {q}: {exc}")
            return None

        for home in targets:
            stats["attempted"] += 1
            key = str(home.get("zpid") or home.get("url"))
            label = f"{home.get('address')}, {home.get('city')}"
            try:
                url = resolve(home)
                if not url:
                    stats["failed_resolve"] += 1
                    print(f"  resolve-fail {label}")
                    continue
                page.goto(url, wait_until="domcontentloaded", timeout=60000)
                page.wait_for_timeout(2200)
                # Ensure history table is in DOM (sometimes below fold / tab)
                try:
                    tab = page.locator("text=Sale & tax history")
                    if tab.count():
                        tab.first.click(timeout=2500)
                        page.wait_for_timeout(800)
                except Exception:
                    pass
                html = page.content()
                if "BasicTable" not in html and "Price Changed" not in html:
                    # one retry after scroll
                    for _ in range(4):
                        page.mouse.wheel(0, 1600)
                        page.wait_for_timeout(400)
                    try:
                        tab = page.locator("text=Sale & tax history")
                        if tab.count():
                            tab.first.click(timeout=2500)
                            page.wait_for_timeout(1000)
                    except Exception:
                        pass
                    html = page.content()
                parsed = parse_redfin_html(html, page.url.split("#")[0])
                if not parsed["ok"] and not parsed["events"]:
                    stats["failed_fetch"] += 1
                    print(f"  fetch-fail {label} ({url})")
                    continue
                updated = score_listing(home, parsed)
                updates[key] = updated
                stats["deepened"] += 1
                if len(stats["examples"]) < 8:
                    stats["examples"].append(
                        {
                            "address": updated.get("address"),
                            "city": updated.get("city"),
                            "score": updated.get("score"),
                            "points": updated.get("points"),
                        }
                    )
                print(
                    f"  deepened {label} -> {updated['score']} "
                    f"({', '.join(updated.get('points', {}))})"
                )
            except Exception as exc:
                stats["failed_fetch"] += 1
                stats["errors"].append(f"{label}: {exc}")
                print(f"  error {label}: {exc}")
            if delay:
                time.sleep(delay)

        browser.close()
    stats["updates"] = updates
    return stats


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


def scrub_published(board: dict) -> None:
    """Drop scrape notes so a deepen run cannot write them back into colorado.json."""
    for key in ("note", "blocked", "leftOff"):
        board.pop(key, None)
    for row in board.get("listings") or []:
        if not isinstance(row, dict):
            continue
        narrative = None
        for old in ("verified_from", "verifiedFrom"):
            if old in row:
                narrative = row.pop(old)
        if narrative and not row.get("fetched"):
            found = extract_fetched(str(narrative))
            if found:
                row["fetched"] = found


def apply_updates(board: dict, updates: dict[str, dict]) -> int:
    n = 0
    for i, home in enumerate(board["listings"]):
        key = str(home.get("zpid") or home.get("url"))
        if key in updates:
            board["listings"][i] = updates[key]
            n += 1
    board["listings"].sort(
        key=lambda h: (-float(h.get("score") or 0), -(h.get("dom") or 0), h.get("address") or "")
    )
    board["fetched"] = today_iso()
    board["count"] = len(board["listings"])
    scrub_published(board)
    return n


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Deepen CO Lowball scores from Redfin pages.")
    parser.add_argument("--limit", type=int, default=120, help="Max homes to deepen (default 120).")
    parser.add_argument("--delay", type=float, default=1.0, help="Seconds between listing fetches.")
    parser.add_argument("--dry-run", action="store_true", help="Do not write colorado.json.")
    parser.add_argument(
        "--city",
        action="append",
        default=[],
        help="Only deepen this city (repeatable). Default: priority metros + top scores.",
    )
    args = parser.parse_args(argv)

    if not CO_PATH.exists():
        print(f"{CO_PATH} missing", file=sys.stderr)
        return 1

    board = json.loads(CO_PATH.read_text())
    listings = board["listings"]
    targets = select_targets(listings, args.limit)
    if args.city:
        wanted = {c.lower() for c in args.city}
        targets = [h for h in targets if (h.get("city") or "").lower() in wanted]
    print(f"Deepening up to {len(targets)} CO homes (limit {args.limit}).")
    if not targets:
        print("Nothing to deepen.")
        return 0

    stats = deepen_with_playwright(targets, args.delay, args.dry_run)
    print(
        f"Done. deepened={stats['deepened']} resolve_fail={stats['failed_resolve']} "
        f"fetch_fail={stats['failed_fetch']}"
    )
    for ex in stats["examples"]:
        print(f"  eg {ex['score']} {ex['city']} {ex['address']} {ex['points']}")

    if args.dry_run:
        print("Dry run; colorado.json not written.")
        return 0

    n = apply_updates(board, stats["updates"])
    CO_PATH.write_text(json.dumps(board, indent=2, ensure_ascii=False) + "\n")
    print(f"Wrote {CO_PATH.name} ({n} rows updated).")
    return 0 if stats["deepened"] else 2


if __name__ == "__main__":
    # Prefer the Playwright venv interpreter when invoked as python3 deepen_colorado.py
    if "playwright" not in sys.modules:
        try:
            import playwright  # noqa: F401
        except ImportError:
            if PW_PYTHON.exists() and Path(sys.executable).resolve() != PW_PYTHON.resolve():
                import os

                os.execv(str(PW_PYTHON), [str(PW_PYTHON), str(Path(__file__).resolve()), *sys.argv[1:]])
    sys.exit(main())
