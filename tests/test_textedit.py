"""TextEdit (macrt.toolbox.textedit): edit records laid out as Inside
Macintosh gives them, their text broken into lines as TextEdit breaks it,
edited, selected and drawn, checked in the TERec and bit by bit in a 1-bit
bitmap in guest memory.

Widths are measured as TextEdit measures them, in the port's font
(Monaco 9), whatever the host has standing in for it."""

import struct

import pytest
from PyQt6.QtGui import QFont

from macrt.toolbox import lowmem, qdraw
from macrt.toolbox import textedit as te
from macrt.toolbox.qdraw import Region
from macrt.toolbox.quickdraw import GrafPort
from macrt.toolbox.system import System
from macrt.toolbox.textedit import (BACKSPACE, DOWN, LEFT, RIGHT, UP, EditRecord, Style,
                                    Tracker, doFace, doSize)
from macrt.toolbox.types import Point, Rect

W, H = 160, 48                  # the offscreen bitmap
ROW_BYTES = W // 8
MONACO = 4
bold = 1


@pytest.fixture
def qd(qapp):
    return qdraw.QuickDraw(System(), qdraw.Fonts(QFont()), lambda port: None)


@pytest.fixture
def w(qd):
    """The width of some text in Monaco 9, a character at a time, as it's drawn."""
    return lambda text: sum(qd.fonts.advances(MONACO, 9, 0, text.decode("mac_roman")))


def offscreen(qd):
    """A port on a fresh, white bitmap, in Monaco 9."""
    heap = qd.sys.sys_heap
    bounds = Rect(0, 0, H, W)
    bits = heap.new_ptr(ROW_BYTES * H, clear=True)
    port = GrafPort(qd.mem, heap.new_ptr(GrafPort.SIZE))
    port.open(heap, qd.sys.screen_bits)
    port.port_bits_base = bits
    port.port_bits_row_bytes = ROW_BYTES
    port.port_bits_bounds = bounds
    port.port_rect = bounds
    qdraw.write_rgn(heap, port.vis_rgn, Region.rect(bounds))
    qd.sys.register(port)
    port.tx_font, port.tx_size = MONACO, 9
    return port, bits


class Record:
    """An edit record in an offscreen port.  Calling it gives the
    EditRecord for one call, as each trap makes one."""

    def __init__(self, qd, width, *, view=None, styled=False):
        self.qd, self.mem = qd, qd.mem
        self.port, self.bits = offscreen(qd)
        dest = Rect(2, 2, H - 2, 2 + width)
        self.h = te.new_record(qd.sys, qd, qd.sys.sys_heap, self.port, dest, view or dest, styled)
        self.ticks = 1000

    def __call__(self) -> EditRecord:
        return EditRecord(self.qd.sys, self.qd, self.h, now=lambda: self.ticks)

    @property
    def rec(self) -> te.TERec:
        return te.TERec(self.mem, self.mem.r32(self.h))

    def line_starts(self):
        rec = self.rec
        return list(struct.unpack(f">{rec.n_lines + 1}h",
                                  self.mem.read(rec.addr + te.HEADER, 2 * (rec.n_lines + 1))))

    def text(self):
        rec = self.rec
        return self.mem.read(self.mem.r32(rec.h_text), rec.te_length)

    def selection(self):
        return self.rec.sel_start, self.rec.sel_end

    def click(self, v, h, drag=()):
        """A click at (v, h), held while the mouse moves through drag."""
        path = [Point(*p) for p in drag]
        tracker = Tracker(lambda: bool(path), lambda: path.pop(0), lambda: None,
                          lambda: self.ticks, 32)
        self().click(Point(v, h), False, tracker)

    # -- the bitmap ----------------------------------------------------------
    def rows(self):
        out = []
        for y in range(H):
            row = self.mem.read(self.bits + y * ROW_BYTES, ROW_BYTES)
            out.append("".join("#" if row[x >> 3] & (0x80 >> (x & 7)) else "."
                               for x in range(W)))
        return out

    def black(self, r: Rect) -> int:
        """Black pixels in r."""
        return sum(row[r.left:r.right].count("#") for row in self.rows()[r.top:r.bottom])


def lines(rec, text):
    """The text as the record has broken it."""
    starts = rec.line_starts()
    return [text[a:b] for a, b in zip(starts, starts[1:])]


# -- the record ----------------------------------------------------------------
def test_a_new_record_is_laid_out_as_inside_macintosh_gives_it(qd):
    r = Record(qd, 60)
    rec = r.rec
    ascent, descent, _, leading = qd.font_info(r.port)
    assert rec.line_height == ascent + descent + leading and rec.font_ascent == ascent
    assert rec.dest_rect == rec.view_rect == Rect(2, 2, H - 2, 62)
    assert (rec.sel_start, rec.sel_end, rec.te_length, rec.n_lines) == (0, 0, 0, 0)
    assert r.line_starts() == [0]
    assert (rec.tx_font, rec.tx_size, rec.in_port) == (MONACO, 9, r.port.addr)
    assert qd.sys.sys_heap.handle_size(rec.h_text) == 0
    assert rec.word_break and rec.clik_loop and rec.h_dispatch_rec
    assert not rec.active


# -- lines -----------------------------------------------------------------------
def test_words_wrap_at_the_dest_width(qd, w):
    r = Record(qd, w(b"three four"))
    r().set_text(b"one two three four")
    assert lines(r, b"one two three four") == [b"one two ", b"three four"]
    assert r.rec.n_lines == 2 and r.rec.te_length == 18


def test_returns_end_lines(qd):
    r = Record(qd, 100)
    r().set_text(b"ab\rcd\r")
    assert r.line_starts() == [0, 3, 6]             # no line for after the last return


def test_spaces_hang_past_the_edge(qd, w):
    r = Record(qd, w(b"abcde"))
    r().set_text(b"abc      def")
    assert r.line_starts() == [0, 9, 12]


def test_a_word_longer_than_the_line_breaks_between_characters(qd, w):
    r = Record(qd, w(b"abcd"))
    r().set_text(b"abcdefghij")
    assert r.line_starts() == [0, 4, 8, 10]


def test_cr_only_and_a_new_dest_rect_take_effect_on_tecaltext(qd, w):
    r = Record(qd, w(b"three four"))
    text = b"one two three four\rx"
    r().set_text(text)
    assert lines(r, text) == [b"one two ", b"three four\r", b"x"]
    r.rec.cr_only = -1
    r().cal_text()
    assert lines(r, text) == [b"one two three four\r", b"x"]
    r.rec.cr_only = 0
    r.rec.dest_rect = Rect(2, 2, H - 2, 2 + w(b"one two three"))
    r().cal_text()
    assert lines(r, text) == [b"one two three ", b"four\r", b"x"]


def test_line_starts_grow_the_record(qd):
    r = Record(qd, 60)
    text = b"\r".join(b"%d" % i for i in range(200))
    r().set_text(text)
    assert r.rec.n_lines == 200
    assert lines(r, text + b"\r")[-1] == b"199"
    assert qd.sys.sys_heap.handle_size(r.h) >= te.HEADER + 2 * 201


def test_the_application_may_swap_the_text(qd):
    """hText replaced behind TextEdit's back, then TECalText."""
    r = Record(qd, 100)
    heap = qd.sys.sys_heap
    h = heap.new_handle(9)
    qd.mem.write(heap.deref(h), b"abc\rdef g")
    r.rec.h_text, r.rec.te_length = h, 9
    r().cal_text()
    assert r.text() == b"abc\rdef g" and r.line_starts() == [0, 4, 9]


# -- editing -------------------------------------------------------------------
def test_typing_and_backspace(qd):
    r = Record(qd, 100)
    for c in b"hello":
        r().key(c)
    assert r.text() == b"hello" and r.selection() == (5, 5)
    r().key(BACKSPACE)
    r().key(BACKSPACE)
    assert r.text() == b"hel" and r.selection() == (3, 3) and r.rec.te_length == 3
    r().key(LEFT)
    r().key(ord("X"))
    assert r.text() == b"heXl" and r.selection() == (3, 3)
    r().key(RIGHT)
    r().key(0x0D)
    assert r.text() == b"heXl\r" and r.line_starts() == [0, 5]


def test_typing_replaces_the_selection(qd):
    r = Record(qd, 100)
    r().set_text(b"one two")
    r().select(4, 7)
    r().key(ord("3"))
    assert r.text() == b"one 3" and r.selection() == (5, 5)


def test_arrows_go_up_and_down_a_line(qd):
    r = Record(qd, 100)
    r().set_text(b"abcdef\rabcdef\rab")
    r().select(10, 10)                              # line 1, column 3
    r().key(UP)
    assert r.selection() == (3, 3)
    r().key(DOWN)
    r().key(DOWN)
    assert r.selection() == (16, 16)                # line 2 is shorter: its end
    r().key(LEFT)
    r().key(LEFT)
    r().key(LEFT)
    assert r.selection() == (13, 13)


def test_insert_and_delete(qd):
    r = Record(qd, 100)
    r().set_text(b"abcdef")
    r().select(2, 4)
    r().insert(b"XY")                               # before the selection, which moves along
    assert r.text() == b"abXYcdef" and r.selection() == (4, 6)
    r().delete()
    assert r.text() == b"abXYef" and r.selection() == (4, 4)


# -- the mouse -----------------------------------------------------------------
def test_click_places_the_insertion_point(qd, w):
    r = Record(qd, 100)
    r().set_text(b"abcdef\rghijkl")
    lh = r.rec.line_height
    r.click(2 + lh + 1, 2 + w(b"ghi") + 1)          # line 1, just right of its third character
    assert r.selection() == (10, 10)
    r.click(2 + lh + 1, 90)                         # past the end of the line
    assert r.selection() == (13, 13)
    r.click(2, 0)                                   # left of the first line
    assert r.selection() == (0, 0)


def test_double_click_selects_a_word_and_drag_extends_it(qd, w):
    r = Record(qd, 150)
    r().set_text(b"one two three")
    x = 2 + w(b"one t")
    r.click(4, x)
    r.ticks += 10
    r.click(4, x)
    assert r.selection() == (4, 7)
    r.ticks += 100                                  # too late for a double click
    r.click(4, x)
    assert r.selection() == (5, 5)
    r.click(4, 2 + w(b"o"), drag=[(4, 2 + w(b"one ")), (4, 2 + w(b"one two t"))])
    assert r.selection() == (1, 9)


def test_shift_click_extends_the_selection(qd, w):
    r = Record(qd, 150)
    r().set_text(b"one two three")
    r().select(2, 4)
    tracker = Tracker(lambda: False, lambda: Point(0, 0), lambda: None, lambda: r.ticks, 32)
    r().click(Point(4, 2 + w(b"one two th")), True, tracker)
    assert r.selection() == (2, 10)


# -- drawing -------------------------------------------------------------------
def test_update_draws_the_text_line_by_line(qd, w):
    r = Record(qd, 100)
    r().set_text(b"abc\rdef")
    r().update(Rect(0, 0, H, W))
    lh = r.rec.line_height
    right = 2 + w(b"abc")
    assert r.black(Rect(2, 2, 2 + lh, right)) > 10
    assert r.black(Rect(2 + lh, 2, 2 + 2 * lh, right)) > 10
    assert r.black(Rect(2 + 2 * lh, 0, H, W)) == 0          # nothing below the text
    assert r.black(Rect(0, right + 1, H, W)) == 0           # nor right of it


def test_the_selection_is_inverted_while_active(qd, w):
    r = Record(qd, 100)
    r().set_text(b"abcdef")
    r().update(Rect(0, 0, H, W))
    lh = r.rec.line_height
    sel = Rect(2, 2, 2 + lh, 2 + w(b"abc"))
    plain = r.black(sel)
    r().select(0, 3)
    assert r.black(sel) == plain                    # not active: no highlighting
    r().activate()
    assert r.black(sel) == sel.width * sel.height - plain
    r().deactivate()
    assert r.black(sel) == plain


def test_hiliting_only_inverts_what_is_drawn_there(qd, w):
    """TESetSelect, TEActivate and TEDeactivate invert the hilite or caret
    and leave the text alone: Microsoft BASIC 2 draws its List window's
    listing itself, over an empty record it selects in."""
    r = Record(qd, 100)
    r().set_text(b"abcdef")                         # not drawn: the app draws
    lh = r.rec.line_height
    block = Rect(2, 2, 2 + lh, 2 + w(b"abcdef"))
    qd.region(r.port, "Paint", Region.rect(block))
    full = block.width * block.height
    r().select(0, 3)
    assert r.black(block) == full                   # not active: nothing
    r().activate()
    r().activate()                                  # already active: nothing
    sel = Rect(2, 2, 2 + lh, 2 + w(b"abc"))
    assert r.black(sel) == 0 and r.black(block) == full - sel.width * sel.height
    r().select(3, 3)                                # the range off, the caret on
    assert r.black(block) == full - lh
    r().select(3, 3)                                # the same: still on
    assert r.black(block) == full - lh
    r().deactivate()
    r().deactivate()
    assert r.black(block) == full


def test_activating_sets_active_as_the_rom_does(qd):
    """TEActivate sets the BOOLEAN with ST, all of its byte: Macintosh
    Pascal tests the byte for $FF."""
    r = Record(qd, 100)
    r().activate()
    assert qd.mem.r8(r.rec.addr + 36) == 0xFF
    r().deactivate()
    assert qd.mem.r8(r.rec.addr + 36) == 0


def test_do_text_finds_hilites_draws_and_places_the_caret(qd, w):
    """DoText, which TEDoText points to, for programs that call it
    themselves as Macintosh Pascal's editor does."""
    r = Record(qd, 100)
    r().set_text(b"abcdef")
    lh = r.rec.line_height
    rec = r.rec
    rec.sel_point = Point(4, 2 + w(b"abc") + 1)
    assert r().do_text(te.teFind, 0, 6) == 3
    assert r().do_text(te.teFind, 0, 2) == 2        # no further than the range
    assert r().do_text(te.teFind, 4, 6) == 4

    line = Rect(2, 2, 2 + lh, 2 + w(b"abcdef"))
    r().do_text(te.teDraw, 0, 6)
    drawn = r.black(line)
    assert drawn > 10
    sel = Rect(2, 2 + w(b"ab"), 2 + lh, 2 + w(b"abcd"))
    plain = r.black(sel)
    r().do_text(te.teHilite, 2, 4)                  # whether active or not
    assert r.black(sel) == sel.width * sel.height - plain
    assert r.black(line) == drawn - plain + r.black(sel)
    r().do_text(te.teHilite, 4, 2)                  # either way round: off again
    assert r.black(line) == drawn

    r().do_text(te.teCaret, 3, 3)
    x = 2 + w(b"abc")
    assert (r.rec.sel_rect.top, r.rec.sel_rect.bottom) == (2, 2 + lh)
    assert r.port.pn_loc == Point(2 + r.rec.font_ascent, x)


def test_the_insertion_point_blinks_at_caret_time(qd, w):
    qd.mem.w32(lowmem.CaretTime, 32)
    r = Record(qd, 100)
    r().set_text(b"a ")                             # a space: no ink where the caret goes
    r().activate()
    lh = r.rec.line_height
    x = 2 + w(b"a ")
    caret = Rect(2, x - 1, 2 + lh, x)
    assert r.black(caret) == lh
    r.ticks += 10
    r().idle()
    assert r.black(caret) == lh                     # not yet
    r.ticks += 32
    r().idle()
    assert r.black(caret) == 0 and r.rec.caret_state == 0
    r.ticks += 32
    r().idle()
    assert r.black(caret) == lh


def test_drawing_stays_in_the_view_rect_and_the_clip_region(qd, w):
    view = Rect(2, 2, 14, 2 + w(b"abcd"))
    r = Record(qd, 150, view=view)
    r().set_text(b"abcdefghij\rklmnop")
    r().update(Rect(0, 0, H, W))
    assert r.black(view) > 10
    assert r.black(Rect(0, view.right, H, W)) == 0
    assert r.black(Rect(view.bottom, 0, H, W)) == 0
    r2 = Record(qd, 150)
    edge = 2 + w(b"ab")
    qdraw.write_rgn(qd.sys.sys_heap, r2.port.clip_rgn, Region.rect(Rect(0, 0, H, edge)))
    r2().set_text(b"abcdefghij")
    r2().update(Rect(0, 0, H, W))
    assert r2.black(Rect(0, edge, H, W)) == 0 and r2.black(Rect(0, 0, H, W)) > 5


def test_typing_redraws_what_changed(qd, w):
    r = Record(qd, 100)
    r().activate()
    for c in b"abc":
        r().key(c)
    lh = r.rec.line_height
    typed = Rect(2, 2, 2 + lh, 2 + w(b"abc") - 1)      # all but the caret
    r2 = Record(qd, 100)
    r2().set_text(b"abc")
    r2().update(Rect(0, 0, H, W))
    assert r.black(typed) == r2.black(typed) > 10
    r().key(BACKSPACE)
    r().key(BACKSPACE)
    r().key(BACKSPACE)
    assert r.black(Rect(0, 3, H, W)) == 0           # all gone but the caret


def test_scrolling_moves_the_dest_rect_and_the_bits(qd):
    r = Record(qd, 100)
    r().set_text(b"abc\rdef\rghi")
    r().update(Rect(0, 0, H, W))
    lh = r.rec.line_height
    second = r.rows()[2 + lh:2 + 2 * lh]
    r().scroll(0, -lh)
    assert r.rec.dest_rect.top == 2 - lh
    assert r.rows()[2:2 + lh] == second


# -- styled text -----------------------------------------------------------------
def test_a_styled_record_keeps_its_runs_and_style_table(qd):
    r = Record(qd, 150, styled=True)
    rec = r.rec
    assert rec.tx_size == -1 and rec.line_height == -1
    r().set_text(b"plain bold plain")
    r().select(6, 10)
    r().set_style(doFace, Style(0, bold, 0), True)
    sr = te.TEStyleRec(qd.mem, qd.mem.r32(r.rec.style_handle))
    assert (sr.n_runs, sr.n_styles) == (3, 2)
    runs = struct.unpack(">8h", qd.mem.read(sr.addr + te.STYLE_REC, 16))
    assert runs == (0, 0, 6, 1, 10, 0, 17, -1)
    tab = qd.mem.r32(sr.style_tab)                  # plain twice, bold once
    assert qd.mem.r16(tab) == 2 and qd.mem.r16(tab + te.ST_ELEMENT) == 1
    assert qd.mem.r8(tab + 8) == 0 and qd.mem.r8(tab + te.ST_ELEMENT + 8) == bold
    style, height, ascent = r().get_style(7)
    assert style.face == bold and style.font == MONACO and height > ascent > 0
    ok, mode, cont = r().continuous_style(doFace | doSize)
    assert ok and cont.face == bold
    scrap = r().style_scrap(4, 12)
    assert te.read_style_scrap(scrap) == [(0, Style(MONACO, 0, 9)), (2, Style(MONACO, bold, 9)),
                                          (6, Style(MONACO, 0, 9))]


def test_typing_takes_the_style_before_it_or_the_null_style(qd):
    r = Record(qd, 150, styled=True)
    r().set_text(b"ab")
    r().select(2, 2)
    r().set_style(doSize, Style(0, 0, 18), False)   # an insertion point: the null style
    r().key(ord("c"))
    r().key(ord("d"))
    assert r().get_style(3)[0].size == 18 and r().get_style(0)[0].size == 9
    assert r().num_styles(0, 4) == 2


# -- TETextBox -------------------------------------------------------------------
def test_text_box_draws_wrapped_and_justified(qd, w):
    r = Record(qd, 100)
    box = Rect(2, 2, H - 2, 102)
    te.text_box(qd, r.port, b"abc def", box, te.teJustRight)
    left = box.right - w(b"abc def")
    assert r.black(Rect(0, 0, H, left - 1)) == 0
    assert r.black(Rect(0, left, H, box.right)) > 10


def test_text_box_draws_chicagos_symbols_but_not_other_controls(qd):
    """A dialog's "hold down $11 and type a period" shows the ⌘."""
    r = Record(qd, 100)
    box = Rect(2, 2, H - 2, 102)
    te.text_box(qd, r.port, b"\x01\x09\x1b", box, 0)
    assert r.black(box) == 0
    te.text_box(qd, r.port, b"\x11", box, 0)
    assert r.black(box) > 5


# -- From 68k code ----------------------------------------------------------------
def test_an_offset_past_either_end_of_the_text_is_its_end(qapp):
    """The ROM pins TESetSelect's offsets to teLength comparing unsigned,
    so -1 is the end: Enchanted Scepters sets the selection to -1, -1
    before each key it types."""
    from guest import Guest
    g = Guest()
    g.port(W, H)
    h = g("TENew", g.rect_ptr((0, 0, H, W)), g.rect_ptr((0, 0, H, W)))
    g("TESetText", g.ptr(b"look"), 4, h)
    for ch in b"ed":
        g("TESetSelect", -1, -1, h)
        g("TEKey", ch, h)
    assert g.contents(g("TEGetText", h)) == b"looked"
    g("TESetSelect", 0, -1, h)
    g("TEKey", ord("x"), h)
    assert g.contents(g("TEGetText", h)) == b"x"


def test_te_do_text_is_called_with_the_record_in_a3(qapp):
    """The routine at TEDoText takes the locked record in A3, the
    characters in D3 and D4 and what to do in D7, and returns the port in
    A0 and, finding, the offset in D0."""
    from guest import Processor
    from macrt.m68k import glue_textedit
    g = Processor()
    port, _ = g.port(W, H)
    h = g("TENew", g.rect_ptr((0, 0, H, W)), g.rect_ptr((0, 0, H, W)))
    g("TESetText", g.ptr(b"abcdef"), 6, h)
    rec = te.TERec(g.mem, g.mem.r32(h))
    rec.sel_point = Point(4, 1000)                  # past the end of the line
    g.cpu.push32(0x1234)
    g.cpu.set_a(3, g.mem.r32(h) | 0x80000000)       # flags in the high byte
    g.cpu.set_d(3, 0xFFFF0001)                      # only the low words count
    g.cpu.set_d(4, 5)
    g.cpu.set_d(7, te.teFind & 0xFFFF)
    glue_textedit.do_text(g)
    assert g.cpu.pc == 0x1234 and g.cpu.sp == g.stack_top
    assert (g.cpu.d(0), g.cpu.a(0)) == (5, port.addr)
    g.cpu.push32(0x1234)
    g.cpu.set_d(7, te.teCaret & 0xFFFF)
    g.cpu.set_d(0, 99)
    glue_textedit.do_text(g)
    assert g.cpu.d(0) == 99 and rec.sel_rect.top == 0
