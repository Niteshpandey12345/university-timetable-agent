"""v3: genetic algorithm, ITC-2007 benchmark, limitations (seats, preferences, basket confidence), software surface."""
import random

from ttagent.benchmarks.ctt import BEST_KNOWN, evaluate, load
from ttagent.benchmarks.ctt_ga import _P, _S, solve_ctt_ga
from ttagent.engine.ga import GAOptions, _Problem, _State, solve_ga
from ttagent.engine.validator import assignments_to_bookings, validate


def test_ga_incremental_cost_equals_full_rescore(ds):
    P = _Problem(ds, GAOptions(semesters=[3]))
    rng = random.Random(3)
    st = _State(P, [rng.choice(c) for c in P.cands])
    for _ in range(250):
        i = rng.randrange(P.n)
        c = rng.choice(P.cands[i])
        dh, dsoft = st.move_delta(i, c)
        st.set_gene(i, c, dh, dsoft)
    assert (st.hard, st.soft) == st._full_cost()


def test_ga_output_is_clash_free(ds):
    r = solve_ga(ds, GAOptions(semesters=[3], time_limit_s=12, population=6, seed=2))
    v = validate(ds, assignments_to_bookings(ds, r["assignments"]), scope_tokens=set(r["scope_tokens"]))
    assert v["hard_violation_total"] == 0                    # conflicted sessions are un-placed, never clashing
    assert v["allocation"]["coverage_pct"] > 90


def test_ctt_parser_and_bounds():
    inst = load("comp01")
    s = inst.summary()
    assert (s["courses"], s["rooms"], s["days"], s["periods_per_day"], s["curricula"], s["lectures"]) == (30, 6, 5, 6, 14, 160)
    assert BEST_KNOWN["comp01"] == (5, 5) and len(BEST_KNOWN) == 21


def test_ctt_ga_incremental_matches_independent_scorer():
    inst = load("comp01")
    P = _P(inst)
    rng = random.Random(5)
    st = _S(P, [rng.choice(P.cands[c]) for c in P.lect])
    for _ in range(300):
        j = rng.randrange(len(P.lect))
        c = rng.choice(P.cands[P.lect[j]])
        dh, dsoft = st.delta(j, c)
        st.setg(j, c, dh, dsoft)
    ev = evaluate(inst, [(P.lect[j], p, r) for j, (p, r) in enumerate(st.g)])
    assert st.full() == (st.hard, st.soft) and st.soft == ev["cost"]


def test_ctt_ga_finds_feasible_solution():
    r = solve_ctt_ga(load("comp01"), time_limit_s=10, population=4)
    assert r["feasible"] and r["cost"] >= BEST_KNOWN["comp01"][0]


def test_real_seat_counts_override_proxy(ds):
    from ttagent.domain.rooms import room_suitable
    r = next(r for r in ds.requirements if ds.rooms[r.venue].category == "lecture_theatre" and len(r.batches) >= 3)
    lt = next(x for x in ds.rooms.values() if x.category == "lecture_theatre" and x.room_id != r.venue)
    small, big = lt.model_copy(update={"seats": 30}), lt.model_copy(update={"seats": 500})
    assert not room_suitable(ds, r, small) and room_suitable(ds, r, big)


def test_teacher_preferences_are_penalised(ds):
    from ttagent.engine.metrics import compute_metrics
    d2 = ds.model_copy(deep=True)
    r = next(r for r in d2.requirements if r.semester == 3 and not d2.is_placeholder(r.faculty[0]))
    d2.faculty[r.faculty[0]].avoid = [("Mon", None)]
    bk = assignments_to_bookings(d2, [{"session_id": f"R{r.token}#0", "token": r.token, "day": "Mon", "start": 1, "room": r.venue}],
                                 include_fixed=False)
    assert compute_metrics(d2, bk)["faculty_preference_violations"] == r.duration


def test_basket_confidence(ds):
    assert {b.confidence for b in ds.baskets} <= {"high", "medium", "low"}
    assert any(b.confidence == "high" for b in ds.baskets)


def test_cli_and_settings_overrides(tmp_path, monkeypatch):
    import ttagent.config as cfg
    from ttagent.cli import main
    import pytest
    with pytest.raises(SystemExit):
        main(["--version"])
    monkeypatch.setattr(cfg, "OVERRIDES_PATH", tmp_path / "overrides.yaml")
    cfg.settings.cache_clear()
    cfg.save_overrides({"solver": {"weights": {"batch_gap": 9}}})
    assert cfg.settings()["solver"]["weights"]["batch_gap"] == 9
    assert cfg.settings()["solver"]["weights"]["saturday_hour"] == 2      # untouched keys survive the merge
    cfg.settings.cache_clear()


def test_ctt_lns_feasible_and_no_worse_than_initial():
    from ttagent.benchmarks.ctt_lns import solve_ctt_lns
    r = solve_ctt_lns(load("comp01"), time_limit_s=15, workers=2, init_share=0.4, sub_limit_s=2)
    assert r["feasible"] and r["cost"] >= BEST_KNOWN["comp01"][0]
    costs = [c for _, c in r["history"]]
    assert costs == sorted(costs, reverse=True)              # incumbent never gets worse
