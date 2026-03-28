"""Settings tab — view and edit config.json from the UI."""

from __future__ import annotations

from pathlib import Path

import streamlit as st

from src.config import load_settings, save_settings

_PROJECT_ROOT = Path(__file__).parent.parent


def _list_prompts() -> list[str]:
    """Return all .md files in prompts/ as paths relative to the project root."""
    return sorted(
        str(p.relative_to(_PROJECT_ROOT))
        for p in (_PROJECT_ROOT / "prompts").glob("*.md")
    )


def run() -> None:
    """Render the Settings tab."""
    st.subheader("⚙️ Settings")
    st.caption(
        "All values are read from `src/config.json`. "
        "Changes here take effect on the next execution. "
        "CLI flags and UI selections override these defaults at runtime."
    )

    cfg = load_settings()
    vai = cfg.vertexai
    proc = cfg.processing
    batch = cfg.batch
    log = cfg.logging

    with st.form("settings_form"):
        st.markdown("#### ☁️ Vertex AI")
        s1, s2, s3 = st.columns([2, 2, 2])
        with s1:
            new_project_id = st.text_input("Default Project ID", value=vai.project_id,
                                           help="Leave blank to rely on PROJECT_ID env var.")
            new_auth_mode: str = st.selectbox(
                "Default Auth Mode", ["api", "gcloud"],
                index=0 if vai.auth_mode == "api" else 1,
            )
        with s2:
            new_location = st.text_input("Default Location", value=vai.location)
            new_model: str = st.selectbox(
                "Default Model",
                ["gemini-2.5-pro", "gemini-2.5-flash", "gemini-3.1-flash-lite-preview"],
                index=["gemini-2.5-pro", "gemini-2.5-flash", "gemini-3.1-flash-lite-preview"].index(vai.model)
                if vai.model in ["gemini-2.5-pro", "gemini-2.5-flash", "gemini-3.1-flash-lite-preview"] else 0,
            )
        with s3:
            new_refine = st.number_input("Default Refinement Passes", min_value=0, value=vai.refine_iterations, step=1)
            new_cse = st.number_input("Default Max Errors (CLEAN)", min_value=-1, value=vai.clean_stop_max_errors, step=1)

        _prompts = _list_prompts()
        s4, s5 = st.columns([3, 3])
        with s4:
            new_ext_prompt: str = st.selectbox(
                "Default Extraction Prompt", _prompts,
                index=_prompts.index(vai.extraction_prompt) if vai.extraction_prompt in _prompts else 0,
            )
        with s5:
            new_ref_prompt: str = st.selectbox(
                "Default Refinement Prompt", _prompts,
                index=_prompts.index(vai.refinement_prompt) if vai.refinement_prompt in _prompts else 0,
            )

        st.markdown("---")
        st.markdown("#### 🔧 Processing")
        p1, p2, p3, p4 = st.columns([2, 2, 2, 2])
        with p1:
            new_backend: str = st.selectbox(
                "Default Backend", ["vertexai", "marker", "pdfplumber"],
                index=["vertexai", "marker", "pdfplumber"].index(proc.backend)
                if proc.backend in ["vertexai", "marker", "pdfplumber"] else 0,
            )
        with p2:
            new_chunk_size = st.number_input("Default Chunk Size (pages)", min_value=0, value=proc.chunk_size, step=5)
        with p3:
            new_chunk_overlap = st.number_input("Default Chunk Overlap (pages)", min_value=0, value=proc.chunk_overlap, step=1)
        with p4:
            new_workers = st.number_input("Default Workers", min_value=1, value=proc.workers, step=1)

        new_validate = st.checkbox("Validate after convert by default", value=proc.validate_after_convert)

        st.markdown("---")
        st.markdown("#### 📂 Batch")
        b1, b2 = st.columns([2, 4])
        with b1:
            new_recursive = st.checkbox("Recursive folder scan", value=batch.recursive)
        with b2:
            new_extensions = st.text_input(
                "File extensions (comma-separated)",
                value=", ".join(batch.extensions),
                help='e.g. ".pdf, .PDF"',
            )

        st.markdown("---")
        st.markdown("#### 📊 Logging")
        st.caption("Execution log (structured JSONL) paths.")
        l1, l2 = st.columns([2, 4])
        with l1:
            new_exec_log_dir = st.text_input(
                "Exec log directory",
                value=log.exec_log_dir,
                help="Folder for execution JSONL logs.",
            )
        with l2:
            new_exec_log_file = st.text_input(
                "Exec log filename",
                value=log.exec_log_file,
                help="Filename inside the exec log directory.",
            )
        st.caption("Application rotating file logs (see `src/logging_config.py`).")
        l3, l4, l5 = st.columns([2, 2, 2])
        with l3:
            new_app_log_dir = st.text_input(
                "App log directory",
                value=log.log_dir,
                help="Folder for rotating app log files.",
            )
        with l4:
            new_log_max_bytes = st.number_input(
                "Log max bytes (per file)",
                min_value=1024,
                value=int(log.log_max_bytes),
                step=1048576,
                help="Rotate when a log file reaches this size (bytes).",
            )
        with l5:
            new_log_backup_count = st.number_input(
                "Log backup count",
                min_value=0,
                value=int(log.log_backup_count),
                step=1,
                help="Number of rotated log files to keep.",
            )

        st.markdown("---")
        submitted = st.form_submit_button("💾 Save settings", type="primary")

    if submitted:
        exts = [e.strip() for e in new_extensions.split(",") if e.strip()]
        if not exts:
            exts = [".pdf"]

        cfg.vertexai.project_id = new_project_id
        cfg.vertexai.auth_mode = new_auth_mode
        cfg.vertexai.location = new_location
        cfg.vertexai.model = new_model
        cfg.vertexai.refine_iterations = int(new_refine)
        cfg.vertexai.clean_stop_max_errors = int(new_cse)
        cfg.vertexai.extraction_prompt = new_ext_prompt
        cfg.vertexai.refinement_prompt = new_ref_prompt

        cfg.processing.backend = new_backend
        cfg.processing.chunk_size = int(new_chunk_size)
        cfg.processing.chunk_overlap = int(new_chunk_overlap)
        cfg.processing.workers = int(new_workers)
        cfg.processing.validate_after_convert = new_validate

        cfg.batch.recursive = new_recursive
        cfg.batch.extensions = exts

        cfg.logging.exec_log_dir = new_exec_log_dir
        cfg.logging.exec_log_file = new_exec_log_file
        cfg.logging.log_dir = new_app_log_dir
        cfg.logging.log_max_bytes = int(new_log_max_bytes)
        cfg.logging.log_backup_count = int(new_log_backup_count)

        save_settings(cfg)
        st.success("Settings saved to `src/config.json`.")
        st.rerun()
