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

v4 safeguards (each one measured by scripts/run_agent_eval.py, which can switch it off):
  * validator  -- every decision (repair? publish?) uses an independent re-validation, never the
                  scheduler's self-report; a mismatch is logged, and "publish" requests are re-checked too;
  * repairer   -- also frees the sessions involved in hard violations (repair_timetable fix_violations=True);
  * scheduler  -- recovery plan when the tool fails or returns nothing: retry, then switch to the GA;
  * approval   -- a human (or the evaluation's simulated officer) decides every publication;
  * memory     -- each conversation remembers its own version, so other users' versions are not touched.

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


AGENT_COMPONENTS = ("validator", "repair", "fallback", "approval", "memory")


def build_graph(bridge, llm=None, *, require_approval: bool | None = None, max_repairs: int | None = None,
                solve_time_s: float = 60, known_faculty: set[str] | None = None, known_rooms: set[str] | None = None,
                assistant_tools: list | None = None, ablate: set[str] | frozenset = frozenset(),
                approver=None):
    """`ablate` switches agent components off for the evaluation (see AGENT_COMPONENTS):
    validator -- trust the scheduler's self-report instead of an independent re-validation;
    repair    -- no diagnoser/repairer loop;     fallback -- no retry/alternative method when a solve fails;
    approval  -- publish without asking a human; memory   -- forget the previous version between turns.
    `approver(question) -> bool` answers approvals automatically (simulated human in the evaluation)."""
    ablate = set(ablate)
    unknown = ablate - set(AGENT_COMPONENTS)
    if unknown:
        raise ValueError(f"unknown components to ablate: {unknown}")
    acfg = settings()["agent"]
    require_approval = acfg["require_approval"] if require_approval is None else require_approval
    if "approval" in ablate:
        require_approval = False
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
        last = None if "memory" in ablate else state.get("last_solution_id")
        if it["action"] in ("what_if_unavailable", "repair") and last:
            attempts = [("repair_timetable", {"solution_id": last, "time_limit_s": max(20, solve_time_s / 2)})]
        else:
            base = {"semesters": it.get("semesters"), "time_limit_s": solve_time_s, "repair": False,
                    "stability_weight": 20.0 if it.get("stability") else None, "label": state["request"][:60]}
            attempts = [("generate_timetable", base)]
            if "fallback" not in ablate:   # recovery plan: retry once, then switch to the GA (always returns a timetable)
                attempts += [("generate_timetable", base), ("generate_timetable", base | {"method": "ga"})]
        sol: dict = {}
        for n, (tool, args) in enumerate(attempts):
            sol = await mcp(trace, "scheduler", "timetable", tool, **args)
            failed = bool(sol.get("error")) or (sol.get("scheduled_hours") == 0 and sol.get("required_hours"))
            if not failed:
                if n:
                    result["recovered"] = {"attempts": n + 1, "via": args.get("method", "decomposed")}
                break
            trace.append({"node": "scheduler", "note": f"attempt {n + 1} failed: {sol.get('error') or 'empty timetable'}"})
        result["solution"] = sol
        return {"result": result, "trace": trace, "solution_id": sol.get("solution_id"),
                "last_solution_id": sol.get("solution_id") or state.get("last_solution_id")}

    # ------------------------------------------------------------------ validator agent
    async def validator(state: AgentState) -> dict:
        trace = list(state.get("trace") or [])
        result = dict(state["result"])
        if state.get("solution_id") and "validator" not in ablate:
            v = await mcp(trace, "validator", "timetable", "validate_timetable", solution_id=state["solution_id"])
            result["validation"] = v
            claimed = (result.get("solution") or {}).get("hard_violations") or {}
            if v.get("hard_violation_total") and not sum(claimed.values()):
                result["self_report_mismatch"] = {"claimed_hard": 0, "found_hard": v["hard_violation_total"]}
                trace.append({"node": "validator", "note": f"self-report mismatch: scheduler claimed 0 hard violations, "
                                                           f"independent check found {v['hard_violation_total']}"})
        return {"result": result, "trace": trace}

    def _hard(state: AgentState) -> int:
        """Hard violations as the deciding authority sees them: the independent validator, or (ablated) the self-report."""
        r = state["result"]
        if "validator" not in ablate and r.get("validation") and "hard_violation_total" in r["validation"]:
            return int(r["validation"]["hard_violation_total"] or 0)
        return int(sum(((r.get("solution") or {}).get("hard_violations") or {}).values()))

    def route_validation(state: AgentState) -> str:
        sol = state["result"].get("solution") or {}
        if sol.get("error") or not state.get("solution_id"):
            return "explainer"
        if "repair" in ablate:
            return "approval"
        log = state["result"].get("repair_log") or []
        problems = (sol.get("unplaced_count") or 0) + _hard(state)
        stalled = bool(log) and log[-1]["after"] is not None and log[-1]["after"] >= (log[-1]["before"] or 0)
        if problems and state.get("repair_iterations", 0) < max_repairs and not stalled:
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
        hard_before = _hard(state)
        before = ((result.get("solution") or {}).get("unplaced_count") or 0) + hard_before
        sol = await mcp(trace, "repairer", "timetable", "repair_timetable", solution_id=state["solution_id"],
                        time_limit_s=max(20, solve_time_s / 2), fix_violations=bool(hard_before))
        n = state.get("repair_iterations", 0) + 1
        after = None if sol.get("error") else (sol.get("unplaced_count") or 0) + int(sum((sol.get("hard_violations") or {}).values()))
        result["repair_log"] = (result.get("repair_log") or []) + [
            {"iteration": n, "before": before, "after": after, "fixed_violations": hard_before, "rounds": sol.get("repair_rounds")}]
        if not sol.get("error"):
            result["solution"] = sol
        return {"result": result, "trace": trace, "repair_iterations": n,
                "solution_id": sol.get("solution_id", state["solution_id"]),
                "last_solution_id": sol.get("solution_id", state.get("last_solution_id"))}

    # ------------------------------------------------------------------ human-in-the-loop approval
    async def approval(state: AgentState) -> dict:
        result = dict(state["result"])
        sol = result.get("solution") or {}
        v = result.get("validation") if "validator" not in ablate else None
        hard = _hard(state)
        coverage = (v or {}).get("allocation", {}).get("coverage_pct", sol.get("coverage_pct"))
        ok = (v.get("valid") if v else sol.get("valid")) and not hard
        if not ok or not sol.get("solution_id"):
            return {"result": result}
        question = (f"Publish timetable {sol['solution_id']}? coverage {coverage}%, hard violations {hard}, "
                    f"soft penalty {(sol.get('quality') or {}).get('soft_penalty')}"
                    + ("" if v else " (as reported by the scheduler; not independently checked)"))
        trace = list(state.get("trace") or [])
        if require_approval:
            if approver is not None:
                approved = bool(approver(question))
                trace.append({"node": "approval", "note": f"simulated human: {'approve' if approved else 'reject'}"})
            else:
                decision = interrupt({"question": question, "solution_id": sol["solution_id"]})
                approved = bool(decision.get("approve")) if isinstance(decision, dict) else bool(decision)
        else:
            approved = True
            trace.append({"node": "approval", "note": "auto-published (no human approval)"})
        if approved:
            result["publish"] = await mcp(trace, "approval", "timetable", "publish_timetable", solution_id=sol["solution_id"])
        result["approval"] = {"approved": approved, "solution_id": sol["solution_id"], "asked_human": require_approval}
        return {"result": result, "trace": trace}

    # ------------------------------------------------------------------ single-call action agent
    async def action(state: AgentState) -> dict:
        it, trace = state["intent"], list(state.get("trace") or [])
        a = it["action"]
        sid = (None if "memory" in ablate else state.get("last_solution_id")) or "latest"
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
            if "validator" not in ablate:      # never publish without an independent re-check
                v = await mcp(trace, "action", "timetable", "validate_timetable", solution_id=sid)
                result["validation"] = v
                if v.get("hard_violation_total"):
                    result["error"] = (f"Not published: the independent validator found {v['hard_violation_total']} hard "
                                       f"violation(s) in {v.get('solution_id', sid)}. Ask me to repair it first.")
                    return {"result": result, "trace": trace}
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
