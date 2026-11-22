# F-02 overload: f02c-overload-20261122T164234Z

5 runs per path; values are mean ± sample std [min–max] over runs.

| Metric | v1 old (1 run) | v2 old (1 run) | v1 /ask fixed | v2 /v1/ask fixed |
| :-- | --: | --: | --: | --: |
| Delivered (n of 86) | 58 | 54 | 57.2 ± 0.4 [57–58] | 57.0 ± 1.0 [56–58] |
| Delivered (%) | 67.4 | 62.8 | 66.5 ± 0.5 [66.3–67.4] | 66.3 ± 1.2 [65.1–67.4] |
| Refused (%) | 22.1 | 27.9 | 23.7 ± 1.0 [22.1–24.4] | 23.5 ± 1.5 [22.1–25.6] |
| Dropped (%) | 10.5 | 9.3 | 9.8 ± 0.7 [9.3–10.5] | 10.3 ± 0.5 [9.3–10.5] |
| Gave up (n) | 9 | 8 | 8.4 ± 0.5 [8–9] | 8.8 ± 0.4 [8–9] |
| Shed up front (n) | 19 | 24 | 20.4 ± 0.9 [19–21] | 20.2 ± 1.3 [19–22] |
|   queue full | 11 | 11 | 11.0 ± 0.0 [11–11] | 11.0 ± 0.0 [11–11] |
|   predicted wait > deadline | 8 | 13 | 9.4 ± 0.9 [8–10] | 9.2 ± 1.3 [8–11] |
|   deadline expired in queue | 0 | 0 | 0.0 ± 0.0 [0–0] | 0.0 ± 0.0 [0–0] |
| Provider calls | 56 | 53 | 55.2 ± 0.4 [55–56] | 55.2 ± 0.8 [54–56] |
| Cancelled streams (stand-in disconnects) | 0 | 7 | 0.0 ± 0.0 [0–0] | 0.0 ± 0.0 [0–0] |
| Stopped mid-stream (v2) | 0 | 7 | 0.0 ± 0.0 [0–0] | 0.0 ± 0.0 [0–0] |
| Partial answers | 0 | 3 | 0.0 ± 0.0 [0–0] | 0.0 ± 0.0 [0–0] |
| Delivered VERIFIED | 48 | 45 | 55.2 ± 0.4 [55–56] | 55.0 ± 1.0 [54–56] |
| Delivered BLOCKED | 8 | 7 | 0.0 ± 0.0 [0–0] | 0.0 ± 0.0 [0–0] |
| Delivered EARLY_REFUSAL | 2 | 2 | 2.0 ± 0.0 [2–2] | 2.0 ± 0.0 [2–2] |
| Provider 429s | 0 | 0 | 0.0 ± 0.0 [0–0] | 0.0 ± 0.0 [0–0] |
| Prompt tokens | 25114 | 20970 | 25406.0 ± 117.1 [25290–25560] | 25382.4 ± 64.4 [25328–25491] |
| Completion tokens | 6389 | 5250 | 6186.8 ± 42.9 [6116–6233] | 6184.2 ± 85.5 [6070–6279] |
| Total time p50 (ms) | 811.4 | 826.4 | 988.2 ± 103.0 [806.5–1052.7] | 1160.0 ± 71.0 [1079.1–1260.4] |
| Total time p95 (ms) | 24080.5 | 24008.5 | 24234.5 ± 275.8 [24082.0–24726.4] | 24787.0 ± 651.1 [24362.8–25940.2] |
| First verified text p50 (ms) | 807.7 | 2620.0 | 985.9 ± 102.8 [804.6–1049.8] | 824.5 ± 165.1 [627.1–986.1] |
| First verified text p95 (ms) | 24079.3 | 23745.9 | 24231.8 ± 275.2 [24078.9–24722.6] | 24177.8 ± 585.8 [23741.5–25153.2] |

## Per run

| Path | Run | Delivered | Refused | Dropped | Calls | Cancelled | Shed | Gave up |
| :-- | :-- | --: | --: | --: | --: | --: | --: | --: |
| v1_ask | run-1 | 57 | 21 | 8 | 55 | 0 | 21 | 8 |
| v1_ask | run-2 | 57 | 20 | 9 | 55 | 0 | 20 | 9 |
| v1_ask | run-3 | 57 | 21 | 8 | 55 | 0 | 21 | 8 |
| v1_ask | run-4 | 58 | 19 | 9 | 56 | 0 | 19 | 9 |
| v1_ask | run-5 | 57 | 21 | 8 | 55 | 0 | 21 | 8 |
| v2_stream | run-1 | 58 | 19 | 9 | 56 | 0 | 19 | 9 |
| v2_stream | run-2 | 56 | 22 | 8 | 54 | 0 | 22 | 8 |
| v2_stream | run-3 | 56 | 21 | 9 | 55 | 0 | 21 | 9 |
| v2_stream | run-4 | 58 | 19 | 9 | 56 | 0 | 19 | 9 |
| v2_stream | run-5 | 57 | 20 | 9 | 55 | 0 | 20 | 9 |

## Arrivals delivered a different number of times (of 5 runs)

| Arrival | Question | v1 runs delivered | v2 runs delivered |
| --: | :-- | --: | --: |
| 27 | q11 | 4 | 5 |
| 30 | q07 | 4 | 2 |
| 34 | q06 | 2 | 4 |
| 35 | q12 | 5 | 4 |
| 60 | q06 | 3 | 2 |
