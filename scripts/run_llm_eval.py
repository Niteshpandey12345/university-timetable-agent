"""
Agent-layer evaluation (RQ6+).

  routing       intent + parameter accuracy of the supervisor on experiments/llm_eval/requests.jsonl
                (rule-based parser always; LLM structured output when a key is configured)
  faithfulness  do the numbers in LLM explanations actually appear in the tool results they explain?
  llm_only      can an LLM produce a valid timetable *without* the solver? (validated by our validator)

    python -m scripts.run_llm_eval                 # all parts that are possible
    python -m scripts.run_llm_eval --parts routing # offline only

An LLM key is read from .env (GOOGLE_API_KEY / ANTHROPIC_API_KEY / OPENAI_API_KEY); without one
only the rule-based routing part runs.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import re
import time
from pathlib import Path

from ttagent.agents.intent import Intent, parse_rules
from ttagent.agents.llm import build_llm, describe, text_of
from ttagent.agents.prompts import EXPLAIN_PROMPT, INTENT_PROMPT
from ttagent.config import ROOT

EVAL = ROOT / "experiments" / "llm_eval"
OUT = ROOT / "experiments" / "results"
FIELDS = ("semesters", "kind", "code", "day", "start", "token", "session_id")


def score_routing(gold: dict, pred: dict) -> dict:
    out = {"action": gold["action"] == pred.get("action")}
    for f in FIELDS:
        if gold.get(f) is not None:
            g, p = gold[f], pred.get(f) if f != "start" else (pred.get("start") or pred.get("slot"))
            if f == "code":
                g, p = str(g).upper(), str(p or "").upper()
            out[f] = g == p
    return out


def routing(llm) -> dict:
    from ttagent.engine.service import TimetableService
    ds = TimetableService().dataset()
    fac = {f for f, v in ds.faculty.items() if not v.is_placeholder}
    rooms = set(ds.rooms)
    reqs = [json.loads(l) for l in (EVAL / "requests.jsonl").read_text().splitlines() if l.strip()]
    parsers = {"rules": lambda q: parse_rules(q, fac, rooms).model_dump()}
    if llm is not None:
        structured = llm.with_structured_output(Intent)

        def via_llm(q):
            from langchain_core.messages import HumanMessage, SystemMessage
            for attempt in range(3):
                try:
                    return structured.invoke([SystemMessage(INTENT_PROMPT), HumanMessage(q)]).model_dump()
                except Exception as e:  # rate limits
                    time.sleep(4 * (attempt + 1))
                    err = e
            return {"action": "ERROR", "error": str(err)[:100]}
        parsers["llm"] = via_llm
    result = {}
    for name, fn in parsers.items():
        rows, t0 = [], time.time()
        for r in reqs:
            pred = fn(r["query"])
            s = score_routing(r, pred)
            rows.append({"query": r["query"], "gold": r["action"], "pred": pred.get("action"), **{f"ok_{k}": v for k, v in s.items()}})
        n = len(rows)
        act = sum(r["ok_action"] for r in rows)
        slot_checks = [v for r in rows for k, v in r.items() if k.startswith("ok_") and k != "ok_action"]
        result[name] = {"n": n, "action_accuracy": round(act / n, 3),
                        "parameter_accuracy": round(sum(slot_checks) / max(1, len(slot_checks)), 3),
                        "exact_match": round(sum(all(v for k, v in r.items() if k.startswith("ok_")) for r in rows) / n, 3),
                        "seconds": round(time.time() - t0, 1),
                        "errors": [r for r in rows if not all(v for k, v in r.items() if k.startswith("ok_"))][:30]}
        print(f"routing[{name}] action={result[name]['action_accuracy']} params={result[name]['parameter_accuracy']} exact={result[name]['exact_match']}")
    return result


_NUM = re.compile(r"(?<![A-Za-z#])(\d+(?:\.\d+)?)(?![A-Za-z])")


def _numbers(obj, acc=None) -> set:
    acc = set() if acc is None else acc
    if isinstance(obj, bool) or obj is None:
        return acc
    if isinstance(obj, (int, float)):
        acc.add(round(float(obj), 2))
        acc.add(float(round(obj)))
    elif isinstance(obj, str):
        for m in _NUM.findall(obj):
            acc.add(round(float(m), 2))
    elif isinstance(obj, dict):
        for k, v in obj.items():
            _numbers(k, acc)
            _numbers(v, acc)
    elif isinstance(obj, (list, tuple)):
        for v in obj:
            _numbers(v, acc)
    return acc


def faithfulness(llm, n_cases: int = 12) -> dict:
    """Generate explanations for real tool results and check every number is grounded in the facts."""
    from langchain_core.messages import HumanMessage, SystemMessage

    from ttagent.agents.graph import _slim
    from ttagent.engine.service import TimetableService
    svc = TimetableService()
    cases = []
    sem_runs = [[1], [3], [5], [7]]
    for sems in sem_runs:
        s = svc.solve(semesters=sems, time_limit_s=20, repair=True)
        cases.append(("Generate timetable for semester %s" % sems[0], {"solution": s, "capacity": svc.capacity_report()}))
        cases.append(("Show all conflicts in the current timetable", {"validation": svc.validate_solution(s["solution_id"])}))
        cases.append(("Compare with the published timetable", {"comparison": svc.compare("published", s["solution_id"])}))
    cases = cases[:n_cases]
    rows = []
    for q, facts in cases:
        f = _slim(facts)
        msg = llm.invoke([SystemMessage(EXPLAIN_PROMPT), HumanMessage(f"User request: {q}\n\nFacts:\n{json.dumps(f, default=str)[:14000]}")])
        ans = text_of(msg)
        allowed = _numbers(f) | _numbers(q) | {0.0, 1.0, 100.0}
        nums = [round(float(x), 2) for x in _NUM.findall(ans)]
        grounded = [x for x in nums if x in allowed or float(round(x)) in allowed]
        rows.append({"request": q, "numbers": len(nums), "grounded": len(grounded),
                     "ungrounded": sorted(set(nums) - set(grounded))[:10], "answer": ans[:1500]})
        time.sleep(1)
    tot = sum(r["numbers"] for r in rows)
    g = sum(r["grounded"] for r in rows)
    out = {"cases": len(rows), "numbers_checked": tot, "grounded_rate": round(g / max(1, tot), 3), "rows": rows}
    print(f"faithfulness: {g}/{tot} numbers grounded ({out['grounded_rate']})")
    return out


def llm_only(llm, semester: int = 7, trials: int = 3) -> dict:
    """Ask the LLM to schedule a semester directly (no solver) and validate the result."""
    from langchain_core.messages import HumanMessage

    from ttagent.domain.timegrid import TimeGrid
    from ttagent.engine.service import TimetableService
    from ttagent.engine.validator import assignments_to_bookings, validate
    svc = TimetableService()
    ds = svc.dataset()
    reqs = [r for r in ds.requirements if r.semester == semester]
    g = TimeGrid.from_settings()
    spec = "\n".join(f"token {r.token}: batches {','.join(r.batches)}; {r.kind.value} {r.course}; {r.duration}h x {r.per_week}/week; "
                     f"teacher {','.join(r.faculty)}; room {r.venue}; elective basket {r.basket_id or '-'}" for r in reqs)
    prompt = (f"Create a weekly university timetable. Days: {', '.join(g.days)}. Teaching slots Mon-Fri: {g.teaching_slots} "
              f"(slot 5 is lunch, never use it), Saturday slots 1-4 only. A 2-hour lab needs two consecutive slots not crossing lunch. "
              "Rules: a batch cannot attend two sessions at once unless both belong to the same elective basket; a teacher cannot "
              "teach two sessions at once; a room holds one session at a time; a requirement meets at most once per day; every "
              "requirement needs exactly its sessions per week.\nRequirements:\n" + spec +
              '\nReturn ONLY JSON: a list of {"token": int, "day": "Mon", "start": int, "room": "LT1"} with one item per session.')
    rows = []
    for t in range(trials):
        t0 = time.time()
        try:
            ans = text_of(llm.invoke([HumanMessage(prompt)]))
            js = json.loads(re.search(r"\[.*\]", ans, re.S).group(0))
            per = {}
            assigns = []
            for a in js:
                tok = int(a["token"])
                k = per.get(tok, 0)
                per[tok] = k + 1
                if tok in {r.token for r in reqs}:
                    assigns.append({"session_id": f"R{tok}#{k}", "token": tok, "day": a["day"], "start": int(a["start"]), "room": str(a["room"])})
            v = validate(ds, assignments_to_bookings(ds, assigns), g, scope_tokens={r.token for r in reqs})
            hard = sum(n for k, n in v["counts"].items() if k.startswith("H"))
            rows.append({"trial": t, "sessions": len(assigns), "hard_violations": hard, "coverage_pct": v["allocation"]["coverage_pct"],
                         "valid": v["valid"], "counts": v["counts"], "seconds": round(time.time() - t0, 1)})
        except Exception as e:
            rows.append({"trial": t, "error": f"{type(e).__name__}: {str(e)[:200]}", "valid": False})
        print("llm_only", rows[-1])
        time.sleep(2)
    solver = svc.solve(semesters=[semester], time_limit_s=30)
    return {"semester": semester, "requirements": len(reqs), "trials": rows,
            "valid_rate": round(sum(1 for r in rows if r.get("valid")) / trials, 3),
            "solver_reference": {k: solver[k] for k in ("coverage_pct", "hard_violations", "valid")}}


def main(parts: list[str]) -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    llm = build_llm()
    res = {"model": describe() if llm else "none (offline)", "when": time.strftime("%Y-%m-%d %H:%M")}
    if "routing" in parts:
        res["routing"] = routing(llm)
    if llm is not None and "faithfulness" in parts:
        res["faithfulness"] = faithfulness(llm)
    if llm is not None and "llm_only" in parts:
        res["llm_only"] = llm_only(llm)
    name = "llm_eval_offline.json" if llm is None else "llm_eval_llm.json"
    (OUT / name).write_text(json.dumps(res, indent=1, default=str), encoding="utf-8")
    print("wrote", OUT / name)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--parts", nargs="*", default=["routing", "faithfulness", "llm_only"])
    main(ap.parse_args().parts)
