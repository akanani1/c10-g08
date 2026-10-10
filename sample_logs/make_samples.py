"""Generate the 17 sample logs and labels.json (build plan sections 7 and 23.6).

Run from anywhere:  python sample_logs/make_samples.py

The output is deterministic (fixed seeds, fixed timestamps, gzip without a timestamp), so the
line numbers in labels.json always match the files, and a test checks the committed files are
what this script writes. Everything here is made up: hosts, users and IPs are documentation
values, and the one password is a deliberately fake one that shows redaction working.
"""

from __future__ import annotations

import csv
import gzip
import io
import json
import random
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pyarrow as pa
import pyarrow.parquet as pq

OUT = Path(__file__).resolve().parent
SEVERITIES = ("low", "medium", "high", "critical")
MAX_GENERATED_EVIDENCE = 10
TEXT_FORMATS = {"plain", "syslog", "kubernetes", "nginx_access", "nginx_error"}


# ------------------------------------------------------------------ building blocks


@dataclass
class Issue:
    """One expected issue and the lines that show it."""

    category: str
    severity: str
    service: str
    hint: str
    fix_keywords: list[str]
    lines: list[int] = field(default_factory=list)


@dataclass
class Log:
    """A sample log being built: its entries (text lines or records) and its labels."""

    file: str
    format: str
    scenario: str
    compressed: bool = False
    generated: bool = False
    same_signature_as: str | None = None
    entries: list[Any] = field(default_factory=list)
    issues: dict[str, Issue] = field(default_factory=dict)
    context: list[int] = field(default_factory=list)
    noise: list[int] = field(default_factory=list)

    def expect(self, key: str, issue: Issue) -> None:
        """Declare an issue this log should produce."""
        self.issues[key] = issue

    def add(
        self, entry: Any, issue: str | None = None, *, context: bool = False, noise: bool = False
    ) -> None:
        """Append a line or record, and note which label it belongs to."""
        self.entries.append(entry)
        number = len(self.entries)
        if issue is not None:
            self.issues[issue].lines.append(number)
        if context:
            self.context.append(number)
        if noise:
            self.noise.append(number)

    def label(self) -> dict[str, Any]:
        """This log's entry in labels.json."""
        severities = [issue.severity for issue in self.issues.values()]
        top = max(severities, key=SEVERITIES.index) if severities else "none"
        label: dict[str, Any] = {
            "file": self.file,
            "format": self.format,
            "compressed": self.compressed,
            "generated": self.generated,
            "scenario": self.scenario,
            "expected_top_severity": top,
            "expect_ticket": "critical" in severities,
            "expect_slack": any(SEVERITIES.index(s) >= 1 for s in severities),
            "expected_issues": [
                {
                    "category": issue.category,
                    "severity": issue.severity,
                    "evidence_lines": _evidence(issue.lines, self.generated),
                    "service": issue.service,
                    "signature_hint": issue.hint,
                    "expected_fix_keywords": issue.fix_keywords,
                }
                for issue in self.issues.values()
            ],
            "context_lines": self.context,
            "noise_lines": self.noise,
        }
        if self.same_signature_as:
            label["same_signature_as"] = self.same_signature_as
        return label


def _evidence(lines: list[int], generated: bool) -> list[int]:
    """Every evidence line, or for big generated logs the first and last five."""
    if not generated or len(lines) <= MAX_GENERATED_EVIDENCE:
        return lines
    half = MAX_GENERATED_EVIDENCE // 2
    return lines[:half] + lines[-half:]


class Clock:
    """Moves forward by a random number of seconds each tick."""

    def __init__(self, start: datetime, rng: random.Random) -> None:
        self.now = start
        self.rng = rng

    def tick(self, low: float = 0.2, high: float = 3.0) -> datetime:
        self.now += timedelta(seconds=self.rng.uniform(low, high))
        return self.now


def _start(day: int, hour: int, minute: int = 0) -> datetime:
    return datetime(2026, 10, day, hour, minute, tzinfo=UTC)


def iso(ts: datetime) -> str:
    """``2026-10-09T14:01:14.003Z``"""
    return ts.strftime("%Y-%m-%dT%H:%M:%S.") + f"{ts.microsecond // 1000:03d}Z"


def app(ts: datetime, level: str, service: str, message: str, trace: str = "-") -> str:
    """Plain app log line: ``<time> <LEVEL> <service> [trace=<id>] <message>``."""
    return f"{iso(ts)} {level:<5} {service} [trace={trace}] {message}"


def syslog(ts: datetime, host: str, proc: str, pid: int, message: str) -> str:
    """Classic syslog line, as Kubernetes nodes and VMs write it."""
    return f"{ts:%b} {ts.day:>2} {ts:%H:%M:%S} {host} {proc}[{pid}]: {message}"


def klog(ts: datetime, severity: str, message: str) -> str:
    """Kubelet (klog) message body, e.g. ``E1009 14:12:09.551002 1123 kubelet.go:2412] ...``."""
    return f"{severity}{ts:%m%d %H:%M:%S}.{ts.microsecond:06d} 1123 kubelet.go:2412] {message}"


def nginx_access(ts: datetime, rng: random.Random, method: str, path: str, status: int) -> str:
    """Nginx combined log line with the request time appended."""
    client = f"10.20.{rng.randint(1, 4)}.{rng.randint(2, 250)}"
    agent = rng.choice(["Mozilla/5.0", "okhttp/4.12", "Mozilla/5.0 (iPhone)"])
    size = rng.randint(150, 6000) if status < 400 else rng.choice([153, 157, 167, 197])
    rt = 30.001 if status == 504 else rng.uniform(0.001, 0.4)
    return (
        f'{client} - - [{ts:%d/%b/%Y:%H:%M:%S} +0000] "{method} {path} HTTP/1.1" {status} '
        f'{size} "-" "{agent}" rt={rt:.3f}'
    )


def nginx_error(ts: datetime, level: str, message: str, request: str | None = None) -> str:
    """Nginx error log line."""
    line = f"{ts:%Y/%m/%d %H:%M:%S} [{level}] 31#31: "
    if request is None:
        return line + message
    return (
        line
        + f"*{ts.second * 97 % 9000} {message}, client: 10.30.{ts.minute % 9}."
        + (f'{ts.second + 10}, server: edge, request: "{request}"')
    )


def trace_id(rng: random.Random) -> str:
    return f"{rng.getrandbits(32):08x}"


# ------------------------------------------------------------------ the 17 scenarios


def payment_db_conn_refused() -> Log:
    """#1: a deploy shrinks the DB pool; payment-api can't reach Postgres."""
    rng = random.Random(1)
    clock = Clock(_start(9, 13, 55), rng)
    log = Log(
        "payment_db_conn_refused.log",
        "plain",
        "A deploy shrinks the DB pool to 5; payment-api gets Connection refused and 503s.",
    )
    log.expect(
        "db",
        Issue(
            "database",
            "critical",
            "payment-api",
            "Connection refused to payments-db",
            ["pool", "max_size", "roll back"],
        ),
    )
    _payment_traffic(log, clock, rng, 40)
    log.add(
        app(
            clock.tick(),
            "INFO",
            "deployer",
            "Deploy started: payment-api v2.14.0 -> v2.15.0 (by ci-bot)",
        ),
        context=True,
    )
    _payment_traffic(log, clock, rng, 6)
    log.add(
        app(
            clock.tick(20, 30),
            "INFO",
            "deployer",
            "Deploy finished: payment-api v2.15.0, 3/3 pods ready",
        ),
        context=True,
    )
    log.add(
        app(
            clock.tick(),
            "INFO",
            "payment-api",
            "Config loaded: db.pool.max_size=5 db.host=payments-db.internal:5432",
        ),
        context=True,
    )
    log.add(
        app(
            clock.tick(),
            "WARN",
            "payment-api",
            "Deprecated config key db.timeout_ms used; use db.timeout",
        ),
        noise=True,
    )
    _payment_traffic(log, clock, rng, 15)
    _payment_outage(log, clock, rng, "db", rounds=18)
    log.add(
        app(
            clock.tick(),
            "FATAL",
            "payment-api",
            "Health check failed 3 times in a row: database unreachable",
        ),
        "db",
    )
    return log


def _payment_traffic(log: Log, clock: Clock, rng: random.Random, count: int) -> None:
    for _ in range(count):
        message = rng.choice(
            [
                f"GET /health 200 {rng.randint(1, 4)}ms",
                f"POST /v1/charges 201 {rng.randint(40, 140)}ms",
                f"GET /v1/charges/ch_{rng.randint(1000, 9999)} 200 {rng.randint(8, 30)}ms",
                f"POST /v1/refunds 201 {rng.randint(50, 160)}ms",
            ]
        )
        log.add(app(clock.tick(), "INFO", "payment-api", message, trace_id(rng)))


def _payment_outage(log: Log, clock: Clock, rng: random.Random, key: str, rounds: int) -> None:
    for round_no in range(rounds):
        waiting = 8 + round_no * 3
        log.add(
            app(
                clock.tick(),
                "WARN",
                "payment-api",
                f"DB pool: 5/5 connections in use, {waiting} waiting",
                trace_id(rng),
            ),
            key,
        )
        trace = trace_id(rng)
        for attempt in (1, 2, 3):
            log.add(
                app(
                    clock.tick(0.3, 0.6),
                    "ERROR",
                    "payment-api",
                    f"Connection refused to payments-db.internal:5432 (attempt {attempt}/3)",
                    trace,
                ),
                key,
            )
        log.add(
            app(
                clock.tick(0.01, 0.02),
                "ERROR",
                "payment-api",
                f"POST /v1/charges 503 {rng.randint(1400, 1700)}ms error_code=DB_UNAVAILABLE",
                trace,
            ),
            key,
        )
        if round_no % 3 == 0:
            log.add(app(clock.tick(), "INFO", "payment-api", "GET /health 200 2ms", trace_id(rng)))


def orders_oom_killed() -> Log:
    """#2: the orders container grows past its memory limit and is OOMKilled, three times."""
    rng = random.Random(2)
    clock = Clock(_start(9, 14, 5), rng)
    log = Log(
        "orders_oom_killed.log",
        "kubernetes",
        "orders grows past its 768Mi limit after cache eviction was turned off; OOMKilled 3 times.",
    )
    log.expect(
        "oom",
        Issue("memory", "critical", "orders", "OOMKilled", ["memory limit", "cache", "eviction"]),
    )
    pod = 'pod="shop/orders-6d9f7c-xk2lp" container="orders"'
    log.add(
        syslog(
            clock.tick(),
            "node-7",
            "argocd-server",
            880,
            "INFO Rollout finished: orders v3.8.0 (cache.eviction=false)",
        ),
        context=True,
    )
    pid = 4410
    for cycle in range(3):
        log.add(
            syslog(
                clock.tick(),
                "node-7",
                "orders",
                pid,
                "INFO Started OrdersApplication in 19.8 seconds",
            )
        )
        for step, memory in enumerate(range(420, 770, 25)):
            for _ in range(3):
                log.add(
                    syslog(
                        clock.tick(2, 6),
                        "node-7",
                        "orders",
                        pid,
                        f"INFO Processed order {rng.randint(10000, 99999)} in "
                        f"{rng.randint(12, 60)}ms",
                    )
                )
            severity = "W" if memory > 700 else "I"
            ts = clock.tick(5, 10)
            log.add(
                syslog(
                    ts,
                    "node-7",
                    "kubelet",
                    1123,
                    klog(ts, severity, f"{pod} memory_working_set={memory}Mi limit=768Mi"),
                ),
                "oom" if severity == "W" else None,
            )
            if step == 6:
                log.add(
                    syslog(
                        clock.tick(),
                        "node-7",
                        "orders",
                        pid,
                        f"WARN cache: product cache size {1.2 + cycle * 0.4:.1f}M entries, "
                        "eviction disabled",
                    ),
                    "oom",
                )
        log.add(
            syslog(
                clock.tick(1, 2),
                "node-7",
                "kernel",
                0,
                f"Memory cgroup out of memory: Killed process {pid} (java) "
                f"total-vm:2457600kB, anon-rss:{786000 + cycle * 211}kB",
            ),
            "oom",
        )
        ts = clock.tick(0.1, 0.5)
        log.add(
            syslog(
                ts,
                "node-7",
                "kubelet",
                1123,
                klog(ts, "E", f"{pod} state=Terminated reason=OOMKilled exitCode=137"),
            ),
            "oom",
        )
        ts = clock.tick(0.5, 1)
        log.add(
            syslog(
                ts,
                "node-7",
                "kubelet",
                1123,
                klog(ts, "I", f"{pod} restartCount={cycle + 1} Back-off restarting"),
            )
        )
        pid += 700 + cycle
    return log


def api_disk_full() -> Log:
    """#3: log cleanup was switched off; /var/data fills up and writes fail."""
    rng = random.Random(3)
    clock = Clock(_start(9, 2, 0), rng)
    log = Log(
        "api_disk_full.txt",
        "syslog",
        "Log cleanup is disabled by a config change; /var/data fills and every write fails.",
    )
    log.expect(
        "disk",
        Issue(
            "disk", "critical", "api", "No space left on device", ["disk", "clean", "log rotation"]
        ),
    )
    log.add(
        syslog(
            clock.tick(),
            "api-2",
            "cron",
            612,
            "INFO Log cleanup job disabled (config changed by ops)",
        ),
        context=True,
    )
    for percent in range(84, 101, 2):
        for _ in range(6):
            log.add(
                syslog(
                    clock.tick(20, 60),
                    "api-2",
                    "api",
                    2211,
                    f"INFO Wrote batch {rng.randint(1000, 9999)} ({rng.randint(100, 900)} rows)",
                )
            )
        if percent >= 90:
            log.add(
                syslog(
                    clock.tick(),
                    "api-2",
                    "api",
                    2211,
                    f"WARN Disk usage on /var/data at {percent}%",
                ),
                "disk",
            )
    for _ in range(25):
        source = rng.random()
        if source < 0.6:
            line = syslog(
                clock.tick(),
                "api-2",
                "api",
                2211,
                f"ERROR Failed to write /var/data/uploads/tmp-{rng.randint(1000, 9999)}"
                ".part: No space left on device",
            )
        elif source < 0.85:
            line = syslog(
                clock.tick(),
                "api-2",
                "postgres",
                1201,
                f'ERROR could not extend file "base/16384/{rng.randint(2000, 2999)}": '
                "No space left on device",
            )
        else:
            line = syslog(
                clock.tick(),
                "api-2",
                "logrotate",
                3300,
                "ERROR error writing /var/log/api/app.log.1: No space left on device",
            )
        log.add(line, "disk")
        if rng.random() < 0.3:
            log.add(
                syslog(
                    clock.tick(),
                    "api-2",
                    "sshd",
                    901,
                    "INFO Accepted publickey for deploy from 10.0.4.12 port 50122",
                )
            )
    return log


def gateway_tls_expired() -> Log:
    """#4: ACME renewal fails, the certificate expires at midnight, handshakes fail."""
    rng = random.Random(4)
    clock = Clock(_start(9, 23, 0), rng)
    log = Log(
        "gateway_tls_expired.jsonl",
        "json_lines",
        "ACME renewal keeps timing out; the gateway certificate expires at midnight.",
    )
    log.expect(
        "tls", Issue("tls", "high", "edge-gateway", "certificate", ["renew", "certificate", "ACME"])
    )
    for minute in range(60, 0, -5):
        for _ in range(4):
            clock.tick(20, 60)
            log.add(
                {
                    "ts": iso(clock.now),
                    "level": "info",
                    "service": "edge-gateway",
                    "msg": f"GET /api/products 200 {rng.randint(5, 80)}ms",
                    "request_id": f"r-{rng.randint(1000, 9999)}",
                }
            )
        if minute <= 30:
            log.add(
                {
                    "ts": iso(clock.tick()),
                    "level": "warning",
                    "service": "edge-gateway",
                    "msg": f"Certificate for api.shop.example expires in {minute} minutes",
                    "cert_cn": "api.shop.example",
                },
                "tls",
            )
        if minute in (40, 20):
            log.add(
                {
                    "ts": iso(clock.tick()),
                    "level": "error",
                    "service": "cert-manager",
                    "msg": "Renewal failed for api.shop.example certificate: ACME challenge "
                    "timed out",
                    "code": "ACME_TIMEOUT",
                },
                "tls",
            )
    clock.now = _start(10, 0, 0)
    for _ in range(30):
        log.add(
            {
                "ts": iso(clock.tick(0.1, 2)),
                "level": "error",
                "service": "edge-gateway",
                "msg": "TLS handshake failed: certificate has expired",
                "client": f"203.0.113.{rng.randint(2, 250)}",
                "request_id": f"r-{rng.randint(1000, 9999)}",
            },
            "tls",
        )
        if rng.random() < 0.2:
            log.add(
                {
                    "ts": iso(clock.tick()),
                    "level": "info",
                    "service": "edge-gateway",
                    "msg": "upstream pool healthy",
                    "upstreams": 6,
                }
            )
    return log


def auth_bruteforce() -> Log:
    """#5: a burst of failed logins from three addresses, then account lockouts."""
    rng = random.Random(5)
    clock = Clock(_start(9, 3, 10), rng)
    log = Log(
        "auth_bruteforce.json",
        "json_document",
        "Password guessing against admin accounts from three addresses; accounts lock.",
    )
    log.expect(
        "auth",
        Issue("auth", "high", "auth-service", "Login failed", ["block", "rate limit", "lockout"]),
    )
    attackers = ["203.0.113.7", "203.0.113.45", "198.51.100.23"]
    users = ["admin", "root", "administrator", "support"]
    failures: dict[str, int] = {}
    for step in range(160):
        if step < 30 or rng.random() < 0.25:
            log.add(
                {
                    "timestamp": iso(clock.tick(2, 9)),
                    "level": "info",
                    "service": "auth-service",
                    "event": "login_succeeded",
                    "message": f"Login succeeded for user u{rng.randint(1000, 9999)}",
                    "source_ip": f"10.40.{rng.randint(1, 9)}.{rng.randint(2, 250)}",
                }
            )
            continue
        user, address = rng.choice(users), rng.choice(attackers)
        log.add(
            {
                "timestamp": iso(clock.tick(0.1, 0.8)),
                "level": "warning",
                "service": "auth-service",
                "event": "login_failed",
                "message": f"Login failed for user {user} from {address}: invalid password",
                "source_ip": address,
                "user": user,
            },
            "auth",
        )
        failures[user] = failures.get(user, 0) + 1
        if failures[user] == 5:
            log.add(
                {
                    "timestamp": iso(clock.tick(0.01, 0.05)),
                    "level": "warning",
                    "service": "auth-service",
                    "event": "account_locked",
                    "message": f"Account locked after 5 failed attempts: user {user}",
                    "user": user,
                },
                "auth",
            )
    return log


def nginx_5xx_spike() -> Log:
    """#6: checkout's upstream goes bad; 502/503/504 on POST /api/checkout only."""
    rng = random.Random(6)
    clock = Clock(_start(9, 14, 20), rng)
    log = Log(
        "nginx_5xx_spike.log",
        "nginx_access",
        "502/503/504 spike on POST /api/checkout while other endpoints stay healthy.",
    )
    log.expect(
        "5xx",
        Issue(
            "http_5xx", "high", "checkout", "POST /api/checkout", ["upstream", "health", "checkout"]
        ),
    )
    for step in range(260):
        ts = clock.tick(0.1, 1.2)
        spike = 90 <= step < 200
        roll = rng.random()
        if roll < 0.4:
            line = nginx_access(ts, rng, "GET", f"/api/products?page={rng.randint(1, 9)}", 200)
        elif roll < 0.6:
            line = nginx_access(ts, rng, "GET", "/api/cart", 200)
        elif roll < 0.97:
            status = rng.choice([502, 502, 503, 504]) if spike and rng.random() < 0.7 else 200
            log.add(
                nginx_access(ts, rng, "POST", "/api/checkout", status),
                "5xx" if status >= 500 else None,
            )
            continue
        else:
            log.add(nginx_access(ts, rng, "GET", "/favicon.ico", 404), noise=True)
            continue
        log.add(line)
    return log


def search_upstream_timeouts() -> Log:
    """#7: the web tier times out reading from search-svc."""
    rng = random.Random(7)
    clock = Clock(_start(9, 17, 0), rng)
    log = Log(
        "search_upstream_timeouts.tsv",
        "tsv",
        "The web tier times out reading from search-svc on port 9200; searches are slow.",
    )
    log.expect(
        "timeout",
        Issue(
            "timeout",
            "medium",
            "search-svc",
            "upstream timed out",
            ["timeout", "search-svc", "capacity"],
        ),
    )
    terms = ["shoes", "bags", "jackets", "hats", "socks", "belts"]
    for _ in range(150):
        host = rng.choice(["web-1", "web-2", "web-3"])
        if rng.random() < 0.25:
            log.add(
                {
                    "time": iso(clock.tick()),
                    "lvl": "WARN",
                    "host": host,
                    "msg": "upstream timed out (110: Connection timed out) while reading "
                    "response header from upstream search-svc:9200",
                    "upstream_ms": 5000,
                },
                "timeout",
            )
        else:
            log.add(
                {
                    "time": iso(clock.tick()),
                    "lvl": "INFO",
                    "host": host,
                    "msg": f"GET /search?q={rng.choice(terms)} 200",
                    "upstream_ms": rng.randint(60, 400),
                }
            )
    return log


def worker_crashloop() -> Log:
    """#8: a release points the worker at a Redis host that refuses connections."""
    rng = random.Random(8)
    clock = Clock(_start(9, 11, 0), rng)
    log = Log(
        "worker_crashloop.log",
        "kubernetes",
        "After a rollout the worker can't reach Redis, exits, and lands in CrashLoopBackOff.",
    )
    log.expect(
        "crash",
        Issue(
            "crash_loop", "critical", "worker", "CrashLoopBackOff", ["redis", "roll back", "config"]
        ),
    )
    pod = 'pod="jobs/worker-7c9d8-p2x4k" container="worker"'
    for _ in range(12):
        log.add(
            syslog(
                clock.tick(5, 20),
                "node-3",
                "kubelet",
                1123,
                klog(clock.now, "I", 'pod="jobs/api-5b7f9-qq8rt" probe=readiness result=success'),
            )
        )
    log.add(
        syslog(
            clock.tick(),
            "node-3",
            "argocd-server",
            880,
            "INFO Rollout finished: worker v1.9.0 (redis host moved to cache.internal)",
        ),
        context=True,
    )
    pid = 7001
    backoff = 10
    for restart in range(1, 9):
        log.add(syslog(clock.tick(), "node-3", "worker", pid, "INFO Starting worker v1.9.0"))
        log.add(
            syslog(
                clock.tick(0.1, 0.3),
                "node-3",
                "worker",
                pid,
                "INFO Connecting to redis://cache.internal:6379",
            )
        )
        log.add(
            syslog(
                clock.tick(5, 6),
                "node-3",
                "worker",
                pid,
                "FATAL Cannot connect to redis://cache.internal:6379: connection refused",
            ),
            "crash",
        )
        ts = clock.tick(0.2, 0.5)
        log.add(
            syslog(
                ts,
                "node-3",
                "kubelet",
                1123,
                klog(ts, "E", f"{pod} state=Terminated reason=Error exitCode=1"),
            ),
            "crash",
        )
        ts = clock.tick(0.2, 0.5)
        log.add(
            syslog(
                ts,
                "node-3",
                "kubelet",
                1123,
                klog(
                    ts,
                    "W",
                    f"{pod} restartCount={restart} Back-off {backoff}s restarting "
                    "failed container (CrashLoopBackOff)",
                ),
            ),
            "crash",
        )
        clock.tick(backoff, backoff + 1)
        backoff = min(backoff * 2, 300)
        pid += 13
    return log


def inventory_bad_config() -> Log:
    """#9: a config change breaks inventory's YAML; it can't start."""
    rng = random.Random(9)
    clock = Clock(_start(9, 8, 30), rng)
    log = Log(
        "inventory_bad_config.txt",
        "plain",
        "A config change leaves invalid YAML and a missing env var; inventory can't start.",
    )
    log.expect(
        "config",
        Issue("config", "high", "inventory", "config.yaml", ["YAML", "INVENTORY_DB_URL", "revert"]),
    )
    log.add(
        app(
            clock.tick(),
            "INFO",
            "config-sync",
            "Config reloaded from git commit 9f3c2a1 (inventory)",
        ),
        context=True,
    )
    for attempt in range(1, 7):
        log.add(app(clock.tick(), "INFO", "inventory", "Starting inventory v4.2.0"))
        log.add(
            app(
                clock.tick(0.1, 0.2),
                "INFO",
                "inventory",
                "Config loaded from /etc/inventory/config.yaml",
            ),
            context=attempt == 1,
        )
        log.add(
            app(
                clock.tick(0.01, 0.05),
                "INFO",
                "inventory",
                "Effective env: DB_USER=inventory DB_PASSWORD=changeme LOG_LEVEL=info",
            )
        )
        log.add(
            app(
                clock.tick(0.01, 0.05),
                "ERROR",
                "inventory",
                "Invalid YAML in /etc/inventory/config.yaml line 14: mapping values are not "
                "allowed here",
            ),
            "config",
        )
        log.add(
            app(
                clock.tick(0.01, 0.05),
                "ERROR",
                "inventory",
                "Missing required env var INVENTORY_DB_URL referenced in config.yaml",
            ),
            "config",
        )
        log.add(
            app(
                clock.tick(0.01, 0.05),
                "FATAL",
                "inventory",
                "Startup aborted: config.yaml is invalid",
            ),
            "config",
        )
        log.add(
            app(
                clock.tick(),
                "INFO",
                "supervisor",
                f"Restarting inventory in 10s (attempt {attempt})",
            )
        )
        for _ in range(rng.randint(3, 6)):
            log.add(
                app(
                    clock.tick(1, 3),
                    "INFO",
                    "gateway",
                    f"GET /api/products/{rng.randint(1, 500)} 200 {rng.randint(5, 40)}ms",
                    trace_id(rng),
                )
            )
    return log


def api_rate_limited() -> Log:
    """#10: the SMS provider rate-limits notify-svc; some batches are dropped."""
    rng = random.Random(10)
    clock = Clock(_start(9, 16, 0), rng)
    log = Log(
        "api_rate_limited.csv",
        "csv",
        "The SMS provider returns 429s; notify-svc retries and gives up on some batches.",
    )
    log.expect(
        "rate",
        Issue("rate_limit", "medium", "notify-svc", "429", ["backoff", "quota", "batch size"]),
    )
    for batch in range(1, 61):
        request = f"n-{batch:03d}"
        if rng.random() < 0.3:
            for delay in (1, 2, 4):
                log.add(
                    {
                        "timestamp": iso(clock.tick(delay, delay + 0.2)),
                        "level": "WARN",
                        "service": "notify-svc",
                        "message": f"SMS provider returned 429 Too Many Requests, retrying in "
                        f"{delay}s",
                        "status_code": 429,
                        "request_id": request,
                    },
                    "rate",
                )
            if rng.random() < 0.5:
                log.add(
                    {
                        "timestamp": iso(clock.tick()),
                        "level": "ERROR",
                        "service": "notify-svc",
                        "message": f"Giving up on SMS batch {batch}/60 after 3 retries",
                        "status_code": 429,
                        "request_id": request,
                    },
                    "rate",
                )
                continue
        log.add(
            {
                "timestamp": iso(clock.tick()),
                "level": "INFO",
                "service": "notify-svc",
                "message": f"Sent SMS batch {batch}/60",
                "status_code": 200,
                "request_id": request,
            }
        )
    return log


def notify_dns_failures() -> Log:
    """#11: a nameserver change breaks resolution of the mail provider."""
    rng = random.Random(11)
    clock = Clock(_start(9, 12, 0), rng)
    log = Log(
        "notify_dns_failures.log",
        "syslog",
        "A nameserver change breaks DNS; notify can't resolve the mail provider (NXDOMAIN).",
    )
    log.expect(
        "dns", Issue("dns", "high", "notify", "NXDOMAIN", ["nameserver", "resolv", "revert"])
    )
    for batch in range(1, 15):
        log.add(
            syslog(
                clock.tick(20, 60),
                "mail-1",
                "notify",
                3301,
                f"INFO Sent email batch {batch} ({rng.randint(20, 60)} messages)",
            )
        )
    log.add(
        syslog(
            clock.tick(),
            "mail-1",
            "netplan",
            220,
            "INFO Configuration updated: nameserver 10.0.0.53 -> 10.0.9.53",
        ),
        context=True,
    )
    log.add(
        syslog(
            clock.tick(),
            "mail-1",
            "systemd-resolved",
            410,
            "WARN Using degraded feature set UDP instead of UDP+EDNS0 for DNS server 10.0.9.53",
        ),
        "dns",
    )
    for batch in range(15, 40):
        log.add(
            syslog(
                clock.tick(10, 30),
                "mail-1",
                "notify",
                3301,
                "ERROR DNS lookup failed for smtp.mail-provider.example: NXDOMAIN",
            ),
            "dns",
        )
        if rng.random() < 0.5:
            log.add(
                syslog(
                    clock.tick(),
                    "mail-1",
                    "notify",
                    3301,
                    f"ERROR Email batch {batch} failed: cannot resolve "
                    "smtp.mail-provider.example (NXDOMAIN)",
                ),
                "dns",
            )
        if rng.random() < 0.3:
            log.add(
                syslog(
                    clock.tick(),
                    "mail-1",
                    "sshd",
                    901,
                    "INFO Accepted publickey for ops from 10.0.4.9 port 51022",
                )
            )
    return log


def kafka_consumer_lag() -> Log:
    """#12 (Parquet): consumers scaled down; lag on orders-events keeps rising."""
    rng = random.Random(12)
    clock = Clock(_start(9, 9, 0), rng)
    log = Log(
        "kafka_consumer_lag.parquet",
        "parquet",
        "Consumers are scaled down during peak; lag on orders-events climbs past 50k.",
        generated=True,
    )
    log.expect(
        "lag",
        Issue(
            "queue",
            "medium",
            "order-consumer",
            "Consumer lag",
            ["scale", "consumers", "partitions"],
        ),
    )
    lag = [2000] * 6
    for step in range(3000):
        if step == 600:
            log.add(
                _kafka_row(
                    clock.tick(), "INFO", "Scaled down order-consumer to 2 replicas", None, None
                ),
                context=True,
            )
        partition = step % 6
        growth = rng.randint(-200, 300) if step < 600 else rng.randint(50, 400)
        lag[partition] = max(0, lag[partition] + growth)
        if lag[partition] > 50_000 and step % 4 == 0:
            log.add(
                _kafka_row(
                    clock.tick(0.2, 1),
                    "WARN",
                    f"Consumer lag on orders-events partition {partition} is "
                    f"{lag[partition]} messages (threshold 50000)",
                    partition,
                    lag[partition],
                ),
                "lag",
            )
        else:
            offset = 1_200_000 + step * 37
            log.add(
                _kafka_row(
                    clock.tick(0.2, 1),
                    "INFO",
                    f"Committed offset {offset} on orders-events partition {partition}",
                    partition,
                    lag[partition],
                )
            )
    return log


def _kafka_row(
    ts: datetime, level: str, message: str, partition: int | None, lag: int | None
) -> dict[str, Any]:
    return {
        "timestamp": ts,
        "level": level,
        "service": "order-consumer",
        "message": message,
        "partition": partition,
        "lag": lag,
        "consumer_group": "orders",
    }


def healthy_service() -> Log:
    """#13: nothing wrong; a few harmless warnings that should not be reported."""
    rng = random.Random(13)
    clock = Clock(_start(9, 10, 0), rng)
    log = Log(
        "healthy_service.csv",
        "csv",
        "A healthy catalog service: normal traffic and three harmless warnings.",
    )
    harmless = {
        40: "Slow health check: 1100ms (threshold 1000ms)",
        95: "Deprecated API version v1 called by client mobile-app/5.2",
        130: "Cache miss rate 12% (normal under 20%)",
    }
    for step in range(150):
        if step in harmless:
            log.add(
                {
                    "timestamp": iso(clock.tick()),
                    "level": "WARN",
                    "service": "catalog",
                    "message": harmless[step],
                    "latency_ms": rng.randint(5, 40),
                },
                noise=True,
            )
            continue
        path = rng.choice(["/api/catalog", "/api/catalog/search", "/health", "/api/catalog/42"])
        log.add(
            {
                "timestamp": iso(clock.tick()),
                "level": "INFO",
                "service": "catalog",
                "message": f"GET {path} 200",
                "latency_ms": rng.randint(3, 120),
            }
        )
    return log


def mixed_incident() -> Log:
    """#14: after a checkout deploy, DB errors, inventory timeouts and gateway 503s together."""
    rng = random.Random(14)
    clock = Clock(_start(9, 15, 0), rng)
    log = Log(
        "mixed_incident.json",
        "json_document",
        "After a checkout deploy: DB connections refused, inventory timeouts and 503s.",
    )
    log.expect(
        "db",
        Issue(
            "database",
            "critical",
            "checkout",
            "Connection refused to orders-db",
            ["pool", "database", "roll back"],
        ),
    )
    log.expect(
        "5xx", Issue("http_5xx", "high", "api-gateway", "returned 503", ["upstream", "checkout"])
    )
    log.expect(
        "timeout", Issue("timeout", "medium", "checkout", "timed out", ["timeout", "inventory-svc"])
    )

    def record(
        severity: str, app_name: str, message: str, status: int | None = None
    ) -> dict[str, Any]:
        entry: dict[str, Any] = {
            "@timestamp": iso(clock.tick()),
            "severity": severity,
            "app": app_name,
            "message": message,
            "trace_id": f"c-{rng.randint(1000, 9999)}",
        }
        if status is not None:
            entry["http"] = {"status": status}
        return entry

    for order in range(1030, 1060):
        log.add(record("INFO", "checkout", f"Order {order} placed", 201))
    log.add(record("INFO", "checkout", "Order 1060 placed by jane.doe@example.com", 201))
    log.add(record("INFO", "checkout", "Deploy started: checkout v5.2.0"), context=True)
    log.add(record("INFO", "checkout", "Deploy finished: checkout v5.2.0"), context=True)
    for _ in range(60):
        roll = rng.random()
        if roll < 0.35:
            log.add(
                record("ERROR", "checkout", "Connection refused to orders-db.internal:5432"), "db"
            )
        elif roll < 0.55:
            log.add(
                record("WARN", "checkout", "Call to inventory-svc timed out after 5000ms", 504),
                "timeout",
            )
        elif roll < 0.8:
            log.add(record("ERROR", "api-gateway", "Upstream checkout returned 503", 503), "5xx")
        else:
            log.add(record("INFO", "catalog", "GET /api/catalog 200", 200))
    return log


def payment_db_conn_refused_repeat() -> Log:
    """#15: the same failure as #1 the next morning (tests JIRA dedupe and regressions)."""
    rng = random.Random(15)
    clock = Clock(_start(10, 9, 0), rng)
    log = Log(
        "payment_db_conn_refused_repeat.log",
        "plain",
        "The same payment DB failure as #1, next morning, after scaling up.",
        same_signature_as="payment_db_conn_refused.log",
    )
    log.expect(
        "db",
        Issue(
            "database",
            "critical",
            "payment-api",
            "Connection refused to payments-db",
            ["pool", "max_size"],
        ),
    )
    _payment_traffic(log, clock, rng, 25)
    log.add(
        app(clock.tick(), "INFO", "autoscaler", "Scaled up payment-api to 4 replicas"), context=True
    )
    _payment_traffic(log, clock, rng, 10)
    _payment_outage(log, clock, rng, "db", rounds=8)
    return log


def edge_nginx_error() -> Log:
    """#16 (gzip): an edge nginx error log with minor problems only."""
    rng = random.Random(16)
    clock = Clock(_start(9, 0, 0), rng)
    log = Log(
        "edge_nginx_error.log.gz",
        "nginx_error",
        "Edge nginx: oversized uploads and a scanner probing for missing pages. Minor only.",
        compressed=True,
        generated=True,
    )
    log.expect("body", Issue("config", "low", "nginx", "too large body", ["client_max_body_size"]))
    log.expect(
        "probe", Issue("other", "low", "nginx", "No such file or directory", ["block", "scanner"])
    )
    probes = ["/wp-login.php", "/.env", "/admin.php", "/phpmyadmin/index.php", "/xmlrpc.php"]
    for _ in range(1500):
        ts = clock.tick(10, 90)
        roll = rng.random()
        if roll < 0.15:
            log.add(
                nginx_error(
                    ts,
                    "error",
                    f"client intended to send too large body: "
                    f"{rng.randint(11, 40) * 1048576} bytes",
                    "POST /api/upload HTTP/1.1",
                ),
                "body",
            )
        elif roll < 0.45:
            path = rng.choice(probes)
            log.add(
                nginx_error(
                    ts,
                    "error",
                    f'open() "/usr/share/nginx/html{path}" failed (2: No such file or directory)',
                    f"GET {path} HTTP/1.1",
                ),
                "probe",
            )
        elif roll < 0.55:
            log.add(
                nginx_error(
                    ts,
                    "warn",
                    "an upstream response is buffered to a temporary file "
                    f"/var/cache/nginx/proxy_temp/{rng.randint(1, 9)}/00/"
                    f"{rng.randint(10000, 99999)}",
                    "GET /api/export HTTP/1.1",
                ),
                noise=True,
            )
        else:
            log.add(nginx_error(ts, "notice", "signal process started"))
    return log


# ------------------------------------------------------------------ the 50k-row log

_LARGE_SERVICES = ["payment-api", "checkout", "catalog", "search", "notify", "auth", "orders"]


def _large_issue_row(rng: random.Random, kind: str) -> tuple[str, str, str, int | None]:
    """(service, level, message, status) for one row of a hidden issue."""
    if kind == "db":
        if rng.random() < 0.05:
            return (
                "payment-api",
                "FATAL",
                "Health check failed 3 times in a row: database unreachable",
                None,
            )
        return (
            "payment-api",
            "ERROR",
            f"Connection refused to payments-db.internal:5432 (attempt {rng.randint(1, 3)}/3)",
            None,
        )
    if kind == "5xx":
        return (
            "checkout",
            "ERROR",
            f"POST /api/checkout {rng.choice([502, 503])}",
            rng.choice([502, 503]),
        )
    if kind == "timeout":
        return (
            "search",
            "WARN",
            "upstream timed out (110: Connection timed out) while reading "
            "response header from upstream search-svc:9200",
            504,
        )
    if kind == "rate":
        if rng.random() < 0.2:
            return (
                "notify",
                "ERROR",
                f"Giving up on SMS batch {rng.randint(1, 60)}/60 after 3 retries",
                429,
            )
        return (
            "notify",
            "WARN",
            f"SMS provider returned 429 Too Many Requests, retrying in {rng.choice([1, 2, 4])}s",
            429,
        )
    if kind == "auth":
        address = rng.choice(["203.0.113.7", "203.0.113.45", "198.51.100.23"])
        return (
            "auth",
            "WARN",
            f"Login failed for user {rng.choice(['admin', 'root'])} from "
            f"{address}: invalid password",
            401,
        )
    if kind == "lag":
        return (
            "orders",
            "WARN",
            f"Consumer lag on orders-events partition {rng.randint(0, 5)} "
            f"is {rng.randint(50_001, 180_000)} messages (threshold 50000)",
            None,
        )
    return (
        "catalog",
        "WARN",
        rng.choice(
            [
                "Slow health check: 1100ms (threshold 1000ms)",
                "Deprecated API version v1 called by client mobile-app/5.2",
            ]
        ),
        None,
    )


def large_50k_mixed() -> Log:
    """#17 (Parquet): 50,000 rows with several real issues hidden in normal traffic."""
    rng = random.Random(17)
    clock = Clock(_start(8, 0, 0), rng)
    log = Log(
        "large_50k_mixed.parquet",
        "parquet",
        "50,000 rows over a day: six real issues and some harmless warnings in normal traffic.",
        generated=True,
    )
    log.expect(
        "db",
        Issue("database", "critical", "payment-api", "Connection refused", ["pool", "database"]),
    )
    log.expect("5xx", Issue("http_5xx", "high", "checkout", "POST /api/checkout", ["upstream"]))
    log.expect("auth", Issue("auth", "high", "auth", "Login failed", ["block", "rate limit"]))
    log.expect("timeout", Issue("timeout", "medium", "search", "upstream timed out", ["timeout"]))
    log.expect("rate", Issue("rate_limit", "medium", "notify", "429", ["backoff", "quota"]))
    log.expect("lag", Issue("queue", "medium", "orders", "Consumer lag", ["scale"]))
    weights = [
        ("db", 3),
        ("5xx", 3),
        ("timeout", 4),
        ("rate", 3),
        ("auth", 2),
        ("lag", 3),
        ("noise", 2),
    ]
    kinds = [kind for kind, weight in weights for _ in range(weight)]
    for row in range(50_000):
        ts = clock.tick(1.0, 2.4)
        if row == 21_000:
            log.add(
                _large_row(
                    ts, rng, "payment-api", "INFO", "Deploy finished: payment-api v2.15.0", None
                ),
                context=True,
            )
            continue
        if rng.random() < 0.02:
            kind = rng.choice(kinds)
            service, level, message, status = _large_issue_row(rng, kind)
            log.add(
                _large_row(ts, rng, service, level, message, status),
                None if kind == "noise" else kind,
                noise=kind == "noise",
            )
            continue
        service = rng.choice(_LARGE_SERVICES)
        path = rng.choice(["/health", "/api/items", "/api/cart", "/api/search?q=x", "/api/me"])
        log.add(_large_row(ts, rng, service, "INFO", f"GET {path} 200", 200))
    return log


def _large_row(
    ts: datetime, rng: random.Random, service: str, level: str, message: str, status: int | None
) -> dict[str, Any]:
    return {
        "ts": ts,
        "level": level,
        "service": service,
        "host": f"{service}-{rng.randint(1, 3)}",
        "message": message,
        "status": status,
        "trace_id": f"{rng.getrandbits(32):08x}",
    }


SCENARIOS: list[Callable[[], Log]] = [
    payment_db_conn_refused,
    orders_oom_killed,
    api_disk_full,
    gateway_tls_expired,
    auth_bruteforce,
    nginx_5xx_spike,
    search_upstream_timeouts,
    worker_crashloop,
    inventory_bad_config,
    api_rate_limited,
    notify_dns_failures,
    kafka_consumer_lag,
    healthy_service,
    mixed_incident,
    payment_db_conn_refused_repeat,
    edge_nginx_error,
    large_50k_mixed,
]


# ------------------------------------------------------------------ writing


def render(log: Log) -> bytes:
    """The file's bytes, in the log's format."""
    if log.format in TEXT_FORMATS:
        data = ("\n".join(log.entries) + "\n").encode()
    elif log.format == "json_lines":
        data = "".join(json.dumps(entry) + "\n" for entry in log.entries).encode()
    elif log.format == "json_document":
        records = ",\n".join("    " + json.dumps(entry) for entry in log.entries)
        document = f'{{\n  "source": "log aggregator export",\n  "logs": [\n{records}\n  ]\n}}\n'
        data = document.encode()
    elif log.format in ("csv", "tsv"):
        buffer = io.StringIO()
        writer = csv.DictWriter(
            buffer,
            fieldnames=list(log.entries[0]),
            lineterminator="\n",
            delimiter="\t" if log.format == "tsv" else ",",
        )
        writer.writeheader()
        writer.writerows(log.entries)
        data = buffer.getvalue().encode()
    else:
        out = pa.BufferOutputStream()
        pq.write_table(pa.Table.from_pylist(log.entries), out)
        data = out.getvalue().to_pybytes()
    return gzip.compress(data, mtime=0) if log.compressed else data


def build() -> dict[str, bytes]:
    """Every file this script produces, by name, including labels.json."""
    logs = [scenario() for scenario in SCENARIOS]
    files = {log.file: render(log) for log in logs}
    labels = {"version": 1, "logs": [log.label() for log in logs]}
    files["labels.json"] = (json.dumps(labels, indent=2) + "\n").encode()
    return files


def main() -> None:
    """Write the sample logs and labels.json next to this script."""
    for name, data in build().items():
        (OUT / name).write_bytes(data)
        print(f"{name:40} {len(data):>10,} bytes")


if __name__ == "__main__":
    main()
