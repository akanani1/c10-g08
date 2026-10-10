"""Inspect a log from the command line: ``python -m incident_suite.parsing <file>``.

Shows what the app would detect: format, column mapping, redactions, patterns and context.
Handy for checking a new sample log before adding it to ``labels.json``. Sends nothing anywhere.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from incident_suite.parsing.grouping import group_lines
from incident_suite.parsing.pipeline import parse_upload
from incident_suite.parsing.readers import ReadError


def main(argv: list[str] | None = None) -> int:
    """Print a summary of how a log file parses. Returns the process exit code."""
    parser = argparse.ArgumentParser(prog="python -m incident_suite.parsing")
    parser.add_argument("path", type=Path, help="log file to inspect")
    parser.add_argument("--context", type=int, default=3, help="context lines around errors")
    args = parser.parse_args(argv)

    try:
        parsed = parse_upload(args.path.read_bytes(), args.path.name)
    except (OSError, ReadError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    grouped = group_lines(parsed.lines, args.context)

    print(f"{parsed.name}: {parsed.note}, {len(parsed.lines)} lines")
    if parsed.column_mapping:
        mapping = ", ".join(
            f"{field} <- {column}" for field, column in parsed.column_mapping.items()
        )
        print(f"mapping: {mapping}")
    if parsed.redactions:
        print("redacted: " + ", ".join(f"{n} {kind}" for kind, n in parsed.redactions.items()))
    for warning in parsed.warnings:
        print(f"warning: {warning}")
    print(f"\n{len(grouped.patterns)} WARN+ patterns ({grouped.quiet_line_count} quieter lines):")
    for pattern in grouped.patterns:
        lines = ",".join(map(str, pattern.example_line_nos))
        print(
            f"  {pattern.level:<5} x{pattern.count:<5} [{pattern.service or '-'}] "
            f"{pattern.signature}  (lines {lines})"
        )
    if grouped.change_lines:
        print("\nchanges:")
        for line in grouped.change_lines:
            print(f"  {line.line_no}: {line.message}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
