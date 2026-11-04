"""Analyse the user study (docs/USER_STUDY.md): completion, correctness, time, SUS, paired Wilcoxon tests.

    python -m scripts.analyze_user_study docs/user_study_template.csv
"""
from __future__ import annotations

import csv
import json
import statistics
import sys
from collections import defaultdict


def sus(row: dict) -> float | None:
    vals = [row.get(f"sus{i}") for i in range(1, 11)]
    if not all(v not in (None, "") for v in vals):
        return None
    v = [int(x) for x in vals]
    return 2.5 * sum((v[i] - 1) if i % 2 == 0 else (5 - v[i]) for i in range(10))


def main(path: str) -> dict:
    from scipy.stats import wilcoxon
    rows = [r for r in csv.DictReader(open(path, encoding="utf-8")) if r.get("seconds")]
    by = defaultdict(list)
    for r in rows:
        by[r["condition"]].append(r)
    out = {}
    for c, rs in by.items():
        sus_scores = {r["participant"]: sus(r) for r in rs if sus(r) is not None}
        out[c] = {"tasks": len(rs),
                  "completion_pct": round(100 * sum(r["completed"].lower() in ("1", "yes", "y", "true") for r in rs) / len(rs), 1),
                  "correct_pct": round(100 * sum(r["correct"].lower() in ("1", "yes", "y", "true") for r in rs) / len(rs), 1),
                  "median_seconds": statistics.median(float(r["seconds"]) for r in rs),
                  "hard_errors_left": sum(int(r["hard_errors_left"] or 0) for r in rs),
                  "sus_mean": round(statistics.mean(sus_scores.values()), 1) if sus_scores else None,
                  "sus_sd": round(statistics.stdev(sus_scores.values()), 1) if len(sus_scores) > 1 else None}
    conds = sorted(by)
    if len(conds) == 2:
        a, b = conds
        ta = {(r["participant"], r["task"][:2]): float(r["seconds"]) for r in by[a]}
        tb = {(r["participant"], r["task"][:2]): float(r["seconds"]) for r in by[b]}
        keys = sorted(set(ta) & set(tb))
        if len(keys) >= 5:
            w = wilcoxon([ta[k] for k in keys], [tb[k] for k in keys])
            out["time_wilcoxon"] = {"pairs": len(keys), "W": float(w.statistic), "p": float(w.pvalue)}
        sa = {r["participant"]: sus(r) for r in by[a] if sus(r) is not None}
        sb = {r["participant"]: sus(r) for r in by[b] if sus(r) is not None}
        ps = sorted(set(sa) & set(sb))
        if len(ps) >= 5:
            w = wilcoxon([sa[p] for p in ps], [sb[p] for p in ps])
            out["sus_wilcoxon"] = {"pairs": len(ps), "W": float(w.statistic), "p": float(w.pvalue)}
    print(json.dumps(out, indent=1))
    return out


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "docs/user_study_template.csv")
