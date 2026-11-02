# User guide

A walkthrough of every feature, in the order you would use them.

## 1. Install (once)

```powershell
python -m venv .venv
Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass   # Windows only, per terminal
.venv\Scripts\Activate.ps1                                    # macOS/Linux: source .venv/bin/activate
python -m pip install -r requirements.txt
```

Optional: `copy .env.example .env` and set `GOOGLE_API_KEY=...` (or `ANTHROPIC_API_KEY`).

## 2. Load the workbook

```powershell
python -m scripts.ingest
```

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
| **Research results** | Figures from `experiments/results/`. |

**⚡ Generate** (top bar) solves directly without the chat, for all semesters or one, using the chosen time budget.

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

```powershell
python -m scripts.chat --trace            # interactive chat, prints the agent path per turn
python -m scripts.chat --thread viva      # resume a saved conversation (memory persists)
python -m scripts.run_demo --semester 5   # scripted demo for presentations
python -m scripts.solve --time 120        # whole university → Excel
python -m scripts.solve --semesters 3 --time 30
python -m scripts.solve --method vba      # the original macro's behaviour, for comparison
python -m scripts.solve --stability 20    # stay close to the published timetable
```

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

## 8. Where things are stored

| Path | Content |
|---|---|
| `data/state/timetable.db` | every timetable version (with parent, so edits and repairs form a history), pins, unavailability, MCP audit log |
| `data/state/agent_memory.db` | LangGraph checkpoints: conversations and pending approvals |
| `data/exports/` | Excel / `.ics` / JSON exports |

Deleting `data/state/` resets everything; it is rebuilt on the next run.
