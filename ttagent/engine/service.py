"""
TimetableService -- the one facade every outer layer uses (MCP servers, API,
scripts, experiments). Returns JSON-safe, *compact* dicts: results are meant to
be read by an LLM, so long lists are summarised and capped.
"""
from __future__ import annotations

import json
import time
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

import yaml

from ttagent.config import PROCESSED_DIR, ensure_dirs
from ttagent.domain.models import KIND_NAMES, Booking, Dataset
from ttagent.domain.timegrid import TimeGrid
from ttagent.engine.cpsat import SolveOptions
from ttagent.engine.diagnosis import capacity_analysis, explain_session
from ttagent.engine.metrics import compute_metrics, stability
from ttagent.engine.validator import annotate, assignments_to_bookings, validate
from ttagent.store.db import Store

PSEUDO = {"published": "the hand-edited grid in the workbook", "vba": "Python port of the VBA Allot macro"}


class ServiceError(ValueError):
    pass


class TimetableService:
    def __init__(self, store: Store | None = None) -> None:
        self.store = store or Store()
        self._ds: Dataset | None = None
        self._ds_ts = 0.0

    # ================================================================ data
    def dataset(self) -> Dataset:
        ts = self.store.updated("dataset")
        if self._ds is None or ts > self._ds_ts:
            raw = self.store.get("dataset")
            if raw is None:
                self.ingest()
                raw = self.store.get("dataset")
                ts = self.store.updated("dataset")
            self._ds = Dataset.model_validate(raw)
            self._ds_ts = ts
        return self._ds

    def ingest(self, path: str | None = None) -> dict[str, Any]:
        from ttagent.ingestion.excel_loader import load_workbook_dataset
        ensure_dirs()
        t0 = time.time()
        ds = load_workbook_dataset(path)
        ds = self._apply_curated_electives(ds)
        self.store.put("dataset", ds.model_dump(mode="json"))
        self._ds, self._ds_ts = ds, self.store.updated("dataset")
        (PROCESSED_DIR / "dataset.json").write_text(ds.model_dump_json(indent=1, exclude={"quality"}), encoding="utf-8")
        (PROCESSED_DIR / "quality_report.json").write_text(json.dumps(ds.quality, indent=1, default=str), encoding="utf-8")
        ev = PROCESSED_DIR / "electives.yaml"
        if not ev.exists() or not (yaml.safe_load(ev.read_text()) or {}).get("authoritative"):
            ev.write_text(yaml.safe_dump({
                "authoritative": False,
                "comment": "Inferred elective baskets. Edit and set authoritative: true to make the solver use your version.",
                "baskets": [b.model_dump() for b in ds.baskets]}, sort_keys=False), encoding="utf-8")
        q = ds.quality
        return {"ingested": ds.source, "seconds": round(time.time() - t0, 2), "summary": q["summary"] | {"elective_baskets": [
            f"{b.basket_id}: {', '.join(b.courses)}" for b in ds.baskets]},
            "issues_by_severity": q["issue_counts"]["by_severity"],
            "vba_macro_unscheduled_requirements": q["vba_macro_bookkeeping"]["requirements_not_fully_scheduled"],
            "published_grid_hard_violations": q["published_grid_validation"]["counts"],
            "strict_model_provably_infeasible": q["capacity_strict_model"]["provably_infeasible"],
            "basket_model_provably_infeasible": q["capacity_basket_aware"]["provably_infeasible"]}

    def _apply_curated_electives(self, ds: Dataset) -> Dataset:
        ev = PROCESSED_DIR / "electives.yaml"
        if not ev.exists():
            return ds
        doc = yaml.safe_load(ev.read_text()) or {}
        if not doc.get("authoritative"):
            return ds
        from ttagent.domain.models import ElectiveBasket
        ds.baskets = [ElectiveBasket(**b) for b in doc.get("baskets", [])]
        lookup = {(b.semester, c): b.basket_id for b in ds.baskets for c in b.courses}
        for r in ds.requirements:
            r.basket_id = lookup.get((r.semester, r.course)) if r.kind.value == "L" else None
        return ds

    def quality_report(self, section: str | None = None, max_issues: int = 40) -> dict[str, Any]:
        q = self.dataset().quality
        if section:
            if section not in q:
                raise ServiceError(f"unknown section {section!r}; choose from {sorted(q)}")
            return {section: q[section]}
        out = {k: v for k, v in q.items() if k != "issues"}
        issues = [i for i in q["issues"] if i["severity"] != "info"] + [i for i in q["issues"] if i["severity"] == "info"]
        out["issues_sample"] = issues[:max_issues]
        return out

    # ================================================================ academic
    def overview(self) -> dict[str, Any]:
        ds = self.dataset()
        per_sem = defaultdict(lambda: {"requirements": 0, "hours": 0, "batches": set(), "courses": set()})
        for r in ds.requirements:
            s = per_sem[r.semester]
            s["requirements"] += 1
            s["hours"] += r.weekly_hours
            s["batches"].update(r.batches)
            s["courses"].add(r.course)
        return {"source": ds.source,
                "semesters": {k: {"requirements": v["requirements"], "weekly_hours": v["hours"],
                                  "batches": sorted(v["batches"], key=_natural), "courses": sorted(v["courses"])}
                              for k, v in sorted(per_sem.items())},
                "elective_baskets": [b.model_dump() for b in ds.baskets],
                "external_programmes": sorted({b for fb in ds.fixed_bookings for b in fb.batches}),
                "time_grid": {"days": list(TimeGrid.from_settings().days), "slots": TimeGrid.from_settings().slot_times,
                              "lunch_slot": TimeGrid.from_settings().lunch_slot,
                              "weekly_teaching_hours_per_batch": TimeGrid.from_settings().capacity_hours()}}

    def list_requirements(self, semester: int | None = None, batch: str | None = None, course: str | None = None,
                          faculty: str | None = None, kind: str | None = None, limit: int = 80) -> dict[str, Any]:
        ds = self.dataset()
        rows = [r for r in ds.requirements
                if (semester is None or r.semester == semester) and (batch is None or batch in r.batches)
                and (course is None or r.course.upper() == course.upper())
                and (faculty is None or faculty.upper() in r.faculty) and (kind is None or r.kind.value == kind.upper())]
        return {"count": len(rows), "weekly_hours": sum(r.weekly_hours for r in rows),
                "requirements": [{"token": r.token, "code": r.code, "kind": KIND_NAMES[r.kind.value], "course": r.course,
                                  "batches": r.batches, "faculty": r.faculty, "venue": r.venue,
                                  "hours_per_session": r.duration, "sessions_per_week": r.per_week,
                                  "elective_basket": r.basket_id} for r in rows[:limit]],
                "truncated": len(rows) > limit}

    def batch_profile(self, batch: str) -> dict[str, Any]:
        ds = self.dataset()
        if batch not in ds.batches:
            raise ServiceError(f"unknown batch {batch!r}")
        cap = capacity_analysis(ds)
        reqs = [r for r in ds.requirements if batch in r.batches]
        row = next((b for b in cap["batches_top"] if b["batch"] == batch), None)
        from ttagent.engine.diagnosis import batch_demand
        d = batch_demand(ds)[batch]
        return {"batch": batch, "semester": ds.batches[batch].semester, "requirements": len(reqs),
                "strict_weekly_hours": sum(r.weekly_hours for r in reqs),
                "basket_aware_weekly_hours": d["hours"] + d["basket_hours"],
                "weekly_capacity_hours": cap["weekly_capacity_h"],
                "electives": sorted({r.basket_id for r in reqs if r.basket_id}),
                "courses": sorted({f"{r.course}({r.kind.value})" for r in reqs}), "capacity_row": row}

    def list_baskets(self) -> list[dict[str, Any]]:
        ds = self.dataset()
        return [b.model_dump() | {"requirements": [r.code for r in ds.requirements if r.basket_id == b.basket_id]}
                for b in ds.baskets]

    # ================================================================ resources
    def list_rooms(self, category: str | None = None) -> list[dict[str, Any]]:
        ds = self.dataset()
        return [r.model_dump() for r in ds.rooms.values() if category is None or r.category == category]

    def list_faculty(self, include_placeholders: bool = True) -> list[dict[str, Any]]:
        ds = self.dataset()
        load = Counter()
        for r in ds.requirements:
            for f in r.faculty:
                load[f] += r.weekly_hours
        return [{"code": f.code, "placeholder": f.is_placeholder, "weekly_hours_required": load[f.code],
                 "unavailable": f.unavailable} for f in ds.faculty.values() if include_placeholders or not f.is_placeholder]

    def faculty_profile(self, code: str) -> dict[str, Any]:
        ds = self.dataset()
        code = code.upper()
        if code not in ds.faculty:
            raise ServiceError(f"unknown faculty {code!r}")
        reqs = [r for r in ds.requirements if code in r.faculty]
        return {"faculty": code, "placeholder": ds.faculty[code].is_placeholder,
                "weekly_hours": sum(r.weekly_hours for r in reqs),
                "teaches": [r.code for r in reqs], "extra_unavailability": [u for u in self.store.unavailability() if u["code"] == code]}

    def capacity_report(self, basket_aware: bool = True) -> dict[str, Any]:
        return capacity_analysis(self.dataset(), basket_aware=basket_aware)

    def add_unavailability(self, kind: str, code: str, day: str, slot: int | None = None, note: str = "") -> dict[str, Any]:
        ds = self.dataset()
        kind = kind.lower()
        code = code.upper() if kind == "faculty" else code
        valid = {"faculty": ds.faculty, "batch": ds.batches, "room": ds.rooms}
        if kind not in valid or code not in valid[kind]:
            raise ServiceError(f"unknown {kind} {code!r}")
        if day not in TimeGrid.from_settings().days:
            raise ServiceError(f"unknown day {day!r}")
        uid = self.store.add_unavailability(kind, code, day, slot, note)
        return {"id": uid, "kind": kind, "code": code, "day": day, "slot": slot,
                "affected_sessions_in_latest": self._affected(kind, code, day, slot)}

    def _affected(self, kind, code, day, slot, sol: dict | None = None) -> list[str]:
        sol = sol or self.store.load_solution("latest")
        if not sol:
            return []
        ds = self.dataset()
        out = []
        for b in assignments_to_bookings(ds, sol["assignments"], include_fixed=False):
            hit = (kind == "faculty" and code in b.faculty) or (kind == "batch" and code in b.batches) or (kind == "room" and b.room == code)
            if hit and b.day == day and (slot is None or slot in b.slots):
                out.append(b.session_id)
        return out

    def free_slots(self, kind: str, code: str, solution_id: str = "latest") -> dict[str, Any]:
        bookings = self.bookings(solution_id)
        grid = TimeGrid.from_settings()
        busy = set()
        for b in bookings:
            if (kind == "faculty" and code.upper() in b.faculty) or (kind == "batch" and code in b.batches) or (kind == "room" and b.room == code):
                busy.update((b.day, s) for s in b.slots)
        free = {d: [grid.slot_times[s] for s in grid.slots_for(d) if (d, s) not in busy] for d in grid.days}
        return {"kind": kind, "code": code, "solution": solution_id, "free": free,
                "free_hours": sum(len(v) for v in free.values()), "busy_hours": len(busy)}

    # ================================================================ timetable
    def solve(self, semesters: list[int] | None = None, method: str = "decomposed", time_limit_s: float = 60,
              repair: bool = True, stability_weight: float | None = None, use_baskets: bool = True,
              room_mode: str = "flex", use_extended: bool = False, label: str = "", seed: int = 7,
              workers: int = 8, repair_time_s: float | None = None, parent: str | None = None,
              extra_forbid: list[dict] | None = None) -> dict[str, Any]:
        ds = self.dataset()
        faults = _faults()
        if faults.get("crash") and _FAULT_STATE["crashes"] < faults["crash"]:
            _FAULT_STATE["crashes"] += 1
            raise ServiceError("solver process crashed (injected fault)")
        if faults.get("budget"):          # degraded compute: tiny budget, one core, no internal repair
            time_limit_s, repair, workers = float(faults["budget"]), False, 1
        pins = [{"token": p["token"], "day": p["day"], "start": p["start"], "room": p["room"]} for p in self.store.pins()]
        forbid = [{u["kind"]: u["code"], "day": u["day"], "slot": u["slot"]} for u in self.store.unavailability()] + (extra_forbid or [])
        opts = SolveOptions(semesters=semesters, time_limit_s=time_limit_s, use_baskets=use_baskets, room_mode=room_mode,
                            use_extended=use_extended, stability=stability_weight, pins=pins, forbid=forbid, seed=seed,
                            workers=workers)
        t0 = time.time()
        if method == "vba":
            from ttagent.engine.greedy_vba import solve_greedy_vba
            res = solve_greedy_vba(ds, include_fixed=True, semesters=semesters)
            repair = False
        elif method == "exact":
            from ttagent.engine.cpsat import solve
            res = solve(ds, opts)
        elif method == "decomposed":
            res = robust_pipeline(ds, opts, repair=False)
        elif method in ("ga", "ga_plain"):
            from ttagent.engine.ga import GAOptions, solve_ga
            res = solve_ga(ds, GAOptions(time_limit_s=time_limit_s, seed=seed, semesters=semesters,
                                         local_search=method == "ga", use_baskets=use_baskets))
        else:
            raise ServiceError("method must be one of: decomposed, exact, ga, ga_plain, vba")
        if repair and res["unplaced"]:
            from ttagent.engine.repair import lns_repair
            res = lns_repair(ds, res, opts, time_limit_s=repair_time_s or max(15, time_limit_s / 3))
        return self._finalise(res, label or f"{method} sem={semesters or 'all'}", parent, time.time() - t0)

    def _finalise(self, res: dict[str, Any], label: str, parent: str | None, wall: float) -> dict[str, Any]:
        ds = self.dataset()
        grid = TimeGrid.from_settings(use_extended=bool(res.get("options", {}).get("use_extended")))
        bookings = assignments_to_bookings(ds, res["assignments"])
        scope = set(res.get("scope_tokens") or [r.token for r in ds.requirements])
        v = validate(ds, bookings, grid, scope_tokens=scope)
        res["validation"] = _compact_validation(v)
        res["metrics"] = compute_metrics(ds, bookings, grid) | {"stability": stability(ds, res["assignments"])}
        res.setdefault("stats", {})["total_wall_s"] = round(wall, 2)
        k = _faults().get("corrupt")
        if k and not label.startswith("move ") and not _FAULT_STATE["corrupted"]:
            _FAULT_STATE["corrupted"] = 1                  # once per server process (one evaluation task / session)
            # evaluation only: silently corrupt k sessions AFTER the self-check, so the stored self-report
            # says "valid" while the timetable has clashes (a buggy or compromised scheduling tool)
            res["assignments"] = _corrupt(ds, res["assignments"], int(k), seed=len(res["assignments"]))
            res["stats"]["fault_injected"] = f"corrupt:{k}"
        sid = self.store.save_solution(res, label=label, parent=parent)
        return self.solution_summary(sid)

    def solution_summary(self, solution_id: str = "latest") -> dict[str, Any]:
        sol = self._load(solution_id)
        v, m = sol["validation"], sol["metrics"]
        repair = (sol["stats"] or {}).get("repair")
        return {"solution_id": sol["id"], "label": sol["label"], "method": sol["method"], "status": sol["status"],
                "valid": v.get("valid"), "coverage_pct": v.get("allocation", {}).get("coverage_pct"),
                "required_hours": v.get("allocation", {}).get("required_hours"),
                "scheduled_hours": v.get("allocation", {}).get("scheduled_hours"),
                "hard_violations": {k: n for k, n in v.get("counts", {}).items() if k.startswith("H")},
                "external_issues": {k: n for k, n in v.get("counts", {}).items() if k.startswith("X")},
                "unplaced": [{"token": u["token"], "code": u["code"], "reason": u.get("reason")} for u in sol["unplaced"][:15]],
                "unplaced_count": len(sol["unplaced"]),
                "used_extended_slot": bool(sol["options"].get("use_extended")),
                "quality": {k: m.get(k) for k in ("soft_penalty", "batch_idle_gaps", "batch_day_overload_hours",
                                                  "faculty_consecutive_excess", "faculty_day_overload_hours",
                                                  "saturday_hours", "late_hours_slot7plus", "extended_hours_slot8plus",
                                                  "room_changes", "weekday_imbalance")},
                "stability_vs_published": m.get("stability"),
                "repair_rounds": (repair or {}).get("rounds"),
                "seconds": (sol["stats"] or {}).get("total_wall_s"), "published": bool(sol.get("published"))}

    def _load(self, solution_id: str) -> dict[str, Any]:
        if solution_id in PSEUDO:
            return self._pseudo(solution_id)
        sol = self.store.load_solution(solution_id)
        if sol is None:
            raise ServiceError(f"no solution {solution_id!r}" + (" -- run solve first" if solution_id == "latest" else ""))
        return sol

    def _pseudo(self, name: str) -> dict[str, Any]:
        cached = self.store.get(f"pseudo:{name}")
        if cached and cached.get("dataset_ts") == self.store.updated("dataset"):
            return cached["sol"]
        ds = self.dataset()
        if name == "published":
            bookings = list(ds.published)
            v = validate(ds, bookings)
            sol = {"id": "published", "label": PSEUDO[name], "method": "manual+vba", "status": "PUBLISHED",
                   "options": {"use_extended": False}, "assignments": [], "unplaced": v["allocation"]["under"],
                   "stats": {}, "validation": _compact_validation(v), "metrics": compute_metrics(ds, bookings), "published": 0}
        else:
            from ttagent.engine.greedy_vba import solve_greedy_vba
            res = solve_greedy_vba(ds, include_fixed=True)
            bookings = assignments_to_bookings(ds, res["assignments"])
            v = validate(ds, bookings)
            sol = {"id": "vba", "label": PSEUDO[name], "method": "greedy_vba", "status": "HEURISTIC",
                   "options": {"use_extended": False}, "assignments": res["assignments"], "unplaced": res["unplaced"],
                   "stats": res["stats"], "validation": _compact_validation(v),
                   "metrics": compute_metrics(ds, bookings) | {"stability": stability(ds, res["assignments"])}, "published": 0}
        self.store.put(f"pseudo:{name}", {"dataset_ts": self.store.updated("dataset"), "sol": sol})
        return sol

    def bookings(self, solution_id: str = "latest") -> list[Booking]:
        ds = self.dataset()
        if solution_id == "published":
            return annotate(ds, ds.published)
        sol = self._load(solution_id)
        return assignments_to_bookings(ds, sol["assignments"])

    def validate_solution(self, solution_id: str = "latest", max_violations: int = 30) -> dict[str, Any]:
        sol = self._load(solution_id)
        ds = self.dataset()
        grid = TimeGrid.from_settings(use_extended=bool(sol["options"].get("use_extended")))
        v = validate(ds, self.bookings(solution_id), grid,
                     scope_tokens=None if solution_id == "published" else {a["token"] for a in sol["assignments"]} | {u["token"] for u in sol["unplaced"]})
        out = _compact_validation(v, max_violations)
        out["solution_id"] = sol["id"]
        out["interpretation"] = ("VALID: no hard constraint broken and every required hour is scheduled." if out["valid"] else
                                 "INVALID: see counts; H* are hard-rule breaches, under_allocated are missing hours.")
        return out

    def metrics(self, solution_id: str = "latest") -> dict[str, Any]:
        return self._load(solution_id)["metrics"]

    def view(self, kind: str, code: str, solution_id: str = "latest") -> dict[str, Any]:
        """Weekly grid for a batch | faculty | room | semester."""
        ds = self.dataset()
        grid = TimeGrid.from_settings(use_extended=True)
        sel = []
        for b in self.bookings(solution_id):
            if kind == "batch" and code in b.batches:
                sel.append(b)
            elif kind == "faculty" and code.upper() in b.faculty:
                sel.append(b)
            elif kind == "room" and b.room == code.upper():
                sel.append(b)
            elif kind == "semester" and any((ds.batches.get(x) and ds.batches[x].semester == int(code)) for x in b.batches) and b.origin != "fixed":
                sel.append(b)
        if kind not in ("batch", "faculty", "room", "semester"):
            raise ServiceError("kind must be batch | faculty | room | semester")
        table: dict[str, dict[str, list[str]]] = {d: {} for d in grid.days}
        for b in sorted(sel, key=lambda b: (grid.day_index(b.day), b.start)):
            for s in b.slots:
                label = f"{b.course}({b.kind.value}) {b.room} {'/'.join(b.faculty)}" + ("" if kind == "batch" else f" [{','.join(b.batches)}]")
                table[b.day].setdefault(grid.slot_times[s], []).append(label)
        return {"kind": kind, "code": code, "solution": solution_id, "sessions": len(sel),
                "hours": sum(b.duration for b in sel), "grid": table}

    def explain_unplaced(self, solution_id: str = "latest", token: int | None = None) -> dict[str, Any]:
        sol = self._load(solution_id)
        ds = self.dataset()
        tokens = [token] if token is not None else sorted({u["token"] for u in sol["unplaced"]})[:5]
        if not tokens:
            return {"solution_id": sol["id"], "message": "Nothing is unplaced in this solution."}
        bookings = self.bookings(solution_id)
        grid = TimeGrid.from_settings(use_extended=bool(sol["options"].get("use_extended")))
        forbid = [{u["kind"]: u["code"], "day": u["day"], "slot": u["slot"]} for u in self.store.unavailability()]
        out = []
        for t in tokens:
            e = explain_session(ds, int(t), bookings, grid, max_rows=12, forbid=forbid)
            out.append(e)
        return {"solution_id": sol["id"], "explanations": out,
                "capacity_check": {k: v for k, v in capacity_analysis(ds).items() if k in ("provably_infeasible", "infeasible_resources")}}

    def move_session(self, session_id: str, day: str, start: int, room: str | None = None,
                     solution_id: str = "latest", commit: bool = True) -> dict[str, Any]:
        """What-if / manual edit: move one session and report exactly what breaks."""
        sol = self._load(solution_id)
        ds = self.dataset()
        assigns = [dict(a) for a in sol["assignments"]]
        target = next((a for a in assigns if a["session_id"] == session_id), None)
        if target is None:
            raise ServiceError(f"session {session_id!r} not in solution {sol['id']}")
        before = validate(ds, assignments_to_bookings(ds, assigns))
        target.update(day=day, start=int(start), room=(room or target["room"]).upper())
        after = validate(ds, assignments_to_bookings(ds, assigns))
        new = Counter(after["counts"]) - Counter(before["counts"])
        introduced = {k: v for k, v in new.items() if k.startswith("H")}
        result = {"session_id": session_id, "requirement": ds.req(target["token"]).code,
                  "moved_to": TimeGrid.from_settings(True).label(day, int(start), ds.req(target["token"]).duration) + f" in {target['room']}",
                  "introduces_violations": introduced,
                  "new_violation_examples": [v["message"] for v in after["violations"] if v["rule"] in introduced][:8],
                  "legal": not introduced}
        if commit and not introduced:
            res = {"method": f"{sol['method']}+edit", "status": "EDITED", "assignments": assigns, "unplaced": sol["unplaced"],
                   "options": sol["options"], "stats": {},
                   "scope_tokens": sorted({a["token"] for a in assigns} | {u["token"] for u in sol["unplaced"]})}
            result["new_solution"] = self._finalise(res, f"move {session_id} -> {day}:{start}", sol["id"], 0)["solution_id"]
        return result

    def violating_sessions(self, solution_id: str = "latest") -> list[str]:
        """Solver-placed sessions involved in any hard violation, from a fresh, independent validation."""
        sol = self._load(solution_id)
        ds = self.dataset()
        grid = TimeGrid.from_settings(use_extended=bool(sol["options"].get("use_extended")))
        v = validate(ds, self.bookings(solution_id), grid, max_examples=100000,
                     scope_tokens={a["token"] for a in sol["assignments"]} | {u["token"] for u in sol["unplaced"]})
        mine = {a["session_id"] for a in sol["assignments"]}
        return sorted({sid for x in v["violations"] if x["rule"].startswith("H") for sid in x.get("sessions", []) if sid in mine})

    def repair(self, solution_id: str = "latest", time_limit_s: float = 30, free_sessions: list[str] | None = None,
               fix_violations: bool = False) -> dict[str, Any]:
        from ttagent.engine.repair import lns_repair
        sol = self._load(solution_id)
        ds = self.dataset()
        if fix_violations:
            free_sessions = sorted(set(free_sessions or []) | set(self.violating_sessions(solution_id)))
        pins = [{"token": p["token"], "day": p["day"], "start": p["start"], "room": p["room"]} for p in self.store.pins()]
        forbid = [{u["kind"]: u["code"], "day": u["day"], "slot": u["slot"]} for u in self.store.unavailability()]
        # sessions that now violate a what-if unavailability must move
        broken = set(free_sessions or [])
        for u in self.store.unavailability():
            broken.update(self._affected(u["kind"], u["code"], u["day"], u["slot"], sol=sol))   # the version being repaired
        assigns = [a for a in sol["assignments"] if a["session_id"] not in broken]
        unplaced = sol["unplaced"] + [{"session_id": a["session_id"], "token": a["token"], "code": ds.req(a["token"]).code,
                                       "reason": "displaced by new constraint"} for a in sol["assignments"] if a["session_id"] in broken]
        scope = sorted({a["token"] for a in sol["assignments"]} | {u["token"] for u in sol["unplaced"]})
        res = {"method": sol["method"], "status": sol["status"], "assignments": assigns, "unplaced": unplaced,
               "options": sol["options"], "scope_tokens": scope, "stats": {}}
        out = lns_repair(ds, res, SolveOptions(pins=pins, forbid=forbid), time_limit_s=time_limit_s)
        return self._finalise(out, f"repair of {sol['id']}", sol["id"], out["repair"]["wall_s"])

    def pin_session(self, token: int, day: str, start: int, room: str | None = None, note: str = "") -> dict[str, Any]:
        ds = self.dataset()
        r = ds.req(int(token))
        grid = TimeGrid.from_settings()
        if start not in grid.starts_for(day, r.duration):
            raise ServiceError(f"{r.code} ({r.duration}h) cannot start at {day} slot {start}; legal starts: {grid.starts_for(day, r.duration)}")
        pid = self.store.add_pin(int(token), day, int(start), room, note)
        return {"pin_id": pid, "requirement": r.code, "at": grid.label(day, int(start), r.duration), "room": room or "solver's choice",
                "note": "Pins apply to the next solve/repair."}

    def compare(self, a: str = "published", b: str = "latest") -> dict[str, Any]:
        sa, sb = self._load(a), self._load(b)
        keys = ["soft_penalty", "batch_idle_gaps", "batch_day_overload_hours", "faculty_consecutive_excess",
                "faculty_day_overload_hours", "saturday_hours", "late_hours_slot7plus", "extended_hours_slot8plus",
                "room_changes", "weekday_imbalance"]
        rows = []
        ka, kb = sa["id"], sb["id"] if sb["id"] != sa["id"] else sb["id"] + "'"
        cov = lambda s: s["validation"].get("allocation", {}).get("coverage_pct")  # noqa: E731
        hard = lambda s: sum(n for k, n in s["validation"].get("counts", {}).items() if k.startswith("H"))  # noqa: E731
        rows.append({"metric": "coverage_pct", ka: cov(sa), kb: cov(sb)})
        rows.append({"metric": "hard_violations", ka: hard(sa), kb: hard(sb)})
        for k in keys:
            rows.append({"metric": k, ka: sa["metrics"].get(k), kb: sb["metrics"].get(k)})
        scope_a = sa["validation"].get("allocation", {}).get("required_hours")
        scope_b = sb["validation"].get("allocation", {}).get("required_hours")
        note = "lower is better for every metric except coverage_pct"
        if scope_a != scope_b:
            note += f". Scopes differ ({scope_a} h vs {scope_b} h required): absolute counts are not directly comparable"
        return {"a": {"id": ka, "label": sa["label"]}, "b": {"id": kb, "label": sb["label"]}, "rows": rows, "note": note}

    def publish(self, solution_id: str = "latest") -> dict[str, Any]:
        sol = self._load(solution_id)
        if not sol["validation"].get("valid"):
            raise ServiceError(f"solution {sol['id']} is not valid; repair it before publishing")
        self.store.mark_published(sol["id"])
        return {"published": sol["id"], "label": sol["label"]}

    def export(self, solution_id: str = "latest", fmt: str = "xlsx", entity: str | None = None) -> dict[str, Any]:
        sol = self._load(solution_id)
        ds = self.dataset()
        out_dir = Path(PROCESSED_DIR.parent / "exports")
        out_dir.mkdir(parents=True, exist_ok=True)
        if fmt == "xlsx":
            from ttagent.export.excel import export_xlsx
            path = export_xlsx(ds, sol, self.bookings(solution_id), out_dir / f"timetable_{sol['id']}.xlsx")
        elif fmt == "ics":
            from ttagent.export.ics import export_ics
            if not entity:
                raise ServiceError("ics export needs entity, e.g. 'batch:1B1' or 'faculty:RAVI'")
            path = export_ics(ds, self.bookings(solution_id), entity, out_dir / f"{entity.replace(':', '_')}_{sol['id']}.ics")
        elif fmt == "json":
            path = out_dir / f"timetable_{sol['id']}.json"
            path.write_text(json.dumps(sol, indent=1, default=str), encoding="utf-8")
        else:
            raise ServiceError("fmt must be xlsx | ics | json")
        return {"path": str(path), "solution_id": sol["id"], "format": fmt}

    def list_solutions(self, limit: int = 15) -> list[dict[str, Any]]:
        return self.store.list_solutions(limit)


_FAULT_STATE = {"crashes": 0, "corrupted": 0}


def _faults() -> dict[str, float]:
    """Fault injection for the agent evaluation, e.g. TTAGENT_FAULT="corrupt:3,crash:1,budget:2". Off by default."""
    import os
    out = {}
    for part in filter(None, os.getenv("TTAGENT_FAULT", "").split(",")):
        k, _, v = part.partition(":")
        out[k.strip()] = float(v or 1)
    return out


def _corrupt(ds, assigns: list[dict], k: int, seed: int = 0) -> list[dict]:
    """Move k solver-placed sessions onto a slot used by another session of the same teacher or batch."""
    import random
    rng = random.Random(seed)
    out = [dict(a) for a in assigns]
    idx = [i for i, a in enumerate(out) if a.get("origin", "solver") == "solver"]
    rng.shuffle(idx)
    done = 0
    for i in idx:
        if done >= k:
            break
        a = out[i]
        ra = ds.req(a["token"])
        people = {f for f in ra.faculty if not ds.is_placeholder(f)} | set(ra.batches)
        mates = [b for b in out if b["token"] != a["token"] and b["day"] != a["day"]
                 and people & ({f for f in ds.req(b["token"]).faculty if not ds.is_placeholder(f)} | set(ds.req(b["token"]).batches))
                 and ds.req(b["token"]).duration == ra.duration
                 and not (ra.basket_id and ra.basket_id == ds.req(b["token"]).basket_id)]
        if mates:
            b = rng.choice(mates)
            a["day"], a["start"] = b["day"], b["start"]
            done += 1
    return out


def robust_pipeline(ds, opts: SolveOptions, repair: bool = True, repair_time_s: float | None = None) -> dict[str, Any]:
    """Decomposed CP-SAT with a fallback: if the time phase returns no solution at all (e.g. a single CP-SAT
    worker that cannot find a first solution in time), start from a short GA run instead, then repair."""
    from ttagent.engine.decomposed import solve_decomposed
    res = solve_decomposed(ds, opts)
    if not res["assignments"] and res["status"] not in ("INFEASIBLE", "MODEL_INVALID"):
        from ttagent.engine.ga import GAOptions, solve_ga
        g = solve_ga(ds, GAOptions(time_limit_s=max(10.0, opts.time_limit_s * 0.4), seed=opts.seed,
                                   semesters=opts.semesters, tokens=opts.tokens, use_baskets=opts.use_baskets,
                                   forbid=list(opts.forbid or [])))
        g.setdefault("stats", {})["fallback"] = f"time phase {res['status']} -> GA start"
        g["options"] = res.get("options", {})
        res = g
    if repair and res["unplaced"]:
        from ttagent.engine.repair import lns_repair
        fb = (res.get("stats") or {}).get("fallback")
        res = lns_repair(ds, res, opts, time_limit_s=repair_time_s or max(15, opts.time_limit_s / 3))
        if fb:
            res.setdefault("stats", {})["fallback"] = fb
    return res


def _compact_validation(v: dict[str, Any], max_violations: int = 25) -> dict[str, Any]:
    alloc = dict(v.get("allocation") or {})
    alloc["under"] = alloc.get("under", [])[:20]
    alloc["over"] = alloc.get("over", [])[:20]
    return {"valid": v["valid"], "hard_violation_total": v["hard_violation_total"], "counts": v["counts"],
            "violations": v["violations"][:max_violations], "allocation": alloc}


def _natural(s: str):
    import re
    return [int(t) if t.isdigit() else t for t in re.split(r"(\d+)", s)]
