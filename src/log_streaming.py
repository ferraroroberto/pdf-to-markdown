"""Worker-thread log streaming primitives.

:class:`TeeStream` and :class:`QueueHandler` tee a conversion worker's
``stdout``/``stderr`` and ``logging`` records into a thread-safe queue so the
Streamlit tabs can render a live Execution Log.  They contain no Streamlit (or
any UI) dependency — only :mod:`io`, :mod:`logging`, and :mod:`queue` — so the
non-UI conversion worker (``src.execute_worker``) can import them without
pulling Streamlit into a non-UI module.  ``app/_common.py`` re-exports both for
the UI tabs, which import them from there.
"""

from __future__ import annotations

import io
import logging
import queue


class TeeStream(io.TextIOBase):
    """Tee a text stream into a queue, line by line, while passing it through."""

    def __init__(self, log_queue: queue.Queue, original: io.TextIOBase) -> None:
        self._q = log_queue
        self._orig = original
        self._buf = ""

    def write(self, s: str) -> int:
        try:
            self._orig.write(s)
            self._orig.flush()
        except Exception:  # noqa: BLE001
            pass
        self._buf += s
        *lines, self._buf = self._buf.split("\n")
        for line in lines:
            clean = line.rstrip("\r").strip()
            if clean:
                self._q.put(clean)
        return len(s)

    def flush(self) -> None:
        try:
            self._orig.flush()
        except Exception:  # noqa: BLE001
            pass
        if self._buf.strip():
            self._q.put(self._buf.rstrip("\r").strip())
            self._buf = ""

    def isatty(self) -> bool:
        return False

    @property
    def encoding(self) -> str:
        return getattr(self._orig, "encoding", "utf-8") or "utf-8"

    @property
    def errors(self) -> str:
        return getattr(self._orig, "errors", "replace") or "replace"


class QueueHandler(logging.Handler):
    """A :class:`logging.Handler` that pushes formatted records onto a queue."""

    def __init__(self, log_queue: queue.Queue) -> None:
        super().__init__()
        self._q = log_queue

    def emit(self, record: logging.LogRecord) -> None:
        self._q.put(self.format(record))
