"""Execute tab — file selection, backend options, live log stream, result display."""

from __future__ import annotations

import io
import logging
import os
import queue
import sys
import time
import threading
import tkinter as tk
from tkinter import filedialog
from pathlib import Path

import streamlit as st

# src.* is importable because app.py prepended the project root to sys.path
from src.backends import list_available
from src.classifier import classify_pdf
from src.pipeline import Pipeline

# Gemini model options shown in the UI (order = dropdown order)
_VAI_MODELS: list[str] = [
    "gemini-2.5-pro",
    "gemini-2.5-flash",
    "gemini-3.1-flash-lite-preview",
]

# Approximate pricing per million tokens (USD); preview models billed at Flash-Lite rates
_GEMINI_PRICING: dict[str, dict[str, float]] = {
    "gemini-2.5-pro":                {"input": 1.25,  "output": 10.0},
    "gemini-2.5-flash":              {"input": 0.075, "output": 0.30},
    "gemini-3.1-flash-lite-preview": {"input": 0.0,   "output": 0.0},  # preview — no public price yet
}


# ── Stream tee (captures tqdm / print output) ──────────────────────────────────


class _TeeStream(io.TextIOBase):
    """Writes to both the original stream and the log queue, line-by-line.

    Setting isatty() → False tells tqdm to emit full lines (no \\r overwriting),
    which makes progress bars readable in the Streamlit log panel.
    """

    def __init__(self, log_queue: queue.Queue, original: io.TextIOBase) -> None:
        self._q = log_queue
        self._orig = original
        self._buf = ""

    def write(self, s: str) -> int:
        # Mirror to original terminal so the cmd window still shows output
        try:
            self._orig.write(s)
            self._orig.flush()
        except Exception:  # noqa: BLE001
            pass

        # Buffer until we have complete lines; strip \r from tqdm rewrites
        self._buf += s
        *lines, self._buf = self._buf.split("\n")
        for line in lines:
            clean = line.rstrip("\r").strip()
            if clean:
                self._q.put(clean)
        return len(s)

    def flush(self) -> None:
        try:
            self._orig.flush()
        except Exception:  # noqa: BLE001
            pass
        if self._buf.strip():
            self._q.put(self._buf.rstrip("\r").strip())
            self._buf = ""

    def isatty(self) -> bool:
        return False  # tqdm → full lines instead of \r rewrites

    @property
    def encoding(self) -> str:
        return getattr(self._orig, "encoding", "utf-8") or "utf-8"

    @property
    def errors(self) -> str:
        return getattr(self._orig, "errors", "replace") or "replace"


# ── Logging helper ─────────────────────────────────────────────────────────────


class _QueueHandler(logging.Handler):
    """Pushes formatted log records into a thread-safe queue."""

    def __init__(self, log_queue: queue.Queue) -> None:
        super().__init__()
        self.log_queue = log_queue

    def emit(self, record: logging.LogRecord) -> None:
        self.log_queue.put(self.format(record))


# ── Conversion worker ──────────────────────────────────────────────────────────


def _run_conversion(
    pdf_path: Path,
    backend: str | None,
    validate: bool,
    verbose: bool,
    result_queue: queue.Queue,
    log_queue: queue.Queue,
    backend_kwargs: dict | None = None,
) -> None:
    """Runs in a background thread; streams log lines and pushes final result.

    Redirects sys.stdout and sys.stderr through _TeeStream so that tqdm progress
    bars (written directly to stderr by marker / docling) appear in the log queue.
    """
    # ── Redirect stdout/stderr to capture tqdm and print() calls ──────────
    orig_stdout, orig_stderr = sys.stdout, sys.stderr
    sys.stdout = _TeeStream(log_queue, orig_stdout)
    sys.stderr = _TeeStream(log_queue, orig_stderr)

    # ── Attach logging handler ─────────────────────────────────────────────
    root = logging.getLogger()
    handler = _QueueHandler(log_queue)
    handler.setFormatter(logging.Formatter("%(levelname)-8s  %(name)s: %(message)s"))
    root.addHandler(handler)
    root.setLevel(logging.DEBUG if verbose else logging.INFO)

    try:
        pipe = Pipeline(backend=backend)
        result = pipe.convert(pdf_path, validate_output=validate, **(backend_kwargs or {}))
        result_queue.put(("ok", result))
    except Exception as exc:  # noqa: BLE001
        result_queue.put(("error", str(exc)))
    finally:
        root.removeHandler(handler)
        sys.stdout = orig_stdout
        sys.stderr = orig_stderr
        log_queue.put(None)  # sentinel — signals end of log stream


# ── Session state bootstrap ────────────────────────────────────────────────────


def _init_state() -> None:
    defaults = {
        "ex_running": False,
        "ex_logs": [],
        "ex_result": None,
        "ex_log_q": None,
        "ex_result_q": None,
        "ex_output_path": None,
    }
    for k, v in defaults.items():
        if k not in st.session_state:
            st.session_state[k] = v


def _clear_output() -> None:
    """Clear execution log, result, and all content below (log + result sections)."""
    st.session_state.ex_logs = []
    st.session_state.ex_result = None
    st.session_state.ex_output_path = None


# ── Tab UI ─────────────────────────────────────────────────────────────────────


def run() -> None:
    """Render the Execute tab."""
    _init_state()

    running: bool = st.session_state.ex_running

    # ── 1. File selection ──────────────────────────────────────────────────
    st.subheader("📂 Select PDF File")

    col_input, col_browse = st.columns([5, 1])

    with col_browse:
        st.markdown("<div style='padding-top:1.9rem'>", unsafe_allow_html=True)
        if st.button(
            "🗂️  Browse…",
            use_container_width=True,
            key="browse_btn",
            disabled=running,
        ):
            root = tk.Tk()
            root.withdraw()
            root.wm_attributes("-topmost", 1)
            chosen = filedialog.askopenfilename(
                title="Select a PDF file",
                filetypes=[("PDF files", "*.pdf"), ("All files", "*.*")],
            )
            root.destroy()
            if chosen:
                st.session_state.file_path_input = chosen
        st.markdown("</div>", unsafe_allow_html=True)

    with col_input:
        file_path_str = st.text_input(
            "File path",
            placeholder=r"C:\path\to\document.pdf",
            help="Paste the full local path to the PDF file, or use Browse to pick one.",
            key="file_path_input",
            disabled=running,
        )

    pdf_path: Path | None = None

    if file_path_str:
        p = Path(file_path_str.strip().strip('"'))
        if not p.exists():
            st.error(f"File not found: `{p}`")
        elif p.suffix.lower() != ".pdf":
            st.error("Only `.pdf` files are supported.")
        else:
            pdf_path = p
            size_kb = p.stat().st_size / 1024
            pages_val: str | int = "—"
            pdf_info = None

            with st.spinner("Inspecting PDF…"):
                try:
                    pdf_info = classify_pdf(pdf_path)
                    pages_val = pdf_info.page_count
                except Exception:  # noqa: BLE001
                    pass

            # File info: Size, Pages, Classification (and extras when available) in one line
            cols = st.columns(5)
            cols[0].metric("Size", f"{size_kb:,.1f} KB")
            cols[1].metric("Pages", pages_val)
            cols[2].metric("Classification", pdf_info.classification if pdf_info is not None else "—")
            cols[3].metric("Avg chars/page", f"{pdf_info.avg_chars_per_page:.0f}" if pdf_info is not None else "—")
            cols[4].metric("Scanned", "Yes" if pdf_info and pdf_info.is_scanned else ("No" if pdf_info else "—"))

    st.divider()

    # ── 2. Options ─────────────────────────────────────────────────────────
    st.subheader("⚙️ Options")

    available_backends = list_available()
    backend_options = ["auto"] + available_backends
    backend_labels: dict[str, str] = {
        "auto": "Auto — classify and pick best",
        "pdfplumber": "pdfplumber  (born-digital, fast)",
        "marker": "marker  (high accuracy, GPU optional)",
        "docling": "docling  (IBM Docling, structured)",
        "vertexai": "vertexai  (Gemini on Vertex AI, cloud)",
    }

    col_backend, col_validate, col_verbose = st.columns([3, 1, 1])

    with col_backend:
        backend_choice: str = st.selectbox(  # type: ignore[assignment]
            "Backend",
            backend_options,
            format_func=lambda x: backend_labels.get(x, x),
            help="Select the extraction engine. 'Auto' classifies the PDF and selects the best installed backend.",
            key="backend_select",
            disabled=running,
        )

    with col_validate:
        st.markdown('<div style="margin-top: 2.3rem;"></div>', unsafe_allow_html=True)  # align with selectbox
        validate_output: bool = st.checkbox(
            "Validate",
            value=True,
            help="Run quality validation after conversion (character similarity, headings, tables).",
            key="validate_check",
            disabled=running,
        )

    with col_verbose:
        st.markdown('<div style="margin-top: 2.3rem;"></div>', unsafe_allow_html=True)  # align with selectbox
        verbose: bool = st.checkbox(
            "Verbose",
            help="Show DEBUG-level log messages.",
            key="verbose_check",
            disabled=running,
        )

    # ── VertexAI-specific options ──────────────────────────────────────────
    if backend_choice == "vertexai":
        st.markdown("##### ☁️ Vertex AI Configuration")
        vai_col1, vai_col2, vai_col3 = st.columns([2, 2, 2])

        with vai_col1:
            st.text_input(
                "Project ID",
                value=os.getenv("PROJECT_ID", ""),
                help="Your Google Cloud project ID.",
                key="vai_project_id",
                disabled=running,
            )

        with vai_col2:
            st.text_input(
                "Location",
                value=os.getenv("LOCATION", "europe-west3"),
                help="Vertex AI region, e.g. europe-west3.",
                key="vai_location",
                disabled=running,
            )

        with vai_col3:
            _env_model = os.getenv("MODEL_ID", "gemini-2.5-pro")
            _model_idx = _VAI_MODELS.index(_env_model) if _env_model in _VAI_MODELS else 0
            st.selectbox(
                "Model",
                _VAI_MODELS,
                index=_model_idx,
                help="Gemini model to use for extraction.",
                key="vai_model_id",
                disabled=running,
            )

        vai_col4, vai_col5, vai_col6 = st.columns([2, 2, 2])

        with vai_col4:
            st.slider(
                "Iterative refinement passes (0 = extraction only)",
                min_value=0,
                max_value=5,
                value=0,
                key="vai_refine_iterations",
                disabled=running,
            )

        with vai_col5:
            st.text_input(
                "Extraction prompt file",
                value="prompts/extraction.md",
                help="Path to the extraction prompt (relative to project root).",
                key="vai_extraction_prompt_file",
                disabled=running,
            )

        with vai_col6:
            if st.session_state.get("vai_refine_iterations", 0) > 0:
                st.text_input(
                    "Refinement prompt file",
                    value="prompts/refinement.md",
                    help="Path to the refinement prompt (relative to project root).",
                    key="vai_refinement_prompt_file",
                    disabled=running,
                )

    if pdf_path is not None and not running:
        st.caption(f"Output will be saved to: `{pdf_path.with_suffix('.md')}`")

    st.divider()

    # ── 3. Execute / Clear / Cancel buttons ─────────────────────────────────
    # NOTE: we intentionally do NOT call st.rerun() inside Execute/Cancel handlers.
    # Letting the script continue to steps 4-7 ensures the log/result sections
    # are reached with the freshly-cleared state, so they render nothing and
    # the old output disappears immediately without a flash.
    if not running:
        col_execute, col_clear = st.columns([1, 1])
        with col_execute:
            if st.button(
                "⚡  Execute",
                type="primary",
                disabled=(pdf_path is None),
                use_container_width=True,
                key="execute_btn",
            ):
                if pdf_path is None:
                    st.warning("Please select a valid PDF file first.")
                    st.stop()

                # Step 1: clear all content below (log + result)
                _clear_output()

                selected_backend: str | None = (
                    None if backend_choice == "auto" else backend_choice
                )

                # Collect backend-specific kwargs
                extra_kwargs: dict = {}
                if backend_choice == "vertexai":
                    extra_kwargs = {
                        "project_id": st.session_state.get("vai_project_id", ""),
                        "location": st.session_state.get("vai_location", "europe-west3"),
                        "model_id": st.session_state.get("vai_model_id", "gemini-2.5-pro"),
                        "refine_iterations": st.session_state.get("vai_refine_iterations", 0),
                        "extraction_prompt_file": st.session_state.get(
                            "vai_extraction_prompt_file", "prompts/extraction.md"
                        ),
                        "refinement_prompt_file": st.session_state.get(
                            "vai_refinement_prompt_file", "prompts/refinement.md"
                        ),
                    }

                log_q: queue.Queue = queue.Queue()
                result_q: queue.Queue = queue.Queue()

                thread = threading.Thread(
                    target=_run_conversion,
                    args=(pdf_path, selected_backend, validate_output, verbose, result_q, log_q),
                    kwargs={"backend_kwargs": extra_kwargs},
                    daemon=True,
                )
                thread.start()

                # Step 2: run conversion (state for polling)
                st.session_state.ex_running = True
                st.session_state.ex_log_q = log_q
                st.session_state.ex_result_q = result_q
                st.session_state.ex_output_path = pdf_path.with_suffix(".md")
                # No st.rerun() — script continues; sections 5/7 see empty state

        with col_clear:
            if st.button(
                "🗑️  Clear",
                type="secondary",
                use_container_width=True,
                key="clear_btn",
                help="Clear the execution log and result; removes all content below.",
            ):
                _clear_output()
                st.rerun()

    else:
        if st.button(
            "⛔  Cancel",
            type="secondary",
            use_container_width=True,
            key="cancel_btn",
        ):
            st.session_state.ex_running = False
            st.session_state.ex_logs.append(
                "— Cancelled by user. The backend may still be running in the background. —"
            )
            # No st.rerun() — script continues to render the final log state

    # ── 4. Poll log queue (uses current session state, not stale `running`) ─
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
            # Rerun so the top of the page (Browse, Execute/Cancel) re-renders
            # with running=False; otherwise they stay disabled/stuck on Cancel.
            st.rerun()

    # ── 5. Render logs (single container to avoid "two boxes" flash) ─────────
    if st.session_state.ex_running or st.session_state.ex_logs:
        import html as _html

        log_html = _html.escape("\n".join(st.session_state.ex_logs))
        # Single markdown block: title + log div in one element so Streamlit
        # doesn't render two separate blocks (subheader + div) that can appear
        # as two boxes and then collapse to one on rerun.
        with st.container(key="execution_log_container"):
            st.markdown(
                f"""<div class="exec-log-outer" style="
                    margin-bottom: 1rem;
                ">
                    <div style="
                        font-size: 1.1rem;
                        font-weight: 600;
                        color: inherit;
                        margin-bottom: 0.5rem;
                    ">📋 Execution Log</div>
                    <div style="
                        height: 320px;
                        overflow: auto;
                        background: #0d1117;
                        border: 1px solid #30363d;
                        border-radius: 6px;
                        padding: 12px 16px;
                        font-family: 'SFMono-Regular', Consolas, 'Liberation Mono', Menlo, monospace;
                        font-size: 0.78rem;
                        line-height: 1.55;
                        white-space: pre;
                        color: #e6edf3;
                    ">{log_html}</div>
                </div>""",
                unsafe_allow_html=True,
            )

    # ── 6. Keep polling while still running ────────────────────────────────
    if st.session_state.ex_running:
        time.sleep(1)
        st.rerun()
        return

    # ── 7. Show result once finished ───────────────────────────────────────
    result_payload = st.session_state.ex_result
    if result_payload is None:
        return

    status, payload = result_payload
    output_path: Path = st.session_state.ex_output_path

    st.divider()

    if status == "error":
        st.error(f"Conversion failed:\n\n```\n{payload}\n```")
        return

    result = payload

    # Save alongside source
    result.save(output_path)

    st.subheader("✅ Result")

    m1, m2, m3, m4 = st.columns(4)
    m1.metric("Backend", result.backend_used)
    m2.metric("Pages", result.page_count if result.page_count is not None else "—")
    m3.metric("Characters", f"{len(result.markdown):,}")
    m4.metric("Tokens (est.)", f"~{result.token_estimate:,}")

    if result.validation:
        v = result.validation
        v_label = "PASS ✅" if v.passed else "FAIL ❌"
        st.info(
            f"Validation: **{v_label}** — "
            f"similarity **{v.char_similarity:.1%}** — "
            f"**{len(v.warnings)}** warning(s)"
        )
        if v.warnings:
            with st.expander("Validation warnings"):
                for w in v.warnings:
                    st.write(f"- {w}")

    st.success(f"Saved → `{output_path}`")

    # ── VertexAI-specific result details ───────────────────────────────────
    if result.backend_used == "vertexai":
        meta = result.metadata
        total_in = meta.get("total_input_tokens", 0)
        total_out = meta.get("total_output_tokens", 0)
        total_tok = meta.get("total_tokens", 0)
        model_used: str = meta.get("model", "gemini-2.5-pro")
        iters_done: int = meta.get("iterations_completed", 0)
        final_verdict: str = meta.get("final_verdict", "N/A")

        pricing = _GEMINI_PRICING.get(model_used, {"input": 0.0, "output": 0.0})
        cost_usd = (total_in / 1_000_000) * pricing["input"] + (total_out / 1_000_000) * pricing["output"]
        cost_label = f"${cost_usd:.4f}" if pricing["input"] > 0 else "preview"

        st.markdown("#### ☁️ Vertex AI Usage")
        vc1, vc2, vc3, vc4, vc5 = st.columns(5)
        vc1.metric("Model", model_used)
        vc2.metric("Input tokens", f"{total_in:,}")
        vc3.metric("Output tokens", f"{total_out:,}")
        vc4.metric("Total tokens", f"{total_tok:,}")
        vc5.metric("Est. cost", cost_label)

        refinement_log: list[dict] = meta.get("refinement_log", [])
        if refinement_log:
            st.markdown("#### 🔄 Refinement Track Record")
            st.info(
                f"**{iters_done}** refinement pass(es) completed — "
                f"final verdict: **{final_verdict}**"
            )
            rows_md = (
                "| Iteration | Errors | Critical | Moderate | Minor | Verdict |\n"
                "|-----------|--------|----------|----------|-------|---------|"
            )
            for row in refinement_log:
                verdict_icon = "✅" if row["verdict"] == "CLEAN" else ("⚠️" if row["verdict"] == "NEEDS ANOTHER PASS" else "❓")
                rows_md += (
                    f"\n| {row['iteration']} | {row['errors_found']} | "
                    f"{row['critical']} | {row['moderate']} | {row['minor']} | "
                    f"{verdict_icon} {row['verdict']} |"
                )
            st.markdown(rows_md)
        else:
            st.info("Extraction only — no refinement passes were run.")

    with st.expander("📄 Markdown preview", expanded=True):
        preview = result.markdown[:6000]
        if len(result.markdown) > 6000:
            preview += "\n\n*… (truncated — open the file for full content)*"
        st.markdown(preview)

    with st.expander("📋 Raw Markdown (copy-ready)"):
        st.code(result.markdown, language="markdown")
