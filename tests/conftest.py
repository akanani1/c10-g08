"""Shared fixtures. Nothing here needs a real key or network access."""

from __future__ import annotations

import pytest
from pydantic import SecretStr

from incident_suite.config import AppConfig


@pytest.fixture
def config() -> AppConfig:
    """Defaults plus a fake OpenRouter key, so clients can be built without network access."""
    return AppConfig(openrouter_api_key=SecretStr("sk-or-test-not-a-real-key"))
