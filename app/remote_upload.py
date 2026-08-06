"""Detect remote access and handle browser-based file uploads.

When the app is accessed through a Cloudflare tunnel (or any reverse proxy),
the native tkinter file browser opens on the *server*, which is useless.
This module provides:

- ``is_remote_session()`` — detect remote vs local access
- ``save_uploaded_file()`` — persist a Streamlit UploadedFile to disk
"""

from __future__ import annotations

import os
from pathlib import Path, PureWindowsPath

import streamlit as st

from src.file_converter import INPUT_EXTENSIONS

_UPLOAD_DIR = Path(__file__).parent.parent / "uploads"

# Used when a supplied name reduces to nothing usable.
_FALLBACK_UPLOAD_NAME = "upload"

# Accepted upload formats, derived from the single source of truth in
# ``src.file_converter`` (PDF + every pre-convertible format) — adding a new
# format there automatically reaches the remote uploader and file pickers.
SUPPORTED_EXTENSIONS = tuple(sorted(ext.lstrip(".") for ext in INPUT_EXTENSIONS))

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


def safe_upload_name(filename: str) -> str:
    """Reduce a browser-supplied *filename* to a bare, directory-free name.

    ``PureWindowsPath`` is used deliberately: it treats both ``/`` and ``\\`` as
    separators on every host OS, so a name composed on one platform cannot carry
    a directory component past a server running on the other. A name that leaves
    nothing usable (empty, whitespace, ``.``/``..``) falls back to a fixed stem.
    """
    name = PureWindowsPath(str(filename or "").strip()).name.strip()
    if not name or name in {".", ".."}:
        return _FALLBACK_UPLOAD_NAME
    return name


def resolve_upload_dest(directory: Path, filename: str) -> Path:
    """Return the path *filename* must be written to inside *directory*.

    The name is reduced to its bare form first; the containment assertion is a
    second, independent check so a future change to the reduction can never
    silently widen where an upload lands.
    """
    dest = directory / safe_upload_name(filename)
    if dest.resolve().parent != directory.resolve():
        raise ValueError(
            f"Refusing to write upload {filename!r} outside {directory}"
        )
    return dest


def save_uploaded_file(uploaded_file) -> Path:
    """Write a Streamlit ``UploadedFile`` to the uploads directory.

    Returns the Path to the saved file on disk.
    """
    dest = resolve_upload_dest(upload_dir(), uploaded_file.name)
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
        dest = resolve_upload_dest(batch_dir, uf.name)
        with open(dest, "wb") as f:
            f.write(uf.getbuffer())
    return batch_dir
