"""
macrt.ui.theme - Colors, fonts, metrics and bitmaps shared by the chrome.

Classic Mac OS was monochrome.  Every widget pulls from here so that
restyling the whole runtime is a single-file change.

Drop Chicago/Geneva lookalikes (e.g. ChicagoFLF) as .ttf/.otf files into
macrt/ui/fonts/ and they are picked up automatically.
"""

from __future__ import annotations

from functools import cache
from pathlib import Path

from PyQt6.QtCore import Qt
from PyQt6.QtGui import QColor, QFont, QFontDatabase, QPainter


# ---------------------------------------------------------------------------
# Colors
# ---------------------------------------------------------------------------
BLACK = QColor("#000000")
WHITE = QColor("#FFFFFF")
GREY = QColor("#888888")
# 50% dither, used for the desktop, disabled text and menu separators.
DITHER = Qt.BrushStyle.Dense4Pattern

# System 7 six-color apple, top to bottom.
APPLE_RAINBOW = [
    QColor("#61BB46"), QColor("#FDB827"), QColor("#F5821F"),
    QColor("#E03A3E"), QColor("#963D97"), QColor("#009DDC"),
]
RAINBOW_APPLE = False


# ---------------------------------------------------------------------------
# Window metrics (pixels)
# ---------------------------------------------------------------------------
BORDER = 1
TITLE_HEIGHT = 18          # title bar rows below the top border, incl. its bottom line
BOX_SIZE = 11              # close and zoom boxes
BOX_INSET = 7              # box distance from the window's outer edge
BOX_CLEARANCE = 1          # white gap the stripes leave around a box
TITLE_CLEARANCE = 6        # white gap the stripes leave around the title
PINSTRIPE_SPACING = 2
STRIPE_INSET = 1           # white gap between the stripes and the frame
SHADOW_OFFSET = 1          # document window drop shadow
ZOOM_INNER_SIZE = 7        # the small square inside the zoom box
ZOOM_INNER_POS = (0, 0)

GROW_BOX = 15              # size box in the content's bottom-right corner
SCROLL_BAR = 16

# dBoxProc frame, outside in: (thickness, color)
DBOX_LAYERS = [(1, BLACK), (1, WHITE), (2, BLACK), (4, WHITE)]
ALT_DBOX_SHADOW = 2
ROUNDED_TITLE_HEIGHT = 19


# ---------------------------------------------------------------------------
# Menu metrics (pixels)
# ---------------------------------------------------------------------------
MENU_BAR_HEIGHT = 20       # incl. its bottom line
MENU_BAR_LEFT = 10
MENU_TITLE_PADDING = 7     # each side of a title
MENU_ITEM_HEIGHT = 16
MENU_LEFT_PAD = 14         # room for the mark column
MENU_RIGHT_PAD = 10
MENU_KEY_GAP = 24
MENU_SHADOW = 1
MENU_BLINKS = 3            # times a chosen item flashes ("Menu Blinking")
MENU_BLINK_MS = 40
STICKY_CLICK_MS = 300      # a shorter click on a title leaves the menu open


# ---------------------------------------------------------------------------
# Fonts
# ---------------------------------------------------------------------------
# None: antialias only when we had to fall back to an outline font.  A
# bitmap-style Mac font (Chicago, Geneva...) in macrt/ui/fonts/ is drawn crisp.
TEXT_ANTIALIAS: bool | None = None
_fonts_loaded = False


def _load_bundled_fonts() -> None:
    global _fonts_loaded
    if _fonts_loaded:
        return
    _fonts_loaded = True
    for path in sorted((Path(__file__).parent / "fonts").glob("*.[ot]tf")):
        QFontDatabase.addApplicationFont(str(path))


def _try_font(preferences: list[tuple[str, int]], fallback: tuple[str, int],
              bold: bool = False) -> QFont:
    """The first installed font from `preferences` (family, pixel size)."""
    _load_bundled_fonts()
    families = set(QFontDatabase.families())
    name, size = next(((n, s) for n, s in preferences if n in families), fallback)
    f = QFont(name)
    f.setPixelSize(size)       # Mac points were screen pixels (72 dpi)
    f.setBold(bold)
    antialias = TEXT_ANTIALIAS if TEXT_ANTIALIAS is not None else (name, size) == fallback
    if not antialias:
        f.setStyleStrategy(QFont.StyleStrategy.NoAntialias)
    return f


SYSTEM_FONTS = [("Chicago", 12), ("ChicagoFLF", 12), ("Charcoal", 12)]
APP_FONTS = [("Geneva", 12), ("Chicago", 12)]
FALLBACK_FONT = ("Helvetica", 13)


@cache
def title_font() -> QFont:
    return _try_font(SYSTEM_FONTS, FALLBACK_FONT, bold=True)


@cache
def menu_bar_font() -> QFont:
    return _try_font(SYSTEM_FONTS, FALLBACK_FONT, bold=True)


@cache
def menu_item_font() -> QFont:
    # Menus use the system font (Chicago), same as the menu bar.
    return _try_font(SYSTEM_FONTS, FALLBACK_FONT, bold=True)


@cache
def body_font() -> QFont:
    return _try_font(APP_FONTS, FALLBACK_FONT)


# ---------------------------------------------------------------------------
# Menu style flag bits (MENU resource item style byte)
# ---------------------------------------------------------------------------
STYLE_BOLD = 0x01
STYLE_ITALIC = 0x02
STYLE_UNDERLINE = 0x04
STYLE_OUTLINE = 0x08
STYLE_SHADOW = 0x10
STYLE_CONDENSE = 0x20
STYLE_EXTEND = 0x40


# ---------------------------------------------------------------------------
# Bitmaps.  Chicago's special glyphs don't exist in host fonts, so the ones
# the chrome needs are drawn as 1-bit pixel art.
# ---------------------------------------------------------------------------
APPLE_ICON = [
    "......##..",
    ".....##...",
    ".....#....",
    ".###...##.",
    "#########.",
    "########..",
    "#######...",
    "#######...",
    "########..",
    "#########.",
    ".########.",
    "..##..##..",
]

COMMAND_GLYPH = [
    ".##...##.",
    "#..#.#..#",
    "#..#.#..#",
    ".#######.",
    "...#.#...",
    ".#######.",
    "#..#.#..#",
    "#..#.#..#",
    ".##...##.",
]

CHECK_GLYPH = [
    "........#",
    ".......##",
    "......##.",
    "#....##..",
    "##..##...",
    ".####....",
    "..##.....",
]

# A hierarchical item's arrow, at the right where a Command key would go.
SUBMENU_GLYPH = [
    "#....",
    "##...",
    "###..",
    "####.",
    "#####",
    "####.",
    "###..",
    "##...",
    "#....",
]

# Top-left rounded screen corner (host="screen"); mirrored for the others.
SCREEN_CORNER = [
    "#####",
    "###..",
    "##...",
    "#....",
    "#....",
]

# Stand-in for an application's ICN# in the Application menu title.
GENERIC_APP_ICON = [
    ".......#.......",
    "......#.#......",
    ".....#...#.....",
    "....#.###.#....",
    "...#.#...#.#...",
    "..#.#..#..#.#..",
    ".#.#..###..#.#.",
    "#.#..#####..#.#",
    ".#.#..###..#.#.",
    "..#.#..#..#.#..",
    "...#.#...#.#...",
    "....#.###.#....",
    ".....#...#.....",
    "......#.#......",
    ".......#.......",
]


def bitmap_size(bitmap: list[str]) -> tuple[int, int]:
    return len(bitmap[0]), len(bitmap)


def draw_bitmap(p: QPainter, x: int, y: int, bitmap: list[str],
                color: QColor = BLACK, row_colors: list[QColor] | None = None) -> None:
    for r, row in enumerate(bitmap):
        c = color
        if row_colors:
            c = row_colors[min(r * len(row_colors) // len(bitmap), len(row_colors) - 1)]
        start = None
        for i, ch in enumerate(row + "."):
            if ch == "#" and start is None:
                start = i
            elif ch != "#" and start is not None:
                p.fillRect(x + start, y + r, i - start, 1, c)
                start = None
