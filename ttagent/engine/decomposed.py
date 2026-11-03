"""
Time-then-room decomposition (the default production solver).

Stage 1  (time assignment, CP-SAT)
    y[s, d, t] -- no room index, ~19k booleans for the whole university.
    Room feasibility is kept by *Hall-type capacity cuts*: for every (d, h),
    every room pool and every capacity threshold c,
        #sessions needing that pool with batch-need >= c  <=  #rooms in pool with capacity >= c.
    Lexicographic: 1a maximise placed hours  ->  1b minimise soft penalties
    subject to "placed hours >= 1a optimum" (warm-started from 1a).

Stage 2  (room assignment, CP-SAT matching)
    z[s, r] over all suitable rooms; minimise room changes vs the requested
    venue and over-sized rooms; never moves a time.

Sessions that stage 2 cannot room (rare: the cuts are necessary, not always
sufficient) are handed to the LNS repair (`repair.py`) which re-solves a small
neighbourhood with the monolithic x[s,d,t,r] model.
"""
from __future__ import annotations

import time
from collections import defaultdict
from typing import Any

from ortools.sat.python import cp_model

from ttagent.config import settings
from ttagent.domain.models import Dataset, Requirement, session_ids
from ttagent.domain.rooms import room_suitable
from ttagent.domain.timegrid import TimeGrid
from ttagent.engine.cpsat import SolveOptions, _blocked_index, _blocks


def _pref_hours(ds: Dataset, r: Requirement, d: str, t: int) -> int:
    """Hours of a session at (d, t) that fall in slots one of its teachers asked to avoid."""
    n = 0
    for f in r.faculty:
        fac = ds.faculty.get(f)
        if fac and fac.avoid:
            n += sum(1 for s in range(t, t + r.duration) for d0, s0 in fac.avoid if d0 == d and (s0 is None or s0 == s))
    return n


def _pool(ds: Dataset, r: Requirement) -> str:
    home = ds.rooms.get(r.venue)
    if home is None:
        return f"room:{r.venue}"
    if home.category == "special_lab":
        return f"lab:{home.lab_type}"
    if home.category == "computer_lab":
        return "computer"
    if home.category == "classroom":
        return "teach:CR"
    return "teach:LT"


def _room_cuts(ds: Dataset, fixed_room_busy: set) -> list[tuple[str, int, set[str], list[str]]]:
    """(name, threshold, session-pools covered, rooms) groups for Hall cuts."""
    rooms = list(ds.rooms.values())
    groups: list[tuple[str, set[str], list]] = []
    lab_types = {r.lab_type for r in rooms if r.category == "special_lab"}
    for lt in lab_types:
        groups.append((f"lab:{lt}", {f"lab:{lt}"}, [r for r in rooms if r.category == "special_lab" and r.lab_type == lt]))
    groups.append(("computer", {"computer"}, [r for r in rooms if r.category == "computer_lab"]))
    lt_rooms = [r for r in rooms if r.category in ("lecture_theatre", "digital_lc")]
    cr_lt = [r for r in rooms if r.category in ("classroom", "lecture_theatre")]
    all_teach = [r for r in rooms if r.category in ("classroom", "lecture_theatre", "digital_lc")]
    groups += [("teach:LT", {"teach:LT"}, lt_rooms), ("teach:CR", {"teach:CR"}, cr_lt),
               ("teach:ALL", {"teach:LT", "teach:CR"}, all_teach)]
    out = []
    for name, pools, rs in groups:
        for c in sorted({r.observed_batch_capacity for r in rs}):
            out.append((name, c, pools, [r.room_id for r in rs if r.observed_batch_capacity >= c]))
    return out


def solve_decomposed(ds: Dataset, opts: SolveOptions | None = None) -> dict[str, Any]:
    opts = opts or SolveOptions()
    t0 = time.time()
    cfg = settings()["solver"]
    W = dict(cfg["weights"]) | opts.weights
    if opts.stability is not None:
        W["stability"] = opts.stability
    grid = TimeGrid.from_settings(use_extended=opts.use_extended)
    req_by_tok = {r.token: r for r in ds.requirements}
    scope = [r for r in ds.requirements
             if (opts.semesters is None or r.semester in opts.semesters)
             and (opts.tokens is None or r.token in opts.tokens)]
    basket_of = (lambda r: r.basket_id) if opts.use_baskets else (lambda r: None)

    from ttagent.domain.models import Booking
    fixed = list(ds.fixed_bookings) if opts.include_fixed else []
    frozen_ids = {f["session_id"] for f in opts.frozen}
    for f in opts.frozen:
        r = req_by_tok[f["token"]]
        fixed.append(Booking(day=f["day"], start=f["start"], duration=r.duration, batches=r.batches, faculty=r.faculty,
                             room=f["room"], course=r.course, kind=r.kind, origin="frozen", session_id=f["session_id"],
                             token=r.token, basket_id=basket_of(r)))
    fb_batch, fb_fac, fb_room = _blocked_index(ds, fixed)

    unavailable = set()
    for code, fac in ds.faculty.items():
        for d, s in fac.unavailable:
            unavailable.add(("faculty", code, d, s))
    for fb in opts.forbid:
        for kind in ("faculty", "batch", "room"):
            if fb.get(kind):
                unavailable.add((kind, fb[kind], fb["day"], fb.get("slot")))

    def unav(kind, code, day, slots):
        return (kind, code, day, None) in unavailable or any((kind, code, day, s) in unavailable for s in slots)

    pins_by_tok = defaultdict(list)
    for p in opts.pins:
        pins_by_tok[int(p["token"])].append(p)
    ref_by_tok = defaultdict(list)
    for rs in ds.reference:
        ref_by_tok[rs.token].append(rs)

    # ======================================================== stage 1 model
    m = cp_model.CpModel()
    Y: dict[str, list] = {}
    P: dict[str, Any] = {}
    sess: list[tuple[str, Requirement, int]] = []
    fac_cov, batch_cov, basket_cov = defaultdict(list), defaultdict(list), defaultdict(list)
    pool_cov = defaultdict(list)       # (pool, need, d, h) -> vars
    home_cov = defaultdict(list)       # (venue, d, h) -> vars
    basket_batches = defaultdict(set)
    sat_terms, late_terms, ext_terms, stab_terms, pref_terms = [], [], [], [], []
    no_cand = {}
    for r in scope:
        bk = basket_of(r)
        if bk:
            basket_batches[bk].update(r.batches)
        pool, need = _pool(ds, r), len(r.batches)
        pins = sorted(pins_by_tok.get(r.token, []), key=lambda p: grid.day_index(p["day"]))
        refs = sorted(ref_by_tok.get(r.token, []), key=lambda x: (grid.day_index(x.day), x.start))
        frozen_days = {f["day"] for f in opts.frozen if f["token"] == r.token}
        suitable_rooms = [rm.room_id for rm in ds.rooms.values() if room_suitable(ds, r, rm)]
        for k, sid in enumerate(session_ids(r)):
            if sid in frozen_ids:
                continue
            pin = pins[k] if k < len(pins) else None
            cands = []
            for d in grid.days:
                if (pin and d != pin["day"]) or d in frozen_days:
                    continue
                for t in grid.starts_for(d, r.duration):
                    if pin and t != int(pin["start"]):
                        continue
                    slots = range(t, t + r.duration)
                    if any(unav("faculty", f, d, slots) for f in r.faculty if not ds.is_placeholder(f)):
                        continue
                    if any(unav("batch", b, d, slots) for b in r.batches):
                        continue
                    ok = True
                    for s in slots:
                        if any((f, d, s) in fb_fac for f in r.faculty if not ds.is_placeholder(f)):
                            ok = False
                        for b in r.batches:
                            if any(not (bk and o == bk) for o in fb_batch.get((b, d, s), [])):
                                ok = False
                        # at least one suitable room free of fixed bookings
                        if not any((rm, d, s) not in fb_room and not unav("room", rm, d, [s]) for rm in suitable_rooms):
                            ok = False
                    if ok:
                        cands.append((d, t))
            sess.append((sid, r, k))
            if not cands:
                no_cand[sid] = "no legal (day, slot) after fixed bookings/pins/unavailability"
            ys = []
            for d, t in cands:
                v = m.NewBoolVar(f"y_{sid}_{d}{t}")
                ys.append((v, d, t))
                for s in range(t, t + r.duration):
                    for f in r.faculty:
                        if not ds.is_placeholder(f):
                            fac_cov[(f, d, s)].append(v)
                    for b in r.batches:
                        (basket_cov[(bk, b, d, s)] if bk else batch_cov[(b, d, s)]).append(v)
                    pool_cov[(pool, need, d, s)].append(v)
                    home_cov[(r.venue, d, s)].append(v)
                    if s >= 7:
                        late_terms.append(v)
                    if grid.is_extended(s):
                        ext_terms.append(v)
                if d == "Sat":
                    sat_terms.append((v, r.duration))
                ph = _pref_hours(ds, r, d, t)
                if ph:
                    pref_terms.append((v, ph))
            Y[sid] = ys
            p = m.NewBoolVar(f"p_{sid}")
            P[sid] = p
            m.Add(sum(v for v, *_ in ys) == p)
            if k < len(refs):
                ref = refs[k]
                hit = [v for v, d, t in ys if d == ref.day and t == ref.start]
                if opts.hint_reference and hit:
                    m.AddHint(hit[0], 1)
                if W.get("stability") and hit:
                    stay = m.NewBoolVar("")
                    m.Add(hit[0] >= stay)
                    stab_terms.append(stay)
                elif W.get("stability"):
                    stab_terms.append(0)
        inst = [sid for sid in session_ids(r) if sid in P]
        if opts.hints or opts.frozen:   # repair mode: see cpsat.py -- one per day, no ordering
            for d in grid.days:
                vs = [v for sid in inst for v, dd, _ in Y[sid] if dd == d]
                if len(vs) > 1:
                    m.AddAtMostOne(vs)
        else:
            for a, b in zip(inst, inst[1:]):
                m.AddImplication(P[b], P[a])
                m.Add(sum(grid.day_index(d) * v for v, d, _ in Y[b]) >= sum(grid.day_index(d) * v for v, d, _ in Y[a]) + 1).OnlyEnforceIf(P[b])

    keep_terms = []
    for h in opts.hints:
        ys_h = Y.get(h["session_id"], [])
        for v, d, t in ys_h:
            if d == h["day"] and t == h["start"]:
                m.AddHint(v, 1)
        if opts.keep_weight and ys_h:
            same = [v for v, d, t in ys_h if d == h["day"] and t == h["start"]]
            stay = m.NewBoolVar("")
            m.Add(sum(same) >= stay) if same else m.Add(stay == 0)
            keep_terms.append(stay)

    for vs in fac_cov.values():
        if len(vs) > 1:
            m.AddAtMostOne(vs)
    U = {}
    for (bk, b, d, s), vs in basket_cov.items():
        u = m.NewBoolVar("")
        for v in vs:
            m.AddImplication(v, u)
        m.Add(u <= sum(vs))
        U[(bk, b, d, s)] = u
    bk_of_batch = defaultdict(list)
    for bk, bs in basket_batches.items():
        for b in bs:
            bk_of_batch[b].append(bk)
    scope_batches = {b for r in scope for b in r.batches}
    occ = {}
    for b in scope_batches:
        for d in grid.days:
            for s in grid.slots_for(d):
                terms = list(batch_cov.get((b, d, s), [])) + [U[(bk, b, d, s)] for bk in bk_of_batch.get(b, []) if (bk, b, d, s) in U]
                if len(terms) > 1:
                    m.Add(sum(terms) <= 1)
                occ[(b, d, s)] = terms

    # Hall cuts (room capacity per pool and capacity threshold)
    n_cuts = 0
    cuts = _room_cuts(ds, fb_room)
    pool_by_ds = defaultdict(list)
    for (pl, need, dd, ss), lst in pool_cov.items():
        pool_by_ds[(dd, ss)].append((pl, need, lst))
    for d in grid.days:
        for s in grid.slots_for(d):
            for name, c, pools, rooms in cuts:
                free = sum(1 for rm in rooms if (rm, d, s) not in fb_room and not unav("room", rm, d, [s]))
                vs = [v for pl, need, lst in pool_by_ds[(d, s)] if pl in pools and need >= c for v in lst]
                if len(vs) > free:
                    m.Add(sum(vs) <= free)
                    n_cuts += 1

    placed_hours = sum(r.duration * P[sid] for sid, r, _ in sess)
    total_hours = sum(r.duration for _, r, _ in sess)

    # ---- phase 1a: maximise placement
    t1 = time.time()
    s1 = cp_model.CpSolver()
    s1.parameters.max_time_in_seconds = max(5.0, opts.time_limit_s * 0.35)
    s1.parameters.num_workers = opts.workers
    s1.parameters.random_seed = opts.seed
    # lexicographic: placement dominates; among equal placements stay where the hints (previous version) were
    m.Maximize((len(keep_terms) + 1) * placed_hours + sum(keep_terms)) if keep_terms else m.Maximize(placed_hours)
    st1 = s1.Solve(m)
    phase1 = {"status": s1.StatusName(st1), "placed_h": None, "s": round(time.time() - t1, 2)}
    if st1 not in (cp_model.OPTIMAL, cp_model.FEASIBLE):
        return _empty(sess, phase1["status"], t0, opts)
    best_placed = int(sum(r.duration * s1.Value(P[sid]) for sid, r, _ in sess))
    phase1["placed_h"] = best_placed
    phase1["upper_bound_h"] = int(s1.BestObjectiveBound() // (len(keep_terms) + 1)) if keep_terms else int(s1.BestObjectiveBound())

    # ---- phase 1b: soft quality, keeping placement
    m.ClearHints()
    for sid, ys in Y.items():
        for v, *_ in ys:
            m.AddHint(v, s1.Value(v))
    m.Add(placed_hours >= best_placed)
    obj = []
    n_soft = 0
    if opts.soft:
        dcap, fcap, fcons = cfg["daily_batch_cap"], cfg["faculty_daily_cap"], cfg["faculty_max_consecutive"]
        for b in scope_batches:
            for d in grid.days:
                slots = grid.slots_for(d)
                o = {s: sum(occ[(b, d, s)]) for s in slots}
                for blk in _blocks(slots, grid.lunch_slot):
                    for i in range(1, len(blk) - 1):
                        g = m.NewBoolVar("")
                        m.Add(g >= o[blk[i - 1]] + o[blk[i + 1]] - o[blk[i]] - 1)
                        obj.append(W["batch_gap"] * g); n_soft += 1
                    for i in range(0, len(blk) - 3):
                        g = m.NewBoolVar("")
                        m.Add(g >= o[blk[i]] + o[blk[i + 3]] - o[blk[i + 1]] - o[blk[i + 2]] - 1)
                        obj.append(2 * W["batch_gap"] * g); n_soft += 1
                ex = m.NewIntVar(0, len(slots), "")
                m.Add(ex >= sum(o.values()) - dcap)
                obj.append(W["batch_day_overload"] * ex); n_soft += 1
        for f in {f for r in scope for f in r.faculty if not ds.is_placeholder(f)}:
            for d in grid.days:
                slots = grid.slots_for(d)
                if not any(fac_cov.get((f, d, s)) for s in slots):
                    continue
                of = {s: sum(fac_cov.get((f, d, s), [])) for s in slots}
                for blk in _blocks(slots, grid.lunch_slot):
                    for i in range(0, len(blk) - fcons):
                        win = blk[i:i + fcons + 1]
                        ex = m.NewIntVar(0, len(win), "")
                        m.Add(ex >= sum(of[s] for s in win) - fcons)
                        obj.append(W["faculty_consecutive"] * ex); n_soft += 1
                ex = m.NewIntVar(0, len(slots), "")
                m.Add(ex >= sum(of.values()) - fcap)
                obj.append(W["faculty_day_overload"] * ex); n_soft += 1
        obj += [W["saturday_hour"] * dur * v for v, dur in sat_terms]
        obj += [W["late_hour"] * v for v in late_terms]
        obj += [W.get("faculty_preference", 5) * h * v for v, h in pref_terms]
        # proxy for room changes: more than one session wanting the same home venue at once
        for (venue, d, s), vs in home_cov.items():
            if len(vs) > 1:
                ex = m.NewIntVar(0, len(vs), "")
                m.Add(ex >= sum(vs) - 1)
                obj.append(W["room_change"] * ex); n_soft += 1
    obj += [W["extended_hour"] * v for v in ext_terms]
    if W.get("stability"):
        obj += [W["stability"] * (1 - s) for s in stab_terms]
    obj += [opts.keep_weight * (1 - s) for s in keep_terms]
    obj.append(W["unplaced_hour"] * (total_hours - placed_hours))
    m.Minimize(sum(obj))
    t2 = time.time()
    s2 = cp_model.CpSolver()
    s2.parameters.max_time_in_seconds = max(5.0, opts.time_limit_s * 0.55)
    s2.parameters.num_workers = opts.workers
    s2.parameters.random_seed = opts.seed
    st2 = s2.Solve(m)
    solver_used = s2 if st2 in (cp_model.OPTIMAL, cp_model.FEASIBLE) else s1
    phase2 = {"status": s2.StatusName(st2), "objective": s2.ObjectiveValue() if solver_used is s2 else None,
              "bound": s2.BestObjectiveBound() if solver_used is s2 else None, "s": round(time.time() - t2, 2)}

    timed = []
    unplaced = []
    for sid, r, k in sess:
        hit = next(((d, t) for v, d, t in Y[sid] if solver_used.Value(v)), None)
        if hit:
            timed.append((sid, r, hit[0], hit[1]))
        else:
            unplaced.append({"session_id": sid, "token": r.token, "code": r.code,
                             "reason": no_cand.get(sid, "no conflict-free time found (stage 1)")})

    # ======================================================== stage 2: rooms
    t3 = time.time()
    prefer = {h["session_id"]: (h["day"], h["start"], h["room"]) for h in opts.hints}
    rooms_res = assign_rooms(ds, timed, fb_room, unav, opts.time_limit_s * 0.1, opts.workers, prefer)
    assignments = rooms_res["assignments"]
    for sid, r in rooms_res["unroomed"]:
        unplaced.append({"session_id": sid, "token": r.token, "code": r.code, "reason": "no free suitable room at chosen time (stage 2)"})
    assignments.extend({**f, "origin": "frozen"} for f in opts.frozen)
    return {
        "method": "decomposed",
        "status": phase2["status"] if solver_used is s2 else phase1["status"],
        "objective": phase2["objective"],
        "best_bound": phase2["bound"],
        "assignments": assignments,
        "unplaced": unplaced,
        "scope_tokens": [r.token for r in scope],
        "stats": {"sessions": len(sess), "frozen": len(opts.frozen), "bool_vars": len(m.Proto().variables),
                  "hall_cuts": n_cuts, "soft_terms": n_soft, "phase1": phase1, "phase2": phase2,
                  "stage2": {"s": round(time.time() - t3, 2), "room_changes": rooms_res["room_changes"],
                             "unroomed": len(rooms_res["unroomed"])},
                  "wall_s": round(time.time() - t0, 2)},
        "options": {k: v for k, v in opts.__dict__.items() if k not in ("hints", "frozen")},
    }


def assign_rooms(ds: Dataset, timed, fb_room: set, unav, time_limit: float = 10, workers: int = 8,
                 prefer: dict | None = None) -> dict[str, Any]:
    m = cp_model.CpModel()
    Z = {}
    cov = defaultdict(list)
    changes, oversize, kept = [], [], []
    prefer = prefer or {}
    for sid, r, d, t in timed:
        opts = []
        for rm in ds.rooms.values():
            if not room_suitable(ds, r, rm):
                continue
            slots = range(t, t + r.duration)
            if any((rm.room_id, d, s) in fb_room for s in slots) or unav("room", rm.room_id, d, list(slots)):
                continue
            v = m.NewBoolVar("")
            opts.append((v, rm.room_id))
            for s in slots:
                cov[(rm.room_id, d, s)].append(v)
            if rm.room_id != r.venue:
                changes.append(v)
                oversize.append((rm.observed_batch_capacity - len(r.batches)) * v)
            if prefer.get(sid) == (d, t, rm.room_id):
                kept.append(v)
        Z[sid] = opts
        m.Add(sum(v for v, _ in opts) <= 1)
    for vs in cov.values():
        if len(vs) > 1:
            m.AddAtMostOne(vs)
    roomed = sum(v for opts in Z.values() for v, _ in opts)
    m.Maximize(10000 * roomed + 300 * sum(kept) - 100 * sum(changes) - sum(oversize))
    s = cp_model.CpSolver()
    s.parameters.max_time_in_seconds = max(3.0, time_limit)
    s.parameters.num_workers = workers
    s.Solve(m)
    out, unroomed = [], []
    for sid, r, d, t in timed:
        room = next((rid for v, rid in Z[sid] if s.Value(v)), None)
        if room:
            out.append({"session_id": sid, "token": r.token, "day": d, "start": t, "room": room})
        else:
            unroomed.append((sid, r))
    return {"assignments": out, "unroomed": unroomed,
            "room_changes": sum(1 for a in out if a["room"] != ds.req(a["token"]).venue)}


def _empty(sess, status, t0, opts):
    return {"method": "decomposed", "status": status, "objective": None, "best_bound": None, "assignments": [],
            "unplaced": [{"session_id": sid, "token": r.token, "code": r.code, "reason": status} for sid, r, _ in sess],
            "scope_tokens": sorted({r.token for _, r, _ in sess}), "stats": {"wall_s": round(time.time() - t0, 2)},
            "options": {k: v for k, v in opts.__dict__.items() if k not in ("hints", "frozen")}}
