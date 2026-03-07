"""Docling backend — IBM's document understanding pipeline."""

from __future__ import annotations

from pathlib import Path

from src.backends.base import BaseBackend


class DoclingBackend(BaseBackend):
    """Wraps IBM's Docling library for document conversion.

    Docling uses DocLayNet for layout analysis and TableFormer for
    table structure recognition, making it especially strong on
    tables and patent documents.
    """

    name = "docling"

    @classmethod
    def is_available(cls) -> bool:
        try:
            from docling.document_converter import DocumentConverter  # noqa: F401
            return True
        except ImportError:
            return False

    def supports_scanned(self) -> bool:
        return True

    def convert(self, pdf_path: Path, **kwargs: object) -> tuple[str, dict]:
        from docling.datamodel.base_models import InputFormat
        from docling.datamodel.pipeline_options import PdfPipelineOptions
        from docling.document_converter import DocumentConverter, PdfFormatOption

        ocr = kwargs.get("ocr", True)
        table_mode = kwargs.get("table_mode", "accurate")

        pipeline_options = PdfPipelineOptions()
        pipeline_options.do_table_structure = True
        pipeline_options.do_ocr = bool(ocr)
        if table_mode == "fast":
            pipeline_options.table_structure_options = {"mode": "fast"}

        converter = DocumentConverter(
            format_options={
                InputFormat.PDF: PdfFormatOption(pipeline_options=pipeline_options),
            }
        )

        result = converter.convert(str(pdf_path))
        markdown = result.document.export_to_markdown()

        metadata: dict = {
            "page_count": getattr(result.document, "page_count", None),
            "backend": self.name,
        }

        return markdown, metadata
