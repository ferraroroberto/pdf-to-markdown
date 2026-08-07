"""Shared UI helpers for the Streamlit tabs.

The Convert File (``execute.py``) and Batch Convert (``tab_batch.py``) tabs grew
in parallel by copy-paste; this module is the single home for the plumbing they
both need so the two tabs cannot drift apart again:

- :class:`TeeStream` / :class:`QueueHandler` — tee stdout/stderr + logging
  records into a worker-thread queue for the live Execution Log.
- :func:`list_extraction_prompts` / :func:`list_refinement_prompts` — discover
  prompt files from ``prompts/`` filtered by filename prefix.
- :func:`sync_config_defaults_on_change` — refresh a tab's session-state
  defaults when ``config.json`` changes on disk, parameterised by key prefix.
- :func:`render_advanced_vertexai_options` — the "Advanced options" Vertex AI
  widget block, parameterised by the tab's widget-key names.
- :func:`drain_log_queue_and_maybe_finish` — drain the worker log queue into
  session state and finish the run when the sentinel arrives.
- :func:`render_log_box` — the dark-themed auto-scrolling Execution Log block.
"""

from __future__ import annotations

import html as _html
import queue as _queue
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, MutableMapping, Optional

import streamlit as st

from src.config import GEMINI_MODELS, VERTEXAI_FIELDS, load_settings

# TeeStream / QueueHandler now live in src.log_streaming (no Streamlit
# dependency) so the non-UI conversion worker can share them.  Re-exported here
# so the UI tabs keep importing them from _common.
from src.log_streaming import QueueHandler, TeeStream  # noqa: F401

_PROJECT_ROOT = Path(__file__).parent.parent
_CONFIG_PATH = _PROJECT_ROOT / "src" / "config.json"


# ── Prompt discovery ─────────────────────────────────────────────────────────


def list_prompts_by_prefix(prefix: str) -> list[str]:
    """Return all .md files in prompts/ whose filename starts with *prefix*."""
    return sorted(
        str(p.relative_to(_PROJECT_ROOT))
        for p in (_PROJECT_ROOT / "prompts").glob(f"{prefix}*.md")
    )


def list_extraction_prompts() -> list[str]:
    return list_prompts_by_prefix("extraction")


def list_refinement_prompts() -> list[str]:
    return list_prompts_by_prefix("refinement")


# ── Config-defaults sync ─────────────────────────────────────────────────────


def sync_config_defaults_on_change(
    running: bool,
    *,
    prefix: str,
    pop_keys: Iterable[str],
    extra: Optional[Callable[[object], None]] = None,
) -> None:
    """Refresh a tab's session-state defaults when config.json changes on disk.

    The Convert File and Batch tabs both watch ``config.json``'s mtime and, when
    it changes, reload chunk/refinement defaults and drop the cached widget keys
    so the widgets re-read the new defaults on the next render. Only the key
    *prefix* (``ex_`` vs ``bt_``) and the set of widget keys to clear differ —
    everything else is shared.

    Parameters
    ----------
    running:
        When True the tab is mid-conversion; never mutate state.
    prefix:
        Session-state key prefix for this tab (``"ex_"`` or ``"bt_"``).
    pop_keys:
        Widget ``key=`` names whose cached values should be cleared so the
        widgets re-read the refreshed defaults.
    extra:
        Optional hook called with the freshly-loaded ``Settings`` for tab-
        specific defaults that aren't shared (e.g. the Batch tab's
        ``recursive`` flag).
    """
    if running:
        return
    try:
        current_mtime = _CONFIG_PATH.stat().st_mtime_ns
    except OSError:
        return

    state_key = f"{prefix}config_mtime_ns"
    previous_mtime = st.session_state.get(state_key)
    if previous_mtime is None:
        st.session_state[state_key] = current_mtime
        return
    if previous_mtime == current_mtime:
        return

    cfg = load_settings()
    vai = cfg.vertexai
    proc = cfg.processing

    st.session_state[state_key] = current_mtime
    st.session_state[f"{prefix}chunk_size"] = proc.chunk_size
    st.session_state[f"{prefix}chunk_overlap"] = proc.chunk_overlap
    st.session_state[f"{prefix}diminishing_returns"] = vai.diminishing_returns_enabled

    if extra is not None:
        extra(cfg)

    for key in pop_keys:
        st.session_state.pop(key, None)


# ── Advanced options (Vertex AI) ─────────────────────────────────────────────


def render_advanced_vertexai_options(
    vai_cfg: Any,
    *,
    running: bool,
    keys: Mapping[str, str],
) -> dict[str, Any]:
    """Render the shared "Advanced options" Vertex AI widget block.

    The Convert File and Batch tabs offer the identical nine Vertex AI settings
    in the identical layout — only their widget ``key=`` names differ, and those
    are load-bearing (they are the session-state names the tabs read back and
    the ones cleared by :func:`sync_config_defaults_on_change`). So the key
    names are injected rather than derived from a prefix.

    Parameters
    ----------
    vai_cfg:
        The resolved ``VertexAISettings`` supplying each widget's default.
    running:
        When True the tab is mid-conversion and every widget is disabled.
    keys:
        Maps each name in :data:`src.config.VERTEXAI_FIELDS` to this tab's
        widget key (e.g. ``{"project_id": "vai_project_id", ...}``).

    Returns
    -------
    The collected values keyed by :data:`src.config.VERTEXAI_FIELDS` — shaped
    exactly like a ``load_settings`` ``vertexai`` override block, so callers
    pass it straight through instead of re-listing the nine fields.
    """
    missing = [name for name in VERTEXAI_FIELDS if name not in keys]
    if missing:
        raise KeyError(f"render_advanced_vertexai_options: missing widget keys for {missing}")

    values: dict[str, Any] = {}

    # Row 1: Project ID | Location | Refinement Passes
    adv1, adv2, adv3 = st.columns([2, 2, 2])
    with adv1:
        values["project_id"] = st.text_input(
            "Project ID",
            value=vai_cfg.project_id,
            help="Google Cloud project ID (from the active machine profile).",
            key=keys["project_id"],
            disabled=running,
        )
    with adv2:
        values["location"] = st.text_input(
            "Location",
            value=vai_cfg.location,
            help="Vertex AI region, e.g. europe-west3.",
            key=keys["location"],
            disabled=running,
        )
    with adv3:
        values["refine_iterations"] = st.number_input(
            "Refinement Passes",
            min_value=0,
            max_value=10,
            value=vai_cfg.refine_iterations,
            step=1,
            help="Number of refinement passes after extraction. 0 = extraction only.",
            key=keys["refine_iterations"],
            disabled=running,
        )

    # Row 2: Auth Mode | Model | Max Errors (CLEAN)
    adv4, adv5, adv6 = st.columns([2, 2, 2])
    with adv4:
        values["auth_mode"] = st.selectbox(
            "Auth Mode",
            ["api", "gcloud"],
            index=0 if vai_cfg.auth_mode == "api" else 1,
            help="**api**: uses GOOGLE_API_KEY.  **gcloud**: Application Default Credentials.",
            key=keys["auth_mode"],
            disabled=running,
        )
    with adv5:
        _model_idx = GEMINI_MODELS.index(vai_cfg.model) if vai_cfg.model in GEMINI_MODELS else 0
        values["model"] = st.selectbox(
            "Model",
            GEMINI_MODELS,
            index=_model_idx,
            help="Gemini model to use for extraction.",
            key=keys["model"],
            disabled=running,
        )
    with adv6:
        values["clean_stop_max_errors"] = st.number_input(
            "Max Errors (CLEAN)",
            min_value=-1,
            value=vai_cfg.clean_stop_max_errors,
            step=1,
            help=(
                "Early-stop threshold for refinement. "
                "**-1**: stop on any CLEAN verdict. **0**: only when 0 errors remain."
            ),
            key=keys["clean_stop_max_errors"],
            disabled=running,
        )

    values["diminishing_returns_enabled"] = st.checkbox(
        "Enable diminishing returns stop",
        value=vai_cfg.diminishing_returns_enabled,
        help=(
            "When enabled, refinement stops early if two consecutive passes show no "
            "reduction in errors."
        ),
        key=keys["diminishing_returns_enabled"],
        disabled=running,
    )

    # Row 3: Extraction Prompt | Refinement Prompt
    _ext_prompts = list_extraction_prompts()
    _ref_prompts = list_refinement_prompts()
    adv7, adv8 = st.columns([3, 3])
    with adv7:
        _ext_default = vai_cfg.extraction_prompt
        values["extraction_prompt"] = st.selectbox(
            "Extraction Prompt",
            _ext_prompts,
            index=_ext_prompts.index(_ext_default) if _ext_default in _ext_prompts else 0,
            key=keys["extraction_prompt"],
            disabled=running,
        )
    with adv8:
        _ref_default = vai_cfg.refinement_prompt
        values["refinement_prompt"] = st.selectbox(
            "Refinement Prompt",
            _ref_prompts,
            index=_ref_prompts.index(_ref_default) if _ref_default in _ref_prompts else 0,
            key=keys["refinement_prompt"],
            disabled=running,
        )

    return values


# ── Worker log-queue draining ────────────────────────────────────────────────


def drain_log_queue_and_maybe_finish(
    state: MutableMapping[str, Any],
    *,
    prefix: str,
) -> None:
    """Drain the worker log queue into session state; finish on the sentinel.

    Both conversion tabs poll their worker thread the same way: pull every
    queued log line into ``<prefix>logs``, and when the ``None`` sentinel
    arrives clear ``<prefix>running``, take the worker's payload off
    ``<prefix>result_q``, and rerun so the finished state renders.

    Parameters
    ----------
    state:
        The Streamlit session state (``st.session_state``).
    prefix:
        Session-state key prefix for this tab (``"ex_"`` or ``"bt_"``).
    """
    log_q = state[f"{prefix}log_q"]
    result_q = state[f"{prefix}result_q"]

    finished = False
    while True:
        try:
            msg = log_q.get_nowait()
        except _queue.Empty:
            break
        if msg is None:
            finished = True
            break
        state[f"{prefix}logs"].append(msg)

    if finished:
        state[f"{prefix}running"] = False
        if not result_q.empty():
            state[f"{prefix}result"] = result_q.get_nowait()
        st.rerun()


# ── Execution Log rendering ──────────────────────────────────────────────────


def render_log_box(log_id: str, lines: list[str]) -> None:
    """Render the dark-themed, auto-scrolling Execution Log block.

    *log_id* must be unique per tab so the auto-scroll script targets the right
    element (e.g. ``"ex_log_box"`` vs ``"bt_log_box"``).
    """
    log_html = _html.escape("\n".join(lines))
    st.markdown(
        f"""<div style="margin-bottom:1rem">
            <div style="font-size:1.1rem;font-weight:600;margin-bottom:0.5rem">Execution Log</div>
            <div id="{log_id}" style="height:320px;overflow:auto;background:#0d1117;border:1px solid #30363d;
                border-radius:6px;padding:12px 16px;font-family:'SFMono-Regular',Consolas,monospace;
                font-size:0.78rem;line-height:1.55;white-space:pre;color:#e6edf3">{log_html}</div>
        </div>
        <script>
            var el = document.getElementById("{log_id}");
            if (el) el.scrollTop = el.scrollHeight;
        </script>""",
        unsafe_allow_html=True,
    )
