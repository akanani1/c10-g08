"""Upload bytes to a redacted, parsed log: read, redact, then parse (sections 3, 18.2 and 23)."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable
from datetime import UTC, datetime

from incident_suite.parsing.parser import (
    ColumnMapper,
    detect_text_format,
    parse_record,
    parse_text_line,
)
from incident_suite.parsing.readers import (
    ContainerFormat,
    ReadLimits,
    StructuredRecord,
    TextRecord,
    read_bytes,
    truncate,
)
from incident_suite.parsing.redact import Redactor
from incident_suite.state import LogFormat, LogLine, ParsedLog, RedactionType

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
