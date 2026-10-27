from datetime import date, timedelta

import pytest

from goodfoods import store
from goodfoods.store import ToolError


def test_seed_has_expected_shape(db_path):
    with store.connect(db_path) as c:
        n = c.execute("SELECT COUNT(*) FROM restaurants").fetchone()[0]
        cuisines = c.execute("SELECT COUNT(DISTINCT cuisine) FROM restaurants").fetchone()[0]
        areas = c.execute("SELECT COUNT(DISTINCT neighborhood) FROM restaurants").fetchone()[0]
    assert 50 <= n <= 100
    assert cuisines >= 15 and areas == 12


@pytest.mark.parametrize("raw,expected", [
    ("7pm", "19:00"), ("7:30 PM", "19:30"), ("19:45", "20:00"), ("8", "20:00"), ("noon", "12:00"), ("11am", "11:00"),
])
def test_normalize_time(raw, expected):
    assert store.normalize_time(raw) == expected


def test_normalize_date_relative_and_errors():
    today = date(2026, 10, 26)  # a Monday
    assert store.normalize_date("tomorrow", today) == date(2026, 10, 27)
    assert store.normalize_date("saturday", today) == date(2026, 10, 31)
    assert store.normalize_date("next monday", today) == date(2026, 11, 2)
    assert store.normalize_date("2 nov", today) == date(2026, 11, 2)
    with pytest.raises(ToolError) as e:
        store.normalize_date("2026-10-01", today)
    assert e.value.code == "DATE_IN_PAST"
    with pytest.raises(ToolError) as e:
        store.normalize_date(str(today + timedelta(days=200)), today)
    assert e.value.code == "DATE_TOO_FAR"


@pytest.mark.parametrize("raw", ["+91 98765 43210", "09876543210", "9876543210"])
def test_phone_normalisation(raw):
    assert store.normalize_phone(raw) == "9876543210"


def test_bad_inputs_are_structured_errors():
    for fn, args in [(store.normalize_phone, ("12345",)), (store.normalize_party, (40,)),
                     (store.normalize_cuisine, ("martian",)), (store.normalize_area, ("Mumbai",))]:
        with pytest.raises(ToolError) as e:
            fn(*args)
        assert e.value.to_dict()["ok"] is False and e.value.hint


def test_synonyms_and_fuzzy_matching():
    assert store.normalize_cuisine("biryani") == "Mughlai"
    assert store.normalize_cuisine("sushi") == "Japanese"
    assert store.normalize_area("hsr") == "HSR Layout"
    assert store.normalize_area("Koramangla") == "Koramangala"  # typo


def test_search_and_recommend(db_path):
    res = store.search_restaurants(cuisine="South Indian", pure_veg=True, db_path=db_path)
    assert res["ok"] and res["restaurants"]
    assert all(r["cuisine"] == "South Indian" and r["pure_veg"] for r in res["restaurants"])
    rec = store.recommend_restaurants(party_size=2, occasion="anniversary", budget_per_person=1500, db_path=db_path)
    assert 1 <= len(rec["recommendations"]) <= 3 and all("why" in r for r in rec["recommendations"])


def test_booking_lifecycle(db_path):
    d = (date.today() + timedelta(days=3)).isoformat()
    with pytest.raises(ToolError) as e:  # server-side confirmation gate
        store.create_reservation("GF010", d, "15:00", 2, "Test Guest", "9876500000", db_path=db_path)
    assert e.value.code == "CONFIRMATION_REQUIRED"

    booked = store.create_reservation("GF010", d, "15:00", 2, "Test Guest", "9876500000",
                                      user_confirmed=True, db_path=db_path)
    code = booked["reservation"]["confirmation_code"]
    assert code.startswith("GF-")

    with pytest.raises(ToolError) as e:  # same guest, same slot
        store.create_reservation("GF010", d, "15:00", 2, "Test Guest", "9876500000", user_confirmed=True,
                                 db_path=db_path)
    assert e.value.code == "DUPLICATE_BOOKING"

    with pytest.raises(ToolError) as e:  # wrong phone looks identical to wrong code
        store.get_reservation(code, "9000000000", db_path=db_path)
    assert e.value.code == "RESERVATION_NOT_FOUND"

    moved = store.modify_reservation(code, "9876500000", new_time="4 pm", user_confirmed=True, db_path=db_path)
    assert moved["reservation"]["time"] == "16:00"
    cancelled = store.cancel_reservation(code, "9876500000", user_confirmed=True, db_path=db_path)
    assert cancelled["reservation"]["status"] == "cancelled"
    with pytest.raises(ToolError) as e:
        store.modify_reservation(code, "9876500000", new_time="17:00", user_confirmed=True, db_path=db_path)
    assert e.value.code == "ALREADY_CANCELLED"


def test_capacity_is_enforced_and_alternatives_offered(db_path):
    d = (date.today() + timedelta(days=5)).isoformat()
    with store.connect(db_path) as c:
        small = c.execute("SELECT id, capacity FROM restaurants ORDER BY capacity LIMIT 1").fetchone()
    rid, cap = small["id"], small["capacity"]
    # fill a lunch slot exactly, then one more guest must be refused
    filled, n = 0, 0
    while filled < cap:
        size = min(6, cap - filled)
        n += 1
        try:
            store.create_reservation(rid, d, "16:00", size, f"Filler {n}", f"98{n:08d}", user_confirmed=True,
                                     db_path=db_path)
            filled += size
        except ToolError:
            break
    res = store.check_availability(rid, d, "16:00", 2, db_path=db_path)
    assert res["available"] is False
    assert res["alternative_times_same_venue"] or res["alternative_venues_same_time"]


def test_outside_opening_hours(db_path):
    d = (date.today() + timedelta(days=2)).isoformat()
    res = store.check_availability("GF001", d, "04:00", 2, db_path=db_path)
    assert res["available"] is False and "opening hours" in res["reason"]


def test_large_party_rules(db_path):
    with pytest.raises(ToolError) as e:
        store.normalize_party(25)
    assert e.value.code == "PARTY_TOO_LARGE"
    rec = store.recommend_restaurants(party_size=15, db_path=db_path)
    assert all("private_dining" in r["features"] for r in rec["recommendations"])


def test_dish_overrides_wrong_cuisine_guess(db_path):
    rec = store.recommend_restaurants(dish="biryani", cuisine="South Indian", party_size=2, db_path=db_path)
    assert rec["recommendations"] and all(r["cuisine"] == "Mughlai" for r in rec["recommendations"])
    assert "dish_note" in rec


# --- regression tests for bugs found in live runs ---------------------------------------------
def test_biryani_in_indiranagar_falls_back_to_nearest_area(db_path):
    """Live bug: model added cuisine='North Indian' and dropped date/time; reply named no outlet."""
    rec = store.recommend_restaurants(party_size=2, dish="biryani", neighborhood="Indiranagar", cuisine="North Indian",
                                      date_="kal", time_="raat 8 baje", db_path=db_path)
    tomorrow = (store._now().date() + timedelta(days=1)).isoformat()
    assert rec["query"]["date"] == tomorrow and rec["query"]["time"] == "20:00"
    assert rec["recommendations"] and all(r["cuisine"] == "Mughlai" for r in rec["recommendations"])
    assert rec["nearest_area_fallback"] is True and "not in Indiranagar" in rec["area_note"]
    assert "dish_note" in rec and "North Indian" in rec["dish_note"]
    # without a slot filter both Mughlai outlets qualify: the nearer one (MG Road ~4 km) must rank first
    rec = store.recommend_restaurants(dish="biryani", neighborhood="Indiranagar", db_path=db_path)
    assert rec["recommendations"][0]["neighborhood"] == "MG Road"
    assert "Closest option: GoodFoods Biryani Darbar - MG Road" in rec["area_note"]


@pytest.mark.parametrize("dish,cuisines", [("biryani", ["Mughlai"]), ("Biriyani", ["Mughlai"]),
                                           ("dosa", ["Seafood", "South Indian"]), ("momos", [])])
def test_dish_to_cuisine_mapping_comes_from_menus(dish, cuisines):
    assert store.dish_cuisines(dish)[1] == cuisines


def test_unknown_dish_returns_nothing_rather_than_unrelated_outlets(db_path):
    rec = store.recommend_restaurants(dish="momos", db_path=db_path)
    assert rec["recommendations"] == [] and "No GoodFoods menu" in rec["dish_note"]


def test_budget_ranks_by_closeness_and_flags_all_within(db_path):
    rec = store.recommend_restaurants(party_size=2, budget_per_person=1500, limit=5, db_path=db_path)
    top = rec["recommendations"][0]
    assert top["budget_fit"] == "close to budget"
    assert 0.75 * 1500 <= top["cost_per_person_inr"] <= 1500
    cheap = store.recommend_restaurants(budget_per_person=5000, db_path=db_path)
    assert "within the ₹5000/person budget" in cheap["budget_note"]


@pytest.mark.parametrize("kwargs", [
    {"neighborhood": "Electronic City", "budget_per_person": 500},
    {"neighborhood": "HSR Layout", "budget_per_person": 500, "occasion": "anniversary"},
    {"neighborhood": "HSR Layout", "budget_per_person": 500, "features": ["rooftop"]},
    {"cuisine": "Mediterranean", "budget_per_person": 900, "occasion": "anniversary"},
])
def test_over_budget_never_outranks_within_budget(db_path, kwargs):
    """Live bug: Sakura (~₹1695/person) topped a ₹1500 anniversary search because the occasion bonus
    outweighed the budget penalty. Area, feature and occasion bonuses must never lift an outlet over budget
    above one within it (on score alone, the first three queries put an in-area over-budget outlet first)."""
    budget = kwargs["budget_per_person"]
    recs = store.recommend_restaurants(limit=5, db_path=db_path, **kwargs)["recommendations"]
    over = [r["cost_per_person_inr"] > budget for r in recs]
    assert recs and not over[0]
    assert over == sorted(over), f"over-budget outlet ranked above a within-budget one: {recs}"


def test_within_budget_prefers_closer_to_budget(db_path):
    recs = store.recommend_restaurants(budget_per_person=1500, occasion="anniversary", limit=5,
                                       db_path=db_path)["recommendations"]
    assert recs[0]["cost_per_person_inr"] <= 1500
    assert recs[0]["cost_per_person_inr"] > min(r["cost_per_person_inr"] for r in recs)


def test_recommend_without_slot_says_availability_not_checked(db_path):
    rec = store.recommend_restaurants(cuisine="thai", db_path=db_path)
    assert rec["availability_checked"] is False and "NOT checked" in rec["availability_note"]
    details = store.get_restaurant_details(rec["recommendations"][0]["restaurant_id"], db_path=db_path)
    assert details["availability"].startswith("not checked")


@pytest.mark.parametrize("raw,expected", [("raat 8 baje", "20:00"), ("8 baje", "20:00"), ("shaam 7:30", "19:30"),
                                          ("saade 8", "20:30"), ("subah 9", "09:00"), ("dopahar 1", "13:00")])
def test_normalize_time_hinglish(raw, expected):
    assert store.normalize_time(raw) == expected


def test_normalize_date_hinglish():
    today = date(2026, 10, 26)
    assert store.normalize_date("kal", today) == date(2026, 10, 27)
    assert store.normalize_date("kal raat", today) == date(2026, 10, 27)
    assert store.normalize_date("aaj", today) == today
    assert store.normalize_date("parso", today) == date(2026, 10, 28)


def test_past_time_today_is_rejected(db_path, monkeypatch):
    from datetime import datetime
    fake_now = datetime(2026, 10, 27, 21, 0, tzinfo=store.config.TIMEZONE)
    monkeypatch.setattr(store, "_now", lambda: fake_now)
    with pytest.raises(ToolError) as e:
        store.check_availability("GF001", "2026-10-27", "20:00", 2, db_path=db_path)
    assert e.value.code == "TIME_IN_PAST"
    with pytest.raises(ToolError) as e:
        store.create_reservation("GF001", "2026-10-27", "13:00", 2, "A", "9876500001", user_confirmed=True,
                                 db_path=db_path)
    assert e.value.code == "TIME_IN_PAST"
    with pytest.raises(ToolError) as e:
        store.normalize_date("2026-10-26", fake_now.date())
    assert e.value.code == "DATE_IN_PAST"


@pytest.mark.parametrize("raw", ["12345", "98765432", "5876543210", "98765432101", "abc"])
def test_invalid_phones_are_rejected_with_clear_message(raw):
    with pytest.raises(ToolError) as e:
        store.normalize_phone(raw)
    assert e.value.code == "BAD_PHONE" and "10 digits" in e.value.message


@pytest.mark.parametrize("n,code", [(0, "BAD_PARTY_SIZE"), (-2, "BAD_PARTY_SIZE"), (21, "PARTY_TOO_LARGE"),
                                    ("lots", "BAD_PARTY_SIZE")])
def test_party_size_bounds(n, code):
    with pytest.raises(ToolError) as e:
        store.normalize_party(n)
    assert e.value.code == code


@pytest.mark.parametrize("n", [1, 20, "4 people"])
def test_party_size_valid(n):
    assert 1 <= store.normalize_party(n) <= 20


@pytest.mark.parametrize("fn,kwargs", [
    (store.create_reservation, dict(restaurant_id="GF010", date_="saturday", time_="15:00", party_size=2,
                                    guest_name="X", phone="9876500009")),
    (store.cancel_reservation, dict(confirmation_code="GF-S00001", phone="9000000000")),
])
def test_server_refuses_writes_without_confirmation_flag(db_path, fn, kwargs):
    with store.connect(db_path) as c:  # use a real booking's phone for the cancel case
        row = c.execute("SELECT code, phone FROM reservations WHERE code='GF-S00001'").fetchone()
    if fn is store.cancel_reservation:
        kwargs = dict(confirmation_code=row["code"], phone=row["phone"])
    with pytest.raises(ToolError) as e:
        fn(**kwargs, db_path=db_path)
    assert e.value.code == "CONFIRMATION_REQUIRED"


@pytest.mark.parametrize("ident", ["GoodFoods Cantina – MG Road", "Cantina - MG Road", "goodfoods cantina—mg road",
                                   "GoodFoods  Cantina -  MG Road"])
def test_restaurant_name_with_typographic_dash_resolves(db_path, ident):
    """Live bug: the model wrote 'GoodFoods Cantina – MG Road' (en dash) and got AMBIGUOUS_RESTAURANT."""
    with store.connect(db_path) as c:
        assert store.resolve_restaurant(c, ident)["name"] == "GoodFoods Cantina - MG Road"
