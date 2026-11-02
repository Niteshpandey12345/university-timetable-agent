"""
Viva demo: a scripted conversation through the full stack
(LangGraph agents -> MCP stdio servers -> CP-SAT engine -> validator), printing
every agent node and MCP call. Works with or without an LLM key.

    python -m scripts.run_demo              # semester 5 (year 3), 60 s solve
    python -m scripts.run_demo --semester 3 --seconds 30
"""
import argparse
import asyncio
import time

from ttagent.agents.agent import TimetableAgent


def turns(sem: int) -> list[str]:
    year = {1: 1, 3: 2, 5: 3, 7: 4}.get(sem, 3)
    return [
        "How good is the data in the Excel workbook?",
        "Is it even possible to schedule everything? Show the capacity analysis.",
        f"Generate timetable for Year {year}.",
        "Show all conflicts in the current timetable.",
        "Why could you not schedule the DBMS lab?",
        "Compare with the published timetable",
        "JP is unavailable on Tuesday",
        "Show timetable of faculty JP",
        "export to excel",
    ]


async def main(sem: int, seconds: float) -> None:
    async with TimetableAgent(require_approval=True, solve_time_s=seconds) as agent:
        tid = f"demo-{int(time.time())}"
        print("=" * 78, f"\nUNIVERSITY TIMETABLE AGENT v2 -- model: {agent.model} -- thread {tid}\n" + "=" * 78)
        for text in turns(sem):
            print(f"\n{'-' * 78}\nUSER: {text}\n{'-' * 78}")
            t0 = time.time()
            out = await agent.ask(text, tid)
            if out["pending_approval"]:
                print(f"[human-in-the-loop] {out['pending_approval']['question']}  -> approving")
                out = await agent.resume(tid, True)
            for t in out["trace"]:
                print(f"   [{t['node']:<12}] " + (f"MCP {t['mcp']} {t.get('args', '')} ({t.get('ms')} ms)" if t.get("mcp")
                                                   else ", ".join(f"{k}={v}" for k, v in t.items() if k not in ("node", "t"))))
            print(f"\nAGENT ({time.time() - t0:.1f}s):\n{out['answer']}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--semester", type=int, default=5)
    ap.add_argument("--seconds", type=float, default=60)
    a = ap.parse_args()
    asyncio.run(main(a.semester, a.seconds))
