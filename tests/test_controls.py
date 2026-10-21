"""The Control Manager, driven by Controls (tests/apps/controls.s): a 68k
application with a push button, a check box, radio buttons and a scroll
bar, clicked with synthetic input."""

from pathlib import Path
from types import SimpleNamespace

import pytest
from conftest import Recorder
from guest import Guest, Processor

from macrt.m68k.glue_controls import TrackControl, _test, controls_of, message
from macrt.macfile import open_application
from macrt.toolbox import constants as k
from macrt.toolbox.controls import TITLE, Control, Geometry
from macrt.toolbox.quickdraw import new_rgn
from macrt.toolbox.types import Point, Rect

APPS = Path(__file__).parent / "apps"
BUTTON, CHECK, RADIO1, RADIO2, SCROLL = -238, -242, -246, -250, -254
CLICKS, ACTIONS = -256, -258


class App:
    def __init__(self, h):
        f = open_application(APPS / "Controls.bin")
        h.launch(Recorder("Other"))
        self.h = h
        self.proc = h.d.launch(f.name, None, app=f)
        h.wait_until(lambda: self.proc.windows and self.proc.windows[0].visible, timeout=10,
                     what="the window")
        self.window = self.proc.windows[0]
        self.mem = self.proc.system.mem
        self.a5 = self.proc.partition.a5
        # The scroll bar is made last, then all of them are drawn again when
        # the window's updated, the scroll bar first and the button last.
        h.wait_until(lambda: self.mem.r32(self.a5 + SCROLL) and all(
            self.black(*at) for at in ((215, 80), (55, 20), (20, 30), (22, 54))),
            what="the controls")
        h.settle()

    def word(self, offset):
        return self.mem.r16s(self.a5 + offset)

    def control(self, offset):
        return Control(self.mem, self.mem.r32(self.a5 + offset))

    def black(self, h, v):
        with self.window.backing.lock:
            return self.window.backing.image.pixel(h, v) & 0xFFFFFF == 0

    def click(self, h, v):
        """A click at (h, v), local to the window."""
        c = self.window.content
        self.h.click(c.left + h, c.top + v)


@pytest.fixture
def app(harness):
    return App(harness())


def test_the_controls_are_made_and_drawn(app):
    w = app.window
    names = [c.title for c in controls_of(_t(app), w)]
    assert names == ["", "Two", "One", "Check me", "Beep"]      # newest first
    scroll = app.control(SCROLL)
    assert (scroll.rect, scroll.max, scroll.value) == ((10, 200, 150, 216), 10, 0)
    assert app.black(55, 20) and app.black(20, 30)          # the button's frame
    assert app.black(22, 54)                                # the check box's
    assert app.black(200, 80) and app.black(215, 80)        # the scroll bar's sides
    thumb = Geometry(scroll).thumb
    assert thumb == (25, 200, 41, 216)                      # at the top, under the arrow
    assert not app.black(208, 33)                           # white
    assert app.black(207, 60) != app.black(208, 60)         # the gray shaft
    assert app.proc.runner.unimplemented_calls == {}


def test_the_button_counts_clicks(app):
    app.click(55, 30)
    app.h.wait_until(lambda: app.word(CLICKS) == 1, what="the click")
    app.h.settle()
    assert not app.black(55, 26)                            # not left highlighted
    app.click(5, 5)                                         # not in a control
    app.click(55, 30)
    app.h.wait_until(lambda: app.word(CLICKS) == 2, what="another")


def test_the_check_box_toggles(app):
    check = app.control(CHECK)
    app.click(30, 59)
    app.h.wait_until(lambda: check.value == 1, what="it checked")
    app.h.wait_until(lambda: app.black(25, 56), what="the X")
    app.click(80, 59)                                       # on its title
    app.h.wait_until(lambda: check.value == 0, what="it unchecked")
    app.h.wait_until(lambda: not app.black(25, 56), what="no X")


def test_radio_buttons_take_turns(app):
    one, two = app.control(RADIO1), app.control(RADIO2)
    app.click(30, 104)
    app.h.wait_until(lambda: (one.value, two.value) == (0, 1), what="Two on")
    app.click(30, 84)
    app.h.wait_until(lambda: (one.value, two.value) == (1, 0), what="One on")


def test_the_scroll_bar_scrolls(app):
    scroll = app.control(SCROLL)
    app.click(208, 145)                                     # the down arrow
    app.h.wait_until(lambda: scroll.value >= 1, what="a line down")
    assert app.word(ACTIONS) >= 1
    lines = scroll.value
    app.click(208, 120)                                     # below the thumb
    app.h.wait_until(lambda: scroll.value >= lines + 5, what="a page down")
    value = scroll.value
    app.click(208, 15)                                      # the up arrow
    app.h.wait_until(lambda: scroll.value < value, what="a line up")
    thumb = Geometry(scroll).thumb
    assert thumb.top > 25
    app.h.wait_until(lambda: app.black(208, thumb.top) and
                     not any(app.black(h, thumb.top + 8) for h in (207, 208)),
                     what="the thumb drawn there")


def test_dragging_the_thumb(app):
    scroll = app.control(SCROLL)
    c = app.window.content
    h = app.h
    h.press(c.left + 208, c.top + 30)
    h.settle(100)
    for v in range(35, 100, 8):
        h.move(c.left + 208, c.top + v)
        h.settle(30)
    h.release(c.left + 208, c.top + 100)
    h.wait_until(lambda: scroll.value > 0, what="the thumb dragged")
    # 70 of the 94 pixels it can travel: 7 of 10.
    assert scroll.value == 7
    assert app.word(ACTIONS) == 0


def test_an_inactive_control_ignores_clicks(app):
    t = _t(app)
    button = app.control(BUTTON)
    with t.system.lock:
        button.hilite = k.inactiveHilite
        assert _test(t, button, Point(30, 55)) == 0
        button.hilite = k.noHilite
        assert _test(t, button, Point(30, 55)) == k.inButton


def test_tracking_from_a_global_point_tracks_where_the_mouse_is(app):
    """MacLanding hands TrackControl the mouse-down's point as it came, in
    global coordinates."""
    t = _t(app)
    c = app.window.content
    where = Point(c.top + 30, c.left + 55)                  # the button, globally
    button = app.mem.r32(app.a5 + BUTTON)
    with t.system.lock:
        assert _test(t, app.control(BUTTON), where) == 0
        app.h.d.input.mac_pos = where
        assert TrackControl(t, button, where, 0) == k.inButton
        app.h.d.input.mac_pos = Point(c.top + 5, c.left + 5)
        assert TrackControl(t, button, where, 0) == 0


def _t(app):
    return app.proc.runner


def _control(g, def_proc):
    c = Control(g.mem, g.heap.new_handle(TITLE + 1, clear=True))
    c.rect, c.vis, c.def_proc = Rect(0, 0, 20, 60), 255, def_proc
    return c


def test_a_definition_the_application_puts_in_the_record_is_called(qapp):
    """Excel makes its dialogs' controls with procID 0, then puts a handle
    to code of its own, which is no resource, in contrlDefProc."""
    g = Guest()
    inside = 5 << 16 | 10                                   # (v 5, h 10)
    button = _control(g, g.system.defproc("CDEF", 0))
    assert message(g, button, k.testCntl, inside) == k.inButton
    assert g.calls == []
    code = g.handle(bytes.fromhex("4EF9 0010 0000"))        # JMP $100000
    own = _control(g, 3 << 24 | code)
    message(g, own, k.testCntl, inside)
    assert g.calls == [(g.heap.deref(code),
                        (("h", 3), ("p", own.handle), ("h", k.testCntl), ("l", inside)))]


def test_68k_code_can_call_cdef_0_itself(qapp):
    """Excel's own CDEF passes the messages it doesn't answer to CDEF 0,
    calling its code with the arguments on the stack."""
    m = Processor()
    entry = m.system.sys_heap.deref(m.system.defproc("CDEF", 0))
    button = _control(m, 3 << 24 | m.handle(bytes.fromhex("4E75")))
    m.run(bytes.fromhex("42A7"                                      # CLR.L -(SP)
                        "3F3C0000"                                  # varCode
                        f"2F3C{button.handle:08X}"
                        f"3F3C{k.testCntl:04X}"
                        "2F3C0005000A"                              # (v 5, h 10)
                        f"207C{entry:08X}4E90"                      # JSR the code
                        "201F"))                                    # MOVE.L (SP)+,D0
    assert m.cpu.d(0) == k.inButton and m.cpu.sp == m.stack_top


class Mouse:
    """The display's input, moved along a path a tick at a time; the
    button comes up at its end."""

    def __init__(self, path):
        self.path = list(path)
        self.mac_pos, self.down = self.path.pop(0), True

    def tick(self, ticks=1):
        if self.path:
            self.mac_pos = self.path.pop(0)
        else:
            self.down = False


def _dragging(g, path):
    mouse = Mouse(path)
    g.tb._d = SimpleNamespace(input=mouse)
    g.sleep = mouse.tick
    return mouse


def test_drag_gray_rgn_follows_the_mouse_with_the_regions_outline(qapp):
    """Sargon III drags its pieces with DragTheRgn: the outline follows the
    mouse, the actionProc is called all the while, and the distance comes
    back with the vertical in the high word."""
    g = Guest()
    port, _ = g.port(64, 32)
    rgn = new_rgn(g.heap, Rect(5, 10, 15, 20))
    _dragging(g, [Point(10, 15), Point(12, 20), Point(13, 25)])
    seen = []
    g.procs[0x123456] = lambda: seen.append(g.rows(port))
    limit, slop = g.rect_ptr((0, 0, 32, 64)), g.rect_ptr((0, 0, 32, 64))
    moved = g("DragGrayRgn", rgn, Point(10, 15), limit, slop, k.noConstraint, 0x123456)
    assert moved == 3 << 16 | 10
    last = seen[-1]
    assert any("#" in row[20:30] for row in last[8:18])         # the outline, moved
    assert all("#" not in row[:20] for row in last)             # and only there
    assert all("#" not in row for row in g.rows(port))          # and gone again


def test_drag_gray_rgn_released_outside_the_slop(qapp):
    g = Guest()
    g.port(64, 32)
    rgn = new_rgn(g.heap, Rect(5, 10, 15, 20))
    _dragging(g, [Point(10, 15), Point(30, 60)])
    limit, slop = g.rect_ptr((0, 0, 32, 64)), g.rect_ptr((0, 0, 20, 40))
    assert g("DragTheRgn", rgn, Point(10, 15), limit, slop, k.noConstraint, 0) == 0x80008000
