"""The Streamlit page renders without keys, using Streamlit's headless test runner."""

from __future__ import annotations

from pathlib import Path

from streamlit.testing.v1 import AppTest

APP = Path(__file__).resolve().parents[2] / "app.py"


def test_app_renders_without_keys() -> None:
    app = AppTest.from_file(str(APP), default_timeout=30).run()
    assert not app.exception
    assert app.title[0].value == "Incident Suite"
