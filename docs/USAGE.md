# User guide

A walkthrough of every feature, in the order you would use them.

## 1. Install (once)

**Easiest (Windows):** double-click `TimetableAgent.bat`. On first run it creates `.venv`, installs the
package, loads the workbook and opens the dashboard in your browser. On macOS/Linux run `./run.sh`.

**Manual:**

```powershell
python -m venv .venv
Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass   # Windows only, per terminal
.venv\Scripts\Activate.ps1                                    # macOS/Linux: source .venv/bin/activate
python -m pip install -e .            # installs the `ttagent` command (or: pip install -r requirements.txt)
ttagent --version                      # 3.0.0
```

Optional: `copy .env.example .env` and set `GOOGLE_API_KEY=...` (or `ANTHROPIC_API_KEY`).

## 2. Load the workbook

```powershell
ttagent ingest                       # same as: python -m scripts.ingest
```

You can also upload a new workbook from the dashboard (**Settings → Upload workbook**).

This prints what was found: 348 requirements, 674 h/week, 7 elective baskets, data issues, and whether the
problem is feasible. It writes three files to `data/processed/`:

| File | What it is |
|---|---|
| `dataset.json` | the clean, typed dataset every component uses |
| `quality_report.json` | every data issue with its cell location |
| `electives.yaml` | inferred elective baskets: **edit and set `authoritative: true`** to override |

To use another workbook: `python -m scripts.ingest path\to\file.xlsm`.

## 3. The dashboard

```powershell
uvicorn ttagent.api.main:app
```

Open http://127.0.0.1:8000.

| Page | What you can do |
|---|---|
| **Overview** | Coverage ring, hard violations, quality KPIs, comparison with the published grid, and hours by day and slot. Use the selector at the top to switch between timetable versions, the workbook's own grid, or the VBA baseline. |
| **Timetable** | Pick Batch / Faculty / Room / Semester and type a code (e.g. `1B1`, `RAVI`, `LT1`, `5`). Colours: blue = lecture, green = tutorial, orange = lab. **⬇ Excel** exports the whole timetable; **⬇ Calendar** exports an `.ics` for that batch, teacher or room. |
| **Agent Studio** | Chat with the agents. While a request runs, the LangGraph diagram lights up each node, the MCP server cards flash when called, and the trace lists every tool call with its arguments and latency. Generating a timetable asks for **approval** before publishing. |
| **System & MCP** | The live architecture. **Connect agent & MCP servers** spawns the 4 servers; this page lists all 30 tools and the MCP audit log. |
| **Data quality** | Workbook issues, learned elective baskets, and the capacity proof. |
| **Methods & compare** | Every allocation method (CP-SAT pipeline, exact CP-SAT, memetic GA, plain GA, VBA port) with its scope. **Run head-to-head** solves the same semesters with each chosen method under the same time budget and shows coverage, violations and soft quality side by side. |
| **Settings** | Change the time budget, soft-constraint weights, Saturday cut-off, approval requirement and default batch size. Changes are saved to `config/overrides.yaml` (your `settings.yaml` is never edited). Upload a new workbook here. |
| **Agent evaluation** | Results of `ttagent agent-eval`: tasks solved and unsafe publications per system variant, what breaks when each agent is removed, and a task-by-variant matrix. |
| **Research results** | Figures from `experiments/results/`. |

**⚡ Generate** (top bar) solves directly without the chat, for all semesters or one, using the chosen time budget
and the method in the **method selector** (CP-SAT / GA / VBA).

## 4. Things to say to the agent

| Goal | Example |
|---|---|
| Build | `Generate timetable for Year 3` (year 1→sem 1, 2→3, 3→5, 4→7) · `Create the timetable for semester 1` · `Generate timetable for all semesters with minimal changes` |
| Check | `Show all conflicts in the current timetable` |
| Understand | `Why could you not schedule the DBMS lab?` · `why is token 104 unplaced` |
| What-if | `RAVI is unavailable on Monday` · `LT1 is not available on Friday` · `JP is unavailable on Tuesday slot 2` |
| Edit | `move R104#0 to Friday slot 2`. The move is committed only if it breaks no rule; otherwise you're told exactly what it would break. |
| Look | `Show timetable of batch 1B1` · `show faculty RAVI timetable` · `display room LT1` |
| Evaluate | `Compare with the published timetable` · `Is it even possible to schedule everything?` · `How good is the data in the Excel workbook?` |
| Output | `export to excel` · `download the calendar for batch 3B1` · `publish it` |

With an LLM key, anything else, such as *"which teacher has the heaviest Friday?"*, goes to the ReAct assistant,
which picks among all 30 MCP tools itself.

## 5. Terminal options

All features are behind one command, `ttagent`:

| Command | Purpose |
|---|---|
| `ttagent serve` | dashboard on http://127.0.0.1:8000 (`--port`, `--no-browser`) |
| `ttagent ingest [file.xlsm]` | parse a workbook |
| `ttagent solve --method decomposed\|exact\|ga\|ga_plain\|vba --time 120 --semesters 3` | solve and export to Excel |
| `ttagent validate` | check the latest timetable with the independent validator |
| `ttagent chat --trace` | terminal chat with the agents |
| `ttagent demo` | scripted demonstration |
| `ttagent benchmark --time 120` | ITC-2007 public benchmark (CP-SAT vs GA); `--only-lns` adds CP-SAT+LNS |
| `ttagent experiments` | research experiments on the real workbook |
| `ttagent agent-eval [--llm]` | agent evaluation: 17 tasks × 7 system variants × injected faults (~35 min) |
| `ttagent llm-eval` | LLM routing, faithfulness and LLM-only baseline (uses your LLM key) |
| `ttagent mcp-tools` | start the 4 MCP servers and list their 30 tools |

The older module commands still work:

```powershell
python -m scripts.chat --trace            # interactive chat, prints the agent path per turn
python -m scripts.chat --thread viva      # resume a saved conversation (memory persists)
python -m scripts.run_demo --semester 5   # scripted demo for presentations
python -m scripts.solve --time 120        # whole university → Excel
python -m scripts.solve --semesters 3 --time 30
python -m scripts.solve --method vba      # the original macro's behaviour, for comparison
python -m scripts.solve --stability 20    # stay close to the published timetable
python -m scripts.solve --method ga --seed 3   # memetic genetic algorithm, for comparison
```

## 5b. See the safeguards live

Start the server with a faulty scheduling tool and watch the agents handle it in **Agent Studio**:

```powershell
$env:TTAGENT_FAULT="corrupt:3"   # the solver silently corrupts 3 sessions but reports "valid"
ttagent serve
```

Ask *"Generate timetable for semester 3"*. The trace shows the validator's independent check finding the clashes,
the repairer fixing them, and only then the approval request. Other faults: `budget:1` (starved solver),
`crash:2` (the first two solver calls fail). Remove the variable (`Remove-Item Env:TTAGENT_FAULT`) for normal use. Each fault fires once per server start.

## 5a. Real seat counts, batch sizes and teacher preferences

The workbook has no seat counts or class sizes, so by default room suitability uses the room category as a proxy.
To use real numbers, copy the templates and fill them in:

| File | Content |
|---|---|
| `config/rooms.yaml` (from `rooms.example.yaml`) | `LT1: 120` — seats per room |
| `config/batches.yaml` (from `batches.example.yaml`) | `3B1: 64` — students per batch |

When both are known, a room is suitable only if seats ≥ the total students of the batches attending.
Re-run `ttagent ingest` afterwards.

Teacher preferences are soft: add them in `config/settings.yaml` under `faculty.avoid`, e.g.
`RAVI: ["Sat", "Mon:1"]` (a whole day, or a day and slot). The solver avoids them when it can, and the
number it could not avoid is reported as `faculty_preference_violations`.

Elective baskets now carry a **confidence** (high / medium / low, from how many co-scheduled cells support
them). Non-high baskets are listed in the data-quality report so a coordinator can confirm them in
`data/processed/electives.yaml`.

## 6. Use the MCP servers from Claude Desktop

Copy the entries from `docs/claude_desktop_config.example.json` into Claude Desktop's
`claude_desktop_config.json` and fix the paths. Claude can then call `generate_timetable`,
`explain_unplaced` and the other tools directly.

## 7. REST API

Interactive docs are at http://127.0.0.1:8000/docs. Main endpoints:

| Method | Path | Purpose |
|---|---|---|
| POST | `/api/solve` | `{"semesters":[3],"time_limit_s":60}` |
| GET | `/api/solutions` · `/api/solutions/{id}` | list versions · summary |
| GET | `/api/solutions/{id}/view?kind=batch&code=1B1` | weekly grid |
| GET | `/api/solutions/{id}/validate` · `/explain` · `/export?fmt=xlsx` | check · diagnose · download |
| POST | `/api/unavailability` · `/api/solutions/{id}/repair` | what-if + repair |
| POST | `/api/chat` · `/api/chat/approve` | talk to the agents · answer an approval |
| GET | `/api/system` · `/api/audit` | live connections · MCP call log |
| POST | `/api/solve` with `"method":"ga"` | choose the allocation method |
| GET/PUT | `/api/settings` | read · change the editable settings (saved to `config/overrides.yaml`) |
| POST | `/api/ingest/upload?filename=x.xlsm` | upload a new workbook (raw body) |
| GET | `/api/version` | version and LLM status |

## 8. Where things are stored

| Path | Content |
|---|---|
| `data/state/timetable.db` | every timetable version (with parent, so edits and repairs form a history), pins, unavailability, MCP audit log |
| `data/state/agent_memory.db` | LangGraph checkpoints: conversations and pending approvals |
| `data/exports/` | Excel / `.ics` / JSON exports |

Deleting `data/state/` resets everything; it is rebuilt on the next run.
