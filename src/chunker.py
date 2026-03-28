"""PDF chunking utilities.

Splits a large PDF into smaller temporary PDFs (chunks), each covering a page
range, and merges a list of per-chunk Markdown strings back into a single document.

Design notes
------------
- Each chunk is an independent PDF slice written to a temp file.  The pipeline
  processes it identically to a single-file run (classify → backend → postprocess).
- Chunk files are written to a ``_chunks/`` subfolder next to the source PDF
  and are deleted automatically after the caller finishes (or kept on error).
- ``overlap`` adds trailing pages from the previous chunk to the start of the
  next chunk, preserving context across boundaries (e.g. tables that span pages).
- ``merge()`` joins chunk markdowns with a ``---`` page-break separator so
  downstream readers know where chunks joined.
"""

from __future__ import annotations

import logging
import shutil
import tempfile
from pathlib import Path

logger = logging.getLogger("chunker")

# Separator inserted between merged chunk outputs
_CHUNK_SEPARATOR = "\n\n---\n\n"


def split_pdf(
    pdf_path: Path,
    chunk_size: int,
    overlap: int = 1,
) -> list[tuple[int, Path, int, int]]:
    """Split *pdf_path* into overlapping page-range chunks.

    Parameters
    ----------
    pdf_path:
        Source PDF.
    chunk_size:
        Number of content pages per chunk (not counting overlap from previous).
    overlap:
        Number of trailing pages from the previous chunk to prepend to the next
        chunk for context continuity.  Default: 1.

    Returns
    -------
    list of (chunk_idx, tmp_pdf_path, start_page, end_page)
        ``start_page`` and ``end_page`` are 0-based, inclusive of overlap.
        Temp PDFs are written to a ``_chunks/`` directory next to *pdf_path*.
    """
    try:
        import pymupdf as fitz  # type: ignore[import]
    except ImportError:
        try:
            import fitz  # type: ignore[import]
        except ImportError as exc:
            raise ImportError(
                "PyMuPDF is required for chunking. Run: pip install pymupdf"
            ) from exc

    if chunk_size <= 0:
        raise ValueError(f"chunk_size must be >= 1, got {chunk_size}")

    overlap = max(0, overlap)

    doc = fitz.open(str(pdf_path))
    total_pages = doc.page_count

    if total_pages == 0:
        doc.close()
        raise ValueError(f"PDF has no pages: {pdf_path}")

    # Build page ranges for each chunk
    chunks_dir = pdf_path.parent / f"_chunks_{pdf_path.stem}"
    chunks_dir.mkdir(exist_ok=True)

    results: list[tuple[int, Path, int, int]] = []
    chunk_idx = 0
    content_start = 0  # start of content pages (no overlap) for this chunk

    while content_start < total_pages:
        # Actual start includes overlap from the previous chunk
        actual_start = max(0, content_start - overlap) if chunk_idx > 0 else 0
        actual_end = min(content_start + chunk_size - 1, total_pages - 1)

        chunk_path = chunks_dir / f"chunk_{chunk_idx:03d}.pdf"

        # Extract the page range into a new PDF
        sub_doc = fitz.open()
        sub_doc.insert_pdf(doc, from_page=actual_start, to_page=actual_end)
        sub_doc.save(str(chunk_path))
        sub_doc.close()

        results.append((chunk_idx, chunk_path, actual_start, actual_end))
        logger.info(
            "ℹ️ Chunk %d: pages %d–%d → %s",
            chunk_idx, actual_start, actual_end, chunk_path.name,
        )

        content_start += chunk_size
        chunk_idx += 1

    doc.close()

    logger.info(
        "ℹ️ Split %s (%d pages) into %d chunk(s) of ~%d pages (overlap=%d)",
        pdf_path.name, total_pages, len(results), chunk_size, overlap,
    )
    return results


def merge_chunks(markdowns: list[str]) -> str:
    """Join per-chunk Markdown strings with a deterministic separator.

    Parameters
    ----------
    markdowns:
        Ordered list of Markdown strings, one per chunk.

    Returns
    -------
    A single Markdown string with ``---`` separators between chunks.
    Non-empty chunks only; empty strings are filtered out.
    """
    non_empty = [m.strip() for m in markdowns if m and m.strip()]
    return _CHUNK_SEPARATOR.join(non_empty)


def cleanup_chunks(pdf_path: Path) -> None:
    """Remove the ``_chunks_<stem>/`` directory created by :func:`split_pdf`.

    Safe to call even if the directory does not exist.
    """
    chunks_dir = pdf_path.parent / f"_chunks_{pdf_path.stem}"
    if chunks_dir.exists():
        shutil.rmtree(chunks_dir, ignore_errors=True)
        logger.info("ℹ️ Removed chunk temp dir: %s", chunks_dir)
