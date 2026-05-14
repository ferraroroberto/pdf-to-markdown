"""Pre-processing: convert Office documents and images to PDF before extraction.

Only used for the Vertex AI backend.
- Word / PowerPoint / other Office formats → Microsoft Office COM (pywin32) on Windows,
  or docling + PyMuPDF on Unix/Linux → PDF
- Images (JPEG, PNG, BMP, TIFF, WebP, GIF) → PyMuPDF → single-page PDF
"""

from __future__ import annotations

import io
import logging
import platform
import shutil
import tempfile
import textwrap
from contextlib import contextmanager
from pathlib import Path
from typing import Generator

logger = logging.getLogger("file_converter")

# File types handled by Office converters
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
    """Convert an Office document to PDF.

    On Windows: uses Microsoft Office COM automation (requires pywin32 + MS Office).
    On Unix/Linux: uses docling to parse content, then renders to PDF via PyMuPDF.
    Works for Word (.doc/.docx/.rtf/.odt), Excel (.xls/.xlsx/.ods),
    and PowerPoint (.ppt/.pptx/.odp).
    """
    if platform.system() == "Windows":
        return _office_to_pdf_windows(source, output_dir)
    return _office_to_pdf_docling(source, output_dir)


def _office_to_pdf_windows(source: Path, output_dir: Path) -> Path:
    """Windows path: Office COM automation via pywin32."""
    try:
        import pythoncom
        import win32com.client
    except ImportError:
        raise RuntimeError(
            "pywin32 is required for Office-to-PDF conversion on Windows. "
            "Install it with: pip install pywin32"
        )

    suffix = source.suffix.lower()
    pdf_path = output_dir / (source.stem + ".pdf")
    abs_source = str(source.resolve())
    abs_pdf = str(pdf_path.resolve())

    logger.info("ℹ️ Converting %s to PDF via Microsoft Office COM…", source.name)

    # COM must be initialized on the calling thread (e.g. Streamlit worker threads).
    # Without this, Dispatch can fail with CO_E_NOTINITIALIZED on later runs.
    pythoncom.CoInitialize()
    try:
        if suffix in {".doc", ".docx", ".rtf", ".odt"}:
            _word_to_pdf(win32com.client, abs_source, abs_pdf)
        elif suffix in {".xls", ".xlsx", ".ods"}:
            _excel_to_pdf(win32com.client, abs_source, abs_pdf)
        elif suffix in {".ppt", ".pptx", ".odp"}:
            _powerpoint_to_pdf(win32com.client, abs_source, abs_pdf)
        else:
            raise ValueError(
                f"Unsupported Office extension for COM conversion: {suffix}"
            )

        if not pdf_path.exists():
            raise FileNotFoundError(
                f"Office COM conversion finished but expected PDF not found: {pdf_path}"
            )

        logger.info(
            "ℹ️ Converted %s → %s (%.1f KB)",
            source.name, pdf_path.name, pdf_path.stat().st_size / 1024,
        )
        return pdf_path
    finally:
        pythoncom.CoUninitialize()


def _office_to_pdf_docling(source: Path, output_dir: Path) -> Path:
    """Unix/Linux path: parse Office file with docling, render content to PDF via PyMuPDF.

    docling (already a project dependency) handles DOCX/PPTX/XLSX natively without
    requiring any system-level Office suite.  The parsed document is walked in
    reading order — text, tables, and **embedded images** are all rendered into
    the PDF so they reach the extraction backend (images would otherwise be lost,
    unlike the Windows COM path which preserves them).
    """
    try:
        from docling.document_converter import DocumentConverter
        from docling_core.types.doc import PictureItem, TableItem, TextItem
    except ImportError:
        raise RuntimeError(
            "docling is required for Office-to-PDF conversion on Linux. "
            "Install it with: pip install docling"
        )

    import fitz  # pymupdf

    PAGE_W, PAGE_H = 595, 842  # A4
    MARGIN = 50
    FS = 9
    LINE_H = FS * 1.5
    WRAP_WIDTH = 95  # characters per line (approx for base-14 Helvetica at 9pt)
    CONTENT_W = PAGE_W - 2 * MARGIN
    CONTENT_H = PAGE_H - 2 * MARGIN
    IMG_GAP = LINE_H  # vertical breathing room around an embedded image

    logger.info("ℹ️ Converting %s to PDF via docling + PyMuPDF…", source.name)

    converter = DocumentConverter()
    doc = converter.convert(str(source.resolve())).document

    pdf = fitz.open()
    page = pdf.new_page(width=PAGE_W, height=PAGE_H)
    y = MARGIN + FS

    def _new_page() -> None:
        nonlocal page, y
        page = pdf.new_page(width=PAGE_W, height=PAGE_H)
        y = MARGIN + FS

    def _write_lines(text: str) -> None:
        """Word-wrap *text* and lay it out, breaking pages as needed."""
        nonlocal y
        for raw_line in text.splitlines() or [""]:
            wrapped = textwrap.wrap(raw_line, WRAP_WIDTH) if raw_line.strip() else [""]
            for line in wrapped or [""]:
                if y + LINE_H > PAGE_H - MARGIN:
                    _new_page()
                page.insert_text((MARGIN, y), line, fontsize=FS)
                y += LINE_H

    def _place_image(img) -> None:
        """Embed a PIL image, scaled to fit the page, breaking pages as needed."""
        nonlocal y
        if img.mode == "CMYK":
            img = img.convert("RGB")
        # Scale down to fit within the content box; never scale up.
        scale = min(CONTENT_W / img.width, CONTENT_H / img.height, 1.0)
        draw_w = img.width * scale
        draw_h = img.height * scale
        if y + draw_h + IMG_GAP > PAGE_H - MARGIN:
            _new_page()
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        page.insert_image(
            fitz.Rect(MARGIN, y, MARGIN + draw_w, y + draw_h),
            stream=buf.getvalue(),
        )
        y += draw_h + IMG_GAP

    for item, _level in doc.iterate_items():
        if isinstance(item, PictureItem):
            img = item.get_image(doc)
            if img is not None:
                _place_image(img)
            else:
                _write_lines("[image]")
        elif isinstance(item, TableItem):
            _write_lines(item.export_to_markdown(doc))
        elif isinstance(item, TextItem):
            _write_lines(item.text)
        # Other node types (groups, key-value, etc.) carry no renderable content.

    pdf_path = output_dir / (source.stem + ".pdf")
    pdf.save(str(pdf_path))
    pdf.close()

    logger.info(
        "ℹ️ Converted %s → %s (%.1f KB)",
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
