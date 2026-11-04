"""
CP-SAT + Large Neighbourhood Search for ITC-2007 CB-CTT (UD2).

The full CP-SAT model is built once (ctt_cpsat). An initial solution comes from a short CP-SAT run.
Then, until the time limit: choose a neighbourhood -- the lectures of one or two *days*, of a random
group of *curricula*, or in a random set of *rooms* -- fix every other lecture through solver
assumptions, warm-start from the incumbent (hints) and re-optimise for a few seconds; keep the result
if it is no worse. This is the same destroy-and-repair principle as the institutional repair agent.
"""
from __future__ import annotations

import random
import time

from ortools.sat.python import cp_model

from ttagent.benchmarks.ctt import Instance, evaluate
from ttagent.benchmarks.ctt_cpsat import build_ctt_model


def solve_ctt_lns(inst: Instance, time_limit_s: float = 120, workers: int = 8, seed: int = 7,
                  init_share: float = 0.5, sub_limit_s: float = 4.0) -> dict:
    t0 = time.time()
    rng = random.Random(seed)
    m, x = build_ctt_model(inst)
    solver = cp_model.CpSolver()
    solver.parameters.num_workers = workers
    solver.parameters.random_seed = seed
    solver.parameters.max_time_in_seconds = max(5.0, time_limit_s * init_share)
    st = solver.Solve(m)
    if st not in (cp_model.OPTIMAL, cp_model.FEASIBLE):
        return {"method": "cpsat_lns", "instance": inst.name, "status": solver.StatusName(st), "cost": None, "feasible": False,
                "seconds": round(time.time() - t0, 1)}
    cur = {k for k, v in x.items() if solver.Value(v)}
    best_cost = evaluate(inst, sorted(cur))["cost"]
    bound = solver.BestObjectiveBound()
    history = [(round(time.time() - t0, 1), best_cost)]
    if st == cp_model.OPTIMAL:
        return _out(inst, cur, "OPTIMAL", bound, t0, history, 0)
    days, ppd = inst.days, inst.ppd
    curricula = list(inst.curricula)
    rooms = [r for r, _ in inst.rooms]
    iters = 0
    while time.time() - t0 < time_limit_s - 1:
        iters += 1
        kind = rng.choice(["day", "curricula", "rooms"])
        if kind == "day":
            ds_ = set(rng.sample(range(days), 1 if rng.random() < 0.6 else 2))
            free = {k for k in cur if k[1] // ppd in ds_}
        elif kind == "curricula":
            qs = rng.sample(curricula, min(len(curricula), rng.randint(2, 6)))
            cs = {c for q in qs for c in inst.curricula[q]}
            free = {k for k in cur if k[0] in cs}
        else:
            rs = set(rng.sample(rooms, min(len(rooms), rng.randint(2, 4))))
            free = {k for k in cur if k[2] in rs}
        sub = m.Clone()                                       # fixed lectures become constants, so presolve removes them
        sx = {k: sub.GetBoolVarFromProtoIndex(v.Index()) for k, v in x.items()}
        for k in cur:
            if k not in free:
                sub.Add(sx[k] == 1)
        for k, v in sx.items():
            sub.AddHint(v, 1 if k in cur else 0)
        solver.parameters.max_time_in_seconds = min(sub_limit_s, max(0.5, time_limit_s - (time.time() - t0) - 0.5))
        st = solver.Solve(sub)
        if st in (cp_model.OPTIMAL, cp_model.FEASIBLE):
            cand = {k for k, v in sx.items() if solver.Value(v)}
            c = evaluate(inst, sorted(cand))
            if c["feasible"] and c["cost"] <= best_cost:
                if c["cost"] < best_cost:
                    history.append((round(time.time() - t0, 1), c["cost"]))
                cur, best_cost = cand, c["cost"]
        if best_cost <= bound:
            break
    return _out(inst, cur, "OPTIMAL" if best_cost <= bound else "FEASIBLE", bound, t0, history, iters)


def _out(inst, cur, status, bound, t0, history, iters):
    lectures = sorted(cur)
    ev = evaluate(inst, lectures)
    return {"method": "cpsat_lns", "instance": inst.name, "status": status, "cost": ev["cost"], "feasible": ev["feasible"],
            "hard_total": ev["hard_total"], "soft": ev["soft"], "bound": bound, "iterations": iters,
            "seconds": round(time.time() - t0, 1), "history": history, "lectures": lectures}
