"""
FastAPI layer + web dashboard.

    uvicorn ttagent.api.main:app --reload      ->  http://127.0.0.1:8000  (dashboard)
                                                    http://127.0.0.1:8000/docs (OpenAPI)

Data endpoints call TimetableService directly (fast, no LLM). /api/chat goes
through the full LangGraph agent, which talks to the four MCP servers.
"""
from __future__ import annotations

import asyncio
import os
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from ttagent.engine.service import ServiceError, TimetableService

STATIC = Path(__file__).parent / "static"
svc = TimetableService()
_agent = None
_agent_error: str | None = None
_agent_lock = asyncio.Lock()


async def get_agent():
    global _agent, _agent_error
    async with _agent_lock:
        if _agent is None and _agent_error is None:
            try:
                from ttagent.agents.agent import TimetableAgent
                _agent = await TimetableAgent(require_approval=os.getenv("TTAGENT_APPROVAL", "1") == "1",
                                              solve_time_s=float(os.getenv("TTAGENT_SOLVE_S", "60"))).__aenter__()
            except Exception as e:  # keep the dashboard usable without the agent
                _agent_error = f"{type(e).__name__}: {e}"
    if _agent is None:
        raise HTTPException(503, f"agent unavailable: {_agent_error}")
    return _agent


@asynccontextmanager
async def lifespan(app: FastAPI):
    await asyncio.to_thread(svc.dataset)  # ingest on first start
    yield
    if _agent is not None:
        await _agent.__aexit__(None, None, None)


app = FastAPI(title="University Timetable Agent v2", version="2.0", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=STATIC), name="static")
RESULTS = Path(__file__).resolve().parents[2] / "experiments" / "results"
RESULTS.mkdir(parents=True, exist_ok=True)
app.mount("/results", StaticFiles(directory=RESULTS), name="results")


def _call(fn, *a, **kw):
    try:
        return fn(*a, **kw)
    except ServiceError as e:
        raise HTTPException(400, str(e))


@app.get("/", response_class=HTMLResponse)
def dashboard() -> str:
    return (STATIC / "index.html").read_text(encoding="utf-8")


@app.get("/health")
def health() -> dict:
    return {"status": "ok", "agent_ready": _agent is not None, "agent_error": _agent_error}


# ---------------------------------------------------------------- data
@app.get("/api/overview")
def overview() -> dict:
    return _call(svc.overview)


@app.get("/api/quality")
def quality(section: str | None = None) -> dict:
    return _call(svc.quality_report, section)


@app.post("/api/ingest")
async def ingest(path: str | None = None) -> dict:
    return await asyncio.to_thread(_call, svc.ingest, path)


@app.post("/api/ingest/upload")
async def ingest_upload(request: Request, filename: str = "uploaded.xlsm") -> dict:
    """Upload a new Master_TimeTable workbook (raw file body) and ingest it."""
    from ttagent.config import ROOT
    if not filename.lower().endswith((".xlsm", ".xlsx")):
        raise HTTPException(400, "please upload an .xlsm or .xlsx workbook")
    data = await request.body()
    if len(data) > 30_000_000:
        raise HTTPException(413, "file too large")
    dest = ROOT / "data" / "raw" / "uploads" / Path(filename).name
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(data)
    out = await asyncio.to_thread(_call, svc.ingest, str(dest))
    for k in ("pseudo:published", "pseudo:vba"):
        svc.store.put(k, None)
    return out


@app.get("/api/version")
def version() -> dict:
    from ttagent import __version__
    from ttagent.agents.llm import describe
    return {"version": __version__, "llm": describe()}


EDITABLE = {"solver": ["time_limit_s", "daily_batch_cap", "faculty_daily_cap", "faculty_max_consecutive", "weights"],
            "agent": ["max_repair_iterations", "require_approval", "model_provider"], "rooms": ["default_batch_size"]}


@app.get("/api/settings")
def get_settings() -> dict:
    from ttagent.config import settings
    cfg = settings()
    return {sec: {k: cfg.get(sec, {}).get(k) for k in keys} for sec, keys in EDITABLE.items()}


@app.put("/api/settings")
async def put_settings(request: Request) -> dict:
    from ttagent.config import save_overrides
    body = await request.json()
    clean = {}
    for sec, keys in EDITABLE.items():
        for k in keys:
            if k in body.get(sec, {}):
                clean.setdefault(sec, {})[k] = body[sec][k]
    save_overrides(clean)
    return get_settings()


@app.get("/api/capacity")
def capacity(basket_aware: bool = True) -> dict:
    return _call(svc.capacity_report, basket_aware)


@app.get("/api/entities")
def entities() -> dict:
    ds = svc.dataset()
    return {"batches": sorted([b for b, v in ds.batches.items() if v.in_requirements], key=lambda s: (len(s), s)),
            "faculty": sorted(f for f, v in ds.faculty.items() if not v.is_placeholder),
            "rooms": sorted(ds.rooms), "semesters": [str(s) for s in ds.semesters()]}


@app.get("/api/requirements")
def requirements(semester: int | None = None, batch: str | None = None, faculty: str | None = None, limit: int = 200) -> dict:
    return _call(svc.list_requirements, semester, batch, None, faculty, None, limit)


# ---------------------------------------------------------------- timetable
class SolveRequest(BaseModel):
    semesters: list[int] | None = None
    method: str = "decomposed"
    time_limit_s: float = 60
    repair: bool = True
    stability_weight: float | None = None
    use_extended: bool = False
    label: str = ""


@app.post("/api/solve")
async def solve(req: SolveRequest) -> dict:
    return await asyncio.to_thread(_call, svc.solve, req.semesters, req.method, req.time_limit_s, req.repair,
                                   req.stability_weight, True, "flex", req.use_extended, req.label)


@app.get("/api/solutions")
def solutions(limit: int = 20) -> list[dict]:
    return svc.list_solutions(limit)


@app.get("/api/solutions/{sid}")
def summary(sid: str) -> dict:
    return _call(svc.solution_summary, sid)


@app.get("/api/solutions/{sid}/validate")
def validate(sid: str) -> dict:
    return _call(svc.validate_solution, sid)


@app.get("/api/solutions/{sid}/metrics")
def metrics(sid: str) -> dict:
    return _call(svc.metrics, sid)


@app.get("/api/solutions/{sid}/view")
def view(sid: str, kind: str, code: str) -> dict:
    return _call(svc.view, kind, code, sid)


@app.get("/api/solutions/{sid}/explain")
def explain(sid: str, token: int | None = None) -> dict:
    return _call(svc.explain_unplaced, sid, token)


class MoveRequest(BaseModel):
    session_id: str
    day: str
    start: int
    room: str | None = None
    solution_id: str = "latest"
    commit: bool = True


@app.post("/api/move")
def move(req: MoveRequest) -> dict:
    return _call(svc.move_session, req.session_id, req.day, req.start, req.room, req.solution_id, req.commit)


class Unavailability(BaseModel):
    kind: str
    code: str
    day: str
    slot: int | None = None
    note: str = ""


@app.post("/api/unavailability")
def add_unavailability(u: Unavailability) -> dict:
    return _call(svc.add_unavailability, u.kind, u.code, u.day, u.slot, u.note)


@app.get("/api/unavailability")
def list_unavailability() -> list[dict]:
    return svc.store.unavailability()


@app.delete("/api/unavailability/{uid}")
def remove_unavailability(uid: int) -> dict:
    return {"removed": svc.store.remove_unavailability(uid)}


@app.post("/api/solutions/{sid}/repair")
async def repair(sid: str, time_limit_s: float = 30) -> dict:
    return await asyncio.to_thread(_call, svc.repair, sid, time_limit_s)


@app.get("/api/compare")
def compare(a: str = "published", b: str = "latest") -> dict:
    return _call(svc.compare, a, b)


@app.post("/api/solutions/{sid}/publish")
def publish(sid: str) -> dict:
    return _call(svc.publish, sid)


@app.get("/api/solutions/{sid}/export")
def export(sid: str, fmt: str = "xlsx", entity: str | None = None) -> FileResponse:
    out = _call(svc.export, sid, fmt, entity)
    return FileResponse(out["path"], filename=Path(out["path"]).name)


@app.get("/api/audit")
def audit(n: int = 60) -> list[dict]:
    return svc.store.audit_tail(n)


# ---------------------------------------------------------------- agent
class ChatRequest(BaseModel):
    message: str
    thread_id: str = "web"


class ApproveRequest(BaseModel):
    thread_id: str = "web"
    approve: bool = True


@app.post("/api/chat")
async def chat(req: ChatRequest) -> dict[str, Any]:
    agent = await get_agent()
    return await agent.ask(req.message, req.thread_id)


@app.post("/api/chat/approve")
async def approve(req: ApproveRequest) -> dict[str, Any]:
    agent = await get_agent()
    return await agent.resume(req.thread_id, req.approve)


@app.get("/api/chat/history/{thread_id}")
async def history(thread_id: str) -> list[dict]:
    agent = await get_agent()
    return await agent.history(thread_id)


@app.post("/api/agent/start")
async def agent_start() -> dict:
    agent = await get_agent()
    return {"ready": True, "model": agent.model}


@app.get("/api/system")
async def system() -> dict:
    """Live connection status: API, store, dataset, LLM, LangGraph, and each MCP server with its tools."""
    import importlib
    from ttagent.agents.llm import describe
    from ttagent.agents.mcp_bridge import SERVERS
    ds = svc.dataset()
    servers = {}
    for name, module in SERVERS.items():
        connected = _agent is not None and name in _agent.bridge.sessions
        if connected:
            tools = [{"name": m["tool"].name, "description": (m["tool"].description or "").strip().split("\n")[0]}
                     for q, m in _agent.bridge.tools.items() if m["server"] == name]
        else:
            mod = importlib.import_module(module)
            tools = [{"name": t.name, "description": (t.description or "").strip().split("\n")[0]}
                     for t in await mod.mcp.list_tools()]
        servers[name] = {"module": module, "transport": "stdio", "connected": connected, "tools": tools}
    calls = svc.store.audit_tail(400)
    per_server = {}
    for c in calls:
        per_server[c["server"]] = per_server.get(c["server"], 0) + 1
    return {
        "api": "ok",
        "dataset": {"source": ds.source, "requirements": len(ds.requirements),
                    "hours": sum(r.weekly_hours for r in ds.requirements), "rooms": len(ds.rooms)},
        "store": {"path": str(svc.store.path), "solutions": len(svc.list_solutions(500))},
        "agent": {"ready": _agent is not None, "error": _agent_error,
                  "model": _agent.model if _agent else describe(),
                  "graph_nodes": ["supervisor", "requirements", "scheduler", "validator", "diagnoser", "repairer",
                                  "approval", "action", "assistant", "explainer", "respond"]},
        "mcp": servers,
        "recent_calls_by_server": per_server,
        "experiments": sorted(p.name for p in RESULTS.glob("*.png")),
    }


@app.get("/api/agent/graph")
async def graph() -> dict:
    agent = await get_agent()
    return {"mermaid": agent.mermaid(), "model": agent.model}
