"""Central configuration. Everything is overridable through environment variables (.env)."""

from __future__ import annotations

import os
from pathlib import Path
from zoneinfo import ZoneInfo

try:  # .env is optional; plain env vars work too
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:  # pragma: no cover
    pass

ROOT_DIR = Path(__file__).resolve().parent.parent

# --- LLM -------------------------------------------------------------------
LLM_API_KEY = os.getenv("LLM_API_KEY") or os.getenv("NVIDIA_API_KEY") or os.getenv("GROQ_API_KEY", "")
LLM_BASE_URL = os.getenv("LLM_BASE_URL", "https://integrate.api.nvidia.com/v1")
LLM_MODEL = os.getenv("LLM_MODEL", "nvidia/nemotron-3-nano-omni-30b-a3b-reasoning")
LLM_TEMPERATURE = float(os.getenv("LLM_TEMPERATURE", "0.2"))
LLM_TIMEOUT_S = float(os.getenv("LLM_TIMEOUT_S", "30"))
LLM_REASONING_EFFORT = os.getenv("LLM_REASONING_EFFORT", "low")  # only sent to reasoning models (gpt-oss)
# Room for the reply *and* any tool-call JSON; too low and calls get cut off mid-arguments.
LLM_MAX_TOKENS = int(os.getenv("LLM_MAX_TOKENS", "4096"))
# Thinking toggle for hybrid-reasoning models (NVIDIA Nemotron, Qwen3), sent as chat_template_kwargs.enable_thinking.
# "" (default) = send nothing, the model's own default; "off" / "on" = force it. Reasoning is never shown to the
# guest either way (reasoning_content is ignored and <think> blocks are stripped). Nemotron 3 Nano followed the
# booking rules better with thinking on in our journey runs, so it is left on by default.
LLM_THINKING = os.getenv("LLM_THINKING", "").strip().lower()

# --- Agent -----------------------------------------------------------------
AGENT_MAX_STEPS = int(os.getenv("AGENT_MAX_STEPS", "6"))  # tool round-trips per user turn
AGENT_HISTORY_MESSAGES = int(os.getenv("AGENT_HISTORY_MESSAGES", "30"))

# --- Business rules ----------------------------------------------------------
TIMEZONE = ZoneInfo(os.getenv("GOODFOODS_TZ", "Asia/Kolkata"))
DB_PATH = Path(os.getenv("GOODFOODS_DB", str(ROOT_DIR / "data" / "goodfoods.db")))
DINING_DURATION_MIN = 90       # a table is held for 90 minutes
SLOT_MINUTES = 30              # bookable times are on the half hour
MAX_ONLINE_PARTY = 20          # bigger groups go to the events team
LARGE_PARTY_THRESHOLD = 10     # parties above this need a private-dining venue
BOOKING_WINDOW_DAYS = 60       # how far ahead guests can book
EVENTS_CONTACT = "events@goodfoods.in / +91 80 4000 1234"
