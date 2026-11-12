"""Capped Groq probes: model discovery, baseline latency/usage, owner-approved minute-limit.

Default is a DRY RUN: it prints the planned calls and which safeguards would block a live
run, and contacts nothing. A live run needs ALL of:

  --live, --max-real-calls N (>= planned calls), MAX_REAL_CALLS >= N in env/.env,
  GROQ_API_KEY configured, and for minute-limit also --owner-approved-rate-limit-probe.

Exit codes: 0 ok (or dry run), 1 runtime/validation failure, 2 refused by a safeguard.
Evidence: evidence/groq_probes/<run_id>/ (manifest.json, rows.csv, traces.jsonl,
summary.json). No API key, Authorization header or org ID is ever written or printed.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import httpx
from pydantic import ValidationError

from relay.config import Settings, load_settings
from relay.errors import RelayError, sanitize_error
from relay.providers.base import RealCallBudget
from relay.providers.http_provider import HttpOpenAIProvider
from relay.providers.probing import (
    ProbeOptions,
    build_plan,
    describe_plan,
    live_preconditions,
    run_probe,
)

EXIT_OK = 0
EXIT_FAILURE = 1
EXIT_REFUSED = 2


def _word_sizes(text: str) -> tuple[int, ...]:
    try:
        sizes = tuple(int(part) for part in text.split(",") if part.strip())
    except ValueError:
        raise argparse.ArgumentTypeError("expected comma-separated integers") from None
    return sizes


def parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("mode", choices=("discover", "baseline", "minute-limit"))
    parser.add_argument("--model", action="append", default=[],
                        help="model id (repeatable; default: GROQ_MODEL)")
    parser.add_argument("--calls-per-model", type=int, default=3)
    parser.add_argument("--prompt-words", type=_word_sizes, default=(400, 1600),
                        help="comma-separated synthetic prompt sizes in words (baseline)")
    parser.add_argument("--max-completion-tokens", type=int, default=256)
    parser.add_argument("--reasoning-effort", default=None)
    parser.add_argument("--minute-limit-calls", type=int, default=None)
    parser.add_argument("--max-real-calls", type=int, default=0,
                        help="hard cap on real HTTP requests for this run")
    parser.add_argument("--live", action="store_true",
                        help="actually contact the provider (default: dry run)")
    parser.add_argument("--owner-approved-rate-limit-probe", action="store_true",
                        help="required for minute-limit (separate owner approval)")
    parser.add_argument("--evidence-root", type=Path, default=Path("evidence"))
    parser.add_argument("--env-file", type=Path, default=Path(".env"))
    return parser.parse_args(argv)


def _emit(payload: dict[str, Any]) -> None:
    print(json.dumps(payload, indent=2, sort_keys=True))


def _options(args: argparse.Namespace, settings: Settings) -> ProbeOptions:
    models = tuple(args.model) or (() if args.mode == "discover" else (settings.groq_model,))
    return ProbeOptions(
        mode=args.mode,
        models=models,
        calls_per_model=args.calls_per_model,
        prompt_words=tuple(args.prompt_words),
        max_completion_tokens=args.max_completion_tokens,
        reasoning_effort=args.reasoning_effort,
        minute_limit_calls=args.minute_limit_calls,
    )


def main(
    argv: Sequence[str] | None = None,
    *,
    settings: Settings | None = None,
    transport: httpx.AsyncBaseTransport | None = None,
) -> int:
    """``settings`` and ``transport`` are injection points for offline tests."""
    args = parse_args(argv)
    try:
        active = settings or load_settings(args.env_file if args.env_file.exists() else None)
        options = _options(args, active)
    except ValidationError as exc:
        fields = sorted({".".join(str(p) for p in err["loc"]) or "options" for err in exc.errors()})
        _emit({"outcome": "invalid_options", "fields": fields,
               "messages": sorted({err["msg"] for err in exc.errors()})})
        return EXIT_FAILURE
    except RelayError as exc:
        _emit({"outcome": "failed", "error": sanitize_error(exc), "message": str(exc)})
        return EXIT_FAILURE

    plan = build_plan(options)
    problems = live_preconditions(
        options,
        plan,
        max_real_calls=args.max_real_calls,
        settings=active,
        owner_approved_rate_limit_probe=args.owner_approved_rate_limit_probe,
    )
    if options.mode == "minute-limit" and not args.owner_approved_rate_limit_probe:
        _emit({"outcome": "refused", "mode": options.mode, "problems": problems})
        return EXIT_REFUSED
    if not args.live:
        _emit({
            "outcome": "dry_run",
            "mode": options.mode,
            "planned_calls": len(plan),
            "plan": describe_plan(plan),
            "live_blocked_by": problems,
            "groq_credential_configured": active.groq_api_key is not None,
            "note": "nothing was sent; pass --live with the caps to run for real",
        })
        return EXIT_OK
    if problems:
        _emit({"outcome": "refused", "mode": options.mode, "problems": problems})
        return EXIT_REFUSED

    def factory(budget: RealCallBudget) -> HttpOpenAIProvider:
        return HttpOpenAIProvider.from_settings(
            active, target="groq", call_budget=budget, transport=transport
        )

    try:
        outcome = asyncio.run(
            run_probe(
                options,
                settings=active,
                max_real_calls=args.max_real_calls,
                evidence_root=args.evidence_root,
                provider_factory=factory,
            )
        )
    except RelayError as exc:
        _emit({"outcome": "failed", "error": sanitize_error(exc)})
        return EXIT_FAILURE
    _emit({"outcome": "completed", **outcome.model_dump()})
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
