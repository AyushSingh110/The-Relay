# Phase V2-14 Report — Load Re-validation, README and Demo

- **Date:** 2026-11-20
- **Phase:** V2-14 (`docs/IMPLEMENTATION_PLAN_V2.md` §V2-14), parts 1–5
- **Status:** completed, pending owner review. Two scenarios still deliver less than v1; see §3.
- **Real Groq calls:** 0 in every part. `results.csv` and `evidence/` are unchanged.

Sections 1–5 are the owner-specified report. Sections 6–19 are the implementation record that
`CLAUDE.md` §6B requires.

## 1. What this phase did

**What was measured.** The five v1 load scenarios (steady, burst with the cache on, burst with
the cache off, halved provider TPM, overload) were replayed against v2's streaming endpoint
`POST /v1/ask`, using the same calibrated stand-in model. Nothing was re-specified. Each
scenario was taken from its canonical v1 run in `evidence/experiments/` (read only):
- the recorded arrival times, questions and client patience;
- the ProviderGuard limits, queue capacity and tokens-per-answer basis;
- the 30 s deadline;
- the stand-in overrides.

Each v2 run used a fresh stand-in, store and cache. Each scenario ran twice, and the second run
is compared (`runs/v2-load-20261119T171412Z`). The two scenarios that regressed were re-run
once after the fixes (`runs/v2-load-rerun-20261120T084043Z`).

**What was fixed and why.** The first comparison showed overload and tpm_halved delivering less
than v1. There were two causes:
- **Cancelled streams were overcharged.** When the per-sentence verifier stops a streamed
  answer, the provider's usage report never arrives. The ProviderGuard then charged the whole
  512-token completion allowance as if it had been used, so fewer calls fit into each minute.
  It now charges the prompt estimate plus the text actually received (content and reasoning).
  It still treats the provider's own headroom, as reported in rate-limit headers, as reduced
  until the provider says otherwise.
- **`/v1/ask` had no `deadline_ms`.** So v1's up-front rejection of requests predicted to wait
  too long was missing. It now takes `deadline_ms` exactly as v1's `/ask` does: admission, guard
  waits and retries, and a coalesced follower's own wait.

A cached answer now also replays the same passages as the live answer.

## 2. Before/after comparison

Delivered = status `ok`: an answer, a stopped (partial) answer or a refusal reached the user.
Refused = `rate_limited`: the service declined up front (queue full, predicted wait, budget).
Dropped = `timeout` or `error`: the user gave up, or the request failed. Times are as the client
saw them. Percentiles are nearest-rank, over delivered requests. Provider calls are the calls
the stand-in received. The same code computes v1 and v2 metrics, and it reproduces v1's own
summary (tested).

### steady

V1 `exp-20261113T074251Z-c26f18c`; V2 `v2-load-20261119T171412Z/steady/run-2` (not re-run, so
before and after are the same run).

| Metric | V1 | V2 (before fixes) | V2 (after fixes) | Change (V1 → final V2) |
| :-- | --: | --: | --: | --: |
| Delivered (%) | 100.0 | 100.0 | 100.0 | +0.0 |
| Refused (%) | 0.0 | 0.0 | 0.0 | +0.0 |
| Dropped (%) | 0.0 | 0.0 | 0.0 | +0.0 |
| Median total (ms) | 701.1 | 652.8 | 652.8 | -48.3 |
| p95 total (ms) | 840.1 | 752.4 | 752.4 | -87.7 |
| Provider calls | 19 | 19 | 19 | +0 |
| Cache hits | 0 | 0 | 0 | +0 |
| Coalesced | 0 | 0 | 0 | +0 |
| First-sentence p50 (ms) | n/a | 462.8 | 462.8 | V2 only |
| First-sentence p95 (ms) | n/a | 608.7 | 608.7 | V2 only |

Delivered / refused / dropped, of 19: V1 19 / 0 / 0; V2 19 / 0 / 0.

### burst_cache_on

V1 `exp-20261113T074759Z-c26f18c`; V2 `v2-load-20261119T171412Z/burst_cache_on/run-2` (not
re-run).

| Metric | V1 | V2 (before fixes) | V2 (after fixes) | Change (V1 → final V2) |
| :-- | --: | --: | --: | --: |
| Delivered (%) | 96.7 | 100.0 | 100.0 | +3.3 |
| Refused (%) | 3.3 | 0.0 | 0.0 | -3.3 |
| Dropped (%) | 0.0 | 0.0 | 0.0 | +0.0 |
| Median total (ms) | 614.2 | 498.8 | 498.8 | -115.4 |
| p95 total (ms) | 1,497.6 | 1,441.2 | 1,441.2 | -56.4 |
| Provider calls | 6 | 6 | 6 | +0 |
| Cache hits | 7 | 7 | 7 | +0 |
| Coalesced | 16 | 17 | 17 | +1 |
| First-sentence p50 (ms) | n/a | 324.5 | 324.5 | V2 only |
| First-sentence p95 (ms) | n/a | 1,215.9 | 1,215.9 | V2 only |

Delivered / refused / dropped, of 30: V1 29 / 1 / 0; V2 30 / 0 / 0.

### burst_cache_off

V1 `exp-20261113T074807Z-c26f18c`; V2 `v2-load-20261119T171412Z/burst_cache_off/run-2` (not
re-run).

| Metric | V1 | V2 (before fixes) | V2 (after fixes) | Change (V1 → final V2) |
| :-- | --: | --: | --: | --: |
| Delivered (%) | 26.7 | 30.0 | 30.0 | +3.3 |
| Refused (%) | 73.3 | 70.0 | 70.0 | -3.3 |
| Dropped (%) | 0.0 | 0.0 | 0.0 | +0.0 |
| Median total (ms) | 1,341.2 | 1,386.4 | 1,386.4 | +45.2 |
| p95 total (ms) | 1,786.5 | 1,749.7 | 1,749.7 | -36.8 |
| Provider calls | 8 | 9 | 9 | +1 |
| Cache hits | 0 | 0 | 0 | +0 |
| Coalesced | 0 | 0 | 0 | +0 |
| First-sentence p50 (ms) | n/a | 1,156.3 | 1,156.3 | V2 only |
| First-sentence p95 (ms) | n/a | 1,556.2 | 1,556.2 | V2 only |

Delivered / refused / dropped, of 30: V1 8 / 22 / 0; V2 9 / 21 / 0.

### tpm_halved¹

V1 `exp-20261113T080532Z-c26f18c`; V2 before fixes `v2-load-20261119T171412Z/tpm_halved/run-2`;
V2 after fixes `v2-load-rerun-20261120T084043Z/tpm_halved/run-1`.

| Metric | V1 | V2 (before fixes) | V2 (after fixes) | Change (V1 → final V2) |
| :-- | --: | --: | --: | --: |
| Delivered (%) | 77.5 | 72.5 | 72.5 | -5.0 |
| Refused (%) | 10.0 | 0.0 | 12.5 | +2.5 |
| Dropped (%) | 12.5 | 27.5 | 15.0 | +2.5 |
| Median total (ms) | 715.4 | 665.6 | 688.8 | -26.6 |
| p95 total (ms) | 19,099.5 | 22,839.4 | 22,912.0 | +3,812.5 |
| Provider calls | 30 | 28 | 28 | -2 |
| Cache hits | 0 | 0 | 0 | +0 |
| Coalesced | 0 | 0 | 0 | +0 |
| First-sentence p50 (ms) | n/a | 503.8 | 529.7 | V2 only |
| First-sentence p95 (ms) | n/a | 22,616.3 | 22,683.6 | V2 only |

Delivered / refused / dropped, of 40: V1 31 / 4 / 5; V2 before 29 / 0 / 11; V2 after
29 / 5 / 6.

### overload¹

V1 `exp-20261113T074816Z-c26f18c`; V2 before fixes `v2-load-20261119T171412Z/overload/run-2`;
V2 after fixes `v2-load-rerun-20261120T084043Z/overload/run-1`.

| Metric | V1 | V2 (before fixes) | V2 (after fixes) | Change (V1 → final V2) |
| :-- | --: | --: | --: | --: |
| Delivered (%) | 67.4 | 55.8 | 62.8 | -4.6 |
| Refused (%) | 22.1 | 23.3 | 27.9 | +5.8 |
| Dropped (%) | 10.5 | 20.9 | 9.3 | -1.2 |
| Median total (ms) | 811.4 | 1,021.0 | 826.4 | +15.0 |
| p95 total (ms) | 24,080.5 | 34,740.9 | 24,008.5 | -72.0 |
| Provider calls | 56 | 47 | 53 | -3 |
| Cache hits | 0 | 0 | 0 | +0 |
| Coalesced | 0 | 0 | 0 | +0 |
| First-sentence p50 (ms) | n/a | 1,963.7 | 2,620.0 | V2 only |
| First-sentence p95 (ms) | n/a | 34,622.5 | 23,745.9 | V2 only |

Delivered / refused / dropped, of 86: V1 58 / 19 / 9; V2 before 48 / 20 / 18; V2 after
54 / 24 / 8.

¹ **Re-run after the fixes.** These two scenarios still deliver less than v1 (§3). Their
first-sentence figures in the "after fixes" column come from the re-run. The owner's brief
listed the pre-fix figures (503.8 / 22,616.3 ms and 1,963.7 / 34,622.5 ms); those are kept in
the "before fixes" column.

## 3. Regressions explained

### overload

- **Answers stopped mid-stream: 7.** The stand-in recorded 7 disconnected streams in this run;
  v1's overload run recorded 0. Every v1 stream ran to completion. In v2 the per-sentence
  verifier stopped 7 answers (all for an uncited sentence), and stopping an answer cancels its
  model stream.
- **Why this costs provider quota.** At the start of each call, the stand-in reserves the
  prompt plus the full 512-token completion allowance against its per-minute budget. It settles
  that reservation down to actual use only when a stream completes. A cancelled stream keeps the
  whole reservation for the rest of the minute. The ProviderGuard reads the provider's remaining
  tokens from the rate-limit headers, so it correctly sees less headroom and lets fewer calls
  start. This settlement behaviour is measured for the stand-in only. Whether Groq keeps or
  refunds a cancelled stream's reservation has not been observed.
- **What the fixes did.**
  - Users giving up fell from 18 to 8 (v1: 9). The guard no longer charges itself for text that
    was never generated, and requests predicted to wait too long are again refused at the door
    (13 here; v1: 8).
  - Delivered rose from 48 to 54, closing 6 of the 10-request gap to v1.
  - Model calls rose from 47 to 53 (v1: 56).
  - No run produced a 429 from the stand-in, so the guard never overshot.
- **What the fixes did not do.** They cannot recover quota that the provider itself still holds
  for a cancelled stream. The remaining 4 requests (54 vs 58) ended as up-front refusals instead
  of answers: 24 refused against v1's 19, while give-ups (8) are below v1's (9).
- **Quantified cost.** Each cancelled stream left about 446 unused tokens reserved at the
  provider: 512 minus the 15–153 tokens it had produced when it was stopped. Over 7 streams
  that is about 3,100 tokens of per-minute headroom per run. At the stand-in's reservation of
  roughly 1,000 tokens per call, that is about 3 calls, and v2 made 3 fewer calls than v1 (53 vs
  56). This is the measurable cost of stopping an unverified answer early.

### tpm_halved

V1 delivered 31 of 40 (77.5 %) and v2 29 of 40 (72.5 %), a 2-request difference.
- **Evidence available:** one canonical v1 run, and three v2 runs of the same schedule, which
  delivered 28, 29 and 29.
- **Conclusion:** with that evidence, a 2-request gap **cannot be distinguished from run-to-run
  noise**. It is not shown to be resolved, and it is not shown to be real.
- **Same mechanism present:** this run had 5 stopped streams and 5 stand-in disconnects (v1:
  0). So a small real effect of the same origin as overload's is plausible. Telling the two
  apart would need repeated v1 and v2 runs.

## 4. What V2 adds over V1

- **burst_cache_on: 96.7 % → 100 %.** V1 refused one request (q14, `queue_full`). In v2 the
  same arrival was admitted and answered (a cache miss); everything else was answered from 6
  model calls, 7 cache hits and 17 coalesced requests. The one-request difference reflects
  queue timing within the burst. It was not caught by SingleFlight.
- **steady: median total 701.1 ms → 652.8 ms (6.9 % faster); p95 840.1 ms → 752.4 ms.**
- **burst_cache_off: 26.7 % → 30.0 %**, that is 8 → 9 of 30. V2's two runs delivered 8 and 9,
  so this one-request gain is within run-to-run variation, not a design improvement. Without a
  cache or coalescing, both versions are bounded by the same queue capacity (3) and guard.
- **First-sentence latency**, a metric v1 did not have. V1 sent each answer in one piece after
  verifying it whole.

| Scenario | First-sentence p50 (ms) | First-sentence p95 (ms) |
| :-- | --: | --: |
| steady | 462.8 | 608.7 |
| burst_cache_on | 324.5 | 1,215.9 |
| burst_cache_off | 1,156.3 | 1,556.2 |
| tpm_halved (re-run) | 529.7 | 22,683.6 |
| overload (re-run) | 2,620.0 | 23,745.9 |

In every scenario the `accepted` event arrived at a median of 6.9–14.1 ms, so the user sees the
request is in progress at once, including in the queue.

## 5. Conclusion

V2 matches or beats v1 in three scenarios:
- **steady:** equal delivery, and 6.9 % faster at the median;
- **burst with the cache on:** 100 % against 96.7 %, on the same 6 model calls;
- **burst with the cache off:** 30.0 % against 26.7 %, within noise.

It delivers less in the two heavy-load scenarios:
- **overload:** 62.8 % against 67.4 %;
- **tpm_halved:** 72.5 % against 77.5 %, a gap that cannot be told apart from noise.

The cause is the cost of streaming verification. When a sentence fails, v2 stops the answer and
cancels the model stream, and the provider keeps the cancelled stream's full reservation for the
minute. So when many answers stop early under heavy load, fewer calls fit and more requests are
turned away.

**The trade-off.** Users see the first verified sentence much earlier: at a median of 324.5 ms
(burst, cache on) to 462.8 ms (steady load), where v1 showed nothing until the whole answer was
verified (median total 614.2 and 701.1 ms in those scenarios). Nothing unverified is ever shown.
In exchange, throughput is somewhat lower under heavy load when many answers stop early.

## 6. Objective

Re-validate v2 against v1's load results, fix the regressions where possible, and produce the
launch material (README and demo scripts). These are V2-14 tasks 1, 2 and 4. Task 3 (a public
demo deployment) was not requested in these prompts and was not done.

## 7. Implementation Summary

**Part 1:**
- cached-answer passages;
- the load harness (`v2_load.py`, `run_v2_load.py`);
- the first comparison (owner's first V2-14 prompt);
- the guard charge fix and `deadline_ms` parity, with review fixes;
- the re-run (owner's second prompt).

All of this code is committed by the owner (`cf2bbe4`, `57c5a01`).

**This final step:** this report, the README rewrite, and the two demo scripts. No code
changes, no load runs, no real calls. The demo's expected outputs were checked in a local dry
run against the stand-in (§13).

## 8. Files Created

| Path | Purpose |
| :-- | :-- |
| `reports/phase-v2-14-load-revalidation.md` | This report. |
| `demo/DEMO_SCRIPT.md` | A 60-second recording script for a real API key. |
| `demo/STAND_IN_DEMO.md` | The same demo, offline with the stand-in. |

Earlier in the phase (committed):
- `src/relay/experiments/v2_load.py`, `scripts/run_v2_load.py`;
- the tests `test_v2_14_cache_sources.py`, `test_v2_14_load.py`, `test_v2_14_guard_charge.py`
  and `test_v2_14_deadline.py`;
- the run directories under `runs/`.

## 9. Files Modified

- **This step:** `README.md` (rewritten), `ENGINEERING_LOG.md` (appended).
- **Earlier, committed:**
  - `control/cache.py`, `control/provider_guard.py`
  - `api/routes/ask_v1.py`, `orchestrator/pipeline.py`
  - `experiments/streaming_load.py`
  - `tests/test_control_plane.py`: two v1 expectations changed on purpose by the guard fix, to
    110 and 202

## 10. Technical Decisions

- **Scenario parameters come from v1's canonical runs**, not from the prompt's descriptions,
  which differed (for example, v1 steady ran with the cache off at 4/min for 5 min). The owner's
  brief said "the same parameters as V1".
- **Cache-off scenarios detach the answer cache** from the composed v2 service, because v2 has
  no "store without cache" setting. That reproduces v1's cache-off behaviour: no hits and no
  coalescing.
- **Guard charge without a usage report:** prompt estimate plus the tokens received, content
  and reasoning together. An unknown amount (a cancelled buffered call) keeps the whole
  reservation. The provider's header headroom is given back only when the provider settled the
  call.
- **The README's quickstart differs from the brief** where the brief's commands would not work:
  - sqlite mode is required for collections;
  - a fresh clone needs the one-time papers, models and index setup;
  - Groq needs the lane switched and `MAX_REAL_CALLS` set.
- **The demo scripts use measured outputs, not the brief's examples,** where the two differed.
  See §14.

## 11. Commands Actually Executed (this step)

- **Re-reading the runs:** `python - <<EOF … v2_load.metrics(...) … EOF` produced every table
  in §2 directly from the run files.
- **Demo dry runs:** `python scratchpad/demo_dry_run.py` and `demo_dry_run2.py`.
  - They start `relay serve` in sqlite mode with the store and uploads in the scratchpad.
  - They walk the demo steps over HTTP against the stand-in.
- **Final checks:** `python -m pytest -q`, `python -m ruff check .`, `sha256sum results.csv`,
  `git status --short evidence results.csv`. Results are in §12 and in the engineering log.

## 12. Test Results

This step made no code changes. The final run after the last edit is recorded in
`ENGINEERING_LOG.md`: **757 passed**, ruff clean, `results.csv` sha256 `8dfb10df…44efd`, and
`evidence/` unchanged.

## 13. Actual Output and Generated Artifacts

Demo dry run against the stand-in (the scripts in `demo/` are built from it):

- **Start-up:** `relay serve` answered `/healthz` 3.9 s after launch on the development machine.
  Its last line before uvicorn is
  `serving the API and the web UI on http://127.0.0.1:<port>  (Ctrl+C to stop)`.
- **Upload:** `1706.03762v7.pdf` (15 pages) to a new collection went queued → parsing →
  embedding → ready in 3.8 s, polled every 0.2 s; `indexing` was too brief to observe. Quality:
  15 pages, 0 empty, no suspected scan.
- **v1-papers:**
  - "How does multi-head attention reduce computation?" gave 2 verified sentences: the first at
    989 ms, the whole answer at 1,087 ms (the first query of the process).
  - "What is the boiling point of water?" was refused up front, with no model call.
- **The uploaded paper:**
  - "How does multi-head attention work?" gave 2 verified sentences, the first at 790 ms.
  - The boiling-point question was refused.

## 14. Errors and Root-Cause Analysis

These are corrections to statements in the brief; the measured values are used instead.

| In the brief | Measured | Source |
| :-- | :-- | :-- |
| First-sentence p50/p95 for tpm_halved 503.8 / 22,616.3 and for overload 1,963.7 / 34,622.5, as final figures | Those are the pre-fix run. The re-run gave 529.7 / 22,683.6 and 2,620.0 / 23,745.9 | summary.json of both runs |
| The fix "recovered 5 of the 9 extra dropped requests" | Give-ups fell 18 → 8 (v1: 9); delivered rose 48 → 54 | rows.jsonl |
| ~460 phantom tokens per stream, ~3,220 per run | About 446 per stream, about 3,100 per run | rows.jsonl (`tokens_out` of stopped streams) |
| Steady "8 % faster" | 6.9 % (701.1 → 652.8 ms) | summaries |
| burst_cache_on: "extra coalesced request caught by SingleFlight" | The request v1 refused was admitted and answered as a cache miss | rows.csv / rows.jsonl, arrival 11 |
| tpm_halved noise "confirmed by repeated runs" | Three v2 runs (28, 29, 29) and one v1 run: "cannot be distinguished from noise" is the most that can be said | §3 |
| `relay serve` prints "Service ready in 3.1 s" | It prints "serving the API and the web UI on http://…"; 3.9 s to healthy here | dry run |
| Demo question "What problem does this paper solve?" on a fresh upload | Refused up front by the collection's scope check (no model call), so it would be refused with Groq too. The demo uses "How does multi-head attention work?" instead | dry run |
| "Request queued — position 1, expected wait 2s" for a single user | A single user is admitted at once: "Request accepted, starting now". The queued banner appears only when both slots are busy | dry run, UI code |
| The sources panel opens by itself | Sources are a collapsed "Sources (N)" list; the panel opens from a citation chip | UI code |
| Stand-in demo with "the default relay.example.toml" | The default cache mode `memory` has no collections or chat; `RELAY_CACHE__MODE=sqlite` is needed | settings, dry run |

## 15. Fixes Applied

None in this step (no code changes). The phase's code fixes are described in §1 and §3.

## 16. Outstanding Issues

- **overload is below v1** (62.8 % against 67.4 %). Owner options from the previous report:
  - accept the gap;
  - keep reading a stopped stream to its end so the provider settles it;
  - measure Groq's settlement with a few approved real calls;
  - repeat the runs to separate the gap from noise.
- **tpm_halved's gap** cannot be told from noise without repeated runs.
- **An uploaded PDF without a usable title is named "document &lt;id&gt;"** in refusals, and
  generic questions about "this paper" are refused by the scope check. That is a usability
  issue for V2-07/V2-08; no change was made.
- **The quickstart is not verified on a fresh clone.** The README's setup was not run end to end
  from a new clone, because that needs the network.

## 17. Acceptance-Criteria Status

| Criterion | Status |
| :-- | :-- |
| Every v1 scenario's delivery rate is at least v1's, or the regression is explained | **Explained, not met**: overload (§3); tpm_halved within noise |
| Before/after table | Met (§2) |
| README with the architecture diagram and measured numbers | Met |
| Demo scripts | Met (`demo/`) |
| The demo stays usable after the free daily quota is spent | `STAND_IN_DEMO.md` runs fully offline; no public deployment exists |
| A new user reaches a first answer in under ten minutes from the README | Not verified on a fresh clone |
| Real calls ≤ 20 | 0 |

## 18. Remaining Risks

- **Groq settlement is unknown.** If Groq refunds a cancelled stream's reservation, the overload
  cost is smaller on the real provider than on the stand-in. If it does not, the cost is the
  same as measured here.
- **Single runs.** The comparisons rest on one v1 run and one or two v2 runs per scenario.

## 19. Recommended Next Phase

The owner decides on the overload gap (§16). After that, the plan's second release (V2-07 to
V2-12) or the public demo deployment (V2-14 task 3). Nothing has been started.
