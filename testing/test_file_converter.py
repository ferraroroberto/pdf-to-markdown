"""Tests for src/file_converter.py — file type detection and PDF conversion."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from src.file_converter import (
    IMAGE_EXTENSIONS,
    OFFICE_EXTENSIONS,
    SUPPORTED_EXTENSIONS,
    convert_to_pdf,
    ensure_pdf,
    needs_conversion,
)


# ---------------------------------------------------------------------------
# needs_conversion
# ---------------------------------------------------------------------------


class TestNeedsConversion:
    @pytest.mark.parametrize("ext", [".pdf"])
    def test_pdf_does_not_need_conversion(self, ext, tmp_path):
        p = tmp_path / f"file{ext}"
        p.touch()
        assert needs_conversion(p) is False

    @pytest.mark.parametrize(
        "ext",
        [".docx", ".doc", ".odt", ".rtf", ".pptx", ".ppt", ".odp", ".xlsx", ".xls", ".ods"],
    )
    def test_office_extensions_need_conversion(self, ext, tmp_path):
        p = tmp_path / f"file{ext}"
        p.touch()
        assert needs_conversion(p) is True

    @pytest.mark.parametrize(
        "ext",
        [".jpg", ".jpeg", ".png", ".bmp", ".tiff", ".tif", ".webp", ".gif"],
    )
    def test_image_extensions_need_conversion(self, ext, tmp_path):
        p = tmp_path / f"file{ext}"
        p.touch()
        assert needs_conversion(p) is True

    def test_unknown_extension_does_not_need_conversion(self, tmp_path):
        p = tmp_path / "file.xyz"
        p.touch()
        assert needs_conversion(p) is False

    def test_case_insensitive(self, tmp_path):
        p = tmp_path / "FILE.DOCX"
        p.touch()
        assert needs_conversion(p) is True


# ---------------------------------------------------------------------------
# Extension set contents
# ---------------------------------------------------------------------------


class TestExtensionSets:
    def test_image_extensions_contains_common_formats(self):
        for ext in (".jpg", ".jpeg", ".png", ".gif", ".webp"):
            assert ext in IMAGE_EXTENSIONS

    def test_office_extensions_contains_word_excel_ppt(self):
        for ext in (".docx", ".pptx", ".xlsx"):
            assert ext in OFFICE_EXTENSIONS

    def test_supported_extensions_is_union(self):
        assert SUPPORTED_EXTENSIONS == OFFICE_EXTENSIONS | IMAGE_EXTENSIONS

    def test_pdf_not_in_supported_extensions(self):
        assert ".pdf" not in SUPPORTED_EXTENSIONS


# ---------------------------------------------------------------------------
# ensure_pdf — passthrough for existing PDF
# ---------------------------------------------------------------------------


class TestEnsurePdfPassthrough:
    def test_yields_same_path_for_pdf(self, minimal_pdf):
        with ensure_pdf(minimal_pdf) as result:
            assert result == minimal_pdf

    def test_no_temp_dir_created_for_pdf(self, minimal_pdf, tmp_path):
        before = set(tmp_path.iterdir())
        with ensure_pdf(minimal_pdf):
            after = set(tmp_path.iterdir())
        assert before == after  # nothing new created in tmp_path


# ---------------------------------------------------------------------------
# ensure_pdf — image conversion and cleanup
# ---------------------------------------------------------------------------


class TestEnsurePdfImageConversion:
    def test_converts_png_to_pdf(self, minimal_png):
        with ensure_pdf(minimal_png) as pdf_path:
            assert pdf_path.suffix.lower() == ".pdf"
            assert pdf_path.exists()

    def test_converted_pdf_is_valid(self, minimal_png):
        import fitz

        with ensure_pdf(minimal_png) as pdf_path:
            doc = fitz.open(str(pdf_path))
            page_count = doc.page_count
            doc.close()
        assert page_count >= 1

    def test_temp_dir_cleaned_up_after_context(self, minimal_png):
        with ensure_pdf(minimal_png) as pdf_path:
            tmp_dir = pdf_path.parent

        # After exiting the context manager the temp dir should be removed
        assert not tmp_dir.exists()

    def test_temp_dir_cleaned_up_on_exception(self, minimal_png):
        tmp_dir_ref = None
        try:
            with ensure_pdf(minimal_png) as pdf_path:
                tmp_dir_ref = pdf_path.parent
                raise RuntimeError("simulated error")
        except RuntimeError:
            pass

        assert tmp_dir_ref is not None
        assert not tmp_dir_ref.exists()


# ---------------------------------------------------------------------------
# convert_to_pdf — output directory handling
# ---------------------------------------------------------------------------


class TestConvertToPdfOutputDir:
    def test_creates_output_dir_if_missing(self, minimal_png, tmp_path):
        out_dir = tmp_path / "new" / "nested" / "dir"
        assert not out_dir.exists()
        pdf = convert_to_pdf(minimal_png, out_dir)
        assert out_dir.exists()
        assert pdf.parent == out_dir

    def test_uses_temp_dir_when_output_dir_is_none(self, minimal_png):
        pdf = convert_to_pdf(minimal_png, output_dir=None)
        assert pdf.exists()
        assert pdf.suffix == ".pdf"

    def test_raises_for_unsupported_extension(self, tmp_path):
        bad = tmp_path / "file.xyz"
        bad.touch()
        with pytest.raises(ValueError, match="Unsupported file type"):
            convert_to_pdf(bad, tmp_path)


# ---------------------------------------------------------------------------
# _office_to_pdf — raises when pywin32 is missing
# ---------------------------------------------------------------------------


class TestOfficeToPdfMissingDependency:
    def test_raises_runtime_error_without_pywin32(self, tmp_path):
        docx = tmp_path / "doc.docx"
        docx.touch()
        with patch.dict("sys.modules", {"win32com": None, "win32com.client": None}):
            with pytest.raises((RuntimeError, ImportError)):
                convert_to_pdf(docx, tmp_path)
