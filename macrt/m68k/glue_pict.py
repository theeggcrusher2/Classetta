"""
QuickDraw pictures (DrawPicture, OpenPicture, ClosePicture, PicComment),
the standard drawing procedures a QDProcs record points at, and the
masked copies CopyMask and CopyDeepMask, with SeedFill and CalcMask that
make their masks.  The pictures themselves are macrt.toolbox.pict's.

A port's grafProcs, when it has them, decide where a picture's bytes
come from and go to (getPicProc and putPicProc, for pictures spooled to
and from disk) and who sees its comments (commentProc), and its text is
drawn and measured with its textProc and txMeasProc (glue_toolbox); the
other procs are left to themselves, since QuickDraw here draws directly.
"""

from __future__ import annotations

import struct
from functools import partial

from ..memory import ADDRESS_MASK, from_mac
from ..toolbox import pict
from ..toolbox.api import CHICAGO_SYMBOLS
from ..toolbox.masks import calc_mask, mask_region, seed_fill
from ..toolbox.memmgr import RESOURCE
from ..toolbox.qdraw import (Region, arc_path, oval_path, read_poly, rect_path, round_rect_path,
                             srcCopy)
from ..toolbox.types import Rect
from .dispatch import trap
from .glue_toolbox import _custom_proc, _port, _rgn, _swap_port_font

VERBS = pict.VERBS
SPOOL = 4096                # bytes per call to a getPicProc or putPicProc
FIXED_72 = 72 << 16

# QDProcs: where each procedure is, and the trap that's its standard one.
PROCS = (("textProc", 0xA882), ("lineProc", 0xA890), ("rectProc", 0xA8A0),
         ("rRectProc", 0xA8AF), ("ovalProc", 0xA8B6), ("arcProc", 0xA8BD),
         ("polyProc", 0xA8C5), ("rgnProc", 0xA8D1), ("bitsProc", 0xA8EB),
         ("commentProc", 0xA8F1), ("txMeasProc", 0xA8ED), ("getPicProc", 0xA8EE),
         ("putPicProc", 0xA8F0))
COMMENT_PROC, GET_PIC_PROC, PUT_PIC_PROC = 36, 44, 48


# =========================================================================
# Pictures (Inside Macintosh I-189, V-88)
# =========================================================================
@trap
def DrawPicture(t, h, r):
    port = _port(t)
    p = t.mem.r32(h) & ADDRESS_MASK if h else 0
    if port is None or not p:
        return
    heap = t.system.heap_of(h)
    get_pic = _custom_proc(t, port, GET_PIC_PROC, 0xA8EE)
    buf = 0
    if get_pic:                         # spooled: the handle holds only the size and frame
        buf = heap.new_ptr(SPOOL)
        if not buf:
            return

        def read(n: int) -> bytes:
            out = bytearray()
            while len(out) < n:
                k = min(n - len(out), SPOOL)
                t.call(get_pic, (("p", buf), ("h", k)))
                out += t.mem.read(buf, k)
            return bytes(out)
        reader = pict.Reader(read, 10)
    else:                               # all of it, in case drawing moves the handle
        reader = pict.Reader.of(t.mem.read(p, heap.handle_size(h)), 10)
    frame = t.mem.r_rect(p + 2)
    proc = _custom_proc(t, port, COMMENT_PROC, 0xA8F1)

    def comment(kind: int, data: bytes | None) -> None:
        dh = heap.new_handle(len(data)) if data else 0
        if dh:
            t.mem.write(heap.deref(dh), data)
        t.call(proc, (("h", kind), ("h", len(data) if dh else 0), ("p", dh)))
        if dh:
            heap.dispose_handle(dh)
    try:
        pict.draw_picture(t.tb.qd, port, frame, reader, t.rect(r), comment if proc else None)
    finally:
        if buf:
            heap.dispose_ptr(buf)


def _open(t, rec: pict.Recorder) -> int:
    """Start recording in the current port: a PicHandle for the picture,
    picSave set to say so, and the pen hidden."""
    port = _port(t)
    if port is None:
        return 0
    h = t.process.heap.new_handle(10)
    if not h:
        return 0
    rec.handle = h
    t.mem.write(t.process.heap.deref(h), rec.picture()[:10])
    t.tb.qd.pictures[port.addr] = rec
    port.pic_save = h
    port.pn_vis = port.pn_vis - 1
    return h


@trap
def OpenPicture(t, r):
    return _open(t, pict.Recorder(t.rect(r)))


@trap
def OpenCPicture(t, params):
    """OpenCPicParams: srcRect, hRes, vRes, version, two reserved.  A
    version 2 picture with an extended header; its frame is srcRect at
    72 dpi."""
    src = t.rect(params)
    h_res, v_res = t.mem.r32(params + 8) or FIXED_72, t.mem.r32(params + 12) or FIXED_72

    def at72(n: int, res: int) -> int:
        return n * FIXED_72 // res
    frame = Rect(at72(src.top, v_res), at72(src.left, h_res),
                 at72(src.bottom, v_res), at72(src.right, h_res))
    return _open(t, pict.Recorder(frame, 2, src, h_res, v_res))


@trap
def ClosePicture(t):
    """Stop recording and put the picture in its handle, or hand it to
    the port's putPicProc, which spools it.  Its picSize is mod 64K, as
    always; GetHandleSize is the real size."""
    qd = t.tb.qd
    port = _port(t)
    if port is None or port.addr not in qd.pictures:
        return
    rec = qd.pictures.pop(port.addr)
    port.pic_save = 0
    port.pn_vis = port.pn_vis + 1
    data = rec.picture()
    h = rec.handle
    heap = t.system.heap_of(h)
    put_pic = _custom_proc(t, port, PUT_PIC_PROC, 0xA8F0)
    if put_pic:
        t.mem.write(heap.deref(h), data[:10])
        buf = heap.new_ptr(SPOOL)
        if not buf:
            return
        for at in range(0, len(data), SPOOL):
            chunk = data[at:at + SPOOL]
            t.mem.write(buf, chunk)
            t.call(put_pic, (("p", buf), ("h", len(chunk))))
        heap.dispose_ptr(buf)
    elif heap.set_handle_size(h, len(data)):
        t.mem.write(heap.deref(h), data)
    else:                               # no room: an empty picture
        t.mem.write(heap.deref(h), rec.picture()[:10])


@trap
def KillPicture(t, h):
    """DisposeHandle for a picture.  One that's a resource (programs do
    kill what GetPicture gave them) is released instead, so the resource
    map doesn't keep a handle that's gone."""
    if not h:
        return
    for addr, rec in list(t.tb.qd.pictures.items()):
        if rec.handle == h:
            del t.tb.qd.pictures[addr]
    heap = t.system.heap_of(h)
    if heap.get_state(h) & RESOURCE:
        t.process.resources.ReleaseResource(h)
    else:
        heap.dispose_handle(h)


@trap
def PicComment(t, kind, size, h):
    """Recorded in the picture being made in the current port, if there
    is one; a comment with no data is a short one."""
    port = _port(t)
    rec = t.tb.qd.pictures.get(port.addr) if port is not None else None
    if rec is None or not port.pic_save:
        return
    p = t.mem.r32(h) & ADDRESS_MASK if h else 0
    rec.comment(kind, t.mem.read(p, size & 0xFFFF) if p and size else None)


# =========================================================================
# The standard procedures (Inside Macintosh I-197)
# =========================================================================
@trap
def SetStdProcs(t, procs):
    for i, (_name, key) in enumerate(PROCS):
        t.mem.w32(procs + 4 * i, t.machine.stub_for(key))


def _std_shape(t, verb: int, r: Rect, path_of) -> None:
    port = _port(t)
    if port is not None and 0 <= verb < len(VERBS):
        t.tb.qd.shape(port, VERBS[verb], path_of, r, port.fill_pat if verb == 4 else None)


@trap
def StdRect(t, verb, r):
    _std_shape(t, verb, t.rect(r), rect_path)


@trap
def StdOval(t, verb, r):
    _std_shape(t, verb, t.rect(r), oval_path)


@trap
def StdRRect(t, verb, r, oval_w, oval_h):
    _std_shape(t, verb, t.rect(r), partial(round_rect_path, oval_w=oval_w, oval_h=oval_h))


@trap
def StdArc(t, verb, r, start, arc):
    _std_shape(t, verb, t.rect(r), partial(arc_path, start=start, arc=arc))


@trap
def StdPoly(t, verb, h):
    port = _port(t)
    if port is None or not 0 <= verb < len(VERBS):
        return
    points = read_poly(t.mem, h)
    if verb == 0:
        t.tb.qd.frame_poly(port, points)
    else:
        t.tb.qd.poly(port, VERBS[verb], points, port.fill_pat if verb == 4 else None)


@trap
def StdRgn(t, verb, h):
    port = _port(t)
    if port is not None and 0 <= verb < len(VERBS):
        t.tb.qd.region(port, VERBS[verb], _rgn(t, h), port.fill_pat if verb == 4 else None)


@trap
def StdLine(t, pt):
    port = _port(t)
    if port is not None:
        t.tb.qd.line_to(port, pt.h, pt.v)


@trap
def StdText(t, count, text, numer, denom):
    """The text at the port's size; numer and denom (a scale) are ignored."""
    port = _port(t)
    if port is not None and count > 0:
        t.tb.qd.draw_text(port, from_mac(t.mem.read(text, count)).translate(CHICAGO_SYMBOLS))


@trap
def StdTxMeas(t, count, text, numer, denom, info):
    """The text's width at the port's size, with its font's FontInfo at
    info.  numer and denom, the scale still to apply, are left as they are,
    since the width is the port's size's already."""
    port = _port(t)
    if port is None:
        return 0
    _swap_port_font(t)
    qd = t.tb.qd
    if info:
        t.mem.write(info, struct.pack(">hhhh", *qd.font_info(port)))
    if count <= 0:
        return 0
    return qd.text_width(port, from_mac(t.mem.read(text, count)).translate(CHICAGO_SYMBOLS))


@trap
def StdBits(t, src_bits, src_r, dst_r, mode, mask_rgn):
    port = _port(t)
    if port is not None:
        t.tb.qd.copy_bits(src_bits, port.addr + 2, t.rect(src_r), t.rect(dst_r), mode,
                          _rgn(t, mask_rgn) if mask_rgn else None, port)


@trap
def StdComment(t, kind, size, h):
    PicComment(t, kind, size, h)


# =========================================================================
# Masked copies (Inside Macintosh IV-24, V-80)
# =========================================================================
def _mask(t, mask_bm: int, mask_r: Rect, dst_r: Rect) -> Region | None:
    """The 1 bits of mask_r in a mask BitMap, mapped onto dst_r."""
    surface, _ = t.tb.qd.bitmap(mask_bm)
    if surface is None or mask_r.empty:
        return None
    return mask_region(surface.image(mask_r), dst_r)


@trap
def CopyMask(t, src, mask, dst, src_r, mask_r, dst_r):
    """CopyBits in srcCopy, but only where the mask has 1s."""
    _copy_masked(t, src, mask, dst, src_r, mask_r, dst_r, srcCopy, 0)


def _copy_masked(t, src, mask, dst, src_r, mask_r, dst_r, mode, mask_rgn) -> None:
    sr, mr, dr = t.rect(src_r), t.rect(mask_r), t.rect(dst_r)
    if sr.empty or dr.empty:
        return
    rgn = _mask(t, mask, mr, dr) if mask else None
    if mask_rgn:
        rgn = _rgn(t, mask_rgn) if rgn is None else rgn.sect(_rgn(t, mask_rgn))
    if rgn is not None and rgn.empty:
        return
    t.tb.qd.copy_bits(src, dst, sr, dr, mode, rgn, _port(t))


@trap
def CopyDeepMask(t, src, mask, dst, src_r, mask_r, dst_r, mode, mask_rgn):
    """CopyMask with a transfer mode and a mask region as well."""
    _copy_masked(t, src, mask, dst, src_r, mask_r, dst_r, mode, mask_rgn)


@trap
def SeedFill(t, src, dst, src_row, dst_row, height, words, seed_h, seed_v):
    """The area around (seed_h, seed_v) of the source that's 0 bits, as
    1 bits in the destination: what a paint bucket would fill."""
    width = 16 * words
    if height <= 0 or words <= 0:
        return
    data = [t.mem.read(src + y * src_row, 2 * words) for y in range(height)]
    for y, row in enumerate(seed_fill(data, width, seed_h, seed_v)):
        t.mem.write(dst + y * dst_row, row)


@trap
def CalcMask(t, src, dst, src_row, dst_row, height, words):
    """The mask that covers the source's 1 bits and the 0 bits they
    enclose: what would draw the source as a solid shape."""
    width = 16 * words
    if height <= 0 or words <= 0:
        return
    data = [t.mem.read(src + y * src_row, 2 * words) for y in range(height)]
    for y, row in enumerate(calc_mask(data, width)):
        t.mem.write(dst + y * dst_row, row)
