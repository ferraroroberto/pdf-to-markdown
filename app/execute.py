"""Execute tab — file selection, backend options, live log stream, result display."""

from __future__ import annotations

import logging
import queue
import sys
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
    QueueHandler,
    TeeStream,
    list_extraction_prompts,
    list_refinement_prompts,
    render_log_box,
    sync_config_defaults_on_change,
)
from remote_upload import is_remote_session, save_uploaded_file, ACCEPT_TYPES
from src import vertexai_pricing
from src.classifier import classify_pdf
from src.config import DEFAULT_MODEL, GEMINI_MODELS, load_settings
from src.corrections_report import (
    aggregate_chunked_vertex_metadata,
    build_refinement_track_table,
    save_chunk_corrections_report,
    save_corrections_report,
)
from src.logger_exec import log_conversion_steps
from src.logging_config import get_file_handler
from src.models import ConversionResult
from src.pipeline import Pipeline

# Both the Vertex AI and hub Gemini backends emit the same rich metadata
# contract (extraction_step, refinement_log, raw_responses, token usage), so
# the verbose-artifact, usage-panel, and refinement-track UI applies to both.
_GEMINI_STYLE_BACKENDS = frozenset({"vertexai", "hubgemini"})

# Gemini model options shown in the UI (order = dropdown order).
# Sourced from the single shared constant in src.config so the Execute, Batch,
# and Settings dropdowns and config.json never drift apart.
_VAI_MODELS: list[str] = GEMINI_MODELS


# ── Prior artifact cleanup (Execute tab) ────────────────────────────────────────


def _erase_prior_execution_artifacts(
    parent: Path,
    stem: str,
    *,
    protect_resolved: frozenset[Path] | None = None,
    logger: logging.Logger | None = None,
    log_removals: bool = False,
    preserve_chunk_files: bool = True,
) -> None:
    """Remove files from a previous run that share *stem* (main output basename).

    Matches ``{stem}.*`` and ``{stem}_chunk_*``, and removes the legacy
    ``_chunks_{stem}/`` temp directory.  Paths whose resolved path is in
    *protect_resolved* are skipped (typically the current source PDF).

    When *preserve_chunk_files* is ``True`` (the default), files matching
    ``{stem}.chunk_*.pdf``, ``{stem}.chunk_*.md``, and
    ``{stem}.chunk_*.corrections.md`` are **kept** so a subsequent run can
    resume from where the previous one left off.  Set to ``False`` to force a
    full clean restart (e.g. when the user explicitly requests it).
    """
    import shutil as _shutil

    protect = protect_resolved or frozenset()

    # Patterns that belong to resumable chunk artifacts (flat-layout naming)
    _CHUNK_SUFFIXES = (".pdf", ".md", ".corrections.md")

    def _is_chunk_artifact(p: Path) -> bool:
        """Return True if *p* is a resumable chunk file (e.g. stem.chunk_001.md)."""
        for sfx in _CHUNK_SUFFIXES:
            if p.name.endswith(sfx):
                inner = p.name[: -len(sfx)]
                # Check if remainder looks like "{stem}.chunk_NNN"
                if inner.startswith(f"{stem}.chunk_"):
                    tail = inner[len(f"{stem}.chunk_"):]
                    if tail.isdigit():
                        return True
        return False

    def _unlink(p: Path) -> None:
        if not p.is_file():
            return
        try:
            if p.resolve() in protect:
                return
        except OSError:
            return
        if preserve_chunk_files and _is_chunk_artifact(p):
            return
        try:
            p.unlink()
            if log_removals and logger is not None:
                logger.info("ℹ️ Removed prior artifact: %s", p.name)
        except OSError as exc:
            if logger is not None:
                logger.warning("⚠️ Could not remove %s: %s", p.name, exc)

    seen: set[Path] = set()
    for pattern in (f"{stem}.*", f"{stem}_chunk_*"):
        for p in parent.glob(pattern):
            if not p.is_file():
                continue
            try:
                key = p.resolve()
            except OSError:
                continue
            if key in seen:
                continue
            seen.add(key)
            _unlink(p)

    # Legacy temp subdir created by old-style split_pdf (always removed — not resumable)
    chunks_dir = parent / f"_chunks_{stem}"
    if chunks_dir.is_dir():
        try:
            _shutil.rmtree(chunks_dir, ignore_errors=True)
            if log_removals and logger is not None:
                logger.info("ℹ️ Removed prior chunk temp dir: %s", chunks_dir.name)
        except OSError as exc:
            if logger is not None:
                logger.warning("⚠️ Could not remove %s: %s", chunks_dir, exc)


# ── Conversion worker ───────────────────────────────────────────────────────────


def _run_conversion(
    pdf_path: Path,
    backend: str,
    verbose: bool,
    result_queue: queue.Queue,
    log_queue: queue.Queue,
    backend_kwargs: dict | None = None,
    chunk_size: int = 0,
    chunk_overlap: int = 1,
    max_chunks: int = 0,
) -> None:
    import shutil as _shutil
    import tempfile as _tempfile

    orig_stdout, orig_stderr = sys.stdout, sys.stderr
    sys.stdout = TeeStream(log_queue, orig_stdout)
    sys.stderr = TeeStream(log_queue, orig_stderr)

    root = logging.getLogger()
    handler = QueueHandler(log_queue)
    handler.setFormatter(logging.Formatter("%(levelname)-8s  %(name)s: %(message)s"))
    handler.setLevel(logging.DEBUG if verbose else logging.INFO)
    root.addHandler(handler)
    root.setLevel(logging.DEBUG)  # let handlers decide

    # Ensure the rotating file handler (DEBUG level) is also present in this thread
    file_handler = get_file_handler()
    _added_file_handler = False
    if file_handler and file_handler not in root.handlers:
        root.addHandler(file_handler)
        _added_file_handler = True

    try:
        pipe = Pipeline(backend=backend)
        kwargs = dict(backend_kwargs or {})

        output_dir = pdf_path.parent
        output_stem = pdf_path.stem

        _protect = frozenset({pdf_path.resolve()}) if pdf_path.exists() else frozenset()
        _erase_prior_execution_artifacts(
            output_dir,
            output_stem,
            protect_resolved=_protect,
            logger=root,
            log_removals=verbose,
        )

        # In verbose mode, pass save dir to backend so raw AI responses are
        # written to disk immediately after each API call.
        if verbose and backend in _GEMINI_STYLE_BACKENDS:
            kwargs["verbose_save_dir"] = output_dir
            kwargs["verbose_file_stem"] = output_stem

        from src.file_converter import needs_conversion
        needs_conv = needs_conversion(pdf_path)

        # For non-PDF files: convert to PDF upfront when chunking is requested
        # or verbose mode is on (so the converted PDF is saved for inspection).
        # Otherwise the pipeline handles conversion internally via ensure_pdf().
        _tmp_conv_dir: Path | None = None
        working_pdf = pdf_path

        if needs_conv and (chunk_size > 0 or verbose):
            from src.file_converter import convert_to_pdf
            if verbose:
                # Save converted PDF permanently next to the source file
                working_pdf = convert_to_pdf(pdf_path, output_dir)
                root.info(
                    "ℹ️ Converted %s → %s (saved for inspection)",
                    pdf_path.name, working_pdf.name,
                )
            else:
                # Temporary directory — cleaned up in finally block
                _tmp_conv_dir = Path(_tempfile.mkdtemp(prefix="pdf2md_conv_"))
                working_pdf = convert_to_pdf(pdf_path, _tmp_conv_dir)

        try:
            from src.vertexai_pricing import load_pricing
            pricing_data = load_pricing()

            if chunk_size > 0:
                from src.chunker import merge_chunks, split_pdf

                # Chunks are written directly to output_dir with consistent naming
                # ({stem}.chunk_NNN.pdf) so they persist for resume and inspection.
                all_chunk_list = split_pdf(
                    working_pdf,
                    chunk_size=chunk_size,
                    overlap=chunk_overlap,
                    output_dir=output_dir,
                    file_stem=output_stem,
                )
                total_available = len(all_chunk_list)

                if max_chunks > 0 and max_chunks < total_available:
                    root.info(
                        "ℹ️ Processing first %d of %d chunk(s) (max_chunks=%d)",
                        max_chunks, total_available, max_chunks,
                    )
                    chunks = all_chunk_list[:max_chunks]
                else:
                    chunks = all_chunk_list

                # Detect already-completed chunks from a prior (interrupted) run.
                resumable: set[int] = {
                    chunk_idx
                    for chunk_idx, _, _, _ in chunks
                    if (
                        (output_dir / f"{output_stem}.chunk_{chunk_idx + 1:03d}.md").exists()
                        and (output_dir / f"{output_stem}.chunk_{chunk_idx + 1:03d}.md").stat().st_size > 0
                    )
                }
                if resumable:
                    root.info(
                        "ℹ️ Resume detected: %d/%d chunk(s) already complete — "
                        "skipping those and continuing from chunk %d.",
                        len(resumable), len(chunks),
                        min(set(range(len(chunks))) - resumable, default=len(chunks)) + 1,
                    )

                chunk_markdowns: list[str] = []
                chunk_metas: list[tuple[int, str, dict]] = []

                for chunk_idx, chunk_path, start_page, end_page in chunks:
                    chunk_num = chunk_idx + 1
                    pages_label = f"{start_page}-{end_page}"
                    chunk_md_path = output_dir / f"{output_stem}.chunk_{chunk_num:03d}.md"

                    # --- Resume: load existing markdown, skip API call ---
                    if chunk_idx in resumable:
                        existing_md = chunk_md_path.read_text(encoding="utf-8")
                        root.info(
                            "ℹ️ Chunk %d/%d — pages %s — ✅ resuming from saved file",
                            chunk_num, len(chunks), pages_label,
                        )
                        chunk_markdowns.append(existing_md)
                        chunk_metas.append((chunk_idx, pages_label, {}))
                        continue

                    root.info(
                        "ℹ️ Chunk %d/%d — pages %s",
                        chunk_num, len(chunks), pages_label,
                    )

                    chunk_kwargs = dict(kwargs)
                    if backend in _GEMINI_STYLE_BACKENDS:
                        chunk_stem = f"{output_stem}.chunk_{chunk_num:03d}"
                        if verbose:
                            chunk_kwargs["verbose_save_dir"] = output_dir
                            chunk_kwargs["verbose_file_stem"] = chunk_stem

                    try:
                        r = pipe.convert(chunk_path, validate_output=False, **chunk_kwargs)
                        chunk_markdowns.append(r.markdown)
                        chunk_metas.append((chunk_idx, pages_label, r.metadata))

                        # Always save chunk markdown immediately — enables resume
                        chunk_md_path.write_text(r.markdown, encoding="utf-8")
                        root.debug("Saved chunk %d markdown → %s", chunk_num, chunk_md_path.name)

                        # Always save per-chunk corrections log immediately
                        corr = save_chunk_corrections_report(
                            r.metadata, output_dir, output_stem, chunk_num, pages_label,
                        )
                        if corr:
                            root.debug("Saved chunk %d corrections → %s", chunk_num, corr.name)

                        _log_conversion_steps(str(pdf_path), chunk_idx, pages_label, r, pricing_data)
                    except Exception as exc:  # noqa: BLE001
                        root.warning("⚠️ Chunk %d failed: %s — skipping", chunk_idx, exc)
                        chunk_markdowns.append(
                            f"\n\n> ⚠️ Chunk {chunk_num} (pages {start_page}–{end_page}) failed: {exc}\n\n"
                        )

                # Merge chunks with robust error handling
                try:
                    merged = merge_chunks(chunk_markdowns, chunk_overlap=chunk_overlap)
                except Exception as exc:  # noqa: BLE001
                    root.error(
                        "❌ merge_chunks failed (%s) — falling back to plain join", exc,
                    )
                    merged = "\n\n---\n\n".join(m for m in chunk_markdowns if m and m.strip())

                if backend in _GEMINI_STYLE_BACKENDS and chunk_metas:
                    combined_meta = {
                        **aggregate_chunked_vertex_metadata(chunk_metas),
                        "chunks": len(chunks),
                        "chunk_size": chunk_size,
                    }
                else:
                    combined_meta = {
                        **(chunk_metas[-1][2] if chunk_metas else {}),
                        "chunks": len(chunks),
                        "chunk_size": chunk_size,
                    }
                result = ConversionResult(
                    source=pdf_path,
                    markdown=merged,
                    backend_used=backend,
                    metadata=combined_meta,
                )
            else:
                # If we pre-converted (verbose + non-PDF), use the saved PDF path
                # directly so the pipeline skips internal conversion.
                r = pipe.convert(working_pdf, validate_output=False, **kwargs)
                # Always report source as the original input file
                if working_pdf != pdf_path:
                    result = ConversionResult(
                        source=pdf_path,
                        markdown=r.markdown,
                        backend_used=r.backend_used,
                        metadata=r.metadata,
                        validation=r.validation,
                    )
                else:
                    result = r
                if result.backend_used in _GEMINI_STYLE_BACKENDS:
                    result.metadata["refinement_track_table"] = build_refinement_track_table(
                        result.metadata, 1, "all",
                    )
                _log_conversion_steps(str(pdf_path), 0, "all", result, pricing_data)

        finally:
            if _tmp_conv_dir is not None:
                _shutil.rmtree(_tmp_conv_dir, ignore_errors=True)

        result_queue.put(("ok", result))
    except Exception as exc:  # noqa: BLE001
        result_queue.put(("error", str(exc)))
    finally:
        root.removeHandler(handler)
        if _added_file_handler and file_handler:
            root.removeHandler(file_handler)
        sys.stdout = orig_stdout
        sys.stderr = orig_stderr
        log_queue.put(None)


def _log_conversion_steps(
    file: str,
    chunk_idx: int,
    chunk_pages: str,
    result: ConversionResult,
    pricing_data: dict,
) -> None:
    """Write exec-log rows for *result* via the shared logger_exec helper."""
    meta = result.metadata
    log_conversion_steps(
        file=file,
        chunk_idx=chunk_idx,
        chunk_pages=chunk_pages,
        meta=meta,
        pricing_data=pricing_data,
        model=meta.get("model", ""),
        auth_mode=meta.get("auth_mode", ""),
        extraction_prompt_hash=meta.get("extraction_prompt_hash", ""),
        refinement_prompt_hash=meta.get("refinement_prompt_hash", ""),
    )


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
        "ex_verbose": False,
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
    st.session_state.ex_verbose = False


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
                target=_run_conversion,
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
            st.session_state.ex_verbose = verbose
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
        status, payload = result_payload
        output_path: Path = st.session_state.ex_output_path

        st.divider()

        if status == "error":
            st.error(f"Conversion failed:\n\n```\n{payload}\n```")
        else:
            result = payload
            result.save(output_path)

            _step_paths: list[Path] = []
            _raw_paths: list[Path] = []
            _chunk_md_paths: list[Path] = []
            _chunk_corr_paths: list[Path] = []
            _chunk_pdf_paths: list[Path] = []
            if st.session_state.get("ex_verbose", False) and result.backend_used in _GEMINI_STYLE_BACKENDS:
                # Save processed markdown snapshots after each step
                for _idx, _iter_md in enumerate(result.metadata.get("iteration_markdowns", []), 1):
                    _step_path = output_path.with_name(f"{output_path.stem}.step_{_idx:02d}.md")
                    _step_path.write_text(_iter_md, encoding="utf-8")
                    _step_paths.append(_step_path)
                # Collect raw response files already written by the backend
                for stale in sorted(output_path.parent.glob(f"{output_path.stem}.raw_step_*.txt")):
                    _raw_paths.append(stale)
                # Updated pattern: chunk raw files now use dot-naming
                for stale in sorted(output_path.parent.glob(f"{output_path.stem}.chunk_*.raw_step_*.txt")):
                    _raw_paths.append(stale)
            # Chunk artifacts (always present when chunking was used, not just verbose)
            if result.metadata.get("chunks", 0) > 1:
                for p in sorted(output_path.parent.glob(f"{output_path.stem}.chunk_*.pdf")):
                    _chunk_pdf_paths.append(p)
                for p in sorted(output_path.parent.glob(f"{output_path.stem}.chunk_*.md")):
                    # Exclude the final merged markdown itself
                    if p != output_path:
                        _chunk_md_paths.append(p)
                for p in sorted(output_path.parent.glob(f"{output_path.stem}.chunk_*.corrections.md")):
                    _chunk_corr_paths.append(p)

            corrections_path: Path | None = None
            if result.backend_used in _GEMINI_STYLE_BACKENDS:
                corrections_path = save_corrections_report(
                    result.source.name, result.metadata, output_path,
                )

            st.subheader("Result")

            # Summary line instead of 4 metric cards
            _pages = result.page_count if result.page_count is not None else "?"
            st.success(
                f"Converted **{_pages} pages** using **{result.backend_used}** "
                f"({len(result.markdown):,} chars, ~{result.token_estimate:,} tokens). "
                f"Saved to `{output_path.name}`"
            )

            # Download button for the markdown
            st.download_button(
                label="Download Markdown",
                data=result.markdown,
                file_name=output_path.name,
                mime="text/markdown",
                key="download_md_btn",
            )

            if _step_paths or _raw_paths or _chunk_pdf_paths or _chunk_md_paths or _chunk_corr_paths:
                with st.expander("Saved artifacts"):
                    if _step_paths:
                        st.caption(f"Intermediate steps ({len(_step_paths)}): " +
                                   ", ".join(f"`{p.name}`" for p in _step_paths))
                    if _raw_paths:
                        st.caption(f"Raw AI responses ({len(_raw_paths)}): " +
                                   ", ".join(f"`{p.name}`" for p in _raw_paths))
                    if _chunk_pdf_paths:
                        st.caption(f"Chunk PDFs ({len(_chunk_pdf_paths)}): " +
                                   ", ".join(f"`{p.name}`" for p in _chunk_pdf_paths))
                    if _chunk_md_paths:
                        st.caption(
                            f"Chunk markdowns ({len(_chunk_md_paths)}) — "
                            "kept for resume: " +
                            ", ".join(f"`{p.name}`" for p in _chunk_md_paths)
                        )
                    if _chunk_corr_paths:
                        st.caption(
                            f"Per-chunk corrections ({len(_chunk_corr_paths)}): " +
                            ", ".join(f"`{p.name}`" for p in _chunk_corr_paths)
                        )
            if corrections_path is not None:
                st.caption(f"Corrections log: `{corrections_path.name}`")

            if result.backend_used in _GEMINI_STYLE_BACKENDS:
                meta = result.metadata
                total_in = meta.get("total_input_tokens", 0)
                total_out = meta.get("total_output_tokens", 0)
                total_tok = meta.get("total_tokens", 0)
                model_used: str = meta.get("model", DEFAULT_MODEL)
                iters_done: int = meta.get("iterations_completed", 0)
                final_verdict: str = meta.get("final_verdict", "N/A")

                if result.backend_used == "hubgemini":
                    # The hub's Gemini (agy) path does not surface token counts,
                    # so cost can't be estimated for this backend.
                    st.markdown("#### Hub Gemini Usage")
                    st.caption(
                        f"**Model**: {model_used} (via local LLM hub) · "
                        "**Tokens**: not reported by the hub Gemini path"
                    )
                else:
                    _pricing_data = vertexai_pricing.load_pricing()
                    cost_label, _ = vertexai_pricing.calculate_cost(
                        model_used, total_in, total_out, _pricing_data
                    )
                    st.markdown("#### Vertex AI Usage")
                    st.caption(
                        f"**Model**: {model_used} · "
                        f"**Tokens**: {total_in:,} in / {total_out:,} out ({total_tok:,} total) · "
                        f"**Est. cost**: {cost_label}"
                    )

                track_table: list[dict] = meta.get("refinement_track_table") or []
                chunk_summaries: list[dict] = meta.get("chunk_refine_summaries") or []
                refinement_log: list[dict] = meta.get("refinement_log", [])
                if track_table or refinement_log:
                    st.markdown("#### Refinement Track Record")
                    if chunk_summaries and len(chunk_summaries) > 1:
                        bullets = "  \n".join(
                            f"- **Chunk {s['chunk']}** (pages {s['pages']}): "
                            f"{s['iterations_completed']} refinement pass(es), verdict **{s['final_verdict']}**"
                            for s in chunk_summaries
                        )
                        st.info(
                            f"**{len(chunk_summaries)} chunks** — **{iters_done}** refinement pass(es) in total "
                            f"(sum across chunks). **Overall**: **{final_verdict}**  \n{bullets}",
                        )
                    else:
                        st.info(
                            f"**{iters_done}** refinement pass(es) — final verdict: **{final_verdict}**",
                        )
                    if track_table:
                        # Coerce every cell to str so PyArrow never sees mixed types per column
                        # (e.g. extraction uses "—" for iteration, refinements use int).
                        def _track_cell(v: object) -> str:
                            return "—" if v is None else str(v)

                        display_rows = []
                        for row in track_table:
                            display_rows.append({
                                "Chunk": _track_cell(row["chunk"]),
                                "Pages": _track_cell(row["pages"]),
                                "Step": _track_cell(row["step"]),
                                "Type": _track_cell(row["step_type"]),
                                "Iter.": _track_cell(row["iteration"]),
                                "Errors": _track_cell(row["errors"]),
                                "Crit.": _track_cell(row["critical"]),
                                "Mod.": _track_cell(row["moderate"]),
                                "Minor": _track_cell(row["minor"]),
                                "Verdict": _track_cell(row["verdict"]),
                                "In tok": _track_cell(row["in_tok"]),
                                "Out tok": _track_cell(row["out_tok"]),
                            })
                        st.dataframe(display_rows, width="stretch")
                    else:
                        rows_md = (
                            "| Iteration | Errors | Critical | Moderate | Minor | Verdict |\n"
                            "|-----------|--------|----------|----------|-------|---------|"
                        )
                        for row in refinement_log:
                            icon = "✅" if row["verdict"] == "CLEAN" else (
                                "⚠️" if row["verdict"] == "NEEDS ANOTHER PASS" else "❓"
                            )
                            rows_md += (
                                f"\n| {row['iteration']} | {row['errors_found']} | "
                                f"{row['critical']} | {row['moderate']} | {row['minor']} | {icon} {row['verdict']} |"
                            )
                        st.markdown(rows_md)
                else:
                    st.info("Extraction only — no refinement passes were run.")

            with st.expander("Markdown preview", expanded=True):
                st.markdown(
                    f"""<div style="max-height:500px;overflow:auto;background:#161b22;
                        border:1px solid #30363d;border-radius:6px;padding:16px;
                        font-size:0.85rem;line-height:1.6;color:#e6edf3">
                        {result.markdown[:20000]}
                    </div>""",
                    unsafe_allow_html=True,
                )
                if len(result.markdown) > 20000:
                    st.caption("Showing first 20,000 characters. Download or view raw for full content.")

            with st.expander("Raw Markdown (copy-ready)"):
                st.code(result.markdown, language="markdown")

            if corrections_path is not None and corrections_path.exists():
                _corrections_text = corrections_path.read_text(encoding="utf-8")
                with st.expander("Corrections Preview"):
                    st.markdown(_corrections_text)
                with st.expander("Corrections Raw (copy-ready)"):
                    st.code(_corrections_text, language="markdown")

    # ── 7. Keep polling while running ────────────────────────────────────────
    if st.session_state.ex_running:
        time.sleep(0.3)
        st.rerun()
