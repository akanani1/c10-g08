"""Upload checks before anything is read, limits on runs, and the admin passcode check.

Escaping for the screen lives in ``ui.py`` and for Slack and JIRA in ``integrations/payloads.py``.
"""

from __future__ import annotations

import hmac
import re
import time
import unicodedata
from collections import deque

from pydantic import SecretStr

ALLOWED_EXTENSIONS = frozenset({"log", "txt", "jsonl", "ndjson", "json", "csv", "tsv", "parquet"})
MAX_DISPLAY_NAME = 120
RUNS_PER_WINDOW = 5
WINDOW_SECONDS = 600
SHARED_RUNS_PER_HOUR = 60  # across every visitor, to protect the model credit
_UNSAFE_NAME_CHARS = re.compile(r"[^\w.\- ()\[\]]")


class UploadError(ValueError):
    """The upload breaks a rule. The message is safe and meant to be shown to the user."""


def extension_of(filename: str) -> str:
    """The extension that decides the reader, ignoring a trailing ``.gz``."""
    name = filename.lower()
    if name.endswith(".gz"):
        name = name[:-3]
    return name.rsplit(".", 1)[-1] if "." in name else ""


def check_upload(filename: str, size_bytes: int, max_upload_mb: int) -> None:
    """Refuse empty, oversized or wrong-type uploads before reading them."""
    allowed = ", ".join(f".{ext}" for ext in sorted(ALLOWED_EXTENSIONS))
    if extension_of(filename) not in ALLOWED_EXTENSIONS:
        msg = f"That file type isn't supported. Use one of {allowed} (optionally gzipped)."
        raise UploadError(msg)
    if size_bytes == 0:
        msg = "The file is empty."
        raise UploadError(msg)
    if size_bytes > max_upload_mb * 1024 * 1024:
        msg = f"The file is over the {max_upload_mb} MB upload limit."
        raise UploadError(msg)


def display_name(filename: str) -> str:
    """A safe name to show on screen and in Slack/JIRA. Never used as a path."""
    name = unicodedata.normalize("NFKC", filename).replace("\\", "/").rsplit("/", 1)[-1]
    name = _UNSAFE_NAME_CHARS.sub("_", name).strip(" .") or "upload"
    return name[:MAX_DISPLAY_NAME]


class RateLimitError(RuntimeError):
    """Too many runs in this session. The message is safe to show."""


class RunLimiter:
    """At most ``max_runs`` runs in any ``window`` seconds. One per browser session.

    It stops one visitor from burning through the model credit by clicking Analyse in a loop.
    """

    def __init__(self, max_runs: int = RUNS_PER_WINDOW, window: float = WINDOW_SECONDS) -> None:
        self._max_runs = max_runs
        self._window = window
        self._started: deque[float] = deque()

    def check(self, now: float | None = None) -> None:
        """Count a new run, or raise ``RateLimitError`` if the limit is reached."""
        now = time.monotonic() if now is None else now
        while self._started and now - self._started[0] >= self._window:
            self._started.popleft()
        if len(self._started) >= self._max_runs:
            wait = int(self._window - (now - self._started[0])) + 1
            msg = f"That's {self._max_runs} runs in a few minutes. Try again in {wait} s."
            raise RateLimitError(msg)
        self._started.append(now)


def passcode_ok(entered: str, expected: SecretStr | None) -> bool:
    """True when ``entered`` matches the admin passcode. Compared in constant time."""
    if expected is None or not entered:
        return False
    return hmac.compare_digest(entered.encode(), expected.get_secret_value().encode())
