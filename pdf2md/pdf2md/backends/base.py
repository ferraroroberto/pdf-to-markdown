"""Abstract base class for PDF extraction backends."""

from __future__ import annotations

from abc import ABC, abstractmethod
from pathlib import Path


class BaseBackend(ABC):
    """Interface that every PDF extraction backend must implement."""

    name: str = "base"

    @classmethod
    @abstractmethod
    def is_available(cls) -> bool:
        """Return True if the backend's dependencies are importable."""
        ...

    @abstractmethod
    def convert(self, pdf_path: Path, **kwargs: object) -> tuple[str, dict]:
        """Convert *pdf_path* to Markdown.

        Returns a tuple of (markdown_string, metadata_dict).
        """
        ...

    def supports_scanned(self) -> bool:
        """Return True if this backend can handle scanned (image-only) PDFs."""
        return False
