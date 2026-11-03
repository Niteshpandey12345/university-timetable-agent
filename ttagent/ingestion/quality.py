"""Data-quality report for the ingested workbook."""
from __future__ import annotations

from collections import Counter
from typing import Any

from ttagent.domain.models import Dataset
from ttagent.domain.timegrid import TimeGrid


def build_quality_report(ds: Dataset, issues: list[dict]) -> dict[str, Any]:
    from ttagent.engine.diagnosis import capacity_analysis
    from ttagent.engine.metrics import compute_metrics
    from ttagent.engine.validator import validate

    grid = TimeGrid.from_settings()
    by_key = {r.key: r for r in ds.requirements}

    # requirement vs published faculty/venue drift
    drift = Counter()
    for b in ds.published:
        r = by_key.get((",".join(b.batches), b.kind.value, b.course))
        if r is None:
            continue
        if set(b.faculty) != set(r.faculty):
            drift[("faculty", r.token, ",".join(r.faculty), ",".join(b.faculty))] += 1
        if b.room != r.venue:
            drift[("venue", r.token, r.venue, b.room)] += 1
    for (what, tok, req_v, pub_v), n in sorted(drift.items()):
        issues.append({"severity": "info", "category": f"{what}_drift",
                       "message": f"token {tok}: requirement says {what} {req_v}, published grid uses {pub_v} ({n} cell(s))",
                       "location": ""})

    for bk in ds.baskets:
        if bk.confidence != "high":
            issues.append({"severity": "warning" if bk.confidence == "low" else "info", "category": "elective_basket_confidence",
                           "message": f"basket {bk.basket_id} ({', '.join(bk.courses)}) inferred from only {bk.evidence} co-scheduled cell(s) "
                                      f"[{bk.confidence} confidence] -- confirm in data/processed/electives.yaml", "location": ""})
    if not any(r.seats for r in ds.rooms.values()):
        issues.append({"severity": "info", "category": "room_capacity_proxy",
                       "message": "no config/rooms.yaml: room capacity uses the observed-batches proxy; add real seat counts to tighten room suitability",
                       "location": ""})

    macro_unplaced = [r for r in ds.requirements if (r.macro_scheduled or 0) < r.per_week]
    published_validation = validate(ds, ds.published, grid)
    strict = capacity_analysis(ds, grid, basket_aware=False)
    aware = capacity_analysis(ds, grid, basket_aware=True)

    sev = Counter(i["severity"] for i in issues)
    cat = Counter(i["category"] for i in issues)
    return {
        "summary": {
            "requirements": len(ds.requirements),
            "sessions_per_week": sum(r.per_week for r in ds.requirements),
            "teaching_hours_per_week": sum(r.weekly_hours for r in ds.requirements),
            "semesters": ds.semesters(),
            "batches_internal": sum(1 for b in ds.batches.values() if b.in_requirements),
            "batches_external": sum(1 for b in ds.batches.values() if not b.in_requirements),
            "faculty": len(ds.faculty),
            "faculty_placeholders": sorted(f for f, v in ds.faculty.items() if v.is_placeholder),
            "rooms": len(ds.rooms),
            "rooms_by_category": dict(Counter(r.category for r in ds.rooms.values())),
            "elective_baskets": [b.model_dump() for b in ds.baskets],
            "fixed_external_bookings": len(ds.fixed_bookings),
            "published_cells": len(ds.published),
            "reference_sessions_mapped": len(ds.reference),
        },
        "issue_counts": {"by_severity": dict(sev), "by_category": dict(cat)},
        "issues": issues,
        "vba_macro_bookkeeping": {
            "requirements_not_fully_scheduled": len(macro_unplaced),
            "hours_missing": sum((r.per_week - (r.macro_scheduled or 0)) * r.duration for r in macro_unplaced),
            "tokens": [r.token for r in macro_unplaced],
        },
        "published_grid_validation": {k: published_validation[k] for k in ("valid", "hard_violation_total", "counts", "allocation")}
        | {"violations": [v for v in published_validation["violations"] if v["rule"] != "H1_batch_clash" or "7B" not in v["message"]][:60]},
        "published_grid_metrics": compute_metrics(ds, ds.published, grid),
        "capacity_strict_model": {k: strict[k] for k in ("provably_infeasible", "weekly_capacity_h")}
        | {"infeasible_count": len(strict["infeasible_resources"]), "infeasible_resources": strict["infeasible_resources"][:30]},
        "capacity_basket_aware": {k: aware[k] for k in ("provably_infeasible", "weekly_capacity_h")}
        | {"infeasible_resources": aware["infeasible_resources"], "batches_top": aware["batches_top"][:8],
           "faculty_top": aware["faculty_top"][:8], "room_pools_top": aware["room_pools_top"][:8],
           "requested_venues_oversubscribed": aware["requested_venues_oversubscribed"]},
    }
