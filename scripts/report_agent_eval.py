"""Figures and a Markdown summary for the agent evaluation (experiments/results/agent_eval/agent_eval_<tag>.json).

    python -m scripts.report_agent_eval            # tag "rules"
    python -m scripts.report_agent_eval --tag llm
"""
from __future__ import annotations

import argparse
import json
from collections import Counter

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np

from ttagent.config import ROOT

RES = ROOT / "experiments" / "results" / "agent_eval"
ORDER = ["full", "-validator", "-repair", "-fallback", "-approval", "-memory", "single_call"]
LABEL = {"full": "Full system", "-validator": "− validator", "-repair": "− repair loop", "-fallback": "− fallback",
         "-approval": "− approval", "-memory": "− memory", "single_call": "Single tool call"}
NODES = ["supervisor", "requirements", "scheduler", "validator", "diagnoser", "repairer", "approval", "action", "explainer"]


def main(tag: str) -> None:
    d = json.loads((RES / f"agent_eval_{tag}.json").read_text())
    rows, S = d["rows"], d["summary"]["by_variant"]
    V = [v for v in ORDER if v in S]
    scen = [s["id"] for s in d["scenarios"]]
    # 1. success and unsafe publications per variant
    fig, ax = plt.subplots(figsize=(8.6, 3.6))
    x = np.arange(len(V))
    ax.bar(x - 0.2, [S[v]["success"] for v in V], 0.4, color="#2563eb", label="tasks solved")
    ax.bar(x + 0.2, [S[v]["unsafe_publications"] for v in V], 0.4, color="#dc2626", label="invalid timetables published")
    for i, v in enumerate(V):
        ax.text(i - 0.2, S[v]["success"] + 0.2, str(S[v]["success"]), ha="center", fontsize=8)
        ax.text(i + 0.2, S[v]["unsafe_publications"] + 0.2, str(S[v]["unsafe_publications"]), ha="center", fontsize=8)
    ax.set_xticks(x, [LABEL[v] for v in V], fontsize=8)
    ax.set_ylabel(f"count (of {S[V[0]]['tasks']} tasks)")
    ax.set_title("Agent ablation: tasks solved and unsafe publications")
    ax.legend(frameon=False, fontsize=8)
    ax.spines[["top", "right"]].set_visible(False)
    fig.tight_layout(); fig.savefig(RES / f"fig_agent_ablation_{tag}.png", dpi=150); plt.close(fig)
    # 2. task x variant matrix (2 = success, 1 = failed safely, 0 = unsafe publication)
    R = {(r["variant"], r["scenario"]): r for r in rows}
    M = np.full((len(scen), len(V)), np.nan)
    for i, s in enumerate(scen):
        for j, v in enumerate(V):
            r = R.get((v, s))
            if r:
                M[i, j] = 2 if r["success"] else (0 if r["unsafe_published"] else 1)
    fig, ax = plt.subplots(figsize=(7.4, 0.32 * len(scen) + 1.2))
    from matplotlib.colors import ListedColormap
    ax.imshow(M, cmap=ListedColormap(["#dc2626", "#f59e0b", "#16a34a"]), vmin=0, vmax=2, aspect="auto")
    cats = {s["id"]: s["cat"] for s in d["scenarios"]}
    ax.set_yticks(range(len(scen)), [f"{s}  {cats[s]}" for s in scen], fontsize=7)
    ax.set_xticks(range(len(V)), [LABEL[v] for v in V], fontsize=7, rotation=30, ha="right")
    ax.set_title("Per task: green solved · amber failed safely · red published clashes", fontsize=9)
    fig.tight_layout(); fig.savefig(RES / f"fig_agent_matrix_{tag}.png", dpi=150); plt.close(fig)
    # 3. which agents act in which task (full system)
    full = [R[("full", s)] for s in scen if ("full", s) in R]
    A = np.array([[r["nodes"].get(n, 0) + sum(1 for _ in range(0)) for n in NODES] for r in full], dtype=float)
    fig, ax = plt.subplots(figsize=(7.4, 0.32 * len(full) + 1.2))
    ax.imshow(A, cmap="Blues", aspect="auto")
    for i in range(A.shape[0]):
        for j in range(A.shape[1]):
            if A[i, j]:
                ax.text(j, i, int(A[i, j]), ha="center", va="center", fontsize=7)
    ax.set_yticks(range(len(full)), [r["scenario"] for r in full], fontsize=7)
    ax.set_xticks(range(len(NODES)), NODES, fontsize=7, rotation=30, ha="right")
    ax.set_title("Full system: how often each agent node ran per task", fontsize=9)
    fig.tight_layout(); fig.savefig(RES / f"fig_agent_activity_{tag}.png", dpi=150); plt.close(fig)
    # markdown
    lines = ["| Variant | Tasks solved | Fault tasks recovered | Invalid timetables published | Tool calls / task | Mean time / task | Human decisions |",
             "|---|---|---|---|---|---|---|"]
    for v in V:
        s = S[v]
        lines.append(f"| {LABEL[v]} | {s['success']}/{s['tasks']} ({s['success_pct']} %) | {s['fault_success']} | "
                     f"{s['unsafe_publications']} | {s['mean_tool_calls']} | {s['mean_wall_s']} s | {s['human_decisions']} |")
    lines += ["", "| Component removed | Tasks lost | Which | Unsafe publications |", "|---|---|---|---|"]
    for k, c in d["summary"]["component_contribution"].items():
        lines.append(f"| {k} | {c['tasks_lost_without_it']} | {', '.join(sorted(c['which'])) or '–'} | {c['unsafe_publications_without_it']} |")
    lines += ["", "| Variant | What-if: sessions moved (W3, W5) |", "|---|---|"]
    for v in V:
        w = [r for r in rows if r["variant"] == v and r["scenario"].startswith("W") and r.get("moved_pct") is not None]
        if w:
            lines.append(f"| {LABEL[v]} | " + ", ".join(f"{r['moved_pct']} %" for r in sorted(w, key=lambda r: r['scenario']))
                         + (" (wrong version)" if not all(r["success"] for r in w) else "") + " |")
    caught = [r["scenario"] for r in rows if r["variant"] == "full" and r["success"]
              and (r["category"] == "fault: silent corruption" or r["category"] == "unsafe request")]
    lines += ["", f"Corrupted versions intercepted by the validator agent in the full system: {len(caught)} ({', '.join(caught)})."]
    (RES / f"agent_eval_{tag}.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print("\n".join(lines))


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--tag", default="rules")
    main(ap.parse_args().tag)
