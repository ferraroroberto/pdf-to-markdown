"""Log Viewer tab — display and filter the persistent exec_log.jsonl."""

from __future__ import annotations

from pathlib import Path

import streamlit as st

_PROJECT_ROOT = Path(__file__).parent.parent


def run() -> None:
    """Render the Log Viewer tab."""
    st.subheader("Execution Log Viewer")

    from src.logger_exec import load_log

    rows = load_log()

    if not rows:
        st.info("No execution log entries yet. Run a conversion to populate the log.")
        return

    # ── Summary ────────────────────────────────────────────────────────────
    extraction_rows = [r for r in rows if r.get("step_type") == "extraction"]
    refinement_rows = [r for r in rows if r.get("step_type") == "refinement"]
    files = {r.get("file", "") for r in rows}
    failed = sum(1 for r in rows if r.get("error"))

    st.caption(
        f"**{len(rows)}** log entries · **{len(files)}** files · "
        f"**{len(extraction_rows)}** extractions · **{len(refinement_rows)}** refinements"
        + (f" · **{failed}** failed" if failed else "")
    )

    st.divider()

    # ── Filters ──────────────────────────────────────────────────────────────
    col_file, col_type, col_verdict, col_model = st.columns([3, 2, 2, 2])

    all_files = sorted({Path(r.get("file", "")).name for r in rows})
    all_types = sorted({r.get("step_type", "") for r in rows})
    all_verdicts = sorted({r.get("verdict", "") for r in rows})
    all_models = sorted({r.get("model", "") for r in rows})

    with col_file:
        filter_file = st.selectbox("Filter by file", ["(all)"] + all_files, key="lv_file_filter")
    with col_type:
        filter_type = st.selectbox("Filter by step type", ["(all)"] + all_types, key="lv_type_filter")
    with col_verdict:
        filter_verdict = st.selectbox("Filter by verdict", ["(all)"] + all_verdicts, key="lv_verdict_filter")
    with col_model:
        filter_model = st.selectbox("Filter by model", ["(all)"] + all_models, key="lv_model_filter")

    filtered = rows
    if filter_file != "(all)":
        filtered = [r for r in filtered if Path(r.get("file", "")).name == filter_file]
    if filter_type != "(all)":
        filtered = [r for r in filtered if r.get("step_type") == filter_type]
    if filter_verdict != "(all)":
        filtered = [r for r in filtered if r.get("verdict") == filter_verdict]
    if filter_model != "(all)":
        filtered = [r for r in filtered if r.get("model") == filter_model]

    st.caption(f"Showing {len(filtered)} of {len(rows)} log rows")

    # ── Table ─────────────────────────────────────────────────────────────────
    display_rows = []
    for r in reversed(filtered):  # newest first
        display_rows.append({
            "Timestamp": r.get("timestamp", ""),
            "File": Path(r.get("file", "")).name,
            "Chunk": r.get("chunk_pages", "all"),
            "Step": r.get("step", 0),
            "Type": r.get("step_type", ""),
            "Model": r.get("model", ""),
            "Auth": r.get("auth_mode", ""),
            "In tok": r.get("input_tokens", 0),
            "Out tok": r.get("output_tokens", 0),
            "Cost": r.get("cost_label", ""),
            "Errors": r.get("errors", 0),
            "Critical": r.get("critical", 0),
            "Moderate": r.get("moderate", 0),
            "Minor": r.get("minor", 0),
            "Verdict": r.get("verdict", ""),
            "Ext. hash": r.get("extraction_prompt_hash", ""),
            "Ref. hash": r.get("refinement_prompt_hash", ""),
            "Error": r.get("error", "") or "",
        })

    st.dataframe(display_rows, width="stretch")

    # ── Clear log ────────────────────────────────────────────────────────────
    st.divider()
    if "lv_confirm_clear" not in st.session_state:
        st.session_state.lv_confirm_clear = False

    if not st.session_state.lv_confirm_clear:
        if st.button("Clear log", key="lv_clear_btn",
                     help="Delete all entries from exec_log.jsonl. Irreversible."):
            st.session_state.lv_confirm_clear = True
            st.rerun()
    else:
        st.warning("This will permanently delete all log entries. Are you sure?")
        col_yes, col_no = st.columns([1, 1])
        with col_yes:
            if st.button("Yes, delete", key="lv_confirm_yes", type="primary"):
                from src.config import load_settings
                cfg = load_settings()
                log_path = _PROJECT_ROOT / cfg.logging.exec_log_dir / cfg.logging.exec_log_file
                if log_path.exists():
                    log_path.unlink()
                st.session_state.lv_confirm_clear = False
                st.success("Log cleared.")
                st.rerun()
        with col_no:
            if st.button("Cancel", key="lv_confirm_no"):
                st.session_state.lv_confirm_clear = False
                st.rerun()
