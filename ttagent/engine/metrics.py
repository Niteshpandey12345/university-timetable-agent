"""Soft-quality metrics: the numbers used to compare timetables in the research study."""
from __future__ import annotations

import statistics
from collections import Counter, defaultdict
from typing import Any

from ttagent.config import settings
from ttagent.domain.models import Booking, Dataset
from ttagent.domain.timegrid import TimeGrid
from ttagent.engine.validator import annotate


def _runs(slots: list[int], lunch: int) -> list[int]:
    """Lengths of runs of consecutive occupied slots (lunch breaks a run)."""
    out, cur, prev = [], 0, None
    for s in sorted(slots):
        if prev is not None and s == prev + 1 and s != lunch + 1:
            cur += 1
        else:
            if cur:
                out.append(cur)
            cur = 1
        prev = s
    if cur:
        out.append(cur)
    return out


def compute_metrics(ds: Dataset, bookings: list[Booking], grid: TimeGrid | None = None) -> dict[str, Any]:
    grid = grid or TimeGrid.from_settings()
    scfg = settings()["solver"]
    bookings = [b for b in annotate(ds, bookings) if b.origin != "fixed"]
    internal = {b for b, bt in ds.batches.items() if bt.in_requirements}

    batch_occ: dict[tuple[str, str], set[int]] = defaultdict(set)
    fac_occ: dict[tuple[str, str], set[int]] = defaultdict(set)
    room_hours: Counter = Counter()
    day_hours: Counter = Counter()
    slot_hours: Counter = Counter()
    sat_h = late_h = ext_h = 0
    for b in bookings:
        for s in b.slots:
            for bt in b.batches:
                if bt in internal:
                    batch_occ[(bt, b.day)].add(s)
            for f in b.faculty:
                if not ds.is_placeholder(f):
                    fac_occ[(f, b.day)].add(s)
            room_hours[b.room] += 1
            day_hours[b.day] += 1
            slot_hours[s] += 1
            sat_h += b.day == "Sat"
            late_h += s >= 7
            ext_h += s >= 8

    teach = lambda d: set(grid.slots_for(d)) | set(grid.extended_slots)  # noqa: E731
    gaps = 0
    over_cap = 0
    spans = []
    for (bt, d), occ in batch_occ.items():
        lo, hi = min(occ), max(occ)
        gaps += sum(1 for s in range(lo, hi + 1) if s in teach(d) and s != grid.lunch_slot and s not in occ)
        spans.append(hi - lo + 1)
        over_cap += max(0, len(occ) - scfg["daily_batch_cap"])
    fac_consec = sum(max(0, r - scfg["faculty_max_consecutive"]) for (f, d), occ in fac_occ.items()
                     for r in _runs(list(occ), grid.lunch_slot))
    fac_over = sum(max(0, len(occ) - scfg["faculty_daily_cap"]) for occ in fac_occ.values())

    # faculty weekly load balance (std of weekly hours)
    fac_week: Counter = Counter()
    for (f, d), occ in fac_occ.items():
        fac_week[f] += len(occ)
    weekday_loads = [day_hours[d] for d in grid.days if d != "Sat"]
    cap_room = grid.capacity_hours()
    req_by_tok = {r.token: r for r in ds.requirements}
    room_changes = sum(1 for b in bookings if b.token in req_by_tok and b.room != req_by_tok[b.token].venue
                       and b.origin != "published")
    w = scfg["weights"]
    penalty = (w["batch_gap"] * gaps + w["batch_day_overload"] * over_cap + w["faculty_consecutive"] * fac_consec
               + w["faculty_day_overload"] * fac_over + w["saturday_hour"] * sat_h + w["late_hour"] * late_h
               + w["extended_hour"] * ext_h + w["room_change"] * room_changes)
    return {
        "batch_idle_gaps": gaps,
        "batch_gaps_per_batch_week": round(gaps / max(1, len(internal)), 2),
        "batch_avg_daily_span": round(statistics.mean(spans), 2) if spans else 0,
        "batch_day_overload_hours": over_cap,
        "faculty_consecutive_excess": fac_consec,
        "faculty_day_overload_hours": fac_over,
        "faculty_weekly_load_std": round(statistics.pstdev(list(fac_week.values())), 2) if fac_week else 0,
        "saturday_hours": sat_h,
        "late_hours_slot7plus": late_h,
        "extended_hours_slot8plus": ext_h,
        "room_changes": room_changes,
        "weekday_imbalance": round((max(weekday_loads) - min(weekday_loads)) / max(1, statistics.mean(weekday_loads)), 3) if weekday_loads else 0,
        "hours_by_day": {d: day_hours[d] for d in grid.days},
        "hours_by_slot": {s: slot_hours[s] for s in sorted(slot_hours)},
        "room_utilisation_pct": {r: round(100 * h / cap_room, 1) for r, h in room_hours.most_common()},
        "soft_penalty": penalty,
    }


def stability(ds: Dataset, assignments: list[dict]) -> dict[str, Any]:
    """How much of the published timetable a new solution keeps (minimal-disruption metric)."""
    ref = Counter((r.token, r.day, r.start) for r in ds.reference)
    got = Counter((a["token"], a["day"], a["start"]) for a in assignments)
    kept = sum(min(n, got[k]) for k, n in ref.items())
    total = sum(ref.values())
    return {"reference_sessions": total, "kept_in_place": kept,
            "kept_pct": round(100 * kept / total, 2) if total else 0.0}
