"""
v3 quantitative + qualitative analysis on the real workbook.

A  method comparison over seeds: CP-SAT pipeline, memetic GA, plain GA (+ VBA port, published grid)
   with Mann-Whitney U tests and Cliff's delta
B  fairness: CP-SAT restricted to 1 worker (the GA is single-threaded)
C  anytime behaviour: quality vs time budget for CP-SAT and GA
D  disruption vs quality trade-off of what-if repair (stay-put weight sweep; answers Lindahl et al. 2019)
E  per-semester breakdown and per-batch daily-load heatmaps (qualitative case study)

    python -m scripts.run_analysis_v3 --time 120 --seeds 5
    python -m scripts.run_analysis_v3 --redo-bd          # only B and D
"""
from __future__ import annotations

import argparse
import csv
import json
import random
import statistics
import time
from collections import defaultdict

from ttagent.config import ROOT
from ttagent.domain.timegrid import TimeGrid
from ttagent.engine.cpsat import SolveOptions
from ttagent.engine.decomposed import solve_decomposed
from ttagent.engine.ga import GAOptions, solve_ga
from ttagent.engine.greedy_vba import solve_greedy_vba
from ttagent.engine.metrics import compute_metrics
from ttagent.engine.repair import lns_repair
from ttagent.engine.validator import assignments_to_bookings, validate
from ttagent.ingestion.excel_loader import load_workbook_dataset

OUT = ROOT / "experiments" / "results" / "v3"
log = lambda *a: print(time.strftime("%H:%M:%S"), *a, flush=True)  # noqa: E731


def row_of(ds, name, res, seconds, seed=None, extra=None):
    ext = bool(res.get("options", {}).get("use_extended"))
    bk = assignments_to_bookings(ds, res["assignments"])
    v = validate(ds, bk, TimeGrid.from_settings(ext), scope_tokens=set(res.get("scope_tokens") or [r.token for r in ds.requirements]))
    m = compute_metrics(ds, bk, TimeGrid.from_settings(ext))
    return {"method": name, "seed": seed, "coverage_pct": v["allocation"]["coverage_pct"],
            "scheduled_h": v["allocation"]["scheduled_hours"], "required_h": v["allocation"]["required_hours"],
            "hard": sum(n for k, n in v["counts"].items() if k.startswith("H")), "soft_penalty": m["soft_penalty"],
            "idle_gaps": m["batch_idle_gaps"], "saturday_h": m["saturday_hours"], "late_h": m["late_hours_slot7plus"],
            "extended_h": m["extended_hours_slot8plus"], "room_changes": m["room_changes"],
            "faculty_consec": m["faculty_consecutive_excess"], "weekday_imbalance": m["weekday_imbalance"],
            "seconds": round(seconds, 1), **(extra or {})}


def cpsat_pipeline(ds, T, seed, workers=8):
    t0 = time.time()
    opts = SolveOptions(time_limit_s=T, seed=seed, workers=workers)
    res = solve_decomposed(ds, opts)
    if res["unplaced"]:
        res = lns_repair(ds, res, opts, time_limit_s=max(15, T / 3))
    return res, time.time() - t0


def mann_whitney(a, b):
    from scipy.stats import mannwhitneyu
    if len(set(a + b)) == 1:
        return {"U": None, "p": 1.0, "cliffs_delta": 0.0}
    u, p = mannwhitneyu(a, b, alternative="two-sided")
    gt = sum(1 for x in a for y in b if x > y)
    lt = sum(1 for x in a for y in b if x < y)
    return {"U": float(u), "p": round(float(p), 4), "cliffs_delta": round((gt - lt) / (len(a) * len(b)), 3)}


def summarise(rows, key):
    vals = [r[key] for r in rows]
    return {"mean": round(statistics.mean(vals), 2), "sd": round(statistics.pstdev(vals), 2), "min": min(vals), "max": max(vals)}


def main(T: float, n_seeds: int, quick: bool) -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    ds = load_workbook_dataset()
    res: dict = {"time_limit_s": T, "seeds": n_seeds, "started": time.strftime("%Y-%m-%d %H:%M")}
    seeds = list(range(1, n_seeds + 1))
    runs, best = [], {}
    best_cpsat, best_key = (None, None), [None]

    # ---------------- A: methods over seeds
    pub = row_of(ds, "Published grid", {"assignments": [], "scope_tokens": None}, 0)
    bk = list(ds.published)
    v = validate(ds, bk)
    m = compute_metrics(ds, bk)
    pub.update({"coverage_pct": v["allocation"]["coverage_pct"], "scheduled_h": v["allocation"]["scheduled_hours"],
                "hard": sum(n for k, n in v["counts"].items() if k.startswith("H")), "soft_penalty": m["soft_penalty"],
                "idle_gaps": m["batch_idle_gaps"], "saturday_h": m["saturday_hours"], "late_h": m["late_hours_slot7plus"],
                "extended_h": m["extended_hours_slot8plus"], "faculty_consec": m["faculty_consecutive_excess"],
                "weekday_imbalance": m["weekday_imbalance"]})
    runs.append(pub)
    t0 = time.time()
    g = solve_greedy_vba(ds, include_fixed=True)
    runs.append(row_of(ds, "VBA macro (port)", g, time.time() - t0))
    best["VBA macro (port)"] = g
    for s in seeds:
        r, sec = cpsat_pipeline(ds, T, s)
        runs.append(row_of(ds, "CP-SAT pipeline", r, sec, s)); log("A cpsat", s, runs[-1]["coverage_pct"], runs[-1]["soft_penalty"])
        best.setdefault("CP-SAT pipeline", r)
        key = (runs[-1]["coverage_pct"], -runs[-1]["soft_penalty"])
        if best_cpsat[0] is None or key > best_key[0]:
            best_cpsat, best_key[0] = (s, r), key
        t1 = time.time()
        gr = solve_ga(ds, GAOptions(time_limit_s=T, seed=s))
        runs.append(row_of(ds, "GA memetic", gr, time.time() - t1, s, {"generations": gr["stats"]["generations"]}))
        log("A ga", s, runs[-1]["coverage_pct"], runs[-1]["soft_penalty"])
        best.setdefault("GA memetic", gr)
        res.setdefault("ga_history", {})[s] = gr["stats"]["history"]
    for s in seeds[: max(1, min(3, n_seeds))]:
        t1 = time.time()
        gp = solve_ga(ds, GAOptions(time_limit_s=T, seed=s, local_search=False))
        runs.append(row_of(ds, "GA plain", gp, time.time() - t1, s, {"generations": gp["stats"]["generations"]}))
        log("A ga_plain", s, runs[-1]["coverage_pct"])
    res["A_runs"] = runs
    by = defaultdict(list)
    for r in runs:
        by[r["method"]].append(r)
    res["A_summary"] = {k: {m_: summarise(v_, m_) for m_ in ("coverage_pct", "hard", "soft_penalty", "idle_gaps", "saturday_h", "seconds")}
                        for k, v_ in by.items()}
    cp, ga = by["CP-SAT pipeline"], by["GA memetic"]
    res["A_tests"] = {"coverage CP-SAT vs GA": mann_whitney([r["coverage_pct"] for r in cp], [r["coverage_pct"] for r in ga]),
                      "soft CP-SAT vs GA": mann_whitney([r["soft_penalty"] for r in cp], [r["soft_penalty"] for r in ga])}
    if by.get("GA plain"):
        res["A_tests"]["coverage GA memetic vs plain"] = mann_whitney([r["coverage_pct"] for r in ga], [r["coverage_pct"] for r in by["GA plain"]])
    _csv(OUT / "A_methods_runs.csv", runs)

    # ---------------- B: fairness (single-thread CP-SAT)
    res["B_single_thread_cpsat"] = section_b(ds, T)

    # ---------------- C: anytime
    budgets = [30, 60] if quick else [30, 60, 240]
    rows_c = []
    for b in budgets:
        r, sec = cpsat_pipeline(ds, b, 1)
        rows_c.append(row_of(ds, "CP-SAT pipeline", r, sec, 1, {"budget_s": b}))
        t1 = time.time()
        gr = solve_ga(ds, GAOptions(time_limit_s=b, seed=1))
        rows_c.append(row_of(ds, "GA memetic", gr, time.time() - t1, 1, {"budget_s": b}))
        log("C", b, rows_c[-2]["coverage_pct"], rows_c[-1]["coverage_pct"])
    for r in runs:
        if r["method"] in ("CP-SAT pipeline", "GA memetic") and r["seed"] == 1:
            rows_c.append(r | {"budget_s": T})
    res["C_anytime"] = rows_c
    _csv(OUT / "C_anytime.csv", rows_c)

    # ---------------- D: disruption vs quality (base = best CP-SAT run: highest coverage, then lowest soft penalty)
    res["D_base_seed"], base = best_cpsat
    res["D_tradeoff"] = section_d(ds, base, T, quick)

    # ---------------- E: per semester + heatmaps
    sem_rows = []
    for name, sol in [("Published grid", None), ("VBA macro (port)", best["VBA macro (port)"]),
                      ("CP-SAT pipeline", best["CP-SAT pipeline"]), ("GA memetic", best["GA memetic"])]:
        bkk = list(ds.published) if sol is None else assignments_to_bookings(ds, sol["assignments"])
        for sem in ds.semesters():
            toks = {r.token for r in ds.requirements if r.semester == sem}
            vv = validate(ds, bkk, TimeGrid.from_settings(True), scope_tokens=toks)
            sem_rows.append({"method": name, "semester": sem, "coverage_pct": vv["allocation"]["coverage_pct"],
                             "required_h": vv["allocation"]["required_hours"]})
    res["E_per_semester"] = sem_rows
    _csv(OUT / "E_per_semester.csv", sem_rows)
    res["E_heatmaps"] = heatmaps(ds, best)
    res["finished"] = time.strftime("%Y-%m-%d %H:%M")
    (OUT / "analysis_v3.json").write_text(json.dumps(res, indent=1, default=str), encoding="utf-8")
    figures(res)
    log("done", OUT)


def section_d(ds, base, T, quick):
    rng = random.Random(42)
    busy = defaultdict(set)
    for a in base["assignments"]:
        for f in ds.req(a["token"]).faculty:
            if not ds.is_placeholder(f):
                busy[f].add(a["day"])
    cands = sorted(f for f in busy if len(busy[f]) >= 3)
    scenarios = [(f, rng.choice(sorted(busy[f]))) for f in rng.sample(cands, 2 if quick else 4)]
    rows_d = []
    base_soft = row_of(ds, "base", base, 0)["soft_penalty"]
    for kw in ([0, 40] if quick else [0, 10, 40, 160]):
        for f, day in scenarios:
            hit = [a for a in base["assignments"] if f in ds.req(a["token"]).faculty and a["day"] == day]
            broken = {**base, "assignments": [a for a in base["assignments"] if a not in hit],
                      "unplaced": [{"session_id": a["session_id"], "token": a["token"], "code": ""} for a in hit]}
            t1 = time.time()
            fx = lns_repair(ds, broken, SolveOptions(forbid=[{"faculty": f, "day": day, "slot": None}]),
                            time_limit_s=max(15, T / 4), keep_weight=kw)
            before = {a["session_id"]: (a["day"], a["start"], a["room"]) for a in base["assignments"]}
            moved = sum(1 for a in fx["assignments"]
                        if a["session_id"] in before and before[a["session_id"]] != (a["day"], a["start"], a["room"]))
            hit_ids = {a["session_id"] for a in hit}
            replaced = sum(1 for a in fx["assignments"] if a["session_id"] in hit_ids)
            rr = row_of(ds, f"repair kw={kw}", fx, time.time() - t1, None,
                        {"keep_weight": kw, "faculty": f, "day": day, "displaced": len(hit), "re_placed": replaced,
                         "moved_other": moved - replaced,
                         "moved": moved, "moved_pct": round(100 * moved / max(1, len(fx["assignments"])), 2),
                         "resolved": replaced == len(hit), "soft_change": None})
            rr["soft_change"] = rr["soft_penalty"] - base_soft
            rows_d.append(rr)
            log("D", kw, f, day, len(hit), replaced, moved, rr["soft_change"])
    _csv(OUT / "D_disruption_tradeoff.csv", rows_d)
    return rows_d


def section_b(ds, T):
    """CP-SAT pipeline restricted to one worker (the GA is single-threaded)."""
    t0 = time.time()
    opts = SolveOptions(time_limit_s=T, seed=1, workers=1)
    dec = solve_decomposed(ds, opts)
    status = dec.get("status")
    r = lns_repair(ds, dec, opts, time_limit_s=max(15, T / 3)) if dec["unplaced"] else dec
    row = row_of(ds, "CP-SAT pipeline (1 worker)", r, time.time() - t0, 1,
                 {"phase1_status": status, "phase1_placed": len(dec["assignments"]), "phase1_unplaced": len(dec["unplaced"]),
                  "phase1_stats": json.dumps(dec.get("stats", {}), default=str)[:400]})
    log("B", row["coverage_pct"], status, len(dec["assignments"]), len(dec["unplaced"]))
    return row


def redo_bd(T: float) -> None:
    """Re-run sections B and D only, on the best CP-SAT seed recorded in analysis_v3.json."""
    ds = load_workbook_dataset()
    res = json.loads((OUT / "analysis_v3.json").read_text())
    cp = [r for r in res["A_runs"] if r["method"] == "CP-SAT pipeline"]
    seed = max(cp, key=lambda r: (r["coverage_pct"], -r["soft_penalty"]))["seed"]
    base, sec = cpsat_pipeline(ds, T, seed)
    log("D base seed", seed, row_of(ds, "base", base, sec)["coverage_pct"])
    res["B_single_thread_cpsat"] = section_b(ds, T)
    res["D_base_seed"] = seed
    res["D_tradeoff"] = section_d(ds, base, T, False)
    (OUT / "analysis_v3.json").write_text(json.dumps(res, indent=1, default=str), encoding="utf-8")
    figures(res)


def heatmaps(ds, best):
    """Daily teaching hours for sample batches: published vs CP-SAT vs GA (qualitative case study)."""
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    grid = TimeGrid.from_settings(True)
    batches = ["1B1", "3B1", "5B1", "7B1"]
    sols = [("Published grid", list(ds.published)), ("CP-SAT pipeline", assignments_to_bookings(ds, best["CP-SAT pipeline"]["assignments"])),
            ("GA memetic", assignments_to_bookings(ds, best["GA memetic"]["assignments"]))]
    fig, axes = plt.subplots(len(batches), len(sols), figsize=(13, 9))
    out = {}
    for i, b in enumerate(batches):
        for j, (name, bks) in enumerate(sols):
            mat = [[0] * 8 for _ in grid.days]
            for bk in bks:
                if b in bk.batches and bk.origin != "fixed":
                    for s in bk.slots:
                        if s <= 8:
                            mat[grid.day_index(bk.day)][s - 1] += 1
            ax = axes[i][j]
            ax.imshow(mat, cmap="Blues", vmin=0, vmax=2, aspect="auto")
            for d in range(len(grid.days)):
                for s in range(8):
                    if mat[d][s] > 1:
                        ax.text(s, d, "x", ha="center", va="center", color="red", fontsize=11, fontweight="bold")
            ax.set_xticks(range(8), [str(s) for s in range(1, 9)], fontsize=7)
            ax.set_yticks(range(len(grid.days)), grid.days, fontsize=7)
            if i == 0:
                ax.set_title(name, fontsize=10)
            if j == 0:
                ax.set_ylabel(f"batch {b}", fontsize=9)
            ax.axvline(4, color="grey", lw=0.5)
            out[f"{b}|{name}"] = mat
    fig.suptitle("Weekly occupancy (slot 5 = lunch; darker = busier; red x = more than one session in a slot)", fontsize=10)
    fig.tight_layout()
    fig.savefig(OUT / "fig_v3_heatmaps.png", dpi=150)
    plt.close(fig)
    return out


def figures(res):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    by = defaultdict(list)
    for r in res["A_runs"]:
        by[r["method"]].append(r)
    names = list(by)
    fig, ax = plt.subplots(1, 2, figsize=(13, 4.4))
    ax[0].boxplot([[r["coverage_pct"] for r in by[n]] for n in names])
    ax[0].set_xticks(range(1, len(names) + 1), [n.replace(" ", "\n", 1) for n in names], fontsize=8)
    ax[0].set_title("Hours scheduled (%) over seeds")
    ax[1].boxplot([[r["soft_penalty"] for r in by[n]] for n in names])
    ax[1].set_xticks(range(1, len(names) + 1), [n.replace(" ", "\n", 1) for n in names], fontsize=8)
    ax[1].set_title("Soft penalty over seeds (lower is better)")
    for a in ax:
        a.spines[["top", "right"]].set_visible(False)
    fig.tight_layout(); fig.savefig(OUT / "fig_v3_methods_box.png", dpi=150); plt.close(fig)

    rows = res["C_anytime"]
    fig, ax = plt.subplots(figsize=(7, 4))
    for mth, c in (("CP-SAT pipeline", "#2a6fdb"), ("GA memetic", "#e0703a")):
        pts = sorted((r["seconds"], r["coverage_pct"], r["budget_s"]) for r in rows if r["method"] == mth)
        ax.plot([p[0] for p in pts], [p[1] for p in pts], "o-", color=c, label=mth)
        for x_, y_, b_ in pts:
            ax.annotate(f"{b_:.0f}s budget", (x_, y_), textcoords="offset points", xytext=(4, -11), fontsize=7, color=c)
    ax.set_xscale("log"); ax.set_xlabel("actual wall-clock time (s, log scale)"); ax.set_ylabel("hours scheduled (%)")
    ax.set_title("Anytime behaviour"); ax.legend(frameon=False); ax.spines[["top", "right"]].set_visible(False)
    fig.tight_layout(); fig.savefig(OUT / "fig_v3_anytime.png", dpi=150); plt.close(fig)

    hist = res.get("ga_history", {})
    if hist:
        fig, ax = plt.subplots(figsize=(7, 4))
        for s, h in hist.items():
            ax.plot([p["t"] for p in h], [p["best_hard"] for p in h], alpha=0.8, label=f"seed {s}")
        ax.set_xlabel("seconds"); ax.set_ylabel("hard violations of best individual"); ax.set_title("GA convergence")
        ax.legend(frameon=False, fontsize=8); ax.spines[["top", "right"]].set_visible(False)
        fig.tight_layout(); fig.savefig(OUT / "fig_v3_ga_convergence.png", dpi=150); plt.close(fig)

    d = res["D_tradeoff"]
    fig, ax = plt.subplots(figsize=(7, 4))
    kws = sorted({r["keep_weight"] for r in d})
    xs = [statistics.mean(r["moved_pct"] for r in d if r["keep_weight"] == k) for k in kws]
    ys = [statistics.mean(r["soft_change"] for r in d if r["keep_weight"] == k) for k in kws]
    ax.plot(xs, ys, "o-", color="#5b9e5f")
    for k, x, y in zip(kws, xs, ys):
        ax.annotate(f"w={k}", (x, y), textcoords="offset points", xytext=(5, 5), fontsize=8)
    ax.set_xlabel("sessions moved (% of timetable)"); ax.set_ylabel("change in soft penalty vs base")
    ax.set_title("What-if repair: disruption vs quality"); ax.spines[["top", "right"]].set_visible(False)
    fig.tight_layout(); fig.savefig(OUT / "fig_v3_disruption_tradeoff.png", dpi=150); plt.close(fig)


def _csv(path, rows):
    keys = list(dict.fromkeys(k for r in rows for k in r))
    with open(path, "w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=keys)
        w.writeheader()
        w.writerows(rows)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--time", type=float, default=120)
    ap.add_argument("--seeds", type=int, default=5)
    ap.add_argument("--quick", action="store_true")
    ap.add_argument("--redo-bd", action="store_true", help="re-run only B (1 worker) and D (repair trade-off)")
    a = ap.parse_args()
    redo_bd(a.time) if a.redo_bd else main(a.time, a.seeds, a.quick)
