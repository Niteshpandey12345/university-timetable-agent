"""Solver, baseline, pins, unavailability and the induced-conflict repair loop -- on the real data."""
from ttagent.domain.timegrid import TimeGrid
from ttagent.engine.cpsat import SolveOptions, solve
from ttagent.engine.decomposed import solve_decomposed
from ttagent.engine.greedy_vba import solve_greedy_vba
from ttagent.engine.repair import lns_repair
from ttagent.engine.validator import assignments_to_bookings, validate


def check(ds, res, extended=False):
    v = validate(ds, assignments_to_bookings(ds, res["assignments"]), TimeGrid.from_settings(extended),
                 scope_tokens=set(res["scope_tokens"]))
    return v


def test_vba_port_reproduces_macro(ds):
    g = solve_greedy_vba(ds)
    toks = {u["token"] for u in g["unplaced"]}
    macro = set(ds.quality["vba_macro_bookkeeping"]["tokens"])
    assert 30 <= len(toks) <= 45
    assert len(toks & macro) / len(toks | macro) > 0.6   # same failures as the real macro


def test_decomposed_semester_3_is_valid(ds):
    res = solve_decomposed(ds, SolveOptions(semesters=[3], time_limit_s=15, workers=4))
    v = check(ds, res)
    assert v["hard_violation_total"] == 0
    assert v["allocation"]["coverage_pct"] == 100.0


def test_exact_model_small_scope(ds):
    toks = [r.token for r in ds.requirements if r.semester == 1][:25]
    res = solve(ds, SolveOptions(tokens=toks, time_limit_s=10, workers=4))
    assert res["status"] in ("OPTIMAL", "FEASIBLE") and not res["unplaced"]
    assert check(ds, res)["hard_violation_total"] == 0


def test_pins_and_unavailability_respected(ds):
    r = next(r for r in ds.requirements if r.semester == 3 and r.kind.value == "L" and not r.basket_id)
    fac = r.faculty[0]
    opts = SolveOptions(semesters=[3], time_limit_s=15, workers=4,
                        pins=[{"token": r.token, "day": "Wed", "start": 2}],
                        forbid=[{"faculty": fac, "day": "Mon", "slot": None}])
    res = solve_decomposed(ds, opts)
    mine = [a for a in res["assignments"] if a["token"] == r.token]
    assert any(a["day"] == "Wed" and a["start"] == 2 for a in mine)
    for b in assignments_to_bookings(ds, res["assignments"], include_fixed=False):
        assert not (fac in b.faculty and b.day == "Mon")


def test_induced_conflict_is_repaired(ds):
    """Spec item: deliberately break a valid timetable and confirm the repair loop fixes it."""
    res = solve_decomposed(ds, SolveOptions(semesters=[3], time_limit_s=15, workers=4))
    assert not res["unplaced"]
    # knock out three sessions, then forbid their old slots so they MUST move
    victims = res["assignments"][:3]
    forbid = [{"batch": ds.req(a["token"]).batches[0], "day": a["day"], "slot": a["start"]} for a in victims]
    broken = {**res,
              "assignments": [a for a in res["assignments"] if a not in victims],
              "unplaced": [{"session_id": a["session_id"], "token": a["token"], "code": ds.req(a["token"]).code}
                           for a in victims]}
    fixed = lns_repair(ds, broken, SolveOptions(forbid=forbid, workers=4), time_limit_s=15)
    assert fixed["repair"]["resolved"], fixed["repair"]
    v = check(ds, fixed, extended=fixed["options"]["use_extended"])
    assert v["hard_violation_total"] == 0 and v["allocation"]["coverage_pct"] == 100.0
    for f, a in zip(forbid, victims):
        moved = next(x for x in fixed["assignments"] if x["session_id"] == a["session_id"])
        assert (moved["day"], moved["start"]) != (a["day"], a["start"])
