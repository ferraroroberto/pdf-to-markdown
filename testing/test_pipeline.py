"""Tests for Pipeline orchestration."""

from __future__ import annotations

from pathlib import Path

import src.pipeline as pipeline_mod
from src.models import ConversionResult
from src.pipeline import Pipeline


class _ZeroPageBackend:
    name = "zero-page-backend"

    def convert(self, pdf_path: Path, **kwargs: object) -> tuple[str, dict]:
        return "# Converted", {"page_count": 0}


def test_pipeline_replaces_non_positive_backend_page_count(monkeypatch, minimal_pdf):
    monkeypatch.setitem(pipeline_mod._BACKENDS, _ZeroPageBackend.name, _ZeroPageBackend)

    result = Pipeline(backend=_ZeroPageBackend.name).convert(minimal_pdf)

    assert isinstance(result, ConversionResult)
    assert result.page_count == 5
