"""
ITC-2007 Track 3 (CB-CTT, UD2) benchmark: CP-SAT vs memetic GA vs published results.

    python -m scripts.run_benchmark_itc --time 120            # all 21 instances
    python -m scripts.run_benchmark_itc --instances comp01 comp07 --time 60
    python -m scripts.run_benchmark_itc --only-lns --time 120  # add the CP-SAT+LNS column to the saved results
"""
from __future__ import annotations

import argparse
import csv
import json
import time

from ttagent.benchmarks.ctt import BEST_KNOWN, TIME_LIMITED, load
from ttagent.benchmarks.ctt_cpsat import solve_ctt_cpsat
from ttagent.benchmarks.ctt_ga import solve_ctt_ga
from ttagent.benchmarks.ctt_lns import solve_ctt_lns
from ttagent.config import ROOT

OUT = ROOT / "experiments" / "results"


def add_lns(instances: list[str], T: float, workers: int, seed: int) -> None:
    """Run CP-SAT+LNS and merge its columns into the saved itc2007_results.json/.csv."""
    path = OUT / "itc2007_results.json"
    data = json.loads(path.read_text())
    by = {r["instance"]: r for r in data["rows"]}
    for name in instances:
        r = solve_ctt_lns(load(name), time_limit_s=T, workers=workers, seed=seed)
        row = by[name]
        ub = row["best_known_ub"]
        row |= {"lns_cost": r["cost"] if r["feasible"] else None, "lns_feasible": r["feasible"], "lns_status": r["status"],
                "lns_iterations": r.get("iterations"),
                "lns_gap_pct": round(100 * (r["cost"] - ub) / max(ub, 1), 1) if r["feasible"] else None}
        print(time.strftime("%H:%M:%S"), json.dumps({"instance": name, "ub": ub, "cpsat": row["cpsat_cost"], "lns": row["lns_cost"],
                                                      "iters": row["lns_iterations"]}), flush=True)
        data["lns_time_limit_s"] = T
        path.write_text(json.dumps(data, indent=1))
    _write_csv(data["rows"])


def _write_csv(rows):
    keys = list(dict.fromkeys(k for r in rows for k in r))
    with open(OUT / "itc2007_results.csv", "w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=keys)
        w.writeheader()
        w.writerows(rows)


def main(instances: list[str], T: float, workers: int, seed: int) -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    rows = []
    for name in instances:
        inst = load(name)
        lb, ub = BEST_KNOWN[name]
        row = inst.summary() | {"best_known_lb": lb, "best_known_ub": ub}
        g = solve_ctt_ga(inst, time_limit_s=T, seed=seed)
        c = solve_ctt_cpsat(inst, time_limit_s=T, workers=workers, seed=seed)
        row |= {"ga_cost": g["cost"] if g["feasible"] else None, "ga_feasible": g["feasible"], "ga_hard": g["hard_total"],
                "ga_generations": g["generations"],
                "cpsat_cost": c["cost"] if c["feasible"] else None, "cpsat_feasible": c["feasible"],
                "cpsat_status": c["status"], "cpsat_bound": c["bound"]}
        for k in ("ga", "cpsat"):
            v = row[f"{k}_cost"]
            row[f"{k}_gap_pct"] = round(100 * (v - ub) / max(ub, 1), 1) if v is not None else None
        for ref, tab in TIME_LIMITED.items():
            if name in tab:
                row[f"{ref} avg"], row[f"{ref} best"] = tab[name]
        rows.append(row)
        print(time.strftime("%H:%M:%S"), json.dumps({k: row[k] for k in ("instance", "best_known_ub", "ga_cost", "cpsat_cost", "cpsat_bound", "cpsat_status")}), flush=True)
        (OUT / "itc2007_results.json").write_text(json.dumps({"time_limit_s": T, "workers": workers, "seed": seed, "rows": rows}, indent=1))
    _write_csv(rows)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--instances", nargs="*", default=[f"comp{i:02d}" for i in range(1, 22)])
    ap.add_argument("--time", type=float, default=120)
    ap.add_argument("--workers", type=int, default=8)
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--only-lns", action="store_true")
    a = ap.parse_args()
    (add_lns if a.only_lns else main)(a.instances, a.time, a.workers, a.seed)
