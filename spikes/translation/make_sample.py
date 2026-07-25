"""Generate the non-Latin-script sample documents used by the #65 translation spike.

The repo's existing fixtures (``tmp/easy``, ``tmp/hard``) are all English and all
born-digital, so neither exercises the two things the spike needs to measure:
a **non-Latin source script** and a **scanned page with no text layer**. Rather
than commit a copyrighted real-world document, this script synthesises a
plausible Russian commercial document — a completed-works certificate with a
header block, a five-column priced table, a narrow fixed-width field, prose, a
signature block and a round stamp — and emits it twice:

``act_digital.pdf``
    Born-digital, with a real text layer. This is the best case for any tool
    that reads the PDF's text objects directly (``pdf2zh`` included).

``act_scanned.pdf``
    The same pages rasterised at 200 DPI and re-wrapped as image-only pages —
    no text layer at all. This is the case the fleet actually cares about
    (phone photos, scanner output, faxed forms) and the one that separates
    OCR-backed pipelines from text-layer-only ones.

Both land in ``tmp/`` (git-ignored). Run from the project root::

    & .\\.venv\\Scripts\\python.exe -m spikes.translation.make_sample

Deliberate design choices, each targeting a risk named in issue #65:

* **Cyrillic body text** — the script is non-Latin but stays in the Latin-1
  metric ballpark, so a translation to English *shrinks* rather than grows.
  The reverse pair (EN → RU) is where overflow bites; the narrow ``Код`` box
  below is the probe for it.
* **A 22 mm-wide fixed field** holding ``Код: 42-А``. Any layout-preserving
  reinsertion has to fit the translated string into that exact box; it is the
  cheapest possible instance of the length-expansion problem.
* **A round stamp drawn as vector art with text on it** — a non-text element
  that must survive untouched, and a piece of text that is *not* part of the
  reading order.
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path

import fitz  # PyMuPDF

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
logger = logging.getLogger("make_sample")

PROJECT_ROOT = Path(__file__).resolve().parents[2]
OUT_DIR = PROJECT_ROOT / "tmp" / "translation_spike"

# Arial ships with Windows and covers Cyrillic. On a machine without it, point
# these at any TTF with Cyrillic coverage (DejaVuSans, Noto Sans).
FONT_REGULAR = Path("C:/Windows/Fonts/arial.ttf")
FONT_BOLD = Path("C:/Windows/Fonts/arialbd.ttf")

PAGE_W, PAGE_H = fitz.paper_size("a4")
MARGIN = 56.0
SCAN_DPI = 200

INK = (0.05, 0.05, 0.08)
MUTED = (0.35, 0.35, 0.40)
RULE = (0.72, 0.72, 0.76)
STAMP = (0.13, 0.22, 0.62)


def _fonts() -> tuple[str, str]:
    """Return ``(regular, bold)`` font aliases, failing loudly if the TTFs are missing."""
    missing = [str(p) for p in (FONT_REGULAR, FONT_BOLD) if not p.exists()]
    if missing:
        raise FileNotFoundError(
            "Cyrillic-capable fonts not found: "
            + ", ".join(missing)
            + ". Edit FONT_REGULAR / FONT_BOLD to point at a TTF with Cyrillic coverage."
        )
    return "sample-regular", "sample-bold"


def _text(page: fitz.Page, x: float, y: float, s: str, *, size: float = 9.5,
          bold: bool = False, color: tuple = INK) -> None:
    reg, bld = _fonts()
    page.insert_text(
        (x, y), s,
        fontname=bld if bold else reg,
        fontfile=str(FONT_BOLD if bold else FONT_REGULAR),
        fontsize=size, color=color,
    )


def _box(page: fitz.Page, rect: fitz.Rect, s: str, *, size: float = 9.5,
         bold: bool = False, align: int = fitz.TEXT_ALIGN_LEFT,
         color: tuple = INK) -> None:
    reg, bld = _fonts()
    page.insert_textbox(
        rect, s,
        fontname=bld if bold else reg,
        fontfile=str(FONT_BOLD if bold else FONT_REGULAR),
        fontsize=size, color=color, align=align,
    )


# Column x-offsets and widths for the priced table, in points.
TABLE_COLS: list[tuple[str, float, int]] = [
    ("№", 26, fitz.TEXT_ALIGN_CENTER),
    ("Наименование работ", 238, fitz.TEXT_ALIGN_LEFT),
    ("Кол-во", 52, fitz.TEXT_ALIGN_CENTER),
    ("Цена, ₽", 78, fitz.TEXT_ALIGN_RIGHT),
    ("Сумма, ₽", 88, fitz.TEXT_ALIGN_RIGHT),
]

TABLE_ROWS: list[tuple[str, ...]] = [
    ("1", "Демонтаж существующих перегородок", "24 м²", "1 250,00", "30 000,00"),
    ("2", "Монтаж гипсокартонных конструкций", "48 м²", "1 780,00", "85 440,00"),
    ("3", "Прокладка электропроводки скрытым способом", "162 м", "310,00", "50 220,00"),
    ("4", "Установка распределительного щита ЩР-24", "1 шт.", "18 900,00", "18 900,00"),
    ("5", "Пусконаладочные работы и приёмо-сдаточные испытания", "1 усл.", "27 400,00", "27 400,00"),
]

TOTALS: list[tuple[str, str, bool]] = [
    ("Итого:", "211 960,00", False),
    ("НДС 20 %:", "42 392,00", False),
    ("Всего к оплате:", "254 352,00", True),
]

WARRANTY = (
    "Исполнитель гарантирует качество выполненных работ в течение 24 (двадцати четырёх) "
    "месяцев с даты подписания настоящего Акта обеими Сторонами. Гарантийные обязательства "
    "не распространяются на дефекты, возникшие вследствие нарушения Заказчиком правил "
    "эксплуатации, самостоятельного вмешательства в смонтированные системы или воздействия "
    "обстоятельств непреодолимой силы."
)

CLAIMS = (
    "Претензии по объёму, стоимости и качеству работ Заказчиком не заявлены. Работы выполнены "
    "в полном объёме и в установленный договором срок. Стороны подтверждают отсутствие взаимных "
    "финансовых требований, за исключением обязательства Заказчика оплатить сумму, указанную в "
    "разделе «Всего к оплате» настоящего Акта, в течение 10 (десяти) банковских дней."
)


def _page_one(doc: fitz.Document) -> None:
    page = doc.new_page(width=PAGE_W, height=PAGE_H)
    right = PAGE_W - MARGIN

    # Letterhead
    _text(page, MARGIN, 74, "ООО «СТРОЙМОНТАЖ ПЛЮС»", size=13, bold=True)
    _text(page, MARGIN, 90, "129090, г. Москва, Проспект Мира, д. 19, стр. 1", size=8, color=MUTED)
    _text(page, MARGIN, 102, "ИНН 7702345678 · КПП 770201001 · ОГРН 1157746321098", size=8, color=MUTED)
    page.draw_line(fitz.Point(MARGIN, 112), fitz.Point(right, 112), color=RULE, width=0.8)

    # Title
    _box(page, fitz.Rect(MARGIN, 132, right, 172),
         "АКТ № 2026-0417\nВЫПОЛНЕННЫХ РАБОТ", size=15, bold=True,
         align=fitz.TEXT_ALIGN_CENTER)

    _text(page, MARGIN, 196, "г. Москва", size=9.5)
    _box(page, fitz.Rect(right - 160, 186, right, 202), "17 апреля 2026 г.",
         size=9.5, align=fitz.TEXT_ALIGN_RIGHT)

    # Parties
    y = 224
    for label, value in (
        ("Исполнитель:", "ООО «Строймонтаж Плюс», в лице генерального директора Ковалёва А. И."),
        ("Заказчик:", "АО «Северный Терминал», в лице директора по эксплуатации Мельник О. В."),
        ("Основание:", "Договор подряда № 41/СМ от 3 февраля 2026 г."),
    ):
        _text(page, MARGIN, y, label, size=9.5, bold=True)
        _box(page, fitz.Rect(MARGIN + 78, y - 10, right, y + 22), value, size=9.5)
        y += 26

    # The narrow fixed-width field — the overflow probe.
    code_rect = fitz.Rect(right - 62, y - 4, right, y + 14)
    page.draw_rect(code_rect, color=RULE, width=0.8)
    _box(page, code_rect + (2, 3, -2, 0), "Код: 42-А", size=8,
         align=fitz.TEXT_ALIGN_CENTER)
    _text(page, MARGIN, y + 9, "Настоящим Стороны подтверждают, что работы выполнены в следующем объёме:", size=9.5)

    # Priced table
    top = y + 32
    row_h = 26.0
    header_h = 22.0
    _draw_table(page, top, row_h, header_h)

    table_bottom = top + header_h + row_h * len(TABLE_ROWS)

    # Totals, right-aligned under the table
    ty = table_bottom + 20
    for label, value, emphasise in TOTALS:
        _box(page, fitz.Rect(right - 260, ty - 10, right - 96, ty + 6), label,
             size=10 if emphasise else 9.5, bold=emphasise, align=fitz.TEXT_ALIGN_RIGHT)
        _box(page, fitz.Rect(right - 92, ty - 10, right, ty + 6), value,
             size=10 if emphasise else 9.5, bold=emphasise, align=fitz.TEXT_ALIGN_RIGHT)
        ty += 20

    _text(page, MARGIN, PAGE_H - 56, "Страница 1 из 2", size=8, color=MUTED)


def _draw_table(page: fitz.Page, top: float, row_h: float, header_h: float) -> None:
    xs: list[float] = [MARGIN]
    for _, w, _align in TABLE_COLS:
        xs.append(xs[-1] + w)

    bottom = top + header_h + row_h * len(TABLE_ROWS)

    # Header band
    page.draw_rect(fitz.Rect(xs[0], top, xs[-1], top + header_h),
                   color=None, fill=(0.93, 0.94, 0.97))

    for i, (title, _w, align) in enumerate(TABLE_COLS):
        _box(page, fitz.Rect(xs[i] + 4, top + 6, xs[i + 1] - 4, top + header_h),
             title, size=8.5, bold=True, align=align)

    for r, row in enumerate(TABLE_ROWS):
        ry = top + header_h + r * row_h
        for i, cell in enumerate(row):
            _, _w, align = TABLE_COLS[i]
            _box(page, fitz.Rect(xs[i] + 4, ry + 5, xs[i + 1] - 4, ry + row_h),
                 cell, size=8.5, align=align)

    # Grid
    for x in xs:
        page.draw_line(fitz.Point(x, top), fitz.Point(x, bottom), color=RULE, width=0.7)
    for r in range(len(TABLE_ROWS) + 1):
        ly = top + header_h + r * row_h
        page.draw_line(fitz.Point(xs[0], ly), fitz.Point(xs[-1], ly), color=RULE, width=0.7)
    page.draw_line(fitz.Point(xs[0], top), fitz.Point(xs[-1], top), color=RULE, width=0.7)


def _page_two(doc: fitz.Document) -> None:
    page = doc.new_page(width=PAGE_W, height=PAGE_H)
    right = PAGE_W - MARGIN

    _text(page, MARGIN, 78, "1. Гарантийные обязательства", size=11, bold=True)
    _box(page, fitz.Rect(MARGIN, 90, right, 168), WARRANTY, size=9.5)

    _text(page, MARGIN, 186, "2. Отсутствие претензий", size=11, bold=True)
    _box(page, fitz.Rect(MARGIN, 198, right, 276), CLAIMS, size=9.5)

    _text(page, MARGIN, 300, "3. Банковские реквизиты Исполнителя", size=11, bold=True)
    _box(page, fitz.Rect(MARGIN, 312, right, 372),
         "Банк: АО «Альфа-Банк», г. Москва\n"
         "Р/с 40702810401234567890 · К/с 30101810200000000593 · БИК 044525593\n"
         "SWIFT: ALFARUMM · IBAN-эквивалент недоступен для расчётов в рублях",
         size=9.5)

    # Signature block
    sy = 430
    for x, role, who in (
        (MARGIN, "Исполнитель", "А. И. Ковалёв"),
        (PAGE_W / 2 + 12, "Заказчик", "О. В. Мельник"),
    ):
        _text(page, x, sy, role, size=9.5, bold=True)
        page.draw_line(fitz.Point(x, sy + 42), fitz.Point(x + 190, sy + 42), color=INK, width=0.8)
        _text(page, x, sy + 54, f"/ {who} /", size=8.5, color=MUTED)
        _text(page, x, sy + 68, "М.П.", size=8.5, color=MUTED)

    _stamp(page, fitz.Point(MARGIN + 96, sy + 96))
    _text(page, MARGIN, PAGE_H - 56, "Страница 2 из 2", size=8, color=MUTED)


def _stamp(page: fitz.Page, centre: fitz.Point) -> None:
    """Draw a round company seal — vector art plus text that is not reading-order text."""
    page.draw_circle(centre, 54, color=STAMP, width=1.6)
    page.draw_circle(centre, 46, color=STAMP, width=0.8)
    _box(page, fitz.Rect(centre.x - 40, centre.y - 22, centre.x + 40, centre.y + 4),
         "СТРОЙМОНТАЖ\nПЛЮС", size=8, bold=True, align=fitz.TEXT_ALIGN_CENTER, color=STAMP)
    _box(page, fitz.Rect(centre.x - 40, centre.y + 6, centre.x + 40, centre.y + 30),
         "ОГРН\n1157746321098", size=6, align=fitz.TEXT_ALIGN_CENTER, color=STAMP)


def build_digital(dest: Path) -> Path:
    """Write the born-digital two-page sample to *dest*."""
    doc = fitz.open()
    _page_one(doc)
    _page_two(doc)
    dest.parent.mkdir(parents=True, exist_ok=True)
    doc.save(str(dest), garbage=4, deflate=True)
    doc.close()
    logger.info("✅ Born-digital sample → %s (%d bytes)", dest, dest.stat().st_size)
    return dest


def build_scanned(source: Path, dest: Path, dpi: int = SCAN_DPI) -> Path:
    """Rasterise *source* at *dpi* and re-wrap it as an image-only PDF at *dest*.

    The result has **no text layer**, which is the whole point: it is the input
    shape that separates an OCR-backed pipeline from a text-layer-only one.
    """
    src = fitz.open(str(source))
    out = fitz.open()
    for page in src:
        pix = page.get_pixmap(dpi=dpi, colorspace=fitz.csGRAY)
        new = out.new_page(width=page.rect.width, height=page.rect.height)
        new.insert_image(new.rect, pixmap=pix)
    out.save(str(dest), garbage=4, deflate=True)
    out.close()
    src.close()

    check = fitz.open(str(dest))
    layer_chars = sum(len(p.get_text().strip()) for p in check)
    check.close()
    if layer_chars:
        raise RuntimeError(
            f"{dest.name} still carries a text layer ({layer_chars} chars) — "
            "rasterisation did not take effect."
        )
    logger.info("✅ Scanned sample → %s (%d bytes, 0 chars of text layer)", dest, dest.stat().st_size)
    return dest


def main() -> int:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    digital = build_digital(OUT_DIR / "act_digital.pdf")
    build_scanned(digital, OUT_DIR / "act_scanned.pdf")
    return 0


if __name__ == "__main__":
    sys.exit(main())
