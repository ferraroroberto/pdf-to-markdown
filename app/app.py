"""Streamlit entry point for the PDF → Markdown converter."""

import sys
from pathlib import Path

import streamlit as st

# Ensure project root is on sys.path so src.* imports resolve
sys.path.insert(0, str(Path(__file__).parent.parent))

st.set_page_config(
    page_title="PDF → Markdown",
    page_icon="📄",
    layout="wide",
)

st.markdown(
    """
<style>
    /* Align tabs flush with the top bar */
    .stTabs { margin-top: -64px !important; }
    /* Hide the Streamlit deploy button */
    .stAppDeployButton { display: none; }
    /* Tighten metric label text */
    [data-testid="stMetricLabel"] { font-size: 0.75rem !important; }
</style>
""",
    unsafe_allow_html=True,
)

# ── Sidebar ────────────────────────────────────────────────────────────────────
st.sidebar.title("📄 PDF → Markdown")
st.sidebar.markdown("Convert PDF documents to clean Markdown for LLMs and other tools.")
st.sidebar.markdown("---")
st.sidebar.markdown(
    "**Available backends**\n"
    "- `pdfplumber` — born-digital PDFs, fast\n"
    "- `marker` — high accuracy, GPU optional\n"
    "- `docling` — IBM Docling, structured output\n"
    "- **Auto** — classifies PDF and picks best"
)
st.sidebar.markdown("---")
st.sidebar.caption(f"Project root: `{Path(__file__).parent.parent}`")

# ── Tabs ───────────────────────────────────────────────────────────────────────
(tab_execute,) = st.tabs(["⚡ Execute"])

with tab_execute:
    import execute  # noqa: PLC0415

    execute.run()
