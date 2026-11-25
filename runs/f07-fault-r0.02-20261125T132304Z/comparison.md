# F-02 overload: f07-fault-r0.02-20261125T132304Z

5 runs per path; values are mean ± sample std [min–max] over runs.

| Metric | v1 old (1 run) | v2 old (1 run) | v1 /ask fixed | v2 /v1/ask fixed |
| :-- | --: | --: | --: | --: |
| Delivered (n of 86) | 58 | 54 | 56.8 ± 0.4 [56–57] | 57.0 ± 1.0 [56–58] |
| Delivered (%) | 67.4 | 62.8 | 66.1 ± 0.5 [65.1–66.3] | 66.3 ± 1.2 [65.1–67.4] |
| Refused (%) | 22.1 | 27.9 | 24.2 ± 0.5 [23.3–24.4] | 23.7 ± 1.0 [22.1–24.4] |
| Dropped (%) | 10.5 | 9.3 | 9.8 ± 0.7 [9.3–10.5] | 10.0 ± 0.7 [9.3–10.5] |
| Gave up (n) | 9 | 8 | 8.4 ± 0.5 [8–9] | 8.6 ± 0.5 [8–9] |
| Shed up front (n) | 19 | 24 | 20.8 ± 0.4 [20–21] | 20.4 ± 0.9 [19–21] |
|   queue full | 11 | 11 | 11.0 ± 0.0 [11–11] | 11.0 ± 0.0 [11–11] |
|   predicted wait > deadline | 8 | 13 | 9.8 ± 0.4 [9–10] | 9.4 ± 0.9 [8–10] |
|   deadline expired in queue | 0 | 0 | 0.0 ± 0.0 [0–0] | 0.0 ± 0.0 [0–0] |
| Provider calls | 56 | 53 | 55.0 ± 0.0 [55–55] | 55.4 ± 0.5 [55–56] |
| Cancelled streams (stand-in disconnects) | 0 | 7 | 0.0 ± 0.0 [0–0] | 0.0 ± 0.0 [0–0] |
| Stopped mid-stream (v2) | 0 | 7 | 0.0 ± 0.0 [0–0] | 0.2 ± 0.4 [0–1] |
| Partial answers | 0 | 3 | 0.0 ± 0.0 [0–0] | 0.2 ± 0.4 [0–1] |
| Delivered VERIFIED | 48 | 45 | 54.4 ± 0.5 [54–55] | 54.8 ± 0.8 [54–56] |
| Delivered BLOCKED | 8 | 7 | 0.4 ± 0.5 [0–1] | 0.2 ± 0.4 [0–1] |
| Delivered EARLY_REFUSAL | 2 | 2 | 2.0 ± 0.0 [2–2] | 2.0 ± 0.0 [2–2] |
| Provider 429s | 0 | 0 | 0.0 ± 0.0 [0–0] | 0.0 ± 0.0 [0–0] |
| Prompt tokens | 25114 | 20970 | 25263.0 ± 387.7 [24571–25481] | 25269.4 ± 89.8 [25114–25337] |
| Completion tokens | 6389 | 5250 | 6103.0 ± 50.9 [6048–6175] | 6206.4 ± 50.6 [6133–6270] |
| Total time p50 (ms) | 811.4 | 826.4 | 1075.3 ± 130.1 [926.7–1225.1] | 1175.5 ± 129.1 [981.5–1309.3] |
| Total time p95 (ms) | 24080.5 | 24008.5 | 24017.0 ± 93.2 [23938.7–24170.4] | 24575.3 ± 309.0 [24244.5–25002.2] |
| First verified text p50 (ms) | 807.7 | 2620.0 | 1072.3 ± 129.7 [924.4–1220.8] | 867.5 ± 127.4 [670.1–992.7] |
| First verified text p95 (ms) | 24079.3 | 23745.9 | 24013.0 ± 92.5 [23934.8–24165.9] | 24128.4 ± 418.1 [23623.2–24541.2] |

## Per run

| Path | Run | Delivered | Refused | Dropped | Calls | Cancelled | Shed | Gave up |
| :-- | :-- | --: | --: | --: | --: | --: | --: | --: |
| v1_ask | run-1 | 57 | 21 | 8 | 55 | 0 | 21 | 8 |
| v1_ask | run-2 | 56 | 21 | 9 | 55 | 0 | 21 | 9 |
| v1_ask | run-3 | 57 | 20 | 9 | 55 | 0 | 20 | 9 |
| v1_ask | run-4 | 57 | 21 | 8 | 55 | 0 | 21 | 8 |
| v1_ask | run-5 | 57 | 21 | 8 | 55 | 0 | 21 | 8 |
| v2_stream | run-1 | 57 | 21 | 8 | 55 | 0 | 21 | 8 |
| v2_stream | run-2 | 58 | 20 | 8 | 56 | 0 | 20 | 8 |
| v2_stream | run-3 | 56 | 21 | 9 | 55 | 0 | 21 | 9 |
| v2_stream | run-4 | 56 | 21 | 9 | 55 | 0 | 21 | 9 |
| v2_stream | run-5 | 58 | 19 | 9 | 56 | 0 | 19 | 9 |

## Arrivals delivered a different number of times (of 5 runs)

| Arrival | Question | v1 runs delivered | v2 runs delivered |
| --: | :-- | --: | --: |
| 27 | q11 | 5 | 4 |
| 30 | q07 | 5 | 4 |
| 34 | q06 | 2 | 3 |
| 35 | q12 | 4 | 5 |
| 36 | q15 | 3 | 4 |
