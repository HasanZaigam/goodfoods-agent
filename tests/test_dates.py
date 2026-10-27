"""Relative date resolution (goodfoods/dates.py) and the large-party hand-off, both enforced in code."""

import json
from datetime import date

import pytest

from goodfoods import config, dates, store
from goodfoods.agent.core import ReservationAgent, reconcile_dates, stated_party_size
from goodfoods.agent.llm import LLMResponse, ToolCall

TUE = date(2026, 10, 27)  # Tuesday
FRI = date(2026, 10, 30)
SAT = date(2026, 10, 31)
SUN = date(2026, 11, 1)


@pytest.mark.parametrize("phrase,expected", [
    # today / tomorrow / day after, English and Hinglish
    ("today", "2026-10-27"), ("tonight", "2026-10-27"), ("aaj", "2026-10-27"),
    ("tomorrow", "2026-10-28"), ("tmrw", "2026-10-28"), ("kal", "2026-10-28"),
    ("day after tomorrow", "2026-10-29"), ("parso", "2026-10-29"), ("parson", "2026-10-29"),
    # bare / this / coming weekday = the next occurrence, today included
    ("friday", "2026-10-30"), ("fri", "2026-10-30"), ("this friday", "2026-10-30"), ("coming friday", "2026-10-30"),
    ("tuesday", "2026-10-27"), ("this tuesday", "2026-10-27"), ("monday", "2026-11-02"),
    ("saturday", "2026-10-31"), ("sunday", "2026-11-01"),
    # next <weekday> = that day in next calendar week (Mon-Sun)
    ("next friday", "2026-11-06"), ("friday next week", "2026-11-06"), ("next monday", "2026-11-02"),
    ("next tuesday", "2026-11-03"), ("next sunday", "2026-11-08"), ("next sat", "2026-11-07"),
    # weekend
    ("weekend", "2026-10-31"), ("this weekend", "2026-10-31"), ("next weekend", "2026-11-07"),
    # Hinglish weekdays and modifiers
    ("shukravar", "2026-10-30"), ("is shukravar", "2026-10-30"), ("agle shukravar", "2026-11-06"),
    ("agla shanivar", "2026-11-07"), ("shanivar", "2026-10-31"), ("ravivar", "2026-11-01"),
    ("itvaar", "2026-11-01"), ("somvar", "2026-11-02"), ("agle somvar", "2026-11-02"),
    ("mangalvar", "2026-10-27"), ("budhvar", "2026-10-28"), ("guruvar", "2026-10-29"),
    ("is weekend", "2026-10-31"), ("agle weekend", "2026-11-07"), ("shukravar agle hafte", "2026-11-06"),
    # case and spacing do not matter
    ("Next  Friday", "2026-11-06"), ("KAL", "2026-10-28"),
])
def test_resolve_relative_from_a_tuesday(phrase, expected):
    assert dates.resolve_relative(phrase, TUE).isoformat() == expected


@pytest.mark.parametrize("today,phrase,expected", [
    # "next <weekday>" is always next calendar week, whatever today is
    (FRI, "friday", "2026-10-30"), (FRI, "next friday", "2026-11-06"),
    (SAT, "friday", "2026-11-06"), (SAT, "next friday", "2026-11-06"),
    (SUN, "next friday", "2026-11-06"), (SUN, "friday", "2026-11-06"), (SUN, "next monday", "2026-11-02"),
    # weekend: today if it is already Saturday or Sunday
    (SAT, "this weekend", "2026-10-31"), (SUN, "weekend", "2026-11-01"), (SAT, "next weekend", "2026-11-07"),
    (SUN, "next weekend", "2026-11-07"),
])
def test_resolve_relative_from_other_days(today, phrase, expected):
    assert dates.resolve_relative(phrase, today).isoformat() == expected


@pytest.mark.parametrize("phrase", ["2026-11-06", "next week", "someday", "", "friday 2 nov"])
def test_non_relative_phrases_are_not_resolved(phrase):
    assert dates.resolve_relative(phrase, TUE) is None


def test_store_normalize_date_uses_resolver_with_fillers():
    assert store.normalize_date("next friday", TUE) == date(2026, 11, 6)
    assert store.normalize_date("agle shukravar ko", TUE) == date(2026, 11, 6)
    assert store.normalize_date("kal raat", TUE) == date(2026, 10, 28)
    assert store.normalize_date("on saturday", TUE) == date(2026, 10, 31)
    assert store.normalize_date("6 nov", TUE) == date(2026, 11, 6)


@pytest.mark.parametrize("text,phrase,expected", [
    ("We are 25 people for an office party next Friday at 8 pm", "next friday", "2026-11-06"),
    ("Kal raat 8 baje 2 logon ke liye", "kal", "2026-10-28"),
    ("table for 2 this weekend please", "this weekend", "2026-10-31"),
    ("agle shanivar 4 log", "agle shanivar", "2026-11-07"),
])
def test_find_relative_in_a_guest_message(text, phrase, expected):
    found = dates.find_relative(text, TUE)
    assert found[0] == phrase and found[1].isoformat() == expected


def test_find_relative_ignores_bare_abbreviations_in_free_text():
    assert dates.find_relative("we sat down and the sun was out", TUE) is None


def test_model_date_arithmetic_is_overridden_by_the_guests_phrase(monkeypatch):
    """Live bug: guest said "next Friday", model sent 2026-10-31 (a Saturday)."""
    monkeypatch.setattr(dates, "today_ist", lambda: TUE)
    args = reconcile_dates({"date": "2026-10-31", "party_size": 4}, "table for 4 next Friday at 8")
    assert args == {"date": "next friday", "party_size": 4}
    assert store.normalize_date(args["date"], TUE) == date(2026, 11, 6)
    # the model already got it right, or passed words: untouched
    assert reconcile_dates({"date": "2026-11-06"}, "next Friday at 8")["date"] == "2026-11-06"
    assert reconcile_dates({"date": "next friday"}, "next Friday at 8")["date"] == "next friday"
    # the guest also named a calendar date, or no relative phrase at all: trust the model
    assert reconcile_dates({"date": "2026-11-02"}, "not next friday, 2 Nov")["date"] == "2026-11-02"
    assert reconcile_dates({"date": "2026-11-02"}, "the first one please")["date"] == "2026-11-02"
    assert reconcile_dates({"new_date": "2026-10-31"}, "move it to next friday")["new_date"] == "next friday"


def test_tool_results_echo_the_resolved_date(db_path):
    rec = store.recommend_restaurants(cuisine="thai", date_="saturday", db_path=db_path)
    assert rec["query"]["weekday"] == "Saturday" and "'saturday' = Saturday" in rec["date_interpreted"]
    chk = store.check_availability("GF002", "kal", "15:00", 2, db_path=db_path)
    assert chk["weekday"] and chk["date_interpreted"].startswith("'kal' = ")
    iso = store.check_availability("GF002", chk["date"], "15:00", 2, db_path=db_path)
    assert "date_interpreted" not in iso


# --- large groups -------------------------------------------------------------------------------
LARGE = dict(restaurant_id="GF002", date_="not a date", time_="25 o'clock", party_size=25)


@pytest.mark.parametrize("fn,kwargs", [
    (store.check_availability, LARGE),
    (store.create_reservation, {**LARGE, "guest_name": "", "phone": "123", "user_confirmed": True}),
    (store.recommend_restaurants, dict(party_size=25, date_="not a date", neighborhood="Atlantis")),
    (store.modify_reservation, dict(confirmation_code="GF-NOPE00", phone="123", new_party_size=25)),
])
def test_every_party_size_tool_returns_party_too_large_first(db_path, fn, kwargs):
    """Party size is validated before anything else, so a bad date/area/code never hides the hand-off."""
    with pytest.raises(store.ToolError) as e:
        fn(db_path=db_path, **kwargs)
    d = e.value.to_dict()
    assert d["error_code"] == "PARTY_TOO_LARGE" and d["events_contact"] == config.EVENTS_CONTACT
    assert "Do not ask any follow-up questions" in d["hint"]


@pytest.mark.parametrize("text,size", [
    ("We are 25 people for an office party", 25), ("hum 30 log hain", 30), ("party of 22 next week", 22),
    ("25 of us", 25), ("table for 2 at 8", None), ("book 25 nov for 2 people", 2), ("group for 40", 40),
])
def test_stated_party_size(text, size):
    assert stated_party_size(text) == size


class _Scripted:
    def __init__(self, steps):
        self.steps, self.seen = list(steps), []

    def chat(self, messages, tools=None):
        self.seen.append(messages)
        return self.steps.pop(0)


def test_large_party_follow_up_question_is_replaced_by_events_contact(toolbox):
    """Live bug: for 25 people the model asked for outlet/date instead of handing off to the events team."""
    llm = _Scripted([
        LLMResponse(content="Sure! Which outlet and what time would you like?"),
        LLMResponse(content="For 25 guests please contact our events team: events@goodfoods.in / +91 80 4000 1234."),
    ])
    r = ReservationAgent(llm, toolbox).run_turn([], "We are 25 people for an office party next Friday at 8 pm")
    assert "events@goodfoods.in" in r.reply and "Which outlet" not in r.reply
    assert any("cannot be booked online" in m["content"] for m in llm.seen[1] if m["role"] == "system")


def test_large_party_contact_is_appended_if_model_ignores_the_fix(toolbox):
    llm = _Scripted([LLMResponse(content="Which outlet?"), LLMResponse(content="And what time?")])
    r = ReservationAgent(llm, toolbox).run_turn([], "hum 30 log hain, kal dinner")
    assert config.EVENTS_CONTACT in r.reply


def test_party_too_large_from_a_tool_also_triggers_the_guard(toolbox):
    llm = _Scripted([
        LLMResponse(content=None, tool_calls=[ToolCall("c1", "recommend_restaurants", json.dumps({"party_size": 21}))]),
        LLMResponse(content="Which area do you prefer?"),
        LLMResponse(content="Our events team handles groups over 20: events@goodfoods.in / +91 80 4000 1234."),
    ])
    r = ReservationAgent(llm, toolbox).run_turn([], "office lunch for our whole team")
    assert r.events[0].result["error_code"] == "PARTY_TOO_LARGE" and "events@goodfoods.in" in r.reply


def test_normal_party_is_not_affected(toolbox):
    llm = _Scripted([LLMResponse(content="Which outlet would you like?")])
    r = ReservationAgent(llm, toolbox).run_turn([], "table for 4 people tomorrow")
    assert r.reply == "Which outlet would you like?" and r.llm_calls == 1
