"""Tests for Convert File result rendering helpers."""

from __future__ import annotations

from app.execute_render import _markdown_preview_html


def test_markdown_preview_escapes_document_html():
    rendered = _markdown_preview_html("# Title\n\n<script>alert('x')</script> & text")

    assert "<script>" not in rendered
    assert "&lt;script&gt;alert(&#x27;x&#x27;)&lt;/script&gt; &amp; text" in rendered
    assert "white-space:pre-wrap" in rendered
