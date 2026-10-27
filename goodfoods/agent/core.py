"""The agent loop, built from scratch (no LangChain / LlamaIndex / agent SDKs).

Per user turn:
  1. send system prompt + trimmed history + tool schemas to the LLM
  2. if the model asks for tools -> validate & execute each call via MCP, append results, loop
  3. if the model answers in text -> run output guards, return
Intent is decided by the LLM (which tool, or none); nothing is routed with keyword rules.
"""

from __future__ import annotations

import json
import logging
import re
import time
from dataclasses import dataclass, field
from datetime import date
from typing import Any, Protocol

from goodfoods import config, dates
from goodfoods.agent import prompts
from goodfoods.agent.llm import ChatModel, LLMError, LLMResponse

log = logging.getLogger("goodfoods.agent")

CODE_RE = re.compile(r"\bGF-[A-Z0-9]{5,6}\b")
DASH_RE = re.compile(r"\bGF[\u2010-\u2015\u2212\u00ad\uFE63\uFF0D]")
# Deterministic safety net for write actions: the model may set user_confirmed=true, but we only
# honour it if the guest's latest message actually reads as a yes. This is a guard, not intent routing.
AFFIRM_RE = re.compile(
    r"\b(yes|yeah|yep|yup|sure|ok|okay|confirm(ed)?|go ahead|book it|do it|please do|correct|perfect|"
    r"sounds good|go for it|lock it|haan|han|haanji|ji haan|theek hai|thik hai|kar do|kardo|chalega|bilkul|"
    r"cancel it|cancel karo|cancel kar do)\b", re.I)
NEGATE_RE = re.compile(r"\b(no|nah|nope|not|don'?t|wait|hold on|nahi|nahin|mat|ruko|change)\b", re.I)
# Statements that a table is free/full. Questions ("shall I check if a table is available?") are not claims.
AVAIL_CLAIM_RE = re.compile(
    r"\b(is|are|was|has been|have been)\s+(still\s+)?(available|free|open for you)\b|"
    r"\bavailab(le|ility) (is )?confirmed\b|"
    r"\bconfirmed (that )?(a |the |your )?table\b|\b(fully booked|is full|are full|booked out|sold out)\b|"
    r"\b(table|slot)s?\s+(is |are )?(available|free)\b|\b(available|khaali|khali) hai\b|\bmil jayegi\b", re.I)
QUESTION_HINT_RE = re.compile(r"\b(check|whether|if|agar|kya)\b", re.I)
AVAIL_TOOLS = {"check_availability", "create_reservation", "modify_reservation"}
DATE_ARGS = ("date", "new_date")
# "25 people", "hum 25 log", "party of 25", "group for 25": a guard on the guest's words, like AFFIRM_RE
PARTY_RE = re.compile(
    r"\b(\d{1,3})\s*(?:people|persons?|guests?|pax|ppl|log|logon|logo|members|adults|of us)\b"
    r"|\b(?:party|group)\s+(?:of|for)\s+(\d{1,3})\b", re.I)  # not "table for 25 nov": that is a date
EVENTS_MARKERS = ("events@goodfoods.in", "4000 1234")
MAX_TOOL_RESULT_CHARS = 3500
WRITE_TOOLS = {"create_reservation", "modify_reservation", "cancel_reservation"}


class Toolbox(Protocol):
    tools: list[dict]

    @property
    def names(self) -> set[str]: ...

    def call(self, name: str, arguments: dict[str, Any]) -> dict: ...


@dataclass
class ToolEvent:
    """One tool invocation, kept for the UI trace and for tests/evals."""
    name: str
    arguments: dict | str
    result: dict
    duration_ms: int

    @property
    def ok(self) -> bool:
        return bool(self.result.get("ok", True))


@dataclass
class TurnResult:
    reply: str
    events: list[ToolEvent] = field(default_factory=list)
    llm_calls: int = 0
    error: str | None = None


class ReservationAgent:
    def __init__(self, llm: ChatModel, toolbox: Toolbox, max_steps: int | None = None,
                 history_limit: int | None = None):
        self.llm, self.toolbox = llm, toolbox
        self.max_steps = max_steps or config.AGENT_MAX_STEPS
        self.history_limit = history_limit or config.AGENT_HISTORY_MESSAGES

    # ------------------------------------------------------------------ public
    def run_turn(self, history: list[dict], user_message: str) -> TurnResult:
        """``history`` (OpenAI-format messages, no system prompt) is extended in place."""
        user_message = (user_message or "").strip()[:2000]
        if not user_message:
            return TurnResult(reply="Sorry, I didn't catch that. Could you type it again?")
        history.append({"role": "user", "content": user_message})
        result = TurnResult(reply="")
        repaired = reformatted = grounded = avail_fixed = events_fixed = False
        extra_system: list[str] = []

        for _ in range(self.max_steps):
            messages = self._build_messages(history, extra_system)
            try:
                result.llm_calls += 1
                resp = self.llm.chat(messages, self.toolbox.tools)
            except LLMError as e:
                if e.kind == "tool_use_failed" and not repaired:
                    log.warning("malformed tool call, repairing: %s", e.failed_generation)
                    repaired = True
                    extra_system = [prompts.REPAIR_TOOL_CALL]
                    continue
                if e.kind == "malformed_output" and not reformatted:
                    # leaked reasoning / <unk> tokens / a tool call typed as text: never show it, retry once
                    log.warning("malformed model output, retrying: %s", e.failed_generation)
                    reformatted = True
                    extra_system = [prompts.MALFORMED_OUTPUT_FIX]
                    continue
                return self._fail(result, history, e)

            if resp.tool_calls:
                history.append(resp.as_message())
                for tc in resp.tool_calls:
                    event = self._execute(tc.name, tc.arguments_raw, last_user=user_message, history=history)
                    result.events.append(event)
                    history.append({"role": "tool", "tool_call_id": tc.id, "name": tc.name,
                                    "content": _truncate(json.dumps(event.result, ensure_ascii=False))})
                extra_system = []
                continue

            # models sometimes print "GF‑1ANHOH" with a Unicode hyphen; normalise so codes copy/paste and match
            reply = DASH_RE.sub("GF-", (resp.content or "").strip())
            if not grounded and self._invented_code(reply, history):
                grounded = True
                extra_system = [prompts.GROUNDING_FIX]
                continue
            if not avail_fixed and claims_availability(reply) and not availability_known(history):
                avail_fixed = True
                extra_system = [prompts.AVAILABILITY_FIX]
                continue
            if needs_events_contact(user_message, result.events) and not has_events_contact(reply):
                if not events_fixed:
                    events_fixed = True
                    extra_system = [prompts.LARGE_PARTY_FIX]
                    continue
                # the model ignored the correction: the guest still gets the contact, deterministically
                reply = (reply + "\n\n" if reply else "") + prompts.LARGE_PARTY_REPLY
            reply = reply or "Sorry, I lost my train of thought. Could you say that again?"
            history.append({"role": "assistant", "content": reply})
            result.reply = reply
            return result

        result.reply = ("I'm having trouble completing that right now. Could you rephrase, or tell me the outlet, "
                        "date, time and number of guests?")
        result.error = "max_steps"
        history.append({"role": "assistant", "content": result.reply})
        return result

    # ---------------------------------------------------------------- helpers
    def _build_messages(self, history: list[dict], extra_system: list[str]) -> list[dict]:
        msgs = [{"role": "system", "content": prompts.build_system_prompt()}]
        msgs += self._trim(history)
        msgs += [{"role": "system", "content": s} for s in extra_system]
        options = options_note(history)
        if options:
            msgs.append({"role": "system", "content": options})
        # small models follow the most recent instruction best: restate the key rules last
        msgs.append({"role": "system", "content": prompts.TURN_REMINDER})
        return msgs

    def _trim(self, history: list[dict]) -> list[dict]:
        """Keep the latest messages but never start on an orphaned tool result."""
        if len(history) <= self.history_limit:
            return list(history)
        window = history[-self.history_limit:]
        while window and window[0]["role"] != "user":
            window = window[1:]
        return window

    def _execute(self, name: str, arguments_raw: str, last_user: str = "",
                 history: list[dict] | None = None) -> ToolEvent:
        start = time.perf_counter()
        if name not in self.toolbox.names:
            result = {"ok": False, "error_code": "UNKNOWN_TOOL", "message": f"There is no tool called '{name}'.",
                      "hint": f"Use one of: {', '.join(sorted(self.toolbox.names))}."}
            return ToolEvent(name, arguments_raw, result, _ms(start))
        try:
            args = json.loads(arguments_raw or "{}")
            if not isinstance(args, dict):
                raise ValueError("arguments must be a JSON object")
        except (json.JSONDecodeError, ValueError) as e:
            result = {"ok": False, "error_code": "BAD_ARGUMENTS", "message": f"Arguments were not valid JSON: {e}",
                      "hint": "Call the tool again with a JSON object."}
            return ToolEvent(name, arguments_raw, result, _ms(start))
        args = {k: v for k, v in args.items() if v not in (None, "", "null")}  # small models love empty strings
        args = reconcile_dates(args, last_user)
        rid = str(args.get("restaurant_id", ""))
        if history is not None and ID_RE.fullmatch(rid.upper()) and not _id_seen(rid, history):
            # small models sometimes invent an id (e.g. GF012) instead of searching: refuse it
            result = {"ok": False, "error_code": "UNVERIFIED_RESTAURANT_ID",
                      "message": f"restaurant_id {rid} did not come from any tool result.",
                      "hint": "Never guess ids. Call search_restaurants with the outlet name, or pass the "
                              "outlet name itself as restaurant_id."}
            return ToolEvent(name, args, result, _ms(start))
        if name in WRITE_TOOLS:
            if isinstance(args.get("user_confirmed"), str):
                args["user_confirmed"] = args["user_confirmed"].strip().lower() == "true"
            if args.get("user_confirmed") and not (_is_affirmative(last_user) and _asked_to_confirm(history)):
                result = {"ok": False, "error_code": "CONFIRMATION_REQUIRED",
                          "message": "The guest has not said yes to a read-back summary in their latest message.",
                          "hint": "Summarise the details and ask the guest to confirm before calling this tool."}
                return ToolEvent(name, args, result, _ms(start))
        try:
            result = self.toolbox.call(name, args)
        except Exception as e:  # MCP transport / validation error
            log.exception("tool call failed")
            result = {"ok": False, "error_code": "TOOL_FAILED", "message": str(e)[:300],
                      "hint": "Check the argument names and types against the tool schema and retry once."}
        return ToolEvent(name, args, result, _ms(start))

    @staticmethod
    def _invented_code(reply: str, history: list[dict]) -> bool:
        codes = set(CODE_RE.findall(reply))
        if not codes:
            return False
        seen = " ".join(m.get("content") or "" for m in history if m["role"] in ("tool", "user"))
        return any(c not in seen for c in codes)

    @staticmethod
    def _fail(result: TurnResult, history: list[dict], e: LLMError) -> TurnResult:
        friendly = {
            "rate_limit": "I'm getting a lot of requests right now. Please try again in a few seconds.",
            "quota": "I've hit today's usage limit with my language model. Please try again a little later.",
            "auth": "I can't reach my language model (API key problem). Please check the configuration.",
            "config": str(e),
            "malformed_output": "Sorry, I got muddled there. Could you say that again?",
        }.get(e.kind, "Sorry, I'm having trouble connecting right now. Please try again in a moment.")
        result.reply, result.error = friendly, f"{e.kind}: {e}"
        # do not persist the failed user turn's partial state beyond the user message itself
        history.append({"role": "assistant", "content": friendly})
        return result


ID_RE = re.compile(r"GF\d{3}")


def _id_seen(rid: str, history: list[dict]) -> bool:
    rid = rid.upper()
    return any(rid in (m.get("content") or "").upper() for m in history if m["role"] in ("tool", "user"))


def _is_affirmative(text: str) -> bool:
    return bool(AFFIRM_RE.search(text or "")) and not NEGATE_RE.search(text or "")


def _asked_to_confirm(history: list[dict] | None) -> bool:
    """The assistant's last text reply before the guest's latest message must have asked a question."""
    if history is None:
        return True
    last_user = max((i for i, m in enumerate(history) if m["role"] == "user"), default=-1)
    prior = [m for m in history[:last_user] if m["role"] == "assistant" and (m.get("content") or "").strip()]
    return bool(prior) and "?" in prior[-1]["content"]


OPTION_TOOLS = {"recommend_restaurants", "search_restaurants"}
# regex, not json.loads: the history copy of a long result may be truncated mid-list
OPTION_RE = re.compile(r'"option": (\d+), "restaurant_id": "([^"]+)", "name": "([^"]+)"')


def last_options(history: list[dict]) -> list[tuple[int, str, str]]:
    """(option, restaurant_id, name) from the latest recommend/search result that returned outlets."""
    for m in reversed(history):
        if m["role"] == "tool" and m.get("name") in OPTION_TOOLS:
            found = OPTION_RE.findall(m.get("content") or "")
            if found:
                return [(int(n), rid, name) for n, rid, name in found]
    return []


def options_note(history: list[dict]) -> str | None:
    """Restate the numbered list so "the first one" / "doosra wala" resolves to an exact restaurant_id."""
    items = last_options(history)
    if not items:
        return None
    lines = [f"{n}. {name} -> restaurant_id {rid}" for n, rid, name in items]
    return ("Outlets from the latest search, in the order shown to the guest:\n" + "\n".join(lines) +
            "\nIf the guest refers to one by position (\"the first one\", \"second\", \"pehla wala\") or by name, "
            "use exactly that restaurant_id in check_availability and create_reservation. Never show restaurant_ids "
            "to the guest.")


def reconcile_dates(args: dict, last_user: str) -> dict:
    """If the guest's latest message says "next friday"/"kal"/"this weekend" but the model sent a different
    calendar date (its own arithmetic), send the guest's phrase instead: the server resolves it deterministically."""
    found = dates.find_relative(last_user)
    if not found or dates.has_explicit_date(last_user):
        return args
    phrase, resolved = found
    out = dict(args)
    for key in DATE_ARGS:
        value = str(out.get(key, "")).strip()
        try:
            given = date.fromisoformat(value)
        except ValueError:
            continue  # already words (resolved by the server) or absent
        if given != resolved:
            log.warning("date %s for %r overridden: guest said %r (= %s)", value, key, phrase, resolved)
            out[key] = phrase
    return out


def stated_party_size(text: str) -> int | None:
    sizes = [int(a or b) for a, b in PARTY_RE.findall(text or "")]
    return max(sizes) if sizes else None


def needs_events_contact(last_user: str, events: list[ToolEvent]) -> bool:
    """Guest said more than the online limit, or a tool this turn said PARTY_TOO_LARGE."""
    size = stated_party_size(last_user)
    return (size is not None and size > config.MAX_ONLINE_PARTY) or any(
        e.result.get("error_code") == "PARTY_TOO_LARGE" for e in events)


def has_events_contact(reply: str) -> bool:
    return any(m in (reply or "") for m in EVENTS_MARKERS)


def claims_availability(reply: str) -> bool:
    """True if a *statement* in the reply says a table is free or full (questions are ignored)."""
    for sentence in re.split(r"(?<=[.!?\n])", reply or ""):
        s = sentence.strip()
        if s and not s.endswith("?") and not QUESTION_HINT_RE.search(s) and AVAIL_CLAIM_RE.search(s):
            return True
    return False


def availability_known(history: list[dict]) -> bool:
    """Did any tool in this conversation actually check a slot?"""
    return any(m["role"] == "tool" and (m.get("name") in AVAIL_TOOLS
                                        or '"availability_checked": true' in (m.get("content") or ""))
               for m in history)


def _ms(start: float) -> int:
    return int((time.perf_counter() - start) * 1000)


def _truncate(text: str) -> str:
    return text if len(text) <= MAX_TOOL_RESULT_CHARS else text[:MAX_TOOL_RESULT_CHARS] + '..."(truncated)"'
