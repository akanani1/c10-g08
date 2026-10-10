"""Upload checks before anything is read.

Rate limiting and output escaping join this module in later builds.
"""

from __future__ import annotations

import re
import unicodedata

ALLOWED_EXTENSIONS = frozenset({"log", "txt", "jsonl", "ndjson", "json", "csv", "tsv", "parquet"})
MAX_DISPLAY_NAME = 120
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
