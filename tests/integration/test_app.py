"""The Streamlit page, driven by Streamlit's headless test runner with fake services."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

import pytest
from langchain_core.embeddings import Embeddings
from pydantic import SecretStr
from streamlit.testing.v1 import AppTest

from incident_suite.agents import LLMError, LLMReply, StructuredLLM
from incident_suite.agents.assistant import AssistantAnswer, ChatStep, RunbookFields
from incident_suite.agents.cookbook import CookbookAnswer, ItemDraft
from incident_suite.agents.jira_agent import ExistingTicket, TicketTracker
from incident_suite.agents.log_reader import ClassifierAnswer, IssueDraft
from incident_suite.agents.notification import Messenger
from incident_suite.agents.remediation import RemediationAnswer
from incident_suite.config import AdminOverrides, AppConfig, admin_overrides, set_admin_overrides
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


def _text(app: AppTest) -> str:
    """All markdown on the page, wherever it sits."""
    return " ".join(m.value for m in app.markdown)


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
    assert app.title[0].value == "TriageGraph"
    assert "Dry run: nothing is sent" in _text(app)


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
        "Roll back payment\\-api \\(ISS\\-1\\)",
        "Alert on pool use above 80% \\(ISS\\-1\\)",
    ]
    assert len(app.main.tabs) == 9
    assert any("ERROR and FATAL lines per" in c.value for c in app.caption)


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
    assert [method for method, _ in jira_calls] == [
        "find",
        "create",
        "remote_link",
        "slack_thread",
    ]
    assert len(posts) == 1
    assert "Approved and sent" in done.success[0].value


def test_bad_upload_is_refused(monkeypatch: pytest.MonkeyPatch, keyed: AppConfig) -> None:
    tiny = keyed.model_copy(update={"max_upload_mb": 1})
    monkeypatch.setattr("incident_suite.config.load_config", lambda: tiny)
    app = _app()
    app.multiselect[0].set_value(["large_50k_mixed.parquet"])
    app = _click(app.run(), "Analyse")
    assert "over the 1 MB upload limit" in app.error[0].value


def test_several_samples_are_one_run_each(
    monkeypatch: pytest.MonkeyPatch, keyed: AppConfig, make_llm: MakeLLM
) -> None:
    two_runs, _ = make_llm(ANSWERS * 2)
    monkeypatch.setattr("incident_suite.agents.OpenRouterLLM", lambda _config: two_runs)
    tiny = keyed.model_copy(update={"max_upload_mb": 1})
    monkeypatch.setattr("incident_suite.config.load_config", lambda: tiny)
    app = _app()
    app.multiselect[0].set_value(
        ["payment_db_conn_refused.log", "large_50k_mixed.parquet", "worker_crashloop.log"]
    )
    app = _click(app.run(), "Analyse")

    assert not app.exception
    assert "over the 1 MB upload limit" in app.error[0].value  # kept above the other runs
    (picker,) = app.get("button_group")
    assert app.session_state["run_name"] == "worker_crashloop.log"  # the last run shows first
    assert sorted(app.session_state["runs"]) == [
        "payment_db_conn_refused.log",
        "worker_crashloop.log",
    ]
    first = app.session_state["runs"]["payment_db_conn_refused.log"]["run_id"]
    picker.set_value("payment_db_conn_refused.log")  # type: ignore[union-attr]
    app = app.run()
    assert app.session_state["run_name"] == "payment_db_conn_refused.log"
    assert app.session_state["run_id"] == first
    assert app.metric[0].value == "CRITICAL"


def test_live_sending_needs_the_passcode(
    monkeypatch: pytest.MonkeyPatch,
    keyed: AppConfig,
    make_llm: MakeLLM,
    make_tracker: MakeTracker,
    make_messenger: MakeMessenger,
) -> None:
    two_runs, _ = make_llm(ANSWERS * 2)
    monkeypatch.setattr("incident_suite.agents.OpenRouterLLM", lambda _config: two_runs)
    guarded = keyed.model_copy(
        update={"live_integrations": True, "admin_passcode": SecretStr("open-sesame")}
    )
    tracker, jira_calls = make_tracker()
    messenger, _ = make_messenger()
    monkeypatch.setattr("incident_suite.config.load_config", lambda: guarded)
    monkeypatch.setattr(
        "incident_suite.graph.connect_services",
        lambda _config, settings: (tracker, messenger) if settings.live else (None, None),
    )

    visitor = _click(_app(), "Analyse")
    assert "Dry run: nothing is sent" in _text(visitor)
    assert jira_calls == []  # a visitor's run never touched JIRA
    assert not [h for h in visitor.subheader if h.value == "Approval needed"]

    admin = _open(_unlock(_app(), "app", "open-sesame"), "analyze")
    assert "Live: approved runs post to Slack and JIRA" in _text(admin)
    paused = _click(admin, "Analyse")
    assert [h for h in paused.subheader if h.value == "Approval needed"]


# ------------------------------------------------------------------ admin console


def _button(app: AppTest, key: str) -> Any:
    return next(b for b in app.button if b.key == key)


def _open(app: AppTest, page: str) -> AppTest:
    """Pick a page from the menu in the left bar."""
    return next(b for b in app.sidebar.button if b.key == f"menu_{page}").click().run()


def _unlock(app: AppTest, page: str, passcode: str) -> AppTest:
    """Pick a settings page and answer the passcode dialog."""
    app = _open(app, page)
    app.text_input(key="passcode").input(passcode)  # in a form: sent with the button
    return app.button(key="unlock").click().run()


def test_left_bar_is_just_the_menu(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("incident_suite.config.load_config", AppConfig)
    app = _app()
    assert [b.label for b in app.sidebar.button] == ["Analyze", "Agent Settings", "App Settings"]
    assert not app.sidebar.text_input
    assert app.sidebar.button(key="menu_analyze").proto.type == "primary"
    app = _open(app, "agents")
    assert app.sidebar.button(key="menu_agents").proto.type == "primary"
    assert [t.value for t in app.title] == [":material/smart_toy: Agent Settings"]
    assert ":material/forum: Assistant" in [s.value for s in app.subheader]  # always on the right
    assert all(b.proto.icon == ":material/save:" for b in app.button if b.label == "Save")


def test_console_is_read_only_without_a_passcode(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("incident_suite.config.load_config", AppConfig)
    app = _open(_app(), "agents")
    assert any("Set ADMIN_PASSCODE" in c.value for c in app.caption)
    assert _button(app, "save_models").disabled
    app = _open(app, "app")
    assert _button(app, "save_jira").disabled


def test_console_saves_for_everyone(monkeypatch: pytest.MonkeyPatch) -> None:
    base = AppConfig(admin_passcode=SecretStr("open-sesame"), jira_project_key="SCRUM")
    monkeypatch.setattr("incident_suite.config.load_config", lambda: base)
    app = _unlock(_app(), "agents", "wrong")
    assert [e.value for e in app.error] == ["That passcode isn't right."]
    assert not [b for b in app.button if b.key == "save_models"]  # still on Analyze

    app = _unlock(_app(), "agents", "open-sesame")
    assert not _button(app, "save_models").disabled

    app.selectbox(key="remediation_model").set_value("openai/gpt-5-nano")
    app = _button(app.run(), "save_remediation").click().run()
    key_box = next(t for t in app.text_input if t.label == "OpenRouter API key")
    key_box.input("sk-or-new-key")
    app = _button(app.run(), "save_models").click().run()

    assert admin_overrides().remediation_model == "openai/gpt-5-nano"
    stored = admin_overrides().openrouter_api_key
    assert stored is not None
    assert stored.get_secret_value() == "sk-or-new-key"
    assert "sk-or-new-key" not in str(app)  # the typed key doesn't linger in the session
    assert any("remediation_model" in c.value for c in app.caption)

    visitor = _app()  # another session runs with the new model
    visitor.session_state["page"] = "agents"
    assert visitor.run().selectbox(key="remediation_model").value == "openai/gpt-5-nano"

    app = _button(app, "back_to_defaults").click().run()
    assert admin_overrides() == AdminOverrides()


def test_restore_defaults_for_one_card(monkeypatch: pytest.MonkeyPatch) -> None:
    base = AppConfig(admin_passcode=SecretStr("open-sesame"))
    monkeypatch.setattr("incident_suite.config.load_config", lambda: base)
    set_admin_overrides(AdminOverrides(remediation_model="openai/gpt-5-nano", max_upload_mb=2))
    app = AppTest.from_file(str(APP), default_timeout=30)
    app.session_state["admin_ok"] = True
    app.session_state["page"] = "agents"
    app = app.run()
    assert _button(app, "restore_cookbook").disabled  # nothing changed on that card
    assert app.selectbox(key="remediation_model").value == "openai/gpt-5-nano"

    app = _button(app, "restore_remediation").click().run()
    assert admin_overrides() == AdminOverrides(max_upload_mb=2)  # other cards keep theirs
    assert app.selectbox(key="remediation_model").value == "openai/gpt-5-mini"
    assert _button(app, "restore_remediation").disabled


def test_console_refuses_to_redirect_the_jira_token(monkeypatch: pytest.MonkeyPatch) -> None:
    base = AppConfig(
        admin_passcode=SecretStr("open-sesame"),
        jira_base_url="https://ours.atlassian.net",
        jira_api_token=SecretStr("ours"),
    )
    monkeypatch.setattr("incident_suite.config.load_config", lambda: base)
    app = AppTest.from_file(str(APP), default_timeout=30)
    app.session_state["admin_ok"] = True
    app.session_state["page"] = "app"
    app = app.run()
    url = next(t for t in app.text_input if t.label == "Base URL")
    url.input("https://evil.example")
    app = _button(app.run(), "save_jira").click().run()
    assert any("needs the JIRA API token too" in e.value for e in app.error)
    assert admin_overrides() == AdminOverrides()


# ------------------------------------------------------------------ assistant


def test_assistant_reports_what_happened_since(
    monkeypatch: pytest.MonkeyPatch,
    keyed: AppConfig,
    make_llm: MakeLLM,
    make_tracker: MakeTracker,
    make_messenger: MakeMessenger,
) -> None:
    summary = AssistantAnswer(summary="Priya picked it up.", updates=["OPS-101: In Progress"])
    llm, calls = make_llm([*ANSWERS, summary])
    monkeypatch.setattr("incident_suite.agents.OpenRouterLLM", lambda _config: llm)
    live = keyed.model_copy(
        update={"live_integrations": True, "admin_passcode": SecretStr("open-sesame")}
    )
    tracker, jira_calls = make_tracker()
    messenger, _ = make_messenger()
    monkeypatch.setattr("incident_suite.config.load_config", lambda: live)
    monkeypatch.setattr(
        "incident_suite.graph.connect_services", lambda _config, _settings: (tracker, messenger)
    )

    app = AppTest.from_file(str(APP), default_timeout=30)
    app.session_state["admin_ok"] = True
    app = _click(app.run(), "Analyse")
    before = [s.value for s in app.subheader]
    assert ":material/forum: Assistant" in before
    assert any("posts to Slack or JIRA" in c.value for c in app.caption)

    app = _click(app, "Approve and send")
    app = _click(app, "What's happened since?")

    assert not app.exception
    panel = _text(app)
    assert "Priya picked it up\\." in panel
    assert "OPS\\-101\\: In Progress" in panel
    assert ("updates", "OPS-101") in jira_calls
    assert calls[-1][0] == "openai/gpt-5-nano"
    usage = app.session_state["result"].usage
    assert "assistant" in [u.agent for u in usage]


def test_assistant_needs_the_passcode(
    monkeypatch: pytest.MonkeyPatch,
    keyed: AppConfig,
    make_tracker: MakeTracker,
    make_messenger: MakeMessenger,
) -> None:
    live = keyed.model_copy(update={"live_integrations": True, "require_approval": False})
    tracker, _ = make_tracker()
    messenger, _ = make_messenger()
    monkeypatch.setattr("incident_suite.config.load_config", lambda: live)
    monkeypatch.setattr(
        "incident_suite.graph.connect_services", lambda _config, _settings: (tracker, messenger)
    )
    app = _click(_app(), "Analyse")  # no ADMIN_PASSCODE: sends, but the assistant stays off
    assert any("admin passcode" in c.value for c in app.caption)
    assert not [b for b in app.button if b.label == "What's happened since?"]


def test_ask_about_an_earlier_incident_without_a_run(
    monkeypatch: pytest.MonkeyPatch,
    keyed: AppConfig,
    make_llm: MakeLLM,
    make_tracker: MakeTracker,
    make_messenger: MakeMessenger,
) -> None:
    summary = AssistantAnswer(summary="Resolved by Priya.", updates=["OPS-7: Done"])
    llm, calls = make_llm([summary])
    monkeypatch.setattr("incident_suite.agents.OpenRouterLLM", lambda _config: llm)
    connected = keyed.model_copy(
        update={
            "jira_base_url": "https://team.atlassian.net",
            "jira_email": "bot@example.com",
            "jira_api_token": SecretStr("token"),
            "jira_project_key": "OPS",
            "admin_passcode": SecretStr("open-sesame"),
        }
    )
    monkeypatch.setattr("incident_suite.config.load_config", lambda: connected)
    tracker, jira_calls = make_tracker()
    messenger, _ = make_messenger()
    monkeypatch.setattr(
        "incident_suite.graph.connect_services", lambda _config, _settings: (tracker, messenger)
    )

    locked = _app()
    assert any("read earlier incidents" in c.value for c in locked.caption)

    app = AppTest.from_file(str(APP), default_timeout=30)
    app.session_state["admin_ok"] = True
    app = _click(app.run(), "Load the latest incidents")
    assert ("recent", "5") in jira_calls
    picker = next(s for s in app.selectbox if s.label == "Incident")
    assert picker.options[0].startswith("OPS-7 · Done · 2026-10-10")
    app = _click(app, "What's happened since?")

    assert not app.exception
    assert "Resolved by Priya\\." in _text(app)
    assert ("updates", "OPS-7") in jira_calls
    assert 'earlier_incident="OPS-7"' in calls[-1][2]
    assert "Looking at C123/1700000000\\.000100" not in _text(app)  # data for the model only
    assert any(c.value.startswith("Cost") for c in app.caption)


def test_earlier_incidents_when_jira_fails_or_is_missing(
    monkeypatch: pytest.MonkeyPatch, keyed: AppConfig, make_tracker: MakeTracker
) -> None:
    jira = {
        "jira_base_url": "https://team.atlassian.net",
        "jira_email": "bot@example.com",
        "jira_api_token": SecretStr("token"),
        "jira_project_key": "OPS",
    }
    monkeypatch.setattr("incident_suite.config.load_config", lambda: keyed)
    assert any("Connect JIRA" in c.value for c in _app().caption)

    monkeypatch.setattr("incident_suite.config.load_config", lambda: keyed.model_copy(update=jira))
    tracker, _ = make_tracker(fail="recent")
    monkeypatch.setattr(
        "incident_suite.graph.connect_services", lambda _config, _settings: (tracker, None)
    )
    app = AppTest.from_file(str(APP), default_timeout=30)
    app.session_state["admin_ok"] = True
    app = _click(app.run(), "Load the latest incidents")
    assert "JIRA returned 401" in app.warning[0].value


# ------------------------------------------------------------------ runbook drafts


class _ByType:
    """A fake model that answers by the schema asked for, so call order doesn't matter."""

    def __init__(
        self, answers: Sequence[Any], failures: dict[type[Any], LLMError] | None = None
    ) -> None:
        self.answers = {type(answer): answer for answer in answers}
        self.failures = failures or {}
        self.asked: list[str] = []

    def __call__(self, model: str, system: str, user: str, schema: type[Any]) -> LLMReply[Any]:  # noqa: ARG002
        self.asked.append(schema.__name__)
        if schema in self.failures:
            raise self.failures[schema]
        return LLMReply(
            self.answers[schema], model, input_tokens=500, output_tokens=100, seconds=0.1
        )


@pytest.mark.usefixtures("keyed")
def test_draft_approve_and_reuse_a_runbook(monkeypatch: pytest.MonkeyPatch) -> None:
    no_source = ANSWERS[1].model_copy(update={"source_ids": []})
    fields = RunbookFields(
        title="Payment DB pool too small",
        signals=["Connection refused to db:<port>"],
        symptoms=["503s on payment-api"],
        likely_causes=["Pool shrank in the deploy"],
        diagnose=["Compare db.pool.max_size before and after"],
        fix_now=["Roll back payment-api"],
        prevent=["Alert on pool use"],
    )
    llm = _ByType([ANSWERS[0], no_source, ANSWERS[2], fields])
    monkeypatch.setattr("incident_suite.agents.OpenRouterLLM", lambda _config: llm)

    app = _click(_app(), "Analyse")
    assert any("No runbook matched" in c.value for c in app.caption)
    app = _click(app, "Draft a runbook (AI)")
    assert not app.exception
    assert "RunbookFields" in llm.asked
    assert any("AI draft, not reviewed" in w.value for w in app.warning)
    assert any("draft-database-" in c.value for c in app.code)

    app = _click(app, "Approve")
    approved = app.session_state["approved_runbooks"]
    (runbook_id,) = approved
    assert runbook_id.startswith("draft-database-")
    assert any("Approved" in s.value for s in app.success)
    usage = {u.agent for u in app.session_state["result"].usage}
    assert "assistant" in usage

    app = _click(app, "Analyse")  # the next run searches the session's own index
    ids, index = app.session_state["draft_index"]
    assert ids == (runbook_id,)
    assert index.size == 16


@pytest.mark.usefixtures("keyed")
def test_a_failed_draft_can_be_tried_again(monkeypatch: pytest.MonkeyPatch) -> None:
    no_source = ANSWERS[1].model_copy(update={"source_ids": []})
    failure = LLMError("The model call failed (APITimeoutError).", 100, 0)
    llm = _ByType([ANSWERS[0], no_source, ANSWERS[2]], failures={RunbookFields: failure})
    monkeypatch.setattr("incident_suite.agents.OpenRouterLLM", lambda _config: llm)

    app = _click(_click(_app(), "Analyse"), "Draft a runbook (AI)")
    assert not app.exception
    assert any("APITimeoutError" in w.value for w in app.warning)
    app = _click(app, "Try again")
    assert [b for b in app.button if b.label == "Draft a runbook (AI)"]


# ------------------------------------------------------------------ chat about the run


@pytest.mark.usefixtures("keyed")
def test_chat_about_a_dry_run(monkeypatch: pytest.MonkeyPatch) -> None:
    answer = ChatStep(answer="The DB refused connections after the deploy (L68).")
    llm = _ByType([*ANSWERS, answer])
    monkeypatch.setattr("incident_suite.agents.OpenRouterLLM", lambda _config: llm)

    app = _click(_app(), "Analyse")
    (box,) = [t for t in app.text_input if t.label == "Ask about this run"]
    box.input("Why did payments fail?")
    app = _click(app, "Ask")

    assert not app.exception
    panel = _text(app)
    assert "Why did payments fail?" in panel
    assert "L68" in panel
    assert app.session_state["chat"] == [
        ("Why did payments fail?", "The DB refused connections after the deploy (L68).")
    ]
    assert "ChatStep" in llm.asked


# ------------------------------------------------------------------ paste logs


@pytest.mark.usefixtures("keyed")
def test_pasted_text_goes_through_the_same_path() -> None:
    app = _app()
    app.radio[0].set_value("Paste text")
    app = app.run()
    assert next(b for b in app.button if b.label == "Analyse").disabled  # nothing pasted yet
    sample = (APP.parent / "sample_logs" / "payment_db_conn_refused.log").read_text()
    app.text_area[0].input(sample + "\nuser=bob password=hunter2\n")
    app = _click(app.run(), "Analyse")

    assert not app.exception
    context = app.session_state["context"]
    assert context.parsed.name == "pasted.log"
    assert context.parsed.redactions  # redacted like an upload
    assert not any("hunter2" in line.raw for line in context.parsed.lines)
    assert app.metric[0].value == "CRITICAL"


@pytest.mark.usefixtures("keyed")
def test_postmortem_download_after_a_run() -> None:
    app = _click(_app(), "Analyse")
    (button,) = [b for b in app.get("download_button") if "postmortem" in b.proto.label]
    assert button.proto.label == "Download postmortem draft (.md)"


# ------------------------------------------------------------------ seen before


def test_card_shows_how_it_was_fixed_last_time(
    monkeypatch: pytest.MonkeyPatch,
    keyed: AppConfig,
    make_tracker: MakeTracker,
    make_messenger: MakeMessenger,
) -> None:
    closed = ExistingTicket(
        key="SCRUM-1",
        url="https://team.atlassian.example/browse/SCRUM-1",
        done=True,
        last_comment="Rolled back to v2.14.0 and raised the pool to 50",
    )
    tracker, _ = make_tracker(existing=_AnyHash(closed))
    messenger, _ = make_messenger()
    live = keyed.model_copy(update={"live_integrations": True})
    monkeypatch.setattr("incident_suite.config.load_config", lambda: live)
    monkeypatch.setattr(
        "incident_suite.graph.connect_services", lambda _config, _settings: (tracker, messenger)
    )
    app = _click(_app(), "Analyse")
    assert any("Seen before in SCRUM-1" in i.value for i in app.info)
    assert any("raised the pool to 50" in i.value for i in app.info)


class _AnyHash(dict[str, ExistingTicket]):
    """Every signature hash finds the same ticket."""

    def __init__(self, ticket: ExistingTicket) -> None:
        super().__init__()
        self.ticket = ticket

    def __bool__(self) -> bool:
        return True  # the fake tracker treats an empty dict as "no tickets"

    def get(self, key: str, default: object = None) -> ExistingTicket:  # noqa: ARG002
        return self.ticket


@pytest.mark.usefixtures("keyed")
def test_replay_tab() -> None:
    app = _click(_app(), "Analyse")
    (slider,) = [s for s in app.slider if s.label == "Step"]
    steps = app.session_state["result"].steps
    app = slider.set_value(len(steps)).run()
    assert any(c.value.startswith(f"Step {len(steps)} of {len(steps)}") for c in app.caption)


@pytest.mark.usefixtures("keyed")
def test_a_new_run_clears_the_old_one(monkeypatch: pytest.MonkeyPatch) -> None:
    llm = _ByType(ANSWERS)
    monkeypatch.setattr("incident_suite.agents.OpenRouterLLM", lambda _config: llm)
    app = _click(_app(), "Analyse")
    first = app.session_state["run_id"]
    app.session_state["chat"] = [("old question", "old answer")]
    app = _click(app, "Analyse")
    assert app.session_state["run_id"] != first
    assert app.session_state["chat"] == []
    assert "old question" not in _text(app)
