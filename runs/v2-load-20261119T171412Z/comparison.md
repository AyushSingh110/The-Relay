# v2 load re-validation: v2-load-20261119T171412Z

## steady (v1 exp-20261113T074251Z-c26f18c, v2 steady/v2-load-20261119T171412Z/steady/run-2)

| Metric | V1 result | V2 result | Change |
|---|---|---|---|
| Requests delivered (%) | 100.0 | 100.0 | +0.0 |
| Requests refused (%) | 0.0 | 0.0 | +0.0 |
| Requests dropped (%) | 0.0 | 0.0 | +0.0 |
| Median total time (ms) | 701.1 | 652.8 | -48.3 |
| p95 total time (ms) | 840.1 | 752.4 | -87.7 |
| Provider calls made | 19 | 19 | +0 |
| Cache hits | 0 | 0 | +0 |
| Coalesced (SingleFlight) | 0 | 0 | +0 |
| First sentence p50 (ms) | V2 only | 462.8 | n/a |
| First sentence p95 (ms) | V2 only | 608.7 | n/a |

## burst_cache_on (v1 exp-20261113T074759Z-c26f18c, v2 burst_cache_on/v2-load-20261119T171412Z/burst_cache_on/run-2)

| Metric | V1 result | V2 result | Change |
|---|---|---|---|
| Requests delivered (%) | 96.7 | 100.0 | +3.3 |
| Requests refused (%) | 3.3 | 0.0 | -3.3 |
| Requests dropped (%) | 0.0 | 0.0 | +0.0 |
| Median total time (ms) | 614.2 | 498.8 | -115.4 |
| p95 total time (ms) | 1497.6 | 1441.2 | -56.4 |
| Provider calls made | 6 | 6 | +0 |
| Cache hits | 7 | 7 | +0 |
| Coalesced (SingleFlight) | 16 | 17 | +1 |
| First sentence p50 (ms) | V2 only | 324.5 | n/a |
| First sentence p95 (ms) | V2 only | 1215.9 | n/a |

## burst_cache_off (v1 exp-20261113T074807Z-c26f18c, v2 burst_cache_off/v2-load-20261119T171412Z/burst_cache_off/run-2)

| Metric | V1 result | V2 result | Change |
|---|---|---|---|
| Requests delivered (%) | 26.7 | 30.0 | +3.3 |
| Requests refused (%) | 73.3 | 70.0 | -3.3 |
| Requests dropped (%) | 0.0 | 0.0 | +0.0 |
| Median total time (ms) | 1341.2 | 1386.4 | +45.2 |
| p95 total time (ms) | 1786.5 | 1749.7 | -36.8 |
| Provider calls made | 8 | 9 | +1 |
| Cache hits | 0 | 0 | +0 |
| Coalesced (SingleFlight) | 0 | 0 | +0 |
| First sentence p50 (ms) | V2 only | 1156.3 | n/a |
| First sentence p95 (ms) | V2 only | 1556.2 | n/a |

## tpm_halved (v1 exp-20261113T080532Z-c26f18c, v2 tpm_halved/v2-load-20261119T171412Z/tpm_halved/run-2)

| Metric | V1 result | V2 result | Change |
|---|---|---|---|
| Requests delivered (%) | 77.5 | 72.5 | -5.0 |
| Requests refused (%) | 10.0 | 0.0 | -10.0 |
| Requests dropped (%) | 12.5 | 27.5 | +15.0 |
| Median total time (ms) | 715.4 | 665.6 | -49.8 |
| p95 total time (ms) | 19099.5 | 22839.4 | +3739.9 |
| Provider calls made | 30 | 28 | -2 |
| Cache hits | 0 | 0 | +0 |
| Coalesced (SingleFlight) | 0 | 0 | +0 |
| First sentence p50 (ms) | V2 only | 503.8 | n/a |
| First sentence p95 (ms) | V2 only | 22616.3 | n/a |

## overload (v1 exp-20261113T074816Z-c26f18c, v2 overload/v2-load-20261119T171412Z/overload/run-2)

| Metric | V1 result | V2 result | Change |
|---|---|---|---|
| Requests delivered (%) | 67.4 | 55.8 | -11.6 |
| Requests refused (%) | 22.1 | 23.3 | +1.2 |
| Requests dropped (%) | 10.5 | 20.9 | +10.4 |
| Median total time (ms) | 811.4 | 1021.0 | +209.6 |
| p95 total time (ms) | 24080.5 | 34740.9 | +10660.4 |
| Provider calls made | 56 | 47 | -9 |
| Cache hits | 0 | 0 | +0 |
| Coalesced (SingleFlight) | 0 | 0 | +0 |
| First sentence p50 (ms) | V2 only | 1963.7 | n/a |
| First sentence p95 (ms) | V2 only | 34622.5 | n/a |

