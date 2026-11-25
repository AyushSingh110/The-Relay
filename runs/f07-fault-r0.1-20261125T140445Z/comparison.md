# F-02 overload: f07-fault-r0.1-20261125T140445Z

5 runs per path; values are mean ± sample std [min–max] over runs.

| Metric | v1 old (1 run) | v2 old (1 run) | v1 /ask fixed | v2 /v1/ask fixed |
| :-- | --: | --: | --: | --: |
| Delivered (n of 86) | 58 | 54 | 57.6 ± 0.5 [57–58] | 56.8 ± 0.8 [56–58] |
| Delivered (%) | 67.4 | 62.8 | 67.0 ± 0.6 [66.3–67.4] | 66.0 ± 1.0 [65.1–67.4] |
| Refused (%) | 22.1 | 27.9 | 23.3 ± 0.8 [22.1–24.4] | 24.0 ± 1.3 [22.1–25.6] |
| Dropped (%) | 10.5 | 9.3 | 9.8 ± 0.7 [9.3–10.5] | 10.0 ± 0.7 [9.3–10.5] |
| Gave up (n) | 9 | 8 | 8.4 ± 0.5 [8–9] | 8.6 ± 0.5 [8–9] |
| Shed up front (n) | 19 | 24 | 20.0 ± 0.7 [19–21] | 20.6 ± 1.1 [19–22] |
|   queue full | 11 | 11 | 11.0 ± 0.0 [11–11] | 11.0 ± 0.0 [11–11] |
|   predicted wait > deadline | 8 | 13 | 9.0 ± 0.7 [8–10] | 9.6 ± 1.1 [8–11] |
|   deadline expired in queue | 0 | 0 | 0.0 ± 0.0 [0–0] | 0.0 ± 0.0 [0–0] |
| Provider calls | 56 | 53 | 55.6 ± 0.5 [55–56] | 55.0 ± 0.7 [54–56] |
| Cancelled streams (stand-in disconnects) | 0 | 7 | 0.0 ± 0.0 [0–0] | 0.6 ± 0.9 [0–2] |
| Stopped mid-stream (v2) | 0 | 7 | 0.0 ± 0.0 [0–0] | 2.8 ± 1.3 [2–5] |
| Partial answers | 0 | 3 | 0.0 ± 0.0 [0–0] | 2.0 ± 0.7 [1–3] |
| Delivered VERIFIED | 48 | 45 | 52.6 ± 0.9 [51–53] | 52.0 ± 0.7 [51–53] |
| Delivered BLOCKED | 8 | 7 | 3.0 ± 1.2 [2–5] | 2.8 ± 1.3 [2–5] |
| Delivered EARLY_REFUSAL | 2 | 2 | 2.0 ± 0.0 [2–2] | 2.0 ± 0.0 [2–2] |
| Provider 429s | 0 | 0 | 0.0 ± 0.0 [0–0] | 0.0 ± 0.0 [0–0] |
| Prompt tokens | 25114 | 20970 | 25413.4 ± 55.4 [25328–25473] | 24971.2 ± 543.7 [24133–25502] |
| Completion tokens | 6389 | 5250 | 6221.2 ± 72.2 [6135–6332] | 6124.0 ± 106.7 [5997–6249] |
| Total time p50 (ms) | 811.4 | 826.4 | 916.0 ± 162.0 [770.6–1165.2] | 1315.4 ± 179.2 [1033.2–1479.6] |
| Total time p95 (ms) | 24080.5 | 24008.5 | 24254.8 ± 173.4 [23996.2–24406.9] | 24560.1 ± 171.3 [24345.7–24783.3] |
| First verified text p50 (ms) | 807.7 | 2620.0 | 911.3 ± 160.4 [767.5–1155.9] | 1077.1 ± 349.1 [725.9–1645.6] |
| First verified text p95 (ms) | 24079.3 | 23745.9 | 24251.3 ± 174.2 [23991.0–24401.8] | 24065.8 ± 272.1 [23744.1–24447.6] |

## Per run

| Path | Run | Delivered | Refused | Dropped | Calls | Cancelled | Shed | Gave up |
| :-- | :-- | --: | --: | --: | --: | --: | --: | --: |
| v1_ask | run-1 | 57 | 21 | 8 | 55 | 0 | 21 | 8 |
| v1_ask | run-2 | 57 | 20 | 9 | 55 | 0 | 20 | 9 |
| v1_ask | run-3 | 58 | 19 | 9 | 56 | 0 | 19 | 9 |
| v1_ask | run-4 | 58 | 20 | 8 | 56 | 0 | 20 | 8 |
| v1_ask | run-5 | 58 | 20 | 8 | 56 | 0 | 20 | 8 |
| v2_stream | run-1 | 56 | 22 | 8 | 54 | 1 | 22 | 8 |
| v2_stream | run-2 | 57 | 21 | 8 | 55 | 0 | 21 | 8 |
| v2_stream | run-3 | 56 | 21 | 9 | 55 | 0 | 21 | 9 |
| v2_stream | run-4 | 58 | 19 | 9 | 56 | 2 | 19 | 9 |
| v2_stream | run-5 | 57 | 20 | 9 | 55 | 0 | 20 | 9 |

## Arrivals delivered a different number of times (of 5 runs)

| Arrival | Question | v1 runs delivered | v2 runs delivered |
| --: | :-- | --: | --: |
| 27 | q11 | 4 | 5 |
| 30 | q07 | 4 | 3 |
| 36 | q15 | 4 | 5 |
| 60 | q06 | 5 | 1 |
| 78 | q04 | 5 | 4 |
