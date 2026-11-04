# User study kit: timetable office, Excel/VBA vs the agent system

This is a ready-to-run protocol for a small within-subjects study with the people who actually maintain the
timetable. It measures what the automated agent evaluation cannot: whether real staff finish real tasks faster
and with fewer errors, and how usable they find the system.

**Time per participant:** about 60 minutes. **Participants:** 5–8 (timetable coordinators, HoD office staff,
faculty who edit the workbook). Five participants already find most usability problems; for timing statistics,
8 is better.

## 0. Analytical estimate before the study (Keystroke-Level Model)

Before running the study with people, the five tasks were analysed with the Keystroke-Level Model (KLM; Card,
Moran & Newell, 1980), which predicts error-free expert time from operator counts: K keystroke 0.28 s, P point
1.1 s, B button 0.1 s, H home 0.4 s, M mental act / reading one item 1.35 s, plus measured system response time R.
Excel-side counts come from the real published grid (755 entries in 40 slot segments; 34 legal starts for a
lecture; the busiest semester-3 teacher NSA has 4 Monday sessions; JP has 21 grid hits and LT1 33). Agent-side
response times were measured on the running system. Script: `python -m scripts.klm_analysis`; results in
`experiments/results/klm/`.

| Task | Excel [s] | Agent [s] | Ratio | Range of ratio |
|---|---:|---:|---:|---:|
| T1 Find every clash in semester 3 | 1063.2 | 35.8 | 29.7× | 24.6–31.9× |
| T2 Explain a missing lecture and where it could go | 347.9 | 60.1 | 5.8× | 5.1–6.0× |
| T3 Teacher absent for a day (4 sessions displaced) | 1299.9 | 22.8 | 57.1× | 47.6–60.1× |
| T4 Move a lab without creating a clash | 19.4 | 31.3 | 0.6× | 0.5–0.7× |
| T5 Weekly timetable of a teacher and a room | 79.0 | 31.7 | 2.5× | 2.1–2.7× |
| **All five** | **2809.4** | **181.7** | **15.5×** | 12.9–16.5× |

The range varies all operator times between "fast" and "slow" settings. The model predicts large savings where
the workbook offers no support (finding clashes, diagnosing, what-if), a small saving for look-ups, and a *loss*
for a single simple move, where editing a cell is quicker than typing a request. These are predictions for an
expert who makes no mistakes; the study below measures real staff, including errors.

## 1. Before you start

* Get the department's permission. Use the consent text in section 6. No personal data beyond role and years
  of experience is recorded.
* Prepare two laptops or one laptop with both set up:
  * **Condition A (current practice):** a copy of `Master_TimeTable_July-Dec-2026.xlsm` with macros enabled.
  * **Condition B (agent system):** double-click `TimetableAgent.bat`; open the **Agent Studio** page.
* Print the task cards (section 3) and the questionnaire (section 5), or use the CSV template.
* Counterbalance: odd-numbered participants do A then B, even-numbered do B then A. Use task set 1 in the
  first condition and task set 2 in the second, so nobody repeats the same task (both sets are matched).

## 2. Procedure

1. Consent and background (role, years of timetabling experience, Excel skill 1–5): 3 min.
2. Training for the first condition: 5 min (Excel: the workbook's layout and macro button; agent: three example
   requests from the chips under the chat box).
3. Task set in the first condition: up to 20 min. Start a stopwatch per task; stop at the participant's
   answer or at 5 min (then record "not completed").
4. SUS questionnaire for that condition: 3 min.
5. Repeat 2–4 for the second condition.
6. Short interview (5 min): "Which would you use next term, and why?", "What did you not trust?",
   "What is missing?".

Record for each task: time in seconds, completed (yes/no), correct (checked by you against the answer key),
and number of hard-rule errors left in the timetable (Condition A: run the validator afterwards with
`ttagent validate`; Condition B: shown by the system).

## 3. Task cards

| # | Set 1 | Set 2 | What it measures |
|---|---|---|---|
| T1 | Find every clash (teacher, batch or room double-booked) in semester 3 | Same for semester 5 | detection |
| T2 | Why is the second weekly CMD lecture of 3B7–3B9 missing? Where could it go? | Why is one AW lecture of 7B1–7B12 missing? Where could it go? | diagnosis |
| T3 | RAVI is on leave next Monday. Produce a valid timetable for semester 3 without RAVI on Monday | Same for teacher AKD, Wednesday, semester 5 | what-if repair |
| T4 | Move one CS lab of 3B1 to Friday afternoon without creating a clash | Same for one lab of 5B2, Thursday afternoon | editing |
| T5 | Produce the weekly timetable of teacher JP and of room LT1 | Same for teacher SG and room LT4 | look-up |

Answer keys: run the system once on the day of the study (`ttagent chat`) and keep its outputs; T1 and T3 are
checked with `ttagent validate`.

## 4. Analysis

Fill `docs/user_study_template.csv` (one row per participant × condition × task) and the SUS answers, then:

```bash
python -m scripts.analyze_user_study docs/user_study_template.csv
```

The script reports, per condition: completion rate, correctness, median time per task, SUS score (0–100),
and paired Wilcoxon signed-rank tests on time and SUS (paired per participant). With 5–8 participants, report
medians and effect sizes, and treat p-values as indicative.

## 5. SUS questionnaire (System Usability Scale, Brooke 1996)

Answer each from 1 (strongly disagree) to 5 (strongly agree), once per condition.

1. I think that I would like to use this system frequently.
2. I found the system unnecessarily complex.
3. I thought the system was easy to use.
4. I think that I would need the support of a technical person to be able to use this system.
5. I found the various functions in this system were well integrated.
6. I thought there was too much inconsistency in this system.
7. I would imagine that most people would learn to use this system very quickly.
8. I found the system very cumbersome to use.
9. I felt very confident using the system.
10. I needed to learn a lot of things before I could get going with this system.

Scoring: odd items contribute (answer − 1), even items (5 − answer); the sum × 2.5 gives 0–100. 68 is about
average; above 80 is good.

## 6. Consent text

> You are invited to take part in a study comparing two ways of maintaining the department timetable. You will
> do five short tasks with each tool and answer a questionnaire. We record task times, whether tasks were
> completed, your questionnaire answers, your role and years of experience. We do not record your name with
> the data. You may stop at any time without giving a reason. The results will be used in a research paper and
> reported only in aggregate.

## 7. Reporting checklist (for the paper)

* number and roles of participants, experience, counterbalancing
* completion, correctness and median time per task and condition, with paired tests and effect sizes
* SUS per condition (mean, SD)
* the main interview themes, with short quotes
* limitations: small sample, one institution, novelty effect, the experimenter is also the developer
