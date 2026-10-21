"""The colour calls on a 1-bit screen."""

import struct

import pytest

from macrt.resfork import Resource
from macrt.toolbox.color import is_light, luminance, rgb_color
from macrt.toolbox.constants import blackColor, whiteColor

from guest import Guest


@pytest.fixture
def g(qapp):
    return Guest()


def rgb(g, r, gr, b) -> int:
    return g.ptr(struct.pack(">HHH", r, gr, b))


def read_rgb(g, p) -> tuple:
    return struct.unpack(">HHH", g.mem.read(p, 6))


def test_rgb_colours_become_black_or_white(g):
    port, _ = g.port()
    g("RGBForeColor", rgb(g, 0xFFFF, 0xFFFF, 0xC000))      # pale yellow: white
    assert port.fg_color == whiteColor
    g("PaintRect", g.rect_ptr((0, 0, 32, 64)))
    assert g.rows(port)[0] == "." * 64
    g("RGBBackColor", rgb(g, 0x2000, 0x2000, 0x8000))      # navy: black
    assert port.bk_color == blackColor
    g("EraseRect", g.rect_ptr((0, 0, 4, 64)))
    assert g.rows(port)[0] == "#" * 64 and g.rows(port)[4] == "." * 64

    out = g.ptr(bytes(6))
    g("GetForeColor", out)
    assert read_rgb(g, out) == (0xFFFF, 0xFFFF, 0xFFFF)
    g("ForeColor", 205)                                    # redColor, as Color QuickDraw has it
    g("GetForeColor", out)
    assert read_rgb(g, out) == (0xDD6B, 0x08C2, 0x06A2)
    g("GetBackColor", out)
    assert read_rgb(g, out) == (0, 0, 0)


def test_luminance():
    assert luminance(0xFFFF, 0xFFFF, 0xFFFF) == 0xFFFF and luminance(0, 0, 0) == 0
    assert is_light(0, 0xFFFF, 0) and not is_light(0xFFFF, 0, 0)     # green light, red dark
    assert rgb_color(0x8000, 0x8000, 0x8000) == whiteColor
    assert rgb_color(0x7FFF, 0x7FFF, 0x7FFF) == blackColor


def test_indexes_are_a_one_bit_devices(g):
    assert g("Color2Index", rgb(g, 0xFFFF, 0xFFFF, 0xFFFF)) == 0
    assert g("Color2Index", rgb(g, 0x1000, 0x8000, 0x1000)) == 1
    out = g.ptr(bytes(6))
    g("Index2Color", 1, out)
    assert read_rgb(g, out) == (0, 0, 0)
    g("Index2Color", 0, out)
    assert read_rgb(g, out) == (0xFFFF, 0xFFFF, 0xFFFF)
    fore = rgb(g, 1, 2, 3)
    assert g("GetGray", 0, rgb(g, 0xFFFF, 0xFFFF, 0xFFFF), fore) is False
    assert read_rgb(g, fore) == (1, 2, 3)


def test_hilite_color_and_the_calls_that_do_nothing(g):
    g("HiliteColor", rgb(g, 0x1111, 0x2222, 0x3333))
    assert read_rgb(g, 0xDA0) == (0x1111, 0x2222, 0x3333)
    p = rgb(g, 0x1111, 0x2222, 0x3333)
    g("InvertColor", p)
    g("OpColor", p)
    assert read_rgb(g, p) == (0x1111, 0x2222, 0x3333)


def colour_table(data: bytes) -> list[tuple]:
    seed, flags, size = struct.unpack_from(">lHh", data)
    return [struct.unpack_from(">HHHH", data, 8 + 8 * i) for i in range(size + 1)]


def test_get_c_table(qapp):
    clut = struct.pack(">lHh", 0, 0, 0) + struct.pack(">HHHH", 0, 1, 2, 3)
    g = Guest([Resource(b"clut", 300, clut)])
    for ct_id, n in ((1, 2), (33, 2), (34, 4), (36, 16), (40, 256), (4, 16), (8, 256)):
        h = g("GetCTable", ct_id)
        entries = colour_table(g.contents(h))
        assert len(entries) == n and struct.unpack_from(">l", g.contents(h))[0] == ct_id
        assert entries[0][1:] == (0xFFFF, 0xFFFF, 0xFFFF) and entries[-1][1:] == (0, 0, 0)
        g("DisposeCTable", h)
        assert not g.heap.is_handle(h)
    assert colour_table(g.contents(g("GetCTable", 8)))[215][1:] == (0xEEEE, 0, 0)
    h = g("GetCTable", 300)
    assert g.contents(h) == clut and not g.heap.get_state(h) & 0x20      # a copy
    assert g("GetCTable", 301) == 0
