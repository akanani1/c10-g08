"""System prompts, one markdown file per agent, loaded with importlib.resources."""

from __future__ import annotations

from importlib.resources import files

PROMPT_NAMES = frozenset({"log_reader", "remediation"})


def load_prompt(name: str) -> str:
    """The default system prompt for an agent, e.g. ``load_prompt("log_reader")``."""
    if name not in PROMPT_NAMES:
        msg = f"unknown prompt {name!r}"
        raise ValueError(msg)
    return files(__name__).joinpath(f"{name}.md").read_text(encoding="utf-8")
