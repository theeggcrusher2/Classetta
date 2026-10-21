"""
TextEdit (Inside Macintosh I-371, V-261): edit records, and how their
text is broken into lines, measured, drawn and edited.

An edit record is a TERec in a handle in the application's heap, laid out
as Inside Macintosh gives it.  The record is what counts: applications
read its fields (nLines, lineStarts, selStart...) and change some
(destRect to scroll, hText to swap the text), so each call reads it
afresh and writes back what it changes before it returns.  The Python
side (records(system)) keeps only what can be worked out again: the line
starts as last computed and what they were computed for.

A styled record (TEStyleNew) has txSize -1, and its txFont and txFace
hold a handle to a TEStyleRec: the style runs, the style table, the line
heights and the null style (Inside Macintosh V-262).

Lines break as TextEdit breaks them: after the last word that fits the
destRect's width (spaces may hang past it), after a return, or, for a
word longer than the line, after the last character that fits; crOnly
< 0 breaks at returns only.  Text is measured and drawn with QuickDraw's
fonts (macrt.toolbox.qdraw), in the record's port, clipped to its
viewRect.  Each line is painted whole, erased, its text drawn and its
part of the selection or the caret inverted, rather than inverted in
place, so the screen can't drift from the record.
"""

from __future__ import annotations

import struct
from bisect import bisect_right
from typing import TYPE_CHECKING, Callable, Iterable, Iterator, NamedTuple

from ..memory import (ADDRESS_MASK, BYTE, INTEGER, LONG, LONGINT, POINT, RECT, Field, Record,
                      from_mac)
from . import lowmem
from .events import tick_count
from .qdraw import CHICAGO_SYMBOLS, QuickDraw, Region
from .types import Point, Rect

if TYPE_CHECKING:
    from .lowmem import World
    from .memmgr import Heap
    from .quickdraw import GrafPort
    from .system import System

CR = 0x0D
BACKSPACE, FORWARD_DELETE = 0x08, 0x7F
LEFT, RIGHT, UP, DOWN = 0x1C, 0x1D, 0x1E, 0x1F
NOT_TYPED = {0x01, 0x04, 0x0B, 0x0C, 0x10}      # home, end, page up and down, function keys
MAX_LENGTH = 32767
HEADER = 96                                     # the TERec up to lineStarts
LINE_SLACK = 32                                 # lineStarts entries to grow by
CARET_ON = 0xFF00 - 0x10000

teJustLeft, teJustCenter, teJustRight, teForceLeft = 0, 1, -1, -2
teFind, teHilite, teDraw, teCaret = 0, 1, -1, -2     # TEDoText's selectors
doFont, doFace, doSize, doColor, doAll, addSize, doToggle = 1, 2, 4, 8, 15, 16, 32
teFAutoScroll, teFTextBuffering, teFOutlineHilite, teFInlineInput, teFUseTextServices = range(5)
teBitClear, teBitSet, teBitTest = 0, 1, -1

# The dispatch record (hDispatchRec): TECustomHook's hooks, and the
# TEFeatureFlag bits.  which -> offset.
HOOK_OFFSETS = {0: 0, 1: 4, 2: 8, 3: 12, 4: 28, 5: 32, 6: 24, 7: 20}
NEW_TE_FLAGS = 16
DISPATCH_SIZE = 36

BLACK_RGB = bytes(6)
STYLE_REC = 20              # TEStyleRec up to its runs
ST_ELEMENT = 18             # a style table entry
SCRP_ELEMENT = 20           # a style scrap entry
TEXT_STYLE = 12

# 68k code for the records' default procedures: the word break routine
# (a break at characters up to a space: Z clear), the click loop (TRUE:
# go on) and a plain RTS for the dispatch record's hooks.
DEFAULT_WORD_BREAK = bytes.fromhex(
    "48E74000"          # MOVEM.L D1,-(SP)
    "7200"              # MOVEQ   #0,D1
    "12300000"          # MOVE.B  0(A0,D0.W),D1
    "0C410020"          # CMPI.W  #$20,D1
    "53C1"              # SLS     D1
    "4A01"              # TST.B   D1
    "4CDF0002"          # MOVEM.L (SP)+,D1    (leaves the flags alone)
    "4E75")             # RTS
CLIK_LOOP = bytes.fromhex("70014E75")     # MOVEQ #1,D0; RTS
RTS = bytes.fromhex("4E75")


class TERec(Record):
    SIZE = HEADER

    dest_rect = Field(0, RECT)
    view_rect = Field(8, RECT)
    sel_rect = Field(16, RECT)
    line_height = Field(24, INTEGER)
    font_ascent = Field(26, INTEGER)
    sel_point = Field(28, POINT)
    sel_start = Field(32, INTEGER)
    sel_end = Field(34, INTEGER)
    active = Field(36, BYTE)                    # a BOOLEAN: its high byte
    word_break = Field(38, LONG)
    clik_loop = Field(42, LONG)
    click_time = Field(46, LONGINT)
    click_loc = Field(50, INTEGER)
    caret_time = Field(52, LONGINT)
    caret_state = Field(56, INTEGER)
    just = Field(58, INTEGER)
    te_length = Field(60, INTEGER)
    h_text = Field(62, LONG)
    h_dispatch_rec = Field(66, LONG)            # recalBack and recalLines before System 6
    clik_stuff = Field(70, INTEGER)             # nonzero: the caret is at the end of a line
    cr_only = Field(72, INTEGER)
    tx_font = Field(74, INTEGER)
    style_handle = Field(74, LONG)              # txFont and txFace, when txSize is -1
    tx_face = Field(76, BYTE)
    tx_mode = Field(78, INTEGER)
    tx_size = Field(80, INTEGER)
    in_port = Field(82, LONG)
    high_hook = Field(86, LONG)
    caret_hook = Field(90, LONG)
    n_lines = Field(94, INTEGER)


class TEStyleRec(Record):
    SIZE = STYLE_REC

    n_runs = Field(0, INTEGER)
    n_styles = Field(2, INTEGER)
    style_tab = Field(4, LONG)
    lh_tab = Field(8, LONG)
    te_ref_con = Field(12, LONGINT)
    null_style = Field(16, LONG)                # NullStRec: teReserved, nullScrap


class Style(NamedTuple):
    """A run's style: what a TextStyle holds."""
    font: int
    face: int
    size: int
    color: bytes = BLACK_RGB

    @classmethod
    def read(cls, mem, p: int) -> Style:
        """From a TextStyle."""
        return cls(mem.r16s(p), mem.r8(p + 2), mem.r16s(p + 4), mem.read(p + 6, 6))

    def write(self, mem, p: int) -> None:
        mem.write(p, struct.pack(">hBxh", self.font, self.face, self.size) + self.color)


Runs = list[tuple[int, Style]]          # (first character, style), in order, the first at 0


class Hooks:
    """What a record's procedures do when they're the application's code.
    The glue runs them on the 68k; these defaults stand in elsewhere."""

    def word_break(self, proc: int, text_ptr: int, offset: int) -> bool:
        return False

    def clik_loop(self, proc: int, te_ptr: int) -> bool:
        return True

    def hilite(self, proc: int, te_ptr: int, r: Rect) -> None:
        """highHook or caretHook: the application draws the rectangle."""

    def text_procs(self, port: GrafPort) -> tuple[int, int]:
        """The port's own textProc and txMeasProc, 0 for QuickDraw's."""
        return 0, 0

    def measure(self, proc: int, h: int, text_ptr: int, count: int) -> int:
        """What the port's txMeasProc measures count characters at text_ptr as."""
        return 0

    def draw(self, proc: int, h: int, text_ptr: int, count: int, clip: Region) -> None:
        """The port's textProc draws count characters at text_ptr at the pen."""


class Tracker(NamedTuple):
    """What TEClick follows the mouse with."""
    still_down: Callable[[], bool]
    mouse: Callable[[], Point]          # global
    pause: Callable[[], None]           # a tick, letting others run
    now: Callable[[], int]
    double_time: int


# =========================================================================
# The Python side
# =========================================================================
class RecordState:
    def __init__(self):
        self.starts: list[int] | None = None    # line starts, as last computed
        self.key: tuple | None = None           # what for
        self.null_style: tuple[int, Style] | None = None    # (where, style) for typing


class Records(dict):
    """Every edit record's RecordState, by handle, and the code the
    records' procedure fields point at."""

    def __init__(self, system: System):
        super().__init__()
        code = DEFAULT_WORD_BREAK + CLIK_LOOP + RTS
        base = system.sys_heap.new_ptr(len(code))
        system.mem.write(base, code)
        self.word_break_proc = base
        self.clik_loop_proc = base + len(DEFAULT_WORD_BREAK)
        self.rts_proc = self.clik_loop_proc + len(CLIK_LOOP)


def records(system: System) -> Records:
    with system.lock:
        r = getattr(system, "textedit", None)
        if r is None:
            r = system.textedit = Records(system)
        return r


def _shown(text: bytes) -> str:
    """Text as it's measured and drawn: control characters as spaces, but
    for the ones Chicago draws as symbols (a dialog's "hold down ⌘")."""
    return from_mac(text.translate(_CONTROLS)).translate(CHICAGO_SYMBOLS)


_CONTROLS = bytes(0x20 if b < 0x20 and b not in CHICAGO_SYMBOLS else b for b in range(256))


# =========================================================================
# Lines
# =========================================================================
class Layout:
    """Text broken into lines for a destination rectangle.  Line i's top
    is top(i) below dest.top; the display lines are the record's nLines
    and, after a final return or for no text at all, an empty one."""

    def __init__(self, text: bytes, runs: Runs, dest: Rect, *, just: int = teJustLeft,
                 cr_only: int = 0, fixed: tuple[int, int] | None = None,
                 is_break: Callable[[int], bool] | None = None, fonts=None,
                 measure: Callable[[int, int, Style], int] | None = None,
                 draw: Callable[[int, int, Region], None] | None = None):
        """measure and draw, if given, are the port's own txMeasProc and
        textProc, on text[a:b] where it is in memory: draw at the pen in
        the port's style, within a region."""
        self.text = text
        self.shown = _shown(text)
        self.runs = runs or [(0, Style(0, 0, 12))]
        self.dest = dest
        self.just = just
        self.cr_only = cr_only
        self.fixed = fixed                  # (lineHeight, fontAscent), else from the styles
        self.is_break = is_break or self._space
        self.fonts = fonts
        self.measure, self.draw = measure, draw
        self._measured: dict[tuple[int, int, Style], int] = {}
        self.starts: list[int] = []
        self._tops: list[int] | None = None
        self._metrics: list[tuple[int, int]] | None = None

    def _space(self, i: int) -> bool:
        return self.text[i] <= 0x20

    # -- measuring ---------------------------------------------------------
    def style_height(self, style: Style) -> tuple[int, int]:
        """(height, ascent) of a style's font, as a style table has them."""
        ascent, descent, _, leading = self.fonts.info(style.font, style.size, style.face)
        return ascent + descent + leading, ascent

    def segments(self, a: int, b: int) -> Iterator[tuple[int, int, Style]]:
        """The runs' pieces of text[a:b]."""
        runs = self.runs
        i = max(bisect_right(runs, a, key=lambda r: r[0]) - 1, 0)
        while a < b and i < len(runs):
            end = runs[i + 1][0] if i + 1 < len(runs) else b
            end = min(max(end, a), b)
            if end > a:
                yield a, end, runs[i][1]
            a = end
            i += 1
        if a < b:
            yield a, b, runs[-1][1]

    def width(self, a: int, b: int) -> int:
        w = 0
        for s, e, style in self.segments(a, b):
            if self.measure is None:
                w += sum(self.fonts.advances(style.font, style.size, style.face, self.shown[s:e]))
                continue
            key = s, e, style
            if key not in self._measured:
                self._measured[key] = self.measure(s, e, style)
            w += self._measured[key]
        return w

    def style_at(self, offset: int) -> Style:
        """The style of the character at offset (of the last, at the end)."""
        offset = min(offset, len(self.text) - 1)
        i = max(bisect_right(self.runs, offset, key=lambda r: r[0]) - 1, 0)
        return self.runs[i][1]

    # -- breaking ----------------------------------------------------------
    def recalc(self, old: list[int] | None = None, at: int = 0, removed: int = 0,
               inserted: int = 0) -> None:
        """The line starts.  Given the old ones and what changed (removed
        characters at `at` replaced by inserted ones), only the lines from
        the one before the change until they fall into step again."""
        n = len(self.text)
        self._tops = self._metrics = None
        if not n:
            self.starts = []
            return
        starts: list[int] = []
        pos = 0
        later: dict[int, int] = {}
        delta = inserted - removed
        if old and old[0] == 0 and all(a < b for a, b in zip(old, old[1:])):
            i = max(bisect_right(old, at) - 2, 0)
            starts, pos = old[:i], old[i]
            later = {s + delta: j for j, s in enumerate(old) if s >= at + removed and s > pos}
        while pos < n:
            starts.append(pos)
            pos = self._next_line(pos)
            j = later.get(pos)
            if j is not None and pos >= at + inserted:
                starts.extend(s + delta for s in old[j:])
                break
        self.starts = starts

    def _next_line(self, start: int) -> int:
        text, n = self.text, len(self.text)
        cr = text.find(b"\r", start)
        para_end = n if cr < 0 else cr
        after_para = n if cr < 0 else cr + 1
        if self.cr_only < 0:
            return after_para
        fit = self._fit(start, para_end, self.dest.width)
        if fit >= para_end:
            return after_para
        brk = self.is_break
        if brk(fit):                    # spaces hang past the edge, and a return after them
            e = fit
            while e < para_end and brk(e):
                e += 1
            return after_para if e == para_end else e
        for p in range(fit, start, -1):
            if brk(p - 1):
                return p
        return max(fit, start + 1)

    def _fit(self, start: int, end: int, limit: int) -> int:
        """The most characters from start that fit in limit pixels.  (No
        more than about one a pixel can, so a long paragraph isn't
        measured whole for each of its lines.)"""
        hi = min(end, start + 2 * max(limit, 0) + 2)
        if self.width(start, hi) <= limit:
            return hi
        lo = start                      # width(lo) fits, width(hi) doesn't
        while hi - lo > 1:
            mid = (lo + hi) // 2
            if self.width(start, mid) <= limit:
                lo = mid
            else:
                hi = mid
        return lo

    # -- display lines -----------------------------------------------------
    @property
    def n_lines(self) -> int:
        return len(self.starts)

    @property
    def count(self) -> int:
        n = len(self.text)
        return len(self.starts) + (1 if not n or self.text[-1] == CR else 0)

    def start(self, i: int) -> int:
        return self.starts[i] if i < len(self.starts) else len(self.text)

    def end(self, i: int) -> int:
        return self.starts[i + 1] if i + 1 < len(self.starts) else len(self.text)

    def text_end(self, i: int) -> int:
        """Where line i's characters end, before its return."""
        s, e = self.start(i), self.end(i)
        return e - 1 if e > s and self.text[e - 1] == CR else e

    def soft(self, i: int) -> bool:
        """Does line i wrap into the next rather than end with a return?"""
        return i + 1 < len(self.starts) and self.text_end(i) == self.end(i)

    def line_metrics(self, i: int) -> tuple[int, int]:
        """(height, ascent) of line i."""
        if self.fixed is not None:
            return self.fixed
        if self._metrics is None:
            self._metrics = []
            for j in range(self.count):
                s, e = self.start(j), self.text_end(j)
                styles = {st for _, _, st in self.segments(s, e)} or {self.style_at(s - 1)}
                hs = [self.style_height(st) for st in styles]
                self._metrics.append((max(h for h, _ in hs), max(a for _, a in hs)))
        return self._metrics[i] if i < len(self._metrics) else self._metrics[-1]

    def top(self, i: int) -> int:
        if self.fixed is not None:
            return i * self.fixed[0]
        if self._tops is None:
            tops = [0]
            for j in range(self.count):
                tops.append(tops[-1] + self.line_metrics(j)[0])
            self._tops = tops
        return self._tops[min(i, len(self._tops) - 1)]

    @property
    def height(self) -> int:
        return self.top(self.count)

    def line_rect(self, i: int) -> Rect:
        top = self.dest.top + self.top(i)
        return Rect(top, self.dest.left, top + self.line_metrics(i)[0], self.dest.right)

    def line_at(self, offset: int, lead: bool = False) -> int:
        """The display line the caret at offset is on.  lead: at the end of
        the line before, if offset is where a wrapped line goes on."""
        n = len(self.text)
        if offset >= n and (not n or self.text[-1] == CR):
            return self.count - 1
        i = max(bisect_right(self.starts, offset) - 1, 0)
        if lead and i > 0 and offset == self.starts[i] and self.soft(i - 1):
            return i - 1
        return i

    def line_at_v(self, v: int) -> int:
        """The line at a vertical position: the first or last for points
        above or below the text."""
        rel = v - self.dest.top
        if self.fixed is not None:
            i = rel // self.fixed[0] if self.fixed[0] > 0 else 0
        else:
            self.top(0)
            i = bisect_right(self._tops, rel) - 1
        return min(max(i, 0), self.count - 1)

    def indent(self, i: int) -> int:
        if self.just in (teJustLeft, teForceLeft):
            return 0
        room = self.dest.width - self.width(self.start(i), self.text_end(i))
        return room // 2 if self.just == teJustCenter else room

    def x_of(self, offset: int, i: int) -> int:
        return self.dest.left + self.indent(i) + self.width(self.start(i), offset)

    def point_of(self, offset: int, lead: bool = False) -> tuple[int, int]:
        """(h, line) of the caret at offset."""
        i = self.line_at(offset, lead)
        return self.x_of(max(offset, self.start(i)), i), i

    def offset_at(self, pt: Point) -> tuple[int, bool]:
        """The character boundary nearest a point, and whether the caret
        there leads the line (at the end of a wrapped line)."""
        i = self.line_at_v(pt.v)
        s, e = self.start(i), self.text_end(i)
        x0 = self.dest.left + self.indent(i)
        h = pt.h - x0
        if h <= 0 or s == e:
            return s, False
        lo, hi = s, e                   # the last boundary at or left of h
        if self.width(s, e) <= h:
            lo = e
        else:
            while hi - lo > 1:
                mid = (lo + hi) // 2
                if self.width(s, mid) <= h:
                    lo = mid
                else:
                    hi = mid
            if h - self.width(s, lo) >= self.width(s, lo + 1) - h:
                lo += 1
        return lo, lo == self.end(i) and self.soft(i)

    def word_at(self, offset: int,
                is_break: Callable[[int], bool] | None = None) -> tuple[int, int]:
        """The word a double-click at offset selects: letters and digits,
        or one other character, or what's between the record's breaks."""
        n = len(self.text)
        if not n:
            return 0, 0
        if is_break is None:
            def same(i: int) -> bool:
                return self.shown[i].isalnum()
        else:
            def same(i: int) -> bool:
                return not is_break(i)
        i = min(offset, n - 1)
        if not same(i) and i > 0 and same(i - 1) and offset == n:
            i -= 1
        if not same(i):
            return i, i + 1
        a, b = i, i + 1
        while a > 0 and same(a - 1):
            a -= 1
        while b < n and same(b):
            b += 1
        return a, b

    # -- drawing -----------------------------------------------------------
    def paint_line(self, qd: QuickDraw, port: GrafPort, i: int, view: Rect,
                   clip: Region) -> Region | None:
        """Erase line i across the view and draw its text; the area
        painted, None if none of it shows."""
        r = self.line_rect(i)
        band = Rect(r.top, min(r.left, view.left), r.bottom, max(r.right, view.right))
        area = Region.rect(band).sect(clip)
        if area.empty:
            return None
        qd.region(port, "Erase", area)
        x = self.dest.left + self.indent(i)
        baseline = r.top + self.line_metrics(i)[1]
        for s, e, style in self.segments(self.start(i), self.text_end(i)):
            port.tx_font, port.tx_face, port.tx_size = style.font, style.face, style.size
            port.pn_loc = Point(baseline, x)
            if self.draw is None:
                qd.draw_text(port, self.shown[s:e], area)
            else:
                self.draw(s, e, area)
            x += self.width(s, e)
        return area

    def hilite_rect(self, i: int, a: int, b: int) -> Rect | None:
        """The part of line i in the selection a..b: from the left edge if
        the selection goes on from the line before, to the right edge if
        it goes on to the next."""
        s, e = self.start(i), self.end(i)
        lo, hi = max(a, s), min(b, e)
        if lo >= hi:
            return None
        r = self.line_rect(i)
        left = r.left if a < s else self.x_of(lo, i)
        right = r.right if b > e or (b == e and not self.soft(i)) else self.x_of(hi, i)
        return Rect(r.top, left, r.bottom, max(right, left))

    def caret_rect(self, offset: int, lead: bool, view: Rect) -> Rect:
        x, i = self.point_of(offset, lead)
        r = self.line_rect(i)
        x = max(x, view.left + 1)
        return Rect(r.top, x - 1, r.bottom, x)


def splice_runs(runs: Runs, length: int, a: int, b: int, new: Runs, new_len: int) -> Runs:
    """The runs after text[a:b] is replaced by new_len characters styled
    new (their starts relative to a)."""
    out: Runs = [(s, style) for s, style in runs if s < a]
    out += [(a + s, style) for s, style in new if s < new_len]
    delta = new_len - (b - a)
    for k, (s, style) in enumerate(runs):
        e = runs[k + 1][0] if k + 1 < len(runs) else length
        if e > b and max(s, b) < length:
            out.append((max(s, b) + delta, style))
    return normal_runs(out, length + delta, style_in(runs, a))


def style_in(runs: Runs, offset: int) -> Style:
    """The style of the run the character at offset is in."""
    i = max(bisect_right(runs, offset, key=lambda r: r[0]) - 1, 0)
    return runs[i][1] if runs else Style(0, 0, 12)


def normal_runs(runs: Runs, length: int, fallback: Style | None = None) -> Runs:
    """Runs in order, none empty, no two alike side by side, the first at 0."""
    out: Runs = []
    for k, (s, style) in enumerate(runs):
        e = runs[k + 1][0] if k + 1 < len(runs) else length
        if e <= s and not (length == 0 and not out):
            continue
        if out and out[-1][1] == style:
            continue
        out.append((s, style))
    if not out:
        out = [(0, runs[0][1] if runs else fallback or Style(0, 0, 12))]
    out[0] = (0, out[0][1])
    return out


def restyle(runs: Runs, length: int, a: int, b: int, change: Callable[[Style], Style]) -> Runs:
    """The runs with change applied to the styles of a..b."""
    cut: Runs = []
    for k, (s, style) in enumerate(runs):
        e = runs[k + 1][0] if k + 1 < len(runs) else length
        for lo, hi in ((s, min(e, a)), (max(s, a), min(e, b)), (max(s, b), e)):
            if lo < hi:
                cut.append((lo, change(style) if a <= lo and hi <= b else style))
    return normal_runs(cut, length, runs[0][1])


def apply_style(style: Style, mode: int, new: Style, continuous_face: int = 0) -> Style:
    """TESetStyle's change to one style.  continuous_face: the faces all
    of the selection has, for doToggle."""
    font, face, size, color = style
    if mode & doFont:
        font = new.font
    if mode & doFace:
        if mode & doToggle and new.face:
            face = (face & ~new.face) if continuous_face & new.face == new.face else face | new.face
        elif new.face:
            face |= new.face
        else:
            face = 0
    if mode & addSize:
        size = max(1, size + new.size)
    elif mode & doSize:
        size = new.size
    if mode & doColor:
        color = new.color
    return Style(font, face, size, color)


def _matches(style: Style, old: Style, mode: int) -> bool:
    return ((not mode & doFont or style.font == old.font)
            and (not mode & doFace or style.face == old.face)
            and (not mode & doSize or style.size == old.size)
            and (not mode & doColor or style.color == old.color))


def _clamp(v: int, n: int) -> int:
    return min(max(v, 0), n)


# =========================================================================
# Style scraps: StScrpRec (Inside Macintosh V-264)
# =========================================================================
def style_scrap(runs: list[tuple[int, Style, int, int]]) -> bytes:
    """A StScrpRec: (start, style, height, ascent) for each run."""
    out = struct.pack(">h", len(runs))
    for start, st, height, ascent in runs:
        out += struct.pack(">ihhhBxh", start, height, ascent, st.font, st.face, st.size) + st.color
    return out


def read_style_scrap(data: bytes) -> Runs:
    """A StScrpRec's runs."""
    if len(data) < 2:
        return []
    n = struct.unpack(">h", data[:2])[0]
    runs: Runs = []
    for k in range(max(n, 0)):
        e = data[2 + SCRP_ELEMENT * k:2 + SCRP_ELEMENT * (k + 1)]
        if len(e) < SCRP_ELEMENT:
            break
        start, _, _, font, face, size = struct.unpack(">ihhhBxh", e[:14])
        runs.append((start, Style(font, face, size, e[14:20])))
    runs.sort(key=lambda r: r[0])
    return runs


# =========================================================================
# Edit records
# =========================================================================
class EditRecord:
    """An edit record, for the length of one call: the TERec in memory,
    its text broken into lines (layout), and the editing and drawing
    TextEdit does with it.  hooks runs the application's procedures; now
    is TickCount."""

    def __init__(self, system: System, qd: QuickDraw, h: int, hooks: Hooks | None = None,
                 now: Callable[[], int] = tick_count):
        self.system = system
        self.mem = system.mem
        self.qd = qd
        self.h = h & ADDRESS_MASK
        self.heap = system.heap_of(self.h)
        self.hooks = hooks or Hooks()
        self.now = now
        self.records = records(system)
        state = self.records.get(self.h)
        if state is None:
            state = self.records[self.h] = RecordState()
        self.state = state
        self.layout = self._layout()

    # -- the record ----------------------------------------------------------
    @property
    def te(self) -> TERec:
        """The TERec, where it is now (handles move)."""
        return TERec(self.mem, self.heap.deref(self.h))

    @property
    def styled(self) -> bool:
        return self.te.tx_size == -1

    @property
    def port(self) -> GrafPort | None:
        p = self.te.in_port & ADDRESS_MASK
        return self.system.port_at(p) if p else None

    @property
    def text(self) -> bytes:
        return self.layout.text

    @property
    def selection(self) -> tuple[int, int]:
        return self._selection(len(self.layout.text))

    def _selection(self, n: int) -> tuple[int, int]:
        te = self.te
        a, b = _clamp(te.sel_start, n), _clamp(te.sel_end, n)
        return min(a, b), max(a, b)

    def _deref(self, h: int) -> int:
        h &= ADDRESS_MASK
        return self.mem.r32(h) & ADDRESS_MASK if h else 0

    def _heap_for(self, h: int) -> Heap:
        try:
            return self.system.heap_of(h & ADDRESS_MASK)
        except ValueError:
            return self.heap

    def _resize(self, h: int, size: int) -> bool:
        heap = self._heap_for(h)
        if heap.is_empty(h):
            return heap.realloc_handle(h, size)
        return heap.handle_size(h) == size or heap.set_handle_size(h, size)

    def _read_text(self) -> bytes:
        te = self.te
        h = te.h_text & ADDRESS_MASK
        p = self._deref(h)
        if not p:
            return b""
        n = min(max(te.te_length, 0), self._heap_for(h).handle_size(h))
        return self.mem.read(p, n)

    def _style_rec(self) -> TEStyleRec | None:
        if not self.styled:
            return None
        p = self._deref(self.te.style_handle)
        return TEStyleRec(self.mem, p) if p else None

    def _read_runs(self, length: int) -> Runs:
        te = self.te
        sr = self._style_rec()
        if sr is None:
            return [(0, Style(te.tx_font, te.tx_face, te.tx_size))]
        mem = self.mem
        tab = self._deref(sr.style_tab)
        n_styles = sr.n_styles
        runs: Runs = []
        for k in range(max(sr.n_runs, 1)):
            start, i = struct.unpack(">hh", mem.read(sr.addr + STYLE_REC + 4 * k, 4))
            if not tab or not 0 <= i < n_styles or start > length:
                break
            e = tab + ST_ELEMENT * i
            runs.append((start, Style(mem.r16s(e + 6), mem.r8(e + 8), mem.r16s(e + 10),
                                      mem.read(e + 12, 6))))
        runs.sort(key=lambda r: r[0])
        return normal_runs(runs, length)

    def _new_layout(self, text: bytes, runs: Runs) -> Layout:
        te = self.te
        fixed = (te.line_height, te.font_ascent) if te.line_height > 0 else None
        measure, draw = self._text_procs()
        return Layout(text, runs, te.dest_rect, just=te.just, cr_only=te.cr_only, fixed=fixed,
                      is_break=self._break_test(), fonts=self.qd.fonts, measure=measure,
                      draw=draw)

    def _key(self, lay: Layout) -> tuple:
        """What the line starts depend on."""
        return (lay.text, lay.dest.width, lay.cr_only, tuple(lay.runs), self.te.word_break,
                lay.measure is None)

    def _text_procs(self) -> tuple[Callable[[int, int, Style], int] | None,
                                   Callable[[int, int, Region], None] | None]:
        """The port's own txMeasProc and textProc, if it has them, called on
        the text where it is in hText, as TextEdit measures and draws it
        with TextWidth and DrawText."""
        port = self.port
        text_proc, meas_proc = self.hooks.text_procs(port) if port is not None else (0, 0)

        def measure(a: int, b: int, style: Style) -> int:
            saved = port.tx_font, port.tx_face, port.tx_size
            port.tx_font, port.tx_face, port.tx_size = style.font, style.face, style.size
            try:
                return self.hooks.measure(meas_proc, self.h, self._deref(self.te.h_text) + a,
                                          b - a)
            finally:
                port.tx_font, port.tx_face, port.tx_size = saved

        def draw(a: int, b: int, clip: Region) -> None:
            self.hooks.draw(text_proc, self.h, self._deref(self.te.h_text) + a, b - a, clip)
        return measure if meas_proc else None, draw if text_proc else None

    def _layout(self) -> Layout:
        """The lines, as last worked out if nothing they depend on has
        changed since, else worked out again and stored in the record."""
        text = self._read_text()
        lay = self._new_layout(text, self._read_runs(len(text)))
        if self.state.starts is not None and self.state.key == self._key(lay):
            lay.starts = list(self.state.starts)
        else:
            lay.recalc()
            self._store_lines(lay)
        return lay

    def _break_test(self) -> Callable[[int], bool] | None:
        proc = self.te.word_break & ADDRESS_MASK
        if proc in (0, self.records.word_break_proc):
            return None

        def is_break(i: int) -> bool:
            return self.hooks.word_break(proc, self._deref(self.te.h_text), i)
        return is_break

    # -- writing it back -----------------------------------------------------
    def _store_lines(self, lay: Layout) -> None:
        """nLines and lineStarts, growing the record if they need room."""
        starts = lay.starts
        self.state.starts = list(starts)
        self.state.key = self._key(lay)
        heap = self.heap
        need = HEADER + 2 * (len(starts) + 1)
        if heap.handle_size(self.h) < need:
            heap.set_handle_size(self.h, need + 2 * LINE_SLACK)
        te = self.te
        te.n_lines = len(starts)
        room = max((heap.handle_size(self.h) - HEADER) // 2, 0)
        data = struct.pack(f">{len(starts) + 1}H", *starts, len(lay.text))
        self.mem.write(te.addr + HEADER, data[:2 * room])

    def _write_text(self, text: bytes) -> bool:
        h = self.te.h_text & ADDRESS_MASK
        if not h:
            h = self.heap.new_handle(len(text))
            if not h:
                return False
            self.te.h_text = h
        elif not self._resize(h, len(text)):
            return False
        self.mem.write(self._deref(h), text)
        self.te.te_length = len(text)
        return True

    def _write_styles(self, lay: Layout) -> None:
        """The style record: its runs, its style table and line heights."""
        sr = self._style_rec()
        if sr is None:
            return
        runs = lay.runs
        styles: list[Style] = []
        index = []
        for _, st in runs:
            if st not in styles:
                styles.append(st)
            index.append(styles.index(st))
        sh = self.te.style_handle & ADDRESS_MASK
        size = STYLE_REC + 4 * (len(runs) + 1)
        if self._heap_for(sh).handle_size(sh) < size:
            if not self._resize(sh, size):
                return
            sr = self._style_rec()
        sr.n_runs, sr.n_styles = len(runs), len(styles)
        data = b"".join(struct.pack(">hh", s, i) for (s, _), i in zip(runs, index))
        self.mem.write(sr.addr + STYLE_REC, data + struct.pack(">hh", len(lay.text) + 1, -1))
        tab = b"".join(struct.pack(">hhhhBxh", index.count(i), *lay.style_height(st), st.font,
                                   st.face, st.size) + st.color for i, st in enumerate(styles))
        heights = [lay.line_metrics(i)[0] for i in range(lay.count)]
        lh = struct.pack(f">{len(heights)}h", *heights)
        for h, data in ((sr.style_tab, tab), (sr.lh_tab, lh)):
            if h & ADDRESS_MASK and self._resize(h, len(data)):
                self.mem.write(self._deref(h), data)

    def _sel_fields(self) -> None:
        """selRect and selPoint: where the insertion point is."""
        te = self.te
        lay = self.layout
        r = lay.caret_rect(self.selection[0], te.clik_stuff != 0, te.view_rect)
        te.sel_rect = r
        te.sel_point = Point(r.top, r.right)

    def _caret_on(self) -> None:
        te = self.te
        te.caret_state = CARET_ON
        te.caret_time = self.now()

    # -- drawing -------------------------------------------------------------
    def _paint(self, lines: Iterable[int], clip: Region | None = None) -> None:
        """Paint lines in the record's port, within its viewRect (and clip)."""
        port = self.port
        if port is None:
            return
        te = self.te
        lay = self.layout
        lay.dest = te.dest_rect
        view = te.view_rect
        area = Region.rect(view) if clip is None else clip.sect(Region.rect(view))
        if area.empty:
            return
        box = area.bbox
        first, last = lay.line_at_v(box.top), lay.line_at_v(box.bottom - 1)
        lines = sorted(i for i in set(lines) if first <= i <= last)
        if not lines:
            return
        saved = (port.tx_font, port.tx_face, port.tx_size, port.tx_mode, port.pn_loc,
                 port.sp_extra)
        port.tx_mode, port.sp_extra = te.tx_mode, 0
        try:
            for i in lines:
                painted = lay.paint_line(self.qd, port, i, view, area)
                if painted is not None:
                    self._hilite(port, i, painted)
        finally:
            (port.tx_font, port.tx_face, port.tx_size, port.tx_mode, port.pn_loc,
             port.sp_extra) = saved

    def _hilite(self, port: GrafPort, i: int, area: Region) -> None:
        """Line i's part of the selection, or the caret, inverted."""
        te = self.te
        if not te.active:
            return
        lay = self.layout
        a, b = self.selection
        lead = te.clik_stuff != 0
        if a < b:
            r, hook = lay.hilite_rect(i, a, b), te.high_hook
        elif te.caret_state and lay.line_at(a, lead) == i:
            r, hook = lay.caret_rect(a, lead, te.view_rect), te.caret_hook
        else:
            return
        self._invert(port, r, hook, area)

    def _invert(self, port: GrafPort, r: Rect | None, hook: int, area: Region) -> None:
        """r inverted, or given to the application's hook if it has one."""
        if r is None or r.empty:
            return
        if hook & ADDRESS_MASK:
            self.hooks.hilite(hook & ADDRESS_MASK, self.h, r)
        else:
            self.qd.region(port, "Invert", Region.rect(r).sect(area))

    def _invert_sel(self) -> None:
        """The selection's hilite, or the caret if it's showing, inverted:
        how the ROM shows and hides them, with the text left as it is (an
        app may have drawn its own over a record's viewRect)."""
        port = self.port
        if port is None or not self.te.active:
            return
        self.layout.dest = self.te.dest_rect
        view = Region.rect(self.te.view_rect)
        for i in sorted(self._span_lines(*self.selection)):
            self._hilite(port, i, view)

    def do_text(self, selector: int, a: int, b: int) -> int:
        """What the ROM's DoText does, for programs that call it through
        TEDoText themselves, as Macintosh Pascal's editor does (Inside
        Macintosh I-391): teFind, the offset from a to b nearest selPoint
        across; teHilite, a to b highlighted; teDraw, their lines drawn;
        teCaret, the pen at the caret at a, with selRect's top and bottom
        its line's."""
        te = self.te
        lay = self.layout
        lay.dest = te.dest_rect
        n = len(lay.text)
        a, b = _clamp(a, n), _clamp(b, n)
        port = self.port
        if selector == teFind:
            i = lay.line_at(a)
            offset, _ = lay.offset_at(Point(lay.line_rect(i).top, te.sel_point.h))
            return min(max(offset, a), b)
        if port is None:
            return 0
        lines = range(lay.line_at(min(a, b)), lay.line_at(max(a, b)) + 1)
        if selector == teHilite:
            view = Region.rect(te.view_rect)
            for i in lines:
                self._invert(port, lay.hilite_rect(i, min(a, b), max(a, b)), te.high_hook, view)
        elif selector == teDraw:
            for i in lines:
                lay.paint_line(self.qd, port, i, te.view_rect, Region.rect(te.view_rect))
        elif selector == teCaret:
            x, i = lay.point_of(a)
            r = lay.line_rect(i)
            te.sel_rect = Rect(r.top, x - 1, r.bottom, x)
            port.pn_loc = Point(r.top + lay.line_metrics(i)[1], x)
        return 0

    def _span_lines(self, a: int, b: int) -> set[int]:
        """The lines from offset a to b, and the one before (where a caret
        at a line's start may be)."""
        lay = self.layout
        lo, hi = min(a, b), max(a, b)
        return set(range(max(lay.line_at(lo) - 1, 0), lay.line_at(hi) + 1))

    def _changed_lines(self, old: Layout, at: int, removed: int, inserted: int) -> set[int]:
        """The lines that differ from old's after an edit."""
        lay = self.layout
        delta = inserted - removed
        last_edited = lay.line_at(at + inserted)
        bottom = self.te.view_rect.bottom
        lines = set()
        i = max(min(old.line_at(at), lay.line_at(at)) - 1, 0)
        while i < lay.count:
            if (i > last_edited and i < old.count and old.start(i) + delta == lay.start(i)
                    and old.top(i) == lay.top(i)):
                break
            lines.add(i)
            if lay.dest.top + lay.top(i) >= bottom:
                break
            i += 1
        return lines

    def _redraw(self, old: Layout, at: int, removed: int, inserted: int,
                also: set[int] = frozenset()) -> None:
        """Paint what an edit changed; erase what's below the text if it's
        got shorter."""
        lay = self.layout
        self._paint(self._changed_lines(old, at, removed, inserted) | also)
        port = self.port
        if port is not None and lay.height < old.height:
            d, v = self.te.dest_rect, self.te.view_rect
            gone = Rect(d.top + lay.height, min(d.left, v.left), d.top + old.height,
                        max(d.right, v.right))
            self.qd.region(port, "Erase", Region.rect(gone).sect(Region.rect(v)))

    # -- editing ---------------------------------------------------------------
    def insertion_style(self, at: int) -> Style:
        """The style typing at offset at gets: the null style set there, else
        that of the character before (the first, at the start)."""
        null = self.state.null_style
        if null is not None and null[0] == at:
            return null[1]
        return self.layout.style_at(max(at - 1, 0))

    def replace(self, a: int, b: int, data: bytes, runs: Runs | None = None,
                sel: tuple[int, int] | None = None) -> bool:
        """Replace text[a:b] with data (styled as runs, else in the insertion
        style), select sel (else an insertion point after it) and redraw
        what changed.  False if there's no room for the text."""
        old = self.layout
        n = len(old.text)
        a, b = sorted((_clamp(a, n), _clamp(b, n)))
        data = data[:max(MAX_LENGTH - (n - (b - a)), 0)]
        text = old.text[:a] + data + old.text[b:]
        new_runs = old.runs
        if self.styled:
            new_runs = splice_runs(old.runs, n, a, b, runs or [(0, self.insertion_style(a))],
                                   len(data))
        if not self._write_text(text):
            return False
        old_sel = self._selection(n)
        lay = self._new_layout(text, new_runs)
        lay.recalc(old.starts, a, b - a, len(data))
        self.layout = lay
        self._store_lines(lay)
        self._write_styles(lay)
        delta = len(data) - (b - a)

        def moved(x: int) -> int:
            return x if x <= a else x + delta if x >= b else a + len(data)
        s, e = sel if sel is not None else (a + len(data),) * 2
        s, e = sorted((_clamp(s, len(text)), _clamp(e, len(text))))
        te = self.te
        te.sel_start, te.sel_end, te.clik_stuff = s, e, 0
        self.state.null_style = None
        if s == e:
            self._caret_on()
        self._sel_fields()
        also = self._span_lines(*map(moved, old_sel)) | self._span_lines(s, e)
        self._redraw(old, a, b - a, len(data), also)
        return True

    def select(self, a: int, b: int, lead: bool = False) -> None:
        """TESetSelect.  lead: an insertion point where a line wraps is at
        the end of the line before (clikStuff)."""
        te = self.te
        n = len(self.layout.text)
        a, b = sorted((_clamp(a, n), _clamp(b, n)))
        lead = lead and a == b
        same = (a, b) == self.selection and lead == (te.clik_stuff != 0)
        if not same or (a == b and te.caret_state):
            self._invert_sel()                          # the old one hidden
        if not same:
            self.state.null_style = None
        te.sel_start, te.sel_end, te.clik_stuff = a, b, 1 if lead else 0
        if a == b:
            self._caret_on()
        self._sel_fields()
        if not same or a == b:
            self._invert_sel()

    def set_text(self, data: bytes) -> bool:
        """TESetText: the text replaced, an insertion point at its end;
        nothing redrawn."""
        data = data[:MAX_LENGTH]
        style = self.layout.style_at(0)
        if not self._write_text(data):
            return False
        lay = self._new_layout(data, [(0, style)] if self.styled else self.layout.runs)
        lay.recalc()
        self.layout = lay
        self._store_lines(lay)
        self._write_styles(lay)
        te = self.te
        te.sel_start = te.sel_end = len(data)
        te.clik_stuff = 0
        self.state.null_style = None
        self._sel_fields()
        return True

    def cal_text(self) -> None:
        """TECalText: the lines worked out again."""
        lay = self.layout
        lay.recalc()
        self._store_lines(lay)
        self._write_styles(lay)
        te = self.te
        te.sel_start, te.sel_end = self.selection
        self._sel_fields()

    def delete(self) -> None:
        a, b = self.selection
        if a < b:
            self.replace(a, b, b"")

    def insert(self, data: bytes, runs: Runs | None = None) -> bool:
        """TEInsert: data before the selection, which moves along."""
        a, b = self.selection
        return self.replace(a, a, data, runs, sel=(a + len(data), b + len(data)))

    def paste(self, data: bytes, runs: Runs | None = None) -> bool:
        """The selection replaced by data, the insertion point after it."""
        a, b = self.selection
        ok = self.replace(a, b, data, runs)
        self.sel_view()
        return ok

    def key(self, ch: int) -> None:
        """TEKey: type a character.  Backspace deletes, the arrow keys move
        the insertion point; return and the rest go in as typed."""
        ch &= 0xFF
        lay = self.layout
        n = len(lay.text)
        a, b = self.selection
        lead = self.te.clik_stuff != 0
        if ch == BACKSPACE:
            if a < b or a > 0:
                self.replace(a - 1 if a == b else a, b, b"")
        elif ch == FORWARD_DELETE:
            if a < b or a < n:
                self.replace(a, b + 1 if a == b else b, b"")
        elif ch in (LEFT, RIGHT):
            if a < b:
                c = a if ch == LEFT else b
            else:
                c = max(a - 1, 0) if ch == LEFT else min(a + 1, n)
            self.select(c, c)
        elif ch in (UP, DOWN):
            x, i = lay.point_of(a if ch == UP else b, lead and a == b)
            j = i - 1 if ch == UP else i + 1
            if j < 0:
                self.select(0, 0)
            elif j >= lay.count:
                self.select(n, n)
            else:
                c, ld = lay.offset_at(Point(lay.dest.top + lay.top(j), x))
                self.select(c, c, ld)
        elif ch in NOT_TYPED:
            return
        else:
            self.replace(a, b, bytes([ch]))
        self.sel_view()

    def click(self, pt: Point, extend: bool, tracker: Tracker) -> None:
        """TEClick: select where the mouse went down (a word, for a double
        click; from the selection's far end, extending), and follow it
        while the button's down, calling the click loop."""
        te = self.te
        off, lead = self.layout.offset_at(pt)
        double = tracker.now() - te.click_time < tracker.double_time and off == te.click_loc
        word = self._break_test()

        def unit(o: int) -> tuple[int, int]:
            return self.layout.word_at(o, word) if double else (o, o)
        if extend:
            a, b = self.selection
            anchor = (b, b) if abs(off - a) < abs(off - b) else (a, a)
        else:
            anchor = unit(off)

        def to(o: int, ld: bool) -> None:
            u = unit(o)
            lo, hi = min(anchor[0], u[0]), max(anchor[1], u[1])
            self.select(lo, hi, ld and lo == hi)
        to(off, lead)
        clik = te.clik_loop & ADDRESS_MASK
        while tracker.still_down():
            if clik not in (0, self.records.clik_loop_proc):
                if not self.hooks.clik_loop(clik, self.h):
                    break
                self.layout = self._layout()            # it may have scrolled
            port = self.port
            p = tracker.mouse()
            if port is not None:
                p = port.global_to_local(p)
            if self.auto_view:
                self._auto_scroll(p)
            to(*self.layout.offset_at(p))
            tracker.pause()
        te = self.te
        te.click_time = tracker.now()
        te.click_loc = off

    def idle(self) -> None:
        """TEIdle: blink the insertion point, every CaretTime ticks."""
        te = self.te
        a, b = self.selection
        if not te.active or a != b:
            return
        now = self.now()
        if now - te.caret_time < self.mem.r32(lowmem.CaretTime):
            return
        if te.caret_state:
            self._invert_sel()
            te.caret_state = 0
        else:
            te.caret_state = CARET_ON
            self._invert_sel()
        te.caret_time = now

    def activate(self) -> None:
        """TEActivate: the selection hilited, or the caret shown."""
        te = self.te
        if te.active:
            return
        te.active = 0xFF                        # as the ROM sets it, with ST
        self._caret_on()
        self._invert_sel()

    def deactivate(self) -> None:
        """TEDeactivate: the hilite or caret inverted away."""
        te = self.te
        if not te.active:
            return
        self._invert_sel()
        te.active = 0
        te.caret_state = 0

    def update(self, r: Rect) -> None:
        """TEUpdate: draw the text in r."""
        self._paint(range(self.layout.count), Region.rect(r))

    def set_just(self, just: int) -> None:
        """TESetJust: nothing redrawn."""
        self.te.just = just
        self.layout.just = just
        self._sel_fields()

    # -- scrolling -------------------------------------------------------------
    def scroll(self, dh: int, dv: int) -> None:
        """TEScroll: move the destRect, the bits in the viewRect with it,
        and draw what comes into view."""
        if not dh and not dv:
            return
        te = self.te
        te.dest_rect = te.dest_rect.offset(dh, dv)
        self.layout.dest = te.dest_rect
        self._sel_fields()
        port = self.port
        if port is not None:
            vacated = self.qd.scroll_rect(port, te.view_rect, dh, dv)
            if not vacated.empty:
                self._paint(range(self.layout.count), vacated)

    def pin_scroll(self, dh: int, dv: int) -> None:
        """TEPinScroll: TEScroll no further than the text goes."""
        d, v = self.te.dest_rect, self.te.view_rect
        bottom = d.top + self.layout.height
        dv = max(dv, min(0, v.bottom - bottom)) if dv < 0 else min(dv, max(0, v.top - d.top))
        dh = max(dh, min(0, v.right - d.right)) if dh < 0 else min(dh, max(0, v.left - d.left))
        self.scroll(dh, dv)

    def sel_view(self) -> None:
        """TESelView: with automatic scrolling on, scroll the insertion
        point (the selection's start) into view."""
        if not self.auto_view:
            return
        te, lay = self.te, self.layout
        v = te.view_rect
        x, i = lay.point_of(self.selection[0], te.clik_stuff != 0)
        r = lay.line_rect(i)
        dv = dh = 0
        if r.top < v.top:
            dv = v.top - r.top
        elif r.bottom > v.bottom:
            dv = max(v.bottom - r.bottom, v.top - r.top)
        if x < v.left:
            dh = v.left - x
        elif x >= v.right:
            dh = v.right - 1 - x
        self.scroll(dh, dv)

    def _auto_scroll(self, p: Point) -> None:
        """Dragging out of the viewRect scrolls, a line at a time."""
        v, lay = self.te.view_rect, self.layout
        step = lay.line_metrics(lay.line_at_v(p.v))[0]
        dv = step if p.v < v.top else -step if p.v >= v.bottom else 0
        dh = 8 if p.h < v.left else -8 if p.h >= v.right else 0
        if dh or dv:
            self.pin_scroll(dh, dv)

    # -- where things are ------------------------------------------------------
    def offset_at(self, pt: Point) -> int:
        """TEGetOffset"""
        return self.layout.offset_at(pt)[0]

    def point_of(self, offset: int) -> Point:
        """TEGetPoint: the bottom left of the character at offset."""
        lay = self.layout
        x, i = lay.point_of(_clamp(offset, len(lay.text)))
        return Point(lay.line_rect(i).bottom, x)

    def height(self, end_line: int, start_line: int) -> int:
        """TEGetHeight: of lines start_line to end_line, counted from 1."""
        lay = self.layout
        lo, hi = sorted((start_line, end_line))
        lo, hi = max(lo, 1), min(hi, lay.count)
        return lay.top(hi) - lay.top(lo - 1) if lo <= hi else 0

    # -- the dispatch record -----------------------------------------------------
    def _dispatch_rec(self) -> int:
        h = self.te.h_dispatch_rec & ADDRESS_MASK
        return self._deref(h) if h and self._heap_for(h).is_handle(h) else 0

    def feature(self, bit: int, action: int) -> int:
        """TEFeatureFlag: the feature as it was, teBitClear or teBitSet."""
        p = self._dispatch_rec()
        if not p or not 0 <= bit < 32:
            return teBitClear
        flags = self.mem.r32(p + NEW_TE_FLAGS)
        was = (flags >> bit) & 1
        if action == teBitClear:
            flags &= ~(1 << bit)
        elif action == teBitSet:
            flags |= 1 << bit
        self.mem.w32(p + NEW_TE_FLAGS, flags & 0xFFFFFFFF)
        return was

    @property
    def auto_view(self) -> bool:
        return self.feature(teFAutoScroll, teBitTest) == teBitSet

    def custom_hook(self, which: int, addr: int) -> int:
        """TECustomHook: set a hook, and return the one it replaces."""
        p = self._dispatch_rec()
        off = HOOK_OFFSETS.get(which)
        if not p or off is None:
            return addr
        old = self.mem.r32(p + off)
        self.mem.w32(p + off, addr)
        return old

    # -- styles ------------------------------------------------------------------
    def get_style(self, offset: int) -> tuple[Style, int, int]:
        """TEGetStyle: the style at offset, its line height and ascent."""
        te = self.te
        if not self.styled:
            return Style(te.tx_font, te.tx_face, te.tx_size), te.line_height, te.font_ascent
        st = self.layout.style_at(offset)
        return (st, *self.layout.style_height(st))

    def _restyle(self, runs: Runs, a: int, b: int, redraw: bool) -> None:
        old = self.layout
        lay = self._new_layout(old.text, runs)
        lay.recalc(old.starts, a, b - a, b - a)
        self.layout = lay
        self._store_lines(lay)
        self._write_styles(lay)
        self._sel_fields()
        if redraw:
            self._redraw(old, a, b - a, b - a)

    def set_style(self, mode: int, new: Style, redraw: bool) -> None:
        """TESetStyle: the selection's style changed; for an insertion
        point, the null style typing gets."""
        if not self.styled:
            return
        a, b = self.selection
        if a == b:
            base = self.insertion_style(a)
            self.state.null_style = (a, apply_style(base, mode, new, base.face))
            return
        face = self.continuous_style(doFace)[2].face
        runs = restyle(self.layout.runs, len(self.text), a, b,
                       lambda st: apply_style(st, mode, new, face))
        self._restyle(runs, a, b, redraw)

    def replace_style(self, mode: int, old: Style, new: Style, redraw: bool) -> None:
        """TEReplaceStyle: in the selection, old's attributes (those mode
        names) become new's."""
        a, b = self.selection
        if not self.styled or a == b:
            return
        runs = restyle(self.layout.runs, len(self.text), a, b,
                       lambda st: apply_style(st, mode, new) if _matches(st, old, mode) else st)
        self._restyle(runs, a, b, redraw)

    def _styles_in(self, a: int, b: int) -> list[Style]:
        if a == b or not self.styled:
            return [self.insertion_style(a)]
        return [st for _, _, st in self.layout.segments(a, b)]

    def continuous_style(self, mode: int) -> tuple[bool, int, Style]:
        """TEContinuousStyle: which of mode's attributes the whole
        selection has; the faces all of it has."""
        styles = self._styles_in(*self.selection)
        first = styles[0]
        face = first.face
        for st in styles:
            face &= st.face
        out, ok = 0, True
        for bit, attr in ((doFont, "font"), (doFace, "face"), (doSize, "size"), (doColor, "color")):
            if mode & bit:
                same = all(getattr(st, attr) == getattr(first, attr) for st in styles)
                ok = ok and same
                if same or (bit == doFace and face):
                    out |= bit
        return ok, out, first._replace(face=face)

    def num_styles(self, a: int, b: int) -> int:
        """TENumStyles: style changes in a..b."""
        n = len(self.text)
        a, b = sorted((_clamp(a, n), _clamp(b, n)))
        return max(len(list(self.layout.segments(a, b))), 1)

    def style_scrap(self, a: int, b: int) -> bytes:
        """The styles of a..b as a StScrpRec (GetStylScrap)."""
        if a < b:
            styles = [(s - a, st) for s, _, st in self.layout.segments(a, b)]
        else:
            styles = [(0, self.insertion_style(a))]
        return style_scrap([(s, st, *self.layout.style_height(st)) for s, st in styles])

    def use_style_scrap(self, a: int, b: int, runs: Runs, redraw: bool) -> None:
        """TEUseStyleScrap: a..b styled as a style scrap's runs."""
        if not self.styled or not runs:
            return
        n = len(self.text)
        a, b = sorted((_clamp(a, n), _clamp(b, n)))
        if a == b:
            return
        lay = self.layout
        cut = [(s, st) for s, st in lay.runs if s < a]
        cut += [(a + s, st) for s, st in runs if s < b - a]
        if b < n:
            cut.append((b, lay.style_at(b)))
        cut += [(s, st) for s, st in lay.runs if s > b]
        self._restyle(normal_runs(cut, n), a, b, redraw)


# =========================================================================
# Records made and disposed of
# =========================================================================
def new_record(system: System, qd: QuickDraw, heap: Heap, port: GrafPort, dest: Rect,
               view: Rect, styled: bool = False) -> int:
    """TENew (TEStyleNew, styled): an edit record for port in heap, with
    no text; 0 if there's no room."""
    recs = records(system)
    mem = system.mem
    sizes = [HEADER + 2 * LINE_SLACK, 0, DISPATCH_SIZE]
    if styled:          # the style record, its style table, line heights, null style and scrap
        sizes += [STYLE_REC + 8, ST_ELEMENT, 2, 8, 2]
    made = [heap.new_handle(size, clear=True) for size in sizes]
    if not all(made):
        for x in made:
            if x:
                heap.dispose_handle(x)
        return 0
    h, h_text, disp = made[:3]
    ascent, descent, _, leading = qd.font_info(port)
    te = TERec(mem, heap.deref(h))
    te.dest_rect, te.view_rect = dest, view
    te.line_height, te.font_ascent = ascent + descent + leading, ascent
    te.word_break, te.clik_loop = recs.word_break_proc, recs.clik_loop_proc
    te.h_text, te.h_dispatch_rec = h_text, disp
    te.tx_font, te.tx_face, te.tx_mode, te.tx_size = (port.tx_font, port.tx_face, port.tx_mode,
                                                      port.tx_size)
    te.in_port = port.addr
    p = heap.deref(disp)
    for off in set(HOOK_OFFSETS.values()):
        mem.w32(p + off, recs.rts_proc)
    recs.pop(h, None)
    if styled:
        style_rec, tab, lh, null, null_scrap = made[3:]
        sr = TEStyleRec(mem, heap.deref(style_rec))
        sr.n_runs, sr.n_styles = 1, 1
        sr.style_tab, sr.lh_tab, sr.null_style = tab, lh, null
        mem.write(sr.addr + STYLE_REC, struct.pack(">hhhh", 0, 0, 1, -1))
        mem.w32(heap.deref(null) + 4, null_scrap)
        style = Style(port.tx_font, port.tx_face, port.tx_size)
        mem.write(heap.deref(tab), struct.pack(">hhhhBxh", 1, 0, 0, style.font, style.face,
                                               style.size) + BLACK_RGB)
        te.style_handle = style_rec
        te.tx_size = -1
        te.line_height = te.font_ascent = -1
        ed = EditRecord(system, qd, h)
        ed._write_styles(ed.layout)
    else:
        EditRecord(system, qd, h)           # nLines and lineStarts
    return h


def dispose_record(system: System, h: int) -> None:
    """TEDispose: the record, its text and the rest of what it owns."""
    h &= ADDRESS_MASK
    heap = system.heap_of(h)
    mem = system.mem
    te = TERec(mem, heap.deref(h))

    def free(x: int) -> None:
        x &= ADDRESS_MASK
        if not x:
            return
        try:
            owner = system.heap_of(x)
        except ValueError:
            return
        if owner.is_handle(x):
            owner.dispose_handle(x)
    sh = te.style_handle & ADDRESS_MASK if te.tx_size == -1 else 0
    p = mem.r32(sh) & ADDRESS_MASK if sh else 0
    if p:
        sr = TEStyleRec(mem, p)
        null = sr.null_style & ADDRESS_MASK
        if null and mem.r32(null) & ADDRESS_MASK:
            free(mem.r32((mem.r32(null) & ADDRESS_MASK) + 4))
        for x in (sr.style_tab, sr.lh_tab, null):
            free(x)
    for x in (sh, te.h_text, te.h_dispatch_rec, h):
        free(x)
    records(system).pop(h, None)


def text_box(qd: QuickDraw, port: GrafPort | None, data: bytes, r: Rect, just: int) -> None:
    """TETextBox: erase r and draw the text in it in the port's font,
    broken into lines as an edit record's are."""
    if port is None:
        return
    style = Style(port.tx_font, port.tx_face, port.tx_size)
    ascent, descent, _, leading = qd.font_info(port)
    lay = Layout(data, [(0, style)], r, just=just, fixed=(ascent + descent + leading, ascent),
                 fonts=qd.fonts)
    lay.recalc()
    clip = Region.rect(r)
    qd.region(port, "Erase", clip)
    saved = port.tx_font, port.tx_face, port.tx_size, port.pn_loc, port.sp_extra
    port.sp_extra = 0
    try:
        for i in range(lay.count):
            if r.top + lay.top(i) >= r.bottom:
                break
            lay.paint_line(qd, port, i, r, clip)
    finally:
        port.tx_font, port.tx_face, port.tx_size, port.pn_loc, port.sp_extra = saved


# =========================================================================
# TextEdit's private scrap (TEScrpHandle and TEScrpLength)
# =========================================================================
def scrap_length(world: World) -> int:
    """TEGetScrapLength.  The length is a word at TEScrpLength, but some
    code writes it as a long."""
    hi, lo = struct.unpack(">HH", world.read(lowmem.TEScrpLength, 4))
    return hi or lo


def set_scrap_length(world: World, n: int) -> None:
    world.write(lowmem.TEScrpLength, struct.pack(">HH", n & 0xFFFF, 0))


def te_scrap(world: World, mem) -> bytes:
    h = world.r32(lowmem.TEScrpHandle) & ADDRESS_MASK
    p = mem.r32(h) & ADDRESS_MASK if h else 0
    return mem.read(p, scrap_length(world)) if p else b""


def set_te_scrap(world: World, heap: Heap, data: bytes) -> bool:
    """The TextEdit scrap's contents, in a handle in heap; False if
    there's no room."""
    h = world.r32(lowmem.TEScrpHandle) & ADDRESS_MASK
    if not h or not heap.is_handle(h):
        h = heap.new_handle(len(data))
        if not h:
            return False
        world.w32(lowmem.TEScrpHandle, h)
    elif heap.is_empty(h):
        if not heap.realloc_handle(h, len(data)):
            return False
    elif heap.handle_size(h) != len(data) and not heap.set_handle_size(h, len(data)):
        return False
    heap.mem.write(heap.deref(h), data)
    set_scrap_length(world, len(data))
    return True
