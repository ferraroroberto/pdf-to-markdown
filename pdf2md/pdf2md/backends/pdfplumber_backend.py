"""Lightweight pdfplumber backend — heuristic extraction for born-digital PDFs."""

from __future__ import annotations

import re
import statistics
from pathlib import Path

import pdfplumber

from pdf2md.backends.base import BaseBackend


class PdfplumberBackend(BaseBackend):
    """Extract text, tables, headings, and lists from born-digital PDFs.

    Uses pdfplumber's character-level data and table detection to build
    structured Markdown without any ML dependencies.
    """

    name = "pdfplumber"

    @classmethod
    def is_available(cls) -> bool:
        try:
            import pdfplumber  # noqa: F401
            return True
        except ImportError:
            return False

    def supports_scanned(self) -> bool:
        return False

    def convert(self, pdf_path: Path, **kwargs: object) -> tuple[str, dict]:
        crop_header: float = float(kwargs.get("crop_header", 50))
        crop_footer: float = float(kwargs.get("crop_footer", 50))
        detect_headings: bool = bool(kwargs.get("detect_headings", True))
        table_settings: dict = dict(kwargs.get("table_settings", {})) if kwargs.get("table_settings") else {
            "vertical_strategy": "lines_strict",
            "horizontal_strategy": "lines_strict",
            "snap_tolerance": 5,
        }

        all_font_sizes: list[float] = []
        page_outputs: list[str] = []

        with pdfplumber.open(str(pdf_path)) as pdf:
            # First pass: collect font sizes across the document for median
            for page in pdf.pages:
                for char in page.chars:
                    size = float(char.get("size", 0))
                    if size > 0:
                        all_font_sizes.append(size)

            median_size = statistics.median(all_font_sizes) if all_font_sizes else 12.0

            for page in pdf.pages:
                page_md = self._process_page(
                    page, crop_header, crop_footer, detect_headings,
                    table_settings, median_size,
                )
                page_outputs.append(page_md)

            page_count = len(pdf.pages)

        markdown = "\n\n---\n\n".join(page_outputs)

        # Normalize: collapse 4+ blank lines to 3, strip trailing whitespace
        markdown = re.sub(r"\n{4,}", "\n\n\n", markdown)
        lines = [line.rstrip() for line in markdown.split("\n")]
        markdown = "\n".join(lines)

        metadata: dict = {
            "page_count": page_count,
            "backend": self.name,
        }
        return markdown, metadata

    # ------------------------------------------------------------------
    # Page-level processing
    # ------------------------------------------------------------------

    def _process_page(
        self,
        page: pdfplumber.page.Page,
        crop_header: float,
        crop_footer: float,
        detect_headings: bool,
        table_settings: dict,
        median_size: float,
    ) -> str:
        bbox = page.bbox  # (x0, y0, x1, y1)
        crop_box = (
            bbox[0],
            bbox[1] + crop_header,
            bbox[2],
            bbox[3] - crop_footer,
        )
        # Clamp so we don't invert the crop region
        if crop_box[1] >= crop_box[3]:
            crop_box = bbox
        cropped = page.crop(crop_box)

        # Extract tables and their bounding boxes
        tables = cropped.find_tables(table_settings=table_settings)
        table_bboxes = [t.bbox for t in tables]
        table_data_list = [t.extract() for t in tables]

        # Extract characters outside table regions
        chars = cropped.chars
        text_chars = [c for c in chars if not self._char_in_any_bbox(c, table_bboxes)]

        # Group characters into lines with font-size info
        lines_with_sizes = self._chars_to_lines_with_sizes(text_chars)

        # Build markdown for text lines
        parts: list[str] = []
        table_idx = 0

        # We need to interleave text and tables by y-position
        table_entries: list[tuple[float, str]] = []
        for bbox_t, data in zip(table_bboxes, table_data_list):
            y_pos = bbox_t[1]  # top of table
            table_md = self._table_to_markdown(data)
            table_entries.append((y_pos, table_md))

        text_entries: list[tuple[float, str]] = []
        bullet_re = re.compile(r"^[\s]*[•●◦\-\*]\s+")
        numbered_re = re.compile(r"^[\s]*\d+[.)]\s+")

        for line_text, avg_size, y_pos in lines_with_sizes:
            stripped = line_text.strip()
            if not stripped:
                text_entries.append((y_pos, ""))
                continue

            formatted = stripped

            # Detect list items
            if bullet_re.match(stripped):
                formatted = re.sub(r"^[\s]*[•●◦\-\*]\s+", "- ", stripped)
            elif numbered_re.match(stripped):
                match = re.match(r"^[\s]*(\d+)[.)]\s+(.*)", stripped)
                if match:
                    formatted = f"{match.group(1)}. {match.group(2)}"

            # Detect headings by font size
            elif detect_headings and avg_size > 0 and median_size > 0:
                ratio = avg_size / median_size
                if ratio > 1.7:
                    formatted = f"# {stripped}"
                elif ratio > 1.35:
                    formatted = f"## {stripped}"

            text_entries.append((y_pos, formatted))

        # Merge text and table entries by y-position
        all_entries: list[tuple[float, str]] = text_entries + table_entries
        all_entries.sort(key=lambda e: e[0])

        return "\n".join(entry[1] for entry in all_entries)

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _char_in_bbox(char: dict, bbox: tuple) -> bool:
        """Check if the center of *char* falls within *bbox*."""
        cx = (float(char["x0"]) + float(char["x1"])) / 2
        cy = (float(char["top"]) + float(char["bottom"])) / 2
        return bbox[0] <= cx <= bbox[2] and bbox[1] <= cy <= bbox[3]

    @classmethod
    def _char_in_any_bbox(cls, char: dict, bboxes: list[tuple]) -> bool:
        return any(cls._char_in_bbox(char, bb) for bb in bboxes)

    @staticmethod
    def _chars_to_lines_with_sizes(chars: list[dict]) -> list[tuple[str, float, float]]:
        """Group characters into lines by y-coordinate proximity.

        Returns a list of (line_text, avg_font_size, y_position) tuples,
        using a 3-point threshold for same-line grouping.
        """
        if not chars:
            return []

        sorted_chars = sorted(chars, key=lambda c: (float(c["top"]), float(c["x0"])))

        lines: list[tuple[str, float, float]] = []
        current_line_chars: list[dict] = [sorted_chars[0]]

        for char in sorted_chars[1:]:
            if abs(float(char["top"]) - float(current_line_chars[-1]["top"])) <= 3:
                current_line_chars.append(char)
            else:
                lines.append(_build_line(current_line_chars))
                current_line_chars = [char]

        if current_line_chars:
            lines.append(_build_line(current_line_chars))

        return lines

    @staticmethod
    def _table_to_markdown(table_data: list[list[str | None]]) -> str:
        """Convert pdfplumber table extraction into a pipe-delimited Markdown table."""
        if not table_data:
            return ""

        # Normalize column counts
        max_cols = max(len(row) for row in table_data)
        normalized: list[list[str]] = []
        for row in table_data:
            cells = [(cell or "").replace("\n", " ").strip() for cell in row]
            while len(cells) < max_cols:
                cells.append("")
            normalized.append(cells)

        lines: list[str] = []
        # Header row
        header = normalized[0]
        lines.append("| " + " | ".join(header) + " |")
        lines.append("| " + " | ".join("---" for _ in header) + " |")

        # Data rows
        for row in normalized[1:]:
            lines.append("| " + " | ".join(row) + " |")

        return "\n".join(lines)


def _build_line(chars: list[dict]) -> tuple[str, float, float]:
    """Build a single line from a list of character dicts."""
    chars_sorted = sorted(chars, key=lambda c: float(c["x0"]))
    text_parts: list[str] = []
    sizes: list[float] = []

    prev_x1: float | None = None
    for ch in chars_sorted:
        x0 = float(ch["x0"])
        if prev_x1 is not None and (x0 - prev_x1) > 3:
            text_parts.append(" ")
        text_parts.append(ch.get("text", ""))
        size = float(ch.get("size", 0))
        if size > 0:
            sizes.append(size)
        prev_x1 = float(ch["x1"])

    text = "".join(text_parts)
    avg_size = statistics.mean(sizes) if sizes else 0.0
    y_pos = float(chars_sorted[0]["top"])
    return text, avg_size, y_pos
