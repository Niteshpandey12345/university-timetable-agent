"""
Multi-term validation: the same pipeline, unchanged, on other timetable workbooks of the institution.

For every workbook: ingestion and data diagnosis (capacity proofs, macro bookkeeping, validation of the published
grid), then the published grid, the VBA-macro port, the CP-SAT pipeline and the memetic GA (seeds 1..n, T s each),
all scored by the same independent validator and metrics. Results -> experiments/results/multiterm/<name>.json.

    python -m scripts.run_multiterm --workbooks jul2025=path.xlsm jan2026=path.xlsm --seeds 5 --time 120
"""
from __future__ import annotations

import argparse
import json
import statistics
import time
from pathlib import Path

from scripts.run_analysis_v3 import cpsat_pipeline, mann_whitney, row_of
from ttagent.config import ROOT
from ttagent.engine.ga import GAOptions, solve_ga
from ttagent.engine.greedy_vba import solve_greedy_vba
from ttagent.engine.metrics import compute_metrics
from ttagent.engine.validator import validate
from ttagent.ingestion.excel_loader import load_workbook_dataset

OUT = ROOT / "experiments" / "results" / "multiterm"
log = lambda *a: print(time.strftime("%H:%M:%S"), *a, flush=True)  # noqa: E731


def diagnose(ds) -> dict:
    q = ds.quality
    s = q["summary"]
    batch_hours = {}
    for r in ds.requirements:
        for b in r.batches:
            batch_hours[b] = batch_hours.get(b, 0) + r.duration * r.per_week
    return {"requirements": s["requirements"], "sessions_per_week": s["sessions_per_week"], "hours_per_week": s["teaching_hours_per_week"],
            "semesters": s["semesters"], "batches_internal": s["batches_internal"], "batches_external": s["batches_external"],
            "faculty": s["faculty"], "rooms": s["rooms"], "fixed_external_bookings": s["fixed_external_bookings"],
            "elective_baskets": [{k: b[k] for k in ("basket_id", "courses", "evidence", "confidence")} for b in s["elective_baskets"]],
            "strict_model_infeasible_batches": q["capacity_strict_model"]["infeasible_count"],
            "strict_model_provably_infeasible": q["capacity_strict_model"]["provably_infeasible"],
            "basket_model_provably_infeasible": q["capacity_basket_aware"]["provably_infeasible"],
            "macro_rows_not_scheduled": q["vba_macro_bookkeeping"]["requirements_not_fully_scheduled"],
            "macro_hours_missing": q["vba_macro_bookkeeping"]["hours_missing"],
            "published_coverage_pct": q["published_grid_validation"]["allocation"]["coverage_pct"],
            "published_hard": q["published_grid_validation"]["hard_violation_total"],
            "published_counts": q["published_grid_validation"]["counts"],
            "max_batch_hours": max(batch_hours.values()), "issues_by_severity": q["issue_counts"]["by_severity"]}


def published_row(ds) -> dict:
    bk = list(ds.published)
    v, m = validate(ds, bk), compute_metrics(ds, bk)
    r = row_of(ds, "Published grid", {"assignments": [], "scope_tokens": None}, 0)
    r.update({"coverage_pct": v["allocation"]["coverage_pct"], "scheduled_h": v["allocation"]["scheduled_hours"],
              "hard": sum(n for k, n in v["counts"].items() if k.startswith("H")), "soft_penalty": m["soft_penalty"],
              "idle_gaps": m["batch_idle_gaps"], "faculty_consec": m["faculty_consecutive_excess"], "room_changes": 0})
    return r


def summarise(runs: list[dict]) -> dict:
    out = {}
    for m in sorted({r["method"] for r in runs}):
        rs = [r for r in runs if r["method"] == m]
        f = lambda k: [r[k] for r in rs]  # noqa: E731
        sd = lambda xs: round(statistics.stdev(xs), 2) if len(xs) > 1 else 0.0  # noqa: E731
        out[m] = {"n": len(rs), "coverage_mean": round(statistics.mean(f("coverage_pct")), 2), "coverage_sd": sd(f("coverage_pct")),
                  "coverage_min": min(f("coverage_pct")), "hard_max": max(f("hard")),
                  "soft_mean": round(statistics.mean(f("soft_penalty")), 1), "soft_sd": sd(f("soft_penalty")),
                  "soft_per_h": round(statistics.mean(r["soft_penalty"] / max(1, r["scheduled_h"]) for r in rs), 2),
                  "room_changes_mean": round(statistics.mean(f("room_changes")), 1),
                  "teacher_runs_mean": round(statistics.mean(f("faculty_consec")), 1),
                  "seconds_mean": round(statistics.mean(f("seconds")), 1)}
    cp = [r for r in runs if r["method"] == "CP-SAT pipeline"]
    ga = [r for r in runs if r["method"] == "GA memetic"]
    tests = {}
    if len(cp) > 1 and len(ga) > 1:
        tests["coverage"] = mann_whitney([r["coverage_pct"] for r in cp], [r["coverage_pct"] for r in ga])
        tests["soft_per_h"] = mann_whitney([r["soft_penalty"] / r["scheduled_h"] for r in cp], [r["soft_penalty"] / r["scheduled_h"] for r in ga])
    return {"by_method": out, "tests": tests}


def run(name: str, path: str, seeds: int, T: float) -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    fp = OUT / f"{name}.json"
    res = json.loads(fp.read_text()) if fp.exists() else {"workbook": Path(path).name, "time_limit_s": T, "runs": []}
    ds = load_workbook_dataset(path)
    res["diagnosis"] = diagnose(ds)
    log(name, json.dumps({k: res["diagnosis"][k] for k in ("requirements", "hours_per_week", "published_coverage_pct", "published_hard",
                                                         "strict_model_infeasible_batches", "macro_hours_missing")}))
    done = {(r["method"], r["seed"]) for r in res["runs"]}
    save = lambda: fp.write_text(json.dumps(res | {"summary": summarise(res["runs"])}, indent=1, default=str), encoding="utf-8")  # noqa: E731
    if ("Published grid", 0) not in done and ("Published grid", None) not in done:
        res["runs"].append(published_row(ds))
    if not any(r["method"] == "VBA macro (port)" for r in res["runs"]):
        t0 = time.time()
        res["runs"].append(row_of(ds, "VBA macro (port)", solve_greedy_vba(ds, include_fixed=True), time.time() - t0))
        log(name, "vba", res["runs"][-1]["coverage_pct"], res["runs"][-1]["hard"])
    save()
    for s in range(1, seeds + 1):
        if ("CP-SAT pipeline", s) not in done:
            r, sec = cpsat_pipeline(ds, T, s)
            res["runs"].append(row_of(ds, "CP-SAT pipeline", r, sec, s)); log(name, "cpsat", s, res["runs"][-1]["coverage_pct"], res["runs"][-1]["hard"]); save()
        if ("GA memetic", s) not in done:
            t0 = time.time()
            g = solve_ga(ds, GAOptions(time_limit_s=T, seed=s))
            res["runs"].append(row_of(ds, "GA memetic", g, time.time() - t0, s)); log(name, "ga", s, res["runs"][-1]["coverage_pct"], res["runs"][-1]["hard"]); save()
    save()


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--workbooks", nargs="+", required=True, help="name=path.xlsm")
    ap.add_argument("--seeds", type=int, default=5)
    ap.add_argument("--time", type=float, default=120)
    a = ap.parse_args()
    for spec in a.workbooks:
        n, p = spec.split("=", 1)
        run(n, p, a.seeds, a.time)
    log("done")
