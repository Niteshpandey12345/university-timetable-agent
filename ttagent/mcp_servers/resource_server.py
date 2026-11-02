"""Resource MCP server: rooms, faculty, capacity proofs, availability and what-if unavailability."""
from __future__ import annotations

from mcp.server.fastmcp import FastMCP

from ttagent.mcp_servers.common import audited, service

mcp = FastMCP("ttagent-resource")
A = audited("resource")


@mcp.tool()
@A
def list_rooms(category: str | None = None) -> dict:
    """Rooms with category (lecture_theatre, classroom, computer_lab, special_lab, digital_lc)
    and observed capacity in batch units. Optional category filter."""
    rooms = service().list_rooms(category)
    return {"count": len(rooms), "rooms": rooms}


@mcp.tool()
@A
def list_faculty(include_placeholders: bool = True) -> dict:
    """Faculty codes with required weekly teaching hours. Placeholders (HSS*, OTH*, RSCH*, ECE*)
    are department pools, not individuals, and are never clash-checked."""
    fac = service().list_faculty(include_placeholders)
    return {"count": len(fac), "faculty": fac}


@mcp.tool()
@A
def get_faculty_profile(code: str) -> dict:
    """What one faculty member teaches, weekly load, and any extra unavailability recorded."""
    return service().faculty_profile(code)


@mcp.tool()
@A
def capacity_report(basket_aware: bool = True) -> dict:
    """Pigeonhole feasibility proof: weekly demand vs capacity for every batch, faculty member and room.
    If provably_infeasible is true no solver can place everything -- a policy change is required
    (parallel electives, more rooms, extended hours). basket_aware=False shows the strict model."""
    return service().capacity_report(basket_aware)


@mcp.tool()
@A
def find_free_slots(kind: str, code: str, solution_id: str = "latest") -> dict:
    """Free teaching hours for a batch | faculty | room in a given timetable (default: latest)."""
    return service().free_slots(kind, code, solution_id)


@mcp.tool()
@A
def add_unavailability(kind: str, code: str, day: str, slot: int | None = None, note: str = "") -> dict:
    """Record that a faculty | batch | room is unavailable on a day (slot=None => whole day).
    Returns sessions of the latest timetable that are now affected; call repair_timetable next."""
    return service().add_unavailability(kind, code, day, slot, note)


@mcp.tool()
@A
def list_unavailability() -> dict:
    """All recorded unavailability constraints."""
    return {"items": service().store.unavailability()}


@mcp.tool()
@A
def remove_unavailability(constraint_id: int) -> dict:
    """Delete an unavailability constraint by id."""
    return {"removed": service().store.remove_unavailability(constraint_id)}


if __name__ == "__main__":
    mcp.run()
