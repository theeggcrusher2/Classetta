"""
QuickDraw pictures: DrawPicture's playback and the recording between
OpenPicture and ClosePicture (Inside Macintosh I-158, V-84; Imaging With
QuickDraw, Appendix A).

    picSize     the picture's size, mod 64K
    picFrame    its bounding rectangle
    version 1   $11 $01, then byte opcodes, their data unaligned
    version 2   $0011 $02FF, a $0C00 header, then word opcodes, each on
                a word boundary from the start of the picture
    end         $FF ($00FF)

Playback draws through QuickDraw (qdraw) in the port, mapped from the
frame to the destination rectangle, so it's clipped, uses the transfer
modes and is recorded into a picture open in the port, as any drawing
is.  This QuickDraw is black and white: pixel maps are thresholded
(dithered for ditherCopy), RGB colours become black or white by how
light they are, and pixel patterns draw as their 1-bit pattern.

Recording turns the drawing calls made in a port while a picture is
open into version 1 opcodes (version 2 for OpenCPicture), with a port
setting recorded only when a call uses it and it has changed since it
was last recorded, as QuickDraw does.
"""

from __future__ import annotations

import logging
import struct
from functools import partial
from typing import TYPE_CHECKING, Callable

from PyQt6.QtCore import QRect, Qt
from PyQt6.QtGui import QImage, qGray

from ..memory import from_mac, to_mac
from .api import CHICAGO_SYMBOLS
from .color import rgb_color
from .constants import blackColor, patCopy, srcOr, whiteColor
from .masks import EXPAND
from .qdraw import (MONO_TABLE, Region, arc_path, oval_path, read_rgn, rect_path,
                    round_rect_path, srcCopy, write_rgn)
from .quickdraw import BLACK_PAT, WHITE_PAT, GrafPort
from .types import Point, Rect

if TYPE_CHECKING:
    from .qdraw import QuickDraw

log = logging.getLogger("macrt.pict")

VERBS = ("Frame", "Paint", "Erase", "Invert", "Fill")
SHAPES = {0x30: "Rect", 0x40: "RoundRect", 0x50: "Oval", 0x60: "Arc"}
BITS_OPS = (0x90, 0x91, 0x98, 0x99, 0x9A, 0x9B)
END = 0xFF
DITHER_COPY = 64            # added to a CopyBits mode: dither colours
TRANSPARENT = 36
MAX_SIDE = 8192             # the biggest bitmap a picture may hold, each way
MAX_READ = 64 << 20         # the most a picture is read for, however it ends
MAX_RECORDING = 16 << 20
FIXED_72 = 72 << 16         # 72 dpi, as a Fixed

# The port settings a picture starts with when it's drawn, so recording
# leaves out any that still have these values.
START = {
    "bk_pat": WHITE_PAT, "tx_font": 0, "tx_face": 0, "tx_mode": srcOr, "sp_extra": 0,
    "pn_size": Point(1, 1), "pn_mode": patCopy, "pn_pat": BLACK_PAT, "fill_pat": BLACK_PAT,
    "ov_size": Point(0, 0), "tx_size": 0, "fg_color": blackColor, "bk_color": whiteColor,
}
# Each setting's opcode and data.
SETTINGS = {
    "bk_pat": (0x02, None), "tx_font": (0x03, ">h"), "tx_face": (0x04, ">B"),
    "tx_mode": (0x05, ">h"), "sp_extra": (0x06, ">l"), "pn_size": (0x07, None),
    "pn_mode": (0x08, ">h"), "pn_pat": (0x09, None), "fill_pat": (0x0A, None),
    "ov_size": (0x0B, None), "tx_size": (0x0D, ">h"), "fg_color": (0x0E, ">l"),
    "bk_color": (0x0F, ">l"),
}
PEN = ("pn_size", "pn_mode", "pn_pat")
TEXT = ("tx_font", "tx_face", "tx_mode", "tx_size", "sp_extra")
TO_MAC = {ord(v): k for k, v in CHICAGO_SYMBOLS.items()}


class PictError(ValueError):
    """A picture that can't be read: it ends early or makes no sense."""


# =========================================================================
# PackBits (Inside Macintosh I-470)
# =========================================================================
def unpack_bits(src: bytes, size: int, unit: int = 1) -> bytes:
    """Expand PackBits data to size bytes.  A flag byte n below 128 is
    followed by n + 1 literal units, one above it by a unit to repeat
    257 - n times; 128 is nothing.  Units are bytes, or words for 16-bit
    pixel maps."""
    return unpack_bits_from(src, size, unit)[0]


def unpack_bits_from(src: bytes, size: int, unit: int = 1) -> tuple[bytes, int]:
    """unpack_bits, and how many bytes of src it took."""
    out = bytearray()
    i, n = 0, len(src)
    while i < n and len(out) < size:
        c = src[i]
        i += 1
        if c < 128:
            k = (c + 1) * unit
            out += src[i:i + k]
            i += k
        elif c > 128:
            out += src[i:i + unit] * (257 - c)
            i += unit
    return bytes(out[:size]).ljust(size, b"\0"), min(i, n)


def pack_bits(data: bytes) -> bytes:
    """PackBits: runs of three or more alike as repeats, the rest literal."""
    out = bytearray()
    i, n = 0, len(data)
    while i < n:
        j = i
        while j + 1 < n and data[j + 1] == data[i] and j - i < 127:
            j += 1
        if j - i >= 2:
            out += bytes([257 - (j - i + 1), data[i]])
            i = j + 1
            continue
        start = i
        while i < n and i - start < 128:
            if i + 2 < n and data[i] == data[i + 1] == data[i + 2]:
                break
            i += 1
        out.append(i - start - 1)
        out += data[start:i]
    return bytes(out)


# =========================================================================
# Reading
# =========================================================================
class Reader:
    """A picture's bytes in order: read(n) gives the next n.  pos counts
    from the start of the picture, for version 2's word alignment."""

    def __init__(self, read: Callable[[int], bytes], pos: int = 0):
        self._read = read
        self.pos = pos

    @classmethod
    def of(cls, data: bytes, start: int = 0) -> Reader:
        at = start

        def read(n: int) -> bytes:
            nonlocal at
            chunk = data[at:at + n]
            at += n
            return chunk
        return cls(read, start)

    def read(self, n: int) -> bytes:
        if n <= 0:
            if n < 0:
                raise PictError(f"a length of {n}")
            return b""
        if self.pos + n > MAX_READ:
            raise PictError("the picture is too big")
        data = self._read(n)
        if len(data) != n:
            raise PictError("the picture ends early")
        self.pos += n
        return data

    def unpack(self, fmt: str) -> tuple:
        return struct.unpack(fmt, self.read(struct.calcsize(fmt)))

    def u8(self) -> int:
        return self.read(1)[0]

    def u16(self) -> int:
        return self.unpack(">H")[0]

    def s16(self) -> int:
        return self.unpack(">h")[0]

    def u32(self) -> int:
        return self.unpack(">L")[0]

    def s32(self) -> int:
        return self.unpack(">l")[0]

    def point(self) -> Point:
        return Point(*self.unpack(">hh"))

    def rect(self) -> Rect:
        return Rect(*self.unpack(">hhhh"))

    def align(self) -> None:
        if self.pos & 1:
            self.read(1)

    def rgn(self) -> Region:
        size = self.u16()
        if size < 10:
            raise PictError(f"a region of {size} bytes")
        return Region.from_data(struct.pack(">H", size) + self.read(size - 2))

    def poly(self) -> list[Point]:
        size = self.u16()
        if size < 10:
            raise PictError(f"a polygon of {size} bytes")
        data = self.read(size - 2)
        return [Point(*struct.unpack_from(">hh", data, 8 + 4 * i))
                for i in range((size - 10) // 4)]


def read_ctab(r: Reader) -> list[int]:
    """A ColorTable's colours by pixel value, as 0xAARRGGBB."""
    _seed, flags, size = r.unpack(">lHh")
    if not -1 <= size <= 255:
        raise PictError(f"a colour table of {size + 1} entries")
    table = [0xFF000000] * 256
    for i in range(size + 1):
        value, red, green, blue = r.unpack(">HHHH")
        table[i if flags & 0x8000 else value & 0xFF] = (
            0xFF000000 | (red >> 8) << 16 | (green >> 8) << 8 | blue >> 8)
    return table


def read_pixmap(r: Reader) -> tuple[int, int, int]:
    """The rest of a PixMap after rowBytes and bounds: (packType,
    pixelSize, cmpCount); the rest doesn't matter here."""
    (_version, pack_type, _pack_size, _h_res, _v_res, _pixel_type, pixel_size,
     cmp_count, _cmp_size, _plane_bytes, _table, _reserved) = r.unpack(">hhlllhhhhlll")
    return pack_type, pixel_size, cmp_count


def read_pixels(r: Reader, row_bytes: int, height: int, packed: bool, pack_type: int = 0,
                width: int = 0, pixel_size: int = 1, cmp_count: int = 1) -> list[bytes]:
    """Pixel data, row by row.  Rows narrower than 8 bytes are never
    packed; packed rows each start with their length, a byte if rowBytes
    is 250 or less, else a word.  packType 1 is unpacked, 2 is 32-bit
    pixels as 3 bytes, unpacked, 3 is PackBits by the word (16-bit), 4 is
    PackBits of each component's row in turn (32-bit)."""
    if pack_type == 0:
        pack_type = {16: 3, 32: 4}.get(pixel_size, 0)
    if not packed or row_bytes < 8 or pack_type == 1:
        return [r.read(row_bytes) for _ in range(height)]
    if pack_type == 2:
        return [r.read(3 * width) for _ in range(height)]
    rows = []
    for _ in range(height):
        data = r.read(r.u16() if row_bytes > 250 else r.u8())
        if pack_type == 3:
            rows.append(unpack_bits(data, row_bytes, 2))
        elif pack_type == 4:
            rows.append(unpack_bits(data, cmp_count * width))
        else:
            rows.append(unpack_bits(data, row_bytes))
    return rows


def _split(depth: int) -> list[bytes]:
    """For each pixel of a byte at depth bits: a table from the byte to it."""
    per = 8 // depth
    mask = (1 << depth) - 1
    return [bytes((b >> (8 - depth * (k + 1))) & mask for b in range(256)) for k in range(per)]


SPLIT = {2: _split(2), 4: _split(4)}


def pixel_image(rows: list[bytes], width: int, height: int, pixel_size: int,
                ctab: list[int] | None, pack_type: int = 0, cmp_count: int = 3) -> QImage:
    """An image of pixel rows: 1-bit black and white (MONO_TABLE), 1- to
    8-bit through a colour table, or 16- or 32-bit direct."""
    if pixel_size == 1 and ctab is None:
        bpl = (width + 7) // 8
        img = QImage(b"".join(row[:bpl].ljust(bpl, b"\0") for row in rows), width, height,
                     bpl, QImage.Format.Format_Mono)
        img.setColorTable(MONO_TABLE)
        return img.copy()
    if pixel_size <= 8:
        out = bytearray()
        for row in rows:
            if pixel_size == 8:
                px = row[:width]
            elif pixel_size == 1:
                px = b"".join(EXPAND[b] for b in row)[:width]
            else:
                per = 8 // pixel_size
                px = bytearray(len(row) * per)
                for k, table in enumerate(SPLIT[pixel_size]):
                    px[k::per] = row.translate(table)
                px = bytes(px[:width])
            out += px.ljust(width, b"\0")
        img = QImage(bytes(out), width, height, width, QImage.Format.Format_Indexed8)
        img.setColorTable(ctab or MONO_TABLE + [0xFF000000] * 254)
        return img.copy()
    if pixel_size == 16:
        out = bytearray()
        for row in rows:
            row = row[:2 * width].ljust(2 * width, b"\0")
            swapped = bytearray(len(row))
            swapped[0::2], swapped[1::2] = row[1::2], row[0::2]
            out += swapped
        return QImage(bytes(out), width, height, 2 * width,
                      QImage.Format.Format_RGB555).copy()
    out = bytearray()
    for row in rows:
        rgb = bytearray(3 * width)
        if pack_type == 2:
            rgb[:] = row[:3 * width].ljust(3 * width, b"\0")
        elif len(row) >= cmp_count * width and pack_type != 1 and len(row) != 4 * width:
            at = width if cmp_count == 4 else 0             # alpha first, if it's there
            for c in range(3):
                rgb[c::3] = row[at + c * width:at + (c + 1) * width]
        else:
            row = row[:4 * width].ljust(4 * width, b"\0")
            for c in range(3):
                rgb[c::3] = row[c + 1::4]
        out += rgb
    return QImage(bytes(out), width, height, 3 * width, QImage.Format.Format_RGB888).copy()


def to_mono(img: QImage, dither: bool = False) -> QImage:
    """img in black and white, 1 bits black: thresholded, or dithered."""
    if img.format() == QImage.Format.Format_Mono and img.colorTable() == MONO_TABLE:
        return img
    gray = img.convertToFormat(QImage.Format.Format_Grayscale8)
    flag = (Qt.ImageConversionFlag.DiffuseDither if dither
            else Qt.ImageConversionFlag.ThresholdDither)
    mono = gray.convertToFormat(QImage.Format.Format_Mono, flag)
    table = mono.colorTable()
    if table and qGray(table[0]) < 128:             # Qt chose 0 for black
        mono.invertPixels()
    mono.setColorTable(MONO_TABLE)
    return mono


# =========================================================================
# Playback
# =========================================================================
class Player:
    """Plays a picture's opcodes into a port, mapped from frame to dst.
    Pen and text positions are kept in the picture's coordinates."""

    def __init__(self, qd: QuickDraw, port: GrafPort, frame: Rect, dst: Rect,
                 clip: Region, comment: Callable[[int, bytes | None], None] | None = None):
        self.qd = qd
        self.port = port
        self.frame = frame
        self.dst = dst
        self.clip = clip                    # the port's clipRgn, which pictures' clips are within
        self.comment = comment
        self.v2 = False
        self.origin = (0, 0)
        self.pen = Point(0, 0)
        self.text_loc = Point(0, 0)
        self.pn_size = Point(1, 1)
        self.ov_size = Point(0, 0)
        self.tx_size = 0
        self.ratio = (1, 1, 1, 1)           # TxRatio: numer v, h; denom v, h
        self.last_rect = Rect(0, 0, 0, 0)
        self.last_poly: list[Point] = []
        self.last_rgn = Region()

    # -- mapping from the picture to the port --------------------------
    def _h(self, x: int) -> int:
        f, d = self.frame, self.dst
        x -= self.origin[0]
        return d.left + (x - f.left) * d.width // f.width if f.width else d.left

    def _v(self, y: int) -> int:
        f, d = self.frame, self.dst
        y -= self.origin[1]
        return d.top + (y - f.top) * d.height // f.height if f.height else d.top

    def map_pt(self, pt: Point) -> Point:
        return Point(self._v(pt.v), self._h(pt.h))

    def map_rect(self, r: Rect) -> Rect:
        return Rect(self._v(r.top), self._h(r.left), self._v(r.bottom), self._h(r.right))

    def scale(self, pt: Point) -> Point:
        """A size scaled as ScalePt scales it: nothing stays nothing,
        anything else is at least 1."""
        f, d = self.frame, self.dst

        def one(n: int, a: int, b: int) -> int:
            return 0 if n <= 0 or not a else max(n * b // a, 1)
        return Point(one(pt.v, f.height, d.height), one(pt.h, f.width, d.width))

    def map_rgn(self, rgn: Region) -> Region:
        """A region mapped as MapRgn maps it, its bands and spans scaled;
        those that shrink to nothing go."""
        f, d = self.frame, self.dst
        if f.width == d.width and f.height == d.height:
            return rgn.offset(d.left - f.left - self.origin[0], d.top - f.top - self.origin[1])
        bands: list = []
        for top, bottom, xs in rgn.bands:
            top, bottom = self._v(top), self._v(bottom)
            if bottom <= top:
                continue
            spans: list[int] = []
            for i in range(0, len(xs) - 1, 2):
                left, right = self._h(xs[i]), self._h(xs[i + 1])
                if left >= right:
                    continue
                if spans and spans[-1] >= left:     # spans that now touch are one
                    spans[-1] = max(spans[-1], right)
                else:
                    spans += [left, right]
            if not spans:
                continue
            if bands and bands[-1][1] == top and bands[-1][2] == tuple(spans):
                bands[-1] = (bands[-1][0], bottom, bands[-1][2])
            else:
                bands.append((top, bottom, tuple(spans)))
        return Region(bands)

    # -- the opcodes -----------------------------------------------------
    def play(self, r: Reader) -> None:
        while True:
            if self.v2:
                r.align()
                op = r.u16()
            else:
                op = r.u8()
            if op == END:
                return
            self.op(op, r)

    def op(self, op: int, r: Reader) -> None:
        port = self.port
        if op == 0x01:
            self._clip(r.rgn())
        elif op in (0x02, 0x09, 0x0A):
            setattr(port, {2: "bk_pat", 9: "pn_pat", 10: "fill_pat"}[op], r.read(8))
        elif op == 0x03:
            port.tx_font = r.s16()
        elif op == 0x04:
            port.tx_face = r.u8()
        elif op == 0x05:
            port.tx_mode = r.s16()
        elif op == 0x06:
            port.sp_extra = r.s32()
        elif op == 0x07:
            self.pn_size = r.point()
            port.pn_size = self.scale(self.pn_size)
        elif op == 0x08:
            port.pn_mode = r.s16()
        elif op == 0x0B:
            self.ov_size = r.point()
        elif op == 0x0C:
            dh, dv = r.unpack(">hh")
            self.origin = (self.origin[0] + dh, self.origin[1] + dv)
        elif op == 0x0D:
            self.tx_size = r.s16()
            self._text_size()
        elif op in (0x0E, 0x0F):
            setattr(port, "fg_color" if op == 0x0E else "bk_color", r.s32())
        elif op == 0x10:
            self.ratio = r.unpack(">hhhh")
            self._text_size()
        elif op == 0x11:
            if r.u8() == 2:                 # $0011 $02FF: version 2 from here on
                r.u8()
                self.v2 = True
        elif 0x12 <= op <= 0x14:
            setattr(port, ("bk_pat", "pn_pat", "fill_pat")[op - 0x12], self._pix_pat(r))
        elif op in (0x15, 0x16):            # PnLocHFrac, ChExtra
            r.read(2)
        elif op in (0x1A, 0x1B):
            setattr(port, "fg_color" if op == 0x1A else "bk_color", rgb_color(*r.unpack(">HHH")))
        elif op in (0x1D, 0x1F):            # HiliteColor, OpColor
            r.read(6)
        elif op == 0x20:
            a = r.point()
            self._line(a, r.point())
        elif op == 0x21:
            self._line(self.pen, r.point())
        elif op == 0x22:
            a = r.point()
            dh, dv = r.unpack(">bb")
            self._line(a, a.offset(dh, dv))
        elif op == 0x23:
            dh, dv = r.unpack(">bb")
            self._line(self.pen, self.pen.offset(dh, dv))
        elif op == 0x28:
            self.text_loc = r.point()
            self._text(r)
        elif op == 0x29:
            self.text_loc = self.text_loc.offset(r.u8(), 0)
            self._text(r)
        elif op == 0x2A:
            self.text_loc = self.text_loc.offset(0, r.u8())
            self._text(r)
        elif op == 0x2B:
            dh, dv = r.u8(), r.u8()
            self.text_loc = self.text_loc.offset(dh, dv)
            self._text(r)
        elif 0x30 <= op <= 0x6F:
            self._shape(op, r)
        elif 0x70 <= op <= 0x7F:
            if op < 0x78:
                self.last_poly = r.poly()
            self._poly(op & 7)
        elif 0x80 <= op <= 0x8F:
            if op < 0x88:
                self.last_rgn = r.rgn()
            if op & 7 < 5:
                fill = port.fill_pat if op & 7 == 4 else None
                self.qd.region(port, VERBS[op & 7], self.map_rgn(self.last_rgn), fill)
        elif op in BITS_OPS:
            self._bits(op, r)
        elif op == 0xA0:
            self._comment(r.u16(), None)
        elif op == 0xA1:
            kind, size = r.unpack(">hH")
            self._comment(kind, r.read(size))
        elif op == 0x0C00:
            self._header(r.read(24))
        else:
            skip(op, r)

    def _clip(self, rgn: Region) -> None:
        h = self.port.clip_rgn
        if h:
            write_rgn(self.qd.sys.heap_of(h), h, self.map_rgn(rgn).sect(self.clip))

    def _header(self, data: bytes) -> None:
        """Version 2's header.  An extended one (-2) says what rectangle,
        at what resolution, the picture was drawn in: its coordinates are
        in that, and the frame is it at 72 dpi."""
        version, = struct.unpack_from(">h", data)
        if version == -2:
            src = Rect(*struct.unpack_from(">hhhh", data, 12))
            if not src.empty:
                self.frame = src

    def _text_size(self) -> None:
        size = self.tx_size
        nv, _nh, dv, _dh = self.ratio
        f, d = self.frame, self.dst
        if (d.height != f.height or nv != dv) and f.height and dv:
            size = max(round((size or 12) * d.height * nv / (f.height * dv)), 1)
        self.port.tx_size = size

    def _pix_pat(self, r: Reader) -> bytes:
        """A PixPat: its type, its 1-bit pattern, and then an RGB colour
        (type 2) or a pixel map, colour table and pixels (type 1).  The
        1-bit pattern is what draws."""
        kind = r.u16()
        pat = r.read(8)
        if kind == 2:
            r.read(6)
        elif kind == 1:
            row_bytes = r.u16() & 0x3FFF
            bounds = r.rect()
            pack_type, pixel_size, cmp_count = read_pixmap(r)
            read_ctab(r)
            read_pixels(r, row_bytes, max(bounds.height, 0), True, pack_type, bounds.width,
                        pixel_size, cmp_count)
        return pat

    def _line(self, a: Point, b: Point) -> None:
        self.pen = b
        self.port.pn_loc = self.map_pt(a)
        end = self.map_pt(b)
        self.qd.line_to(self.port, end.h, end.v)

    def _text(self, r: Reader) -> None:
        text = from_mac(r.read(r.u8()))
        self.port.pn_loc = self.map_pt(self.text_loc)
        self.qd.draw_text(self.port, text.translate(CHICAGO_SYMBOLS))

    def _shape(self, op: int, r: Reader) -> None:
        family, same, verb = op & 0xF0, op & 8, op & 7
        if not same:
            self.last_rect = r.rect()
        angles = r.unpack(">hh") if family == 0x60 else ()
        if verb >= len(VERBS):
            return
        kind = SHAPES[family]
        if kind == "Rect":
            path_of = rect_path
        elif kind == "Oval":
            path_of = oval_path
        elif kind == "RoundRect":
            ov = self.scale(self.ov_size)
            path_of = partial(round_rect_path, oval_w=ov.h, oval_h=ov.v)
        else:
            path_of = partial(arc_path, start=angles[0], arc=angles[1])
        fill = self.port.fill_pat if verb == 4 else None
        self.qd.shape(self.port, VERBS[verb], path_of, self.map_rect(self.last_rect), fill)

    def _poly(self, verb: int) -> None:
        points = [self.map_pt(pt) for pt in self.last_poly]
        if verb == 0:
            self.qd.frame_poly(self.port, points)
        elif verb < len(VERBS):
            self.qd.poly(self.port, VERBS[verb], points,
                         self.port.fill_pat if verb == 4 else None)

    def _bits(self, op: int, r: Reader) -> None:
        """BitsRect, BitsRgn, PackBitsRect, PackBitsRgn, DirectBitsRect,
        DirectBitsRgn: a bitmap or pixel map, source and destination
        rectangles, a mode, a mask region for the Rgn ones, then the bits."""
        direct = op in (0x9A, 0x9B)
        if direct:
            r.read(4)                       # baseAddr, $000000FF
        row_bytes = r.u16()
        bounds = r.rect()
        ctab = None
        pack_type, pixel_size, cmp_count = 0, 1, 1
        if direct or row_bytes & 0x8000:
            pack_type, pixel_size, cmp_count = read_pixmap(r)
            if not direct:
                ctab = read_ctab(r)
        row_bytes &= 0x3FFF
        src, dst = r.rect(), r.rect()
        mode = r.u16()
        mask = self.map_rgn(r.rgn()) if op & 1 else None
        width, height = bounds.width, bounds.height
        if (not 0 < width <= MAX_SIDE or not 0 < height <= MAX_SIDE
                or pixel_size not in (1, 2, 4, 8, 16, 32)):
            raise PictError(f"a {width}x{height} {pixel_size}-bit bitmap")
        rows = read_pixels(r, row_bytes, height, op >= 0x98, pack_type, width, pixel_size,
                           cmp_count)
        img = pixel_image(rows, width, height, pixel_size, ctab, pack_type, cmp_count)
        img = to_mono(img, bool(mode & DITHER_COPY))
        if src.empty:
            return
        img = img.copy(QRect(src.left - bounds.left, src.top - bounds.top, src.width, src.height))
        self.qd.draw_bits(self.port.addr + 2, self.map_rect(dst), img, transfer_mode(mode),
                          mask, self.port)

    def _comment(self, kind: int, data: bytes | None) -> None:
        if self.comment is not None:
            self.comment(kind, data)


def transfer_mode(mode: int) -> int:
    """The black-and-white mode for a CopyBits mode: Color QuickDraw's
    arithmetic modes copy (transparent ORs), and ditherCopy is a flag."""
    mode &= ~DITHER_COPY & 0xFFFF
    if mode >= 32:
        return srcOr if mode == TRANSPARENT else srcCopy
    return mode & 7


def skip(op: int, r: Reader) -> None:
    """Pass over an opcode this QuickDraw ignores or that's reserved, by
    the sizes Imaging With QuickDraw A-3 gives the reserved ranges."""
    if op <= 0xFF:
        if 0x24 <= op <= 0x2F or 0x92 <= op <= 0x9F or 0xA2 <= op <= 0xAF:
            r.read(r.u16())
        elif 0xD0 <= op <= 0xFE:
            r.read(r.u32())
    elif op <= 0x7FFF:
        r.read((op >> 8) * 2)
    elif op >= 0x8100:
        r.read(r.u32())


def draw_picture(qd: QuickDraw, port: GrafPort, frame: Rect, r: Reader, dst: Rect,
                 comment: Callable[[int, bytes | None], None] | None = None) -> None:
    """DrawPicture: play the opcodes r reads, those after the picture's
    size and frame, in port, mapped from frame to dst.  The picture starts
    with the port's settings reset (the pen 1 by 1 however it's scaled)
    and they're put back after; one that can't be read is drawn as far as
    it can be."""
    if frame.empty or dst.empty:
        return
    mem = port.mem
    saved = mem.read(port.addr + 32, 56)            # bkPat to bkColor
    clip = read_rgn(mem, port.clip_rgn)
    for field, value in START.items():
        if field != "ov_size":
            setattr(port, field, value)
    port.pn_loc = Point(0, 0)
    player = Player(qd, port, frame, dst, clip, comment)
    try:
        player.play(r)
    except (ValueError, struct.error, IndexError, OverflowError) as e:
        log.info("DrawPicture stopped: %s", e)
    finally:
        mem.write(port.addr + 32, saved)
        if port.clip_rgn:
            write_rgn(qd.sys.heap_of(port.clip_rgn), port.clip_rgn, clip)


def picture_frame(data: bytes) -> Rect:
    return Rect(*struct.unpack_from(">hhhh", data, 2)) if len(data) >= 10 else Rect(0, 0, 0, 0)


# =========================================================================
# Recording
# =========================================================================
class Recorder:
    """What's drawn in a port between OpenPicture and ClosePicture, as a
    picture.  qdraw calls it with each drawing call and the port, before
    the call changes anything."""

    def __init__(self, frame: Rect, version: int = 1, src: Rect | None = None,
                 h_res: int = FIXED_72, v_res: int = FIXED_72):
        self.frame = frame
        self.v2 = version == 2
        self.data = bytearray()
        self.state: dict[str, object] = {}
        self.clip: Region | None = None
        self.last_rect: Rect | None = None
        self.pen: Point | None = None           # where the last line ended
        self.text_loc = Point(0, 0)
        self.full = False
        self.handle = 0                         # the PicHandle, for the glue
        if self.v2:
            self._op(0x0011)
            self.data += b"\x02\xff"
            self._op(0x0C00)
            self.data += struct.pack(">hhll", -2, 0, h_res, v_res)
            self.data += struct.pack(">hhhh", *(src or frame)) + bytes(4)
        else:
            self.data += b"\x11\x01"

    def picture(self) -> bytes:
        """The whole picture so far, ended."""
        if self.v2:
            data = bytes(self.data) + (b"\0" if len(self.data) & 1 else b"") + b"\0\xff"
        else:
            data = bytes(self.data) + b"\xff"
        return struct.pack(">Hhhhh", (10 + len(data)) & 0xFFFF, *self.frame) + data

    # -- writing ---------------------------------------------------------
    def _op(self, op: int) -> None:
        if self.v2:
            if len(self.data) & 1:          # the picture starts at an even 10 bytes in
                self.data.append(0)
            self.data += struct.pack(">H", op)
        else:
            self.data.append(op & 0xFF)

    def _recording(self) -> bool:
        if len(self.data) > MAX_RECORDING:
            self.full = True
        return not self.full

    def _set(self, field: str, value) -> None:
        if self.state.get(field, START[field]) == value:
            return
        op, fmt = SETTINGS[field]
        self._op(op)
        if isinstance(value, Point):
            self.data += struct.pack(">hh", value.v, value.h)
        elif fmt is None:
            self.data += bytes(value)
        else:
            self.data += struct.pack(fmt, value)
        self.state[field] = value

    def _sync(self, port: GrafPort, fields: tuple = (), **values) -> None:
        """Record the clip region, the colours and the settings a call
        uses, those that have changed."""
        clip = read_rgn(port.mem, port.clip_rgn)
        if clip != self.clip:
            try:
                data = clip.to_data()
            except struct.error:            # too big a region for a picture: its box
                data = Region.rect(clip.bbox).to_data()
            self._op(0x01)
            self.data += data
            self.clip = clip
        self._set("fg_color", port.fg_color)
        self._set("bk_color", port.bk_color)
        for field in fields:
            self._set(field, getattr(port, field))
        for field, value in values.items():
            self._set(field, value)

    def _verb(self, port: GrafPort, verb: str, pat: bytes | None, **values) -> int:
        if verb in ("Frame", "Paint"):
            self._sync(port, PEN, **values)
        elif verb == "Erase":
            self._sync(port, ("bk_pat",), **values)
        elif verb == "Fill":
            self._sync(port, fill_pat=bytes(pat if pat is not None else BLACK_PAT), **values)
        else:
            self._sync(port, **values)
        return VERBS.index(verb)

    # -- the calls -------------------------------------------------------
    def shape(self, port: GrafPort, verb: str, path_of, r: Rect, pat: bytes | None) -> None:
        if not self._recording():
            return
        func = getattr(path_of, "func", path_of)
        kw = getattr(path_of, "keywords", {})
        extra: dict = {}
        if func is rect_path:
            base, angles = 0x30, b""
        elif func is oval_path:
            base, angles = 0x50, b""
        elif func is round_rect_path:
            base, angles = 0x40, b""
            extra["ov_size"] = Point(kw.get("oval_h", 0), kw.get("oval_w", 0))
        elif func is arc_path:
            base, angles = 0x60, struct.pack(">hh", kw.get("start", 0), kw.get("arc", 0))
        else:                               # some other shape: the pixels it covers
            self.region(port, verb, Region.from_path(path_of(r)), pat)
            return
        n = self._verb(port, verb, pat, **extra)
        if r == self.last_rect:
            self._op(base + 8 + n)
        else:
            self._op(base + n)
            self.data += struct.pack(">hhhh", *r)
            self.last_rect = r
        self.data += angles

    def region(self, port: GrafPort, verb: str, rgn: Region, pat: bytes | None) -> None:
        if not self._recording():
            return
        try:
            data = rgn.to_data()
        except struct.error:
            return
        self._op(0x80 + self._verb(port, verb, pat))
        self.data += data

    def poly(self, port: GrafPort, verb: str, points: list[Point], pat: bytes | None) -> None:
        if not self._recording() or not points or len(points) > 16000:
            return
        self._op(0x70 + self._verb(port, verb, pat))
        bbox = (min(p.v for p in points), min(p.h for p in points),
                max(p.v for p in points), max(p.h for p in points))
        self.data += struct.pack(">Hhhhh", 10 + 4 * len(points), *bbox)
        for p in points:
            self.data += struct.pack(">hh", p.v, p.h)

    def line(self, port: GrafPort, a: Point, b: Point) -> None:
        if not self._recording():
            return
        self._sync(port, PEN)
        dh, dv = b.h - a.h, b.v - a.v
        short = -128 <= dh <= 127 and -128 <= dv <= 127
        if a == self.pen:
            self._op(0x23 if short else 0x21)
        else:
            self._op(0x22 if short else 0x20)
            self.data += struct.pack(">hh", a.v, a.h)
        self.data += struct.pack(">bb", dh, dv) if short else struct.pack(">hh", b.v, b.h)
        self.pen = b

    def text(self, port: GrafPort, text: str, width: Callable[[str], int]) -> None:
        if not self._recording():
            return
        self._sync(port, TEXT)
        loc = port.pn_loc
        while text:
            chunk, text = text[:255], text[255:]
            data = to_mac(chunk.translate(TO_MAC))
            dh, dv = loc.h - self.text_loc.h, loc.v - self.text_loc.v
            if dv == 0 and 0 <= dh <= 255:
                self._op(0x29)
                self.data.append(dh)
            elif dh == 0 and 0 <= dv <= 255:
                self._op(0x2A)
                self.data.append(dv)
            elif 0 <= dh <= 255 and 0 <= dv <= 255:
                self._op(0x2B)
                self.data += bytes([dh, dv])
            else:
                self._op(0x28)
                self.data += struct.pack(">hh", loc.v, loc.h)
            self.data += bytes([len(data)]) + data
            self.text_loc = loc
            if text:
                loc = loc.offset(width(chunk), 0)

    def bits(self, port: GrafPort, img: QImage, dst: Rect, mode: int,
             mask: Region | None) -> None:
        """CopyBits into the port: the bits as a 1-bit bitmap, packed
        if its rows are 8 bytes or more."""
        width, height = img.width(), img.height()
        row_bytes = (width + 15) // 16 * 2
        if not self._recording() or not width or not height or row_bytes >= 0x4000:
            return
        mask_data = b""
        if mask is not None:
            try:
                mask_data = mask.to_data()
            except struct.error:
                mask_data = Region.rect(mask.bbox).to_data()
        self._sync(port)
        mono = to_mono(img)
        bpl = mono.bytesPerLine()
        ptr = mono.constBits()
        ptr.setsize(mono.sizeInBytes())
        raw = bytes(ptr)
        packed = row_bytes >= 8
        self._op((0x98 if packed else 0x90) + (1 if mask is not None else 0))
        bounds = struct.pack(">hhhh", 0, 0, height, width)
        self.data += struct.pack(">H", row_bytes) + bounds + bounds
        self.data += struct.pack(">hhhhh", *dst, mode) + mask_data
        for y in range(height):
            row = raw[y * bpl:y * bpl + row_bytes]
            if packed:
                p = pack_bits(row)
                self.data += (struct.pack(">H", len(p)) if row_bytes > 250
                              else bytes([len(p)])) + p
            else:
                self.data += row

    def comment(self, kind: int, data: bytes | None) -> None:
        if not self._recording():
            return
        if data is None:
            self._op(0xA0)
            self.data += struct.pack(">h", kind)
        else:
            self._op(0xA1)
            self.data += struct.pack(">hH", kind, len(data)) + data
