"""
Excel export in the institution's own format.

Sheet 'Main' reproduces the workbook layout the VBA macros expect
(B2:J301, 50 stacking rows per day, cells as BATCHES:K-COURSE@ROOM_FACULTY),
so the result can be pasted straight back into the Master_TimeTable file and
its FacultyWise / VenueWise / SemesterWise macros keep working.
Further sheets: one per semester (readable grid), Faculty, Rooms, Unplaced,
Validation, Metrics.
"""
from __future__ import annotations

from collections import defaultdict
from pathlib import Path
from typing import Any

from openpyxl import Workbook
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter

from ttagent.domain.models import Booking, Dataset
from ttagent.domain.timegrid import TimeGrid

HEAD = PatternFill("solid", fgColor="1F3A5F")
LUNCH = PatternFill("solid", fgColor="E8E8E8")
KIND_FILL = {"L": "DCEBFA", "T": "E3F4E1", "P": "FCEBD5"}
THIN = Border(*(Side(style="thin", color="B0B0B0"),) * 4)


def _header(ws, row: int, values: list[str]) -> None:
    for i, v in enumerate(values, 1):
        c = ws.cell(row, i, v)
        c.font = Font(bold=True, color="FFFFFF")
        c.fill = HEAD
        c.alignment = Alignment(horizontal="center", vertical="center")


def export_xlsx(ds: Dataset, sol: dict[str, Any], bookings: list[Booking], path: Path) -> Path:
    grid = TimeGrid.from_settings(use_extended=True)
    wb = Workbook()

    # ---------------- Main (VBA-compatible token grid)
    ws = wb.active
    ws.title = "Main"
    ws.cell(1, 1, "")
    for s in range(1, 10):
        ws.cell(1, s + 1, f"Slot-{s}").font = Font(bold=True)
    by_cell: dict[tuple[str, int], list[str]] = defaultdict(list)
    for b in bookings:
        for s in b.slots:
            by_cell[(b.day, s)].append(b.code)
    for di, day in enumerate(grid.days):
        top = 2 + di * 50
        ws.cell(top, 1, day).font = Font(bold=True)
        for s in range(1, 10):
            for k, code in enumerate(sorted(by_cell[(day, s)])[:50]):
                ws.cell(top + k, s + 1, code)
    ws.column_dimensions["A"].width = 6
    for s in range(2, 11):
        ws.column_dimensions[get_column_letter(s)].width = 34

    # ---------------- per-semester readable grids
    sems = sorted({ds.batches[b].semester for bk in bookings for b in bk.batches
                   if b in ds.batches and ds.batches[b].in_requirements and ds.batches[b].semester})
    for sem in sems:
        sh = wb.create_sheet(f"Sem{sem}")
        _grid_sheet(sh, grid, [b for b in bookings if b.origin != "fixed" and any(
            ds.batches.get(x) and ds.batches[x].semester == sem for x in b.batches)], f"Semester {sem}", show_batches=True)

    # ---------------- faculty & rooms (long tables, filterable)
    for title, keyf in (("Faculty", lambda b: [f for f in b.faculty]), ("Rooms", lambda b: [b.room])):
        sh = wb.create_sheet(title)
        _header(sh, 1, [title[:-1] if title != "Faculty" else "Faculty", "Day", "Time", "Course", "Type", "Batches", "Room", "Faculty"])
        rows = []
        for b in bookings:
            for k in keyf(b):
                rows.append((k, grid.day_index(b.day), b.start, b))
        for i, (k, _, _, b) in enumerate(sorted(rows, key=lambda x: (x[0], x[1], x[2])), 2):
            vals = [k, b.day, grid.label(b.day, b.start, b.duration).split(" ")[1], b.course, b.kind.value,
                    ",".join(b.batches), b.room, ",".join(b.faculty)]
            for j, v in enumerate(vals, 1):
                sh.cell(i, j, v)
        sh.auto_filter.ref = f"A1:H{max(2, len(rows) + 1)}"
        for j, w in enumerate([10, 6, 13, 10, 6, 40, 9, 18], 1):
            sh.column_dimensions[get_column_letter(j)].width = w

    # ---------------- unplaced / validation / metrics
    sh = wb.create_sheet("Unplaced")
    _header(sh, 1, ["Token", "Requirement", "Reason"])
    for i, u in enumerate(sol.get("unplaced", []), 2):
        sh.cell(i, 1, u.get("token")); sh.cell(i, 2, u.get("code")); sh.cell(i, 3, u.get("reason", ""))
    sh.column_dimensions["B"].width = 60; sh.column_dimensions["C"].width = 50

    sh = wb.create_sheet("Validation")
    v = sol.get("validation", {})
    sh.cell(1, 1, "Valid").font = Font(bold=True); sh.cell(1, 2, str(v.get("valid")))
    sh.cell(2, 1, "Coverage %").font = Font(bold=True); sh.cell(2, 2, (v.get("allocation") or {}).get("coverage_pct"))
    r = 4
    _header(sh, r, ["Rule", "Count"])
    for k, n in (v.get("counts") or {}).items():
        r += 1; sh.cell(r, 1, k); sh.cell(r, 2, n)
    r += 2
    _header(sh, r, ["Rule", "Message"])
    for viol in v.get("violations", []):
        r += 1; sh.cell(r, 1, viol["rule"]); sh.cell(r, 2, viol["message"])
    sh.column_dimensions["A"].width = 26; sh.column_dimensions["B"].width = 100

    sh = wb.create_sheet("Metrics")
    _header(sh, 1, ["Metric", "Value"])
    i = 1
    for k, val in (sol.get("metrics") or {}).items():
        if isinstance(val, dict):
            val = ", ".join(f"{a}: {b}" for a, b in list(val.items())[:20])
        i += 1; sh.cell(i, 1, k); sh.cell(i, 2, str(val))
    sh.column_dimensions["A"].width = 30; sh.column_dimensions["B"].width = 100
    path.parent.mkdir(parents=True, exist_ok=True)
    wb.save(path)
    return path


def _grid_sheet(sh, grid: TimeGrid, bookings: list[Booking], title: str, show_batches: bool) -> None:
    sh.cell(1, 1, title).font = Font(bold=True, size=13)
    slots = list(range(1, 9))
    _header(sh, 2, ["Day"] + [grid.slot_times[s] for s in slots])
    cell: dict[tuple[str, int], list[Booking]] = defaultdict(list)
    for b in bookings:
        for s in b.slots:
            cell[(b.day, s)].append(b)
    for di, day in enumerate(grid.days):
        row = 3 + di
        c = sh.cell(row, 1, day)
        c.font = Font(bold=True)
        c.alignment = Alignment(vertical="center")
        max_lines = 1
        for j, s in enumerate(slots, 2):
            items = sorted(cell[(day, s)], key=lambda b: b.code)
            txt = "\n".join(f"{b.course}({b.kind.value}) {b.room} {'/'.join(b.faculty)}" + (f" [{','.join(b.batches)}]" if show_batches else "") for b in items)
            x = sh.cell(row, j, "LUNCH" if s == grid.lunch_slot else txt)
            x.alignment = Alignment(wrap_text=True, vertical="top")
            x.border = THIN
            if s == grid.lunch_slot:
                x.fill = LUNCH
            elif items:
                x.fill = PatternFill("solid", fgColor=KIND_FILL[items[0].kind.value])
            max_lines = max(max_lines, len(items))
        sh.row_dimensions[row].height = min(409, 15 * max_lines + 4)
    sh.column_dimensions["A"].width = 6
    for j in range(2, len(slots) + 2):
        sh.column_dimensions[get_column_letter(j)].width = 42
    sh.freeze_panes = "B3"
