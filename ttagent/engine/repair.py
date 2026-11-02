"""
Large-Neighbourhood-Search repair with an escalating relaxation ladder.

Given a solution with unplaced sessions (or a set of sessions the user wants
moved), free a *neighbourhood* -- every session sharing a batch, a teacher or a
room with the problem sessions -- freeze everything else, and re-solve that
small piece with the exact monolithic CP-SAT model (rooms inside). If that
still fails, escalate:

    level 0  same rules, 1st-degree neighbourhood
    level 1  wider neighbourhood (2nd degree) + more alternative rooms
    level 2  allow the extended 16:00-17:00 slot (penalised, used only if needed)

The ladder is what the Repair agent walks; each rung is a policy decision the
agent reports in plain language ("I had to use the 4 PM slot for ...").
"""
from __future__ import annotations

import time
from typing import Any

from ttagent.domain.models import Dataset
from ttagent.engine.cpsat import SolveOptions, solve

LADDER = [
    {"level": 0, "name": "local re-solve (exact model, rooms inside)", "engine": "exact", "degree": 1, "cap": 60,
     "max_alt_rooms": 8, "use_extended": False},
    {"level": 1, "name": "large neighbourhood (decomposed model)", "engine": "decomposed", "degree": 2, "cap": 320,
     "max_alt_rooms": 12, "use_extended": False},
    {"level": 2, "name": "allow extended 16:00-17:00 slot", "engine": "decomposed", "degree": 2, "cap": 320,
     "max_alt_rooms": 12, "use_extended": True},
]


def neighbourhood(ds: Dataset, seed_tokens: set[int], assignments: list[dict], degree: int = 1,
                  cap: int = 200) -> set[str]:
    """Session ids most likely to be *blocking* the seed requirements.

    Ranked: seeds' own sessions > share a batch (direct blockers) > share a
    teacher > share the requested venue; a 2nd-degree pass adds the blockers
    of those blockers. Capped so the exact model stays small.
    """
    req = {r.token: r for r in ds.requirements}
    rank: dict[int, int] = {t: 0 for t in seed_tokens}
    frontier = set(seed_tokens)
    for depth in range(degree):
        batches = {b for t in frontier for b in req[t].batches}
        facs = {f for t in frontier for f in req[t].faculty if not ds.is_placeholder(f)}
        venues = {req[t].venue for t in frontier}
        nxt = set()
        for r in ds.requirements:
            if r.token in rank:
                continue
            if set(r.batches) & batches:
                score = 1
            elif set(r.faculty) & facs:
                score = 2
            elif r.venue in venues:
                score = 3
            else:
                continue
            rank[r.token] = score + 3 * depth
            nxt.add(r.token)
        frontier = nxt
    free = [a for a in assignments if a["token"] in rank]
    free.sort(key=lambda a: (rank[a["token"]], a["session_id"]))
    return {a["session_id"] for a in free[:cap]}


def lns_repair(ds: Dataset, result: dict[str, Any], base: SolveOptions | None = None,
               time_limit_s: float = 20, max_level: int = 2, extra_free: set[str] | None = None,
               keep_weight: float = 40.0) -> dict[str, Any]:
    base = base or SolveOptions()
    t0 = time.time()
    current = dict(result)
    rounds = []
    # the ladder, then one re-seeded retry of the two widest rungs (CP-SAT search is randomised)
    plan = LADDER[: max_level + 1] + [{**r, "name": r["name"] + " (retry, new seed)", "reseed": True}
                                      for r in LADDER[max(1, max_level - 1): max_level + 1]]
    for rung in plan:
        unplaced = current["unplaced"]
        if not unplaced and not extra_free:
            break
        seeds = {u["token"] for u in unplaced}
        if extra_free:
            seeds |= {a["token"] for a in current["assignments"] if a["session_id"] in extra_free}
        assigns = [a for a in current["assignments"] if a.get("origin") != "frozen"] + \
                  [a for a in current["assignments"] if a.get("origin") == "frozen"]
        free_ids = neighbourhood(ds, seeds, assigns, rung["degree"], rung["cap"]) | (extra_free or set())
        frozen = [{k: a[k] for k in ("session_id", "token", "day", "start", "room")}
                  for a in assigns if a["session_id"] not in free_ids]
        hints = [a for a in assigns if a["session_id"] in free_ids]
        opts = SolveOptions(**{**base.__dict__,
                               "frozen": frozen, "hints": hints, "pins": base.pins,
                               "tokens": sorted(set(current.get("scope_tokens") or [r.token for r in ds.requirements])),
                               "semesters": None, "time_limit_s": time_limit_s,
                               "max_alt_rooms": rung["max_alt_rooms"],
                               "use_extended": rung["use_extended"] or bool(current.get("options", {}).get("use_extended")),
                               "seed": base.seed + (101 if rung.get("reseed") else 0),
                               "room_mode": "flex", "hint_reference": False, "keep_weight": keep_weight,
                               "soft": False})
        if rung["engine"] == "exact":
            res = solve(ds, opts)
        else:
            from ttagent.engine.decomposed import solve_decomposed
            res = solve_decomposed(ds, opts)
        before = len(unplaced)
        after = len(res["unplaced"])
        rounds.append({"level": rung["level"], "rung": rung["name"], "freed_sessions": len(free_ids),
                       "unplaced_before": before, "unplaced_after": after, "status": res["status"],
                       "solve_s": res["stats"]["wall_s"]})
        if res["status"] in ("OPTIMAL", "FEASIBLE") and after <= before:
            for a in res["assignments"]:
                a.pop("origin", None)
            current = {**current, "assignments": res["assignments"], "unplaced": res["unplaced"]}
        extra_free = None
        if not current["unplaced"]:
            break
    current = dict(current)
    used_ext = any(a["start"] + ds.req(a["token"]).duration - 1 >= 8 for a in current["assignments"])
    current["options"] = {**current.get("options", {}), "use_extended": bool(used_ext)}
    current["repair"] = {"rounds": rounds, "wall_s": round(time.time() - t0, 2),
                         "resolved": not current["unplaced"]}
    current["method"] = f"{result.get('method', 'solver')}+lns"
    return current
