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
- :func:`render_log_box` — the dark-themed auto-scrolling Execution Log block.
"""

from __future__ import annotations

import html as _html
import io
import logging
import queue
from pathlib import Path
from typing import Callable, Iterable, Optional

import streamlit as st

from src.config import load_settings

_PROJECT_ROOT = Path(__file__).parent.parent
_CONFIG_PATH = _PROJECT_ROOT / "src" / "config.json"


# ── Stream tee + queue logging ───────────────────────────────────────────────


class TeeStream(io.TextIOBase):
    """Tee a text stream into a queue, line by line, while passing it through."""

    def __init__(self, log_queue: queue.Queue, original: io.TextIOBase) -> None:
        self._q = log_queue
        self._orig = original
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


class QueueHandler(logging.Handler):
    """A :class:`logging.Handler` that pushes formatted records onto a queue."""

    def __init__(self, log_queue: queue.Queue) -> None:
        super().__init__()
        self._q = log_queue

    def emit(self, record: logging.LogRecord) -> None:
        self._q.put(self.format(record))


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
