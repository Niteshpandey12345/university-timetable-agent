"""
v4 evidence-widening experiments on the real workbook (results -> experiments/results/v4/).

  seeds     CP-SAT pipeline and hand-set memetic GA for seeds 6-10 (merged with v3 seeds 1-5 -> 10 seeds)
  tune      random-search tuning of the GA on semesters 5 and 3 (the GA's weakest), then the tuned GA on 10 seeds
  family    a family of 7 instances derived from the real data (each semester alone, +10 % teaching load,
            two lecture theatres closed, three busy teachers absent for a day), 2 seeds per method
  onecore   the CP-SAT pipeline restricted to one worker, after the v4 fallback fix

    python -m scripts.run_analysis_v4 --sections seeds
    python -m scripts.run_analysis_v4 --sections tune family onecore
"""
from __future__ import annotations

import argparse
import json
import random
import statistics
import time
from collections import defaultdict

from scripts.run_analysis_v3 import cpsat_pipeline, mann_whitney, row_of
from ttagent.config import ROOT
from ttagent.engine.cpsat import SolveOptions
from ttagent.engine.decomposed import solve_decomposed
from ttagent.engine.ga import GAOptions, solve_ga
from ttagent.engine.repair import lns_repair
from ttagent.ingestion.excel_loader import load_workbook_dataset

OUT = ROOT / "experiments" / "results" / "v4"
V3 = ROOT / "experiments" / "results" / "v3" / "analysis_v3.json"
log = lambda *a: print(time.strftime("%H:%M:%S"), *a, flush=True)  # noqa: E731


def _load() -> dict:
    p = OUT / "analysis_v4.json"
    return json.loads(p.read_text()) if p.exists() else {}


def _save(res: dict) -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "analysis_v4.json").write_text(json.dumps(res, indent=1, default=str), encoding="utf-8")


def seeds(ds, T: float, res: dict) -> None:
    runs = res.setdefault("seeds_runs", [])
    done = {(r["method"], r["seed"]) for r in runs}
    for s in range(6, 11):
        if ("CP-SAT pipeline", s) not in done:
            r, sec = cpsat_pipeline(ds, T, s)
            runs.append(row_of(ds, "CP-SAT pipeline", r, sec, s)); log("seeds cpsat", s, runs[-1]["coverage_pct"]); _save(res)
        if ("GA memetic", s) not in done:
            t1 = time.time()
            g = solve_ga(ds, GAOptions(time_limit_s=T, seed=s))
            runs.append(row_of(ds, "GA memetic", g, time.time() - t1, s)); log("seeds ga", s, runs[-1]["coverage_pct"]); _save(res)
    summarise_seeds(res)


def summarise_seeds(res: dict) -> None:
    v3 = json.loads(V3.read_text())["A_runs"]
    allr = [r for r in v3 if r["method"] in ("CP-SAT pipeline", "GA memetic")] + res.get("seeds_runs", []) + \
           [r for r in res.get("tuned_runs", [])]
    by = defaultdict(list)
    for r in allr:
        by[r["method"]].append(r)
    summ = {}
    for m, rs in by.items():
        summ[m] = {"n": len(rs)} | {k: {"mean": round(statistics.mean(r[k] for r in rs), 2),
                                        "sd": round(statistics.stdev([r[k] for r in rs]), 2) if len(rs) > 1 else 0.0}
                                    for k in ("coverage_pct", "soft_penalty", "idle_gaps", "room_changes", "faculty_consec", "seconds")}
        summ[m]["soft_per_h"] = round(statistics.mean(r["soft_penalty"] / max(1, r["scheduled_h"]) for r in rs), 2)
    res["seeds_summary"] = summ
    cp, ga = by["CP-SAT pipeline"], by["GA memetic"]
    tests = {"coverage CP-SAT vs GA (10 seeds)": mann_whitney([r["coverage_pct"] for r in cp], [r["coverage_pct"] for r in ga]),
             "soft CP-SAT vs GA (10 seeds)": mann_whitney([r["soft_penalty"] for r in cp], [r["soft_penalty"] for r in ga])}
    if by.get("GA memetic (tuned)"):
        gt = by["GA memetic (tuned)"]
        tests["coverage CP-SAT vs tuned GA"] = mann_whitney([r["coverage_pct"] for r in cp], [r["coverage_pct"] for r in gt])
        tests["soft CP-SAT vs tuned GA"] = mann_whitney([r["soft_penalty"] for r in cp], [r["soft_penalty"] for r in gt])
        tests["coverage tuned vs hand-set GA"] = mann_whitney([r["coverage_pct"] for r in gt], [r["coverage_pct"] for r in ga])
    res["seeds_tests"] = tests


SPACE = {"population": [8, 16, 24, 40], "tournament": [2, 3, 5], "crossover_rate": [0.5, 0.7, 0.85, 0.95],
         "mutation_rate": [0.01, 0.03, 0.06, 0.1], "ls_iters": [60, 120, 250, 500], "ls_sample": [30, 60, 120]}


def tune(ds, T: float, res: dict, n_configs: int = 10, budget_s: float = 30) -> None:
    """Two-stage racing (a light stand-in for irace).
    Stage 1: random configurations on the GA's two hardest sub-instances (semesters 5 and 3), 30 s, 2 seeds each.
    Stage 2: the 3 best of stage 1 plus the hand-set default on the *full* university with the real budget (2 seeds);
    the winner is then run on 10 seeds. (Stage 1 alone picked a configuration that did not transfer: see notes.)"""
    rng = random.Random(2026)
    base = {"population": 24, "tournament": 3, "crossover_rate": 0.85, "mutation_rate": 0.03, "ls_iters": 120, "ls_sample": 60}
    configs = [base] + [{k: rng.choice(v) for k, v in SPACE.items()} for _ in range(n_configs - 1)]
    trials = res.setdefault("tune_trials", [])
    for i, cfg in enumerate(configs):
        if any(t["config"] == cfg for t in trials):
            continue
        scores = []
        for sem in (5, 3):
            for s in (101, 102):
                g = solve_ga(ds, GAOptions(time_limit_s=budget_s, seed=s, semesters=[sem], **cfg))
                r = row_of(ds, "ga", g, budget_s, s)
                scores.append(r["coverage_pct"] * 1000 - r["soft_penalty"])   # coverage first, then soft
        trials.append({"config": cfg, "score": statistics.mean(scores), "is_default": cfg == base})
        log("tune", i, cfg, round(trials[-1]["score"], 1)); _save(res)
    # stage 2 on the target instance
    short = sorted([t for t in trials if not t["is_default"]], key=lambda t: -t["score"])[:3] + [t for t in trials if t["is_default"]]
    race = res.setdefault("tune_race", [])
    for t in short:
        if any(r["config"] == t["config"] for r in race):
            continue
        rows = []
        for s in (201, 202):
            g = solve_ga(ds, GAOptions(time_limit_s=T, seed=s, **t["config"]))
            rows.append(row_of(ds, "ga", g, T, s))
        race.append({"config": t["config"], "is_default": t["is_default"], "stage1_score": t["score"],
                     "coverage": [r["coverage_pct"] for r in rows], "soft": [r["soft_penalty"] for r in rows],
                     "score": statistics.mean(r["coverage_pct"] * 1000 - r["soft_penalty"] for r in rows)})
        log("race", t["config"], race[-1]["coverage"], round(race[-1]["score"], 1)); _save(res)
    best = max(race, key=lambda r: r["score"])
    res["tuned_config"] = best["config"]
    res["tuned_is_default"] = best["is_default"]
    res["tune_budget"] = {"stage1_configs": len(trials), "stage1_runs_each": 4, "stage1_run_s": budget_s,
                          "stage1_instances": "semesters 5 and 3", "stage2_configs": len(race), "stage2_runs_each": 2,
                          "stage2_run_s": T, "stage2_instance": "whole university",
                          "total_cpu_s": len(trials) * 4 * budget_s + len(race) * 2 * T}
    runs = res.setdefault("tuned_runs", [])
    if res.get("tuned_runs_config") != best["config"]:
        res["stage1_winner_full_instance"] = runs[:1]          # the stage-1 winner on the full instance (seed 1)
        runs.clear()
        res["tuned_runs_config"] = best["config"]
    if best["is_default"]:          # the race kept the hand-set configuration: its 10 seeds already exist
        v3 = json.loads(V3.read_text())["A_runs"]
        runs[:] = [r | {"method": "GA memetic (tuned)"} for r in v3 + res.get("seeds_runs", []) if r["method"] == "GA memetic"]
    for s in range(1, 11):
        if any(r["seed"] == s for r in runs):
            continue
        t1 = time.time()
        g = solve_ga(ds, GAOptions(time_limit_s=T, seed=s, **best["config"]))
        runs.append(row_of(ds, "GA memetic (tuned)", g, time.time() - t1, s)); log("tuned ga", s, runs[-1]["coverage_pct"]); _save(res)
    summarise_seeds(res)


def derived_instances(ds):
    """Stress variants of the real instance. Returns [(name, dataset, semesters, forbid)]."""
    out = [(f"semester {s}", ds, [s], []) for s in ds.semesters()]
    # +10 % load: duplicate one session's worth of hours for ~10 % of lecture requirements (extra weekly lecture)
    d2 = ds.model_copy(deep=True)
    rng = random.Random(5)
    lec = [r for r in d2.requirements if r.kind.value == "L" and r.per_week < 4]
    base_h = sum(r.duration * r.per_week for r in ds.requirements)
    added = 0
    for r in rng.sample(lec, len(lec)):
        if added >= 0.10 * base_h:
            break
        r.per_week += 1
        added += r.duration
    out.append((f"+{round(100 * added / base_h)} % teaching load", d2, None, []))
    # two lecture theatres closed all week
    lts = sorted(r for r, x in ds.rooms.items() if x.category == "lecture_theatre")[:2]
    out.append((f"rooms {'+'.join(lts)} closed", ds, None,
                [{"room": rm, "day": d, "slot": None} for rm in lts for d in ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat")]))
    # three busiest real teachers absent on one day each
    load = defaultdict(int)
    for r in ds.requirements:
        for f in r.faculty:
            if not ds.is_placeholder(f):
                load[f] += r.duration * r.per_week
    top = sorted(load, key=load.get, reverse=True)[:3]
    out.append((f"teachers {', '.join(top)} absent", ds, None,
                [{"faculty": f, "day": d, "slot": None} for f, d in zip(top, ("Mon", "Wed", "Fri"))]))
    return out


def family(ds, T: float, res: dict) -> None:
    rows = res.setdefault("family_runs", [])
    cfg = res.get("tuned_config") or {}
    for name, d, sems, forbid in derived_instances(ds):
        for s in (1, 2):
            if not any(r["instance"] == name and r["seed"] == s and r["method"] == "CP-SAT pipeline" for r in rows):
                t0 = time.time()
                opts = SolveOptions(time_limit_s=T, seed=s, semesters=sems, forbid=forbid)
                r = solve_decomposed(d, opts)
                if r["unplaced"]:
                    r = lns_repair(d, r, opts, time_limit_s=max(15, T / 3))
                rows.append(row_of(d, "CP-SAT pipeline", r, time.time() - t0, s, {"instance": name}))
                log("family", name, "cpsat", s, rows[-1]["coverage_pct"]); _save(res)
            if not any(r["instance"] == name and r["seed"] == s and r["method"] == "GA memetic (tuned)" for r in rows):
                t0 = time.time()
                g = solve_ga(d, GAOptions(time_limit_s=T, seed=s, semesters=sems, forbid=forbid, **cfg))
                rows.append(row_of(d, "GA memetic (tuned)", g, time.time() - t0, s, {"instance": name}))
                log("family", name, "ga", s, rows[-1]["coverage_pct"]); _save(res)
    agg = defaultdict(list)
    for r in rows:
        agg[(r["instance"], r["method"])].append(r)
    res["family_summary"] = [{"instance": i, "method": m, "coverage_mean": round(statistics.mean(x["coverage_pct"] for x in v), 2),
                              "coverage_min": min(x["coverage_pct"] for x in v), "hard_max": max(x["hard"] for x in v),
                              "soft_mean": round(statistics.mean(x["soft_penalty"] for x in v), 1), "n": len(v)}
                             for (i, m), v in agg.items()]
    wins = 0
    names = sorted({r["instance"] for r in rows})
    for n in names:
        c = [x for x in agg[(n, "CP-SAT pipeline")]]
        g = [x for x in agg[(n, "GA memetic (tuned)")]]
        if (statistics.mean(x["coverage_pct"] for x in c), -statistics.mean(x["soft_penalty"] for x in c)) > \
           (statistics.mean(x["coverage_pct"] for x in g), -statistics.mean(x["soft_penalty"] for x in g)):
            wins += 1
    res["family_cpsat_wins"] = f"{wins}/{len(names)}"


def onecore(ds, T: float, res: dict) -> None:
    from ttagent.engine.service import robust_pipeline
    t0 = time.time()
    r = robust_pipeline(ds, SolveOptions(time_limit_s=T, seed=1, workers=1))
    res["onecore"] = row_of(ds, "CP-SAT pipeline (1 worker, v4 fallback)", r, time.time() - t0, 1,
                            {"fallback_used": (r.get("stats") or {}).get("fallback")})
    log("onecore", res["onecore"]["coverage_pct"], res["onecore"]["fallback_used"])


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--sections", nargs="+", default=["seeds", "tune", "family", "onecore"])
    ap.add_argument("--time", type=float, default=120)
    a = ap.parse_args()
    ds = load_workbook_dataset()
    res = _load()
    for sec in a.sections:
        T = 60 if sec == "family" else a.time
        {"seeds": seeds, "tune": tune, "family": family, "onecore": onecore}[sec](ds, T, res)
        _save(res)
    log("done")
