"""Premium Streamlit workspace for the DevPilot mission workflow."""
from __future__ import annotations

from datetime import datetime
from html import escape
from pathlib import Path
from queue import Empty, Queue
import threading
from typing import Any

import streamlit as st

from devpilot.config import load_gemini_config
from devpilot.firestore_store import (
    FirestoreError,
    configured_project_id,
    test_firestore_connection,
)
from devpilot.gui_bridge import Interaction, mission_stages, run_mission_worker
from devpilot.llm_bridge import ask_gemini
from devpilot.mission_state import (
    DEFAULT_STATE_DIR,
    is_firestore_backend,
    list_missions,
    load_mission,
    recover_stale_missions,
)
from devpilot.orchestrator import MAX_RETRIES
from devpilot.repository_tools import RepositoryTools

NAV_ITEMS = (
    "Command Center",
    "Mission Scrolls",
    "Shadow Coder",
    "ANBU Review",
    "Training Grounds",
    "Intelligence Reports",
    "Village Settings",
)
NAV_GLYPHS = {
    "Command Center": "⌂",
    "Mission Scrolls": "▤",
    "Shadow Coder": "⌘",
    "ANBU Review": "◉",
    "Training Grounds": "△",
    "Intelligence Reports": "≋",
    "Village Settings": "◌",
}
QUICK_STARTS = (
    ("Add a feature", "Add a small feature to the existing repository. Preserve current behavior and add tests."),
    ("Fix a bug", "Investigate and fix the bug described below using the existing code and tests: "),
    ("Improve tests", "Identify a meaningful missing test for the existing behavior and add it."),
)


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
        "dp_selected_mission": None,
        "dp_nav": "Command Center",
        "dp_nav_radio": "Command Center",
        "dp_request": "",
        "dp_gemini_connection": "Not tested",
        "dp_gemini_error": None,
        "dp_firestore_connection": "Not tested",
        "dp_firestore_error": None,
    }
    for key, value in defaults.items():
        if key not in st.session_state:
            st.session_state[key] = value
    if st.session_state.dp_nav not in NAV_ITEMS:
        st.session_state.dp_nav = "Command Center"
    if st.session_state.dp_nav_radio not in NAV_ITEMS:
        st.session_state.dp_nav_radio = st.session_state.dp_nav


def _set_request(value: str) -> None:
    st.session_state.dp_request = value


def _select_mission(mission_id: str) -> None:
    st.session_state.dp_selected_mission = mission_id
    st.session_state.dp_nav = "Mission Scrolls"
    st.session_state.dp_nav_radio = "Mission Scrolls"


def _navigate(destination: str) -> None:
    if destination in NAV_ITEMS:
        st.session_state.dp_nav = destination
        st.session_state.dp_nav_radio = destination


def _load_recent_missions(limit: int | None = None) -> tuple[list[dict[str, Any]], int]:
    if is_firestore_backend():
        return list_missions(limit=limit), 0

    missions = []
    corrupt_count = 0
    if not DEFAULT_STATE_DIR.exists():
        return missions, corrupt_count
    for path in DEFAULT_STATE_DIR.glob("*.json"):
        try:
            mission = load_mission(path.stem)
        except (OSError, ValueError):
            corrupt_count += 1
            continue
        if mission is not None:
            missions.append(mission)
    missions.sort(
        key=lambda item: (
            (item.get("activity_log") or [{}])[-1].get("timestamp", ""),
            item.get("mission_id", ""),
        ),
        reverse=True,
    )
    return (missions[:limit] if limit is not None else missions), corrupt_count


def _status_class(status: Any) -> str:
    normalized = str(status or "unknown").casefold().replace(" ", "-")
    known = {
        "completed", "running", "failed", "cancelled", "pending",
        "queued", "implementing", "reviewing", "awaiting-approval",
    }
    return normalized if normalized in known else "unknown"


def _format_timestamp(value: Any) -> str:
    if not isinstance(value, str) or not value:
        return "Time unavailable"
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone().strftime("%b %d · %H:%M")
    except ValueError:
        return "Time unavailable"


def _mission_time(mission: dict[str, Any]) -> str:
    log = mission.get("activity_log") or []
    return _format_timestamp(log[-1].get("timestamp")) if log else "Time unavailable"


def _metric_card(label: str, value: str | int, detail: str, tone: str = "") -> str:
    return (
        f'<article class="dp-metric {escape(tone)}"><div class="dp-metric-label">{escape(label)}</div>'
        f'<div class="dp-metric-value">{escape(str(value))}</div>'
        f'<div class="dp-metric-detail">{escape(detail)}</div></article>'
    )


def _render_status(status: Any) -> None:
    safe_status = escape(str(status or "unknown").replace("_", " ").title())
    st.markdown(
        f'<span class="dp-status dp-status-{_status_class(status)}">'
        f'<span class="dp-status-dot"></span>{safe_status}</span>',
        unsafe_allow_html=True,
    )


def _render_mission_list(missions: list[dict[str, Any]], *, compact: bool = False) -> None:
    if not missions:
        st.markdown(
            '<div class="dp-empty"><div class="dp-empty-mark">⌁</div>'
            '<strong>No missions yet</strong><span>Start a mission to see its plan, changes, and evidence here.</span></div>',
            unsafe_allow_html=True,
        )
        return
    for mission in missions:
        mission_id = str(mission.get("mission_id", ""))
        request = str(mission.get("user_request", "Untitled mission"))
        cols = st.columns([6, 1.4, 1.2] if not compact else [6, 1.6])
        with cols[0]:
            st.markdown(
                f'<div class="dp-mission-row"><div class="dp-mission-title">{escape(request[:112])}</div>'
                f'<div class="dp-mission-meta"><span>{escape(mission_id[:8])}</span>'
                f'<span>{escape(_mission_time(mission))}</span>'
                f'<span>{len(mission.get("tasks", []))} task(s)</span></div></div>',
                unsafe_allow_html=True,
            )
        with cols[1]:
            _render_status(mission.get("status"))
        if not compact:
            with cols[2]:
                st.button(
                    "Open",
                    key=f"open-mission-{mission_id}",
                    on_click=_select_mission,
                    args=(mission_id,),
                    use_container_width=True,
                )


def _render_interaction(interaction: Interaction) -> None:
    details = interaction.details
    key = str(id(interaction))
    with st.container(border=True):
        if interaction.kind == "clarification":
            st.markdown("#### FIELD CLARIFICATION")
            st.write(details["question"])
            with st.form(f"clarification-form-{key}"):
                answer = st.text_area("Clarification", key=f"clarification-{key}")
                left, right = st.columns(2)
                submitted = left.form_submit_button("Continue planning", type="primary", use_container_width=True)
                cancelled = right.form_submit_button("Cancel mission", use_container_width=True)
            if submitted:
                if answer.strip():
                    _answer(interaction, answer.strip())
                else:
                    st.warning("Enter a clarification or cancel the mission.")
            elif cancelled:
                _answer(interaction, None)
            return

        if interaction.kind == "patch":
            task = details["task"]
            st.markdown(f"#### SHADOW CODER · PATCH SCROLL · {escape(str(task.get('task_id', 'task')))}")
            st.caption("Provide a unified diff. It will be reviewed and tested after submission.")
            with st.form(f"patch-form-{key}"):
                patch = st.text_area(
                    "Unified diff",
                    height=240,
                    key=f"patch-{key}",
                    placeholder="--- a/sample.py\n+++ b/sample.py\n@@ ...",
                )
                left, right = st.columns(2)
                submitted = left.form_submit_button("Submit patch", type="primary", use_container_width=True)
                rejected = right.form_submit_button("Cancel mission", use_container_width=True)
            if submitted:
                if patch.strip():
                    _answer(interaction, patch)
                else:
                    st.warning("Paste a unified diff before submitting.")
            elif rejected:
                _answer(interaction, None)
            return

        stage = details["stage"]
        payload = details["details"]
        if stage == "task":
            st.markdown("#### MISSION DOSSIER · TASK AUTHORIZATION")
            st.write(payload.get("description", "Approve this task?"))
            for criterion in payload.get("acceptance_criteria", []):
                st.markdown(f"- {criterion}")
            if payload.get("relevant_files"):
                st.caption("Relevant files · " + ", ".join(payload["relevant_files"]))
        else:
            st.markdown("#### HOKAGE AUTHORIZATION")
            st.caption("Inspect the proposed change. It remains unapplied until you explicitly authorize it.")
            path, additions, deletions = _patch_summary(payload["patch"])
            summary_cols = st.columns([4, 1, 1])
            summary_cols[0].markdown(f"**File** · `{escape(path)}`")
            summary_cols[1].markdown(f"**Added** · <span class='sh-add-count'>+{additions}</span>", unsafe_allow_html=True)
            summary_cols[2].markdown(f"**Removed** · <span class='sh-del-count'>−{deletions}</span>", unsafe_allow_html=True)
            st.code(payload["patch"], language="diff", line_numbers=True)
            try:
                preview = RepositoryTools().preview_patch(payload["patch"])
            except (ValueError, FileNotFoundError) as exc:
                st.error(f"Could not preview the proposed change: {exc}")
            else:
                st.caption(f"Target · {preview['path']}")
                left_code, right_code = st.columns(2)
                code_language = "python" if preview["path"].endswith(".py") else "text"
                with left_code:
                    st.markdown("**Before**")
                    st.code(preview["before"] or "(new file)", language=code_language, line_numbers=True)
                with right_code:
                    st.markdown("**After**")
                    st.code(preview["after"], language=code_language, line_numbers=True)
        acknowledged = st.checkbox(
            "I reviewed this mission action and understand what will happen.",
            key=f"acknowledge-{key}",
        )
        left, right = st.columns(2)
        if left.button(
            "Authorize mission action",
            key=f"approve-{key}",
            type="primary",
            disabled=not acknowledged,
            use_container_width=True,
        ):
            _answer(interaction, True)
        if right.button("Reject and stop mission", key=f"reject-{key}", use_container_width=True):
            _answer(interaction, False)


def _patch_summary(patch: str) -> tuple[str, int, int]:
    path = "New or modified file"
    additions = deletions = 0
    for line in patch.splitlines():
        if line.startswith("+++ "):
            path = line[4:].removeprefix("b/").strip()
        elif line.startswith("+") and not line.startswith("+++"):
            additions += 1
        elif line.startswith("-") and not line.startswith("---"):
            deletions += 1
    return path, additions, deletions


def _answer(interaction: Interaction, value: Any) -> None:
    interaction.answer(value)
    st.session_state.dp_pending = None
    st.rerun()


def _render_task_plan(mission: dict[str, Any]) -> None:
    tasks = mission.get("tasks", [])
    findings = mission.get("findings", [])
    approvals = {
        item.get("task_id")
        for item in findings
        if item.get("type") == "task_approval" and item.get("approved")
    }
    proposals = [item for item in findings if item.get("type") == "patch_proposal"]
    reviews = [item for item in findings if item.get("type") == "review"]
    results = mission.get("test_results") or []
    st.markdown("### Mission plan")
    if not tasks:
        st.caption("Planning is in progress. Tasks will appear here when the plan is saved.")
        return
    latest_by_task = {result.get("task_id"): result for result in results}
    completed = sum(
        bool(latest_by_task.get(task.get("task_id"), {}).get("passed"))
        for task in tasks
    )
    progress = int(100 * completed / len(tasks)) if tasks else 0
    st.markdown(
        f'<div class="dp-progress-head"><span>{completed} of {len(tasks)} tasks verified</span>'
        f'<span>{progress}%</span></div><div class="dp-progress"><span style="width:{progress}%"></span></div>',
        unsafe_allow_html=True,
    )
    for index, task in enumerate(tasks, start=1):
        task_id = task.get("task_id", f"task-{index}")
        task_proposals = [p for p in proposals if p.get("task_id") == task_id]
        latest_proposal = task_proposals[-1] if task_proposals else None
        task_reviews = [r for r in reviews if r.get("task_id") == task_id]
        task_results = [r for r in results if r.get("task_id") == task_id]
        if task_results and task_results[-1].get("passed"):
            status = "completed"
        elif task_results and not task_results[-1].get("passed"):
            status = "failed"
        elif mission.get("status") == "cancelled":
            status = "cancelled"
        elif latest_proposal and latest_proposal.get("status") == "applied":
            status = "reviewing"
        elif latest_proposal:
            status = "awaiting approval"
        elif task_id in approvals:
            status = "implementing"
        else:
            status = "queued"
        with st.expander(f"{index:02d}  /  {task.get('description', task_id)}", expanded=index == 1):
            _render_status(status)
            retry_count = max(0, len(task_results) - 1)
            st.caption(
                f"Strategist order · {index} of {len(tasks)}"
                f"   /   Retry count · {retry_count}"
                "   /   Dependencies · not recorded by planner"
            )
            criteria = task.get("acceptance_criteria", [])
            if criteria:
                st.markdown("**Acceptance criteria**")
                for criterion in criteria:
                    st.markdown(f"- {criterion}")
            files = task.get("relevant_files", [])
            if files:
                st.caption("Relevant files · " + ", ".join(files))
            if latest_proposal:
                st.caption(f"Latest proposal · {latest_proposal.get('status', 'unknown')}")
                st.code(latest_proposal.get("patch", ""), language="diff", line_numbers=True)
            if task_reviews:
                st.caption(f"Independent review · {task_reviews[-1].get('status', 'unknown')}")
            if task_results and task_results[-1].get("error"):
                st.error(task_results[-1]["error"])
            elif task_results and task_results[-1].get("stderr") and not task_results[-1].get("passed"):
                st.error(task_results[-1]["stderr"])


def _render_diff_review(mission: dict[str, Any]) -> None:
    reviews = [item for item in mission.get("findings", []) if item.get("type") == "review"]
    proposals = [item for item in mission.get("findings", []) if item.get("type") == "patch_proposal"]
    if not proposals:
        st.markdown(
            '<div class="dp-empty"><strong>No proposed code yet</strong>'
            '<span>Changes appear after the task is approved and a patch is generated.</span></div>',
            unsafe_allow_html=True,
        )
    for proposal in reversed(proposals):
        with st.container(border=True):
            st.markdown(f"#### {escape(str(proposal.get('task_id', 'Task')))} · patch")
            st.caption(f"Proposal {str(proposal.get('proposal_id', ''))[:8]} · {proposal.get('status', 'unknown')}")
            st.code(proposal.get("patch", ""), language="diff", line_numbers=True)
            try:
                preview = RepositoryTools().preview_patch(proposal.get("patch", ""))
            except (ValueError, FileNotFoundError) as exc:
                st.caption(f"Before/after preview unavailable: {escape(str(exc))}")
            else:
                before, after = st.columns(2)
                code_language = "python" if preview["path"].casefold().endswith(".py") else "text"
                with before:
                    st.markdown("**Before**")
                    st.code(preview["before"] or "(new file)", language=code_language, line_numbers=True)
                with after:
                    st.markdown("**After**")
                    st.code(preview["after"], language=code_language, line_numbers=True)
    st.markdown("### Independent review")
    if not reviews:
        st.caption("No review findings have been recorded for this mission yet.")
    for review in reversed(reviews):
        with st.container(border=True):
            st.markdown(f"**{review.get('task_id')} · {str(review.get('status', 'unknown')).title()}**")
            if not review.get("findings"):
                st.success("No findings recorded")
            for finding in review.get("findings", []):
                severity = str(finding.get("severity", "info")).casefold()
                st.markdown(
                    f'<div class="dp-finding dp-finding-{escape(severity)}">'
                    f'<strong>{escape(severity.upper())}</strong>'
                    f'<span>{escape(str(finding.get("category", "review")))} / '
                    f'{escape(str(finding.get("code", "finding")))}</span>'
                    f'<p>{escape(str(finding.get("message", "")))}</p></div>',
                    unsafe_allow_html=True,
                )


def _render_test_results(mission: dict[str, Any]) -> None:
    results = mission.get("test_results") or []
    if not results:
        st.markdown(
            '<div class="dp-empty"><strong>No test runs yet</strong>'
            '<span>Test results and captured output will be recorded here after an approved patch.</span></div>',
            unsafe_allow_html=True,
        )
        return
    for result in reversed(results):
        passed = bool(result.get("passed"))
        with st.container(border=True):
            top = st.columns([5, 1])
            with top[0]:
                st.markdown(
                    f"#### {result.get('task_id', 'Task')} · attempt {result.get('attempt', '?')}"
                )
            with top[1]:
                _render_status("completed" if passed else "failed")
            if result.get("command"):
                st.caption("Command · Python pytest in an isolated temporary repository copy")
            if result.get("returncode") is not None:
                st.caption(f"Exit code · {result['returncode']}")
            if result.get("timed_out"):
                st.warning("The test command exceeded its time limit.")
            if result.get("error"):
                st.error(result["error"])
            if result.get("stdout"):
                with st.expander("Standard output", expanded=not passed):
                    st.code(result["stdout"], language="text")
            if result.get("stderr"):
                with st.expander("Standard error", expanded=True):
                    st.code(result["stderr"], language="text")


def _render_activity(mission: dict[str, Any]) -> None:
    entries = mission.get("activity_log", [])
    if not entries:
        st.caption("No activity has been recorded.")
        return
    for item in reversed(entries):
        event = str(item.get("event", "activity")).replace("_", " ").title()
        timestamp = _format_timestamp(item.get("timestamp"))
        detail = " · ".join(
            f"{key.replace('_', ' ')}: {value}"
            for key, value in item.items()
            if key not in {"event", "timestamp", "mission_id"}
        )
        st.markdown(
            f'<div class="dp-activity"><span class="dp-activity-mark"></span>'
            f'<div><strong>{escape(event)}</strong><small>{escape(timestamp)}</small>'
            f'<p>{escape(detail)}</p></div></div>',
            unsafe_allow_html=True,
        )


def _render_mission_workspace(mission: dict[str, Any], *, view: str = "Missions") -> None:
    status = str(mission.get("status", "unknown"))
    tasks = mission.get("tasks", [])
    task_count = len(tasks)
    mission_id = str(mission.get("mission_id", ""))
    results = mission.get("test_results") or []
    latest_results = {
        result.get("task_id"): result
        for result in results
    }
    verified_count = sum(
        bool(latest_results.get(task.get("task_id"), {}).get("passed"))
        for task in tasks
    )
    progress_angle = round(360 * verified_count / task_count) if task_count else 0
    role_stages = mission_stages(mission)
    completion_class = "dp-mission-complete" if status == "completed" else ""
    st.markdown(
        f'<div class="dp-workspace-head {completion_class}"><div><div class="dp-eyebrow">MISSION DOSSIER · {escape(mission_id[:8])}</div>'
        f'<h2>{escape(str(mission.get("user_request", "Mission"))[:160])}</h2></div>'
        f'<div class="dp-seal-progress" style="--progress-angle:{progress_angle}deg" role="img" '
        f'aria-label="{verified_count} of {task_count} tasks verified"><span>{verified_count}<small>/{task_count}</small></span></div></div>',
        unsafe_allow_html=True,
    )
    role_labels = (
        ("Planner", "MISSION STRATEGIST", "strategist"),
        ("Developer", "SHADOW CODER", "coder"),
        ("Reviewer", "ANBU INSPECTOR", "inspector"),
        ("Tests", "TRAINING GROUNDS", "training"),
    )
    role_cards = "".join(
        f'<article class="dp-agent dp-agent-{css_role}"><small>{escape(label)}</small>'
        f'<strong>{escape(role_stages.get(stage, "Waiting"))}</strong></article>'
        for stage, label, css_role in role_labels
    )
    st.markdown(f'<section class="dp-agent-grid" aria-label="Agent status">{role_cards}</section>', unsafe_allow_html=True)
    if status == "failed":
        failures = [
            item for item in mission.get("activity_log", [])
            if item.get("event") in {"patch_generation_failed", "mission_failed"}
        ]
        if failures:
            failure = failures[-1]
            st.error(
                failure.get("error")
                or failure.get("reason")
                or "Mission failed; inspect test and review evidence below."
            )
    cols = st.columns([1.2, 1, 1, 1])
    with cols[0]:
        st.markdown("**Current state**")
        _render_status(status)
    with cols[1]:
        st.markdown("**Dossier tasks**")
        st.markdown(f'<div class="dp-inline-stat">{task_count}</div>', unsafe_allow_html=True)
    with cols[2]:
        st.markdown("**Recovery retries**")
        st.markdown(f'<div class="dp-inline-stat">{int(mission.get("retry_count", 0))}</div>', unsafe_allow_html=True)
    with cols[3]:
        st.markdown("**Last update**")
        st.caption(_mission_time(mission))

    if view in {"ANBU Review", "Code Review"}:
        _render_diff_review(mission)
    elif view in {"Training Grounds", "Test Results"}:
        _render_test_results(mission)
    elif view in {"Intelligence Reports", "Activity Log"}:
        _render_activity(mission)
    elif view == "Shadow Coder":
        _render_diff_review(mission)
    else:
        tab_plan, tab_changes, tab_tests, tab_review, tab_activity = st.tabs(
            ["Mission scroll", "Shadow coder", "Training grounds", "ANBU review", "Field intelligence"]
        )
        with tab_plan:
            _render_task_plan(mission)
        with tab_changes:
            _render_diff_review(mission)
        with tab_tests:
            _render_test_results(mission)
        with tab_review:
            reviews_only = {
                **mission,
                "findings": [item for item in mission.get("findings", []) if item.get("type") in {"review", "patch_proposal"}],
            }
            _render_diff_review(reviews_only)
        with tab_activity:
            _render_activity(mission)


def _render_metric_grid(missions: list[dict[str, Any]], pending_approval: bool) -> None:
    completed = sum(mission.get("status") == "completed" for mission in missions)
    failed = sum(mission.get("status") == "failed" for mission in missions)
    test_runs = sum(len(mission.get("test_results") or []) for mission in missions)
    passed = sum(
        bool(result.get("passed"))
        for mission in missions
        for result in (mission.get("test_results") or [])
    )
    test_summary = f"{passed}/{test_runs} passed" if test_runs else "No test runs"
    cards = "".join(
        [
            _metric_card("Completed missions", completed, "Persisted mission records", "violet"),
            _metric_card("Hokage approvals", "1" if pending_approval else "0", "Current session", "cyan"),
            _metric_card("Test runs", test_runs, test_summary, "green"),
            _metric_card("Failed missions", failed, "Persisted mission records", "red"),
        ]
    )
    st.markdown(f'<section class="dp-metrics">{cards}</section>', unsafe_allow_html=True)


def _render_dashboard(missions: list[dict[str, Any]]) -> None:
    st.markdown(
        '<section class="dp-hero"><div class="dp-hero-copy">'
        '<div class="dp-eyebrow">SHINOBI TECH  /  FIELD OPERATIONS</div>'
        '<h1>DEVPILOT</h1><div class="dp-hero-subtitle">THE HIDDEN LEAF COMMAND CENTER</div>'
        '<p>Plan with intent. Review every change. Verify before you ship.</p>'
        '</div><div class="dp-crest" aria-label="Original DevPilot command insignia" role="img">'
        '<span class="dp-crest-ring"></span><span class="dp-crest-sun"></span>'
        '<span class="dp-crest-torii-top"></span><span class="dp-crest-torii-mid"></span>'
        '<span class="dp-crest-pillar dp-crest-left"></span><span class="dp-crest-pillar dp-crest-right"></span>'
        '<span class="dp-crest-base"></span></div>'
        '<div class="dp-hero-rule"><span>MISSION CONTROL</span><i></i><span>LOCAL REPOSITORY · DEMO_REPO</span></div></section>',
        unsafe_allow_html=True,
    )
    st.markdown("### Mission briefing")
    for index, (label, text) in enumerate(QUICK_STARTS):
        st.button(
            f"{index + 1:02d}  /  {label}",
            key=f"quick-start-{index}",
            on_click=_set_request,
            args=(text,),
            help="Populate the briefing with this starting prompt.",
        )
    with st.form("mission-start", border=False):
        request = st.text_area(
            "Mission request",
            key="dp_request",
            height=130,
            placeholder="State the objective, expected behavior, and constraints for this mission…",
            disabled=st.session_state.dp_active,
            help="The strategist will inspect demo_repo, prepare a plan, and ask before any proposed code is applied.",
        )
        controls = st.columns([1, 1, 2.5])
        with controls[0]:
            max_retries = st.number_input(
                "Recovery attempts",
                min_value=0,
                max_value=MAX_RETRIES,
                value=1,
                step=1,
                help="Maximum correction attempts after the first patch.",
                disabled=st.session_state.dp_active,
            )
        with controls[1]:
            use_llm = st.checkbox(
                "AI-generated patch",
                value=False,
                help="Uses Google Gemini. Each task and patch still requires your approval.",
                disabled=st.session_state.dp_active,
            )
        with controls[2]:
            st.markdown('<div class="dp-repo-label">Mission boundary</div><div class="dp-repo-value">demo_repo  /  local village</div>', unsafe_allow_html=True)
        started = st.form_submit_button(
            "Start Mission",
            type="primary",
            disabled=st.session_state.dp_active,
            use_container_width=True,
        )
    if started and request.strip():
        _start_mission(request.strip(), int(max_retries), bool(use_llm))
    elif started:
        st.warning("Write a mission briefing before dispatch.")

    pending = st.session_state.dp_pending
    _render_metric_grid(missions, bool(pending and pending.kind == "approval"))
    mission_id = st.session_state.dp_mission_id or st.session_state.dp_selected_mission
    mission = load_mission(mission_id) if mission_id else st.session_state.dp_result
    if mission:
        _render_mission_workspace(mission, view="Command Center")
    elif st.session_state.dp_active:
        st.markdown('<div class="dp-loading"><span class="dp-spinner"></span>Planning your mission…</div>', unsafe_allow_html=True)
    if st.session_state.dp_error:
        st.error(st.session_state.dp_error)
    st.markdown("### Recent mission scrolls")
    _render_mission_list(missions[:5])


def _render_pending_interaction() -> None:
    pending = st.session_state.dp_pending
    if pending is not None:
        st.markdown("### Action required")
        _render_interaction(pending)


def _render_mission_list_page(missions: list[dict[str, Any]]) -> None:
    st.markdown('<div class="dp-eyebrow">ARCHIVE OF COMPLETED AND ACTIVE OPERATIONS</div><h1 class="dp-page-title">Mission Scrolls</h1>', unsafe_allow_html=True)
    st.caption("Open a persisted mission to inspect its plan, code changes, review, and test evidence.")
    _render_mission_list(missions)
    selected_id = st.session_state.dp_selected_mission or st.session_state.dp_mission_id
    mission = load_mission(selected_id) if selected_id else None
    if mission:
        st.divider()
        _render_mission_workspace(mission)
    elif st.session_state.dp_active:
        st.info("A mission is running. Open Command Center to see live progress and authorization requests.")


def _render_detail_page(view: str, missions: list[dict[str, Any]]) -> None:
    st.markdown(f'<div class="dp-eyebrow">FIELD INTELLIGENCE · SELECTED MISSION</div><h1 class="dp-page-title">{escape(view)}</h1>', unsafe_allow_html=True)
    selected_id = st.session_state.dp_selected_mission or st.session_state.dp_mission_id
    mission = load_mission(selected_id) if selected_id else None
    if mission is None and missions:
        mission = missions[0]
        st.session_state.dp_selected_mission = mission["mission_id"]
    if mission is None:
        st.markdown('<div class="dp-empty"><strong>No mission evidence available</strong><span>Start a mission to populate this view with actual persisted results.</span></div>', unsafe_allow_html=True)
        return
    options = [
        f"{item.get('mission_id', '')[:8]} · {str(item.get('user_request', 'Mission'))[:56]}"
        for item in missions
    ]
    if options:
        ids = [item.get("mission_id") for item in missions]
        current_index = ids.index(mission["mission_id"]) if mission["mission_id"] in ids else 0
        selected_label = st.selectbox("Mission", options, index=current_index, label_visibility="collapsed")
        selected_index = options.index(selected_label)
        mission = missions[selected_index]
        st.session_state.dp_selected_mission = mission["mission_id"]
    _render_mission_workspace(mission, view=view)


def _render_settings() -> None:
    st.markdown('<div class="dp-eyebrow">VILLAGE OPERATIONS</div><h1 class="dp-page-title">Village Settings</h1>', unsafe_allow_html=True)
    st.markdown("### Gemini configuration")
    config = load_gemini_config()
    if st.button("Test Gemini connection", key="test-gemini-connection"):
        try:
            ask_gemini("Reply with exactly: OK", timeout=20)
        except (RuntimeError, ValueError) as exc:
            st.session_state.dp_gemini_connection = "Failed"
            st.session_state.dp_gemini_error = str(exc)
        else:
            st.session_state.dp_gemini_connection = "Connected"
            st.session_state.dp_gemini_error = None

    st.markdown(
        f'<div class="dp-settings-grid">'
        f'<article><small>Village repository</small><strong>demo_repo only</strong><span>Changes and test execution stay inside the configured local repository.</span></article>'
        f'<article><small>Provider</small><strong>Google Gemini</strong><span>All model requests use this provider.</span></article>'
        f'<article><small>API key</small><strong>{"Configured" if config.key_configured else "Missing"}</strong><span>Secret value is never displayed.</span></article>'
        f'<article><small>Selected model</small><strong>{escape(config.model)}</strong><span>Configured with GEMINI_MODEL or the application default.</span></article>'
        f'<article><small>Connection</small><strong>{escape(st.session_state.dp_gemini_connection)}</strong><span>Connection status reflects an explicit test, not key presence.</span></article>'
        f'<article><small>Human approval</small><strong>Always required</strong><span>Each task and proposed patch is reviewed before application.</span></article>'
        f'</div>',
        unsafe_allow_html=True,
    )
    if st.session_state.dp_gemini_error:
        st.error(st.session_state.dp_gemini_error)
    if not config.key_configured:
        st.info("Set GEMINI_API_KEY in your process environment or project .env file. See .env.example.")
    st.caption("To change Gemini configuration, update the server environment or project .env file and restart Streamlit.")
    st.divider()
    st.markdown("### Mission persistence")
    if st.button("Test Firestore connection", key="test-firestore-connection"):
        try:
            test_firestore_connection()
        except FirestoreError as exc:
            st.session_state.dp_firestore_connection = "Failed"
            st.session_state.dp_firestore_error = str(exc)
        else:
            st.session_state.dp_firestore_connection = "Connected"
            st.session_state.dp_firestore_error = None

    try:
        firestore_enabled = is_firestore_backend()
    except FirestoreError as exc:
        firestore_enabled = False
        st.error(str(exc))
        storage_name = "Invalid configuration"
    else:
        storage_name = "Cloud Firestore" if firestore_enabled else "Local JSON"
    st.markdown(
        f'<div class="dp-settings-grid">'
        f'<article><small>Storage backend</small><strong>{storage_name}</strong><span>Backend failures are reported; Firestore does not fall back to local files.</span></article>'
        f'<article><small>Project</small><strong>{escape(configured_project_id())}</strong><span>Firebase Admin SDK · Cloud Firestore only.</span></article>'
        f'<article><small>Connection</small><strong>{escape(st.session_state.dp_firestore_connection)}</strong><span>Connection status reflects an explicit check.</span></article>'
        f'<article><small>Credentials</small><strong>Never displayed</strong><span>Configure a service account in Streamlit secrets or environment variables.</span></article>'
        f'</div>',
        unsafe_allow_html=True,
    )
    if st.session_state.dp_firestore_error:
        st.error(st.session_state.dp_firestore_error)
    if firestore_enabled:
        st.caption("Configure [firebase] in .streamlit/secrets.toml or set GOOGLE_APPLICATION_CREDENTIALS, then restart Streamlit.")
    else:
        st.info("Local JSON storage is explicitly selected with DEVPILOT_STORAGE_BACKEND=json.")
    with st.expander("Safety and execution"):
        st.markdown(
            "- Patch paths are confined to the selected repository root.\n"
            "- Test commands run against a temporary copy with pytest hooks and plugin autoload disabled.\n"
            "- The test runner is not an operating-system sandbox; do not run untrusted code.\n"
            "- Static security findings are heuristic and do not replace a security audit."
        )


def _start_mission(request: str, max_retries: int, use_llm: bool) -> None:
    events: Queue = Queue()
    st.session_state.dp_events = events
    st.session_state.dp_pending = None
    st.session_state.dp_mission_id = None
    st.session_state.dp_selected_mission = None
    st.session_state.dp_result = None
    st.session_state.dp_error = None
    cancel_event = threading.Event()
    st.session_state.dp_cancel_event = cancel_event
    st.session_state.dp_active = True
    thread = threading.Thread(
        target=run_mission_worker,
        args=(request, events),
        kwargs={"max_retries": max_retries, "cancel_event": cancel_event, "use_llm": use_llm},
        daemon=True,
    )
    st.session_state.dp_thread = thread
    thread.start()
    st.rerun()


@st.fragment(run_every=1)
def _live_panel() -> None:
    events: Queue | None = st.session_state.dp_events
    refresh_app = False
    if events is not None:
        while True:
            try:
                event = events.get_nowait()
            except Empty:
                break
            kind = event["kind"]
            if kind == "interaction":
                st.session_state.dp_pending = event["interaction"]
                refresh_app = True
            elif kind == "activity":
                st.session_state.dp_mission_id = event["entry"].get("mission_id")
                refresh_app = True
            elif kind == "result":
                st.session_state.dp_result = event["mission"]
                st.session_state.dp_mission_id = event["mission"].get("mission_id")
                st.session_state.dp_selected_mission = event["mission"].get("mission_id")
                st.session_state.dp_active = False
                st.session_state.dp_pending = None
                refresh_app = True
            elif kind == "error":
                st.session_state.dp_error = event["message"]
                st.session_state.dp_active = False
                st.session_state.dp_pending = None
                refresh_app = True

    if refresh_app:
        st.rerun()

    mission_id = st.session_state.dp_mission_id
    if st.session_state.dp_active:
        if st.button("Cancel mission", key="cancel-mission"):
            cancel_event = st.session_state.dp_cancel_event
            if cancel_event is not None:
                cancel_event.set()
            pending = st.session_state.dp_pending
            if pending is not None:
                pending.answer(None)
                st.session_state.dp_pending = None
            st.info("Cancellation requested. The active operation will stop at its next safe checkpoint.")
    if st.session_state.dp_pending:
        _render_pending_interaction()
    if st.session_state.dp_error:
        st.error(st.session_state.dp_error)


def _apply_theme() -> None:
    st.markdown(
        """
        <style>
        :root {
          color-scheme: dark;
          --dp-bg:#100f0e; --dp-panel:#191715; --dp-panel-2:#211e1a; --dp-border:#39312b;
          --dp-text:#f2eee6; --dp-muted:#aaa092; --dp-crimson:#c8503d; --dp-gold:#d4ad68;
          --dp-green:#90b98a; --dp-red:#e56c55; --dp-amber:#d4ad68; --dp-radius:10px;
          --dp-shadow:0 16px 42px rgba(0,0,0,.34);
        }
        html, body, [class*="css"] { font-family:"Segoe UI",Inter,-apple-system,BlinkMacSystemFont,sans-serif; }
        .stApp { color:var(--dp-text); background:
          radial-gradient(ellipse at 81% 3%,rgba(171,53,38,.16),transparent 31%),
          radial-gradient(ellipse at 4% 32%,rgba(193,143,78,.055),transparent 34%),
          repeating-linear-gradient(3deg,rgba(244,223,191,.012) 0 1px,transparent 1px 5px),var(--dp-bg); }
        [data-testid="stHeader"] { background:rgba(16,15,14,.9); backdrop-filter:blur(14px); border-bottom:1px solid rgba(212,173,104,.09); }
        [data-testid="stToolbar"] { right:1rem; }
        .block-container { max-width:1420px; padding:2.1rem clamp(1rem,3vw,2.8rem) 4rem; }
        [data-testid="stSidebar"] { background:linear-gradient(180deg,#171412,#11100f 70%); border-right:1px solid var(--dp-border); }
        [data-testid="stSidebar"] > div:first-child { padding-top:1.2rem; }
        [data-testid="stSidebar"] [data-testid="stRadio"] label { position:relative; border-radius:5px; padding:.38rem .55rem; transition:background .18s ease,color .18s ease,border-color .18s ease; border-left:2px solid transparent; }
        [data-testid="stSidebar"] [data-testid="stRadio"] label:hover { background:#27201c; color:#fff; }
        [data-testid="stSidebar"] [data-testid="stRadio"] label:has(input:checked) { background:linear-gradient(90deg,rgba(173,51,39,.2),transparent); border-left-color:#d1543e; color:#f1d8b7; }
        [data-testid="stSidebar"] [data-testid="stMarkdownContainer"] p { color:var(--dp-muted); }
        h1,h2,h3,h4 { letter-spacing:-.035em; color:var(--dp-text); }
        h1 { font-weight:800; }
        h2,h3 { font-weight:700; }
        p, label, .stCaption { color:var(--dp-muted); }
        [data-testid="stForm"] { border:1px solid #554331; background:
          repeating-linear-gradient(0deg,rgba(111,79,45,.035) 0 1px,transparent 1px 4px),
          linear-gradient(145deg,rgba(38,31,25,.97),rgba(25,22,19,.98)); padding:1.1rem 1.2rem 1rem; border-radius:var(--dp-radius); box-shadow:inset 0 1px rgba(255,230,187,.035),var(--dp-shadow); }
        [data-testid="stTextArea"] textarea, [data-testid="stTextInput"] input,
        [data-testid="stNumberInput"] input, [data-testid="stSelectbox"] [data-baseweb="select"] > div {
          background:#13110f!important; color:var(--dp-text)!important; border:1px solid #514235!important; border-radius:7px!important;
        }
        input[type="checkbox"],input[type="radio"] { accent-color:#bd4834; }
        textarea:focus, input:focus { border-color:#cc6347!important; box-shadow:0 0 0 2px rgba(204,99,71,.18)!important; }
        [data-testid="stButton"] button, [data-testid="stFormSubmitButton"] button {
          min-height:2.4rem; border:1px solid #463b30; border-radius:6px; background:#211d19; color:var(--dp-text);
          font-weight:650; transition:transform .16s ease,border-color .16s ease,background .16s ease,box-shadow .16s ease;
        }
        [data-testid="stButton"] button:hover, [data-testid="stFormSubmitButton"] button:hover {
          border-color:#b15b43; background:#2b211c; box-shadow:0 5px 18px rgba(159,64,43,.13); transform:translateY(-1px);
        }
        [data-testid="stButton"] button:active, [data-testid="stFormSubmitButton"] button:active { transform:translateY(0) scale(.99); }
        [data-testid="stFormSubmitButton"] button[kind="primary"], [data-testid="stButton"] button[kind="primary"] {
          color:#fff8ee; border-color:#c16549; background:linear-gradient(110deg,#8e3028,#bd4834 72%,#cc6241);
          box-shadow:0 5px 20px rgba(171,57,38,.2);
        }
        [data-testid="stExpander"] { border:1px solid var(--dp-border); border-radius:7px; background:rgba(29,25,22,.75); transition:border-color .2s ease,background .2s ease; }
        [data-testid="stExpander"] summary:hover { color:var(--dp-amber); }
        [data-testid="stTabs"] [role="tab"] { color:var(--dp-muted); }
        [data-testid="stTabs"] [role="tab"][aria-selected="true"] { color:#e4b978; }
        [data-testid="stTabs"] [data-baseweb="tab-highlight"] { background:#b74432; }
        [data-testid="stCode"] { border:1px solid #40362d; border-radius:7px; background:#100e0d; overflow-x:auto; }
        [data-testid="stCode"] code { font-family:"Cascadia Code",Consolas,"SFMono-Regular",monospace!important; font-size:.84rem; }
        [data-testid="stMetric"] { background:transparent; }
        hr { border-color:var(--dp-border); }
        .dp-logo { display:flex; align-items:center; gap:11px; margin:.2rem .15rem 1.3rem; }
        .dp-logo-mark { display:grid; place-items:center; position:relative; width:35px; height:35px; border-radius:50%;
          border:1px solid #a34a34; color:#f1ddba; background:radial-gradient(circle,#b64130 0 28%,#221815 29% 56%,#141110 57%); box-shadow:0 0 18px rgba(180,64,42,.18); }
        .dp-logo-mark::before,.dp-logo-mark::after { content:""; position:absolute; width:20px; height:2px; background:#d4ad68; }
        .dp-logo-mark::before { top:9px; box-shadow:0 3px #d4ad68; }
        .dp-logo-mark::after { bottom:7px; width:14px; }
        .dp-logo-name { font-size:1.05rem; font-weight:800; letter-spacing:-.04em; color:var(--dp-text); }
        .dp-logo-sub { font-size:.67rem; letter-spacing:.11em; color:var(--dp-muted); text-transform:uppercase; }
        .dp-live-dot,.dp-status-dot { display:inline-block; width:7px; height:7px; border-radius:50%; background:var(--dp-green); margin-right:7px; }
        .dp-live-dot { box-shadow:0 0 0 4px rgba(197,72,48,.12); animation:dp-pulse 2.8s ease-in-out infinite; background:#d1543e; }
        .dp-sidebar-repo { margin:.25rem 0 1.2rem; padding:.7rem .75rem; border:1px solid #47392d; border-radius:6px; background:#1b1714; }
        .dp-sidebar-repo small { display:block; font-size:.65rem; color:var(--dp-muted); text-transform:uppercase; letter-spacing:.1em; }
        .dp-sidebar-repo strong { display:block; margin-top:.3rem; font-size:.82rem; color:#efe4d5; }
        .dp-eyebrow { color:var(--dp-amber); font-family:"Cascadia Code",Consolas,monospace; font-size:.68rem; letter-spacing:.13em; margin-bottom:.65rem; }
        .dp-hero { position:relative; display:flex; align-items:center; justify-content:space-between; gap:1.5rem; min-height:245px;
          overflow:hidden; padding:clamp(1.35rem,3vw,2.4rem); margin:.35rem 0 1.7rem;
          border:1px solid #644434; border-radius:8px;
          background:radial-gradient(circle at 82% 43%,rgba(179,55,39,.27) 0 8%,rgba(179,55,39,.12) 9%,transparent 31%),
          repeating-linear-gradient(168deg,transparent 0 23px,rgba(203,172,121,.028) 24px,transparent 25px 48px),
          linear-gradient(112deg,#211b18,#171412 56%,#201714); box-shadow:var(--dp-shadow); isolation:isolate;
          animation:dp-reveal .7s cubic-bezier(.2,.8,.2,1) both; }
        .dp-hero::before { content:""; position:absolute; inset:12px; border:1px solid rgba(211,173,104,.18); pointer-events:none; z-index:-1; }
        .dp-hero::after { content:""; position:absolute; left:-18%; right:30%; top:53%; height:1px; background:linear-gradient(90deg,transparent,rgba(221,182,117,.25),transparent); transform:rotate(-8deg); z-index:-1; }
        .dp-hero-copy { position:relative; z-index:1; min-width:0; flex:1; }
        .dp-hero h1 { margin:.15rem 0 0; font-family:"Yu Mincho","Hiragino Mincho ProN","Noto Serif JP",Georgia,serif; font-size:clamp(1.5rem,3vw,2.5rem); font-weight:700; letter-spacing:.05em; line-height:1.1; white-space:nowrap; color:#f3e7d4; text-shadow:0 4px 20px rgba(166,60,42,.18); }
        .dp-hero-subtitle { color:#d5b578; font-family:"Yu Mincho","Hiragino Mincho ProN",Georgia,serif; font-size:clamp(.72rem,1.4vw,.93rem); letter-spacing:.23em; margin:.45rem 0 .85rem; }
        .dp-hero p { margin:0; max-width:37rem; font-size:.9rem; }
        .dp-hero-rule { position:absolute; left:clamp(1.35rem,3vw,2.4rem); right:clamp(1.35rem,3vw,2.4rem); bottom:17px; display:flex; align-items:center; gap:12px; color:#94836c; font-family:Consolas,monospace; font-size:.61rem; letter-spacing:.12em; }
        .dp-hero-rule i { height:1px; flex:1; background:linear-gradient(90deg,#78563a,transparent); }
        .dp-crest { position:relative; width:190px; height:190px; flex:0 0 190px; margin-right:4%; border-radius:50%;
          border:1px solid rgba(213,173,104,.38); box-shadow:0 0 0 9px rgba(213,173,104,.035),0 0 0 20px rgba(213,173,104,.02),0 0 52px rgba(173,57,39,.12);
          animation:dp-crest-reveal 1.1s cubic-bezier(.2,.8,.2,1) .18s both; }
        .dp-crest-ring { position:absolute; inset:14px; border:1px dashed rgba(215,178,115,.35); border-radius:50%; }
        .dp-crest-sun { position:absolute; inset:49px; border-radius:50%; background:radial-gradient(circle at 40% 35%,#df7950,#ac382c 68%,#762b25); box-shadow:0 0 34px rgba(203,76,51,.24); }
        .dp-crest-torii-top,.dp-crest-torii-mid,.dp-crest-base { position:absolute; left:36px; right:36px; height:7px; background:#d6b77f; border:1px solid #7d5035; z-index:2; }
        .dp-crest-torii-top { top:72px; left:27px; right:27px; transform:skewY(-3deg); }
        .dp-crest-torii-mid { top:88px; left:42px; right:42px; height:4px; background:#e4c990; }
        .dp-crest-pillar { position:absolute; top:78px; height:62px; width:6px; background:#e1c48b; z-index:2; }
        .dp-crest-left { left:58px; }.dp-crest-right { right:58px; }
        .dp-crest-base { top:137px; left:48px; right:48px; height:5px; }
        .dp-metrics { display:grid; grid-template-columns:repeat(4,minmax(0,1fr)); gap:10px; margin:1.25rem 0 2rem; }
        .dp-metric { position:relative; overflow:hidden; padding:15px 16px; border:1px solid #39312b; border-radius:6px;
          background:linear-gradient(145deg,#1d1916,#171513); transition:transform .18s ease,border-color .18s ease; }
        .dp-metric::before { content:""; position:absolute; top:0; left:14px; width:36px; height:2px; background:#a63d30; }
        .dp-metric:hover { transform:translateY(-2px); border-color:#76513b; }
        .dp-metrics { display:grid; grid-template-columns:repeat(4,minmax(0,1fr)); gap:12px; margin:1.35rem 0 2rem; }
        .dp-metric { padding:15px 16px; border:1px solid var(--dp-border); border-radius:11px; background:linear-gradient(145deg,#151720,#11131a); transition:transform .18s ease,border-color .18s ease; }
        .dp-metric:hover { transform:translateY(-2px); border-color:#454153; }
        .dp-metric-label { font-size:.75rem; color:var(--dp-muted); }
        .dp-metric-value { margin:.55rem 0 .25rem; font-size:1.65rem; font-weight:750; letter-spacing:-.05em; }
        .dp-metric-detail { font-size:.7rem; color:#777b8d; }
        .dp-metric.violet .dp-metric-value { color:#d6b477; }
        .dp-metric.cyan .dp-metric-value { color:#d47b5d; }
        .dp-metric.green .dp-metric-value { color:var(--dp-green); }
        .dp-metric.red .dp-metric-value { color:var(--dp-red); }
        .dp-repo-label { font-size:.78rem; color:var(--dp-muted); margin:.25rem 0 .5rem; }
        .dp-repo-value { padding:.62rem .7rem; border:1px solid var(--dp-border); background:#15120f; border-radius:6px; font-size:.83rem; }
        .dp-status { display:inline-flex; align-items:center; padding:.32rem .55rem; border:1px solid var(--dp-border); border-radius:999px; font-size:.68rem; color:#c6c7d2; white-space:nowrap; }
        .dp-status .dp-status-dot { width:6px; height:6px; margin-right:6px; background:#9295a7; }
        .dp-status-completed .dp-status-dot { background:var(--dp-green); }
        .dp-status-running .dp-status-dot,.dp-status-implementing .dp-status-dot,.dp-status-reviewing .dp-status-dot,.dp-status-awaiting-approval .dp-status-dot { background:var(--dp-gold); }
        .dp-status-failed .dp-status-dot { background:var(--dp-red); }
        .dp-status-pending .dp-status-dot,.dp-status-queued .dp-status-dot { background:var(--dp-amber); }
        .dp-status-cancelled .dp-status-dot { background:#838697; }
        .dp-mission-row { padding:.42rem .2rem; }
        .dp-mission-title { font-size:.86rem; font-weight:650; color:var(--dp-text); overflow-wrap:anywhere; }
        .dp-mission-meta { display:flex; flex-wrap:wrap; gap:.75rem; padding-top:.35rem; color:#7f8293; font-size:.68rem; }
        .dp-empty { position:relative; display:flex; flex-direction:column; gap:.45rem; align-items:flex-start; margin:.8rem 0; padding:1.25rem; border:1px dashed #554431; border-radius:6px; color:var(--dp-muted); background:rgba(29,23,19,.45); }
        .dp-empty strong { color:var(--dp-text); font-size:.9rem; }
        .dp-empty span { font-size:.8rem; }
        .dp-empty-mark { color:var(--dp-amber); font-size:1.3rem; }
        .dp-page-title { margin:.1rem 0 .45rem; }
        .dp-workspace-head { display:flex; justify-content:space-between; align-items:flex-start; margin:1.2rem 0 1.3rem; }
        .dp-workspace-head h2 { margin:0; font-size:1.45rem; }
        .dp-inline-stat { font-size:1.45rem; font-weight:750; color:var(--dp-gold); }
        .dp-progress-head { display:flex; justify-content:space-between; color:var(--dp-muted); font-size:.75rem; margin:.8rem 0 .4rem; }
        .dp-progress { height:4px; background:#302821; border-radius:99px; overflow:hidden; margin-bottom:1rem; }
        .dp-progress span { display:block; height:100%; background:linear-gradient(90deg,#a63b30,#d7b574); transition:width .55s ease; }
        .dp-activity { display:grid; grid-template-columns:15px 1fr; gap:10px; padding:.7rem 0; border-bottom:1px solid #22242e; }
        .dp-activity-mark { width:7px; height:7px; margin-top:6px; border-radius:50%; background:#be4b35; box-shadow:0 0 10px rgba(190,75,53,.28); }
        .dp-activity strong { display:block; font-size:.82rem; }
        .dp-activity small { display:block; color:#777b8d; font-size:.68rem; margin-top:2px; }
        .dp-activity p { font-family:"Cascadia Code",Consolas,monospace; font-size:.7rem; margin:.35rem 0 0; overflow-wrap:anywhere; }
        .dp-finding { padding:.7rem .8rem; margin:.55rem 0; border:1px solid var(--dp-border); border-left:3px solid var(--dp-amber); border-radius:5px; background:#1d1915; }
        .dp-finding strong { font-family:Consolas,monospace; font-size:.66rem; color:var(--dp-amber); }
        .dp-finding span { margin-left:.6rem; font-size:.74rem; color:#d5c9b9; }
        .dp-finding p { margin:.4rem 0 0; font-size:.8rem; }
        .dp-finding-critical,.dp-finding-high { border-left-color:var(--dp-red); }
        .dp-finding-critical strong,.dp-finding-high strong { color:var(--dp-red); }
        .dp-finding-low { border-left-color:var(--dp-gold); }
        .dp-settings-grid { display:grid; grid-template-columns:repeat(2,minmax(0,1fr)); gap:12px; margin:1rem 0; }
        .dp-settings-grid article { display:flex; flex-direction:column; gap:.45rem; padding:1rem; border:1px solid var(--dp-border); border-radius:6px; background:var(--dp-panel); }
        .dp-settings-grid small { color:var(--dp-muted); font-size:.7rem; }
        .dp-settings-grid strong { font-size:.95rem; }
        .dp-settings-grid span { color:#7f8293; font-size:.75rem; }
        .dp-loading { display:flex; align-items:center; gap:.7rem; padding:1rem; color:var(--dp-muted); }
        .dp-spinner { width:16px; height:16px; border:2px solid #4b3b2b; border-top-color:#c64f38; border-radius:50%; animation:dp-spin .8s linear infinite; }
        .dp-topline { display:flex; align-items:center; gap:.55rem; padding:.2rem 0 1.2rem; color:#988b78; font-size:.66rem; letter-spacing:.09em; }
        .dp-topline b { color:var(--dp-text); font-size:.79rem; letter-spacing:0; }
        .dp-topline i { color:#776047; font-style:normal; }
        .dp-topline-right { justify-content:flex-end; letter-spacing:0; font-size:.72rem; }
        .dp-topline-right .dp-status-dot { margin:0; background:#9295a7; }
        .dp-topline-right .dp-connection-dot-connected { background:var(--dp-green); }
        .dp-topline-right .dp-connection-dot-failed { background:var(--dp-red); }
        .dp-sidebar-footer { margin-top:1.4rem; padding-top:.8rem; border-top:1px solid var(--dp-border); color:#988b78; font-size:.72rem; }
        .dp-sidebar-footer .dp-live-dot { vertical-align:middle; }
        .dp-workspace-head { position:relative; padding:1rem 1rem 1rem 1.2rem; border-left:3px solid #ac3d30; background:linear-gradient(90deg,rgba(164,57,41,.1),transparent 72%); }
        .dp-seal-progress { width:58px; height:58px; flex:0 0 58px; display:grid; place-items:center; border-radius:50%;
          background:conic-gradient(#c64f38 var(--progress-angle),#393027 0); position:relative; color:#f0dfc2; }
        .dp-seal-progress::before { content:""; position:absolute; inset:4px; background:#181411; border-radius:50%; border:1px solid #6b4934; }
        .dp-seal-progress span { position:relative; z-index:1; font-family:Consolas,monospace; font-size:.9rem; }
        .dp-seal-progress small { color:#988b78; font-size:.62rem; }
        .dp-mission-complete .dp-seal-progress { box-shadow:0 0 25px rgba(203,153,82,.2); }
        .dp-agent-grid { display:grid; grid-template-columns:repeat(4,minmax(0,1fr)); gap:8px; margin:.7rem 0 1.25rem; }
        .dp-agent { position:relative; display:flex; flex-direction:column; gap:.42rem; padding:.68rem .75rem; border:1px solid #39312b; border-radius:5px; background:#191613; overflow:hidden; }
        .dp-agent::before { content:""; position:absolute; left:0; top:0; bottom:0; width:2px; background:#8d6a41; }
        .dp-agent small { color:#968a78; font-family:Consolas,monospace; font-size:.61rem; letter-spacing:.1em; }
        .dp-agent strong { color:#e7dac5; font-size:.77rem; }
        .dp-agent-strategist::before { background:#c9a76b; }
        .dp-agent-coder::before { background:#c64f38; }
        .dp-agent-inspector::before { background:#aaa6a0; }
        .dp-agent-training::before { background:#83a47b; }
        .sh-add-count { color:#9cc48c; font-family:Consolas,monospace; }
        .sh-del-count { color:#e2826e; font-family:Consolas,monospace; }
        .dp-hero + h3 { margin-top:1.6rem; }
        .dp-hero-copy { animation:dp-copy-reveal .6s ease-out both; }
        .dp-metric { animation:dp-dossier-rise .42s ease-out both; }
        .dp-metric:nth-child(2) { animation-delay:.06s; }
        .dp-metric:nth-child(3) { animation-delay:.12s; }
        .dp-metric:nth-child(4) { animation-delay:.18s; }
        [data-testid="stTabs"] [role="tab"] { transition:color .18s ease,background .18s ease; }
        [data-testid="stButton"] button:focus-visible,[data-testid="stFormSubmitButton"] button:focus-visible,
        [data-testid="stRadio"] label:focus-within { outline:2px solid #d5ad68!important; outline-offset:2px; }
        [data-testid="stCode"] pre { max-width:100%; overflow-x:auto; }
        @keyframes dp-spin { to { transform:rotate(360deg); } }
        @keyframes dp-pulse { 50% { box-shadow:0 0 0 6px rgba(198,79,56,0); } }
        @keyframes dp-reveal { from { opacity:0; clip-path:inset(0 100% 0 0); } to { opacity:1; clip-path:inset(0 0 0 0); } }
        @keyframes dp-copy-reveal { from { opacity:0; transform:translateY(8px); } to { opacity:1; transform:translateY(0); } }
        @keyframes dp-crest-reveal { from { opacity:0; transform:scale(.88) rotate(-8deg); } to { opacity:1; transform:scale(1) rotate(0); } }
        @keyframes dp-dossier-rise { from { opacity:0; transform:translateY(7px); } to { opacity:1; transform:translateY(0); } }
        @media (max-width:900px) { .dp-metrics { grid-template-columns:repeat(2,minmax(0,1fr)); } }
        @media (max-width:620px) {
          .block-container { padding:1.25rem .8rem 2.5rem; }
          .dp-metrics { gap:8px; }
          .dp-metric { padding:12px; }
          .dp-metric-value { font-size:1.35rem; }
          .dp-settings-grid { grid-template-columns:1fr; }
          .dp-agent-grid { grid-template-columns:repeat(2,minmax(0,1fr)); }
          .dp-hero { min-height:225px; padding-bottom:3.4rem; }
          .dp-crest { width:88px; height:88px; flex-basis:88px; margin-right:0; }
          .dp-crest-ring { inset:7px; }.dp-crest-sun { inset:24px; }
          .dp-crest-torii-top { top:32px; left:12px; right:12px; height:4px; }
          .dp-crest-torii-mid { top:40px; left:20px; right:20px; height:3px; }
          .dp-crest-pillar { top:35px; height:30px; width:4px; }.dp-crest-left { left:27px; }.dp-crest-right { right:27px; }
          .dp-crest-base { top:64px; left:23px; right:23px; height:3px; }
          .dp-hero-rule { font-size:.5rem; letter-spacing:.04em; gap:6px; }
          .dp-hero-subtitle { letter-spacing:.1em; }
          .dp-hero h1 { font-size:clamp(1.4rem,6vw,2rem); letter-spacing:.04em; }
          .dp-workspace-head h2 { font-size:1.12rem; overflow-wrap:anywhere; }
          [data-testid="stTabs"] button { font-size:.72rem; padding:.3rem; }
        }
        @media (prefers-reduced-motion:reduce) {
          *,*::before,*::after { scroll-behavior:auto!important; animation-duration:.01ms!important; animation-iteration-count:1!important; transition-duration:.01ms!important; }
        }
        </style>
        """,
        unsafe_allow_html=True,
    )


def main() -> None:
    st.set_page_config(page_title="DevPilot · Shinobi Tech", page_icon="D", layout="wide")
    _initialise_state()
    _apply_theme()
    worker = st.session_state.dp_thread
    events = st.session_state.dp_events
    orphaned_ids: set[str] = set()
    active_ids: set[str] = set()
    if st.session_state.dp_active:
        if worker is not None and worker.is_alive():
            if st.session_state.dp_mission_id:
                active_ids.add(st.session_state.dp_mission_id)
        elif events is None or events.empty():
            if st.session_state.dp_mission_id:
                orphaned_ids.add(st.session_state.dp_mission_id)
            pending = st.session_state.dp_pending
            if pending is not None:
                pending.answer(None)
            st.session_state.dp_pending = None
            st.session_state.dp_active = False
            st.session_state.dp_error = (
                "The previous mission worker stopped unexpectedly. Its run was "
                "closed; start a new mission or reopen its record for details."
            )
    storage_error = None
    try:
        recovered_missions = recover_stale_missions(
            exclude_ids=active_ids,
            orphaned_ids=orphaned_ids,
        )
        missions, corrupt_count = _load_recent_missions()
    except (FirestoreError, ValueError) as exc:
        recovered_missions = []
        missions, corrupt_count = [], 0
        storage_error = str(exc)
    selected_nav = st.session_state.dp_nav if st.session_state.dp_nav in NAV_ITEMS else "Command Center"

    with st.sidebar:
        st.markdown(
            '<div class="dp-logo"><div class="dp-logo-mark" aria-hidden="true"><span></span></div><div>'
            '<div class="dp-logo-name">DevPilot</div><div class="dp-logo-sub">Shinobi Tech · Command</div></div></div>',
            unsafe_allow_html=True,
        )
        st.markdown(
            '<div class="dp-sidebar-repo"><small>Active repository</small><strong>demo_repo · local village</strong></div>',
            unsafe_allow_html=True,
        )
        st.markdown('<div class="dp-eyebrow">WORKSPACE</div>', unsafe_allow_html=True)
        selected_nav = st.radio(
            "Command navigation",
            NAV_ITEMS,
            index=NAV_ITEMS.index(selected_nav),
            format_func=lambda item: f"{NAV_GLYPHS[item]}    {item}",
            label_visibility="collapsed",
            key="dp_nav_radio",
        )
        st.session_state.dp_nav = selected_nav
        st.markdown('<div class="dp-eyebrow" style="margin-top:1.3rem">RECENT MISSIONS</div>', unsafe_allow_html=True)
        if missions:
            for mission in missions[:5]:
                request = str(mission.get("user_request", "Mission"))
                button_label = f"{request[:28]}{'…' if len(request) > 28 else ''} · {mission.get('status', 'pending')}"
                st.button(
                    button_label,
                    key=f"sidebar-mission-{mission['mission_id']}",
                    on_click=_select_mission,
                    args=(mission["mission_id"],),
                    use_container_width=True,
                )
        else:
            st.caption("No saved missions")
        agent_state = "Field teams deployed" if st.session_state.dp_active else "Field teams standing by"
        st.markdown(
            f'<div class="dp-sidebar-footer"><span class="dp-live-dot"></span>{escape(agent_state)}</div>',
            unsafe_allow_html=True,
        )

    connection = st.session_state.dp_gemini_connection
    header_left, header_right = st.columns([4, 2])
    with header_left:
        st.markdown(
            '<div class="dp-topline"><span>SHINOBI TECH</span><b>demo_repo</b><i>/</i><span>LOCAL OPERATIONS</span></div>',
            unsafe_allow_html=True,
        )
    with header_right:
        conn_label = f"Google Gemini · {connection}"
        connection_class = {
            "Connected": "connected",
            "Failed": "failed",
        }.get(connection, "not-tested")
        st.markdown(
            f'<div class="dp-topline dp-topline-right"><span class="dp-status-dot dp-connection-dot-{connection_class}"></span>'
            f'<span>{escape(conn_label)}</span></div>',
            unsafe_allow_html=True,
        )
    if corrupt_count:
        st.warning(f"{corrupt_count} mission record(s) could not be loaded. Their contents were left unchanged.")
    if recovered_missions:
        st.info(
            f"Closed {len(recovered_missions)} stale or interrupted mission(s). "
            "Their records remain available in Mission Scrolls."
        )
    if storage_error:
        st.error(f"Mission storage is unavailable: {storage_error}")

    with st.popover("⌕  Command palette", help="Search and open a workspace section."):
        command_query = st.text_input("Find a command", key="dp_command_query", placeholder="Search command center…")
        matches = [
            item for item in NAV_ITEMS
            if command_query.casefold() in item.casefold()
        ]
        if not matches:
            st.caption("No matching command.")
        for destination in matches:
            st.button(
                f"{NAV_GLYPHS[destination]}  {destination}",
                key=f"command-{destination}",
                on_click=_navigate,
                args=(destination,),
                use_container_width=True,
            )

    if storage_error and selected_nav != "Village Settings":
        st.info("Open Village Settings to check Firestore configuration. No mission data was loaded or written.")
        return

    try:
        if selected_nav == "Command Center":
            _render_dashboard(missions)
            if st.session_state.dp_active or st.session_state.dp_result or st.session_state.dp_error or st.session_state.dp_events is not None:
                _live_panel()
        elif selected_nav == "Mission Scrolls":
            _render_mission_list_page(missions)
            if st.session_state.dp_active or st.session_state.dp_pending:
                _live_panel()
        elif selected_nav in {"Shadow Coder", "ANBU Review", "Training Grounds", "Intelligence Reports"}:
            _render_detail_page(selected_nav, missions)
            if st.session_state.dp_active or st.session_state.dp_pending:
                _live_panel()
        else:
            _render_settings()
    except FirestoreError as exc:
        st.error(f"Mission storage is unavailable: {exc}")


main()
