"""Icons: PlotIcon, the icon families and suites of the Icon Utilities,
colour icons and SICNs, drawn on a 1-bit port."""

import struct

import pytest

from macrt.resfork import Resource
from macrt.toolbox.icons import (LARGE, SMALL, atBottom, atHorizontalCenter, atLeft, atNone,
                                 atRight, atTop, atVerticalCenter, best_size, place, ttDisabled,
                                 ttNone, ttSelected)
from macrt.toolbox.types import Point, Rect

from guest import Guest


def icon(rows: list[int], width: int = 32) -> bytes:
    """An icon's bits, from a row's bits as an int for each row."""
    return b"".join(r.to_bytes(width // 8, "big") for r in rows)


FRAME = icon([0xFFFFFFFF] + [0x80000001] * 30 + [0xFFFFFFFF])
LEFT_HALF = icon([0xFFFF0000] * 32)
SOLID = icon([0xFFFFFFFF] * 32)
EMPTY = bytes(128)
MIDDLE = icon([0] * 8 + [0x00FFFF00] * 16 + [0] * 8)        # rows 8-23, columns 8-23
SMALL_SOLID = icon([0xFFFF] * 16, 16)


def guest(*resources):
    return Guest([Resource(kind.encode(), rid, data) for kind, rid, data in resources])


def black(rows, top, left, bottom, right) -> bool:
    return all(rows[y][x] == "#" for y in range(top, bottom) for x in range(left, right))


def white(rows, top, left, bottom, right) -> bool:
    return all(rows[y][x] == "." for y in range(top, bottom) for x in range(left, right))


@pytest.fixture
def g(qapp):
    return guest(("ICN#", 128, EMPTY + MIDDLE),
                 ("ICN#", 129, FRAME + SOLID), ("ics#", 129, SMALL_SOLID * 2),
                 ("icl8", 130, bytes([0xFF] * 16 + [0] * 16) * 32),
                 ("ICON", 131, LEFT_HALF))


def fill(g, port):
    g("PaintRect", g.rect_ptr((0, 0, port.port_bits_bounds.bottom, port.port_bits_bounds.right)))


# -- PlotIcon -------------------------------------------------------------------
def test_plot_icon_copies_the_whole_icon(g):
    port, _ = g.port()
    fill(g, port)
    h = g("GetIcon", 131)
    g("PlotIcon", g.rect_ptr((0, 0, 32, 32)), h)
    rows = g.rows(port)
    assert all(r[:32] == "#" * 16 + "." * 16 for r in rows)      # white bits too: no mask
    assert black(rows, 0, 32, 32, 64)


def test_plot_icon_scales_to_the_rect(g):
    port, _ = g.port()
    g("PlotIcon", g.rect_ptr((8, 0, 24, 48)), g("GetIcon", 131))   # half high, wider
    rows = g.rows(port)
    assert all(rows[y][:48] == "#" * 24 + "." * 24 for y in range(8, 24))
    assert white(rows, 0, 0, 8, 64) and white(rows, 24, 0, 32, 64)


def test_plot_icon_takes_a_handle_the_program_made(g):
    """The ROM only dereferences the handle: Leisure Suit Larry passes a
    pointer to a pointer to the bits, all on its stack."""
    port, _ = g.port()
    stack = 0x2800                                  # in no heap
    with pytest.raises(ValueError):
        g.system.heap_of(stack)
    g.mem.write(stack, LEFT_HALF)
    g.mem.w32(stack + 128, stack)
    g("PlotIcon", g.rect_ptr((0, 0, 32, 32)), stack + 128)
    assert all(r[:32] == "#" * 16 + "." * 16 for r in g.rows(port)[:32])


# -- families -------------------------------------------------------------------
def test_plot_icon_id_draws_through_the_mask(g):
    port, _ = g.port()
    fill(g, port)
    assert g("PlotIconID", g.rect_ptr((0, 0, 32, 32)), atNone, ttNone, 128) == 0
    rows = g.rows(port)
    assert white(rows, 8, 8, 24, 24)
    assert black(rows, 0, 0, 8, 64) and black(rows, 24, 0, 32, 64) and black(rows, 8, 0, 24, 8)
    assert g("PlotIconID", g.rect_ptr((0, 0, 32, 32)), atNone, ttNone, 999) == -192


def test_the_size_drawn_is_the_rects(g):
    port, _ = g.port()
    g("PlotIconID", g.rect_ptr((0, 0, 32, 32)), atNone, ttNone, 129)    # the ICN#: a frame
    g("PlotIconID", g.rect_ptr((0, 40, 16, 56)), atNone, ttNone, 129)   # the ics#: solid
    rows = g.rows(port)
    assert rows[0][:32] == "#" * 32 and rows[1][:32] == "#" + "." * 30 + "#"
    assert black(rows, 0, 40, 16, 56)
    # Only a large icon: shrunk to fit.
    g("PlotIconID", g.rect_ptr((16, 40, 32, 56)), atNone, ttNone, 128)
    assert best_size(Rect(0, 0, 16, 16), lambda size: size == LARGE) == LARGE
    assert best_size(Rect(0, 0, 40, 40), lambda size: size == SMALL) == SMALL
    assert best_size(Rect(0, 0, 40, 40), lambda size: False) is None


def test_alignment():
    r = Rect(0, 0, 32, 64)
    assert place(r, 32, 32, atNone) == r
    assert place(r, 32, 32, atVerticalCenter | atHorizontalCenter) == Rect(0, 16, 32, 48)
    assert place(r, 32, 32, atTop | atLeft) == Rect(0, 0, 32, 32)
    assert place(r, 32, 32, atRight) == Rect(0, 32, 32, 64)
    assert place(Rect(0, 0, 64, 32), 16, 16, atBottom) == Rect(32, 0, 64, 32)


def test_an_aligned_icon_keeps_its_shape(g):
    port, _ = g.port()
    g("PlotIconID", g.rect_ptr((0, 0, 32, 64)), atRight, ttNone, 129)
    rows = g.rows(port)
    assert white(rows, 0, 0, 32, 32) and rows[0][32:] == "#" * 32 and rows[5][32:33] == "#"


def test_transforms(g):
    port, _ = g.port()
    g("PlotIconID", g.rect_ptr((0, 0, 32, 32)), atNone, ttSelected, 129)
    rows = g.rows(port)
    assert rows[0][:32] == "." * 32 and black(rows, 1, 1, 31, 31)      # inverted in its mask
    g("PlotIconID", g.rect_ptr((0, 32, 16, 48)), atNone, ttDisabled, 129)   # solid, greyed
    rows = g.rows(port)
    assert rows[0][32:48] == "#." * 8 and rows[1][32:48] == ".#" * 8


def test_a_colour_only_family_is_made_one_bit(g):
    port, _ = g.port()
    fill(g, port)
    g("PlotIconID", g.rect_ptr((0, 0, 32, 32)), atNone, ttNone, 130)
    rows = g.rows(port)
    assert all(r[:32] == "#" * 16 + "." * 16 for r in rows)      # no mask: all of it


def test_icon_regions_and_hit_testing(g):
    rgn = g("NewRgn")
    r = g.rect_ptr((0, 0, 32, 32))
    assert g("IconIDToRgn", rgn, r, atNone, 128) == 0
    assert g.rect(g.heap.deref(rgn) + 2) == Rect(8, 8, 24, 24)
    assert g("PtInIconID", Point(10, 10), r, atNone, 128)
    assert not g("PtInIconID", Point(2, 2), r, atNone, 128)
    assert g("RectInIconID", g.rect_ptr((0, 0, 9, 9)), r, atNone, 128)
    assert not g("RectInIconID", g.rect_ptr((0, 0, 8, 8)), r, atNone, 128)


# -- suites ---------------------------------------------------------------------
def test_icon_suites(g):
    port, _ = g.port()
    out = g.ptr(bytes(4))
    assert g("GetIconSuite", out, 129, 0xFFFFFFFF) == 0
    suite = g.mem.r32(out)
    g("PlotIconSuite", g.rect_ptr((0, 0, 32, 32)), atNone, ttNone, suite)
    g("PlotIconSuite", g.rect_ptr((0, 40, 16, 56)), atNone, ttNone, suite)
    rows = g.rows(port)
    assert rows[1][:32] == "#" + "." * 30 + "#" and black(rows, 0, 40, 16, 56)

    assert g("GetSuiteLabel", suite) == 0
    g("SetSuiteLabel", suite, 3)
    assert g("GetSuiteLabel", suite) == 3

    assert g("GetIconFromSuite", out, suite, int.from_bytes(b"icl8", "big")) == -50
    assert g.mem.r32(out) == 0
    mine = g.handle(bytes([0xFF] * 1024))
    assert g("AddIconToSuite", mine, suite, int.from_bytes(b"icl8", "big")) == 0
    assert g("GetIconFromSuite", out, suite, int.from_bytes(b"icl8", "big")) == 0
    assert g.mem.r32(out) == mine
    assert g("GetIconFromSuite", out, suite, int.from_bytes(b"ICN#", "big")) == 0
    resource = g.mem.r32(out)

    assert g("DisposeIconSuite", suite, True) == 0
    assert not g.heap.is_handle(mine) and not g.heap.is_handle(suite)
    assert g.contents(resource) == FRAME + SOLID           # still the Resource Manager's


def test_a_suite_of_only_some_icons(g):
    port, _ = g.port()
    out = g.ptr(bytes(4))
    g("GetIconSuite", out, 129, 0x100)                     # svSmall1Bit: just the ics#
    g("PlotIconSuite", g.rect_ptr((0, 0, 32, 32)), atNone, ttNone, g.mem.r32(out))
    assert black(g.rows(port), 0, 0, 32, 32)               # the small one, stretched
    g("NewIconSuite", out)
    assert g("PlotIconSuite", g.rect_ptr((0, 0, 32, 32)), atNone, ttNone, g.mem.r32(out)) == 0


# -- colour icons and SICNs -----------------------------------------------------
def cicn(with_bitmap: bool) -> bytes:
    """A 16-by-16 cicn at 8 bits: its mask the left 12 columns, its BitMap
    the left 4 columns black, its pixels black in the left 8."""
    bounds = struct.pack(">hhhh", 0, 0, 16, 16)
    pixmap = (struct.pack(">LH", 0, 0x8000 | 16) + bounds
              + struct.pack(">hhlllhhhhlll", 0, 0, 0, 72 << 16, 72 << 16, 0, 8, 1, 8, 0, 0, 0))
    mask = struct.pack(">LH", 0, 2) + bounds
    bmap = struct.pack(">LH", 0, 2 if with_bitmap else 0) + (bounds if with_bitmap else bytes(8))
    ctab = struct.pack(">lHh", 0, 0, 1) + struct.pack(">HHHH", 0, 0xFFFF, 0xFFFF, 0xFFFF) \
        + struct.pack(">HHHH", 1, 0, 0, 0)
    data = pixmap + mask + bmap + bytes(4)
    data += icon([0xFFF0] * 16, 16)
    if with_bitmap:
        data += icon([0xF000] * 16, 16)
    return data + ctab + bytes([1] * 8 + [0] * 8) * 16


def test_colour_icons(qapp):
    g = guest(("cicn", 128, cicn(True)), ("cicn", 129, cicn(False)))
    port, _ = g.port()
    fill(g, port)
    h = g("GetCIcon", 128)
    p = g.heap.deref(h)
    ctab, pixels = g.mem.r32(p + 42), g.mem.r32(p + 78)
    assert g.heap.handle_size(ctab) == 8 + 2 * 8 and g.heap.handle_size(pixels) == 256
    assert g.mem.r32(p + 50) == p + 82                     # the mask's bits, in the record
    g("PlotCIcon", g.rect_ptr((0, 0, 16, 16)), h)
    rows = g.rows(port)
    assert all(r[:16] == "####" + "." * 8 + "####" for r in rows[:16])   # the BitMap, masked
    g("DisposeCIcon", h)
    assert not any(map(g.heap.is_handle, (h, ctab, pixels)))

    # With no BitMap, the pixels, as light or dark.
    h = g("GetCIcon", 129)
    assert g("PlotCIconHandle", g.rect_ptr((16, 0, 32, 32)), atNone, ttNone, h) == 0
    rows = g.rows(port)
    assert rows[20][:32] == "#" * 16 + "." * 8 + "#" * 8          # stretched to 32 wide
    assert g("GetCIcon", 999) == 0


def test_sicn(g):
    port, _ = g.port()
    fill(g, port)
    h = g.handle(icon([0] * 16, 16) + icon([0xFF00] * 16, 16))
    assert g("PlotSICNHandle", g.rect_ptr((0, 0, 16, 16)), atNone, ttNone, h) == 0
    rows = g.rows(port)
    assert all(r[:16] == "." * 8 + "#" * 8 for r in rows[:16])
    assert g("PlotIconHandle", g.rect_ptr((0, 32, 32, 64)), atNone, ttNone,
             g.handle(EMPTY + MIDDLE)) == 0
    rows = g.rows(port)
    assert white(rows, 8, 40, 24, 56) and rows[0][32:] == "#" * 32
