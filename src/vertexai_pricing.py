"""Vertex AI Gemini pricing — fetch, cache, and compute costs from the official pricing page.

Usage
-----
    from src import vertexai_pricing

    pricing = vertexai_pricing.load_pricing()          # dict[model_id, {input, output}]
    label, found = vertexai_pricing.calculate_cost(    # ("$0.0042", True) or ("not found", False)
        model_id, input_tokens, output_tokens
    )
    vertexai_pricing.fetch_and_cache()                 # refresh live pricing → writes JSON + MD cache
"""

from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timezone
from html.parser import HTMLParser
from pathlib import Path
from urllib.request import Request, urlopen

logger = logging.getLogger("vertexai_pricing")

_PRICING_URL = "https://cloud.google.com/vertex-ai/generative-ai/pricing"
_PROJECT_ROOT = Path(__file__).parent.parent
CACHE_JSON_PATH = _PROJECT_ROOT / "pricing" / "vertexai_pricing_cache.json"
CACHE_MD_PATH = _PROJECT_ROOT / "pricing" / "vertexai_pricing.md"

# ---------------------------------------------------------------------------
# Hardcoded fallback — extracted from the Google Cloud pricing page 2026-03-27
# Standard tier, ≤ 200K input-token context window, USD per 1 M tokens.
# ---------------------------------------------------------------------------
_FALLBACK_PRICING: dict[str, dict[str, float]] = {
    # Gemini 3.x
    "gemini-3.1-pro-preview":         {"input": 2.00,  "output": 12.00},
    "gemini-3.1-flash-lite-preview":  {"input": 0.25,  "output": 1.50},
    "gemini-3.1-flash-image-preview": {"input": 0.50,  "output": 3.00},
    "gemini-3-pro-preview":           {"input": 2.00,  "output": 12.00},
    "gemini-3-flash-preview":         {"input": 0.50,  "output": 3.00},
    # Gemini 2.5
    "gemini-2.5-pro":                 {"input": 1.25,  "output": 10.00},
    "gemini-2.5-flash":               {"input": 0.30,  "output": 2.50},
    "gemini-2.5-flash-lite":          {"input": 0.10,  "output": 0.40},
    # Gemini 2.0
    "gemini-2.0-flash":               {"input": 0.15,  "output": 0.60},
    "gemini-2.0-flash-lite":          {"input": 0.075, "output": 0.30},
    # Gemini 1.5
    "gemini-1.5-flash":               {"input": 0.075, "output": 0.30},
    "gemini-1.5-pro":                 {"input": 1.25,  "output": 5.00},
}

# Map of display name fragments (lower-case) → canonical API model IDs
_DISPLAY_TO_ID: dict[str, str] = {
    "gemini 3.1 pro preview":          "gemini-3.1-pro-preview",
    "gemini 3.1 flash-lite preview":   "gemini-3.1-flash-lite-preview",
    "gemini 3.1 flash image preview":  "gemini-3.1-flash-image-preview",
    "gemini 3 pro preview":            "gemini-3-pro-preview",
    "gemini 3 pro image preview":      "gemini-3-pro-image-preview",
    "gemini 3 flash preview":          "gemini-3-flash-preview",
    "gemini 2.5 pro":                  "gemini-2.5-pro",
    "gemini 2.5flash":                 "gemini-2.5-flash",   # typo on page
    "gemini 2.5 flash":                "gemini-2.5-flash",
    "gemini 2.5 flash lite":           "gemini-2.5-flash-lite",
    "gemini 2.5 flash-lite":           "gemini-2.5-flash-lite",
    "gemini 2.0 flash":                "gemini-2.0-flash",
    "gemini 2.0 flash lite":           "gemini-2.0-flash-lite",
    "gemini 1.5 flash":                "gemini-1.5-flash",
    "gemini 1.5 pro":                  "gemini-1.5-pro",
    "gemini 1.0 pro":                  "gemini-1.0-pro",
}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _normalize_model_name(name: str) -> str:
    """Convert a display name like 'Gemini 2.5 Pro' to a canonical model ID."""
    key = name.strip().lower()
    if key in _DISPLAY_TO_ID:
        return _DISPLAY_TO_ID[key]
    for display, model_id in _DISPLAY_TO_ID.items():
        if display in key or key in display:
            return model_id
    return re.sub(r"\s+", "-", key)


def _parse_price(cell: str) -> float | None:
    """Extract a USD dollar amount from a cell like '$1.25' or 'N/A'."""
    s = cell.strip()
    if s in ("N/A", "n/a", "", "-", "—"):
        return None
    m = re.search(r"\$?([\d,]+\.?\d*)", s)
    if m:
        return float(m.group(1).replace(",", ""))
    return None


# ---------------------------------------------------------------------------
# HTML parser
# ---------------------------------------------------------------------------


class _PricingParser(HTMLParser):
    """Extract Gemini pricing from Google Cloud's Vertex AI pricing HTML.

    Tracks <h3> headings to identify Standard vs Priority/Batch sections and
    only records pricing from Standard (or token-based) sections.
    """

    _INPUT_KEYWORDS = ("input (text", "1m input tokens", "input text")
    _OUTPUT_KEYWORDS = ("text output", "1m output text tokens", "output text")

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._pricing: dict[str, dict[str, float]] = {}
        self._skip: int = 0
        # Heading state
        self._in_heading = False
        self._heading_buf = ""
        self._is_standard_section = False
        # Table / row / cell state
        self._in_table = False
        self._in_row = False
        self._in_cell = False
        self._cells: list[str] = []
        self._cell_buf = ""
        self._current_model: str | None = None

    def handle_starttag(self, tag: str, attrs: list) -> None:  # type: ignore[override]
        if tag in ("script", "style", "noscript"):
            self._skip += 1
            return
        if self._skip:
            return
        if tag in ("h2", "h3"):
            self._in_heading = True
            self._heading_buf = ""
        elif tag == "table":
            self._in_table = True
            self._current_model = None
        elif tag == "tr" and self._in_table:
            self._in_row = True
            self._cells = []
        elif tag in ("td", "th") and self._in_row:
            self._in_cell = True
            self._cell_buf = ""

    def handle_endtag(self, tag: str) -> None:  # type: ignore[override]
        if tag in ("script", "style", "noscript"):
            self._skip = max(0, self._skip - 1)
            return
        if self._skip:
            return
        if tag in ("h2", "h3"):
            self._in_heading = False
            heading = self._heading_buf.strip().lower()
            self._is_standard_section = heading in ("standard", "token-based pricing")
            self._current_model = None
        elif tag == "table":
            self._in_table = False
            self._current_model = None
        elif tag == "tr" and self._in_row:
            self._in_row = False
            if self._cells and self._is_standard_section:
                self._process_row(self._cells)
            self._cells = []
        elif tag in ("td", "th") and self._in_cell:
            self._in_cell = False
            self._cells.append(self._cell_buf.strip())

    def handle_data(self, data: str) -> None:  # type: ignore[override]
        if self._skip:
            return
        if self._in_heading:
            self._heading_buf += data
        elif self._in_cell:
            self._cell_buf += data

    def _process_row(self, cells: list[str]) -> None:
        non_empty = [c for c in cells if c.strip()]
        if not non_empty:
            return
        first = non_empty[0].strip()
        if first.lower() in ("model", "type", "feature"):
            return

        # Model name row: single non-empty cell that begins with "Gemini"
        if first.lower().startswith("gemini") and len(non_empty) == 1:
            self._current_model = _normalize_model_name(first)
            self._pricing.setdefault(self._current_model, {})
            return

        if self._current_model is None:
            return

        first_lower = first.lower()

        if any(kw in first_lower for kw in self._INPUT_KEYWORDS):
            for c in cells[1:]:
                price = _parse_price(c)
                if price is not None:
                    self._pricing[self._current_model].setdefault("input", price)
                    break

        elif any(kw in first_lower for kw in self._OUTPUT_KEYWORDS):
            for c in cells[1:]:
                price = _parse_price(c)
                if price is not None:
                    self._pricing[self._current_model].setdefault("output", price)
                    break

    def get_pricing(self) -> dict[str, dict[str, float]]:
        return {k: v for k, v in self._pricing.items() if "input" in v and "output" in v}


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def fetch_and_cache() -> dict[str, dict[str, float]]:
    """Fetch live pricing from Google Cloud, write JSON + Markdown caches, and return the dict.

    Raises RuntimeError if the page cannot be fetched or yields no data.
    Falls back to _FALLBACK_PRICING if parsing produces an empty result.
    """
    logger.info("ℹ️ Fetching Vertex AI pricing from %s", _PRICING_URL)

    req = Request(
        _PRICING_URL,
        headers={
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/123.0.0.0 Safari/537.36"
            )
        },
    )
    try:
        with urlopen(req, timeout=30) as resp:
            html_content: str = resp.read().decode("utf-8", errors="replace")
    except Exception as exc:
        raise RuntimeError(f"Failed to fetch pricing page: {exc}") from exc

    parser = _PricingParser()
    parser.feed(html_content)
    pricing = parser.get_pricing()

    if not pricing:
        logger.warning("⚠️ Live fetch yielded no parseable pricing — merging fallback data")
        pricing = dict(_FALLBACK_PRICING)

    # Merge fallback entries that are missing from live data (ensures known models always present)
    for model_id, prices in _FALLBACK_PRICING.items():
        pricing.setdefault(model_id, prices)

    fetched_at = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    _write_cache(pricing, fetched_at)

    logger.info("ℹ️ Pricing cached: %d models → %s and %s", len(pricing), CACHE_JSON_PATH, CACHE_MD_PATH)
    return pricing


def load_pricing() -> dict[str, dict[str, float]]:
    """Return cached pricing dict, fetching from Google Cloud if no cache exists."""
    if CACHE_JSON_PATH.exists():
        try:
            data = json.loads(CACHE_JSON_PATH.read_text(encoding="utf-8"))
            result: dict[str, dict[str, float]] = data.get("pricing", {})
            if result:
                return result
        except Exception:  # noqa: BLE001
            pass
    # No cache — fall through to live fetch; on failure use hardcoded fallback
    try:
        return fetch_and_cache()
    except Exception as exc:  # noqa: BLE001
        logger.warning("⚠️ Could not fetch live pricing (%s) — using built-in fallback", exc)
        return dict(_FALLBACK_PRICING)


def calculate_cost(
    model_id: str,
    input_tokens: int,
    output_tokens: int,
    pricing: dict[str, dict[str, float]] | None = None,
) -> tuple[str, bool]:
    """Return (cost_label, found).

    Parameters
    ----------
    model_id:
        The Vertex AI model ID (e.g. ``"gemini-2.5-pro"``).
    input_tokens:
        Total input/prompt tokens consumed.
    output_tokens:
        Total output/candidate tokens consumed.
    pricing:
        Optional pre-loaded pricing dict; if None, calls load_pricing().

    Returns
    -------
    cost_label : str
        Human-readable cost string like ``"$0.0042"`` or ``"not found"``.
    found : bool
        True when the model was located in the pricing table.
    """
    if pricing is None:
        pricing = load_pricing()

    p: dict[str, float] | None = None

    # Exact match
    if model_id in pricing:
        p = pricing[model_id]
    else:
        # Case-insensitive fallback
        model_lower = model_id.lower()
        for key, val in pricing.items():
            if key.lower() == model_lower:
                p = val
                break

    if p is None:
        return "not found", False

    cost_usd = (
        (input_tokens / 1_000_000) * p.get("input", 0.0)
        + (output_tokens / 1_000_000) * p.get("output", 0.0)
    )
    return f"${cost_usd:.4f}", True


def get_cache_info() -> dict:
    """Return metadata about the current cache (fetched_at, num_models, paths)."""
    if CACHE_JSON_PATH.exists():
        try:
            data = json.loads(CACHE_JSON_PATH.read_text(encoding="utf-8"))
            return {
                "fetched_at": data.get("fetched_at", "unknown"),
                "num_models": len(data.get("pricing", {})),
                "json_path": str(CACHE_JSON_PATH),
                "md_path": str(CACHE_MD_PATH),
                "cached": True,
            }
        except Exception:  # noqa: BLE001
            pass
    return {
        "fetched_at": "never",
        "num_models": len(_FALLBACK_PRICING),
        "json_path": str(CACHE_JSON_PATH),
        "md_path": str(CACHE_MD_PATH),
        "cached": False,
    }


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _build_markdown_table(pricing: dict[str, dict[str, float]], fetched_at: str) -> str:
    lines = [
        "# Vertex AI Gemini Pricing",
        "",
        f"_Source: {_PRICING_URL}_",
        f"_Fetched: {fetched_at}_",
        "",
        "Standard pricing — USD per 1 million tokens, ≤ 200K input-token context.",
        "",
        "| Model ID | Input ($/1M tokens) | Output ($/1M tokens) |",
        "|----------|---------------------|----------------------|",
    ]
    for model_id in sorted(pricing):
        p = pricing[model_id]
        inp = f"${p['input']:.4f}" if "input" in p else "N/A"
        out = f"${p['output']:.4f}" if "output" in p else "N/A"
        lines.append(f"| `{model_id}` | {inp} | {out} |")
    return "\n".join(lines)


def _write_cache(pricing: dict[str, dict[str, float]], fetched_at: str) -> None:
    CACHE_JSON_PATH.parent.mkdir(parents=True, exist_ok=True)
    payload = {"fetched_at": fetched_at, "pricing": pricing}
    CACHE_JSON_PATH.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    CACHE_MD_PATH.write_text(_build_markdown_table(pricing, fetched_at), encoding="utf-8")
