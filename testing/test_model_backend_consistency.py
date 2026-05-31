"""Regression tests for issue #17 — backend-attribute and model-id drift.

Two clusters guarded here:

1. ``ProcessingSettings`` no longer carries a ``backend`` field, so any code that
   reads ``settings.processing.backend`` crashes with ``AttributeError``. The
   CLI single/batch paths and the batch error paths must use the single
   hardcoded backend name instead.
2. The configured Gemini model id, every UI dropdown list, and the pricing table
   must agree on one canonical id sourced from ``src.config.GEMINI_MODELS``.
"""

from __future__ import annotations

import inspect
import json
from pathlib import Path

from src.config import GEMINI_MODELS, _CONFIG_PATH, load_settings
from src.vertexai_backend import VertexAIBackend, _resolve_prompt_path
from src.vertexai_pricing import _FALLBACK_PRICING, calculate_cost


# ---------------------------------------------------------------------------
# Cluster A — no source reads the removed ProcessingSettings.backend field
# ---------------------------------------------------------------------------


class TestNoProcessingBackendRead:
    def test_processing_settings_has_no_backend_field(self):
        settings = load_settings()
        assert not hasattr(settings.processing, "backend")

    def test_cli_and_batch_do_not_read_processing_backend(self):
        from src import batch, cli

        for module in (cli, batch):
            src_text = inspect.getsource(module)
            assert "processing.backend" not in src_text, (
                f"{module.__name__} still reads settings.processing.backend"
            )

    def test_backend_name_is_the_single_constant(self):
        assert VertexAIBackend.name == "vertexai"


# ---------------------------------------------------------------------------
# Cluster B — configured model id reconciles with dropdowns + pricing
# ---------------------------------------------------------------------------


class TestModelIdConsistency:
    def test_configured_model_is_in_shared_dropdown_list(self):
        settings = load_settings()
        assert settings.vertexai.model in GEMINI_MODELS

    def test_configured_model_has_pricing(self):
        settings = load_settings()
        label, found = calculate_cost(settings.vertexai.model, 1_000_000, 1_000_000)
        assert found is True
        assert label != "not found"

    def test_every_dropdown_model_has_pricing(self):
        for model in GEMINI_MODELS:
            label, found = calculate_cost(model, 1_000, 1_000)
            assert found is True, f"no pricing for dropdown model {model!r}"
            assert label != "not found"

    def test_dropdown_models_present_in_fallback_pricing(self):
        for model in GEMINI_MODELS:
            assert model in _FALLBACK_PRICING, (
                f"{model!r} missing from _FALLBACK_PRICING"
            )

    def test_ui_dropdowns_source_the_shared_constant(self):
        """All three UI model lists must reference GEMINI_MODELS, not inline copies.

        Read the source as text rather than importing the modules — the app
        package pulls in Streamlit, which is a UI-only dependency we don't want
        to require in the test environment.
        """
        app_dir = Path(__file__).resolve().parent.parent / "app"
        for filename in ("execute.py", "tab_batch.py", "tab_settings.py"):
            text = (app_dir / filename).read_text(encoding="utf-8")
            assert "GEMINI_MODELS" in text, (
                f"{filename} does not reference the shared GEMINI_MODELS constant"
            )
            # The old inline hard-coded model list must not survive.
            assert '"gemini-2.5-pro", "gemini-2.5-flash"' not in text, (
                f"{filename} still has an inline hard-coded model list"
            )


# ---------------------------------------------------------------------------
# Cluster B — prompt-path separators resolve cross-platform
# ---------------------------------------------------------------------------


class TestPromptPathResolution:
    def test_backslash_path_resolves_to_real_file(self):
        resolved = _resolve_prompt_path("prompts\\extraction_rag.md")
        assert resolved.exists()
        assert resolved.name == "extraction_rag.md"

    def test_forward_slash_path_resolves_to_real_file(self):
        resolved = _resolve_prompt_path("prompts/extraction_rag.md")
        assert resolved.exists()

    def test_absolute_path_passed_through(self, tmp_path: Path):
        target = tmp_path / "extraction.md"
        target.write_text("x", encoding="utf-8")
        assert _resolve_prompt_path(str(target)) == target


# ---------------------------------------------------------------------------
# config.json on disk uses the canonical id + forward-slash prompt paths
# ---------------------------------------------------------------------------


class TestConfigFileOnDisk:
    def test_active_machine_model_is_canonical(self):
        raw = json.loads(_CONFIG_PATH.read_text(encoding="utf-8"))
        active = next(m for m in raw["machines"] if m["name"] == raw["active_machine"])
        assert active["model"] in GEMINI_MODELS

    def test_config_prompt_paths_use_forward_slashes(self):
        raw = json.loads(_CONFIG_PATH.read_text(encoding="utf-8"))
        for machine in raw["machines"]:
            for key in ("extraction_prompt", "refinement_prompt"):
                assert "\\" not in machine[key], (
                    f"{key} for machine {machine['name']!r} uses a backslash"
                )
