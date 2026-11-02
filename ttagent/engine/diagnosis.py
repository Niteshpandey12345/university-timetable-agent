"""
Diagnosis: *why* something cannot be scheduled.

1. capacity_analysis -- a pigeonhole proof. If a batch, teacher or room needs
   more hours than the week has, NO solver can succeed; the agent must ask for
   a policy change (electives in parallel, extra hours, another room) instead
   of retrying. This is what explains the 40 rows the VBA macro left unplaced.

2. explain_session -- for one unplaced requirement, list every legal start and
   who blocks it (batch / faculty / room), the free "safe havens", and rooms
   that would unblock it. A typed, structured superset of the workbook's
   'Conflict Report' sheet (VBA conflict_report + findSafeHaven).
"""
from __future__ import annotations

from collections import Counter, defaultdict
from typing import Any

from ttagent.domain.models import Booking, Dataset
from ttagent.domain.timegrid import TimeGrid


def batch_demand(ds: Dataset, basket_aware: bool = True) -> dict[str, dict[str, int]]:
    plain: Counter = Counter()
    basket_hours: dict[tuple[str, str], int] = {}
    for r in ds.requirements:
        for b in r.batches:
            if basket_aware and r.basket_id:
                k = (b, r.basket_id)
                basket_hours[k] = max(basket_hours.get(k, 0), r.weekly_hours)
            else:
                plain[b] += r.weekly_hours
    out: dict[str, dict[str, int]] = defaultdict(lambda: {"hours": 0, "basket_hours": 0})
    for b, h in plain.items():
        out[b]["hours"] += h
    for (b, _), h in basket_hours.items():
        out[b]["basket_hours"] += h
    return out


def capacity_analysis(ds: Dataset, grid: TimeGrid | None = None, basket_aware: bool = True) -> dict[str, Any]:
    grid = grid or TimeGrid.from_settings()
    cap = grid.capacity_hours()
    fixed_fac: Counter = Counter()
    fixed_room: Counter = Counter()
    for fb in ds.fixed_bookings:
        for f in fb.faculty:
            fixed_fac[f] += fb.duration
        fixed_room[fb.room] += fb.duration

    batches = []
    for b, d in sorted(batch_demand(ds, basket_aware).items()):
        need = d["hours"] + d["basket_hours"]
        batches.append({"batch": b, "demand_h": need, "capacity_h": cap, "slack_h": cap - need,
                        "utilisation_pct": round(100 * need / cap, 1), "infeasible": need > cap})
    fac_need: Counter = Counter()
    for r in ds.requirements:
        for f in r.faculty:
            fac_need[f] += r.weekly_hours
    faculty = []
    for f, h in fac_need.items():
        if ds.is_placeholder(f):
            continue
        need = h + fixed_fac[f]
        faculty.append({"faculty": f, "demand_h": need, "capacity_h": cap, "slack_h": cap - need,
                        "utilisation_pct": round(100 * need / cap, 1), "infeasible": need > cap})
    room_need: Counter = Counter()
    for r in ds.requirements:
        room_need[r.venue] += r.weekly_hours
    rooms = []   # requested venues: a *preference*, so over-subscription is reported, not "infeasible"
    for rid, h in room_need.items():
        need = h + fixed_room[rid]
        rooms.append({"room": rid, "demand_h": need, "capacity_h": cap, "slack_h": cap - need,
                      "utilisation_pct": round(100 * need / cap, 1), "infeasible": False,
                      "requested_venue_oversubscribed": need > cap})
    # room *pools* (any suitable room, by capacity threshold): this is the real pigeonhole test
    from ttagent.engine.decomposed import _pool, _room_cuts
    pools = []
    for name, c, pool_set, rids in _room_cuts(ds, set()):
        need = sum(r.weekly_hours for r in ds.requirements if _pool(ds, r) in pool_set and len(r.batches) >= c)
        if not need:
            continue
        free = len(rids) * cap - sum(fixed_room[x] for x in rids)
        pools.append({"pool": f"{name} (capacity>={c} batches)", "rooms": len(rids), "demand_h": need, "capacity_h": free,
                      "slack_h": free - need, "utilisation_pct": round(100 * need / free, 1) if free else 999.0,
                      "infeasible": need > free})
    pools.sort(key=lambda x: -x["utilisation_pct"])
    key = lambda x: -x["utilisation_pct"]  # noqa: E731
    batches.sort(key=key), faculty.sort(key=key), rooms.sort(key=key)
    infeasible = [x for x in batches + faculty + pools if x["infeasible"]]
    return {
        "weekly_capacity_h": cap,
        "basket_aware": basket_aware,
        "provably_infeasible": bool(infeasible),
        "infeasible_resources": infeasible,
        "batches_top": batches[:15],
        "faculty_top": faculty[:15],
        "room_pools_top": pools[:10],
        "requested_venues_oversubscribed": [x for x in rooms if x["requested_venue_oversubscribed"]],
        "note": "Requested venues are preferences; the solver may use any suitable room. Infeasibility is judged on room pools.",
    }


def _occupancy(ds: Dataset, bookings: list[Booking]):
    by_batch, by_fac, by_room = defaultdict(list), defaultdict(list), defaultdict(list)
    for b in bookings:
        for s in b.slots:
            for bt in b.batches:
                by_batch[(bt, b.day, s)].append(b)
            for f in b.faculty:
                if not ds.is_placeholder(f):
                    by_fac[(f, b.day, s)].append(b)
            by_room[(b.room, b.day, s)].append(b)
    return by_batch, by_fac, by_room


def explain_session(ds: Dataset, token: int, bookings: list[Booking], grid: TimeGrid | None = None,
                    max_rows: int = 40, forbid: list[dict] | None = None) -> dict[str, Any]:
    grid = grid or TimeGrid.from_settings()
    r = ds.req(token)
    own = [b for b in bookings if b.token == token]
    others = [b for b in bookings if b.token != token]
    by_batch, by_fac, by_room = _occupancy(ds, others)
    used_days = {b.day for b in own}
    rows, havens, room_fixable = [], [], []
    blockers: Counter = Counter()
    alt_rooms = [rm for rm in ds.rooms.values() if rm.room_id != r.venue
                 and rm.category == ds.rooms[r.venue].category
                 and rm.observed_batch_capacity >= len(r.batches)
                 and (ds.rooms[r.venue].lab_type is None or rm.lab_type == ds.rooms[r.venue].lab_type)]
    for day in grid.days:
        for start in grid.starts_for(day, r.duration):
            slots = range(start, start + r.duration)
            reasons = []
            for s in slots:
                for bt in r.batches:
                    for b in by_batch.get((bt, day, s), []):
                        if not (r.basket_id and b.basket_id == r.basket_id):
                            reasons.append(("batch", bt, b.code))
                for f in r.faculty:
                    for b in by_fac.get((f, day, s), []):
                        reasons.append(("faculty", f, b.code))
                for b in by_room.get((r.venue, day, s), []):
                    reasons.append(("room", r.venue, b.code))
            if day in used_days:
                reasons.append(("same_day", day, "requirement already meets this day"))
            for f in r.faculty:
                for d0, s0 in (ds.faculty[f].unavailable if f in ds.faculty else []):
                    if d0 == day and (s0 is None or s0 in slots):
                        reasons.append(("unavailable", f, "faculty unavailability"))
            for fb in forbid or []:
                hit = (fb.get("faculty") in r.faculty) or (fb.get("batch") in r.batches) or (fb.get("room") == r.venue)
                if hit and fb["day"] == day and (fb.get("slot") is None or fb["slot"] in slots):
                    reasons.append(("unavailable", fb.get("faculty") or fb.get("batch") or fb.get("room"), "what-if unavailability"))
            kinds = {k for k, *_ in reasons}
            for k in kinds:
                blockers[k] += 1
            label = grid.label(day, start, r.duration)
            if not reasons:
                havens.append(label)
            elif kinds == {"room"}:
                free = [rm.room_id for rm in alt_rooms
                        if all(not by_room.get((rm.room_id, day, s)) for s in slots)]
                if free:
                    room_fixable.append({"slot": label, "free_rooms": free[:6]})
            if len(rows) < max_rows:
                rows.append({"slot": label, "blocked_by": sorted({f"{k}:{w}" for k, w, _ in reasons}),
                             "examples": sorted({c for *_, c in reasons})[:3]})
    total = sum(len(grid.starts_for(d, r.duration)) for d in grid.days)
    return {
        "token": token, "requirement": r.code, "needs_h_per_week": r.weekly_hours,
        "placed_sessions": len(own), "required_sessions": r.per_week,
        "candidate_starts": total, "blocker_histogram": dict(blockers),
        "safe_havens": havens, "fixable_by_room_change": room_fixable[:10],
        "detail": rows,
    }
