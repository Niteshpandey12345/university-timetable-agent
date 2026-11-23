"""v4: agent safeguards (independent validation, violation repair, fallback), fault injection, robustness fixes."""
import asyncio

import pytest

from ttagent.engine.service import _corrupt, _faults
from ttagent.engine.validator import assignments_to_bookings, validate


def test_fault_spec_parsing(monkeypatch):
    monkeypatch.setenv("TTAGENT_FAULT", "corrupt:3,crash:1,budget:2")
    assert _faults() == {"corrupt": 3.0, "crash": 1.0, "budget": 2.0}
    monkeypatch.setenv("TTAGENT_FAULT", "")
    assert _faults() == {}


def test_corruption_creates_clashes_that_violation_repair_removes(svc):
    s = svc.solve(semesters=[7], time_limit_s=10)
    sol = svc.store.load_solution(s["solution_id"])
    ds = svc.dataset()
    bad = _corrupt(ds, sol["assignments"], 2, seed=1)
    v = validate(ds, assignments_to_bookings(ds, bad), scope_tokens={a["token"] for a in bad})
    assert v["hard_violation_total"] > 0
    broken = dict(sol, assignments=bad)
    sid = svc.store.save_solution(broken, label="broken")             # stored self-report still says valid
    assert svc.solution_summary(sid)["valid"] is True
    assert svc.validate_solution(sid)["hard_violation_total"] > 0      # the independent re-check does not
    assert svc.violating_sessions(sid)
    fixed = svc.repair(sid, time_limit_s=15, fix_violations=True)
    assert svc.validate_solution(fixed["solution_id"])["hard_violation_total"] == 0


def test_robust_pipeline_falls_back_when_time_phase_returns_nothing(ds, monkeypatch):
    import ttagent.engine.decomposed as dec
    from ttagent.engine.cpsat import SolveOptions
    from ttagent.engine.service import robust_pipeline
    monkeypatch.setattr(dec, "solve_decomposed",
                        lambda d, o: dec._empty([], "UNKNOWN", 0, o) | {"assignments": [], "unplaced": []})
    r = robust_pipeline(ds, SolveOptions(semesters=[7], time_limit_s=10, workers=1), repair=False)
    assert r["stats"]["fallback"].startswith("time phase UNKNOWN") and r["assignments"]


def test_ga_respects_what_if_forbids(ds):
    from ttagent.engine.ga import GAOptions, solve_ga
    r = solve_ga(ds, GAOptions(semesters=[7], time_limit_s=6, population=4,
                               forbid=[{"room": "LT1", "day": d, "slot": None} for d in ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat")]))
    assert all(a["room"] != "LT1" for a in r["assignments"])


def test_unknown_ablation_rejected():
    from ttagent.agents.graph import build_graph
    with pytest.raises(ValueError):
        build_graph(None, ablate={"nonsense"})


def test_ctt_decomposition_feasible():
    from ttagent.benchmarks.ctt import BEST_KNOWN, load
    from ttagent.benchmarks.ctt_decomp import solve_ctt_decomposed
    r = solve_ctt_decomposed(load("comp01"), time_limit_s=20, workers=2)
    assert r["feasible"] and r["cost"] >= BEST_KNOWN["comp01"][0]


@pytest.mark.workbook
def test_agent_catches_silent_corruption_and_never_publishes_a_clash(tmp_path, monkeypatch):
    """End to end through the MCP servers: the scheduling tool lies; the validator agent catches it."""
    from ttagent.agents.agent import TimetableAgent
    from ttagent.engine.service import TimetableService
    from ttagent.store.db import Store
    db = tmp_path / "tt.db"
    import shutil
    from ttagent.config import DB_PATH
    s0 = TimetableService()
    s0.dataset()
    shutil.copy(DB_PATH, db)
    st = Store(db)
    for s in st.list_solutions(200):
        st.update_solution(s["id"], published=0)
    monkeypatch.setenv("TTAGENT_DB", str(db))
    monkeypatch.setenv("TTAGENT_FAULT", "corrupt:2")

    async def run():
        async with TimetableAgent(use_llm=False, require_approval=True, solve_time_s=12, approver=lambda q: True,
                                  checkpoint_path=str(tmp_path / "mem.db")) as ag:
            return await ag.ask("Generate timetable for semester 7", thread_id="t")
    out = asyncio.run(run())
    monkeypatch.setenv("TTAGENT_FAULT", "")
    svc = TimetableService(Store(db))
    pubs = [s for s in svc.store.list_solutions(50) if s["published"]]
    assert any(t.get("mcp") == "timetable.repair_timetable" for t in out["trace"])
    assert all(svc.validate_solution(p["id"])["hard_violation_total"] == 0 for p in pubs)
