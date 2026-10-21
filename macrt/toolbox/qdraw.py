"""
QuickDraw's drawing: lines, shapes, regions, polygons, text and CopyBits.

A port draws onto one of two kinds of surface.  A window's is its
Backing, the image its view on the host shows.  Any other port's is its
portBits: a 1-bit bitmap in guest memory, as on a Mac, so an application
can draw offscreen and CopyBits the result into a window, or read the
bits itself.  Either way the drawing is done with a QPainter, clipped to
the port's portRect, visRgn and clipRgn, with QuickDraw's patterns and
transfer modes.

Regions are real QuickDraw regions: the region data in guest memory is
read into a Region (rows of spans) and written back, so the region
calls are exact and an application can walk the data itself.

Text is drawn in host fonts standing in for the Mac's, by family and
size, in the port's style and transfer mode.
"""

from __future__ import annotations

import re
import struct
from contextlib import contextmanager
from functools import lru_cache
from itertools import accumulate
from typing import TYPE_CHECKING, Callable, Iterator

from PyQt6.QtCore import QPoint, QPointF, QRect, QRectF, Qt
from PyQt6.QtGui import (QBrush, QColor, QFont, QFontDatabase, QFontMetrics, QImage, QPainter,
                         QPainterPath, QPolygonF, QRegion)

from ..memory import ADDRESS_MASK, RAM
from . import constants as k
from .bitfont import AppFonts, style_offsets
from .constants import whiteColor
from .memmgr import Heap
from .quickdraw import BLACK_PAT, GrafPort
from .types import Point, Rect

if TYPE_CHECKING:
    from .system import System

# Transfer modes
srcCopy, srcOr, srcXor, srcBic, notSrcCopy, notSrcOr, notSrcXor, notSrcBic = range(8)
patCopy, patOr, patXor, patBic, notPatCopy, notPatOr, notPatXor, notPatBic = range(8, 16)

# Style bits (txFace)
bold, italic, underline, outline, shadow, condense, extend = (1 << i for i in range(7))

WHITE_RGB = 0xFFFFFFFF
BLACK_RGB = 0xFF000000
MONO_TABLE = [WHITE_RGB, BLACK_RGB]     # bit 0 white, bit 1 black, as on a Mac
MAX_BITMAP_ROWS = 4096
EMPTY = Rect(0, 0, 0, 0)
RGN_END = 0x7FFF


# =========================================================================
# Regions
# =========================================================================
def _spans_op(a: tuple, b: tuple, op: Callable[[bool, bool], bool]) -> tuple:
    """Combine two rows of spans, each a sorted tuple of boundaries
    (start, end, start, end...)."""
    if not a and not b:
        return ()
    sa, sb = set(a), set(b)
    out = []
    ina = inb = inside = False
    for x in sorted(sa | sb):
        if x in sa:
            ina = not ina
        if x in sb:
            inb = not inb
        now = op(ina, inb)
        if now != inside:
            out.append(x)
            inside = now
    return tuple(out)


class Region:
    """A region as bands of rows with the same spans:
    [(top, bottom, (left, right, left, right...)), ...], top to bottom,
    no two adjacent bands alike."""

    __slots__ = ("bands",)

    def __init__(self, bands=()):
        self.bands = list(bands)

    @classmethod
    def rect(cls, r: Rect) -> Region:
        return cls() if r.empty else cls([(r.top, r.bottom, (r.left, r.right))])

    @property
    def empty(self) -> bool:
        return not self.bands

    @property
    def bbox(self) -> Rect:
        bands = self.bands
        if not bands:
            return EMPTY
        if len(bands) == 1:
            t, b, xs = bands[0]
            return Rect(t, xs[0], b, xs[-1])
        return Rect(bands[0][0], min(b[2][0] for b in bands),
                    bands[-1][1], max(b[2][-1] for b in bands))

    @property
    def is_rect(self) -> bool:
        return len(self.bands) == 1 and len(self.bands[0][2]) == 2

    def __eq__(self, other) -> bool:
        return isinstance(other, Region) and self.bands == other.bands

    def __repr__(self) -> str:
        return f"Region({self.bands!r})"

    def offset(self, dh: int, dv: int) -> Region:
        return Region([(t + dv, b + dv, tuple(x + dh for x in xs)) for t, b, xs in self.bands])

    def contains(self, pt: Point) -> bool:
        for t, b, xs in self.bands:
            if t <= pt.v < b:
                return any(xs[i] <= pt.h < xs[i + 1] for i in range(0, len(xs), 2))
        return False

    def _combine(self, other: Region, op) -> Region:
        ys = sorted({y for t, b, _ in self.bands for y in (t, b)}
                    | {y for t, b, _ in other.bands for y in (t, b)})
        out: list = []
        ia = ib = 0
        a, b = self.bands, other.bands
        for top, bottom in zip(ys, ys[1:]):
            while ia < len(a) and a[ia][1] <= top:
                ia += 1
            while ib < len(b) and b[ib][1] <= top:
                ib += 1
            xa = a[ia][2] if ia < len(a) and a[ia][0] <= top else ()
            xb = b[ib][2] if ib < len(b) and b[ib][0] <= top else ()
            xs = _spans_op(xa, xb, op)
            if not xs:
                continue
            if out and out[-1][1] == top and out[-1][2] == xs:
                out[-1] = (out[-1][0], bottom, xs)
            else:
                out.append((top, bottom, xs))
        return Region(out)

    def union(self, other: Region) -> Region:
        return self._combine(other, lambda x, y: x or y)

    def sect(self, other: Region) -> Region:
        a, b = self.bands, other.bands
        if len(a) == 1 and len(b) == 1 and len(a[0][2]) == 2 and len(b[0][2]) == 2:
            (t1, b1, (l1, r1)), (t2, b2, (l2, r2)) = a[0], b[0]
            t, bt, l, r = max(t1, t2), min(b1, b2), max(l1, l2), min(r1, r2)
            return Region([(t, bt, (l, r))]) if t < bt and l < r else Region()
        return self._combine(other, lambda x, y: x and y)

    def diff(self, other: Region) -> Region:
        return self._combine(other, lambda x, y: x and not y)

    def xor(self, other: Region) -> Region:
        return self._combine(other, lambda x, y: x != y)

    def inset(self, dh: int, dv: int) -> Region:
        """InsetRgn: shrink by dh and dv (grow, for negative ones)."""
        r = self
        if dh:
            out = []
            for t, b, xs in r.bands:
                row = Region()
                for i in range(0, len(xs), 2):
                    l, rt = (xs[i] + dh, xs[i + 1] - dh)
                    if l < rt:
                        row = row.union(Region([(t, b, (l, rt))]))
                out.extend(row.bands)
            r = Region(_normalize(out))
        for _ in range(abs(dv)):
            up, down = r.offset(0, -1), r.offset(0, 1)
            r = r.sect(up).sect(down) if dv > 0 else r.union(up).union(down)
        return r

    def to_qregion(self) -> QRegion:
        q = QRegion()
        rects = [QRect(xs[i], t, xs[i + 1] - xs[i], b - t)
                 for t, b, xs in self.bands for i in range(0, len(xs), 2)]
        if rects:
            q.setRects(rects)
        return q

    def rects(self) -> Iterator[Rect]:
        for t, b, xs in self.bands:
            for i in range(0, len(xs), 2):
                yield Rect(t, xs[i], b, xs[i + 1])

    # -- QuickDraw's region data ------------------------------------------
    @classmethod
    def from_data(cls, data: bytes) -> Region:
        if len(data) < 10:
            return cls()
        size = struct.unpack_from(">H", data)[0]
        bbox = Rect(*struct.unpack_from(">hhhh", data, 2))
        if size <= 10 or len(data) <= 10:
            return cls.rect(bbox)
        words = struct.unpack_from(f">{(min(size, len(data)) - 10) // 2}h", data, 10)
        bands: list = []
        cur: set[int] = set()
        prev_v = None
        i = 0
        while i < len(words):
            v = words[i]
            i += 1
            if v == RGN_END:
                break
            if prev_v is not None and cur:
                bands.append((prev_v, v, tuple(sorted(cur))))
            while i < len(words) and words[i] != RGN_END:
                cur ^= {words[i]}
                i += 1
            i += 1
            prev_v = v
        return cls(_normalize(bands))

    def to_data(self) -> bytes:
        bbox = self.bbox
        if self.is_rect or self.empty:
            return struct.pack(">Hhhhh", 10, *bbox)
        words: list[int] = []

        def emit(v: int, xs) -> None:
            if xs:
                words.append(v)
                words.extend(sorted(xs))
                words.append(RGN_END)

        prev: tuple = ()
        prev_bottom = None
        for t, b, xs in self.bands:
            if prev_bottom is not None and t != prev_bottom:
                emit(prev_bottom, prev)
                prev = ()
            emit(t, set(prev) ^ set(xs))
            prev, prev_bottom = xs, b
        emit(prev_bottom, prev)
        words.append(RGN_END)
        body = struct.pack(f">{len(words)}h", *words)
        return struct.pack(">Hhhhh", 10 + len(body), *bbox) + body

    # -- from shapes --------------------------------------------------------
    @classmethod
    def from_path(cls, path: QPainterPath) -> Region:
        """The pixels a path fills, as QuickDraw would paint them."""
        box = path.boundingRect().toAlignedRect().adjusted(-1, -1, 1, 1)
        if box.isEmpty() or box.width() > 8192 or box.height() > 8192:
            return cls()
        img = QImage(box.width(), box.height(), QImage.Format.Format_Grayscale8)
        img.fill(0)
        p = QPainter(img)
        p.setRenderHint(QPainter.RenderHint.Antialiasing, False)
        p.translate(-box.x(), -box.y())
        p.fillPath(path, QColor(255, 255, 255))
        p.end()
        return cls.from_mask(img, box.x(), box.y())

    @classmethod
    def from_mask(cls, img: QImage, left: int, top: int) -> Region:
        """The non-zero pixels of a Grayscale8 image placed at (left, top)."""
        bpl = img.bytesPerLine()
        ptr = img.constBits()
        ptr.setsize(img.sizeInBytes())
        raw = bytes(ptr)
        bands: list = []
        for y in range(img.height()):
            row = raw[y * bpl:y * bpl + img.width()]
            xs: list[int] = []
            for m in re.finditer(rb"[^\x00]+", row):
                xs += (left + m.start(), left + m.end())
            xs_t = tuple(xs)
            v = top + y
            if bands and bands[-1][1] == v and bands[-1][2] == xs_t:
                bands[-1] = (bands[-1][0], v + 1, xs_t)
            elif xs_t:
                bands.append((v, v + 1, xs_t))
        return cls(bands)


def _normalize(bands: list) -> list:
    out: list = []
    for t, b, xs in sorted(bands):
        if not xs or b <= t:
            continue
        if out and out[-1][1] == t and out[-1][2] == xs:
            out[-1] = (out[-1][0], b, xs)
        else:
            out.append((t, b, xs))
    return out


def read_rgn(mem: RAM, h: int) -> Region:
    """The region a RgnHandle holds; empty for NIL or a purged handle."""
    if not h:
        return Region()
    p = mem.r32(h) & ADDRESS_MASK
    if not p:
        return Region()
    size = mem.r16(p)
    if size <= 10:
        return Region.rect(mem.r_rect(p + 2))
    return Region.from_data(mem.read(p, min(size, 0x10000)))


def write_rgn(heap: Heap, h: int, rgn: Region) -> bool:
    data = rgn.to_data()
    if heap.handle_size(h) != len(data) and not heap.set_handle_size(h, len(data)):
        return False
    heap.mem.write(heap.deref(h), data)
    return True


# =========================================================================
# Shapes as paths
# =========================================================================
def rect_path(r: Rect) -> QPainterPath:
    path = QPainterPath()
    path.addRect(QRectF(r.left, r.top, r.width, r.height))
    return path


def oval_path(r: Rect) -> QPainterPath:
    path = QPainterPath()
    path.addEllipse(QRectF(r.left, r.top, r.width, r.height))
    return path


def round_rect_path(r: Rect, oval_w: int, oval_h: int) -> QPainterPath:
    path = QPainterPath()
    path.addRoundedRect(QRectF(r.left, r.top, r.width, r.height),
                        max(min(oval_w, r.width), 0) / 2, max(min(oval_h, r.height), 0) / 2)
    return path


def arc_path(r: Rect, start: int, arc: int) -> QPainterPath:
    """A wedge of the oval in r.  QuickDraw's angles go clockwise from
    12 o'clock; Qt's anticlockwise from 3."""
    path = QPainterPath()
    box = QRectF(r.left, r.top, r.width, r.height)
    path.moveTo(box.center())
    path.arcTo(box, 90 - start, -arc)
    path.closeSubpath()
    return path


def poly_path(points: list[Point]) -> QPainterPath:
    path = QPainterPath()
    path.setFillRule(Qt.FillRule.OddEvenFill)
    if len(points) >= 2:
        path.addPolygon(QPolygonF([QPointF(pt.h, pt.v) for pt in points]))
        path.closeSubpath()
    return path


def frame_path(shape: Callable[[Rect], QPainterPath], r: Rect, pen: Point) -> QPainterPath:
    """The outline of a shape, pen.h wide and pen.v high, inside r."""
    outer = shape(r)
    inner_r = r.inset(pen.h, pen.v)
    if inner_r.empty:
        return outer
    return outer.subtracted(shape(inner_r))


def line_path(a: Point, b: Point, pen: Point) -> QPainterPath:
    """What the pen covers going from a to b: its rectangle at every point."""
    w, h = pen.h, pen.v
    path = QPainterPath()
    if w <= 0 or h <= 0:
        return path
    if a.h == b.h or a.v == b.v:
        top, left = min(a.v, b.v), min(a.h, b.h)
        path.addRect(QRectF(left, top, abs(b.h - a.h) + w, abs(b.v - a.v) + h))
        return path
    corners = [(a.h, a.v), (a.h + w, a.v), (a.h + w, a.v + h), (a.h, a.v + h),
               (b.h, b.v), (b.h + w, b.v), (b.h + w, b.v + h), (b.h, b.v + h)]
    hull = _convex_hull(corners)
    path.addPolygon(QPolygonF([QPointF(x, y) for x, y in hull]))
    path.closeSubpath()
    return path


def _convex_hull(points: list[tuple[int, int]]) -> list[tuple[int, int]]:
    pts = sorted(set(points))

    def cross(o, a, b):
        return (a[0] - o[0]) * (b[1] - o[1]) - (a[1] - o[1]) * (b[0] - o[0])

    lower: list = []
    for p in pts:
        while len(lower) >= 2 and cross(lower[-2], lower[-1], p) <= 0:
            lower.pop()
        lower.append(p)
    upper: list = []
    for p in reversed(pts):
        while len(upper) >= 2 and cross(upper[-2], upper[-1], p) <= 0:
            upper.pop()
        upper.append(p)
    return lower[:-1] + upper[:-1]


def qrect(r: Rect) -> QRect:
    return QRect(r.left, r.top, r.width, r.height)


# =========================================================================
# Patterns and transfer modes
# =========================================================================
CLEAR = 0x00000000          # leaves the pixel alone (drawn over)
KEEP = 0xFF000000           # leaves the pixel alone (XORed with)
INVERT = 0xFFFFFFFF         # inverts the pixel (XORed with)
XOR = QPainter.CompositionMode.RasterOp_SourceXorDestination
OVER = QPainter.CompositionMode.CompositionMode_SourceOver


def mode_table(mode: int, fg: int = BLACK_RGB,
               bk: int = WHITE_RGB) -> tuple[list[int], QPainter.CompositionMode]:
    """The colours a 1-bit image's 0 and 1 bits become in a transfer mode
    (the not- modes swap them), and how they're combined with what's there.
    1 bits draw in the foreground colour and 0 bits, where they draw at
    all, in the background colour; XOR ignores both."""
    zero, one = {srcCopy: (bk, fg), srcOr: (CLEAR, fg), srcXor: (KEEP, INVERT),
                 srcBic: (CLEAR, bk)}[mode & 3]
    if mode & 4:
        zero, one = one, zero
    return [zero, one], (XOR if mode & 3 == srcXor else OVER)


def port_colors(port) -> tuple[int, int]:
    """The foreground and background colours a port draws in on a 1-bit
    screen.  Of the eight classic colours (ForeColor, BackColor) only
    whiteColor has bit 0, the plane a black-and-white screen shows, clear,
    so it draws white and the rest black.  A port no colour was ever set
    in draws black on white."""
    if port is None or port is _NoPort:
        return BLACK_RGB, WHITE_RGB
    return (WHITE_RGB if port.fg_color == whiteColor else BLACK_RGB,
            BLACK_RGB if port.bk_color & 1 else WHITE_RGB)


def _mono(data: bytes, width: int, height: int, bpl: int) -> QImage:
    img = QImage(data, width, height, bpl, QImage.Format.Format_Mono)
    img.setColorTable(MONO_TABLE)
    return img.copy()           # its own copy of the bits, which data doesn't outlive


@lru_cache(maxsize=256)
def ink(pat: bytes, mode: int, fg: int = BLACK_RGB,
        bk: int = WHITE_RGB) -> tuple[QBrush, QPainter.CompositionMode]:
    """A brush that paints pat in a pattern mode, and how it combines with
    what's there."""
    rows = (bytes(pat) + bytes(8))[:8]
    img = _mono(b"".join(bytes([r, 0, 0, 0]) for r in rows), 8, 8, 4)
    table, comp = mode_table(mode, fg, bk)
    img.setColorTable(table)
    return QBrush(img.convertToFormat(QImage.Format.Format_ARGB32_Premultiplied)), comp


def source_image(src: QImage, mode: int, fg: int = BLACK_RGB,
                 bk: int = WHITE_RGB) -> tuple[QImage, QPainter.CompositionMode]:
    """src, black and white, ready to draw in a source mode (CopyBits,
    text): black is a 1 bit, white a 0."""
    mono = src.convertToFormat(QImage.Format.Format_Mono, MONO_TABLE,
                               Qt.ImageConversionFlag.ThresholdDither)
    table, comp = mode_table(mode, fg, bk)
    mono.setColorTable(table)
    return mono.convertToFormat(QImage.Format.Format_ARGB32_Premultiplied), comp


# =========================================================================
# Fonts
# =========================================================================
SERIF, SANS, MONO = "serif", "sans", "mono"
FAMILIES = {        # Mac font family -> host stand-ins
    2: SERIF, 3: SANS, 4: MONO, 5: SERIF, 6: SERIF, 7: SANS, 8: SANS, 9: SANS,
    11: SANS, 12: SANS, 13: SERIF, 20: SERIF, 21: SANS, 22: MONO, 23: SERIF, 24: SERIF,
}
HOST_FAMILIES = {
    SERIF: ["New York", "Times", "Times New Roman", "Liberation Serif", "DejaVu Serif"],
    SANS: ["Geneva", "Helvetica", "Arial", "Liberation Sans", "DejaVu Sans"],
    MONO: ["Monaco", "Menlo", "Courier New", "Liberation Mono", "DejaVu Sans Mono"],
}
HINTS = {SERIF: QFont.StyleHint.Serif, SANS: QFont.StyleHint.SansSerif,
         MONO: QFont.StyleHint.TypeWriter}
FONT_NAMES = {0: "Chicago", 1: "Geneva", 2: "New York", 3: "Geneva", 4: "Monaco", 5: "Venice",
              6: "London", 7: "Athens", 8: "San Francisco", 9: "Toronto", 11: "Cairo",
              12: "Los Angeles", 13: "Zapf Dingbats", 20: "Times", 21: "Helvetica",
              22: "Courier", 23: "Symbol", 24: "Taliesin"}
FONT_NUMBERS = {name.lower(): num for num, name in FONT_NAMES.items() if num}
FONT_NUMBERS["chicago"] = 0
# What host fonts stand in for, always: the families macrt's System has.
# Venice, Cairo and the rest are drawn from their bitmaps when a font file
# or the System brings them (Font Mover shows Cairo's pictures in it), and
# only otherwise from the nearest host font.
STANDARD_FAMILIES = {0, 1, 2, 3, 4, 20, 21, 22}

# Characters Chicago draws as symbols, which host fonts know by their Unicode
# names.  Text comes in with them as those; a bitmap font wants the codes back.
CHICAGO_SYMBOLS = str.maketrans({k.commandMark: "\u2318", k.checkMark: "\u2713",
                                 k.diamondMark: "\u25c6"})
_TO_CODES = {ord(v): chr(c) for c, v in CHICAGO_SYMBOLS.items()}


# What Chicago, Geneva and Monaco have no width for and draw nothing for,
# whatever the host font standing in for them would draw.
BLANKS = "\x00\r"


def char_codes(text: str) -> bytes:
    return text.translate(_TO_CODES).encode("mac_roman", "replace")


class Fonts:
    """Host fonts for Mac font families.  system is what stands in for
    Chicago, the system font.  measured, if set, gives the strike a family
    is measured with at a size and how much to scale it by: the one it's
    drawn from, an application's own, or the one a host font standing in
    for it is measured with (bitfont.AppFonts.widths).  app_font says
    which family family 1, the application font, is: ApFontID's."""

    def __init__(self, system: QFont):
        self.system = QFont(system)
        self._families: dict[str, str | None] = {}
        self.measured: Callable[[int, int], tuple | None] | None = None
        self.app_font: Callable[[], int] = lambda: 3        # Geneva
        self._widths: dict[tuple[int, int, int], tuple[object, float, dict[str, int]]] = {}

    def _strike(self, family: int, size: int) -> tuple[object, float]:
        found = self.measured(family, size) if self.measured is not None else None
        return found if found is not None else (None, 1.0)

    def advances(self, family: int, size: int, face: int, text: str,
                 space_extra: int = 0) -> list[int]:
        """How far the pen moves for each character of text: a whole number
        of pixels, its own width, whatever comes before or after it, with
        a pixel more for outline and shadow, and space_extra more for a
        space.  Programs that measure text a character at a time and draw
        it a piece at a time, as word processors do, need it to add up."""
        family = self.app_font() if family == 1 else family
        strike, scale = self._strike(family, size)
        key = (family, size, face)
        known = self._widths.get(key)
        if known is None or known[0] is not strike or known[1] != scale:
            known = self._widths[key] = (strike, scale, {})
        table = known[2]
        new = set(text).difference(table)
        if new:
            style = (1 if face & bold and strike is not None else 0) + (
                1 if face & (outline | shadow) else 0)
            fm = QFontMetrics(self.font(family, size, face)) if strike is None else None
            for ch in new:
                table[ch] = style + (round(strike.char_width(char_codes(ch)[0]) * scale)
                                     if strike is not None else
                                     0 if ch in BLANKS else fm.horizontalAdvance(ch))
        widths = [table[ch] for ch in text]
        if space_extra:
            widths = [w + space_extra if ch == " " else w for w, ch in zip(widths, text)]
        return widths

    def blanks(self, family: int, size: int, text: str) -> set[str]:
        """The characters of text the font draws nothing for: glyphs of no
        width, as Chicago's NUL and return are."""
        strike, _ = self._strike(family, size)
        if strike is None:
            return set(text) & set(BLANKS)
        return {ch for ch in set(text) if not strike.char_width(char_codes(ch)[0])}

    def info(self, family: int, size: int, face: int) -> tuple[int, int, int, int]:
        """ascent, descent, widMax, leading"""
        strike, scale = self._strike(family, size)
        if strike is not None:
            return tuple(round(n * scale) for n in (strike.ascent, strike.descent,
                                                    strike.wid_max, strike.leading))
        fm = QFontMetrics(self.font(family, size, face))
        return fm.ascent(), fm.descent(), fm.maxWidth(), max(fm.leading(), 0)

    def _family(self, kind: str) -> str | None:
        if kind not in self._families:
            have = set(QFontDatabase.families())
            self._families[kind] = next((f for f in HOST_FAMILIES[kind] if f in have), None)
        return self._families[kind]

    def font(self, family: int, size: int, face: int) -> QFont:
        return self._font(self.app_font() if family == 1 else family, size, face)

    @lru_cache(maxsize=128)
    def _font(self, family: int, size: int, face: int) -> QFont:
        if family == 0:
            f = QFont(self.system)
            f.setPixelSize(size or 12)
        else:
            kind = FAMILIES.get(family, SANS)
            name = self._family(kind)
            f = QFont(name) if name else QFont()
            f.setStyleHint(HINTS[kind])
            f.setPixelSize(size or 12)
            f.setBold(bool(face & bold))
        if family == 0 and face & bold:
            f.setBold(True)
        f.setItalic(bool(face & italic))
        f.setUnderline(bool(face & underline))
        if face & condense:
            f.setStretch(QFont.Stretch.SemiCondensed.value)
        elif face & extend:
            f.setStretch(QFont.Stretch.SemiExpanded.value)
        f.setStyleStrategy(QFont.StyleStrategy.NoAntialias)
        f.setKerning(False)
        return f

    def for_port(self, port: GrafPort) -> QFont:
        return self.font(port.tx_font, port.tx_size, port.tx_face)


# =========================================================================
# Surfaces: where a port's pixels are
# =========================================================================
class WindowSurface:
    """A window's Backing, whose area is relative to the top left of the
    port's portRect."""

    def __init__(self, port: GrafPort):
        self.port = port
        self.backing = port.backing

    def _origin(self) -> tuple[int, int]:
        r, a = self.port.port_rect, self.backing.area
        return r.left + a.left, r.top + a.top

    @contextmanager
    def painter(self, area: Rect | None = None):
        with self.backing.lock:
            p = QPainter(self.backing.image)
            h, v = self._origin()
            p.translate(-h, -v)
            try:
                yield p
            finally:
                p.end()

    def image(self, r: Rect) -> QImage:
        """The pixels in r (local), white where there are none."""
        h, v = self._origin()
        out = QImage(max(r.width, 1), max(r.height, 1), QImage.Format.Format_RGB32)
        out.fill(QColor(255, 255, 255))
        with self.backing.lock:
            p = QPainter(out)
            p.drawImage(QPoint(0, 0), self.backing.image,
                        QRect(r.left - h, r.top - v, r.width, r.height))
            p.end()
        return out

    def mono(self, r: Rect, need: Rect) -> QImage:
        """The pixels in r (local) as a 1-bit image, black 1 as on a Mac.
        Those of need, inside r, are as image() has them; the rest may be
        anything, if they're beyond the Backing."""
        h, v = self._origin()
        with self.backing.lock:
            img = self.backing.image
            part = None
            if img.rect().contains(QRect(need.left - h, need.top - v, need.width, need.height)):
                part = img.copy(QRect(r.left - h, r.top - v, r.width, r.height))
        if part is None:
            part = self.image(r)
        return part.convertToFormat(QImage.Format.Format_Mono, MONO_TABLE,
                                    Qt.ImageConversionFlag.ThresholdDither)

    def pixel(self, pt: Point) -> bool:
        h, v = self._origin()
        with self.backing.lock:
            img = self.backing.image
            x, y = pt.h - h, pt.v - v
            if 0 <= x < img.width() and 0 <= y < img.height():
                return (img.pixel(x, y) & 0xFFFFFF) == 0
        return False


class MemorySurface:
    """A 1-bit BitMap in guest memory, read into an image to draw on and
    written back after.  Only the rows a call touches are read."""

    def __init__(self, mem: RAM, base: int, row_bytes: int, bounds: Rect):
        self.mem = mem
        self.base = base
        self.row_bytes = row_bytes
        self.bounds = bounds

    @classmethod
    def make(cls, mem: RAM, base: int, row_bytes: int, bounds: Rect) -> MemorySurface | None:
        """None if it isn't a BitMap QuickDraw can draw in: a PixMap (the
        top bit of rowBytes), or one that doesn't fit in memory."""
        base &= ADDRESS_MASK
        if (row_bytes & 0x8000 or row_bytes <= 0 or bounds.empty
                or bounds.height > MAX_BITMAP_ROWS or bounds.width > row_bytes * 8
                or not base or base + row_bytes * bounds.height > mem.size):
            return None
        return cls(mem, base, row_bytes, bounds)

    def _rows(self, area: Rect | None) -> tuple[int, int]:
        b = self.bounds
        if area is None:
            return b.top, b.bottom
        return max(b.top, area.top), min(b.bottom, area.bottom)

    def _decode(self, top: int, bottom: int) -> QImage:
        rb = self.row_bytes
        data = self.mem.read(self.base + (top - self.bounds.top) * rb, rb * (bottom - top))
        return _mono(data, rb * 8, bottom - top, rb).convertToFormat(QImage.Format.Format_RGB32)

    def _encode(self, img: QImage, top: int) -> None:
        mono = img.convertToFormat(QImage.Format.Format_Mono, MONO_TABLE,
                                   Qt.ImageConversionFlag.ThresholdDither)
        bpl = mono.bytesPerLine()
        ptr = mono.constBits()
        ptr.setsize(mono.sizeInBytes())
        raw = bytes(ptr)
        rb = self.row_bytes
        rows = b"".join(raw[y * bpl:y * bpl + rb] for y in range(mono.height()))
        self.mem.write(self.base + (top - self.bounds.top) * rb, rows)

    def put_mono(self, mono: QImage, left: int, r: Region) -> None:
        """Write mono's pixels where r is, mono's top left at r's top and
        left, which is on a byte of the bitmap.  r is inside bounds.  The
        rows are written whole, with what's beside r as it was: one write,
        with no painting to do, as a call that draws a little is drawn."""
        b, rb = self.bounds, self.row_bytes
        top, bottom = r.bands[0][0], r.bands[-1][1]
        first = (left - b.left) >> 3
        nb = min((mono.width() + 7) >> 3, rb - first)
        bpl = mono.bytesPerLine()
        ptr = mono.constBits()
        ptr.setsize(mono.sizeInBytes())
        raw = bytes(ptr)
        pre, post = bytes(first), bytes(rb - first - nb)
        rows = bottom - top
        src = b"".join([pre + raw[y * bpl:y * bpl + nb] + post for y in range(rows)])
        bits, y, masks = nb * 8, top, []
        for t, bt, xs in r.bands:
            if t > y:
                masks.append(bytes(rb * (t - y)))
            m = 0
            for i in range(0, len(xs), 2):
                x0, x1 = xs[i] - left, xs[i + 1] - left
                m |= ((1 << (x1 - x0)) - 1) << (bits - x1)
            masks.append((pre + m.to_bytes(nb, "big") + post) * (bt - t))
            y = bt
        mask = int.from_bytes(b"".join(masks), "big")
        at = self.base + (top - b.top) * rb
        old = int.from_bytes(self.mem.read(at, rows * rb), "big")
        new = old & ~mask | int.from_bytes(src, "big") & mask
        self.mem.write(at, new.to_bytes(rows * rb, "big"))

    @contextmanager
    def painter(self, area: Rect | None = None):
        top, bottom = self._rows(area)
        if bottom <= top:
            yield None
            return
        img = self._decode(top, bottom)
        p = QPainter(img)
        p.translate(-self.bounds.left, -top)
        try:
            yield p
        finally:
            p.end()
            self._encode(img, top)

    def image(self, r: Rect) -> QImage:
        top, bottom = self._rows(r)
        out = QImage(max(r.width, 1), max(r.height, 1), QImage.Format.Format_RGB32)
        out.fill(QColor(255, 255, 255))
        if bottom > top:
            p = QPainter(out)
            p.drawImage(QPoint(0, top - r.top), self._decode(top, bottom),
                        QRect(r.left - self.bounds.left, 0, r.width, bottom - top))
            p.end()
        return out

    def pixel(self, pt: Point) -> bool:
        b = self.bounds
        if not b.contains(pt):
            return False
        x, y = pt.h - b.left, pt.v - b.top
        return bool(self.mem.r8(self.base + y * self.row_bytes + (x >> 3)) & (0x80 >> (x & 7)))


class ImageSurface:
    """A host image a port draws in, its top left at the top left of
    bounds: a page being printed."""

    def __init__(self, img: QImage, bounds: Rect):
        self.img = img
        self.bounds = bounds

    @contextmanager
    def painter(self, area: Rect | None = None):
        p = QPainter(self.img)
        p.translate(-self.bounds.left, -self.bounds.top)
        try:
            yield p
        finally:
            p.end()

    def image(self, r: Rect) -> QImage:
        out = QImage(max(r.width, 1), max(r.height, 1), QImage.Format.Format_RGB32)
        out.fill(QColor(255, 255, 255))
        p = QPainter(out)
        p.drawImage(QPoint(0, 0), self.img, QRect(r.left - self.bounds.left,
                                                  r.top - self.bounds.top, r.width, r.height))
        p.end()
        return out

    def pixel(self, pt: Point) -> bool:
        x, y = pt.h - self.bounds.left, pt.v - self.bounds.top
        if 0 <= x < self.img.width() and 0 <= y < self.img.height():
            return (self.img.pixel(x, y) & 0xFFFFFF) == 0
        return False


# =========================================================================
# QuickDraw
# =========================================================================
class QuickDraw:
    """The drawing calls for one process.  Coordinates are local to the
    port drawn in.  repaint(window) is called, on the guest's thread, when
    a window's pixels have changed."""

    def __init__(self, system: System, fonts: Fonts, repaint: Callable[[GrafPort], None]):
        self.sys = system
        self.mem = system.mem
        self.fonts = fonts
        self.repaint = repaint
        self.app_fonts: AppFonts | None = None      # the process's own bitmap fonts
        self.polys: dict[int, list] = {}            # port -> [PolyHandle, points]
        self.rgns: dict[int, list] = {}             # port -> [Region so far, lines]
        self.pictures: dict[int, object] = {}       # port -> the pict.Recorder it's recording
        self.surfaces: dict[int, ImageSurface] = {}  # port -> the host image it draws in

    # -- where the pixels go -------------------------------------------------
    def surface(self, port: GrafPort):
        """A window draws in its Backing while its portBits are the screen;
        anything else (SetPortBits to an offscreen BitMap) draws there."""
        own = self.surfaces.get(port.addr)
        if own is not None:
            return own
        base = port.port_bits_base & ADDRESS_MASK
        if port.backing is not None and base == self.sys.screen_bits.base:
            self.sys.screen.look()      # what the program wrote in the screen buffer first
            return WindowSurface(port)
        return MemorySurface.make(self.mem, base, port.port_bits_row_bytes,
                                  port.port_bits_bounds)

    def clip_region(self, port: GrafPort) -> Region:
        clip = Region.rect(port.port_rect)
        for h in (port.vis_rgn, port.clip_rgn):
            if h:
                clip = clip.sect(read_rgn(self.mem, h))
        return clip

    @contextmanager
    def _on(self, port: GrafPort, surface, area: Rect | None, clip: Region | None):
        """A painter on surface, clipped to clip, with patterns lined up
        with port's bitmap as QuickDraw lines them up."""
        if surface is None or (clip is not None and clip.empty):
            yield None
            return
        if clip is not None and area is not None:
            box = clip.bbox
            area = Rect(max(area.top, box.top), max(area.left, box.left),
                        min(area.bottom, box.bottom), min(area.right, box.right))
            if area.empty:
                yield None
                return
        with surface.painter(area) as p:
            if p is not None:
                p.setRenderHint(QPainter.RenderHint.Antialiasing, False)
                if clip is not None:
                    if clip.is_rect:
                        p.setClipRect(qrect(clip.bbox))
                    else:
                        p.setClipRegion(clip.to_qregion())
                b = port.port_bits_bounds
                p.setBrushOrigin(b.left, b.top)
            yield p
        if isinstance(surface, WindowSurface):
            self.sys.screen.drawn(port, area, clip)
            self.repaint(port)

    def painting(self, port: GrafPort | None, area: Rect | None = None,
                 mask: Region | None = None):
        """A QPainter on port's pixels, clipped as QuickDraw clips (to its
        portRect, visRgn and clipRgn, and mask); None if there's nothing to
        draw on."""
        if port is None:
            return self._on(port, None, area, None)
        clip = self.clip_region(port)
        if mask is not None:
            clip = clip.sect(mask)
        return self._on(port, self.surface(port), area, clip)

    def fill(self, port: GrafPort | None, path: QPainterPath, pat: bytes, mode: int) -> None:
        if port is None or path.isEmpty():
            return
        box = path.boundingRect().toAlignedRect()
        area = Rect(box.top(), box.left(), box.bottom() + 1, box.right() + 1)
        brush, comp = ink(bytes(pat), mode, *port_colors(port))
        with self.painting(port, area) as p:
            if p is not None:
                p.setCompositionMode(comp)
                p.fillPath(path, brush)

    def _picture(self, port: GrafPort, call: str, *args) -> bool:
        """Record a drawing call in the picture port is recording, if it
        is; True if the call shouldn't draw as well: OpenPicture hides the
        pen, and while it's hidden nothing draws.  A program can stop the
        recording for a while by clearing picSave."""
        rec = self.pictures.get(port.addr)
        if rec is None or not port.pic_save:
            return False
        getattr(rec, call)(port, *args)
        return port.pn_vis < 0

    # -- the pen -------------------------------------------------------------
    def move_to(self, port: GrafPort, h: int, v: int) -> None:
        port.pn_loc = Point(v, h)
        rec = self.rgns.get(port.addr)
        if rec is not None:
            rec[1].append([Point(v, h)])

    def line_to(self, port: GrafPort, h: int, v: int) -> None:
        start, end = port.pn_loc, Point(v, h)
        port.pn_loc = end
        if (self._record_line(port, start, end) or self._picture(port, "line", start, end)
                or port.pn_vis < 0):
            return
        self.fill(port, line_path(start, end, port.pn_size), port.pn_pat, port.pn_mode)

    # -- shapes --------------------------------------------------------------
    def shape(self, port: GrafPort | None, verb: str, path_of: Callable[[Rect], QPainterPath],
              r: Rect, pat: bytes | None = None) -> None:
        """Frame, Paint, Erase, Invert or Fill a shape; path_of makes the
        shape's path for a rectangle.  QuickDraw draws nothing in an empty
        rectangle, as MacGammon's, bottom above top, is; Qt would draw the
        one its corners make."""
        if port is None or (verb == "Frame" and not r.empty
                            and self._record_shape(port, path_of(r))):
            return
        if self._picture(port, "shape", verb, path_of, r, pat) or r.empty:
            return
        if verb == "Frame":
            if port.pn_vis >= 0:
                self.fill(port, frame_path(path_of, r, port.pn_size), port.pn_pat, port.pn_mode)
        elif verb == "Paint":
            if port.pn_vis >= 0:
                self.fill(port, path_of(r), port.pn_pat, port.pn_mode)
        elif verb == "Erase":
            self.fill(port, path_of(r), port.bk_pat, patCopy)
        elif verb == "Invert":
            self.fill(port, path_of(r), BLACK_PAT, patXor)
        elif verb == "Fill":
            self.fill(port, path_of(r), pat if pat is not None else BLACK_PAT, patCopy)

    def region(self, port: GrafPort | None, verb: str, rgn: Region,
               pat: bytes | None = None) -> None:
        """Frame, Paint, Erase, Invert or Fill a region."""
        if port is None or rgn.empty:
            return
        if verb == "Frame":
            rec = self.rgns.get(port.addr)
            if rec is not None:
                rec[0] = rec[0].xor(rgn)
                return
        if not self._picture(port, "region", verb, rgn, pat):
            self._paint_region(port, verb, rgn, pat)

    def poly(self, port: GrafPort | None, verb: str, points: list[Point],
             pat: bytes | None = None) -> None:
        """Paint, Erase, Invert or Fill a polygon (frame_poly frames one)."""
        if port is None or self._picture(port, "poly", verb, points, pat):
            return
        rgn = Region.from_path(poly_path(points))
        if not rgn.empty:
            self._paint_region(port, verb, rgn, pat)

    def _paint_region(self, port: GrafPort, verb: str, rgn: Region, pat: bytes | None) -> None:
        if verb == "Frame":
            pen = port.pn_size
            rgn = rgn.diff(rgn.inset(pen.h, pen.v))
        if verb in ("Frame", "Paint"):
            if port.pn_vis < 0:
                return
            pat, mode = port.pn_pat, port.pn_mode
        elif verb == "Erase":
            pat, mode = port.bk_pat, patCopy
        elif verb == "Invert":
            pat, mode = BLACK_PAT, patXor
        else:
            pat, mode = (pat if pat is not None else BLACK_PAT), patCopy
        brush, comp = ink(bytes(pat), mode, *port_colors(port))
        with self.painting(port, rgn.bbox, rgn) as p:
            if p is not None:
                p.setCompositionMode(comp)
                p.fillRect(qrect(rgn.bbox), brush)

    # -- recording regions and polygons ------------------------------------
    def open_rgn(self, port: GrafPort) -> None:
        self.rgns[port.addr] = [Region(), [[port.pn_loc]]]
        port.pn_vis = port.pn_vis - 1

    def close_rgn(self, port: GrafPort) -> Region:
        """What was drawn since OpenRgn: the shapes' outlines and closed
        loops of lines, each XORed in, as QuickDraw records them."""
        rec = self.rgns.pop(port.addr, None)
        if rec is None:
            return Region()
        port.pn_vis = port.pn_vis + 1
        rgn = rec[0]
        for line in rec[1]:
            if len(line) >= 3:
                rgn = rgn.xor(Region.from_path(poly_path(line)))
        return rgn

    def _record_shape(self, port: GrafPort, path: QPainterPath) -> bool:
        rec = self.rgns.get(port.addr)
        if rec is None:
            return False
        rec[0] = rec[0].xor(Region.from_path(path))
        return True

    def _record_line(self, port: GrafPort, a: Point, b: Point) -> bool:
        recorded = False
        poly = self.polys.get(port.addr)
        if poly is not None:
            if not poly[1]:
                poly[1].append(a)
            poly[1].append(b)
            recorded = True
        rec = self.rgns.get(port.addr)
        if rec is not None:
            if not rec[1] or rec[1][-1][-1] != a:
                rec[1].append([a])
            rec[1][-1].append(b)
            recorded = True
        return recorded

    def open_poly(self, port: GrafPort, h: int) -> None:
        self.polys[port.addr] = [h, []]
        port.pn_vis = port.pn_vis - 1

    def close_poly(self, port: GrafPort) -> None:
        rec = self.polys.pop(port.addr, None)
        if rec is not None:
            port.pn_vis = port.pn_vis + 1
            write_poly(self.sys.heap_of(rec[0]), rec[0], rec[1])

    def frame_poly(self, port: GrafPort | None, points: list[Point]) -> None:
        """The lines from point to point; closed only if the last point is
        the first."""
        if port is None or len(points) < 2:
            return
        port.pn_loc = points[-1]
        if self._picture(port, "poly", "Frame", points, None) or port.pn_vis < 0:
            return
        path = QPainterPath()
        path.setFillRule(Qt.FillRule.WindingFill)
        for a, b in zip(points, points[1:]):
            path.addPath(line_path(a, b, port.pn_size))
        self.fill(port, path, port.pn_pat, port.pn_mode)

    # -- text ----------------------------------------------------------------
    def font(self, port: GrafPort | None) -> QFont:
        return self.fonts.for_port(port) if port is not None else self.fonts.font(0, 12, 0)

    def strike(self, port: GrafPort | None):
        """The bitmap font the port's text is drawn in, and its scale, if
        it's a family of the application's own rather than a standard one,
        or a standard one it has its own strike of (AppFonts.source)."""
        if port is None or self.app_fonts is None:
            return None
        return self.app_fonts.find(port.tx_font, port.tx_size)

    def _advances(self, port: GrafPort | None, text: str) -> list[int]:
        """fonts.advances in the port's host font."""
        if port is None:
            return self.fonts.advances(0, 12, 0, text)
        return self.fonts.advances(port.tx_font, port.tx_size, port.tx_face, text,
                                   port.sp_extra >> 16)

    def text_width(self, port: GrafPort | None, text: str) -> int:
        found = self.strike(port)
        if found is not None:
            strike, scale = found
            w = strike.width(char_codes(text), port.tx_face, port.sp_extra >> 16)
            return round(w * scale)
        return sum(self._advances(port, text))

    def text_positions(self, port: GrafPort | None, text: str) -> list[int]:
        """MeasureText: where the pen is after each character of text, from
        0 before the first."""
        return list(accumulate(self._advances(port, text), initial=0))

    def host_widths(self, family: int, size: int, face: int, text: str) -> list[int]:
        """How far the pen moves for each character of text in the host font
        standing in for a standard family, as text_width adds them up."""
        return self.fonts.advances(family, size, face, text)

    def host_font_info(self, family: int, size: int, face: int) -> tuple[int, int, int, int]:
        return self.fonts.info(family, size, face)

    def font_info(self, port: GrafPort | None) -> tuple[int, int, int, int]:
        """ascent, descent, widMax, leading"""
        found = self.strike(port)
        if found is not None:
            st, scale = found
            return tuple(round(n * scale) for n in (st.ascent, st.descent, st.wid_max, st.leading))
        if port is None:
            return self.fonts.info(0, 12, 0)
        return self.fonts.info(port.tx_font, port.tx_size, port.tx_face)

    def draw_text(self, port: GrafPort | None, text: str, mask: Region | None = None) -> None:
        """Draw text at the pen in the port's font, style and text mode, and
        move the pen past it.  mask clips it further (TextEdit's viewRect)."""
        if port is None or not text:
            return
        found = self.strike(port)
        if found is not None:
            self._draw_strike(port, text, *found, mask)
            return
        font = self.font(port)
        fm = QFontMetrics(font)
        pen = port.pn_loc
        advances = self._advances(port, text)
        width = sum(advances)
        hidden = self._picture(port, "text", text, lambda s: self.text_width(port, s))
        port.pn_loc = pen.offset(width, 0)
        if hidden:
            return
        face = port.tx_face
        extra = 2 if face & (outline | shadow) else 0
        area = Rect(pen.v - fm.ascent() - 1, pen.h - 1, pen.v + fm.descent() + extra + 1,
                    pen.h + width + extra + 1)
        # The text black on white, then drawn in the text mode.
        img = QImage(max(area.width, 1), max(area.height, 1), QImage.Format.Format_RGB32)
        img.fill(QColor(255, 255, 255))
        p = QPainter(img)
        p.setFont(font)
        p.setPen(QColor(0, 0, 0))
        x, y = pen.h - area.left, pen.v - area.top
        # Where the font has nothing to draw, the host font would draw a box.
        blanks = self.fonts.blanks(port.tx_font, port.tx_size, text)
        drawn = [(px, ch) for px, ch in zip(accumulate(advances, initial=0), text)
                 if ch not in blanks]
        if face & (outline | shadow):
            for dx, dy in style_offsets(bool(face & shadow)):
                for px, ch in drawn:
                    p.drawText(x + px + dx, y + dy, ch)
            p.setPen(QColor(255, 255, 255))
        for px, ch in drawn:
            p.drawText(x + px, y, ch)
        p.end()
        src, comp = source_image(img, port.tx_mode, *port_colors(port))
        with self.painting(port, area, mask) as dst:
            if dst is not None:
                dst.setCompositionMode(comp)
                dst.drawImage(QPoint(area.left, area.top), src)

    def _draw_strike(self, port: GrafPort, text: str, strike, scale: float,
                     mask: Region | None) -> None:
        """draw_text in a bitmap font."""
        pen = port.pn_loc
        width = self.text_width(port, text)
        hidden = self._picture(port, "text", text, lambda s: self.text_width(port, s))
        port.pn_loc = pen.offset(width, 0)
        if hidden:
            return
        glyphs, left, baseline = strike.render(char_codes(text), port.tx_face,
                                               port.sp_extra >> 16)
        if scale != 1.0:
            glyphs = glyphs.scaled(max(round(glyphs.width() * scale), 1),
                                   max(round(glyphs.height() * scale), 1),
                                   Qt.AspectRatioMode.IgnoreAspectRatio,
                                   Qt.TransformationMode.FastTransformation)
            left, baseline = round(left * scale), round(baseline * scale)
        area = Rect(pen.v - baseline, pen.h - left, pen.v - baseline + glyphs.height(),
                    pen.h - left + glyphs.width())
        # Black on white, then drawn in the text mode, as a host font is.
        img = QImage(glyphs.size(), QImage.Format.Format_RGB32)
        img.fill(QColor(255, 255, 255))
        p = QPainter(img)
        p.drawImage(QPoint(0, 0), glyphs)
        p.end()
        src, comp = source_image(img, port.tx_mode, *port_colors(port))
        with self.painting(port, area, mask) as dst:
            if dst is not None:
                dst.setCompositionMode(comp)
                dst.drawImage(QPoint(area.left, area.top), src)

    def text_box(self, port: GrafPort | None, text: str, r: Rect, just: int) -> None:
        """TETextBox: erase r and draw the text in it, wrapped at words."""
        if port is None:
            return
        self.fill(port, rect_path(r), port.bk_pat, patCopy)
        align = {0: Qt.AlignmentFlag.AlignLeft, 1: Qt.AlignmentFlag.AlignHCenter,
                 -1: Qt.AlignmentFlag.AlignRight}.get(just, Qt.AlignmentFlag.AlignLeft)
        with self.painting(port, r) as p:
            if p is not None:
                p.setFont(self.font(port))
                p.setPen(QColor(0, 0, 0))
                p.drawText(qrect(r), align | Qt.TextFlag.TextWordWrap, text.replace("\r", "\n"))

    # -- bits ----------------------------------------------------------------
    def bitmap(self, bm: int) -> tuple[object, GrafPort | None]:
        """The surface a BitMap pointer is, and the port whose portBits it
        is, if it's one's."""
        port = self.sys.port_at(bm - 2)
        if port is not None:
            return self.surface(port), port
        return MemorySurface.make(self.mem, self.mem.r32(bm), self.mem.r16(bm + 4),
                                  self.mem.r_rect(bm + 6)), None

    def copy_bits(self, src_bm: int, dst_bm: int, src_r: Rect, dst_r: Rect, mode: int,
                  mask: Region | None, current: GrafPort | None) -> None:
        """CopyBits: src_r of one bitmap to dst_r of another, scaled to fit.
        Clipped to the current port's regions when drawing in its bits."""
        if src_r.empty or dst_r.empty:
            return
        src, _ = self.bitmap(src_bm)
        if src is not None:
            self.draw_bits(dst_bm, dst_r, src.image(src_r), mode, mask, current)

    def draw_bits(self, dst_bm: int, dst_r: Rect, img: QImage, mode: int, mask: Region | None,
                  current: GrafPort | None) -> None:
        """Draw an image into dst_r of a BitMap as CopyBits draws bits:
        black and white, scaled, in a source mode and the current port's
        colours, clipped to the port's regions when they're its bits."""
        if dst_r.empty:
            return
        dst, dst_port = self.bitmap(dst_bm)
        on_port = dst_port is not None and dst_port is current
        if on_port and self._picture(dst_port, "bits", img, dst_r, mode, mask):
            return
        if dst is None:
            return
        img, comp = source_image(img, mode, *port_colors(current))
        if on_port:
            ctx = self.painting(dst_port, dst_r, mask)
        else:
            ctx = self._on(dst_port or current or _NoPort, dst, dst_r, mask)
        with ctx as p:
            if p is not None:
                p.setCompositionMode(comp)
                p.drawImage(qrect(dst_r), img)

    def scroll_rect(self, port: GrafPort | None, r: Rect, dh: int, dv: int) -> Region:
        """ScrollRect: move the bits in r (clipped to the port) by dh, dv,
        erase what's left behind, and return that."""
        if port is None:
            return Region()
        surface = self.surface(port)
        clip = self.clip_region(port).sect(Region.rect(r))
        if surface is None or clip.empty:
            return Region()
        box = clip.bbox
        img = surface.image(box)
        moved = clip.offset(dh, dv).sect(clip)
        vacated = clip.diff(moved)
        with self._on(port, surface, box, moved) as p:
            if p is not None:
                p.drawImage(QPoint(box.left + dh, box.top + dv), img)
        brush, _ = ink(bytes(port.bk_pat), patCopy, *port_colors(port))
        with self._on(port, surface, box, vacated) as p:
            if p is not None:
                p.fillRect(qrect(box), brush)
        return vacated

    def get_pixel(self, port: GrafPort | None, pt: Point) -> bool:
        surface = self.surface(port) if port is not None else None
        return surface.pixel(pt) if surface is not None else False


class _NoPort:
    """For CopyBits into a bare BitMap: patterns line up at its (0, 0)."""
    port_bits_bounds = Rect(0, 0, 0, 0)


# =========================================================================
# Polygons: polySize, polyBBox, then the points
# =========================================================================
def read_poly(mem: RAM, h: int) -> list[Point]:
    p = mem.r32(h) & ADDRESS_MASK if h else 0
    if not p:
        return []
    size = mem.r16(p)
    n = max(size - 10, 0) // 4
    return [mem.r_point(p + 10 + 4 * i) for i in range(n)]


def write_poly(heap: Heap, h: int, points: list[Point]) -> None:
    size = 10 + 4 * len(points)
    if heap.handle_size(h) != size and not heap.set_handle_size(h, size):
        return
    p = heap.deref(h)
    mem = heap.mem
    mem.w16(p, size)
    if points:
        bbox = Rect(min(pt.v for pt in points), min(pt.h for pt in points),
                    max(pt.v for pt in points), max(pt.h for pt in points))
    else:
        bbox = EMPTY
    mem.w_rect(p + 2, bbox)
    for i, pt in enumerate(points):
        mem.w_point(p + 10 + 4 * i, pt)
