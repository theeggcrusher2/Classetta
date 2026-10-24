"""
Toolbox traps: QuickDraw, the Font, Window, Menu, Event and Resource
Managers, and the Toolbox Utilities.  Each turns the guest's pointers and
handles into the Python values the Toolbox (macrt.toolbox.api) takes, and
its results back.  Names and arguments are as in Inside Macintosh; see
dispatch for how they're called.

Calls that wait for the user (WaitNextEvent, MenuSelect, DragWindow...)
let the other applications run meanwhile.
"""

from __future__ import annotations

import math
import struct
import threading
from contextlib import contextmanager

from PyQt6.QtGui import QImage

from ..memory import ADDRESS_MASK, from_mac, to_mac
from ..toolbox import constants as k
from ..toolbox import lowmem, sane
from ..toolbox.api import CHICAGO_SYMBOLS, IN_FRONT
from ..toolbox.bitfont import font_family, font_id
from ..toolbox.events import MODIFIER_KEYS, tick_count, to_next_tick
from ..toolbox.memmgr import noErr
from ..toolbox.pict import pack_bits, unpack_bits_from
from ..toolbox.menus import MenuInfo, MenuItem, parse_menu_items
from ..toolbox.qdraw import (STANDARD_FAMILIES, MemorySurface, Region, read_poly, read_rgn,
                             write_poly, write_rgn)
from ..toolbox.quickdraw import (GRAY_PAT, QD_RAND_SEED, WIDE_OPEN, GrafPort, init_qd_globals,
                                 new_rgn, set_rect_rgn)
from ..toolbox.resources import Entry, ResourceManager, ResourceMap
from ..toolbox.types import Point, Rect
from ..toolbox.windows import WindowRecord
from ..ui import keymap
from . import glue_desk, glue_sony, wdefs
from .dispatch import RESERR_FLAGS, SIGNATURES, raw, trap
from .glue_controls import _s16, drag_outline, kill_controls, scratch

EMPTY = Rect(0, 0, 0, 0)
paramErr = -50
pixmapTooDeepErr = -148


def _noop(name: str):
    """A trap that does nothing here: the host does it, or it doesn't apply."""
    def call(t, *args):
        pass
    call.__name__ = name
    return call


def _menu_handle(m: MenuInfo | None) -> int:
    return m.handle if m is not None else 0


def _char(code: int) -> str:
    return bytes([code & 0xFF]).decode("mac_roman")


def _the_port_var(t) -> int:
    """Where the QuickDraw global thePort is: 0(A5) points at it."""
    return t.mem.r32(t.process.partition.a5) & ADDRESS_MASK


def _port(t) -> GrafPort | None:
    p = t.mem.r32(_the_port_var(t)) & ADDRESS_MASK
    return t.system.port_at(p) if p else None


def _custom_proc(t, port: GrafPort, offset: int, key: int) -> int:
    """The port's own procedure at offset in its QDProcs, or 0 if it has
    none there or it's the standard one."""
    procs = port.graf_procs & ADDRESS_MASK
    if not procs:
        return 0
    proc = t.mem.r32(procs + offset) & ADDRESS_MASK
    return 0 if proc in (0, t.machine.stub_for(key)) else proc


# =========================================================================
# QuickDraw: setting up
# =========================================================================
@trap
def InitGraf(t, globals_ptr):
    init_qd_globals(t.mem, globals_ptr, t.system.screen_bits)
    # at 0(A5), the A5 the program runs with: Fokker Triplane moves its A5
    # world below $70000 before it calls this, and a desk accessory finds
    # the globals there; the rest of QuickDraw looks for them there now too
    a5 = t.cpu.a(5) & ADDRESS_MASK
    t.process.partition.a5 = a5
    t.mem.w32(a5, globals_ptr)
    t.mem.w8(lowmem.QDExist, 0)


@trap
def OpenPort(t, p):
    port = GrafPort(t.mem, p)
    port.open(t.process.heap, t.system.screen_bits)
    t.system.register(port)
    t.mem.w32(_the_port_var(t), p)


@trap
def InitPort(t, p):
    port = t.system.port_at(p) or GrafPort(t.mem, p)
    vis, clip = port.vis_rgn, port.clip_rgn
    screen = t.system.screen_bits
    port.open(t.process.heap, screen)
    try:
        heaps = (t.system.heap_of(vis), t.system.heap_of(clip)) if vis and clip else None
    except ValueError:          # not handles: a record that was never opened
        heaps = None
    if heaps:                   # keep its regions, set to the screen and wide open (IM I-163)
        t.process.heap.dispose_handle(port.vis_rgn)
        t.process.heap.dispose_handle(port.clip_rgn)
        port.vis_rgn, port.clip_rgn = vis, clip
        set_rect_rgn(heaps[0], vis, screen.bounds)
        set_rect_rgn(heaps[1], clip, WIDE_OPEN)
    t.system.register(port)
    t.mem.w32(_the_port_var(t), p)


@trap
def ClosePort(t, p):
    port = t.system.port_at(p)
    if port is not None and not isinstance(port, WindowRecord):
        port.close(t.system.heap_of(port.vis_rgn) if port.vis_rgn else t.process.heap)
        t.system.unregister(port)


@trap
def SetPort(t, p):
    t.mem.w32(_the_port_var(t), p)


@trap
def GetPort(t, pp):
    t.w32(pp, t.mem.r32(_the_port_var(t)))


@trap
def GetWMgrPort(t, pp):
    t.w32(pp, t.mem.r32(lowmem.WMgrPort))


@trap
def SetOrigin(t, h, v):
    port = _port(t)
    if port is None:
        return
    r = port.port_rect
    dh, dv = h - r.left, v - r.top
    port.port_rect = r.offset(dh, dv)
    port.port_bits_bounds = port.port_bits_bounds.offset(dh, dv)
    if port.vis_rgn:
        _set_rgn(t, port.vis_rgn, _rgn(t, port.vis_rgn).offset(dh, dv))


@trap
def PortSize(t, width, height):
    port = _port(t)
    if port is not None:
        r = port.port_rect
        port.port_rect = Rect(r.top, r.left, r.top + height, r.left + width)


@trap
def MovePortTo(t, left, top):
    t.tb.MovePortTo(left, top)


@trap
def GlobalToLocal(t, p):
    port = _port(t)
    if port is not None:
        t.mem.w_point(p, port.global_to_local(t.mem.r_point(p)))


@trap
def LocalToGlobal(t, p):
    port = _port(t)
    if port is not None:
        t.mem.w_point(p, port.local_to_global(t.mem.r_point(p)))


# =========================================================================
# QuickDraw: the pen and text settings, which live in the port
# =========================================================================
def _set(t, field: str, value) -> None:
    port = _port(t)
    if port is not None:
        setattr(port, field, value)


@trap
def MoveTo(t, h, v):
    t.tb.MoveTo(h, v)


@trap
def Move(t, dh, dv):
    t.tb.Move(dh, dv)


@trap
def LineTo(t, h, v):
    t.tb.LineTo(h, v)


@trap
def Line(t, dh, dv):
    t.tb.Line(dh, dv)


@trap
def GetPen(t, p):
    port = _port(t)
    if port is not None:
        t.mem.w_point(p, port.pn_loc)


@trap
def PenSize(t, width, height):
    _set(t, "pn_size", Point(height, width))


@trap
def PenMode(t, mode):
    _set(t, "pn_mode", mode)


@trap
def PenPat(t, p):
    _set(t, "pn_pat", t.mem.read(p, 8))


@trap
def BackPat(t, p):
    _set(t, "bk_pat", t.mem.read(p, 8))


@trap
def PenNormal(t):
    port = _port(t)
    if port is not None:
        port.pn_size = Point(1, 1)
        port.pn_mode = k.patCopy
        port.pn_pat = b"\xFF" * 8


@trap
def HidePen(t):
    port = _port(t)
    if port is not None:
        port.pn_vis = port.pn_vis - 1


@trap
def ShowPen(t):
    port = _port(t)
    if port is not None:
        port.pn_vis = port.pn_vis + 1


PEN_STATE = 18      # pnLoc, pnSize, pnMode, pnPat


@trap
def GetPenState(t, p):
    port = _port(t)
    if port is not None:
        t.mem.write(p, t.mem.read(port.addr + 48, 4) + t.mem.read(port.addr + 52, 14))


@trap
def SetPenState(t, p):
    port = _port(t)
    if port is not None:
        t.mem.write(port.addr + 48, t.mem.read(p, PEN_STATE))


@trap
def TextFont(t, font):
    _set(t, "tx_font", font)


@trap
def TextFace(t, face):
    _set(t, "tx_face", face)


@trap
def TextMode(t, mode):
    _set(t, "tx_mode", mode)


@trap
def TextSize(t, size):
    _set(t, "tx_size", size)


@trap
def SpaceExtra(t, extra):
    _set(t, "sp_extra", extra)


@trap
def ForeColor(t, color):
    _set(t, "fg_color", color)


@trap
def BackColor(t, color):
    _set(t, "bk_color", color)


# =========================================================================
# QuickDraw: text
# =========================================================================
# Each leaves the Font Manager's output record in low memory, as the
# FMSwapFont QuickDraw calls first does.  A port with its own textProc or
# txMeasProc has its text drawn or measured by it, as QuickDraw calls
# them in place of StdText and StdTxMeas: Macintosh Pascal's draw its
# program's formatting bytes as bold keywords and indents.
TEXT_PROC, TX_MEAS_PROC = 0, 40         # in QDProcs
StdText, StdTxMeas = 0xA882, 0xA8ED
UNITY = 0x00010001                      # the Point (1, 1): numer and denom, not scaled


def text_procs(t, port: GrafPort | None) -> tuple[int, int]:
    """The port's own textProc and txMeasProc, 0 for the standard ones."""
    if port is None:
        return 0, 0
    return _custom_proc(t, port, TEXT_PROC, StdText), _custom_proc(t, port, TX_MEAS_PROC, StdTxMeas)


def draw_with(t, proc: int, p: int, count: int) -> None:
    """A textProc draws count characters at p at the pen."""
    t.call(proc, (("h", count), ("p", p), ("l", UNITY), ("l", UNITY)))


def measure_with(t, proc: int, p: int, count: int) -> int:
    """What a txMeasProc measures count characters at p as: the width it
    returns, scaled by the numer and denom it leaves.  They and the
    FontInfo it fills in are on the stack."""
    cpu = t.cpu
    ctx = cpu.context()
    cpu.sp = var = cpu.sp - 16
    t.mem.write(var, struct.pack(">LL8x", UNITY, UNITY))
    width = _s16(t.call(proc, (("h", count), ("p", p), ("p", var), ("p", var + 4),
                               ("p", var + 8)), "h"))
    numer, denom = t.mem.r16(var + 2), t.mem.r16(var + 6)
    cpu.restore(ctx)
    return width * numer // denom if numer and denom and numer != denom else width


def _draw(t, text: bytes, at: int | None = None) -> None:
    """text drawn, by the port's textProc if it has one; at is where it is
    in memory, None for DrawChar's character, which goes on the stack."""
    _swap_port_font(t)
    proc = text_procs(t, _port(t))[0]
    if proc:
        with _in_memory(t, text, at) as p:
            draw_with(t, proc, p, len(text))
    elif text:
        t.tb.DrawString(from_mac(text))


def _width(t, text: bytes, at: int | None = None) -> int:
    _swap_port_font(t)
    proc = text_procs(t, _port(t))[1]
    if proc:
        with _in_memory(t, text, at) as p:
            return measure_with(t, proc, p, len(text))
    return t.tb.StringWidth(from_mac(text)) if text else 0


@contextmanager
def _in_memory(t, text: bytes, at: int | None):
    """Where text is for a port's own procs: at, or on the stack."""
    if at is not None:
        yield at
        return
    cpu = t.cpu
    ctx = cpu.context()
    cpu.sp = cpu.sp - 2
    t.mem.write(cpu.sp, text)
    try:
        yield cpu.sp
    finally:
        cpu.restore(ctx)


def _text(t, p: int, count: int) -> bytes:
    return t.mem.read(p, count) if p and count > 0 else b""


@trap
def DrawString(t, p):
    _draw(t, _text(t, p + 1, t.mem.r8(p) if p else 0), p + 1)


@trap
def DrawChar(t, ch):
    _draw(t, bytes([ch & 0xFF]))


@trap
def DrawText(t, p, first, count):
    _draw(t, _text(t, p + first, count), p + first)


@trap
def StringWidth(t, p):
    return _width(t, _text(t, p + 1, t.mem.r8(p) if p else 0), p + 1)


@trap
def CharWidth(t, ch):
    return _width(t, bytes([ch & 0xFF]))


@trap
def TextWidth(t, p, first, count):
    return _width(t, _text(t, p + first, count), p + first)


@trap
def MeasureText(t, count, p, locs):
    """Where the pen is after each of count characters at p, as TextWidth
    measures them: count + 1 integers at locs, 0 first.  The Oregon Trail
    breaks its messages into lines with it."""
    if count < 0:
        return
    _swap_port_font(t)
    at = t.tb.MeasureText(from_mac(t.mem.read(p, count))) if count else [0]
    t.mem.write(locs, struct.pack(f">{len(at)}H", *(x & 0xFFFF for x in at)))


@trap
def GetFontInfo(t, p):
    _swap_port_font(t)
    t.mem.write(p, struct.pack(">hhhh", *t.tb.GetFontInfo()))


# =========================================================================
# QuickDraw: shapes
# =========================================================================
def _shape(verb: str, kind: str):
    def draw(t, r, *args):
        pat = t.mem.read(args[-1], 8) if verb == "Fill" else None
        t.tb.shape(verb, kind, t.rect(r), args[:2], pat)
    draw.__name__ = verb + kind
    return draw


for _verb in ("Frame", "Paint", "Erase", "Invert", "Fill"):
    for _kind in ("Rect", "Oval", "RoundRect", "Arc"):
        trap(_shape(_verb, _kind))


# =========================================================================
# QuickDraw: regions
# =========================================================================
def _rgn(t, h: int) -> Region:
    return read_rgn(t.mem, h)


def _set_rgn(t, h: int, rgn: Region | Rect) -> None:
    if h:
        write_rgn(t.system.heap_of(h), h, rgn if isinstance(rgn, Region) else Region.rect(rgn))


@trap
def NewRgn(t):
    return new_rgn(t.process.heap)


@trap
def DisposeRgn(t, h):
    if h:
        t.system.heap_of(h).dispose_handle(h)


@trap
def OpenRgn(t):
    port = _port(t)
    if port is not None:
        t.tb.qd.open_rgn(port)


@trap
def CloseRgn(t, h):
    port = _port(t)
    _set_rgn(t, h, t.tb.qd.close_rgn(port) if port is not None else Region())


@trap
def SetEmptyRgn(t, h):
    _set_rgn(t, h, Region())


@trap
def RectRgn(t, h, r):
    _set_rgn(t, h, t.rect(r))


@trap
def SetRectRgn(t, h, left, top, right, bottom):
    _set_rgn(t, h, Rect(top, left, bottom, right))


@trap
def CopyRgn(t, src, dst):
    _set_rgn(t, dst, _rgn(t, src))


@trap
def OffsetRgn(t, h, dh, dv):
    _set_rgn(t, h, _rgn(t, h).offset(dh, dv))


@trap
def InsetRgn(t, h, dh, dv):
    _set_rgn(t, h, _rgn(t, h).inset(dh, dv))


@trap
def SectRgn(t, a, b, dst):
    _set_rgn(t, dst, _rgn(t, a).sect(_rgn(t, b)))


@trap
def UnionRgn(t, a, b, dst):
    _set_rgn(t, dst, _rgn(t, a).union(_rgn(t, b)))


@trap
def DiffRgn(t, a, b, dst):
    _set_rgn(t, dst, _rgn(t, a).diff(_rgn(t, b)))


@trap
def XorRgn(t, a, b, dst):
    _set_rgn(t, dst, _rgn(t, a).xor(_rgn(t, b)))


@trap
def PtInRgn(t, pt, h):
    return _rgn(t, h).contains(pt)


@trap
def RectInRgn(t, r, h):
    return not _rgn(t, h).sect(Region.rect(t.rect(r))).empty


@trap
def EqualRgn(t, a, b):
    return _rgn(t, a) == _rgn(t, b)


@trap
def EmptyRgn(t, h):
    return _rgn(t, h).empty


def _draw_rgn(verb: str):
    def draw(t, h, *pat):
        t.tb.qd.region(_port(t), verb, _rgn(t, h), t.mem.read(pat[0], 8) if pat else None)
    draw.__name__ = verb + "Rgn"
    return draw


for _verb in ("Frame", "Paint", "Erase", "Invert", "Fill"):
    trap(_draw_rgn(_verb))


@trap
def ClipRect(t, r):
    port = _port(t)
    if port is not None:
        _set_rgn(t, port.clip_rgn, t.rect(r))


@trap
def GetClip(t, h):
    port = _port(t)
    if port is not None:
        _set_rgn(t, h, _rgn(t, port.clip_rgn))


@trap
def SetClip(t, h):
    port = _port(t)
    if port is not None:
        _set_rgn(t, port.clip_rgn, _rgn(t, h))


@trap
def BitMapToRegion(t, h, bm):
    """The 1 bits of a BitMap, as a region in its coordinates."""
    base, row_bytes, bounds = t.mem.r32(bm), t.mem.r16(bm + 4), t.mem.r_rect(bm + 6)
    surface = MemorySurface.make(t.mem, base, row_bytes, bounds)
    if surface is None:
        return pixmapTooDeepErr if row_bytes & 0x8000 else paramErr
    img = surface.image(bounds).convertToFormat(QImage.Format.Format_Grayscale8)
    img.invertPixels()                                  # black, the 1 bits, non-zero
    _set_rgn(t, h, Region.from_mask(img, bounds.left, bounds.top))
    return noErr


# =========================================================================
# QuickDraw: polygons
# =========================================================================
@trap
def OpenPoly(t):
    port = _port(t)
    h = t.process.heap.new_handle(10, clear=True)
    if h and port is not None:
        t.mem.w16(t.process.heap.deref(h), 10)
        t.tb.qd.open_poly(port, h)
    return h


@trap
def ClosePoly(t):
    port = _port(t)
    if port is not None:
        t.tb.qd.close_poly(port)


@trap
def KillPoly(t, h):
    if h:
        t.system.heap_of(h).dispose_handle(h)


@trap
def OffsetPoly(t, h, dh, dv):
    if h:
        write_poly(t.system.heap_of(h), h, [pt.offset(dh, dv) for pt in read_poly(t.mem, h)])


@trap
def FramePoly(t, h):
    t.tb.qd.frame_poly(_port(t), read_poly(t.mem, h))


def _fill_poly(verb: str):
    def fill(t, h, *pat):
        t.tb.qd.poly(_port(t), verb, read_poly(t.mem, h), t.mem.read(pat[0], 8) if pat else None)
    fill.__name__ = verb + "Poly"
    return fill


for _verb in ("Paint", "Erase", "Invert", "Fill"):
    trap(_fill_poly(_verb))


# =========================================================================
# QuickDraw: bits
# =========================================================================
@trap
def SetPortBits(t, bm):
    port = _port(t)
    if port is not None:
        t.mem.write(port.addr + 2, t.mem.read(bm, 14))


@trap
def CopyBits(t, src, dst, src_r, dst_r, mode, mask_rgn):
    mask = _rgn(t, mask_rgn) if mask_rgn else None
    t.tb.qd.copy_bits(src, dst, t.rect(src_r), t.rect(dst_r), mode, mask, _port(t))


@trap
def ScrollRect(t, r, dh, dv, update_rgn):
    _set_rgn(t, update_rgn, t.tb.ScrollRect(t.rect(r), dh, dv))


@trap
def GetPixel(t, h, v):
    port = _port(t)
    return t.tb.qd.get_pixel(port, Point(v, h)) if port is not None else False


# =========================================================================
# QuickDraw: mapping from one rectangle to another
# =========================================================================
def _map(n: int, a0: int, a1: int, b0: int, b1: int) -> int:
    return b0 + (n - a0) * (b1 - b0) // (a1 - a0) if a1 != a0 else b0


def _map_pt(pt: Point, a: Rect, b: Rect) -> Point:
    return Point(_map(pt.v, a.top, a.bottom, b.top, b.bottom),
                 _map(pt.h, a.left, a.right, b.left, b.right))


@trap
def MapPt(t, p, src, dst):
    t.set_point(p, _map_pt(t.point(p), t.rect(src), t.rect(dst)))


@trap
def MapRect(t, r, src, dst):
    a, b = t.rect(src), t.rect(dst)
    x = t.rect(r)
    tl = _map_pt(Point(x.top, x.left), a, b)
    br = _map_pt(Point(x.bottom, x.right), a, b)
    t.set_rect(r, Rect(tl.v, tl.h, br.v, br.h))


@trap
def ScalePt(t, p, src, dst):
    a, b = t.rect(src), t.rect(dst)
    pt = t.point(p)
    h = pt.h * b.width // a.width if a.width else pt.h
    v = pt.v * b.height // a.height if a.height else pt.v
    t.set_point(p, Point(max(v, 1), max(h, 1)))


@trap
def MapRgn(t, h, src, dst):
    a, b = t.rect(src), t.rect(dst)
    bands = []
    for top, bottom, xs in _rgn(t, h).bands:
        top, bottom = (_map(y, a.top, a.bottom, b.top, b.bottom) for y in (top, bottom))
        bands.append((top, bottom, tuple(_map(x, a.left, a.right, b.left, b.right) for x in xs)))
    _set_rgn(t, h, Region([band for band in bands if band[1] > band[0]]))


@trap
def MapPoly(t, h, src, dst):
    if h:
        a, b = t.rect(src), t.rect(dst)
        write_poly(t.system.heap_of(h), h, [_map_pt(pt, a, b) for pt in read_poly(t.mem, h)])


# =========================================================================
# QuickDraw: rectangle and point arithmetic
# =========================================================================
def _sect(a: Rect, b: Rect) -> Rect | None:
    r = Rect(max(a.top, b.top), max(a.left, b.left), min(a.bottom, b.bottom),
             min(a.right, b.right))
    return None if r.empty else r


def _union(a: Rect, b: Rect) -> Rect:
    if a.empty:
        return b
    if b.empty:
        return a
    return Rect(min(a.top, b.top), min(a.left, b.left), max(a.bottom, b.bottom),
                max(a.right, b.right))


@trap
def SetRect(t, r, left, top, right, bottom):
    t.set_rect(r, Rect(top, left, bottom, right))


@trap
def OffsetRect(t, r, dh, dv):
    t.set_rect(r, t.rect(r).offset(dh, dv))


@trap
def InsetRect(t, r, dh, dv):
    # Only the sides move: the ROM leaves a rectangle inset to nothing as
    # it is, not (0,0,0,0) as Inside Macintosh says, and PhrazeCraze
    # insets one to nothing and out again to turn over a letter.
    t.set_rect(r, t.rect(r).inset(dh, dv))


@trap
def SectRect(t, a, b, dst):
    r = _sect(t.rect(a), t.rect(b))
    t.set_rect(dst, r or EMPTY)
    return r is not None


@trap
def UnionRect(t, a, b, dst):
    t.set_rect(dst, _union(t.rect(a), t.rect(b)))


@trap
def Pt2Rect(t, a, b, dst):
    t.set_rect(dst, Rect(min(a.v, b.v), min(a.h, b.h), max(a.v, b.v), max(a.h, b.h)))


@trap
def PtInRect(t, pt, r):
    return t.rect(r).contains(pt)


@trap
def EqualRect(t, a, b):
    return t.rect(a) == t.rect(b)


@trap
def EmptyRect(t, r):
    return t.rect(r).empty


@trap
def SetPt(t, p, h, v):
    t.set_point(p, Point(v, h))


@trap
def AddPt(t, src, dst):
    d = t.point(dst)
    t.set_point(dst, Point(d.v + src.v, d.h + src.h))


@trap
def SubPt(t, src, dst):
    d = t.point(dst)
    t.set_point(dst, Point(d.v - src.v, d.h - src.h))


@trap
def DeltaPoint(t, a, b):
    """a - b, as a Point in a long: vertical in the high word."""
    return (((a.v - b.v) & 0xFFFF) << 16) | ((a.h - b.h) & 0xFFFF)


@trap
def EqualPt(t, a, b):
    return a == b


@trap
def PtToAngle(t, r, pt, angle_p):
    """Degrees clockwise from 12 o'clock to the line from r's centre to
    pt, as r's shape has them: the line through its top right corner is
    at 45 whatever its shape."""
    b = t.rect(r)
    dh = (2 * pt.h - b.left - b.right) / max(b.width, 1)
    dv = (2 * pt.v - b.top - b.bottom) / max(b.height, 1)
    t.w16(angle_p, round(math.degrees(math.atan2(dh, 0.0 - dv))) % 360)


@trap
def PinRect(t, r, pt):
    b = t.rect(r)
    v = min(max(pt.v, b.top), b.bottom - 1)
    h = min(max(pt.h, b.left), b.right - 1)
    return ((v & 0xFFFF) << 16) | (h & 0xFFFF)


@trap
def Random(t):
    """QuickDraw's: randSeed times 7^5 mod 2^31-1, low word, never -32768."""
    p = (_the_port_var(t) + QD_RAND_SEED) & ADDRESS_MASK
    seed = (t.mem.r32(p) * 16807) % 0x7FFFFFFF
    t.mem.w32(p, seed)
    r = seed & 0xFFFF
    return 0 if r == 0x8000 else r


@trap
def StuffHex(t, p, s):
    try:
        t.mem.write(p, bytes.fromhex(t.str255(s)))
    except ValueError:
        pass


# =========================================================================
# Cursors: the host's own
# =========================================================================
@trap
def InitCursor(t):
    t.system.input.cursor(None)


@trap
def HideCursor(t):
    t.system.input.cursor(-1)


@trap
def ShowCursor(t):
    t.system.input.cursor(1)


trap(_noop("ObscureCursor"))


@trap
def SetCursor(t, p):
    pass


@trap
def ShieldCursor(t, r, offset):
    pass


# =========================================================================
# Font Manager
# =========================================================================
@trap
def InitFonts(t):
    pass


def _family(t, family: int) -> int:
    """Families 0 and 1 are the system and application fonts."""
    if family == 0:
        return t.mem.r16(lowmem.SysFontFam)
    if family == 1:
        return t.mem.r16(lowmem.ApFontID)
    return family


@contextmanager
def _unloaded(t):
    """Resources found without being read in, for their names and IDs."""
    rm = _rm(t)
    load = rm.res_load
    rm.SetResLoad(False)
    try:
        yield rm
    finally:
        rm.SetResLoad(load)


@trap
def GetFontName(t, family, p):
    """The family's name: its FOND's, or the name of the FONT or NFNT that
    heads it (ID family * 128), "" for a family not installed."""
    family = _family(t, family)
    name = ""
    with _unloaded(t) as rm:
        font = font_id(family)
        for kind, rid in (("FOND", family), ("FONT", font), ("NFNT", font)):
            h = rm.GetResource(kind, rid) if rid is not None and -0x8000 <= rid < 0x8000 else 0
            if h:
                name = rm.GetResInfo(h)[2]
                break
    t.set_str255(p, name)


@trap
def GetFNum(t, p, num_p):
    """The number of the family named, in any case; 0 if none has it."""
    name = t.str255(p)
    num = 0
    with _unloaded(t) as rm:
        h = rm.GetNamedResource("FOND", name) if name else 0
        if h:
            num = rm.GetResInfo(h)[0]
        elif name:
            h = rm.GetNamedResource("FONT", name) or rm.GetNamedResource("NFNT", name)
            num = font_family(rm.GetResInfo(h)[0]) if h else 0
    t.w16(num_p, num)


@trap
def RealFont(t, family, size):
    """Whether the family is installed at the size, not scaled to it: a FONT
    of that size, or a size, or an outline font (size 0), its FOND lists."""
    family = _family(t, family)
    if not 0 < size < 128:
        return False
    with _unloaded(t) as rm:
        rid = font_id(family, size)
        if rid is not None and rm.GetResource("FONT", rid):
            return True
    rm = _rm(t)
    h = rm.GetResource("FOND", family)
    if not h:
        return False
    heap = t.system.heap_of(h)
    data = t.mem.read(heap.deref(h), heap.handle_size(h))
    if len(data) < 54:
        return False
    n = struct.unpack_from(">h", data, 52)[0] + 1
    sizes = {struct.unpack_from(">h", data, 54 + 6 * i)[0]
             for i in range(min(n, (len(data) - 54) // 6))}
    return size in sizes or 0 in sizes


@trap
def SetFontLock(t, lock):
    pass


# The output record, FMOutput, at FMgrOutRec (Inside Macintosh I, "The Font
# Manager").  Programs of 1984 read it after measuring a character: Word
# 1.0 takes every character's width from the FONT its fontHandle holds,
# and scales it by numer and denom, rather than call CharWidth again.
FMOUTPUT = ">hIBBBBBBbBBBbBhhhh"
FONT_HEADER = ">HHHHhhhhHhhhH"
propFont = 0x9000
_bold, _italic, _underline, _outline, _shadow = (1 << i for i in range(5))


@trap
def FMSwapFont(t, inp):
    """The font for an FMInput record's family, size and face."""
    family, size, face = struct.unpack(">hhB", t.mem.read(inp, 5))
    return _swap_font(t, family, size, face)


def _swap_port_font(t) -> None:
    port = _port(t)
    if port is not None:
        _swap_font(t, port.tx_font, port.tx_size, port.tx_face)


def _swap_font(t, family: int, size: int, face: int) -> int:
    """Pick the font, as QuickDraw draws it, and leave the output record
    in low memory.  An application's own family, or a standard one it
    has its own strike of, is drawn from its FONT or NFNT, which is the
    fontHandle, with extra the pixels its style adds to each character;
    any other standard family is drawn with a host font, and the
    fontHandle is the FONT it's measured with, a real System file's, or
    else a FONT made from the host font, style and all."""
    qd = t.tb.qd
    size = size or 12
    found = None
    if qd.app_fonts is not None:
        found = qd.app_fonts.source(family, size)
    if found is None and qd.app_fonts is not None and family in STANDARD_FAMILIES:
        measured = qd.app_fonts.measure(family, size)
        found = (measured[0], 1.0, *measured[1:]) if measured is not None else None
    if found is not None:
        strike, scale, kind, rid = found
        handle = _rm(t).GetResource(kind, rid)
        extra = (1 if face & _bold else 0) + (1 if face & (_outline | _shadow) else 0)
        info = [round(n * scale) for n in (strike.ascent, strike.descent, strike.wid_max,
                                           strike.leading)]
        numer, denom = (size, round(size / scale)) if scale != 1 else (1, 1)
    else:
        handle = _host_font(t, family, size, face)
        extra = 0
        info = list(qd.host_font_info(family, size, face))
        numer = denom = 1
    ascent, descent, wid_max = (max(0, min(n, 255)) for n in info[:3])
    leading = max(-128, min(info[3], 127))
    t.mem.write(lowmem.FMgrOutRec, struct.pack(
        FMOUTPUT, 0, handle, 1 if face & _bold else 0, 1 if face & _italic else 0, 1, 1, 1,
        1 if face & (_outline | _shadow) else 0, extra, ascent, descent, wid_max, leading, 0,
        numer, numer, denom, denom))
    return lowmem.FMgrOutRec


def _host_font(t, family: int, size: int, face: int) -> int:
    """A FONT record for a host font: its widths are the ones macrt draws
    with, so what a program adds up from them is where the pen goes.  Its
    strike is blank, since nothing draws from it."""
    fonts = t.system.font_records
    key = (family, size, face)
    if key not in fonts:
        qd = t.tb.qd
        text = from_mac(bytes(range(256))).translate(CHICAGO_SYMBOLS)
        widths = [min(w, 255) for w in qd.host_widths(family, size, face, text)]
        ascent, descent, wid_max, leading = qd.host_font_info(family, size, face)
        height = max(ascent + descent, 1)
        n = 256 + 2                         # each character, the missing one, and the end
        strike = bytes(2 * height)          # a word wide
        ow_table = struct.calcsize(FONT_HEADER) + len(strike) + 2 * n
        record = (struct.pack(FONT_HEADER, propFont, 0, 255, min(wid_max, 255), 0, -descent,
                              wid_max, height, (ow_table - 16) // 2, ascent, descent, leading, 1)
                  + strike + bytes(2 * n) + struct.pack(f">{n}H", *widths, 0, 0xFFFF))
        heap = t.system.sys_heap
        h = heap.new_handle(len(record))
        if not h:
            return 0
        t.mem.write(heap.deref(h), record)
        fonts[key] = h
    return fonts[key]


# =========================================================================
# Window Manager
# =========================================================================
@trap
def InitWindows(t):
    if t.mem.r8(lowmem.WWExist):            # the program threw the last port away, as
        t.system.open_wmgr_port()           # Rogue's start does, saying there's none
    t.mem.w32(_the_port_var(t), t.mem.r32(lowmem.WMgrPort))


def _behind(t, p: int):
    if p == 0xFFFFFFFF:
        return IN_FRONT
    return t.window(p)


@trap
def NewWindow(t, storage, bounds, title, visible, proc_id, behind, go_away, ref_con):
    return t.tb.NewWindow(t.rect(bounds), t.str255(title), bool(visible), proc_id,
                          _behind(t, behind), bool(go_away), ref_con, storage)


@trap
def GetNewWindow(t, window_id, storage, behind):
    return t.tb.GetNewWindow(window_id, storage, _behind(t, behind))


def _on_window(method: str):
    def call(t, p, *args):
        w = t.window(p)
        if w is not None:
            return getattr(t.tb, method)(w, *args)
    call.__name__ = method
    return call


for _name in ("SelectWindow", "BringToFront", "ShowWindow", "HideWindow", "DrawGrowIcon",
              "BeginUpdate", "EndUpdate"):
    trap(_on_window(_name))


@trap
def CloseWindow(t, p):
    w = t.window(p)
    if w is not None:
        kill_controls(t, w, erase=False)
        t.tb.CloseWindow(w)


@trap
def DisposeWindow(t, p):
    w = t.window(p)
    if w is not None:
        kill_controls(t, w, erase=False)
        t.tb.DisposeWindow(w)


@trap
def HiliteWindow(t, p, hilite):
    w = t.window(p)
    if w is not None:
        t.tb.HiliteWindow(w, bool(hilite))


@trap
def ShowHide(t, p, show):
    w = t.window(p)
    if w is not None:
        t.tb.ShowHide(w, bool(show))


@trap
def SendBehind(t, p, behind):
    w = t.window(p)
    if w is not None:
        t.tb.SendBehind(w, t.window(behind))


@trap
def FrontWindow(t):
    return t.tb.FrontWindow()


@trap
def FindWindow(t, pt, which_p):
    part, w = t.tb.FindWindow(pt)
    t.w32(which_p, w.addr if w is not None else 0)
    if w is not None and w.window_kind < 0 and part not in (k.inDesk, k.inMenuBar):
        return k.inSysWindow                # a desk accessory's: SystemClick's to deal with
    return part


@trap
def SetWTitle(t, p, title):
    w = t.window(p)
    if w is not None:
        t.tb.SetWTitle(w, t.str255(title))


@trap
def GetWTitle(t, p, title_p):
    w = t.window(p)
    t.set_str255(title_p, w.title if w is not None else "")


@trap
def SetWRefCon(t, p, ref_con):
    w = t.window(p)
    if w is not None:
        w.ref_con = ref_con


@trap
def GetWRefCon(t, p):
    w = t.window(p)
    return w.ref_con if w is not None else 0


@trap
def GetWVariant(t, p):
    w = t.window(p)
    return w.proc_id & 0xF if w is not None else 0


@trap
def MoveWindow(t, p, h, v, front):
    w = t.window(p)
    if w is not None:
        t.tb.MoveWindow(w, h, v, bool(front))


@trap
def SizeWindow(t, p, width, height, update):
    w = t.window(p)
    if w is not None:
        t.tb.SizeWindow(w, width, height, bool(update))


@trap
def ZoomWindow(t, p, part, front):
    w = t.window(p)
    if w is not None:
        t.tb.ZoomWindow(w, part, bool(front))


@trap
def DragWindow(t, p, start, bounds):
    w = t.window(p)
    if w is not None:
        with t.yielding():
            t.tb.DragWindow(w, start, t.rect(bounds) if bounds else None)


def _drag_rgn(t, h, start, limit, slop, axis, action, pat: bytes) -> int:
    """The region's outline, in thePort, follows the mouse until the
    button comes up: how far, the vertical distance in the high word, or
    $80008000 if it came up outside slop."""
    port = _port(t)
    if port is None or not h:
        return 0
    moved = drag_outline(t, port, start, read_rgn(t.mem, h), t.rect(limit), t.rect(slop),
                         axis, action, pat)
    return (moved.v & 0xFFFF) << 16 | moved.h & 0xFFFF


@trap
def DragGrayRgn(t, h, start, limit, slop, axis, action):
    return _drag_rgn(t, h, start, limit, slop, axis, action, GRAY_PAT)


@trap
def DragTheRgn(t, h, start, limit, slop, axis, action):
    """DragGrayRgn in DragPattern's pattern: Sargon III drags its pieces so."""
    return _drag_rgn(t, h, start, limit, slop, axis, action, t.mem.read(lowmem.DragPattern, 8))


@trap
def TrackGoAway(t, p, pt):
    w = t.window(p)
    if w is None:
        return False
    with t.yielding():
        return t.tb.TrackGoAway(w, pt)


@trap
def TrackBox(t, p, pt, part):
    w = t.window(p)
    if w is None:
        return False
    with t.yielding():
        return t.tb.TrackBox(w, pt, part)


@trap
def GrowWindow(t, p, pt, size_rect):
    w = t.window(p)
    if w is None:
        return 0
    with t.yielding():
        return t.tb.GrowWindow(w, pt, t.rect(size_rect))


@trap
def InvalRect(t, r):
    t.tb.InvalRect(t.rect(r))


@trap
def InvalRgn(t, h):
    t.tb.InvalRgn(_rgn(t, h))


@trap
def ValidRect(t, r):
    t.tb.ValidRect(t.rect(r))


@trap
def ValidRgn(t, h):
    t.tb.ValidRgn(_rgn(t, h))


@trap
def CheckUpdate(t, p):
    return False


@trap
def GetWindowPic(t, p):
    w = t.window(p)
    return w.window_pic if w is not None else 0


@trap
def SetWindowPic(t, p, pic):
    w = t.window(p)
    if w is not None:
        w.window_pic = pic


for _name in ("DrawNew", "CalcVis", "CalcVisBehind", "ClipAbove", "PaintOne", "PaintBehind",
              "SaveOld"):
    trap(_noop(_name))


# =========================================================================
# Menu Manager
# =========================================================================
@trap
def InitMenus(t):
    """Clears the menu bar and draws it, empty, as a Mac's does: a game
    with no menus of its own has a bar in the screen buffer to draw over."""
    t.tb.ClearMenuBar()
    t.tb.DrawMenuBar()


@trap
def InitProcMenu(t, mbar_id):
    """The menu bar's definition procedure.  macrt draws the bar and
    tracks the mouse itself, but an application's own MBDF is told about
    each menu as it's pulled down (MenuSelect)."""
    t.process.menus.mbdf_id = mbar_id
    h = t.process.resources.defproc("MBDF", mbar_id)
    t.process.world.w32(lowmem.MBDFHndl, h)
    if _app_mbdf(t):
        _call_mbdf(t, k.mbInit)


def _app_mbdf(t) -> int:
    """The MBDF InitProcMenu named if it's the application's, else 0."""
    h = t.process.world.r32(lowmem.MBDFHndl) & ADDRESS_MASK
    return h if h and t.system.defproc_id(h)[0] != "MBDF" else 0


def _call_mbdf(t, msg: int, param1: int = 0, param2: int = 0) -> int:
    h = _app_mbdf(t)
    heap = t.system.heap_of(h)
    if not heap.deref(h):
        t.process.resources.LoadResource(h)
    entry = heap.deref(h)
    if not entry:
        return 0
    state = heap.get_state(h)
    heap.lock(h)
    try:
        return t.call(entry, (("h", t.process.menus.mbdf_id & 7), ("h", msg), ("h", param1),
                              ("l", param2)), "l")
    finally:
        heap.set_state(h, state)


@trap
def NewMenu(t, menu_id, title):
    return _menu_handle(t.tb.NewMenu(menu_id, t.str255(title)))


@trap
def GetMenu(t, menu_id):
    return _menu_handle(t.tb.GetMenu(menu_id))


@trap
def GetNewMBar(t, mbar_id):
    return t.tb.GetNewMBar(mbar_id)


@trap
def GetMenuBar(t):
    return t.tb.GetMenuBar()


@trap
def SetMenuBar(t, h):
    if h:
        t.tb.SetMenuBar(h)


@trap
def DrawMenuBar(t):
    t.tb.DrawMenuBar()


@trap
def ClearMenuBar(t):
    t.tb.ClearMenuBar()


@trap
def GetMenuHandle(t, menu_id):
    return _menu_handle(t.tb.GetMHandle(menu_id))


@trap
def InsertMenu(t, h, before_id):
    m = t.menu(h)
    if m is not None:
        t.tb.InsertMenu(m, before_id)


@trap
def DeleteMenu(t, menu_id):
    t.tb.DeleteMenu(menu_id)


@trap
def DisposeMenu(t, h):
    m = t.menu(h)
    if m is not None:
        t.tb.DisposeMenu(m)


@trap
def AppendMenu(t, h, data):
    m = t.menu(h)
    if m is not None:
        t.tb.AppendMenu(m, t.str255(data))


def _resource_names(t, rtype: int) -> list[str]:
    """The names a menu of the resource type lists, sorted.  A font menu
    lists the font families too.  Names that start with a period are
    hidden, as drivers' are among the desk accessories, and so are those
    that start with a percent sign."""
    kinds = [t.ostype(rtype)] + ([b"FOND"] if t.ostype(rtype) == b"FONT" else [])
    names = set()
    with _unloaded(t) as rm:
        for kind in kinds:
            for i in range(1, rm.CountResources(kind) + 1):
                h = rm.GetIndResource(kind, i)
                name = rm.GetResInfo(h)[2] if h else ""
                if name and name[0] not in ".%":
                    names.add(name)
    return sorted(names, key=str.lower)


@trap
def AppendResMenu(t, h, rtype):
    InsertResMenu(t, h, rtype, 0x7FFF)


@trap
def InsertResMenu(t, h, rtype, after):
    m = t.menu(h)
    if m is None:
        return
    names = _resource_names(t, rtype)
    if names:
        items = m.items
        after = max(0, min(after, len(items)))
        new = [MenuItem(name) for name in names]    # as written: no metacharacters
        m.set_items(items[:after] + new + items[after:])
        t.tb.menus_changed()


@trap
def CountMItems(t, h):
    m = t.menu(h)
    return m.count() if m is not None else 0


@trap
def EnableItem(t, h, item):
    m = t.menu(h)
    if m is not None:
        t.tb.EnableItem(m, item)


@trap
def DisableItem(t, h, item):
    m = t.menu(h)
    if m is not None:
        t.tb.DisableItem(m, item)


@trap
def CheckItem(t, h, item, checked):
    m = t.menu(h)
    if m is not None:
        t.tb.CheckItem(m, item, bool(checked))


@trap
def SetItemMark(t, h, item, mark):
    m = t.menu(h)
    if m is not None:
        t.tb.SetItemMark(m, item, "" if not mark & 0xFF else _char(mark))


@trap
def GetItemMark(t, h, item, mark_p):
    m = t.menu(h)
    it = m.item(item) if m is not None else None
    t.w16(mark_p, to_mac(it.mark)[0] if it is not None and it.mark else 0)


@trap
def SetMenuItemText(t, h, item, text):
    m = t.menu(h)
    if m is not None:
        t.tb.SetItem(m, item, t.str255(text))


@trap
def GetMenuItemText(t, h, item, text_p):
    m = t.menu(h)
    t.set_str255(text_p, t.tb.GetItem(m, item) if m is not None else "")


@trap
def SetItemStyle(t, h, item, style):
    m = t.menu(h)
    if m is not None:
        t.tb._change_item(m, item, style=style & 0xFF)


@trap
def GetItemStyle(t, h, item, style_p):
    m = t.menu(h)
    it = m.item(item) if m is not None else None
    if style_p:
        t.mem.w8(style_p, it.style if it is not None else 0)


@trap
def SetItemCmd(t, h, item, cmd):
    m = t.menu(h)
    if m is not None:
        t.tb._change_item(m, item, cmd="" if not cmd & 0xFF else _char(cmd))


@trap
def GetItemCmd(t, h, item, cmd_p):
    m = t.menu(h)
    it = m.item(item) if m is not None else None
    t.w16(cmd_p, to_mac(it.cmd)[0] if it is not None and it.cmd else 0)


@trap
def SetItemIcon(t, h, item, icon):
    m = t.menu(h)
    if m is not None:
        t.tb._change_item(m, item, icon=icon)


@trap
def GetItemIcon(t, h, item, icon_p):
    m = t.menu(h)
    it = m.item(item) if m is not None else None
    t.w16(icon_p, it.icon if it is not None else 0)


@trap
def InsertMenuItem(t, h, data, after):
    m = t.menu(h)
    if m is None:
        return
    items = m.items
    after = max(0, min(after, len(items)))
    new = parse_menu_items(t.str255(data))
    m.set_items(items[:after] + new + items[after:])
    t.tb.menus_changed()


@trap
def DeleteMenuItem(t, h, item):
    m = t.menu(h)
    if m is None:
        return
    items = m.items
    if 1 <= item <= len(items):
        del items[item - 1]
        m.set_items(items)
        t.tb.menus_changed()


@trap
def MenuSelect(t, pt):
    if not _app_mbdf(t):
        with t.yielding():
            result = t.tb.MenuSelect(pt)
        return glue_desk.menu_picked(t, result)
    shown = []

    def opening(menu_id: int, where: Rect) -> None:
        """As the System's MenuSelect does: restore what the last menu
        covered, then ask where this one goes and save what it covers."""
        with t.resumed():
            _restore_menu(t, shown)
            offset = t.process.menus.offset(menu_id)
            if not offset:
                return
            with scratch(t, 8) as rp:
                t.mem.w_rect(rp, where)
                t.menu_rect = rp
                try:
                    r = _call_mbdf(t, k.mbRect, 0, offset) & ADDRESS_MASK
                    _call_mbdf(t, k.mbSave, offset, r or rp)
                finally:
                    t.menu_rect = 0
            shown.append(offset)

    with t.yielding():
        result = t.tb.MenuSelect(pt, opening)
    _restore_menu(t, shown)
    return glue_desk.menu_picked(t, result)


@trap
def PopUpMenuSelect(t, h, top, left, item):
    """The menu the caller drew a pop-up box for, pulled down at (top,
    left) with item's top highlighted there: (menu ID << 16) | chosen
    item, 0 for nothing.  The host draws it, so the app's MBDF doesn't
    hear about it; the caller marks and stores the choice itself."""
    with t.yielding():
        return t.tb.PopUpMenuSelect(t.menu(h), top, left, item)


def _restore_menu(t, shown: list[int]) -> None:
    if shown:
        shown.clear()
        _call_mbdf(t, k.mbRestore)


@trap
def MenuKey(t, ch):
    return glue_desk.menu_picked(t, t.tb.MenuKey(ch & 0xFF))


@trap
def HiliteMenu(t, menu_id):
    t.tb.HiliteMenu(menu_id)


for _name in ("CalcMenuSize", "FlashMenuBar", "SetMenuFlash"):
    trap(_noop(_name))


# =========================================================================
# The System's WDEFs, MDEF and MBDF, for 68k code that calls one itself.
# The host draws menus, so there's little for the MDEF and MBDF to answer,
# but the arguments come off the stack.
# =========================================================================
def _system_defproc(t, name: str, msg: int) -> int:
    t.unimplemented(f"the System's {name}, message {msg}", 0xAA68)
    return 0


@trap
def SystemWDEF0(t, var, w, msg, param):
    """WDEF 0's code, for an application's own WDEF that passes it the
    messages it doesn't answer, as Excel's do."""
    return wdefs.system_wdef(t, 0, var, w, msg, param)


@trap
def SystemWDEF1(t, var, w, msg, param):
    return wdefs.system_wdef(t, 1, var, w, msg, param)


@trap
def SystemMDEF0(t, msg, menu, rect_p, hit, item_p):
    _system_defproc(t, "MDEF 0", msg)


@trap
def SystemMBDF0(t, selector, msg, param1, param2):
    """For an application's MBDF that leaves the work to the System's:
    there's nothing to draw, save or work out here, and mbRect has where
    the menu being pulled down goes."""
    if msg == k.mbRect and t.menu_rect:
        return t.menu_rect
    if msg in (k.mbDraw, k.mbCalc, k.mbInit, k.mbDispose, k.mbHilite, k.mbHeight, k.mbSave,
               k.mbRestore, k.mbSaveAlt, k.mbResetAlt):
        return 0
    return _system_defproc(t, "MBDF 0", msg)


# =========================================================================
# Event Manager
# =========================================================================
@trap
def WaitNextEvent(t, mask, event_p, sleep, mouse_rgn):
    # As under MultiFinder, where a program that calls this needn't call
    # SystemTask: desk accessories get their time.
    glue_desk.system_task(t)
    glue_sony.put_back(t)
    with t.yielding():
        got, ev = t.tb.WaitNextEvent(mask & 0xFFFF, glue_desk.time_wanted(t, max(sleep, 0)))
    return _returned(t, event_p, got, ev)


@trap
def GetNextEvent(t, mask, event_p):
    # Nothing to do returns at once, as on a Mac, where a program may wait
    # for the Ticks to change and then look once, as Hacker II does as it
    # types.  Asked again in the same tick, it waits for the next: programs
    # call this in a loop, and the host's other applications want the
    # processor too.
    glue_sony.put_back(t)
    wait = to_next_tick() if t.null_tick == tick_count() else 0
    with t.yielding():
        got, ev = t.process.events.get(mask & 0xFFFF, wait, t.tb._next_update)
    if not got:
        t.null_tick = ev.when
    t.tb._end_grow_preview()
    return _returned(t, event_p, got, ev)


def _returned(t, event_p: int, got: bool, ev) -> bool:
    """Write the event GetNextEvent returns, mount the disk of a
    disk-inserted one, and give a desk accessory one that's its own:
    whether it's the application's."""
    t.write_event(event_p, ev)
    if got and ev.what == k.diskEvt:
        from .glue_files import disk_inserted
        disk_inserted(t, event_p)
    return got and not glue_desk.system_event(t, event_p)


@trap
def EventAvail(t, mask, event_p):
    glue_sony.put_back(t)
    got, ev = t.process.events.peek(mask & 0xFFFF, t.tb._next_update)
    t.write_event(event_p, ev)
    return got


@trap
def TickCount(t):
    """Ticks, as the ROM's has it: what the program reads there itself
    goes on from it."""
    return t.mem.r32(lowmem.Ticks)


@trap
def Button(t):
    return t.tb.Button()


@trap
def StillDown(t):
    return t.tb.StillDown()


@trap
def WaitMouseUp(t):
    return t.tb.WaitMouseUp()


@trap
def GetMouse(t, p):
    # Mouse, as the ROM has it: under the host's pointer, unless the
    # program has moved the mouse itself
    pt = t.mem.r_point(lowmem.Mouse)
    port = _port(t)
    t.set_point(p, pt if port is None else port.global_to_local(pt))


@trap
def GetKeys(t, p):
    """The KeyMap, with the modifier keys as they are now."""
    keys = bytearray(t.mem.read(lowmem.KeyMap, 16))
    _, mods = t.process.events.input_state()
    for bit, code in MODIFIER_KEYS:
        if mods & bit:
            keys[code >> 3] |= 1 << (code & 7)
    t.mem.write(p, bytes(keys))


@trap
def KeyTranslate(t, trans_data, keycode, state_p):
    """A key code and modifiers to characters, by the US layout whatever
    the KCHR, with no dead keys: the character in the low byte."""
    t.w32(state_p, 0)
    return keymap.translate(keycode & 0x7F, keycode >> 8)


@trap
def SysBeep(t, duration):
    t.tb.SysBeep(duration)


# =========================================================================
# Resource Manager
# =========================================================================
def _rm(t) -> ResourceManager:
    return t.process.resources


@trap
def GetResource(t, rtype, rid):
    return _rm(t).GetResource(rtype, rid)


@trap
def Get1Resource(t, rtype, rid):
    return _rm(t).Get1Resource(rtype, rid)


@trap
def GetNamedResource(t, rtype, name):
    return _rm(t).GetNamedResource(rtype, t.pstr(name))


@trap
def Get1NamedResource(t, rtype, name):
    return _rm(t).Get1NamedResource(rtype, t.pstr(name))


@trap
def GetIndResource(t, rtype, index):
    return _rm(t).GetIndResource(rtype, index)


@trap
def Get1IndResource(t, rtype, index):
    return _rm(t).Get1IndResource(rtype, index)


@trap
def CountResources(t, rtype):
    return _rm(t).CountResources(rtype)


@trap
def Count1Resources(t, rtype):
    return _rm(t).Count1Resources(rtype)


@trap
def CountTypes(t):
    return _rm(t).CountTypes()


@trap
def Count1Types(t):
    return _rm(t).Count1Types()


@trap
def GetIndType(t, type_p, index):
    t.mem.write(type_p, _rm(t).GetIndType(index) or bytes(4))


@trap
def Get1IndType(t, type_p, index):
    t.mem.write(type_p, _rm(t).Get1IndType(index) or bytes(4))


@trap
def GetResInfo(t, h, id_p, type_p, name_p):
    rid, rtype, name = _rm(t).GetResInfo(h)
    t.w16(id_p, rid)
    if type_p:
        t.mem.write(type_p, (rtype or b"").ljust(4, b"\0")[:4])
    t.set_str255(name_p, name or "")


@trap
def SetResInfo(t, h, rid, name):
    _rm(t).SetResInfo(h, rid, t.str255(name) if name else None)


@trap
def AddResource(t, h, rtype, rid, name):
    _rm(t).AddResource(h, rtype, rid, t.str255(name))


@trap
def OpenResFile(t, name):
    return _rm(t).OpenResFile(t.str255(name))


@trap
def HOpenResFile(t, vref, dir_id, name, permission):
    return _rm(t).HOpenResFile(vref, dir_id, t.str255(name), permission)


@trap
def OpenRFPerm(t, name, vref, permission):
    return _rm(t).OpenRFPerm(t.str255(name), vref, permission)


@trap
def CreateResFile(t, name):
    _rm(t).CreateResFile(t.str255(name))


@trap
def HCreateResFile(t, vref, dir_id, name):
    _rm(t).HCreateResFile(vref, dir_id, t.str255(name))


@trap
def SetResLoad(t, load):
    _rm(t).SetResLoad(bool(load))


@trap
def GetResourceSizeOnDisk(t, h):
    return _rm(t).SizeResource(h)


@trap
def GetMaxResourceSize(t, h):
    return _rm(t).MaxSizeRsrc(h)


@trap
def SetResPurge(t, install):
    pass


@trap
def RsrcZoneInit(t):
    pass


# What the System does as it starts up, and no application should
# (Inside Macintosh I-114).  Mombasa does: it gets the System file's
# reference number back, with the files open left as they are.
@trap
def InitResources(t):
    return _rm(t).sys_refnum


def _forward(name: str):
    def call(t, *args):
        return getattr(_rm(t), name)(*args)
    call.__name__ = name
    return call


for _name in ("CloseResFile", "CurResFile", "UseResFile", "HomeResFile", "ResError",
              "UpdateResFile", "GetResFileAttrs", "SetResFileAttrs", "UniqueID", "Unique1ID",
              "LoadResource", "ReleaseResource", "DetachResource", "GetResAttrs",
              "SetResAttrs", "RemoveResource", "ChangedResource", "WriteResource"):
    trap(_forward(_name))


# References: entries in the current file's map for resources in other
# files, which the 64K ROM kept and later ones don't.  MacWrite adds one
# for each font a document uses, to the document it saves.  Here they're
# not kept.
@trap
def AddReference(t, h, rid, name):
    _rm(t).error = noErr


@trap
def RmveReference(t, h):
    _rm(t).error = noErr


# The Resource Manager's procedures end by storing ResErr, which leaves the
# flags as TST.W would.  MacWrite branches on them after CreateResFile
# without testing anything itself.
for _name in ("CreateResFile", "HCreateResFile", "CloseResFile", "UseResFile",
              "UpdateResFile", "SetResFileAttrs", "LoadResource", "ReleaseResource",
              "DetachResource", "SetResAttrs", "SetResInfo", "AddResource", "RemoveResource",
              "ChangedResource", "WriteResource", "AddReference", "RmveReference"):
    RESERR_FLAGS.add(SIGNATURES[_name].trap)


# The Resource Manager reads in each resource it's asked for by calling
# _ResourceStub's address with the map's handle in A4 and the reference
# list entry at A2 (and A3); the resource's handle comes back in A0.
# Applications patch it to read in resources of their own making, such as
# compressed ones, and leave the rest to the original.
RESOURCE_STUB = 0xA0FC


@raw(RESOURCE_STUB)
def ResourceStub(t, word):
    """The original, which reads the resource in if ResLoad says to."""
    cpu, rm = t.cpu, _rm(t)
    m = ResourceMap(t.mem, cpu.a(4))
    cpu.set_a(0, rm.read_in(m, Entry(m, cpu.a(2) & ADDRESS_MASK, b""), rm.res_load))


def load_resource(t, m: ResourceMap, e: Entry, force: bool) -> int | None:
    """The Resource Manager's load hook: the application's patch on
    _ResourceStub reads the resource in, with ResLoad set to force while
    it does.  None if there's no patch, or the application isn't the one
    running."""
    patch = t.patches.get(RESOURCE_STUB)
    if patch is None or not t.holding or threading.current_thread() is not t.process.thread:
        return None
    cpu, rm = t.cpu, _rm(t)
    ctx = cpu.context()
    res_load = rm.res_load
    rm.SetResLoad(force)
    rm.error = noErr
    cpu.set_a(4, m.handle)
    cpu.set_a(3, e.addr)
    cpu.set_a(2, e.addr)
    cpu.set_a(0, patch)
    t._jsr(patch)
    h = cpu.a(0) & ADDRESS_MASK
    cpu.restore(ctx)
    rm.SetResLoad(res_load)
    return h


# =========================================================================
# Toolbox Utilities
# =========================================================================
def _new_handle_with(t, data: bytes) -> int:
    heap = t.process.heap
    h = heap.new_handle(len(data))
    if h:
        t.mem.write(heap.deref(h), data)
    return h


@trap
def GetString(t, string_id):
    return t.tb.GetString(string_id)


@trap
def NewString(t, p):
    return _new_handle_with(t, t.mem.read(p, t.mem.r8(p) + 1))


@trap
def SetString(t, h, p):
    data = t.mem.read(p, t.mem.r8(p) + 1)
    heap = t.system.heap_of(h)
    if heap.set_handle_size(h, len(data)):
        t.mem.write(heap.deref(h), data)


def _get(rtype: str, name: str):
    def call(t, rid):
        return _rm(t).GetResource(rtype, rid)
    call.__name__ = name
    return call


for _rtype, _name in (("ICON", "GetIcon"), ("CURS", "GetCursor"), ("PAT ", "GetPattern"),
                      ("PICT", "GetPicture")):
    trap(_get(_rtype, _name))


@trap
def Munger(t, h, offset, p1, len1, p2, len2):
    """Find p1 in the handle from offset and replace it with p2 (IM I-468)."""
    heap = t.system.heap_of(h)
    data = t.mem.read(heap.deref(h), heap.handle_size(h))
    if offset > len(data):
        return -1
    if p1 == 0:                                 # replace len1 bytes at offset
        start = offset
        end = len(data) if len1 < 0 else min(offset + len1, len(data))
    else:
        target = t.mem.read(p1, max(len1, 0))
        if len1 == 0:                           # insert at offset
            start = end = offset
        else:
            start = data.find(target, offset)
            if start < 0:
                return -1
            end = start + len(target)
    if p2 == 0:
        return start                            # just find
    new = data[:start] + t.mem.read(p2, max(len2, 0)) + data[end:]
    if not heap.set_handle_size(h, len(new)):
        return -1
    t.mem.write(heap.deref(h), new)
    return start + max(len2, 0)


@trap
def PackBits(t, src_var, dst_var, count):
    """Pack count bytes from the pointer at src_var to the one at dst_var,
    moving each past what it took and gave (IM I-470)."""
    m = t.mem
    src, dst, count = m.r32(src_var), m.r32(dst_var), max(count, 0)
    packed = pack_bits(m.read(src, count))
    m.write(dst, packed)
    m.w32(src_var, (src + count) & 0xFFFFFFFF)
    m.w32(dst_var, (dst + len(packed)) & 0xFFFFFFFF)


@trap
def UnpackBits(t, src_var, dst_var, count):
    """Unpack count bytes the same way."""
    m = t.mem
    src, dst, count = m.r32(src_var), m.r32(dst_var), max(count, 0)
    most = count + count // 128 + 2                 # all of it literal
    data, took = unpack_bits_from(m.read(src, min(most, m.size - (src & ADDRESS_MASK))), count)
    m.write(dst, data)
    m.w32(src_var, (src + took) & 0xFFFFFFFF)
    m.w32(dst_var, (dst + count) & 0xFFFFFFFF)


@trap
def HiWord(t, x):
    return (x >> 16) & 0xFFFF


@trap
def LoWord(t, x):
    return x & 0xFFFF


@trap
def BitAnd(t, a, b):
    return a & b


@trap
def BitOr(t, a, b):
    return a | b


@trap
def BitXor(t, a, b):
    return a ^ b


@trap
def BitNot(t, a):
    return ~a


@trap
def BitShift(t, x, count):
    count &= 31 if count >= 0 else -1
    x &= 0xFFFFFFFF
    return (x << count) if count >= 0 else (x >> -count)


def _bit(p: int, n: int) -> tuple[int, int]:
    """Bit n counting from the high bit of the byte at p, as BitTst does."""
    return p + (n >> 3), 0x80 >> (n & 7)


@trap
def BitTst(t, p, n):
    at, mask = _bit(p, n)
    return bool(t.mem.r8(at) & mask)


@trap
def BitSet(t, p, n):
    at, mask = _bit(p, n)
    t.mem.w8(at, t.mem.r8(at) | mask)


@trap
def BitClr(t, p, n):
    at, mask = _bit(p, n)
    t.mem.w8(at, t.mem.r8(at) & ~mask)


def _held(v: int) -> int:
    return max(min(v, 0x7FFFFFFF), -0x80000000)


def _quotient(num: int, den: int) -> int:
    """num / den as the 128K ROM's fixed-point routines round it: half a
    unit away from zero, an overflow held to $7FFFFFFF or $80000000, and
    a division by zero $80000000 if num is negative, else $7FFFFFFF
    (Inside Macintosh IV-63)."""
    if den == 0:
        return -0x80000000 if num < 0 else 0x7FFFFFFF
    q = (2 * abs(num) + abs(den)) // (2 * abs(den))
    return _held(q if (num < 0) == (den < 0) else -q)


def _scaled(x: float, bits: int) -> int:
    """x with that many fraction bits, rounded half away from zero."""
    if math.isnan(x):
        return 0x7FFFFFFF
    if math.isinf(x):
        return _held(int(math.copysign(1 << 32, x)))
    return _held(int(math.copysign(math.floor(abs(x) * (1 << bits) + 0.5), x)))


# A Fixed has 16 fraction bits, a Fract 30 (Inside Macintosh IV-62)
FIXED, FRACT = 16, 30
# The ROM reduces an angle by 0.C910 hex, FixATan2(1, 1), for pi/4, so its
# sine and cosine repeat every 2 * 3.1416015625 rather than every 2 pi
_PI = 4 * 0xC910 / 65536


@trap
def FixMul(t, a, b):
    return _quotient(a * b, 1 << FIXED)


@trap
def FixRatio(t, num, den):
    return _quotient(num << FIXED, den)


@trap
def FixDiv(t, a, b):
    return _quotient(a << FIXED, b)


@trap
def FracMul(t, a, b):
    return _quotient(a * b, 1 << FRACT)


@trap
def FracDiv(t, a, b):
    return _quotient(a << FRACT, b)


@trap
def FracSqrt(t, x):
    """x, and the root, taken unsigned: 0 to 4 (Inside Macintosh IV-64)."""
    n = (x & 0xFFFFFFFF) << FRACT
    r = math.isqrt(n)
    return r + (n - r * r > r)


@trap
def FracSin(t, x):
    return _scaled(math.sin(x / (1 << FIXED) * math.pi / _PI), FRACT)


@trap
def FracCos(t, x):
    return _scaled(math.cos(x / (1 << FIXED) * math.pi / _PI), FRACT)


@trap
def FixATan2(t, x, y):
    """The arctangent of y/x in radians, from -pi to pi."""
    return _scaled(math.atan2(y, x), FIXED)


@trap
def Long2Fix(t, x):
    return _held(x << FIXED)


@trap
def Fix2Long(t, x):
    return _quotient(x, 1 << FIXED)


@trap
def Fix2Frac(t, x):
    return _held(x << FRACT - FIXED)


@trap
def Frac2Fix(t, x):
    return _quotient(x, 1 << FRACT - FIXED)


def _extended(t, p: int) -> float:
    return sane.to_float(sane.unpack(sane.X, t.mem.read(p, 10)))


@trap
def R_X2Fix(t, p):
    """X2Fix, which takes the address of its extended."""
    return _scaled(_extended(t, p), FIXED)


@trap
def R_X2Frac(t, p):
    return _scaled(_extended(t, p), FRACT)


@trap
def FixRound(t, x):
    return (x + 0x8000) >> 16


@trap
def SlopeFromAngle(t, angle):
    """The slope dh/dv of a line that many degrees clockwise from 12
    o'clock, the angle taken mod 180 (IM I-475): v grows downward, so
    45 degrees is -1.0."""
    return _scaled(-math.tan(math.radians(angle % 180)), FIXED)


@trap
def AngleFromSlope(t, slope):
    """The angle, 1 to 180 degrees clockwise from 12 o'clock, of a line
    with that slope dh/dv; a slope of 0 is 180 (IM I-475)."""
    angle = round(math.degrees(math.atan(-slope / 65536)))
    return angle if angle > 0 else angle + 180


@trap
def LongMul(t, a, b, p):
    t.mem.write(p, struct.pack(">q", a * b))


@trap
def NumToString(t, n, p):
    t.set_str255(p, str(n))


@trap
def StringToNum(t, p):
    s = t.str255(p).strip()
    sign = -1 if s.startswith("-") else 1
    digits = s.lstrip("+-")
    value = 0
    for c in digits:
        value = value * 10 + (ord(c) - 48)     # as Pack7 does: no checking
    return None, (sign * value) & 0xFFFFFFFF


# =========================================================================
# Package Manager (IM I-483): every package is always there, so there's
# nothing to load or unload
# =========================================================================
for _name in ("InitPack", "InitAllPacks", "DILoad", "DIUnload"):
    trap(_noop(_name))
