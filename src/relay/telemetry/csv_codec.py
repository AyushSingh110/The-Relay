"""Explicit, non-coercing CSV encoding for `ResultRow`.

Blank cells mean "not applicable / not measured" and map to ``None``; they are never
written as ``0`` or ``-1``. Parsing is strict: e.g. ``"1.0"`` is not an integer and
``"True"`` is not a boolean.
"""

from __future__ import annotations

import csv
import io
from collections.abc import Iterable, Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Final

from pydantic import ValidationError

from relay.errors import ContractViolationError
from relay.schemas import ResultRow

COLUMNS: Final[tuple[str, ...]] = tuple(ResultRow.model_fields)

INT_FIELDS: Final = frozenset({"retries", "tokens_in", "tokens_out", "queue_depth_at_entry"})
MS_FIELDS: Final = frozenset(
    {
        "first_token_ms",
        "total_ms",
        "provider_first_token_ms",
        "server_total_ms",
        "waiting_ms",
        "working_ms",
        "retrying_ms",
        "retrieval_ms",
    }
)
USD_FIELDS: Final = frozenset({"cost_usd"})
BOOL_FIELDS: Final = frozenset({"provider_called", "degraded"})
DATETIME_FIELDS: Final = frozenset({"start_time"})


def format_timestamp(value: datetime) -> str:
    utc = value.astimezone(UTC)
    return utc.strftime("%Y-%m-%dT%H:%M:%S.") + f"{utc.microsecond // 1000:03d}Z"


def format_value(name: str, value: Any) -> str:
    if value is None:
        return ""
    if name in BOOL_FIELDS:
        return "true" if value else "false"
    if name in MS_FIELDS:
        return f"{value:.3f}"
    if name in USD_FIELDS:
        return f"{value:.8f}"
    if name in DATETIME_FIELDS:
        return format_timestamp(value)
    return str(value)


def parse_value(name: str, raw: str) -> Any:
    if raw == "":
        return None
    if name in INT_FIELDS:
        if not raw.isdigit():
            raise ValueError(f"{name}: expected a non-negative integer, got {raw!r}")
        return int(raw)
    if name in MS_FIELDS or name in USD_FIELDS:
        return float(raw)
    if name in BOOL_FIELDS:
        if raw not in ("true", "false"):
            raise ValueError(f"{name}: expected 'true' or 'false', got {raw!r}")
        return raw == "true"
    if name in DATETIME_FIELDS:
        return datetime.fromisoformat(raw)
    return raw


def row_to_record(row: ResultRow) -> dict[str, str]:
    return {name: format_value(name, getattr(row, name)) for name in COLUMNS}


def row_from_record(record: Mapping[str, str | None], *, where: str = "row") -> ResultRow:
    missing = [name for name in COLUMNS if record.get(name) is None]
    if missing:
        raise ContractViolationError(f"{where}: missing columns {missing}")
    try:
        values = {name: parse_value(name, record[name] or "") for name in COLUMNS}
        return ResultRow(**values)
    except (ValueError, ValidationError) as exc:
        raise ContractViolationError(f"{where}: {exc}") from exc


def render_csv(rows: Iterable[ResultRow]) -> str:
    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\n")
    writer.writerow(COLUMNS)
    for row in rows:
        record = row_to_record(row)
        writer.writerow([record[name] for name in COLUMNS])
    return buffer.getvalue()


def header_line() -> str:
    return render_csv([])


def read_rows(path: Path) -> list[ResultRow]:
    """Read and validate every row of a results-format CSV file."""
    with path.open("r", encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle, restkey="__extra__")
        if tuple(reader.fieldnames or ()) != COLUMNS:
            raise ContractViolationError(
                f"{path}: header does not match the ResultRow column contract"
            )
        rows: list[ResultRow] = []
        for line_no, record in enumerate(reader, start=2):
            if "__extra__" in record:
                raise ContractViolationError(f"{path}:{line_no}: too many cells")
            rows.append(row_from_record(record, where=f"{path}:{line_no}"))
    return rows
