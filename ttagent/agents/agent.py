"""
TimetableAgent: one live agent session = MCP bridge (4 server subprocesses) +
compiled LangGraph + SQLite checkpointer (persistent memory per thread_id).

    async with TimetableAgent() as agent:
        out = await agent.ask("Generate timetable for semester 3", thread_id="demo")
        if out["pending_approval"]:
            out = await agent.resume("demo", approve=True)
"""
from __future__ import annotations

import uuid
from typing import Any

from langchain_core.messages import HumanMessage
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
from langgraph.types import Command

from ttagent.agents.graph import build_graph
from ttagent.agents.llm import build_llm, describe
from ttagent.agents.mcp_bridge import MCPBridge
from ttagent.config import CHECKPOINT_DB, ensure_dirs


class TimetableAgent:
    def __init__(self, *, use_llm: bool = True, require_approval: bool | None = None, solve_time_s: float = 60,
                 checkpoint_path: str | None = None) -> None:
        self.use_llm = use_llm
        self.require_approval = require_approval
        self.solve_time_s = solve_time_s
        self.checkpoint_path = checkpoint_path or str(CHECKPOINT_DB)
        self.bridge: MCPBridge | None = None
        self.graph = None
        self._saver_cm = None
        self.model = "offline (rule-based)"

    async def __aenter__(self) -> "TimetableAgent":
        ensure_dirs()
        self.bridge = await MCPBridge().__aenter__()
        llm = build_llm() if self.use_llm else None
        self.model = describe() if llm else "offline (rule-based)"
        ov = await self.bridge.call("academic", "get_overview")
        fac = await self.bridge.call("resource", "list_faculty")
        rooms = await self.bridge.call("resource", "list_rooms")
        builder = build_graph(self.bridge, llm, require_approval=self.require_approval, solve_time_s=self.solve_time_s,
                              known_faculty={f["code"] for f in fac.get("faculty", [])},
                              known_rooms={r["room_id"] for r in rooms.get("rooms", [])})
        self._saver_cm = AsyncSqliteSaver.from_conn_string(self.checkpoint_path)
        saver = await self._saver_cm.__aenter__()
        self.graph = builder.compile(checkpointer=saver)
        self.overview = ov
        return self

    async def __aexit__(self, *exc: Any) -> None:
        if self._saver_cm is not None:
            await self._saver_cm.__aexit__(*exc)
        if self.bridge is not None:
            await self.bridge.__aexit__(*exc)

    @staticmethod
    def new_thread() -> str:
        return uuid.uuid4().hex[:10]

    async def ask(self, text: str, thread_id: str = "default") -> dict[str, Any]:
        cfg = {"configurable": {"thread_id": thread_id}, "recursion_limit": 60}
        state = await self.graph.ainvoke({"messages": [HumanMessage(text)], "request": text}, cfg)
        return self._package(state, thread_id)

    async def resume(self, thread_id: str, approve: bool) -> dict[str, Any]:
        cfg = {"configurable": {"thread_id": thread_id}, "recursion_limit": 60}
        state = await self.graph.ainvoke(Command(resume={"approve": approve}), cfg)
        return self._package(state, thread_id)

    def _package(self, state: dict, thread_id: str) -> dict[str, Any]:
        pending = state.get("__interrupt__")
        return {"thread_id": thread_id,
                "answer": state.get("answer", "") if not pending else "",
                "pending_approval": pending[0].value if pending else None,
                "intent": state.get("intent"),
                "solution_id": state.get("solution_id") or state.get("last_solution_id"),
                "trace": state.get("trace", []),
                "model": self.model}

    async def history(self, thread_id: str) -> list[dict[str, str]]:
        snap = await self.graph.aget_state({"configurable": {"thread_id": thread_id}})
        return [{"role": "user" if m.type == "human" else "assistant", "content": m.content if isinstance(m.content, str) else str(m.content)}
                for m in (snap.values or {}).get("messages", [])]

    def mermaid(self) -> str:
        return self.graph.get_graph().draw_mermaid()
