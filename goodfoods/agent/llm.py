"""Minimal OpenAI-compatible chat client (NVIDIA NIM + Nemotron 3 Nano by default) written directly on httpx.

No SDKs or agent frameworks: the request/response contract is small enough to own, and owning
it lets us handle the failure modes small models actually hit (malformed tool calls, 429s).
"""

from __future__ import annotations

import json
import logging
import re
import time
from dataclasses import dataclass, field
from typing import Any, Protocol

import httpx

from goodfoods import config

log = logging.getLogger("goodfoods.llm")


@dataclass
class ToolCall:
    id: str
    name: str
    arguments_raw: str  # keep raw text: parsing (and its failure) is the agent's job
    extra: dict | None = None  # provider-specific fields (e.g. Gemini thought_signature) echoed back verbatim


@dataclass
class LLMResponse:
    content: str | None
    tool_calls: list[ToolCall] = field(default_factory=list)
    usage: dict[str, Any] = field(default_factory=dict)

    def as_message(self) -> dict:
        """Assistant message to append to history, in OpenAI wire format."""
        msg: dict[str, Any] = {"role": "assistant", "content": self.content or ""}
        if self.tool_calls:
            msg["tool_calls"] = [
                {**(tc.extra or {}), "id": tc.id, "type": "function",
                 "function": {"name": tc.name, "arguments": tc.arguments_raw}}
                for tc in self.tool_calls
            ]
        return msg


class LLMError(Exception):
    """Raised when the provider fails after retries. ``kind`` lets the agent pick a recovery."""

    def __init__(self, kind: str, message: str, failed_generation: str | None = None):
        super().__init__(message)
        self.kind, self.failed_generation = kind, failed_generation


class ChatModel(Protocol):
    def chat(self, messages: list[dict], tools: list[dict] | None = None) -> LLMResponse: ...


class OpenAICompatibleClient:
    def __init__(self, api_key: str | None = None, base_url: str | None = None, model: str | None = None,
                 temperature: float | None = None, timeout_s: float | None = None, max_retries: int = 5):
        self.api_key = api_key if api_key is not None else config.LLM_API_KEY
        if not self.api_key:
            raise LLMError("config", "No API key found. Set LLM_API_KEY in your .env file (see .env.example).")
        self.base_url = (base_url or config.LLM_BASE_URL).rstrip("/")
        self.model = model or config.LLM_MODEL
        self.temperature = config.LLM_TEMPERATURE if temperature is None else temperature
        self.max_retries = max_retries
        self._http = httpx.Client(timeout=timeout_s or config.LLM_TIMEOUT_S,
                                  headers={"Authorization": f"Bearer {self.api_key}"})

    def chat(self, messages: list[dict], tools: list[dict] | None = None) -> LLMResponse:
        payload: dict[str, Any] = {"model": self.model, "messages": messages, "temperature": self.temperature}
        if tools:
            payload.update(tools=tools, tool_choice="auto")
        if config.LLM_MAX_TOKENS > 0:
            payload["max_tokens"] = config.LLM_MAX_TOKENS
        if "gpt-oss" in self.model:
            # reasoning models: keep hidden reasoning short -> fewer tokens, lower latency, fewer 429s
            payload["reasoning_effort"] = config.LLM_REASONING_EFFORT
        thinking = thinking_kwargs(self.model)
        if thinking is not None:
            payload["chat_template_kwargs"] = thinking
        delay = 2.0
        for attempt in range(1, self.max_retries + 1):
            try:
                resp = self._http.post(f"{self.base_url}/chat/completions", json=payload)
            except httpx.TimeoutException:
                if attempt == self.max_retries:
                    raise LLMError("timeout", "The language model timed out.")
                time.sleep(delay)
                delay *= 2
                continue
            except httpx.HTTPError as e:
                raise LLMError("network", f"Could not reach the language model: {e}") from e

            if resp.status_code == 200:
                return self._parse(resp.json())
            body = _safe_json(resp)
            err = body.get("error", {}) if isinstance(body, dict) else {}
            if resp.status_code == 400 and err.get("code") == "tool_use_failed":
                # Groq returns this when the model emits an invalid tool call; let the agent repair it.
                raise LLMError("tool_use_failed", err.get("message", "invalid tool call"),
                               failed_generation=err.get("failed_generation"))
            if resp.status_code in (429, 500, 502, 503, 504) and attempt < self.max_retries:
                wait = _retry_after(resp) or delay
                if resp.status_code == 429 and wait > 60:
                    # a multi-minute wait means the daily/token quota is used up: retrying just burns time
                    raise LLMError("quota", f"Provider quota exhausted (resets in ~{int(wait // 60)} min). "
                                            "Wait, or switch LLM_MODEL / provider in .env.")
                log.warning("LLM %s, retrying in %.1fs (attempt %d/%d)", resp.status_code, wait, attempt, self.max_retries)
                time.sleep(min(wait, 30))
                delay = min(delay * 2, 30)
                continue
            if resp.status_code in (401, 403):
                raise LLMError("auth", "The API key was rejected. Check LLM_API_KEY in .env "
                                       "(it must belong to the LLM_BASE_URL provider).")
            if resp.status_code == 429:
                raise LLMError("rate_limit", "Rate limit reached. Please wait a moment and try again.")
            raise LLMError("http", f"LLM error {resp.status_code}: {err.get('message') or resp.text[:200]}")
        raise LLMError("unknown", "LLM request failed.")  # pragma: no cover

    @staticmethod
    def _parse(data: dict) -> LLMResponse:
        choice = data["choices"][0]
        msg = choice["message"]
        calls = [
            ToolCall(id=tc.get("id") or f"call_{i}", name=tc["function"]["name"],
                     arguments_raw=tc["function"].get("arguments") or "{}",
                     extra={k: v for k, v in tc.items() if k not in ("id", "type", "function", "index")} or None)
            for i, tc in enumerate(msg.get("tool_calls") or [])
        ]
        # reasoning_content / reasoning are deliberately ignored: chain-of-thought never reaches the guest
        raw = msg.get("content")
        if is_malformed(raw):
            raise LLMError("malformed_output", "model output contained raw tokens or a tool call written as text",
                           failed_generation=(raw or "")[:500])
        content = sanitize_content(raw)
        if choice.get("finish_reason") == "length" and not calls:
            raise LLMError("malformed_output", "model output was cut off (max_tokens)",
                           failed_generation=(raw or "")[:500])
        return LLMResponse(content=content, tool_calls=calls, usage=data.get("usage", {}))


THINK_BLOCK_RE = re.compile(r"<think(?:ing)?>.*?</think(?:ing)?>", re.S | re.I)
# a reply that starts mid-thought (template opened <think> in the prompt) and closes it in the content
THINK_PREFIX_RE = re.compile(r"^.*?</think(?:ing)?>", re.S | re.I)
# raw special tokens or a tool call printed as text: the response is broken, not something to show a guest
MALFORMED_MARKERS = ("<unk>", "<tool_call>", "</tool_call>", "</function>", "<function=")


def thinking_kwargs(model: str) -> dict | None:
    """chat_template_kwargs to send for this model, or None to leave the field out."""
    mode = config.LLM_THINKING
    if mode in ("off", "false", "0"):
        return {"enable_thinking": False}
    if mode in ("on", "true", "1"):
        return {"enable_thinking": True}
    return None


def is_malformed(content: str | None) -> bool:
    return bool(content) and any(m in content.lower() for m in MALFORMED_MARKERS)


def sanitize_content(content: str | None) -> str | None:
    """Strip any reasoning that leaked into the user-facing content."""
    if not content:
        return content
    text = THINK_BLOCK_RE.sub("", content)
    text = THINK_PREFIX_RE.sub("", text)
    # an unclosed <think> means everything after it is reasoning
    text = re.split(r"<think(?:ing)?>", text, maxsplit=1, flags=re.I)[0]
    return text.strip()


def _retry_after(resp: httpx.Response) -> float | None:
    """Seconds to wait from Retry-After or Groq's 'try again in 7.66s' message."""
    header = resp.headers.get("retry-after")
    try:
        if header:
            return float(header) + 0.5
    except ValueError:
        pass
    m = re.search(r"try again in ([\d.]+)(ms|s)", resp.text or "")
    if m:
        v = float(m.group(1))
        return (v / 1000 if m.group(2) == "ms" else v) + 0.5
    return None


def _safe_json(resp: httpx.Response) -> Any:
    try:
        return resp.json()
    except (json.JSONDecodeError, ValueError):
        return {}
