"""
List Manager traps (Pack0, Inside Macintosh IV-259).

Lists are records in the application heap (see macrt.toolbox.lists).
The standard definition, LDEF 0, which draws a cell's data as a line of
text and inverts it while it's selected, is Python.  An application's
own LDEF is 68k code, called as the ROM calls it: pascal void
LDEF(short message, Boolean select, Rect *cellRect, Cell theCell, short
dataOffset, short dataLen, ListHandle theList), with the list's port the
current one and, for drawing and highlighting, its clip region narrowed
to the cell.

A list's scroll bars are ordinary scroll-bar controls in its window,
with the list in their refCon.  LClick follows the mouse on the
application's thread, a tick at a time, calling the list's lClikLoop
as it goes; in a scroll bar it's TrackControl that does.
"""

from __future__ import annotations

from contextlib import contextmanager

from ..memory import ADDRESS_MASK, from_mac
from ..toolbox import constants as k
from ..toolbox import intl, lowmem
from ..toolbox.lists import CELL_ARRAY, NOWHERE, ListRec
from ..toolbox.memmgr import memFullErr, noErr
from ..toolbox.qdraw import Region, read_rgn, write_rgn
from ..toolbox.system import RTS
from ..toolbox.types import Point, Rect
from .dispatch import trap
from .glue_controls import (DisposeControl, HideControl, MoveControl, SetControlValue,
                            ShowControl, SizeControl, TestControl, TrackControl, _ctl, _draw,
                            _the_port_var, new_control, scratch)

GROW_BOX = 15               # what a size box takes from a lone scroll bar


# =========================================================================
# Finding things
# =========================================================================
def _list(t, h: int) -> ListRec | None:
    """The list a ListHandle is, None for NIL or an empty handle."""
    h &= ADDRESS_MASK
    if not h or not t.mem.r32(h) & ADDRESS_MASK:
        return None
    return ListRec(t.system, h)


def _bars(lst: ListRec) -> list[int]:
    return [h & ADDRESS_MASK for h in (lst.v_scroll, lst.h_scroll) if h & ADDRESS_MASK]


def _drawing(lst: ListRec) -> bool:
    return not lst.list_flags & k.lDrawingModeOff


def _shows_hilite(lst: ListRec, i: int) -> bool:
    """Whether cell i is drawn highlighted when it's selected."""
    if lst.sel_flags & k.lNoNilHilite:
        start, end = lst.span(i)
        return end > start
    return True


@contextmanager
def _in_port(t, lst: ListRec, clip: Rect | None = None):
    """The list's port the current one, as the List Manager has it while it
    draws; with clip, its clip region narrowed to that too."""
    var = _the_port_var(t)
    saved = t.mem.r32(var)
    t.mem.w32(var, lst.port)
    port = t.system.port_at(lst.port & ADDRESS_MASK)
    old_clip = None
    if clip is not None and port is not None and port.clip_rgn:
        old_clip = read_rgn(t.mem, port.clip_rgn)
        write_rgn(t.system.heap_of(port.clip_rgn), port.clip_rgn,
                  old_clip.sect(Region.rect(clip)))
    try:
        yield port
    finally:
        if old_clip is not None and port.clip_rgn:
            write_rgn(t.system.heap_of(port.clip_rgn), port.clip_rgn, old_clip)
        t.mem.w32(var, saved)


# =========================================================================
# Talking to the definition procedure
# =========================================================================
def _message(t, lst: ListRec, msg: int, select: bool = False, r: Rect = NOWHERE,
             cell: Point = Point(0, 0), offset: int = 0, length: int = 0) -> None:
    h = lst.def_proc & ADDRESS_MASK
    if not h:
        return
    with _in_port(t, lst, r if msg in (k.lDrawMsg, k.lHiliteMsg) else None):
        if t.system.defproc_id(h) == ("LDEF", 0):
            _standard(t, lst, msg, select, r, offset, length)
            return
        heap = t.system.heap_of(h)
        if not heap.deref(h):
            t.process.resources.LoadResource(h)
        entry = heap.deref(h)
        if not entry or t.mem.r16(entry) == RTS:
            return
        state = heap.get_state(h)
        heap.lock(h)
        try:
            with scratch(t, 8) as rp:
                t.mem.w_rect(rp, r)
                t.call(entry, (("h", msg), ("b", select), ("p", rp), ("P", cell),
                               ("h", offset), ("h", length), ("p", lst.handle)))
        finally:
            heap.set_state(h, state)


def _standard(t, lst: ListRec, msg: int, select: bool, r: Rect, offset: int,
              length: int) -> None:
    """LDEF 0: the data as text, from indent in the cell."""
    tb = t.tb
    if msg == k.lInitMsg:
        lst.indent = Point(tb.GetFontInfo()[0], 5)
    elif msg == k.lDrawMsg:
        tb.EraseRect(r)
        if length > 0:
            data = t.mem.read(t.mem.r32(lst.cells & ADDRESS_MASK) + offset, length)
            ind = lst.indent
            tb.MoveTo(r.left + ind.h, r.top + ind.v)
            tb.DrawString(from_mac(data))
        if select:
            tb.InvertRect(r)
    elif msg == k.lHiliteMsg:
        tb.InvertRect(r)


@trap
def SystemLDEF0(t, msg, select, rect_p, cell, offset, length, h):
    """LDEF 0's code, for 68k code that calls it itself."""
    lst = _list(t, h)
    if lst is not None:
        _standard(t, lst, msg, bool(select), t.rect(rect_p), offset, length)


def draw_cell(t, lst: ListRec, cell: Point) -> None:
    """LDraw: a cell in view, as its LDEF draws it."""
    i = lst.index(cell)
    r = lst.cell_rect(cell)
    if i < 0 or r.empty:
        return
    start, end = lst.span(i)
    select = lst.is_selected(i) and lst.active and _shows_hilite(lst, i)
    _message(t, lst, k.lDrawMsg, select, r, cell, start, end - start)


def _hilite(t, lst: ListRec, i: int) -> None:
    """Have the LDEF show cell i's new selection state, if it's in view."""
    cell = lst.cell(i)
    r = lst.cell_rect(cell)
    if r.empty or not _shows_hilite(lst, i):
        return
    start, end = lst.span(i)
    _message(t, lst, k.lHiliteMsg, lst.is_selected(i), r, cell, start, end - start)


def _select(t, lst: ListRec, i: int, on: bool) -> None:
    if lst.is_selected(i) != on:
        lst.set_selected(i, on)
        if _drawing(lst) and lst.active:
            _hilite(t, lst, i)


def _update(t, lst: ListRec, area: Region) -> None:
    """Draw the cells in view that are in area."""
    vis = lst.visible
    for v in range(vis.top, vis.bottom):
        for h in range(vis.left, vis.right):
            cell = Point(v, h)
            if lst.has(cell) and not area.sect(Region.rect(lst.cell_rect(cell))).empty:
                draw_cell(t, lst, cell)


def _redraw(t, lst: ListRec, r: Rect) -> None:
    """Erase the part of rView in r and draw the cells there again."""
    r = _sect(r, lst.r_view)
    if r.empty or not _drawing(lst):
        return
    with _in_port(t, lst):
        t.tb.EraseRect(r)
    _update(t, lst, Region.rect(r))


def _sect(a: Rect, b: Rect) -> Rect:
    r = Rect(max(a.top, b.top), max(a.left, b.left), min(a.bottom, b.bottom),
             min(a.right, b.right))
    return r if not r.empty else NOWHERE


# =========================================================================
# Scrolling
# =========================================================================
def _sync_bars(t, lst: ListRec) -> None:
    """Scroll bar settings to match how far the list can and has scrolled."""
    b, top_left, vis = lst.data_bounds, lst.scroll_max(), lst.visible
    for h, lo, hi, value in ((lst.v_scroll, b.top, top_left.v, vis.top),
                             (lst.h_scroll, b.left, top_left.h, vis.left)):
        c = _ctl(t, h)
        if c is None or (c.min, c.max, c.value) == (lo, hi, value):
            continue
        c.min, c.max, c.value = lo, hi, value
        if _drawing(lst):
            _draw(t, c)


def _keep_in_range(lst: ListRec) -> bool:
    """Scroll back what's in view if it's past the furthest the list can
    scroll, as after rows are deleted.  Whether it moved."""
    b, top_left, vis = lst.data_bounds, lst.scroll_max(), lst.visible
    top = max(b.top, min(vis.top, top_left.v))
    left = max(b.left, min(vis.left, top_left.h))
    if (top, left) == (vis.top, vis.left):
        return False
    lst.visible = vis.offset(left - vis.left, top - vis.top)
    return True


def _scroll(t, lst: ListRec, d_cols: int, d_rows: int) -> None:
    b, top_left, vis = lst.data_bounds, lst.scroll_max(), lst.visible
    left = max(b.left, min(vis.left + d_cols, top_left.h))
    top = max(b.top, min(vis.top + d_rows, top_left.v))
    dh, dv = left - vis.left, top - vis.top
    if not dh and not dv:
        return
    lst.visible = vis.offset(dh, dv)
    _sync_bars(t, lst)
    if not _drawing(lst):
        return
    size = lst.cell_size
    with _in_port(t, lst) as port:
        if port is None:
            return
        left_behind = t.tb.ScrollRect(lst.r_view, -dh * size.h, -dv * size.v)
    _update(t, lst, left_behind)


def _scroll_to_bars(t, lst: ListRec) -> None:
    vis = lst.visible
    v, h = _ctl(t, lst.v_scroll), _ctl(t, lst.h_scroll)
    _scroll(t, lst, h.value - vis.left if h else 0, v.value - vis.top if v else 0)


def _set_cell_size(t, lst: ListRec, size: Point) -> None:
    """LCellSize: zero is the default, rView's width shared among the
    columns, and a line of the port's font."""
    width, height = size.h, size.v
    if not width:
        width = lst.r_view.width // max(1, lst.columns)
    if not height:
        with _in_port(t, lst):
            ascent, descent, _, leading = t.tb.GetFontInfo()
        height = ascent + descent + leading
    lst.cell_size = Point(height, width)
    lst.fit_visible()
    _sync_bars(t, lst)


# =========================================================================
# Making and disposing of lists
# =========================================================================
def _bar_rects(lst: ListRec, vertical: bool, horizontal: bool) -> tuple[Rect, Rect]:
    """Where the scroll bars go: outside rView, overlapping its frame."""
    view = lst.r_view
    grow = GROW_BOX if lst.list_flags & k.lGrowBox else 0
    v = Rect(view.top - 1, view.right, view.bottom + 1 - (grow if not horizontal else 0),
             view.right + 16)
    h = Rect(view.bottom, view.left - 1, view.bottom + 16,
             view.right + 1 - (grow if not vertical else 0))
    return v, h


@trap
def LNew(t, r_view, data_bounds, c_size, the_proc, window, draw_it, has_grow, scroll_h,
         scroll_v):
    view, bounds = t.rect(r_view), t.rect(data_bounds)
    n = max(bounds.width, 0) * max(bounds.height, 0)
    heap = t.process.heap
    ldef = t.process.resources.defproc("LDEF", the_proc)
    h = heap.new_handle(CELL_ARRAY + 2 * (n + 1), clear=True) if ldef else 0
    cells = heap.new_handle(0) if h else 0
    if not cells:
        if h:
            heap.dispose_handle(h)
        t.set_mem_err(memFullErr)
        return 0
    lst = ListRec(t.system, h)
    lst.r_view = view
    lst.port = window
    lst.active = True
    lst.clik_loc = Point(-0x8000, -0x8000)
    lst.mouse_loc = Point(-1, -1)
    lst.last_click = Point(-1, -1)
    lst.def_proc = ldef
    lst.data_bounds = bounds
    lst.cells = cells
    lst.max_index = 2 * n
    lst.visible = Rect(bounds.top, bounds.left, bounds.top, bounds.left)
    lst.list_flags = (0 if draw_it else k.lDrawingModeOff) | (k.lGrowBox if has_grow else 0)
    _set_cell_size(t, lst, c_size)
    w = t.window(window)
    if w is not None:
        v_rect, h_rect = _bar_rects(lst, bool(scroll_v), bool(scroll_h))
        b, top_left = lst.data_bounds, lst.scroll_max()
        if scroll_v:
            lst.v_scroll = new_control(t, w, v_rect, b"", bool(draw_it), b.top, b.top,
                                       top_left.v, k.scrollBarProc, h)
            lst.list_flags |= k.lDoVAutoscroll
        if scroll_h:
            lst.h_scroll = new_control(t, w, h_rect, b"", bool(draw_it), b.left, b.left,
                                       top_left.h, k.scrollBarProc, h)
            lst.list_flags |= k.lDoHAutoscroll
    _message(t, lst, k.lInitMsg)
    t.set_mem_err(noErr)
    return h


@trap
def LDispose(t, h):
    lst = _list(t, h)
    if lst is None:
        return
    _message(t, lst, k.lCloseMsg)
    for bar in _bars(lst):
        DisposeControl(t, bar)
    cells = lst.cells & ADDRESS_MASK
    if cells:
        t.system.heap_of(cells).dispose_handle(cells)
    t.system.heap_of(lst.handle).dispose_handle(lst.handle)


# =========================================================================
# Rows and columns
# =========================================================================
def _add(t, lst: ListRec, count: int, at: int, rows: bool) -> int:
    b = lst.data_bounds
    at = max(b.top, min(at, b.bottom)) if rows else max(b.left, min(at, b.right))
    if count <= 0:
        return at
    cells, cols = lst.all(), lst.columns
    empty = [(b"", False)]
    if rows:
        i = (at - b.top) * cols
        cells[i:i] = empty * (count * cols)
        bounds = Rect(b.top, b.left, b.bottom + count, b.right)
    else:
        j = at - b.left
        cells = [c for r in range(lst.rows)
                 for c in cells[r * cols:r * cols + j] + empty * count
                 + cells[r * cols + j:(r + 1) * cols]]
        bounds = Rect(b.top, b.left, b.bottom, b.right + count)
    if not lst.rebuild(cells, bounds):
        t.set_mem_err(memFullErr)
        return at
    t.set_mem_err(noErr)
    _sync_bars(t, lst)
    _redraw(t, lst, _from(lst, at, rows))
    return at


def _from(lst: ListRec, at: int, rows: bool) -> Rect:
    """The part of rView showing row (or column) at and those after it."""
    view, size, vis = lst.r_view, lst.cell_size, lst.visible
    if rows:
        return Rect(max(view.top, view.top + (at - vis.top) * size.v), view.left,
                    view.bottom, view.right)
    return Rect(view.top, max(view.left, view.left + (at - vis.left) * size.h),
                view.bottom, view.right)


def _delete(t, lst: ListRec, count: int, at: int, rows: bool) -> None:
    b = lst.data_bounds
    lo, hi = (b.top, b.bottom) if rows else (b.left, b.right)
    if count == 0 or (at <= lo and at + count >= hi):
        at, count = lo, hi - lo                     # all of them
    elif not lo <= at < hi:
        return
    count = min(count, hi - at)
    if count <= 0:
        return
    cells, cols = lst.all(), lst.columns
    if rows:
        del cells[(at - b.top) * cols:(at - b.top + count) * cols]
        bounds = Rect(b.top, b.left, b.bottom - count, b.right)
    else:
        j = at - b.left
        cells = [c for r in range(lst.rows)
                 for c in cells[r * cols:r * cols + j] + cells[r * cols + j + count:(r + 1) * cols]]
        bounds = Rect(b.top, b.left, b.bottom, b.right - count)
    lst.rebuild(cells, bounds)                      # smaller: there's always room
    moved = _keep_in_range(lst)
    _sync_bars(t, lst)
    _redraw(t, lst, lst.r_view if moved else _from(lst, at, rows))


@trap
def LAddRow(t, count, row_num, h):
    lst = _list(t, h)
    return _add(t, lst, count, row_num, True) if lst is not None else 0


@trap
def LAddColumn(t, count, col_num, h):
    lst = _list(t, h)
    return _add(t, lst, count, col_num, False) if lst is not None else 0


@trap
def LDelRow(t, count, row_num, h):
    lst = _list(t, h)
    if lst is not None:
        _delete(t, lst, count, row_num, True)


@trap
def LDelColumn(t, count, col_num, h):
    lst = _list(t, h)
    if lst is not None:
        _delete(t, lst, count, col_num, False)


# =========================================================================
# Cells' data
# =========================================================================
def set_cell(t, lst: ListRec, cell: Point, data: bytes, append: bool = False) -> None:
    """LSetCell (LAddToCell, with append), and the cell drawn again."""
    i = lst.index(cell)
    if i < 0:
        return
    if append:
        data = lst.data(i) + data
    if not lst.set_data(i, data):
        t.set_mem_err(memFullErr)
        return
    t.set_mem_err(noErr)
    if _drawing(lst):
        draw_cell(t, lst, cell)


@trap
def LSetCell(t, data_ptr, data_len, the_cell, h):
    lst = _list(t, h)
    if lst is not None:
        set_cell(t, lst, the_cell, t.mem.read(data_ptr, data_len) if data_len > 0 else b"")


@trap
def LAddToCell(t, data_ptr, data_len, the_cell, h):
    lst = _list(t, h)
    if lst is not None and data_len > 0:
        set_cell(t, lst, the_cell, t.mem.read(data_ptr, data_len), append=True)


@trap
def LClrCell(t, the_cell, h):
    lst = _list(t, h)
    if lst is not None:
        set_cell(t, lst, the_cell, b"")


@trap
def LGetCell(t, data_ptr, data_len_p, the_cell, h):
    """As much of the data as fits in *dataLen, and how much that was."""
    lst = _list(t, h)
    i = lst.index(the_cell) if lst is not None else -1
    data = lst.data(i) if i >= 0 else b""
    n = max(0, min(len(data), t.mem.r16s(data_len_p)))
    if n:
        t.mem.write(data_ptr, data[:n])
    t.w16(data_len_p, n)


@trap
def LGetCellDataLocation(t, offset_p, len_p, the_cell, h):
    """Where the cell's data is in the cells block: -1 for no cell."""
    lst = _list(t, h)
    i = lst.index(the_cell) if lst is not None else -1
    if i < 0:
        t.w16(offset_p, -1)
        t.w16(len_p, -1)
        return
    start, end = lst.span(i)
    t.w16(offset_p, start)
    t.w16(len_p, end - start)


# =========================================================================
# Selection
# =========================================================================
@trap
def LSetSelect(t, set_it, the_cell, h):
    lst = _list(t, h)
    i = lst.index(the_cell) if lst is not None else -1
    if i >= 0:
        _select(t, lst, i, bool(set_it))


@trap
def LGetSelect(t, next_, cell_p, h):
    """With next, the first selected cell from *theCell on, row by row,
    into *theCell; else whether *theCell is selected."""
    lst = _list(t, h)
    if lst is None or not cell_p:
        return False
    cell = t.point(cell_p)
    if not next_:
        i = lst.index(cell)
        return i >= 0 and lst.is_selected(i)
    found = lst.first_selected(cell)
    if found is None:
        return False
    t.set_point(cell_p, found)
    return True


@trap
def LNextCell(t, h_next, v_next, cell_p, h):
    lst = _list(t, h)
    if lst is None:
        return False
    cell = lst.next_cell(t.point(cell_p), bool(h_next), bool(v_next))
    if cell is None:
        return False
    t.set_point(cell_p, cell)
    return True


@trap
def LSearch(t, data_ptr, data_len, search_proc, cell_p, h):
    """The first cell from *theCell on, row by row, whose data matches,
    as the search procedure says (0 for a match) or, without one,
    IUMagIDString."""
    lst = _list(t, h)
    if lst is None:
        return False
    data_len = max(data_len, 0)
    target = t.mem.read(data_ptr, data_len)
    cells = lst.cells & ADDRESS_MASK
    heap = t.system.heap_of(cells)
    cell = t.point(cell_p)
    while cell is not None:
        i = lst.index(cell)
        if i >= 0:
            if search_proc:
                start, end = lst.span(i)
                state = heap.get_state(cells)
                heap.lock(cells)
                try:
                    differ = t.call(search_proc, (("p", data_ptr), ("p", heap.deref(cells) + start),
                                                  ("h", data_len), ("h", end - start)), "h")
                finally:
                    heap.set_state(cells, state)
                match = differ & 0xFFFF == 0
            else:
                match = intl.equal(target, lst.data(i)) == 0
            if match:
                t.set_point(cell_p, cell)
                return True
        cell = lst.next_cell(cell, True, True)
    return False


# =========================================================================
# Drawing
# =========================================================================
@trap
def LDraw(t, the_cell, h):
    lst = _list(t, h)
    if lst is not None:
        draw_cell(t, lst, the_cell)


@trap
def LUpdate(t, rgn, h):
    """The cells, and the scroll bars, in rgn."""
    lst = _list(t, h)
    if lst is None:
        return
    area = read_rgn(t.mem, rgn)
    _update(t, lst, area)
    for bar in _bars(lst):
        c = _ctl(t, bar)
        if c is not None and not area.sect(Region.rect(c.rect)).empty:
            _draw(t, c)


@trap
def LSetDrawingMode(t, draw_it, h):
    """Drawing back on shows the scroll bars, but the cells are drawn by
    the application's LUpdate."""
    lst = _list(t, h)
    if lst is None:
        return
    if not draw_it:
        lst.list_flags |= k.lDrawingModeOff
        return
    was_off = not _drawing(lst)
    lst.list_flags &= ~k.lDrawingModeOff
    if was_off and lst.active:
        for bar in _bars(lst):
            c = _ctl(t, bar)
            if c is not None and c.visible:
                _draw(t, c)
            else:
                ShowControl(t, bar)


@trap
def LActivate(t, act, h):
    """The selection highlighted or not, the scroll bars shown or hidden."""
    lst = _list(t, h)
    if lst is None or lst.active == bool(act):
        return
    lst.active = bool(act)
    if not _drawing(lst):
        return
    for bar in _bars(lst):
        (ShowControl if act else HideControl)(t, bar)
    for i in lst.selected():
        _hilite(t, lst, i)


@trap
def LCellSize(t, c_size, h):
    lst = _list(t, h)
    if lst is not None:
        _set_cell_size(t, lst, c_size)


@trap
def LRect(t, r_p, the_cell, h):
    lst = _list(t, h)
    t.set_rect(r_p, lst.cell_rect(the_cell) if lst is not None else NOWHERE)


@trap
def LScroll(t, d_cols, d_rows, h):
    lst = _list(t, h)
    if lst is not None:
        _scroll(t, lst, d_cols, d_rows)


@trap
def LAutoScroll(t, h):
    """Bring the first selected cell to the top left, if it's out of view."""
    lst = _list(t, h)
    if lst is None:
        return
    cell = lst.first_selected(lst.data_bounds.top_left)
    vis = lst.visible
    if cell is not None and not vis.contains(cell):
        _scroll(t, lst, cell.h - vis.left, cell.v - vis.top)


@trap
def LSize(t, width, height, h):
    """A new size for rView, with the scroll bars moved to fit; what
    changed is left to an update event."""
    lst = _list(t, h)
    if lst is None:
        return
    old = lst.r_view
    view = lst.r_view = old.sized(width, height)
    lst.fit_visible()
    _keep_in_range(lst)
    v_rect, h_rect = _bar_rects(lst, bool(lst.v_scroll), bool(lst.h_scroll))
    for bar, r in ((lst.v_scroll, v_rect), (lst.h_scroll, h_rect)):
        if bar & ADDRESS_MASK:
            MoveControl(t, bar, r.left, r.top)
            SizeControl(t, bar, r.width, r.height)
    _sync_bars(t, lst)
    if _drawing(lst):
        with _in_port(t, lst):
            t.tb.InvalRect(Rect(old.top - 1, old.left - 1, old.bottom + 16,
                                old.right + 16).union(Rect(view.top - 1, view.left - 1,
                                                           view.bottom + 16, view.right + 16)))


# =========================================================================
# Following the mouse
# =========================================================================
@trap
def LLastClick(t, h):
    lst = _list(t, h)
    cell = lst.last_click if lst is not None else Point(-1, -1)
    return (cell.v & 0xFFFF) << 16 | cell.h & 0xFFFF


def _mouse(t, lst: ListRec) -> Point:
    port = t.system.port_at(lst.port & ADDRESS_MASK)
    pos = t.tb._d.input.mac_pos
    return port.global_to_local(pos) if port is not None else pos


def _clik_loop(t, lst: ListRec) -> bool:
    """Call lClikLoop: TRUE in D0 to go on.  A word is kept for it on the
    stack, in case it was written as a Pascal function."""
    cpu = t.cpu
    ctx = cpu.context()
    cpu.sp = cpu.sp - 2
    with _in_port(t, lst):
        t._jsr(lst.clik_loop & ADDRESS_MASK)
    go = cpu.d(0) & 0xFF != 0
    cpu.restore(ctx)
    return go


def _track_bar(t, lst: ListRec, bar: int, pt: Point, part: int) -> None:
    if part == k.inThumb:
        if TrackControl(t, bar, pt, 0):
            _scroll_to_bars(t, lst)
        return

    def step(h, part):
        c = _ctl(t, h)
        if c is None:
            return
        vis = lst.visible
        page = max((vis.height if h == lst.v_scroll & ADDRESS_MASK else vis.width) - 1, 1)
        delta = {k.inUpButton: -1, k.inDownButton: 1, k.inPageUp: -page,
                 k.inPageDown: page}.get(part, 0)
        SetControlValue(t, h, c.value + delta)
        _scroll_to_bars(t, lst)
    TrackControl(t, bar, pt, step)


class _Selection:
    """What a click and drag in the cells does to the selection
    (Inside Macintosh IV-267): with the Command key, each cell the mouse
    passes takes the opposite of the first's state; with Shift, the
    selection is the rectangle from it, or from the cells already
    selected, to the mouse; with neither, the one cell under the mouse.
    selFlags change that."""

    def __init__(self, t, lst: ListRec, start: Point, mods: int):
        self.t, self.lst = t, lst
        flags = lst.sel_flags
        only_one = flags & k.lOnlyOne
        shift = bool(mods & k.shiftKey) and not only_one
        cmd = bool(mods & k.cmdKey) and not only_one and not flags & k.lNoDisjoint
        i = lst.index(start)
        was = i >= 0 and lst.is_selected(i)
        self.before = set(lst.selected())
        self.keep = False               # cells outside the rectangle keep their state
        if cmd:
            self.mode, self.sense = "pass", not was
        elif shift and flags & k.lNoRect:
            self.mode, self.sense = "pass", not was if flags & k.lUseSense else True
        elif shift or (flags & k.lExtendDrag and not only_one):
            self.mode = "rect"
            self.sense = not was if shift and flags & k.lUseSense else True
            self.keep = shift and bool(flags & (k.lUseSense | k.lNoExtend))
            self.anchor = self._clamp(start)
            if shift and not self.keep and self.before:
                for j in self.before:
                    c = lst.cell(j)
                    self.anchor = self._span(self.anchor, c)
            elif not shift:
                self.before = set()
        else:
            self.mode = "one"

    def _clamp(self, cell: Point) -> Rect:
        b = self.lst.data_bounds
        v = max(b.top, min(cell.v, b.bottom - 1))
        h = max(b.left, min(cell.h, b.right - 1))
        return Rect(v, h, v + 1, h + 1)

    @staticmethod
    def _span(r: Rect, cell: Point) -> Rect:
        return Rect(min(r.top, cell.v), min(r.left, cell.h), max(r.bottom, cell.v + 1),
                    max(r.right, cell.h + 1))

    def track(self, cell: Point, first: bool = False) -> None:
        lst, t = self.lst, self.t
        i = lst.index(cell)
        if self.mode == "one":
            if i < 0 and not first:
                return
            for j in lst.selected():
                if j != i:
                    _select(t, lst, j, False)
            if i >= 0:
                _select(t, lst, i, True)
        elif self.mode == "pass":
            if i >= 0:
                _select(t, lst, i, self.sense)
        elif lst.count:
            c = self._clamp(cell)
            r = self._span(self.anchor, Point(c.top, c.left))
            for j in range(lst.count):
                inside = r.contains(lst.cell(j))
                _select(t, lst, j, self.sense if inside else self.keep and j in self.before)


def _autoscroll(t, lst: ListRec, mouse: Point) -> None:
    """A cell's worth toward the mouse when it's outside rView."""
    view, flags = lst.r_view, lst.list_flags
    dv = dh = 0
    if flags & k.lDoVAutoscroll:
        dv = -1 if mouse.v < view.top else 1 if mouse.v >= view.bottom else 0
    if flags & k.lDoHAutoscroll:
        dh = -1 if mouse.h < view.left else 1 if mouse.h >= view.right else 0
    if dv or dh:
        _scroll(t, lst, dh, dv)


@trap
def LClick(t, pt, modifiers, h):
    """Follow a click in the list until the button comes up.  TRUE for a
    double click: the same cell again, within the double-click time."""
    lst = _list(t, h)
    if lst is None:
        return False
    for bar in _bars(lst):
        part = TestControl(t, bar, pt)
        if part:
            _track_bar(t, lst, bar, pt, part)
            return False
    view = lst.r_view
    if not view.contains(pt):
        return False
    start = lst.cell_at(pt)
    now = t.tb.TickCount()
    double = (start == lst.last_click and lst.has(start)
              and now - lst.clik_time <= t.mem.r32(lowmem.DoubleTime))
    lst.clik_loc = pt
    lst.last_click = start
    selection = _Selection(t, lst, start, modifiers)
    selection.track(start, first=True)
    last = start
    while True:
        mouse = _mouse(t, lst)
        lst.mouse_loc = mouse
        if lst.clik_loop & ADDRESS_MASK and not _clik_loop(t, lst):
            break
        _autoscroll(t, lst, mouse)
        cell = lst.cell_at(Point(max(view.top, min(mouse.v, view.bottom - 1)),
                                 max(view.left, min(mouse.h, view.right - 1))))
        if cell != last:
            selection.track(cell)
            last = cell
        if not t.tb.StillDown():
            break
        t.sleep(1)
    lst.clik_time = t.tb.TickCount()
    return double
