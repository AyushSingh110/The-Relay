"""P10 load scenarios against the Relay (stand-in by default; one owner-approved real run).

  python scripts/run_load_tests.py --scenario steady
  python scripts/run_load_tests.py --scenario burst --cache off
  python scripts/run_load_tests.py --scenario retry_amplification --retry-budget off
  python scripts/run_load_tests.py --scenario overload --tokens-per-answer 760 \
         --tokens-per-answer-basis "measured in steady run exp-..."
  python scripts/run_load_tests.py --scenario real_steady --source real            # dry run
  python scripts/run_load_tests.py --scenario real_steady --source real --live --max-real-calls 12

The Relay app and the stand-in each run under uvicorn on 127.0.0.1. Every run writes its
own evidence/experiments/<run_id>/ (manifest.json, rows.csv, traces.jsonl, arrivals.json,
summary.json). Only the real_steady scenario may use --source real.
Exit codes: 0 ok (or dry run), 1 failure, 2 refused by a safeguard.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import httpx

from relay.config import Settings, load_settings
from relay.control.cache import corpus_digest
from relay.control.provider_guard import GuardConfig
from relay.errors import RelayError, sanitize_error
from relay.experiments.eval_runner import live_problems, load_corpus_and_questions, load_retrievers
from relay.experiments.load_runner import REAL_TOKENS_PER_ANSWER, LoadSetup, run_load
from relay.experiments.workloads import SCENARIOS, ScenarioSpec, build_arrivals, load_pools
from relay.providers.base import ChatProvider, RealCallBudget
from relay.providers.http_provider import HttpOpenAIProvider
from relay.standin.server import (
    API_PREFIX,
    create_standin_app,
    load_profile,
    serve_on_loopback,
)
from relay.telemetry.run_store import RunHandle, create_run

EXIT_OK, EXIT_FAILURE, EXIT_REFUSED = 0, 1, 2
DEFAULT_PROFILE = Path("evidence/standin_calibration/cal-20261113T022403Z-3f687c6/profile.json")
DEFAULT_MODEL = "qwen/qwen3.8-27b"


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--scenario", choices=sorted(SCENARIOS), required=True)
    parser.add_argument("--cache", choices=("on", "off"), default=None,
                        help="override the scenario's cache mode")
    parser.add_argument("--retry-budget", choices=("on", "off"), default=None)
    parser.add_argument("--seed", type=int, default=None)
    parser.add_argument("--source", choices=("stand-in", "real"), default="stand-in")
    parser.add_argument("--live", action="store_true")
    parser.add_argument("--max-real-calls", type=int, default=0)
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument("--profile", type=Path, default=DEFAULT_PROFILE)
    parser.add_argument("--tokens-per-answer", type=float, default=REAL_TOKENS_PER_ANSWER)
    parser.add_argument("--tokens-per-answer-basis", default=(
        "P07 Table 3 real qwen/qwen3.8-27b small_fast: mean tokens_in 1406.7 + tokens_out 106.2"))
    parser.add_argument("--evidence-dir", type=Path, default=Path("evidence"))
    parser.add_argument("--questions-file", type=Path, default=Path("eval/questions.json"))
    parser.add_argument("--load-questions", type=Path, default=Path("eval/load_questions.json"))
    parser.add_argument("--papers-dir", type=Path, default=Path("data/papers"))
    parser.add_argument("--indexes-dir", type=Path, default=Path("data/indexes"))
    parser.add_argument("--models-dir", type=Path, default=Path("data/models"))
    return parser.parse_args(argv)


def scenario_spec(args: argparse.Namespace) -> ScenarioSpec:
    spec = SCENARIOS[args.scenario]
    update: dict[str, Any] = {}
    if args.cache is not None:
        update["cache_mode"] = "exact" if args.cache == "on" else "off"
    if args.retry_budget is not None:
        update["retry_budget_enabled"] = args.retry_budget == "on"
    if args.seed is not None:
        update["seed"] = args.seed
    return spec.model_copy(update=update)


def guard_config(spec: ScenarioSpec, settings: Settings) -> GuardConfig:
    base: dict[str, Any] = {"max_concurrency": 2, "tpm_limit": 8000, "rpd_limit": 1000,
                            "rpm_limit": None, "tpd_limit": None,
                            "daily_safety_fraction": settings.daily_safety_fraction}
    return GuardConfig(**{**base, **spec.guard_overrides})


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


async def run(args: argparse.Namespace, *, settings: Settings,
              git_state: tuple[str, bool] | None = None) -> int:
    spec = scenario_spec(args)
    if args.source == "real" and spec.name != "real_steady":
        print("refused: only the real_steady scenario may use --source real")
        return EXIT_REFUSED
    corpus = load_corpus_and_questions(args.papers_dir, args.questions_file)
    arrivals = build_arrivals(spec, load_pools(args.load_questions))
    print(f"plan: {spec.name}: {len(arrivals)} requests over {arrivals[-1].at_s:.0f} s; "
          f"cache={spec.cache_mode}; retry budget={'on' if spec.retry_budget_enabled else 'off'};"
          f" source={args.source}; model={args.model}")
    problems = live_problems(source=args.source, live=args.live,
                             max_real_calls=args.max_real_calls, planned_calls=len(arrivals),
                             settings=settings)
    if problems:
        dry = args.source == "real" and not args.live
        for problem in problems:
            print(f"  {'would refuse' if dry else 'refused'}: {problem}")
        return EXIT_OK if dry else EXIT_REFUSED

    retrievers, vocabulary = load_retrievers(
        [spec.retrieval_setting], papers_dir=args.papers_dir, indexes_dir=args.indexes_dir,
        models_dir=args.models_dir)
    config = guard_config(spec, settings)
    profile = load_profile(args.profile) if args.source == "stand-in" else None
    if profile is not None and spec.standin_overrides:
        profile = profile.with_overrides(spec.standin_overrides)
    handle = create_run(
        args.evidence_dir, "experiment", scenarios=[spec.name], source=args.source,
        model_id=args.model, retrieval_settings=[spec.retrieval_setting],
        cache_mode=spec.cache_mode, seed=spec.seed, git_state=git_state,
        config_snapshot={
            "scenario": spec.model_dump(mode="json"), "requests_planned": len(arrivals),
            "guard": config.model_dump(), "guard_basis": {
                "tpm_limit": "x-ratelimit-limit-tokens header (P02-LIVE) unless overridden",
                "rpd_limit": "x-ratelimit-limit-requests header (P02-LIVE) unless overridden",
                "rpm_limit": "unknown (not measured); not enforced locally"},
            "tokens_per_answer": args.tokens_per_answer,
            "tokens_per_answer_basis": args.tokens_per_answer_basis,
            "standin_profile": None if profile is None else {
                "path": str(args.profile), "label": profile.label,
                "calibration_run_id": profile.calibration_run_id,
                "overrides": spec.standin_overrides},
            "questions_file_sha256": _sha(args.questions_file),
            "load_questions_sha256": _sha(args.load_questions),
            "corpus_sha256": corpus_digest(corpus.paper_sha256s),
            "max_real_calls": args.max_real_calls, "settings": settings.manifest_snapshot(),
        },
        notes=f"{spec.name}; cache={spec.cache_mode}; retry_budget="
              f"{'on' if spec.retry_budget_enabled else 'off'}; source={args.source}",
    )
    (handle.run_dir / "arrivals.json").write_text(
        json.dumps([a.model_dump() for a in arrivals], indent=1) + "\n", encoding="utf-8")
    questions = corpus.questions.by_id()
    try:
        if profile is not None:
            summary = await _with_standin(spec, arrivals, handle, profile, args, config,
                                          corpus, retrievers, vocabulary, questions)
        else:
            summary = await _with_groq(spec, arrivals, handle, args, settings, config,
                                       corpus, retrievers, vocabulary, questions)
        (handle.run_dir / "summary.json").write_text(
            json.dumps(summary, indent=2, sort_keys=True, default=str) + "\n", encoding="utf-8")
        notes = (f"{handle.manifest.notes}; rows {summary['requests_sent']}; "
                 f"status {summary['status']}")
        manifest = handle.finalize("complete", notes=notes)
    except (RelayError, OSError, ValueError, httpx.HTTPError) as exc:
        handle.finalize("aborted", notes=f"aborted: {sanitize_error(exc)}")
        print(f"run aborted: {sanitize_error(exc)}")
        return EXIT_FAILURE
    _print_summary(handle, summary, manifest.real_call_count)
    return EXIT_OK


def _setup(args: argparse.Namespace, corpus: Any, retrievers: Any, vocabulary: Any,
           questions: Any, config: GuardConfig, **extra: Any) -> LoadSetup:
    return LoadSetup(model_id=args.model, source=args.source,
                     corpus_sha256=corpus_digest(corpus.paper_sha256s), retrievers=retrievers,
                     vocabulary=vocabulary, questions=questions, guard_config=config,
                     tokens_per_answer=args.tokens_per_answer,
                     tokens_per_answer_basis=args.tokens_per_answer_basis, **extra)


async def _with_standin(spec: ScenarioSpec, arrivals: Any, handle: RunHandle, profile: Any,
                        args: argparse.Namespace, config: GuardConfig, corpus: Any,
                        retrievers: Any, vocabulary: Any, questions: Any) -> dict[str, Any]:
    async with serve_on_loopback(create_standin_app(profile)) as standin_base:
        admin = standin_base.removesuffix(API_PREFIX)

        async def stats() -> dict[str, Any]:
            async with httpx.AsyncClient(base_url=admin) as client:
                return (await client.get("/admin/stats")).json()

        provider: ChatProvider = HttpOpenAIProvider(base_url=standin_base, source="stand-in")
        try:
            return await run_load(spec, arrivals, run=handle, provider=provider,
                                  setup=_setup(args, corpus, retrievers, vocabulary, questions,
                                               config, standin_stats=stats))
        finally:
            await provider.aclose()


async def _with_groq(spec: ScenarioSpec, arrivals: Any, handle: RunHandle,
                     args: argparse.Namespace, settings: Settings, config: GuardConfig,
                     corpus: Any, retrievers: Any, vocabulary: Any,
                     questions: Any) -> dict[str, Any]:
    budget = RealCallBudget(args.max_real_calls, on_consume=lambda: handle.record_real_calls(1))
    provider = HttpOpenAIProvider.from_settings(settings, target="groq", call_budget=budget)
    try:
        return await run_load(spec, arrivals, run=handle, provider=provider,
                              setup=_setup(args, corpus, retrievers, vocabulary, questions,
                                           config, real_calls=lambda: budget.used))
    finally:
        await provider.aclose()


def _print_summary(handle: RunHandle, s: dict[str, Any], real_calls: int) -> None:
    print(f"run: {handle.run_dir}  (real calls recorded: {real_calls})")
    print(f"  requests {s['requests_sent']}; offered {s['offered_rate_per_min']}/min; wall "
          f"{s['wall_duration_s']} s; status {s['status']}; errors {s['error_codes']}")
    ft, tt = s["first_token_ms_delivered"], s["total_ms_all"]
    print(f"  first token (delivered) n={ft['n']} p50/p95/p99 {ft['p50']}/{ft['p95']}/{ft['p99']}"
          f" ms; total (all) n={tt['n']} p50/p95/p99 {tt['p50']}/{tt['p95']}/{tt['p99']} ms")
    print(f"  given up {s['given_up']}; cache {s['cache_status']}; provider-call rows "
          f"{s['rows_with_provider_call']}; retries {s['retries_total']}; capacity "
          f"{s['capacity']['capacity']} ({s['capacity']['binding_bound']}); queue peak "
          f"{s['queue_peak_depth']}")
    if "standin_stats" in s:
        st = s["standin_stats"]
        print(f"  stand-in: requests {st['requests']}, statuses {st['by_status']}, 429s "
              f"{st['rate_limited']}, injected errors {st['errors_injected']}")


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        return asyncio.run(run(args, settings=load_settings()))
    except RelayError as exc:
        print(f"failed: {sanitize_error(exc)}")
        return EXIT_FAILURE


if __name__ == "__main__":
    sys.exit(main())
