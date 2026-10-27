"""Terminal chat with the agent (handy for quick testing):  python cli.py [--trace]"""

from __future__ import annotations

import argparse
import json

from goodfoods.agent.core import ReservationAgent
from goodfoods.agent.llm import OpenAICompatibleClient
from goodfoods.agent.mcp_bridge import MCPToolbox
from goodfoods.agent.prompts import GREETING


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--trace", action="store_true", help="print tool calls")
    args = parser.parse_args()
    toolbox = MCPToolbox()
    agent = ReservationAgent(OpenAICompatibleClient(), toolbox)
    history: list[dict] = []
    print(f"Goodie: {GREETING}\n(type 'quit' to exit)")
    try:
        while True:
            user = input("\nYou: ").strip()
            if user.lower() in {"quit", "exit"}:
                break
            result = agent.run_turn(history, user)
            if args.trace:
                for e in result.events:
                    print(f"  [tool] {e.name}({json.dumps(e.arguments, ensure_ascii=False)}) -> "
                          f"{'ok' if e.ok else e.result.get('error_code')}")
            print(f"Goodie: {result.reply}")
    except (KeyboardInterrupt, EOFError):
        pass
    finally:
        toolbox.close()


if __name__ == "__main__":
    main()
