"""
Independent timetable validator.

Deliberately shares *no* code with the CP-SAT model: it re-derives every
hard rule from first principles over plain `Booking` objects, so it can
judge any timetable -- solver output, the VBA-greedy baseline, or the
hand-edited grid in the workbook. A solver bug cannot hide behind a
validator that reuses its own logic.

Hard rules (H1-H9):
  H1 batch clash        a batch in two places at once (parallel electives of the
                        same basket are exempt)
  H2 faculty clash      a real faculty member in two places (placeholders exempt)
  H3 room clash         two sessions in one room
  H4 lunch              anything in the lunch slot
  H5 outside hours      outside teaching slots / Saturday after 1 PM
  H6 block integrity    a P/L block not contiguous or wrong length
  H7 same-day repeat    two sessions of the same requirement on one day
  H8 room suitability   moved to a room of the wrong type or too small
  H9 faculty availability
plus allocation (under/over-scheduled weekly hours).
"""
from __future__ import annotations

import itertools
from collections import Counter, defaultdict
from typing import Any, Iterable

from ttagent.domain.models import Booking, Dataset, Requirement
from ttagent.domain.rooms import room_suitable
from ttagent.domain.timegrid import TimeGrid


def booking_key(b: Booking) -> tuple[str, str, str]:
    return (",".join(b.batches), b.kind.value, b.course)


def annotate(ds: Dataset, bookings: Iterable[Booking]) -> list[Booking]:
    """Attach requirement token + basket id to bookings that lack them (e.g. published cells)."""
    by_key = {r.key: r for r in ds.requirements}
    out = []
    for b in bookings:
        if b.token is None and b.origin != "fixed":
            r = by_key.get(booking_key(b))
            if r is not None:
                b = b.model_copy(update={"token": r.token, "basket_id": b.basket_id or r.basket_id})
            elif b.basket_id is None:
                bk = _basket_for(ds, b)
                if bk:
                    b = b.model_copy(update={"basket_id": bk})
        out.append(b)
    return out


def _basket_for(ds: Dataset, b: Booking) -> str | None:
    from ttagent.ingestion.token_parser import batch_parts
    sem = batch_parts(b.batches[0])[0]
    for bk in ds.baskets:
        if bk.semester == sem and b.course in bk.courses and b.kind.value == "L":
            return bk.basket_id
    return None


def _real_faculty(ds: Dataset, b: Booking) -> set[str]:
    return {f for f in b.faculty if not ds.is_placeholder(f)}


def validate(ds: Dataset, bookings: list[Booking], grid: TimeGrid | None = None, *,
             scope_tokens: set[int] | None = None, check_allocation: bool = True,
             max_examples: int = 400) -> dict[str, Any]:
    grid = grid or TimeGrid.from_settings()
    bookings = annotate(ds, bookings)
    violations: list[dict[str, Any]] = []
    counts: Counter = Counter()

    def add(rule: str, msg: str, **kw: Any) -> None:
        counts[rule] += 1
        if len(violations) < max_examples:
            violations.append({"rule": rule, "message": msg, **kw})

    # ---- H4/H5/H9 slot legality ------------------------------------------
    for b in bookings:
        legal = set(grid.slots_for(b.day))
        for s in b.slots:
            if b.origin == "fixed" and (s == grid.lunch_slot or s not in legal):
                add("X_external_outside_hours", f"external booking {b.code} at {b.day} slot {s}", day=b.day, slot=s)
            elif s == grid.lunch_slot:
                add("H4_lunch", f"{b.code} placed in lunch slot on {b.day}", day=b.day, slot=s, origin=b.origin)
            elif s not in legal:
                rule = "H5_saturday_cutoff" if b.day == "Sat" else "H5_outside_hours"
                add(rule, f"{b.code} at {b.day} slot {s} is outside teaching hours", day=b.day, slot=s, origin=b.origin)
        for f in _real_faculty(ds, b):
            fac = ds.faculty.get(f)
            if fac:
                for d, s in fac.unavailable:
                    if d == b.day and (s is None or s in b.slots):
                        add("H9_faculty_unavailable", f"{f} is unavailable {d}{':' + str(s) if s else ''} but teaches {b.code}",
                            day=b.day, faculty=f)

    # ---- H1-H3 clashes per (day, slot) -----------------------------------
    cell: dict[tuple[str, int], list[Booking]] = defaultdict(list)
    for b in bookings:
        for s in b.slots:
            cell[(b.day, s)].append(b)
    for (day, slot), items in cell.items():
        for a, b in itertools.combinations(items, 2):
            if a.origin == b.origin == "fixed":
                if (set(a.batches) & set(b.batches)) or a.room == b.room:
                    add("X_external_clash", f"external bookings clash at {day} slot {slot}: {a.code} | {b.code}", day=day, slot=slot)
                continue
            if a.code == b.code and a.origin == b.origin == "published":
                add("D_duplicate_entry", f"{a.code} entered twice at {day} slot {slot}", day=day, slot=slot)
                continue
            shared = set(a.batches) & set(b.batches)
            if shared and not (a.basket_id and a.basket_id == b.basket_id):
                add("H1_batch_clash", f"batch {','.join(sorted(shared))} has {a.course}({a.kind.value}) and {b.course}({b.kind.value}) at {day} slot {slot}",
                    day=day, slot=slot, a=a.code, b=b.code)
            fshared = _real_faculty(ds, a) & _real_faculty(ds, b)
            if fshared:
                add("H2_faculty_clash", f"faculty {','.join(sorted(fshared))} double-booked at {day} slot {slot}: {a.code} | {b.code}",
                    day=day, slot=slot, a=a.code, b=b.code)
            if a.room == b.room:
                add("H3_room_clash", f"room {a.room} double-booked at {day} slot {slot}: {a.code} | {b.code}",
                    day=day, slot=slot, a=a.code, b=b.code)

    # ---- H6/H7/H8 requirement-level checks --------------------------------
    hours: Counter = Counter()
    per_day: Counter = Counter()
    req_by_tok = {r.token: r for r in ds.requirements}
    for b in bookings:
        if b.token is None:
            continue
        r = req_by_tok.get(b.token)
        if r is None:
            continue
        hours[b.token] += b.duration
        if b.origin != "published":
            per_day[(b.token, b.day)] += 1
            if b.duration != r.duration:
                add("H6_block_integrity", f"{r.code} placed as {b.duration}h block, needs {r.duration}h", token=b.token)
            if b.room != r.venue:
                room = ds.rooms.get(b.room)
                if room is None or not room_suitable(ds, r, room):
                    add("H8_room_unsuitable", f"{r.code} moved to unsuitable room {b.room}", token=b.token)
    for (tok, day), n in per_day.items():
        if n > 1:
            add("H7_same_day_repeat", f"{req_by_tok[tok].code} scheduled {n}x on {day}", token=tok, day=day)

    allocation: dict[str, Any] = {}
    if check_allocation:
        scope = [r for r in ds.requirements if scope_tokens is None or r.token in scope_tokens]
        under, over = [], []
        for r in scope:
            got = hours.get(r.token, 0)
            if got < r.weekly_hours:
                under.append({"token": r.token, "code": r.code, "required_h": r.weekly_hours, "scheduled_h": got})
            elif got > r.weekly_hours:
                over.append({"token": r.token, "code": r.code, "required_h": r.weekly_hours, "scheduled_h": got})
        req_h = sum(r.weekly_hours for r in scope)
        got_h = sum(min(hours.get(r.token, 0), r.weekly_hours) for r in scope)
        allocation = {"required_hours": req_h, "scheduled_hours": got_h,
                      "coverage_pct": round(100 * got_h / req_h, 2) if req_h else 100.0,
                      "under_allocated": len(under), "over_allocated": len(over),
                      "under": under[:200], "over": over[:200]}

    hard = {k: v for k, v in counts.items() if k.startswith("H")}
    return {
        "valid": not hard and (not allocation or allocation["under_allocated"] == 0),
        "hard_violation_total": sum(hard.values()),
        "counts": dict(sorted(counts.items())),
        "violations": violations,
        "allocation": allocation,
    }


def assignments_to_bookings(ds: Dataset, assignments: list[dict] | list, include_fixed: bool = True) -> list[Booking]:
    req_by_tok = {r.token: r for r in ds.requirements}
    out: list[Booking] = []
    for a in assignments:
        a = a if isinstance(a, dict) else a.model_dump()
        r: Requirement = req_by_tok[a["token"]]
        out.append(Booking(day=a["day"], start=a["start"], duration=r.duration, batches=r.batches,
                           faculty=r.faculty, room=a["room"], course=r.course, kind=r.kind,
                           origin=a.get("origin", "solver"), session_id=a["session_id"],
                           token=r.token, basket_id=r.basket_id))
    if include_fixed:
        out.extend(ds.fixed_bookings)
    return out
