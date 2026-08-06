"""Tests for remote-upload destination handling.

Uploaded file names arrive from the browser and are attacker-controlled, so the
destination they resolve to must always land inside the intended folder.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from app import remote_upload
from app.remote_upload import resolve_upload_dest, safe_upload_name


class _FakeUpload:
    """Minimal stand-in for a Streamlit ``UploadedFile``."""

    def __init__(self, name: str, data: bytes = b"%PDF-1.4\n") -> None:
        self.name = name
        self._data = data

    def getbuffer(self) -> bytes:
        return self._data


@pytest.mark.parametrize(
    "supplied",
    [
        "../escaped.pdf",
        "..\\escaped.pdf",
        "../../escaped.pdf",
        "..\\..\\escaped.pdf",
        "sub/dir/escaped.pdf",
        "sub\\dir\\escaped.pdf",
        "C:/Windows/Temp/escaped.pdf",
        "/etc/cron.d/escaped.pdf",
    ],
)
def test_supplied_name_cannot_leave_the_target_directory(tmp_path: Path, supplied: str) -> None:
    dest = resolve_upload_dest(tmp_path, supplied)

    assert dest.resolve().parent == tmp_path.resolve()
    assert dest.name == "escaped.pdf"


def test_ordinary_name_is_preserved(tmp_path: Path) -> None:
    assert resolve_upload_dest(tmp_path, "quarterly report.pdf").name == "quarterly report.pdf"


@pytest.mark.parametrize("supplied", ["", "   ", ".", "..", "../", "..\\"])
def test_degenerate_names_fall_back_to_a_usable_name(tmp_path: Path, supplied: str) -> None:
    dest = resolve_upload_dest(tmp_path, supplied)

    assert dest.name
    assert dest.resolve().parent == tmp_path.resolve()


def test_safe_upload_name_strips_both_separator_styles() -> None:
    assert safe_upload_name("a/b/c.pdf") == "c.pdf"
    assert safe_upload_name("a\\b\\c.pdf") == "c.pdf"


def test_single_save_writes_inside_the_upload_dir(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(remote_upload, "_UPLOAD_DIR", tmp_path / "uploads")

    dest = remote_upload.save_uploaded_file(_FakeUpload("../../pwned.pdf"))

    assert dest.name == "pwned.pdf"
    assert dest.resolve().parent == (tmp_path / "uploads").resolve()
    assert not (tmp_path / "pwned.pdf").exists()
    assert dest.read_bytes() == b"%PDF-1.4\n"


def test_batch_save_keeps_every_file_inside_the_batch_folder(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(remote_upload, "_UPLOAD_DIR", tmp_path / "uploads")

    folder = remote_upload.save_uploaded_files(
        [_FakeUpload("ok.pdf"), _FakeUpload("../../pwned.pdf")]
    )

    written = sorted(p.name for p in folder.iterdir())
    assert written == ["ok.pdf", "pwned.pdf"]
    assert not (tmp_path / "pwned.pdf").exists()
    assert not (tmp_path / "uploads" / "pwned.pdf").exists()
