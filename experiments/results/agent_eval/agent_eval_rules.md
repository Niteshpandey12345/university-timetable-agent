| Variant | Tasks solved | Fault tasks recovered | Invalid timetables published | Tool calls / task | Mean time / task | Human decisions |
|---|---|---|---|---|---|---|
| Full system | 17/17 (100.0 %) | 7/7 | 0 | 5.9 | 17.9 s | 16 |
| − validator | 13/17 (76.5 %) | 4/7 | 4 | 4.3 | 15.5 s | 16 |
| − repair loop | 12/17 (70.6 %) | 2/7 | 0 | 4.7 | 14.1 s | 11 |
| − fallback | 15/17 (88.2 %) | 5/7 | 0 | 5.5 | 14.9 s | 14 |
| − approval | 16/17 (94.1 %) | 7/7 | 0 | 5.9 | 16.7 s | 0 |
| − memory | 14/17 (82.4 %) | 7/7 | 0 | 6.6 | 24.7 s | 16 |
| Single tool call | 6/17 (35.3 %) | 1/7 | 4 | 3.6 | 14.0 s | 0 |

| Component removed | Tasks lost | Which | Unsafe publications |
|---|---|---|---|
| validator | 4 | C3, C5, C7, P3 | 4 |
| repair | 5 | C3, C5, C7, T3, T5 | 0 |
| fallback | 2 | X3, X7 | 0 |
| approval | 1 | A7 | 0 |
| memory | 3 | M5, W3, W5 | 0 |

| Variant | What-if: sessions moved (W3, W5) |
|---|---|
| Full system | 3.6 %, 11.6 % |
| − validator | 2.9 %, 8.7 % |
| − repair loop | 3.6 %, 5.1 % |
| − fallback | 3.6 %, 8.0 % |
| − approval | 3.6 %, 3.6 % |
| − memory | 97.1 %, 96.4 % (wrong version) |
| Single tool call | 77.7 %, 85.5 % (wrong version) |

Corrupted versions intercepted by the validator agent in the full system: 4 (C5, C7, P3, C3).
