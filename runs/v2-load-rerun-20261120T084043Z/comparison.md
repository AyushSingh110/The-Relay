# v2 load re-validation: v2-load-rerun-20261120T084043Z

## overload (v1 exp-20261113T074816Z-c26f18c, v2 v2-load-rerun-20261120T084043Z/overload/run-1)

| Metric | V1 result | V2 result | Change |
|---|---|---|---|
| Requests delivered (%) | 67.4 | 62.8 | -4.6 |
| Requests refused (%) | 22.1 | 27.9 | +5.8 |
| Requests dropped (%) | 10.5 | 9.3 | -1.2 |
| Median total time (ms) | 811.4 | 826.4 | +15.0 |
| p95 total time (ms) | 24080.5 | 24008.5 | -72.0 |
| Provider calls made | 56 | 53 | -3 |
| Cache hits | 0 | 0 | +0 |
| Coalesced (SingleFlight) | 0 | 0 | +0 |
| First sentence p50 (ms) | V2 only | 2620.0 | n/a |
| First sentence p95 (ms) | V2 only | 23745.9 | n/a |

## tpm_halved (v1 exp-20261113T080532Z-c26f18c, v2 v2-load-rerun-20261120T084043Z/tpm_halved/run-1)

| Metric | V1 result | V2 result | Change |
|---|---|---|---|
| Requests delivered (%) | 77.5 | 72.5 | -5.0 |
| Requests refused (%) | 10.0 | 12.5 | +2.5 |
| Requests dropped (%) | 12.5 | 15.0 | +2.5 |
| Median total time (ms) | 715.4 | 688.8 | -26.6 |
| p95 total time (ms) | 19099.5 | 22912.0 | +3812.5 |
| Provider calls made | 30 | 28 | -2 |
| Cache hits | 0 | 0 | +0 |
| Coalesced (SingleFlight) | 0 | 0 | +0 |
| First sentence p50 (ms) | V2 only | 529.7 | n/a |
| First sentence p95 (ms) | V2 only | 22683.6 | n/a |

