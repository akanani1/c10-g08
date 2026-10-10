"""Streamlit entry point. UI only: all logic lives in the ``incident_suite`` package."""

from __future__ import annotations

import streamlit as st

from incident_suite import __version__
from incident_suite.config import load_config


def _status(label: str, connected: bool) -> str:
    return f"{label}: {'connected' if connected else 'not connected'}"


def main() -> None:
    """Render the page."""
    st.set_page_config(page_title="Incident Suite", page_icon=":rotating_light:", layout="wide")
    config = load_config()

    st.title("Incident Suite")
    st.caption(
        "Multi-agent DevOps incident analysis: upload an ops log, get fixes, a checklist, "
        f"a Slack summary and JIRA tickets. Version {__version__}."
    )
    st.info("Scaffold deploy. Log analysis arrives in the next builds.")

    with st.sidebar:
        st.subheader("Integrations")
        st.write(_status("Models (OpenRouter)", config.llm_configured))
        st.write(_status("Slack", config.slack_configured))
        st.write(_status("JIRA", config.jira_configured))
        st.write("Mode: live" if config.live_integrations else "Mode: dry run")


main()
