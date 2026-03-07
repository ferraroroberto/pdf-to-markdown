"""Output quality validation — compare extracted markdown against the source PDF."""

from __future__ import annotations

import difflib
import re
from pathlib import Path

import fitz  # PyMuPDF

from src.models import ValidationReport


def validate(pdf_path: str | Path, markdown: str) -> ValidationReport:
    """Validate *markdown* output against the source *pdf_path*.

    Extracts raw text from the PDF independently, strips markdown
    formatting, then computes similarity and structural metrics.
    """
    pdf_path = Path(pdf_path)

    # 1. Extract raw text from the PDF
    source_text = _extract_raw_text(pdf_path)

    # 2. Strip markdown formatting
    plain_output = _strip_markdown(markdown)

    # 3. Compute character similarity
    char_similarity = _compute_similarity(source_text, plain_output)

    # 4. Count structural elements
    heading_count = _count_headings(markdown)
    table_count = _count_tables(markdown)
    list_item_count = _count_list_items(markdown)

    # 5. Table row consistency
    table_row_consistent = _check_table_row_consistency(markdown)

    # 6. Build warnings
    warnings: list[str] = []
    source_len = len(source_text.strip())
    output_len = len(plain_output.strip())

    if source_len > 0:
        ratio = output_len / source_len
        if ratio < 0.30:
            warnings.append(
                f"CRITICAL: Output is only {ratio:.0%} of source characters — significant content loss"
            )
        elif ratio < 0.60:
            warnings.append(
                f"Output is only {ratio:.0%} of source characters — possible content loss"
            )
        elif ratio > 2.0:
            warnings.append(
                f"Output is {ratio:.0%} of source characters — possible content duplication"
            )

    if char_similarity < 0.50:
        warnings.append(
            f"CRITICAL: Character similarity is {char_similarity:.0%} — output may not match source"
        )
    elif char_similarity < 0.85:
        warnings.append(
            f"Character similarity is {char_similarity:.0%} — below recommended threshold"
        )

    if not table_row_consistent:
        warnings.append("Table row column counts are inconsistent")

    return ValidationReport(
        char_similarity=char_similarity,
        source_char_count=source_len,
        output_char_count=output_len,
        heading_count=heading_count,
        table_count=table_count,
        list_item_count=list_item_count,
        table_row_consistency=table_row_consistent,
        warnings=warnings,
    )


# ------------------------------------------------------------------
# Helpers
# ------------------------------------------------------------------


def _extract_raw_text(pdf_path: Path) -> str:
    """Extract all text from every page of the PDF using PyMuPDF."""
    doc = fitz.open(str(pdf_path))
    texts: list[str] = []
    for page in doc:
        texts.append(page.get_text("text"))
    doc.close()
    return "\n".join(texts)


def _strip_markdown(text: str) -> str:
    """Remove markdown formatting, returning plain text."""
    # Remove heading markers
    text = re.sub(r"^#{1,6}\s+", "", text, flags=re.MULTILINE)
    # Remove table separators
    text = re.sub(r"^\|[-\s|:]+\|\s*$", "", text, flags=re.MULTILINE)
    # Remove pipe characters (table cells)
    text = text.replace("|", " ")
    # Remove bold/italic markers
    text = re.sub(r"\*{1,2}(.*?)\*{1,2}", r"\1", text)
    text = re.sub(r"_{1,2}(.*?)_{1,2}", r"\1", text)
    # Remove link syntax [text](url) → text
    text = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", text)
    # Remove horizontal rules
    text = re.sub(r"^-{3,}\s*$", "", text, flags=re.MULTILINE)
    # Remove list markers
    text = re.sub(r"^[-*+]\s+", "", text, flags=re.MULTILINE)
    text = re.sub(r"^\d+[.)]\s+", "", text, flags=re.MULTILINE)
    # Collapse whitespace
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def _normalize_for_comparison(text: str) -> str:
    """Lowercase and collapse whitespace for fuzzy comparison."""
    return re.sub(r"\s+", " ", text.lower()).strip()


def _compute_similarity(source: str, output: str) -> float:
    """Compute character-level similarity between source and output text.

    For texts longer than 50,000 characters, samples three chunks
    (beginning, middle, end) and averages their ratios.
    """
    norm_src = _normalize_for_comparison(source)
    norm_out = _normalize_for_comparison(output)

    if not norm_src and not norm_out:
        return 1.0
    if not norm_src or not norm_out:
        return 0.0

    threshold = 50_000
    if len(norm_src) <= threshold and len(norm_out) <= threshold:
        return difflib.SequenceMatcher(None, norm_src, norm_out).ratio()

    # Sample three chunks for performance
    chunk_size = threshold // 3
    ratios: list[float] = []

    for start_fn in [
        lambda t: t[:chunk_size],
        lambda t: t[len(t) // 2 - chunk_size // 2 : len(t) // 2 + chunk_size // 2],
        lambda t: t[-chunk_size:],
    ]:
        src_chunk = start_fn(norm_src)
        out_chunk = start_fn(norm_out)
        ratios.append(difflib.SequenceMatcher(None, src_chunk, out_chunk).ratio())

    return sum(ratios) / len(ratios)


def _count_headings(markdown: str) -> int:
    return len(re.findall(r"^#{1,6}\s", markdown, re.MULTILINE))


def _count_tables(markdown: str) -> int:
    """Count distinct tables in markdown.

    A table is a contiguous block of lines where every line starts and
    ends with a pipe character.
    """
    lines = markdown.split("\n")
    in_table = False
    count = 0
    for line in lines:
        stripped = line.strip()
        is_table_line = stripped.startswith("|") and stripped.endswith("|") and len(stripped) > 1
        if is_table_line and not in_table:
            count += 1
            in_table = True
        elif not is_table_line:
            in_table = False
    return count


def _count_list_items(markdown: str) -> int:
    unordered = len(re.findall(r"^[-*+]\s", markdown, re.MULTILINE))
    ordered = len(re.findall(r"^\d+[.)]\s", markdown, re.MULTILINE))
    return unordered + ordered


def _check_table_row_consistency(markdown: str) -> bool:
    """Check that within each table, all data rows have the same column count."""
    lines = markdown.split("\n")
    tables: list[list[str]] = []
    current_table: list[str] = []

    for line in lines:
        stripped = line.strip()
        is_table_line = stripped.startswith("|") and stripped.endswith("|") and len(stripped) > 1
        if is_table_line:
            current_table.append(stripped)
        else:
            if current_table:
                tables.append(current_table)
                current_table = []
    if current_table:
        tables.append(current_table)

    for table in tables:
        # Filter out separator rows
        data_rows = [
            row for row in table
            if not re.match(r"^\|[\s\-:|]+\|$", row)
        ]
        if not data_rows:
            continue
        col_counts = {row.count("|") - 1 for row in data_rows}
        if len(col_counts) > 1:
            return False

    return True
