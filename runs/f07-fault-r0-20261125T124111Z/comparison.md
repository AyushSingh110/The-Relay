# F-02 overload: f07-fault-r0-20261125T124111Z

5 runs per path; values are mean ± sample std [min–max] over runs.

| Metric | v1 old (1 run) | v2 old (1 run) | v1 /ask fixed | v2 /v1/ask fixed |
| :-- | --: | --: | --: | --: |
| Delivered (n of 86) | 58 | 54 | 57.2 ± 0.8 [56–58] | 57.0 ± 1.0 [56–58] |
| Delivered (%) | 67.4 | 62.8 | 66.5 ± 1.0 [65.1–67.4] | 66.3 ± 1.2 [65.1–67.4] |
| Refused (%) | 22.1 | 27.9 | 24.0 ± 1.0 [23.3–25.6] | 23.5 ± 1.0 [22.1–24.4] |
| Dropped (%) | 10.5 | 9.3 | 9.5 ± 0.5 [9.3–10.5] | 10.3 ± 0.5 [9.3–10.5] |
| Gave up (n) | 9 | 8 | 8.2 ± 0.4 [8–9] | 8.8 ± 0.4 [8–9] |
| Shed up front (n) | 19 | 24 | 20.6 ± 0.9 [20–22] | 20.2 ± 0.8 [19–21] |
|   queue full | 11 | 11 | 11.0 ± 0.0 [11–11] | 11.0 ± 0.0 [11–11] |
|   predicted wait > deadline | 8 | 13 | 9.6 ± 0.9 [9–11] | 9.2 ± 0.8 [8–10] |
|   deadline expired in queue | 0 | 0 | 0.0 ± 0.0 [0–0] | 0.0 ± 0.0 [0–0] |
| Provider calls | 56 | 53 | 55.2 ± 0.8 [54–56] | 55.6 ± 0.5 [55–56] |
| Cancelled streams (stand-in disconnects) | 0 | 7 | 0.0 ± 0.0 [0–0] | 0.2 ± 0.4 [0–1] |
| Stopped mid-stream (v2) | 0 | 7 | 0.0 ± 0.0 [0–0] | 0.0 ± 0.0 [0–0] |
| Partial answers | 0 | 3 | 0.0 ± 0.0 [0–0] | 0.0 ± 0.0 [0–0] |
| Delivered VERIFIED | 48 | 45 | 55.2 ± 0.8 [54–56] | 55.0 ± 1.0 [54–56] |
| Delivered BLOCKED | 8 | 7 | 0.0 ± 0.0 [0–0] | 0.0 ± 0.0 [0–0] |
| Delivered EARLY_REFUSAL | 2 | 2 | 2.0 ± 0.0 [2–2] | 2.0 ± 0.0 [2–2] |
| Provider 429s | 0 | 0 | 0.0 ± 0.0 [0–0] | 0.0 ± 0.0 [0–0] |
| Prompt tokens | 25114 | 20970 | 25359.4 ± 191.1 [25118–25582] | 25327.0 ± 225.9 [24972–25587] |
| Completion tokens | 6389 | 5250 | 6145.8 ± 115.2 [6029–6267] | 6253.2 ± 53.7 [6210–6337] |
| Total time p50 (ms) | 811.4 | 826.4 | 943.9 ± 207.6 [783.5–1216.1] | 1550.2 ± 536.3 [1085.6–2392.4] |
| Total time p95 (ms) | 24080.5 | 24008.5 | 24170.0 ± 374.3 [23762.0–24661.9] | 24661.0 ± 558.1 [24164.9–25579.6] |
| First verified text p50 (ms) | 807.7 | 2620.0 | 940.8 ± 207.8 [781.3–1212.8] | 970.7 ± 249.8 [679.1–1353.7] |
| First verified text p95 (ms) | 24079.3 | 23745.9 | 24165.8 ± 374.5 [23757.7–24660.5] | 24079.5 ± 413.7 [23715.4–24683.3] |

## Per run

| Path | Run | Delivered | Refused | Dropped | Calls | Cancelled | Shed | Gave up |
| :-- | :-- | --: | --: | --: | --: | --: | --: | --: |
| v1_ask | run-1 | 57 | 21 | 8 | 55 | 0 | 21 | 8 |
| v1_ask | run-2 | 57 | 20 | 9 | 55 | 0 | 20 | 9 |
| v1_ask | run-3 | 58 | 20 | 8 | 56 | 0 | 20 | 8 |
| v1_ask | run-4 | 56 | 22 | 8 | 54 | 0 | 22 | 8 |
| v1_ask | run-5 | 58 | 20 | 8 | 56 | 0 | 20 | 8 |
| v2_stream | run-1 | 56 | 21 | 9 | 55 | 0 | 21 | 9 |
| v2_stream | run-2 | 58 | 20 | 8 | 56 | 0 | 20 | 8 |
| v2_stream | run-3 | 58 | 19 | 9 | 56 | 0 | 19 | 9 |
| v2_stream | run-4 | 57 | 20 | 9 | 56 | 1 | 20 | 9 |
| v2_stream | run-5 | 56 | 21 | 9 | 55 | 0 | 21 | 9 |

## Arrivals delivered a different number of times (of 5 runs)

| Arrival | Question | v1 runs delivered | v2 runs delivered |
| --: | :-- | --: | --: |
| 31 | q11 | 4 | 5 |
| 33 | q08 | 5 | 3 |
| 35 | q12 | 4 | 5 |
| 36 | q15 | 3 | 5 |
| 43 | q12 | 5 | 2 |
| 60 | q06 | 4 | 3 |
| 77 | q10 | 4 | 5 |
