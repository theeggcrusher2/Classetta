"""
QuickDraw data in guest memory: GrafPorts, the QuickDraw globals in an
application's A5 world, and regions.

The drawing is in qdraw.  Each window port has a Backing, a QImage that
the drawing calls paint into and the window shows; other ports draw into
their portBits.  Guests draw on their own thread, so every access to a
Backing goes through its lock.
"""

from __future__ import annotations

import threading
from contextlib import contextmanager

from PyQt6.QtGui import QColor, QImage, QPainter

from ..memory import LONG, LONGINT, BYTE, INTEGER, PATTERN, POINT, RAM, RECT, Field, Record
from .constants import blackColor, patCopy, srcOr, whiteColor
from .memmgr import Heap
from .types import Point, Rect

BLACK = QColor("#000000")
WHITE = QColor("#FFFFFF")

WHITE_PAT = bytes(8)
BLACK_PAT = b"\xFF" * 8
GRAY_PAT = b"\xAA\x55" * 4
LT_GRAY_PAT = b"\x88\x22" * 4
DK_GRAY_PAT = b"\x77\xDD" * 4

# The arrow cursor: 16 rows of data, 16 of mask, then the hot spot (v, h).
ARROW_DATA = (0x0000, 0x4000, 0x6000, 0x7000, 0x7800, 0x7C00, 0x7E00, 0x7F00,
              0x7F80, 0x7C00, 0x6C00, 0x4600, 0x0600, 0x0300, 0x0300, 0x0000)
ARROW_MASK = (0xC000, 0xE000, 0xF000, 0xF800, 0xFC00, 0xFE00, 0xFF00, 0xFF80,
              0xFFC0, 0xFFE0, 0xFE00, 0xEF00, 0xCF00, 0x8780, 0x0780, 0x0380)
ARROW_HOT_SPOT = Point(1, 1)

WIDE_OPEN = Rect(-32767, -32767, 32767, 32767)


class BitMap:
    """A BitMap's three fields: baseAddr, rowBytes, bounds."""

    def __init__(self, base: int, row_bytes: int, bounds: Rect):
        self.base = base
        self.row_bytes = row_bytes
        self.bounds = bounds


# ---------------------------------------------------------------------------
# QuickDraw globals.  0(A5) holds the address of thePort; the other globals
# sit below it (Inside Macintosh I-163).  InitGraf is handed that address.
# ---------------------------------------------------------------------------
QD_THE_PORT = 0
QD_WHITE = -8
QD_BLACK = -16
QD_GRAY = -24
QD_LT_GRAY = -32
QD_DK_GRAY = -40
QD_ARROW = -108
QD_SCREEN_BITS = -122
QD_RAND_SEED = -126
QD_GLOBALS_SIZE = 206       # including 76 private bytes below randSeed


def init_qd_globals(mem: RAM, the_port: int, screen: BitMap) -> None:
    """What InitGraf puts in an application's QuickDraw globals."""
    mem.fill(the_port + 4 - QD_GLOBALS_SIZE, QD_GLOBALS_SIZE)
    mem.w32(the_port + QD_THE_PORT, 0)
    for offset, pat in ((QD_WHITE, WHITE_PAT), (QD_BLACK, BLACK_PAT), (QD_GRAY, GRAY_PAT),
                        (QD_LT_GRAY, LT_GRAY_PAT), (QD_DK_GRAY, DK_GRAY_PAT)):
        mem.write(the_port + offset, pat)
    arrow = the_port + QD_ARROW
    for i, (row, mask) in enumerate(zip(ARROW_DATA, ARROW_MASK)):
        mem.w16(arrow + 2 * i, row)
        mem.w16(arrow + 32 + 2 * i, mask)
    mem.w_point(arrow + 64, ARROW_HOT_SPOT)
    bits = the_port + QD_SCREEN_BITS
    mem.w32(bits, screen.base)
    mem.w16(bits + 4, screen.row_bytes)
    mem.w_rect(bits + 6, screen.bounds)
    mem.w32(the_port + QD_RAND_SEED, 1)


# ---------------------------------------------------------------------------
# Regions.  A region is a handle to rgnSize, rgnBBox, then (for anything
# but a rectangle) the region data.  These make rectangular ones; qdraw
# reads and writes the rest.
# ---------------------------------------------------------------------------
RECT_RGN_SIZE = 10


def new_rgn(heap: Heap, r: Rect = Rect(0, 0, 0, 0)) -> int:
    h = heap.new_handle(RECT_RGN_SIZE)
    if h:
        set_rect_rgn(heap, h, r)
    return h


def set_rect_rgn(heap: Heap, h: int, r: Rect) -> None:
    if r.empty:
        r = Rect(0, 0, 0, 0)
    if heap.handle_size(h) != RECT_RGN_SIZE:
        heap.set_handle_size(h, RECT_RGN_SIZE)
    p = heap.deref(h)
    heap.mem.w16(p, RECT_RGN_SIZE)
    heap.mem.w_rect(p + 2, r)


def rgn_bbox(mem: RAM, h: int) -> Rect:
    return mem.r_rect((mem.r32(h) & 0xFFFFFF) + 2)


# ---------------------------------------------------------------------------
# GrafPort
# ---------------------------------------------------------------------------
class GrafPort(Record):
    SIZE = 108

    device = Field(0, INTEGER)
    port_bits_base = Field(2, LONG)
    port_bits_row_bytes = Field(6, INTEGER)
    port_bits_bounds = Field(8, RECT)
    port_rect = Field(16, RECT)
    vis_rgn = Field(24, LONG)
    clip_rgn = Field(28, LONG)
    bk_pat = Field(32, PATTERN)
    fill_pat = Field(40, PATTERN)
    pn_loc = Field(48, POINT)
    pn_size = Field(52, POINT)
    pn_mode = Field(56, INTEGER)
    pn_pat = Field(58, PATTERN)
    pn_vis = Field(66, INTEGER)
    tx_font = Field(68, INTEGER)
    tx_face = Field(70, BYTE)
    tx_mode = Field(72, INTEGER)
    tx_size = Field(74, INTEGER)
    sp_extra = Field(76, LONGINT)
    fg_color = Field(80, LONGINT)
    bk_color = Field(84, LONGINT)
    colr_bit = Field(88, INTEGER)
    pat_stretch = Field(90, INTEGER)
    pic_save = Field(92, LONG)
    rgn_save = Field(96, LONG)
    poly_save = Field(100, LONG)
    graf_procs = Field(104, LONG)

    def __init__(self, mem: RAM, addr: int):
        super().__init__(mem, addr)
        self.backing: Backing | None = None

    def open(self, heap: Heap, screen: BitMap) -> None:
        """OpenPort: a port onto the whole screen with default settings."""
        self.mem.fill(self.addr, GrafPort.SIZE)
        self.port_bits_base = screen.base
        self.port_bits_row_bytes = screen.row_bytes
        self.port_bits_bounds = screen.bounds
        self.port_rect = screen.bounds
        self.vis_rgn = new_rgn(heap, screen.bounds)
        self.clip_rgn = new_rgn(heap, WIDE_OPEN)
        self.bk_pat = WHITE_PAT
        self.fill_pat = BLACK_PAT
        self.pn_size = Point(1, 1)
        self.pn_mode = patCopy
        self.pn_pat = BLACK_PAT
        self.tx_mode = srcOr
        self.fg_color = blackColor
        self.bk_color = whiteColor

    def close(self, heap: Heap) -> None:
        """ClosePort: release the regions, not the record."""
        for h in (self.vis_rgn, self.clip_rgn):
            heap.dispose_handle(h)
        self.vis_rgn = self.clip_rgn = 0

    def local_to_global(self, pt: Point) -> Point:
        b = self.port_bits_bounds
        return Point(pt.v - b.top, pt.h - b.left)

    def global_to_local(self, pt: Point) -> Point:
        b = self.port_bits_bounds
        return Point(pt.v + b.top, pt.h + b.left)


class Backing:
    """Where a window port's drawing goes until QuickDraw draws through
    portBits.  It keeps the pixels in `area`, relative to the top left of
    the portRect: the whole port, but for a window that reaches far off
    the screen (WindowRecord.kept)."""

    def __init__(self, area: Rect, port: Rect | None = None):
        self.lock = threading.RLock()
        self.area = area
        self.port = port or area        # the portRect, relative to its top left
        self.image = self._blank(area.width, area.height)

    @staticmethod
    def _blank(width: int, height: int) -> QImage:
        image = QImage(max(width, 1), max(height, 1), QImage.Format.Format_RGB32)
        image.fill(WHITE)
        return image

    def fit(self, area: Rect, port: Rect) -> bool:
        """Keep the pixels in area of the port, now port, instead: those the
        two areas share as they were.  True if area has pixels the port had
        before but this didn't keep, which need drawing again."""
        with self.lock:
            had = self.port.sect(area)
            dropped = not had.empty and had.sect(self.area) != had
            self.port = port
            if area != self.area:
                old, before = self.image, self.area
                self.image = self._blank(area.width, area.height)
                p = QPainter(self.image)
                p.drawImage(before.left - area.left, before.top - area.top, old)
                p.end()
                self.area = area
            return dropped

    def clear(self) -> None:
        """All white, as the Window Manager leaves a window's content when
        it shows it."""
        with self.lock:
            self.image.fill(WHITE)

    def scroll(self, dh: int, dv: int) -> None:
        """Move the pixels kept by (dh, dv), what's left behind white."""
        with self.lock:
            old = self.image
            self.image = self._blank(old.width(), old.height())
            p = QPainter(self.image)
            p.drawImage(dh, dv, old)
            p.end()

    @contextmanager
    def painter(self):
        """A painter whose (0, 0) is the top left of the portRect."""
        with self.lock:
            p = QPainter(self.image)
            p.setRenderHint(QPainter.RenderHint.Antialiasing, False)
            p.translate(-self.area.left, -self.area.top)
            try:
                yield p
            finally:
                p.end()
