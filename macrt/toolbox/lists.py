"""
List Manager data (Inside Macintosh IV-259).

A list is a ListRec in a relocatable block in the application heap, and
a ListHandle is that block's handle.  The record ends in cellArray: a
word for each cell, row by row from dataBounds' top left, and one more.
A cell's word is where its data starts in the cells handle's block, with
the high bit set while the cell is selected; the next word, without
that bit, is where the data ends, so the last word is the length of all
of it.  Applications read the record and the data themselves, and
LGetCellDataLocation hands out offsets into it, so macrt keeps both
exactly as the ROM does.

Drawing, the mouse and the scroll bars are the trap glue's
(macrt.m68k.glue_lists).  What's here is the record, its cells and the
arithmetic of where they are.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from ..memory import ADDRESS_MASK, BOOLEAN, BYTE, INTEGER, LONG, LONGINT, POINT, RECT, Field
from .types import Point, Rect

if TYPE_CHECKING:
    from .system import System

CELL_ARRAY = 86             # the record up to cellArray
SELECTED = 0x8000
MAX_DATA = 0x7FFF           # how far a cellArray offset reaches
NOWHERE = Rect(0, 0, 0, 0)


class ListRec:
    r_view = Field(0, RECT)
    port = Field(8, LONG)
    indent = Field(12, POINT)
    cell_size = Field(16, POINT)
    visible = Field(20, RECT)               # the cells in view, as a rectangle of cells
    v_scroll = Field(28, LONG)
    h_scroll = Field(32, LONG)
    sel_flags = Field(36, BYTE)
    active = Field(37, BOOLEAN)
    list_flags = Field(39, BYTE)
    clik_time = Field(40, LONGINT)
    clik_loc = Field(44, POINT)
    mouse_loc = Field(48, POINT)
    clik_loop = Field(52, LONG)
    last_click = Field(56, POINT)
    ref_con = Field(60, LONGINT)
    def_proc = Field(64, LONG)              # the LDEF's handle
    user_handle = Field(68, LONG)
    data_bounds = Field(72, RECT)
    cells = Field(80, LONG)
    max_index = Field(84, INTEGER)

    def __init__(self, system: System, handle: int):
        self.system = system
        self.mem = system.mem
        self.handle = handle & ADDRESS_MASK

    def __eq__(self, other) -> bool:
        return isinstance(other, ListRec) and other.handle == self.handle

    def __hash__(self) -> int:
        return self.handle

    def __repr__(self) -> str:
        return f"<ListRec ${self.handle:06X} {self.data_bounds}>"

    @property
    def addr(self) -> int:
        return self.mem.r32(self.handle) & ADDRESS_MASK

    # =====================================================================
    # Cells.  An index is a cell's place in cellArray.
    # =====================================================================
    @property
    def columns(self) -> int:
        b = self.data_bounds
        return max(b.right - b.left, 0)

    @property
    def rows(self) -> int:
        b = self.data_bounds
        return max(b.bottom - b.top, 0)

    @property
    def count(self) -> int:
        return self.columns * self.rows

    def has(self, cell: Point) -> bool:
        return self.data_bounds.contains(cell)

    def index(self, cell: Point) -> int:
        """cell's index, -1 for a cell outside dataBounds."""
        b = self.data_bounds
        if not b.contains(cell):
            return -1
        return (cell.v - b.top) * self.columns + cell.h - b.left

    def cell(self, i: int) -> Point:
        b, cols = self.data_bounds, self.columns
        return Point(b.top + i // cols, b.left + i % cols)

    def _word(self, i: int) -> int:
        return self.mem.r16(self.addr + CELL_ARRAY + 2 * i)

    def _set_word(self, i: int, value: int) -> None:
        self.mem.w16(self.addr + CELL_ARRAY + 2 * i, value)

    def span(self, i: int) -> tuple[int, int]:
        """Where cell i's data starts and ends in the cells block."""
        return self._word(i) & MAX_DATA, self._word(i + 1) & MAX_DATA

    @property
    def data_length(self) -> int:
        return self._word(self.count) & MAX_DATA

    def data(self, i: int) -> bytes:
        start, end = self.span(i)
        if end <= start:
            return b""
        return self.mem.read((self.mem.r32(self.cells & ADDRESS_MASK) & ADDRESS_MASK) + start,
                             end - start)

    def is_selected(self, i: int) -> bool:
        return bool(self._word(i) & SELECTED)

    def set_selected(self, i: int, on: bool) -> None:
        w = self._word(i)
        self._set_word(i, w | SELECTED if on else w & ~SELECTED)

    def selected(self) -> list[int]:
        return [i for i in range(self.count) if self.is_selected(i)]

    def set_data(self, i: int, data: bytes) -> bool:
        """Make cell i's data this.  False, with nothing changed, if
        there's no room: in the heap, or in the 32K the offsets reach."""
        start, end = self.span(i)
        total = self.data_length
        delta = len(data) - (end - start)
        if total + delta > MAX_DATA:
            return False
        h = self.cells & ADDRESS_MASK
        heap = self.system.heap_of(h)
        tail = self.mem.read(heap.deref(h) + end, total - end) if total > end else b""
        if delta > 0 and not heap.set_handle_size(h, total + delta):
            return False
        self.mem.write(heap.deref(h) + start, data + tail)
        if delta < 0:
            heap.set_handle_size(h, total + delta)
        if delta:
            for j in range(i + 1, self.count + 1):
                w = self._word(j)
                self._set_word(j, (w & SELECTED) | ((w & MAX_DATA) + delta))
        return True

    def all(self) -> list[tuple[bytes, bool]]:
        """Every cell's data and whether it's selected, in index order."""
        return [(self.data(i), self.is_selected(i)) for i in range(self.count)]

    def rebuild(self, cells: list[tuple[bytes, bool]], bounds: Rect) -> bool:
        """Make the list's cells these, row by row in bounds: what adding
        and deleting rows and columns does.  False, with nothing changed,
        if there's no room."""
        data = b"".join(d for d, _ in cells)
        if len(data) > MAX_DATA:
            return False
        lh, ch = self.handle, self.cells & ADDRESS_MASK
        l_heap, c_heap = self.system.heap_of(lh), self.system.heap_of(ch)
        old_l, new_l = l_heap.handle_size(lh), CELL_ARRAY + 2 * (len(cells) + 1)
        old_c, new_c = c_heap.handle_size(ch), len(data)
        if new_l > old_l and not l_heap.set_handle_size(lh, new_l):
            return False
        if new_c > old_c and not c_heap.set_handle_size(ch, new_c):
            l_heap.set_handle_size(lh, old_l)
            return False
        self.data_bounds = bounds
        self.max_index = 2 * len(cells)
        offset = 0
        for i, (d, on) in enumerate(cells):
            self._set_word(i, offset | (SELECTED if on else 0))
            offset += len(d)
        self._set_word(len(cells), offset)
        self.mem.write(c_heap.deref(ch), data)
        if new_l < old_l:
            l_heap.set_handle_size(lh, new_l)
        if new_c < old_c:
            c_heap.set_handle_size(ch, new_c)
        return True

    # =====================================================================
    # Moving through the cells
    # =====================================================================
    def next_cell(self, cell: Point, h_next: bool, v_next: bool) -> Point | None:
        """LNextCell: along the row (h_next), on to the start of the next
        row when both are set; down the column (v_next), on to the top of
        the next one when only it is.  None past the last."""
        b = self.data_bounds
        v, h = cell
        if h_next:
            h += 1
            if h < b.right:
                return Point(v, h)
            if v_next and v + 1 < b.bottom:
                return Point(v + 1, b.left)
            return None
        if v_next:
            v += 1
            if v < b.bottom:
                return Point(v, h)
            if h + 1 < b.right:
                return Point(b.top, h + 1)
        return None

    def first_selected(self, cell: Point) -> Point | None:
        """LGetSelect's search: the first selected cell at or after cell,
        row by row."""
        b = self.data_bounds
        if cell.v < b.top or (cell.v == b.top and cell.h < b.left):
            cell = b.top_left
        elif cell.h < b.left:
            cell = Point(cell.v, b.left)
        elif cell.h >= b.right:
            cell = Point(cell.v + 1, b.left)
        i = self.index(cell)
        if i < 0:
            return None
        for j in range(i, self.count):
            if self.is_selected(j):
                return self.cell(j)
        return None

    # =====================================================================
    # Where cells are
    # =====================================================================
    def cell_rect(self, cell: Point) -> Rect:
        """LRect: where a cell in view is in the list's port; (0,0,0,0)
        for one that's scrolled out of it."""
        vis = self.visible
        if not vis.contains(cell):
            return NOWHERE
        size, view = self.cell_size, self.r_view
        top = view.top + (cell.v - vis.top) * size.v
        left = view.left + (cell.h - vis.left) * size.h
        return Rect(top, left, min(top + size.v, view.bottom), min(left + size.h, view.right))

    def cell_at(self, pt: Point) -> Point:
        """The cell a point in rView is over, which may be past the last."""
        view, size, vis = self.r_view, self.cell_size, self.visible
        return Point(vis.top + (pt.v - view.top) // max(size.v, 1),
                     vis.left + (pt.h - view.left) // max(size.h, 1))

    def fit_visible(self) -> None:
        """visible: as many cells from its top left as rView shows, the
        last ones perhaps only in part."""
        view, size, vis = self.r_view, self.cell_size, self.visible
        cols = -(-view.width // size.h) if size.h > 0 else 0
        rows = -(-view.height // size.v) if size.v > 0 else 0
        self.visible = Rect(vis.top, vis.left, vis.top + max(rows, 0), vis.left + max(cols, 0))

    def scroll_max(self) -> Point:
        """The furthest visible's top left goes: far enough to bring the
        last row and column wholly into view."""
        b, vis, size, view = self.data_bounds, self.visible, self.cell_size, self.r_view
        v = b.bottom - vis.height + (1 if size.v * vis.height > view.height else 0)
        h = b.right - vis.width + (1 if size.h * vis.width > view.width else 0)
        return Point(max(v, b.top), max(h, b.left))
