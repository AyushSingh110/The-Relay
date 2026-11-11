"""Evidence run directories: exclusive creation, manifests, rows and traces.

Layout (ARCHITECTURE.md section 13.1)::

    evidence/groq_probes/<run_id>/          manifest.json, rows.csv, traces.jsonl
    evidence/standin_calibration/<run_id>/  manifest.json (+ calibration outputs)
    evidence/experiments/<run_id>/          manifest.json, rows.csv, traces.jsonl

A run directory is never reused. Writes are serialised with a lock so concurrent
callers in one process cannot interleave lines.
"""

from __future__ import annotations

import json
import os
import subprocess
import threading
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Final

from pydantic import ValidationError

from relay.errors import (
    RunAlreadyExistsError,
    RunClosedError,
    RunNotFoundError,
    RunStoreError,
)
from relay.redaction import redact_secrets
from relay.schemas import RUN_ID_PREFIX, ResultRow, RunKind, RunManifest, Source
from relay.telemetry.csv_codec import header_line, render_csv

MANIFEST_FILE: Final = "manifest.json"
ROWS_FILE: Final = "rows.csv"
TRACES_FILE: Final = "traces.jsonl"

KIND_DIRS: Final[dict[str, str]] = {
    "groq_probe": "groq_probes",
    "standin_calibration": "standin_calibration",
    "experiment": "experiments",
}
KINDS_WITH_ROWS: Final = frozenset({"groq_probe", "experiment"})
NO_GIT: Final = "nogit"


def make_run_id(kind: RunKind, started_at: datetime, git_commit: str) -> str:
    stamp = started_at.astimezone(UTC).strftime("%Y%m%dT%H%M%SZ")
    return f"{RUN_ID_PREFIX[kind]}-{stamp}-{git_commit}"


def current_git_state(repo_root: Path) -> tuple[str, bool]:
    """Return (short commit SHA, working-tree-dirty). Falls back to ('nogit', True)."""
    try:
        sha = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=repo_root, capture_output=True, text=True, check=True, timeout=10,
        ).stdout.strip().lower()
        status = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=repo_root, capture_output=True, text=True, check=True, timeout=10,
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return NO_GIT, True
    return (sha or NO_GIT), bool(status.strip())


def _atomic_write_text(path: Path, text: str) -> None:
    tmp = path.with_name(f".{path.name}.tmp")
    with tmp.open("w", encoding="utf-8", newline="") as handle:
        handle.write(text)
    os.replace(tmp, path)


def _write_manifest(run_dir: Path, manifest: RunManifest) -> None:
    payload = json.dumps(manifest.model_dump(mode="json"), indent=2, sort_keys=True) + "\n"
    _atomic_write_text(run_dir / MANIFEST_FILE, payload)


def load_manifest(run_dir: Path) -> RunManifest:
    path = run_dir / MANIFEST_FILE
    if not path.is_file():
        raise RunNotFoundError(f"no manifest at {path}")
    try:
        return RunManifest.model_validate_json(path.read_text(encoding="utf-8"))
    except ValidationError as exc:
        raise RunStoreError(f"invalid manifest {path}: {exc}") from exc


def run_dir_for(evidence_root: Path, run_id: str) -> Path:
    """Locate a run directory from its run_id (the prefix encodes the kind)."""
    prefix = run_id.split("-", 1)[0]
    for kind, run_prefix in RUN_ID_PREFIX.items():
        if prefix == run_prefix:
            path = evidence_root / KIND_DIRS[kind] / run_id
            if not path.is_dir():
                raise RunNotFoundError(f"run directory not found: {path}")
            return path
    raise RunNotFoundError(f"unrecognised run_id prefix: {run_id!r}")


class RunHandle:
    """Write access to one in-progress run directory."""

    def __init__(self, run_dir: Path, manifest: RunManifest) -> None:
        self._run_dir = run_dir
        self._manifest = manifest
        self._lock = threading.Lock()
        self._next_seq = 1
        self._request_ids: set[str] = set()

    @property
    def run_id(self) -> str:
        return self._manifest.run_id

    @property
    def run_dir(self) -> Path:
        return self._run_dir

    @property
    def manifest(self) -> RunManifest:
        return self._manifest

    def new_request_id(self) -> str:
        with self._lock:
            request_id = f"{self.run_id}-{self._next_seq:06d}"
            self._next_seq += 1
            return request_id

    def append_row(self, row: ResultRow) -> None:
        if self._manifest.kind not in KINDS_WITH_ROWS:
            raise RunStoreError(f"{self._manifest.kind} runs do not record result rows")
        if row.run_id != self.run_id:
            raise RunStoreError(f"row run_id {row.run_id!r} does not match run {self.run_id!r}")
        if row.scenario not in self._manifest.scenarios:
            raise RunStoreError(f"scenario {row.scenario!r} is not declared in the run manifest")
        if self._manifest.source is not None and row.source != self._manifest.source:
            raise RunStoreError(f"row source {row.source!r} differs from run source")
        line = render_csv([row]).removeprefix(header_line())
        with self._lock:
            self._ensure_open()
            if row.request_id in self._request_ids:
                raise RunStoreError(f"duplicate request_id within run: {row.request_id}")
            with (self._run_dir / ROWS_FILE).open("a", encoding="utf-8", newline="") as handle:
                handle.write(line)
            self._request_ids.add(row.request_id)

    def append_trace(self, record: Mapping[str, Any]) -> None:
        """Append one sanitised JSON record to traces.jsonl."""
        line = json.dumps(redact_secrets(dict(record)), sort_keys=True, default=str) + "\n"
        with self._lock:
            self._ensure_open()
            with (self._run_dir / TRACES_FILE).open("a", encoding="utf-8", newline="") as handle:
                handle.write(line)

    def record_real_calls(self, count: int = 1) -> None:
        if count < 0:
            raise ValueError("count must be non-negative")
        with self._lock:
            self._ensure_open()
            self._manifest = self._manifest.model_copy(
                update={"real_call_count": self._manifest.real_call_count + count}
            )
            _write_manifest(self._run_dir, self._manifest)

    def finalize(
        self,
        status: str = "complete",
        *,
        notes: str | None = None,
        ended_at: datetime | None = None,
    ) -> RunManifest:
        if status not in ("complete", "aborted"):
            raise ValueError("final status must be 'complete' or 'aborted'")
        with self._lock:
            self._ensure_open()
            data = self._manifest.model_dump()
            data.update(
                status=status,
                ended_at=ended_at or datetime.now(UTC),
                notes=notes if notes is not None else self._manifest.notes,
            )
            self._manifest = RunManifest.model_validate(data)
            _write_manifest(self._run_dir, self._manifest)
            return self._manifest

    def __enter__(self) -> RunHandle:
        return self

    def __exit__(self, exc_type: type[BaseException] | None, *_: object) -> None:
        if self._manifest.status != "in_progress":
            return
        if exc_type is None:
            self.finalize("complete")
            return
        self.finalize("aborted", notes=f"aborted by exception: {exc_type.__name__}")

    def _ensure_open(self) -> None:
        if self._manifest.status != "in_progress":
            raise RunClosedError(f"run {self.run_id} is already {self._manifest.status}")


def create_run(
    evidence_root: Path,
    kind: RunKind,
    *,
    scenarios: Sequence[str],
    config_snapshot: Mapping[str, Any],
    source: Source | None = None,
    model_id: str | None = None,
    reasoning_settings: Mapping[str, Any] | None = None,
    retrieval_settings: Sequence[str] = (),
    cache_mode: str | None = None,
    seed: int | None = None,
    notes: str | None = None,
    started_at: datetime | None = None,
    git_state: tuple[str, bool] | None = None,
    repo_root: Path | None = None,
) -> RunHandle:
    """Create a new, exclusive run directory with an ``in_progress`` manifest."""
    started = started_at or datetime.now(UTC)
    git_commit, git_dirty = git_state or current_git_state(repo_root or Path.cwd())
    run_id = make_run_id(kind, started, git_commit)
    try:
        manifest = RunManifest(
            run_id=run_id,
            kind=kind,
            scenarios=tuple(scenarios),
            source=source,
            model_id=model_id,
            reasoning_settings=None if reasoning_settings is None else dict(reasoning_settings),
            retrieval_settings=tuple(retrieval_settings),
            cache_mode=cache_mode,  # type: ignore[arg-type]
            seed=seed,
            config_snapshot=redact_secrets(dict(config_snapshot)),
            git_commit=git_commit,
            git_dirty=git_dirty,
            started_at=started,
            ended_at=None,
            real_call_count=0,
            status="in_progress",
            notes=notes,
        )
    except ValidationError as exc:
        raise RunStoreError(f"invalid run manifest: {exc}") from exc

    kind_dir = evidence_root / KIND_DIRS[kind]
    kind_dir.mkdir(parents=True, exist_ok=True)
    run_dir = kind_dir / run_id
    try:
        run_dir.mkdir(exist_ok=False)
    except FileExistsError:
        raise RunAlreadyExistsError(f"run directory already exists: {run_dir}") from None

    _write_manifest(run_dir, manifest)
    if kind in KINDS_WITH_ROWS:
        _atomic_write_text(run_dir / ROWS_FILE, header_line())
    return RunHandle(run_dir, manifest)


__all__ = [
    "KIND_DIRS",
    "KINDS_WITH_ROWS",
    "MANIFEST_FILE",
    "ROWS_FILE",
    "TRACES_FILE",
    "RunHandle",
    "create_run",
    "current_git_state",
    "load_manifest",
    "make_run_id",
    "run_dir_for",
]
