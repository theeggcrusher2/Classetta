"""
Masks for CopyMask and friends: the region a mask bitmap's 1 bits cover,
and SeedFill and CalcMask, which make masks the way MacPaint's paint
bucket and lasso do (Inside Macintosh IV-24).

Bits are worked on a pixel to a byte, 1 for a set bit, so that bytes'
find and translate can do the scanning.
"""

from __future__ import annotations

from PyQt6.QtCore import Qt
from PyQt6.QtGui import QImage

from .qdraw import Region
from .types import Rect

EXPAND = [bytes((b >> (7 - i)) & 1 for i in range(8)) for b in range(256)]
TO_ASCII = bytes.maketrans(b"\x00\x01", b"01")
FLIP = bytes.maketrans(b"\x00\x01", b"\x01\x00")
DARK = bytes(255 if v < 128 else 0 for v in range(256))


def to_pixels(row: bytes, width: int) -> bytearray:
    """A row of bits as a byte a pixel, 1 for a set bit."""
    return bytearray(b"".join(EXPAND[b] for b in row)[:width].ljust(width, b"\0"))


def to_bits(pixels: bytes | bytearray, row_bytes: int) -> bytes:
    """A byte a pixel back to a row of row_bytes bytes of bits."""
    s = bytes(pixels).translate(TO_ASCII).ljust(8 * row_bytes, b"0")[:8 * row_bytes]
    return int(s, 2).to_bytes(row_bytes, "big")


def mask_region(img: QImage, dst: Rect) -> Region:
    """The dark pixels of an image of a mask, scaled to and placed at dst."""
    if img.width() != dst.width or img.height() != dst.height:
        img = img.scaled(dst.width, dst.height, Qt.AspectRatioMode.IgnoreAspectRatio,
                         Qt.TransformationMode.FastTransformation)
    gray = img.convertToFormat(QImage.Format.Format_Grayscale8)
    width = gray.width()
    for y in range(gray.height()):
        line = gray.scanLine(y)
        line.setsize(width)
        line[:] = bytes(line).translate(DARK)
    return Region.from_mask(gray, dst.left, dst.top)


def _flood(free: list[bytearray], width: int, seeds: list[tuple[int, int]]) -> list[bytearray]:
    """Scanline flood fill: the pixels of free (1s) reachable from seeds
    across edges, not corners, as 1s.  free is used up."""
    height = len(free)
    filled = [bytearray(width) for _ in range(height)]
    stack = [(x, y) for x, y in seeds if 0 <= y < height and 0 <= x < width]
    while stack:
        x, y = stack.pop()
        row = free[y]
        if not row[x]:
            continue
        left = row.rfind(0, 0, x) + 1
        right = row.find(0, x)
        if right < 0:
            right = width
        row[left:right] = bytes(right - left)
        filled[y][left:right] = b"\x01" * (right - left)
        for ny in (y - 1, y + 1):
            if not 0 <= ny < height:
                continue
            near = free[ny]
            i = near.find(1, left, right)
            while i >= 0:
                stack.append((i, ny))
                j = near.find(0, i, right)
                if j < 0:
                    break
                i = near.find(1, j, right)
    return filled


def seed_fill(rows: list[bytes], width: int, seed_h: int, seed_v: int) -> list[bytes]:
    """SeedFill: 1s where paint poured at (seed_h, seed_v) would go,
    through the 0 bits of rows."""
    row_bytes = len(rows[0]) if rows else 0
    free = [to_pixels(row, width).translate(FLIP) for row in rows]
    filled = _flood(free, width, [(seed_h, seed_v)])
    return [to_bits(f, row_bytes) for f in filled]


def calc_mask(rows: list[bytes], width: int) -> list[bytes]:
    """CalcMask: 1s for the 1 bits of rows and every 0 bit they enclose,
    that is everything paint poured in from the edges couldn't reach."""
    row_bytes = len(rows[0]) if rows else 0
    height = len(rows)
    free = [to_pixels(row, width).translate(FLIP) for row in rows]
    seeds = [(x, y) for y in (0, height - 1) for x in range(width)]
    seeds += [(x, y) for y in range(height) for x in (0, width - 1)]
    outside = _flood(free, width, seeds)
    return [to_bits(o.translate(FLIP), row_bytes) for o in outside]
