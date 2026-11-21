"""``relay eval ...`` (plan V2-12): import-v1, run, report, gate.

    relay eval import-v1 [--with-entailment]
    relay eval run [--collection C] [--set S] [--preset small_fast] [--lane standin]
                   [--replay [V1_RUN]] [--summary-out FILE] [--real-calls N]
    relay eval report --run R [--json FILE]
    relay eval gate --baseline RUN|FILE --candidate RUN|FILE

All of them use the store at ``paths.store_path``. ``--replay`` answers with the in-process
stand-in from v1's recorded passages (offline, deterministic: what CI runs). Without it the
run uses the configured service and the collection's own index; a Groq lane is refused
unless ``--real-calls N`` gives an explicit call budget (the owner's approval).
"""

from __future__ import annotations

import argparse
import json
from collections.abc import Callable
from pathlib import Path
from typing import Any, Final

from relay.app.bootstrap import DEV_TENANT_ID, V1_COLLECTION_ID, ensure_dev_tenant
from relay.app.settings import RelaySettings
from relay.eval.loader import V1_SET_NAME, import_v1
from relay.eval.report import format_summary, gate, gate_metrics, load_metrics, summarise
from relay.eval.runner import run_eval
from relay.eval.standin import REPLAY_RUN, replay_pipeline
from relay.store import Store, open_store

QUESTIONS_FILE: Final = Path("eval/questions.json")
EVIDENCE_DIR: Final = Path("evidence")
EXIT_OK: Final = 0
EXIT_FAILED: Final = 1
Out = Callable[[str], None]


async def ensure_v1_set(store: Store, *, questions_file: Path = QUESTIONS_FILE,
                        evidence_dir: Path = EVIDENCE_DIR, scorer: Any = None) -> str:
    """The v1 eval set (imported on first use, with the v1-papers collection row)."""
    await ensure_dev_tenant(store)
    if await store.collections.get_by_id(V1_COLLECTION_ID) is None:
        await store.collections.create(DEV_TENANT_ID, "v1-papers",
                                       collection_id=V1_COLLECTION_ID)
    found = await store.eval_sets.find(V1_COLLECTION_ID, V1_SET_NAME)
    if found is not None and await store.eval_questions.list_for_set(found.id):
        return found.id
    report = await import_v1(store, questions_file=questions_file, evidence_dir=evidence_dir,
                             collection_id=V1_COLLECTION_ID, scorer=scorer)
    return report.eval_set_id


async def import_command(settings: RelaySettings, args: argparse.Namespace, out: Out) -> int:
    scorer = _scorer(settings) if args.with_entailment else None
    async with open_store(settings.paths.store_path) as store:
        await ensure_dev_tenant(store)
        if await store.collections.get_by_id(V1_COLLECTION_ID) is None:
            await store.collections.create(DEV_TENANT_ID, "v1-papers",
                                           collection_id=V1_COLLECTION_ID)
        report = await import_v1(store, questions_file=args.questions,
                                 evidence_dir=args.evidence, collection_id=V1_COLLECTION_ID,
                                 scorer=scorer)
    out(f"eval set {report.eval_set_id} (v1-papers): {report.questions} questions added, "
        f"{len(report.runs)} runs, {report.answers} answers, {report.rubric_grades} rubric "
        f"grades, {report.human_grades} owner grades, {report.entailment_grades} entailment "
        f"grades; {len(report.skipped_runs)} runs already imported")
    return EXIT_OK


def _scorer(settings: RelaySettings) -> Any:
    """The L4 scorer when its model is installed (else None: entailment not graded)."""
    from relay.orchestrator.verifier import EntailmentScorer

    v = settings.verifier
    if not (v.l4_model_dir / v.l4_model_file).is_file():
        return None
    return EntailmentScorer(v.l4_model_dir, model_file=v.l4_model_file, threads=v.l4_threads)


async def run_command(settings: RelaySettings, args: argparse.Namespace, out: Out) -> int:
    scorer = _scorer(settings) if args.with_entailment else None
    async with open_store(settings.paths.store_path) as store:
        if args.replay is not None:
            set_id = args.set or await ensure_v1_set(store, questions_file=args.questions,
                                                     evidence_dir=args.evidence)
            questions = {q.source_id: q.question
                         for q in await store.eval_questions.list_for_set(set_id)
                         if q.source_id}
            async with replay_pipeline(evidence_dir=args.evidence, questions=questions,
                                       retrieval_setting=args.preset,
                                       run_id=args.replay) as replay:
                run = await run_eval(store, replay.pipeline, set_id,
                                     retrieval_setting=args.preset, kind="replay",
                                     collection_id=V1_COLLECTION_ID, lane="standin",
                                     model_id=replay.model_id, source=args.replay,
                                     scorer=scorer)
        else:
            run = await _live_run(settings, store, args, scorer, out)
            if run is None:
                return EXIT_FAILED
        summary = await summarise(store, run.id)
    out(format_summary(summary))
    if args.summary_out is not None:
        _write_summary(args.summary_out, summary)
        out(f"summary written to {args.summary_out}")
    return EXIT_OK


async def _live_run(settings: RelaySettings, store: Store, args: argparse.Namespace,
                    scorer: Any, out: Out) -> Any:
    from relay.app.compose import open_runtime
    from relay.config import load_settings
    from relay.control.router import RoutedGenerator

    lanes = {lane.name: lane for lane in settings.lanes}
    lane_name = args.lane or settings.primary_lane.name
    if lane_name not in lanes:
        out(f"relay eval run: no lane {lane_name!r} (lanes: {sorted(lanes)})")
        return None
    if lanes[lane_name].kind == "groq" and not args.real_calls:
        out("relay eval run: a Groq lane makes real calls; give --real-calls N (the owner's "
            "approved budget) to run it")
        return None
    collection_id = args.collection or V1_COLLECTION_ID
    set_id = args.set
    if set_id is None:
        sets = await store.eval_sets.list_for_collection(collection_id)
        if not sets:
            out(f"relay eval run: collection {collection_id} has no eval set "
                "(relay eval import-v1 for v1-papers)")
            return None
        set_id = sets[0].id
    env = load_settings(args.env_file)
    if args.real_calls:
        env = env.model_copy(update={"max_real_calls": args.real_calls})
    async with open_runtime(settings, env) as runtime:
        service = runtime.service
        router = service.router
        if router is None:
            raise RuntimeError("the service has no router")
        pipeline = service.pipeline.with_generator(
            RoutedGenerator(router).pinned(router.lane(lane_name), "answer"))
        return await run_eval(
            store, pipeline, set_id, retrieval_setting=args.preset,
            kind="real" if lanes[lane_name].kind == "groq" else "standin",
            collection_id=collection_id,
            pipeline_collection=None if collection_id == V1_COLLECTION_ID else collection_id,
            lane=lane_name, model_id=lanes[lane_name].model, scorer=scorer)


def _write_summary(path: Path, summary: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    data = {"gate": gate_metrics(summary), "summary": {k: v for k, v in summary.items()
                                                       if k != "rows"}}
    path.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")


async def report_command(settings: RelaySettings, args: argparse.Namespace, out: Out) -> int:
    async with open_store(settings.paths.store_path) as store:
        try:
            summary = await summarise(store, args.run)
        except KeyError:
            out(f"relay eval report: no run {args.run}")
            return EXIT_FAILED
    out(format_summary(summary))
    if args.json is not None:
        _write_summary(args.json, summary)
        out(f"summary written to {args.json}")
    return EXIT_OK


async def _metrics(store: Store | None, ref: str) -> dict[str, Any] | None:
    path = Path(ref)
    if path.suffix == ".json" and path.is_file():
        return load_metrics(path)
    if store is None:
        return None
    try:
        return gate_metrics(await summarise(store, ref))
    except KeyError:
        return None


async def gate_command(settings: RelaySettings, args: argparse.Namespace, out: Out) -> int:
    needs_store = not all(Path(r).suffix == ".json" and Path(r).is_file()
                          for r in (args.baseline, args.candidate))
    if needs_store:
        async with open_store(settings.paths.store_path) as store:
            baseline = await _metrics(store, args.baseline)
            candidate = await _metrics(store, args.candidate)
    else:
        baseline, candidate = await _metrics(None, args.baseline), await _metrics(
            None, args.candidate)
    if baseline is None or candidate is None:
        out("relay eval gate: baseline or candidate not found (a run id or a summary .json)")
        return EXIT_FAILED
    passed, reasons = gate(baseline, candidate)
    out(f"baseline  {_line(baseline)}")
    out(f"candidate {_line(candidate)}")
    if passed:
        out("gate: PASS")
        return EXIT_OK
    for reason in reasons:
        out(f"gate: FAIL - {reason}")
    return EXIT_FAILED


def _line(metrics: dict[str, Any]) -> str:
    def pct(key: str) -> str:
        value = metrics.get(key)
        return "n/a" if value is None else f"{value:.1%}"
    return (f"correctness {pct('correctness')}, grounded {pct('grounded_rate')}, "
            f"refusal precision {pct('refusal_precision')}")


def add_parser(sub: Any) -> None:
    evaluate = sub.add_parser("eval", help="evaluation: import, run, report, gate")
    evaluate.add_argument("--config", type=Path, default=None)
    commands = evaluate.add_subparsers(dest="eval_command", required=True)
    imp = commands.add_parser("import-v1", help="the v1 questions, answers and grades")
    run = commands.add_parser("run", help="answer and grade an eval set")
    for parser in (imp, run):
        parser.add_argument("--questions", type=Path, default=QUESTIONS_FILE)
        parser.add_argument("--evidence", type=Path, default=EVIDENCE_DIR)
        parser.add_argument("--with-entailment", action="store_true",
                            help="also grade with the L4 model (when it is installed)")
    run.add_argument("--collection", default=None)
    run.add_argument("--set", default=None)
    run.add_argument("--preset", default="small_fast")
    run.add_argument("--lane", default=None)
    run.add_argument("--replay", nargs="?", const=REPLAY_RUN, default=None,
                     help=f"stand-in on v1's recorded passages (default run {REPLAY_RUN})")
    run.add_argument("--summary-out", type=Path, default=None)
    run.add_argument("--real-calls", type=int, default=0,
                     help="the approved real-call budget for a Groq lane")
    run.add_argument("--env-file", type=Path, default=Path(".env"))
    report = commands.add_parser("report", help="the side-by-side grader summary of a run")
    report.add_argument("--run", required=True)
    report.add_argument("--json", type=Path, default=None)
    check = commands.add_parser("gate", help="fail (exit 1) on a regression")
    check.add_argument("--baseline", required=True)
    check.add_argument("--candidate", required=True)


COMMANDS: Final = {"import-v1": import_command, "run": run_command, "report": report_command,
                   "gate": gate_command}

__all__ = ["COMMANDS", "add_parser", "ensure_v1_set"]
