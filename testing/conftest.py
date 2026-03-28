"""Shared pytest fixtures for the pdf2md test suite."""

from __future__ import annotations

import struct
import zlib
from pathlib import Path

import pytest


# ---------------------------------------------------------------------------
# PDF fixture
# ---------------------------------------------------------------------------


@pytest.fixture
def minimal_pdf(tmp_path: Path) -> Path:
    """5-page PDF with readable text on every page (no external files needed)."""
    import fitz  # PyMuPDF — project dependency

    doc = fitz.open()
    for i in range(5):
        page = doc.new_page(width=595, height=842)  # A4
        page.insert_text(
            (72, 100),
            f"Page {i + 1}\n\nThis is sample text on page {i + 1}.\n"
            "Lorem ipsum dolor sit amet, consectetur adipiscing elit.",
        )
    pdf_path = tmp_path / "sample.pdf"
    doc.save(str(pdf_path))
    doc.close()
    return pdf_path


# ---------------------------------------------------------------------------
# Image fixture
# ---------------------------------------------------------------------------


def _make_minimal_png() -> bytes:
    """Return raw bytes of a valid 4x4 red PNG (no Pillow required)."""

    def chunk(tag: bytes, data: bytes) -> bytes:
        crc = zlib.crc32(tag + data) & 0xFFFFFFFF
        return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", crc)

    # IHDR: 4×4, 8-bit RGB
    ihdr = chunk(b"IHDR", struct.pack(">IIBBBBB", 4, 4, 8, 2, 0, 0, 0))
    # Each scanline: filter byte 0 + 4 pixels × RGB (255, 0, 0)
    scanline = b"\x00" + b"\xff\x00\x00" * 4
    raw = scanline * 4
    idat = chunk(b"IDAT", zlib.compress(raw))
    iend = chunk(b"IEND", b"")
    return b"\x89PNG\r\n\x1a\n" + ihdr + idat + iend


@pytest.fixture
def minimal_png(tmp_path: Path) -> Path:
    """Minimal valid 4×4 red PNG image."""
    img_path = tmp_path / "sample.png"
    img_path.write_bytes(_make_minimal_png())
    return img_path
