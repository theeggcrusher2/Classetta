"""
Point and Rect with Toolbox semantics.

Field order matches the in-memory layout of the Toolbox structs
(Point is v,h; Rect is top,left,bottom,right) so the trap layer can read
them straight out of guest memory.  A Rect contains a point when
left <= h < right and top <= v < bottom, same as PtInRect.
"""

from __future__ import annotations

from typing import NamedTuple


class Point(NamedTuple):
    v: int
    h: int

    def offset(self, dh: int, dv: int) -> Point:
        return Point(self.v + dv, self.h + dh)


class Rect(NamedTuple):
    top: int
    left: int
    bottom: int
    right: int

    @classmethod
    def from_xywh(cls, x: int, y: int, w: int, h: int) -> Rect:
        return cls(y, x, y + h, x + w)

    @property
    def width(self) -> int:
        return self.right - self.left

    @property
    def height(self) -> int:
        return self.bottom - self.top

    @property
    def top_left(self) -> Point:
        return Point(self.top, self.left)

    @property
    def empty(self) -> bool:
        return self.bottom <= self.top or self.right <= self.left

    def contains(self, pt: Point) -> bool:
        return self.left <= pt.h < self.right and self.top <= pt.v < self.bottom

    def offset(self, dh: int, dv: int) -> Rect:
        return Rect(self.top + dv, self.left + dh, self.bottom + dv, self.right + dh)

    def inset(self, dh: int, dv: int) -> Rect:
        return Rect(self.top + dv, self.left + dh, self.bottom - dv, self.right - dh)

    def moved_to(self, h: int, v: int) -> Rect:
        return Rect(v, h, v + self.height, h + self.width)

    def sized(self, w: int, h: int) -> Rect:
        return Rect(self.top, self.left, self.top + h, self.left + w)

    def sect(self, other: Rect) -> Rect:
        """SectRect: what the two share, or an empty rect at (0, 0)."""
        r = Rect(max(self.top, other.top), max(self.left, other.left),
                 min(self.bottom, other.bottom), min(self.right, other.right))
        return Rect(0, 0, 0, 0) if r.empty else r

    def union(self, other: Rect) -> Rect:
        """UnionRect: an empty rect adds nothing."""
        if self.empty:
            return other
        if other.empty:
            return self
        return Rect(min(self.top, other.top), min(self.left, other.left),
                    max(self.bottom, other.bottom), max(self.right, other.right))
