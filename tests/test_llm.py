"""LLM client output handling: reasoning never reaches the guest, broken generations are retried."""

import json

import pytest

from goodfoods import config
from goodfoods.agent import llm as llm_mod
from goodfoods.agent.core import ReservationAgent, options_note
from goodfoods.agent.llm import LLMError, LLMResponse, OpenAICompatibleClient, is_malformed, sanitize_content


def _completion(content, reasoning=None, tool_calls=None, finish="stop"):
    msg = {"role": "assistant", "content": content, "reasoning_content": reasoning}
    if tool_calls:
        msg["tool_calls"] = tool_calls
    return {"choices": [{"message": msg, "finish_reason": finish}], "usage": {}}


@pytest.mark.parametrize("raw,clean", [
    ("Hello there!", "Hello there!"),
    ("<think>The user wants a table. I should call...</think>\nHere are 3 options.", "Here are 3 options."),
    ("<thinking>plan</thinking>Sure!", "Sure!"),
    ("We need to check availability first.</think>\n\nWhich time works?", "Which time works?"),
    ("Sure, one moment.<think>now call check_availability", "Sure, one moment."),
    (None, None),
    ("", ""),
])
def test_sanitize_strips_reasoning(raw, clean):
    assert sanitize_content(raw) == clean


@pytest.mark.parametrize("raw,bad", [
    ("Here are your options.", False),
    ("We need to call check_availability with restaurant_id,<unk><unk><unk>", True),
    ('<tool_call>{"name": "check_availability"}</tool_call>', True),
    ("<function=check_availability>{}</function>", True),
    ("</parameter>\n</function>\n</tool_call>", True),
    (None, False),
])
def test_malformed_detection(raw, bad):
    assert is_malformed(raw) is bad


def test_parse_ignores_reasoning_content():
    resp = OpenAICompatibleClient._parse(_completion("51", reasoning="User asks 17*3, compute 51."))
    assert resp.content == "51"


def test_parse_rejects_leaked_reasoning_with_unk_tokens():
    """Live bug (Nemotron): chain-of-thought, then <unk> tokens and a tool call typed as text."""
    raw = "We need to handle the request... call check_availability,<unk><unk><unk>\n</parameter>\n</function>"
    with pytest.raises(LLMError) as e:
        OpenAICompatibleClient._parse(_completion(raw))
    assert e.value.kind == "malformed_output"


def test_parse_rejects_truncated_text():
    with pytest.raises(LLMError) as e:
        OpenAICompatibleClient._parse(_completion("Here are the outlets: 1. GoodFoods", finish="length"))
    assert e.value.kind == "malformed_output"


def test_parse_keeps_real_tool_calls():
    tc = [{"id": "c1", "type": "function", "function": {"name": "check_availability", "arguments": "{}"}}]
    resp = OpenAICompatibleClient._parse(_completion(None, tool_calls=tc))
    assert resp.tool_calls[0].name == "check_availability"


@pytest.mark.parametrize("mode,model,expected", [
    ("", "nvidia/nemotron-3-nano-omni-30b-a3b-reasoning", None),
    ("", "openai/gpt-oss-20b", None),
    ("on", "nvidia/nemotron-3-nano-omni-30b-a3b-reasoning", {"enable_thinking": True}),
    ("off", "nvidia/nemotron-3-nano-omni-30b-a3b-reasoning", {"enable_thinking": False}),
])
def test_thinking_toggle(monkeypatch, mode, model, expected):
    monkeypatch.setattr(config, "LLM_THINKING", mode)
    assert llm_mod.thinking_kwargs(model) == expected


def test_request_sends_thinking_toggle_and_max_tokens(monkeypatch):
    sent = {}

    class FakeHTTP:
        def post(self, url, json):
            sent.update(json)
            import httpx
            return httpx.Response(200, json=_completion("hi"))

    monkeypatch.setattr(config, "LLM_THINKING", "off")
    client = OpenAICompatibleClient(api_key="k", base_url="https://integrate.api.nvidia.com/v1",
                                    model="nvidia/nemotron-3-nano-omni-30b-a3b-reasoning")
    client._http = FakeHTTP()
    client.chat([{"role": "user", "content": "hi"}])
    assert sent["chat_template_kwargs"] == {"enable_thinking": False}
    assert sent["max_tokens"] == config.LLM_MAX_TOKENS >= 2048


class _Scripted:
    def __init__(self, steps):
        self.steps, self.seen = list(steps), []

    def chat(self, messages, tools=None):
        self.seen.append(messages)
        step = self.steps.pop(0)
        if isinstance(step, Exception):
            raise step
        return step


def test_agent_retries_malformed_output_once(toolbox):
    llm = _Scripted([LLMError("malformed_output", "x", failed_generation="We need to...<unk>"),
                     LLMResponse(content="Which date and time should I check?")])
    r = ReservationAgent(llm, toolbox).run_turn([], "Table for 6 at Andhra Spice BTM")
    assert r.reply == "Which date and time should I check?" and r.error is None
    assert any("malformed" in m["content"] for m in llm.seen[1] if m["role"] == "system")


def test_agent_gives_up_after_second_malformed_output(toolbox):
    bad = LLMError("malformed_output", "x", failed_generation="<unk>")
    r = ReservationAgent(_Scripted([bad, bad]), toolbox).run_turn([], "hi")
    assert "<unk>" not in r.reply and "muddled" in r.reply and r.error.startswith("malformed_output")


def test_options_note_maps_positions_to_ids_even_when_truncated():
    recs = {"ok": True, "recommendations": [
        {"option": 1, "restaurant_id": "GF002", "name": "GoodFoods Tandoor House - Whitefield", "why": "x" * 2000},
        {"option": 2, "restaurant_id": "GF058", "name": "GoodFoods Sakura - MG Road", "why": "y" * 2000},
    ]}
    content = json.dumps(recs, ensure_ascii=False)[:3000]  # second item's tail is cut off, like history truncation
    note = options_note([{"role": "tool", "name": "recommend_restaurants", "content": content}])
    assert "1. GoodFoods Tandoor House - Whitefield -> restaurant_id GF002" in note
    assert "2. GoodFoods Sakura - MG Road -> restaurant_id GF058" in note
    assert options_note([{"role": "user", "content": "hi"}]) is None
