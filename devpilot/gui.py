"""Streamlit dashboard for the DevPilot mission workflow."""
from __future__ import annotations

from html import escape
from queue import Empty, Queue
import threading
from typing import Any

import streamlit as st

from devpilot.gui_bridge import Interaction, mission_stages, run_mission_worker
from devpilot.mission_state import load_mission
from devpilot.orchestrator import MAX_RETRIES
from devpilot.repository_tools import RepositoryTools

def _initialise_state() -> None:
    defaults = {
        "dp_active": False,
        "dp_events": None,
        "dp_pending": None,
        "dp_mission_id": None,
        "dp_result": None,
        "dp_error": None,
        "dp_thread": None,
        "dp_cancel_event": None,
    }
    for key, value in defaults.items():
        if key not in st.session_state:
            st.session_state[key] = value


def _answer(interaction: Interaction, value: Any) -> None:
    interaction.answer(value)
    st.session_state.dp_pending = None
    st.rerun(scope="fragment")


def _render_interaction(interaction: Interaction) -> None:
    details = interaction.details
    key = str(id(interaction))
    if interaction.kind == "patch":
        task = details["task"]
        st.subheader(f"Patch for {task.get('task_id', 'task')} · attempt {details['attempt'] + 1}")
        with st.form(f"patch-form-{key}"):
            patch = st.text_area("Unified diff", height=240, key=f"patch-{key}", placeholder="Paste a unified diff for a file in demo_repo/…")
            left, right = st.columns(2)
            submitted = left.form_submit_button("Submit patch", type="primary", use_container_width=True)
            rejected = right.form_submit_button("Cancel mission", use_container_width=True)
        if submitted:
            if not patch.strip():
                st.warning("Paste a unified diff before submitting.")
            else:
                _answer(interaction, patch)
        elif rejected:
            _answer(interaction, None)
        return

    stage = details["stage"]
    payload = details["details"]
    if stage == "task":
        st.subheader("Task approval requested")
        st.write(payload.get("description", "Approve this task?"))
        for criterion in payload.get("acceptance_criteria", []):
            st.markdown(f"- {criterion}")
        if payload.get("relevant_files"):
            st.caption("Relevant files: " + ", ".join(payload["relevant_files"]))
    else:
        st.subheader("Code diff approval")
        st.caption("Review the proposed diff. It will not be applied until you approve it.")
        st.code(payload["patch"], language="diff")
        try:
            preview = RepositoryTools().preview_patch(payload["patch"])
        except (ValueError, FileNotFoundError) as exc:
            st.error(f"Could not preview the proposed change: {exc}")
        else:
            st.caption(f"Target file: `{preview['path']}`")
            code_language = "python" if preview["path"].endswith(".py") else "text"
            before, after = st.columns(2)
            before.markdown("**Before**")
            before.code(preview["before"], language=code_language)
            after.markdown("**After**")
            after.code(preview["after"], language=code_language)
    left, right = st.columns(2)
    if left.button("Approve", key=f"approve-{key}", type="primary", use_container_width=True):
        _answer(interaction, True)
    if right.button("Reject", key=f"reject-{key}", use_container_width=True):
        _answer(interaction, False)


def _render_mission(mission: dict[str, Any]) -> None:
    stages = mission_stages(mission)
    cards = "".join(
        f'<article class="devpilot-stage-card"><span>{escape(label)}</span>'
        f'<strong>{escape(value)}</strong></article>'
        for label, value in stages.items()
    )
    st.markdown(f'<section class="devpilot-stage-grid">{cards}</section>', unsafe_allow_html=True)

    proposals = [item for item in mission.get("findings", []) if item.get("type") == "patch_proposal"]
    if proposals:
        with st.expander("Proposed code changes", expanded=True):
            for proposal in proposals:
                st.caption(f"{proposal.get('task_id')} · {proposal.get('status')}")
                st.code(proposal.get("patch", ""), language="diff")

    left, right = st.columns(2)
    with left:
        with st.expander("Test results", expanded=True):
            results = mission.get("test_results") or []
            if not results:
                st.caption("No test result has been recorded yet.")
            for result in results:
                label = "Passed" if result.get("passed") else "Failed"
                st.markdown(f"**{result.get('task_id')} · attempt {result.get('attempt')}: {label}**")
                if result.get("returncode") is not None:
                    st.caption(f"Exit code: {result['returncode']}")
                if result.get("stdout"):
                    st.code(result["stdout"], language="text")
                if result.get("stderr") or result.get("error"):
                    st.error(result.get("stderr") or result.get("error"))
    with right:
        with st.expander("Review findings", expanded=True):
            reviews = [item for item in mission.get("findings", []) if item.get("type") == "review"]
            if not reviews:
                st.caption("The Reviewer has not completed a review yet.")
            for review in reviews:
                st.markdown(f"**{review['task_id']} · {review['status']}**")
                if not review.get("findings"):
                    st.caption("No findings")
                for finding in review.get("findings", []):
                    st.warning(f"{finding['category']}/{finding['code']}: {finding['message']}")

    with st.expander("Mission activity", expanded=True):
        for entry in reversed(mission.get("activity_log", [])):
            details = " · ".join(
                f"{key}: {value}" for key, value in entry.items()
                if key not in {"event", "timestamp", "mission_id"}
            )
            st.markdown(f"`{entry.get('event', 'event')}`" + (f" — {details}" if details else ""))


@st.fragment(run_every=1)
def _live_panel() -> None:
    events: Queue | None = st.session_state.dp_events
    if events is not None:
        while True:
            try:
                event = events.get_nowait()
            except Empty:
                break
            kind = event["kind"]
            if kind == "interaction":
                st.session_state.dp_pending = event["interaction"]
            elif kind == "activity":
                st.session_state.dp_mission_id = event["entry"].get("mission_id")
            elif kind == "result":
                st.session_state.dp_result = event["mission"]
                st.session_state.dp_active = False
                st.session_state.dp_pending = None
            elif kind == "error":
                st.session_state.dp_error = event["message"]
                st.session_state.dp_active = False
                st.session_state.dp_pending = None

    mission = st.session_state.dp_result
    mission_id = st.session_state.dp_mission_id
    if mission_id:
        mission = load_mission(mission_id)
    if mission:
        _render_mission(mission)
    elif st.session_state.dp_active:
        st.info("Starting mission and planning the request…")
    if st.session_state.dp_error:
        st.error(st.session_state.dp_error)
    if st.session_state.dp_pending:
        st.divider()
        _render_interaction(st.session_state.dp_pending)


def main() -> None:
    st.set_page_config(page_title="DevPilot", page_icon="◈", layout="wide")
    _initialise_state()
    st.markdown("""
    <style>
    .stApp { background: #EDF1F8; color: #1F2A44; }
    .block-container { max-width: 1160px; padding-top: 2.5rem; }
    .devpilot-hero { padding: 1.55rem 1.8rem; border-radius: 24px; background: #EDF1F8;
        box-shadow: 13px 13px 26px #CCD2DE, -13px -13px 26px #FFFFFF; margin-bottom: 1.5rem; }
    .devpilot-kicker { color: #5B67D6; font-size: .75rem; font-weight: 700; letter-spacing: .12em; }
    .devpilot-hero h1 { margin: .25rem 0; color: #1F2A44; font-size: 2.25rem; letter-spacing: -.04em; }
    .devpilot-hero p { margin: 0; color: #65708A; }
    .devpilot-stage-grid { display:grid; grid-template-columns:repeat(5, minmax(0, 1fr));
        gap:14px; margin:0 0 1rem; }
    .devpilot-stage-card { display:flex; flex-direction:column; gap:7px; padding:14px 16px;
        border-radius:16px; background:#EDF1F8; box-shadow:7px 7px 14px #CCD2DE, -7px -7px 14px #FFFFFF; }
    .devpilot-stage-card span { color:#65708A; font-size:.8rem; }
    .devpilot-stage-card strong { color:#1F2A44; font-size:1.15rem; overflow-wrap:anywhere; }
    @media (max-width: 760px) { .devpilot-stage-grid { grid-template-columns:repeat(2, minmax(0, 1fr)); } }
    div[data-testid="stForm"] { background:#EDF1F8; border:0; padding:20px; border-radius:20px;
        box-shadow:inset 4px 4px 9px #D1D7E3, inset -4px -4px 9px #FFFFFF; }
    .stTextArea textarea { background:#F6F8FC; border:0; border-radius:12px;
        box-shadow:inset 3px 3px 6px #D6DCE7, inset -3px -3px 6px #FFFFFF; }
    .stButton > button, .stFormSubmitButton > button { border:0; border-radius:12px; font-weight:650;
        box-shadow:5px 5px 10px #CCD2DE, -5px -5px 10px #FFFFFF; }
    .stButton > button:hover, .stFormSubmitButton > button:hover { color:#4653C5; border:0;
        box-shadow:inset 3px 3px 6px #D6DCE7, inset -3px -3px 6px #FFFFFF; }
    .stExpander { background:#EDF1F8; border:0; border-radius:16px;
        box-shadow:6px 6px 12px #D0D6E2, -6px -6px 12px #FFFFFF; }
    </style>
    """, unsafe_allow_html=True)
    st.markdown("""
    <section class="devpilot-hero">
      <div class="devpilot-kicker">PECATHON · PS-001</div>
      <h1>◈ DevPilot</h1>
      <p>Approval-gated coding missions · repository access stays inside demo_repo/</p>
    </section>
    """, unsafe_allow_html=True)
    if st.session_state.dp_mission_id:
        st.caption(f"Mission ID: {st.session_state.dp_mission_id}")

    with st.form("mission-start"):
        request = st.text_area("Mission request", placeholder="Describe the code change to make in demo_repo/…", disabled=st.session_state.dp_active)
        max_retries = st.number_input(
            "Recovery retries",
            min_value=0,
            max_value=MAX_RETRIES,
            value=1,
            step=1,
            help="Maximum correction attempts after the first patch.",
            disabled=st.session_state.dp_active,
        )
        started = st.form_submit_button("Start Mission", type="primary", disabled=st.session_state.dp_active)
    if started and request.strip():
        events: Queue = Queue()
        st.session_state.dp_events = events
        st.session_state.dp_pending = None
        st.session_state.dp_mission_id = None
        st.session_state.dp_result = None
        st.session_state.dp_error = None
        cancel_event = threading.Event()
        st.session_state.dp_cancel_event = cancel_event
        st.session_state.dp_active = True
        thread = threading.Thread(
            target=run_mission_worker,
            args=(request.strip(), events),
            kwargs={"max_retries": int(max_retries), "cancel_event": cancel_event},
            daemon=True,
        )
        st.session_state.dp_thread = thread
        thread.start()
        st.rerun()
    elif started:
        st.warning("Enter a mission request first.")

    if st.session_state.dp_active and st.button("Cancel Mission", key="cancel-mission"):
        cancel_event = st.session_state.dp_cancel_event
        if cancel_event is not None:
            cancel_event.set()
        pending = st.session_state.dp_pending
        if pending is not None:
            pending.answer(None)
            st.session_state.dp_pending = None
        st.info("Cancellation requested. The active operation will stop at its next safe checkpoint.")

    if st.session_state.dp_active or st.session_state.dp_result or st.session_state.dp_error:
        _live_panel()


main()
