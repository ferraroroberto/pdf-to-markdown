"""PDF type classification — born-digital vs scanned."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import fitz  # PyMuPDF


@dataclass
class PDFInfo:
    """Summary information about a PDF document."""

    path: Path
    page_count: int
    is_scanned: bool
    has_text_layer: bool
    avg_chars_per_page: float
    has_images: bool

    @property
    def classification(self) -> str:
        """Return 'scanned' or 'born-digital'."""
        return "scanned" if self.is_scanned else "born-digital"


def classify_pdf(pdf_path: str | Path) -> PDFInfo:
    """Classify a PDF as born-digital or scanned by sampling its pages.

    Opens the PDF with PyMuPDF, samples up to 10 pages, and checks
    text density and image presence to determine the document type.
    """
    pdf_path = Path(pdf_path)
    doc = fitz.open(str(pdf_path))
    page_count = len(doc)

    sample_indices = list(range(min(page_count, 10)))

    total_chars = 0
    has_images = False
    has_text_layer = False

    for idx in sample_indices:
        page = doc[idx]
        text = page.get_text("text")
        char_count = len(text.strip())
        total_chars += char_count
        if char_count > 0:
            has_text_layer = True
        if page.get_images(full=True):
            has_images = True

    doc.close()

    avg_chars = total_chars / max(len(sample_indices), 1)
    is_scanned = avg_chars < 50

    return PDFInfo(
        path=pdf_path,
        page_count=page_count,
        is_scanned=is_scanned,
        has_text_layer=has_text_layer,
        avg_chars_per_page=avg_chars,
        has_images=has_images,
    )
