"""
Solve from the command line (no LLM, no MCP) and export to Excel.

    python -m scripts.solve                       # whole university, 120 s
    python -m scripts.solve --semesters 3 --time 30
    python -m scripts.solve --method vba          # the VBA macro baseline
    python -m scripts.solve --stability 20        # minimal changes vs the published grid
"""
import argparse
import json

from ttagent.engine.service import TimetableService

if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--semesters", type=int, nargs="*")
    ap.add_argument("--method", default="decomposed", choices=["decomposed", "exact", "ga", "ga_plain", "vba"])
    ap.add_argument("--time", type=float, default=120)
    ap.add_argument("--stability", type=float, default=None)
    ap.add_argument("--workers", type=int, default=8)
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--no-repair", action="store_true")
    ap.add_argument("--no-export", action="store_true")
    a = ap.parse_args()
    svc = TimetableService()
    s = svc.solve(semesters=a.semesters or None, method=a.method, time_limit_s=a.time, repair=not a.no_repair,
                  stability_weight=a.stability, workers=a.workers, seed=a.seed)
    print(json.dumps(s, indent=1, default=str))
    if not a.no_export:
        print("Excel:", svc.export(s["solution_id"], "xlsx")["path"])
