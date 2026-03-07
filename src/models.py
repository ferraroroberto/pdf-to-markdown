"""Data models for pdf2md conversion results and validation reports."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class ValidationReport:
    """Quality report produced by comparing extracted markdown against the source PDF."""

    char_similarity: float
    source_char_count: int
    output_char_count: int
    heading_count: int
    table_count: int
    list_item_count: int
    table_row_consistency: bool
    warnings: list[str] = field(default_factory=list)

    @property
    def has_critical_warnings(self) -> bool:
        """Return True if any warning starts with 'CRITICAL'."""
        return any(w.startswith("CRITICAL") for w in self.warnings)

    @property
    def passed(self) -> bool:
        """Return True if quality thresholds are met and no critical warnings exist."""
        return self.char_similarity >= 0.85 and not self.has_critical_warnings

    def summary(self) -> str:
        """Return a formatted multi-line summary of all validation metrics."""
        status = "PASS" if self.passed else "FAIL"
        lines = [
            f"Validation: {status}",
            f"  Character similarity : {self.char_similarity:.1%}",
            f"  Source chars          : {self.source_char_count:,}",
            f"  Output chars          : {self.output_char_count:,}",
            f"  Headings              : {self.heading_count}",
            f"  Tables                : {self.table_count}",
            f"  List items            : {self.list_item_count}",
            f"  Table row consistency : {'Yes' if self.table_row_consistency else 'No'}",
        ]
        if self.warnings:
            lines.append("  Warnings:")
            for w in self.warnings:
                lines.append(f"    - {w}")
        return "\n".join(lines)


@dataclass
class ConversionResult:
    """Container for the output of a PDF-to-Markdown conversion."""

    source: Path
    markdown: str
    backend_used: str
    metadata: dict = field(default_factory=dict)
    validation: ValidationReport | None = None

    @property
    def token_estimate(self) -> int:
        """Rough token estimate (~4 chars per token for English text)."""
        return len(self.markdown) // 4

    @property
    def page_count(self) -> int | None:
        """Page count extracted from conversion metadata."""
        return self.metadata.get("page_count")

    def save(self, output_path: str | Path) -> Path:
        """Write the markdown content to *output_path*, creating parent dirs as needed."""
        output_path = Path(output_path)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(self.markdown, encoding="utf-8")
        return output_path
