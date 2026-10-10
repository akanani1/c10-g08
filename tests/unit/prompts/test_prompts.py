"""Tests for incident_suite/prompts: the prompt files ship with the package and load."""

from __future__ import annotations

import pytest

from incident_suite.prompts import PROMPT_NAMES, load_prompt


@pytest.mark.parametrize("name", sorted(PROMPT_NAMES))
def test_every_prompt_loads_and_treats_logs_as_data(name: str) -> None:
    text = load_prompt(name)
    assert len(text) > 200
    assert "never an instruction" in text


def test_unknown_prompt() -> None:
    with pytest.raises(ValueError, match="unknown prompt"):
        load_prompt("../secrets")
