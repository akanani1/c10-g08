"""Pydantic models shared by every agent: the run settings and what flows through the graph.

Every agent's input and output is one of these models. Model output is validated against them,
so a malformed answer is caught at the boundary instead of deep inside the UI.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Annotated, Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from incident_suite.config import (
    MAX_TICKETS_CEILING,
    RUN_TOKEN_BUDGET_CEILING,
    AppConfig,
    check_chat_model,
    check_embedding_model,
)

MAX_PROMPT_CHARS = 20_000


class StrictModel(BaseModel):
    """Base for every state model: unknown fields are an error, not silently dropped."""

    model_config = ConfigDict(extra="forbid")


# ------------------------------------------------------------------ enums


class Severity(StrEnum):
    """How bad an issue is. The order matters: see :meth:`rank`."""

    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"

    @property
    def rank(self) -> int:
        """0 for low up to 3 for critical, for sorting and threshold checks."""
        return list(Severity).index(self)


class Category(StrEnum):
    """What kind of problem an issue is."""

    DATABASE = "database"
    MEMORY = "memory"
    DISK = "disk"
    TLS = "tls"
    AUTH = "auth"
    HTTP_5XX = "http_5xx"
    TIMEOUT = "timeout"
    CRASH_LOOP = "crash_loop"
    CONFIG = "config"
    RATE_LIMIT = "rate_limit"
    DNS = "dns"
    QUEUE = "queue"
    OTHER = "other"


class LogLevel(StrEnum):
    """Normalised log level. Readers map WARNING to WARN, CRIT and PANIC to FATAL, and so on."""

    TRACE = "TRACE"
    DEBUG = "DEBUG"
    INFO = "INFO"
    WARN = "WARN"
    ERROR = "ERROR"
    FATAL = "FATAL"
    UNKNOWN = "UNKNOWN"


class LogFormat(StrEnum):
    """Formats the readers understand (build plan section 23). Gzip is a wrapper, not a format."""

    PLAIN = "plain"
    NGINX_ACCESS = "nginx_access"
    NGINX_ERROR = "nginx_error"
    SYSLOG = "syslog"
    KUBERNETES = "kubernetes"
    JSON_LINES = "json_lines"
    JSON_DOCUMENT = "json_document"
    CSV = "csv"
    TSV = "tsv"
    PARQUET = "parquet"


class RedactionType(StrEnum):
    """Kinds of secret or personal data masked before the model sees a log (section 18.2)."""

    EMAIL = "email"
    # Labels, not secrets:
    BEARER_TOKEN = "bearer_token"  # noqa: S105  # nosec B105
    JWT = "jwt"
    AWS_ACCESS_KEY = "aws_access_key"
    PRIVATE_KEY = "private_key"
    CREDENTIAL_PAIR = "credential_pair"


class TicketAction(StrEnum):
    """What the JIRA agent decided for one critical issue (section 4.2)."""

    CREATE = "create"
    COMMENT = "comment"
    REGRESSION = "regression"
    SKIPPED_OVER_CAP = "skipped_over_cap"


# ------------------------------------------------------------------ run settings

Confidence = Annotated[float, Field(ge=0.0, le=1.0)]
PromptOverride = Annotated[str | None, Field(default=None, max_length=MAX_PROMPT_CHARS)]


class AgentSettings(StrictModel):
    """Model and optional prompt override for one LLM agent. ``None`` means the default prompt."""

    model: str
    system_prompt: PromptOverride = None

    @field_validator("model")
    @classmethod
    def _model_allowed(cls, value: str) -> str:
        return check_chat_model(value)


class LogReaderSettings(AgentSettings):
    """Log Reader / Classifier options."""

    context_lines: int = Field(default=3, ge=0, le=20)


class RemediationSettings(AgentSettings):
    """Remediation options. Below ``confidence_threshold`` an issue goes back for more context."""

    top_k: int = Field(default=3, ge=1, le=10)
    confidence_threshold: Confidence = 0.5


class JiraSettings(StrictModel):
    """JIRA Ticket agent options."""

    max_tickets: int = Field(default=MAX_TICKETS_CEILING, ge=0, le=MAX_TICKETS_CEILING)
    min_severity: Severity = Severity.CRITICAL


class RunSettings(StrictModel):
    """Everything a single run can be tuned with. Travels in the graph state.

    Agents read their options from here, never from config directly, so the Agent settings page
    (section 20) only has to produce a valid ``RunSettings``. Bounds live on the fields, so an
    imported JSON file can't get around them.
    """

    log_reader: LogReaderSettings
    remediation: RemediationSettings
    cookbook: AgentSettings
    jira: JiraSettings = Field(default_factory=JiraSettings)
    embedding_model: str
    token_budget: int = Field(ge=1_000, le=RUN_TOKEN_BUDGET_CEILING)
    require_approval: bool = True
    live: bool = False
    redaction: frozenset[RedactionType] = frozenset(RedactionType)

    @field_validator("embedding_model")
    @classmethod
    def _embedding_allowed(cls, value: str) -> str:
        return check_embedding_model(value)

    @classmethod
    def from_config(cls, config: AppConfig) -> Self:
        """Defaults for a new session, taken from the deployment config."""
        return cls(
            log_reader=LogReaderSettings(model=config.classifier_model),
            remediation=RemediationSettings(model=config.remediation_model),
            cookbook=AgentSettings(model=config.classifier_model),
            jira=JiraSettings(max_tickets=config.max_tickets_per_run),
            embedding_model=config.embedding_model,
            token_budget=config.run_token_budget,
            require_approval=config.require_approval,
            live=config.live_integrations,
        )

    @property
    def prompts_edited(self) -> bool:
        """True if any agent runs with a prompt other than its default file."""
        return any(
            agent.system_prompt is not None
            for agent in (self.log_reader, self.remediation, self.cookbook)
        )


# ------------------------------------------------------------------ parsed log


class LogLine(StrictModel):
    """One parsed line (text formats) or row (structured formats). Text is already redacted."""

    line_no: int = Field(ge=1, description="1-based line number, or row number for tables")
    raw: str
    message: str
    timestamp: datetime | None = None
    level: LogLevel = LogLevel.UNKNOWN
    service: str | None = None
    host: str | None = None
    error_code: str | None = None
    trace_id: str | None = None
    extra: dict[str, str] = Field(default_factory=dict)


class Pattern(StrictModel):
    """Lines grouped by masked signature, e.g. ``Connection refused to db:<port>`` x 412."""

    signature: str
    signature_hash: str = Field(pattern=r"^[0-9a-f]{8,16}$")
    level: LogLevel
    count: int = Field(ge=1)
    first_seen: datetime | None = None
    last_seen: datetime | None = None
    example_line_nos: list[int] = Field(min_length=1, max_length=10)
    service: str | None = None
    error_code: str | None = None

    @model_validator(mode="after")
    def _time_order(self) -> Self:
        if self.first_seen and self.last_seen and self.first_seen > self.last_seen:
            msg = "first_seen is after last_seen"
            raise ValueError(msg)
        return self


class ParsedLog(StrictModel):
    """An uploaded log after reading, redaction and parsing. Every text field is redacted."""

    name: str
    fingerprint: str = Field(pattern=r"^[0-9a-f]{16}$")
    format: LogFormat
    compressed: bool = False
    note: str = Field(description='What the app detected, e.g. "Detected CSV, 3 columns"')
    column_mapping: dict[str, str] = Field(default_factory=dict)
    lines: list[LogLine]
    redactions: dict[RedactionType, int] = Field(default_factory=dict)
    warnings: list[str] = Field(default_factory=list)


class GroupedLog(StrictModel):
    """What the pre-filter keeps for the model: WARN+ patterns plus the lines around them."""

    patterns: list[Pattern]
    patterns_dropped: int = Field(default=0, ge=0, description="WARN+ patterns over the cap")
    context_lines: list[LogLine] = Field(default_factory=list)
    change_lines: list[LogLine] = Field(
        default_factory=list, description="Deploys, config changes and restarts"
    )
    quiet_line_count: int = Field(default=0, ge=0, description="Lines below WARN")


# ------------------------------------------------------------------ agent outputs


class Issue(StrictModel):
    """A classified problem. Produced by the Log Reader / Classifier."""

    id: str = Field(pattern=r"^ISS-\d{1,4}$")
    title: str = Field(min_length=3, max_length=200)
    category: Category
    severity: Severity
    confidence: Confidence
    evidence_lines: list[int] = Field(min_length=1)
    affected_service: str | None = None
    signature_hash: str | None = None


class RunbookSource(StrictModel):
    """A runbook the fix was based on, with its match score."""

    runbook: str
    score: Confidence


class Remediation(StrictModel):
    """The fix for one issue. Produced by the Remediation agent."""

    issue_id: str = Field(pattern=r"^ISS-\d{1,4}$")
    root_cause: str = Field(description="One or two sentences, a hypothesis citing evidence lines")
    steps: list[str] = Field(min_length=1)
    rationale: str
    sources: list[RunbookSource] = Field(default_factory=list)
    confidence: Confidence


class ChecklistItem(StrictModel):
    """One actionable line, tied to the issues it addresses."""

    text: str = Field(min_length=3)
    issue_ids: list[str] = Field(default_factory=list)


class Checklist(StrictModel):
    """The Cookbook Synthesizer's output."""

    do_now: list[ChecklistItem] = Field(default_factory=list)
    this_week: list[ChecklistItem] = Field(default_factory=list)
    prevent: list[ChecklistItem] = Field(default_factory=list)


class TicketOutcome(StrictModel):
    """What happened (or, in dry run, would happen) in JIRA for one issue."""

    issue_id: str
    action: TicketAction
    key: str | None = Field(default=None, pattern=r"^[A-Z][A-Z0-9]+-\d+$")
    url: str | None = None
    related_key: str | None = None
    sent: bool = False
    error: str | None = None


class SlackOutcome(StrictModel):
    """What happened (or would happen) in Slack for this run."""

    sent: bool = False
    permalink: str | None = None
    error: str | None = None


class AgentUsage(StrictModel):
    """Tokens, cost and time spent by one agent in one run."""

    agent: str
    model: str | None = None
    calls: int = Field(default=0, ge=0)
    input_tokens: int = Field(default=0, ge=0)
    output_tokens: int = Field(default=0, ge=0)
    cost_usd: float = Field(default=0.0, ge=0)
    seconds: float = Field(default=0.0, ge=0)


class RunResult(StrictModel):
    """Everything a finished run produced. Shown in the app and summarised in Slack and JIRA."""

    run_id: str
    log_name: str
    log_fingerprint: str
    log_format: LogFormat
    line_count: int = Field(ge=0)
    redactions: dict[RedactionType, int] = Field(default_factory=dict)
    patterns: list[Pattern] = Field(default_factory=list)
    issues: list[Issue] = Field(default_factory=list)
    remediations: list[Remediation] = Field(default_factory=list)
    checklist: Checklist = Field(default_factory=Checklist)
    tickets: list[TicketOutcome] = Field(default_factory=list)
    slack: SlackOutcome = Field(default_factory=SlackOutcome)
    usage: list[AgentUsage] = Field(default_factory=list)
    trace_url: str | None = None
    dry_run: bool = True
    errors: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _remediations_match_issues(self) -> Self:
        known = {issue.id for issue in self.issues}
        unknown = sorted({fix.issue_id for fix in self.remediations} - known)
        if unknown:
            msg = f"remediations refer to unknown issues: {', '.join(unknown)}"
            raise ValueError(msg)
        return self

    @property
    def top_severity(self) -> Severity | None:
        """The worst severity found, or ``None`` when there are no issues."""
        return max((issue.severity for issue in self.issues), key=lambda s: s.rank, default=None)

    @property
    def total_cost_usd(self) -> float:
        """Cost of the whole run across agents."""
        return sum(item.cost_usd for item in self.usage)
