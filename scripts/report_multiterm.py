"""Tables and a figure for the multi-term validation (experiments/results/multiterm/*.json + the main term)."""
from __future__ import annotations

import json
import statistics

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

from ttagent.config import ROOT

R = ROOT / "experiments" / "results"
OUT = R / "multiterm"
TERMS = [("jul2026", "Jul–Dec 2026 (main)"), ("jul2026_v15", "Jul–Dec 2026, earlier draft"),
         ("jul2025", "Jul–Dec 2025"), ("jan2026", "Jan–Jun 2026")]
METHODS = ["Published grid", "VBA macro (port)", "GA memetic", "CP-SAT pipeline"]


def main_term() -> dict:
    """The main term from the v3/v4 analyses (seeds 1-5, to match the other terms)."""
    v3 = json.loads((R / "v3/analysis_v3.json").read_text())["A_runs"]
    runs = [r for r in v3 if r["method"] in METHODS]
    from ttagent.ingestion.excel_loader import load_workbook_dataset
    from scripts.run_multiterm import diagnose
    return {"runs": runs, "diagnosis": diagnose(load_workbook_dataset())}


def load() -> dict:
    data = {"jul2026": main_term()}
    for k, _ in TERMS[1:]:
        p = OUT / f"{k}.json"
        if p.exists():
            data[k] = json.loads(p.read_text())
    return data


def stats(runs, m):
    rs = [r for r in runs if r["method"] == m]
    if not rs:
        return None
    cov = [r["coverage_pct"] for r in rs]
    sph = [r["soft_penalty"] / max(1, r["scheduled_h"]) for r in rs]
    return {"n": len(rs), "cov": statistics.mean(cov), "cov_sd": statistics.stdev(cov) if len(cov) > 1 else 0.0,
            "hard": max(r["hard"] for r in rs), "sph": statistics.mean(sph), "covs": cov}


def main() -> None:
    from scripts.run_analysis_v3 import mann_whitney
    data = load()
    diag = ["| Term | Requirements | Hours/week | Rooms | Teachers | Batches over capacity (macro model) | Macro hours missing | Published coverage [%] | Published hard violations [count] |",
            "|---|---:|---:|---:|---:|---:|---:|---:|---:|"]
    res = ["| Term | Method | Runs | Coverage κ [%] | Hard violations [count] | Φ̂ [pts h⁻¹] |", "|---|---|---:|---:|---:|---:|"]
    tests = {}
    for k, label in TERMS:
        if k not in data:
            continue
        d, runs = data[k]["diagnosis"], data[k]["runs"]
        diag.append(f"| {label} | {d['requirements']} | {d['hours_per_week']} | {d['rooms']} | {d['faculty']} | {d['strict_model_infeasible_batches']} | "
                    f"{d['macro_hours_missing']} | {d['published_coverage_pct']} | {d['published_hard']} |")
        for m in METHODS:
            s = stats(runs, m)
            if s:
                cov = f"{s['cov']:.2f} ± {s['cov_sd']:.2f}" if s["n"] > 1 else f"{s['cov']:.2f}"
                res.append(f"| {label} | {m} | {s['n']} | {cov} | {s['hard']} | {s['sph']:.2f} |")
        cp, ga = [r for r in runs if r["method"] == "CP-SAT pipeline"], [r for r in runs if r["method"] == "GA memetic"]
        if len(cp) > 1 and len(ga) > 1:
            tests[label] = {"coverage": mann_whitney([r["coverage_pct"] for r in cp], [r["coverage_pct"] for r in ga]),
                            "soft_per_h": mann_whitney([r["soft_penalty"] / r["scheduled_h"] for r in cp], [r["soft_penalty"] / r["scheduled_h"] for r in ga])}
    md = "\n".join(diag) + "\n\n" + "\n".join(res) + "\n\n" + "\n".join(
        f"- {t}: coverage p = {v['coverage']['p']:.2g} (δ = {v['coverage']['cliffs_delta']}), Φ̂ p = {v['soft_per_h']['p']:.2g} (δ = {v['soft_per_h']['cliffs_delta']})"
        for t, v in tests.items())
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "multiterm_table.md").write_text(md + "\n", encoding="utf-8")
    (OUT / "multiterm_tests.json").write_text(json.dumps(tests, indent=1), encoding="utf-8")
    print(md)
    # figure: coverage per term and method
    plt.rcParams.update({"font.family": "Liberation Serif", "font.size": 9})
    fig, ax = plt.subplots(figsize=(7.1, 2.8))
    cols = {"Published grid": "#7f8c8d", "VBA macro (port)": "#c0392b", "GA memetic": "#d35400", "CP-SAT pipeline": "#1f4e9c"}
    terms = [(k, l) for k, l in TERMS if k in data]
    for i, (k, l) in enumerate(terms):
        for j, m in enumerate(METHODS):
            s = stats(data[k]["runs"], m)
            if not s:
                continue
            x = i + (j - 1.5) * 0.2
            ax.scatter([x] * len(s["covs"]), s["covs"], s=12, color=cols[m], label=m if i == 0 else None, zorder=3)
            if m == "Published grid":
                ax.annotate(f"{s['hard']} hard", (x, s["covs"][0]), (x - 0.05, s["covs"][0] - 1.3), fontsize=6.5, color="#555555")
    ax.set_xticks(range(len(terms)), [l for _, l in terms]); ax.set_ylabel("Coverage κ [%]")
    ax.legend(ncol=4, frameon=False, fontsize=7.5, loc="lower center", bbox_to_anchor=(0.5, 1.0))
    ax.spines[["top", "right"]].set_visible(False)
    fig.tight_layout(); fig.savefig(OUT / "fig_multiterm.png", dpi=300); plt.close(fig)


if __name__ == "__main__":
    main()
