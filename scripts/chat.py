"""
Interactive terminal chat with the LangGraph agent (MCP servers start automatically).

    python -m scripts.chat                 # new thread
    python -m scripts.chat --thread viva   # resume a persisted conversation
    python -m scripts.chat --trace         # print the agent path / MCP calls per turn
"""
import argparse
import asyncio

from ttagent.agents.agent import TimetableAgent


async def main(thread: str | None, trace: bool, solve_s: float) -> None:
    async with TimetableAgent(require_approval=True, solve_time_s=solve_s) as agent:
        tid = thread or agent.new_thread()
        print(f"Timetable agent ready -- model: {agent.model} -- thread: {tid}  (Ctrl+C or 'exit' to quit)\n")
        while True:
            try:
                text = input("you> ").strip()
            except (EOFError, KeyboardInterrupt):
                break
            if not text or text.lower() in ("exit", "quit"):
                break
            out = await agent.ask(text, tid)
            while out["pending_approval"]:
                ans = input(f"\n[approval] {out['pending_approval']['question']} [y/N] ").strip().lower()
                out = await agent.resume(tid, ans in ("y", "yes"))
            print("\nagent>", out["answer"], "\n")
            if trace:
                print("  path:", " -> ".join(t.get("mcp") or t["node"] for t in out["trace"]), "\n")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--thread")
    ap.add_argument("--trace", action="store_true")
    ap.add_argument("--solve-seconds", type=float, default=60)
    a = ap.parse_args()
    asyncio.run(main(a.thread, a.trace, a.solve_seconds))
