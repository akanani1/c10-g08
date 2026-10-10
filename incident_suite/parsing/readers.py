"""Read uploaded bytes into records, whatever the file format.

The content decides the format, not the extension: Parquet starts with ``PAR1``, gzip with
``1f 8b``, JSON with ``{`` or ``[``, and CSV/TSV is confirmed by a consistent header and column
count. Anything else is read as text. Every reader enforces the same caps, so a small upload
can't expand into something that exhausts memory.

``parse_upload`` is the entry point: read, then redact, then parse into a ``ParsedLog``.
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
import re
import zlib
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

import pyarrow as pa
import pyarrow.parquet as pq

from incident_suite.parsing.parser import (
    ColumnMapper,
    detect_text_format,
    parse_record,
    parse_text_line,
)
from incident_suite.parsing.redact import Redactor
from incident_suite.state import LogFormat, LogLine, ParsedLog, RedactionType

GZIP_MAGIC = b"\x1f\x8b"
PARQUET_MAGIC = b"PAR1"
TRUNCATION_MARK = " …[truncated]"
_SNIFF_LINES = 20
_HEADER_CELL = re.compile(r"^[@A-Za-z_][\w.@ -]{0,63}$")
# Keys that usually hold the list of records in a JSON document
_RECORD_LIST_KEYS = ("logs", "records", "events", "entries", "items", "data", "results", "hits")

# Big CSV cells are cut to max_line_chars after reading, but the csv module must not refuse
# them first. 10 MB is far above any cell that fits under the decompressed-size cap per row.
csv.field_size_limit(10 * 1024 * 1024)


class ContainerFormat(StrEnum):
    """How the records are stored. Text logs get their line format detected by the parser."""

    TEXT = "text"
    JSON_LINES = "json_lines"
    JSON_DOCUMENT = "json_document"
    CSV = "csv"
    TSV = "tsv"
    PARQUET = "parquet"


class ReadError(ValueError):
    """The file can't be read. The message is safe and meant to be shown to the user."""


@dataclass(frozen=True)
class ReadLimits:
    """Caps applied while reading."""

    max_decompressed_bytes: int = 50 * 1024 * 1024
    max_records: int = 200_000
    max_columns: int = 200
    max_line_chars: int = 4096
    max_json_depth: int = 5


@dataclass(frozen=True)
class TextRecord:
    """One line of a text log. ``line_no`` is 1-based."""

    line_no: int
    text: str


@dataclass(frozen=True)
class StructuredRecord:
    """One row or object with named fields. ``line_no`` is the 1-based row number."""

    line_no: int
    fields: dict[str, str]


Record = TextRecord | StructuredRecord


@dataclass(frozen=True)
class ReadResult:
    """What a reader found: the container format, the records and a note for the user."""

    container: ContainerFormat
    records: list[Record]
    compressed: bool = False
    columns: list[str] = field(default_factory=list)
    delimiter: str | None = None
    warnings: list[str] = field(default_factory=list)

    @property
    def note(self) -> str:
        """What the app tells the user it detected, e.g. "Detected CSV, 3 columns"."""
        names = {
            ContainerFormat.TEXT: "text",
            ContainerFormat.JSON_LINES: "JSON lines",
            ContainerFormat.JSON_DOCUMENT: "JSON document",
            ContainerFormat.CSV: "CSV",
            ContainerFormat.TSV: "TSV",
            ContainerFormat.PARQUET: "Parquet",
        }
        text = f"Detected {names[self.container]}"
        if self.compressed:
            text += " (gzip)"
        if self.columns:
            text += f", {len(self.columns)} columns"
        return text


# ------------------------------------------------------------------ helpers


def truncate(text: str, limit: int) -> str:
    """Cut ``text`` to ``limit`` characters, marking the cut."""
    if len(text) <= limit:
        return text
    return text[: max(0, limit - len(TRUNCATION_MARK))] + TRUNCATION_MARK


def gunzip(data: bytes, limit: int) -> bytes:
    """Decompress gzip data as a stream, stopping as soon as the output passes ``limit``."""
    inflater = zlib.decompressobj(wbits=16 + zlib.MAX_WBITS)
    out = bytearray()
    try:
        chunk = inflater.decompress(data, limit + 1 - len(out))
        out += chunk
        while inflater.unconsumed_tail and len(out) <= limit:
            out += inflater.decompress(inflater.unconsumed_tail, limit + 1 - len(out))
    except zlib.error as exc:
        msg = "The .gz file is damaged and can't be decompressed."
        raise ReadError(msg) from exc
    if len(out) <= limit and not inflater.eof:
        msg = "The .gz file is damaged or cut short and can't be decompressed."
        raise ReadError(msg)
    if len(out) > limit:
        mb = limit // (1024 * 1024)
        msg = f"The file is larger than {mb} MB once decompressed, so it was not read."
        raise ReadError(msg)
    return bytes(out)


def decode_text(data: bytes) -> str:
    """Bytes to text: UTF-8 (or UTF-16 with a BOM), bad bytes become replacement characters."""
    if data.startswith((b"\xff\xfe", b"\xfe\xff")):
        return data.decode("utf-16", errors="replace")
    return data.decode("utf-8-sig", errors="replace")


def _check_record_count(count: int, limits: ReadLimits) -> None:
    if count > limits.max_records:
        msg = f"The file has more than {limits.max_records:,} lines or rows, so it was not read."
        raise ReadError(msg)


def _check_columns(columns: list[str], limits: ReadLimits) -> None:
    if len(columns) > limits.max_columns:
        msg = f"The file has {len(columns)} columns; the limit is {limits.max_columns}."
        raise ReadError(msg)


def to_text(value: Any) -> str:
    """Render a cell value as text. ``None`` becomes an empty string."""
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (list, tuple, dict)):
        return json.dumps(value, ensure_ascii=False, default=str, separators=(",", ":"))
    if hasattr(value, "isoformat"):
        return str(value.isoformat())
    return str(value)


def flatten(obj: Mapping[str, Any], max_depth: int, prefix: str = "") -> dict[str, Any]:
    """Flatten nested objects into dotted keys (``http.status``) up to ``max_depth`` levels.

    Anything deeper is kept as a JSON string under the last allowed key.
    """
    flat: dict[str, Any] = {}
    for key, value in obj.items():
        name = f"{prefix}{key}"
        if isinstance(value, Mapping) and max_depth > 1:
            flat |= flatten(value, max_depth - 1, f"{name}.")
        else:
            flat[name] = value
    return flat


# ------------------------------------------------------------------ detection


def _looks_like_delimited(text: str) -> str | None:
    """Return the delimiter if ``text`` starts with a header row and a consistent table."""
    lines = [line for line in text.splitlines()[:_SNIFF_LINES] if line.strip()]
    if len(lines) < 2:
        return None
    for delimiter in ("\t", ",", ";", "|"):
        rows = list(csv.reader(lines, delimiter=delimiter))
        header = rows[0]
        if len(header) < 2 or not all(_HEADER_CELL.match(cell.strip()) for cell in header):
            continue
        if all(len(row) == len(header) for row in rows[1:]):
            return delimiter
    return None


def _strip_gz(name: str) -> str:
    return name[:-3] if name.lower().endswith(".gz") else name


def detect_container(data: bytes, filename: str = "") -> ContainerFormat:
    """Work out the container format from content first, then the file extension."""
    if data.startswith(PARQUET_MAGIC):
        return ContainerFormat.PARQUET
    text = decode_text(data[:65536])
    stripped = text.lstrip()
    if stripped.startswith("["):
        return ContainerFormat.JSON_DOCUMENT
    if stripped.startswith("{"):
        first_line = stripped.split("\n", 1)[0].strip()
        try:
            first = json.loads(first_line)
        except (json.JSONDecodeError, RecursionError):
            return ContainerFormat.JSON_DOCUMENT
        holds_records = isinstance(first, dict) and any(
            isinstance(value, list) and value and isinstance(value[0], dict)
            for value in first.values()
        )
        return ContainerFormat.JSON_DOCUMENT if holds_records else ContainerFormat.JSON_LINES
    delimiter = _looks_like_delimited(text)
    if delimiter is not None:
        return ContainerFormat.TSV if delimiter == "\t" else ContainerFormat.CSV
    extension = filename.lower().rsplit(".", 1)[-1] if "." in filename else ""
    if extension in {"csv", "tsv"} and "\n" in text:
        return ContainerFormat.TSV if extension == "tsv" else ContainerFormat.CSV
    return ContainerFormat.TEXT


# ------------------------------------------------------------------ readers


def read_text(text: str, limits: ReadLimits) -> list[Record]:
    """One record per line, keeping line numbers even for blank lines."""
    lines = text.splitlines()
    _check_record_count(len(lines), limits)
    return [
        TextRecord(line_no=number, text=truncate(line, limits.max_line_chars))
        for number, line in enumerate(lines, start=1)
        if line.strip()
    ]


def _structured(line_no: int, obj: Mapping[str, Any], limits: ReadLimits) -> StructuredRecord:
    flat = flatten(obj, limits.max_json_depth)
    if len(flat) > limits.max_columns:
        msg = f"Record {line_no} has {len(flat)} fields; the limit is {limits.max_columns}."
        raise ReadError(msg)
    return StructuredRecord(
        line_no=line_no,
        fields={str(k): truncate(to_text(v), limits.max_line_chars) for k, v in flat.items()},
    )


def _load_json(text: str, where: str) -> Any:
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        msg = f"Invalid JSON {where}: {exc.msg} (line {exc.lineno}, column {exc.colno})."
        raise ReadError(msg) from exc
    except RecursionError as exc:
        msg = f"The JSON {where} is nested too deeply to read."
        raise ReadError(msg) from exc


def read_json_lines(text: str, limits: ReadLimits) -> tuple[list[Record], list[str]]:
    """One object per line. Lines that aren't JSON objects are kept as text records."""
    lines = text.splitlines()
    _check_record_count(len(lines), limits)
    records: list[Record] = []
    not_json = 0
    for number, line in enumerate(lines, start=1):
        if not line.strip():
            continue
        try:
            obj = json.loads(line)
        except (json.JSONDecodeError, RecursionError):
            obj = None
        if isinstance(obj, dict):
            records.append(_structured(number, obj, limits))
        else:
            not_json += 1
            records.append(TextRecord(line_no=number, text=truncate(line, limits.max_line_chars)))
    warnings = (
        [f"{not_json} lines were not JSON objects and were read as text."] if not_json else []
    )
    return records, warnings


def _find_record_list(document: Any) -> list[Any]:
    if isinstance(document, list):
        return document
    if isinstance(document, dict):
        for key in _RECORD_LIST_KEYS:
            value = document.get(key)
            if isinstance(value, list):
                return value
        for value in document.values():
            if isinstance(value, list) and value and isinstance(value[0], dict):
                return value
        return [document]
    msg = "The JSON document doesn't contain any log records."
    raise ReadError(msg)


def read_json_document(text: str, limits: ReadLimits) -> list[Record]:
    """A JSON array of records, or an object holding one (e.g. ``{"logs": [...]}``)."""
    items = _find_record_list(_load_json(text, "document"))
    _check_record_count(len(items), limits)
    return [
        _structured(number, item if isinstance(item, dict) else {"message": item}, limits)
        for number, item in enumerate(items, start=1)
    ]


def read_delimited(
    text: str, delimiter: str, limits: ReadLimits
) -> tuple[list[Record], list[str], list[str]]:
    """CSV or TSV with a header row. Rows that don't match the header are kept and reported."""
    reader = csv.reader(io.StringIO(text), delimiter=delimiter)
    try:
        header = [cell.strip() or f"column_{i}" for i, cell in enumerate(next(reader), start=1)]
    except StopIteration as exc:
        msg = "The file is empty."
        raise ReadError(msg) from exc
    _check_columns(header, limits)
    records: list[Record] = []
    malformed = 0
    try:
        for number, row in enumerate(reader, start=1):
            _check_record_count(number, limits)
            if not any(cell.strip() for cell in row):
                continue
            if len(row) != len(header):
                malformed += 1
            cells = dict(zip(header, row, strict=False))
            if len(row) > len(header):
                cells["_extra"] = delimiter.join(row[len(header) :])
            records.append(
                StructuredRecord(
                    line_no=number,
                    fields={k: truncate(v, limits.max_line_chars) for k, v in cells.items()},
                )
            )
    except csv.Error as exc:
        msg = f"The file couldn't be read as a table: {exc}."
        raise ReadError(msg) from exc
    warnings = (
        [f"{malformed} rows had a different number of columns than the header."]
        if malformed
        else []
    )
    return records, header, warnings


def read_parquet(data: bytes, limits: ReadLimits) -> tuple[list[Record], list[str]]:
    """Parquet via pyarrow only, in batches. No pickle or custom-type loading."""
    try:
        parquet = pq.ParquetFile(io.BytesIO(data))
    except (pa.ArrowException, OSError) as exc:
        msg = "The Parquet file is damaged or not a Parquet file."
        raise ReadError(msg) from exc
    columns = list(parquet.schema_arrow.names)
    _check_columns(columns, limits)
    _check_record_count(parquet.metadata.num_rows, limits)
    records: list[Record] = []
    for batch in parquet.iter_batches(batch_size=10_000):
        first = len(records) + 1
        records += [
            _structured(number, row, limits)
            for number, row in enumerate(batch.to_pylist(), start=first)
        ]
    return records, columns


def read_bytes(data: bytes, filename: str = "", limits: ReadLimits | None = None) -> ReadResult:
    """Read an uploaded file into records. Raises :class:`ReadError` with a user-facing message."""
    limits = limits or ReadLimits()
    if not data.strip():
        msg = "The file is empty."
        raise ReadError(msg)
    compressed = data.startswith(GZIP_MAGIC)
    if compressed:
        data = gunzip(data, limits.max_decompressed_bytes)
        filename = _strip_gz(filename)
        if data.startswith(GZIP_MAGIC):
            msg = "Gzip inside gzip isn't supported."
            raise ReadError(msg)
    elif len(data) > limits.max_decompressed_bytes:
        msg = "The file is too large to read."
        raise ReadError(msg)

    container = detect_container(data, filename)
    if container is ContainerFormat.PARQUET:
        records, columns = read_parquet(data, limits)
        return ReadResult(container, records, compressed, columns=columns)

    text = decode_text(data)
    if container is ContainerFormat.JSON_LINES:
        records, warnings = read_json_lines(text, limits)
        return ReadResult(container, records, compressed, warnings=warnings)
    if container is ContainerFormat.JSON_DOCUMENT:
        return ReadResult(container, read_json_document(text, limits), compressed)
    if container in (ContainerFormat.CSV, ContainerFormat.TSV):
        delimiter = _looks_like_delimited(text) or (
            "\t" if container is ContainerFormat.TSV else ","
        )
        records, header, warnings = read_delimited(text, delimiter, limits)
        return ReadResult(
            container, records, compressed, columns=header, delimiter=delimiter, warnings=warnings
        )
    return ReadResult(container, read_text(text, limits), compressed)


# ------------------------------------------------------------------ read, redact, parse

_STRUCTURED_FORMATS = {
    ContainerFormat.JSON_LINES: LogFormat.JSON_LINES,
    ContainerFormat.JSON_DOCUMENT: LogFormat.JSON_DOCUMENT,
    ContainerFormat.CSV: LogFormat.CSV,
    ContainerFormat.TSV: LogFormat.TSV,
    ContainerFormat.PARQUET: LogFormat.PARQUET,
}
_TEXT_NAMES = {
    LogFormat.PLAIN: "app log",
    LogFormat.NGINX_ACCESS: "nginx access log",
    LogFormat.NGINX_ERROR: "nginx error log",
    LogFormat.SYSLOG: "syslog",
    LogFormat.KUBERNETES: "Kubernetes log",
}


def fingerprint(data: bytes) -> str:
    """Short hash of the uploaded bytes, shown with the run ID in the app, Slack and JIRA."""
    return hashlib.sha256(data).hexdigest()[:16]


def _parse_text(
    records: Iterable[TextRecord], redactor: Redactor, year: int
) -> tuple[LogFormat, list[LogLine]]:
    redacted = [(record.line_no, redactor.redact_line(record.text)) for record in records]
    fmt = detect_text_format([text for _, text in redacted])
    parsed = [parse_text_line(line_no, text, fmt, year) for line_no, text in redacted]
    # In a timestamped log, a line without its own timestamp (a stack trace, a wrapped
    # message) belongs to the entry above it.
    timestamped = sum(1 for line in parsed if line.timestamp is not None) * 2 >= len(parsed)
    lines: list[LogLine] = []
    for line in parsed:
        if timestamped and lines and line.timestamp is None:
            previous = lines[-1]
            inherited = {
                "level": previous.level,
                "service": previous.service,
                "timestamp": previous.timestamp,
            }
            lines.append(line.model_copy(update=inherited))
        else:
            lines.append(line)
    return fmt, lines


def _raw(fields: dict[str, str], delimiter: str | None, limit: int) -> str:
    if delimiter is not None:
        return truncate(delimiter.join(fields.values()), limit)
    return truncate(json.dumps(fields, ensure_ascii=False, separators=(",", ":")), limit)


def parse_upload(
    data: bytes,
    filename: str,
    redaction: Iterable[RedactionType] = tuple(RedactionType),
    limits: ReadLimits | None = None,
    year: int | None = None,
) -> ParsedLog:
    """Read, redact and parse an upload. Raises ``ReadError`` with a user-facing message.

    ``year`` fills in syslog timestamps, which have none; it defaults to the current year.
    """
    limits = limits or ReadLimits()
    year = year or datetime.now(UTC).year
    result = read_bytes(data, filename, limits)
    redactor = Redactor(redaction)

    if result.container is ContainerFormat.TEXT:
        fmt, lines = _parse_text(
            (r for r in result.records if isinstance(r, TextRecord)), redactor, year
        )
        note = f"Detected {_TEXT_NAMES[fmt]}" + (" (gzip)" if result.compressed else "")
        mapping: dict[str, str] = {}
    else:
        fmt = _STRUCTURED_FORMATS[result.container]
        mapper = ColumnMapper()
        lines = []
        for record in result.records:
            if isinstance(record, StructuredRecord):
                fields = {k: redactor.redact_field(k, v) for k, v in record.fields.items()}
                raw = _raw(fields, result.delimiter, limits.max_line_chars)
                lines.append(parse_record(record.line_no, fields, raw, mapper, year))
            else:  # a non-JSON line inside a JSON-lines file
                text = redactor.redact_line(record.text)
                lines.append(parse_text_line(record.line_no, text, LogFormat.PLAIN, year))
        note = result.note
        mapping = mapper.mapping

    return ParsedLog(
        name=filename,
        fingerprint=fingerprint(data),
        format=fmt,
        compressed=result.compressed,
        note=note,
        column_mapping=mapping,
        lines=lines,
        redactions=redactor.summary(),
        warnings=result.warnings,
    )
