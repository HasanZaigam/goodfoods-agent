"""Domain layer: restaurant search, recommendations, availability and reservation lifecycle.

Every public function returns plain JSON-serialisable dicts and raises ``ToolError`` for
anything the *guest* can fix (bad date, full slot, wrong phone...). The MCP server turns
those errors into structured tool results so the LLM can recover instead of crashing.
"""

from __future__ import annotations

import difflib
import json
import math
import re
import secrets
import sqlite3
import string
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Iterator

from goodfoods import config, dates
from goodfoods.data.seed import CONCEPTS, FEATURES, NEIGHBORHOODS, seed

CUISINES = sorted({c[0] for c in CONCEPTS.values()})
AREAS = sorted(NEIGHBORHOODS)

# Synonyms guests actually type -> canonical cuisine
CUISINE_SYNONYMS = {
    "indian": "North Indian", "punjabi": "North Indian", "mughlai": "Mughlai", "biryani": "Mughlai",
    "south indian": "South Indian", "dosa": "South Indian", "udupi": "South Indian",
    "chinese": "Chinese", "indo-chinese": "Chinese", "italian": "Italian", "pizza": "Italian",
    "pasta": "Italian", "japanese": "Japanese", "sushi": "Japanese", "mexican": "Mexican",
    "seafood": "Seafood", "fish": "Seafood", "coastal": "Seafood", "mangalorean": "Seafood",
    "continental": "Continental", "european": "Continental", "thai": "Thai",
    "mediterranean": "Mediterranean", "lebanese": "Mediterranean", "middle eastern": "Mediterranean",
    "bbq": "BBQ & Grill", "barbecue": "BBQ & Grill", "grill": "BBQ & Grill", "andhra": "Andhra",
    "kerala": "Kerala", "malabar": "Kerala", "vegan": "Healthy & Vegan", "healthy": "Healthy & Vegan",
    "salad": "Healthy & Vegan", "cafe": "Cafe", "coffee": "Cafe", "bakery": "Cafe",
    "asian": "Pan-Asian", "pan asian": "Pan-Asian", "korean": "Pan-Asian", "thali": "Rajasthani & Gujarati",
    "gujarati": "Rajasthani & Gujarati", "rajasthani": "Rajasthani & Gujarati",
}
AREA_SYNONYMS = {
    "hsr": "HSR Layout", "btm": "BTM Layout", "ecity": "Electronic City", "e-city": "Electronic City",
    "church street": "MG Road", "brigade road": "MG Road", "itpl": "Whitefield", "jp nagar": "JP Nagar",
    "jpnagar": "JP Nagar", "malleswaram": "Malleshwaram", "indira nagar": "Indiranagar",
}
OCCASION_FEATURES = {
    "date": ["rooftop", "live_music", "outdoor_seating"],
    "romantic": ["rooftop", "live_music", "outdoor_seating"],
    "anniversary": ["rooftop", "live_music", "private_dining"],
    "birthday": ["private_dining", "live_music", "bar"],
    "party": ["private_dining", "bar", "live_music"],
    "business": ["private_dining", "wheelchair_accessible", "parking"],
    "family": ["kid_friendly", "parking", "wheelchair_accessible"],
    "kids": ["kid_friendly", "parking"],
    "casual": [],
}
# Approximate neighborhood centres, used to offer the *nearest* area when the requested one has no match.
AREA_COORDS = {
    "Indiranagar": (12.9719, 77.6412), "Koramangala": (12.9352, 77.6245), "HSR Layout": (12.9116, 77.6474),
    "Whitefield": (12.9698, 77.7500), "JP Nagar": (12.9063, 77.5857), "Jayanagar": (12.9250, 77.5938),
    "MG Road": (12.9756, 77.6066), "Malleshwaram": (13.0031, 77.5643), "Electronic City": (12.8452, 77.6602),
    "Hebbal": (13.0358, 77.5970), "Marathahalli": (12.9569, 77.7011), "BTM Layout": (12.9166, 77.6101),
}
# Spellings guests type for the same dish; the dish itself is matched against our menus (signature dishes).
DISH_ALIASES = {"biriyani": "biryani", "biriani": "biryani", "briyani": "biryani", "biryaani": "biryani",
                "dosai": "dosa", "idly": "idli", "kabab": "kebab", "kabob": "kebab", "noodle": "noodles"}
DASH_RE = re.compile(r"[\u2010-\u2015\u2212\uFE63\uFF0D]")
BUDGET_TOLERANCE = 0.15  # up to 15% over the per-person budget still counts as "close to budget"


class ToolError(Exception):
    """A recoverable, guest-facing error. ``hint`` tells the agent what to do next."""

    def __init__(self, code: str, message: str, hint: str = "", **extra):
        super().__init__(message)
        self.code, self.message, self.hint, self.extra = code, message, hint, extra

    def to_dict(self) -> dict:
        out = {"ok": False, "error_code": self.code, "message": self.message}
        if self.hint:
            out["hint"] = self.hint
        out.update(self.extra)
        return out


# --------------------------------------------------------------------------- db
@contextmanager
def connect(db_path: Path | None = None) -> Iterator[sqlite3.Connection]:
    path = Path(db_path or config.DB_PATH)
    if not path.exists():
        seed(path)
    conn = sqlite3.connect(path, timeout=10, isolation_level=None)  # autocommit; explicit txns
    conn.row_factory = sqlite3.Row
    try:
        yield conn
    finally:
        conn.close()


def _now() -> datetime:
    return datetime.now(config.TIMEZONE)


def _restaurant_row(row: sqlite3.Row) -> dict:
    d = dict(row)
    d["features"] = json.loads(d["features"])
    d["signature_dishes"] = json.loads(d["signature_dishes"])
    d["veg_only"] = bool(d["veg_only"])
    return d


def _summary(r: dict) -> dict:
    return {
        "restaurant_id": r["id"],
        "name": r["name"],
        "neighborhood": r["neighborhood"],
        "cuisine": r["cuisine"],
        "rating": r["rating"],
        "avg_cost_for_two_inr": r["avg_cost_for_two"],
        "price_tier": "₹" * r["price_tier"],
        "pure_veg": r["veg_only"],
        "features": r["features"],
    }


# ------------------------------------------------------------------ normalisers
def _fuzzy(value: str, choices: list[str], synonyms: dict[str, str]) -> str | None:
    v = value.strip().lower()
    if not v:
        return None
    for c in choices:
        if c.lower() == v:
            return c
    if v in synonyms:
        return synonyms[v]
    for key, canon in synonyms.items():
        if key in v:
            return canon
    match = difflib.get_close_matches(v, [c.lower() for c in choices], n=1, cutoff=0.75)
    if match:
        return next(c for c in choices if c.lower() == match[0])
    return None


def normalize_cuisine(value: str | None) -> str | None:
    if not value:
        return None
    found = _fuzzy(value, CUISINES, CUISINE_SYNONYMS)
    if not found:
        raise ToolError("UNKNOWN_CUISINE", f"We don't serve '{value}' cuisine.", "Offer one of the available cuisines.",
                        available_cuisines=CUISINES)
    return found


def normalize_area(value: str | None) -> str | None:
    if not value:
        return None
    found = _fuzzy(value, AREAS, AREA_SYNONYMS)
    if not found:
        raise ToolError("UNKNOWN_NEIGHBORHOOD", f"GoodFoods has no outlet in '{value}'.",
                        "Suggest the nearest listed neighborhood.", available_neighborhoods=AREAS)
    return found


MONTHS = {m: i for i, m in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], start=1)}


def normalize_date(value: str, today: date | None = None) -> date:
    """Accepts ISO dates plus the phrases guests (and small LLMs) actually produce."""
    today = today or _now().date()
    raw = (value or "").strip().lower()
    if not raw:
        raise ToolError("MISSING_DATE", "A date is required.", "Ask the guest which date they want.")
    # Hinglish fillers around the date words ("kal raat", "shukravar ko")
    raw = re.sub(r"\b(raat|shaam|sham|subah|dopahar|ko|ke liye|on)\b", " ", raw).strip()
    raw = " ".join(raw.split())
    parsed = dates.resolve_relative(raw, today)  # today/kal/parso, this/next <weekday>, weekend (see dates.py)
    if parsed is None:
        try:
            parsed = date.fromisoformat(raw)
        except ValueError:
            pass
    if parsed is None:
        m = re.match(r"^(\d{1,2})[/\-.](\d{1,2})(?:[/\-.](\d{2,4}))?$", raw)  # DD/MM[/YYYY]
        if m:
            d_, mo, y = int(m.group(1)), int(m.group(2)), m.group(3)
            year = int(y) + (2000 if y and len(y) == 2 else 0) if y else today.year
            parsed = _safe_date(year, mo, d_, today, explicit_year=bool(y))
    if parsed is None:
        m = re.match(r"^(\d{1,2})(?:st|nd|rd|th)?\s+([a-z]+)\.?,?\s*(\d{4})?$", raw) or \
            re.match(r"^([a-z]+)\.?\s+(\d{1,2})(?:st|nd|rd|th)?,?\s*(\d{4})?$", raw)
        if m:
            a, b, y = m.groups()
            day_s, mon_s = (a, b) if a.isdigit() else (b, a)
            mo = MONTHS.get(mon_s[:3])
            if mo:
                parsed = _safe_date(int(y) if y else today.year, mo, int(day_s), today, explicit_year=bool(y))
    if parsed is None:
        raise ToolError("BAD_DATE", f"Could not understand the date '{value}'.",
                        "Ask the guest for the date again, e.g. 'tomorrow' or '2026-11-02'.")
    if parsed < today:
        raise ToolError("DATE_IN_PAST", f"{parsed.isoformat()} is in the past (today is {today.isoformat()}).",
                        "Ask the guest for a future date.")
    if parsed > today + timedelta(days=config.BOOKING_WINDOW_DAYS):
        raise ToolError("DATE_TOO_FAR", f"Bookings open only {config.BOOKING_WINDOW_DAYS} days ahead.",
                        "Ask for a nearer date.")
    return parsed


def _safe_date(year: int, month: int, day: int, today: date, explicit_year: bool) -> date | None:
    try:
        d = date(year, month, day)
    except ValueError:
        return None
    if not explicit_year and d < today:  # "5 Jan" said in December means next year
        try:
            d = date(year + 1, month, day)
        except ValueError:
            return None
    return d


def normalize_time(value: str) -> str:
    raw = (value or "").strip().lower().replace(".", ":")
    if not raw:
        raise ToolError("MISSING_TIME", "A time is required.", "Ask the guest what time they want.")
    named = {"noon": "12:00", "midnight": "00:00", "lunch": "13:00", "dinner": "20:00", "breakfast": "09:00"}
    if raw in named:
        raw = named[raw]
    # Hinglish / spoken forms: "raat 8 baje" -> 8 pm, "subah 9" -> 9 am, "saade 7" -> 7:30
    half = bool(re.search(r"\bsaa?de\b", raw))
    period = ("pm" if re.search(r"\b(raat|shaam|sham|evening|night|tonight)\b", raw)
              else "am" if re.search(r"\b(subah|morning)\b", raw)
              else "noon" if re.search(r"\b(dopahar|afternoon)\b", raw) else None)
    raw = re.sub(r"\b(raat|shaam|sham|evening|night|tonight|subah|morning|dopahar|afternoon|in the|at|"
                 r"baje|bajey|bje|o'?clock|saa?de|ko)\b", " ", raw).strip()
    raw = " ".join(raw.split())
    if half and re.fullmatch(r"\d{1,2}", raw):
        raw += ":30"
    if period and not re.search(r"(am|pm)$", raw) and re.match(r"^\d{1,2}(:\d{2})?$", raw):
        hour = int(raw.split(":")[0])
        if period == "pm" or (period == "noon" and 1 <= hour <= 6):
            raw += "pm"
        elif period == "am":
            raw += "am"
    m = re.match(r"^(\d{1,2})(?::?(\d{2}))?\s*(am|pm)?$", raw.replace(" ", "") if "m" in raw else raw)
    if not m:
        raise ToolError("BAD_TIME", f"Could not understand the time '{value}'.",
                        "Ask for a time like '7:30 pm' or '19:30'.")
    hour, minute, ampm = int(m.group(1)), int(m.group(2) or 0), m.group(3)
    if ampm == "pm" and hour < 12:
        hour += 12
    if ampm == "am" and hour == 12:
        hour = 0
    if not ampm and 1 <= hour <= 10 and not raw.startswith("0"):  # "table at 8" means 8 pm; "08:00" stays
        hour += 12
    if hour > 23 or minute > 59:
        raise ToolError("BAD_TIME", f"'{value}' is not a valid time.", "Ask for a valid time.")
    # snap to the booking grid
    total = hour * 60 + minute
    total = int(round(total / config.SLOT_MINUTES) * config.SLOT_MINUTES) % (24 * 60)
    return f"{total // 60:02d}:{total % 60:02d}"


def normalize_phone(value: str) -> str:
    digits = re.sub(r"\D", "", value or "")
    if digits.startswith("91") and len(digits) == 12:
        digits = digits[2:]
    if digits.startswith("0") and len(digits) == 11:
        digits = digits[1:]
    if not re.fullmatch(r"[6-9]\d{9}", digits):
        raise ToolError("BAD_PHONE", f"'{value}' is not a valid Indian mobile number "
                                     "(10 digits, starting with 6, 7, 8 or 9).",
                        "Ask the guest to re-share their 10-digit mobile number.")
    return digits


def normalize_party(value: int | str) -> int:
    try:
        n = int(value) if not isinstance(value, str) else int(re.search(r"\d+", value).group())
    except (TypeError, ValueError, AttributeError):
        raise ToolError("BAD_PARTY_SIZE", f"'{value}' is not a valid number of guests.", "Ask how many people.")
    if n < 1:
        raise ToolError("BAD_PARTY_SIZE", "Party size must be at least 1.", "Ask how many people.")
    if n > config.MAX_ONLINE_PARTY:
        raise ToolError("PARTY_TOO_LARGE",
                        f"Groups of more than {config.MAX_ONLINE_PARTY} guests can't be booked online; our events "
                        f"team handles them: {config.EVENTS_CONTACT}.",
                        "Reply now: tell the guest their group of this size is handled by the events team and give "
                        "events_contact exactly. Do not ask any follow-up questions and do not call other tools.",
                        party_size=n, max_online_party=config.MAX_ONLINE_PARTY, events_contact=config.EVENTS_CONTACT)
    return n


def reject_past_slot(day: date, hhmm: str) -> None:
    """A time earlier today is a guest error (clear message), not just "unavailable"."""
    now = _now()
    if day == now.date() and _minutes(hhmm) <= now.hour * 60 + now.minute:
        raise ToolError("TIME_IN_PAST", f"{hhmm} today has already passed (it is {now:%H:%M} now).",
                        "Ask the guest for a later time today or another date.")


# ------------------------------------------------------------- availability core
def _minutes(hhmm: str) -> int:
    h, m = map(int, hhmm.split(":"))
    return h * 60 + m


def _hours(r: dict) -> tuple[int, int]:
    open_m, close_m = _minutes(r["open_time"]), _minutes(r["close_time"])
    if close_m <= open_m:  # closes at/after midnight
        close_m += 24 * 60
    return open_m, close_m


def _is_open_for(r: dict, hhmm: str) -> bool:
    open_m, close_m = _hours(r)
    t = _minutes(hhmm)
    last_seating = close_m - 60
    return open_m <= t <= last_seating


def _seats_taken(conn: sqlite3.Connection, rid: str, day: str, hhmm: str, exclude_code: str | None = None) -> int:
    start = _minutes(hhmm)
    end = start + config.DINING_DURATION_MIN
    rows = conn.execute(
        "SELECT time, party_size, code FROM reservations WHERE restaurant_id=? AND date=? AND status='confirmed'",
        (rid, day),
    ).fetchall()
    taken = 0
    for row in rows:
        if exclude_code and row["code"] == exclude_code:
            continue
        s = _minutes(row["time"])
        if s < end and start < s + config.DINING_DURATION_MIN:
            taken += row["party_size"]
    return taken


def _check(conn, r: dict, day: date, hhmm: str, party: int, exclude_code: str | None = None) -> tuple[bool, str]:
    if not _is_open_for(r, hhmm):
        return False, f"outside opening hours ({r['open_time']}-{r['close_time']}, last seating 1h before close)"
    if day == _now().date() and _minutes(hhmm) < _now().hour * 60 + _now().minute + 30:
        return False, "that time has already passed or is less than 30 minutes away"
    if party > config.LARGE_PARTY_THRESHOLD and "private_dining" not in r["features"]:
        return False, f"parties above {config.LARGE_PARTY_THRESHOLD} need a venue with private dining"
    free = r["capacity"] - _seats_taken(conn, r["id"], day.isoformat(), hhmm, exclude_code)
    if free < party:
        return False, "fully booked at that time"
    return True, "available"


def _nearby_times(conn, r: dict, day: date, hhmm: str, party: int, exclude_code: str | None = None) -> list[str]:
    base = _minutes(hhmm)
    out = []
    for delta in (30, -30, 60, -60, 90, -90, 120, -120):
        t = base + delta
        if 0 <= t < 24 * 60:
            cand = f"{t // 60:02d}:{t % 60:02d}"
            if _check(conn, r, day, cand, party, exclude_code)[0]:
                out.append(cand)
        if len(out) == 3:
            break
    return sorted(out)


# --------------------------------------------------------------------- lookups
def _all_restaurants(conn) -> list[dict]:
    return [_restaurant_row(r) for r in conn.execute("SELECT * FROM restaurants ORDER BY id")]


def _name_key(name: str) -> str:
    return " ".join(DASH_RE.sub("-", name).replace("-", " - ").lower().split())


def resolve_restaurant(conn, identifier: str) -> dict:
    ident = (identifier or "").strip()
    if not ident:
        raise ToolError("MISSING_RESTAURANT", "Restaurant is required.", "Search first, then use restaurant_id.")
    row = conn.execute("SELECT * FROM restaurants WHERE upper(id)=?", (ident.upper(),)).fetchone()
    if row:
        return _restaurant_row(row)
    # small models sometimes pass the name instead of the id, often with a typographic dash ("Cantina – MG Road")
    all_r = _all_restaurants(conn)
    low = _name_key(ident)
    exact = [r for r in all_r if _name_key(r["name"]) in (low, "goodfoods " + low)]
    hits = exact or [r for r in all_r if low in _name_key(r["name"])]
    if not hits:
        names = {_name_key(r["name"]): r for r in all_r}
        close = difflib.get_close_matches(low, list(names), n=3, cutoff=0.6)
        hits = [names[c] for c in close]
    if len(hits) == 1:
        return hits[0]
    if hits:
        raise ToolError("AMBIGUOUS_RESTAURANT", f"'{identifier}' matches several outlets.",
                        "Ask the guest which one they mean.", candidates=[_summary(h) for h in hits[:5]])
    raise ToolError("RESTAURANT_NOT_FOUND", f"No GoodFoods outlet matches '{identifier}'.",
                    "Use search_restaurants to find a valid restaurant_id.")


# ----------------------------------------------------------------- public API
def search_restaurants(
    cuisine: str | None = None,
    neighborhood: str | None = None,
    max_cost_for_two: int | None = None,
    min_rating: float | None = None,
    features: list[str] | None = None,
    pure_veg: bool | None = None,
    query: str | None = None,
    limit: int = 5,
    db_path: Path | None = None,
) -> dict:
    cuisine, area = normalize_cuisine(cuisine), normalize_area(neighborhood)
    want_feats = [f for f in (features or []) if f in FEATURES]
    unknown_feats = [f for f in (features or []) if f not in FEATURES]
    limit = max(1, min(int(limit or 5), 10))
    with connect(db_path) as conn:
        results = []
        for r in _all_restaurants(conn):
            if cuisine and r["cuisine"] != cuisine:
                continue
            if area and r["neighborhood"] != area:
                continue
            if max_cost_for_two and r["avg_cost_for_two"] > int(max_cost_for_two):
                continue
            if min_rating and r["rating"] < float(min_rating):
                continue
            if pure_veg and not r["veg_only"]:
                continue
            if any(f not in r["features"] for f in want_feats):
                continue
            if query:
                hay = " ".join([r["name"], r["cuisine"], r["neighborhood"], *r["signature_dishes"]]).lower()
                if not all(tok in hay for tok in query.lower().split()):
                    continue
            results.append(r)
        results.sort(key=lambda r: (-r["rating"], r["avg_cost_for_two"]))
    out = {
        "ok": True,
        "total_matches": len(results),
        "restaurants": [{"option": n, **_summary(r)} for n, r in enumerate(results[:limit], 1)],
    }
    if unknown_feats:
        out["ignored_features"] = unknown_feats
        out["valid_features"] = FEATURES
    if not results:
        out["hint"] = "No exact match. Relax one filter (area, budget or rating) and search again."
    return out


def _distance_km(a: str, b: str) -> float:
    (lat1, lon1), (lat2, lon2) = AREA_COORDS[a], AREA_COORDS[b]
    dlat, dlon = math.radians(lat2 - lat1), math.radians(lon2 - lon1)
    h = math.sin(dlat / 2) ** 2 + math.cos(math.radians(lat1)) * math.cos(math.radians(lat2)) * math.sin(dlon / 2) ** 2
    return round(6371 * 2 * math.asin(math.sqrt(h)), 1)


def dish_cuisines(dish: str) -> tuple[str, list[str]]:
    """(canonical dish, cuisines whose menus list it). Driven by the data, e.g. biryani -> ['Mughlai']."""
    d = " ".join((dish or "").strip().lower().split())
    d = " ".join(DISH_ALIASES.get(w, w) for w in d.split())
    hits = sorted({c for c, _, dishes, _ in CONCEPTS.values() if any(d in x for x in dishes)})
    if not hits and d in CUISINE_SYNONYMS:  # e.g. "pasta" is not a listed dish but clearly Italian
        hits = [CUISINE_SYNONYMS[d]]
    return d, hits


def recommend_restaurants(
    party_size: int | None = None,
    date_: str | None = None,
    time_: str | None = None,
    cuisine: str | None = None,
    neighborhood: str | None = None,
    budget_per_person: int | None = None,
    occasion: str | None = None,
    features: list[str] | None = None,
    pure_veg: bool | None = None,
    dish: str | None = None,
    limit: int = 3,
    db_path: Path | None = None,
) -> dict:
    party = normalize_party(party_size) if party_size else None  # first: >20 guests go to the events team
    notes: dict[str, str] = {}
    dish_l, cuisines = (dish_cuisines(dish) if dish and dish.strip() else ("", []))
    if dish_l:
        # a named dish is the strongest signal; a cuisine the model guessed on top of it is dropped
        guessed = _fuzzy(cuisine, CUISINES, CUISINE_SYNONYMS) if cuisine else None
        if not cuisines:
            notes["dish_note"] = (f"No GoodFoods menu lists '{dish}'. Tell the guest plainly; do not claim any "
                                  "outlet serves it.")
        elif guessed in cuisines:
            cuisines = [guessed]
        elif cuisine:
            notes["dish_note"] = (f"'{dish}' is on the menu at our {'/'.join(cuisines)} outlets; "
                                  f"cuisine '{cuisine}' was ignored.")
    else:
        c = normalize_cuisine(cuisine)
        cuisines = [c] if c else []
    area = normalize_area(neighborhood)
    day = normalize_date(date_) if date_ else None
    slot = normalize_time(time_) if time_ else None
    if day and slot:
        reject_past_slot(day, slot)
    budget = int(budget_per_person) if budget_per_person else None
    occ = (occasion or "").lower()
    occ_feats = next((v for k, v in OCCASION_FEATURES.items() if k in occ), [])
    want_feats = [f for f in (features or []) if f in FEATURES]
    limit = max(1, min(int(limit or 3), 5))
    check_slots = bool(day and slot and party)

    scored, unavailable = [], 0
    with connect(db_path) as conn:
        for r in _all_restaurants(conn):
            if pure_veg and not r["veg_only"]:
                continue
            if cuisines and r["cuisine"] not in cuisines:
                continue
            if dish_l and not cuisines:
                continue  # nobody serves it: return nothing rather than something unrelated
            reasons, score = [], r["rating"] * 20
            if cuisines:
                reasons.append(f"serves {r['cuisine']}")
            if dish_l:
                listed = [d for d in r["signature_dishes"] if dish_l in d]
                if listed:
                    reasons.append(f"known for {listed[0]}")
            distance = None
            if area:
                distance = _distance_km(area, r["neighborhood"])
                if r["neighborhood"] == area:
                    score += 20
                    reasons.append(f"in {area}")
                else:
                    score -= min(35, 5 + 2 * distance)  # nearer areas rank higher
            per_person = r["avg_cost_for_two"] / 2
            fit, over_budget = None, False
            if budget:
                ratio = per_person / budget
                over_budget = ratio > 1  # sorted below every within-budget outlet, whatever its bonuses
                if ratio > 1 + BUDGET_TOLERANCE:
                    score -= min(40, (ratio - 1) * 50)
                    fit = "above budget"
                elif over_budget:
                    score -= 50 * (ratio - 1)
                else:
                    score += 30 - 40 * (1 - ratio)  # within budget: closest to what the guest wants to spend ranks highest
                if fit is None:
                    fit = ("slightly above budget" if ratio > 1 else
                           "close to budget" if ratio >= 0.75 else "well below budget")
                    reasons.append(f"about ₹{int(per_person)}/person, {fit}")
            for f in want_feats:
                if f in r["features"]:
                    score += 10
                    reasons.append(f.replace("_", " "))
                else:
                    score -= 12
            matched_occ = [f for f in occ_feats if f in r["features"]]
            if matched_occ:
                score += 6 * len(matched_occ)
                reasons.append(f"good for {occ}: {', '.join(m.replace('_', ' ') for m in matched_occ)}")
            if "business" in occ and "live_music" in r["features"]:
                score -= 8
            if check_slots:
                ok, _ = _check(conn, r, day, slot, party)
                if not ok:
                    unavailable += 1
                    continue
            elif party and party > config.LARGE_PARTY_THRESHOLD and "private_dining" not in r["features"]:
                continue
            reasons.append(f"rated {r['rating']}")
            scored.append((over_budget, score, r, reasons, distance, fit))
    scored.sort(key=lambda x: (x[0], -x[1]))  # budget first: an over-budget outlet never outranks one within it
    recs = []
    for n, (_, score, r, reasons, distance, fit) in enumerate(scored[:limit], 1):
        item = {"option": n, **_summary(r)}  # numbered so "the first one" maps to an exact restaurant_id
        item["cost_per_person_inr"] = int(r["avg_cost_for_two"] / 2)
        item["why"] = "; ".join(reasons)
        item["signature_dishes"] = r["signature_dishes"]
        if area:
            item["in_requested_area"] = r["neighborhood"] == area
            if r["neighborhood"] != area:
                item["distance_from_requested_area_km"] = distance
        if fit:
            item["budget_fit"] = fit
        if check_slots:
            item["availability"] = f"table for {party} free on {day.isoformat()} at {slot}"
        recs.append(item)

    query = {"date": day.isoformat() if day else None, "weekday": day.strftime("%A") if day else None,
             "time": slot, "party_size": party, "neighborhood": area, "dish": dish_l or None,
             "cuisines": cuisines or None, "budget_per_person": budget}
    out: dict = {"ok": True, "query": {k: v for k, v in query.items() if v is not None}, "recommendations": recs,
                 "availability_checked": check_slots}
    if day and dates.describe(str(date_), day):
        out["date_interpreted"] = dates.describe(str(date_), day)
    out.update(notes)
    if not check_slots:
        missing = [k for k, v in (("date", day), ("time", slot), ("party_size", party)) if not v]
        out["availability_note"] = (f"Availability NOT checked: missing {', '.join(missing)}. If the guest gave it "
                                    "(e.g. 'kal' = tomorrow, '8 baje' = 20:00), call again with it; otherwise do not "
                                    "say a table is free.")
    if area and recs and not any(r["neighborhood"] == area for r in recs):
        out["nearest_area_fallback"] = True
        nearest = min(recs, key=lambda x: x["distance_from_requested_area_km"])
        out["area_note"] = (f"Nothing matching in {area}. Closest option: {nearest['name']} in "
                            f"{nearest['neighborhood']} (~{nearest['distance_from_requested_area_km']} km away). "
                            f"Tell the guest it is not in {area} and offer these.")
    if budget and recs and all(r["cost_per_person_inr"] <= budget for r in recs):
        lo, hi = min(r["cost_per_person_inr"] for r in recs), max(r["cost_per_person_inr"] for r in recs)
        out["budget_note"] = f"All suggestions are within the ₹{budget}/person budget (₹{lo}-₹{hi} per person)."
    if unavailable:
        out["note"] = f"{unavailable} otherwise-matching outlets are full or closed at that time."
    if not recs:
        out["hint"] = "Nothing fits every constraint. Say so plainly and suggest relaxing area, time or budget."
    return out


def get_restaurant_details(restaurant_id: str, db_path: Path | None = None) -> dict:
    with connect(db_path) as conn:
        r = resolve_restaurant(conn, restaurant_id)
    details = _summary(r)
    details.update(
        address=r["address"], capacity=r["capacity"], opening_hours=f"{r['open_time']} - {r['close_time']}",
        signature_dishes=r["signature_dishes"], phone=r["phone"],
    )
    return {"ok": True, "restaurant": details,
            "availability": "not checked: call check_availability with date, time and party_size before saying "
                            "a table is free or full"}


def check_availability(restaurant_id: str, date_: str, time_: str, party_size: int,
                       db_path: Path | None = None) -> dict:
    party = normalize_party(party_size)  # first: a 25-person group gets the events team, not a date question
    day, slot = normalize_date(date_), normalize_time(time_)
    reject_past_slot(day, slot)
    with connect(db_path) as conn:
        r = resolve_restaurant(conn, restaurant_id)
        ok, why = _check(conn, r, day, slot, party)
        out = {"ok": True, "restaurant_id": r["id"], "restaurant_name": r["name"], "date": day.isoformat(),
               "weekday": day.strftime("%A"), "time": slot, "party_size": party, "available": ok}
        if dates.describe(str(date_), day):
            out["date_interpreted"] = dates.describe(str(date_), day)  # the model echoes this, not its own maths
        if not ok:
            out["reason"] = why
            out["alternative_times_same_venue"] = _nearby_times(conn, r, day, slot, party)
            alts = []
            for other in _all_restaurants(conn):
                if other["id"] != r["id"] and other["cuisine"] == r["cuisine"] and _check(conn, other, day, slot, party)[0]:
                    alts.append(other)
            alts.sort(key=lambda o: (o["neighborhood"] != r["neighborhood"], -o["rating"]))
            out["alternative_venues_same_time"] = [_summary(a) for a in alts[:3]]
    return out


def _new_code(conn) -> str:
    alphabet = string.ascii_uppercase + string.digits
    while True:
        code = "GF-" + "".join(secrets.choice(alphabet) for _ in range(6))
        if not conn.execute("SELECT 1 FROM reservations WHERE code=?", (code,)).fetchone():
            return code


def _require_confirmation(user_confirmed: bool, action: str) -> None:
    if not user_confirmed:
        raise ToolError("CONFIRMATION_REQUIRED", f"The guest has not explicitly confirmed the {action}.",
                        f"Read back the full {action} details and ask the guest to confirm with a yes. "
                        "Only then call this tool again with user_confirmed=true.")


def _reservation_view(conn, row: sqlite3.Row) -> dict:
    r = _restaurant_row(conn.execute("SELECT * FROM restaurants WHERE id=?", (row["restaurant_id"],)).fetchone())
    d = date.fromisoformat(row["date"])
    return {
        "confirmation_code": row["code"], "status": row["status"], "restaurant_id": r["id"],
        "restaurant_name": r["name"], "address": r["address"], "date": row["date"], "weekday": d.strftime("%A"),
        "time": row["time"], "party_size": row["party_size"], "guest_name": row["guest_name"],
        "phone_last4": row["phone"][-4:], "special_requests": row["special_requests"],
    }


def create_reservation(restaurant_id: str, date_: str, time_: str, party_size: int, guest_name: str,
                       phone: str, special_requests: str | None = None, user_confirmed: bool = False,
                       db_path: Path | None = None) -> dict:
    party = normalize_party(party_size)
    day, slot = normalize_date(date_), normalize_time(time_)
    reject_past_slot(day, slot)
    name = " ".join((guest_name or "").split())
    if not name or len(name) > 60 or not re.search(r"[A-Za-zऀ-෿]", name):
        raise ToolError("BAD_NAME", "A valid guest name is required.", "Ask for the name for the booking.")
    mobile = normalize_phone(phone)
    notes = (special_requests or "").strip()[:300] or None
    with connect(db_path) as conn:
        r = resolve_restaurant(conn, restaurant_id)
        _require_confirmation(user_confirmed, "booking")
        conn.execute("BEGIN IMMEDIATE")  # serialise writers: no double-booking under concurrency
        try:
            dup = conn.execute(
                "SELECT code FROM reservations WHERE restaurant_id=? AND date=? AND time=? AND phone=? AND status='confirmed'",
                (r["id"], day.isoformat(), slot, mobile)).fetchone()
            if dup:
                conn.execute("ROLLBACK")
                raise ToolError("DUPLICATE_BOOKING", "This guest already holds that exact booking.",
                                "Tell the guest the existing confirmation code instead of booking again.",
                                existing_code=dup["code"])
            ok, why = _check(conn, r, day, slot, party)
            if not ok:
                conn.execute("ROLLBACK")
                raise ToolError("NOT_AVAILABLE", f"Cannot book: {why}.", "Offer the alternatives below.",
                                alternative_times_same_venue=_nearby_times(conn, r, day, slot, party))
            code, now = _new_code(conn), _now().isoformat(timespec="seconds")
            conn.execute(
                "INSERT INTO reservations (code,restaurant_id,guest_name,phone,party_size,date,time,status,"
                "special_requests,created_at,updated_at) VALUES (?,?,?,?,?,?,?,'confirmed',?,?,?)",
                (code, r["id"], name, mobile, party, day.isoformat(), slot, notes, now, now))
            conn.execute("COMMIT")
        except ToolError:
            raise
        except Exception:
            conn.execute("ROLLBACK")
            raise
        row = conn.execute("SELECT * FROM reservations WHERE code=?", (code,)).fetchone()
        return {"ok": True, "message": "Reservation confirmed.", "reservation": _reservation_view(conn, row)}


def _owned_reservation(conn, code: str, phone: str) -> sqlite3.Row:
    c = (code or "").strip().upper()
    if c and not c.startswith("GF-"):
        c = "GF-" + c
    row = conn.execute("SELECT * FROM reservations WHERE code=?", (c,)).fetchone()
    # same error for "wrong code" and "wrong phone" so codes can't be enumerated
    if not row or row["phone"] != normalize_phone(phone):
        raise ToolError("RESERVATION_NOT_FOUND", "No reservation matches that code and phone number.",
                        "Ask the guest to re-check the confirmation code and the phone used for booking.")
    return row


def get_reservation(confirmation_code: str, phone: str, db_path: Path | None = None) -> dict:
    with connect(db_path) as conn:
        return {"ok": True, "reservation": _reservation_view(conn, _owned_reservation(conn, confirmation_code, phone))}


def modify_reservation(confirmation_code: str, phone: str, new_date: str | None = None, new_time: str | None = None,
                       new_party_size: int | None = None, special_requests: str | None = None,
                       user_confirmed: bool = False, db_path: Path | None = None) -> dict:
    if not any([new_date, new_time, new_party_size, special_requests]):
        raise ToolError("NOTHING_TO_CHANGE", "No change was requested.", "Ask what the guest wants to change.")
    new_party = normalize_party(new_party_size) if new_party_size else None
    with connect(db_path) as conn:
        row = _owned_reservation(conn, confirmation_code, phone)
        if row["status"] != "confirmed":
            raise ToolError("ALREADY_CANCELLED", "This reservation was cancelled and can't be modified.",
                            "Offer to make a new booking.")
        r = _restaurant_row(conn.execute("SELECT * FROM restaurants WHERE id=?", (row["restaurant_id"],)).fetchone())
        day = normalize_date(new_date) if new_date else date.fromisoformat(row["date"])
        slot = normalize_time(new_time) if new_time else row["time"]
        party = new_party or row["party_size"]
        reject_past_slot(day, slot)
        _require_confirmation(user_confirmed, "change")
        conn.execute("BEGIN IMMEDIATE")
        ok, why = _check(conn, r, day, slot, party, exclude_code=row["code"])
        if not ok:
            conn.execute("ROLLBACK")
            raise ToolError("NOT_AVAILABLE", f"Cannot move the booking: {why}. The original booking is unchanged.",
                            "Offer the alternatives below.",
                            alternative_times_same_venue=_nearby_times(conn, r, day, slot, party, row["code"]))
        conn.execute(
            "UPDATE reservations SET date=?, time=?, party_size=?, special_requests=COALESCE(?, special_requests),"
            " updated_at=? WHERE code=?",
            (day.isoformat(), slot, party, (special_requests or "").strip() or None,
             _now().isoformat(timespec="seconds"), row["code"]))
        conn.execute("COMMIT")
        updated = conn.execute("SELECT * FROM reservations WHERE code=?", (row["code"],)).fetchone()
        return {"ok": True, "message": "Reservation updated.", "reservation": _reservation_view(conn, updated)}


def cancel_reservation(confirmation_code: str, phone: str, user_confirmed: bool = False,
                       db_path: Path | None = None) -> dict:
    with connect(db_path) as conn:
        row = _owned_reservation(conn, confirmation_code, phone)
        if row["status"] == "cancelled":
            return {"ok": True, "message": "This reservation was already cancelled.",
                    "reservation": _reservation_view(conn, row)}
        _require_confirmation(user_confirmed, "cancellation")
        conn.execute("UPDATE reservations SET status='cancelled', updated_at=? WHERE code=?",
                     (_now().isoformat(timespec="seconds"), row["code"]))
        updated = conn.execute("SELECT * FROM reservations WHERE code=?", (row["code"],)).fetchone()
        return {"ok": True, "message": "Reservation cancelled.", "reservation": _reservation_view(conn, updated)}
