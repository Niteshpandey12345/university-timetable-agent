"""CP-SAT model for ITC-2007 CB-CTT (UD2): x[c, p, r] = 1 iff a lecture of course c is in period p, room r."""
from __future__ import annotations

import time
from collections import defaultdict

from ortools.sat.python import cp_model

from ttagent.benchmarks.ctt import Instance, evaluate


def build_ctt_model(inst: Instance):
    """Return (model, x) for the UD2 formulation; x[(course, period, room)] -> BoolVar."""
    m = cp_model.CpModel()
    P, R = inst.periods, [r for r, _ in inst.rooms]
    cap = dict(inst.rooms)
    cur_of = inst.curricula_of()
    x, y = {}, {}
    for c in inst.courses:
        for p in range(P):
            if (c.cid, p) in inst.unavailable:
                continue
            vs = []
            for r in R:
                v = m.NewBoolVar("")
                x[(c.cid, p, r)] = v
                vs.append(v)
            yp = m.NewBoolVar("")
            m.Add(sum(vs) == yp)                       # at most one lecture of a course per period
            y[(c.cid, p)] = yp
        m.Add(sum(y[(c.cid, p)] for p in range(P) if (c.cid, p) in y) == c.lectures)    # H1
    for p in range(P):
        for r in R:
            vs = [x[(c.cid, p, r)] for c in inst.courses if (c.cid, p, r) in x]
            if len(vs) > 1:
                m.AddAtMostOne(vs)                       # H2
    by_teacher = defaultdict(list)
    for c in inst.courses:
        by_teacher[c.teacher].append(c.cid)
    for p in range(P):
        for cs in by_teacher.values():
            vs = [y[(c, p)] for c in cs if (c, p) in y]
            if len(vs) > 1:
                m.AddAtMostOne(vs)                       # H3 teacher
    occ = {}
    for q, cs in inst.curricula.items():
        for p in range(P):
            vs = [y[(c, p)] for c in cs if (c, p) in y]
            if len(vs) > 1:
                m.AddAtMostOne(vs)                       # H3 curriculum
            occ[(q, p)] = sum(vs) if vs else 0
    obj = []
    students = {c.cid: c.students for c in inst.courses}
    for (c, p, r), v in x.items():                      # RoomCapacity
        over = students[c] - cap[r]
        if over > 0:
            obj.append(over * v)
    for c in inst.courses:                              # MinWorkingDays
        dz = []
        for d in range(inst.days):
            ps = [y[(c.cid, p)] for p in range(d * inst.ppd, (d + 1) * inst.ppd) if (c.cid, p) in y]
            if not ps:
                continue
            z = m.NewBoolVar("")
            m.Add(z <= sum(ps))
            dz.append(z)
        short = m.NewIntVar(0, c.min_days, "")
        m.Add(short >= c.min_days - sum(dz))
        obj.append(5 * short)
    for c in inst.courses:                              # RoomStability
        us = []
        for r in R:
            u = m.NewBoolVar("")
            for p in range(P):
                if (c.cid, p, r) in x:
                    m.AddImplication(x[(c.cid, p, r)], u)
            us.append(u)
        extra = m.NewIntVar(0, len(R), "")
        m.Add(extra >= sum(us) - 1)
        obj.append(extra)
    for q in inst.curricula:                            # IsolatedLectures
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
    m.Minimize(sum(obj))
    return m, x


def solve_ctt_cpsat(inst: Instance, time_limit_s: float = 120, workers: int = 8, seed: int = 7,
                    hint: list[tuple[str, int, str]] | None = None) -> dict:
    t0 = time.time()
    m, x = build_ctt_model(inst)
    if hint:
        hs = {(c, p, r) for c, p, r in hint}
        for k, v in x.items():
            m.AddHint(v, 1 if k in hs else 0)
    solver = cp_model.CpSolver()
    solver.parameters.max_time_in_seconds = float(time_limit_s)
    solver.parameters.num_workers = workers
    solver.parameters.random_seed = seed
    st = solver.Solve(m)
    lectures = [k for k, v in x.items() if st in (cp_model.OPTIMAL, cp_model.FEASIBLE) and solver.Value(v)]
    ev = evaluate(inst, lectures) if lectures else {"feasible": False, "cost": None, "hard_total": None}
    return {"method": "cpsat", "instance": inst.name, "status": solver.StatusName(st),
            "objective": solver.ObjectiveValue() if lectures else None,
            "bound": solver.BestObjectiveBound() if lectures else None,
            "cost": ev.get("cost"), "feasible": ev.get("feasible"), "hard_total": ev.get("hard_total"),
            "soft": ev.get("soft"), "seconds": round(time.time() - t0, 1), "vars": len(x), "lectures": lectures}
