"""QuickDraw's drawing, checked bit by bit in 1-bit bitmaps in guest
memory, and its regions, checked against QuickDraw's own region format."""

import pytest
from PyQt6.QtGui import QColor, QFont

from macrt.toolbox import qdraw
from macrt.toolbox.qdraw import Region, oval_path, rect_path
from macrt.toolbox.quickdraw import BLACK_PAT, GRAY_PAT, Backing, GrafPort
from macrt.toolbox.system import System
from macrt.toolbox.types import Point, Rect

W, H = 64, 32                   # the offscreen bitmap
ROW_BYTES = W // 8


@pytest.fixture
def qd(qapp):
    system = System()
    return qdraw.QuickDraw(system, qdraw.Fonts(QFont()), lambda port: None)


def offscreen(qd, bounds=Rect(0, 0, H, W)):
    """A port on a fresh, white bitmap; returns it and the bitmap's address."""
    heap = qd.sys.sys_heap
    bits = heap.new_ptr(ROW_BYTES * bounds.height, clear=True)
    port = GrafPort(qd.mem, heap.new_ptr(GrafPort.SIZE))
    port.open(heap, qd.sys.screen_bits)
    port.port_bits_base = bits
    port.port_bits_row_bytes = ROW_BYTES
    port.port_bits_bounds = bounds
    port.port_rect = bounds
    qdraw.write_rgn(heap, port.vis_rgn, Region.rect(bounds))
    qd.sys.register(port)
    return port, bits


def rows(qd, bits, n=H):
    """The bitmap as strings of . and #."""
    out = []
    for y in range(n):
        row = qd.mem.read(bits + y * ROW_BYTES, ROW_BYTES)
        out.append("".join("#" if row[x >> 3] & (0x80 >> (x & 7)) else "." for x in range(W)))
    return out


def ones(qd, bits):
    return sum(r.count("#") for r in rows(qd, bits))


# -- regions -------------------------------------------------------------------
def test_region_data_is_quickdraws():
    # Two overlapping squares, as QuickDraw stores their union: per row
    # where something changes, the h coordinates where it changes, $7FFF.
    rgn = Region.rect(Rect(0, 0, 10, 10)).union(Region.rect(Rect(5, 5, 15, 15)))
    assert rgn.to_data().hex() == (
        "002c" "00000000000f000f"
        "0000" "0000000a" "7fff"
        "0005" "000a000f" "7fff"
        "000a" "00000005" "7fff"
        "000f" "0005000f" "7fff"
        "7fff")
    assert Region.from_data(rgn.to_data()) == rgn
    assert Region.rect(Rect(1, 2, 3, 4)).to_data().hex() == "000a0001000200030004"


def test_region_operations():
    a, b = Region.rect(Rect(0, 0, 10, 10)), Region.rect(Rect(5, 5, 15, 15))
    assert a.sect(b) == Region.rect(Rect(5, 5, 10, 10))
    assert a.diff(b).bbox == Rect(0, 0, 10, 10)
    assert not a.diff(b).contains(Point(7, 7)) and a.diff(b).contains(Point(7, 2))
    x = a.xor(b)
    assert x.contains(Point(2, 2)) and x.contains(Point(12, 12))
    assert not x.contains(Point(7, 7))
    assert a.union(b).inset(1, 1).bbox == Rect(1, 1, 14, 14)
    assert a.inset(-2, -3).bbox == Rect(-3, -2, 13, 12)
    assert a.inset(5, 5).empty
    assert a.offset(3, 4).bbox == Rect(4, 3, 14, 13)
    assert a.diff(a).empty and Region().union(a) == a


def test_shape_regions():
    oval = Region.from_path(oval_path(Rect(0, 0, 8, 8)))
    assert oval.bbox == Rect(0, 0, 8, 8)
    assert not oval.contains(Point(0, 0)) and oval.contains(Point(4, 4))


# -- drawing in a bitmap -----------------------------------------------------
def test_paint_and_frame_rect(qd):
    port, bits = offscreen(qd)
    qd.shape(port, "Paint", rect_path, Rect(1, 2, 3, 6))
    qd.shape(port, "Frame", rect_path, Rect(5, 0, 9, 8))
    assert rows(qd, bits, 10) == [
        "." * W,
        "..####" + "." * (W - 6),
        "..####" + "." * (W - 6),
        "." * W,
        "." * W,
        "########" + "." * (W - 8),
        "#......#" + "." * (W - 8),
        "#......#" + "." * (W - 8),
        "########" + "." * (W - 8),
        "." * W,
    ]


def test_an_empty_rectangle_draws_nothing(qd):
    # MacGammon frames one whose top is below its bottom.
    port, bits = offscreen(qd)
    for verb in ("Frame", "Paint", "Fill", "Invert"):
        qd.shape(port, verb, rect_path, Rect(30, 4, 10, 40))
        qd.shape(port, verb, oval_path, Rect(2, 40, 20, 30))
    assert ones(qd, bits) == 0


def test_pen_size_and_lines(qd):
    port, bits = offscreen(qd)
    port.pn_size = Point(2, 3)                      # 3 wide, 2 high
    qd.move_to(port, 4, 1)
    qd.line_to(port, 10, 1)
    r = rows(qd, bits, 4)
    assert r[1][4:13] == "#########" and r[2][4:13] == "#########"
    assert r[1][3] == "." and r[1][13] == "." and r[3].count("#") == 0
    assert port.pn_loc == Point(1, 10)


def test_patterns_line_up_with_the_bitmap(qd):
    port, bits = offscreen(qd)
    port.pn_pat = GRAY_PAT
    qd.shape(port, "Paint", rect_path, Rect(0, 0, 2, 8))
    qd.shape(port, "Paint", rect_path, Rect(0, 9, 2, 17))  # same pattern phase, a pixel along
    r = rows(qd, bits, 2)
    assert r[0][:8] == "#.#.#.#." and r[1][:8] == ".#.#.#.#"
    assert r[0][9:17] == ".#.#.#.#"


@pytest.mark.parametrize("mode, expect", [
    (qdraw.patCopy, "####...."),
    (qdraw.patOr, "####.#.#"),
    (qdraw.patXor, ".....#.#"),
    (qdraw.patBic, ".....#.#"),
    (qdraw.notPatCopy, "....####"),
    (qdraw.notPatOr, "########"),
    (qdraw.notPatXor, "#####.#."),
    (qdraw.notPatBic, "####...."),
])
def test_pattern_transfer_modes(qd, mode, expect):
    port, bits = offscreen(qd)
    qd.mem.w8(bits, 0b11110101)                     # what's there
    port.pn_pat = bytes([0xF0] * 8)                 # what's painted
    port.pn_mode = mode
    qd.shape(port, "Paint", rect_path, Rect(0, 0, 1, 8))
    assert rows(qd, bits, 1)[0][:8] == expect


def test_drawing_is_clipped_to_the_clip_region(qd):
    port, bits = offscreen(qd)
    clip = Region.rect(Rect(0, 0, 4, 4)).union(Region.rect(Rect(4, 4, 8, 8)))
    qdraw.write_rgn(qd.sys.sys_heap, port.clip_rgn, clip)
    qd.shape(port, "Paint", rect_path, Rect(0, 0, H, W))
    r = rows(qd, bits, 9)
    assert r[0][:9] == "####....." and r[5][:9] == "....####."
    assert ones(qd, bits) == 32


def test_hidden_pen_draws_nothing(qd):
    port, bits = offscreen(qd)
    port.pn_vis = -1
    qd.shape(port, "Paint", rect_path, Rect(0, 0, 8, 8))
    qd.shape(port, "Frame", rect_path, Rect(0, 0, 8, 8))
    assert ones(qd, bits) == 0
    qd.shape(port, "Erase", rect_path, Rect(0, 0, 8, 8))
    qd.shape(port, "Invert", rect_path, Rect(0, 0, 2, 2))      # not pen drawing
    assert ones(qd, bits) == 4


def test_local_coordinates_follow_the_origin(qd):
    port, bits = offscreen(qd, Rect(100, 200, 100 + H, 200 + W))
    qd.shape(port, "Paint", rect_path, Rect(100, 200, 101, 202))
    assert rows(qd, bits, 1)[0][:3] == "##."


def test_text_moves_the_pen(qd):
    port, bits = offscreen(qd)
    qd.move_to(port, 2, 20)
    width = qd.text_width(port, "Hi")
    qd.draw_text(port, "Hi")
    assert port.pn_loc == Point(20, 2 + width)
    assert ones(qd, bits) > 10


def test_text_a_character_at_a_time_is_the_same_text(qd):
    # As word processors measure and draw it: every character its own width.
    text = "Wavy, jolly text."
    port, whole = offscreen(qd)
    qd.move_to(port, 2, 20)
    qd.draw_text(port, text)
    assert qd.text_width(port, text) == sum(qd.text_width(port, ch) for ch in text)
    port2, pieces = offscreen(qd)
    qd.move_to(port2, 2, 20)
    for ch in text:
        qd.draw_text(port2, ch)
    assert port2.pn_loc == port.pn_loc
    assert rows(qd, pieces) == rows(qd, whole)


# -- bits ---------------------------------------------------------------------
def bitmap(qd, base, bounds):
    bm = qd.sys.sys_heap.new_ptr(14)
    qd.mem.w32(bm, base)
    qd.mem.w16(bm + 4, ROW_BYTES)
    qd.mem.w_rect(bm + 6, bounds)
    return bm


@pytest.mark.parametrize("mode, expect", [
    (qdraw.srcCopy, "##..##.."),
    (qdraw.srcOr, "###.##.#"),
    (qdraw.srcXor, ".##.#..#"),
    (qdraw.srcBic, "..#....#"),
])
def test_copy_bits_modes(qd, mode, expect):
    src_port, src = offscreen(qd)
    dst_port, dst = offscreen(qd)
    qd.mem.w8(src, 0b11001100)
    qd.mem.w8(dst, 0b10100101)
    full = Rect(0, 0, H, W)
    qd.copy_bits(bitmap(qd, src, full), bitmap(qd, dst, full), Rect(0, 0, 1, 8),
                 Rect(0, 0, 1, 8), mode, None, None)
    assert rows(qd, dst, 1)[0][:8] == expect


def test_copy_bits_scales_and_masks(qd):
    src_port, src = offscreen(qd)
    dst_port, dst = offscreen(qd)
    qd.mem.w8(src, 0b10000000)                      # one black pixel
    qd.copy_bits(src_port.addr + 2, dst_port.addr + 2, Rect(0, 0, 1, 2), Rect(0, 0, 4, 8),
                 qdraw.srcCopy, None, dst_port)
    assert rows(qd, dst, 5)[:5] == ["####" + "." * (W - 4)] * 4 + ["." * W]
    qd.copy_bits(src_port.addr + 2, dst_port.addr + 2, Rect(0, 0, 1, 1), Rect(10, 10, 12, 12),
                 qdraw.srcCopy, Region.rect(Rect(10, 10, 11, 11)), dst_port)
    assert rows(qd, dst, 12)[10][9:13] == ".#.." and rows(qd, dst, 12)[11][9:13] == "...."


def test_scroll_rect(qd):
    port, bits = offscreen(qd)
    qd.shape(port, "Paint", rect_path, Rect(0, 0, 2, 2))
    vacated = qd.scroll_rect(port, Rect(0, 0, 8, 8), 3, 1)
    r = rows(qd, bits, 4)
    assert r[0][:8] == "........" and r[1][:8] == "...##..." and r[2][:8] == "...##..."
    assert vacated == Region.rect(Rect(0, 0, 8, 8)).diff(Region.rect(Rect(1, 3, 8, 8)))


def test_get_pixel(qd):
    port, bits = offscreen(qd)
    qd.mem.w8(bits + ROW_BYTES, 0b01000000)
    assert qd.get_pixel(port, Point(1, 1)) and not qd.get_pixel(port, Point(1, 0))


# -- recording -------------------------------------------------------------------
def test_open_rgn_records_frames_and_closed_lines(qd):
    port, bits = offscreen(qd)
    qd.open_rgn(port)
    qd.shape(port, "Frame", rect_path, Rect(0, 0, 10, 10))
    qd.shape(port, "Frame", rect_path, Rect(5, 5, 15, 15))
    rgn = qd.close_rgn(port)
    assert ones(qd, bits) == 0                      # nothing drawn meanwhile
    assert rgn == Region.rect(Rect(0, 0, 10, 10)).xor(Region.rect(Rect(5, 5, 15, 15)))
    qd.open_rgn(port)
    qd.move_to(port, 0, 0)
    for h, v in ((20, 0), (20, 10), (0, 10), (0, 0)):
        qd.line_to(port, h, v)
    assert qd.close_rgn(port) == Region.rect(Rect(0, 0, 10, 20))
    assert port.pn_vis == 0


def test_polygons(qd):
    port, bits = offscreen(qd)
    heap = qd.sys.sys_heap
    h = heap.new_handle(10, clear=True)
    qd.open_poly(port, h)
    qd.move_to(port, 0, 0)
    qd.line_to(port, 8, 0)
    qd.line_to(port, 8, 4)
    qd.line_to(port, 0, 0)
    qd.close_poly(port)
    assert ones(qd, bits) == 0
    assert qdraw.read_poly(qd.mem, h) == [Point(0, 0), Point(0, 8), Point(4, 8), Point(0, 0)]
    p = heap.deref(h)
    assert qd.mem.r16(p) == 26 and qd.mem.r_rect(p + 2) == Rect(0, 0, 4, 8)
    qd.frame_poly(port, qdraw.read_poly(qd.mem, h))
    assert ones(qd, bits) > 0 and port.pn_loc == Point(0, 0)


def test_erase_uses_the_background_pattern(qd):
    port, bits = offscreen(qd)
    qd.shape(port, "Fill", rect_path, Rect(0, 0, 1, 8), BLACK_PAT)
    port.bk_pat = bytes([0x0F] * 8)
    qd.shape(port, "Erase", rect_path, Rect(0, 0, 1, 8))
    assert rows(qd, bits, 1)[0][:8] == "....####"


def test_a_backing_keeps_only_its_area(qapp):
    port = Rect(0, 0, 100, 30000)
    b = Backing(Rect(0, 0, 100, 1000), port)
    assert (b.image.width(), b.image.height()) == (1000, 100)
    with b.painter() as p:
        p.fillRect(990, 10, 5, 5, QColor("#000000"))
    assert not b.fit(Rect(0, 500, 100, 1000), port)         # less of it: all still there
    assert b.image.width() == 500
    assert b.image.pixelColor(490, 10) == QColor("#000000")
    assert b.fit(Rect(0, 500, 100, 2000), port)             # 1000 to 2000 wasn't kept
    assert b.image.pixelColor(490, 10) == QColor("#000000")
    with b.painter() as p:                                  # (0, 0) is still the port's
        p.fillRect(1500, 20, 1, 1, QColor("#000000"))
    assert b.image.pixelColor(1000, 20) == QColor("#000000")


def test_a_backing_that_grows_with_its_port_has_nothing_to_redraw(qapp):
    b = Backing(Rect(0, 0, 100, 200))
    assert not b.fit(Rect(0, 0, 150, 300), Rect(0, 0, 150, 300))  # the new part is the app's
    assert (b.image.width(), b.image.height()) == (300, 150)
