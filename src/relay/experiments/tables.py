"""Recomputable result tables (IMPLEMENTATION_PLAN P11).

Everything is computed from the canonical ``results.csv`` (built by
``scripts/build_results.py``) plus, per run, files in that run's evidence directory:
``manifest.json`` (labels), ``summary.json`` (harness-measured duration, capacity,
stand-in attempts), ``grades.json`` (rubric grades) and ``traces.jsonl`` (forensics).

Conventions (P11): times in ms; **linear-interpolation percentiles** (the "linear" /
type-7 definition) with N stated; percentages to one decimal; "not measured" when N = 0.
Output is deterministic: the same inputs give byte-identical text.

Grades: ``answer_grade`` in ``results.csv`` is blank until the owner has reviewed the
proposed grades. Until then, quality columns use the rubric's *proposed* grades from
``grades.json`` and say so.
"""

from __future__ import annotations

import json
import math
import statistics
from collections import Counter
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Final

from relay.schemas import ResultRow, RunManifest
from relay.telemetry.csv_codec import read_rows
from relay.telemetry.run_store import TRACES_FILE, load_manifest, run_dir_for

NOT_MEASURED: Final = "not measured"
LOAD_SCENARIOS: Final = ("steady", "burst", "overload", "tpm_halved", "daily_exhausted",
                         "retry_amplification", "real_steady")
UNANSWERABLE: Final = frozenset({"q16", "q17", "q18", "q19", "q20", "q21"})


# --- Formatting ------------------------------------------------------------------------


def linear_percentile(values: Sequence[float], q: float) -> float | None:
    """Linear interpolation between closest ranks (numpy ``method="linear"``)."""
    if not 0.0 <= q <= 1.0:
        raise ValueError("q must be in [0, 1]")
    if not values:
        return None
    ordered = sorted(values)
    position = (len(ordered) - 1) * q
    low = math.floor(position)
    high = min(low + 1, len(ordered) - 1)
    return ordered[low] + (ordered[high] - ordered[low]) * (position - low)


def fmt_ms(value: float | None) -> str:
    return NOT_MEASURED if value is None else f"{value:.1f}"


def fmt_pct(numerator: int, denominator: int) -> str:
    if denominator == 0:
        return NOT_MEASURED
    return f"{100.0 * numerator / denominator:.1f}% ({numerator}/{denominator})"


def fmt_mean(values: Sequence[float]) -> str:
    return NOT_MEASURED if not values else f"{statistics.fmean(values):.1f}"


def percentiles(values: Sequence[float], qs: Sequence[float] = (0.5, 0.95, 0.99)) -> str:
    """'p50 / p95 / p99 (N=n)' or 'not measured (N=0)'."""
    if not values:
        return f"{NOT_MEASURED} (N=0)"
    shown = " / ".join(fmt_ms(linear_percentile(values, q)) for q in qs)
    return f"{shown} (N={len(values)})"


def markdown_table(headers: Sequence[str], rows: Iterable[Sequence[Any]]) -> str:
    lines = ["| " + " | ".join(headers) + " |", "|" + "|".join(" :-- " for _ in headers) + "|"]
    lines += ["| " + " | ".join(str(cell) for cell in row) + " |" for row in rows]
    return "\n".join(lines)


# --- Inputs ----------------------------------------------------------------------------


@dataclass
class RunInfo:
    run_id: str
    manifest: RunManifest
    summary: dict[str, Any] | None
    proposed: dict[str, str]  # request_id -> proposed grade
    reviewed: dict[str, str]  # request_id -> final grade (owner-reviewed only)
    rows: list[ResultRow] = field(default_factory=list)

    @property
    def scenario(self) -> str:
        return self.manifest.scenarios[0]

    @property
    def label(self) -> str:
        snapshot = self.manifest.config_snapshot.get("scenario", {})
        parts = [f"cache={self.manifest.cache_mode}"]
        if isinstance(snapshot, dict) and "retry_budget_enabled" in snapshot:
            parts.append(f"retry budget={'on' if snapshot['retry_budget_enabled'] else 'off'}")
        return ", ".join(parts)


def load_inputs(results_csv: Path, evidence_root: Path) -> dict[str, RunInfo]:
    rows = read_rows(results_csv)
    runs: dict[str, RunInfo] = {}
    for row in rows:
        info = runs.get(row.run_id)
        if info is None:
            run_dir = run_dir_for(evidence_root, row.run_id)
            grades = _read_json(run_dir / "grades.json")
            graded = [] if grades is None else grades["grades"]
            info = RunInfo(
                run_id=row.run_id, manifest=load_manifest(run_dir),
                summary=_read_json(run_dir / "summary.json"),
                proposed={g["request_id"]: g["proposed"] for g in graded},
                reviewed={g["request_id"]: g["final"] for g in graded if g.get("final")},
            )
            runs[row.run_id] = info
        info.rows.append(row)
    return dict(sorted(runs.items()))


def _read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8")) if path.is_file() else None


def read_traces(evidence_root: Path, run_id: str) -> list[dict[str, Any]]:
    path = run_dir_for(evidence_root, run_id) / TRACES_FILE
    if not path.is_file():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()
            if line.strip()]


def _grade(info: RunInfo, request_id: str) -> tuple[str | None, str]:
    """(grade, source): the owner's final grade when reviewed, else the proposal."""
    if request_id in info.reviewed:
        return info.reviewed[request_id], "reviewed"
    if request_id in info.proposed:
        return info.proposed[request_id], "proposed"
    return None, "ungraded"


def _grade_note(infos: Iterable[RunInfo]) -> str:
    sources = Counter(_grade(i, r.request_id)[1] for i in infos for r in i.rows
                      if r.request_id in i.proposed)
    if not sources:
        return "No grades."
    if sources.get("proposed"):
        return (f"Quality columns use rubric grades: {dict(sorted(sources.items()))}. "
                "PROPOSED grades are pending owner review and are a literal-matching lower "
                "bound, not final quality figures.")
    return f"Quality columns use owner-reviewed grades ({sources['reviewed']})."


def _ttft_excl_wait(row: ResultRow) -> float | None:
    if row.provider_first_token_ms is None:
        return None
    return row.provider_first_token_ms - (row.waiting_ms or 0.0)


def _quality_cells(info: RunInfo, rows: Sequence[ResultRow]) -> list[str]:
    answerable = [r for r in rows if r.question_id not in UNANSWERABLE]
    unanswerable = [r for r in rows if r.question_id in UNANSWERABLE]
    llm = [r for r in rows if r.verification_verdict in ("VERIFIED", "BLOCKED")]
    correct = sum(_grade(info, r.request_id)[0] == "correct" for r in answerable)
    refused_ok = sum(_grade(info, r.request_id)[0] == "refused_correctly" for r in unanswerable)
    return [
        fmt_pct(sum(r.verification_verdict == "VERIFIED" for r in llm), len(llm)),
        fmt_pct(correct, len(answerable)),
        fmt_pct(refused_ok, len(unanswerable)),
    ]


def _token_cells(rows: Sequence[ResultRow]) -> list[str]:
    called = [r for r in rows if r.provider_called]
    return [fmt_mean([float(r.tokens_in) for r in called]),
            fmt_mean([float(r.tokens_out) for r in called]),
            fmt_mean([float(r.tokens_in + r.tokens_out) for r in called])]


# --- Tables ----------------------------------------------------------------------------


def _runs_for(runs: Mapping[str, RunInfo], predicate: Callable[[RunInfo], bool],
              ) -> list[RunInfo]:
    return [info for info in runs.values() if predicate(info)]


def table_probes(runs: Mapping[str, RunInfo]) -> str:
    probes = _runs_for(runs, lambda i: i.scenario.startswith("probe_"))
    body = []
    for info in probes:
        for model in sorted({r.model_id or "" for r in info.rows}):
            rs = [r for r in info.rows if r.model_id == model]
            body.append([info.run_id, model, len(rs), percentiles(
                [r.first_token_ms for r in rs if r.first_token_ms is not None], (0.5, 1.0)),
                percentiles([r.total_ms for r in rs], (0.5, 1.0)), *_token_cells(rs)])
    return markdown_table(["run", "model", "N", "first token p50 / max ms", "total p50 / max ms",
                           "tokens in (mean)", "tokens out (mean)", "tokens / call"], body)


def table1(runs: Mapping[str, RunInfo], evidence_root: Path) -> str:
    infos = _runs_for(runs, lambda i: i.scenario == "model_comparison")
    body = []
    for info in infos:
        truncated = Counter(t["model_id"] for t in read_traces(evidence_root, info.run_id)
                            if t.get("record") == "eval_request"
                            and t["outcome"]["trace"]["truncated"])
        for model in sorted({r.model_id or "" for r in info.rows}):
            rs = [r for r in info.rows if r.model_id == model]
            ttft = [v for v in map(_ttft_excl_wait, rs) if v is not None]
            body.append([model, len(rs), percentiles(ttft, (0.5, 0.95)), *_token_cells(rs),
                         f"{truncated.get(model, 0)}/{len(rs)}", *_quality_cells(info, rs)])
    note = _grade_note(infos)
    return (markdown_table(
        ["model", "N", "TTFT excl. quota wait p50 / p95 ms", "tokens in", "tokens out",
         "tokens / answer", "truncated at max_completion_tokens", "citation pass",
         "correct (answerable)", "refused correctly (unanswerable)"], body)
        + f"\n\nRun: {', '.join(i.run_id for i in infos)}. {note} TTFT is from request start "
          "to the first provider content token minus the guard's quota wait; it includes "
          "retrieval.")


def table2a(runs: Mapping[str, RunInfo]) -> str:
    infos = sorted(_runs_for(runs, lambda i: i.scenario in LOAD_SCENARIOS),
                   key=lambda i: (LOAD_SCENARIOS.index(i.scenario), i.run_id))
    body = []
    for info in infos:
        rs, s = info.rows, info.summary or {}
        n = len(rs)
        status = Counter(r.status for r in rs)
        delivered = status.get("ok", 0)
        duration = s.get("wall_duration_s")
        cache = Counter(r.cache_status for r in rs)
        capacity = s.get("capacity") or {}
        attempts = (s.get("standin_stats") or {}).get("requests", s.get("real_calls"))
        first = sum(r.provider_called for r in rs)
        body.append([
            info.scenario, info.label, info.manifest.source, info.run_id, n,
            NOT_MEASURED if duration is None else f"{duration:.1f}",
            NOT_MEASURED if s.get("offered_rate_per_min") is None
            else f"{s['offered_rate_per_min']:.1f}",
            NOT_MEASURED if not duration else f"{delivered / duration * 60:.1f}",
            fmt_pct(delivered, n), fmt_pct(status.get("rate_limited", 0), n),
            fmt_pct(status.get("timeout", 0), n), fmt_pct(status.get("error", 0), n),
            fmt_pct(sum(r.error_code == "client_gave_up" for r in rs), n),
            f"{cache.get('exact_hit', 0)} / {cache.get('coalesced', 0)}",
            first, sum(r.retries for r in rs),
            NOT_MEASURED if attempts is None or not first else f"{attempts / first:.2f}",
            capacity.get("capacity", NOT_MEASURED),
            NOT_MEASURED if "service_rate_per_s" not in capacity
            else f"{capacity['service_rate_per_s'] * 60:.1f}",
            s.get("queue_peak_depth", NOT_MEASURED),
        ])
    return (markdown_table(
        ["scenario", "label", "source", "run", "requests", "duration s", "offered /min",
         "delivered /min", "delivered", "rate-limited", "timeout", "error", "given up",
         "exact hit / coalesced", "rows with provider call", "retries",
         "provider attempts per first attempt", "derived queue capacity",
         "derived service rate /min", "queue peak"], body)
        + "\n\nDuration and offered rate are as measured by the harness (summary.json). "
          "Provider attempts come from the stand-in's /admin/stats or the real-call counter. "
          "Capacity = floor(service rate x patience median), with service rate = "
          "TPM / (60 x tokens per answer) (ARCHITECTURE.md §15).")


def _slowest(rows: Sequence[ResultRow]) -> ResultRow | None:
    delivered = [r for r in rows if r.status == "ok" and r.server_total_ms is not None]
    return max(delivered, key=lambda r: (r.total_ms, r.request_id)) if delivered else None


def _guard_at(traces: Sequence[Mapping[str, Any]], request_id: str) -> str:
    for record in traces:
        if record.get("record") == "flight" and record.get("leader_request_id") == request_id:
            guard = record.get("guard") or {}
            return (f"guard after the call: tpm_available {guard.get('tpm_available')}, "
                    f"tokens_last_minute {guard.get('tokens_last_minute')}, "
                    f"requests_last_minute {guard.get('requests_last_minute')}")
    return "no flight record (cache hit or coalesced)"


def table2b(runs: Mapping[str, RunInfo], evidence_root: Path) -> str:
    infos = sorted(_runs_for(runs, lambda i: i.scenario in LOAD_SCENARIOS),
                   key=lambda i: (LOAD_SCENARIOS.index(i.scenario), i.run_id))
    latency, forensics = [], []
    for info in infos:
        rs = info.rows
        ok = [r for r in rs if r.status == "ok"]
        latency.append([
            info.scenario, info.label, info.manifest.source, info.run_id,
            percentiles([r.first_token_ms for r in ok if r.first_token_ms is not None]),
            percentiles([r.total_ms for r in ok]), percentiles([r.total_ms for r in rs]),
            percentiles([r.waiting_ms for r in ok if r.waiting_ms is not None], (0.5, 0.95)),
        ])
        slow = _slowest(rs)
        if slow is None:
            continue
        before = slow.provider_first_token_ms
        server = slow.server_total_ms or 0.0
        forensics.append([
            info.scenario, info.label, slow.request_id, slow.question_id or "",
            fmt_ms(slow.total_ms),
            fmt_ms(slow.waiting_ms), fmt_ms(slow.working_ms), fmt_ms(slow.retrying_ms),
            fmt_ms(slow.retrieval_ms), fmt_ms(before),
            NOT_MEASURED if before is None else fmt_ms(server - before),
            fmt_ms(slow.total_ms - server), slow.retries, slow.cache_status,
            _guard_at(read_traces(evidence_root, info.run_id), slow.request_id),
        ])
    return (markdown_table(
        ["scenario", "label", "source", "run", "first token p50 / p95 / p99 ms (delivered)",
         "total p50 / p95 / p99 ms (delivered)", "total p50 / p95 / p99 ms (all)",
         "waiting p50 / p95 ms (delivered)"], latency)
        + "\n\n**Slowest delivered request per run.** The answer is buffered and verified, "
          "so all server time comes before the client's first token. Before / after are "
          "measured around the provider's first content token.\n\n"
        + markdown_table(
            ["scenario", "label", "request", "question", "total ms", "waiting ms", "working ms",
             "retrying ms", "retrieval ms", "before provider first token ms",
             "after provider first token ms", "client overhead ms", "retries", "cache",
             "evidence from traces"], forensics))


def table3(runs: Mapping[str, RunInfo]) -> str:
    infos = _runs_for(runs, lambda i: i.scenario == "retrieval_eval")
    order = ("small_fast", "balanced_rerank", "large_wide")
    body = []
    for info in infos:
        for setting in sorted({r.retrieval_setting for r in info.rows},
                              key=lambda s: (order.index(s) if s in order else 9, s)):
            rs = [r for r in info.rows if r.retrieval_setting == setting]
            called = [r for r in rs if r.provider_called]
            ttft = [v for v in map(_ttft_excl_wait, called) if v is not None]
            model_only = [v - (r.retrieval_ms or 0.0) for r in called
                          if (v := _ttft_excl_wait(r)) is not None]
            body.append([
                setting, info.manifest.model_id or "", len(rs), len(called),
                *_quality_cells(info, rs), *_token_cells(rs),
                percentiles(ttft, (0.5, 0.95)), percentiles(model_only, (0.5, 0.95)),
                percentiles([r.retrieval_ms for r in rs if r.retrieval_ms is not None],
                            (0.5, 0.95)),
                sum(r.verification_verdict == "EARLY_REFUSAL" for r in rs),
                sum(r.status != "ok" for r in rs),
            ])
    note = _grade_note(infos)
    return (markdown_table(
        ["setting", "model", "N", "provider calls", "citation pass", "correct (answerable)",
         "refused correctly (unanswerable q16-q21)", "tokens in", "tokens out",
         "tokens / answer", "TTFT excl. wait p50 / p95 ms", "model-only TTFT p50 / p95 ms",
         "retrieval p50 / p95 ms", "early refusals (no call)", "not delivered"], body)
        + f"\n\nRun: {', '.join(i.run_id for i in infos)}. {note} Model-only TTFT also "
          "removes retrieval time.")


def render(results_csv: Path, evidence_root: Path, *, calibration_run: str | None = None,
           canonical_path: Path | None = None) -> str:
    runs = load_inputs(results_csv, evidence_root)
    total_rows = sum(len(i.rows) for i in runs.values())
    sources = Counter(r.source for i in runs.values() for r in i.rows)
    parts = [
        "# The Relay: computed tables",
        "",
        f"Computed from `{results_csv.as_posix()}` ({total_rows} rows, {len(runs)} runs; "
        f"sources {dict(sorted(sources.items()))}) and the run evidence under "
        f"`{evidence_root.as_posix()}/`. Percentiles use linear interpolation; times in ms; "
        "N is stated for every percentile; 'not measured' means N = 0.",
    ]
    if canonical_path is not None:
        parts.append(f"Canonical run list: `{canonical_path.as_posix()}`.")
    if calibration_run:
        parts.append(f"Stand-in runs use the profile from calibration run `{calibration_run}` "
                     "(cited here; calibration runs have no result rows).")
    parts += ["", "## Real service probes (P02-LIVE)", "", table_probes(runs),
              "", "## Table 1: Model comparison", "", table1(runs, evidence_root),
              "", "## Table 2a: Capacity and throughput by load scenario", "", table2a(runs),
              "", "## Table 2b: Latency percentiles and slow-request forensics", "",
              table2b(runs, evidence_root),
              "", "## Table 3: Retrieval quality versus cost", "", table3(runs), ""]
    return "\n".join(parts)


__all__ = ["NOT_MEASURED", "RunInfo", "fmt_pct", "linear_percentile", "load_inputs",
           "markdown_table", "percentiles", "render", "table1", "table2a", "table2b",
           "table3", "table_probes"]
