"""Streamlit chat UI for the GoodFoods reservation agent.   Run:  streamlit run app.py"""

from __future__ import annotations

import json

import streamlit as st

from goodfoods import config
from goodfoods.agent.core import ReservationAgent
from goodfoods.agent.llm import LLMError, OpenAICompatibleClient
from goodfoods.agent.mcp_bridge import MCPToolbox
from goodfoods.agent.prompts import GREETING

st.set_page_config(page_title="GoodFoods Reservations", page_icon="🍽️", layout="centered")

TOOL_LABELS = {
    "search_restaurants": "🔎 Searched outlets",
    "recommend_restaurants": "⭐ Ranked recommendations",
    "get_restaurant_details": "📍 Fetched outlet details",
    "check_availability": "🗓️ Checked availability",
    "create_reservation": "✅ Created reservation",
    "get_reservation": "📄 Looked up reservation",
    "modify_reservation": "✏️ Modified reservation",
    "cancel_reservation": "❌ Cancelled reservation",
}
SAMPLES = [
    "Suggest a romantic place for our anniversary this Saturday, around ₹1500 per person",
    "Table for 4 at a North Indian place in Koramangala tomorrow at 8",
    "Kal raat 8 baje 2 logon ke liye Indiranagar mein biryani wali jagah suggest karo",
    "I want to cancel my booking",
    "We're 25 people for an office party next Friday",
    "Can you write me a poem about cats?",
]


@st.cache_resource(show_spinner="Starting the GoodFoods tool server (MCP)…")
def get_toolbox() -> MCPToolbox:
    return MCPToolbox()


def get_agent() -> ReservationAgent | None:
    try:
        return ReservationAgent(OpenAICompatibleClient(), get_toolbox())
    except LLMError as e:
        st.error(f"{e} — copy `.env.example` to `.env` and add your key.")
        return None


def reset() -> None:
    st.session_state.history = []  # LLM-format messages
    st.session_state.chat = [{"role": "assistant", "content": GREETING, "events": []}]  # display


if "chat" not in st.session_state:
    reset()

with st.sidebar:
    st.header("🍽️ GoodFoods")
    st.caption(f"Model: `{config.LLM_MODEL}` · Tools via MCP")
    show_trace = st.toggle("Show agent tool calls", value=True)
    if st.button("New conversation", use_container_width=True):
        reset()
        st.rerun()
    st.subheader("Try asking")
    for s in SAMPLES:
        if st.button(s, use_container_width=True, key=s):
            st.session_state.pending = s
            st.rerun()

st.title("GoodFoods Reservations")
st.caption("75 outlets across Bengaluru · recommendations · live availability · book, change or cancel")


def render_events(events: list[dict]) -> None:
    if not events or not show_trace:
        return
    with st.expander(f"Agent actions ({len(events)})", expanded=False):
        for ev in events:
            status = "" if ev["ok"] else " — ⚠️ " + ev["result"].get("error_code", "error")
            st.markdown(f"**{TOOL_LABELS.get(ev['name'], ev['name'])}**{status} · {ev['ms']} ms")
            st.code(json.dumps(ev["arguments"], ensure_ascii=False, indent=1), language="json")
            st.json(ev["result"], expanded=False)


for msg in st.session_state.chat:
    with st.chat_message(msg["role"], avatar="🧑" if msg["role"] == "user" else "🍽️"):
        st.markdown(msg["content"])
        render_events(msg.get("events", []))

prompt = st.chat_input("Ask about a table, a cuisine, or your booking…")
prompt = prompt or st.session_state.pop("pending", None)

if prompt:
    st.session_state.chat.append({"role": "user", "content": prompt})
    with st.chat_message("user", avatar="🧑"):
        st.markdown(prompt)
    agent = get_agent()
    if agent:
        with st.chat_message("assistant", avatar="🍽️"):
            with st.spinner("Thinking…"):
                result = agent.run_turn(st.session_state.history, prompt)
            st.markdown(result.reply)
            events = [{"name": e.name, "arguments": e.arguments, "result": e.result, "ok": e.ok,
                       "ms": e.duration_ms} for e in result.events]
            render_events(events)
            if result.error and show_trace:
                st.caption(f"⚠️ {result.error}")
        st.session_state.chat.append({"role": "assistant", "content": result.reply, "events": events})
