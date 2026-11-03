"""
Genetic-algorithm allocator (memetic GA) for the institutional timetable.

Designed as a *strong* baseline, following the timetabling-GA literature (Lewis &
Paechter 2007; Rossi-Doria et al. 2002; Abdullah et al. 2007): plain GAs are weak
on highly constrained timetables, so this one uses

* direct encoding      gene = (day, start, room) per weekly session, drawn only from
                       *legal* candidates (lunch, block contiguity, Saturday cut-off,
                       fixed external bookings and unavailability already filtered);
* constructive seeding randomised most-constrained-first greedy, so every
                       individual starts near-feasible;
* crossover            requirement-level uniform crossover (all sessions of one
                       requirement come from the same parent -- keeps day spreads);
* mutation             re-sample a gene of a conflicting session;
* local search         conflict-directed best-improvement moves (the "memetic" part;
                       can be switched off for the plain-GA ablation);
* selection            tournament, steady-state replacement with elitism and
                       duplicate rejection.

Fitness = 10 000 * hard_violations + soft_penalty, evaluated *incrementally* with
occupancy counters, so a move costs O(cells touched) instead of a full re-score.
Hard semantics are identical to the validator (per-batch elective baskets,
placeholder faculty exempt). The final best individual is made conflict-free by
un-placing the sessions involved in remaining clashes, so its output is directly
comparable with the CP-SAT methods (coverage + 0 hard violations).
"""
from __future__ import annotations

import random
import time
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from typing import Any

from ttagent.config import settings
from ttagent.domain.models import Dataset, Requirement, session_ids
from ttagent.domain.rooms import room_options
from ttagent.domain.timegrid import TimeGrid

HARD_W = 10_000


@dataclass
class GAOptions:
    time_limit_s: float = 60
    seed: int = 7
    population: int = 24
    tournament: int = 3
    crossover_rate: float = 0.85
    mutation_rate: float = 0.03       # per requirement
    local_search: bool = True         # False => plain GA (ablation)
    ls_iters: int = 120
    ls_sample: int = 60               # candidates evaluated per LS move
    max_alt_rooms: int = 6
    semesters: list[int] | None = None
    tokens: list[int] | None = None
    use_baskets: bool = True
    log_every_s: float = 5.0
    trace: list = field(default_factory=list)


class _Problem:
    """Static data: sessions, candidate genes and the cells each gene occupies."""

    def __init__(self, ds: Dataset, opts: GAOptions) -> None:
        self.ds = ds
        self.grid = grid = TimeGrid.from_settings()
        cfg = settings()["solver"]
        self.W = cfg["weights"]
        self.dcap = cfg["daily_batch_cap"]
        scope = [r for r in ds.requirements
                 if (opts.semesters is None or r.semester in opts.semesters)
                 and (opts.tokens is None or r.token in opts.tokens)]
        self.scope = scope
        bk = (lambda r: r.basket_id) if opts.use_baskets else (lambda r: None)
        # fixed external bookings -> base occupancy
        self.fixed_room, self.fixed_fac, self.fixed_batch = Counter(), Counter(), defaultdict(Counter)
        for b in ds.fixed_bookings:
            for s in b.slots:
                self.fixed_room[(b.room, b.day, s)] += 1
                for f in b.faculty:
                    if not ds.is_placeholder(f):
                        self.fixed_fac[(f, b.day, s)] += 1
                for bt in b.batches:
                    self.fixed_batch[(bt, b.day, s)][("fixed", b.code)] += 1
        unav = set()
        for code, fac in ds.faculty.items():
            for d, s in fac.unavailable:
                unav.add((code, d, s))
        self.sessions: list[tuple[str, Requirement, str]] = []     # (sid, req, groupkey)
        self.cands: list[list[tuple[str, int, str]]] = []
        self.req_sessions: dict[int, list[int]] = defaultdict(list)
        for r in scope:
            rooms = room_options(ds, r, "flex", opts.max_alt_rooms)
            cand = []
            for d in grid.days:
                for t in grid.starts_for(d, r.duration):
                    slots = range(t, t + r.duration)
                    if any((f, d, None) in unav or any((f, d, s) in unav for s in slots) for f in r.faculty):
                        continue
                    if any(self.fixed_fac.get((f, d, s)) for f in r.faculty if not ds.is_placeholder(f) for s in slots):
                        continue
                    if any(self.fixed_batch.get((b, d, s)) for b in r.batches for s in slots):
                        continue
                    for room in rooms:
                        if not any(self.fixed_room.get((room, d, s)) for s in slots):
                            cand.append((d, t, room))
            if not cand:
                cand = [(d, t, r.venue) for d in grid.days for t in grid.starts_for(d, r.duration)]
            for k, sid in enumerate(session_ids(r)):
                gkey = bk(r) or sid
                self.req_sessions[r.token].append(len(self.sessions))
                self.sessions.append((sid, r, gkey))
                self.cands.append(cand)
        self.n = len(self.sessions)
        self.real_fac = [[f for f in r.faculty if not ds.is_placeholder(f)] for _, r, _ in self.sessions]
        self.internal_batches = {b for r in scope for b in r.batches}
        self.has_pref = any(f.avoid for f in ds.faculty.values())

    def cells(self, i: int, gene: tuple[str, int, str]):
        sid, r, g = self.sessions[i]
        d, t, room = gene
        return d, range(t, t + r.duration), room, r, g


class _State:
    """One individual with incremental occupancy and cost."""

    __slots__ = ("P", "genes", "room", "fac", "batch", "reqday", "bday", "hard", "soft")

    def __init__(self, P: _Problem, genes: list) -> None:
        self.P = P
        self.genes = list(genes)
        self.room: Counter = Counter()
        self.fac: Counter = Counter()
        self.batch: dict = defaultdict(Counter)
        self.reqday: Counter = Counter()
        self.bday: dict = defaultdict(set)
        self.hard = 0
        self.soft = 0
        for i, g in enumerate(self.genes):
            if g is not None:
                self._apply(i, g, +1)
        self.hard, self.soft = self._full_cost()

    # -------------------------------------------------- bookkeeping
    def _apply(self, i: int, gene, sign: int) -> None:
        P = self.P
        d, slots, room, r, gkey = P.cells(i, gene)
        for s in slots:
            self.room[(room, d, s)] += sign
            for f in P.real_fac[i]:
                self.fac[(f, d, s)] += sign
            for b in r.batches:
                c = self.batch[(b, d, s)]
                c[gkey] += sign
                if c[gkey] == 0:
                    del c[gkey]
                if b in P.internal_batches:
                    if c:
                        self.bday[(b, d)].add(s)
                    else:
                        self.bday[(b, d)].discard(s)
        self.reqday[(r.token, d)] += sign

    def _batch_conf(self, key) -> int:
        groups = len(self.batch.get(key, ())) + (len(self.P.fixed_batch.get(key, ())) if key in self.P.fixed_batch else 0)
        return max(0, groups - 1)

    def _local_hard(self, i: int, gene) -> int:
        P = self.P
        d, slots, room, r, _ = P.cells(i, gene)
        h = 0
        for s in slots:
            h += max(0, self.room[(room, d, s)] + P.fixed_room.get((room, d, s), 0) - 1)
            for f in P.real_fac[i]:
                h += max(0, self.fac[(f, d, s)] + P.fixed_fac.get((f, d, s), 0) - 1)
            for b in r.batches:
                h += self._batch_conf((b, d, s))
        h += max(0, self.reqday[(r.token, d)] - 1)
        return h

    def _gene_soft(self, i: int, gene) -> int:
        P = self.P
        d, slots, room, r, _ = P.cells(i, gene)
        W = P.W
        v = W["saturday_hour"] * r.duration * (d == "Sat") + W["late_hour"] * sum(1 for s in slots if s >= 7)
        if room != r.venue:
            v += W["room_change"]
        if P.has_pref:
            from ttagent.engine.decomposed import _pref_hours
            v += W.get("faculty_preference", 5) * _pref_hours(P.ds, r, d, slots.start)
        return v

    def _bday_soft(self, b: str, d: str) -> int:
        occ = self.bday.get((b, d))
        if not occ:
            return 0
        P = self.P
        lo, hi = min(occ), max(occ)
        teach = P.grid.slots_for(d)
        gaps = sum(1 for s in teach if lo < s < hi and s not in occ)
        return P.W["batch_gap"] * gaps + P.W["batch_day_overload"] * max(0, len(occ) - P.dcap)

    def _full_cost(self) -> tuple[int, int]:
        P = self.P
        h = 0
        for k, c in self.room.items():
            h += max(0, c + P.fixed_room.get(k, 0) - 1)
        for k, c in self.fac.items():
            h += max(0, c + P.fixed_fac.get(k, 0) - 1)
        keys = set(self.batch) | {k for k in P.fixed_batch if k in self.batch}
        for k in keys:
            h += self._batch_conf(k)
        for k, c in self.reqday.items():
            h += max(0, c - 1)
        s = sum(self._gene_soft(i, g) for i, g in enumerate(self.genes) if g is not None)
        s += sum(self._bday_soft(b, d) for (b, d) in list(self.bday))
        return h, s

    @property
    def fitness(self) -> int:
        return HARD_W * self.hard + self.soft

    # -------------------------------------------------- moves
    def _touched_bdays(self, i: int, genes) -> set:
        r = self.P.sessions[i][1]
        return {(b, g[0]) for g in genes if g is not None for b in r.batches if b in self.P.internal_batches}

    def move_delta(self, i: int, new) -> tuple[int, int]:
        """(delta_hard, delta_soft) of setting gene i to `new` (applied then reverted)."""
        old = self.genes[i]
        bd = self._touched_bdays(i, [old, new])
        hard_cells = self._hard_cells(i, [old, new])
        h0 = self._cells_hard(hard_cells)
        s0 = sum(self._bday_soft(b, d) for b, d in bd) + (self._gene_soft(i, old) if old else 0)
        if old is not None:
            self._apply(i, old, -1)
        self._apply(i, new, +1)
        h1 = self._cells_hard(hard_cells)
        s1 = sum(self._bday_soft(b, d) for b, d in bd) + self._gene_soft(i, new)
        self._apply(i, new, -1)
        if old is not None:
            self._apply(i, old, +1)
        return h1 - h0, s1 - s0

    def _hard_cells(self, i: int, genes) -> tuple:
        P = self.P
        r = P.sessions[i][1]
        rooms, facs, bats, reqd = set(), set(), set(), set()
        for g in genes:
            if g is None:
                continue
            d, slots, room, _, _ = P.cells(i, g)
            for s in slots:
                rooms.add((room, d, s))
                for f in P.real_fac[i]:
                    facs.add((f, d, s))
                for b in r.batches:
                    bats.add((b, d, s))
            reqd.add((r.token, d))
        return rooms, facs, bats, reqd

    def _cells_hard(self, cells) -> int:
        P = self.P
        rooms, facs, bats, reqd = cells
        return (sum(max(0, self.room[k] + P.fixed_room.get(k, 0) - 1) for k in rooms)
                + sum(max(0, self.fac[k] + P.fixed_fac.get(k, 0) - 1) for k in facs)
                + sum(self._batch_conf(k) for k in bats)
                + sum(max(0, self.reqday[k] - 1) for k in reqd))

    def set_gene(self, i: int, new, dh: int, dsoft: int) -> None:
        old = self.genes[i]
        if old is not None:
            self._apply(i, old, -1)
        self._apply(i, new, +1)
        self.genes[i] = new
        self.hard += dh
        self.soft += dsoft

    def conflicted(self) -> list[int]:
        return [i for i, g in enumerate(self.genes) if g is not None and self._local_hard(i, g) > 0]


# ====================================================================== GA
def _construct(P: _Problem, rng: random.Random) -> list:
    order = sorted(range(P.n), key=lambda i: (len(P.cands[i]) + rng.random() * 40, -len(P.sessions[i][1].batches)))
    st = _State(P, [None] * P.n)
    for i in order:
        cands = P.cands[i] if len(P.cands[i]) <= 80 else rng.sample(P.cands[i], 80)
        best, bk = None, None
        for c in cands:
            dh, ds_ = st.move_delta(i, c)
            key = (dh, ds_ + rng.random())
            if bk is None or key < bk:
                best, bk = (c, dh, ds_), key
        st.set_gene(i, best[0], best[1], best[2])
    return st.genes


def _local_search(st: _State, rng: random.Random, iters: int, sample: int) -> None:
    P = st.P
    for _ in range(iters):
        conf = st.conflicted()
        if conf:
            i = rng.choice(conf)
        else:
            i = rng.randrange(P.n)          # soft improvement once feasible
        cands = P.cands[i] if len(P.cands[i]) <= sample else rng.sample(P.cands[i], sample)
        best = None
        for c in cands:
            if c == st.genes[i]:
                continue
            dh, ds_ = st.move_delta(i, c)
            score = HARD_W * dh + ds_
            if best is None or score < best[0]:
                best = (score, c, dh, ds_)
        if best and best[0] <= 0:
            st.set_gene(i, best[1], best[2], best[3])


def solve_ga(ds: Dataset, opts: GAOptions | None = None) -> dict[str, Any]:
    opts = opts or GAOptions()
    t0 = time.time()
    rng = random.Random(opts.seed)
    P = _Problem(ds, opts)
    deadline = t0 + opts.time_limit_s
    pop: list[_State] = []
    seed_deadline = t0 + 0.3 * opts.time_limit_s     # seeding may use at most 30% of the budget
    while len(pop) < opts.population and (time.time() < seed_deadline or len(pop) < 2):
        st = _State(P, _construct(P, rng))
        if opts.local_search:
            _local_search(st, rng, opts.ls_iters, opts.ls_sample)
        pop.append(st)
    pop.sort(key=lambda s: s.fitness)
    history = [{"t": round(time.time() - t0, 2), "gen": 0, "best_hard": pop[0].hard, "best_soft": pop[0].soft}]
    gen, last_log = 0, time.time()
    seen = {tuple(s.genes) for s in pop}
    while time.time() < deadline:
        gen += 1
        def tour():
            return min(rng.sample(pop, min(opts.tournament, len(pop))), key=lambda s: s.fitness)
        a, b = tour(), tour()
        genes = list(a.genes)
        if rng.random() < opts.crossover_rate:
            for tok, idxs in P.req_sessions.items():
                if rng.random() < 0.5:
                    for i in idxs:
                        genes[i] = b.genes[i]
        for tok, idxs in P.req_sessions.items():
            if rng.random() < opts.mutation_rate:
                i = rng.choice(idxs)
                genes[i] = rng.choice(P.cands[i])
        child = _State(P, genes)
        if opts.local_search:
            _local_search(child, rng, opts.ls_iters, opts.ls_sample)
        key = tuple(child.genes)
        if key in seen:
            continue
        worst = max(pop, key=lambda s: s.fitness)
        if child.fitness < worst.fitness:
            pop.remove(worst)
            pop.append(child)
            seen.add(key)
        best = min(pop, key=lambda s: s.fitness)
        if time.time() - last_log >= opts.log_every_s:
            history.append({"t": round(time.time() - t0, 2), "gen": gen, "best_hard": best.hard, "best_soft": best.soft})
            last_log = time.time()
    best = min(pop, key=lambda s: s.fitness)
    history.append({"t": round(time.time() - t0, 2), "gen": gen, "best_hard": best.hard, "best_soft": best.soft})

    # repair to a clash-free timetable: un-place the most conflicted sessions until hard = 0
    st = _State(P, best.genes)
    removed = []
    while True:
        conf = st.conflicted()
        if not conf:
            break
        worst_i = max(conf, key=lambda i: (st._local_hard(i, st.genes[i]), P.sessions[i][1].duration))
        st._apply(worst_i, st.genes[worst_i], -1)
        st.genes[worst_i] = None
        removed.append(worst_i)
    assignments, unplaced = [], []
    for i, g in enumerate(st.genes):
        sid, r, _ = P.sessions[i]
        if g is None:
            unplaced.append({"session_id": sid, "token": r.token, "code": r.code, "reason": "GA: in conflict in best individual"})
        else:
            assignments.append({"session_id": sid, "token": r.token, "day": g[0], "start": g[1], "room": g[2]})
    return {"method": "ga_memetic" if opts.local_search else "ga_plain", "status": "HEURISTIC",
            "objective": best.fitness, "best_bound": None, "assignments": assignments, "unplaced": unplaced,
            "scope_tokens": [r.token for r in P.scope],
            "stats": {"generations": gen, "population": opts.population, "best_hard_before_repair": best.hard,
                      "best_soft": best.soft, "removed_for_feasibility": len(removed), "history": history,
                      "wall_s": round(time.time() - t0, 2)},
            "options": {"time_limit_s": opts.time_limit_s, "seed": opts.seed, "local_search": opts.local_search,
                        "population": opts.population, "use_extended": False}}
