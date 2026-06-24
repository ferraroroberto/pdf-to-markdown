"""Convert File tab — file selection, options, live log stream, result display.

Thin tab wiring only.  The conversion worker and artifact orchestration live in
``src.execute_worker`` (pure logic, unit-testable without Streamlit); the result
rendering lives in ``execute_render``.  This module owns the Streamlit widgets,
session state, and the worker-thread handoff.
"""

from __future__ import annotations

import queue
import time
import threading
try:
    import tkinter as tk
    from tkinter import filedialog
    _HAS_TKINTER = True
except ModuleNotFoundError:
    _HAS_TKINTER = False
from pathlib import Path

import streamlit as st

from _common import (
    list_extraction_prompts,
    list_refinement_prompts,
    render_log_box,
    sync_config_defaults_on_change,
)
from execute_render import render_result
from remote_upload import is_remote_session, save_uploaded_file, ACCEPT_TYPES
from src.classifier import classify_pdf
from src.config import GEMINI_MODELS, load_settings
from src.execute_worker import run_execute_conversion

# Gemini model options shown in the UI (order = dropdown order).
# Sourced from the single shared constant in src.config so the Execute, Batch,
# and Settings dropdowns and config.json never drift apart.
_VAI_MODELS: list[str] = GEMINI_MODELS


# ── Session state bootstrap ─────────────────────────────────────────────────────


def _init_state() -> None:
    cfg = load_settings()
    vai = cfg.vertexai
    proc = cfg.processing
    defaults = {
        "ex_running": False,
        "ex_logs": [],
        "ex_result": None,
        "ex_log_q": None,
        "ex_result_q": None,
        "ex_output_path": None,
        "ex_source_path": None,   # original input file path (for cleanup of converted PDF)
        "ex_chunk_size": proc.chunk_size,
        "ex_chunk_overlap": proc.chunk_overlap,
        "ex_max_chunks": 0,
        "ex_diminishing_returns": vai.diminishing_returns_enabled,
    }
    for k, v in defaults.items():
        if k not in st.session_state:
            st.session_state[k] = v


# Widget keys cleared when config.json changes so the Execute-tab widgets
# re-read the refreshed defaults on the next render.
_SYNC_POP_KEYS = (
    "ex_chunk_size_input",
    "ex_chunk_overlap_input",
    "vai_project_id",
    "vai_location",
    "vai_auth_mode",
    "vai_model_id",
    "vai_refine_iterations",
    "vai_extraction_prompt_file",
    "vai_refinement_prompt_file",
    "vai_clean_stop_max_errors",
    "vai_diminishing_returns",
)


def _clear_output() -> None:
    """Reset Execute tab session state only — does not delete files on disk."""
    st.session_state.ex_logs = []
    st.session_state.ex_result = None
    st.session_state.ex_output_path = None
    st.session_state.ex_source_path = None


# ── Tab UI ──────────────────────────────────────────────────────────────────────


def run() -> None:
    """Render the Execute tab."""
    _init_state()
    cfg = load_settings()
    vai_cfg = cfg.vertexai
    proc_cfg = cfg.processing

    running: bool = st.session_state.ex_running
    sync_config_defaults_on_change(running, prefix="ex_", pop_keys=_SYNC_POP_KEYS)

    # ── 1. File selection ───────────────────────────────────────────────────
    st.subheader("Select File")

    _remote = is_remote_session()

    if _remote:
        # Remote mode — use browser file uploader (drag-and-drop)
        st.caption("🌐 Remote session detected — upload a file from your browser.")
        uploaded = st.file_uploader(
            "Upload a file",
            type=ACCEPT_TYPES,
            help="Drag and drop or click to upload a PDF, Word, PowerPoint, Excel, or image file.",
            key="ex_file_upload",
            disabled=running,
        )
        if uploaded is not None:
            saved = save_uploaded_file(uploaded)
            st.session_state.file_path_input = str(saved)
            file_path_str = str(saved)
        else:
            file_path_str = ""
    else:
        # Local mode — native file browser + text input
        if _HAS_TKINTER:
            col_input, col_browse = st.columns([5, 1])
            _col_input = col_input
        else:
            _col_input = st.container()

        if _HAS_TKINTER:
            with col_browse:
                st.markdown("<div style='padding-top:1.9rem'>", unsafe_allow_html=True)
                if st.button("Browse...", width="stretch", key="browse_btn", disabled=running):
                    from src.file_converter import IMAGE_EXTENSIONS, INPUT_EXTENSIONS

                    # Derive the glob patterns from the canonical frozensets so the
                    # picker never drifts from what the pipeline actually accepts.
                    all_supported = " ".join(f"*{e}" for e in sorted(INPUT_EXTENSIONS))
                    image_patterns = " ".join(f"*{e}" for e in sorted(IMAGE_EXTENSIONS))
                    root = tk.Tk()
                    root.withdraw()
                    root.wm_attributes("-topmost", 1)
                    chosen = filedialog.askopenfilename(
                        title="Select a file",
                        filetypes=[
                            ("All supported files", all_supported),
                            ("PDF files", "*.pdf"),
                            ("Word documents", "*.docx *.doc"),
                            ("PowerPoint presentations", "*.pptx *.ppt"),
                            ("Images", image_patterns),
                            ("All files", "*.*"),
                        ],
                    )
                    root.destroy()
                    if chosen:
                        st.session_state.file_path_input = chosen
                st.markdown("</div>", unsafe_allow_html=True)

        with _col_input:
            file_path_str = st.text_input(
                "File path",
                placeholder=r"/path/to/document.pdf",
                help="Paste the full local path to a PDF, Word, PowerPoint, or image file."
                + (" Use Browse to pick one." if _HAS_TKINTER else ""),
                key="file_path_input",
                disabled=running,
            )

    pdf_path: Path | None = None

    if file_path_str:
        from src.file_converter import INPUT_EXTENSIONS, needs_conversion

        p = Path(file_path_str.strip().strip('"'))
        if not p.exists():
            st.error(f"File not found: `{p}`")
        elif p.suffix.lower() not in INPUT_EXTENSIONS:
            st.error(f"Unsupported file type: `{p.suffix}`. Supported: PDF, Word, PowerPoint, Excel, images.")
        else:
            pdf_path = p
            size_kb = p.stat().st_size / 1024

            if p.suffix.lower() == ".pdf":
                pdf_info = None
                with st.spinner("Inspecting PDF…"):
                    try:
                        pdf_info = classify_pdf(pdf_path)
                    except Exception:  # noqa: BLE001
                        pass

                cols = st.columns(5)
                cols[0].metric("Size", f"{size_kb:,.1f} KB")
                cols[1].metric("Pages", pdf_info.page_count if pdf_info else "—")
                cols[2].metric("Classification", pdf_info.classification if pdf_info else "—")
                cols[3].metric("Avg chars/page", f"{pdf_info.avg_chars_per_page:.0f}" if pdf_info else "—")
                cols[4].metric("Scanned", "Yes" if pdf_info and pdf_info.is_scanned else ("No" if pdf_info else "—"))
            else:
                from src.file_converter import OFFICE_EXTENSIONS, IMAGE_EXTENSIONS
                suffix = p.suffix.lower()
                if suffix in OFFICE_EXTENSIONS:
                    file_type = "Office document"
                else:
                    file_type = "Image"
                cols = st.columns(3)
                cols[0].metric("Size", f"{size_kb:,.1f} KB")
                cols[1].metric("Type", file_type)
                cols[2].metric("Format", p.suffix.upper())
                st.info("ℹ️ This file will be converted to PDF before extraction.")

    st.divider()

    # ── 2. Options ──────────────────────────────────────────────────────────
    verbose: bool = st.checkbox(
        "Verbose",
        help="Show DEBUG-level log messages and save intermediate artifacts.",
        key="verbose_check",
        disabled=running,
    )

    # ── Advanced options (Vertex AI + chunking) ──────────────────────────────
    with st.expander("Advanced options", expanded=False):
        # Row 1: Project ID | Location | Refinement Passes
        adv1, adv2, adv3 = st.columns([2, 2, 2])
        with adv1:
            st.text_input(
                "Project ID",
                value=vai_cfg.project_id,
                help="Google Cloud project ID (from the active machine profile).",
                key="vai_project_id",
                disabled=running,
            )
        with adv2:
            st.text_input(
                "Location",
                value=vai_cfg.location,
                help="Vertex AI region, e.g. europe-west3.",
                key="vai_location",
                disabled=running,
            )
        with adv3:
            st.number_input(
                "Refinement Passes",
                min_value=0,
                max_value=10,
                value=vai_cfg.refine_iterations,
                step=1,
                help="Number of refinement passes after extraction. 0 = extraction only.",
                key="vai_refine_iterations",
                disabled=running,
            )

        # Row 2: Auth Mode | Model | Max Errors (CLEAN)
        adv4, adv5, adv6 = st.columns([2, 2, 2])
        with adv4:
            st.selectbox(
                "Auth Mode",
                ["api", "gcloud"],
                index=0 if vai_cfg.auth_mode == "api" else 1,
                help="**api**: uses GOOGLE_API_KEY.  **gcloud**: Application Default Credentials.",
                key="vai_auth_mode",
                disabled=running,
            )
        with adv5:
            _model_idx = _VAI_MODELS.index(vai_cfg.model) if vai_cfg.model in _VAI_MODELS else 0
            st.selectbox(
                "Model",
                _VAI_MODELS,
                index=_model_idx,
                help="Gemini model to use for extraction.",
                key="vai_model_id",
                disabled=running,
            )
        with adv6:
            st.number_input(
                "Max Errors (CLEAN)",
                min_value=-1,
                value=vai_cfg.clean_stop_max_errors,
                step=1,
                help=(
                    "Early-stop threshold for refinement. "
                    "**-1**: stop on any CLEAN verdict. **0**: only when 0 errors remain."
                ),
                key="vai_clean_stop_max_errors",
                disabled=running,
            )

        st.checkbox(
            "Enable diminishing returns stop",
            value=vai_cfg.diminishing_returns_enabled,
            help=(
                "When enabled, refinement stops early if two consecutive passes show no "
                "reduction in errors."
            ),
            key="vai_diminishing_returns",
            disabled=running,
        )

        # Row 3: Extraction Prompt | Refinement Prompt
        _ext_prompts = list_extraction_prompts()
        _ref_prompts = list_refinement_prompts()
        adv7, adv8 = st.columns([3, 3])
        with adv7:
            _ext_default = vai_cfg.extraction_prompt
            st.selectbox(
                "Extraction Prompt",
                _ext_prompts,
                index=_ext_prompts.index(_ext_default) if _ext_default in _ext_prompts else 0,
                key="vai_extraction_prompt_file",
                disabled=running,
            )
        with adv8:
            _ref_default = vai_cfg.refinement_prompt
            st.selectbox(
                "Refinement Prompt",
                _ref_prompts,
                index=_ref_prompts.index(_ref_default) if _ref_default in _ref_prompts else 0,
                key="vai_refinement_prompt_file",
                disabled=running,
            )

        st.markdown("---")
        st.markdown("##### Processing")

        # Row 4: Chunk Size | Chunk Overlap | Max Chunks
        col_chunk, col_overlap, col_max_chunks = st.columns([2, 2, 2])
        with col_chunk:
            chunk_size: int = st.number_input(
                "Chunk Size (pages)",
                min_value=0,
                value=proc_cfg.chunk_size,
                step=5,
                help="Split the document into chunks of this many pages. 0 disables chunking.",
                key="ex_chunk_size_input",
                disabled=running,
            )
        with col_overlap:
            chunk_overlap: int = st.number_input(
                "Chunk Overlap (pages)",
                min_value=0,
                value=proc_cfg.chunk_overlap,
                step=1,
                help="Trailing pages from the previous chunk included at the start of the next, for context continuity.",
                key="ex_chunk_overlap_input",
                disabled=running,
            )
        with col_max_chunks:
            max_chunks: int = st.number_input(
                "Max Chunks (0 = all)",
                min_value=0,
                value=st.session_state.get("ex_max_chunks", 0),
                step=1,
                help="Stop after processing this many chunks. 0 means process all chunks.",
                key="ex_max_chunks_input",
                disabled=running,
            )

        st.checkbox(
            "Validate after convert",
            value=proc_cfg.validate_after_convert,
            help=(
                "Run a post-conversion validation check on the output markdown. "
                "The default for this checkbox is controlled by **Settings → Validate after convert by default**."
            ),
            key="ex_validate_after_convert",
            disabled=running,
        )

    auth_mode: str = st.session_state.get("vai_auth_mode", vai_cfg.auth_mode)

    if pdf_path is not None and not running:
        st.caption(f"Output will be saved to: `{pdf_path.with_suffix('.md')}`")

    st.divider()

    # ── 3. Execute button ────────────────────────────────────────────────────
    if not running:
        _btn_col, _dry_col = st.columns([4, 2])

        with _dry_col:
            st.markdown('<div style="margin-top: 0.35rem;"></div>', unsafe_allow_html=True)
            dry_run_check = st.toggle(
                "Dry run (estimate only)",
                key="dry_run_check",
                help="Count pages and estimate token cost without calling the API.",
            )

        _execute_clicked = _btn_col.button(
            "Convert",
            type="primary",
            disabled=(pdf_path is None),
            width="stretch",
            key="execute_btn",
        )

        if _execute_clicked:
            if pdf_path is None:
                st.warning("Please select a valid PDF file first.")
                st.stop()

            _clear_output()

            # Build the backend kwargs through the shared single source of
            # truth (src.config.build_backend_kwargs) that the CLI and batch
            # entry points already use, so the Execute tab cannot drift on the
            # kwarg set or the default prompt names. The hub backend's "ignore
            # the Vertex model and use the stable hub alias" rule lives inside
            # that helper — the UI only assembles the per-run overrides.
            from src.config import build_backend_kwargs
            _backend_name = cfg.backend
            _run_settings = load_settings({
                "backend": _backend_name,
                "vertexai": {
                    "project_id": st.session_state.get("vai_project_id", vai_cfg.project_id),
                    "location": st.session_state.get("vai_location", vai_cfg.location),
                    "model": st.session_state.get("vai_model_id", vai_cfg.model),
                    "auth_mode": auth_mode,
                    "refine_iterations": st.session_state.get("vai_refine_iterations", vai_cfg.refine_iterations),
                    "clean_stop_max_errors": st.session_state.get("vai_clean_stop_max_errors", vai_cfg.clean_stop_max_errors),
                    "diminishing_returns_enabled": st.session_state.get("vai_diminishing_returns", vai_cfg.diminishing_returns_enabled),
                    "extraction_prompt": st.session_state.get("vai_extraction_prompt_file", vai_cfg.extraction_prompt),
                    "refinement_prompt": st.session_state.get("vai_refinement_prompt_file", vai_cfg.refinement_prompt),
                },
            })
            extra_kwargs: dict = build_backend_kwargs(_run_settings, dry_run=dry_run_check)

            log_q: queue.Queue = queue.Queue()
            result_q: queue.Queue = queue.Queue()

            thread = threading.Thread(
                target=run_execute_conversion,
                args=(pdf_path, _backend_name, verbose, result_q, log_q),
                kwargs={
                    "backend_kwargs": extra_kwargs,
                    "chunk_size": chunk_size,
                    "chunk_overlap": chunk_overlap,
                    "max_chunks": max_chunks,
                },
                daemon=True,
            )
            thread.start()

            st.session_state.ex_running = True
            st.session_state.ex_log_q = log_q
            st.session_state.ex_result_q = result_q
            st.session_state.ex_output_path = pdf_path.with_suffix(".md")
            st.session_state.ex_source_path = str(pdf_path)
            st.session_state.ex_max_chunks = max_chunks
            st.rerun()

    # ── 4. Poll log queue ───────────────────────────────────────────────────
    if st.session_state.ex_running:
        log_q = st.session_state.ex_log_q
        result_q = st.session_state.ex_result_q

        finished = False
        while True:
            try:
                msg = log_q.get_nowait()
            except queue.Empty:
                break
            if msg is None:
                finished = True
                break
            st.session_state.ex_logs.append(msg)

        if finished:
            st.session_state.ex_running = False
            if not result_q.empty():
                st.session_state.ex_result = result_q.get_nowait()
            st.rerun()

    # ── 5. Render logs ──────────────────────────────────────────────────────
    if st.session_state.ex_logs:
        render_log_box("ex_log_box", st.session_state.ex_logs)

    # ── 6. Show result ──────────────────────────────────────────────────────
    result_payload = st.session_state.ex_result
    if result_payload is not None and not st.session_state.ex_running:
        render_result(result_payload, st.session_state.ex_output_path)

    # ── 7. Keep polling while running ────────────────────────────────────────
    if st.session_state.ex_running:
        time.sleep(0.3)
        st.rerun()
