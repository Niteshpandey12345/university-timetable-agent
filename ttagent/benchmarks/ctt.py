"""
ITC-2007 Track 3 curriculum-based course timetabling (CB-CTT): parser, UD2 scorer, best-known bounds.

UD2 objective (Di Gaspero, McCollum & Schaerf 2007; Bonutti et al. 2012):
  hard  H1 Lectures (all scheduled, distinct periods)  H2 RoomOccupancy
        H3 Conflicts (same curriculum or teacher)       H4 Availability
  soft  RoomCapacity      1 per student over capacity, per lecture
        MinWorkingDays    5 per day below the course minimum
        IsolatedLectures  2 per curriculum lecture with no adjacent lecture of that curriculum the same day
        RoomStability     1 per distinct room beyond the first, per course

The scorer is independent of both solvers (they are checked against it).
"""
from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import Path

from ttagent.config import ROOT

INSTANCE_DIR = ROOT / "data" / "benchmarks" / "itc2007"

# Best-known UD2 values: (lower bound, best upper bound). Source: Ceschia, Di Gaspero & Schaerf,
# arXiv:2201.07525 (Jan 2022), Table 13. Re-check on OptHub before publication.
BEST_KNOWN = {
    "comp01": (5, 5), "comp02": (24, 24), "comp03": (58, 64), "comp04": (35, 35), "comp05": (247, 284),
    "comp06": (27, 27), "comp07": (6, 6), "comp08": (37, 37), "comp09": (96, 96), "comp10": (4, 4),
    "comp11": (0, 0), "comp12": (248, 294), "comp13": (59, 59), "comp14": (51, 51), "comp15": (58, 64),
    "comp16": (18, 18), "comp17": (56, 56), "comp18": (61, 61), "comp19": (57, 57), "comp20": (4, 4),
    "comp21": (74, 74),
}
# Results under the ITC-2007 time limit (avg, best) reported in Ceschia et al. Table 13.
TIME_LIMITED = {
    "Abdullah & Turabieh 2012": {"comp01": (5.0, 5), "comp02": (36.36, 26), "comp03": (74.36, 70), "comp04": (38.45, 35),
                                 "comp05": (314.45, 295), "comp06": (45.27, 30), "comp07": (12.0, 7), "comp08": (40.82, 37),
                                 "comp09": (108.36, 102), "comp10": (8.36, 5), "comp12": (320.27, 315), "comp13": (64.27, 59),
                                 "comp14": (64.36, 61), "comp16": (23.73, 18), "comp17": (76.36, 60), "comp18": (75.64, 69),
                                 "comp19": (66.82, 57), "comp20": (13.45, 7), "comp21": (100.73, 86)},
    "Kiefer et al. 2017": {"comp01": (5.0, 5), "comp02": (41.5, 34), "comp03": (71.7, 68), "comp04": (35.1, 35),
                           "comp05": (305.2, 294), "comp06": (47.8, 41), "comp07": (14.5, 10), "comp08": (41.0, 39),
                           "comp09": (102.8, 100), "comp10": (14.3, 7), "comp12": (319.4, 306), "comp13": (60.7, 59),
                           "comp14": (54.1, 51), "comp16": (33.8, 26), "comp17": (75.7, 67), "comp18": (66.9, 64),
                           "comp19": (62.6, 59), "comp20": (27.2, 19), "comp21": (97.0, 93)},
}


@dataclass
class Course:
    cid: str
    teacher: str
    lectures: int
    min_days: int
    students: int


@dataclass
class Instance:
    name: str
    title: str
    days: int
    ppd: int
    courses: list[Course]
    rooms: list[tuple[str, int]]
    curricula: dict[str, list[str]]
    unavailable: set[tuple[str, int]] = field(default_factory=set)   # (course, period)

    @property
    def periods(self) -> int:
        return self.days * self.ppd

    def curricula_of(self) -> dict[str, list[str]]:
        out = defaultdict(list)
        for q, cs in self.curricula.items():
            for c in cs:
                out[c].append(q)
        return out

    def summary(self) -> dict:
        return {"instance": self.name, "courses": len(self.courses), "rooms": len(self.rooms), "days": self.days,
                "periods_per_day": self.ppd, "curricula": len(self.curricula),
                "lectures": sum(c.lectures for c in self.courses), "unavailability": len(self.unavailable)}


def load(name_or_path: str) -> Instance:
    p = Path(name_or_path)
    if not p.exists():
        p = INSTANCE_DIR / f"{name_or_path}.ctt"
    lines = [l.strip() for l in p.read_text().splitlines()]
    head = {}
    i = 0
    while lines[i] and ":" in lines[i] and not lines[i].endswith(":"):
        k, v = lines[i].split(":", 1)
        head[k.strip()] = v.strip()
        i += 1
    days, ppd = int(head["Days"]), int(head["Periods_per_day"])
    courses, rooms, curricula, unav = [], [], {}, set()
    section = None
    for l in lines[i:]:
        if not l:
            continue
        if l.endswith(":"):
            section = l[:-1]
            continue
        if l == "END.":
            break
        t = l.split()
        if section == "COURSES":
            courses.append(Course(t[0], t[1], int(t[2]), int(t[3]), int(t[4])))
        elif section == "ROOMS":
            rooms.append((t[0], int(t[1])))
        elif section == "CURRICULA":
            curricula[t[0]] = t[2:2 + int(t[1])]
        elif section == "UNAVAILABILITY_CONSTRAINTS":
            unav.add((t[0], int(t[1]) * ppd + int(t[2])))
    return Instance(p.stem, head.get("Name", p.stem), days, ppd, courses, rooms, curricula, unav)


def evaluate(inst: Instance, lectures: list[tuple[str, int, str]]) -> dict:
    """Score a solution given as (course, period, room) triples. Returns hard counts and UD2 soft cost."""
    cap = dict(inst.rooms)
    cmap = {c.cid: c for c in inst.courses}
    cur_of = inst.curricula_of()
    hard = Counter()
    per_course = Counter(c for c, _, _ in lectures)
    for c in inst.courses:
        if per_course[c.cid] != c.lectures:
            hard["H1_lectures"] += abs(c.lectures - per_course[c.cid])
    cp = Counter((c, p) for c, p, _ in lectures)
    hard["H1_same_period"] += sum(n - 1 for n in cp.values() if n > 1)
    rp = Counter((r, p) for _, p, r in lectures)
    hard["H2_room"] += sum(n - 1 for n in rp.values() if n > 1)
    tp = Counter((cmap[c].teacher, p) for c, p, _ in lectures)
    hard["H3_teacher"] += sum(n - 1 for n in tp.values() if n > 1)
    qp = Counter((q, p) for c, p, _ in lectures for q in cur_of.get(c, []))
    hard["H3_curriculum"] += sum(n - 1 for n in qp.values() if n > 1)
    hard["H4_availability"] += sum(1 for c, p, _ in lectures if (c, p) in inst.unavailable)
    soft = Counter()
    for c, p, r in lectures:
        soft["RoomCapacity"] += max(0, cmap[c].students - cap[r])
    days = defaultdict(set)
    rooms = defaultdict(set)
    for c, p, r in lectures:
        days[c].add(p // inst.ppd)
        rooms[c].add(r)
    for c in inst.courses:
        soft["MinWorkingDays"] += 5 * max(0, c.min_days - len(days[c.cid]))
        soft["RoomStability"] += max(0, len(rooms[c.cid]) - 1)
    occ = defaultdict(set)
    for (q, p) in qp:
        occ[q].add(p)
    for q, ps in occ.items():
        for p in ps:
            d, s = divmod(p, inst.ppd)
            if not ((s > 0 and p - 1 in ps) or (s < inst.ppd - 1 and p + 1 in ps)):
                soft["IsolatedLectures"] += 2 * qp[(q, p)]
    return {"hard": {k: v for k, v in hard.items() if v}, "hard_total": sum(hard.values()),
            "soft": dict(soft), "cost": sum(soft.values()), "feasible": sum(hard.values()) == 0}
