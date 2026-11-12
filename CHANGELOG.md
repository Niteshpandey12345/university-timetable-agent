# Changelog

## 4.1.0 (November 2026): validation on other terms
- **Multi-term validation** (`scripts/run_multiterm.py`, `scripts/report_multiterm.py`): the unchanged pipeline
  on three further workbooks (Jul–Dec 2025, Jan–Jun 2026, an earlier draft of Jul–Dec 2026). CP-SAT reaches
  99.45–99.94 % coverage with 0 hard violations in every term and the lowest soft penalty per hour (p = 0.008).
- **Agent evaluation replicated** on two more terms: full system 51/51 pooled, single tool call 17/51 with 11
  unsafe publications. Tasks are now term-independent (`TTAGENT_EVAL_BASE_DB`, `--seed`).
- `TTAGENT_PROCESSED_DIR` to keep each term's data separate. See [docs/MULTITERM_VALIDATION.md](docs/MULTITERM_VALIDATION.md).

## 4.0.0 (November 2026): measuring what the agents contribute
- **Agent evaluation** (`ttagent agent-eval`): 17 timetabling-office tasks, 7 with an injected tool fault,
  run end-to-end through the LangGraph workflow and the MCP servers on 7 system variants (full, five
  one-component ablations, single tool call). Full system 17/17 with 0 unsafe publications; single tool call
  6/17 with 4. See [docs/AGENT_EVALUATION.md](docs/AGENT_EVALUATION.md).
- **Safer workflow:** independent validation gates repair and publication; violation-driven repair; scheduler
  retry and GA fallback; repairs keep the conversation's own version and each session's room; ablation
  switches; fault injection (`TTAGENT_FAULT`) for testing and demos.
- **Solver evidence:** 10 seeds; two-stage GA tuning race; 7-instance stress family; single-core fallback.
- **ITC-2007:** time-then-room decomposition (`ttagent/benchmarks/ctt_decomp.py`), median gap 106 % → 37.5 %.
- **Software:** Agent evaluation dashboard page; user-study kit; `agent-eval --llm` harness; 52 tests.

## 3.0.0 (November 2026): GA comparison, public benchmark, software
- Memetic genetic algorithm allocator; statistical comparison over seeds; anytime and disruption studies.
- ITC-2007 Track 3 parser, UD2 scorer, CP-SAT and GA solvers.
- Real seat counts and batch sizes, teacher preferences, elective-basket confidence.
- `ttagent` command, Windows launcher, Settings and Methods pages, workbook upload.

## 2.0.0 (November 2026): agentic system on the real workbook
- LangGraph multi-agent workflow over 4 MCP servers (30 tools), decomposed CP-SAT engine, LNS repair,
  independent validator, VBA-macro port, dashboard, project report.
