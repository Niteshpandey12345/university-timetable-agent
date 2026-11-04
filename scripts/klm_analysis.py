"""
Analytical usability evaluation with the Keystroke-Level Model (KLM; Card, Moran & Newell, 1980).

Predicts the error-free expert time for the five user-study tasks (docs/USER_STUDY.md) in the current Excel
workbook and in the agent system. Excel-side operator counts are derived from the real published grid
(entries per slot, candidate start slots, a teacher's sessions); agent-side response times R are measured
from the running system (scripts/klm_times.json, produced by timing the actual requests). It is a model-based
estimate that precedes, and does not replace, the user study.

KLM operators [s]: K keystroke 0.28 (average skilled typist), P point 1.1, B mouse button 0.1, H home hands 0.4,
M mental preparation / reading-judging one item 1.35, R(t) measured system response.

    python -m scripts.klm_analysis
"""
from __future__ import annotations

import json
from collections import Counter, defaultdict
from pathlib import Path

from ttagent.config import ROOT
from ttagent.ingestion.excel_loader import load_workbook_dataset

OUT = ROOT / "experiments" / "results" / "klm"
TIMES = OUT / "agent_response_times.json"
BASE = {"K": 0.28, "P": 1.1, "B": 0.1, "H": 0.4, "M": 1.35}


def t(ops: Counter, op: dict, R: float = 0.0) -> float:
    return sum(n * op[k] for k, n in ops.items()) + R


def typed(text: str) -> Counter:
    """Typing a request: home to keyboard, one mental preparation, the characters, Enter."""
    return Counter(H=1, M=1, K=len(text) + 1)


def data_counts(ds) -> dict:
    """Counts from the published (hand-edited) grid that bound the work of an expert using the workbook."""
    pub = list(ds.published)
    cell = Counter()                                     # entries per (day, slot) column segment
    for b in pub:
        for s in b.slots:
            cell[(b.day, s)] += 1
    sem3 = {r.token for r in ds.requirements if r.semester == 3}
    sem3_batches = {bt for r in ds.requirements if r.semester == 3 for bt in r.batches}
    # busiest real teacher of semester 3 and the weekday with most of their published sessions
    load = Counter()
    for r in ds.requirements:
        if r.semester == 3:
            for f in r.faculty:
                if not ds.is_placeholder(f):
                    load[f] += r.duration * r.per_week
    teacher = load.most_common(1)[0][0]
    tday = Counter(b.day for b in pub if teacher in b.faculty and b.day != "Sat").most_common(1)[0]
    lab = next(r for r in ds.requirements if "3B1" in r.batches and r.kind.value == "P")
    hits = lambda pred: sum(len(list(b.slots)) for b in pub if pred(b))
    return {"segments": len(cell), "entries": sum(cell.values()),
            "segments_sem3": len({k for b in pub if set(b.batches) & sem3_batches for k in [(b.day, s) for s in b.slots]}),
            "entries_in_sem3_segments": sum(n for k, n in cell.items() if any(set(b.batches) & sem3_batches and b.day == k[0] and k[1] in b.slots for b in pub)),
            "legal_starts_1h_lecture": 34, "resources_lecture": 5,
            "teacher": teacher, "teacher_day": tday[0], "displaced_sessions": tday[1],
            "lab": lab.code, "lab_hours": lab.duration, "lab_people": len(lab.batches) + len([f for f in lab.faculty if not ds.is_placeholder(f)]) + 1,
            "hits_JP": hits(lambda b: "JP" in b.faculty), "hits_LT1": hits(lambda b: b.room == "LT1"),
            "mean_entries_per_segment": round(sum(cell.values()) / len(cell), 1)}


def models(c: dict, R: dict) -> dict:
    """Operator counts per task. Excel: lower bound for an error-free expert. Agent: typed request + response + reading."""
    tasks = {}
    # T1 detect all clashes in semester 3: read every entry of every slot segment that holds a semester-3 class
    # (teacher and room clashes can involve other semesters), pointing once per segment
    ex = Counter(P=c["segments_sem3"], M=c["entries_in_sem3_segments"])
    ag = typed("Show all conflicts in the current timetable") + Counter(M=R["T1"]["lines"])
    tasks["T1 Find every clash in semester 3"] = (ex, 0.0, ag, R["T1"]["s"])
    # T2 diagnose a missing lecture: for each legal start, point to the slot and check 3 batches, 1 teacher, 1 room;
    # then look for another lecture theatre in the room-blocked starts (2 rooms each)
    ex = Counter(P=c["legal_starts_1h_lecture"], M=c["legal_starts_1h_lecture"] * c["resources_lecture"] + 30 * 2)
    ag = typed("Why could you not schedule the CMD lecture?") + Counter(M=R["T2"]["lines"])
    tasks["T2 Explain a missing lecture and where it could go"] = (ex, 0.0, ag, R["T2"]["s"])
    # T3 what-if: find the teacher's sessions that day (find dialog), re-place each with a T2-like search, edit
    # (select, cut, select, paste), and re-check the two touched segments
    k = c["displaced_sessions"]
    find = Counter(H=1, K=2 + len(c["teacher"]) + 1, P=1, B=2, M=k)
    search = Counter(P=34, M=34 * 5)
    edit = Counter(P=2, B=4, K=4)
    recheck = Counter(P=2, M=int(2 * c["mean_entries_per_segment"]))
    ex = find + sum((search + edit + recheck for _ in range(k)), Counter())
    ag = typed(f"{c['teacher']} is unavailable on Monday") + Counter(M=R["T3b"]["lines"], P=1, B=2)
    tasks[f"T3 Teacher absent for a day ({k} sessions displaced)"] = (ex, 0.0, ag, R["T3b_repair_s"])
    # T4 move a lab without a clash: check every hour of the lab for all its batches, teachers and room, then edit
    ex = Counter(P=c["lab_hours"], M=c["lab_hours"] * c["lab_people"]) + Counter(P=2, B=4, K=4)
    ag = typed("Show timetable of batch 3B1") + Counter(M=6) + typed("move R50#0 to Friday slot 3") + Counter(M=R["T4b"]["lines"])
    tasks["T4 Move a lab without creating a clash"] = (ex, 0.0, ag, R["T4a"]["s"] + R["T4b"]["s"])
    # T5 weekly look-ups: find dialog per code, read every hit
    ex = Counter(H=1, K=2 + 2 + 1, P=1, B=2, M=c["hits_JP"]) + Counter(K=2 + 3 + 1, P=1, B=2, M=c["hits_LT1"])
    ag = typed("show faculty JP timetable") + Counter(M=6) + typed("display room LT1") + Counter(M=6)
    tasks["T5 Weekly timetable of a teacher and a room"] = (ex, 0.0, ag, R["T5a"]["s"] + R["T5b"]["s"])
    return tasks


def main() -> None:
    ds = load_workbook_dataset()
    c = data_counts(ds)
    meas = json.loads(TIMES.read_text())
    R = {k: {"s": v["seconds"], "lines": max(1, v.get("answer_lines") or 6)} for k, v in meas.items() if isinstance(v, dict)}
    R["T4b"]["lines"] = 3
    R["T3b_repair_s"] = meas.get("T3b_repair_s", 20.0)
    T = models(c, R)
    grid = {"low": {"K": 0.20, "P": 1.0, "B": 0.1, "H": 0.36, "M": 1.2}, "base": BASE, "high": {"K": 0.50, "P": 1.2, "B": 0.1, "H": 0.44, "M": 1.5}}
    rows = []
    for name, (ex, rx, ag, ra) in T.items():
        row = {"task": name, "excel_ops": dict(ex), "agent_ops": dict(ag), "agent_R_s": ra}
        for g, op in grid.items():
            e, a = t(ex, op, rx), t(ag, op, ra)
            row[g] = {"excel_s": round(e, 1), "agent_s": round(a, 1), "ratio": round(e / a, 1)}
        rows.append(row)
    tot = {g: {"excel_s": round(sum(r[g]["excel_s"] for r in rows), 1), "agent_s": round(sum(r[g]["agent_s"] for r in rows), 1)} for g in grid}
    for g in tot:
        tot[g]["ratio"] = round(tot[g]["excel_s"] / tot[g]["agent_s"], 1)
    OUT.mkdir(parents=True, exist_ok=True)
    res = {"operators": grid, "data_counts": c, "tasks": rows, "total": tot,
           "note": "KLM predicts error-free expert time; Excel times are lower bounds (no errors, no re-reading)."}
    (OUT / "klm_results.json").write_text(json.dumps(res, indent=1), encoding="utf-8")
    lines = ["| Task | Excel [s] | Agent [s] | Ratio | Range of ratio |", "|---|---:|---:|---:|---:|"]
    for r in rows:
        lines.append(f"| {r['task']} | {r['base']['excel_s']} | {r['base']['agent_s']} | {r['base']['ratio']}× | {min(r['low']['ratio'], r['high']['ratio'])}–{max(r['low']['ratio'], r['high']['ratio'])}× |")
    lines.append(f"| **All five** | **{tot['base']['excel_s']}** | **{tot['base']['agent_s']}** | **{tot['base']['ratio']}×** | {min(tot['low']['ratio'], tot['high']['ratio'])}–{max(tot['low']['ratio'], tot['high']['ratio'])}× |")
    (OUT / "klm_table.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(json.dumps(c, indent=1)); print("\n".join(lines))


if __name__ == "__main__":
    main()
