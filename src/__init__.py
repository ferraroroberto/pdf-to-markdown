"""PDF to Markdown conversion — clean, structured output for LLMs."""

from src.auth import build_client
from src.batch import run_batch
from src.chunk_runner import ChunkOutcome, ChunkSpec, convert_chunked
from src.chunker import cleanup_chunks, merge_chunks, split_pdf
from src.classifier import PDFInfo, classify_pdf
from src.config import Settings, load_settings, save_settings
from src.logger_exec import append_row, load_log
from src.models import ChunkResult, ConversionResult, ValidationReport
from src.pipeline import Pipeline
from src.hub_gemini_backend import HubGeminiBackend
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
    "convert_chunked",
    "ChunkSpec",
    "ChunkOutcome",
    "run_batch",
    "append_row",
    "load_log",
    "ConversionResult",
    "ChunkResult",
    "ValidationReport",
    "PDFInfo",
    "classify_pdf",
    "validate",
    "postprocess",
    "VertexAIBackend",
    "HubGeminiBackend",
    "__version__",
]
