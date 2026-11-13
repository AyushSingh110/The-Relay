"""Command-line driver shared by scripts/run_model_comparison.py and run_retrieval_eval.py.

A real run is refused unless every safeguard holds: ``--source real --live``,
``--max-real-calls N`` with N >= planned first attempts and N <= MAX_REAL_CALLS, and a
configured GROQ_API_KEY. Without ``--live`` a real run is a dry run that prints the plan
and creates nothing. Exit codes: 0 ok (or dry run), 1 failure, 2 refused by a safeguard.
"""

from __future__ import annotations

import argparse
import hashlib
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path

from relay.config import Settings
from relay.control.cache import corpus_digest
from relay.control.retry import RetryConfig
from relay.errors import RelayError, sanitize_error
from relay.experiments.eval_runner import (
    COMPARISON_QUESTIONS,
    SHORTLIST,
    EvalRunner,
    Scenario,
    build_lane,
    format_table,
    guard_config,
    live_problems,
    load_corpus_and_questions,
    load_retrievers,
    plan_items,
    write_artifacts,
)
from relay.orchestrator.prompt import PROMPT_VERSION
from relay.orchestrator.tools import PassageRetriever
from relay.providers.base import ChatProvider, RealCallBudget
from relay.providers.http_provider import HttpOpenAIProvider
from relay.retrieval.hybrid_index import RETRIEVAL_SETTINGS
from relay.schemas import Source
from relay.telemetry.run_store import RunHandle, create_run

EXIT_OK = 0
EXIT_FAILURE = 1
EXIT_REFUSED = 2

# (provider, calls_remaining) for a run; injected by tests.
ProviderFactory = Callable[[RunHandle], tuple[ChatProvider, Callable[[], int] | None]]


def _csv(text: str) -> tuple[str, ...]:
    return tuple(part.strip() for part in text.split(",") if part.strip())


def build_parser(scenario: Scenario, description: str) -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=description,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--source", choices=("real", "stand-in"), default="stand-in")
    parser.add_argument("--live", action="store_true", help="send real Groq requests")
    parser.add_argument("--max-real-calls", type=int, default=0)
    if scenario == "model_comparison":
        parser.add_argument("--models", type=_csv, default=SHORTLIST)
        parser.add_argument("--questions", type=_csv, default=COMPARISON_QUESTIONS)
        parser.add_argument("--settings", type=_csv, default=("balanced_rerank",))
    else:
        parser.add_argument("--model", required=True, help="the owner-selected model")
        parser.add_argument("--questions", type=_csv, default=None, help="default: all")
        parser.add_argument("--settings", type=_csv, default=tuple(RETRIEVAL_SETTINGS))
    parser.add_argument("--questions-file", type=Path, default=Path("eval/questions.json"))
    parser.add_argument("--papers-dir", type=Path, default=Path("data/papers"))
    parser.add_argument("--indexes-dir", type=Path, default=Path("data/indexes"))
    parser.add_argument("--models-dir", type=Path, default=Path("data/models"))
    parser.add_argument("--evidence-dir", type=Path, default=Path("evidence"))
    parser.add_argument("--standin-url", default=None, help="default: STANDIN_URL setting")
    parser.add_argument("--tpm-limit", type=int, default=8000,
                        help="per model; default from the P02-LIVE limit-tokens header")
    parser.add_argument("--rpd-limit", type=int, default=1000,
                        help="per model; default from the P02-LIVE limit-requests header")
    parser.add_argument("--rpm-limit", type=int, default=10,
                        help="local pacing cap (assumed; Groq's RPM was not measured)")
    parser.add_argument("--tpd-limit", type=int, default=None, help="unknown by default")
    parser.add_argument("--max-completion-tokens", type=int, default=512)
    parser.add_argument("--reasoning-effort", default=None)
    return parser


def models_of(args: argparse.Namespace) -> tuple[str, ...]:
    return tuple(args.models) if hasattr(args, "models") else (args.model,)


def _file_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


async def execute(args: argparse.Namespace, scenario: Scenario, *, settings: Settings,
                  provider_factory: ProviderFactory | None = None,
                  retrievers: Mapping[str, PassageRetriever] | None = None,
                  vocabulary: frozenset[str] | None = None,
                  git_state: tuple[str, bool] | None = None,
                  out: Callable[[str], None] = print) -> int:
    source: Source = args.source
    models = models_of(args)
    corpus = load_corpus_and_questions(args.papers_dir, args.questions_file)
    items = plan_items(corpus.questions, question_ids=args.questions, models=models,
                       settings=args.settings)
    out(f"plan: {len(items)} requests = {len(items) // len(models)} question x setting "
        f"pairs x {len(models)} model(s); source={source}; cache=off; sequential")
    out(f"  models: {', '.join(models)}; settings: {', '.join(args.settings)}")
    out(f"  real-call cap: {args.max_real_calls} (first attempts planned: {len(items)}; "
        "retries need headroom; early refusals make no call)")
    problems = live_problems(source=source, live=args.live, max_real_calls=args.max_real_calls,
                             planned_calls=len(items), settings=settings)
    if problems:
        dry_run = source == "real" and not args.live
        for problem in problems:
            out(f"  {'would refuse' if dry_run else 'refused'}: {problem}")
        return EXIT_OK if dry_run else EXIT_REFUSED  # a dry run never creates a run

    if retrievers is None:
        retrievers, vocabulary = load_retrievers(
            args.settings, papers_dir=args.papers_dir, indexes_dir=args.indexes_dir,
            models_dir=args.models_dir)
    config = guard_config(tpm=args.tpm_limit, rpd=args.rpd_limit, rpm=args.rpm_limit,
                          tpd=args.tpd_limit,
                          daily_safety_fraction=settings.daily_safety_fraction)
    retry = RetryConfig()
    run = create_run(
        args.evidence_dir, "experiment", scenarios=[scenario], source=source,
        model_id=models[0] if len(models) == 1 else None,
        reasoning_settings={"reasoning_effort": args.reasoning_effort},
        retrieval_settings=args.settings, cache_mode="off", git_state=git_state,
        config_snapshot={
            "scenario": scenario, "models": list(models),
            "question_ids": [i.question.id for i in items[::len(models) * len(args.settings)]],
            "questions_file_sha256": _file_sha256(args.questions_file),
            "corpus_sha256": corpus_digest(corpus.paper_sha256s),
            "prompt_version": PROMPT_VERSION, "max_completion_tokens": args.max_completion_tokens,
            "max_real_calls": args.max_real_calls, "guard": config.model_dump(),
            "guard_limit_basis": {
                "tpm_limit": "x-ratelimit-limit-tokens header (P02-LIVE), per model",
                "rpd_limit": "x-ratelimit-limit-requests header (P02-LIVE), per model",
                "rpm_limit": "local pacing cap, assumed (RPM not measured)",
                "tpd_limit": "unknown (not exposed by Groq, not measured)"},
            "retry": retry.model_dump(), "prompt_budget": "none (no degraded answers)",
            "settings": settings.manifest_snapshot(),
        },
        notes=f"{scenario}; planned requests {len(items)}",
    )
    provider: ChatProvider | None = None
    try:
        provider, remaining = (provider_factory or _default_factory(settings, args))(run)
        lanes = {model: build_lane(provider, model_id=model, retrievers=retrievers,
                                   top_k={s: RETRIEVAL_SETTINGS[s].top_k for s in args.settings},
                                   config=config, vocabulary=vocabulary, retry=retry,
                                   max_completion_tokens=args.max_completion_tokens,
                                   reasoning_effort=args.reasoning_effort)
                 for model in models}
        runner = EvalRunner(run=run, lanes=lanes, scenario=scenario, source=source,
                            calls_remaining=remaining)
        result = await runner.run(items)
        summary = write_artifacts(run, result, corpus.questions,
                                  daily_safety_fraction=settings.daily_safety_fraction)
        notes = (f"{scenario}; requests {len(result.rows)} of {len(items)}; stopped: "
                 f"{result.stopped_reason or 'no'}; lane stops: {result.lane_stops or 'none'}")
        manifest = run.finalize("complete", notes=notes)
    except (RelayError, OSError, ValueError) as exc:
        run.finalize("aborted", notes=f"aborted: {sanitize_error(exc)}")
        out(f"run aborted: {sanitize_error(exc)}")
        return EXIT_FAILURE
    finally:
        if provider is not None:
            await provider.aclose()
    out(f"run: {run.run_dir}  (real calls recorded: {manifest.real_call_count})")
    out(f"rows: {len(result.rows)}; not run: {len(result.not_run)}; "
        f"stopped: {result.stopped_reason or 'no'}")
    out(format_table(summary))
    return EXIT_OK


def _default_factory(settings: Settings, args: argparse.Namespace) -> ProviderFactory:
    def factory(run: RunHandle) -> tuple[ChatProvider, Callable[[], int] | None]:
        if args.source == "real":
            budget = RealCallBudget(args.max_real_calls,
                                    on_consume=lambda: run.record_real_calls(1))
            provider = HttpOpenAIProvider.from_settings(settings, target="groq",
                                                        call_budget=budget)
            return provider, lambda: budget.remaining
        return HttpOpenAIProvider(base_url=args.standin_url or settings.standin_url,
                                  source="stand-in"), None
    return factory


def main(argv: Sequence[str] | None, scenario: Scenario, description: str) -> int:
    import asyncio

    from relay.config import load_settings

    args = build_parser(scenario, description).parse_args(argv)
    try:
        settings = load_settings()
        return asyncio.run(execute(args, scenario, settings=settings))
    except RelayError as exc:
        print(f"failed: {sanitize_error(exc)}")
        return EXIT_FAILURE


__all__ = ["EXIT_FAILURE", "EXIT_OK", "EXIT_REFUSED", "build_parser", "execute", "main"]
