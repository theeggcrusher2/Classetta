"""The List Manager: lists kept as the ROM keeps them, LDEF 0, and a 68k
application's list followed through clicks and its scroll bar."""

from pathlib import Path

import pytest
from conftest import NO_MODS, Recorder
from guest import Guest
from PyQt6.QtCore import Qt

from macrt.macfile import open_application
from macrt.resfork import Resource
from macrt.toolbox import constants as k
from macrt.toolbox import lowmem
from macrt.toolbox.controls import Control
from macrt.toolbox.lists import CELL_ARRAY, ListRec
from macrt.toolbox.qdraw import Region, write_rgn
from macrt.toolbox.types import Point, Rect


def new_list(g, bounds=(0, 0, 3, 1), view=(0, 0, 30, 40), size=Point(0, 0), proc=0,
             draw=False):
    port = g.tb.GetPort()
    if port is None:
        port, _ = g.port(64, 48)
    h = g("LNew", g.rect_ptr(view), g.rect_ptr(bounds), size, proc, port.addr, draw, False,
          False, False)
    assert h
    return ListRec(g.system, h)


def offsets(g, lst):
    return [g.mem.r16(lst.addr + CELL_ARRAY + 2 * i) for i in range(lst.count + 1)]


def put(g, lst, v, h, data: bytes, trap="LSetCell"):
    g(trap, g.ptr(data), len(data), Point(v, h), lst.handle)


def cell_text(g, lst, v, h, room=255):
    buf, length = g.ptr(bytes(room)), g.ptr(room.to_bytes(2, "big"))
    g("LGetCell", buf, length, Point(v, h), lst.handle)
    return g.mem.read(buf, g.mem.r16(length))


def texts(g, lst):
    b = lst.data_bounds
    return [[cell_text(g, lst, v, h) for h in range(b.left, b.right)]
            for v in range(b.top, b.bottom)]


# =========================================================================
# The record
# =========================================================================
def test_a_new_list_is_laid_out_as_the_rom_makes_it(qapp):
    g = Guest()
    lst = new_list(g, bounds=(0, 0, 5, 2))
    ascent, descent, _, leading = g.tb.GetFontInfo()
    assert lst.r_view == Rect(0, 0, 30, 40)
    assert lst.port == g.tb.GetPort().addr
    assert lst.cell_size == Point(ascent + descent + leading, 20)    # rView's width, shared
    rows = -(-30 // lst.cell_size.v)
    assert lst.visible == Rect(0, 0, rows, 2)
    assert lst.indent == Point(ascent, 5)                           # LDEF 0's
    assert lst.def_proc == g.system.defproc("LDEF", 0)
    assert lst.max_index == 20 and offsets(g, lst) == [0] * 11
    assert g.heap.handle_size(lst.cells) == 0
    assert lst.active and lst.last_click == Point(-1, -1)
    assert lst.list_flags == k.lDrawingModeOff
    assert g.heap.handle_size(lst.handle) == CELL_ARRAY + 22


def test_cells_keep_their_data_where_the_rom_does(qapp):
    g = Guest()
    lst = new_list(g, bounds=(0, 0, 3, 1))
    put(g, lst, 1, 0, b"Second")
    put(g, lst, 0, 0, b"One")
    put(g, lst, 0, 0, b"!", trap="LAddToCell")
    assert offsets(g, lst) == [0, 4, 10, 10]
    assert g.contents(lst.cells) == b"One!Second"
    assert texts(g, lst) == [[b"One!"], [b"Second"], [b""]]
    assert cell_text(g, lst, 1, 0, room=3) == b"Sec"             # as much as fits

    offset, length = g.ptr(bytes(2)), g.ptr(bytes(2))
    g("LGetCellDataLocation", offset, length, Point(1, 0), lst.handle)
    assert (g.mem.r16s(offset), g.mem.r16s(length)) == (4, 6)
    g("LGetCellDataLocation", offset, length, Point(3, 0), lst.handle)
    assert (g.mem.r16s(offset), g.mem.r16s(length)) == (-1, -1)

    g("LClrCell", Point(0, 0), lst.handle)
    assert offsets(g, lst) == [0, 0, 6, 6]
    assert g.contents(lst.cells) == b"Second"
    put(g, lst, 5, 0, b"nowhere")                                 # outside dataBounds
    assert g.contents(lst.cells) == b"Second"


def test_the_selection_is_each_cells_high_bit(qapp):
    g = Guest()
    lst = new_list(g, bounds=(0, 0, 3, 2))
    put(g, lst, 0, 1, b"x")
    g("LSetSelect", True, Point(0, 1), lst.handle)
    g("LSetSelect", True, Point(2, 0), lst.handle)
    assert offsets(g, lst) == [0, 0x8000, 1, 1, 0x8001, 1, 1]
    cell = g.ptr(bytes(4))
    assert g("LGetSelect", False, cell, lst.handle) is False      # (0,0) isn't
    assert g("LGetSelect", True, cell, lst.handle) is True
    assert g.point(cell) == Point(0, 1)
    g.set_point(cell, Point(0, 2))                                # past the row's end
    assert g("LGetSelect", True, cell, lst.handle) is True
    assert g.point(cell) == Point(2, 0)
    g.set_point(cell, Point(2, 1))
    assert g("LGetSelect", True, cell, lst.handle) is False
    assert g.point(cell) == Point(2, 1)
    g("LSetSelect", False, Point(0, 1), lst.handle)
    assert offsets(g, lst)[1] == 0


def test_rows_and_columns_come_and_go_with_their_data(qapp):
    g = Guest()
    lst = new_list(g, bounds=(0, 0, 2, 2))
    for v, h, s in ((0, 0, b"a"), (0, 1, b"b"), (1, 0, b"c"), (1, 1, b"d")):
        put(g, lst, v, h, s)
    g("LSetSelect", True, Point(1, 1), lst.handle)

    assert g("LAddRow", 1, 1, lst.handle) == 1
    assert texts(g, lst) == [[b"a", b"b"], [b"", b""], [b"c", b"d"]]
    assert g("LAddRow", 2, 99, lst.handle) == 3                   # past the end: at the end
    assert lst.data_bounds == Rect(0, 0, 5, 2) and lst.max_index == 20
    assert g("LAddColumn", 1, 1, lst.handle) == 1
    assert texts(g, lst)[0] == [b"a", b"", b"b"] and texts(g, lst)[2] == [b"c", b"", b"d"]
    cell = g.ptr(bytes(4))
    assert g("LGetSelect", True, cell, lst.handle) and g.point(cell) == Point(2, 2)

    g("LDelColumn", 1, 1, lst.handle)
    g("LDelRow", 3, 3, lst.handle)                                # more than there are
    g("LDelRow", 1, 1, lst.handle)
    assert texts(g, lst) == [[b"a", b"b"], [b"c", b"d"]]
    g.set_point(cell, Point(0, 0))
    assert g("LGetSelect", True, cell, lst.handle) and g.point(cell) == Point(1, 1)
    assert g.contents(lst.cells) == b"abcd"

    g("LDelRow", 0, 0, lst.handle)                                # 0: all of them
    assert lst.data_bounds == Rect(0, 0, 0, 2) and offsets(g, lst) == [0]
    assert g.heap.handle_size(lst.cells) == 0


def test_lnextcell_goes_along_rows_or_down_columns(qapp):
    g = Guest()
    lst = new_list(g, bounds=(0, 0, 2, 2))
    cell = g.ptr(bytes(4))

    def walk(h_next, v_next):
        g.set_point(cell, Point(0, 0))
        out = [g.point(cell)]
        while g("LNextCell", h_next, v_next, cell, lst.handle):
            out.append(g.point(cell))
        return out
    assert walk(True, True) == [Point(0, 0), Point(0, 1), Point(1, 0), Point(1, 1)]
    assert walk(True, False) == [Point(0, 0), Point(0, 1)]
    assert walk(False, True) == [Point(0, 0), Point(1, 0), Point(0, 1), Point(1, 1)]
    assert walk(False, False) == [Point(0, 0)]


def test_lsearch_matches_as_iumagidstring_or_the_applications_procedure(qapp):
    g = Guest()
    lst = new_list(g, bounds=(0, 0, 4, 1))
    for v, s in enumerate((b"apple", b"Pear", b"pear", b"fig")):
        put(g, lst, v, 0, s)
    cell = g.ptr(bytes(4))
    key = g.ptr(b"pear")
    assert g("LSearch", key, 4, 0, cell, lst.handle) is True
    assert g.point(cell) == Point(2, 0)                           # case matters
    assert g("LSearch", g.ptr(b"kiwi"), 4, 0, cell, lst.handle) is False

    proc = 0x500000
    seen = []

    def shorter(a, b, a_len, b_len):
        seen.append(g.mem.read(b, b_len))
        return 0 if b_len < a_len else 1
    g.procs[proc] = shorter
    g.set_point(cell, Point(1, 0))
    assert g("LSearch", key, 4, proc, cell, lst.handle) is True
    assert g.point(cell) == Point(3, 0) and seen == [b"Pear", b"pear", b"fig"]


# =========================================================================
# Where the cells are, and scrolling
# =========================================================================
def test_cells_are_where_rview_and_visible_put_them(qapp):
    g = Guest()
    lst = new_list(g, bounds=(0, 0, 10, 2), view=(10, 4, 40, 44), size=Point(10, 20))
    assert lst.visible == Rect(0, 0, 3, 2)
    r = g.ptr(bytes(8))
    g("LRect", r, Point(1, 1), lst.handle)
    assert g.rect(r) == Rect(20, 24, 30, 44)
    g("LRect", r, Point(5, 0), lst.handle)
    assert g.rect(r) == Rect(0, 0, 0, 0)                          # scrolled out of view

    g("LScroll", 0, 4, lst.handle)
    assert lst.visible == Rect(4, 0, 7, 2)
    g("LRect", r, Point(5, 0), lst.handle)
    assert g.rect(r) == Rect(20, 4, 30, 24)
    g("LScroll", 0, 100, lst.handle)
    assert lst.visible.top == 7                                   # the last row at the bottom
    g("LScroll", 0, -100, lst.handle)
    assert lst.visible.top == 0

    g("LSetSelect", True, Point(8, 1), lst.handle)
    g("LAutoScroll", lst.handle)
    assert lst.visible.top == 7                                   # as far as it goes
    assert g("LLastClick", lst.handle) == 0xFFFFFFFF                # no click yet: (-1, -1)


def test_a_partly_shown_last_row_can_be_scrolled_into_view(qapp):
    g = Guest()
    lst = new_list(g, bounds=(0, 0, 10, 1), view=(0, 0, 35, 40), size=Point(10, 40))
    assert lst.visible == Rect(0, 0, 4, 1)                        # 3 and a half rows
    assert lst.scroll_max() == Point(7, 0)
    g("LDelRow", 8, 2, lst.handle)                                # two rows left
    assert lst.scroll_max() == Point(0, 0)


# =========================================================================
# Drawing
# =========================================================================
def black(rows, r: Rect) -> int:
    return sum(row[r.left:r.right].count("#") for row in rows[r.top:r.bottom])


def test_ldef_0_draws_the_text_and_inverts_the_selection(qapp):
    g = Guest()
    port, _ = g.port(64, 48)
    lst = new_list(g, bounds=(0, 0, 2, 1), view=(0, 0, 40, 64), size=Point(16, 64), draw=True)
    put(g, lst, 0, 0, b"Hi")
    first, second = Rect(0, 0, 16, 64), Rect(16, 0, 32, 64)
    rows = g.rows(port)
    assert 0 < black(rows, first) < 16 * 64 // 4 and black(rows, second) == 0
    assert black(rows, Rect(0, 0, 16, 5)) == 0                    # indented

    g("LSetSelect", True, Point(0, 0), lst.handle)
    rows = g.rows(port)
    assert black(rows, first) > 16 * 64 * 3 // 4
    g("LSetSelect", True, Point(1, 0), lst.handle)                # an empty cell too
    assert black(g.rows(port), second) == 16 * 64

    lst.sel_flags = k.lNoNilHilite
    g("LSetSelect", False, Point(1, 0), lst.handle)               # not highlighted: stays
    assert black(g.rows(port), second) == 16 * 64
    g("LDraw", Point(1, 0), lst.handle)
    assert black(g.rows(port), second) == 0

    g("LActivate", False, lst.handle)
    assert black(g.rows(port), first) < 16 * 64 // 4
    g("LActivate", True, lst.handle)
    assert black(g.rows(port), first) > 16 * 64 * 3 // 4


def test_scrolling_moves_what_is_drawn_and_draws_what_comes_into_view(qapp):
    g = Guest()
    port, _ = g.port(64, 48)
    lst = new_list(g, bounds=(0, 0, 4, 1), view=(0, 0, 32, 64), size=Point(16, 64), draw=True)
    for v in range(4):
        put(g, lst, v, 0, b"#" * (v + 1))
    g("LSetSelect", True, Point(2, 0), lst.handle)
    before = g.rows(port)
    g("LScroll", 0, 1, lst.handle)
    after = g.rows(port)
    assert after[0:16] == before[16:32]                           # row 1 moved up
    assert black(after, Rect(16, 0, 32, 64)) > 16 * 64 * 3 // 4  # row 2, selected


def test_drawing_off_leaves_the_port_alone(qapp):
    g = Guest()
    port, _ = g.port(64, 48)
    lst = new_list(g, bounds=(0, 0, 2, 1), view=(0, 0, 32, 64), size=Point(16, 64))
    put(g, lst, 0, 0, b"Hi")
    g("LSetSelect", True, Point(0, 0), lst.handle)
    assert black(g.rows(port), Rect(0, 0, 48, 64)) == 0
    g("LSetDrawingMode", True, lst.handle)
    rgn = g.heap.new_handle(10)
    write_rgn(g.heap, rgn, Region.rect(Rect(0, 0, 48, 64)))
    g("LUpdate", rgn, lst.handle)
    assert black(g.rows(port), Rect(0, 0, 16, 64)) > 16 * 64 * 3 // 4


def test_an_applications_ldef_is_called_with_the_roms_parameters(qapp):
    code = bytes.fromhex("4E71 4E75")                             # NOP; RTS
    g = Guest([Resource(b"LDEF", 128, code)])
    port, _ = g.port(64, 48)
    seen = []

    def ldef(msg, select, r, cell, offset, length, h):
        seen.append((msg, select, g.mem.r_rect(r), cell, offset, length, h))
    g.procs = _ProcsAt(lambda addr: ldef)
    lst = new_list(g, bounds=(0, 0, 2, 1), view=(0, 0, 32, 64), size=Point(16, 64), proc=128,
                   draw=True)
    entry = g.heap.deref(lst.def_proc)
    assert g.calls[0][0] == entry
    assert seen == [(k.lInitMsg, 0, Rect(0, 0, 0, 0), Point(0, 0), 0, 0, lst.handle)]
    put(g, lst, 1, 0, b"xyz")
    assert seen[-1] == (k.lDrawMsg, 0, Rect(16, 0, 32, 64), Point(1, 0), 0, 3, lst.handle)
    g("LSetSelect", True, Point(1, 0), lst.handle)
    assert seen[-1] == (k.lHiliteMsg, 1, Rect(16, 0, 32, 64), Point(1, 0), 0, 3, lst.handle)
    g("LDispose", lst.handle)
    assert seen[-1][0] == k.lCloseMsg
    assert not g.heap.is_handle(lst.handle)


class _ProcsAt(dict):
    """Guest.procs that answers for any address."""

    def __init__(self, proc_for):
        super().__init__()
        self.proc_for = proc_for

    def get(self, addr, default=None):
        return self.proc_for(addr)


# =========================================================================
# Lists (tests/apps/lists.s): a 68k application's list, clicked with
# synthetic input
# =========================================================================
APPS = Path(__file__).parent / "apps"
LIST, CLICKS, DOUBLES = -238, -240, -242
SHIFT, COMMAND = Qt.KeyboardModifier.ShiftModifier, Qt.KeyboardModifier.ControlModifier


class App:
    def __init__(self, h):
        f = open_application(APPS / "Lists.bin")
        h.launch(Recorder("Other"))
        self.h = h
        self.proc = h.d.launch(f.name, None, app=f)
        h.wait_until(lambda: self.proc.windows and self.proc.windows[0].visible, timeout=10,
                     what="the window")
        self.window = self.proc.windows[0]
        self.system = self.proc.system
        self.mem = self.system.mem
        self.a5 = self.proc.partition.a5
        h.wait_until(lambda: self.mem.r32(self.a5 + LIST), what="the list")
        self.list = ListRec(self.system, self.mem.r32(self.a5 + LIST))
        h.wait_until(lambda: self.ink(self.position(0)) and self.black(19, 9), what="it drawn")

    def word(self, offset):
        return self.mem.r16s(self.a5 + offset)

    def black(self, h, v):
        with self.window.backing.lock:
            return self.window.backing.image.pixel(h, v) & 0xFFFFFF == 0

    def picture(self, r: Rect) -> tuple[str, ...]:
        with self.window.backing.lock:
            image = self.window.backing.image
            return tuple("".join("#" if image.pixel(h, v) & 0xFFFFFF == 0 else "."
                                 for h in range(r.left, r.right))
                         for v in range(r.top, r.bottom))

    def ink(self, r: Rect) -> int:
        return sum(row.count("#") for row in self.picture(r))

    def position(self, p: int) -> Rect:
        """Where the p'th row from the top of rView is."""
        view, height = self.list.r_view, self.list.cell_size.v
        return Rect(view.top + p * height, view.left,
                    min(view.top + (p + 1) * height, view.bottom), view.right)

    def row_rect(self, row: int) -> Rect:
        with self.system.lock:
            return self.list.cell_rect(Point(row, 0))

    def click(self, h, v, mods=NO_MODS):
        c = self.window.content
        self.h.click(c.left + h, c.top + v, mods)

    def click_row(self, row: int, mods=NO_MODS):
        r = self.row_rect(row)
        assert r.height, f"row {row} isn't in view"
        clicks = self.word(CLICKS)
        self.click((r.left + r.right) // 2, (r.top + r.bottom) // 2, mods)
        self.h.wait_until(lambda: self.word(CLICKS) == clicks + 1, what=f"the click on {row}")

    def selected(self) -> list[int]:
        with self.system.lock:
            return self.list.selected()

    def shown(self, row: int) -> bool:
        """Whether the row is drawn highlighted."""
        r = self.row_rect(row)
        return self.ink(r) > r.width * r.height * 3 // 4


@pytest.fixture
def app(harness):
    return App(harness())


def test_the_list_is_made_and_drawn(app):
    lst = app.list
    assert lst.data_bounds == Rect(0, 0, 20, 1) and lst.r_view == Rect(10, 20, 130, 120)
    height = lst.cell_size.v
    assert lst.cell_size.h == 100 and lst.visible == Rect(0, 0, -(-120 // height), 1)
    assert [lst.data(i) for i in range(20)] == [str(n).encode() for n in range(1, 21)]
    assert lst.list_flags & k.lDrawingModeOff == 0
    rows = range(120 // height)
    # The fixture saw the first row drawn; the program may still be drawing the rest.
    app.h.wait_until(lambda: all(app.ink(app.position(p)) for p in rows) and
                     app.black(120, 70) and app.black(135, 70), what="the rows and the bar")
    assert not any(app.shown(row) for row in rows)
    bar = Control(app.mem, lst.v_scroll)
    assert bar.rect == Rect(9, 120, 131, 136) and bar.vis
    assert (bar.min, bar.value, bar.max) == (0, 0, lst.scroll_max().v)
    assert bar.max == 20 - 120 // height
    assert app.proc.runner.unimplemented_calls == {}


def test_a_click_selects_the_cell_under_it(app):
    app.click_row(2)
    assert app.selected() == [2]
    app.h.wait_until(lambda: app.shown(2), what="it highlighted")
    app.click_row(4)
    assert app.selected() == [4]
    app.h.wait_until(lambda: app.shown(4) and not app.shown(2), what="the highlight moved")
    assert app.word(DOUBLES) == 0


def test_shift_and_command_add_to_the_selection(app):
    app.click_row(1)
    app.click_row(3, SHIFT)
    assert app.selected() == [1, 2, 3]
    app.click_row(2, COMMAND)
    assert app.selected() == [1, 3]
    app.h.wait_until(lambda: [app.shown(r) for r in range(5)] ==
                     [False, True, False, True, False], what="those highlighted")


def test_a_double_click_is_reported(app):
    app.mem.w32(lowmem.DoubleTime, 600)                 # however slow the test machine
    app.click_row(5)
    app.click_row(5)
    assert app.word(DOUBLES) == 1
    app.click_row(6)
    assert app.word(DOUBLES) == 1                       # another cell: not a double click


def test_dragging_moves_the_selection_and_scrolls_past_the_bottom(app):
    h, c = app.h, app.window.content
    r = app.row_rect(1)
    h.press(c.left + 70, c.top + r.top + 2)
    h.settle(60)
    h.move(c.left + 70, c.top + app.row_rect(3).top + 2)
    h.wait_until(lambda: app.selected() == [3], what="the selection dragged")
    h.move(c.left + 70, c.top + 140)                    # below the list
    h.wait_until(lambda: app.list.visible.top >= 2, what="it scrolled")
    h.release(c.left + 70, c.top + 140)
    h.wait_until(lambda: app.word(CLICKS) == 1, what="LClick done")
    top, bar = app.list.visible.top, Control(app.mem, app.list.v_scroll)
    assert bar.value == top
    assert app.selected() == [app.list.cell_at(Point(129, 70)).v]  # the cell at the bottom


def test_the_scroll_bar_scrolls_the_list(app):
    lst, height = app.list, app.list.cell_size.v
    whole = 120 // height
    before = {row: app.picture(app.position(row)) for row in range(whole)}
    app.click(128, 123)                                 # the down arrow
    app.h.wait_until(lambda: lst.visible.top >= 1, what="a line down")
    app.h.settle()
    top = lst.visible.top
    assert Control(app.mem, lst.v_scroll).value == top
    for row in range(top, whole):                       # moved up, not redrawn differently
        assert app.picture(app.position(row - top)) == before[row]
    app.click(128, 110)                                 # below the thumb: a page
    app.h.wait_until(lambda: lst.visible.top >= top + whole - 1, what="a page down")
    app.h.settle()
    top = lst.visible.top
    assert Control(app.mem, lst.v_scroll).value == top
    data = lst.data(lst.index(Point(top, 0)))
    assert data == str(top + 1).encode() and app.ink(app.position(0))
