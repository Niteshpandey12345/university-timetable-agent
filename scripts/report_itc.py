"""Figures and a Markdown table for the ITC-2007 results (experiments/results/itc2007_results.json)."""
from __future__ import annotations

import json

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np

from ttagent.config import ROOT

RES = ROOT / "experiments" / "results"


def main() -> None:
    data = json.loads((RES / "itc2007_results.json").read_text())
    rows = data["rows"]
    names = [r["instance"] for r in rows]
    has_lns = any(r.get("lns_cost") is not None for r in rows)
    has_dec = any(r.get("decomp_cost") is not None for r in rows)
    series = [("GA (memetic)", "ga_cost", "#c2410c"), ("CP-SAT", "cpsat_cost", "#2563eb")]
    if has_lns:
        series.append(("CP-SAT + LNS", "lns_cost", "#059669"))
    if has_dec:
        series.append(("CP-SAT time-then-room", "decomp_cost", "#059669"))
    ref = "Kiefer et al. 2017 avg"
    # Figure 1: cost relative to best-known UB (log scale), per instance
    fig, ax = plt.subplots(figsize=(11, 4.2))
    x = np.arange(len(rows))
    w = 0.8 / (len(series) + 1)
    for i, (lab, k, col) in enumerate(series):
        ax.bar(x + i * w, [r.get(k) or np.nan for r in rows], w, label=lab, color=col)
    ax.bar(x + len(series) * w, [r.get(ref) or np.nan for r in rows], w, label=ref.replace(" avg", " (avg, time-limited)"), color="#6b7280")
    ax.plot(x + 0.4 - w / 2, [max(r["best_known_ub"], 0.5) for r in rows], "k_", markersize=14, mew=2, label="Best known")
    ax.set_yscale("log")
    ax.set_xticks(x + 0.4 - w / 2, names, rotation=45)
    ax.set_ylabel("UD2 penalty (log)")
    ax.set_title(f"ITC-2007 Track 3, {data['time_limit_s']:.0f} s per instance, all solutions feasible")
    ax.legend(ncol=3, fontsize=8, frameon=False)
    ax.spines[["top", "right"]].set_visible(False)
    fig.tight_layout(); fig.savefig(RES / "fig_itc_costs.png", dpi=150); plt.close(fig)
    # Figure 2: CP-SAT vs GA scatter
    fig, ax = plt.subplots(figsize=(4.6, 4.4))
    g, c = [r["ga_cost"] for r in rows], [r["cpsat_cost"] for r in rows]
    ax.scatter(c, g, color="#2563eb")
    lim = [0.8, max(g) * 1.3]
    ax.plot(lim, lim, "k--", lw=0.8)
    for r in rows:
        ax.annotate(r["instance"][-2:], (r["cpsat_cost"] + 0.5, r["ga_cost"]), fontsize=7)
    ax.set_xscale("symlog"); ax.set_yscale("symlog")
    ax.set_xlabel("CP-SAT penalty"); ax.set_ylabel("GA penalty")
    ax.set_title("Above the line: CP-SAT better")
    ax.spines[["top", "right"]].set_visible(False)
    fig.tight_layout(); fig.savefig(RES / "fig_itc_scatter.png", dpi=150); plt.close(fig)
    # Markdown table
    hdr = ["Instance", "Lectures", "Best known (LB–UB)", "GA", "CP-SAT", "CP-SAT bound"] + (["CP-SAT+LNS"] if has_lns else []) + (["Time-then-room"] if has_dec else []) + ["Kiefer'17 avg", "A&T'12 avg"]
    lines = ["| " + " | ".join(hdr) + " |", "|" + "---|" * len(hdr)]
    for r in rows:
        best = min(v for v in (r["ga_cost"], r["cpsat_cost"], r.get("lns_cost"), r.get("decomp_cost")) if v is not None)
        fmt = lambda v: "–" if v is None else (f"**{v}**" if v == best else str(v))
        cells = [r["instance"], str(r["lectures"]), f"{r['best_known_lb']}–{r['best_known_ub']}", fmt(r["ga_cost"]),
                 fmt(r["cpsat_cost"]) + (" (opt)" if r["cpsat_status"] == "OPTIMAL" else ""), f"{r['cpsat_bound']:.0f}"]
        if has_lns:
            cells.append(fmt(r.get("lns_cost")))
        if has_dec:
            cells.append(fmt(r.get("decomp_cost")))
        cells += [str(r.get("Kiefer et al. 2017 avg", "–")), str(r.get("Abdullah & Turabieh 2012 avg", "–"))]
        lines.append("| " + " | ".join(cells) + " |")
    agg = {k: round(float(np.median([r[k.replace("cost", "gap_pct")] for r in rows if r.get(k.replace("cost", "gap_pct")) is not None])), 1) for _, k, _ in series}
    wins = sum(r["cpsat_cost"] < r["ga_cost"] for r in rows)
    if has_dec:
        done = [r for r in rows if r.get("decomp_cost") is not None]
        dw = sum(r["decomp_cost"] < r["cpsat_cost"] for r in done)
        dt = sum(r["decomp_cost"] == r["cpsat_cost"] for r in done)
        kief = [r for r in done if r.get("Kiefer et al. 2017 avg") is not None]
        kb = sum(r["decomp_cost"] <= r["Kiefer et al. 2017 avg"] for r in kief)
        ga_w = sum(r["decomp_cost"] < r["ga_cost"] for r in done)
    lines += ["", f"Median gap to best-known UB: " + ", ".join(f"{lab} {agg[k]}%" for lab, k, _ in series) + f". CP-SAT beats GA on {wins}/{len(rows)} instances."]
    if has_dec:
        lines.append(f"Time-then-room vs plain CP-SAT: better on {dw}, equal on {dt} of {len(done)}; beats the GA on {ga_w}/{len(done)}; "
                     f"at or below the Kiefer et al. time-limited average on {kb}/{len(kief)}.")
    (RES / "itc2007_table.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print("\n".join(lines))


if __name__ == "__main__":
    main()
