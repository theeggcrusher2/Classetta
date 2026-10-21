"""
Applications' own window definition procedures.

The host draws the frames of the windows the System's WDEF 0 and 1
define.  A window whose windowDefProc is the application's own code, a
WDEF resource of its own or a handle to code it made itself (SuperPaint's
palettes are one window shaped so), is that code's to shape and draw, as
on a Mac (Inside Macintosh I-297): FUNCTION WDEF(varCode: INTEGER;
theWindow: WindowPtr; message: INTEGER; param: LONGINT): LONGINT works
out the window's structure and content regions (wCalcRgns), draws its
frame in the Window Manager port (wDraw), and says which part of the
window a point is in (wHit).  The window's view on the host takes the
shape of the structure region, and shows the frame drawn there.

68k code runs on the application's thread, so the host only notes a
window whose frame needs doing (Process.frames_due) and the application
does it at its next trap.

An application's WDEF may leave some of its messages to the System's
(Excel's do), so system_wdef answers them for WDEF 0 and 1 with the
host's frames: the regions they take, drawn in the image wDraw draws in.
"""

from __future__ import annotations

import time
from contextlib import nullcontext

from PyQt6.QtGui import QColor, QImage, QPainter, QRegion

from ..memory import ADDRESS_MASK
from ..toolbox import constants as k
from ..toolbox import lowmem
from ..toolbox.events import ProcessKilled
from ..toolbox.qdraw import ImageSurface, Region, read_rgn, write_rgn
from ..toolbox.system import RTS
from ..toolbox.types import Point
from ..toolbox.windows import WindowRecord, describe_proc
from ..ui import chrome


def entry(t, w: WindowRecord) -> int:
    """Where w's own WDEF starts; 0 if it has none to run."""
    if not w.own_wdef:
        return 0
    h = w.window_def_proc & ADDRESS_MASK
    p = t.mem.r32(h) & ADDRESS_MASK
    if not p:
        res = t.process.resources
        err = res.error
        res.LoadResource(h)             # purged, as a Mac's Window Manager would find it
        res.error = err
        p = t.mem.r32(h) & ADDRESS_MASK
    return p if p and t.mem.r16(p) != RTS else 0


def message(t, w: WindowRecord, msg: int, param: int = 0) -> int:
    """Send w's WDEF a message, in the Window Manager port, holding the
    machine to run it (TrackGoAway waits without it)."""
    at = entry(t, w)
    if not at:
        return 0
    with nullcontext() if t.holding else t.resumed():
        var = t.mem.r32(t.process.partition.a5) & ADDRESS_MASK     # thePort
        saved = t.mem.r32(var)
        t.mem.w32(var, t.mem.r32(lowmem.WMgrPort))
        t.in_wdef += 1
        try:
            return t.call(at, (("h", (w.window_def_proc >> 24) & 0xFF), ("p", w.addr),
                               ("h", msg), ("l", param)), "l")
        finally:
            t.in_wdef -= 1
            t.mem.w32(var, saved)


def _live(t, w: WindowRecord) -> bool:
    return t.system.port_at(w.addr) is w


# =========================================================================
# Shape and frame
# =========================================================================
def refresh(t) -> None:
    """Shape and draw the windows whose frames need it, between two of the
    application's traps."""
    due = t.process.frames_due
    while due:
        w = due.pop()
        if _live(t, w) and entry(t, w):
            shape(t, w)


def shape(t, w: WindowRecord) -> None:
    message(t, w, k.wCalcRgns)
    w.wdef_shaped = True
    if w.visible:
        draw(t, w)
    d = t.tb._d
    d.bridge.post(d.reshape, w)


def draw(t, w: WindowRecord, part: int = 0) -> None:
    """wDraw: the whole frame, or for part (wInGoAway, wInZoomIn or
    wInZoomOut) that box's highlighting turned over, in an image of the
    structure region's box that the window's view shows."""
    mem = t.mem
    struc = w.wdef_struc()
    box = struc.bbox
    if box.empty:
        return
    c = w.content.top_left
    at = (box.left - c.h, box.top - c.v)
    old = w.frame_image
    if (old is not None and old[:2] == at and old[2].width() == box.width
            and old[2].height() == box.height):
        img = old[2].copy()
    else:
        img = QImage(box.width, box.height, QImage.Format.Format_RGB32)
        img.fill(QColor(255, 255, 255))
    wmgr = mem.r32(lowmem.WMgrPort) & ADDRESS_MASK
    port = t.system.port_at(wmgr)
    surfaces = t.tb.qd.surfaces
    before = surfaces.get(wmgr)
    clip = port.clip_rgn if port is not None else 0
    kept = read_rgn(mem, clip) if clip else None
    if clip:
        write_rgn(t.system.heap_of(clip), clip, struc)
    surfaces[wmgr] = ImageSurface(img, box)
    try:
        message(t, w, k.wDraw, part)
    finally:
        if before is None:
            surfaces.pop(wmgr, None)
        else:
            surfaces[wmgr] = before
        if kept is not None:
            write_rgn(t.system.heap_of(clip), clip, kept)
    w.frame_image = (*at, img)


# =========================================================================
# What the Toolbox asks
# =========================================================================
class WindowDefinitions:
    """The Toolbox's way to an application's own WDEFs (Toolbox.wdefs)."""

    def __init__(self, t):
        self.t = t

    def created(self, w: WindowRecord) -> None:
        message(self.t, w, k.wNew)

    def disposing(self, w: WindowRecord) -> None:
        self.t.process.frames_due.discard(w)
        message(self.t, w, k.wDispose)

    def hit(self, w: WindowRecord, pt: Point) -> int:
        return message(self.t, w, k.wHit, (pt.v & 0xFFFF) << 16 | (pt.h & 0xFFFF))

    def track(self, w: WindowRecord, part: int) -> bool:
        """TrackGoAway's or TrackBox's work: the box highlighted while the
        mouse is in it, until the button comes up; True if it came up
        there."""
        t = self.t
        d = t.tb._d
        inside = False
        while True:
            now = self.hit(w, d.input.mac_pos) == part
            if now != inside:
                inside = now
                self._toggle(w, part)
            if not d.input.down:
                break
            if t.process.events.killed:
                raise ProcessKilled()
            if t.holding:
                t.sleep(1)
            else:
                time.sleep(1 / 60)
        if inside:
            self._toggle(w, part)
        return inside

    def _toggle(self, w: WindowRecord, part: int) -> None:
        t = self.t
        with nullcontext() if t.holding else t.resumed():
            draw(t, w, part)
        d = t.tb._d
        d.bridge.post(d.reshape, w)


# =========================================================================
# The System's WDEF 0 and 1
# =========================================================================
def system_wdef(t, def_id: int, var: int, w_ptr: int, msg: int, param: int) -> int:
    w = t.window(w_ptr & ADDRESS_MASK)
    if w is None:
        return 0
    if msg == k.wDraw and param in (k.wInGoAway, k.wInZoomIn, k.wInZoomOut):
        w.wdef_pressed = 0 if w.wdef_pressed else param
    elif msg == k.wDraw:
        w.wdef_pressed = 0
    f = chrome.Frame.for_window(w, definition=describe_proc(def_id << 4 | var & 0xF),
                                pressed=w.wdef_pressed)
    c = w.content
    l, top, _, _ = chrome.insets(f)
    sw, sh = chrome.structure_size(f, c.width, c.height)
    x0, y0 = c.left - l, c.top - top
    if msg == k.wCalcRgns:
        inner = chrome.content_mask(f, sw, sh)
        cont = Region.rect(c) if inner is None else _region(inner).offset(c.left, c.top)
        struc = _region(chrome.mask(f, sw, sh)).offset(x0, y0)
        for h, rgn in ((w.cont_rgn, cont), (w.struc_rgn, struc)):
            write_rgn(t.system.heap_of(h), h, rgn)
    elif msg == k.wDraw and w.visible:
        surface = t.tb.qd.surfaces.get(t.mem.r32(lowmem.WMgrPort) & ADDRESS_MASK)
        if isinstance(surface, ImageSurface):    # the image draw() has it draw in
            with surface.painter() as p:
                p.translate(x0, y0)
                p.setClipRegion(chrome.mask(f, sw, sh))
                chrome.paint(p, f, sw, sh)
    elif msg == k.wHit:
        return chrome.hit(f, sw, sh, _s16(param) - x0, _s16(param >> 16) - y0)
    return 0


def _s16(n: int) -> int:
    n &= 0xFFFF
    return n - 0x10000 if n & 0x8000 else n


def _region(q: QRegion) -> Region:
    box = q.boundingRect()
    if box.isEmpty():
        return Region()
    img = QImage(box.width(), box.height(), QImage.Format.Format_Grayscale8)
    img.fill(0)
    p = QPainter(img)
    p.translate(-box.x(), -box.y())
    p.setClipRegion(q)
    p.fillRect(box, QColor(255, 255, 255))
    p.end()
    return Region.from_mask(img, box.x(), box.y())
