"""Agent-loop tests: a scripted fake LLM drives the real agent against the real MCP server.

This checks the plumbing (tool dispatch, guards, recovery) deterministically, without an API key.
"""

import json
from datetime import date, timedelta

import pytest

from goodfoods.agent.core import ReservationAgent
from goodfoods.agent.llm import LLMError, LLMResponse, ToolCall


class ScriptedLLM:
    """Returns pre-baked responses; each step may be a callable that inspects the messages."""

    def __init__(self, steps):
        self.steps, self.seen = list(steps), []

    def chat(self, messages, tools=None):
        self.seen.append(messages)
        step = self.steps.pop(0)
        if isinstance(step, Exception):
            raise step
        return step(messages) if callable(step) else step


def call(name, **args):
    return LLMResponse(content=None, tool_calls=[ToolCall(id=f"c_{name}", name=name, arguments_raw=json.dumps(args))])


def say(text):
    return LLMResponse(content=text)


def last_tool_result(messages):
    return json.loads(next(m for m in reversed(messages) if m["role"] == "tool")["content"])


def test_tools_are_discovered_over_mcp(toolbox):
    assert {"recommend_restaurants", "check_availability", "create_reservation", "cancel_reservation"} <= toolbox.names
    for t in toolbox.tools:
        assert t["type"] == "function" and t["function"]["parameters"]["type"] == "object"


def test_full_booking_journey(toolbox):
    d = (date.today() + timedelta(days=4)).isoformat()
    llm = ScriptedLLM([
        call("recommend_restaurants", cuisine="North Indian", party_size=2),
        say("Here are a few options... Shall I check GoodFoods Tandoor House?"),
        call("check_availability", restaurant_id="GF002", date=d, time="14:00", party_size=2),
        say("It's free. Name and mobile number?"),
        say("Booking for Hasan, 2 guests... Shall I confirm?"),
        call("create_reservation", restaurant_id="GF002", date=d, time="14:00", party_size=2,
             guest_name="Hasan", phone="9811122233", user_confirmed=True),
        lambda msgs: say(f"Done! Your code is {last_tool_result(msgs)['reservation']['confirmation_code']}."),
    ])
    agent, history = ReservationAgent(llm, toolbox), []
    r1 = agent.run_turn(history, "North Indian for two?")
    assert r1.events[0].name == "recommend_restaurants" and r1.events[0].ok
    r2 = agent.run_turn(history, f"Check GF002 on {d} at 2pm")
    assert r2.events[0].result["available"] is True
    agent.run_turn(history, "Hasan, 9811122233")
    r4 = agent.run_turn(history, "yes please")
    assert r4.events[0].ok and "GF-" in r4.reply
    assert history[-1]["role"] == "assistant"


def test_write_blocked_without_explicit_yes(toolbox):
    d = (date.today() + timedelta(days=4)).isoformat()
    llm = ScriptedLLM([
        call("create_reservation", restaurant_id="GF003", date=d, time="15:00", party_size=2,
             guest_name="Eager Model", phone="9811100000", user_confirmed=True),
        say("Let me confirm the details first: ... Shall I book?"),
    ])
    r = ReservationAgent(llm, toolbox).run_turn([], "Book GF003 for 2 at 3pm, name Eager, 9811100000")
    assert r.events[0].result["error_code"] == "CONFIRMATION_REQUIRED"


def test_malformed_arguments_and_unknown_tool_recover(toolbox):
    llm = ScriptedLLM([
        LLMResponse(content=None, tool_calls=[ToolCall("x1", "search_restaurants", "{cuisine: thai")]),
        LLMResponse(content=None, tool_calls=[ToolCall("x2", "order_food", "{}")]),
        call("search_restaurants", cuisine="thai"),
        say("We have Thai outlets in ..."),
    ])
    r = ReservationAgent(llm, toolbox).run_turn([], "thai places?")
    codes = [e.result.get("error_code") for e in r.events]
    assert codes[:2] == ["BAD_ARGUMENTS", "UNKNOWN_TOOL"] and r.events[2].ok
    assert r.reply.startswith("We have Thai")


def test_provider_tool_use_failed_is_repaired_once(toolbox):
    llm = ScriptedLLM([
        LLMError("tool_use_failed", "bad call", failed_generation="<function=search>"),
        call("search_restaurants", neighborhood="hsr"),
        say("Here are outlets in HSR Layout."),
    ])
    r = ReservationAgent(llm, toolbox).run_turn([], "places in hsr")
    assert r.error is None and r.events[0].ok
    assert any("previous tool call was invalid" in m["content"] for m in llm.seen[1] if m["role"] == "system")


def test_invented_confirmation_code_triggers_rewrite(toolbox):
    llm = ScriptedLLM([say("Booked! Your code is GF-ZZZZZZ."), say("I haven't booked anything yet. Shall I?")])
    r = ReservationAgent(llm, toolbox).run_turn([], "is it booked?")
    assert "GF-ZZZZZZ" not in r.reply


def test_rate_limit_gives_friendly_message(toolbox):
    llm = ScriptedLLM([LLMError("rate_limit", "429")])
    r = ReservationAgent(llm, toolbox).run_turn([], "hi")
    assert "try again" in r.reply.lower() and r.error.startswith("rate_limit")


def test_runaway_loop_is_capped(toolbox):
    llm = ScriptedLLM([call("search_restaurants", cuisine="cafe")] * 10)
    r = ReservationAgent(llm, toolbox, max_steps=3).run_turn([], "loop")
    assert r.error == "max_steps" and len(r.events) == 3


def test_history_trim_never_starts_with_tool_message(toolbox):
    agent = ReservationAgent(ScriptedLLM([]), toolbox, history_limit=4)
    history = [{"role": "user", "content": "a"}, {"role": "assistant", "content": "", "tool_calls": []},
               {"role": "tool", "tool_call_id": "1", "content": "{}"}, {"role": "assistant", "content": "b"},
               {"role": "user", "content": "c"}, {"role": "assistant", "content": "d"}]
    assert agent._trim(history)[0]["role"] == "user"


def test_unicode_hyphen_in_code_is_normalised(toolbox):
    llm = ScriptedLLM([say("Your code is GF\u20111ANHOH and GF\u2013ABCDE1.")])
    history = [{"role": "tool", "tool_call_id": "t", "content": '{"confirmation_code": "GF-1ANHOH", "x": "GF-ABCDE1"}'}]
    r = ReservationAgent(llm, toolbox).run_turn(history, "what is my code?")
    assert "GF-1ANHOH" in r.reply and "GF-ABCDE1" in r.reply


def test_guessed_restaurant_id_is_blocked(toolbox):
    llm = ScriptedLLM([
        call("check_availability", restaurant_id="GF012", date="saturday", time="20:00", party_size=6),
        call("check_availability", restaurant_id="GoodFoods Andhra Spice - BTM Layout", date="saturday",
             time="20:00", party_size=6),
        say("Here is what I found."),
    ])
    r = ReservationAgent(llm, toolbox).run_turn([], "Table for 6 at GoodFoods Andhra Spice - BTM Layout Saturday 8 pm")
    assert r.events[0].result["error_code"] == "UNVERIFIED_RESTAURANT_ID"
    assert r.events[1].ok and "Andhra Spice - BTM Layout" in r.events[1].result["restaurant_name"]



# --- guards added after live runs ---------------------------------------------------------------
def test_unchecked_availability_claim_triggers_rewrite(toolbox):
    """Live bug: after get_restaurant_details only, the model said 'a table for two is available'."""
    llm = ScriptedLLM([
        call("get_restaurant_details", restaurant_id="GoodFoods Tandoor House - Whitefield"),
        say("I have confirmed that a table for two is available this Saturday at 8 pm. Your name?"),
        lambda msgs: say("Which time and how many guests should I check?")
        if any("no tool has checked" in m["content"] for m in msgs if m["role"] == "system") else say("unreachable"),
    ])
    r = ReservationAgent(llm, toolbox).run_turn([], "Check Tandoor House Whitefield please")
    assert r.events[0].result["availability"].startswith("not checked")
    assert "available" not in r.reply and r.reply.startswith("Which time")


def test_availability_claim_after_check_is_allowed(toolbox):
    d = (date.today() + timedelta(days=4)).isoformat()
    llm = ScriptedLLM([
        call("check_availability", restaurant_id="GoodFoods Dosa Co. - Indiranagar", date=d, time="15:00", party_size=2),
        say("Good news, a table for 2 is available at 3 pm. May I have your name and mobile number?"),
    ])
    r = ReservationAgent(llm, toolbox).run_turn([], f"Dosa Co. Indiranagar on {d} at 3 pm for 2")
    assert r.llm_calls == 2 and "available" in r.reply


def _readback_history():
    return [{"role": "user", "content": "book Dosa Co Indiranagar"},
            {"role": "assistant", "content": "Dosa Co. Indiranagar, 2 guests, 3 pm, Asha, 9811100001. Shall I confirm?"}]


@pytest.mark.parametrize("message,allowed", [
    ("yes", True), ("haan kar do", True), ("ok go ahead", True), ("yes please confirm", True),
    ("no, change the time", False), ("great, my name is Asha", False), ("wait", False), ("not yet", False),
])
def test_confirmation_guard_reads_the_guests_actual_words(toolbox, message, allowed):
    d = (date.today() + timedelta(days=9)).isoformat()
    llm = ScriptedLLM([
        call("create_reservation", restaurant_id="GoodFoods Dosa Co. - Indiranagar", date=d, time="15:00",
             party_size=2, guest_name="Asha", phone=f"98111{sum(map(ord, message)) % 100000:05d}", user_confirmed=True),
        say("ok"),
    ])
    r = ReservationAgent(llm, toolbox).run_turn(_readback_history(), message)
    code = r.events[0].result.get("error_code")
    assert (code != "CONFIRMATION_REQUIRED") is allowed, (message, r.events[0].result)


def test_confirmation_guard_needs_a_readback_question_first(toolbox):
    """A 'yes' in the very first message cannot authorise a write: nothing was read back yet."""
    d = (date.today() + timedelta(days=9)).isoformat()
    llm = ScriptedLLM([
        call("create_reservation", restaurant_id="GoodFoods Dosa Co. - Indiranagar", date=d, time="16:00",
             party_size=2, guest_name="Asha", phone="9811100002", user_confirmed=True),
        say("Let me read that back first..."),
    ])
    r = ReservationAgent(llm, toolbox).run_turn([], "Yes book Dosa Co Indiranagar 4pm for 2, Asha 9811100002, confirm")
    assert r.events[0].result["error_code"] == "CONFIRMATION_REQUIRED"


def test_model_cannot_skip_flag_either(toolbox):
    """Even with a real 'yes', the MCP server itself refuses a write whose user_confirmed is false."""
    d = (date.today() + timedelta(days=9)).isoformat()
    llm = ScriptedLLM([
        call("create_reservation", restaurant_id="GoodFoods Dosa Co. - Indiranagar", date=d, time="17:00",
             party_size=2, guest_name="Asha", phone="9811100003", user_confirmed=False),
        say("ok"),
    ])
    r = ReservationAgent(llm, toolbox).run_turn(_readback_history(), "yes")
    assert r.events[0].result["error_code"] == "CONFIRMATION_REQUIRED"


def test_system_prompt_has_scope_and_hinglish_date_rules():
    from datetime import datetime

    from goodfoods import config
    from goodfoods.agent import prompts
    p = prompts.build_system_prompt(datetime(2026, 10, 27, 12, 0, tzinfo=config.TIMEZONE))
    assert "ONLY help with GoodFoods" in p and "poems" in p and "Hinglish" in p
    assert "pass the guest's own words" in p and "never your own calculation" in p
    assert "get_restaurant_details does NOT check tables" in p


def test_bridge_forwards_db_setting_to_mcp_server(tmp_path, monkeypatch):
    """Live bug: the MCP SDK's minimal default env dropped GOODFOODS_DB, so the server used another database."""
    from goodfoods.agent.mcp_bridge import MCPToolbox
    from goodfoods.data.seed import seed
    db = seed(tmp_path / "other.db", reset=True)
    monkeypatch.setenv("GOODFOODS_DB", str(db))
    from goodfoods import store
    d = (date.today() + timedelta(days=5)).isoformat()
    code = store.create_reservation("GF002", d, "15:00", 2, "Env Test", "9811177777", user_confirmed=True,
                                    db_path=db)["reservation"]["confirmation_code"]
    tb = MCPToolbox()  # no explicit env: must inherit GOODFOODS_DB
    try:
        assert tb.call("get_reservation", {"confirmation_code": code, "phone": "9811177777"})["ok"] is True
    finally:
        tb.close()
