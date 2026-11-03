"""Memetic GA for ITC-2007 CB-CTT (UD2), same design as ttagent.engine.ga (direct encoding,
constructive seeding, course-level uniform crossover, mutation, conflict-directed local search,
incremental evaluation). Availability (H4) is enforced by the gene domain."""
from __future__ import annotations

import random
import time
from collections import Counter, defaultdict

from ttagent.benchmarks.ctt import Instance, evaluate

HW = 100_000


class _P:
    def __init__(self, inst: Instance):
        self.inst = inst
        self.cap = dict(inst.rooms)
        self.rooms = [r for r, _ in inst.rooms]
        self.course = {c.cid: c for c in inst.courses}
        self.cur_of = inst.curricula_of()
        self.lect = [c.cid for c in inst.courses for _ in range(c.lectures)]
        self.by_course = defaultdict(list)
        for j, c in enumerate(self.lect):
            self.by_course[c].append(j)
        self.cands = {}
        for c in inst.courses:
            self.cands[c.cid] = [(p, r) for p in range(inst.periods) if (c.cid, p) not in inst.unavailable for r in self.rooms]


class _S:
    __slots__ = ("P", "g", "rp", "cp", "tp", "qp", "cdays", "crooms", "hard", "soft")

    def __init__(self, P: _P, genes):
        self.P, self.g = P, list(genes)
        self.rp, self.cp, self.tp, self.qp = Counter(), Counter(), Counter(), Counter()
        self.cdays, self.crooms = defaultdict(Counter), defaultdict(Counter)
        for j, gene in enumerate(self.g):
            self._ap(j, gene, 1)
        self.hard, self.soft = self.full()

    def _ap(self, j, gene, s):
        P = self.P
        c = P.lect[j]
        p, r = gene
        self.rp[(r, p)] += s
        self.cp[(c, p)] += s
        self.tp[(P.course[c].teacher, p)] += s
        for q in P.cur_of.get(c, ()):
            self.qp[(q, p)] += s
        self.cdays[c][p // P.inst.ppd] += s
        if not self.cdays[c][p // P.inst.ppd]:
            del self.cdays[c][p // P.inst.ppd]
        self.crooms[c][r] += s
        if not self.crooms[c][r]:
            del self.crooms[c][r]

    def _iso_qd(self, q, d):
        ppd = self.P.inst.ppd
        occ = [self.qp[(q, d * ppd + s)] for s in range(ppd)]
        return sum(2 * occ[s] for s in range(ppd) if occ[s] and not ((s > 0 and occ[s - 1]) or (s < ppd - 1 and occ[s + 1])))

    def _course_soft(self, c):
        co = self.P.course[c]
        return 5 * max(0, co.min_days - len(self.cdays[c])) + max(0, len(self.crooms[c]) - 1)

    def full(self):
        h = sum(max(0, n - 1) for n in self.rp.values()) + sum(max(0, n - 1) for n in self.cp.values()) \
            + sum(max(0, n - 1) for n in self.tp.values()) + sum(max(0, n - 1) for n in self.qp.values())
        P = self.P
        s = sum(max(0, P.course[P.lect[j]].students - P.cap[r]) for j, (p, r) in enumerate(self.g))
        s += sum(self._course_soft(c) for c in P.course)
        s += sum(self._iso_qd(q, d) for q in P.inst.curricula for d in range(P.inst.days))
        return h, s

    def fit(self):
        return HW * self.hard + self.soft

    def _local(self, j, genes):
        P = self.P
        c = P.lect[j]
        t = P.course[c].teacher
        rps, cps, tps, qps, qds = set(), set(), set(), set(), set()
        for p, r in genes:
            rps.add((r, p)); cps.add((c, p)); tps.add((t, p))
            for q in P.cur_of.get(c, ()):
                qps.add((q, p)); qds.add((q, p // P.inst.ppd))
        def score():
            h = sum(max(0, self.rp[k] - 1) for k in rps) + sum(max(0, self.cp[k] - 1) for k in cps) \
                + sum(max(0, self.tp[k] - 1) for k in tps) + sum(max(0, self.qp[k] - 1) for k in qps)
            s = self._course_soft(c) + sum(self._iso_qd(q, d) for q, d in qds)
            return h, s
        return score

    def delta(self, j, new):
        old = self.g[j]
        P = self.P
        sc = self._local(j, [old, new])
        h0, s0 = sc()
        s0 += max(0, P.course[P.lect[j]].students - P.cap[old[1]])
        self._ap(j, old, -1); self._ap(j, new, 1)
        h1, s1 = sc()
        s1 += max(0, P.course[P.lect[j]].students - P.cap[new[1]])
        self._ap(j, new, -1); self._ap(j, old, 1)
        return h1 - h0, s1 - s0

    def setg(self, j, new, dh, ds):
        self._ap(j, self.g[j], -1); self._ap(j, new, 1)
        self.g[j] = new
        self.hard += dh; self.soft += ds

    def conflicted(self):
        P = self.P
        out = []
        for j, (p, r) in enumerate(self.g):
            c = P.lect[j]
            if self.rp[(r, p)] > 1 or self.cp[(c, p)] > 1 or self.tp[(P.course[c].teacher, p)] > 1 \
                    or any(self.qp[(q, p)] > 1 for q in P.cur_of.get(c, ())):
                out.append(j)
        return out


def _construct(P, rng):
    order = sorted(range(len(P.lect)), key=lambda j: (len(P.cands[P.lect[j]]) + rng.random() * 30))
    genes = [rng.choice(P.cands[P.lect[j]]) for j in range(len(P.lect))]
    st = _S(P, genes)
    for j in order:
        cs = P.cands[P.lect[j]]
        cs = cs if len(cs) <= 120 else rng.sample(cs, 120)
        best = min(((HW * dh + ds + rng.random(), c, dh, ds) for c in cs for dh, ds in [st.delta(j, c)]))
        if best[0] < 0:
            st.setg(j, best[1], best[2], best[3])
    return st


def _swap_delta(st, j, k):
    """Delta of swapping the (period, room) genes of lectures j and k."""
    gj, gk = st.g[j], st.g[k]
    h0, s0 = st.hard, st.soft
    dh1, ds1 = st.delta(j, gk)
    st.setg(j, gk, dh1, ds1)
    dh2, ds2 = st.delta(k, gj)
    st.setg(j, gj, -dh1, -ds1)
    return dh1 + dh2, ds1 + ds2


def _ls(st, rng, iters, sample):
    P = st.P
    n = len(P.lect)
    for it in range(iters):
        conf = st.conflicted() if st.hard else []
        if conf:
            j = rng.choice(conf)
        elif rng.random() < 0.5:
            # soft-targeted: lectures in too-small rooms or of courses with soft cost
            bad = [i for i, (p, r) in enumerate(st.g) if P.course[P.lect[i]].students > P.cap[r]]
            j = rng.choice(bad) if bad else rng.randrange(n)
        else:
            j = rng.randrange(n)
        cs = P.cands[P.lect[j]]
        cs = cs if len(cs) <= sample else rng.sample(cs, sample)
        best = None
        for c in cs:
            if c == st.g[j]:
                continue
            dh, ds = st.delta(j, c)
            sc = HW * dh + ds
            if best is None or sc < best[0]:
                best = (sc, "move", c, dh, ds)
        # room swaps inside the same period (fixes capacity / stability without new conflicts)
        p = st.g[j][0]
        mates = [k for k in range(n) if k != j and st.g[k][0] == p]
        for k in rng.sample(mates, min(len(mates), 12)):
            if (st.g[k][0], st.g[j][1]) not in [(q, r) for q, r in P.cands[P.lect[k]]] and False:
                continue
            dh, ds = _swap_delta(st, j, k)
            sc = HW * dh + ds
            if best is None or sc < best[0]:
                best = (sc, "swap", k, dh, ds)
        if best and best[0] <= 0:
            if best[1] == "move":
                st.setg(j, best[2], best[3], best[4])
            else:
                k = best[2]
                gj, gk = st.g[j], st.g[k]
                dh1, ds1 = st.delta(j, gk)
                st.setg(j, gk, dh1, ds1)
                dh2, ds2 = st.delta(k, gj)
                st.setg(k, gj, dh2, ds2)


def solve_ctt_ga(inst: Instance, time_limit_s: float = 120, seed: int = 7, population: int = 16,
                 local_search: bool = True, ls_iters: int = 400, ls_sample: int = 60) -> dict:
    t0 = time.time()
    rng = random.Random(seed)
    P = _P(inst)
    deadline = t0 + time_limit_s
    pop = []
    seed_deadline = t0 + 0.3 * time_limit_s           # seeding may use at most 30% of the budget
    while len(pop) < population and (time.time() < seed_deadline or len(pop) < 2):
        st = _construct(P, rng)
        if local_search:
            _ls(st, rng, ls_iters, ls_sample)
        pop.append(st)
    gens, hist = 0, []
    while time.time() < deadline:
        gens += 1
        a = min(rng.sample(pop, min(3, len(pop))), key=_S.fit)
        b = min(rng.sample(pop, min(3, len(pop))), key=_S.fit)
        genes = list(a.g)
        for c, js in P.by_course.items():
            if rng.random() < 0.5:
                for j in js:
                    genes[j] = b.g[j]
            if rng.random() < 0.03:
                j = rng.choice(js)
                genes[j] = rng.choice(P.cands[c])
        ch = _S(P, genes)
        if local_search:
            _ls(ch, rng, ls_iters, ls_sample)
        worst = max(pop, key=_S.fit)
        if ch.fit() < worst.fit():
            pop.remove(worst)
            pop.append(ch)
        if gens % 10 == 0:
            b = min(pop, key=_S.fit)
            hist.append((round(time.time() - t0, 1), b.hard, b.soft))
    best = min(pop, key=_S.fit)
    lectures = [(P.lect[j], p, r) for j, (p, r) in enumerate(best.g)]
    ev = evaluate(inst, lectures)
    return {"method": "ga_memetic" if local_search else "ga_plain", "instance": inst.name, "status": "HEURISTIC",
            "cost": ev["cost"], "feasible": ev["feasible"], "hard_total": ev["hard_total"], "soft": ev["soft"],
            "internal": (best.hard, best.soft), "generations": gens, "seconds": round(time.time() - t0, 1),
            "history": hist, "lectures": lectures}
