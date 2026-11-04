"""
Workbook -> Dataset.

Reads the *real* Master_TimeTable .xlsm exactly the way its VBA macro does
(same cell ranges, same 50-rows-per-day stacking), then adds the inference
the macro never had:

* room catalogue with categories and observed capacity (in batch units)
* faculty registry with placeholder detection (HSS*, OTH*, RSCH*, ECE*)
* elective baskets inferred from human co-scheduling in the published grid
* fixed external bookings (programmes not in the requirement table: BBA, MCA...)
* a reference schedule: the published grid mapped back to requirement tokens
"""
from __future__ import annotations

import itertools
import re
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

import networkx as nx
import openpyxl
from openpyxl.utils import column_index_from_string

from ttagent.config import settings, workbook_path
from ttagent.domain.models import (Batch, Booking, Dataset, ElectiveBasket, Faculty, Kind,
                                   ReferenceSession, Requirement, Room)
from ttagent.ingestion.token_parser import batch_parts, normalise_faculty, parse_token, room_category


class Issue(dict):
    def __init__(self, severity: str, category: str, message: str, location: str = "", **extra: Any):
        super().__init__(severity=severity, category=category, message=message, location=location, **extra)


def _col(letter: str) -> int:
    return column_index_from_string(letter)


def load_workbook_dataset(path: str | Path | None = None) -> Dataset:
    path = Path(path) if path else workbook_path()
    cfg = settings()
    wcfg, tg = cfg["workbook"], cfg["time_grid"]
    days: list[str] = tg["days"]
    wb = openpyxl.load_workbook(path, data_only=True, keep_vba=False)
    ws = wb[wcfg["main_sheet"]]
    issues: list[Issue] = []

    # ------------------------------------------------------------------ requirements
    c = {k: _col(v) for k, v in wcfg["requirement_columns"].items()}
    reqs: list[Requirement] = []
    row = wcfg["requirement_first_row"]
    declared = ws["N3"].value
    while True:
        tok = ws.cell(row, c["token"]).value
        if tok is None or str(tok).strip() == "":
            break
        batch_raw = str(ws.cell(row, c["batch"]).value or "").replace(" ", "")
        kind = str(ws.cell(row, c["type"]).value or "").strip().upper()
        course = str(ws.cell(row, c["course"]).value or "").strip()
        hours = ws.cell(row, c["hours"]).value
        venue = str(ws.cell(row, c["venue"]).value or "").strip().upper()
        fac_raw = str(ws.cell(row, c["faculty"]).value or "")
        freq = ws.cell(row, c["frequency"]).value
        sched = ws.cell(row, c["freq_scheduled"]).value
        loc = f"Main!K{row}"
        fac, notes = normalise_faculty(fac_raw)
        for n in notes:
            issues.append(Issue("info", "normalisation", f"token {tok}: {n}", loc))
        if kind not in ("L", "T", "P") or not batch_raw or not course or not venue or not fac:
            issues.append(Issue("error", "requirement_incomplete", f"token {tok} skipped: missing fields", loc))
            row += 1
            continue
        batches = batch_raw.split(",")
        sems = [batch_parts(b)[0] for b in batches if batch_parts(b)[0] is not None]
        sem = Counter(sems).most_common(1)[0][0] if sems else 0
        if len(set(sems)) > 1:
            issues.append(Issue("warning", "mixed_semester", f"token {tok} mixes semesters {sorted(set(sems))}", loc))
        reqs.append(Requirement(
            token=int(tok), batches=batches, kind=Kind(kind), course=course, duration=int(hours or 1),
            venue=venue, faculty=fac, per_week=int(freq or 1), semester=sem,
            program=batch_parts(batches[0])[1], source_row=row,
            macro_scheduled=int(sched) if isinstance(sched, (int, float)) else 0,
        ))
        row += 1
    if declared and int(declared) != len(reqs):
        issues.append(Issue("warning", "row_count", f"N3 declares {declared} tokens, parsed {len(reqs)}", "Main!N3"))

    dup = Counter(r.key for r in reqs)
    for k, n in dup.items():
        if n > 1:
            issues.append(Issue("warning", "duplicate_requirement", f"{n} requirement rows share key {k}"))

    req_batches = {b for r in reqs for b in r.batches}

    # ------------------------------------------------------------------ published grid
    first_row, rows_per_day = wcfg["grid_first_row"], wcfg["grid_rows_per_day"]
    first_col, n_slots = _col(wcfg["grid_first_col"]), wcfg["grid_slots"]
    published: list[Booking] = []
    seen_cells: set[tuple[str, int, str]] = set()
    for di, day in enumerate(days):
        for r in range(first_row + di * rows_per_day, first_row + (di + 1) * rows_per_day):
            for si in range(n_slots):
                raw = ws.cell(r, first_col + si).value
                if raw is None or str(raw).strip() == "":
                    continue
                slot = si + 1
                loc = f"Main!{openpyxl.utils.get_column_letter(first_col + si)}{r}"
                pt = parse_token(raw)
                if pt is None:
                    issues.append(Issue("warning", "unparseable_cell", f"ignored cell value {str(raw)!r}", loc))
                    continue
                for n in pt.notes:
                    sev = "warning" if pt.recovered else "info"
                    issues.append(Issue(sev, "normalisation", f"{pt.raw}: {n}", loc))
                k = (day, slot, pt.raw)
                if k in seen_cells:
                    issues.append(Issue("info", "duplicate_cell", f"{pt.raw} appears twice in {day} slot {slot}", loc))
                    continue
                seen_cells.add(k)
                published.append(Booking(day=day, start=slot, duration=1, batches=pt.batches,
                                         faculty=pt.faculty, room=pt.venue, course=pt.course,
                                         kind=Kind(pt.kind), origin="published", label=loc))

    # ------------------------------------------------------------------ catalogues
    placeholder_res = [re.compile(p) for p in cfg["faculty"]["placeholder_patterns"]]
    fac_codes = {f for r in reqs for f in r.faculty} | {f for b in published for f in b.faculty}
    faculty = {f: Faculty(code=f, is_placeholder=any(p.match(f) for p in placeholder_res)) for f in sorted(fac_codes)}
    for key, attr in (("unavailable", "unavailable"), ("avoid", "avoid")):
        for code, rules in (cfg["faculty"].get(key) or {}).items():
            if code in faculty:
                for rule in rules:
                    d, _, s = str(rule).partition(":")
                    getattr(faculty[code], attr).append((d, int(s) if s else None))

    room_ids = {r.venue for r in reqs} | {b.room for b in published}
    rooms: dict[str, Room] = {}
    for rid in sorted(room_ids):
        cat, lab = room_category(rid)
        cap = max([len(r.batches) for r in reqs if r.venue == rid] + [len(b.batches) for b in published if b.room == rid] + [1])
        rooms[rid] = Room(room_id=rid, category=cat, lab_type=lab, observed_batch_capacity=cap,
                          used_by_requirements=sum(1 for r in reqs if r.venue == rid))

    # optional real-world data: seat counts per room, students per batch
    from ttagent.config import ROOT
    import yaml as _yaml
    seats_file, sizes_file = ROOT / "config" / "rooms.yaml", ROOT / "config" / "batches.yaml"
    if seats_file.exists():
        for rid, seats in (_yaml.safe_load(seats_file.read_text()) or {}).items():
            if str(rid).upper() in rooms and seats:
                rooms[str(rid).upper()].seats = int(seats)
    sizes = (_yaml.safe_load(sizes_file.read_text()) or {}) if sizes_file.exists() else {}

    all_batches = req_batches | {b for bk in published for b in bk.batches}
    batches = {}
    for b in sorted(all_batches):
        sem, prog, _ = batch_parts(b)
        batches[b] = Batch(code=b, semester=sem, program=prog, in_requirements=b in req_batches,
                           size=int(sizes[b]) if b in sizes else None)

    # ------------------------------------------------------------------ fixed external bookings
    fixed = [b.model_copy(update={"origin": "fixed"}) for b in published
             if not (set(b.batches) & req_batches)]
    internal = [b for b in published if set(b.batches) & req_batches]

    # ------------------------------------------------------------------ elective baskets
    baskets = infer_baskets(internal, reqs, cfg["electives"])
    by_sem_course = {(bk.semester, c): bk.basket_id for bk in baskets for c in bk.courses}
    kinds = set(cfg["electives"]["kinds"])
    for r in reqs:
        if r.kind.value in kinds:
            r.basket_id = by_sem_course.get((r.semester, r.course))

    # ------------------------------------------------------------------ reference schedule
    reference, ref_issues = build_reference(internal, reqs)
    issues.extend(ref_issues)

    ds = Dataset(source=str(path.name), requirements=reqs, rooms=rooms, faculty=faculty, batches=batches,
                 baskets=baskets, fixed_bookings=fixed, published=published, reference=reference)
    from ttagent.ingestion.quality import build_quality_report  # local import: uses the engine
    ds.quality = build_quality_report(ds, issues)
    return ds


def infer_baskets(internal: list[Booking], reqs: list[Requirement], ecfg: dict) -> list[ElectiveBasket]:
    """Courses of the same semester whose batch sets overlap yet were deliberately
    co-scheduled by the timetable office are parallel electives. Connected
    components of that co-occurrence graph = elective baskets."""
    if not ecfg.get("infer_from_grid", True):
        return []
    kinds = set(ecfg.get("kinds", ["L"]))
    by_slot: dict[tuple[str, int], list[Booking]] = defaultdict(list)
    for b in internal:
        by_slot[(b.day, b.start)].append(b)
    G = nx.Graph()
    weight: Counter = Counter()
    req_courses = {(r.semester, r.course) for r in reqs}
    for cell in by_slot.values():
        for a, b in itertools.combinations(cell, 2):
            if a.course == b.course or a.kind.value not in kinds or b.kind.value not in kinds:
                continue
            if not set(a.batches) & set(b.batches):
                continue
            sa, sb = batch_parts(a.batches[0])[0], batch_parts(b.batches[0])[0]
            if sa != sb or (sa, a.course) not in req_courses or (sb, b.course) not in req_courses:
                continue
            u, v = f"{sa}:{a.course}", f"{sb}:{b.course}"
            G.add_edge(u, v)
            weight[tuple(sorted((u, v)))] += 1
    out = []
    for comp in sorted(nx.connected_components(G), key=lambda c: sorted(c)):
        ev = sum(w for (u, v), w in weight.items() if u in comp and v in comp)
        if ev < ecfg.get("min_cooccurrence", 1):
            continue
        sem = int(next(iter(comp)).split(":")[0])
        courses = sorted(n.split(":")[1] for n in comp)
        conf = "high" if ev >= 6 else ("medium" if ev >= 3 else "low")
        out.append(ElectiveBasket(basket_id=f"S{sem}-E{len([b for b in out if b.semester == sem]) + 1}",
                                  semester=sem, courses=courses, evidence=ev, confidence=conf,
                                  note="inferred: co-scheduled for overlapping batches in published grid"))
    return out


def build_reference(internal: list[Booking], reqs: list[Requirement]) -> tuple[list[ReferenceSession], list[Issue]]:
    issues: list[Issue] = []
    by_key = defaultdict(list)
    for r in reqs:
        by_key[r.key].append(r)
    cells: dict[tuple[int, str], dict[int, Booking]] = defaultdict(dict)
    unmatched: Counter = Counter()
    for b in internal:
        key = (",".join(b.batches), b.kind.value, b.course)
        rs = by_key.get(key)
        if not rs:
            unmatched[key] += 1
            continue
        cells[(rs[0].token, b.day)][b.start] = b
    for key, n in unmatched.items():
        issues.append(Issue("warning", "grid_without_requirement",
                            f"published grid has {n} cell(s) for {key[0]}:{key[1]}-{key[2]} with no matching requirement row (manual addition/split)"))
    ref: list[ReferenceSession] = []
    tok_req = {r.token: r for r in reqs}
    for (tok, day), slotmap in cells.items():
        r = tok_req[tok]
        slots = sorted(slotmap)
        runs, cur = [], [slots[0]]
        for s in slots[1:]:
            if s == cur[-1] + 1:
                cur.append(s)
            else:
                runs.append(cur)
                cur = [s]
        runs.append(cur)
        for run in runs:
            for i in range(0, len(run), r.duration):
                chunk = run[i:i + r.duration]
                b = slotmap[chunk[0]]
                if len(chunk) < r.duration:
                    issues.append(Issue("warning", "broken_lab_block",
                                        f"{r.code}: published {day} slot {chunk[0]} has {len(chunk)}h of a {r.duration}h block",
                                        b.label or ""))
                ref.append(ReferenceSession(token=tok, day=day, start=chunk[0], duration=len(chunk),
                                            room=b.room, faculty=b.faculty))
    return ref, issues
