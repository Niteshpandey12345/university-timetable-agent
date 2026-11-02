"""
Research experiments on the real Master_TimeTable workbook.

RQ1  Is the institution's implicit model feasible at all?          (pigeonhole proof)
RQ2  How do scheduling methods compare?                            (published, VBA port, exact CP-SAT,
                                                                    decomposed, decomposed+LNS, +stability)
RQ3  Which modelling choices matter?                               (ablations: electives, room flexibility,
                                                                    soft objective)
RQ4  Is the pipeline robust to solver randomness?                  (seeds)
RQ5  Can the agent absorb disruptions?                             (random faculty-unavailability what-ifs)
RQ6  Does the supervisor route requests correctly?                 (intent accuracy on a labelled set)

    python -m scripts.run_experiments --time 60            # ~20 min on 2 cores, faster on more
    python -m scripts.run_experiments --quick              # smoke run
"""
from __future__ import annotations

import argparse
import csv
import json
import random
import statistics
import time
from pathlib import Path

from ttagent.config import ROOT
from ttagent.domain.timegrid import TimeGrid
from ttagent.engine.cpsat import SolveOptions, solve
from ttagent.engine.decomposed import solve_decomposed
from ttagent.engine.diagnosis import capacity_analysis
from ttagent.engine.greedy_vba import solve_greedy_vba
from ttagent.engine.metrics import compute_metrics, stability
from ttagent.engine.repair import lns_repair
from ttagent.engine.validator import assignments_to_bookings, validate
from ttagent.ingestion.excel_loader import load_workbook_dataset

OUT = ROOT / "experiments" / "results"


def evaluate(ds, name, res=None, bookings=None, seconds=None, extended=False):
    if bookings is None:
        bookings = assignments_to_bookings(ds, res["assignments"])
    grid = TimeGrid.from_settings(use_extended=extended)
    v = validate(ds, bookings, grid)
    m = compute_metrics(ds, bookings, grid)
    hard = sum(n for k, n in v["counts"].items() if k.startswith("H"))
    row = {"method": name, "coverage_pct": v["allocation"]["coverage_pct"],
           "scheduled_h": v["allocation"]["scheduled_hours"], "required_h": v["allocation"]["required_hours"],
           "unplaced_requirements": v["allocation"]["under_allocated"], "hard_violations": hard,
           "batch_clash": v["counts"].get("H1_batch_clash", 0), "faculty_clash": v["counts"].get("H2_faculty_clash", 0),
           "room_clash": v["counts"].get("H3_room_clash", 0),
           "outside_hours": v["counts"].get("H5_outside_hours", 0) + v["counts"].get("H5_saturday_cutoff", 0),
           "soft_penalty": m["soft_penalty"], "idle_gaps": m["batch_idle_gaps"],
           "batch_day_overload": m["batch_day_overload_hours"], "faculty_consec_excess": m["faculty_consecutive_excess"],
           "faculty_day_overload": m["faculty_day_overload_hours"], "saturday_h": m["saturday_hours"],
           "late_h": m["late_hours_slot7plus"], "extended_h": m["extended_hours_slot8plus"],
           "room_changes": m["room_changes"], "weekday_imbalance": m["weekday_imbalance"],
           "kept_vs_published_pct": stability(ds, res["assignments"])["kept_pct"] if res else 100.0,
           "seconds": round(seconds, 1) if seconds is not None else None}
    row["_hours_by_day"] = m["hours_by_day"]
    row["_hours_by_slot"] = m["hours_by_slot"]
    return row


def pipeline(ds, T, seed=7, stability_w=None, workers=8, use_baskets=True, room_mode="flex", soft=True):
    t0 = time.time()
    opts = SolveOptions(time_limit_s=T, seed=seed, stability=stability_w, workers=workers, use_baskets=use_baskets,
                        room_mode=room_mode, soft=soft)
    res = solve_decomposed(ds, opts)
    first = len(res["unplaced"])
    if res["unplaced"]:
        res = lns_repair(ds, res, opts, time_limit_s=max(15, T / 3))
    return res, time.time() - t0, first


def main(T: float, quick: bool, workers: int) -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    ds = load_workbook_dataset()
    results: dict = {"time_limit_s": T, "workers": workers, "started": time.strftime("%Y-%m-%d %H:%M")}
    log = lambda *a: print(time.strftime("%H:%M:%S"), *a, flush=True)  # noqa: E731

    # ---------------- RQ1
    strict, aware = capacity_analysis(ds, basket_aware=False), capacity_analysis(ds, basket_aware=True)
    results["rq1_capacity"] = {
        "weekly_capacity_h": strict["weekly_capacity_h"],
        "strict_infeasible": strict["provably_infeasible"],
        "strict_over_capacity": [{k: x.get(k) for k in ("batch", "faculty", "pool", "demand_h", "capacity_h")} for x in strict["infeasible_resources"]],
        "basket_aware_infeasible": aware["provably_infeasible"],
        "max_batch_utilisation_basket_aware": aware["batches_top"][0],
        "requested_venues_oversubscribed": aware["requested_venues_oversubscribed"],
        "vba_macro_unscheduled": ds.quality["vba_macro_bookkeeping"]}
    log("RQ1 strict infeasible:", strict["provably_infeasible"], "| basket-aware:", aware["provably_infeasible"])

    # ---------------- RQ2 methods
    rows = []
    rows.append(evaluate(ds, "Published grid (manual + macro)", bookings=list(ds.published)))
    t0 = time.time(); g = solve_greedy_vba(ds, include_fixed=True)
    rows.append(evaluate(ds, "VBA Allot macro (Python port)", g, seconds=time.time() - t0))
    log("RQ2 vba", rows[-1]["coverage_pct"])
    if not quick:
        t0 = time.time(); ex = solve(ds, SolveOptions(time_limit_s=T, workers=workers))
        rows.append(evaluate(ds, "CP-SAT exact (rooms inside)", ex, seconds=time.time() - t0)); log("RQ2 exact", rows[-1]["coverage_pct"])
    t0 = time.time(); dec = solve_decomposed(ds, SolveOptions(time_limit_s=T, workers=workers))
    rows.append(evaluate(ds, "CP-SAT decomposed (no repair)", dec, seconds=time.time() - t0)); log("RQ2 dec", rows[-1]["coverage_pct"])
    t0 = time.time()
    full = lns_repair(ds, dec, SolveOptions(workers=workers), time_limit_s=max(15, T / 3)) if dec["unplaced"] else dec
    rows.append(evaluate(ds, "Decomposed + LNS repair (ours)", full, seconds=rows[-1]["seconds"] + time.time() - t0,
                         extended=full.get("options", {}).get("use_extended", False)))
    results["rq2_repair_rounds"] = (full.get("repair") or {}).get("rounds")
    log("RQ2 full", rows[-1]["coverage_pct"], rows[-1]["hard_violations"])
    if not quick:
        st, secs, _ = pipeline(ds, T, stability_w=20, workers=workers)
        rows.append(evaluate(ds, "Ours + stability (minimal change)", st, seconds=secs,
                             extended=st.get("options", {}).get("use_extended", False)))
        log("RQ2 stable", rows[-1]["coverage_pct"], rows[-1]["kept_vs_published_pct"])
    results["rq2_methods"] = rows
    _csv(OUT / "rq2_methods.csv", rows)

    # ---------------- RQ3 ablations (stage-1 only, same budget, no repair -> isolates the modelling choice)
    abl = []
    for name, kw in [("full model", {}), ("no elective baskets", {"use_baskets": False}),
                     ("requested venue only (no room choice)", {"room_mode": "home"}),
                     ("no soft objective", {"soft": False})]:
        if quick and name != "full model":
            continue
        t0 = time.time()
        if kw.get("room_mode") == "home":
            r = solve(ds, SolveOptions(time_limit_s=T, workers=workers, room_mode="home"))
        else:
            r = solve_decomposed(ds, SolveOptions(time_limit_s=T, workers=workers, **kw))
        row = evaluate(ds, name, r, seconds=time.time() - t0)
        abl.append(row); log("RQ3", name, row["coverage_pct"], row["soft_penalty"])
    results["rq3_ablation"] = abl
    _csv(OUT / "rq3_ablation.csv", abl)

    # ---------------- RQ4 seeds
    seeds = [] if quick else [11, 23]
    srows = [rows[[r["method"] for r in rows].index("Decomposed + LNS repair (ours)")] | {"seed": 7}]
    for s in seeds:
        r, secs, first = pipeline(ds, T, seed=s, workers=workers)
        srows.append(evaluate(ds, f"seed {s}", r, seconds=secs, extended=r.get("options", {}).get("use_extended", False)) | {"seed": s})
        log("RQ4 seed", s, srows[-1]["coverage_pct"])
    results["rq4_seeds"] = {
        "runs": srows,
        "coverage_mean": round(statistics.mean(r["coverage_pct"] for r in srows), 2),
        "soft_penalty_mean": round(statistics.mean(r["soft_penalty"] for r in srows), 1),
        "soft_penalty_std": round(statistics.pstdev(r["soft_penalty"] for r in srows), 1),
        "hard_violations_max": max(r["hard_violations"] for r in srows)}

    # ---------------- RQ5 what-if resilience
    rng = random.Random(42)
    base = full
    busy = {}
    for a in base["assignments"]:
        for f in ds.req(a["token"]).faculty:
            if not ds.is_placeholder(f):
                busy.setdefault(f, set()).add(a["day"])
    cands = sorted(f for f in busy if len(busy[f]) >= 3)
    scen = []
    for i in range(2 if quick else 6):
        f = rng.choice(cands)
        day = rng.choice(sorted(busy[f]))
        forbid = [{"faculty": f, "day": day, "slot": None}]
        hit = [a for a in base["assignments"] if f in ds.req(a["token"]).faculty and a["day"] == day]
        keep = [a for a in base["assignments"] if a not in hit]
        broken = {**base, "assignments": keep,
                  "unplaced": [{"session_id": a["session_id"], "token": a["token"], "code": ds.req(a["token"]).code} for a in hit]}
        t0 = time.time()
        fixed = lns_repair(ds, broken, SolveOptions(forbid=forbid, workers=workers), time_limit_s=max(15, T / 3))
        before = {a["session_id"]: (a["day"], a["start"], a["room"]) for a in base["assignments"]}
        moved = sum(1 for a in fixed["assignments"] if before.get(a["session_id"]) != (a["day"], a["start"], a["room"]))
        bk = assignments_to_bookings(ds, fixed["assignments"])
        v = validate(ds, bk, TimeGrid.from_settings(fixed.get("options", {}).get("use_extended", False)))
        ok_forbid = not any(f in b.faculty and b.day == day for b in bk if b.origin != "fixed")
        scen.append({"faculty": f, "day": day, "sessions_displaced": len(hit), "resolved": fixed["repair"]["resolved"],
                     "coverage_pct": v["allocation"]["coverage_pct"],
                     "hard_violations": sum(n for k, n in v["counts"].items() if k.startswith("H")),
                     "constraint_respected": ok_forbid, "sessions_moved_total": moved,
                     "disruption_pct": round(100 * moved / max(1, len(fixed["assignments"])), 2),
                     "seconds": round(time.time() - t0, 1)})
        log("RQ5", scen[-1])
    results["rq5_whatif"] = scen
    _csv(OUT / "rq5_whatif.csv", scen)

    # ---------------- RQ6 intent routing
    results["rq6_intent"] = intent_eval(ds)
    log("RQ6 intent accuracy", results["rq6_intent"]["accuracy"])

    results["finished"] = time.strftime("%Y-%m-%d %H:%M")
    (OUT / "results.json").write_text(json.dumps(results, indent=1, default=str), encoding="utf-8")
    figures(results)
    log("done ->", OUT)


INTENT_SET = [
    ("Generate timetable for Year 3.", "generate"), ("Create the timetable for semester 1", "generate"),
    ("build a schedule for all semesters", "generate"), ("make the 4th year timetable with minimal changes", "generate"),
    ("Show all conflicts in the current timetable.", "validate"), ("check the timetable for clashes", "validate"),
    ("are there any violations?", "validate"), ("validate the latest version", "validate"),
    ("Why could you not schedule the DBMS lab?", "explain_unplaced"), ("why is token 104 unplaced", "explain_unplaced"),
    ("what could not be scheduled and why", "explain_unplaced"),
    ("Show timetable of batch 1B1", "view"), ("show faculty RAVI timetable", "view"), ("display room LT1", "view"),
    ("view semester 5 timetable", "view"),
    ("RAVI is on leave on Monday", "what_if_unavailable"), ("JP is unavailable on Tuesday", "what_if_unavailable"),
    ("LT1 is not available on Friday", "what_if_unavailable"),
    ("move R104#0 to Friday slot 2", "move"), ("reschedule R12#1 to Wed slot 3", "move"),
    ("Compare with the published timetable", "compare"), ("is it better than the macro?", "compare"),
    ("how good is the data in the excel workbook", "quality"), ("show me data issues", "quality"),
    ("Is it even possible to schedule everything?", "capacity"), ("do we have enough rooms", "capacity"),
    ("export to excel", "export"), ("download the calendar for batch 3B1", "export"),
    ("publish it", "publish"), ("repair the timetable", "repair"), ("hello", "chat"), ("what can you do", "chat"),
]


def intent_eval(ds) -> dict:
    from ttagent.agents.intent import parse_rules
    fac = {f for f, v in ds.faculty.items() if not v.is_placeholder}
    rows, ok = [], 0
    for q, gold in INTENT_SET:
        got = parse_rules(q, fac, set(ds.rooms)).action
        ok += got == gold
        rows.append({"query": q, "gold": gold, "predicted": got, "correct": got == gold})
    _csv(OUT / "rq6_intent.csv", rows)
    return {"parser": "rule-based (offline)", "n": len(rows), "accuracy": round(ok / len(rows), 3),
            "errors": [r for r in rows if not r["correct"]]}


def _csv(path: Path, rows: list[dict]) -> None:
    if not rows:
        return
    keys = [k for k in rows[0] if not k.startswith("_")]
    with open(path, "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=keys, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)


def figures(results: dict) -> None:
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    rows = results["rq2_methods"]
    names = [r["method"].replace(" (", "\n(") for r in rows]
    c1, c2, c3 = "#2a6fdb", "#e0703a", "#5b9e5f"

    fig, ax = plt.subplots(1, 2, figsize=(13, 4.6))
    ax[0].barh(names, [r["coverage_pct"] for r in rows], color=c1)
    ax[0].set_xlim(min(r["coverage_pct"] for r in rows) - 3, 100.5)
    ax[0].set_title("Weekly hours scheduled (%)")
    for i, r in enumerate(rows):
        ax[0].text(r["coverage_pct"], i, f" {r['coverage_pct']}", va="center", fontsize=8)
    ax[1].barh(names, [r["hard_violations"] for r in rows], color=c2)
    ax[1].set_title("Hard-constraint violations (lower is better)")
    for i, r in enumerate(rows):
        ax[1].text(r["hard_violations"], i, f" {r['hard_violations']}", va="center", fontsize=8)
    for a in ax:
        a.invert_yaxis()
        a.spines[["top", "right"]].set_visible(False)
    fig.tight_layout(); fig.savefig(OUT / "fig1_coverage_violations.png", dpi=160); plt.close(fig)

    keys = [("idle_gaps", "idle gaps"), ("faculty_consec_excess", "faculty >3h run"), ("saturday_h", "Saturday h"),
            ("extended_h", "16:00+ h"), ("batch_day_overload", "batch >5h/day")]
    fig, ax = plt.subplots(figsize=(12, 4.6))
    w = 0.8 / len(rows)
    for i, r in enumerate(rows):
        ax.bar([k + i * w for k in range(len(keys))], [r[k] for k, _ in keys], w, label=r["method"])
    ax.set_xticks([k + 0.4 - w / 2 for k in range(len(keys))], [l for _, l in keys])
    ax.set_title("Soft-quality metrics (lower is better)")
    ax.legend(fontsize=8, frameon=False)
    ax.spines[["top", "right"]].set_visible(False)
    fig.tight_layout(); fig.savefig(OUT / "fig2_soft_quality.png", dpi=160); plt.close(fig)

    pub = rows[0]
    ours = next(r for r in rows if r["method"].startswith("Decomposed + LNS"))
    days = list(pub["_hours_by_day"])
    fig, ax = plt.subplots(1, 2, figsize=(12, 4))
    x = range(len(days))
    ax[0].bar([i - 0.2 for i in x], [pub["_hours_by_day"][d] for d in days], 0.4, label="published", color=c2)
    ax[0].bar([i + 0.2 for i in x], [ours["_hours_by_day"][d] for d in days], 0.4, label="ours", color=c1)
    ax[0].set_xticks(list(x), days); ax[0].set_title("Teaching hours per day"); ax[0].legend(frameon=False)
    slots = sorted(set(map(str, pub["_hours_by_slot"])) | set(map(str, ours["_hours_by_slot"])), key=int)
    gp = {str(k): v for k, v in pub["_hours_by_slot"].items()}
    go = {str(k): v for k, v in ours["_hours_by_slot"].items()}
    xs = range(len(slots))
    ax[1].bar([i - 0.2 for i in xs], [gp.get(s, 0) for s in slots], 0.4, label="published", color=c2)
    ax[1].bar([i + 0.2 for i in xs], [go.get(s, 0) for s in slots], 0.4, label="ours", color=c1)
    ax[1].set_xticks(list(xs), [f"slot {s}" for s in slots]); ax[1].set_title("Teaching hours per slot")
    for a in ax:
        a.spines[["top", "right"]].set_visible(False)
    fig.tight_layout(); fig.savefig(OUT / "fig3_distribution.png", dpi=160); plt.close(fig)

    abl = results.get("rq3_ablation") or []
    if len(abl) > 1:
        fig, ax = plt.subplots(figsize=(8, 3.6))
        ax.barh([r["method"] for r in abl], [r["coverage_pct"] for r in abl], color=c3)
        for i, r in enumerate(abl):
            ax.text(r["coverage_pct"], i, f" {r['coverage_pct']}%", va="center", fontsize=8)
        ax.invert_yaxis(); ax.set_xlim(min(r["coverage_pct"] for r in abl) - 5, 101)
        ax.set_title("Ablation: hours scheduled by stage-1 solve (same budget, no repair)")
        ax.spines[["top", "right"]].set_visible(False)
        fig.tight_layout(); fig.savefig(OUT / "fig4_ablation.png", dpi=160); plt.close(fig)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--time", type=float, default=60)
    ap.add_argument("--workers", type=int, default=8)
    ap.add_argument("--quick", action="store_true")
    a = ap.parse_args()
    main(a.time, a.quick, a.workers)
