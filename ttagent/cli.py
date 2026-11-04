"""
`ttagent` command-line interface (installed by `pip install -e .`).

    ttagent serve                 start the web app and open the browser
    ttagent ingest [file.xlsm]    load a timetable workbook
    ttagent solve --method ga     solve (decomposed | exact | ga | ga_plain | vba) and export Excel
    ttagent validate [--solution published]
    ttagent chat                  talk to the LangGraph agents in the terminal
    ttagent demo                  scripted demonstration with full MCP trace
    ttagent benchmark             ITC-2007 public benchmark (CP-SAT vs CP-SAT+LNS vs GA)
    ttagent experiments           research experiments on the real data
    ttagent llm-eval              agent-layer evaluation
    ttagent mcp-tools             start the 4 MCP servers and list their tools
"""
from __future__ import annotations

import argparse
import json
import sys

from ttagent import __version__


def _print(obj) -> None:
    print(json.dumps(obj, indent=1, default=str))


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="ttagent", description="University Timetable Agent: LangGraph + MCP + CP-SAT/GA")
    ap.add_argument("--version", action="version", version=f"ttagent {__version__}")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("serve", help="start the web dashboard")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8000)
    p.add_argument("--no-browser", action="store_true")

    p = sub.add_parser("ingest", help="load a workbook")
    p.add_argument("path", nargs="?")

    p = sub.add_parser("solve", help="generate a timetable")
    p.add_argument("--method", default="decomposed", choices=["decomposed", "exact", "ga", "ga_plain", "vba"])
    p.add_argument("--semesters", type=int, nargs="*")
    p.add_argument("--time", type=float, default=120)
    p.add_argument("--seed", type=int, default=7)
    p.add_argument("--stability", type=float)
    p.add_argument("--no-repair", action="store_true")
    p.add_argument("--export", default="xlsx", choices=["xlsx", "json", "none"])

    p = sub.add_parser("validate", help="validate a timetable version")
    p.add_argument("--solution", default="latest")

    p = sub.add_parser("chat", help="terminal chat with the agents")
    p.add_argument("--thread")
    p.add_argument("--trace", action="store_true")

    p = sub.add_parser("demo", help="scripted demonstration")
    p.add_argument("--semester", type=int, default=5)
    p.add_argument("--seconds", type=float, default=60)

    p = sub.add_parser("benchmark", help="ITC-2007 CB-CTT benchmark")
    p.add_argument("--instances", nargs="*")
    p.add_argument("--time", type=float, default=120)
    p.add_argument("--only-lns", action="store_true", help="add the CP-SAT+LNS column to saved results")

    p = sub.add_parser("experiments", help="research experiments on the real workbook")
    p.add_argument("--time", type=float, default=60)
    p.add_argument("--quick", action="store_true")

    sub.add_parser("llm-eval", help="agent-layer evaluation (uses an LLM key if configured)")
    sub.add_parser("mcp-tools", help="list MCP servers and tools")

    a = ap.parse_args(argv)
    if a.cmd == "serve":
        import threading
        import webbrowser

        import uvicorn
        url = f"http://{a.host}:{a.port}"
        print(f"Timetable Agent {__version__} -> {url}   (Ctrl+C to stop)")
        if not a.no_browser:
            threading.Timer(2.0, lambda: webbrowser.open(url)).start()
        uvicorn.run("ttagent.api.main:app", host=a.host, port=a.port, log_level="warning")
    elif a.cmd == "ingest":
        from ttagent.engine.service import TimetableService
        _print(TimetableService().ingest(a.path))
    elif a.cmd == "solve":
        from ttagent.engine.service import TimetableService
        svc = TimetableService()
        s = svc.solve(semesters=a.semesters or None, method=a.method, time_limit_s=a.time, seed=a.seed,
                      stability_weight=a.stability, repair=not a.no_repair)
        _print(s)
        if a.export != "none":
            print("exported:", svc.export(s["solution_id"], a.export)["path"])
    elif a.cmd == "validate":
        from ttagent.engine.service import TimetableService
        _print(TimetableService().validate_solution(a.solution))
    elif a.cmd == "chat":
        import asyncio

        from scripts.chat import main as chat_main
        asyncio.run(chat_main(a.thread, a.trace, 60))
    elif a.cmd == "demo":
        import asyncio

        from scripts.run_demo import main as demo_main
        asyncio.run(demo_main(a.semester, a.seconds))
    elif a.cmd == "benchmark":
        from scripts.run_benchmark_itc import add_lns, main as bench
        (add_lns if a.only_lns else bench)(a.instances or [f"comp{i:02d}" for i in range(1, 22)], a.time, 8, 7)
    elif a.cmd == "experiments":
        from scripts.run_experiments import main as exp
        exp(a.time, a.quick, 8)
    elif a.cmd == "llm-eval":
        from scripts.run_llm_eval import main as ev
        ev(["routing", "faithfulness", "llm_only"])
    elif a.cmd == "mcp-tools":
        import asyncio

        from ttagent.agents.mcp_bridge import _smoke
        asyncio.run(_smoke())
    return 0


if __name__ == "__main__":
    sys.exit(main())
