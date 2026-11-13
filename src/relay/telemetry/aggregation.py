"""Deterministic aggregation of canonical runs into `results.csv`."""

from __future__ import annotations

import json
import os
import re
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from relay.errors import (
    CanonicalRunsError,
    DuplicateRequestIdError,
    DuplicateRunSelectionError,
    IncompleteRunError,
    ResultsOverwriteRefusedError,
)
from relay.schemas import RUN_ID_PATTERN, ResultRow
from relay.telemetry.csv_codec import read_rows, render_csv
from relay.telemetry.run_store import KINDS_WITH_ROWS, ROWS_FILE, load_manifest, run_dir_for


class CanonicalRuns(BaseModel):
    model_config = ConfigDict(frozen=True, strict=True, extra="forbid")

    schema_version: Literal[1]
    runs: list[str] = Field(default_factory=list)


@dataclass(frozen=True)
class BuildSummary:
    output: Path
    run_count: int
    row_count: int
    outcome: Literal["written", "unchanged", "checked"]
    would_change: bool


def load_canonical_runs(path: Path) -> CanonicalRuns:
    try:
        canonical = CanonicalRuns.model_validate_json(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise CanonicalRunsError(f"canonical run list not found: {path}") from None
    except ValidationError as exc:
        raise CanonicalRunsError(f"invalid canonical run list {path}: {exc}") from exc

    malformed = [run for run in canonical.runs if not re.fullmatch(RUN_ID_PATTERN, run)]
    if malformed:
        raise CanonicalRunsError(f"malformed run_id(s) in {path}: {malformed}")
    duplicates = sorted(run for run, n in Counter(canonical.runs).items() if n > 1)
    if duplicates:
        raise DuplicateRunSelectionError(f"runs selected more than once: {duplicates}")
    return canonical


def collect_rows(evidence_root: Path, canonical: CanonicalRuns) -> list[ResultRow]:
    rows: list[ResultRow] = []
    for run_id in canonical.runs:
        rows.extend(_rows_for_run(evidence_root, run_id))

    counts = Counter(row.request_id for row in rows)
    duplicates = sorted(request_id for request_id, n in counts.items() if n > 1)
    if duplicates:
        shown = duplicates[:10]
        raise DuplicateRequestIdError(
            f"{len(duplicates)} duplicate request_id(s) across selected runs, e.g. {shown}"
        )
    return sorted(rows, key=lambda row: (row.run_id, row.request_id))


def _rows_for_run(evidence_root: Path, run_id: str) -> list[ResultRow]:
    run_dir = run_dir_for(evidence_root, run_id)
    manifest = load_manifest(run_dir)
    if manifest.run_id != run_id:
        raise IncompleteRunError(f"{run_dir}: manifest run_id {manifest.run_id!r} != {run_id!r}")
    if manifest.kind not in KINDS_WITH_ROWS:
        raise IncompleteRunError(f"{run_id}: {manifest.kind} runs have no result rows")
    if manifest.status != "complete":
        raise IncompleteRunError(f"{run_id}: run status is {manifest.status!r}, not 'complete'")

    rows_path = run_dir / ROWS_FILE
    if not rows_path.is_file():
        raise IncompleteRunError(f"{run_id}: missing {ROWS_FILE}")
    rows = read_rows(rows_path)
    for row in rows:
        if row.run_id != run_id:
            raise IncompleteRunError(f"{rows_path}: row {row.request_id} has run_id {row.run_id}")
        if row.scenario not in manifest.scenarios:
            raise IncompleteRunError(
                f"{rows_path}: scenario {row.scenario!r} not declared in the run manifest"
            )
        if manifest.source is not None and row.source != manifest.source:
            raise IncompleteRunError(f"{rows_path}: row {row.request_id} source differs from run")
    return rows


def _atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.tmp")
    with tmp.open("w", encoding="utf-8", newline="") as handle:
        handle.write(text)
    os.replace(tmp, path)


def build_results(
    evidence_root: Path,
    canonical_path: Path,
    output: Path,
    *,
    replace: bool = False,
    check_only: bool = False,
) -> BuildSummary:
    canonical = load_canonical_runs(canonical_path)
    rows = collect_rows(evidence_root, canonical)
    text = render_csv(rows)

    existing = output.read_text(encoding="utf-8") if output.is_file() else None
    would_change = existing != text
    summary = {"output": output, "run_count": len(canonical.runs), "row_count": len(rows)}

    if check_only:
        return BuildSummary(**summary, outcome="checked", would_change=would_change)
    if not would_change:
        return BuildSummary(**summary, outcome="unchanged", would_change=False)
    if existing is not None and not replace:
        raise ResultsOverwriteRefusedError(
            f"{output} exists with different content; re-run with --replace to regenerate it"
        )
    _atomic_write(output, text)
    return BuildSummary(**summary, outcome="written", would_change=True)


def describe(summary: BuildSummary) -> str:
    return json.dumps(
        {
            "output": str(summary.output),
            "runs": summary.run_count,
            "rows": summary.row_count,
            "outcome": summary.outcome,
            "would_change": summary.would_change,
        },
        sort_keys=True,
    )
