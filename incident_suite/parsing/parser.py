"""Turn records into ``LogLine`` objects.

Text logs: the format is detected from a sample of lines (app logs, nginx access and error,
syslog, Kubernetes), and each line is parsed with that format first, then any other known
format, then as plain text. Structured records (JSON, CSV, TSV, Parquet) have their columns
mapped onto ``LogLine`` fields by common names.

A line that matches nothing is still kept, with the whole text as the message, so nothing
is silently dropped.
"""

from __future__ import annotations

import re
from collections import Counter
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime

from incident_suite.state import LogFormat, LogLevel, LogLine

# ------------------------------------------------------------------ levels

_LEVELS: dict[str, LogLevel] = {
    "trace": LogLevel.TRACE,
    "finest": LogLevel.TRACE,
    "debug": LogLevel.DEBUG,
    "dbg": LogLevel.DEBUG,
    "fine": LogLevel.DEBUG,
    "info": LogLevel.INFO,
    "information": LogLevel.INFO,
    "informational": LogLevel.INFO,
    "notice": LogLevel.INFO,
    "warn": LogLevel.WARN,
    "warning": LogLevel.WARN,
    "error": LogLevel.ERROR,
    "err": LogLevel.ERROR,
    "severe": LogLevel.ERROR,
    "fatal": LogLevel.FATAL,
    "critical": LogLevel.FATAL,
    "crit": LogLevel.FATAL,
    "panic": LogLevel.FATAL,
    "emerg": LogLevel.FATAL,
    "emergency": LogLevel.FATAL,
    "alert": LogLevel.FATAL,
}
_KLOG_LEVELS = {"I": LogLevel.INFO, "W": LogLevel.WARN, "E": LogLevel.ERROR, "F": LogLevel.FATAL}
# Bunyan / pino numeric levels
_NUMERIC_LEVELS = {
    10: LogLevel.TRACE,
    20: LogLevel.DEBUG,
    30: LogLevel.INFO,
    40: LogLevel.WARN,
    50: LogLevel.ERROR,
    60: LogLevel.FATAL,
}

_INFER_FATAL = re.compile(r"(?i)\b(fatal|panic(ked)?)\b")
_INFER_ERROR = re.compile(
    r"(?i)\b(error|exception|fail(s|ed|ure)?|refused|denied|unreachable|timed? ?out|killed"
    r"|oom\w*|crash\w*|nxdomain|cannot|can't|unable|no space left|expired|traceback)\b"
)
_INFER_WARN = re.compile(
    r"(?i)\b(warn(ing)?|retry(ing)?|deprecated|slow|back-?off|throttl\w*|too many requests)\b"
)


def normalize_level(value: str | None) -> LogLevel | None:
    """Map a level word or number (``WARNING``, ``err``, ``50``) to a :class:`LogLevel`."""
    if value is None:
        return None
    text = value.strip().strip("[]").lower()
    if text.isdigit():
        return _NUMERIC_LEVELS.get(int(text))
    return _LEVELS.get(text)


def infer_level(message: str) -> LogLevel:
    """Guess a level from the words in a message, for lines that don't state one."""
    if _INFER_FATAL.search(message):
        return LogLevel.FATAL
    if _INFER_ERROR.search(message):
        return LogLevel.ERROR
    if _INFER_WARN.search(message):
        return LogLevel.WARN
    return LogLevel.UNKNOWN


def level_from_status(code: str | None) -> LogLevel | None:
    """HTTP status to level: 5xx is ERROR, 4xx is WARN, the rest INFO."""
    if code is None or not re.fullmatch(r"[1-5]\d\d", code):
        return None
    if code.startswith("5"):
        return LogLevel.ERROR
    if code.startswith("4"):
        return LogLevel.WARN
    return LogLevel.INFO


# ------------------------------------------------------------------ timestamps

_MONTHS = {
    name: number
    for number, name in enumerate(
        ("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"),
        start=1,
    )
}
_SYSLOG_TS = re.compile(r"^(?P<mon>[A-Za-z]{3}) +(?P<day>\d{1,2}) (?P<hms>\d{2}:\d{2}:\d{2})$")
_EPOCH_SECONDS_MAX = 1e11
_EPOCH_MILLIS_MAX = 1e14
_EPOCH_MICROS_MAX = 1e17


def _utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def _from_epoch(number: float) -> datetime | None:
    if number <= 0:
        return None
    if number < _EPOCH_SECONDS_MAX:
        seconds = number
    elif number < _EPOCH_MILLIS_MAX:
        seconds = number / 1e3
    elif number < _EPOCH_MICROS_MAX:
        seconds = number / 1e6
    else:
        seconds = number / 1e9
    try:
        return datetime.fromtimestamp(seconds, tz=UTC)
    except (OverflowError, OSError, ValueError):
        return None


def parse_timestamp(text: str | None, default_year: int) -> datetime | None:
    """Parse the timestamp styles we see in logs into an aware UTC datetime, or ``None``.

    ``default_year`` is used for syslog stamps, which don't carry a year.
    """
    if not text:
        return None
    value = text.strip()
    try:
        return _from_epoch(float(value))
    except ValueError:
        pass
    try:
        return _utc(datetime.fromisoformat(value.replace(",", ".")))
    except ValueError:
        pass
    for fmt in ("%d/%b/%Y:%H:%M:%S %z", "%Y/%m/%d %H:%M:%S"):
        try:
            return _utc(datetime.strptime(value, fmt))
        except ValueError:
            continue
    match = _SYSLOG_TS.match(value)
    if match and match["mon"].lower() in _MONTHS:
        hour, minute, second = (int(part) for part in match["hms"].split(":"))
        try:
            return datetime(
                default_year,
                _MONTHS[match["mon"].lower()],
                int(match["day"]),
                hour,
                minute,
                second,
                tzinfo=UTC,
            )
        except ValueError:
            return None
    return None


# ------------------------------------------------------------------ text formats

_ISO = r"\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(?:[.,]\d+)?(?:Z|[+-]\d{2}:?\d{2})?"
_APP = re.compile(rf"^\[?(?P<ts>{_ISO})\]?\s+\[?(?P<level>[A-Za-z]{{3,11}})\]?:?\s+(?P<rest>.*)$")
_NGINX_ACCESS = re.compile(
    r'^(?P<client>\S+) \S+ (?P<user>\S+) \[(?P<ts>[^\]]+)\] "(?P<method>[A-Z]+) (?P<path>\S+)'
    r'(?: HTTP/[\d.]+)?" (?P<status>\d{3}) (?P<bytes>\d+|-)'
    r'(?: "(?P<referer>[^"]*)" "(?P<agent>[^"]*)")?(?P<tail>.*)$'
)
_NGINX_ERROR = re.compile(
    r"^(?P<ts>\d{4}/\d{2}/\d{2} \d{2}:\d{2}:\d{2}) \[(?P<level>\w+)\] \d+#\d+: (?:\*\d+ )?"
    r"(?P<msg>.*)$"
)
_SYSLOG = re.compile(
    rf"^(?:<\d{{1,3}}>(?:1 )?)?(?P<ts>[A-Z][a-z]{{2}} +\d{{1,2}} \d{{2}}:\d{{2}}:\d{{2}}|{_ISO}) "
    r"(?P<host>[\w.-]+) (?P<proc>[\w./@-]+?)(?:\[(?P<pid>\d+)\])?: (?P<msg>.*)$"
)
# Container runtime (CRI) lines, as written by Kubernetes nodes: "<ts> stdout F <message>"
_CRI = re.compile(rf"^(?P<ts>{_ISO}) (?P<stream>stdout|stderr) [FP] (?P<msg>.*)$")
_KLOG = re.compile(
    r"^(?P<sev>[IWEF])\d{4} \d{2}:\d{2}:\d{2}\.\d+\s+(?:\d+ )?(?:[\w.-]+:\d+\] )?(?P<msg>.*)$"
)
_KERNEL_TS = re.compile(r"^\[\s*\d+\.\d+\]\s*")
_LEADING_LEVEL = re.compile(
    r"^\[?(?P<level>TRACE|DEBUG|INFO|NOTICE|WARN|WARNING|ERROR|ERR|FATAL|CRITICAL|CRIT|PANIC)\]?"
    r":?\s+(?P<msg>.*)$"
)
_SERVICE_PREFIX = re.compile(r"^(?P<svc>[a-z0-9][a-z0-9_.\-/]{0,63})(?=\s+\[|\s+-\s|:\s)\s*:?\s*")
_BRACKET = re.compile(r"^\[(?P<inner>[^\]]{0,200})\]\s*")
_TRACE_KEY = re.compile(
    r"(?i)^(?:trace|trace_?id|request_?id|req_?id|rid|correlation_?id|span_?id)[=:](?P<id>\S+)$"
)
_KV = re.compile(r'\b(?P<key>[a-z_]+)=(?:"(?P<quoted>[^"]*)"|(?P<bare>[^\s,;]+))')
_ERROR_CODE_KEYS = {"error_code", "err_code", "code", "status", "status_code"}
_TRACE_KEYS = {"trace_id", "traceid", "request_id", "req_id", "correlation_id", "trace"}
_EXTRA_KEYS = {"pod", "container", "namespace", "node"}
_K8S_PROCS = re.compile(r"^(kubelet|kube-[\w-]+|containerd|dockerd|crio|etcd)$")
_K8S_WORDS = re.compile(r"pod=|CrashLoopBackOff|OOMKilled|Back-off restarting|kubelet")


@dataclass(frozen=True)
class _Parts:
    """What a format-specific parser pulled out of one line."""

    message: str
    timestamp: str | None = None
    level: LogLevel | None = None
    service: str | None = None
    host: str | None = None
    error_code: str | None = None
    trace_id: str | None = None
    extra: tuple[tuple[str, str], ...] = ()


def _clean_id(value: str | None) -> str | None:
    return None if value in (None, "", "-") else value


def _parse_app(line: str) -> _Parts | None:
    match = _APP.match(line)
    if not match or normalize_level(match["level"]) is None:
        return None
    rest = match["rest"]
    service = trace = None
    extra: list[tuple[str, str]] = []
    svc = _SERVICE_PREFIX.match(rest)
    if svc:
        service, rest = svc["svc"], rest[svc.end() :]
    bracket = _BRACKET.match(rest)
    if bracket:
        rest = rest[bracket.end() :]
        trace_match = _TRACE_KEY.match(bracket["inner"].strip())
        if trace_match:
            trace = _clean_id(trace_match["id"])
        else:
            extra.append(("thread", bracket["inner"]))
    return _Parts(
        message=rest,
        timestamp=match["ts"],
        level=normalize_level(match["level"]),
        service=service,
        trace_id=trace,
        extra=tuple(extra),
    )


def _parse_nginx_access(line: str) -> _Parts | None:
    match = _NGINX_ACCESS.match(line)
    if not match:
        return None
    path = match["path"].split("?", 1)[0]
    extra = [("client_ip", match["client"]), ("bytes", match["bytes"])]
    if match["agent"]:
        extra.append(("user_agent", match["agent"]))
    request_time = re.search(r"\b(?:rt|request_time)=([\d.]+)", match["tail"] or "")
    if request_time:
        extra.append(("request_time", request_time[1]))
    return _Parts(
        message=f"{match['method']} {path} {match['status']}",
        timestamp=match["ts"],
        level=level_from_status(match["status"]),
        service="nginx",
        error_code=match["status"],
        extra=tuple(extra),
    )


def _parse_nginx_error(line: str) -> _Parts | None:
    match = _NGINX_ERROR.match(line)
    if not match:
        return None
    message, _, context = match["msg"].partition(", client: ")
    extra = (("context", "client: " + context),) if context else ()
    return _Parts(
        message=message,
        timestamp=match["ts"],
        level=normalize_level(match["level"]),
        service="nginx",
        extra=extra,
    )


def _split_leading_level(message: str) -> tuple[LogLevel | None, str]:
    klog = _KLOG.match(message)
    if klog:
        return _KLOG_LEVELS[klog["sev"]], klog["msg"]
    leading = _LEADING_LEVEL.match(message)
    if leading:
        return normalize_level(leading["level"]), leading["msg"]
    return None, message


def _parse_syslog(line: str) -> _Parts | None:
    match = _SYSLOG.match(line)
    if not match:
        return None
    message = _KERNEL_TS.sub("", match["msg"])
    level, message = _split_leading_level(message)
    return _Parts(
        message=message,
        timestamp=match["ts"],
        level=level,
        service=match["proc"],
        host=match["host"],
    )


def _parse_cri(line: str) -> _Parts | None:
    match = _CRI.match(line)
    if not match:
        return None
    inner = _parse_app(match["msg"])
    if inner:
        return inner
    level, message = _split_leading_level(match["msg"])
    if level is None and match["stream"] == "stderr":
        level = infer_level(message)
    return _Parts(message=message, timestamp=match["ts"], level=level)


_PARSERS: dict[LogFormat, Callable[[str], _Parts | None]] = {
    LogFormat.PLAIN: _parse_app,
    LogFormat.NGINX_ACCESS: _parse_nginx_access,
    LogFormat.NGINX_ERROR: _parse_nginx_error,
    LogFormat.SYSLOG: _parse_syslog,
    LogFormat.KUBERNETES: _parse_syslog,
}
_FALLBACKS = (_parse_app, _parse_syslog, _parse_nginx_access, _parse_nginx_error, _parse_cri)


def detect_text_format(lines: Sequence[str]) -> LogFormat:
    """Pick the text format most of the first 200 lines match. Falls back to plain."""
    sample = [line for line in lines[:200] if line.strip()]
    if not sample:
        return LogFormat.PLAIN
    scores = {
        LogFormat.NGINX_ACCESS: sum(1 for line in sample if _NGINX_ACCESS.match(line)),
        LogFormat.NGINX_ERROR: sum(1 for line in sample if _NGINX_ERROR.match(line)),
        LogFormat.SYSLOG: sum(1 for line in sample if _SYSLOG.match(line)),
        LogFormat.KUBERNETES: sum(1 for line in sample if _CRI.match(line)),
        LogFormat.PLAIN: sum(1 for line in sample if _parse_app(line)),
    }
    best = max(scores, key=lambda fmt: scores[fmt])
    if scores[best] * 2 < len(sample):
        return LogFormat.PLAIN
    if best is LogFormat.SYSLOG:
        procs = {m["proc"] for line in sample if (m := _SYSLOG.match(line))}
        if any(_K8S_PROCS.match(proc) for proc in procs) or any(
            _K8S_WORDS.search(line) for line in sample
        ):
            return LogFormat.KUBERNETES
    return best


def _key_values(message: str) -> dict[str, str]:
    found: dict[str, str] = {}
    for match in _KV.finditer(message):
        key = match["key"].lower()
        found.setdefault(key, match["quoted"] if match["quoted"] is not None else match["bare"])
    return found


def _build_line(line_no: int, raw: str, parts: _Parts, default_year: int) -> LogLine:
    pairs = _key_values(parts.message)
    error_code = parts.error_code or next(
        (pairs[key] for key in _ERROR_CODE_KEYS if key in pairs), None
    )
    trace_id = parts.trace_id or _clean_id(
        next((pairs[key] for key in _TRACE_KEYS if key in pairs), None)
    )
    extra = dict(parts.extra) | {key: pairs[key] for key in _EXTRA_KEYS if key in pairs}
    level = parts.level or level_from_status(error_code) or infer_level(parts.message)
    return LogLine(
        line_no=line_no,
        raw=raw,
        message=parts.message.strip() or raw,
        timestamp=parse_timestamp(parts.timestamp, default_year),
        level=level,
        service=parts.service,
        host=parts.host,
        error_code=error_code,
        trace_id=trace_id,
        extra=extra,
    )


def parse_text_line(line_no: int, text: str, fmt: LogFormat, default_year: int) -> LogLine:
    """Parse one text line, trying the detected format first. Never fails."""
    for parser in (_PARSERS[fmt], *_FALLBACKS):
        parts = parser(text)
        if parts is not None:
            return _build_line(line_no, text, parts, default_year)
    return _build_line(line_no, text, _Parts(message=text), default_year)


# ------------------------------------------------------------------ structured records

FIELD_ALIASES: dict[str, tuple[str, ...]] = {
    "timestamp": ("timestamp", "@timestamp", "time", "ts", "datetime", "date", "event_time"),
    "level": ("level", "severity", "loglevel", "log_level", "lvl", "log.level", "levelname"),
    "service": (
        "service",
        "service.name",
        "app",
        "application",
        "component",
        "container",
        "logger",
    ),
    "host": ("host", "hostname", "host.name", "pod", "node", "instance"),
    "message": ("message", "msg", "log", "text", "event", "description"),
    "error_code": (
        "error_code",
        "code",
        "status",
        "status_code",
        "http.status",
        "http.status_code",
        "response.status",
    ),
    "trace_id": (
        "trace_id",
        "traceid",
        "trace.id",
        "request_id",
        "requestid",
        "correlation_id",
        "span_id",
    ),
}


class ColumnMapper:
    """Maps record fields onto ``LogLine`` fields, and remembers which column it used."""

    def __init__(self) -> None:
        self._used: dict[str, Counter[str]] = {name: Counter() for name in FIELD_ALIASES}

    def map(self, fields: Mapping[str, str]) -> dict[str, str]:
        """Return ``{logline_field: column_name}`` for one record."""
        by_lower = {key.lower(): key for key in fields}
        chosen: dict[str, str] = {}
        for target, aliases in FIELD_ALIASES.items():
            for alias in aliases:
                column = by_lower.get(alias)
                if column is not None and column not in chosen.values() and fields[column]:
                    chosen[target] = column
                    self._used[target][column] += 1
                    break
        return chosen

    @property
    def mapping(self) -> dict[str, str]:
        """The column used most often for each field, for display ("message <- msg")."""
        return {
            target: counts.most_common(1)[0][0] for target, counts in self._used.items() if counts
        }


def parse_record(
    line_no: int,
    fields: Mapping[str, str],
    raw: str,
    mapper: ColumnMapper,
    default_year: int,
) -> LogLine:
    """Build a ``LogLine`` from a structured record whose values are already redacted."""
    chosen = mapper.map(fields)

    def get(target: str) -> str | None:
        column = chosen.get(target)
        return fields[column] if column else None

    mapped = set(chosen.values())
    rest = {key: value for key, value in fields.items() if key not in mapped and value != ""}
    message = get("message") or " ".join(f"{key}={value}" for key, value in rest.items()) or raw
    error_code = get("error_code")
    level = normalize_level(get("level")) or level_from_status(error_code) or infer_level(message)
    return LogLine(
        line_no=line_no,
        raw=raw,
        message=message,
        timestamp=parse_timestamp(get("timestamp"), default_year),
        level=level,
        service=get("service"),
        host=get("host"),
        error_code=error_code,
        trace_id=_clean_id(get("trace_id")),
        extra=rest,
    )
