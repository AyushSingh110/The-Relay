"""Calibrate the stand-in from Phase 02-LIVE probe evidence. Makes NO Groq calls.

Reads every complete run under ``<evidence>/groq_probes/`` (``traces.jsonl``), fits a
:class:`StandinProfile`, then replays the same call shapes (model, prompt size,
``max_completion_tokens``) against the stand-in on 127.0.0.1 through
``HttpOpenAIProvider`` and compares the quantiles with what Groq did.

Writes a new run directory ``<evidence>/standin_calibration/<run_id>/``:

    manifest.json   run metadata (kind standin_calibration, source stand-in, seed)
    profile.json    the profile the stand-in should be started with
    fit_report.json fit details, header checks, quantile comparison, and the list of
                    ways the stand-in is least like the real thing

Without usable evidence the profile is ``uncalibrated_defaults`` and no comparison is
made. Exit codes: 0 ok, 1 failure (the run is marked aborted).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any, Final

from relay.errors import RelayError, sanitize_error
from relay.providers.base import ChatMessage, ChatRequest, collect_stream
from relay.providers.errors import ProviderError
from relay.providers.http_provider import HttpOpenAIProvider
from relay.standin.profile import (
    Limits,
    ProbeCall,
    StandinProfile,
    calls_from_traces,
    fit_profile,
    header_checks,
    quantiles,
)
from relay.standin.server import create_standin_app, serve_on_loopback
from relay.telemetry.run_store import create_run

EXIT_OK: Final = 0
EXIT_FAILURE: Final = 1
SCENARIO: Final = "standin_calibration"
METRICS: Final = ("headers_ms", "first_content_ms", "total_ms", "prompt_tokens",
                  "completion_tokens")

LEAST_LIKE_THE_REAL_THING: Final[tuple[str, ...]] = (
    "Latency is fitted from very little: 5 sequential calls per model, tiny answers, one "
    "time of day, one client location (Groq region 'bom'). The spread (sigma) and the "
    "prompt-size slope are rough; there is no measured tail, no diurnal change, and no "
    "network jitter beyond what the fitted header delay already contains.",
    "No concurrency knee was measured: every real call was sequential. The knee and the "
    "slowdown per extra request are assumptions, so the stand-in's behaviour under load "
    "is a design input, not a calibration.",
    "Failure behaviour is invented. Groq returned 0 errors in 16 calls, so 5xx rates, "
    "stragglers and stalled streams are scenario settings, not measured rates.",
    "No 429 was ever observed on Groq. The 429 body wording, the retry-after value and "
    "whether Groq rejects (rather than queues) at a limit follow the documentation and "
    "the client's parser, not evidence.",
    "Token accounting differs. Groq's remaining-tokens drop did not equal the reported "
    "usage; the stand-in reserves prompt + max_completion_tokens at admission and gives "
    "back the unused part at the end. The real rule is unknown, and the token-bucket "
    "reading of reset-tokens failed for some calls (see header_checks).",
    "RPM and TPD are not known. Groq exposes neither in headers; the stand-in enforces "
    "them only when an experiment sets a value, labelled as a scenario setting.",
    "Groq keeps separate limit counters per model (each model restarted at 999). The "
    "stand-in keeps one set of buckets for all models.",
    "Answers are extractive copies of passage sentences, at most two sentences. Real "
    "answers are longer and paraphrased, so output length, output tokens per answer and "
    "the time spent streaming content are not like Groq's. Output tokenisation "
    "(4 characters per token) is assumed.",
    "Reasoning is a fixed number of filler tokens per model (the measured median). Real "
    "reasoning length varies with the question and can use most of the token budget.",
    "Prompt tokenisation was fitted on synthetic probe prompts, not on paper text with "
    "equations and identifiers, which usually tokenises into more tokens per character.",
    "Streaming granularity differs: one SSE chunk per word with an even inter-token gap. "
    "Groq batches tokens into chunks of varying size.",
)


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--evidence-dir", type=Path, default=Path("evidence"))
    parser.add_argument("--seed", type=int, default=0)
    parser.add_argument("--replay-repeats", type=int, default=3,
                        help="replays of each probe call shape (0 = no replay)")
    args = parser.parse_args(argv)
    if args.replay_repeats < 0:
        parser.error("--replay-repeats must be >= 0")
    return args


# --- Evidence --------------------------------------------------------------------------


def load_probe_calls(evidence_dir: Path) -> tuple[list[ProbeCall], list[dict[str, Any]]]:
    """Calls from complete probe runs, and a note for every run looked at."""
    calls: list[ProbeCall] = []
    seen: list[dict[str, Any]] = []
    probes = evidence_dir / "groq_probes"
    for run_dir in sorted(p for p in probes.iterdir() if p.is_dir()) if probes.is_dir() else []:
        note: dict[str, Any] = {"run_id": run_dir.name}
        manifest_path, traces_path = run_dir / "manifest.json", run_dir / "traces.jsonl"
        if not manifest_path.is_file() or not traces_path.is_file():
            seen.append({**note, "used": False, "reason": "manifest or traces missing"})
            continue
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        if manifest.get("kind") != "groq_probe" or manifest.get("status") != "complete":
            seen.append({**note, "used": False,
                         "reason": f"status {manifest.get('status')!r}, not complete"})
            continue
        records = [json.loads(line) for line in
                   traces_path.read_text(encoding="utf-8").splitlines() if line.strip()]
        found = calls_from_traces(records, run_dir.name)
        calls.extend(found)
        seen.append({**note, "used": bool(found), "records": len(records),
                     "inference_calls_used": len(found)})
    return calls, seen


# --- Replay ----------------------------------------------------------------------------


def _replay_request(call: ProbeCall) -> ChatRequest:
    return ChatRequest(model=call.model, max_completion_tokens=call.max_completion_tokens,
                       messages=(ChatMessage(role="user", content="w" * call.prompt_chars),))


async def replay(profile: StandinProfile, calls: Sequence[ProbeCall],
                 repeats: int) -> tuple[list[dict[str, Any]], list[str]]:
    """Run each call shape ``repeats`` times, sequentially (as the probes were)."""
    unlimited = profile.model_copy(update={"limits": Limits()})
    results: list[dict[str, Any]] = []
    errors: list[str] = []
    async with serve_on_loopback(create_standin_app(unlimited)) as base_url:
        provider = HttpOpenAIProvider(base_url=base_url, source="stand-in")
        try:
            for _ in range(repeats):
                for call in calls:
                    try:
                        result = await collect_stream(provider.stream_chat(
                            _replay_request(call)))
                    except ProviderError as exc:
                        errors.append(sanitize_error(exc))
                        continue
                    usage = result.usage
                    results.append({
                        "model": call.model, "label": call.label,
                        "headers_ms": result.since_sent_ms(result.opened_ns),
                        "first_content_ms": result.first_content_ms,
                        "total_ms": result.total_ms,
                        "prompt_tokens": None if usage is None else usage.prompt_tokens,
                        "completion_tokens": None if usage is None else usage.completion_tokens,
                    })
        finally:
            await provider.aclose()
    return results, errors


def _values(rows: Sequence[Any], metric: str) -> list[float]:
    out = []
    for row in rows:
        value = row.get(metric) if isinstance(row, dict) else getattr(row, metric)
        if value is not None:
            out.append(float(value))
    return out


def compare(calls: Sequence[ProbeCall], replayed: Sequence[dict[str, Any]],
            ) -> dict[str, dict[str, Any]]:
    """Per model and metric: measured vs stand-in quantiles, and the p50 ratio."""
    comparison: dict[str, dict[str, Any]] = {}
    for model in sorted({c.model for c in calls}):
        measured = [c for c in calls if c.model == model]
        stand_in = [r for r in replayed if r["model"] == model]
        per_metric: dict[str, Any] = {}
        for metric in METRICS:
            real_q = quantiles(_values(measured, metric))
            fake_q = quantiles(_values(stand_in, metric))
            ratio = None
            if real_q["p50"] and fake_q["p50"] is not None:
                ratio = round(float(fake_q["p50"]) / float(real_q["p50"]), 3)
            per_metric[metric] = {"measured": real_q, "standin": fake_q,
                                  "p50_ratio_standin_over_measured": ratio}
        comparison[model] = per_metric
    return comparison


def largest_differences(comparison: dict[str, dict[str, Any]], top: int = 5,
                        ) -> list[dict[str, Any]]:
    ranked = []
    for model, metrics in comparison.items():
        for metric, entry in metrics.items():
            ratio = entry["p50_ratio_standin_over_measured"]
            if ratio:
                ranked.append({"model": model, "metric": metric, "p50_ratio": ratio,
                               "abs_log_ratio": round(abs(math.log(ratio)), 3)})
    return sorted(ranked, key=lambda r: -r["abs_log_ratio"])[:top]


# --- Main ------------------------------------------------------------------------------


def _write_json(path: Path, data: Any) -> None:
    path.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")


async def run(args: argparse.Namespace, *, git_state: tuple[str, bool] | None = None) -> int:
    calls, runs_seen = load_probe_calls(args.evidence_dir)
    profile, fit = fit_profile(calls, seed=args.seed)
    note = (f"profile label: {profile.label}; probe calls used: {len(calls)}")
    handle = create_run(
        args.evidence_dir, "standin_calibration", scenarios=[SCENARIO],
        config_snapshot={"seed": args.seed, "replay_repeats": args.replay_repeats,
                         "evidence_dir": str(args.evidence_dir)},
        source="stand-in", seed=args.seed, notes=note, git_state=git_state,
    )
    try:
        profile = profile.model_copy(update={"calibration_run_id": handle.run_id}) \
            if profile.label == "calibrated" else profile
        replay_info: dict[str, Any]
        comparison: dict[str, dict[str, Any]] = {}
        if profile.label != "calibrated" or args.replay_repeats == 0:
            replay_info = {"skipped": True, "reason": "no evidence to compare against"
                           if profile.label != "calibrated" else "--replay-repeats 0"}
        else:
            replayed, errors = await replay(profile, calls, args.replay_repeats)
            comparison = compare(calls, replayed)
            replay_info = {
                "skipped": False, "repeats": args.replay_repeats, "calls": len(replayed),
                "errors": errors, "limits_enforced": False,
                "how": "each probe call shape (model, prompt characters, "
                       "max_completion_tokens) replayed sequentially on 127.0.0.1 through "
                       "HttpOpenAIProvider; rate limits switched off so only latency and "
                       "token counts are compared; the prompt text is synthetic",
                "per_call": replayed,
            }
        report = {
            "run_id": handle.run_id, "label": profile.label, "seed": args.seed,
            "probe_runs": runs_seen, "n_calls": len(calls), "fit": fit,
            "header_checks": header_checks(calls), "replay": replay_info,
            "comparison": comparison,
            "largest_differences": largest_differences(comparison),
            "least_like_the_real_thing": list(LEAST_LIKE_THE_REAL_THING),
            "note": "Measured values come only from the probe traces listed in probe_runs. "
                    "Stand-in values are from the replay in this run. Nothing here is a new "
                    "Groq measurement.",
        }
        _write_json(handle.run_dir / "profile.json", profile.model_dump(mode="json"))
        _write_json(handle.run_dir / "fit_report.json", report)
        handle.finalize("complete")
    except (RelayError, OSError, ValueError) as exc:
        handle.finalize("aborted", notes=f"{note}; aborted: {sanitize_error(exc)}")
        print(f"calibration failed: {sanitize_error(exc)}", file=sys.stderr)
        return EXIT_FAILURE

    print(f"run: {handle.run_dir}")
    print(f"label: {profile.label}; probe calls: {len(calls)}; "
          f"replayed calls: {replay_info.get('calls', 0)}")
    for model, metrics in comparison.items():
        parts = []
        for metric in ("headers_ms", "first_content_ms", "total_ms"):
            entry = metrics[metric]
            parts.append(f"{metric} p50 measured {entry['measured']['p50']} / stand-in "
                         f"{entry['standin']['p50']}")
        print(f"  {model}: " + "; ".join(parts))
    return EXIT_OK


def main(argv: Sequence[str] | None = None) -> int:
    return asyncio.run(run(parse_args(argv)))


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = ["LEAST_LIKE_THE_REAL_THING", "compare", "load_probe_calls", "main", "parse_args",
           "replay", "run"]
