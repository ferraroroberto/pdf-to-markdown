"""PDF to Markdown conversion — clean, structured output for LLMs."""

from src.auth import build_client
from src.batch import run_batch
from src.chunker import cleanup_chunks, merge_chunks, split_pdf
from src.classifier import PDFInfo, classify_pdf
from src.config import Settings, load_settings, save_settings
from src.logger_exec import append_row, load_log
from src.models import BatchResult, ChunkResult, ConversionResult, ValidationReport
from src.pipeline import Pipeline
from src.postprocess import postprocess
from src.validation import validate
from src.vertexai_backend import VertexAIBackend

__version__ = "0.2.0"

__all__ = [
    "Pipeline",
    "Settings",
    "load_settings",
    "save_settings",
    "build_client",
    "split_pdf",
    "merge_chunks",
    "cleanup_chunks",
    "run_batch",
    "append_row",
    "load_log",
    "ConversionResult",
    "ChunkResult",
    "BatchResult",
    "ValidationReport",
    "PDFInfo",
    "classify_pdf",
    "validate",
    "postprocess",
    "VertexAIBackend",
    "__version__",
]
