"""
Colour on a black-and-white Mac.  There's no Color QuickDraw here (ROM85
says so), but programs written for both ask about colours anyway.  An RGB
colour becomes the classic colour it would draw as on a 1-bit screen,
black or white by how light it is, and colour tables describe 1-bit or
grey devices.

    ColorTable  ctSeed (long), ctFlags, ctSize (entries - 1), then each
                entry's value and red, green, blue (Inside Macintosh V-135)
"""

from __future__ import annotations

import struct

from .constants import blackColor, whiteColor

WHITE = (0xFFFF, 0xFFFF, 0xFFFF)
BLACK = (0, 0, 0)

# The eight classic colours (Inside Macintosh I-158) as Color QuickDraw
# gives them in RGB.
CLASSIC_RGB = {
    blackColor: BLACK, whiteColor: WHITE,
    205: (0xDD6B, 0x08C2, 0x06A2),          # redColor
    341: (0x0000, 0x8000, 0x11B0),          # greenColor
    409: (0x0000, 0x0000, 0xD400),          # blueColor
    273: (0x0241, 0xAB54, 0xEAFF),          # cyanColor
    137: (0xF2D7, 0x0856, 0x84EC),          # magentaColor
    69: (0xFC00, 0xF37D, 0x052F),           # yellowColor
}

CT_DEVICE = 0x8000          # ctFlags: entries are in pixel value order, values ignored


def luminance(r: int, g: int, b: int) -> int:
    """How light an RGB colour is, 0 to $FFFF, by the NTSC weights."""
    return (299 * (r & 0xFFFF) + 587 * (g & 0xFFFF) + 114 * (b & 0xFFFF)) // 1000


def is_light(r: int, g: int, b: int) -> bool:
    return luminance(r, g, b) >= 0x8000


def rgb_color(r: int, g: int, b: int) -> int:
    """The classic colour an RGB colour draws as on a 1-bit screen."""
    return whiteColor if is_light(r, g, b) else blackColor


def color_rgb(color: int) -> tuple[int, int, int]:
    """The RGB of a classic colour (GetForeColor in a plain GrafPort)."""
    rgb = CLASSIC_RGB.get(color)
    if rgb is None:
        rgb = BLACK if color & 1 else WHITE
    return rgb


def color_table(colors: list[tuple[int, int, int]], seed: int = 0, flags: int = 0) -> bytes:
    """A ColorTable record for colors, by pixel value."""
    out = struct.pack(">lHh", seed, flags, len(colors) - 1)
    for value, (r, g, b) in enumerate(colors):
        out += struct.pack(">HHHH", value, r, g, b)
    return out


def standard_table(ct_id: int) -> bytes | None:
    """The standard colour tables GetCTable knows without a clut: 1, 4
    and 8 bits of the standard colours and 33, 34, 36 and 40, the greys
    (32 + depth).  A table's seed is its ID.  None for any other."""
    if ct_id in (1, 33):
        return color_table([WHITE, BLACK], ct_id)
    if ct_id in (34, 36, 40):
        n = 1 << (ct_id - 32)
        grays = [0xFFFF - i * 0xFFFF // (n - 1) for i in range(n)]
        return color_table([(v, v, v) for v in grays], ct_id)
    if ct_id in (4, 8):
        return color_table([((c >> 16 & 0xFF) * 0x101, (c >> 8 & 0xFF) * 0x101,
                             (c & 0xFF) * 0x101)
                            for c in (STANDARD_4BIT if ct_id == 4 else STANDARD_8BIT)], ct_id)
    return None


def _standard_8bit() -> list[int]:
    """The Mac's standard 256 colours, as 0xAARRGGBB: a 6x6x6 cube from
    white, then ramps of red, green, blue and grey without the cube's
    levels, then black."""
    cube = (0xFF, 0xCC, 0x99, 0x66, 0x33, 0x00)
    ramp = (0xEE, 0xDD, 0xBB, 0xAA, 0x88, 0x77, 0x55, 0x44, 0x22, 0x11)
    out = [(r << 16) | (g << 8) | b for r in cube for g in cube for b in cube][:215]
    out += [v << 16 for v in ramp] + [v << 8 for v in ramp] + list(ramp)
    out += [(v << 16) | (v << 8) | v for v in ramp] + [0]
    return [0xFF000000 | c for c in out]


# The standard 16 colours: white, yellow, orange, red, magenta, purple,
# blue, cyan, green, dark green, brown, tan, light, medium and dark grey,
# black.
STANDARD_4BIT = [0xFF000000 | c for c in (
    0xFFFFFF, 0xFCF305, 0xFF6402, 0xDD0806, 0xF20884, 0x4600A5, 0x0000D4, 0x02ABEA,
    0x1FB714, 0x006411, 0x562C05, 0x90713A, 0xC0C0C0, 0x808080, 0x404040, 0x000000)]
STANDARD_8BIT = _standard_8bit()
