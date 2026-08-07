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
    drain_log_queue_and_maybe_finish,
    render_advanced_vertexai_options,
    render_log_box,
    sync_config_defaults_on_change,
)
from execute_render import render_result
from remote_upload import is_remote_session, save_uploaded_file, ACCEPT_TYPES
from src.classifier import classify_pdf
from src.config import load_settings
from src.execute_worker import run_execute_conversion


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


# Widget keys this tab uses for the shared Advanced-options Vertex AI block,
# keyed by src.config.VERTEXAI_FIELDS name.
_ADV_KEYS = {
    "project_id": "vai_project_id",
    "location": "vai_location",
    "model": "vai_model_id",
    "auth_mode": "vai_auth_mode",
    "refine_iterations": "vai_refine_iterations",
    "clean_stop_max_errors": "vai_clean_stop_max_errors",
    "diminishing_returns_enabled": "vai_diminishing_returns",
    "extraction_prompt": "vai_extraction_prompt_file",
    "refinement_prompt": "vai_refinement_prompt_file",
}

# Widget keys cleared when config.json changes so the Execute-tab widgets
# re-read the refreshed defaults on the next render.
_SYNC_POP_KEYS = (
    "ex_chunk_size_input",
    "ex_chunk_overlap_input",
    *_ADV_KEYS.values(),
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
        vai_values = render_advanced_vertexai_options(
            vai_cfg, running=running, keys=_ADV_KEYS,
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
                "vertexai": vai_values,
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
        drain_log_queue_and_maybe_finish(st.session_state, prefix="ex_")

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
