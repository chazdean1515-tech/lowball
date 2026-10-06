#!/usr/bin/env python3
"""Shared publish rules for the Lowball boards.

A later refresh or Colorado deepen run calls prepare_board() before writing
JSON, so community floor plans, wrong links, duplicate addresses, rental
history, and relist or pending events older than 12 months do not come back.

The recency window is the 12 months before the board's fetched date. An
off-market return uses the removal date, and it counts only when the new
listing started after that removal. A pending or contingent return counts
when any of its pending dates falls in the window. A history price under
$20,000 on a home listed at $40,000 or more is treated as rent and is not a
price cut. The card price is the price after the latest cut.
"""

from __future__ import annotations

import calendar
import json
import re
from datetime import date, datetime, timedelta
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path
from urllib.parse import unquote

ROOT = Path(__file__).resolve().parent
RECENCY_DAYS = 365
RENT_UNDER = 20_000
SALE_LIST = 40_000
MIN_HOME_PRICE = 10_000
RULES_SENTENCE = (
    " Off the market, a relist under the prior ask, or a pending or contingent "
    "sale scores only when that event is dated within the 12 months before this "
    "board's fetched date. Older history is ignored. A relist counts only when "
    "the new listing started after the prior listing ended. A rent amount in the "
    "price history is not a price cut."
)
UNDATED_SENTENCE = (
    " Rows with no fetch date are earlier fetches and are listed after dated rows."
)
RELIST_LINE_RE = re.compile(
    r"^Prior listing removed (.+?), then this one started (.+?)\. \+12(?:\.0)?\.?$"
)
DROP_RE = re.compile(
    r"^(Down from .+? of )\$([0-9][0-9,]*)(.*?)(\. That is )[0-9.]+"
    r"(% off the original ask\. \+)[0-9.]+( of 10\.)$"
)
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
    "KEY": "KY",
}
MONTHS = {
    name: i
    for i, name in enumerate(
        ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"],
        1,
    )
}
POINT_RE = re.compile(r"\+\s*(\d+(?:\.\d+)?)(?:\s*of\s*\d+(?:\.\d+)?)?\.?\s*$")
FULL_DATE_RE = re.compile(
    r"(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\.?\s+(\d{1,2}),?\s+(\d{4})",
    re.I,
)
MONTH_YEAR_RE = re.compile(
    r"(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\s+(\d{4})",
    re.I,
)
PAIR_RE = re.compile(r"\$([0-9]{1,3}(?:,[0-9]{3})*) to \$([0-9]{1,3}(?:,[0-9]{3})*)")
LATEST_RE = re.compile(
    r"Latest cut is (?:([0-9.]+)%, )?from \$([0-9]{1,3}(?:,[0-9]{3})*) to \$([0-9]{1,3}(?:,[0-9]{3})*)"
    r"(?: on ([A-Z][a-z]{2,8}\.? \d{1,2}, \d{4}))?",
)
CUTS_RE = re.compile(
    r"^(\d+) cuts( on MLS #[A-Za-z0-9-]+| on this listing)? \((.*)\)\. "
    r"\+\d+(?:\.\d+)? of 15\.$"
)
QUOTE_RE = re.compile(r'Remarks say "(.*)"')
INTERNAL_ROW_KEYS = ("deepened", "verifiedFrom", "verified_from", "note")


def round1(value: Decimal) -> Decimal:
    return value.quantize(Decimal("0.1"), rounding=ROUND_HALF_UP)


def money(n: int) -> str:
    return f"${n:,}"


def norm_street(value: str) -> str:
    text = value.upper().replace("#", " ").replace("UNIT", " ")
    text = re.sub(r"[^A-Z0-9 ]", " ", text)
    parts = [STREET.get(part, part) for part in text.split()]
    return " ".join(parts)


def norm_place(value: str) -> str:
    text = (value or "").lower().replace(".", "")
    text = re.sub(r"\bsaint\b", "st", text)
    text = re.sub(r"\bfort\b", "ft", text)
    text = re.sub(r"\bmount\b", "mt", text)
    return re.sub(r"[^a-z0-9]+", " ", text).strip()


def street_of(kind: str, row: dict) -> str:
    address = str(row.get("address") or "")
    if kind == "fl":
        return address.split(",")[0].strip()
    return address.strip()


def address_key(kind: str, row: dict) -> tuple[str, str, str]:
    return (
        norm_street(street_of(kind, row)),
        norm_place(str(row.get("city") or "")),
        str(row.get("zip") or "").strip(),
    )


def address_street_number(street: str) -> str | None:
    match = re.match(r"\s*(\d+)", street or "")
    return match.group(1) if match else None


def url_street_number(url: str) -> str | None:
    text = unquote(url or "")
    match = re.search(r"/homedetails/([^/]+)/", text, re.I)
    slug = match.group(1) if match else ""
    if not slug:
        match = re.search(r"redfin\.com/[A-Za-z]{2}/[^/]+/([^/]+)/", text, re.I)
        slug = match.group(1) if match else ""
    if not slug:
        return None
    match = re.match(r"(\d+)", slug)
    return match.group(1) if match else None


def link_mismatches_address(kind: str, row: dict) -> bool:
    number = address_street_number(street_of(kind, row))
    linked = url_street_number(str(row.get("url") or ""))
    return bool(number and linked and number != linked)


def is_community_url(url: str) -> bool:
    return "/community/" in (url or "").lower()


def is_non_home_price(price) -> bool:
    try:
        amount = int(price)
    except (TypeError, ValueError):
        return False
    return 0 < amount < MIN_HOME_PRICE


def is_non_home(row: dict) -> bool:
    """A price under $10,000 is a lot unless the row is a manufactured home with living area."""
    if not is_non_home_price(row.get("price")):
        return False
    street = str(row.get("address") or "")
    if re.search(r"\blots?\b", street, re.I):
        return True
    try:
        sqft = int(row.get("sqft") or 0)
    except (TypeError, ValueError):
        sqft = 0
    try:
        beds = float(row.get("beds") or 0)
    except (TypeError, ValueError):
        beds = 0
    if sqft >= 400 and beds >= 1:
        return False
    return True


def is_rent_amount(amount, list_price) -> bool:
    try:
        amount = int(amount)
        list_price = int(list_price)
    except (TypeError, ValueError):
        return False
    return list_price >= SALE_LIST and 0 < amount < RENT_UNDER


def parse_dates(text: str) -> list[date]:
    found: list[date] = []
    for match in FULL_DATE_RE.finditer(text or ""):
        month = MONTHS[match.group(1)[:3].lower()]
        try:
            found.append(date(int(match.group(3)), month, int(match.group(2))))
        except ValueError:
            continue
    for match in MONTH_YEAR_RE.finditer(text or ""):
        # "Sep 14, 2026" is already a full date. This pattern is month + year only.
        tail = text[match.end(1) : match.start(2)]
        if re.search(r"\d", tail):
            continue
        month = MONTHS[match.group(1)[:3].lower()]
        year = int(match.group(2))
        try:
            last = calendar.monthrange(year, month)[1]
            found.append(date(year, month, last))
        except ValueError:
            continue
    return found


def reason_points(text: str) -> Decimal:
    match = POINT_RE.search(str(text).strip())
    if not match:
        return Decimal(0)
    return Decimal(match.group(1))


def score_from_reasons(reasons: list[str]) -> float:
    total = sum((reason_points(reason) for reason in reasons), Decimal(0))
    if total > 100:
        total = Decimal(100)
    return float(round1(total))


def board_cutoff(board: dict) -> date:
    raw = str(board.get("fetched") or "2026-10-05")[:10]
    try:
        as_of = date.fromisoformat(raw)
    except ValueError:
        as_of = date(2026, 10, 5)
    return as_of - timedelta(days=RECENCY_DAYS)


def within_window(when: date | None, cutoff: date) -> bool:
    return when is not None and when >= cutoff


def is_pending_reason(text: str) -> bool:
    lowered = text.lower()
    if not re.search(r"\+\s*12(?:\.0)?\.?\s*$", text):
        return False
    return "pending" in lowered or "contingent" in lowered


def is_relist_reason(text: str) -> bool:
    if is_pending_reason(text):
        return False
    lowered = text.lower()
    if not re.search(r"\+\s*12(?:\.0)?\.?\s*$", text):
        return False
    if lowered.startswith("latest cut") or "days on" in lowered:
        return False
    return any(
        token in lowered
        for token in ("removed", "relisted", "delisted", "off the market", "expired")
    )


def is_relist_companion(text: str) -> bool:
    lowered = text.lower()
    if lowered.startswith("came back") or lowered.startswith("relisted the same day"):
        return True
    return "relisted lower" in lowered or "relisted-lower" in lowered


def event_kept(text: str, cutoff: date) -> bool:
    dates = parse_dates(text)
    if not dates:
        return True
    if is_pending_reason(text):
        return any(when >= cutoff for when in dates)
    if is_relist_reason(text):
        return dates[0] >= cutoff
    return True


def came_back_amount(text: str) -> int | None:
    match = re.search(
        r"(?:came back at|relisted the same day at) \$([0-9,]+)",
        text,
        re.I,
    )
    if not match:
        return None
    return int(match.group(1).replace(",", ""))


def _token_core(token: str) -> str:
    return re.sub(r"[^A-Za-z]", "", token or "")


def clean_remark(snippet: str) -> str | None:
    """Trim a sliced quote onto word boundaries. A second pass leaves the result alone."""
    text = (snippet or "").strip()
    if not text:
        return None
    # A short quote that already ends cleanly is not a sliced fragment.
    if len(text) <= 80 and re.search(r"[.!?]$", text) and not re.match(r"[a-z]", text):
        return text
    # One leading fragment. Capitalize what remains so a later pass does not peel another word.
    if re.match(r"[a-z]", text):
        space = text.find(" ")
        if space == -1:
            return None
        text = text[space + 1 :].strip()
        if text and text[0].islower():
            text = text[0].upper() + text[1:]
    if text and not re.search(r"[.!?]$", text):
        parts = text.split()
        if parts:
            parts.pop()
        while parts and len(_token_core(parts[-1])) <= 2:
            parts.pop()
        text = " ".join(parts).strip()
        if text and not re.search(r"[.!?]$", text):
            text = text.rstrip(" -–—") + "."
    text = text.strip(" -–—")
    if len(text) < 12:
        return None
    return text


def apply_remarks(reasons: list[str]) -> list[str]:
    out = []
    for reason in reasons:
        match = QUOTE_RE.search(reason)
        if not match:
            out.append(reason)
            continue
        cleaned = clean_remark(match.group(1))
        if not cleaned:
            continue
        if cleaned == match.group(1):
            out.append(reason)
            continue
        out.append(reason[: match.start(1)] + cleaned + reason[match.end(1) :])
    return out


def apply_recency(reasons: list[str], cutoff: date, list_price) -> list[str]:
    out = []
    dropped_relist = False
    for reason in reasons:
        if is_pending_reason(reason) or is_relist_reason(reason):
            if event_kept(reason, cutoff):
                if is_relist_reason(reason):
                    dropped_relist = False
                out.append(reason)
            else:
                if is_relist_reason(reason):
                    dropped_relist = True
            continue
        if is_relist_companion(reason):
            amount = came_back_amount(reason)
            if dropped_relist or (amount is not None and is_rent_amount(amount, list_price)):
                continue
            dates = parse_dates(reason)
            if dates and dates[0] < cutoff:
                continue
        out.append(reason)
    return out


def _cut_points(percent: Decimal) -> Decimal:
    raw = percent * Decimal("2.3")
    if raw > 25:
        raw = Decimal(25)
    return round1(raw)


def _count_points(n_cuts: int) -> Decimal:
    raw = Decimal(5) * n_cuts
    if raw > 15:
        raw = Decimal(15)
    return round1(raw)


def apply_rental_cuts(reasons: list[str], list_price) -> tuple[list[str], dict]:
    """Drop rent-sized history from cut reasons. Returns reasons and field updates."""
    updates: dict = {}
    has_rent = any(
        is_rent_amount(int(amount.replace(",", "")), list_price)
        for pair in PAIR_RE.findall(" ".join(reasons))
        for amount in pair
    ) or any(
        (came_back_amount(reason) is not None and is_rent_amount(came_back_amount(reason), list_price))
        for reason in reasons
    )
    if not has_rent:
        return reasons, {}
    latest_idx = None
    cuts_idx = None
    latest = None
    sale_pairs: list[tuple[int, int]] = []
    cuts_prefix = ""
    for i, reason in enumerate(reasons):
        if reason.lower().startswith("latest cut") and latest is None:
            match = LATEST_RE.search(reason)
            if match:
                latest_idx = i
                latest = {
                    "pct": match.group(1),
                    "frm": int(match.group(2).replace(",", "")),
                    "to": int(match.group(3).replace(",", "")),
                    "date": match.group(4),
                }
        cuts_match = CUTS_RE.match(reason)
        if cuts_match:
            cuts_idx = i
            cuts_prefix = cuts_match.group(2) or ""
            for frm, to in PAIR_RE.findall(cuts_match.group(3)):
                pair = (int(frm.replace(",", "")), int(to.replace(",", "")))
                if is_rent_amount(pair[0], list_price) or is_rent_amount(pair[1], list_price):
                    continue
                sale_pairs.append(pair)
    latest_is_rent = bool(
        latest
        and (
            is_rent_amount(latest["frm"], list_price)
            or is_rent_amount(latest["to"], list_price)
        )
    )
    if cuts_idx is None and not latest_is_rent:
        return reasons, updates

    out = list(reasons)
    if cuts_idx is not None:
        if sale_pairs:
            n_cuts = len(sale_pairs)
            pts = _count_points(n_cuts)
            trail = ", ".join(f"{money(frm)} to {money(to)}" for frm, to in sale_pairs)
            label = "cut" if n_cuts == 1 else "cuts"
            out[cuts_idx] = f"{n_cuts} {label}{cuts_prefix} ({trail}). +{float(pts):.1f} of 15."
            updates["cuts"] = n_cuts
            updates["cuts_points"] = float(pts)
        else:
            out[cuts_idx] = ""
            updates["cuts"] = 0
            updates["cuts_points"] = 0
    if latest_is_rent:
        if sale_pairs:
            frm, to = sale_pairs[-1]
            pct = round1((Decimal(frm - to) / Decimal(frm)) * Decimal(100))
            pts = _cut_points(pct)
            dated = ""
            if latest and latest["date"] and latest["frm"] == frm and latest["to"] == to:
                dated = f" on {latest['date']}"
            out[latest_idx] = (
                f"Latest cut is {float(pct)}%, from {money(frm)} to {money(to)}{dated}. "
                f"Rent amounts in the history are not counted. +{float(pts):.1f} of 25."
            )
            updates["cut_percent"] = float(pct)
            updates["cut_dollars"] = frm - to
            updates["latest_points"] = float(pts)
        elif latest_idx is not None:
            out[latest_idx] = ""
            updates["cut_percent"] = 0
            updates["cut_dollars"] = 0
            updates["latest_points"] = 0
    out = [reason for reason in out if reason]
    # A rent-sized return is not "relisted lower," even when the relist itself is recent.
    kept = []
    for reason in out:
        amount = came_back_amount(reason)
        if amount is not None and is_rent_amount(amount, list_price):
            continue
        kept.append(reason)
    return kept, updates


def relist_started_after_removal(removed: str, started: str) -> bool:
    """A relist counts only when the new listing starts after the prior one ends."""
    removal_dates = parse_dates(removed or "")
    start_dates = parse_dates(started or "")
    if not removal_dates or not start_dates:
        return True
    return start_dates[0] >= removal_dates[0]


def relist_lines_overlap(text: str) -> bool:
    match = RELIST_LINE_RE.match((text or "").strip())
    if not match:
        return False
    return not relist_started_after_removal(match.group(1), match.group(2))


def apply_relist_order(reasons: list[str]) -> list[str]:
    """Drop the +12 relist, and its companion line, when the two listings overlapped."""
    out = []
    drop_companion = False
    for reason in reasons:
        if is_relist_reason(reason) and relist_lines_overlap(reason):
            drop_companion = True
            continue
        if drop_companion and is_relist_companion(reason):
            drop_companion = False
            continue
        drop_companion = False
        out.append(reason)
    return out


def repair_cut_pairs(pairs: list[tuple[int, int]]) -> list[tuple[int, int]]:
    """Drop a duplicated cut list, then chain two cuts that both start at the same price."""
    cleaned = list(pairs)
    while len(cleaned) >= 2 and len(cleaned) % 2 == 0:
        half = len(cleaned) // 2
        if cleaned[:half] != cleaned[half:]:
            break
        cleaned = cleaned[:half]
    deduped: list[tuple[int, int]] = []
    for pair in cleaned:
        if pair not in deduped:
            deduped.append(pair)
    chained: list[tuple[int, int]] = []
    for frm, to in deduped:
        if chained and frm == chained[-1][0] and to < chained[-1][1]:
            frm = chained[-1][1]
        if to < frm:
            chained.append((frm, to))
    return chained


def _rewrite_cuts_reason(reason: str, pairs: list[tuple[int, int]]) -> str:
    match = CUTS_RE.match(reason)
    if not match:
        return reason
    n_cuts = len(pairs)
    pts = _count_points(n_cuts)
    prefix = match.group(2) or ""
    trail = ", ".join(f"{money(frm)} to {money(to)}" for frm, to in pairs)
    label = "cut" if n_cuts == 1 else "cuts"
    return f"{n_cuts} {label}{prefix} ({trail}). +{float(pts):.1f} of 15."


def _latest_cut_line(frm: int, to: int, when: str | None) -> tuple[str, float, float]:
    pct = round1((Decimal(frm - to) / Decimal(frm)) * Decimal(100))
    pts = _cut_points(pct)
    dated = f" on {when}" if when else ""
    line = (
        f"Latest cut is {float(pct)}%, from {money(frm)} to {money(to)}{dated}. "
        f"+{float(pts)} of 25."
    )
    return line, float(pct), float(pts)


def _reprice_drop(reason: str, price: int) -> str | None:
    match = DROP_RE.match(reason)
    if not match:
        return reason
    first = int(match.group(2).replace(",", ""))
    if price >= first:
        return None
    pct = round1((Decimal(first - price) / Decimal(first)) * Decimal(100))
    pts = min(Decimal(10), pct)
    pts = round1(pts)
    return (
        f"{match.group(1)}{money(first)}{match.group(3)}{match.group(4)}"
        f"{float(pct)}{match.group(5)}{float(pts)}{match.group(6)}"
    )


def apply_listed_price(row: dict, reasons: list[str]) -> tuple[list[str], dict]:
    """Make the card price and the latest cut describe the same current price.

    A cut list that was pasted twice is kept once. Two cuts that both start at
    the same ask are chained, so the later one starts where the earlier one ended.
    """
    updates: dict = {}
    latest_idx = None
    latest = None
    cuts_idx = None
    for i, reason in enumerate(reasons):
        if latest is None and reason.lower().startswith("latest cut"):
            match = LATEST_RE.search(reason)
            if match and match.group(2) and match.group(3):
                latest_idx = i
                latest = {
                    "frm": int(match.group(2).replace(",", "")),
                    "to": int(match.group(3).replace(",", "")),
                    "date": match.group(4),
                }
        if cuts_idx is None and CUTS_RE.match(reason):
            cuts_idx = i
    out = list(reasons)
    chain_end = None
    if cuts_idx is not None:
        parsed = [
            (int(frm.replace(",", "")), int(to.replace(",", "")))
            for frm, to in PAIR_RE.findall(out[cuts_idx])
        ]
        repaired = repair_cut_pairs(parsed)
        if repaired and repaired != parsed:
            out[cuts_idx] = _rewrite_cuts_reason(out[cuts_idx], repaired)
            updates["cuts"] = len(repaired)
            updates["cuts_points"] = float(_count_points(len(repaired)))
            chain_end = repaired[-1]
    # Rewrite the latest-cut line only when the repaired chain ends at the same
    # price and the step that got there started lower than the printed "from".
    if (
        chain_end
        and latest
        and chain_end[1] == latest["to"]
        and chain_end[0] != latest["frm"]
    ):
        line, pct, pts = _latest_cut_line(chain_end[0], chain_end[1], latest["date"])
        out[latest_idx] = line
        latest = {"frm": chain_end[0], "to": chain_end[1], "date": latest["date"]}
        updates["cut_percent"] = pct
        updates["cut_dollars"] = chain_end[0] - chain_end[1]
        updates["latest_points"] = pts
    if not latest:
        return out, updates
    try:
        price = int(row.get("price"))
    except (TypeError, ValueError):
        return out, updates
    if price == latest["to"]:
        return out, updates
    out_price = latest["to"]
    updates["price"] = out_price
    repriced = []
    for reason in out:
        if reason.lower().startswith("down from "):
            revised = _reprice_drop(reason, out_price)
            if revised:
                repriced.append(revised)
            continue
        if reason.lower().startswith("list price is under"):
            amounts = [int(amount.replace(",", "")) for amount in re.findall(r"\$([0-9,]+)", reason)]
            if amounts and not any(out_price < amount for amount in amounts):
                continue
        repriced.append(reason)
    return repriced, updates


def _sync_points(row: dict, reasons: list[str], updates: dict) -> None:
    points = row.get("points")
    if not isinstance(points, dict):
        return
    managed = {
        "latestCut": lambda text: text.lower().startswith("latest cut"),
        "cuts": lambda text: " of 15" in text and "cut" in text.lower(),
        "fellThrough": is_pending_reason,
        "offThenBack": is_relist_reason,
        "relistedLower": lambda text: bool(came_back_amount(text)) or (
            text.lower().startswith("came back")
        ),
        "motivatedRemarks": lambda text: text.lower().startswith("remarks"),
    }
    rebuilt = dict(points)
    for key, pred in managed.items():
        matched = [reason for reason in reasons if pred(reason)]
        if matched:
            rebuilt[key] = float(sum((reason_points(reason) for reason in matched), Decimal(0)))
        else:
            rebuilt.pop(key, None)
    if "latest_points" in updates:
        if updates["latest_points"]:
            rebuilt["latestCut"] = updates["latest_points"]
        else:
            rebuilt.pop("latestCut", None)
    if "cuts_points" in updates:
        if updates["cuts_points"]:
            rebuilt["cuts"] = updates["cuts_points"]
        else:
            rebuilt.pop("cuts", None)
    row["points"] = rebuilt


def _apply_cut_fields(kind: str, row: dict, updates: dict) -> None:
    if "price" in updates:
        row["price"] = updates["price"]
    if "cut_percent" in updates:
        if kind == "fl":
            row["latest_cut_percent"] = updates["cut_percent"]
            row["latest_cut_dollars"] = updates.get("cut_dollars") or 0
        else:
            row["cutPercent"] = updates["cut_percent"]
    if "cuts" in updates and kind == "fl":
        row["cuts_counted"] = updates["cuts"]
    summary = row.get("cut_summary")
    if isinstance(summary, str) and "cut_percent" in updates:
        piece = (
            f"Latest cut {money(updates.get('cut_dollars') or 0)} ({updates['cut_percent']}%)"
            if updates["cut_percent"]
            else "No sale-price cut after rent amounts were set aside"
        )
        if summary.startswith("Latest cut") or summary.startswith("No price cut") or summary.startswith("No sale-price"):
            row["cut_summary"] = re.sub(r"^[^·]+", piece, summary, count=1)


def rescore_row(kind: str, row: dict, cutoff: date) -> bool:
    original = list(row.get("reasons") or [])
    reasons = apply_remarks(original)
    reasons = apply_recency(reasons, cutoff, row.get("price"))
    reasons, updates = apply_rental_cuts(reasons, row.get("price"))
    reasons = apply_relist_order(reasons)
    reasons, price_updates = apply_listed_price(row, reasons)
    updates.update(price_updates)
    changed = reasons != original or bool(updates)
    if not changed and "deepened" not in row:
        city = row.get("city")
        if isinstance(city, str) and city.isupper() and len(city) > 2:
            changed = True
        status = str(row.get("status") or "")
        if "status word not on" in status:
            changed = True
    if isinstance(row.get("city"), str) and row["city"].isupper() and len(row["city"]) > 2:
        row["city"] = row["city"].title()
    if "status word not on" in str(row.get("status") or ""):
        row["status"] = "For sale"
    for key in INTERNAL_ROW_KEYS:
        if key in row:
            row.pop(key, None)
            changed = True
    if reasons != list(row.get("reasons") or []):
        row["reasons"] = reasons
        changed = True
    new_score = score_from_reasons(reasons)
    old_score = row.get("score")
    try:
        same_score = f"{float(old_score):.1f}" == f"{new_score:.1f}"
    except (TypeError, ValueError):
        same_score = False
    if not same_score:
        row["score"] = new_score
        changed = True
    if isinstance(row.get("points"), dict):
        before_points = json.dumps(row["points"], sort_keys=True)
        _sync_points(row, reasons, updates)
        if json.dumps(row["points"], sort_keys=True) != before_points:
            changed = True
    if updates:
        _apply_cut_fields(kind, row, updates)
        changed = True
    return changed


def keep_rank(kind: str, row: dict) -> tuple:
    number = address_street_number(street_of(kind, row))
    linked = url_street_number(str(row.get("url") or ""))
    matches = 1 if not number or not linked or number == linked else 0
    try:
        zpid = int(row.get("zpid") or 0)
    except (TypeError, ValueError):
        zpid = 0
    return (
        matches,
        1 if row.get("source") == "Redfin" else 0,
        len(row.get("reasons") or []),
        1 if row.get("mls") else 0,
        1 if row.get("fetched") else 0,
        -zpid,
    )


def board_sort_key(row: dict) -> tuple:
    """Dated rows first, then score. A row with no fetch date is an earlier fetch."""
    dated = 0 if row.get("fetched") else 1
    try:
        score = float(row.get("score") or 0)
    except (TypeError, ValueError):
        score = 0.0
    try:
        dom = float(row.get("dom") or 0)
    except (TypeError, ValueError):
        dom = 0.0
    return (dated, -score, -dom, str(row.get("address") or ""))


def prepare_board(board: dict, kind: str) -> dict:
    """Filter and rescore one board in place. Returns counts for the summary."""
    cutoff = board_cutoff(board)
    stats = {
        "before": len(board.get("listings") or []),
        "community": 0,
        "non_home": 0,
        "bad_link": 0,
        "duplicate": 0,
        "rescored": 0,
        "cutoff": cutoff.isoformat(),
    }
    kept = []
    for row in board.get("listings") or []:
        if not isinstance(row, dict):
            continue
        url = str(row.get("url") or "")
        if is_community_url(url):
            stats["community"] += 1
            continue
        if is_non_home(row):
            stats["non_home"] += 1
            continue
        if link_mismatches_address(kind, row):
            stats["bad_link"] += 1
            continue
        kept.append(row)

    chosen: dict[tuple, dict] = {}
    order: list[tuple] = []
    for row in kept:
        key = address_key(kind, row)
        current = chosen.get(key)
        if current is None:
            chosen[key] = row
            order.append(key)
            continue
        stats["duplicate"] += 1
        if keep_rank(kind, row) > keep_rank(kind, current):
            chosen[key] = row
    listings = [chosen[key] for key in order]
    for row in listings:
        if rescore_row(kind, row, cutoff):
            stats["rescored"] += 1
    listings.sort(key=board_sort_key)
    board["listings"] = listings
    board["count"] = len(listings)
    if "original_count" in board:
        original = sum(1 for row in listings if row.get("original") is True)
        board["original_count"] = original
        board["new_count"] = board["count"] - original
    if "originalCount" in board:
        original = sum(1 for row in listings if not row.get("source"))
        board["originalCount"] = original
        board["newCount"] = board["count"] - original
    rules = board.get("rules")
    if isinstance(rules, str) and "12 months" not in rules:
        rules = rules.rstrip() + RULES_SENTENCE
    if isinstance(rules, str) and "after the prior listing ended" not in rules:
        rules = rules.rstrip() + (
            " A relist counts only when the new listing started after the prior listing ended."
            if "12 months" in rules
            else ""
        )
    if isinstance(rules, str) and "no fetch date" not in rules:
        rules = rules.rstrip() + UNDATED_SENTENCE
    if isinstance(rules, str):
        board["rules"] = rules
    stats["after"] = len(listings)
    stats["removed"] = stats["before"] - stats["after"]
    return stats


def self_test() -> int:
    cutoff = date(2026, 10, 5) - timedelta(days=365)
    old = "Pending or contingent, then back on the market (Jul 18, 2016). +12."
    recent = "Pending or contingent, then back on the market (Aug 6, 2026). +12."
    removal = "Prior listing removed Jun 7, 2013, then this one started Apr 23, 2026. +12."
    if event_kept(old, cutoff) or not event_kept(recent, cutoff) or event_kept(removal, cutoff):
        print("self-test failed: recency window", file=__import__("sys").stderr)
        return 1
    reasons = [
        "Latest cut is 99.5%, from $375,000 to $1,710 on Sep 30, 2026. +25.0 of 25.",
        "2 cuts on MLS #1 ($425,000 to $400,000, $375,000 to $1,710). +10.0 of 15.",
        "Came back at $1,710, under the prior $300,000 ask. +8.",
        removal,
        'Remarks say "esh Price. Fresh Opportunity. Motivated Sellers! value—th". +4.',
    ]
    row = {
        "address": "3834 Pecos St",
        "city": "Denver",
        "zip": "80211",
        "price": 375000,
        "reasons": reasons,
        "score": 49,
        "points": {"latestCut": 25, "cuts": 10, "relistedLower": 8, "offThenBack": 12, "motivatedRemarks": 4},
        "cutPercent": 99.5,
        "deepened": True,
    }
    rescore_row("co", row, cutoff)
    blob = " ".join(row["reasons"])
    if "1,710" in blob or "2013" in blob or "deepened" in row:
        print("self-test failed: rent or old event survived", blob, file=__import__("sys").stderr)
        return 1
    if row["cutPercent"] != 5.9:
        print("self-test failed: cut percent", row["cutPercent"], file=__import__("sys").stderr)
        return 1
    if abs(row["score"] - score_from_reasons(row["reasons"])) > 0.05:
        print("self-test failed: score", row["score"], row["reasons"], file=__import__("sys").stderr)
        return 1
    if not clean_remark("Motivated Seller."):
        print("self-test failed: short remark dropped", file=__import__("sys").stderr)
        return 1
    board = {
        "fetched": "2026-10-05",
        "count": 3,
        "originalCount": 0,
        "newCount": 3,
        "rules": "Points only when the listing page shows the signal.",
        "listings": [
            {
                "address": "The Ian Plan, Stonehaven",
                "city": "Seagoville",
                "zip": "75159",
                "price": 300000,
                "url": "https://www.zillow.com/community/stonehaven/1_zpid/",
                "reasons": ["Latest cut is 5.0%, from $300,000 to $285,000. +11.5 of 25."],
                "score": 11.5,
                "zpid": "1",
                "source": "Zillow",
            },
            {
                "address": "201 Poudre Bay",
                "city": "Windsor",
                "zip": "80550",
                "price": 599900,
                "url": "https://www.zillow.com/homedetails/201-Poudre-Bay-Windsor-CO-80550/2_zpid/",
                "reasons": ["Latest cut is 2.4%, from $614,900 to $599,900. +5.6 of 25."],
                "score": 5.6,
                "zpid": "2",
                "source": "Zillow",
            },
            {
                "address": "201 Poudre Bay",
                "city": "Windsor",
                "zip": "80550",
                "price": 599900,
                "url": "https://www.zillow.com/homedetails/1408-Willow-Way-Windsor-CO-80550/3_zpid/",
                "reasons": ["Latest cut is 2.4%, from $614,900 to $599,900. +5.6 of 25."],
                "score": 5.6,
                "zpid": "3",
                "source": "Zillow",
            },
            {
                "address": "7403 46th Ave N Lot 78",
                "city": "Saint Petersburg",
                "zip": "33709",
                "price": 4999,
                "sqft": 960,
                "beds": 2,
                "url": "https://www.zillow.com/homedetails/7403-46th-Ave-N-LOT-78/4_zpid/",
                "reasons": [],
                "score": 1,
                "zpid": "4",
                "source": "Zillow",
            },
            {
                "address": "1280 Lakeview Rd #246",
                "city": "Clearwater",
                "zip": "33756",
                "price": 7500,
                "sqft": 672,
                "beds": 2,
                "url": "https://www.zillow.com/homedetails/1280-Lakeview-Rd-246/5_zpid/",
                "reasons": [],
                "score": 1,
                "zpid": "5",
                "source": "Zillow",
            },
        ],
    }
    stats = prepare_board(board, "co")
    if stats["community"] != 1 or stats["non_home"] != 1 or stats["bad_link"] != 1:
        print("self-test failed: filters", stats, file=__import__("sys").stderr)
        return 1
    if len(board["listings"]) != 2 or any("Willow" in row["url"] for row in board["listings"]):
        print("self-test failed: dedupe", board["listings"], file=__import__("sys").stderr)
        return 1
    if "12 months" not in board["rules"]:
        print("self-test failed: rules sentence missing", file=__import__("sys").stderr)
        return 1
    again = prepare_board(board, "co")
    if again["removed"] or again["rescored"]:
        print("self-test failed: not idempotent", again, file=__import__("sys").stderr)
        return 1
    if "after the prior listing ended" not in board["rules"] or "no fetch date" not in board["rules"]:
        print("self-test failed: rules sentences", board["rules"], file=__import__("sys").stderr)
        return 1

    overlap = {
        "address": "1426 Rembrandt Rd",
        "city": "Boulder",
        "price": 2850000,
        "cutPercent": 19.3,
        "fetched": "2026-10-05",
        "reasons": [
            "Latest cut is 19.3%, from $2,850,000 to $2,300,000 on Sep 21, 2026. +25.0 of 25.",
            "2 cuts on MLS #6481690 ($2,850,000 to $2,350,000, $2,850,000 to $2,300,000). +10.0 of 15.",
            "514 days on Zillow. +10.0 of 10.",
            "Prior listing removed Apr 30, 2026, then this one started May 8, 2025. +12.",
            "Came back at $2,350,000, not under the prior $2,350,000 ask. Not lower, so the extra relist points are not added.",
        ],
        "score": 57,
        "points": {"latestCut": 25, "cuts": 10, "offThenBack": 12},
    }
    rescore_row("co", overlap, cutoff)
    overlap_blob = " ".join(overlap["reasons"])
    if "+12" in overlap_blob or "May 8, 2025" in overlap_blob:
        print("self-test failed: overlapping relist kept", overlap["reasons"], file=__import__("sys").stderr)
        return 1
    if overlap["price"] != 2300000 or "from $2,850,000 to $2,300,000" in overlap_blob:
        print("self-test failed: rembrandt price", overlap["price"], overlap["reasons"], file=__import__("sys").stderr)
        return 1
    if "from $2,350,000 to $2,300,000" not in overlap_blob:
        print("self-test failed: rembrandt chain", overlap["reasons"], file=__import__("sys").stderr)
        return 1
    kept_relist = {
        "address": "1 Same Day",
        "city": "Denver",
        "price": 500000,
        "reasons": [
            "Prior listing removed Jul 21, 2026, then this one started Jul 21, 2026. +12.",
            "Came back at $500,000, under the prior $600,000 ask. +8.",
        ],
        "score": 20,
    }
    rescore_row("co", kept_relist, cutoff)
    if not any(is_relist_reason(reason) for reason in kept_relist["reasons"]):
        print("self-test failed: same-day relist dropped", kept_relist["reasons"], file=__import__("sys").stderr)
        return 1
    stale = {
        "address": "2725 8Th St",
        "city": "Boulder",
        "price": 2058000,
        "cutPercent": 5.0,
        "reasons": [
            "Latest cut is 5.0%, from $2,058,000 to $1,956,000 on Sep 17, 2026. +11.4 of 25.",
            "3 cuts on MLS #4312354 ($2,200,000 to $2,058,000, $2,200,000 to $2,058,000, $2,058,000 to $1,956,000). +15.0 of 15.",
            "Down from the Jun 7, 2026 ask of $2,200,000. That is 6.5% off the original ask. +6.5 of 10.",
        ],
        "score": 32.9,
        "points": {"latestCut": 11.4, "cuts": 15, "dropFromFirstAsk": 6.5},
    }
    rescore_row("co", stale, cutoff)
    if stale["price"] != 1956000 or stale["reasons"][1].startswith("3 cuts"):
        print("self-test failed: duplicate cut or price", stale["price"], stale["reasons"], file=__import__("sys").stderr)
        return 1
    if "10.0 of 10" not in stale["reasons"][2]:
        print("self-test failed: drop not repriced", stale["reasons"], file=__import__("sys").stderr)
        return 1
    order_board = {
        "fetched": "2026-10-05",
        "rules": "Points only when the listing page shows the signal.",
        "listings": [
            {"address": "Old High", "city": "Austin", "price": 100000, "score": 70, "reasons": [], "dom": 10},
            {"address": "New Low", "city": "Austin", "price": 100000, "score": 20, "reasons": [], "fetched": "2026-10-03", "dom": 10},
        ],
    }
    prepare_board(order_board, "tx")
    if [row["address"] for row in order_board["listings"]] != ["New Low", "Old High"]:
        print("self-test failed: undated sort", order_board["listings"], file=__import__("sys").stderr)
        return 1
    print("listing_rules self-test ok")
    return 0


def main() -> int:
    import sys

    if "--self-test" in sys.argv:
        return self_test()
    files = {
        "fl": ROOT / "listings.json",
        "tx": ROOT / "texas.json",
        "co": ROOT / "colorado.json",
    }
    for kind, path in files.items():
        board = json.loads(path.read_text(encoding="utf-8"))
        stats = prepare_board(board, kind)
        path.write_text(json.dumps(board, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        from city_coords import sync_board_cities
        sync_board_cities(board, kind)
        print(
            f"{path.name}: {stats['before']} -> {stats['after']} "
            f"(community {stats['community']}, non-home {stats['non_home']}, "
            f"bad link {stats['bad_link']}, duplicate {stats['duplicate']}, "
            f"rescored {stats['rescored']}, cutoff {stats['cutoff']})"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
