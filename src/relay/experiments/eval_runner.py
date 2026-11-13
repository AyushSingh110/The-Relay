"""Answer-quality evaluation runs: model comparison and Table 3 (IMPLEMENTATION_PLAN P07).

Isolated from caching and load testing by construction:

- the pipeline is called in-process, one request at a time (no HTTP service, no cache,
  no SingleFlight, no admission queue), so every row has ``cache_status=off``;
- each model has its own :class:`ProviderGuard` (Groq keeps separate limits per model)
  that paces calls by TPM / RPM and stops at the daily safety fraction;
- the pipeline gets **no** prompt budget, so passages are never dropped to fit TPM
  headroom (no degraded answers): the guard waits for quota instead.

Per run directory (``evidence/experiments/<run_id>/``):

    rows.csv            one ResultRow per request; answer_grade stays blank until the
                        owner has reviewed the proposed grades
    traces.jsonl        one record per request (pipeline trace, passages, guard state)
    answers.jsonl       AnswerRecord per delivered answer (input to grade_answers.py)
    grades.json         deterministic rubric proposals, all ``pending`` owner review
    answers_audit.json  question, expected answer, gold pages, answer, citations,
                        retrieved pages and the proposed grade with its reasons
    summary.json        per (model, retrieval setting) comparison figures

Timing: the "client" is this runner. Answers are buffered and verified before delivery,
so ``first_token_ms`` (first delivered byte) equals ``total_ms`` for delivered rows.
"""

from __future__ import annotations

import asyncio
import json
import math
import os
import statistics
import time
from collections import Counter
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Final, Literal

from relay.config import Settings
from relay.control.provider_guard import GuardConfig, GuardedGenerator, ProviderGuard, Sleep
from relay.control.retry import RetryConfig, RetryPolicy
from relay.errors import ConfigurationError, sanitize_error
from relay.eval.grader import (
    AnswerCitation,
    AnswerRecord,
    GradeRecord,
    GradesFile,
    propose_grades,
    write_grades,
)
from relay.eval.questions import EvalQuestion, QuestionSet, load_question_file
from relay.ingestion.arxiv_fetcher import load_corpus_manifest
from relay.orchestrator.pipeline import (
    DEFAULT_MAX_COMPLETION_TOKENS,
    Pipeline,
    PipelineConfig,
    PipelineOutcome,
    PipelineRequest,
)
from relay.orchestrator.tools import PassageRetriever, ToolGateway
from relay.providers.base import ChatProvider
from relay.providers.errors import RealCallBudgetExceededError
from relay.retrieval.hybrid_index import RETRIEVAL_SETTINGS
from relay.schemas import ResultRow, Source
from relay.telemetry.run_store import RunHandle
from relay.telemetry.spans import (
    ClientTimer,
    MonotonicClock,
    ServerSpanTracker,
    SpanSummary,
    WallClock,
)

Scenario = Literal["model_comparison", "retrieval_eval"]
SCENARIOS: Final[tuple[str, ...]] = ("model_comparison", "retrieval_eval")
SHORTLIST: Final[tuple[str, ...]] = ("openai/gpt-oss-20b", "openai/gpt-oss-120b",
                                     "qwen/qwen3.8-27b")
# Stratified selection (not authorship): one Transformer numeric trap, two Moshi, one
# H-Net, one cross-paper trap, and the unanswerable question that reaches the model
# (q16-q20 are refused before any model call).
COMPARISON_QUESTIONS: Final[tuple[str, ...]] = ("q03", "q07", "q10", "q12", "q13", "q21")
# Header-observed per-model limits from P02-LIVE (x-ratelimit-limit-tokens / -requests).
HEADER_TPM: Final = 8000
HEADER_RPD: Final = 1000
# Local pacing cap. ASSUMED, not a measured Groq limit (RPM is not in Groq's headers).
DEFAULT_RPM_PACING: Final = 10

ANSWERS_FILE: Final = "answers.jsonl"
GRADES_FILE: Final = "grades.json"
AUDIT_FILE: Final = "answers_audit.json"
SUMMARY_FILE: Final = "summary.json"


# --- Items and lanes -------------------------------------------------------------------


@dataclass(frozen=True)
class EvalItem:
    question: EvalQuestion
    model_id: str
    retrieval_setting: str


def plan_items(questions: QuestionSet, *, question_ids: Sequence[str] | None,
               models: Sequence[str], settings: Sequence[str]) -> list[EvalItem]:
    """Question-major order, models innermost: each model's TPM bucket recovers while the
    other models are called."""
    by_id = questions.by_id()
    ids = list(question_ids) if question_ids else [q.id for q in questions.questions]
    unknown = [i for i in ids if i not in by_id]
    if unknown:
        raise ConfigurationError(f"unknown question ids: {unknown}")
    if len(set(ids)) != len(ids) or len(set(models)) != len(models):
        raise ConfigurationError("question ids and models must not repeat")
    bad = [s for s in settings if s not in RETRIEVAL_SETTINGS]
    if bad:
        raise ConfigurationError(f"unknown retrieval settings: {bad}")
    return [EvalItem(by_id[qid], model, setting)
            for qid in ids for setting in settings for model in models]


@dataclass
class Lane:
    """Everything that answers for one model."""

    model_id: str
    pipeline: Pipeline
    guard: ProviderGuard


def guard_config(*, tpm: int | None = HEADER_TPM, rpd: int | None = HEADER_RPD,
                 rpm: int | None = DEFAULT_RPM_PACING, tpd: int | None = None,
                 daily_safety_fraction: float = 0.75) -> GuardConfig:
    return GuardConfig(max_concurrency=1, rpm_limit=rpm, tpm_limit=tpm, rpd_limit=rpd,
                       tpd_limit=tpd, daily_safety_fraction=daily_safety_fraction)


def build_lane(provider: ChatProvider, *, model_id: str,
               retrievers: Mapping[str, PassageRetriever], top_k: Mapping[str, int],
               config: GuardConfig, vocabulary: frozenset[str] | None = None,
               retry: RetryConfig | None = None,
               max_completion_tokens: int = DEFAULT_MAX_COMPLETION_TOKENS,
               reasoning_effort: str | None = None, temperature: float | None = 0.0,
               sleep: Sleep = asyncio.sleep,
               clock: MonotonicClock = time.perf_counter_ns) -> Lane:
    guard = ProviderGuard(config, clock=clock, sleep=sleep)
    generator = GuardedGenerator(provider, guard, RetryPolicy(retry or RetryConfig()),
                                 sleep=sleep, clock=clock)
    pipeline = Pipeline(
        gateway=ToolGateway(retrievers, clock=clock), generator=generator,
        config=PipelineConfig(model_id=model_id, max_completion_tokens=max_completion_tokens,
                              temperature=temperature, reasoning_effort=reasoning_effort),
        top_k=top_k, vocabulary=vocabulary, budget=None,  # never degrade in evaluations
    )
    return Lane(model_id=model_id, pipeline=pipeline, guard=guard)


# --- Runner ----------------------------------------------------------------------------


@dataclass
class EvalResult:
    run_id: str
    rows: list[ResultRow] = field(default_factory=list)
    outcomes: dict[str, PipelineOutcome] = field(default_factory=dict)
    items: dict[str, EvalItem] = field(default_factory=dict)
    timings: dict[str, float] = field(default_factory=dict)
    not_run: list[dict[str, str]] = field(default_factory=list)
    stopped_reason: str | None = None
    lane_stops: dict[str, str] = field(default_factory=dict)


def _utc_now() -> datetime:
    return datetime.now(UTC)


class EvalRunner:
    def __init__(self, *, run: RunHandle, lanes: Mapping[str, Lane], scenario: Scenario,
                 source: Source, calls_remaining: Callable[[], int] | None = None,
                 clock: MonotonicClock = time.perf_counter_ns,
                 wall_clock: WallClock = _utc_now) -> None:
        if scenario not in run.manifest.scenarios:
            raise ConfigurationError(f"scenario {scenario!r} is not declared in the run")
        self._run = run
        self._lanes = dict(lanes)
        self._scenario = scenario
        self._source: Source = source
        self._calls_remaining = calls_remaining
        self._clock = clock
        self._wall_clock = wall_clock

    def _stop_check(self, result: EvalResult, item: EvalItem) -> str | None:
        if result.stopped_reason is not None:
            return result.stopped_reason
        if self._calls_remaining is not None and self._calls_remaining() < 1:
            result.stopped_reason = "--max-real-calls reached"
            return result.stopped_reason
        if item.model_id in result.lane_stops:
            return result.lane_stops[item.model_id]
        daily = self._lanes[item.model_id].guard.daily_stop_reason()
        if daily is not None:
            result.lane_stops[item.model_id] = f"daily safety stop: {daily}"
            return result.lane_stops[item.model_id]
        return None

    async def run(self, items: Sequence[EvalItem]) -> EvalResult:
        result = EvalResult(run_id=self._run.run_id)
        missing = sorted({i.model_id for i in items} - set(self._lanes))
        if missing:
            raise ConfigurationError(f"no lane for models: {missing}")
        for item in items:
            reason = self._stop_check(result, item)
            if reason is not None:
                result.not_run.append({"question_id": item.question.id, "model_id": item.model_id,
                                       "retrieval_setting": item.retrieval_setting,
                                       "reason": reason})
                continue
            await self._one(result, item)
        return result

    async def _one(self, result: EvalResult, item: EvalItem) -> None:
        lane = self._lanes[item.model_id]
        request_id = self._run.new_request_id()
        timer = ClientTimer(self._clock, self._wall_clock)
        spans = ServerSpanTracker(self._clock)
        request = PipelineRequest(request_id=request_id, question=item.question.question,
                                  retrieval_setting=item.retrieval_setting)
        try:
            outcome = await lane.pipeline.run(request, spans)
        except RealCallBudgetExceededError as exc:
            # Refused locally on a retry: earlier attempts of this request may have been
            # sent, so the request is recorded in the traces rather than as a row.
            result.stopped_reason = "--max-real-calls reached during a retry"
            self._run.append_trace({
                "record": "eval_stopped", "request_id": request_id,
                "question_id": item.question.id, "model_id": item.model_id,
                "retrieval_setting": item.retrieval_setting, "error": sanitize_error(exc),
                "spans": spans.finish().model_dump(),
                "guard": lane.guard.snapshot().model_dump(mode="json"),
            })
            return
        summary = spans.finish()
        if outcome.status == "ok":
            timer.mark_first_byte()  # buffered: the whole answer arrives at once
        timing = timer.finish()
        row = ResultRow(
            request_id=request_id, scenario=self._scenario, source=self._source,
            start_time=timing.start_time, first_token_ms=timing.first_token_ms,
            total_ms=timing.total_ms, status=outcome.status, retries=outcome.retries,
            tokens_in=outcome.tokens_in, tokens_out=outcome.tokens_out,
            retrieval_setting=item.retrieval_setting, answer_grade=None,
            run_id=self._run.run_id, question_id=item.question.id,
            provider_first_token_ms=summary.provider_first_token_ms,
            server_total_ms=summary.server_total_ms, queue_depth_at_entry=None,
            waiting_ms=summary.waiting_ms, working_ms=summary.working_ms,
            retrying_ms=summary.retrying_ms, retrieval_ms=outcome.retrieval_ms,
            cache_status="off", provider_called=outcome.provider_called,
            degraded=outcome.degraded, model_id=outcome.model_id or None,
            usage_source=outcome.usage_source, cost_usd=None,
            verification_verdict=outcome.verification_verdict, error_code=outcome.error_code,
        )
        self._run.append_row(row)
        self._run.append_trace(_trace(row, outcome, summary, item, lane))
        result.rows.append(row)
        result.outcomes[request_id] = outcome
        result.items[request_id] = item
        if outcome.error_code == "provider_auth_error":
            result.stopped_reason = "provider rejected the credentials"
        elif outcome.error_code in ("circuit_open", "daily_budget_exhausted"):
            result.lane_stops[item.model_id] = f"stopped after {outcome.error_code}"


def _trace(row: ResultRow, outcome: PipelineOutcome, spans: SpanSummary, item: EvalItem,
           lane: Lane) -> dict[str, Any]:
    return {
        "record": "eval_request", "request_id": row.request_id,
        "question_id": item.question.id, "model_id": item.model_id,
        "retrieval_setting": item.retrieval_setting,
        "client": {"start_time": row.start_time.isoformat(), "total_ms": row.total_ms,
                   "first_token_ms": row.first_token_ms},
        "spans": spans.model_dump(),
        "outcome": outcome.model_dump(mode="json"),
        "guard": lane.guard.snapshot().model_dump(mode="json"),
    }


# --- Grading artifacts -----------------------------------------------------------------


def answer_records(result: EvalResult) -> list[AnswerRecord]:
    """Delivered answers only. Undelivered rows (errors, shed) stay ungraded."""
    records = []
    for request_id, outcome in result.outcomes.items():
        if outcome.delivered_text is None:
            continue
        records.append(AnswerRecord(
            request_id=request_id, question_id=result.items[request_id].question.id,
            answer_text=outcome.delivered_text, refused=outcome.refused,
            citations=tuple(AnswerCitation(paper_id=c.paper_id, page_start=c.page_start,
                                           page_end=c.page_end) for c in outcome.citations),
        ))
    return records


def audit_entries(result: EvalResult, grades: GradesFile) -> list[dict[str, Any]]:
    by_request = {g.request_id: g for g in grades.grades}
    entries = []
    for row in result.rows:
        outcome = result.outcomes[row.request_id]
        question = result.items[row.request_id].question
        grade: GradeRecord | None = by_request.get(row.request_id)
        entries.append({
            "request_id": row.request_id, "question_id": question.id,
            "model_id": result.items[row.request_id].model_id,
            "retrieval_setting": row.retrieval_setting, "question": question.question,
            "answerable": question.answerable, "category": question.category,
            "trap_type": question.trap_type, "expected_answer": question.expected_answer,
            "gold_pages": [f"{g.paper_id} p.{g.page}" for g in question.gold_citations],
            "status": row.status, "kind": outcome.kind, "error_code": row.error_code,
            "verification_verdict": row.verification_verdict,
            "verifier_failures": [] if outcome.trace.verification is None else
            [f.model_dump() for f in outcome.trace.verification.failures],
            "delivered_text": outcome.delivered_text, "raw_answer": outcome.trace.raw_answer,
            "finish_reason": outcome.trace.finish_reason,
            "cited_pages": [f"{c.paper_id} pp.{c.page_start}-{c.page_end} [P{c.tag}]"
                            for c in outcome.citations],
            "retrieved": [f"[P{i}] {p.paper_id} pp.{p.page_start}-{p.page_end} {p.chunk_id}"
                          for i, p in enumerate(outcome.passages, start=1)],
            "tokens_in": row.tokens_in, "tokens_out": row.tokens_out,
            "proposed_grade": None if grade is None else grade.proposed,
            "grade_reasons": [] if grade is None else list(grade.reasons),
            "review_status": None if grade is None else grade.review_status,
        })
    return entries


# --- Summary ---------------------------------------------------------------------------


def _pct(values: Sequence[float], q: float) -> float | None:
    """Nearest-rank percentile; None for no values."""
    if not values:
        return None
    ordered = sorted(values)
    return round(ordered[max(1, math.ceil(q * len(ordered))) - 1], 3)


def _rate(numerator: int, denominator: int) -> float | None:
    return None if denominator == 0 else round(numerator / denominator, 3)


def summarise(result: EvalResult, grades: GradesFile, *,
              daily_safety_fraction: float = 0.75, rpd: int | None = HEADER_RPD,
              ) -> dict[str, Any]:
    proposed = {g.request_id: g.proposed for g in grades.grades}
    groups: dict[tuple[str, str], list[ResultRow]] = {}
    for row in result.rows:
        key = (result.items[row.request_id].model_id, row.retrieval_setting)
        groups.setdefault(key, []).append(row)
    table = []
    for (model, setting), rows in sorted(groups.items()):
        outcomes = [result.outcomes[r.request_id] for r in rows]
        called = [r for r in rows if r.provider_called]
        llm = [o for o in outcomes if o.kind in ("answer", "safe_refusal", "llm_refusal")]
        answered = [r for r in rows if result.items[r.request_id].question.answerable]
        unanswerable = [r for r in rows if not result.items[r.request_id].question.answerable]
        grade_counts = Counter(proposed[r.request_id] for r in rows if r.request_id in proposed)
        ttft = [r.provider_first_token_ms for r in called if r.provider_first_token_ms is not None]
        # The guard's quota wait happens before the call, so it is removed for comparisons.
        ttft_net = [r.provider_first_token_ms - (r.waiting_ms or 0.0) for r in called
                    if r.provider_first_token_ms is not None]
        waits = [r.waiting_ms or 0.0 for r in called]
        totals = [r.total_ms for r in rows if r.provider_called]
        table.append({
            "model_id": model, "retrieval_setting": setting, "requests": len(rows),
            "status": dict(sorted(Counter(r.status for r in rows).items())),
            "kinds": dict(sorted(Counter(o.kind for o in outcomes).items())),
            "provider_calls": len(called), "retries": sum(r.retries for r in rows),
            "tokens_in_total": sum(r.tokens_in for r in rows),
            "tokens_out_total": sum(r.tokens_out for r in rows),
            "tokens_in_mean": None if not called else
            round(statistics.fmean(r.tokens_in for r in called), 1),
            "tokens_out_mean": None if not called else
            round(statistics.fmean(r.tokens_out for r in called), 1),
            "usage_sources": dict(sorted(Counter(r.usage_source for r in called).items())),
            "provider_ttft_ms": {"n": len(ttft), "p50": _pct(ttft, 0.5),
                                 "max": _pct(ttft, 1.0),
                                 "basis": "request start to first content token, "
                                          "including the guard's quota wait"},
            "provider_ttft_excl_wait_ms": {"n": len(ttft_net), "p50": _pct(ttft_net, 0.5),
                                           "max": _pct(ttft_net, 1.0),
                                           "basis": "provider_ttft minus waiting_ms: "
                                                    "retrieval + prompt + model"},
            "quota_wait_ms": {"n": len(waits), "p50": _pct(waits, 0.5),
                              "max": _pct(waits, 1.0)},
            "total_ms_called": {"n": len(totals), "p50": _pct(totals, 0.5),
                                "max": _pct(totals, 1.0)},
            "verified": sum(o.verification_verdict == "VERIFIED" for o in outcomes),
            "blocked": sum(o.verification_verdict == "BLOCKED" for o in outcomes),
            "llm_refusals": sum(o.kind == "llm_refusal" for o in outcomes),
            "early_refusals": sum(o.kind == "early_refusal" for o in outcomes),
            "truncated": sum(o.trace.truncated for o in outcomes),
            "reasoning_chars_mean": None if not llm else
            round(statistics.fmean(o.trace.reasoning_chars for o in llm), 1),
            "citation_pass_rate": _rate(
                sum(o.verification_verdict == "VERIFIED" for o in llm),
                sum(o.verification_verdict in ("VERIFIED", "BLOCKED") for o in llm)),
            "proposed_grades": dict(sorted(grade_counts.items())),
            "proposed_correct_rate_answerable": _rate(
                sum(proposed.get(r.request_id) == "correct" for r in answered), len(answered)),
            "proposed_refused_correctly_rate_unanswerable": _rate(
                sum(proposed.get(r.request_id) == "refused_correctly" for r in unanswerable),
                len(unanswerable)),
        })
    return {
        "run_id": result.run_id,
        "grades_status": "PROPOSED by the deterministic rubric; pending owner review",
        "stopped_reason": result.stopped_reason, "lane_stops": result.lane_stops,
        "not_run": result.not_run,
        "answers_per_day": {
            "rpd_bound_per_model": None if rpd is None else math.floor(rpd *
                                                                       daily_safety_fraction),
            "rpd_basis": "x-ratelimit-limit-requests header (P02-LIVE) x daily safety fraction",
            "tpd_bound": None,
            "tpd_basis": "not computable: TPD is not exposed by Groq and was not measured",
        },
        "table": table,
    }


def write_artifacts(run: RunHandle, result: EvalResult, questions: QuestionSet, *,
                    daily_safety_fraction: float = 0.75) -> dict[str, Any]:
    answers = answer_records(result)
    grades = propose_grades(run.run_id, questions, answers)
    run_dir = run.run_dir
    (run_dir / ANSWERS_FILE).write_text(
        "".join(a.model_dump_json() + "\n" for a in answers), encoding="utf-8", newline="\n")
    write_grades(run_dir / GRADES_FILE, grades)
    _write_json(run_dir / AUDIT_FILE, audit_entries(result, grades))
    summary = summarise(result, grades, daily_safety_fraction=daily_safety_fraction)
    _write_json(run_dir / SUMMARY_FILE, summary)
    return summary


def _write_json(path: Path, data: Any) -> None:
    tmp = path.with_name(f".{path.name}.tmp")
    tmp.write_text(json.dumps(data, indent=2, sort_keys=True, ensure_ascii=False) + "\n",
                   encoding="utf-8", newline="\n")
    os.replace(tmp, path)


def format_table(summary: Mapping[str, Any]) -> str:
    """The comparison as a plain-text table (for the terminal and the report)."""
    head = ("model", "setting", "n", "calls", "ttft p50", "ttft max", "wait max", "tok in",
            "tok out", "verified", "blocked", "cite pass", "correct*", "refused ok*")
    lines = [" | ".join(head)]
    for r in summary["table"]:
        lines.append(" | ".join(str(v) for v in (
            r["model_id"], r["retrieval_setting"], r["requests"], r["provider_calls"],
            r["provider_ttft_excl_wait_ms"]["p50"], r["provider_ttft_excl_wait_ms"]["max"],
            r["quota_wait_ms"]["max"], r["tokens_in_mean"],
            r["tokens_out_mean"], r["verified"], r["blocked"], r["citation_pass_rate"],
            r["proposed_correct_rate_answerable"],
            r["proposed_refused_correctly_rate_unanswerable"])))
    lines.append("ttft = request start to first content token minus the guard's quota wait "
                 "(includes retrieval and prompt build); * proposed by the rubric, pending "
                 "owner review")
    return "\n".join(lines)


# --- Loading and live safeguards -------------------------------------------------------


@dataclass(frozen=True)
class Corpus:
    questions: QuestionSet
    page_counts: dict[str, int]
    paper_sha256s: tuple[str, ...]


def load_corpus_and_questions(papers_dir: Path, questions_file: Path) -> Corpus:
    corpus = load_corpus_manifest(papers_dir)
    page_counts = {r.paper_id: r.page_count for r in corpus.papers}
    questions, report = load_question_file(questions_file, page_counts)
    if questions is None or not report.valid:
        raise ConfigurationError(f"{questions_file} is not valid: {list(report.errors)}")
    return Corpus(questions, page_counts, tuple(r.sha256 for r in corpus.papers))


def load_retrievers(settings: Iterable[str], *, papers_dir: Path, indexes_dir: Path,
                    models_dir: Path) -> tuple[dict[str, PassageRetriever], frozenset[str]]:
    """Retrievers for the named settings, integrity-checked against the corpus manifest.
    Returns them with the BM25 vocabulary (for the scope analyzer)."""
    os.environ.setdefault("HF_HUB_OFFLINE", "1")
    from relay.retrieval.embedder import EMBEDDING_MODEL, FastEmbedEmbedder, FastEmbedReranker
    from relay.retrieval.hybrid_index import HybridIndex, HybridRetriever

    chunk_sets = (load_corpus_manifest(papers_dir).extraction or {}).get("chunk_sets", {})
    wanted = [RETRIEVAL_SETTINGS[name] for name in settings]
    embedder = FastEmbedEmbedder(models_dir, local_files_only=True)
    reranker = (FastEmbedReranker(models_dir, local_files_only=True)
                if any(s.rerank for s in wanted) else None)
    retrievers: dict[str, PassageRetriever] = {}
    vocabulary: set[str] = set()
    for setting in wanted:
        index = HybridIndex.load(
            indexes_dir / setting.chunk_preset,
            expected_chunks_sha256=chunk_sets.get(setting.chunk_preset, {}).get("sha256"),
            expected_embedder_id=EMBEDDING_MODEL)
        retrievers[setting.name] = HybridRetriever(index, setting, embedder, reranker)
        vocabulary |= index.vocabulary
    return retrievers, frozenset(vocabulary)


def live_problems(*, source: Source, live: bool, max_real_calls: int, planned_calls: int,
                  settings: Settings) -> list[str]:
    """Reasons a real run must be refused. Empty = every safeguard is satisfied."""
    if source != "real":
        return []
    problems = []
    if not live:
        problems.append("dry run: pass --live to send real Groq requests")
    if max_real_calls < 1:
        problems.append("--max-real-calls must be at least 1 for a real run")
    if planned_calls > max_real_calls:
        problems.append(f"plan needs up to {planned_calls} first attempts but "
                        f"--max-real-calls is {max_real_calls}")
    if settings.max_real_calls < max_real_calls:
        problems.append(f"MAX_REAL_CALLS in the environment/.env is {settings.max_real_calls};"
                        f" it must be >= --max-real-calls ({max_real_calls})")
    if settings.groq_api_key is None or not settings.groq_api_key.get_secret_value().strip():
        problems.append("GROQ_API_KEY is not configured")
    return problems


__all__ = [
    "COMPARISON_QUESTIONS",
    "SCENARIOS",
    "SHORTLIST",
    "Corpus",
    "EvalItem",
    "EvalResult",
    "EvalRunner",
    "Lane",
    "answer_records",
    "audit_entries",
    "build_lane",
    "format_table",
    "guard_config",
    "live_problems",
    "load_corpus_and_questions",
    "load_retrievers",
    "plan_items",
    "summarise",
    "write_artifacts",
]
