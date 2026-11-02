"""Data MCP server: workbook ingestion and data-quality auditing."""
from __future__ import annotations

from mcp.server.fastmcp import FastMCP

from ttagent.mcp_servers.common import audited, service

mcp = FastMCP("ttagent-data")
A = audited("data")


@mcp.tool()
@A
def ingest_workbook(path: str | None = None) -> dict:
    """(Re)ingest the Master_TimeTable .xlsm workbook: parse the requirement table and the
    published grid, infer rooms/faculty/elective baskets/external bookings, store the dataset
    and return a summary with headline data-quality findings. path=None uses the configured workbook."""
    return service().ingest(path)


@mcp.tool()
@A
def data_quality_report(section: str | None = None) -> dict:
    """Data-quality report. section (optional) is one of: summary, issue_counts, vba_macro_bookkeeping,
    published_grid_validation, published_grid_metrics, capacity_strict_model, capacity_basket_aware."""
    return service().quality_report(section)


@mcp.tool()
@A
def list_data_issues(severity: str | None = None, category: str | None = None, limit: int = 30) -> dict:
    """List individual data issues (malformed tokens, faculty/venue drift between the requirement table
    and the published grid, unmatched grid entries...). Filter by severity (info|warning|error) or category."""
    issues = service().dataset().quality["issues"]
    sel = [i for i in issues if (severity is None or i["severity"] == severity) and (category is None or i["category"] == category)]
    return {"count": len(sel), "issues": sel[:limit]}


if __name__ == "__main__":
    mcp.run()
