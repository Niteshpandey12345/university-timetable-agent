"""
LangGraph multi-agent workflow.

                    +-------------+
       user ------> | supervisor  |  intent: LLM structured output, rule-based fallback
                    +------+------+
         generate /        |        \\  view / validate / explain / move / compare /
        what-if / repair   |         \\ quality / capacity / export / publish      chat
                           v          v                                            v
                 +------------------+  +------------------+                +-------------+
                 | requirements     |  | action agent     |                | assistant   |
                 | (capacity proof) |  | (1 MCP call)     |                | ReAct + all |
                 +--------+---------+  +---------+--------+                | 30 MCP tools|
                          v                      |                         +------+------+
                 +------------------+            |                                |
                 | scheduler        |  CP-SAT via MCP                             |
                 +--------+---------+            |                                |
                          v                      |                                |
                 +------------------+  valid?    |                                |
          +----> | validator        |---- yes ---+--> approval gate (interrupt) --+
          |      +--------+---------+            |          |                     |
          |               | no, unplaced         |          v                     v
          |      +--------v---------+            +----> explainer ------------> END
          |      | diagnoser        |  explain_unplaced + capacity
          |      +--------+---------+
          |      +--------v---------+
          +------| repairer         |  LNS ladder via MCP   (<= max_repair_iterations)
                 +------------------+

Deterministic nodes call MCP tools directly (the workflow is the contract);
LLMs are used only for understanding (supervisor), free-form Q&A (assistant)
and wording (explainer). State is checkpointed to SQLite per thread_id, so a
conversation -- and a pending approval -- survives a restart.
"""
from __future__ import annotations

import json
import time
from typing import Annotated, Any, TypedDict

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage
from langgraph.graph import END, START, StateGraph
from langgraph.graph.message import add_messages
from langgraph.types import interrupt

from ttagent.agents.intent import Intent, parse_rules
from ttagent.agents.llm import text_of
from ttagent.agents.prompts import ASSISTANT_PROMPT, EXPLAIN_PROMPT, INTENT_PROMPT
from ttagent.agents.render import render
from ttagent.config import settings


class AgentState(TypedDict, total=False):
    messages: Annotated[list, add_messages]
    request: str
    intent: dict
    result: dict
    solution_id: str
    repair_iterations: int
    trace: list
    answer: str
    last_solution_id: str      # memory across turns


SOLVE_ACTIONS = {"generate", "what_if_unavailable", "repair"}
SIMPLE_ACTIONS = {"view", "validate", "explain_unplaced", "move", "compare", "quality", "capacity", "export", "publish"}


def build_graph(bridge, llm=None, *, require_approval: bool | None = None, max_repairs: int | None = None,
                solve_time_s: float = 60, known_faculty: set[str] | None = None, known_rooms: set[str] | None = None,
                assistant_tools: list | None = None):
    acfg = settings()["agent"]
    require_approval = acfg["require_approval"] if require_approval is None else require_approval
    max_repairs = acfg["max_repair_iterations"] if max_repairs is None else max_repairs
    react = None
    if llm is not None:
        from langchain.agents import create_agent
        react = create_agent(model=llm, tools=assistant_tools or bridge.langchain_tools(), system_prompt=ASSISTANT_PROMPT)

    def tr(state: AgentState, node: str, **info: Any) -> list:
        return (state.get("trace") or []) + [{"node": node, "t": time.strftime("%H:%M:%S"), **info}]

    async def mcp(state_trace: list, node: str, server: str, tool: str, **args: Any) -> Any:
        t0 = time.time()
        out = await bridge.call(server, tool, **args)
        state_trace.append({"node": node, "mcp": f"{server}.{tool}", "args": {k: v for k, v in args.items() if v is not None},
                            "ms": round((time.time() - t0) * 1000),
                            "error": out.get("error") if isinstance(out, dict) else None})
        return out

    # ------------------------------------------------------------------ supervisor
    async def supervisor(state: AgentState) -> dict:
        text = state["request"]
        intent: Intent | None = None
        how = "rules"
        if llm is not None:
            try:
                parsed = await llm.with_structured_output(Intent).ainvoke(
                    [SystemMessage(INTENT_PROMPT), HumanMessage(text)])
                if parsed is not None:
                    intent, how = parsed, "llm"
            except Exception as e:  # provider hiccup -> deterministic fallback
                how = f"rules (llm failed: {type(e).__name__})"
        if intent is None:
            intent = parse_rules(text, known_faculty, known_rooms)
        # a rules-based guess of "chat" with an LLM available goes to the ReAct assistant
        return {"intent": intent.model_dump(), "result": {}, "repair_iterations": 0, "answer": "",
                "trace": [{"node": "supervisor", "t": time.strftime("%H:%M:%S"), "intent": intent.action, "parser": how}]}

    def route_supervisor(state: AgentState) -> str:
        a = state["intent"]["action"]
        if a in SOLVE_ACTIONS:
            return "requirements"
        if a in SIMPLE_ACTIONS:
            return "action"
        return "assistant"

    # ------------------------------------------------------------------ requirements / capacity agent
    async def requirements(state: AgentState) -> dict:
        it, trace = state["intent"], list(state.get("trace") or [])
        result = dict(state.get("result") or {})
        result["capacity"] = await mcp(trace, "requirements", "resource", "capacity_report", basket_aware=True)
        if it["semesters"]:
            reqs = await mcp(trace, "requirements", "academic", "get_requirements", semester=it["semesters"][0], limit=1)
            result["scope"] = {"semesters": it["semesters"], "requirements_in_first_semester": reqs.get("count")}
        if it["action"] == "what_if_unavailable":
            if not (it.get("kind") and it.get("code") and it.get("day")):
                result["error"] = "Please say who/what is unavailable and on which day (e.g. 'RAVI is unavailable on Monday')."
            else:
                result["unavailability"] = await mcp(trace, "requirements", "resource", "add_unavailability",
                                                     kind=it["kind"], code=it["code"], day=it["day"], slot=it.get("slot"),
                                                     note=state["request"][:200])
        return {"result": result, "trace": trace}

    def route_requirements(state: AgentState) -> str:
        r = state["result"]
        if r.get("error") or (r.get("unavailability") or {}).get("error"):
            return "explainer"
        return "scheduler"

    # ------------------------------------------------------------------ scheduler agent
    async def scheduler(state: AgentState) -> dict:
        it, trace = state["intent"], list(state.get("trace") or [])
        result = dict(state["result"])
        if it["action"] in ("what_if_unavailable", "repair") and state.get("last_solution_id"):
            sol = await mcp(trace, "scheduler", "timetable", "repair_timetable",
                            solution_id=state["last_solution_id"], time_limit_s=max(20, solve_time_s / 2))
        else:
            sol = await mcp(trace, "scheduler", "timetable", "generate_timetable", semesters=it.get("semesters"),
                            time_limit_s=solve_time_s, repair=False,
                            stability_weight=20.0 if it.get("stability") else None,
                            label=state["request"][:60])
        result["solution"] = sol
        return {"result": result, "trace": trace, "solution_id": sol.get("solution_id"),
                "last_solution_id": sol.get("solution_id") or state.get("last_solution_id")}

    # ------------------------------------------------------------------ validator agent
    async def validator(state: AgentState) -> dict:
        trace = list(state.get("trace") or [])
        result = dict(state["result"])
        if state.get("solution_id"):
            result["validation"] = await mcp(trace, "validator", "timetable", "validate_timetable", solution_id=state["solution_id"])
        return {"result": result, "trace": trace}

    def route_validation(state: AgentState) -> str:
        sol = state["result"].get("solution") or {}
        if sol.get("error") or not state.get("solution_id"):
            return "explainer"
        log = state["result"].get("repair_log") or []
        stalled = bool(log) and log[-1]["after"] is not None and log[-1]["after"] >= (log[-1]["before"] or 0)
        if sol.get("unplaced_count") and state.get("repair_iterations", 0) < max_repairs and not stalled:
            return "diagnoser"
        return "approval"

    # ------------------------------------------------------------------ diagnoser + repairer
    async def diagnoser(state: AgentState) -> dict:
        trace = list(state.get("trace") or [])
        result = dict(state["result"])
        result["diagnosis"] = await mcp(trace, "diagnoser", "timetable", "explain_unplaced", solution_id=state["solution_id"])
        return {"result": result, "trace": trace}

    async def repairer(state: AgentState) -> dict:
        trace = list(state.get("trace") or [])
        result = dict(state["result"])
        before = (result.get("solution") or {}).get("unplaced_count")
        sol = await mcp(trace, "repairer", "timetable", "repair_timetable", solution_id=state["solution_id"],
                        time_limit_s=max(20, solve_time_s / 2))
        n = state.get("repair_iterations", 0) + 1
        result["repair_log"] = (result.get("repair_log") or []) + [
            {"iteration": n, "before": before, "after": sol.get("unplaced_count"), "rounds": sol.get("repair_rounds")}]
        if not sol.get("error"):
            result["solution"] = sol
        return {"result": result, "trace": trace, "repair_iterations": n,
                "solution_id": sol.get("solution_id", state["solution_id"]),
                "last_solution_id": sol.get("solution_id", state.get("last_solution_id"))}

    # ------------------------------------------------------------------ human-in-the-loop approval
    async def approval(state: AgentState) -> dict:
        result = dict(state["result"])
        sol = result.get("solution") or {}
        if not require_approval or not sol.get("valid"):
            return {"result": result}
        decision = interrupt({
            "question": f"Publish timetable {sol['solution_id']}? coverage {sol.get('coverage_pct')}%, "
                        f"hard violations {sum((sol.get('hard_violations') or {}).values())}, "
                        f"soft penalty {(sol.get('quality') or {}).get('soft_penalty')}",
            "solution_id": sol["solution_id"]})
        approved = bool(decision.get("approve")) if isinstance(decision, dict) else bool(decision)
        trace = list(state.get("trace") or [])
        if approved:
            result["publish"] = await mcp(trace, "approval", "timetable", "publish_timetable", solution_id=sol["solution_id"])
        result["approval"] = {"approved": approved, "solution_id": sol["solution_id"]}
        return {"result": result, "trace": trace}

    # ------------------------------------------------------------------ single-call action agent
    async def action(state: AgentState) -> dict:
        it, trace = state["intent"], list(state.get("trace") or [])
        a = it["action"]
        sid = state.get("last_solution_id") or "latest"
        result: dict[str, Any] = {}
        if a == "view":
            if not it.get("kind"):
                result["error"] = "Tell me whose timetable: a batch (1B1), faculty code (RAVI), room (LT1) or semester."
            else:
                result["view"] = await mcp(trace, "action", "timetable", "view_timetable", kind=it["kind"], code=it["code"],
                                           solution_id=await _existing(trace, sid))
        elif a == "validate":
            result["validation"] = await mcp(trace, "action", "timetable", "validate_timetable", solution_id=await _existing(trace, sid))
        elif a == "explain_unplaced":
            result["diagnosis"] = await mcp(trace, "action", "timetable", "explain_unplaced",
                                            solution_id=await _existing(trace, sid), token=it.get("token"))
        elif a == "move":
            if not (it.get("session_id") and it.get("day") and it.get("start")):
                result["move"] = {"error": "Give the session id (e.g. R104#0), the day and the start slot (1-7)."}
            else:
                result["move"] = await mcp(trace, "action", "timetable", "move_session", session_id=it["session_id"],
                                           day=it["day"], start=it["start"], room=it.get("room"), solution_id=sid)
        elif a == "compare":
            result["comparison"] = await mcp(trace, "action", "timetable", "compare_timetables", a="published",
                                             b=await _existing(trace, sid))
        elif a == "quality":
            result["quality"] = await mcp(trace, "action", "data", "data_quality_report")
        elif a == "capacity":
            result["capacity"] = await mcp(trace, "action", "resource", "capacity_report", basket_aware=True)
        elif a == "export":
            result["export"] = await mcp(trace, "action", "timetable", "export_timetable", solution_id=await _existing(trace, sid),
                                         fmt=it.get("fmt") or "xlsx", entity=it.get("entity"))
        elif a == "publish":
            result["publish"] = await mcp(trace, "action", "timetable", "publish_timetable", solution_id=sid)
        return {"result": result, "trace": trace}

    async def _existing(trace: list, sid: str) -> str:
        """Use the latest generated version, or fall back to the workbook's published grid."""
        if sid != "latest":
            return sid
        sols = await bridge.call("timetable", "list_solutions", limit=1)
        return "latest" if sols.get("items") else "published"

    # ------------------------------------------------------------------ ReAct assistant (LLM + all MCP tools)
    async def assistant(state: AgentState) -> dict:
        trace = tr(state, "assistant")
        if react is None:
            return {"answer": ("I'm running offline (no LLM key set), so I handle structured requests: generate / "
                               "validate / view / explain unplaced / what-if unavailability / move / compare / "
                               "data quality / capacity / export / publish. Example: 'Generate timetable for "
                               "semester 3', 'Show timetable of batch 1B1', 'RAVI is unavailable on Monday'."),
                    "trace": trace}
        n0 = len(bridge.trace)
        out = await react.ainvoke({"messages": [HumanMessage(state["request"])]})
        msgs = out["messages"]
        final = next((m for m in reversed(msgs) if isinstance(m, AIMessage) and text_of(m).strip()), None)
        for call in bridge.trace[n0:]:
            trace.append({"node": "assistant", "mcp": f"{call['server']}.{call['tool']}", "args": call["args"], "ms": call["ms"]})
        return {"answer": text_of(final) if final else "(no answer produced)", "trace": trace}

    # ------------------------------------------------------------------ explainer
    async def explainer(state: AgentState) -> dict:
        trace = tr(state, "explainer")
        r = state.get("result") or {}
        a = state["intent"]["action"]
        if r.get("error"):
            return {"answer": r["error"], "trace": trace}
        text = render(a, r)
        if llm is not None and a in SOLVE_ACTIONS | {"explain_unplaced", "validate", "compare", "capacity", "quality"}:
            try:
                facts = json.dumps(_slim(r), default=str)[:14000]
                msg = await llm.ainvoke([SystemMessage(EXPLAIN_PROMPT),
                                         HumanMessage(f"User request: {state['request']}\n\nFacts:\n{facts}")])
                prose = text_of(msg).strip()
                if prose:
                    text = prose + "\n\n<details><summary>Exact figures</summary>\n\n" + text + "\n</details>"
            except Exception as e:
                trace.append({"node": "explainer", "note": f"LLM failed ({type(e).__name__}); template used"})
        return {"answer": text, "trace": trace}

    async def respond(state: AgentState) -> dict:
        return {"messages": [AIMessage(content=state.get("answer") or "")]}

    g = StateGraph(AgentState)
    for name, fn in [("supervisor", supervisor), ("requirements", requirements), ("scheduler", scheduler),
                     ("validator", validator), ("diagnoser", diagnoser), ("repairer", repairer),
                     ("approval", approval), ("action", action), ("assistant", assistant),
                     ("explainer", explainer), ("respond", respond)]:
        g.add_node(name, fn)
    g.add_edge(START, "supervisor")
    g.add_conditional_edges("supervisor", route_supervisor, ["requirements", "action", "assistant"])
    g.add_conditional_edges("requirements", route_requirements, ["scheduler", "explainer"])
    g.add_edge("scheduler", "validator")
    g.add_conditional_edges("validator", route_validation, ["diagnoser", "approval", "explainer"])
    g.add_edge("diagnoser", "repairer")
    g.add_edge("repairer", "validator")
    g.add_edge("approval", "explainer")
    g.add_edge("action", "explainer")
    g.add_edge("assistant", "respond")
    g.add_edge("explainer", "respond")
    g.add_edge("respond", END)
    return g


def _slim(r: dict) -> dict:
    """Trim bulky fields before giving facts to the LLM."""
    out = dict(r)
    if "view" in out:
        out["view"] = {k: out["view"][k] for k in ("kind", "code", "hours", "sessions") if k in out["view"]}
    if "diagnosis" in out and isinstance(out["diagnosis"], dict):
        d = dict(out["diagnosis"])
        d["explanations"] = [{k: e[k] for k in ("requirement", "blocker_histogram", "safe_havens", "fixable_by_room_change",
                                                 "placed_sessions", "required_sessions")} for e in d.get("explanations", [])]
        out["diagnosis"] = d
    if "capacity" in out and isinstance(out["capacity"], dict):
        c = out["capacity"]
        out["capacity"] = {k: c.get(k) for k in ("provably_infeasible", "infeasible_resources", "weekly_capacity_h")} | {
            "batches_top": (c.get("batches_top") or [])[:3], "room_pools_top": (c.get("room_pools_top") or [])[:3]}
    return out
