"""Deterministic Markdown renderers: the Explainer's offline mode and its fallback when an LLM fails."""
from __future__ import annotations

from typing import Any


def _q(sol: dict) -> str:
    q = sol.get("quality") or {}
    return (f"idle gaps {q.get('batch_idle_gaps')}, faculty >3h-in-a-row {q.get('faculty_consecutive_excess')}, "
            f"Saturday hours {q.get('saturday_hours')}, 16:00+ hours {q.get('extended_hours_slot8plus')}, "
            f"room changes {q.get('room_changes')}")


def render(action: str, r: dict[str, Any]) -> str:
    fn = globals().get(f"_r_{action}", _r_generic)
    try:
        return fn(r)
    except Exception as e:  # rendering must never break the graph
        return _r_generic(r) + f"\n\n_(renderer note: {e})_"


def _r_generate(r):
    s = r.get("solution") or {}
    lines = [f"### Timetable {s.get('solution_id')} ({s.get('label')})"]
    if r.get("capacity", {}).get("provably_infeasible"):
        lines.append("> Capacity check: the request is **provably infeasible** -- some resource needs more hours than the week has.")
    lines.append(f"- Coverage: **{s.get('scheduled_hours')}/{s.get('required_hours')} h ({s.get('coverage_pct')}%)**, "
                 f"hard violations: **{sum((s.get('hard_violations') or {}).values())}**, valid: **{s.get('valid')}**")
    lines.append(f"- Quality: {_q(s)}")
    if s.get("used_extended_slot"):
        lines.append("- Note: the repair agent had to use the extended 16:00-17:00 slot for a few sessions.")
    for rd in r.get("repair_log", []):
        lines.append(f"- Repair round {rd.get('iteration')}: unplaced {rd.get('before')} -> {rd.get('after')}")
    if s.get("unplaced_count"):
        lines.append(f"- Still unplaced ({s['unplaced_count']}): " + "; ".join(u["code"] for u in s.get("unplaced", [])[:5]))
        for e in (r.get("diagnosis") or {}).get("explanations", [])[:3]:
            lines.append(f"  - {e['requirement']}: blockers {e['blocker_histogram']}; free slots {e['safe_havens'][:3] or 'none'}")
    ap = r.get("approval")
    if ap:
        lines.append(f"- Approval: {'published' if ap.get('approved') else 'not published (awaiting/declined)'}")
    if s.get("valid") and not ap:
        lines.append("- Next: review it (view a batch/faculty), then publish or export to Excel.")
    return "\n".join(lines)


_r_repair = _r_generate


def _r_what_if_unavailable(r):
    u = r.get("unavailability") or {}
    head = (f"### What-if: {u.get('kind')} {u.get('code')} unavailable on {u.get('day')}"
            f"{' slot ' + str(u.get('slot')) if u.get('slot') else ''}\n"
            f"- Sessions affected in the current timetable: {len(u.get('affected_sessions_in_latest') or [])}\n")
    return head + _r_generate(r)


def _r_validate(r):
    v = r.get("validation") or {}
    a = v.get("allocation") or {}
    lines = [f"### Validation of {v.get('solution_id')}: {'VALID' if v.get('valid') else 'INVALID'}",
             f"- Coverage {a.get('scheduled_hours')}/{a.get('required_hours')} h ({a.get('coverage_pct')}%), "
             f"under-allocated requirements: {a.get('under_allocated')}"]
    counts = v.get("counts") or {}
    if counts:
        lines.append("| rule | count |\n|---|---|\n" + "\n".join(f"| {k} | {n} |" for k, n in counts.items()))
    for x in (v.get("violations") or [])[:8]:
        lines.append(f"- {x['message']}")
    return "\n".join(lines)


def _r_view(r):
    v = r.get("view") or {}
    days = list((v.get("grid") or {}).keys())
    slots = sorted({s for d in days for s in v["grid"][d]})
    out = [f"### {v.get('kind')} {v.get('code')} -- {v.get('hours')} h/week ({v.get('solution')})",
           "| day | " + " | ".join(slots) + " |", "|---|" + "---|" * len(slots)]
    for d in days:
        out.append(f"| {d} | " + " | ".join("<br>".join(v['grid'][d].get(s, [])) for s in slots) + " |")
    return "\n".join(out)


def _r_explain_unplaced(r):
    d = r.get("diagnosis") or {}
    if d.get("message"):
        return d["message"]
    out = ["### Why these sessions could not be placed"]
    for e in d.get("explanations", []):
        out.append(f"**{e['requirement']}** ({e['placed_sessions']}/{e['required_sessions']} sessions placed, "
                   f"{e['candidate_starts']} legal start times)")
        out.append(f"- blocked by: {e['blocker_histogram']}")
        out.append(f"- free slots: {', '.join(e['safe_havens'][:6]) or 'none'}")
        if e.get("fixable_by_room_change"):
            f = e["fixable_by_room_change"][0]
            out.append(f"- would fit at {f['slot']} in another room: {', '.join(f['free_rooms'])}")
        for row in e.get("detail", [])[:4]:
            out.append(f"  - {row['slot']}: {', '.join(row['blocked_by'])}")
    cap = d.get("capacity_check") or {}
    out.append(f"- Pigeonhole capacity check: {'INFEASIBLE' if cap.get('provably_infeasible') else 'no resource is over capacity'}")
    return "\n".join(out)


def _r_compare(r):
    c = r.get("comparison") or {}
    a, b = c.get("a", {}).get("id"), c.get("b", {}).get("id")
    rows = "\n".join(f"| {x['metric']} | {x.get(list(x)[1])} | {x.get(list(x)[2])} |" for x in c.get("rows", []))
    return f"### {c.get('a', {}).get('label')} vs {c.get('b', {}).get('label')}\n| metric | {a} | {b} |\n|---|---|---|\n{rows}\n\n_{c.get('note', '')}_"


def _r_quality(r):
    q = r.get("quality") or {}
    s = q.get("summary", {})
    vb = q.get("vba_macro_bookkeeping", {})
    pv = q.get("published_grid_validation", {})
    return ("### Workbook data quality\n"
            f"- {s.get('requirements')} requirements, {s.get('teaching_hours_per_week')} teaching h/week, semesters {s.get('semesters')}, "
            f"{s.get('rooms')} rooms, {s.get('faculty')} faculty codes ({len(s.get('faculty_placeholders', []))} placeholders)\n"
            f"- Issues: {q.get('issue_counts', {}).get('by_category')}\n"
            f"- VBA macro left {vb.get('requirements_not_fully_scheduled')} requirements ({vb.get('hours_missing')} h) unscheduled\n"
            f"- Published grid hard violations: {pv.get('counts')}\n"
            f"- Elective baskets inferred: {[b['basket_id'] + ':' + '/'.join(b['courses']) for b in s.get('elective_baskets', [])]}")


def _r_capacity(r):
    c = r.get("capacity") or {}
    out = [f"### Capacity (pigeonhole) analysis -- weekly capacity {c.get('weekly_capacity_h')} h per resource",
           f"- Provably infeasible: **{c.get('provably_infeasible')}** ({len(c.get('infeasible_resources', []))} resources over capacity)"]
    for x in (c.get("batches_top") or [])[:3]:
        out.append(f"- batch {x['batch']}: {x['demand_h']}/{x['capacity_h']} h ({x['utilisation_pct']}%)")
    for x in (c.get("faculty_top") or [])[:3]:
        out.append(f"- faculty {x['faculty']}: {x['demand_h']}/{x['capacity_h']} h")
    for x in (c.get("room_pools_top") or [])[:3]:
        out.append(f"- room pool {x['pool']}: {x['demand_h']}/{x['capacity_h']} h")
    if c.get("requested_venues_oversubscribed"):
        out.append(f"- Requested venues oversubscribed (solver reassigns rooms): "
                   + ", ".join(f"{x['room']} {x['demand_h']}h" for x in c["requested_venues_oversubscribed"]))
    return "\n".join(out)


def _r_move(r):
    m = r.get("move") or {}
    if m.get("error"):
        return f"Move failed: {m['error']}"
    if m.get("legal"):
        return f"Moved **{m['requirement']}** to {m['moved_to']}. No hard rule broken; new version {m.get('new_solution')}."
    return (f"Moving **{m.get('requirement')}** to {m.get('moved_to')} would break: {m.get('introduces_violations')}\n"
            + "\n".join(f"- {x}" for x in m.get("new_violation_examples", [])) + "\nNothing was changed.")


def _r_export(r):
    e = r.get("export") or {}
    return e.get("error") or f"Exported {e.get('format')} for {e.get('solution_id')}: `{e.get('path')}`"


def _r_publish(r):
    p = r.get("publish") or {}
    return p.get("error") or f"Published timetable {p.get('published')} ({p.get('label')})."


def _r_generic(r):
    import json
    return "```json\n" + json.dumps(r, indent=1, default=str)[:3000] + "\n```"
