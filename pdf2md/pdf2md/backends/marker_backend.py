"""Marker backend — ML-powered PDF-to-Markdown conversion."""

from __future__ import annotations

from pathlib import Path

from pdf2md.backends.base import BaseBackend


class MarkerBackend(BaseBackend):
    """Wraps the marker-pdf library for high-quality PDF conversion.

    Marker uses deep-learning models (Surya) for layout detection and OCR,
    making it suitable for both born-digital and scanned PDFs.
    """

    name = "marker"

    @classmethod
    def is_available(cls) -> bool:
        try:
            import marker  # noqa: F401
            return True
        except ImportError:
            return False

    def supports_scanned(self) -> bool:
        return True

    def convert(self, pdf_path: Path, **kwargs: object) -> tuple[str, dict]:
        from marker.config.parser import ConfigParser
        from marker.converters.pdf import PdfConverter
        from marker.output import text_from_rendered

        config_dict: dict = {}

        force_ocr = kwargs.get("force_ocr", False)
        if force_ocr:
            config_dict["force_ocr"] = True

        page_range = kwargs.get("page_range")
        if page_range:
            config_dict["page_range"] = str(page_range)

        use_llm = kwargs.get("use_llm", False)
        if use_llm:
            config_dict["use_llm"] = True

        config_parser = ConfigParser(config_dict)
        converter = PdfConverter(config=config_parser.generate_config_dict())
        rendered = converter(str(pdf_path))
        markdown, _, images = text_from_rendered(rendered)

        metadata: dict = {
            "page_count": rendered.metadata.get("page_count") if isinstance(rendered.metadata, dict) else None,
            "backend": self.name,
        }

        return markdown, metadata
