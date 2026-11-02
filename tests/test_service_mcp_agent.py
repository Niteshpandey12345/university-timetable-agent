"""Service facade, real MCP stdio round-trip, and the LangGraph agent offline."""
import asyncio
from pathlib import Path

import pytest

from ttagent.agents.intent import parse_rules
from ttagent.engine.service import ServiceError


def test_service_flow(svc):
    s = svc.solve(semesters=[3], time_limit_s=12, workers=4)
    assert s["valid"] and s["coverage_pct"] == 100.0
    a = svc.store.load_solution("latest")["assignments"][0]
    m = svc.move_session(a["session_id"], a["day"], a["start"], commit=False)
    assert m["legal"]                                     # moving to its own place breaks nothing
    assert svc.validate_solution("published")["counts"]["H2_faculty_clash"] >= 3
    assert Path(svc.export(fmt="xlsx")["path"]).exists()
    assert Path(svc.export(fmt="ics", entity="batch:3B1")["path"]).read_text().count("BEGIN:VEVENT") > 10
    with pytest.raises(ServiceError):
        svc.publish("published")


@pytest.mark.parametrize("text,action", [
    ("Generate timetable for Year 3.", "generate"),
    ("Show all conflicts in the current timetable.", "validate"),
    ("Why could you not schedule the DBMS lab?", "explain_unplaced"),
    ("Compare with the published timetable", "compare"),
    ("RAVI is on leave on Monday", "what_if_unavailable"),
    ("move R104#0 to Friday slot 2", "move"),
    ("show timetable of 1B1", "view"),
    ("export calendar for batch 1B1", "export"),
])
def test_intent_rules(text, action):
    assert parse_rules(text, {"RAVI"}, {"LT1"}).action == action


def test_year_maps_to_semester():
    assert parse_rules("Generate timetable for Year 3").semesters == [5]


def test_mcp_round_trip():
    from ttagent.agents.mcp_bridge import MCPBridge

    async def go():
        async with MCPBridge() as b:
            assert len(b.tools) == 30
            ov = await b.call("academic", "get_overview")
            assert set(ov["semesters"]) == {"1", "3", "5", "7"}
            tools = {t.name: t for t in b.langchain_tools()}
            out = await tools["resource__capacity_report"].ainvoke({"basket_aware": False})
            assert '"provably_infeasible": true' in out
    asyncio.run(go())


def test_agent_offline_generate_with_approval(tmp_path):
    from ttagent.agents.agent import TimetableAgent

    async def go():
        async with TimetableAgent(use_llm=False, require_approval=True, solve_time_s=12,
                                  checkpoint_path=str(tmp_path / "mem.db")) as ag:
            out = await ag.ask("Generate timetable for semester 3", "t")
            assert out["pending_approval"] and "Publish timetable" in out["pending_approval"]["question"]
            path = [t.get("mcp") for t in out["trace"] if t.get("mcp")]
            assert path[:2] == ["resource.capacity_report", "academic.get_requirements"]
            assert "timetable.generate_timetable" in path and "timetable.validate_timetable" in path
            out = await ag.resume("t", approve=True)
            assert "100.0%" in out["answer"] and "published" in out["answer"]
            out = await ag.ask("show timetable of batch 3B1", "t")
            assert out["intent"]["action"] == "view" and "| Mon |" in out["answer"]
            assert len(await ag.history("t")) == 4         # memory persisted in the checkpointer
    asyncio.run(go())
