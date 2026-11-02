"""
MCP client bridge.

Spawns the four MCP servers as stdio subprocesses (using *this* interpreter,
`sys.executable`, so the venv is honoured on Windows), keeps one ClientSession
per server, and exposes:

* `call(server, tool, **args)` -- used by deterministic LangGraph nodes, which
  follow the workflow without asking the LLM which tool to use;
* `langchain_tools()` -- every MCP tool as a typed LangChain StructuredTool
  (schema generated from the tool's MCP inputSchema, incl. Optional/anyOf and
  typed arrays), used by the ReAct assistant node.
"""
from __future__ import annotations

import datetime as dt
import json
import os
import sys
import time
from contextlib import AsyncExitStack
from typing import Any

from langchain_core.tools import StructuredTool
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from pydantic import Field, create_model

from ttagent.config import ROOT

SERVERS = {
    "data": "ttagent.mcp_servers.data_server",
    "academic": "ttagent.mcp_servers.academic_server",
    "resource": "ttagent.mcp_servers.resource_server",
    "timetable": "ttagent.mcp_servers.timetable_server",
}


class MCPBridge:
    def __init__(self, servers: dict[str, str] | None = None, trace: list | None = None) -> None:
        self.servers = servers or SERVERS
        self._stack = AsyncExitStack()
        self.sessions: dict[str, ClientSession] = {}
        self.tools: dict[str, dict[str, Any]] = {}  # qualified name -> {server, tool}
        self.trace: list[dict[str, Any]] = trace if trace is not None else []

    async def __aenter__(self) -> "MCPBridge":
        env = dict(os.environ)
        env["PYTHONPATH"] = str(ROOT) + os.pathsep + env.get("PYTHONPATH", "")
        env.setdefault("PYTHONIOENCODING", "utf-8")
        for name, module in self.servers.items():
            params = StdioServerParameters(command=sys.executable, args=["-m", module], env=env, cwd=str(ROOT))
            read, write = await self._stack.enter_async_context(stdio_client(params))
            session = await self._stack.enter_async_context(ClientSession(read, write))
            await session.initialize()
            self.sessions[name] = session
            for t in (await session.list_tools()).tools:
                self.tools[f"{name}__{t.name}"] = {"server": name, "tool": t}
        return self

    async def __aexit__(self, *exc: Any) -> None:
        await self._stack.aclose()

    async def call(self, server: str, tool: str, **arguments: Any) -> Any:
        args = {k: v for k, v in arguments.items() if v is not None}
        t0 = time.time()
        result = await self.sessions[server].call_tool(tool, arguments=args,
                                                       read_timeout_seconds=dt.timedelta(seconds=1800))
        if result.structuredContent is not None:
            data = result.structuredContent
            data = data.get("result", data) if isinstance(data, dict) and set(data) == {"result"} else data
        else:
            text = "".join(getattr(c, "text", "") for c in result.content)
            try:
                data = json.loads(text)
            except json.JSONDecodeError:
                data = {"text": text}
        self.trace.append({"server": server, "tool": tool, "args": args, "ms": round((time.time() - t0) * 1000),
                           "error": data.get("error") if isinstance(data, dict) else None})
        return data

    def langchain_tools(self) -> list[StructuredTool]:
        out = []
        for qname, meta in self.tools.items():
            server, tool = meta["server"], meta["tool"]
            out.append(StructuredTool.from_function(
                coroutine=self._make_runner(server, tool.name),
                name=qname,
                description=f"[{server} MCP server] {(tool.description or tool.name).strip()}",
                args_schema=schema_to_model(qname, tool.inputSchema or {}),
            ))
        return out

    def _make_runner(self, server: str, name: str):
        async def _run(**kwargs: Any) -> str:
            data = await self.call(server, name, **kwargs)
            s = json.dumps(data, default=str)
            return s if len(s) < 12000 else s[:12000] + ' ... "[truncated: ask for a narrower view]"'
        return _run


_JSON = {"string": str, "integer": int, "number": float, "boolean": bool, "object": dict}


def _py_type(spec: dict[str, Any]):
    if "anyOf" in spec:
        types = [s for s in spec["anyOf"] if s.get("type") != "null"]
        base = _py_type(types[0]) if types else str
        return base | None
    t = spec.get("type", "string")
    if t == "array":
        return list[_py_type(spec.get("items", {"type": "string"}))]
    return _JSON.get(t, str)


def schema_to_model(name: str, schema: dict[str, Any]):
    props = schema.get("properties", {})
    required = set(schema.get("required", []))
    fields = {}
    for p, spec in props.items():
        typ = _py_type(spec)
        desc = spec.get("description") or spec.get("title") or p
        if p in required:
            fields[p] = (typ, Field(..., description=desc))
        else:
            fields[p] = (typ | None if "anyOf" not in spec else typ, Field(spec.get("default"), description=desc))
    return create_model(f"{name}_args".replace("-", "_"), **fields)


async def _smoke() -> None:
    async with MCPBridge() as b:
        print(f"{len(b.tools)} MCP tools:", ", ".join(sorted(b.tools)))
        ov = await b.call("academic", "get_overview")
        print("semesters:", list(ov["semesters"]))
        print("capacity:", (await b.call("resource", "capacity_report"))["provably_infeasible"])


if __name__ == "__main__":
    import asyncio
    asyncio.run(_smoke())
