"""Batch tab — folder selection, batch processing, live log, results table."""

from __future__ import annotations

import html as _html
import logging
import os
import queue
import sys
import threading
import time
import tkinter as tk
from pathlib import Path
from tkinter import filedialog

import streamlit as st

from src.config import load_settings
from src.models import ChunkResult

_PROJECT_ROOT = Path(__file__).parent.parent

# Gemini model options
_VAI_MODELS: list[str] = [
    "gemini-2.5-pro",
    "gemini-2.5-flash",
    "gemini-3.1-flash-lite-preview",
]


# ── Logging / stream plumbing (reuse execute.py helpers) ───────────────────────

class _QueueHandler(logging.Handler):
    def __init__(self, q: queue.Queue) -> None:
        super().__init__()
        self._q = q

    def emit(self, record: logging.LogRecord) -> None:
        self._q.put(self.format(record))


class _TeeStream:
    def __init__(self, q: queue.Queue, orig) -> None:
        self._q = q
        self._orig = orig
        self._buf = ""

    def write(self, s: str) -> int:
        try:
            self._orig.write(s)
            self._orig.flush()
        except Exception:  # noqa: BLE001
            pass
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
        return False

    @property
    def encoding(self) -> str:
        return getattr(self._orig, "encoding", "utf-8") or "utf-8"

    @property
    def errors(self) -> str:
        return getattr(self._orig, "errors", "replace") or "replace"


# ── Worker ──────────────────────────────────────────────────────────────────────


def _run_batch_worker(
    folder: Path,
    output_dir: Path,
    backend_kwargs: dict,
    chunk_size: int,
    chunk_overlap: int,
    recursive: bool,
    verbose: bool,
    result_q: queue.Queue,
    log_q: queue.Queue,
) -> None:
    orig_stdout, orig_stderr = sys.stdout, sys.stderr
    sys.stdout = _TeeStream(log_q, orig_stdout)
    sys.stderr = _TeeStream(log_q, orig_stderr)

    root = logging.getLogger()
    handler = _QueueHandler(log_q)
    handler.setFormatter(logging.Formatter("%(levelname)-8s  %(name)s: %(message)s"))
    root.addHandler(handler)
    root.setLevel(logging.DEBUG if verbose else logging.INFO)

    try:
        from src.batch import run_batch
        from src.config import load_settings

        settings = load_settings({
            "vertexai": {
                "project_id": backend_kwargs.get("project_id", ""),
                "location": backend_kwargs.get("location", "europe-west3"),
                "model": backend_kwargs.get("model_id", "gemini-2.5-pro"),
                "auth_mode": backend_kwargs.get("auth_mode", "api"),
                "refine_iterations": backend_kwargs.get("refine_iterations", 0),
                "clean_stop_max_errors": backend_kwargs.get("clean_stop_max_errors", 0),
                "extraction_prompt": backend_kwargs.get("extraction_prompt_file", "prompts/extraction.md"),
                "refinement_prompt": backend_kwargs.get("refinement_prompt_file", "prompts/refinement.md"),
            },
            "processing": {
                "chunk_size": chunk_size,
                "chunk_overlap": chunk_overlap,
            },
            "batch": {
                "recursive": recursive,
            },
        })

        results = run_batch(
            folder=folder,
            output_dir=output_dir,
            settings=settings,
            validate_output=False,
            dry_run=backend_kwargs.get("dry_run", False),
            on_progress=lambda msg: log_q.put(msg),
        )
        result_q.put(("ok", results))
    except Exception as exc:  # noqa: BLE001
        result_q.put(("error", str(exc)))
    finally:
        root.removeHandler(handler)
        sys.stdout = orig_stdout
        sys.stderr = orig_stderr
        log_q.put(None)


# ── Session state ───────────────────────────────────────────────────────────────


def _init_state() -> None:
    cfg = load_settings()
    defaults = {
        "bt_running": False,
        "bt_logs": [],
        "bt_result": None,
        "bt_log_q": None,
        "bt_result_q": None,
        "bt_folder": "",
        "bt_output": "",
        "bt_auth_mode": cfg.vertexai.auth_mode,
        "bt_chunk_size": cfg.processing.chunk_size,
        "bt_chunk_overlap": cfg.processing.chunk_overlap,
        "bt_recursive": cfg.batch.recursive,
        "bt_verbose": False,
    }
    for k, v in defaults.items():
        if k not in st.session_state:
            st.session_state[k] = v


# ── Tab UI ──────────────────────────────────────────────────────────────────────


def run() -> None:
    """Render the Batch tab."""
    _init_state()
    cfg = load_settings()
    vai = cfg.vertexai
    proc = cfg.processing

    running: bool = st.session_state.bt_running

    st.subheader("📂 Batch Folder Processing")

    # ── Folder & Output selection ───────────────────────────────────────────
    fc1, fc2 = st.columns([5, 1])
    with fc2:
        st.markdown("<div style='padding-top:1.9rem'>", unsafe_allow_html=True)
        if st.button("🗂️  Browse…", width="stretch", key="bt_browse_folder", disabled=running):
            root = tk.Tk()
            root.withdraw()
            root.wm_attributes("-topmost", 1)
            chosen = filedialog.askdirectory(title="Select input folder")
            root.destroy()
            if chosen:
                st.session_state.bt_folder = chosen
        st.markdown("</div>", unsafe_allow_html=True)
    with fc1:
        folder_str: str = st.text_input(
            "Input folder",
            placeholder=r"C:\path\to\pdfs",
            key="bt_folder",
            disabled=running,
        )

    oc1, oc2 = st.columns([5, 1])
    with oc2:
        st.markdown("<div style='padding-top:1.9rem'>", unsafe_allow_html=True)
        if st.button("🗂️  Browse…", width="stretch", key="bt_browse_output", disabled=running):
            root = tk.Tk()
            root.withdraw()
            root.wm_attributes("-topmost", 1)
            chosen = filedialog.askdirectory(title="Select output folder")
            root.destroy()
            if chosen:
                st.session_state.bt_output = chosen
        st.markdown("</div>", unsafe_allow_html=True)
    with oc1:
        output_str: str = st.text_input(
            "Output folder",
            placeholder=r"C:\path\to\output",
            key="bt_output",
            disabled=running,
        )

    st.divider()

    # ── Options ─────────────────────────────────────────────────────────────
    st.subheader("⚙️ Options")

    col_auth, col_chunk, col_overlap, col_rec, col_verbose = st.columns([2, 2, 2, 1, 1])

    with col_auth:
        auth_idx = 0 if vai.auth_mode == "api" else 1
        auth_mode: str = st.selectbox(
            "Auth Mode",
            ["api", "gcloud"],
            index=auth_idx,
            key="bt_auth_mode",
            help="**api**: GOOGLE_API_KEY.  **gcloud**: Application Default Credentials.",
            disabled=running,
        )

    with col_chunk:
        chunk_size: int = st.number_input(
            "Chunk size (pages, 0 = off)",
            min_value=0,
            value=proc.chunk_size,
            step=5,
            key="bt_chunk_size_input",
            disabled=running,
        )

    with col_overlap:
        chunk_overlap: int = st.number_input(
            "Chunk overlap (pages)",
            min_value=0,
            value=proc.chunk_overlap,
            step=1,
            key="bt_chunk_overlap_input",
            disabled=running,
        )

    with col_rec:
        st.markdown('<div style="margin-top:2.3rem"></div>', unsafe_allow_html=True)
        recursive: bool = st.checkbox("Recursive", value=cfg.batch.recursive, key="bt_recursive_check", disabled=running)

    with col_verbose:
        st.markdown('<div style="margin-top:2.3rem"></div>', unsafe_allow_html=True)
        verbose: bool = st.checkbox("Verbose", key="bt_verbose_check", disabled=running)

    # ── Vertex AI options ───────────────────────────────────────────────────
    st.markdown("##### ☁️ Vertex AI Configuration")
    vb1, vb2, vb3 = st.columns([2, 2, 2])
    with vb1:
        bt_project_id: str = st.text_input(
            "Project ID", value=vai.project_id or os.getenv("PROJECT_ID", ""),
            key="bt_project_id", disabled=running,
        )
    with vb2:
        bt_location: str = st.text_input(
            "Location", value=vai.location, key="bt_location", disabled=running,
        )
    with vb3:
        _env_model = os.getenv("MODEL_ID", vai.model)
        _model_idx = _VAI_MODELS.index(_env_model) if _env_model in _VAI_MODELS else 0
        bt_model: str = st.selectbox(
            "Model", _VAI_MODELS, index=_model_idx, key="bt_model_id", disabled=running,
        )

    vb4, vb5, vb6 = st.columns([2, 2, 2])
    with vb4:
        bt_refine: int = st.slider(
            "Refinement passes", 0, 10, vai.refine_iterations,
            key="bt_refine_iterations", disabled=running,
        )
    with vb5:
        bt_extract_prompt: str = st.text_input(
            "Extraction prompt", value=vai.extraction_prompt,
            key="bt_extraction_prompt", disabled=running,
        )
    with vb6:
        if st.session_state.get("bt_refine_iterations", 0) > 0:
            bt_refine_prompt: str = st.text_input(
                "Refinement prompt", value=vai.refinement_prompt,
                key="bt_refinement_prompt", disabled=running,
            )

    st.divider()

    # ── Execute buttons ─────────────────────────────────────────────────────
    if not running:
        btn_col, dry_col, clean_col = st.columns([4, 2, 1])

        with clean_col:
            if st.button("🧹 Clean", width="stretch", key="bt_clean_btn"):
                st.session_state.bt_logs = []
                st.session_state.bt_result = None
                st.rerun()

        with dry_col:
            bt_dry_run = st.checkbox(
                "Dry run (estimate only)", key="bt_dry_run_check",
                help="No API calls — estimates token counts only.",
            )

        folder_valid = folder_str and Path(folder_str.strip()).is_dir()
        output_valid = bool(output_str.strip())

        bt_clicked = btn_col.button(
            "📂  Run Batch",
            type="primary",
            width="stretch",
            key="bt_execute_btn",
            disabled=(not folder_valid or not output_valid),
        )

        if not folder_valid:
            st.caption("⚠️ Select a valid input folder to enable execution.")
        elif not output_valid:
            st.caption("⚠️ Specify an output folder.")

        if bt_clicked:
            st.session_state.bt_logs = []
            st.session_state.bt_result = None

            backend_kwargs = {
                "project_id": st.session_state.get("bt_project_id", ""),
                "location": st.session_state.get("bt_location", "europe-west3"),
                "model_id": st.session_state.get("bt_model_id", "gemini-2.5-pro"),
                "auth_mode": auth_mode,
                "refine_iterations": st.session_state.get("bt_refine_iterations", 0),
                "clean_stop_max_errors": vai.clean_stop_max_errors,
                "extraction_prompt_file": st.session_state.get("bt_extraction_prompt", "prompts/extraction.md"),
                "refinement_prompt_file": st.session_state.get("bt_refinement_prompt", "prompts/refinement.md"),
                "dry_run": bt_dry_run,
            }

            log_q: queue.Queue = queue.Queue()
            result_q: queue.Queue = queue.Queue()

            thread = threading.Thread(
                target=_run_batch_worker,
                args=(
                    Path(folder_str.strip()),
                    Path(output_str.strip()),
                    backend_kwargs,
                    chunk_size,
                    chunk_overlap,
                    recursive,
                    verbose,
                    result_q,
                    log_q,
                ),
                daemon=True,
            )
            thread.start()

            st.session_state.bt_running = True
            st.session_state.bt_log_q = log_q
            st.session_state.bt_result_q = result_q
            st.rerun()

    # ── Poll log queue ──────────────────────────────────────────────────────
    if st.session_state.bt_running:
        log_q = st.session_state.bt_log_q
        result_q = st.session_state.bt_result_q

        finished = False
        while True:
            try:
                msg = log_q.get_nowait()
            except queue.Empty:
                break
            if msg is None:
                finished = True
                break
            st.session_state.bt_logs.append(msg)

        if finished:
            st.session_state.bt_running = False
            if not result_q.empty():
                st.session_state.bt_result = result_q.get_nowait()
            st.rerun()

    # ── Render logs ─────────────────────────────────────────────────────────
    if st.session_state.bt_logs:
        log_html = _html.escape("\n".join(reversed(st.session_state.bt_logs)))
        st.markdown(
            f"""<div style="margin-bottom:1rem">
                <div style="font-size:1.1rem;font-weight:600;margin-bottom:0.5rem">📋 Execution Log</div>
                <div style="height:320px;overflow:auto;background:#0d1117;border:1px solid #30363d;
                    border-radius:6px;padding:12px 16px;font-family:'SFMono-Regular',Consolas,monospace;
                    font-size:0.78rem;line-height:1.55;white-space:pre;color:#e6edf3">{log_html}</div>
            </div>""",
            unsafe_allow_html=True,
        )

    # ── Results table ───────────────────────────────────────────────────────
    result_payload = st.session_state.bt_result
    if result_payload is not None and not st.session_state.bt_running:
        st.divider()
        status, payload = result_payload

        if status == "error":
            st.error(f"Batch failed:\n\n```\n{payload}\n```")
        else:
            results: list[ChunkResult] = payload

            st.subheader("✅ Batch Results")

            # Summary metrics
            total_in = sum(r.metadata.get("total_input_tokens", 0) for r in results)
            total_out = sum(r.metadata.get("total_output_tokens", 0) for r in results)
            total_tok = total_in + total_out
            files = {r.source for r in results}
            failed = sum(1 for r in results if r.failed)
            models_used = {r.metadata.get("model", "") for r in results if r.metadata.get("model")}

            from src import vertexai_pricing
            pricing_data = vertexai_pricing.load_pricing()
            # Use the most common model for summary cost estimate
            model_for_cost = next(iter(models_used), "gemini-2.5-pro")
            cost_label, _ = vertexai_pricing.calculate_cost(model_for_cost, total_in, total_out, pricing_data)

            sm1, sm2, sm3, sm4, sm5, sm6 = st.columns(6)
            sm1.metric("Files", len(files))
            sm2.metric("Chunks/Results", len(results))
            sm3.metric("Failed", failed)
            sm4.metric("Input tokens", f"{total_in:,}")
            sm5.metric("Output tokens", f"{total_out:,}")
            sm6.metric("Est. cost", cost_label)

            st.caption(f"Model: {', '.join(models_used) or '—'}")

            # Per-result table
            table_rows = []
            for r in results:
                table_rows.append({
                    "File": r.source.name,
                    "Chunk": r.chunk_pages,
                    "Iteration": r.iteration,
                    "Errors": r.errors,
                    "Critical": r.critical,
                    "Moderate": r.moderate,
                    "Minor": r.minor,
                    "Verdict": r.verdict,
                    "Cost": r.cost_label,
                    "Error": r.error or "",
                })

            st.dataframe(table_rows, width="stretch")

    # ── Keep polling ────────────────────────────────────────────────────────
    if st.session_state.bt_running:
        time.sleep(1)
        st.rerun()
