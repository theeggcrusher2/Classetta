"""
macrt.ui.window - Widgets that show a WindowRecord.

A MacWindowView never decides anything itself.  It draws the chrome for
the record it's given (via macrt.ui.chrome), shows the window's port as
its content, and resizes and moves when the record changes.  A window
with a WDEF of the application's own has the shape of the structure
region the WDEF works out, and shows the frame it drew.  Clicks go to the
guest through the Display's input router.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from PyQt6.QtCore import QPoint, Qt
from PyQt6.QtGui import QPainter
from PyQt6.QtWidgets import QWidget

from ..toolbox.constants import wInGoAway, wNoHit
from ..toolbox.qdraw import Region, read_rgn
from ..toolbox.types import Point, Rect
from ..toolbox.windows import WindowKind, WindowRecord
from . import chrome, theme
from .chrome import Frame
from .menubar import MenuBarWidget

if TYPE_CHECKING:
    from ..process import Process
    from .display import Display

TOP_LEVEL_FLAGS = (Qt.WindowType.Window | Qt.WindowType.FramelessWindowHint
                   | Qt.WindowType.NoDropShadowWindowHint)


class _Hosted(QWidget):
    """A widget the Display places on the Mac screen: a host window of its
    own (host="desktop") or a child of the emulated screen (host="screen")."""

    def __init__(self, display: Display, parent: QWidget | None):
        super().__init__(parent, TOP_LEVEL_FLAGS if parent is None else Qt.WindowType.Widget)
        self.display = display
        self._placed_at: QPoint | None = None
        if parent is None:
            self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
            self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)

    def place(self, mac_pt: Point) -> None:
        self._placed_at = self.display.parent_pos(mac_pt)
        if self.pos() != self._placed_at:
            self.move(self._placed_at)

    def mac_origin(self) -> Point:
        return self.display.mac_from_parent_pos(self.pos())

    def moveEvent(self, event):  # noqa: N802
        # Only host-window managers move us behind our back.
        if self.isWindow() and self.isVisible() and self.pos() != self._placed_at:
            self._placed_at = self.pos()
            self.moved_by_host()
        super().moveEvent(event)

    def moved_by_host(self) -> None:
        pass

    def closeEvent(self, event):  # noqa: N802
        # A Mac window goes away when its application says so.  The host's
        # close button or Alt-F4 would only hide it behind the app's back.
        if not self.display.shutting_down:
            event.ignore()


class ContentView(QWidget):
    """The window's port: its Backing until QuickDraw draws through
    portBits.  Later this also hosts native dialog items."""

    def __init__(self, view: MacWindowView):
        super().__init__(view)
        self.view = view
        self.setProperty("macrt_role", "content")
        self.setMouseTracking(True)

    def paintEvent(self, event):  # noqa: N802
        backing = self.view.rec.backing
        p = QPainter(self)
        p.fillRect(self.rect(), theme.WHITE)
        with backing.lock:
            p.drawImage(backing.area.left, backing.area.top, backing.image)


class MacWindowView(_Hosted):
    def __init__(self, display: Display, rec: WindowRecord, *, menu_bar: bool,
                 parent: QWidget | None = None):
        super().__init__(display, parent)
        self.rec = rec
        self.pressed = wNoHit
        self.preview_size: tuple[int, int] | None = None   # live GrowWindow feedback
        self.setProperty("macrt_role", "window")
        self.setMouseTracking(True)
        self.content = ContentView(self)
        self.wants_bar = menu_bar
        self.bar: MenuBarWidget | None = None
        self._fit_bar()

    def _fit_bar(self) -> None:
        """A menu bar under the title bar, for a window whose frame the host
        draws, of a kind that has one."""
        rec = self.rec
        wanted = (self.wants_bar and not rec.wdef_shaped
                  and chrome.supports_menu_bar(rec.definition.kind))
        if wanted and self.bar is None:
            self.bar = MenuBarWidget(self.display, self)
            self.bar.process = rec.process
            self.bar.show()
        elif not wanted and self.bar is not None:
            self.bar.hide()
            self.bar.deleteLater()
            self.bar = None

    def frame(self) -> Frame:
        return Frame.for_window(self.rec, menu_bar=self.bar is not None, pressed=self.pressed)

    def regions(self) -> tuple[Region, Region]:
        """The structure and content regions its WDEF worked out, global."""
        rec = self.rec
        with self.display.system.lock:
            return rec.wdef_struc(), read_rgn(rec.mem, rec.cont_rgn)

    def structure_origin(self) -> Point:
        """Top-left of the structure region, in global (Mac) coordinates."""
        if self.rec.wdef_shaped:
            return self.regions()[0].bbox.top_left
        l, t, _, _ = chrome.insets(self.frame())
        return self.rec.content.top_left.offset(-l, -t)

    def content_offset(self) -> tuple[int, int]:
        """Where the content area's top left is, from the structure region's."""
        o, c = self.structure_origin(), self.rec.content
        return c.left - o.h, c.top - o.v

    def content_size(self) -> tuple[int, int]:
        return self.preview_size or (self.rec.content.width, self.rec.content.height)

    def sync(self) -> None:
        """Bring the widget in line with the record, and the record's
        structure region in line with the frame drawn around it.  A window
        with its own WDEF is the WDEF's to shape and draw, at the
        application's next trap, which reshapes it after."""
        rec = self.rec
        if rec.own_wdef and rec.process.runner is not None:
            rec.process.frames_due.add(rec)
        elif rec.wdef_shaped:
            rec.wdef_shaped = False         # it has a standard windowDefProc now
            rec.frame_image = None
        self.reshape()

    def reshape(self) -> None:
        self._fit_bar()
        shown = self.relayout()
        o = self.structure_origin()
        self.place(o)
        if not self.rec.wdef_shaped:
            with self.display.system.lock:
                self.rec.struc_rect = Rect(o.v, o.h, o.v + self.height(), o.h + self.width())
        self.setWindowTitle(f"{self.rec.title} — {self.rec.process.name}"
                            if self.rec.title else self.rec.process.name)
        self.setVisible(shown and self.rec.visible and not self.rec.process.hidden)
        self.update()
        self.content.update()
        if self.bar is not None:
            self.bar.update()

    def relayout(self) -> bool:
        """Size and mask the widget; False if it has no shape to show."""
        if self.rec.wdef_shaped:
            return self._relayout_shaped()
        f = self.frame()
        w, h = chrome.structure_size(f, *self.content_size())
        s = chrome.shadow(f)
        self.resize(w + s, h + s)
        self.setMask(chrome.mask(f, w, h))
        self.content.setGeometry(chrome.content_rect(f, w, h))
        shape = chrome.content_mask(f, w, h)
        if shape is None:
            self.content.clearMask()
        else:
            self.content.setMask(shape)
        if self.bar is not None:
            self.bar.setGeometry(chrome.menu_bar_rect(f, w))
        self.content.show()
        return True

    def _relayout_shaped(self) -> bool:
        struc, cont = self.regions()
        box = struc.bbox
        if box.empty:
            return False
        o = box.top_left
        self.resize(box.width, box.height)
        self.setMask(struc.offset(-o.h, -o.v).to_qregion())
        c = self.rec.content
        self.content.setGeometry(c.left - o.h, c.top - o.v, c.width, c.height)
        shape = cont.sect(Region.rect(c)).offset(-c.left, -c.top)
        self.content.setVisible(not shape.empty)
        if not shape.empty:
            self.content.setMask(shape.to_qregion())
        return True

    def hit(self, local: QPoint) -> int:
        f = self.frame()
        w, h = chrome.structure_size(f, *self.content_size())
        return chrome.hit(f, w, h, local.x(), local.y())

    def box_rect(self, part: int):
        f = self.frame()
        w = chrome.structure_size(f, *self.content_size())[0]
        return chrome.close_box_rect(f, w) if part == wInGoAway else chrome.zoom_box_rect(f, w)

    def set_pressed(self, part: int) -> None:
        if part != self.pressed:
            self.pressed = part
            self.update()

    def paintEvent(self, event):  # noqa: N802
        proc = self.rec.process
        if proc.faulted:
            return
        if self.rec.wdef_shaped:
            self._paint_shaped()
            return
        try:
            f = self.frame()                    # everything it reads from the record
            w, h = chrome.structure_size(f, *self.content_size())
        except Exception as e:  # noqa: BLE001 - one app's bad record mustn't stop the GUI
            self.display.guest_fault(proc, e)
            return
        chrome.paint(QPainter(self), f, w, h)

    def _paint_shaped(self) -> None:
        """What the window's WDEF drew, and white where it drew nothing."""
        p = QPainter(self)
        p.fillRect(self.rect(), theme.WHITE)
        drawn = self.rec.frame_image
        if drawn is not None:
            h, v, img = drawn
            l, t = self.content_offset()
            p.drawImage(l + h, t + v, img)

    def moved_by_host(self) -> None:
        self.display.view_moved(self)


class AppBarWindow(_Hosted):
    """With menu_bar="window", a menu bar needs a window to live in.  An app
    with no document windows open gets this one: its name and its menus."""

    def __init__(self, display: Display, process: Process, parent: QWidget | None = None):
        super().__init__(display, parent)
        self.process = process
        self.setProperty("macrt_role", "appbar")
        self.setMouseTracking(True)
        self.bar = MenuBarWidget(display, self)
        self.bar.process = process
        self.setWindowTitle(process.name)
        self.relayout()

    def frame(self) -> Frame:
        return Frame(WindowKind.DOCUMENT, title=self.process.name,
                     hilited=self.display.front_process is self.process, menu_bar=True)

    def relayout(self) -> None:
        f = self.frame()
        w, h = chrome.structure_size(f, max(self.bar.natural_width(), 220), 0)
        s = chrome.shadow(f)
        self.resize(w + s, h + s)
        self.setMask(chrome.mask(f, w, h))
        self.bar.setGeometry(chrome.menu_bar_rect(f, w))
        self.update()

    def paintEvent(self, event):  # noqa: N802
        f = self.frame()
        w, h = self.width() - chrome.shadow(f), self.height() - chrome.shadow(f)
        chrome.paint(QPainter(self), f, w, h)
