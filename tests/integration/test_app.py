"""The Streamlit page, driven by Streamlit's headless test runner with fake services."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

import pytest
from langchain_core.embeddings import Embeddings
from pydantic import SecretStr
from streamlit.testing.v1 import AppTest

from incident_suite.agents import StructuredLLM
from incident_suite.agents.cookbook import CookbookAnswer, ItemDraft
from incident_suite.agents.jira_agent import TicketTracker
from incident_suite.agents.log_reader import ClassifierAnswer, IssueDraft
from incident_suite.agents.notification import Messenger
from incident_suite.agents.remediation import RemediationAnswer
from incident_suite.config import AppConfig
from incident_suite.state import Category, NotificationPlan, Severity

APP = Path(__file__).resolve().parents[2] / "app.py"
MakeLLM = Callable[[Sequence[Any]], tuple[StructuredLLM, list[tuple[str, str, str]]]]
MakeTracker = Callable[..., tuple[TicketTracker, list[tuple[str, str]]]]
MakeMessenger = Callable[..., tuple[Messenger, list[NotificationPlan]]]

ANSWERS = [
    ClassifierAnswer(
        issues=[
            IssueDraft(
                title="Payment DB connection pool exhausted after deploy",
                category=Category.DATABASE,
                severity=Severity.CRITICAL,
                confidence=0.9,
                pattern_ids=["P1", "P2"],
                evidence_lines=[],
                affected_service="payment-api",
            )
        ]
    ),
    RemediationAnswer(
        root_cause="Likely the deploy lowered db.pool.max_size to 5.",
        steps=["Roll back payment-api to v2.14.0", "Raise db.pool.max_size"],
        rationale="Errors start right after the deploy and config load.",
        source_ids=["R1"],
        confidence=0.85,
    ),
    CookbookAnswer(
        do_now=[ItemDraft(text="Roll back payment-api", issue_ids=["ISS-1"])],
        this_week=[],
        prevent=[ItemDraft(text="Alert on pool use above 80%", issue_ids=["ISS-1"])],
    ),
]


def _app() -> AppTest:
    return AppTest.from_file(str(APP), default_timeout=30).run()


def _click(app: AppTest, label: str) -> AppTest:
    (button,) = [b for b in app.button if b.label == label]
    return button.click().run()


@pytest.fixture
def keyed(
    monkeypatch: pytest.MonkeyPatch, config: AppConfig, embeddings: Embeddings, make_llm: MakeLLM
) -> AppConfig:
    """The page with a key, a fake model and offline embeddings. Returns the config used."""
    llm, _ = make_llm(ANSWERS)
    monkeypatch.setattr("incident_suite.config.load_config", lambda: config)
    monkeypatch.setattr("incident_suite.agents.OpenRouterLLM", lambda _config: llm)
    monkeypatch.setattr("incident_suite.agents.make_embeddings", lambda _config: embeddings)
    return config


def test_app_renders_without_keys(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("incident_suite.config.load_config", AppConfig)
    app = _app()
    assert not app.exception
    assert app.title[0].value == "Incident Suite"
    assert "Dry run" in app.sidebar.info[0].value


def test_analyse_without_a_key_explains_why(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("incident_suite.config.load_config", AppConfig)
    app = _click(_app(), "Analyse")
    assert "OPENROUTER_API_KEY" in app.error[0].value


@pytest.mark.usefixtures("keyed")
def test_dry_run_shows_the_results() -> None:
    app = _click(_app(), "Analyse")

    assert not app.exception
    assert app.metric[0].value == "CRITICAL"
    assert app.metric[1].value == "1"
    markdown = " ".join(m.value for m in app.markdown)
    assert "Payment DB connection pool exhausted after deploy" in markdown
    assert "Roll back payment\\-api to v2\\.14\\.0" in markdown
    assert any("Dry run: nothing was sent" in info.value for info in app.info)
    assert [c.label for c in app.checkbox] == [
        "Roll back payment\\-api (ISS-1)",
        "Alert on pool use above 80% (ISS-1)",
    ]
    assert len(app.tabs) == 7


def test_live_run_waits_for_approval_then_sends(
    monkeypatch: pytest.MonkeyPatch,
    keyed: AppConfig,
    make_tracker: MakeTracker,
    make_messenger: MakeMessenger,
) -> None:
    live = keyed.model_copy(
        update={"live_integrations": True, "slack_bot_token": SecretStr("xoxb-test")}
    )
    tracker, jira_calls = make_tracker()
    messenger, posts = make_messenger()
    monkeypatch.setattr("incident_suite.config.load_config", lambda: live)
    monkeypatch.setattr(
        "incident_suite.graph.connect_services", lambda _config, _settings: (tracker, messenger)
    )

    paused = _click(_app(), "Analyse")
    assert [h.value for h in paused.subheader if h.value == "Approval needed"]
    assert posts == []

    done = _click(paused, "Approve and send")
    assert not done.exception
    assert [method for method, _ in jira_calls] == ["find", "create", "remote_link"]
    assert len(posts) == 1
    assert "Approved and sent" in done.success[0].value


def test_bad_upload_is_refused(monkeypatch: pytest.MonkeyPatch, keyed: AppConfig) -> None:
    tiny = keyed.model_copy(update={"max_upload_mb": 1})
    monkeypatch.setattr("incident_suite.config.load_config", lambda: tiny)
    app = _app()
    app.selectbox[0].set_value("large_50k_mixed.parquet")
    app = _click(app.run(), "Analyse")
    assert "over the 1 MB upload limit" in app.error[0].value
