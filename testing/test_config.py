"""Tests for src/config.py — settings load, save, and merge logic."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest

from src.config import (
    BatchSettings,
    LoggingSettings,
    ProcessingSettings,
    Settings,
    VertexAISettings,
    _deep_merge,
    load_settings,
    save_settings,
)


# ---------------------------------------------------------------------------
# _deep_merge
# ---------------------------------------------------------------------------


class TestDeepMerge:
    def test_flat_override(self):
        base = {"a": 1, "b": 2}
        result = _deep_merge(base, {"b": 99})
        assert result == {"a": 1, "b": 99}

    def test_nested_partial_override(self):
        base = {"x": {"a": 1, "b": 2}, "y": 3}
        result = _deep_merge(base, {"x": {"b": 99}})
        assert result == {"x": {"a": 1, "b": 99}, "y": 3}

    def test_override_does_not_mutate_base(self):
        base = {"a": {"nested": 1}}
        _deep_merge(base, {"a": {"nested": 2}})
        assert base["a"]["nested"] == 1

    def test_empty_override_returns_copy_of_base(self):
        base = {"a": 1}
        result = _deep_merge(base, {})
        assert result == {"a": 1}
        assert result is not base

    def test_override_replaces_list(self):
        base = {"extensions": [".pdf"]}
        result = _deep_merge(base, {"extensions": [".docx", ".pdf"]})
        assert result["extensions"] == [".docx", ".pdf"]

    def test_new_key_added(self):
        base = {"a": 1}
        result = _deep_merge(base, {"b": 2})
        assert result == {"a": 1, "b": 2}


# ---------------------------------------------------------------------------
# load_settings — defaults (no config file)
# ---------------------------------------------------------------------------


class TestLoadSettingsDefaults:
    def test_returns_settings_instance(self, tmp_path):
        nonexistent = tmp_path / "nonexistent.json"
        with patch("src.config._CONFIG_PATH", nonexistent):
            s = load_settings()
        assert isinstance(s, Settings)

    def test_default_backend(self, tmp_path):
        with patch("src.config._CONFIG_PATH", tmp_path / "missing.json"):
            s = load_settings()
        assert s.processing.backend == "vertexai"

    def test_default_chunk_size_zero(self, tmp_path):
        with patch("src.config._CONFIG_PATH", tmp_path / "missing.json"):
            s = load_settings()
        assert s.processing.chunk_size == 0

    def test_default_extensions(self, tmp_path):
        with patch("src.config._CONFIG_PATH", tmp_path / "missing.json"):
            s = load_settings()
        assert s.batch.extensions == [".pdf"]

    def test_default_location(self, tmp_path):
        with patch("src.config._CONFIG_PATH", tmp_path / "missing.json"):
            s = load_settings()
        assert s.vertexai.location == "europe-west3"


# ---------------------------------------------------------------------------
# load_settings — reads from file
# ---------------------------------------------------------------------------


class TestLoadSettingsFromFile:
    def test_reads_backend_from_file(self, tmp_path):
        cfg = tmp_path / "config.json"
        cfg.write_text(json.dumps({"processing": {"backend": "marker"}}), encoding="utf-8")
        with patch("src.config._CONFIG_PATH", cfg):
            s = load_settings()
        assert s.processing.backend == "marker"

    def test_reads_model_from_file(self, tmp_path):
        cfg = tmp_path / "config.json"
        cfg.write_text(json.dumps({"vertexai": {"model": "gemini-2.5-flash"}}), encoding="utf-8")
        with patch("src.config._CONFIG_PATH", cfg):
            s = load_settings()
        assert s.vertexai.model == "gemini-2.5-flash"

    def test_corrupt_json_falls_back_to_defaults(self, tmp_path):
        cfg = tmp_path / "config.json"
        cfg.write_text("this is not json", encoding="utf-8")
        with patch("src.config._CONFIG_PATH", cfg):
            s = load_settings()
        assert s.processing.backend == "vertexai"  # default

    def test_reads_nested_extensions_list(self, tmp_path):
        cfg = tmp_path / "config.json"
        cfg.write_text(
            json.dumps({"batch": {"extensions": [".pdf", ".docx"]}}), encoding="utf-8"
        )
        with patch("src.config._CONFIG_PATH", cfg):
            s = load_settings()
        assert s.batch.extensions == [".pdf", ".docx"]


# ---------------------------------------------------------------------------
# load_settings — overrides
# ---------------------------------------------------------------------------


class TestLoadSettingsOverrides:
    def test_override_replaces_value(self, tmp_path):
        with patch("src.config._CONFIG_PATH", tmp_path / "missing.json"):
            s = load_settings({"processing": {"backend": "pdfplumber"}})
        assert s.processing.backend == "pdfplumber"

    def test_override_partial_nested_keeps_other_keys(self, tmp_path):
        cfg = tmp_path / "config.json"
        cfg.write_text(
            json.dumps({"vertexai": {"model": "gemini-2.5-pro", "location": "us-east1"}}),
            encoding="utf-8",
        )
        with patch("src.config._CONFIG_PATH", cfg):
            s = load_settings({"vertexai": {"model": "gemini-2.5-flash"}})
        assert s.vertexai.model == "gemini-2.5-flash"
        assert s.vertexai.location == "us-east1"  # not overridden

    def test_override_chunk_size_type_coercion(self, tmp_path):
        with patch("src.config._CONFIG_PATH", tmp_path / "missing.json"):
            s = load_settings({"processing": {"chunk_size": "10"}})  # string → int
        assert s.processing.chunk_size == 10
        assert isinstance(s.processing.chunk_size, int)


# ---------------------------------------------------------------------------
# save_settings
# ---------------------------------------------------------------------------


class TestSaveSettings:
    def test_save_writes_valid_json(self, tmp_path):
        cfg = tmp_path / "config.json"
        with patch("src.config._CONFIG_PATH", cfg):
            s = load_settings()
            save_settings(s)
        data = json.loads(cfg.read_text(encoding="utf-8"))
        assert "processing" in data
        assert "vertexai" in data

    def test_save_and_reload_roundtrip(self, tmp_path):
        cfg = tmp_path / "config.json"
        with patch("src.config._CONFIG_PATH", cfg):
            s = load_settings({"processing": {"backend": "marker", "chunk_size": 5}})
            save_settings(s)
            s2 = load_settings()
        assert s2.processing.backend == "marker"
        assert s2.processing.chunk_size == 5

    def test_save_preserves_extensions_list(self, tmp_path):
        cfg = tmp_path / "config.json"
        with patch("src.config._CONFIG_PATH", cfg):
            s = load_settings({"batch": {"extensions": [".pdf", ".pptx"]}})
            save_settings(s)
            s2 = load_settings()
        assert s2.batch.extensions == [".pdf", ".pptx"]
