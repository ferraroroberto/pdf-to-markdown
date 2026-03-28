"""Streamlit entry point for the PDF → Markdown converter."""

import logging
import sys
from pathlib import Path

import streamlit as st

# Load .env from project root if present (populates PROJECT_ID, GOOGLE_API_KEY, etc.)
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
    .stTabs { margin-top: -64px !important; }
    .stAppDeployButton { display: none; }
    [data-testid="stMetricLabel"] { font-size: 0.75rem !important; }
</style>
""",
    unsafe_allow_html=True,
)

# ── Sidebar ─────────────────────────────────────────────────────────────────────
from src.backends import list_available  # noqa: E402

st.sidebar.title("📄 PDF → Markdown")
st.sidebar.markdown("Convert PDF documents to clean Markdown for LLMs and other tools.")
st.sidebar.markdown("---")

_BACKEND_DESCRIPTIONS: dict[str, str] = {
    "pdfplumber": "born-digital PDFs, fast",
    "marker":     "high accuracy, GPU optional",
    "vertexai":   "Gemini on Vertex AI, cloud ☁️",
}

_installed = list_available()
_lines = ["**Available backends**", ""]
for _b, _desc in _BACKEND_DESCRIPTIONS.items():
    _tick = "✅" if _b in _installed else "○"
    _lines.append(f"{_tick} `{_b}` — {_desc}\n")
st.sidebar.markdown("\n".join(_lines))

st.sidebar.markdown("---")
st.sidebar.caption(f"Project root: `{Path(__file__).parent.parent}`")

# ── Tabs ─────────────────────────────────────────────────────────────────────────
tab_execute, tab_batch, tab_log, tab_settings = st.tabs(
    ["⚡ Execute", "📂 Batch", "📊 Log Viewer", "⚙️ Settings"]
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
