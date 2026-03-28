"""Pre-processing: convert Office documents and images to PDF before extraction.

Only used for the Vertex AI backend.
- Word / PowerPoint / other Office formats → Microsoft Office COM (pywin32) → PDF
- Images (JPEG, PNG, BMP, TIFF, WebP, GIF) → PyMuPDF → single-page PDF
"""

from __future__ import annotations

import logging
import shutil
import tempfile
from contextlib import contextmanager
from pathlib import Path
from typing import Generator

logger = logging.getLogger("file_converter")

# File types handled by LibreOffice
OFFICE_EXTENSIONS: frozenset[str] = frozenset({
    ".docx", ".doc", ".odt", ".rtf",       # Word
    ".pptx", ".ppt", ".odp",               # PowerPoint
    ".xlsx", ".xls", ".ods",               # Excel / spreadsheets
})

# Image types handled by PyMuPDF
IMAGE_EXTENSIONS: frozenset[str] = frozenset({
    ".jpg", ".jpeg", ".png", ".bmp",
    ".tiff", ".tif", ".webp", ".gif",
})

# All supported non-PDF extensions
SUPPORTED_EXTENSIONS: frozenset[str] = OFFICE_EXTENSIONS | IMAGE_EXTENSIONS


def needs_conversion(path: Path) -> bool:
    """Return True if the file needs pre-conversion to PDF."""
    return path.suffix.lower() in SUPPORTED_EXTENSIONS


def convert_to_pdf(source: Path, output_dir: Path | None = None) -> Path:
    """Convert *source* to PDF and return the path to the generated PDF.

    Parameters
    ----------
    source:
        Input file (Word, PowerPoint, Excel, or image).
    output_dir:
        Directory to write the PDF into.  A temporary directory is created if
        *output_dir* is None — the caller is responsible for cleanup.

    Returns
    -------
    Path to the generated ``.pdf`` file inside *output_dir*.
    """
    if output_dir is None:
        output_dir = Path(tempfile.mkdtemp(prefix="pdf2md_conv_"))
    else:
        output_dir.mkdir(parents=True, exist_ok=True)

    suffix = source.suffix.lower()
    if suffix in IMAGE_EXTENSIONS:
        return _image_to_pdf(source, output_dir)
    if suffix in OFFICE_EXTENSIONS:
        return _office_to_pdf(source, output_dir)
    raise ValueError(f"Unsupported file type: {suffix!r}")


@contextmanager
def ensure_pdf(source: Path) -> Generator[Path, None, None]:
    """Context manager: yield a PDF path for *source*, converting if needed.

    If *source* is already a ``.pdf``, yields it unchanged with no cleanup.
    If *source* needs conversion, converts to a temp directory, yields the
    resulting PDF, and cleans up the temp directory on exit.
    """
    if source.suffix.lower() == ".pdf":
        yield source
        return

    tmp_dir = Path(tempfile.mkdtemp(prefix="pdf2md_conv_"))
    try:
        pdf_path = convert_to_pdf(source, tmp_dir)
        yield pdf_path
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


# ── Internal helpers ──────────────────────────────────────────────────────────


def _office_to_pdf(source: Path, output_dir: Path) -> Path:
    """Convert an Office document to PDF using Microsoft Office COM automation.

    Requires pywin32 (pip install pywin32) and Microsoft Office to be installed.
    Works for Word (.doc/.docx/.rtf/.odt), Excel (.xls/.xlsx/.ods),
    and PowerPoint (.ppt/.pptx/.odp).
    """
    try:
        import win32com.client
    except ImportError:
        raise RuntimeError(
            "pywin32 is required for Office-to-PDF conversion. "
            "Install it with: pip install pywin32"
        )

    suffix = source.suffix.lower()
    pdf_path = output_dir / (source.stem + ".pdf")
    abs_source = str(source.resolve())
    abs_pdf = str(pdf_path.resolve())

    logger.info("Converting %s to PDF via Microsoft Office COM…", source.name)

    if suffix in {".doc", ".docx", ".rtf", ".odt"}:
        _word_to_pdf(win32com.client, abs_source, abs_pdf)
    elif suffix in {".xls", ".xlsx", ".ods"}:
        _excel_to_pdf(win32com.client, abs_source, abs_pdf)
    elif suffix in {".ppt", ".pptx", ".odp"}:
        _powerpoint_to_pdf(win32com.client, abs_source, abs_pdf)
    else:
        raise ValueError(f"Unsupported Office format: {suffix!r}")

    if not pdf_path.exists():
        raise FileNotFoundError(
            f"Office COM conversion finished but expected PDF not found: {pdf_path}"
        )

    logger.info(
        "Converted %s → %s (%.1f KB)",
        source.name, pdf_path.name, pdf_path.stat().st_size / 1024,
    )
    return pdf_path


def _word_to_pdf(com, source: str, pdf_path: str) -> None:
    word = com.Dispatch("Word.Application")
    word.Visible = False
    doc = None
    try:
        doc = word.Documents.Open(source)
        doc.SaveAs(pdf_path, FileFormat=17)  # 17 = wdFormatPDF
    finally:
        if doc is not None:
            doc.Close(False)
        word.Quit()


def _excel_to_pdf(com, source: str, pdf_path: str) -> None:
    excel = com.Dispatch("Excel.Application")
    excel.Visible = False
    wb = None
    try:
        wb = excel.Workbooks.Open(source)
        wb.ExportAsFixedFormat(0, pdf_path)  # 0 = xlTypePDF
    finally:
        if wb is not None:
            wb.Close(False)
        excel.Quit()


def _powerpoint_to_pdf(com, source: str, pdf_path: str) -> None:
    ppt = com.Dispatch("PowerPoint.Application")
    prs = None
    try:
        prs = ppt.Presentations.Open(source, WithWindow=False)
        prs.SaveAs(pdf_path, 32)  # 32 = ppSaveAsPDF
    finally:
        if prs is not None:
            prs.Close()
        ppt.Quit()


def _image_to_pdf(source: Path, output_dir: Path) -> Path:
    """Embed an image in a single-page PDF using PyMuPDF."""
    import fitz  # pymupdf — always available as a project dependency

    logger.info("ℹ️ Converting image %s to PDF via PyMuPDF…", source.name)

    pdf_path = output_dir / (source.stem + ".pdf")

    # Open the image as a PyMuPDF document (creates a virtual 1-page doc)
    img_doc = fitz.open(str(source))
    pdf_bytes = img_doc.convert_to_pdf()
    img_doc.close()

    pdf_path.write_bytes(pdf_bytes)

    logger.info(
        "ℹ️ Converted %s → %s (%.1f KB)",
        source.name, pdf_path.name, pdf_path.stat().st_size / 1024,
    )
    return pdf_path
