"""Pre-processing: convert Office documents and images to PDF before extraction.

Only used for the Vertex AI backend.
- Word / PowerPoint / other Office formats → LibreOffice headless → PDF
- Images (JPEG, PNG, BMP, TIFF, WebP, GIF) → PyMuPDF → single-page PDF
"""

from __future__ import annotations

import logging
import shutil
import subprocess
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
    """Convert an Office document to PDF using LibreOffice headless mode."""
    logger.info("ℹ️ Converting %s to PDF via LibreOffice…", source.name)

    cmd = [
        "libreoffice", "--headless",
        "--convert-to", "pdf",
        "--outdir", str(output_dir),
        str(source),
    ]
    try:
        proc = subprocess.run(
            cmd, capture_output=True, text=True, timeout=120
        )
    except FileNotFoundError:
        raise RuntimeError(
            "LibreOffice is required for Office-to-PDF conversion but was not found. "
            "Install it from https://www.libreoffice.org/"
        )
    except subprocess.TimeoutExpired:
        raise RuntimeError(
            f"LibreOffice conversion timed out for {source.name} (>120 s)."
        )

    if proc.returncode != 0:
        stderr = proc.stderr.strip()
        raise RuntimeError(
            f"LibreOffice conversion failed (exit {proc.returncode}) for {source.name}"
            + (f": {stderr}" if stderr else "")
        )

    pdf_path = output_dir / (source.stem + ".pdf")
    if not pdf_path.exists():
        raise FileNotFoundError(
            f"LibreOffice finished but expected PDF not found: {pdf_path}"
        )

    logger.info(
        "ℹ️ Converted %s → %s (%.1f KB)",
        source.name, pdf_path.name, pdf_path.stat().st_size / 1024,
    )
    return pdf_path


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
