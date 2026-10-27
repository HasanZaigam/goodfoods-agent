"""Run the README user journeys against the REAL model and save transcripts.

    python scripts/run_journeys.py            # all journeys
    python scripts/run_journeys.py booking    # one journey by name

Writes docs/TRANSCRIPTS.md (paste-ready for the README) and prints a pass/fail summary.
A journey PASSES only if every check holds: the right tools ran in the right turn, with the right resolved
arguments, and the reply says what the tool returned (no exact-text matching, since LLM wording varies).
Every journey also checks that no reply claims a table is free/full before a tool checked it.
"""

from __future__ import annotations

import json
import os
import re
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Callable

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
# Journeys run on their own freshly seeded DB (inherited by the MCP server process), so results are reproducible
# and the demo database used by the app is left untouched.
os.environ.setdefault("GOODFOODS_DB", str(ROOT / "data" / "journeys.db"))

from goodfoods import config, store  # noqa: E402
from goodfoods.agent.core import AVAIL_TOOLS, ReservationAgent, TurnResult, claims_availability  # noqa: E402
from goodfoods.agent.llm import OpenAICompatibleClient  # noqa: E402
from goodfoods.agent.mcp_bridge import MCPToolbox  # noqa: E402
from goodfoods.data.seed import seed  # noqa: E402


PAUSE_S = float(os.getenv("JOURNEY_PAUSE_S", "8"))  # free-tier tokens/minute


Check = tuple[bool, str]


@dataclass
class Journey:
    name: str
    title: str
    turns: list[str]
    check: Callable[[list[TurnResult]], list[Check]]


def _events(results, turn: int | None = None):
    rs = results if turn is None else results[turn:turn + 1]
    return [e for r in rs for e in r.events]


def _ok_calls(results, name, turn: int | None = None):
    return [e for e in _events(results, turn) if e.name == name and e.ok]


def _check(cond, msg) -> Check:
    return bool(cond), msg


def _readable_times(hhmm: str) -> list[str]:
    h, m = map(int, hhmm.split(":"))
    h12 = h % 12 or 12
    return [hhmm, f"{h12}:{m:02d}"] + ([f"{h12} pm", f"{h12}pm", f"{h12} PM", f"{h12} baje"] if m == 0 else [])


def grounded_availability(results) -> list[Check]:
    """Global rule: a reply may only claim a table is free/full after a tool checked it (this turn or earlier)."""
    checked = False
    for i, r in enumerate(results):
        checked = checked or any(e.name in AVAIL_TOOLS or e.result.get("availability_checked") for e in r.events)
        if claims_availability(r.reply) and not checked:
            return [(False, f"turn {i + 1} claims availability without any availability check")]
    return [(True, "every availability claim was backed by a tool check")]


def booking_checks(rs) -> list[Check]:
    sat = _next_saturday().isoformat()
    recs = _ok_calls(rs, "recommend_restaurants", 0)
    q = recs[-1].result.get("query", {}) if recs else {}
    checks_t1 = _ok_calls(rs, "check_availability", 1)
    booked = _ok_calls(rs, "create_reservation")
    code = booked[0].result["reservation"]["confirmation_code"] if booked else None
    # "The first one" must mean the first outlet recommend_restaurants returned, not whatever the model listed first
    first = (recs[-1].result.get("recommendations") or [{}])[0] if recs else {}
    first_id, first_name = first.get("restaurant_id"), first.get("name")
    checked_ids = {e.result.get("restaurant_id") for e in _ok_calls(rs, "check_availability", 1)}
    booked_ids = {e.result["reservation"]["restaurant_id"] for e in booked}
    return [
        _check(recs and q.get("budget_per_person") == 1500 and q.get("party_size") == 2,
               f"turn 1: recommend_restaurants got budget 1500/person and party 2 (query={q})"),
        _check(q.get("date") == sat and q.get("time") == "20:00", f"turn 1: resolved to {sat} 20:00"),
        _check(recs and not ({"features", "cuisine", "pure_veg"} & set(recs[-1].arguments)),
               f"turn 1: no filters the guest didn't ask for (args={recs[-1].arguments if recs else None})"),
        _check(any(e.result.get("available") is True for e in checks_t1),
               "turn 2: check_availability ran (and was free) before asking for name/phone"),
        _check(first_id and checked_ids == {first_id},
               f"turn 2: 'the first one' checked {first_id} ({first_name}), the first recommendation "
               f"(checked={sorted(map(str, checked_ids))})"),
        _check(first_id and booked_ids == {first_id},
               f"turn 4: booked {first_id}, the first recommendation (booked={sorted(map(str, booked_ids))})"),
        _check(not _ok_calls(rs, "create_reservation", 2), "turn 3: no booking before the guest said yes"),
        _check(len(booked) == 1 and _ok_calls(rs, "create_reservation", 3), "turn 4: booked exactly once, after 'yes'"),
        _check(code and code in rs[-1].reply, f"final reply quotes the real code {code}"),
    ]


def full_checks(rs) -> list[Check]:
    checks = [e for e in _events(rs) if e.name == "check_availability" and e.ok]
    full = [e for e in checks if e.result.get("available") is False]
    if not full:
        return [(False, "check_availability never reported the slot as full")]
    res = full[0].result
    alt_strings = [t for hhmm in res.get("alternative_times_same_venue", []) for t in _readable_times(hhmm)]
    alt_strings += [v["neighborhood"] for v in res.get("alternative_venues_same_time", [])]
    return [
        (True, "check_availability reported the slot as full"),
        _check(alt_strings and any(a in rs[-1].reply for a in alt_strings),
               "reply offers at least one alternative time or outlet returned by the tool"),
        _check(not _ok_calls(rs, "create_reservation"), "nothing was booked"),
    ]


def hinglish_checks(rs) -> list[Check]:
    tomorrow = (datetime.now(config.TIMEZONE).date() + timedelta(days=1)).isoformat()
    calls = [e for e in _ok_calls(rs, "recommend_restaurants") if e.result.get("recommendations")]
    if not calls:
        return [(False, "recommend_restaurants returned no restaurants")]
    res = calls[-1].result
    q, recs = res.get("query", {}), res["recommendations"]
    names = [r["name"].removeprefix("GoodFoods ").split(" - ")[0] for r in recs]
    return [
        _check(len(recs) >= 1, f"{len(recs)} restaurant(s) returned"),
        _check(q.get("date") == tomorrow and q.get("time") == "20:00" and q.get("party_size") == 2,
               f"'kal raat 8 baje, 2 log' resolved to {tomorrow} 20:00 for 2 (query={q})"),
        _check(all(r["cuisine"] == "Mughlai" for r in recs), "biryani mapped to our Mughlai (biryani) outlets"),
        _check(any(n in rs[-1].reply for n in names), f"reply names a returned outlet ({', '.join(set(names))})"),
        _check(not res.get("nearest_area_fallback") or "Indiranagar" in rs[-1].reply,
               "reply tells the guest the outlet is not in Indiranagar"),
        _check(not re.search(r"[\u0900-\u097F]", rs[-1].reply), "replied in Hinglish (Latin script), as the guest wrote"),
    ]


def cancel_checks(rs) -> list[Check]:
    cancelled = _ok_calls(rs, "cancel_reservation", 1)
    res = cancelled[0].result["reservation"] if cancelled else {}
    names_booking = [res.get("confirmation_code", "?"), res.get("restaurant_name", "?").split(" - ")[-1]]
    return [
        _check(_ok_calls(rs, "get_reservation", 0), "turn 1: looked the booking up first"),
        _check(not _ok_calls(rs, "cancel_reservation", 0), "turn 1: did not cancel before the guest confirmed"),
        _check(cancelled and cancelled[0].result["reservation"]["status"] == "cancelled", "turn 2: cancelled"),
        _check(re.search(r"cancel", rs[1].reply, re.I) and any(n in rs[1].reply for n in names_booking),
               "reply confirms the cancellation of that booking (code or outlet named)"),
    ]


FOLLOW_UP_RE = re.compile(r"which (outlet|restaurant|area|time|date|cuisine)|what time|could you (let me know|share|tell)"
                          r"|may i (have|know)|please (share|let me know) (the|your|which)", re.I)
POEM_WORDS = re.compile(r"\b(paws?|whiskers?|purrs?|meow|feline|kitten)\b", re.I)
BOOKING_WORDS = re.compile(r"\b(table|book|booking|reservation|restaurant|outlet|goodfoods)\b", re.I)


def edge_checks(rs) -> list[Check]:
    poem = rs[1].reply
    lines = [ln for ln in poem.splitlines() if ln.strip()]
    past = rs[2]
    return [
        _check(not _ok_calls(rs, "create_reservation"), "no booking was made for any invalid request"),
        _check(re.search(r"events@goodfoods\.in|4000 1234", rs[0].reply), "25 guests: shared the events team contact"),
        _check(not FOLLOW_UP_RE.search(rs[0].reply), "25 guests: no follow-up questions before the hand-off"),
        _check(not re.search(r"\b31\b|saturday", rs[0].reply, re.I),
               f"'next Friday' was not turned into Sat 31 Oct (next Friday = {_next_friday()})"),
        _check(not POEM_WORDS.search(poem) and len(lines) <= 3 and len(poem) <= 300,
               f"poem: declined in a short reply ({len(lines)} line(s), {len(poem)} chars), no poem"),
        _check(BOOKING_WORDS.search(poem), "poem: redirected to tables/bookings"),
        _check(any(e.result.get("error_code") == "DATE_IN_PAST" for e in past.events)
               or re.search(r"past|yesterday|already (gone|passed)|beet", past.reply, re.I),
               "yesterday: rejected as a past date"),
    ]


def _next_saturday():
    today = datetime.now(config.TIMEZONE).date()
    return today + timedelta(days=(5 - today.weekday()) % 7)


def _next_friday():
    from goodfoods import dates
    return dates.resolve_relative("next friday", datetime.now(config.TIMEZONE).date()).isoformat()


def _full_slot() -> tuple[str, str]:
    """Find an outlet that is genuinely full this Saturday at 20:00 for 6 guests."""
    sat = _next_saturday()
    if sat == datetime.now(config.TIMEZONE).date():
        sat += timedelta(days=7)
    with store.connect() as conn:
        for r in store._all_restaurants(conn):
            if not store._check(conn, r, sat, "20:00", 6)[0] and store._is_open_for(r, "20:00"):
                return r["name"], sat.isoformat()
    return "GoodFoods Sakura - Indiranagar", sat.isoformat()


def _seed_booking() -> str:
    d = (datetime.now(config.TIMEZONE).date() + timedelta(days=6)).isoformat()
    try:
        res = store.create_reservation("GF005", d, "13:30", 2, "Demo Guest", "9876501234", user_confirmed=True)
        return res["reservation"]["confirmation_code"]
    except store.ToolError as e:  # re-running the script: reuse the booking from the last run
        if e.code == "DUPLICATE_BOOKING":
            return e.extra["existing_code"]
        raise


def build_journeys() -> list[Journey]:
    full_name, _ = _full_slot()
    code = _seed_booking()
    return [
        Journey("booking", "Recommendation → availability → booking",
                ["Suggest a romantic place for our anniversary this Saturday at 8 pm, around ₹1500 per person, 2 of us",
                 "The first one sounds good, please check it",
                 "Name is Hasan Zaigam, mobile 9876543210",
                 "yes, confirm"],
                booking_checks),
        Journey("full", "Fully booked → alternatives",
                [f"Table for 6 at {full_name} this Saturday at 8 pm"],
                full_checks),
        Journey("hinglish", "Hinglish request (dish + relative date)",
                ["Kal raat 8 baje 2 logon ke liye Indiranagar mein biryani wali jagah suggest karo"],
                hinglish_checks),
        Journey("cancel", "Look up & cancel",
                [f"I want to cancel my booking {code}, my phone is 9876501234", "yes, cancel it"],
                cancel_checks),
        Journey("edge", "Edge cases: large group, out of scope, past date",
                ["We are 25 people for an office party next Friday at 8 pm",
                 "Also, can you write me a poem about cats?",
                 "Ok then book a table for 2 yesterday at 8 pm"],
                edge_checks),
    ]


def run(selected: set[str] | None = None) -> int:
    seed(reset=True)
    toolbox = MCPToolbox()
    llm = OpenAICompatibleClient()
    md = [f"# Example conversations (real runs)\n\nModel: `{llm.model}` · generated "
          f"{datetime.now(config.TIMEZONE):%Y-%m-%d %H:%M} IST by `scripts/run_journeys.py`\n"]
    passed = total = 0
    try:
        for j in build_journeys():
            if selected and j.name not in selected:
                continue
            total += 1
            agent, history, results = ReservationAgent(llm, toolbox), [], []
            md.append(f"\n## {j.title}\n")
            print(f"\n=== {j.title} ===")
            for user in j.turns:
                time.sleep(PAUSE_S)  # free-tier token-per-minute limits: space out turns
                r = agent.run_turn(history, user)
                results.append(r)
                print(f"You:    {user}")
                for e in r.events:
                    status = "ok" if e.ok else e.result.get("error_code")
                    print(f"  [tool] {e.name} {json.dumps(e.arguments, ensure_ascii=False)} -> {status}")
                print(f"Goodie: {r.reply}\n")
                if r.error:
                    print(f"  [error] {r.error}\n")
                    if r.error.startswith("quota"):
                        raise SystemExit("\nStopped: LLM quota exhausted. Wait for the reset or switch model, then re-run.")
                md.append(f"**Guest:** {user}\n")
                for e in r.events:
                    status = "✅" if e.ok else f"⚠️ {e.result.get('error_code')}"
                    md.append(f"> `{e.name}({json.dumps(e.arguments, ensure_ascii=False)})` → {status}\n")
                md.append(f"**Goodie:** {r.reply}\n")
            checks = j.check(results) + grounded_availability(results)
            ok = all(c for c, _ in checks)
            passed += ok
            md.append(f"\n**Checks: {'PASS' if ok else 'FAIL'}**\n")
            md += [f"- {'✅' if c else '❌'} {why}" for c, why in checks]
            md.append("")
            for c, why in checks:
                print(f"    {'ok  ' if c else 'FAIL'} {why}")
            print(f"--> {'PASS' if ok else 'FAIL'}")
    finally:
        toolbox.close()
        out = ROOT / "docs" / "TRANSCRIPTS.md"  # written even if a quota error stops the run early
        out.write_text("\n".join(md), encoding="utf-8")
    print(f"\n{passed}/{total} journeys passed. Transcripts saved to {out.relative_to(ROOT)}")
    return 0 if passed == total else 1


if __name__ == "__main__":
    raise SystemExit(run(set(sys.argv[1:]) or None))
