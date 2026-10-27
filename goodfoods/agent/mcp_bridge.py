"""Synchronous bridge to the GoodFoods MCP server.

The MCP Python client is async, while Streamlit (and our agent loop) are sync. We run one
long-lived MCP session on a background event loop thread and submit calls to it, so the server
process is started once, not on every message.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import threading
from concurrent.futures import Future
from typing import Any

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from goodfoods import config


class MCPToolbox:
    def __init__(self, command: str | None = None, args: list[str] | None = None, env: dict | None = None,
                 call_timeout_s: float = 20):
        if env is None:
            # the MCP SDK passes only a minimal default env to the server: forward our own settings explicitly
            env = {k: v for k, v in os.environ.items() if k.startswith("GOODFOODS_")}
        self._params = StdioServerParameters(command=command or sys.executable,
                                             args=args or ["-m", "goodfoods.mcp_server"], env=env,
                                             cwd=str(config.ROOT_DIR))  # so `-m goodfoods...` works from any dir
        self._timeout = call_timeout_s
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(target=self._loop.run_forever, daemon=True, name="mcp-loop")
        self._thread.start()
        self._session: ClientSession | None = None
        self._stop: asyncio.Event | None = None
        self._ready: Future = Future()
        self._runner = asyncio.run_coroutine_threadsafe(self._run(), self._loop)
        self.tools: list[dict] = self._ready.result(timeout=30)  # raises if the server failed to start

    async def _run(self) -> None:
        try:
            async with stdio_client(self._params) as (read, write):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    listed = await session.list_tools()
                    self._session, self._stop = session, asyncio.Event()
                    self._ready.set_result([self._to_openai(t) for t in listed.tools])
                    await self._stop.wait()
        except BaseException as e:  # surface start-up failures to the constructor
            if not self._ready.done():
                self._ready.set_exception(e)
            raise

    @staticmethod
    def _to_openai(tool) -> dict:
        """MCP tool -> OpenAI-style function-calling schema (the only translation layer we need)."""
        schema = dict(tool.inputSchema or {"type": "object", "properties": {}})
        schema.pop("title", None)
        for prop in schema.get("properties", {}).values():
            prop.pop("title", None)
            # collapse pydantic's anyOf[X, null] into X: small models handle flat schemas far better
            if "anyOf" in prop:
                non_null = [s for s in prop["anyOf"] if s.get("type") != "null"]
                if len(non_null) == 1:
                    prop.update(non_null[0])
                    prop.pop("anyOf")
            prop.pop("default", None)
        return {"type": "function",
                "function": {"name": tool.name, "description": (tool.description or "").strip(), "parameters": schema}}

    @property
    def names(self) -> set[str]:
        return {t["function"]["name"] for t in self.tools}

    def call(self, name: str, arguments: dict[str, Any]) -> dict:
        if self._session is None:
            raise RuntimeError("MCP session is not running")
        fut = asyncio.run_coroutine_threadsafe(self._session.call_tool(name, arguments), self._loop)
        result = fut.result(timeout=self._timeout)
        if result.structuredContent:
            data = result.structuredContent
            # FastMCP wraps non-object returns as {"result": ...}
            return data.get("result", data) if isinstance(data, dict) and set(data) == {"result"} else data
        text = "".join(getattr(c, "text", "") for c in result.content)
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            return {"ok": not result.isError, "text": text}

    def close(self) -> None:
        if self._stop is not None:
            self._loop.call_soon_threadsafe(self._stop.set)
        try:
            self._runner.result(timeout=5)
        except Exception:
            pass
        self._loop.call_soon_threadsafe(self._loop.stop)
