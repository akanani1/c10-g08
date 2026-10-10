"""Accuracy report for the README: run every sample log through the agents and score them.

    python eval/run_eval.py                      # all 17 samples, about $0.10 to $0.30
    python eval/run_eval.py --only payment_db_conn_refused.log,healthy_service.csv

Runs are dry runs with the real models, so nothing is sent to Slack or JIRA. Each run is
compared with ``sample_logs/labels.json``:

- **Found:** an expected issue is matched to a found one that shares an evidence line, or
  else has the same service, or else has the label's signature hint in its title.
- **Category / severity accuracy:** matched issues with the right category or severity, out
  of all expected issues. A missed issue counts as wrong.
- **Fix mentions the key idea:** the fix for a matched issue mentions one of the label's
  ``expected_fix_keywords``.
- **Critical issues ticketed:** logs that should open a ticket and would.
- **Slack decision:** logs where "post to Slack or not" matches the label.

The scoring functions are pure, so ``tests/unit/test_run_eval.py`` checks them without a key.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from incident_suite.state import Issue, Remediation, RunResult, TicketAction

SAMPLES = Path(__file__).resolve().parents[1] / "sample_logs"
LARGE_SAMPLE = "large_50k_mixed.parquet"


@dataclass(frozen=True)
class SampleScore:
    """How one sample log did."""

    file: str
    expected: int
    found: int
    category_ok: int
    severity_ok: int
    fix_ok: int
    ticket_expected: bool
    ticket_planned: bool
    slack_expected: bool
    slack_planned: bool
    lines: int
    patterns: int
    tokens: int
    cost_usd: float
    seconds: float
    errors: int


# ------------------------------------------------------------------ scoring


def _match(expected: dict[str, Any], candidates: Sequence[Issue]) -> Issue | None:
    """The found issue that best fits one labelled issue, or ``None``."""
    lines = set(expected.get("evidence_lines", []))
    service = str(expected.get("service", "")).lower()
    hint = str(expected.get("signature_hint", "")).lower()
    tests: tuple[Callable[[Issue], bool], ...] = (
        lambda issue: bool(lines & set(issue.evidence_lines)),
        lambda issue: bool(service) and service in (issue.affected_service or "").lower(),
        lambda issue: bool(hint) and hint in issue.title.lower(),
    )
    for test in tests:
        for issue in candidates:
            if test(issue):
                return issue
    return None


def _mentions(fix: Remediation | None, keywords: Sequence[str]) -> bool:
    if fix is None:
        return False
    text = " ".join([*fix.steps, fix.rationale, fix.root_cause]).lower()
    return any(keyword.lower() in text for keyword in keywords)


def score(label: dict[str, Any], result: RunResult, seconds: float) -> SampleScore:
    """Compare one run with its label."""
    fixes = {fix.issue_id: fix for fix in result.remediations}
    unmatched = list(result.issues)
    found = category_ok = severity_ok = fix_ok = 0
    for expected in label["expected_issues"]:
        issue = _match(expected, unmatched)
        if issue is None:
            continue
        unmatched.remove(issue)
        found += 1
        category_ok += issue.category == expected["category"]
        severity_ok += issue.severity == expected["severity"]
        fix_ok += _mentions(fixes.get(issue.id), expected.get("expected_fix_keywords", []))
    return SampleScore(
        file=label["file"],
        expected=len(label["expected_issues"]),
        found=found,
        category_ok=category_ok,
        severity_ok=severity_ok,
        fix_ok=fix_ok,
        ticket_expected=bool(label["expect_ticket"]),
        ticket_planned=any(t.action is not TicketAction.SKIPPED_OVER_CAP for t in result.tickets),
        slack_expected=bool(label["expect_slack"]),
        slack_planned=result.notification is not None and result.notification.post,
        lines=result.line_count,
        patterns=len(result.patterns),
        tokens=sum(u.input_tokens + u.output_tokens for u in result.usage),
        cost_usd=result.total_cost_usd,
        seconds=seconds,
        errors=len(result.errors),
    )


def _ratio(hits: int, total: int) -> str:
    return f"{hits}/{total} ({hits / total:.0%})" if total else "n/a"


def summary(scores: Sequence[SampleScore]) -> list[tuple[str, str]]:
    """The README results table, as (metric, result) rows."""
    expected = sum(s.expected for s in scores)
    should_ticket = [s for s in scores if s.ticket_expected]
    rows = [
        ("Issues found", _ratio(sum(s.found for s in scores), expected)),
        ("Category accuracy", _ratio(sum(s.category_ok for s in scores), expected)),
        ("Severity accuracy", _ratio(sum(s.severity_ok for s in scores), expected)),
        ("Fix mentions the key idea", _ratio(sum(s.fix_ok for s in scores), expected)),
        (
            "Critical issues correctly ticketed",
            _ratio(sum(s.ticket_planned for s in should_ticket), len(should_ticket)),
        ),
        (
            "Tickets that shouldn't have been opened",
            str(sum(s.ticket_planned and not s.ticket_expected for s in scores)),
        ),
        (
            "Slack decision right",
            _ratio(sum(s.slack_planned == s.slack_expected for s in scores), len(scores)),
        ),
    ]
    if scores:
        rows.append(
            (
                "Average per run: tokens / cost / time",
                f"{sum(s.tokens for s in scores) / len(scores):,.0f} / "
                f"${sum(s.cost_usd for s in scores) / len(scores):.4f} / "
                f"{sum(s.seconds for s in scores) / len(scores):.1f} s",
            )
        )
    large = next((s for s in scores if s.file == LARGE_SAMPLE), None)
    if large is not None:
        rows.append(
            (
                "50k-row log: patterns / tokens / cost / time",
                f"{large.lines:,} rows to {large.patterns} patterns / {large.tokens:,} / "
                f"${large.cost_usd:.4f} / {large.seconds:.1f} s",
            )
        )
    return rows


def markdown(scores: Sequence[SampleScore]) -> str:
    """The summary table and a per-log table, ready to paste into the README."""
    lines = ["| Metric | Result |", "|---|---|"]
    lines += [f"| {metric} | {value} |" for metric, value in summary(scores)]
    lines += [
        "",
        "| Log | Found | Category | Severity | Ticket (expected / planned) | Tokens | Cost |",
        "|---|---|---|---|---|---|---|",
    ]
    lines += [
        f"| `{s.file}` | {s.found}/{s.expected} | {s.category_ok} | {s.severity_ok} | "
        f"{'yes' if s.ticket_expected else 'no'} / {'yes' if s.ticket_planned else 'no'} | "
        f"{s.tokens:,} | ${s.cost_usd:.4f} |"
        for s in scores
    ]
    return "\n".join(lines)


# ------------------------------------------------------------------ running


def main(argv: list[str] | None = None) -> int:
    """Run the samples with the real models and print the tables."""
    from incident_suite.agents import OpenRouterLLM, make_embeddings  # noqa: PLC0415
    from incident_suite.config import load_config  # noqa: PLC0415
    from incident_suite.graph import RunContext, build_graph, start_run  # noqa: PLC0415
    from incident_suite.parsing.readers import parse_upload  # noqa: PLC0415
    from incident_suite.rag.retriever import RunbookRetriever  # noqa: PLC0415
    from incident_suite.rag.runbooks import load_runbooks  # noqa: PLC0415
    from incident_suite.state import RunSettings  # noqa: PLC0415
    from incident_suite.telemetry import new_run_id  # noqa: PLC0415

    parser = argparse.ArgumentParser(prog="python eval/run_eval.py")
    parser.add_argument("--only", default="", help="comma-separated sample file names")
    args = parser.parse_args(argv)
    config = load_config()
    if not config.llm_configured:
        print("error: OPENROUTER_API_KEY is not set", file=sys.stderr)
        return 1
    labels = json.loads((SAMPLES / "labels.json").read_text(encoding="utf-8"))["logs"]
    wanted = {name.strip() for name in args.only.split(",") if name.strip()}
    labels = [label for label in labels if not wanted or label["file"] in wanted]
    settings = RunSettings.from_config(config).model_copy(update={"live": False})
    llm = OpenRouterLLM(config)
    retriever = RunbookRetriever(load_runbooks(), make_embeddings(config))
    scores = []
    for label in labels:
        parsed = parse_upload((SAMPLES / label["file"]).read_bytes(), label["file"], year=2026)
        context = RunContext(parsed=parsed, settings=settings, llm=llm, retriever=retriever)
        started = time.perf_counter()
        result = start_run(build_graph(), context, new_run_id())
        scores.append(score(label, result, time.perf_counter() - started))
        last = scores[-1]
        print(f"{label['file']}: {last.found}/{last.expected} found, ${last.cost_usd:.4f}")
    print("\n" + markdown(scores))
    return 0


if __name__ == "__main__":
    sys.exit(main())
