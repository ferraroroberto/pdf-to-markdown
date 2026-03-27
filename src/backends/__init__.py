"""Backend registry and auto-selection logic."""

from __future__ import annotations

from src.backends.base import BaseBackend
from src.backends.docling_backend import DoclingBackend
from src.backends.marker_backend import MarkerBackend
from src.backends.pdfplumber_backend import PdfplumberBackend
from src.backends.vertexai_backend import VertexAIBackend

BACKEND_REGISTRY: list[type[BaseBackend]] = [
    MarkerBackend,
    DoclingBackend,
    VertexAIBackend,
    PdfplumberBackend,
]


def get_backend(name: str) -> BaseBackend:
    """Instantiate a backend by name.

    Raises ``ValueError`` if the name is unknown and ``RuntimeError``
    if the backend's dependencies are not installed.
    """
    for cls in BACKEND_REGISTRY:
        if cls.name == name:
            if not cls.is_available():
                raise RuntimeError(
                    f"Backend '{name}' is not available — its dependencies are not installed."
                )
            return cls()
    known = [c.name for c in BACKEND_REGISTRY]
    raise ValueError(f"Unknown backend '{name}'. Available: {known}")


def get_best_available(needs_ocr: bool = False) -> BaseBackend:
    """Return the first available backend in preference order.

    If *needs_ocr* is True, backends that do not support scanned PDFs
    are skipped.
    """
    for cls in BACKEND_REGISTRY:
        if needs_ocr and not cls().supports_scanned():
            continue
        if cls.is_available():
            return cls()
    raise RuntimeError(
        "No suitable PDF extraction backend is available. "
        "Install at least pdfplumber (pip install pdfplumber)."
    )


def list_available() -> list[str]:
    """Return the names of all backends whose dependencies are installed."""
    return [cls.name for cls in BACKEND_REGISTRY if cls.is_available()]
