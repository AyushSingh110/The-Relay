# v2 load re-validation: v2-load-rerun-20261120T084027Z

## overload (v1 exp-20261113T074816Z-c26f18c, v2 v2-load-rerun-20261120T084027Z/overload/run-1)

| Metric | V1 result | V2 result | Change |
|---|---|---|---|
| Requests delivered (%) | 67.4 | 60.5 | -6.9 |
| Requests refused (%) | 22.1 | 30.2 | +8.1 |
| Requests dropped (%) | 10.5 | 9.3 | -1.2 |
| Median total time (ms) | 811.4 | 744.8 | -66.6 |
| p95 total time (ms) | 24080.5 | 24773.7 | +693.2 |
| Provider calls made | 56 | 51 | -5 |
| Cache hits | 0 | 0 | +0 |
| Coalesced (SingleFlight) | 0 | 0 | +0 |
| First sentence p50 (ms) | V2 only | 956.5 | n/a |
| First sentence p95 (ms) | V2 only | 24606.0 | n/a |

## tpm_halved (v1 exp-20261113T080532Z-c26f18c, v2 v2-load-rerun-20261120T084027Z/tpm_halved/run-1)

| Metric | V1 result | V2 result | Change |
|---|---|---|---|
| Requests delivered (%) | 77.5 | 70.0 | -7.5 |
| Requests refused (%) | 10.0 | 17.5 | +7.5 |
| Requests dropped (%) | 12.5 | 12.5 | +0.0 |
| Median total time (ms) | 715.4 | 716.8 | +1.4 |
| p95 total time (ms) | 19099.5 | 21975.5 | +2876.0 |
| Provider calls made | 30 | 27 | -3 |
| Cache hits | 0 | 0 | +0 |
| Coalesced (SingleFlight) | 0 | 0 | +0 |
| First sentence p50 (ms) | V2 only | 539.7 | n/a |
| First sentence p95 (ms) | V2 only | 21864.9 | n/a |

