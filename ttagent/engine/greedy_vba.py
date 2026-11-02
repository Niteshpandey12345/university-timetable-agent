"""
Faithful Python port of the workbook's VBA scheduler (Module2.Allot).

Kept as the research *baseline*. Behaviour reproduced from the macro:

* requirements are processed top-to-bottom in sheet order (rows 5..N3+4);
* days Mon..Sat, slots 1..7 (VBA k = 2..8); slot 5 is lunch (Main!R3 = 6);
  Saturday stops at slot 4 ("remove 1PM onward classes on Sat");
* a multi-hour block must fit entirely before lunch or entirely after it;
* a token is placed at most once per day;
* conflict test against everything already in the same column of that day:
    venue equal  OR  faculty InStr() substring match (either direction)
    OR  any batch in common (`intersect`)
* the "9AM optimisation": a first pass only tries slot 1 of every day,
  then a normal row-wise pass;
* rooms are never changed and electives are never run in parallel.

The `vba_substring_faculty` flag reproduces the macro's InStr() semantics
(e.g. 'PP' matches 'PPK'); set False for exact set semantics.
"""
from __future__ import annotations

import time
from collections import defaultdict
from typing import Any

from ttagent.domain.models import Dataset, Requirement, session_ids
from ttagent.domain.timegrid import TimeGrid


def solve_greedy_vba(ds: Dataset, include_fixed: bool = False, vba_substring_faculty: bool = True,
                     semesters: list[int] | None = None) -> dict[str, Any]:
    t0 = time.time()
    grid = TimeGrid.from_settings(use_extended=False)
    occupied: dict[tuple[str, int], list[tuple[set[str], str, str]]] = defaultdict(list)  # (day,slot) -> (batches, faculty_str, venue)
    if include_fixed:
        for b in ds.fixed_bookings:
            for s in b.slots:
                occupied[(b.day, s)].append((set(b.batches), ",".join(b.faculty), b.room))

    def clash(day: str, slot: int, r: Requirement) -> bool:
        fac = ",".join(r.faculty)
        for batches, fstr, venue in occupied[(day, slot)]:
            if venue.upper() == r.venue.upper():
                return True
            if vba_substring_faculty:
                if fac in fstr or fstr in fac:
                    return True
            elif set(fstr.split(",")) & set(r.faculty):
                return True
            if batches & set(r.batches):
                return True
        return False

    def fits(day: str, start: int, r: Requirement) -> bool:
        return start in grid.starts_for(day, r.duration) and all(not clash(day, s, r) for s in range(start, start + r.duration))

    assignments, unplaced = [], []
    scope = [r for r in sorted(ds.requirements, key=lambda r: r.source_row or r.token)
             if semesters is None or r.semester in semesters]
    for r in scope:
        placed_days: list[str] = []
        sids = session_ids(r)
        # pass 1: "9AM optimisation" -- slot 1 of each day
        for day in grid.days:
            if len(placed_days) == r.per_week:
                break
            if day not in placed_days and fits(day, 1, r):
                placed_days.append(day)
                _commit(occupied, assignments, r, sids[len(placed_days) - 1], day, 1)
        # pass 2: row-wise scan
        for day in grid.days:
            if len(placed_days) == r.per_week:
                break
            if day in placed_days:
                continue
            for start in grid.starts_for(day, r.duration):
                if fits(day, start, r):
                    placed_days.append(day)
                    _commit(occupied, assignments, r, sids[len(placed_days) - 1], day, start)
                    break
        for sid in sids[len(placed_days):]:
            unplaced.append({"session_id": sid, "token": r.token, "code": r.code, "reason": "greedy: no clash-free column"})
    return {"method": "greedy_vba", "status": "HEURISTIC", "objective": None, "best_bound": None,
            "assignments": assignments, "unplaced": unplaced, "scope_tokens": [r.token for r in scope],
            "stats": {"wall_s": round(time.time() - t0, 3), "include_fixed": include_fixed,
                      "vba_substring_faculty": vba_substring_faculty},
            "options": {"include_fixed": include_fixed}}


def _commit(occupied, assignments, r: Requirement, sid: str, day: str, start: int) -> None:
    for s in range(start, start + r.duration):
        occupied[(day, s)].append((set(r.batches), ",".join(r.faculty), r.venue))
    assignments.append({"session_id": sid, "token": r.token, "day": day, "start": start, "room": r.venue})
