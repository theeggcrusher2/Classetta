"""
Control Manager data and the standard controls.

A control is a ControlRecord (Inside Macintosh I-317) in a relocatable
block in the application heap, and a ControlHandle is that block's
handle.  A window's controlList links its controls, newest first.  A
Control dereferences its handle every time it's used, because the block
can move.

How a control looks and behaves comes from its definition procedure, a
CDEF resource.  procID is the CDEF's ID times 16 plus a variation.  The
two standard ones are here: CDEF 0 (push buttons, check boxes and radio
buttons) and CDEF 1 (scroll bars).  They draw with QuickDraw in the
owner window's port, in black and white as in Systems 6 and 7.  An
application's own CDEF is 68k code, which the trap glue calls
(macrt.m68k.glue_controls).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from PyQt6.QtCore import QRectF
from PyQt6.QtGui import QPainterPath

from ..memory import (
    ADDRESS_MASK,
    BYTE,
    INTEGER,
    LONG,
    LONGINT,
    RAM,
    RECT,
    Codec,
    from_mac,
)
from . import constants as k
from .qdraw import (
    frame_path,
    oval_path,
    patBic,
    patCopy,
    patXor,
    rect_path,
    round_rect_path,
)
from .quickdraw import BLACK_PAT, GRAY_PAT, GrafPort
from .types import Point, Rect

if TYPE_CHECKING:
    from .qdraw import QuickDraw

TITLE = 40                  # contrlTitle, after the fixed part of the record
ARROW = 16                  # a scroll bar's arrow boxes, and its thumb, are square
BUTTON_CORNER = 16          # a push button's corner diameter
CHECK_BOX = 12              # check box and radio button, square
TITLE_GAP = 17              # from the control's left to its title


class _Field:
    """A ControlRecord field, read through the handle."""

    def __init__(self, offset: int, codec: Codec):
        self.offset = offset
        self.codec = codec

    def __get__(self, c, owner=None):
        if c is None:
            return self
        return self.codec.read(c.mem, c.addr + self.offset)

    def __set__(self, c, value) -> None:
        self.codec.write(c.mem, c.addr + self.offset, value)


class Control:
    next_control = _Field(0, LONG)
    owner = _Field(4, LONG)                 # contrlOwner, a WindowPtr
    rect = _Field(8, RECT)
    vis = _Field(16, BYTE)                  # 255 visible, 0 not
    hilite = _Field(17, BYTE)
    value = _Field(18, INTEGER)
    min = _Field(20, INTEGER)
    max = _Field(22, INTEGER)
    def_proc = _Field(24, LONG)             # CDEF handle; variation in the high byte
    data = _Field(28, LONG)
    action = _Field(32, LONG)
    ref_con = _Field(36, LONGINT)

    def __init__(self, mem: RAM, handle: int):
        self.mem = mem
        self.handle = handle

    def __eq__(self, other) -> bool:
        return isinstance(other, Control) and other.handle == self.handle

    def __hash__(self) -> int:
        return self.handle

    def __repr__(self) -> str:
        return f"<Control ${self.handle:06X} {self.title!r}>"

    @property
    def addr(self) -> int:
        return self.mem.r32(self.handle) & ADDRESS_MASK

    @property
    def title(self) -> str:
        return from_mac(self.mem.r_pstr(self.addr + TITLE))

    @property
    def variation(self) -> int:
        return (self.def_proc >> 24) & 0xF

    @property
    def visible(self) -> bool:
        return self.vis != 0

    @property
    def active(self) -> bool:
        return self.hilite != k.inactiveHilite

    def pin(self, value: int) -> int:
        """value kept between the control's minimum and maximum."""
        lo, hi = self.min, self.max
        return max(lo, min(value, hi)) if hi >= lo else lo


# =========================================================================
# CDEF 0 and CDEF 1
# =========================================================================
class Geometry:
    """Where a scroll bar's parts are."""

    def __init__(self, c: Control):
        r = self.rect = c.rect
        self.vertical = r.height > r.width
        length = r.height if self.vertical else r.width
        # The arrow boxes and the thumb share their frame lines with the
        # bar's and each other's, so the thumb's top travels from the up
        # arrow's last row to the down arrow's first row less a thumb.
        self.travel = length - 3 * ARROW + 2
        span = c.max - c.min
        self.thumb_at = None
        if span > 0 and self.travel >= 0 and c.active:
            self.thumb_at = ARROW - 1 + (c.pin(c.value) - c.min) * self.travel // span

    def along(self, start: int, end: int) -> Rect:
        """A stretch of the bar, start to end along its length, full width."""
        r = self.rect
        if self.vertical:
            return Rect(r.top + start, r.left, r.top + end, r.right)
        return Rect(r.top, r.left + start, r.bottom, r.left + end)

    @property
    def length(self) -> int:
        return self.rect.height if self.vertical else self.rect.width

    @property
    def up(self) -> Rect:
        return self.along(0, ARROW)

    @property
    def down(self) -> Rect:
        return self.along(self.length - ARROW, self.length)

    @property
    def thumb(self) -> Rect | None:
        if self.thumb_at is None:
            return None
        return self.along(self.thumb_at, self.thumb_at + ARROW)

    def offset_of(self, pt: Point) -> int:
        r = self.rect
        return pt.v - r.top if self.vertical else pt.h - r.left


def standard(proc_id: int) -> bool:
    """CDEF 0 or 1, which macrt draws itself."""
    return proc_id >> 4 in (0, 1)


def test(c: Control, proc_id: int, pt: Point) -> int:
    """testCntl: the part of an active, visible control the point is in."""
    if not c.rect.contains(pt):
        return 0
    if proc_id >> 4 == 0:
        return k.inButton if proc_id & 7 == k.pushButProc else k.inCheckBox
    g = Geometry(c)
    at = g.offset_of(pt)
    if at < ARROW:
        return k.inUpButton
    if at >= g.length - ARROW:
        return k.inDownButton
    if g.thumb_at is None:
        return 0
    if at < g.thumb_at:
        return k.inPageUp
    if at >= g.thumb_at + ARROW:
        return k.inPageDown
    return k.inThumb


def thumb_limits(c: Control, start: Point) -> tuple[Rect, Rect, int]:
    """thumbCntl, for a drag that starts at `start`: where the mouse may
    go (limitRect: the thumb from one end of its travel to the other), how
    far it may stray before the drag is off (slopRect), and the axis."""
    g = Geometry(c)
    r = c.rect
    before = (g.thumb_at or 0) - (ARROW - 1)        # pixels it can go back
    after = g.travel - before                       # and on
    if g.vertical:
        limit = Rect(start.v - before, r.left, start.v + after + 1, r.right)
        return limit, r.inset(-24, -24), k.vAxisOnly
    limit = Rect(r.top, start.h - before, r.bottom, start.h + after + 1)
    return limit, r.inset(-24, -24), k.hAxisOnly


def value_at(c: Control, moved: int) -> int:
    """posCntl: the value with the thumb moved `moved` pixels along the bar."""
    g = Geometry(c)
    if g.thumb_at is None or g.travel <= 0:
        return c.value
    at = max(0, min(g.thumb_at - (ARROW - 1) + moved, g.travel))
    span = c.max - c.min
    return c.min + (at * span + g.travel // 2) // g.travel


# The up arrow, in a 16-pixel box whose frame is drawn already.
UP_ARROW = [
    ".......X.......",
    "......X.X......",
    ".....X...X.....",
    "....X.....X....",
    "...X.......X...",
    "..XXXX...XXXX..",
    ".....X...X.....",
    ".....X...X.....",
    ".....X...X.....",
    ".....XXXXX.....",
]
ARROW_TOP = 3               # rows above the bitmap in the box


def _filled(rows: list[str]) -> list[str]:
    """The arrow pressed: black inside its outline."""
    out = []
    for row in rows:
        first, last = row.find("X"), row.rfind("X")
        out.append(row if first < 0 else
                   row[:first] + "X" * (last - first + 1) + row[last + 1:])
    return out


def _arrow_path(box: Rect, direction: str, pressed: bool) -> QPainterPath:
    rows = _filled(UP_ARROW) if pressed else UP_ARROW
    path = QPainterPath()
    size = ARROW
    for y, row in enumerate(rows):
        for x, ch in enumerate(row):
            if ch != "X":
                continue
            # (x, y) in an up arrow; turned for the other directions.
            col, line = x + 0.5, y + ARROW_TOP
            if direction == "down":
                line = size - 1 - line
            elif direction == "left":
                col, line = line, x + 0.5
            elif direction == "right":
                col, line = size - 1 - line, x + 0.5
            path.addRect(QRectF(box.left + int(col), box.top + int(line), 1, 1))
    return path


class Painter:
    """Draws a standard control in its owner's port: the port's pen and
    text settings are put back afterwards."""

    def __init__(self, qd: QuickDraw, port: GrafPort):
        self.qd = qd
        self.port = port

    def fill(self, path: QPainterPath, pat: bytes = BLACK_PAT, mode: int = patCopy) -> None:
        self.qd.fill(self.port, path, pat, mode)

    def erase(self, path: QPainterPath) -> None:
        self.qd.fill(self.port, path, self.port.bk_pat, patCopy)

    def frame(self, shape, r: Rect, width: int = 1) -> None:
        self.fill(frame_path(shape, r, Point(width, width)))

    def text(self, c: Control, h: int, r: Rect, centered: bool) -> Rect:
        """The title at h (or centered in r), its baseline centered in r
        vertically, in the system font or the window's.  Returns where it
        went."""
        port, qd = self.port, self.qd
        saved = (port.pn_loc, port.tx_font, port.tx_face, port.tx_size, port.tx_mode)
        try:
            if not c.variation & k.useWFont:
                port.tx_font, port.tx_face, port.tx_size = 0, 0, 0
            port.tx_mode = k.srcOr
            title = c.title
            width = qd.text_width(port, title)
            ascent, descent, _, _ = qd.font_info(port)
            if centered:
                h = r.left + (r.width - width) // 2
            base = r.top + (r.height - ascent - descent) // 2 + ascent
            port.pn_loc = Point(base, h)
            qd.draw_text(port, title)
            return Rect(base - ascent, h, base + descent, h + width)
        finally:
            port.pn_loc, port.tx_font, port.tx_face, port.tx_size, port.tx_mode = saved

    def dim(self, r: Rect) -> None:
        """Gray out what's in r (an inactive control's title)."""
        self.fill(rect_path(r), GRAY_PAT, patBic)


def draw(qd: QuickDraw, port: GrafPort, c: Control, proc_id: int) -> None:
    """drawCntl for CDEF 0 and 1: the whole control, in its hilite state."""
    if not c.visible:
        return
    p = Painter(qd, port)
    if proc_id >> 4 == 0:
        kind = proc_id & 7
        if kind == k.checkBoxProc or kind == k.radioButProc:
            _draw_check(p, c, kind)
        else:
            _draw_button(p, c)
    else:
        _draw_scroll_bar(p, c)


def _draw_button(p: Painter, c: Control) -> None:
    r = c.rect
    corner = min(BUTTON_CORNER, r.height, r.width)

    def shape(r):
        return round_rect_path(r, corner, corner)
    p.erase(shape(r))
    p.frame(shape, r)
    where = p.text(c, 0, r.inset(1, 1), centered=True)
    if not c.active:
        p.dim(where)
    elif c.hilite:
        p.fill(shape(r.inset(1, 1)), BLACK_PAT, patXor)


def _draw_check(p: Painter, c: Control, kind: int) -> None:
    r = c.rect
    mid = r.top + r.height // 2
    box = Rect(mid - CHECK_BOX // 2, r.left + 2, mid + CHECK_BOX // 2, r.left + 2 + CHECK_BOX)
    shape = rect_path if kind == k.checkBoxProc else oval_path
    p.erase(rect_path(r))
    pressed = c.active and c.hilite == k.inCheckBox
    p.frame(rect_path if kind == k.checkBoxProc else oval_path, box, 2 if pressed else 1)
    if c.value:
        if kind == k.checkBoxProc:
            x = QPainterPath()
            for i in range(CHECK_BOX - 2):
                x.addRect(QRectF(box.left + 1 + i, box.top + 1 + i, 1, 1))
                x.addRect(QRectF(box.right - 2 - i, box.top + 1 + i, 1, 1))
            p.fill(x)
        else:
            p.fill(shape(box.inset(3, 3)))
    where = p.text(c, r.left + TITLE_GAP, r, centered=False)
    if not c.active:
        p.dim(where)


def _draw_scroll_bar(p: Painter, c: Control) -> None:
    g = Geometry(c)
    r = c.rect
    p.frame(rect_path, r)
    inside = r.inset(1, 1)
    if not c.active or g.length < 2 * ARROW:
        p.erase(rect_path(inside))
        return
    up, down = ("up", "down") if g.vertical else ("left", "right")
    for box, direction, part in ((g.up, up, k.inUpButton), (g.down, down, k.inDownButton)):
        p.erase(rect_path(box.inset(1, 1)))
        p.frame(rect_path, box)
        p.fill(_arrow_path(box, direction, c.hilite == part))
    shaft = (g.along(ARROW, g.length - ARROW).inset(1, 0) if g.vertical
             else g.along(ARROW, g.length - ARROW).inset(0, 1))
    thumb = g.thumb
    if thumb is None:
        p.erase(rect_path(shaft))
        return
    p.fill(rect_path(shaft), GRAY_PAT)
    p.erase(rect_path(thumb.inset(1, 1)))
    p.frame(rect_path, thumb)
