"""
Timetable MCP server: the only server that can create or change a timetable.
Every allocation is produced by the deterministic CP-SAT engine and independently
re-validated -- the LLM can request, inspect and explain, never invent.
"""
from __future__ import annotations

import json

from mcp.server.fastmcp import FastMCP

from ttagent.mcp_servers.common import audited, service

mcp = FastMCP("ttagent-timetable")
A = audited("timetable")


@mcp.tool()
@A
def generate_timetable(semesters: list[int] | None = None, method: str = "decomposed", time_limit_s: float = 60,
                       repair: bool = True, stability_weight: float | None = None, use_extended: bool = False,
                       label: str = "") -> dict:
    """Generate a timetable with the constraint solver and store it as a new version.
    semesters: e.g. [3] or [1,3]; None = whole university (semesters 1,3,5,7).
    method: decomposed (default, time-then-room CP-SAT) | exact (single CP-SAT model, small scopes) |
    ga (memetic genetic algorithm) | ga_plain (GA without local search) | vba (macro baseline).
    repair: run the LNS repair ladder if anything is left unplaced.
    stability_weight > 0 keeps sessions where the published timetable has them (minimal disruption).
    Returns coverage, hard violations, quality metrics and unplaced sessions."""
    return service().solve(semesters=semesters, method=method, time_limit_s=time_limit_s, repair=repair,
                           stability_weight=stability_weight, use_extended=use_extended, label=label)


@mcp.tool()
@A
def validate_timetable(solution_id: str = "latest") -> dict:
    """Independently re-check every hard rule (batch/faculty/room clashes, lunch, hours, block integrity,
    same-day repeats, room suitability, availability) and weekly allocation. solution_id may be a
    version id, 'latest', 'published' (the workbook grid) or 'vba' (macro baseline)."""
    return service().validate_solution(solution_id)


@mcp.tool()
@A
def get_solution_summary(solution_id: str = "latest") -> dict:
    """Headline numbers of a stored timetable version (coverage, violations, quality, repair rounds)."""
    return service().solution_summary(solution_id)


@mcp.tool()
@A
def view_timetable(kind: str, code: str, solution_id: str = "latest") -> dict:
    """Weekly grid for kind=batch|faculty|room|semester and its code (e.g. batch '1B1', faculty 'RAVI',
    room 'LT1', semester '3'). Works on 'published' and 'vba' too."""
    return service().view(kind, code, solution_id)


@mcp.tool()
@A
def explain_unplaced(solution_id: str = "latest", token: int | None = None) -> dict:
    """Why a requirement could not be placed: for each legal start, which batch/faculty/room blocks it,
    free 'safe havens', room changes that would unblock it, plus the pigeonhole capacity check."""
    return service().explain_unplaced(solution_id, token)


@mcp.tool()
@A
def move_session(session_id: str, day: str, start: int, room: str | None = None,
                 solution_id: str = "latest", commit: bool = True) -> dict:
    """What-if / manual edit: move one session (id like 'R104#0') to day+start slot (and optionally room).
    Reports exactly which hard rules it would break; commits a new version only if it breaks none."""
    return service().move_session(session_id, day, start, room, solution_id, commit)


@mcp.tool()
@A
def repair_timetable(solution_id: str = "latest", time_limit_s: float = 30, fix_violations: bool = False) -> dict:
    """Repair a timetable after new constraints (unavailability, pins) or with unplaced sessions:
    frees a neighbourhood around the problems and re-solves it with CP-SAT, escalating
    (local -> wider -> extended 16:00 slot). With fix_violations=True, every session involved in a hard
    violation found by an independent re-validation is also freed. Stores a new version."""
    return service().repair(solution_id, time_limit_s, fix_violations=fix_violations)


@mcp.tool()
@A
def pin_session(token: int, day: str, start: int, room: str | None = None, note: str = "") -> dict:
    """Fix one weekly session of requirement `token` at day/start (and room) for future solves."""
    return service().pin_session(token, day, start, room, note)


@mcp.tool()
@A
def list_pins() -> dict:
    """All pinned sessions."""
    return {"items": service().store.pins()}


@mcp.tool()
@A
def remove_pin(pin_id: int) -> dict:
    """Remove a pin by id."""
    return {"removed": service().store.remove_pin(pin_id)}


@mcp.tool()
@A
def compare_timetables(a: str = "published", b: str = "latest") -> dict:
    """Side-by-side metrics of two timetables (ids, 'latest', 'published', 'vba')."""
    return service().compare(a, b)


@mcp.tool()
@A
def publish_timetable(solution_id: str = "latest") -> dict:
    """Mark a VALID timetable version as the published one. Refuses invalid versions."""
    return service().publish(solution_id)


@mcp.tool()
@A
def export_timetable(solution_id: str = "latest", fmt: str = "xlsx", entity: str | None = None) -> dict:
    """Export: fmt=xlsx (workbook in the institution's Master format + per-semester sheets),
    ics (calendar; entity like 'batch:1B1' or 'faculty:RAVI'), json."""
    return service().export(solution_id, fmt, entity)


@mcp.tool()
@A
def list_solutions(limit: int = 15) -> dict:
    """Stored timetable versions, newest first."""
    return {"items": service().list_solutions(limit)}


@mcp.tool()
@A
def audit_log(n: int = 30) -> dict:
    """The most recent MCP tool calls across all servers (for traceability)."""
    return {"items": service().store.audit_tail(n)}


@mcp.resource("timetable://overview")
def overview_resource() -> str:
    """Institution overview as JSON."""
    return json.dumps(service().overview(), default=str)


@mcp.resource("timetable://solutions")
def solutions_resource() -> str:
    """Stored timetable versions as JSON."""
    return json.dumps(service().list_solutions(30), default=str)


@mcp.prompt()
def plan_semester(semester: int) -> str:
    """Prompt template: generate, validate and explain a semester timetable."""
    return (f"Generate the timetable for semester {semester}. First check capacity_report, then call "
            f"generate_timetable(semesters=[{semester}]), then validate_timetable. If anything is unplaced call "
            f"explain_unplaced and repair_timetable. Finish with a short summary of coverage, clashes and quality.")


if __name__ == "__main__":
    mcp.run()
