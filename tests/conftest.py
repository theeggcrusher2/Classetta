import os
import sys
import threading
import time

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PyQt6.QtCore import QPoint, Qt
from PyQt6.QtTest import QTest
from PyQt6.QtWidgets import QApplication

from macrt.toolbox import constants as k
from macrt.toolbox.types import Point, Rect
from macrt.ui import chrome
from macrt.ui.display import Display, DisplayConfig

LB = Qt.MouseButton.LeftButton
NO_MODS = Qt.KeyboardModifier.NoModifier


@pytest.fixture(scope="session")
def qapp():
    return QApplication.instance() or QApplication([])


@pytest.fixture(autouse=True)
def gui_errors(monkeypatch):
    """An exception on the GUI thread fails the test.  Qt can't raise it
    into the test, and the runtime only logs it so other apps keep going."""
    errors = []

    def hook(etype, value, tb):
        errors.append(value)
        sys.__excepthook__(etype, value, tb)

    monkeypatch.setattr(sys, "excepthook", hook)
    yield errors
    assert not errors, f"raised on the GUI thread: {errors!r}"


def hi_word(x):
    v = (x >> 16) & 0xFFFF
    return v - 0x10000 if v & 0x8000 else v


class Recorder:
    """A small guest app.  It opens one document window, then runs the
    usual event loop and logs every Toolbox answer it gets in `log`."""

    FILE, EDIT = 129, 130

    def __init__(self, name, *, windows=1, proc_id=k.zoomDocProc, at=(40, 60)):
        self.name = name
        self.n_windows = windows
        self.proc_id = proc_id
        self.at = at
        self.log = []
        self.events = []
        self.ready = threading.Event()
        self.tb = None

    def __call__(self, tb):
        self.tb = tb
        file = tb.NewMenu(self.FILE, "File")
        tb.AppendMenu(file, "New/N;Close/W;(-;Quit/Q")
        edit = tb.NewMenu(self.EDIT, "Edit")
        tb.AppendMenu(edit, "(Undo/Z;Cut/X;Copy/C")
        tb.InsertMenu(file, 0)
        tb.InsertMenu(edit, 0)
        tb.DrawMenuBar()
        for i in range(self.n_windows):
            self.new_window()
        self.ready.set()
        while True:
            got, ev = tb.WaitNextEvent(k.everyEvent, 5)
            if not got:
                continue
            self.events.append((ev.what, ev.message))
            if self.handle(ev):
                return

    def new_window(self):
        n = len(self.tb._p.windows)
        h, v = self.at
        bounds = Rect.from_xywh(h + 20 * n, v + 20 * n, 260, 160)
        return self.tb.NewWindow(bounds, f"{self.name} {n + 1}", True, self.proc_id)

    def handle(self, ev):
        tb = self.tb
        if ev.what == k.mouseDown:
            part, win = tb.FindWindow(ev.where)
            self.log.append(("FindWindow", part, win))
            if part == k.inMenuBar:
                return self.menu(tb.MenuSelect(ev.where))
            if part == k.inGoAway:
                ok = tb.TrackGoAway(win, ev.where)
                self.log.append(("TrackGoAway", ok))
                if ok:
                    tb.DisposeWindow(win)
            elif part == k.inDrag:
                tb.DragWindow(win, ev.where, None)
                self.log.append(("DragWindow", win.content))
            elif part == k.inGrow:
                size = tb.GrowWindow(win, ev.where, Rect(60, 100, 2000, 2000))
                self.log.append(("GrowWindow", size))
                if size:
                    tb.SizeWindow(win, size & 0xFFFF, size >> 16, True)
            elif part in (k.inZoomIn, k.inZoomOut):
                ok = tb.TrackBox(win, ev.where, part)
                self.log.append(("TrackBox", part, ok))
                if ok:
                    tb.ZoomWindow(win, part, False)
            elif part == k.inContent and win is not tb.FrontWindow():
                tb.SelectWindow(win)
        elif ev.what == k.keyDown and ev.modifiers & k.cmdKey:
            return self.menu(tb.MenuKey(chr(ev.message & 0xFF)))
        elif ev.what == k.updateEvt:
            tb.BeginUpdate(ev.message)
            tb.EndUpdate(ev.message)
        return False

    def menu(self, result):
        self.log.append(("MenuSelect", result))
        menu, item = hi_word(result), result & 0xFFFF
        if menu == self.FILE:
            if item == 1:
                self.new_window()
            elif item == 2 and self.tb.FrontWindow() is not None:
                self.tb.DisposeWindow(self.tb.FrontWindow())
            elif item == 4:
                self.tb.HiliteMenu(0)
                return True
        self.tb.HiliteMenu(0)
        return False

    def last(self, name):
        for entry in reversed(self.log):
            if entry[0] == name:
                return entry
        return None


class Harness:
    def __init__(self, qapp, **config):
        config.setdefault("host", "screen")
        config.setdefault("screen_size", (800, 560))
        self.app = qapp
        self.d = Display(DisplayConfig(**config))
        self.d.show()
        self._held = None

    def launch(self, guest: Recorder):
        proc = self.d.launch(guest.name, guest)
        self.wait_until(guest.ready.is_set, timeout=5, what=f"{guest.name}'s window")
        self.settle()
        return proc

    # -- time --------------------------------------------------------------
    def wait_until(self, cond, timeout=3.0, what="condition"):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            QApplication.processEvents()
            if cond():
                return
            QTest.qWait(10)
        raise AssertionError(f"timed out waiting for {what}")

    def settle(self, ms=150):
        QTest.qWait(ms)

    # -- mouse, in Mac global coordinates ----------------------------------
    def _target(self, h, v):
        g = self.d.global_from_mac(Point(v, h))
        w = QApplication.activePopupWidget() or self._held or QApplication.widgetAt(g)
        assert w is not None, f"nothing at ({h}, {v})"
        return w, w.mapFromGlobal(g)

    def press(self, h, v, mods=NO_MODS):
        w, p = self._target(h, v)
        self._held = w
        QTest.mousePress(w, LB, mods, p)

    def move(self, h, v):
        w, p = self._target(h, v)
        QTest.mouseMove(w, p)

    def release(self, h, v, mods=NO_MODS):
        w, p = self._target(h, v)
        QTest.mouseRelease(w, LB, mods, p)
        self._held = None

    def click(self, h, v, mods=NO_MODS):
        self.press(h, v, mods)
        self.release(h, v, mods)

    def key(self, key, mods=NO_MODS):
        target = self.d.screen_widget
        QTest.keyClick(target, key, mods)

    # -- where things are ----------------------------------------------------
    def mac(self, widget, local: QPoint):
        m = self.d.mac_from_global(widget.mapToGlobal(local))
        return m.h, m.v

    def title_point(self, rec, dx=60):
        view = self.d.views[rec]
        f = view.frame()
        r = chrome.title_rect(f, view.width() - chrome.shadow(f))
        h, v = self.mac(view, r.topLeft())
        return h + dx, v + r.height() // 2

    def box_point(self, rec, part):
        view = self.d.views[rec]
        r = view.box_rect(part)
        assert r is not None, "window has no such box"
        return self.mac(view, r.center())

    def grow_point(self, rec):
        return rec.content.right - 5, rec.content.bottom - 5

    def menu_title_point(self, bar, title):
        for menu, cell in bar.cells():
            if menu.title == title:
                return self.mac(bar, cell.center())
        raise KeyError(title)

    def item_point(self, index):
        pop = QApplication.activePopupWidget()
        assert pop is not None, "no menu is pulled down"
        return self.mac(pop, pop.item_rect(index).center())

    def choose(self, bar, title, index):
        """Press on a menu title, drag to an item, release."""
        self.press(*self.menu_title_point(bar, title))
        self.wait_until(lambda: QApplication.activePopupWidget() is not None, what="pulldown")
        h, v = self.item_point(index)
        self.move(h, v)
        self.release(h, v)

    def close(self):
        self.d.shutdown(quit_app=False)
        for proc in list(self.d.launch_order):
            if proc.thread is not None:
                proc.thread.join(2)
        QApplication.processEvents()


@pytest.fixture
def harness(qapp):
    made = []

    def make(**config):
        h = Harness(qapp, **config)
        made.append(h)
        return h

    yield make
    for h in made:
        h.close()
