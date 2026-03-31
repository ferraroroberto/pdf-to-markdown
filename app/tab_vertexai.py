"""VertexAI Info tab — pricing table, cache management, and usage link."""

from __future__ import annotations

from pathlib import Path

import streamlit as st

from src import vertexai_pricing
from src.config import load_settings

_PROJECT_ROOT = Path(__file__).parent.parent


def run() -> None:
    """Render the VertexAI Info tab."""
    cfg = load_settings()
    project_id = cfg.vertexai.project_id

    st.subheader("Vertex AI — Model Pricing")
    st.caption(
        "Pricing table fetched from the "
        "[Google Cloud Vertex AI pricing page](https://cloud.google.com/vertex-ai/generative-ai/pricing). "
        "Standard tier, USD per 1 million tokens, ≤ 200K input-token context window."
    )

    # ── Cache metadata & refresh ─────────────────────────────────────────────
    cache_info = vertexai_pricing.get_cache_info()

    meta_col, btn_col = st.columns([4, 1])
    with meta_col:
        if cache_info["cached"]:
            st.info(
                f"**{cache_info['num_models']} models** cached · Last updated: **{cache_info['fetched_at']}**",
                icon="ℹ️",
            )
        else:
            st.warning(
                f"Using built-in fallback pricing ({cache_info['num_models']} models). "
                "Click **Refresh** to fetch live data.",
                icon="⚠️",
            )
    with btn_col:
        st.markdown("<div style='padding-top:0.35rem'>", unsafe_allow_html=True)
        if st.button("🔄 Refresh pricing", key="vai_refresh_pricing", width="stretch"):
            with st.spinner("Fetching Vertex AI pricing…"):
                try:
                    vertexai_pricing.fetch_and_cache()
                    st.success("Pricing table updated.")
                except Exception as _e:  # noqa: BLE001
                    st.error(f"Fetch failed: {_e}")
            st.rerun()
        st.markdown("</div>", unsafe_allow_html=True)

    st.divider()

    # ── Pricing table (markdown viewer) ─────────────────────────────────────
    md_path = vertexai_pricing.CACHE_MD_PATH
    if md_path.exists():
        pricing_md = md_path.read_text(encoding="utf-8")
        st.markdown(pricing_md)
    else:
        pricing = vertexai_pricing.load_pricing()
        rows = ["| Model ID | Input ($/1M tokens) | Output ($/1M tokens) |",
                "|----------|---------------------|----------------------|"]
        for model_id in sorted(pricing):
            p = pricing[model_id]
            inp = f"${p['input']:.4f}" if "input" in p else "N/A"
            out = f"${p['output']:.4f}" if "output" in p else "N/A"
            rows.append(f"| `{model_id}` | {inp} | {out} |")
        st.markdown("\n".join(rows))

    st.divider()

    # ── Usage dashboard link ──────────────────────────────────────────────────
    st.markdown("#### Usage Dashboard")
    if project_id:
        usage_url = (
            f"https://console.cloud.google.com/vertex-ai/studio/settings/usage-dashboard"
            f"?orgonly=true&project={project_id}&supportedpurview=organizationId"
        )
        st.markdown(
            f"View API usage and quota for project **`{project_id}`** in the Google Cloud Console:",
        )
        st.link_button("Open Usage Dashboard ↗", usage_url)
    else:
        st.caption(
            "Set a **Project ID** in Settings to generate a direct link to the usage dashboard."
        )
