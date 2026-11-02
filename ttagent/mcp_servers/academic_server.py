"""Academic MCP server: read-only view of semesters, batches, courses and weekly requirements."""
from __future__ import annotations

from mcp.server.fastmcp import FastMCP

from ttagent.mcp_servers.common import audited, service

mcp = FastMCP("ttagent-academic")
A = audited("academic")


@mcp.tool()
@A
def get_overview() -> dict:
    """Semesters with their batches, courses and weekly teaching hours; elective baskets;
    external programmes; the weekly time grid (days, slot times, lunch)."""
    return service().overview()


@mcp.tool()
@A
def get_requirements(semester: int | None = None, batch: str | None = None, course: str | None = None,
                     faculty: str | None = None, kind: str | None = None, limit: int = 80) -> dict:
    """Weekly teaching requirements, filterable. kind: L (lecture), T (tutorial), P (practical/lab).
    Each row: token id, batches, course, faculty, requested venue, hours per session, sessions per week."""
    return service().list_requirements(semester, batch, course, faculty, kind, limit)


@mcp.tool()
@A
def get_batch_profile(batch: str) -> dict:
    """One batch (e.g. '1B1', '5B21'): its courses, weekly hours (strict and elective-aware) vs weekly capacity."""
    return service().batch_profile(batch)


@mcp.tool()
@A
def get_elective_baskets() -> dict:
    """Elective baskets: groups of courses that run in parallel for overlapping batches
    (students attend one). Inferred from the published grid; editable in data/processed/electives.yaml."""
    return {"baskets": service().list_baskets()}


if __name__ == "__main__":
    mcp.run()
