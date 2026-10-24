"""Guest apps driven through real (synthetic) mouse and key input."""

import sys
import threading
import time

import pytest
from PyQt6.QtCore import QPoint, Qt
from PyQt6.QtGui import QColor
from PyQt6.QtWidgets import QApplication

from macrt.toolbox import constants as k
from macrt.toolbox import lowmem, qdraw
from macrt.toolbox.types import Point, Rect
from macrt.ui import chrome
from macrt.ui import display as display_module
from macrt.ui.menubar import MenuTracker

from conftest import Recorder

MENUBARS = ["global", "window"]


def bar_for(h, proc):
    return h.d.default_bar(proc)


# -- the window frame --------------------------------------------------------
@pytest.mark.parametrize("menu_bar", MENUBARS)
def test_close_box_disposes_window(harness, menu_bar):
    h = harness(menu_bar=menu_bar)
    app = Recorder("App")
    proc = h.launch(app)
    rec = proc.windows[0]
    h.click(*h.box_point(rec, k.wInGoAway))
    h.wait_until(lambda: not proc.windows, what="window disposed")
    assert app.last("TrackGoAway") == ("TrackGoAway", True)
    assert rec not in h.d.views


def test_close_box_released_outside_does_nothing(harness):
    h = harness()
    app = Recorder("App")
    proc = h.launch(app)
    rec = proc.windows[0]
    bh, bv = h.box_point(rec, k.wInGoAway)
    h.press(bh, bv)
    h.wait_until(lambda: h.d.views[rec].pressed == k.wInGoAway, what="box drawn pressed")
    h.move(bh + 60, bv + 60)
    h.wait_until(lambda: h.d.views[rec].pressed == k.wNoHit, what="box released")
    h.release(bh + 60, bv + 60)
    h.wait_until(lambda: app.last("TrackGoAway") is not None)
    assert app.last("TrackGoAway") == ("TrackGoAway", False)
    assert list(proc.windows) == [rec]


@pytest.mark.parametrize("menu_bar", MENUBARS)
def test_drag_window_by_title(harness, menu_bar):
    h = harness(menu_bar=menu_bar)
    app = Recorder("App")
    proc = h.launch(app)
    rec = proc.windows[0]
    before = rec.content
    th, tv = h.title_point(rec)
    h.press(th, tv)
    h.wait_until(lambda: h.d.tracker is not None, what="the drag")
    h.move(th + 50, tv + 30)
    h.move(th + 100, tv + 60)
    h.release(th + 100, tv + 60)
    h.wait_until(lambda: app.last("DragWindow") is not None)
    assert rec.content == before.offset(100, 60)
    assert h.d.views[rec].structure_origin() == h.d.views[rec].mac_origin()


def test_grow_window(harness):
    h = harness()
    app = Recorder("App")
    proc = h.launch(app)
    rec = proc.windows[0]
    w0, h0 = rec.content.width, rec.content.height
    gh, gv = h.grow_point(rec)
    h.press(gh, gv)
    h.wait_until(lambda: h.d.tracker is not None, what="the grow")
    h.move(gh + 70, gv + 40)
    h.release(gh + 70, gv + 40)
    h.wait_until(lambda: rec.content.width == w0 + 70, what="SizeWindow")
    assert rec.content.height == h0 + 40
    assert app.last("GrowWindow")[1] == ((h0 + 40) << 16) | (w0 + 70)
    assert h.d.views[rec].preview_size is None


@pytest.mark.parametrize("menu_bar", MENUBARS)
def test_zoom_box_toggles_between_states(harness, menu_bar):
    h = harness(menu_bar=menu_bar)
    app = Recorder("App")
    proc = h.launch(app)
    rec = proc.windows[0]
    user = rec.content
    h.click(*h.box_point(rec, k.wInZoomOut))
    h.wait_until(lambda: rec.zoomed_out, what="zoom out")
    assert rec.content == h.d.standard_state(rec)
    assert h.d.views[rec].structure_origin().v >= h.d.desk_top
    h.click(*h.box_point(rec, k.wInZoomIn))
    h.wait_until(lambda: not rec.zoomed_out, what="zoom in")
    assert rec.content == user


def test_inactive_window_click_selects_it(harness):
    h = harness()
    app = Recorder("App", windows=2)
    proc = h.launch(app)
    back = proc.windows[1]
    h.click(back.content.left + 5, back.content.top + 5)    # corner not covered by the front one
    h.wait_until(lambda: proc.windows[0] is back, what="SelectWindow")
    assert back.hilited and not proc.windows[1].hilited


def test_selecting_a_window_highlighted_behind_another_activates_it(harness):
    """Multiplan brings its formula bar's windows in front of the sheet,
    which stays highlighted, and selects the sheet when it's clicked in."""
    h = harness()
    app = Recorder("App", windows=2)
    proc = h.launch(app)
    front, back = proc.windows
    app.tb.BringToFront(back)               # in front, and not highlighted
    assert list(proc.windows) == [back, front] and front.hilited and not back.hilited
    seen = len(app.events)
    app.tb.SelectWindow(front)
    assert list(proc.windows) == [front, back] and front.hilited and not back.hilited
    h.wait_until(lambda: (k.activateEvt, front) in app.events[seen:], what="activate")
    assert app.events[seen:].index((k.activateEvt, back)) < app.events[seen:].index(
        (k.activateEvt, front))


def test_the_ghost_window_is_never_the_front_one(harness):
    """Multiplan points GhostWindow at its formula bar's window, which it
    keeps in front of the sheet, and selects the sheet when it's clicked
    in if FrontWindow isn't the sheet: the click is lost if it isn't."""
    h = harness()
    app = Recorder("App", windows=2)
    proc = h.launch(app)
    front, back = proc.windows
    proc.world.w32(lowmem.GhostWindow, front.addr)
    try:
        assert app.tb.FrontWindow() is back
    finally:
        proc.world.w32(lowmem.GhostWindow, 0)
    assert app.tb.FrontWindow() is front


def test_send_behind_activates_the_next_window(harness):
    h = harness()
    app = Recorder("App", windows=3)
    proc = h.launch(app)
    front, middle, back = proc.windows
    app.tb.SendBehind(back, front)          # not the front one: nothing activates
    assert list(proc.windows) == [front, back, middle] and front.hilited
    app.tb.SendBehind(front, None)
    assert list(proc.windows) == [back, middle, front]
    assert back.hilited and not front.hilited
    h.wait_until(lambda: (k.activateEvt, back) in app.events, what="activate")


def test_a_window_wholly_covered_is_updated_when_its_uncovered(harness):
    # King's Quest makes its status window behind its full-screen picture,
    # and shows it when it gets an update.
    h = harness()
    app = Recorder("App", windows=1)
    proc = h.launch(app)
    front = proc.windows[0]
    back = app.tb.NewWindow(front.content.inset(40, 40), "Behind", True, k.plainDBox,
                            behind=None, go_away=False)
    assert list(proc.windows) == [front, back] and back.update_pending
    seen = len(app.events)
    with proc.system.lock:
        front.invalidate(front.content)
    proc.events.wake()
    h.wait_until(lambda: (k.updateEvt, front) in app.events[seen:], what="the front's update")
    assert (k.updateEvt, back) not in app.events and back.update_pending
    app.tb.HideWindow(front)
    h.wait_until(lambda: (k.updateEvt, back) in app.events, what="the uncovered window's update")


def test_show_hide_leaves_hiliting_and_activation_alone(harness):
    h = harness()
    app = Recorder("App", windows=2)
    proc = h.launch(app)
    front, back = proc.windows
    seen = len(app.events)
    app.tb.ShowHide(front, False)
    assert not front.visible and front.hilited and not back.hilited
    app.tb.ShowHide(front, True)
    assert front.visible and list(proc.windows) == [front, back]
    h.wait_until(lambda: (k.updateEvt, front) in app.events[seen:], what="update")
    assert all(what != k.activateEvt for what, _ in app.events[seen:])


def test_a_window_shown_again_has_its_content_erased(harness):
    # SimCity hides its scenario's story window, then shows it again and
    # draws its copy protection's question in it without erasing.
    h = harness()
    app = Recorder("App", windows=1)
    proc = h.launch(app)
    w = proc.windows[0]
    white = QColor("#ffffff")
    for hide, show in ((app.tb.HideWindow, app.tb.ShowWindow),
                       (lambda w: app.tb.ShowHide(w, False), lambda w: app.tb.ShowHide(w, True))):
        w.backing.clear()
        with w.backing.lock:
            w.backing.image.fill(QColor("#000000"))
        show(w)                                     # already shown: kept
        assert w.backing.image.pixelColor(10, 10) != white
        hide(w)
        show(w)
        assert w.visible and w.backing.image.pixelColor(10, 10) == white


def test_a_window_whose_storage_has_flags_in_the_high_byte(harness):
    # SuperPaint gives NewWindow a locked handle's master pointer, lock bit
    # and all, and calls ShowHide with it.
    h = harness()
    app = Recorder("App", windows=1)
    proc = h.launch(app)
    first = proc.windows[0]
    storage = proc.heap.new_ptr(156) | 0x80000000
    w = app.tb.NewWindow(Rect.from_xywh(40, 60, 200, 100), "Flagged", False, storage=storage)
    for p in (storage, storage & 0xFFFFFF):
        assert proc.system.port_at(p) is w
    assert list(proc.windows) == [w, first]
    app.tb.ShowHide(w, True)
    assert w.visible
    app.tb.DisposeWindow(w)
    assert list(proc.windows) == [first] and proc.system.port_at(storage) is None


# -- menus -------------------------------------------------------------------
@pytest.mark.parametrize("menu_bar", MENUBARS)
def test_menu_press_drag_release(harness, menu_bar):
    h = harness(menu_bar=menu_bar)
    app = Recorder("App")
    proc = h.launch(app)
    bar = bar_for(h, proc)
    h.choose(bar, "File", 1)                      # New
    h.wait_until(lambda: len(proc.windows) == 2, what="File > New")
    assert app.last("MenuSelect") == ("MenuSelect", (Recorder.FILE << 16) | 1)
    h.wait_until(lambda: bar.hilite_id == 0, what="HiliteMenu(0)")


def test_sticky_menu_stays_open_after_a_click(harness):
    h = harness()
    app = Recorder("App")
    proc = h.launch(app)
    bar = bar_for(h, proc)
    h.click(*h.menu_title_point(bar, "File"))
    h.wait_until(lambda: QApplication.activePopupWidget() is not None, what="pulldown")
    h.settle(100)
    assert isinstance(h.d.tracker, MenuTracker) and h.d.tracker.sticky
    h.click(*h.item_point(2))                     # Close
    h.wait_until(lambda: not proc.windows, what="File > Close")
    assert app.last("MenuSelect")[1] == (Recorder.FILE << 16) | 2


def test_classic_menu_closes_on_release(harness):
    h = harness(sticky_menus=False)
    app = Recorder("App")
    proc = h.launch(app)
    h.click(*h.menu_title_point(bar_for(h, proc), "File"))
    h.wait_until(lambda: app.last("MenuSelect") is not None)
    assert app.last("MenuSelect") == ("MenuSelect", 0)
    assert QApplication.activePopupWidget() is None


def test_disabled_item_returns_zero(harness):
    h = harness()
    app = Recorder("App")
    proc = h.launch(app)
    h.choose(bar_for(h, proc), "Edit", 1)         # (Undo is disabled
    h.wait_until(lambda: app.last("MenuSelect") is not None)
    assert app.last("MenuSelect") == ("MenuSelect", 0)


def _popup(h, top=80, left=80, item=1):
    app = Recorder("Popper", popup=True)
    proc = h.launch(app)
    app.popup_args = (top, left, item)
    w = proc.windows[0]
    h.press(w.content.left + 30, w.content.top + 30)
    h.wait_until(lambda: QApplication.activePopupWidget() is not None, what="popup")
    return app, proc


def test_popup_drag_release_selects_item(harness):
    h = harness()
    app, proc = _popup(h)
    h.move(*h.item_point(2))                     # Two
    h.release(*h.item_point(2))
    h.wait_until(lambda: app.last("PopUpMenuSelect") is not None)
    assert app.last("PopUpMenuSelect") == ("PopUpMenuSelect",
                                           (Recorder.POPUP << 16) | 2)


def test_popup_off_the_bar_still_reports_the_item(harness):
    h = harness()
    app, proc = _popup(h)                        # nothing under the mouse
    h.release(*h.item_point(1))
    h.wait_until(lambda: app.last("PopUpMenuSelect") is not None)
    assert app.last("PopUpMenuSelect") == ("PopUpMenuSelect",
                                           (Recorder.POPUP << 16) | 1)


def test_popup_released_outside_returns_zero(harness):
    h = harness()
    app, proc = _popup(h)
    h.release(10, 400)                           # off the menu
    h.wait_until(lambda: app.last("PopUpMenuSelect") is not None)
    assert app.last("PopUpMenuSelect") == ("PopUpMenuSelect", 0)
    assert QApplication.activePopupWidget() is None


def test_popup_positions_item_top_at_top(harness):
    h = harness()
    app, proc = _popup(h, top=120, left=150, item=2)
    pop = QApplication.activePopupWidget()
    h.wait_until(lambda: pop.hilite == 2)
    top, _ = h.mac(pop, pop.item_rect(2).topLeft())
    assert abs(top - 120) <= 2


def test_command_key_goes_through_menu_key(harness):
    h = harness()
    app = Recorder("App")
    proc = h.launch(app)
    h.key(Qt.Key.Key_N, Qt.KeyboardModifier.ControlModifier)
    h.wait_until(lambda: len(proc.windows) == 2, what="Cmd-N")
    assert app.last("MenuSelect") == ("MenuSelect", (Recorder.FILE << 16) | 1)


# -- more than one app -------------------------------------------------------
def test_global_bar_follows_front_app(harness):
    h = harness(menu_bar="global")
    a, b = Recorder("Alpha", at=(30, 60)), Recorder("Beta", at=(420, 260))
    pa, pb = h.launch(a), h.launch(b)
    assert h.d.global_bar.process is pb
    wa = pa.windows[0]
    h.click(wa.content.left + 10, wa.content.top + 10)
    h.wait_until(lambda: h.d.front_process is pa, what="switch to Alpha")
    assert h.d.global_bar.process is pa
    assert wa.hilited and not pb.windows[0].hilited
    h.wait_until(lambda: (k.osEvt, (k.suspendResumeMessage << 24)) in b.events
                 and (k.osEvt, (k.suspendResumeMessage << 24) | k.resumeFlag) in a.events,
                 what="suspend/resume events")
    # The switching click is not passed on as a mouseDown.
    assert not any(e[0] == "FindWindow" for e in a.log)


def test_application_menu(harness):
    h = harness(menu_bar="global")
    pa = h.launch(Recorder("Alpha", at=(30, 60)))
    bar = h.d.global_bar
    assert h.d.application_menu_for(bar) is None          # one app: no menu ("auto")
    pb = h.launch(Recorder("Beta", at=(420, 260)))
    menu = h.d.application_menu_for(bar)
    assert [i.text for i in menu.items] == ["Hide Beta", "Hide Others", "Show All", "-",
                                            "Alpha", "Beta"]
    assert menu.items[5].mark == k.checkMark
    # Pick "Alpha" from it.
    menu_cell = bar.cells()[-1][1]
    h.press(*h.mac(bar, menu_cell.center()))
    h.wait_until(lambda: QApplication.activePopupWidget() is not None, what="pulldown")
    ih, iv = h.item_point(5)
    h.move(ih, iv)
    h.release(ih, iv)
    h.wait_until(lambda: h.d.front_process is pa, what="switch via Application menu")
    # Hide Others hides Beta's window.
    h.press(*h.mac(bar, bar.cells()[-1][1].center()))
    h.wait_until(lambda: QApplication.activePopupWidget() is not None, what="pulldown")
    ih, iv = h.item_point(2)
    h.move(ih, iv)
    h.release(ih, iv)
    h.wait_until(lambda: pb.hidden, what="Hide Others")
    assert not h.d.views[pb.windows[0]].isVisible()


def test_window_bars_belong_to_their_app(harness):
    h = harness(menu_bar="window")
    a, b = Recorder("Alpha", at=(30, 60)), Recorder("Beta", at=(420, 260))
    pa, pb = h.launch(a), h.launch(b)
    bar_a = h.d.views[pa.windows[0]].bar
    assert bar_a is not None and bar_a.process is pa
    assert h.d.global_bar is None
    # Clicking Alpha's menu bar while Beta is in front only switches apps.
    h.click(*h.menu_title_point(bar_a, "File"))
    h.wait_until(lambda: h.d.front_process is pa, what="switch")
    assert a.last("MenuSelect") is None
    h.choose(bar_a, "File", 1)
    h.wait_until(lambda: len(pa.windows) == 2, what="Alpha File > New")
    assert len(pb.windows) == 1


def test_window_mode_dialog_in_front_still_has_a_bar(harness):
    h = harness(menu_bar="window")
    app = Recorder("App")
    proc = h.launch(app)
    doc_bar = h.d.views[proc.windows[0]].bar
    app.tb.NewWindow(app.tb.ScreenBounds().inset(250, 200), "", True, k.dBoxProc, go_away=False)
    h.wait_until(lambda: len(proc.windows) == 2)
    assert h.d.views[proc.windows[0]].bar is None
    assert h.d.default_bar(proc) is doc_bar


def test_window_mode_bar_only_window_when_no_windows(harness):
    h = harness(menu_bar="window")
    app = Recorder("App")
    proc = h.launch(app)
    h.key(Qt.Key.Key_W, Qt.KeyboardModifier.ControlModifier)
    h.wait_until(lambda: proc in h.d.app_bars, what="bar-only window")
    appbar = h.d.app_bars[proc]
    assert appbar.isVisible()
    h.choose(appbar.bar, "File", 1)
    h.wait_until(lambda: proc.windows and proc not in h.d.app_bars, what="window replaces it")


def test_quit_removes_app_and_its_windows(harness):
    h = harness(menu_bar="global")
    pa = h.launch(Recorder("Alpha", at=(30, 60)))
    pb = h.launch(Recorder("Beta", at=(420, 260)))
    h.key(Qt.Key.Key_Q, Qt.KeyboardModifier.ControlModifier)
    h.wait_until(lambda: pb not in h.d.processes, what="Beta quit")
    assert h.d.front_process is pa
    assert h.d.global_bar.process is pa
    assert all(rec.process is pa for rec in h.d.views)
    assert h.d.system.port_at(pb.partition.a5 - 4) is None
    assert h.d.system._room_for(1 << 20) == pb.partition.start   # its memory is free again


def test_a_click_on_a_quit_apps_window_goes_nowhere(harness):
    """A press can still reach the window of an app that has just quit,
    before the window is gone: it brings nothing forward and goes to no one."""
    h = harness(menu_bar="global")
    alpha = Recorder("Alpha", at=(30, 60))
    pa = h.launch(alpha)
    pb = h.launch(Recorder("Beta", at=(420, 260)))
    view = next(v for rec, v in h.d.views.items() if rec.process is pb)
    h.key(Qt.Key.Key_Q, Qt.KeyboardModifier.ControlModifier)
    h.wait_until(lambda: pb not in h.d.processes, what="Beta quit")
    h.settle()
    seen = len(alpha.events)
    h.d.mouse_down(view)
    h.d.mouse_up()
    h.settle()
    assert h.d.front_process is pa
    assert not [e for e in alpha.events[seen:] if e[0] in (k.mouseDown, k.mouseUp)]


# -- guest memory ------------------------------------------------------------
def test_windows_and_menus_are_in_guest_memory(harness):
    h = harness()
    app = Recorder("App")
    proc = h.launch(app)
    rec = proc.windows[0]
    mem = h.d.system.mem
    assert proc.world.r32(lowmem.WindowList) == rec.addr
    assert mem.r_pstr(mem.r32(mem.r32(rec.addr + 134))) == b"App 1"
    assert [m.title for m in proc.menus] == ["File", "Edit"]
    view = h.d.views[rec]
    o = view.mac_origin()
    assert rec.struc_rect.top_left == Point(o.v, o.h)            # the frame we drew


def test_view_follows_a_poked_record(harness):
    """Guest memory is the source of truth: change the record the way 68k
    code would, and the widget follows on the next sync."""
    h = harness()
    proc = h.launch(Recorder("App"))
    rec = proc.windows[0]
    before = rec.content
    mem = h.d.system.mem
    mem.w_rect(rec.addr + 8, mem.r_rect(rec.addr + 8).offset(-30, -20))   # portBits.bounds
    mem.w_pstr(mem.r32(mem.r32(rec.addr + 134)), b"Pok")                    # same length
    h.d.sync(rec)
    view = h.d.views[rec]
    assert rec.content == before.offset(30, 20)
    assert view.structure_origin() == view.mac_origin()
    assert view.windowTitle().startswith("Pok")


# -- robustness ----------------------------------------------------------------
class Slow(Recorder):
    """Busy for a moment after each click, so the mouse is on its way by
    the time it calls DragWindow or GrowWindow."""

    def handle(self, ev):
        if ev.what == k.mouseDown:
            time.sleep(0.2)
        return super().handle(ev)


def test_drag_and_grow_follow_the_mouse_from_the_click(harness):
    h = harness()
    app = Slow("App")
    proc = h.launch(app)
    rec = proc.windows[0]
    before = rec.content
    th, tv = h.title_point(rec)
    h.press(th, tv)
    h.move(th + 50, tv + 30)                    # before the app gets to DragWindow
    h.wait_until(lambda: h.d.tracker is not None, what="the drag")
    h.move(th + 100, tv + 60)
    h.release(th + 100, tv + 60)
    h.wait_until(lambda: app.last("DragWindow") is not None)
    assert (rec.content.left - before.left, rec.content.top - before.top) == (100, 60)

    w0, h0 = rec.content.width, rec.content.height
    gh, gv = h.grow_point(rec)
    h.press(gh, gv)
    h.move(gh + 40, gv + 20)
    h.wait_until(lambda: h.d.tracker is not None, what="the grow")
    h.move(gh + 80, gv + 40)
    h.release(gh + 80, gv + 40)
    h.wait_until(lambda: (rec.content.width, rec.content.height) == (w0 + 80, h0 + 40),
                 what="the new size")


class TakesNoSize(Recorder):
    """Calls GrowWindow but not SizeWindow, as an app that refuses the size would."""

    def handle(self, ev):
        if ev.what == k.mouseDown:
            part, win = self.tb.FindWindow(ev.where)
            if part == k.inGrow:
                self.log.append(("GrowWindow", self.tb.GrowWindow(win, ev.where,
                                                                  Rect(60, 100, 2000, 2000))))
                return False
        return super().handle(ev)


def test_a_size_the_app_doesnt_take_isnt_kept(harness):
    h = harness()
    app = TakesNoSize("App")
    proc = h.launch(app)
    rec = proc.windows[0]
    view = h.d.views[rec]
    width = view.width()
    gh, gv = h.grow_point(rec)
    h.press(gh, gv)
    h.wait_until(lambda: h.d.tracker is not None, what="the grow")
    h.move(gh + 70, gv + 40)
    h.release(gh + 70, gv + 40)
    h.wait_until(lambda: app.last("GrowWindow") is not None)
    assert app.last("GrowWindow")[1] != 0
    h.wait_until(lambda: view.preview_size is None and view.width() == width,
                 what="the window back at its size")
    assert rec.struc_rect.right == rec.content.right + 2      # frame and shadow


def test_the_bar_only_window_can_go_while_its_being_dragged(harness):
    h = harness(menu_bar="window")
    app = Recorder("App", windows=0)
    proc = h.launch(app)
    h.wait_until(lambda: proc in h.d.app_bars, what="the bar-only window")
    bar = h.d.app_bars[proc]
    f = bar.frame()
    tr = chrome.title_rect(f, bar.width() - chrome.shadow(f))
    th, tv = h.mac(bar, QPoint(tr.x() + 30, tr.y() + tr.height() // 2))
    h.press(th, tv)
    assert h.d.tracker is not None and h.d.tracker.widget is bar
    threading.Thread(target=app.new_window).start()      # the app opens a window meanwhile
    h.wait_until(lambda: proc not in h.d.app_bars, what="the bar-only window to go")
    assert h.d.tracker is None
    h._held = h.d.views[proc.windows[0]]
    h.move(th + 20, tv + 20)                             # nothing left to track it


class QuitsFromTheMenu(Recorder):
    """Quits from File > Quit without HiliteMenu(0), as many apps do."""

    def menu(self, result):
        if result >> 16 == self.FILE and result & 0xFFFF == 4:
            return True
        return super().menu(result)


def test_a_highlighted_title_doesnt_pass_to_the_next_app(harness):
    h = harness(menu_bar="global")
    h.launch(Recorder("Alpha", at=(30, 60)))
    beta = h.launch(QuitsFromTheMenu("Beta", at=(420, 260)))
    h.key(Qt.Key.Key_Q, Qt.KeyboardModifier.ControlModifier)
    h.wait_until(lambda: beta not in h.d.processes, what="Beta to quit")
    assert h.d.global_bar.process.name == "Alpha" and h.d.global_bar.hilite_id == 0


def test_the_host_cant_close_a_mac_window(harness):
    h = harness(host="desktop")
    app = Recorder("App")
    proc = h.launch(app)
    view = h.d.views[proc.windows[0]]
    view.close()                                         # Alt-F4, or the taskbar
    h.settle()
    assert view.isVisible() and proc.windows[0].visible


@pytest.mark.parametrize("host", ["screen", "desktop"])
def test_a_window_shown_by_its_visible_flag_is_shown(harness, host):
    # Cap'n Magneto shows its title screen's window by setting the flag.
    h = harness(host=host)
    proc = h.launch(Recorder("App"))
    rec = proc.windows[0]
    view = h.d.views[rec]
    screen = h.d.system.screen
    with h.d.system.lock:
        rec.visible = False
        screen.moved()                      # as a 68k program's traps do, a tick on
    h.wait_until(lambda: view.isHidden(), what="the view hidden")
    with h.d.system.lock:
        rec.visible = True
        screen.moved()
    h.wait_until(lambda: not view.isHidden(), what="the view shown")


def test_an_app_that_breaks_its_own_record_is_stopped_alone(harness):
    h = harness()
    good = h.launch(Recorder("Good", at=(420, 260)))
    bad = h.launch(Recorder("Bad"))
    rec = bad.windows[0]
    h.d.system.mem.w32(rec.addr + 134, 0x00FFFFF0)      # titleHandle: past the end of RAM
    h.d.views[rec].update()
    h.wait_until(lambda: bad not in h.d.processes, what="the bad app to stop")
    assert good in h.d.processes and good.windows[0] in h.d.views


def test_gui_errors_are_logged_not_fatal(harness, monkeypatch):
    monkeypatch.setattr(sys, "excepthook", sys.__excepthook__)
    harness()
    assert sys.excepthook is display_module._report_gui_error   # PyQt aborts without a hook


class WideWindow(Recorder):
    """Makes a window far wider than the screen, as Excel 3 does, and
    moves it until another part of it is in view."""

    def new_window(self):
        w = self.tb.NewWindow(Rect.from_xywh(40, 60, 30000, 160), "Wide", True,
                              k.documentProc)
        self.log.append(("made", w.backing.area))
        self.tb.BeginUpdate(w)
        self.tb.EndUpdate(w)
        self.tb.MoveWindow(w, -20000, 60)
        self.log.append(("moved", w.backing.area, w.update_pending))
        return w


def test_a_window_past_the_screen_keeps_what_can_be_seen(harness):
    h = harness()
    app = WideWindow("App")
    h.launch(app)
    assert app.ready.wait(10)
    s = h.d.system.screen_bits.bounds
    # Up to a screen's width or height past the screen's edges
    assert app.last("made") == ("made", Rect(0, 0, 160, 2 * s.width - 40))
    assert app.last("moved") == ("moved", Rect(0, 20000 - s.width, 160, 20000 + 2 * s.width),
                                 True)


class MovesItsPort(Recorder):
    """Draws a dot, then moves the port 8 pixels left and up and makes the
    window that much bigger, as Multiplan moves its formula bar's window:
    the window goes with the port, and the dot stays where it was."""

    def new_window(self):
        tb = self.tb
        w = super().new_window()
        tb.SetPort(w)
        tb.BeginUpdate(w)
        tb.EndUpdate(w)
        tb.PaintRect(Rect(20, 30, 21, 31))
        at = w.content
        tb.MovePortTo(at.left - 8, at.top - 8)
        tb.SizeWindow(w, at.width + 8, at.height + 8, False)
        self.log.append(("moved", at, w.content))
        return w


def test_a_window_whose_port_moves_leaves_its_pixels_where_they_were(harness):
    h = harness()
    app = MovesItsPort("App")
    proc = h.launch(app)
    assert app.ready.wait(10)
    rec = proc.windows[0]
    _, before, after = app.last("moved")
    assert after == Rect(before.top - 8, before.left - 8, before.bottom, before.right)
    assert h.d.views[rec].structure_origin() == h.d.views[rec].mac_origin()
    image, black = rec.backing.image, QColor("#000000")
    assert image.pixelColor(38, 28) == black           # 30, 20 in its port before
    assert image.pixelColor(30, 20) != black


class DrawsOnlyWhatNeedsIt(Recorder):
    """Paints its window black, asks for the left half to be drawn again
    but for the top of that, and erases all of it in the update, as
    MacWrite does: only the rest of the left half is erased."""

    def new_window(self):
        tb = self.tb
        w = super().new_window()
        tb.SetPort(w)
        tb.BeginUpdate(w)
        tb.EndUpdate(w)
        r = w.port_rect
        tb.PaintRect(r)
        tb.InvalRect(Rect(0, 0, r.bottom, r.width // 2))
        tb.ValidRect(Rect(0, 0, 40, r.width // 2))
        tb.BeginUpdate(w)
        self.log.append(("during", qdraw.read_rgn(w.mem, w.vis_rgn)))
        tb.EraseRect(r)
        tb.EndUpdate(w)
        self.log.append(("after", qdraw.read_rgn(w.mem, w.vis_rgn), w.update_pending))
        return w


def test_an_update_draws_only_in_the_update_region(harness):
    h = harness()
    app = DrawsOnlyWhatNeedsIt("App")
    proc = h.launch(app)
    rec = proc.windows[0]
    r = rec.port_rect
    assert app.last("during") == ("during", qdraw.Region.rect(Rect(40, 0, r.bottom, r.width // 2)))
    assert app.last("after") == ("after", qdraw.Region.rect(r), False)
    image, black = rec.backing.image, QColor("#000000")
    assert image.pixelColor(10, 100) != black          # what was in the update region
    assert image.pixelColor(10, 20) == black           # validated
    assert image.pixelColor(200, 100) == black         # never invalid


class DrawsTheGrowIcon(Recorder):
    def handle(self, ev):
        if ev.what == k.updateEvt:
            self.tb.SetPort(ev.message)
            self.tb.DrawGrowIcon(ev.message)
        return super().handle(ev)


def test_grow_icon_lines_meet_the_scroll_bars(harness):
    h = harness()
    app = DrawsTheGrowIcon("App")
    proc = h.launch(app)
    rec = proc.windows[0]
    h.settle()
    image, r = rec.backing.image, rec.port_rect
    black = QColor("#000000")
    # A scroll bar's edge is 15 pixels in: its rect is (right - 15, top - 1, right + 1, ...).
    assert image.pixelColor(r.right - 15, 10) == black
    assert image.pixelColor(r.right - 16, 10) != black
    assert image.pixelColor(10, r.bottom - 15) == black
    assert image.pixelColor(10, r.bottom - 16) != black


class ClipsTheGrowIcon(Recorder):
    def handle(self, ev):
        if ev.what == k.updateEvt:
            w = ev.message
            self.tb.SetPort(w)
            r = w.port_rect
            # Shadowgate's way to draw the size box without the line along the bottom.
            qdraw.write_rgn(self.tb._sys.heap_of(w.clip_rgn), w.clip_rgn,
                            qdraw.Region.rect(Rect(r.top, r.right - 15, r.bottom, r.right)))
            self.tb.DrawGrowIcon(w)
        return super().handle(ev)


def test_the_grow_icon_is_clipped_to_the_ports_clip(harness):
    h = harness()
    app = ClipsTheGrowIcon("App")
    proc = h.launch(app)
    rec = proc.windows[0]
    h.settle()
    image, r = rec.backing.image, rec.port_rect
    black = QColor("#000000")
    assert image.pixelColor(r.right - 15, 10) == black
    assert image.pixelColor(r.right - 10, r.bottom - 15) == black
    assert image.pixelColor(10, r.bottom - 15) != black
