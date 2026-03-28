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
  downstream readers know where chunks joined.  When ``chunk_overlap`` > 0,
  trailing lines of each chunk that repeat the start of the next chunk (same
  PDF pages, re-extracted) are dropped from the *previous* chunk so the merged
  document keeps the later chunk’s fuller extraction without duplication.
"""

from __future__ import annotations

import logging
import shutil
import tempfile
from difflib import SequenceMatcher
from pathlib import Path

logger = logging.getLogger("chunker")

# Separator inserted between merged chunk outputs
_CHUNK_SEPARATOR = "\n\n---\n\n"

# Avoid stripping a single coincidentally matching short line (e.g. "---")
_MIN_OVERLAP_MATCH_CHARS = 48
_MAX_OVERLAP_MATCH_LINES = 400
# When line-for-line suffix/prefix fails (different OCR for overlapped pages), drop
# a repeated top-level heading at the end of the previous chunk if it matches the
# start of the next chunk (same section re-extracted with more detail).
_MAX_HEADING_OVERLAP_TAIL_LINES = 160
# Fuzzy matching: minimum similarity ratio (0–1) and lines to verify after first match.
_FUZZY_MATCH_THRESHOLD = 0.85
_FUZZY_VERIFY_LINES = 6


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

    logger.debug("split_pdf() — path=%s, chunk_size=%d, overlap=%d", pdf_path, chunk_size, overlap)

    if chunk_size <= 0:
        raise ValueError(f"chunk_size must be >= 1, got {chunk_size}")

    overlap = max(0, overlap)

    doc = fitz.open(str(pdf_path))
    total_pages = doc.page_count
    logger.debug("PDF opened: %d pages, %d bytes", total_pages, pdf_path.stat().st_size)

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


def _non_empty_lines(text: str) -> list[str]:
    return [ln.strip() for ln in text.splitlines() if ln.strip()]


def _longest_suffix_prefix_line_match(prev: str, following: str) -> int:
    """Return *k* such that the last *k* non-empty lines of *prev* equal the first
    *k* non-empty lines of *following*, maximized (capped).
    """
    a = _non_empty_lines(prev)
    b = _non_empty_lines(following)
    if not a or not b:
        return 0
    max_k = min(len(a), len(b), _MAX_OVERLAP_MATCH_LINES)
    for k in range(max_k, 0, -1):
        if a[-k:] == b[:k]:
            matched_chars = sum(len(line) for line in a[-k:])
            if matched_chars >= _MIN_OVERLAP_MATCH_CHARS or k >= 2:
                return k
    return 0


def _truncate_prev_drop_matching_suffix(prev: str, k: int) -> str:
    """Remove the last *k* non-empty lines (and everything after the first of them)."""
    if k <= 0:
        return prev
    lines = prev.splitlines(keepends=True)
    nonempty_idx = [i for i, ln in enumerate(lines) if ln.strip()]
    if len(nonempty_idx) < k:
        return prev
    first_drop = nonempty_idx[-k]
    return "".join(lines[:first_drop]).rstrip()


def _dedupe_prev_by_repeated_heading(prev: str, following: str) -> str:
    """If *following* starts with a level-1 ``# `` heading that also appears near the
    end of *prev*, truncate *prev* at the **last** such line so the next chunk
    owns that section (typical PDF overlap: short tail vs full re-extraction).
    """
    next_lines = _non_empty_lines(following)
    if not next_lines:
        return prev
    first = next_lines[0]
    # Level-1 only: one leading "#", not "##" or "###".
    if not first.startswith("# ") or first.startswith("##"):
        return prev
    prev_lines = prev.splitlines()
    if not prev_lines:
        return prev
    last_match: int | None = None
    for i, ln in enumerate(prev_lines):
        if ln.strip() == first:
            last_match = i
    if last_match is None:
        return prev
    tail = len(prev_lines) - last_match
    if tail > _MAX_HEADING_OVERLAP_TAIL_LINES:
        return prev
    return "\n".join(prev_lines[:last_match]).rstrip()


def _find_overlap_cutpoint(prev_lines: list[str], next_lines: list[str]) -> int | None:
    """Detect where overlap content begins in *prev_lines* using fuzzy matching.

    Searches backwards through the tail of *prev_lines* for the first line of
    *next_lines* using character-level similarity (SequenceMatcher).  If found,
    verifies that the following lines also match at the same threshold, then
    returns the index in *prev_lines* where the overlap starts — i.e.
    ``prev_lines[:cutpoint]`` should be kept and the rest discarded.

    Returns ``None`` if no reliable overlap is detected.  This handles the
    common case where the LLM re-extracts overlapped PDF pages with minor
    differences (extra/missing punctuation, emoji, reformatted URLs, etc.).
    """
    if not prev_lines or not next_lines:
        return None

    next_first = next_lines[0]
    search_start = max(0, len(prev_lines) - _MAX_OVERLAP_MATCH_LINES)

    # Search from the tail of prev backwards – the overlap is at the end.
    best_idx: int | None = None
    for i in range(len(prev_lines) - 1, search_start - 1, -1):
        if SequenceMatcher(None, prev_lines[i], next_first).ratio() >= _FUZZY_MATCH_THRESHOLD:
            best_idx = i
            break

    if best_idx is None:
        return None

    # Verify: subsequent lines must also match well enough.
    verify_n = min(_FUZZY_VERIFY_LINES, len(prev_lines) - best_idx, len(next_lines))
    block_prev = "\n".join(prev_lines[best_idx: best_idx + verify_n])
    block_next = "\n".join(next_lines[:verify_n])

    if len(block_prev) < _MIN_OVERLAP_MATCH_CHARS and verify_n < 2:
        return None

    if SequenceMatcher(None, block_prev, block_next).ratio() >= _FUZZY_MATCH_THRESHOLD:
        return best_idx

    return None


def merge_chunks(markdowns: list[str], chunk_overlap: int = 0) -> str:
    """Join per-chunk Markdown strings with a deterministic separator.

    Parameters
    ----------
    markdowns:
        Ordered list of Markdown strings, one per chunk.
    chunk_overlap:
        If greater than zero, PDF page overlap was used between chunks.  For each
        boundary, matching non-empty lines at the end of a chunk and the start of
        the next are removed from the *previous* chunk so overlapped pages are
        not duplicated in the merged output.

    Returns
    -------
    A single Markdown string with ``---`` separators between chunks.
    Non-empty chunks only; empty strings are filtered out.
    """
    non_empty = [m.strip() for m in markdowns if m and m.strip()]
    if chunk_overlap <= 0 or len(non_empty) < 2:
        return _CHUNK_SEPARATOR.join(non_empty)

    adjusted: list[str] = []
    for i, md in enumerate(non_empty):
        if i > 0:
            prev = adjusted[-1]
            k = _longest_suffix_prefix_line_match(prev, md)
            if k > 0:
                trimmed = _truncate_prev_drop_matching_suffix(prev, k)
                if trimmed:
                    adjusted[-1] = trimmed
                else:
                    adjusted.pop()
            else:
                # Exact match failed – try fuzzy overlap detection.
                prev_lines = _non_empty_lines(prev)
                cutpoint = _find_overlap_cutpoint(prev_lines, _non_empty_lines(md))
                if cutpoint is not None:
                    k_fuzzy = len(prev_lines) - cutpoint
                    if k_fuzzy > 0:
                        trimmed = _truncate_prev_drop_matching_suffix(prev, k_fuzzy)
                        logger.debug(
                            "Fuzzy overlap: stripped %d lines from chunk %d", k_fuzzy, i - 1
                        )
                        if trimmed:
                            adjusted[-1] = trimmed
                        else:
                            adjusted.pop()
                else:
                    trimmed_h = _dedupe_prev_by_repeated_heading(adjusted[-1], md)
                    if trimmed_h != adjusted[-1]:
                        if trimmed_h:
                            adjusted[-1] = trimmed_h
                        else:
                            adjusted.pop()
        adjusted.append(md)

    return _CHUNK_SEPARATOR.join(adjusted)


def cleanup_chunks(pdf_path: Path) -> None:
    """Remove the ``_chunks_<stem>/`` directory created by :func:`split_pdf`.

    Safe to call even if the directory does not exist.
    """
    chunks_dir = pdf_path.parent / f"_chunks_{pdf_path.stem}"
    if chunks_dir.exists():
        shutil.rmtree(chunks_dir, ignore_errors=True)
        logger.info("ℹ️ Removed chunk temp dir: %s", chunks_dir)
