"""Deterministic synthetic data for GoodFoods: 75 locations across Bengaluru + existing bookings.

Run directly to (re)build the database:  python -m goodfoods.data.seed --reset
"""

from __future__ import annotations

import argparse
import json
import random
import sqlite3
from datetime import date, datetime, timedelta
from pathlib import Path

from goodfoods import config

NUM_RESTAURANTS = 75
SEED = 42

NEIGHBORHOODS = {
    # area: (address hint, rough cost multiplier)
    "Indiranagar": ("100 Feet Road", 1.25),
    "Koramangala": ("80 Feet Road, 5th Block", 1.15),
    "HSR Layout": ("27th Main, Sector 1", 1.0),
    "Whitefield": ("ITPL Main Road", 1.1),
    "JP Nagar": ("24th Main, 6th Phase", 0.9),
    "Jayanagar": ("11th Main, 4th Block", 0.9),
    "MG Road": ("Church Street", 1.3),
    "Malleshwaram": ("Sampige Road", 0.85),
    "Electronic City": ("Hosur Road, Phase 1", 0.85),
    "Hebbal": ("Bellary Road", 1.05),
    "Marathahalli": ("Outer Ring Road", 0.95),
    "BTM Layout": ("16th Main, 2nd Stage", 0.85),
}

# concept name -> (cuisine, base cost for two INR, signature dishes, veg_only)
CONCEPTS = {
    "Tandoor House": ("North Indian", 1400, ["butter chicken", "dal makhani", "garlic naan"], False),
    "Dosa Co.": ("South Indian", 500, ["ghee roast dosa", "filter coffee", "idli vada"], True),
    "Biryani Darbar": ("Mughlai", 1100, ["dum biryani", "galouti kebab", "phirni"], False),
    "Wok Street": ("Chinese", 1000, ["hakka noodles", "chilli chicken", "dim sum"], False),
    "Trattoria": ("Italian", 1800, ["wood-fired pizza", "truffle risotto", "tiramisu"], False),
    "Sakura": ("Japanese", 2600, ["sushi platter", "ramen", "tempura"], False),
    "Cantina": ("Mexican", 1500, ["tacos al pastor", "burrito bowl", "churros"], False),
    "Coastal Kitchen": ("Seafood", 1900, ["fish curry rice", "prawn ghee roast", "neer dosa"], False),
    "Bistro": ("Continental", 1700, ["grilled chicken steak", "mushroom stroganoff", "caesar salad"], False),
    "Thai Leaf": ("Thai", 1600, ["green curry", "pad thai", "tom yum"], False),
    "Mezze Bar": ("Mediterranean", 1800, ["falafel platter", "hummus trio", "lamb shawarma"], False),
    "Grill Yard": ("BBQ & Grill", 1600, ["live grill platter", "peri peri chicken", "paneer tikka"], False),
    "Andhra Spice": ("Andhra", 800, ["andhra meals", "gongura mutton", "chicken 65"], False),
    "Kerala Kitchen": ("Kerala", 900, ["appam stew", "malabar parotta", "karimeen pollichathu"], False),
    "Green Bowl": ("Healthy & Vegan", 1200, ["buddha bowl", "millet salad", "cold-pressed juices"], True),
    "Chai & Bake": ("Cafe", 600, ["masala chai", "bun maska", "croissants"], True),
    "Pan Asia": ("Pan-Asian", 2000, ["laksa", "korean fried chicken", "bao"], False),
    "Thali Ghar": ("Rajasthani & Gujarati", 900, ["unlimited thali", "dal baati churma", "dhokla"], True),
}

FEATURES = [
    "outdoor_seating", "private_dining", "live_music", "kid_friendly",
    "wheelchair_accessible", "bar", "rooftop", "parking", "pet_friendly",
]

OPEN_HOURS = [("11:00", "23:00"), ("12:00", "23:30"), ("07:30", "22:30"), ("12:00", "00:00")]

SCHEMA = """
CREATE TABLE IF NOT EXISTS restaurants (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    neighborhood TEXT NOT NULL,
    address TEXT NOT NULL,
    cuisine TEXT NOT NULL,
    price_tier INTEGER NOT NULL,           -- 1 (budget) .. 4 (fine dining)
    avg_cost_for_two INTEGER NOT NULL,     -- INR
    capacity INTEGER NOT NULL,             -- total seats
    rating REAL NOT NULL,
    veg_only INTEGER NOT NULL,
    features TEXT NOT NULL,                -- JSON list
    signature_dishes TEXT NOT NULL,        -- JSON list
    open_time TEXT NOT NULL,               -- HH:MM
    close_time TEXT NOT NULL,              -- HH:MM, "00:00" = midnight
    phone TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS reservations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    code TEXT UNIQUE NOT NULL,
    restaurant_id TEXT NOT NULL REFERENCES restaurants(id),
    guest_name TEXT NOT NULL,
    phone TEXT NOT NULL,
    party_size INTEGER NOT NULL,
    date TEXT NOT NULL,                    -- YYYY-MM-DD
    time TEXT NOT NULL,                    -- HH:MM
    status TEXT NOT NULL DEFAULT 'confirmed',
    special_requests TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_res_slot ON reservations(restaurant_id, date, status);
"""


def _price_tier(cost_for_two: int) -> int:
    if cost_for_two < 800:
        return 1
    if cost_for_two < 1500:
        return 2
    if cost_for_two < 2200:
        return 3
    return 4


def build_restaurants(rng: random.Random) -> list[dict]:
    combos = [(c, n) for c in CONCEPTS for n in NEIGHBORHOODS]
    rng.shuffle(combos)
    # make sure every concept and every neighborhood appears at least once
    chosen: list[tuple[str, str]] = []
    for concept in CONCEPTS:
        chosen.append(next(x for x in combos if x[0] == concept and x not in chosen))
    for area in NEIGHBORHOODS:
        if not any(a == area for _, a in chosen):
            chosen.append(next(x for x in combos if x[1] == area and x not in chosen))
    for combo in combos:
        if len(chosen) >= NUM_RESTAURANTS:
            break
        if combo not in chosen:
            chosen.append(combo)

    rows = []
    for i, (concept, area) in enumerate(sorted(chosen, key=lambda x: (x[1], x[0])), start=1):
        cuisine, base_cost, dishes, veg_only = CONCEPTS[concept]
        street, mult = NEIGHBORHOODS[area]
        cost = int(round(base_cost * mult * rng.uniform(0.9, 1.15), -1))
        capacity = rng.choice([24, 32, 40, 48, 60, 80, 100, 120])
        feats = set(rng.sample(FEATURES, k=rng.randint(2, 5)))
        if capacity >= 80:
            feats.add("private_dining")
        if cuisine == "Cafe":
            feats.discard("bar")
        open_t, close_t = ("07:30", "22:30") if cuisine in {"Cafe", "South Indian"} else rng.choice(OPEN_HOURS[:2] + OPEN_HOURS[3:])
        rows.append(
            {
                "id": f"GF{i:03d}",
                "name": f"GoodFoods {concept} - {area}",
                "neighborhood": area,
                "address": f"{rng.randint(1, 450)}, {street}, {area}, Bengaluru",
                "cuisine": cuisine,
                "price_tier": _price_tier(cost),
                "avg_cost_for_two": cost,
                "capacity": capacity,
                "rating": round(rng.uniform(3.6, 4.9), 1),
                "veg_only": int(veg_only),
                "features": json.dumps(sorted(feats)),
                "signature_dishes": json.dumps(dishes),
                "open_time": open_t,
                "close_time": close_t,
                "phone": f"+91 80 {rng.randint(4000, 4999)} {rng.randint(1000, 9999)}",
            }
        )
    return rows


FIRST = ["Aarav", "Diya", "Rohan", "Ananya", "Kabir", "Meera", "Arjun", "Isha", "Vikram", "Sneha", "Farhan", "Priya"]
LAST = ["Sharma", "Iyer", "Reddy", "Khan", "Nair", "Gupta", "Rao", "Menon", "Das", "Patel"]


def build_reservations(rng: random.Random, restaurants: list[dict], today: date) -> list[dict]:
    """Pre-book tables so some popular slots (weekend dinners) are genuinely full."""
    rows, n = [], 0
    now = datetime.now(config.TIMEZONE).isoformat(timespec="seconds")
    for day_offset in range(0, 15):
        d = today + timedelta(days=day_offset)
        weekend = d.weekday() >= 4
        for r in restaurants:
            # occupancy of the dinner peak: busier on weekends and for highly rated outlets;
            # top outlets on weekend nights end up genuinely full, most others have room.
            peak = (rng.uniform(0.45, 1.05) if weekend else rng.uniform(0.15, 0.6)) * (r["rating"] / 4.4)
            plan = {"19:30": 0.3, "20:00": 0.4, "20:30": 0.3, "13:00": 0.5}  # the 3 dinner slots overlap
            for t, share in plan.items():
                seats_target = int(r["capacity"] * min(peak, 1.0) * share)
                booked = 0
                while booked < seats_target:
                    size = min(rng.choice([2, 2, 2, 3, 4, 4, 5, 6]), seats_target - booked)
                    n += 1
                    rows.append(
                        {
                            "code": f"GF-S{n:05d}",
                            "restaurant_id": r["id"],
                            "guest_name": f"{rng.choice(FIRST)} {rng.choice(LAST)}",
                            "phone": f"9{rng.randint(100000000, 999999999)}",
                            "party_size": size,
                            "date": d.isoformat(),
                            "time": t,
                            "status": "confirmed",
                            "special_requests": None,
                            "created_at": now,
                            "updated_at": now,
                        }
                    )
                    booked += size
    return rows


def seed(db_path: Path | None = None, reset: bool = False, today: date | None = None) -> Path:
    db_path = Path(db_path or config.DB_PATH)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    if reset and db_path.exists():
        db_path.unlink()
    conn = sqlite3.connect(db_path)
    try:
        conn.executescript(SCHEMA)
        if conn.execute("SELECT COUNT(*) FROM restaurants").fetchone()[0]:
            return db_path  # already seeded
        rng = random.Random(SEED)
        restaurants = build_restaurants(rng)
        today = today or datetime.now(config.TIMEZONE).date()
        reservations = build_reservations(rng, restaurants, today)
        conn.executemany(
            "INSERT INTO restaurants VALUES (:id,:name,:neighborhood,:address,:cuisine,:price_tier,"
            ":avg_cost_for_two,:capacity,:rating,:veg_only,:features,:signature_dishes,:open_time,:close_time,:phone)",
            restaurants,
        )
        conn.executemany(
            "INSERT INTO reservations (code,restaurant_id,guest_name,phone,party_size,date,time,status,"
            "special_requests,created_at,updated_at) VALUES (:code,:restaurant_id,:guest_name,:phone,"
            ":party_size,:date,:time,:status,:special_requests,:created_at,:updated_at)",
            reservations,
        )
        conn.commit()
    finally:
        conn.close()
    return db_path


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Seed the GoodFoods database")
    parser.add_argument("--reset", action="store_true", help="delete and rebuild the database")
    args = parser.parse_args()
    path = seed(reset=args.reset)
    with sqlite3.connect(path) as c:
        print(f"Seeded {path}: {c.execute('SELECT COUNT(*) FROM restaurants').fetchone()[0]} restaurants, "
              f"{c.execute('SELECT COUNT(*) FROM reservations').fetchone()[0]} existing reservations")
