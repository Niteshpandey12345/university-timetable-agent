"""
Time-then-room decomposition for ITC-2007 CB-CTT (UD2) -- the same idea as the institutional engine
(ttagent/engine/decomposed.py) and Lach & Luebbecke (2012).

Stage 1 (periods): y[c,p]; at most #rooms lectures per period; teacher/curriculum conflicts; unavailability.
         Objective: MinWorkingDays + IsolatedLectures + a room-capacity estimate. For every period and every
         room-size threshold k (the k-th largest room), lectures that need more seats than the k-th largest room
         beyond the k-1 larger rooms must overflow, so  sum_c [students_c > cap_k] y[c,p] - (k-1)  is a lower bound
         on the lectures that cannot get a room at least as big; the minimum excess students is charged.
Stage 2 (rooms): with periods fixed, x[c,p,r] only for chosen (c,p): one room per lecture, one lecture per room,
         minimise RoomCapacity + RoomStability.
Stage 3 (polish): the full model (ctt_cpsat) warm-started from the decomposed solution for the remaining time.
"""
from __future__ import annotations

import time
from collections import defaultdict

from ortools.sat.python import cp_model

from ttagent.benchmarks.ctt import Instance, evaluate


def _time_model(inst: Instance):
    m = cp_model.CpModel()
    P = inst.periods
    nrooms = len(inst.rooms)
    caps = sorted((c for _, c in inst.rooms), reverse=True)
    y = {}
    for c in inst.courses:
        for p in range(P):
            if (c.cid, p) not in inst.unavailable:
                y[(c.cid, p)] = m.NewBoolVar("")
        m.Add(sum(y[(c.cid, p)] for p in range(P) if (c.cid, p) in y) == c.lectures)
    for p in range(P):
        vs = [y[(c.cid, p)] for c in inst.courses if (c.cid, p) in y]
        if len(vs) > nrooms:
            m.Add(sum(vs) <= nrooms)
    by_teacher = defaultdict(list)
    for c in inst.courses:
        by_teacher[c.teacher].append(c.cid)
    for p in range(P):
        for cs in by_teacher.values():
            vs = [y[(c, p)] for c in cs if (c, p) in y]
            if len(vs) > 1:
                m.AddAtMostOne(vs)
    occ = {}
    for q, cs in inst.curricula.items():
        for p in range(P):
            vs = [y[(c, p)] for c in cs if (c, p) in y]
            if len(vs) > 1:
                m.AddAtMostOne(vs)
            occ[(q, p)] = sum(vs) if vs else 0
    obj = []
    for c in inst.courses:                                   # MinWorkingDays
        dz = []
        for d in range(inst.days):
            ps = [y[(c.cid, p)] for p in range(d * inst.ppd, (d + 1) * inst.ppd) if (c.cid, p) in y]
            if ps:
                z = m.NewBoolVar("")
                m.Add(z <= sum(ps))
                dz.append(z)
        short = m.NewIntVar(0, c.min_days, "")
        m.Add(short >= c.min_days - sum(dz))
        obj.append(5 * short)
    for q in inst.curricula:                                 # IsolatedLectures
        for p in range(P):
            o = occ[(q, p)]
            if isinstance(o, int):
                continue
            d, s = divmod(p, inst.ppd)
            prev = occ.get((q, p - 1), 0) if s > 0 else 0
            nxt = occ.get((q, p + 1), 0) if s < inst.ppd - 1 else 0
            iso = m.NewBoolVar("")
            m.Add(iso >= o - prev - nxt)
            obj.append(2 * iso)
    # room-capacity estimate (Hall-type counting per size threshold)
    stud = {c.cid: c.students for c in inst.courses}
    distinct = sorted(set(caps), reverse=True)
    for p in range(P):
        for cap in distinct[1:]:
            k = sum(1 for x in caps if x > cap)              # rooms strictly bigger than this threshold
            big = [(c.cid, stud[c.cid] - cap) for c in inst.courses if stud[c.cid] > cap and (c.cid, p) in y]
            if len(big) <= k:
                continue
            over = m.NewIntVar(0, len(big), "")
            m.Add(over >= sum(y[(c, p)] for c, _ in big) - k)
            obj.append(min(e for _, e in big) * over)
    m.Minimize(sum(obj))
    return m, y


def _room_model(inst: Instance, periods: dict[str, list[int]], hint_rooms: dict | None = None):
    m = cp_model.CpModel()
    R = [r for r, _ in inst.rooms]
    cap = dict(inst.rooms)
    stud = {c.cid: c.students for c in inst.courses}
    x = {}
    for c, ps in periods.items():
        for p in ps:
            vs = []
            for r in R:
                v = m.NewBoolVar("")
                x[(c, p, r)] = v
                vs.append(v)
            m.AddExactlyOne(vs)
    by_pr = defaultdict(list)
    for (c, p, r), v in x.items():
        by_pr[(p, r)].append(v)
    for vs in by_pr.values():
        if len(vs) > 1:
            m.AddAtMostOne(vs)
    obj = [max(0, stud[c] - cap[r]) * v for (c, p, r), v in x.items() if stud[c] > cap[r]]
    for c in periods:
        us = []
        for r in R:
            u = m.NewBoolVar("")
            for p in periods[c]:
                m.AddImplication(x[(c, p, r)], u)
            us.append(u)
        extra = m.NewIntVar(0, len(R), "")
        m.Add(extra >= sum(us) - 1)
        obj.append(extra)
    m.Minimize(sum(obj))
    return m, x


def solve_ctt_decomposed(inst: Instance, time_limit_s: float = 120, workers: int = 8, seed: int = 7,
                         split: tuple[float, float] = (0.5, 0.15)) -> dict:
    from ttagent.benchmarks.ctt_cpsat import solve_ctt_cpsat
    t0 = time.time()
    m1, y = _time_model(inst)
    s1 = cp_model.CpSolver()
    s1.parameters.max_time_in_seconds = time_limit_s * split[0]
    s1.parameters.num_workers = workers
    s1.parameters.random_seed = seed
    st = s1.Solve(m1)
    if st not in (cp_model.OPTIMAL, cp_model.FEASIBLE):
        return {"method": "cpsat_decomposed", "instance": inst.name, "status": s1.StatusName(st), "feasible": False, "cost": None}
    periods = defaultdict(list)
    for (c, p), v in y.items():
        if s1.Value(v):
            periods[c].append(p)
    m2, x = _room_model(inst, periods)
    s2 = cp_model.CpSolver()
    s2.parameters.max_time_in_seconds = time_limit_s * split[1]
    s2.parameters.num_workers = workers
    s2.parameters.random_seed = seed
    st2 = s2.Solve(m2)
    if st2 not in (cp_model.OPTIMAL, cp_model.FEASIBLE):   # rooms always exist (<= #rooms per period), so rare
        return {"method": "cpsat_decomposed", "instance": inst.name, "status": "ROOMS_" + s2.StatusName(st2), "feasible": False, "cost": None}
    lectures = [k for k, v in x.items() if s2.Value(v)]
    stage = evaluate(inst, lectures)
    remaining = time_limit_s - (time.time() - t0)
    polished = None
    if remaining > 5:
        polished = solve_ctt_cpsat(inst, time_limit_s=remaining, workers=workers, seed=seed, hint=lectures)
        if polished.get("feasible") and polished["cost"] < stage["cost"]:
            lectures = polished["lectures"]
    ev = evaluate(inst, lectures)
    return {"method": "cpsat_decomposed", "instance": inst.name, "status": "FEASIBLE", "feasible": ev["feasible"],
            "cost": ev["cost"], "hard_total": ev["hard_total"], "soft": ev["soft"],
            "stage1_bound_no_stability": s1.BestObjectiveBound(), "stage2_cost": stage["cost"],
            "polish_bound": (polished or {}).get("bound"), "seconds": round(time.time() - t0, 1), "lectures": lectures}
