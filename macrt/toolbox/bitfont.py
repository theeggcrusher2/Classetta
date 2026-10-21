"""
Bitmap fonts: the FONT and NFNT resources an application brings with it.

macrt draws the standard families (Chicago, Geneva, New York and the rest)
with host fonts, measured with the widths of a real System file's strike
of them where there's one. A family of an application's own is another matter: its
characters can be anything, and often aren't letters at all. MacPaint
draws its tool palette in one, and games keep their sprites and score
digits in them. Those are drawn from the font's strike, as QuickDraw drew
them (Inside Macintosh I, "The Font Manager", and IV, "Font Records").

A FontRec is a header, then the strike, one bitmap holding every glyph
side by side, then two tables with an entry per character from firstChar
to lastChar, plus the missing-character glyph and one more:

    locTable    where each glyph starts in the strike, in pixels
    owTable     offset (high byte) and width (low byte); -1 for a
                character the font hasn't got

A glyph is drawn at pen.h + kernMax + offset, and the pen moves on by its
width.
"""

from __future__ import annotations

import struct
from collections.abc import Callable
from dataclasses import dataclass

from PyQt6.QtCore import QPoint, QRect, Qt
from PyQt6.QtGui import QColor, QImage, QPainter

bold, italic, underline, outline, shadow = (1 << i for i in range(5))     # as in qdraw

HEADER = 26
MISSING = 0xFFFF


@dataclass
class Strike:
    first: int
    last: int
    wid_max: int
    kern_max: int
    ascent: int
    descent: int
    leading: int
    image: QImage           # the strike: black glyphs on a transparent ground
    locs: list[int]
    ows: list[int]

    @classmethod
    def parse(cls, data: bytes) -> Strike | None:
        """None for anything that isn't a FontRec, or is one cut short."""
        if len(data) < HEADER:
            return None
        (_kind, first, last, wid_max, kern_max, n_descent, _rect_width, height, ow_loc,
         ascent, descent, leading, row_words) = struct.unpack_from(">HHHhhhhhHhhhH", data)
        if not 0 <= first <= last <= 255 or height <= 0 or row_words <= 0 or height > 255:
            return None
        n = last - first + 3
        strike = row_words * 2 * height
        if len(data) < HEADER + strike + 2 * n:
            return None
        locs = list(struct.unpack_from(f">{n}H", data, HEADER + strike))
        # owTLoc is in words from itself; an NFNT keeps its high word in nDescent.
        at = 16 + 2 * (ow_loc | (n_descent << 16 if n_descent > 0 else 0))
        if not HEADER + strike + 2 * n <= at <= len(data) - 2 * (n - 1):
            at = HEADER + strike + 2 * n
        # Many fonts stop at the missing glyph's entry, without the final -1.
        count = min(n, (len(data) - at) // 2)
        if count < n - 1:
            return None
        ows = list(struct.unpack_from(f">{count}H", data, at)) + [MISSING] * (n - count)
        bits = QImage(data[HEADER:HEADER + strike], row_words * 16, height, row_words * 2,
                      QImage.Format.Format_Mono)
        bits.setColorTable([QColor(0, 0, 0, 0).rgba(), QColor(0, 0, 0).rgba()])
        image = bits.convertToFormat(QImage.Format.Format_ARGB32)
        return cls(first, last, wid_max, kern_max, ascent, descent, max(leading, 0), image,
                   locs, ows)

    def _glyph(self, code: int) -> int:
        """Which glyph a character code draws: its own, or the missing one."""
        if self.first <= code <= self.last and self.ows[code - self.first] != MISSING:
            return code - self.first
        return self.last - self.first + 1

    def char_width(self, code: int) -> int:
        ow = self.ows[self._glyph(code)]
        return 0 if ow == MISSING else ow & 0xFF

    def width(self, codes: bytes, face: int = 0, space_extra: int = 0) -> int:
        extra = (1 if face & bold else 0) + (1 if face & (outline | shadow) else 0)
        return sum(self.char_width(c) + extra for c in codes) + codes.count(32) * space_extra

    def render(self, codes: bytes, face: int = 0,
               space_extra: int = 0) -> tuple[QImage, int, int]:
        """The text, black on a transparent ground, and where the pen and
        the baseline are in it: pixels from its left and from its top.  It
        covers the glyphs' rows, ascent above the baseline to descent below,
        and from the leftmost pixel drawn to the rightmost, or to the pen's
        end if that's further; the style's extra pixels widen it."""
        extra = (1 if face & bold else 0) + (1 if face & (outline | shadow) else 0)
        hollow = face & (outline | shadow)
        placed, x = [], 0
        lo, hi = 0, 0
        for c in codes:
            g = self._glyph(c)
            ow = self.ows[g]
            if ow != MISSING:
                left, right = self.locs[g], self.locs[g + 1]
                gx = x + self.kern_max + (ow >> 8)
                if right > left:
                    placed.append((gx, left, right))
                    lo, hi = min(lo, gx), max(hi, gx + right - left + (1 if face & bold else 0))
                x += (ow & 0xFF) + extra
            if c == 32:
                x += space_extra
        hi = max(hi, x)
        slant = (self.ascent + 1) // 2 if face & italic else 0
        lo -= 1 if hollow else 0
        hi += (2 if face & shadow else 1 if hollow else 0) + slant
        top = 1 if hollow else 0
        below = max(self.descent, 2 if face & underline else 0) + (
            2 if face & shadow else 1 if hollow else 0)
        out = QImage(max(hi - lo, 1), top + self.ascent + below, QImage.Format.Format_ARGB32)
        out.fill(Qt.GlobalColor.transparent)
        height = self.ascent + self.descent
        p = QPainter(out)
        for gx, left, right in placed:
            at = QPoint(gx - lo, top)
            src = QRect(left, 0, right - left, height)
            p.drawImage(at, self.image, src)
            if face & bold:
                p.drawImage(at + QPoint(1, 0), self.image, src)
        p.end()
        baseline = top + self.ascent
        if slant:
            out = _slanted(out, baseline)
        if hollow:
            out = _hollow(out, bool(face & shadow))
        if face & underline:
            p = QPainter(out)
            p.fillRect(-lo, baseline + 1, x, 1, QColor(0, 0, 0))
            p.end()
        return out, -lo, baseline


def _slanted(img: QImage, baseline: int) -> QImage:
    """Italic, as QuickDraw slants a bitmap font: a row moves right one
    pixel for every two it is above the baseline."""
    out = QImage(img.size(), QImage.Format.Format_ARGB32)
    out.fill(Qt.GlobalColor.transparent)
    p = QPainter(out)
    for y in range(img.height()):
        p.drawImage(QPoint(max(baseline - y, 0) // 2, y), img, QRect(0, y, img.width(), 1))
    p.end()
    return out


def style_offsets(shadowed: bool) -> list[tuple[int, int]]:
    """Where QuickDraw ORs a glyph's copies to outline it: every pixel
    round it, and for shadow, one further down and to the right; the
    glyph itself is then taken out."""
    reach = range(-1, 3 if shadowed else 2)
    return [(dx, dy) for dy in reach for dx in reach if (dx, dy) != (0, 0)]


def _hollow(img: QImage, shadowed: bool) -> QImage:
    """Outline: the glyphs' edges, one pixel out, with the glyphs
    themselves left out; shadow thickens the edge down and to the right."""
    out = QImage(img.size(), QImage.Format.Format_ARGB32)
    out.fill(Qt.GlobalColor.transparent)
    p = QPainter(out)
    for dx, dy in style_offsets(shadowed):
        p.drawImage(QPoint(dx, dy), img)
    p.setCompositionMode(QPainter.CompositionMode.CompositionMode_DestinationOut)
    p.drawImage(QPoint(0, 0), img)
    p.end()
    return out


def font_id(family: int, size: int = 0) -> int | None:
    """The ID of the FONT of the family at the size, family * 128 + size
    in a word: families 256 to 511 have IDs below 0, as a desk
    accessory's own fonts do.  None for a family past them."""
    if not 0 <= family < 512:
        return None
    rid = family << 7 | size
    return rid - 0x10000 if rid >= 0x8000 else rid


def font_family(rid: int) -> int:
    """The family of a FONT's ID."""
    return (rid & 0xFFFF) >> 7


def font_ids(fond: bytes, size: int) -> list[int]:
    """The resource IDs a FOND's font association table gives for the
    size, plain style first."""
    if len(fond) < 54:
        return []
    n = struct.unpack_from(">h", fond, 52)[0] + 1
    entries = [struct.unpack_from(">hhh", fond, 54 + 6 * i)
               for i in range(max(min(n, (len(fond) - 54) // 6), 0))]
    return [rid for s, style, rid in sorted(entries, key=lambda e: e[1] != 0)
            if s == size and style == 0]


def fond_sizes(fond: bytes) -> set[int]:
    if len(fond) < 54:
        return set()
    n = struct.unpack_from(">h", fond, 52)[0] + 1
    return {struct.unpack_from(">h", fond, 54 + 6 * i)[0]
            for i in range(max(min(n, (len(fond) - 54) // 6), 0))} - {0}


def fond_ids(fond: bytes) -> set[int]:
    """The resource IDs of all the fonts a FOND lists, every size and style."""
    if len(fond) < 54:
        return set()
    n = struct.unpack_from(">h", fond, 52)[0] + 1
    return {struct.unpack_from(">h", fond, 58 + 6 * i)[0]
            for i in range(max(min(n, (len(fond) - 54) // 6), 0))}


class AppFonts:
    """Which strike to draw a family in at a size, looked for through an
    application's resource chain as the Font Manager looks: a FONT whose
    ID is family * 128 + size, or the NFNT or FONT a FOND lists for the
    size.  With none of the size, the nearest size is scaled to it.
    standard are the families host fonts stand in for: they're drawn from
    a strike only when the application has its own of the size, as
    Balance of Power has New York 36 to trace its title's letters in, and
    otherwise only measured with one (measure).  Family 1, the
    application font, is the one app_font gives: ApFontID's."""

    def __init__(self, resources, standard: set[int]):
        self.rm = resources
        self.standard = frozenset(standard)
        self.app_font: Callable[[], int] = lambda: 3        # Geneva
        self._strikes: dict[bytes, Strike | None] = {}
        self._found: dict[tuple, tuple[Strike, float] | None] = {}

    def find(self, family: int, size: int) -> tuple[Strike, float] | None:
        """The strike and how much to scale it by, or None for a host font."""
        found = self.source(family, size)
        return found[:2] if found is not None else None

    def source(self, family: int, size: int) -> tuple[Strike, float, str, int] | None:
        """find's answer, and the type and ID of the resource the strike is."""
        family = self.app_font() if family == 1 else family
        if not 0 <= family < 0x8000:
            return None
        size = size if 0 < size < 128 else 12
        key = (family, size, self.rm.top, self.rm.cur_map)
        if key not in self._found:
            if len(self._found) > 512:
                self._found.clear()
            self._found[key] = (self._own(family, size) if family in self.standard
                                else self._look(family, size))
        return self._found[key]

    def _strike(self, kind: str, rid: int, system: bool = True) -> Strike | None:
        data = self.rm.peek(kind, rid, system) if -0x8000 <= rid < 0x8000 else None
        if not data:
            return None
        if data not in self._strikes:
            self._strikes[data] = Strike.parse(data)
        return self._strikes[data]

    def _at(self, family: int, size: int, fond: bytes | None,
            system: bool = True) -> tuple[Strike, str, int] | None:
        rid = font_id(family, size)
        places = [("FONT", rid)] if rid is not None else []
        for rid in font_ids(fond, size) if fond else ():
            places += [("NFNT", rid), ("FONT", rid)]
        for kind, rid in places:
            s = self._strike(kind, rid, system)
            if s is not None:
                return s, kind, rid
        return None

    def _own(self, family: int, size: int) -> tuple[Strike, float, str, int] | None:
        """A standard family's strike of exactly the size in the
        application's files, not the System's.  The system font's too:
        the menus and controls macrt draws itself stay the host's, but
        text drawn in a port comes from the application's own Chicago,
        as Gemstone Warrior's keyboard dialog has its double arrow, $B9,
        between "Walk" and "Run"."""
        found = self._at(family, size, self.rm.peek("FOND", family, False), False)
        return (found[0], 1.0, *found[1:]) if found is not None else None

    def _look(self, family: int, size: int) -> tuple[Strike, float, str, int] | None:
        fond = self.rm.peek("FOND", family)
        found = self._at(family, size, fond)
        if found is not None:
            return found[0], 1.0, *found[1:]
        sizes = fond_sizes(fond) if fond else set()
        sizes |= {rid & 0x7F for rid in self.rm.peek_ids("FONT") if font_family(rid) == family}
        for other in sorted(sizes - {0, size}, key=lambda n: (abs(n - size), -n)):
            found = self._at(family, other, fond)
            if found is not None:
                return found[0], size / other, *found[1:]
        return None

    def widths(self, family: int, size: int) -> tuple[Strike, float] | None:
        """The strike text in the family is measured with at the size, and
        how much to scale its widths by: the one it's drawn from (find),
        else the one the host font standing in for it is (measure)."""
        found = self.find(family, size)
        if found is not None:
            return found
        measured = self.measure(family, size)
        return (measured[0], 1.0) if measured is not None else None

    def measure(self, family: int, size: int) -> tuple[Strike, str, int] | None:
        """The strike of a standard family at exactly the size, and the type
        and ID of its resource, if there's one in the chain: a real System
        file's Chicago 12 and Geneva 9, say.  The host font standing in
        for the family is measured with its widths, so that text goes
        where it did on a Mac and programs that add widths up and draw
        fields of them (the Alarm Clock's time) find them as wide."""
        family = self.app_font() if family == 1 else family
        if family not in self.standard:
            return None
        size = size if 0 < size < 128 else 12
        key = ("measure", family, size, self.rm.top, self.rm.cur_map)
        if key not in self._found:
            if len(self._found) > 512:
                self._found.clear()
            self._found[key] = self._at(family, size, self.rm.peek("FOND", family))
        return self._found[key]
