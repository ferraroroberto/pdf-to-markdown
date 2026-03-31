"""Streamlit entry point for the PDF → Markdown converter."""

import logging
import sys
from pathlib import Path

import streamlit as st

# Load .env from project root if present (populates GOOGLE_API_KEY)
try:
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).parent.parent / ".env", override=False)
except ImportError:
    pass

# Ensure project root is on sys.path so src.* imports resolve
sys.path.insert(0, str(Path(__file__).parent.parent))

# Initialise centralised logging (console=INFO, file=DEBUG in tmp/)
from src.logging_config import setup_logging  # noqa: E402
setup_logging()

# Suppress "missing ScriptRunContext" warnings from nested event loops (tkinter dialog).
logging.getLogger("streamlit.runtime.scriptrunner_utils.script_run_context").setLevel(
    logging.ERROR
)

st.set_page_config(
    page_title="PDF → Markdown",
    page_icon="📄",
    layout="wide",
)

st.markdown(
    """
<style>
    .stAppDeployButton { display: none; }
    [data-testid="stMetricLabel"] { font-size: 0.75rem !important; }
</style>
""",
    unsafe_allow_html=True,
)

# ── Sidebar ─────────────────────────────────────────────────────────────────────
from src.config import load_settings as _load_settings, save_settings as _save_settings  # noqa: E402

st.sidebar.title("PDF to Markdown")
st.sidebar.markdown("Convert PDF documents to clean Markdown for LLMs and other tools.")
st.sidebar.markdown("---")

_cfg = _load_settings()

# ── Machine selector ─────────────────────────────────────────────────────────
_machine_names = [m.name for m in _cfg.machines]
_active_idx = _machine_names.index(_cfg.active_machine) if _cfg.active_machine in _machine_names else 0

_selected_machine = st.sidebar.selectbox(
    "Machine",
    _machine_names,
    index=_active_idx,
    help="Select the machine profile to use. Each profile has its own Vertex AI settings.",
    key="global_machine",
)

if _selected_machine != _cfg.active_machine:
    _cfg.active_machine = _selected_machine
    _save_settings(_cfg)
    st.rerun()

# Reload settings after potential machine switch so vertexai is resolved correctly
_cfg = _load_settings()
_vai = _cfg.vertexai

st.sidebar.markdown("---")
st.sidebar.caption(f"Project root: `{Path(__file__).parent.parent}`")

# ── Tabs ─────────────────────────────────────────────────────────────────────────
tab_execute, tab_batch, tab_log, tab_settings, tab_vertexai = st.tabs(
    ["Convert File", "Batch Convert", "History", "Settings", "Vertex AI"]
)

with tab_execute:
    import execute  # noqa: PLC0415
    execute.run()

with tab_batch:
    import tab_batch as batch_tab  # noqa: PLC0415
    batch_tab.run()

with tab_log:
    import tab_log as log_tab  # noqa: PLC0415
    log_tab.run()

with tab_settings:
    import tab_settings as settings_tab  # noqa: PLC0415
    settings_tab.run()

with tab_vertexai:
    import tab_vertexai as vertexai_tab  # noqa: PLC0415
    vertexai_tab.run()
