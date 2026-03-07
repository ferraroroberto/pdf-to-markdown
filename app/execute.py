"""Execute tab — file selection, backend options, live log stream, result display."""

from __future__ import annotations

import html as html_module
import io
import logging
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
        result = pipe.convert(pdf_path, validate_output=validate)
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

            st.markdown(
                "<style>.file-info-block { font-size: 0.85rem; line-height: 1.4; color: inherit; }</style>",
                unsafe_allow_html=True,
            )
            st.markdown(
                f'<div class="file-info-block">'
                f'<div><strong>Filename:</strong> {html_module.escape(p.name)}</div>'
                f'<div><strong>Folder:</strong> {html_module.escape(str(p.parent))}</div>'
                f'<div><strong>Size:</strong> {size_kb:,.1f} KB  ·  <strong>Pages:</strong> {pages_val}</div>'
                f'</div>',
                unsafe_allow_html=True,
            )

            if pdf_info is not None:
                st.caption(
                    f"Classification: **{pdf_info.classification}**  ·  "
                    f"Avg chars/page: **{pdf_info.avg_chars_per_page:.0f}**  ·  "
                    f"Scanned: **{'Yes' if pdf_info.is_scanned else 'No'}**"
                )

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
        validate_output: bool = st.checkbox(
            "Validate",
            help="Run quality validation after conversion (character similarity, headings, tables).",
            key="validate_check",
            disabled=running,
        )

    with col_verbose:
        verbose: bool = st.checkbox(
            "Verbose",
            help="Show DEBUG-level log messages.",
            key="verbose_check",
            disabled=running,
        )

    if pdf_path is not None and not running:
        st.caption(f"Output will be saved to: `{pdf_path.with_suffix('.md')}`")

    st.divider()

    # ── 3. Execute / Cancel button ─────────────────────────────────────────
    # NOTE: we intentionally do NOT call st.rerun() inside these handlers.
    # Letting the script continue to steps 4-7 ensures the log/result sections
    # are reached with the freshly-cleared state, so they render nothing and
    # the old output disappears immediately without a flash.
    if not running:
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

            selected_backend: str | None = (
                None if backend_choice == "auto" else backend_choice
            )

            log_q: queue.Queue = queue.Queue()
            result_q: queue.Queue = queue.Queue()

            thread = threading.Thread(
                target=_run_conversion,
                args=(pdf_path, selected_backend, validate_output, verbose, result_q, log_q),
                daemon=True,
            )
            thread.start()

            # Clear previous output before the script reaches sections 5 and 7
            st.session_state.ex_logs = []
            st.session_state.ex_result = None
            st.session_state.ex_running = True
            st.session_state.ex_log_q = log_q
            st.session_state.ex_result_q = result_q
            st.session_state.ex_output_path = pdf_path.with_suffix(".md")
            # No st.rerun() — script continues; sections 5/7 see empty state

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

    # ── 5. Render logs ─────────────────────────────────────────────────────
    if st.session_state.ex_logs:
        import html as _html

        st.subheader("📋 Execution Log")
        log_html = _html.escape("\n".join(st.session_state.ex_logs))
        st.markdown(
            f"""<div style="
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
            ">{log_html}</div>""",
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

    with st.expander("📄 Markdown preview", expanded=True):
        preview = result.markdown[:6000]
        if len(result.markdown) > 6000:
            preview += "\n\n*… (truncated — open the file for full content)*"
        st.markdown(preview)

    with st.expander("📋 Raw Markdown (copy-ready)"):
        st.code(result.markdown, language="markdown")
