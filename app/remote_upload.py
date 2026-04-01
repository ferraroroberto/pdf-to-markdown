"""Detect remote access and handle browser-based file uploads.

When the app is accessed through a Cloudflare tunnel (or any reverse proxy),
the native tkinter file browser opens on the *server*, which is useless.
This module provides:

- ``is_remote_session()`` — detect remote vs local access
- ``save_uploaded_file()`` — persist a Streamlit UploadedFile to disk
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path

import streamlit as st

_UPLOAD_DIR = Path(__file__).parent.parent / "uploads"

# File extensions accepted by the app (mirrors execute.py filter)
SUPPORTED_EXTENSIONS = (
    "pdf", "docx", "doc", "pptx", "ppt", "xlsx", "xls",
    "jpg", "jpeg", "png", "bmp", "tiff", "tif", "webp", "gif",
)

ACCEPT_TYPES = [f".{e}" for e in SUPPORTED_EXTENSIONS]


def is_remote_session() -> bool:
    """Return True when the current session is accessed from a remote browser.

    Detection strategy (in order):
    1. ``PDF2MD_REMOTE=1`` environment variable (set by launch_server.sh).
    2. Presence of Cloudflare tunnel headers (``Cf-Connecting-Ip``).
    3. ``X-Forwarded-For`` header (generic reverse-proxy indicator).
    """
    # Env-var override (most reliable — set by launch_server.sh)
    if os.environ.get("PDF2MD_REMOTE", "").strip() == "1":
        return True

    # Inspect request headers (Streamlit >= 1.31)
    try:
        headers = st.context.headers  # type: ignore[attr-defined]
        if headers.get("Cf-Connecting-Ip"):
            return True
        if headers.get("X-Forwarded-For"):
            return True
    except AttributeError:
        pass  # older Streamlit — fall back to env var only

    return False


def upload_dir() -> Path:
    """Return (and create) the uploads directory."""
    _UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    return _UPLOAD_DIR


def save_uploaded_file(uploaded_file) -> Path:
    """Write a Streamlit ``UploadedFile`` to the uploads directory.

    Returns the Path to the saved file on disk.
    """
    dest = upload_dir() / uploaded_file.name
    with open(dest, "wb") as f:
        f.write(uploaded_file.getbuffer())
    return dest


def save_uploaded_files(uploaded_files: list) -> Path:
    """Save multiple uploaded files into a timestamped sub-folder.

    Returns the Path to the folder containing the saved files.
    """
    import time

    batch_dir = upload_dir() / f"batch_{int(time.time())}"
    batch_dir.mkdir(parents=True, exist_ok=True)
    for uf in uploaded_files:
        dest = batch_dir / uf.name
        with open(dest, "wb") as f:
            f.write(uf.getbuffer())
    return batch_dir


def cleanup_upload(path: Path) -> None:
    """Remove an uploaded file or batch folder (best-effort)."""
    try:
        if path.is_dir():
            shutil.rmtree(path, ignore_errors=True)
        elif path.is_file():
            path.unlink(missing_ok=True)
    except OSError:
        pass
