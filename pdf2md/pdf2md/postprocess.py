"""Markdown cleaning and normalization post-processing."""

from __future__ import annotations

import re
from collections import Counter


def postprocess(markdown: str, **options: bool) -> str:
    """Apply a sequence of cleaning steps to raw markdown output.

    All options default to ``True``.  Pass ``option_name=False`` to
    skip a specific step.
    """
    if options.get("strip_headers_footers", True):
        markdown = _strip_headers_footers(markdown)

    if options.get("strip_page_numbers", True):
        markdown = _strip_page_numbers(markdown)

    if options.get("fix_broken_paragraphs", True):
        markdown = _fix_broken_paragraphs(markdown)

    if options.get("compact_tables", True):
        markdown = _compact_tables(markdown)

    if options.get("normalize_whitespace", True):
        markdown = _normalize_whitespace(markdown)

    return markdown


# ------------------------------------------------------------------
# Individual cleaning steps
# ------------------------------------------------------------------


def _strip_headers_footers(text: str) -> str:
    """Remove short lines (<80 chars) that appear 3 or more times."""
    lines = text.split("\n")
    counts: Counter[str] = Counter()
    for line in lines:
        stripped = line.strip()
        if 0 < len(stripped) < 80:
            counts[stripped] += 1

    repeated = {line for line, count in counts.items() if count >= 3}
    if not repeated:
        return text

    return "\n".join(
        line for line in lines if line.strip() not in repeated
    )


def _strip_page_numbers(text: str) -> str:
    """Remove standalone page-number lines."""
    page_number_patterns = [
        re.compile(r"^\s*\d{1,4}\s*$"),                   # bare number
        re.compile(r"^\s*page\s+\d+\s*$", re.IGNORECASE), # "Page X"
        re.compile(r"^\s*-\s*\d+\s*-\s*$"),                # "- X -"
        re.compile(r"^\s*\d+\s+of\s+\d+\s*$", re.IGNORECASE),  # "X of Y"
    ]
    lines = text.split("\n")
    filtered: list[str] = []
    for line in lines:
        if any(pat.match(line) for pat in page_number_patterns):
            continue
        filtered.append(line)
    return "\n".join(filtered)


def _fix_broken_paragraphs(text: str) -> str:
    """Join lines that were split mid-sentence.

    A line is joined with the next if it does NOT end with sentence-ending
    punctuation and the next line starts with a lowercase letter.
    Lines that are headings, table rows, list items, blank, or horizontal
    rules are never joined.
    """
    lines = text.split("\n")
    result: list[str] = []
    skip_re = re.compile(r"^\s*(#|(\|)|([-*+]\s)|(\d+[.)]\s)|(---)|$)")
    ends_sentence = re.compile(r"[.!?:;]\s*$")

    i = 0
    while i < len(lines):
        current = lines[i]

        # Skip special lines
        if skip_re.match(current):
            result.append(current)
            i += 1
            continue

        # Try to join with next line(s)
        while (
            i + 1 < len(lines)
            and not ends_sentence.search(current)
            and not skip_re.match(current)
            and not skip_re.match(lines[i + 1])
            and lines[i + 1].strip()
            and lines[i + 1].strip()[0].islower()
        ):
            current = current.rstrip() + " " + lines[i + 1].strip()
            i += 1

        result.append(current)
        i += 1

    return "\n".join(result)


def _compact_tables(text: str) -> str:
    """Trim whitespace inside table cells and normalize formatting."""
    lines = text.split("\n")
    out: list[str] = []
    for line in lines:
        stripped = line.strip()
        if stripped.startswith("|") and stripped.endswith("|"):
            cells = stripped.split("|")
            # cells[0] and cells[-1] are empty strings from leading/trailing pipes
            trimmed = [c.strip() for c in cells[1:-1]]
            out.append("| " + " | ".join(trimmed) + " |")
        else:
            out.append(line)
    return "\n".join(out)


def _normalize_whitespace(text: str) -> str:
    """Strip trailing whitespace per line and collapse excessive blank lines."""
    lines = [line.rstrip() for line in text.split("\n")]
    text = "\n".join(lines)
    text = re.sub(r"\n{4,}", "\n\n\n", text)
    return text
