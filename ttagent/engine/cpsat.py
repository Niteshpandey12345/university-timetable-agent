"""
CP-SAT timetable model (Google OR-Tools).

Decision variables
    x[s, d, t, r] = 1  iff session s starts on day d at slot t in room r
    p[s]          = 1  iff session s is placed (lets the model return the best
                       *partial* timetable instead of just "INFEASIBLE")
    u[K, d, h]    = 1  iff elective basket K is running at (d, h)

Hard constraints
    room       sum of sessions covering (r, d, h)                    <= 1
    faculty    sum of sessions taught by f covering (d, h)           <= 1   (placeholders exempt)
    batch      sum of non-elective sessions of b covering (d, h)
               + sum of baskets K containing b active at (d, h)      <= 1
    blocks     an L-hour block never crosses lunch / leaves the day   (domain filtering)
    spread     instances of one requirement on strictly increasing days (no same-day repeat,
               plus symmetry breaking)
    fixed      external bookings (BBA, MCA...), frozen sessions, pins, unavailability

Objective (minimise)
    1000*unplaced_hours + 8*batch_gaps + 6*batch_day_overload + 6*faculty_consecutive
    + 3*faculty_day_overload + 2*saturday_hours + 1*late_hours + 25*extended_hours
    + 4*room_changes [+ stability * moved_sessions]
"""
from __future__ import annotations

import time
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any

from ortools.sat.python import cp_model

from ttagent.config import settings
from ttagent.domain.models import Booking, Dataset, Requirement, session_ids
from ttagent.domain.rooms import room_options
from ttagent.domain.timegrid import TimeGrid


@dataclass
class SolveOptions:
    time_limit_s: float = 60
    workers: int = 8
    seed: int = 7
    semesters: list[int] | None = None
    tokens: list[int] | None = None
    use_baskets: bool = True
    room_mode: str = "flex"          # flex | home
    max_alt_rooms: int = 5
    use_extended: bool = False
    soft: bool = True
    weights: dict[str, float] = field(default_factory=dict)
    stability: float | None = None   # overrides weights.stability
    hint_reference: bool = True
    hints: list[dict] = field(default_factory=list)
    pins: list[dict] = field(default_factory=list)       # {token, day, start, room?}
    forbid: list[dict] = field(default_factory=list)     # {faculty|batch|room, day, slot?}
    frozen: list[dict] = field(default_factory=list)     # assignments kept as-is (LNS)
    keep_weight: float = 0.0          # >0: penalise moving a hinted session (minimal-perturbation repair)
    include_fixed: bool = True
    log: bool = False

    @classmethod
    def from_dict(cls, d: dict[str, Any] | None) -> "SolveOptions":
        d = dict(d or {})
        known = {k: v for k, v in d.items() if k in cls.__dataclass_fields__}
        return cls(**known)


def _blocked_index(ds: Dataset, bookings: list[Booking]):
    batch, fac, room = defaultdict(list), set(), set()
    for b in bookings:
        for s in b.slots:
            for bt in b.batches:
                batch[(bt, b.day, s)].append(b.basket_id)
            for f in b.faculty:
                if not ds.is_placeholder(f):
                    fac.add((f, b.day, s))
            room.add((b.room, b.day, s))
    return batch, fac, room


def solve(ds: Dataset, opts: SolveOptions | None = None) -> dict[str, Any]:
    opts = opts or SolveOptions()
    t0 = time.time()
    cfg = settings()["solver"]
    W = dict(cfg["weights"]) | opts.weights
    if opts.stability is not None:
        W["stability"] = opts.stability
    grid = TimeGrid.from_settings(use_extended=opts.use_extended)
    req_by_tok = {r.token: r for r in ds.requirements}

    # ------------------------------------------------------------ scope
    frozen_ids = {f["session_id"] for f in opts.frozen}
    scope: list[Requirement] = [r for r in ds.requirements
                                if (opts.semesters is None or r.semester in opts.semesters)
                                and (opts.tokens is None or r.token in opts.tokens)]
    basket_of = (lambda r: r.basket_id) if opts.use_baskets else (lambda r: None)

    fixed: list[Booking] = list(ds.fixed_bookings) if opts.include_fixed else []
    for f in opts.frozen:
        r = req_by_tok[f["token"]]
        fixed.append(Booking(day=f["day"], start=f["start"], duration=r.duration, batches=r.batches,
                             faculty=r.faculty, room=f["room"], course=r.course, kind=r.kind, origin="frozen",
                             session_id=f["session_id"], token=r.token, basket_id=basket_of(r)))
    fb_batch, fb_fac, fb_room = _blocked_index(ds, fixed)

    unavailable: set[tuple[str, str, int | None]] = set()
    for code, fac in ds.faculty.items():
        for d, s in fac.unavailable:
            unavailable.add(("faculty", code, d, s))
    for fb in opts.forbid:
        for kind in ("faculty", "batch", "room"):
            if fb.get(kind):
                unavailable.add((kind, fb[kind], fb["day"], fb.get("slot")))

    def is_unavailable(kind: str, code: str, day: str, slots) -> bool:
        if (kind, code, day, None) in unavailable:
            return True
        return any((kind, code, day, s) in unavailable for s in slots)

    pins_by_tok: dict[int, list[dict]] = defaultdict(list)
    for p in opts.pins:
        pins_by_tok[int(p["token"])].append(p)
    ref_by_tok: dict[int, list] = defaultdict(list)
    for rs in ds.reference:
        ref_by_tok[rs.token].append(rs)

    m = cp_model.CpModel()
    sessions: list[tuple[str, Requirement, int]] = []
    X: dict[str, list[tuple[cp_model.IntVar, str, int, str]]] = {}
    P: dict[str, cp_model.IntVar] = {}
    room_cov, fac_cov = defaultdict(list), defaultdict(list)
    batch_cov, basket_cov = defaultdict(list), defaultdict(list)
    basket_batches: dict[str, set[str]] = defaultdict(set)
    late_terms, sat_terms, ext_terms, room_terms, stab_terms = [], [], [], [], []
    unplaceable_reason: dict[str, str] = {}

    hint_room = {h["session_id"]: h["room"] for h in opts.hints}
    for r in scope:
        bk = basket_of(r)
        if bk:
            basket_batches[bk].update(r.batches)
        base_rooms = room_options(ds, r, opts.room_mode, opts.max_alt_rooms)
        pins = sorted(pins_by_tok.get(r.token, []), key=lambda p: grid.day_index(p["day"]))
        refs = sorted(ref_by_tok.get(r.token, []), key=lambda x: (grid.day_index(x.day), x.start))
        for k, sid in enumerate(session_ids(r)):
            if sid in frozen_ids:
                continue
            pin = pins[k] if k < len(pins) else None
            rooms = base_rooms + [hint_room[sid]] if hint_room.get(sid) and hint_room[sid] not in base_rooms else base_rooms
            cands = []
            for d in grid.days:
                if pin and d != pin["day"]:
                    continue
                for t in grid.starts_for(d, r.duration):
                    if pin and t != int(pin["start"]):
                        continue
                    slots = range(t, t + r.duration)
                    if any(is_unavailable("faculty", f, d, slots) for f in r.faculty if not ds.is_placeholder(f)):
                        continue
                    if any(is_unavailable("batch", b, d, slots) for b in r.batches):
                        continue
                    bad = False
                    for s in slots:
                        for b in r.batches:
                            for other_bk in fb_batch.get((b, d, s), []):
                                if not (bk and other_bk == bk):
                                    bad = True
                        if any((f, d, s) in fb_fac for f in r.faculty if not ds.is_placeholder(f)):
                            bad = True
                        if bad:
                            break
                    if bad:
                        continue
                    for room in rooms:
                        if pin and pin.get("room") and room != pin["room"]:
                            continue
                        if any((room, d, s) in fb_room for s in slots) or is_unavailable("room", room, d, slots):
                            continue
                        cands.append((d, t, room))
            sessions.append((sid, r, k))
            if not cands:
                unplaceable_reason[sid] = "no legal (day, slot, room) left after fixed bookings/pins/unavailability"
            xs = []
            for d, t, room in cands:
                v = m.NewBoolVar(f"x_{sid}_{d}{t}_{room}")
                xs.append((v, d, t, room))
                for s in range(t, t + r.duration):
                    room_cov[(room, d, s)].append(v)
                    for f in r.faculty:
                        if not ds.is_placeholder(f):
                            fac_cov[(f, d, s)].append(v)
                    for b in r.batches:
                        (basket_cov[(bk, b, d, s)] if bk else batch_cov[(b, d, s)]).append(v)
                    if s >= 7:
                        late_terms.append(v)
                    if grid.is_extended(s):
                        ext_terms.append(v)
                if d == "Sat":
                    sat_terms.append((v, r.duration))
                if room != r.venue:
                    room_terms.append(v)
            X[sid] = xs
            p = m.NewBoolVar(f"p_{sid}")
            P[sid] = p
            m.Add(sum(v for v, *_ in xs) == p)
            # stability: prefer the published (day, start) for the k-th instance
            if W.get("stability", 0) and k < len(refs):
                ref = refs[k]
                same = [v for v, d, t, _ in xs if d == ref.day and t == ref.start]
                stay = m.NewBoolVar(f"stay_{sid}")
                m.Add(sum(same) >= 1).OnlyEnforceIf(stay) if same else m.Add(stay == 0)
                stab_terms.append(stay)
            # hints
            if opts.hint_reference and k < len(refs):
                ref = refs[k]
                for v, d, t, room in xs:
                    if d == ref.day and t == ref.start and room == (ref.room if ref.room in [c[2] for c in cands] else r.venue):
                        m.AddHint(v, 1)
                        break

        # spread + symmetry breaking across instances of the same requirement
        inst = [sid for sid in session_ids(r) if sid in P]
        if opts.hints or opts.frozen:
            # repair mode: previous versions may have instance #1 before #0, so no ordering
            # symmetry-breaking (it would make the warm start infeasible) -- just one per day
            for d in grid.days:
                vs = [v for sid in inst for v, dd, *_ in X[sid] if dd == d]
                if len(vs) > 1:
                    m.AddAtMostOne(vs)
        else:
            for a, b in zip(inst, inst[1:]):
                m.AddImplication(P[b], P[a])
                day_a = sum(grid.day_index(d) * v for v, d, *_ in X[a])
                day_b = sum(grid.day_index(d) * v for v, d, *_ in X[b])
                m.Add(day_b >= day_a + 1).OnlyEnforceIf(P[b])
        # frozen instances of this requirement occupy their days
        frozen_days = [grid.day_index(f["day"]) for f in opts.frozen if f["token"] == r.token]
        for sid in inst:
            for fd in frozen_days:
                for v, d, *_ in X[sid]:
                    if grid.day_index(d) == fd:
                        m.Add(v == 0)

    keep_terms, room_keep_terms = [], []
    for h in opts.hints:
        xs_h = X.get(h["session_id"], [])
        for v, d, t, room in xs_h:
            if d == h["day"] and t == h["start"] and room == h["room"]:
                m.AddHint(v, 1)
        if opts.keep_weight and xs_h:
            same = [v for v, d, t, _ in xs_h if d == h["day"] and t == h["start"]]
            stay = m.NewBoolVar("")
            m.Add(sum(same) >= stay) if same else m.Add(stay == 0)
            keep_terms.append(stay)
            # also keep the room when possible (a room change is a change for students and staff too)
            same_room = [v for v, d, t, room in xs_h if d == h["day"] and t == h["start"] and room == h["room"]]
            stay_r = m.NewBoolVar("")
            m.Add(sum(same_room) >= stay_r) if same_room else m.Add(stay_r == 0)
            room_keep_terms.append(stay_r)

    # ------------------------------------------------------------ resource constraints
    for vs in room_cov.values():
        if len(vs) > 1:
            m.AddAtMostOne(vs)
    for vs in fac_cov.values():
        if len(vs) > 1:
            m.AddAtMostOne(vs)
    U: dict[tuple[str, str, int], cp_model.IntVar] = {}
    for (bk, b, d, s), vs in basket_cov.items():
        u = m.NewBoolVar(f"u_{bk}_{b}_{d}{s}")
        for v in vs:
            m.AddImplication(v, u)
        m.Add(u <= sum(vs))
        U[(bk, b, d, s)] = u
    baskets_of_batch: dict[str, list[str]] = defaultdict(list)
    for bk, bs in basket_batches.items():
        for b in bs:
            baskets_of_batch[b].append(bk)
    scope_batches = {b for r in scope for b in r.batches}
    occ: dict[tuple[str, str, int], list] = {}
    for b in scope_batches:
        for d in grid.days:
            for s in grid.slots_for(d):
                terms = list(batch_cov.get((b, d, s), [])) + [U[(bk, b, d, s)] for bk in baskets_of_batch.get(b, []) if (bk, b, d, s) in U]
                if len(terms) > 1:
                    m.Add(sum(terms) <= 1)
                occ[(b, d, s)] = terms

    # ------------------------------------------------------------ objective
    obj = []
    for sid, r, _ in sessions:
        obj.append(W["unplaced_hour"] * r.duration * (1 - P[sid]))
    n_soft = 0
    if opts.soft:
        dcap, fcap, fcons = cfg["daily_batch_cap"], cfg["faculty_daily_cap"], cfg["faculty_max_consecutive"]
        for b in scope_batches:
            for d in grid.days:
                slots = grid.slots_for(d)
                blocks = _blocks(slots, grid.lunch_slot)
                o = {s: sum(occ[(b, d, s)]) for s in slots}
                for blk in blocks:
                    for i in range(1, len(blk) - 1):                     # single-hour holes
                        g = m.NewBoolVar("")
                        m.Add(g >= o[blk[i - 1]] + o[blk[i + 1]] - o[blk[i]] - 1)
                        obj.append(W["batch_gap"] * g); n_soft += 1
                    for i in range(0, len(blk) - 3):                     # two-hour holes
                        g = m.NewBoolVar("")
                        m.Add(g >= o[blk[i]] + o[blk[i + 3]] - o[blk[i + 1]] - o[blk[i + 2]] - 1)
                        obj.append(2 * W["batch_gap"] * g); n_soft += 1
                ex = m.NewIntVar(0, len(slots), "")
                m.Add(ex >= sum(o.values()) - dcap)
                obj.append(W["batch_day_overload"] * ex); n_soft += 1
        fac_codes = {f for r in scope for f in r.faculty if not ds.is_placeholder(f)}
        for f in fac_codes:
            for d in grid.days:
                slots = grid.slots_for(d)
                of = {s: sum(fac_cov.get((f, d, s), [])) for s in slots}
                if not any(fac_cov.get((f, d, s)) for s in slots):
                    continue
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
        obj += [W["room_change"] * v for v in room_terms]
    obj += [W["extended_hour"] * v for v in ext_terms]
    if W.get("stability"):
        obj += [W["stability"] * (1 - s) for s in stab_terms]
    obj += [opts.keep_weight * (1 - s) for s in keep_terms]
    obj += [max(1, opts.keep_weight / 4) * (1 - s) for s in room_keep_terms]
    m.Minimize(sum(obj))

    build_s = time.time() - t0
    solver = cp_model.CpSolver()
    solver.parameters.max_time_in_seconds = float(opts.time_limit_s)
    solver.parameters.num_workers = int(opts.workers)
    solver.parameters.random_seed = int(opts.seed)
    solver.parameters.log_search_progress = bool(opts.log)
    status = solver.Solve(m)
    status_name = solver.StatusName(status)

    assignments, unplaced = [], []
    if status in (cp_model.OPTIMAL, cp_model.FEASIBLE):
        for sid, r, k in sessions:
            hit = next(((d, t, room) for v, d, t, room in X[sid] if solver.Value(v)), None)
            if hit:
                assignments.append({"session_id": sid, "token": r.token, "day": hit[0], "start": hit[1], "room": hit[2]})
            else:
                unplaced.append({"session_id": sid, "token": r.token, "code": r.code,
                                 "reason": unplaceable_reason.get(sid, "displaced by higher-value placements")})
    else:
        unplaced = [{"session_id": sid, "token": r.token, "code": r.code, "reason": status_name} for sid, r, _ in sessions]
    assignments.extend({**f, "origin": "frozen"} for f in opts.frozen)
    stats = m.Proto()
    return {
        "method": "cpsat",
        "status": status_name,
        "objective": solver.ObjectiveValue() if status in (cp_model.OPTIMAL, cp_model.FEASIBLE) else None,
        "best_bound": solver.BestObjectiveBound() if status in (cp_model.OPTIMAL, cp_model.FEASIBLE) else None,
        "assignments": assignments,
        "unplaced": unplaced,
        "scope_tokens": [r.token for r in scope],
        "stats": {"sessions": len(sessions), "frozen": len(opts.frozen), "bool_vars": len(stats.variables),
                  "constraints": len(stats.constraints), "soft_terms": n_soft, "build_s": round(build_s, 2),
                  "solve_s": round(solver.WallTime(), 2), "wall_s": round(time.time() - t0, 2)},
        "options": {k: v for k, v in opts.__dict__.items() if k not in ("hints", "frozen")},
    }


def _blocks(slots: list[int], lunch: int) -> list[list[int]]:
    out, cur = [], []
    for s in sorted(slots):
        if cur and (s != cur[-1] + 1 or s == lunch + 1):
            out.append(cur)
            cur = []
        cur.append(s)
    if cur:
        out.append(cur)
    return out
