"""
macrt.ui.tracking - Mouse tracking behind TrackGoAway, TrackBox,
DragWindow and GrowWindow.

Each tracker answers one blocking Toolbox call.  The guest thread waits on
`future` while the GUI thread feeds the tracker mouse events.  The guest
may get to the call after the button is already up; every tracker checks
for that in start(), as StillDown would on a real Mac.
"""

from __future__ import annotations

from concurrent.futures import Future
from typing import TYPE_CHECKING

from PyQt6.QtCore import QPoint
from PyQt6.QtGui import QGuiApplication
from PyQt6.QtWidgets import QWidget

from ..toolbox.types import Point, Rect

if TYPE_CHECKING:
    from .display import Display
    from .window import MacWindowView


class Tracker:
    def __init__(self, display: Display, future: Future):
        self.display = display
        self.future = future
        self.done = False

    def start(self) -> None: ...
    def move(self, gpos: QPoint) -> None: ...
    def press(self, gpos: QPoint) -> None: ...
    def release(self, gpos: QPoint) -> None: ...

    def cancel(self) -> None:
        self.finish(None)

    def finish(self, result) -> None:
        if self.done:
            return
        self.done = True
        self.cleanup()
        self.display.tracker_finished(self)
        if not self.future.done():
            self.future.set_result(result)

    def cleanup(self) -> None: ...


def _system_move(widget: QWidget) -> bool:
    """Wayland won't let clients position their own windows; ask the compositor."""
    if not widget.isWindow() or QGuiApplication.platformName() != "wayland":
        return False
    handle = widget.windowHandle()
    return bool(handle and handle.startSystemMove())


class BoxTracker(Tracker):
    """TrackGoAway / TrackBox: True if released inside the box."""

    def __init__(self, display, future, view: MacWindowView, part: int):
        super().__init__(display, future)
        self.view = view
        self.part = part

    def _inside(self, gpos: QPoint) -> bool:
        box = self.view.box_rect(self.part)
        return box is not None and box.contains(self.view.mapFromGlobal(gpos))

    def start(self) -> None:
        if not self.display.input.down:
            self.finish(self._inside(self.display.input.global_pos))
        else:
            self.move(self.display.input.global_pos)

    def move(self, gpos):
        self.view.set_pressed(self.part if self._inside(gpos) else 0)

    def release(self, gpos):
        self.finish(self._inside(gpos))

    def cancel(self):
        self.finish(False)

    def cleanup(self):
        self.view.set_pressed(0)


class DragTracker(Tracker):
    """DragWindow: returns the structure's new top-left, or None if unmoved.

    Classic Mac dragged a gray outline and moved the window on release.
    Host windows can't draw outside themselves, so the window moves live.
    """

    def __init__(self, display, future, widget: QWidget, origin: Point, limit: Rect,
                 grab: Point):
        super().__init__(display, future)
        self.widget = widget
        self.origin = origin        # structure top-left when the drag began
        self.limit = limit          # where the grab point is allowed to go
        self.current = origin
        self.grab = grab            # the mouseDown's point (startPt)

    def start(self):
        if not self.display.input.down:
            self.finish(None)
        elif _system_move(self.widget):
            self.finish(None)       # the view reports the new position as it moves
        else:
            self.move(self.display.input.global_pos)    # the mouse may be on its way

    def move(self, gpos):
        pt = self.display.mac_from_global(gpos)
        v = min(max(pt.v, self.limit.top), self.limit.bottom - 1)
        h = min(max(pt.h, self.limit.left), self.limit.right - 1)
        self.current = Point(self.origin.v + v - self.grab.v, self.origin.h + h - self.grab.h)
        self.widget.place(self.current)

    def release(self, gpos):
        self.move(gpos)
        self.finish(self.current if self.current != self.origin else None)

    def cancel(self):
        self.widget.place(self.origin)
        self.finish(None)


class GrowTracker(Tracker):
    """GrowWindow: returns (height << 16) | width, or 0 if unchanged."""

    def __init__(self, display, future, view: MacWindowView, limits: Rect, grab: Point):
        super().__init__(display, future)
        self.view = view
        # sizeRect: top/left are the minimum height/width, bottom/right the maximum
        self.limits = limits
        self.start_size = (view.rec.content.width, view.rec.content.height)
        self.size = self.start_size
        self.grab = grab            # the mouseDown's point (startPt)

    def start(self):
        if not self.display.input.down:
            self.finish(0)
        else:
            self.move(self.display.input.global_pos)

    def move(self, gpos):
        pt = self.display.mac_from_global(gpos)
        lim = self.limits
        w = min(max(self.start_size[0] + pt.h - self.grab.h, lim.left), lim.right)
        h = min(max(self.start_size[1] + pt.v - self.grab.v, lim.top), lim.bottom)
        if (w, h) != self.size:
            self.size = (w, h)
            self.view.preview_size = self.size
            self.view.relayout()

    def release(self, gpos):
        self.move(gpos)
        if self.size == self.start_size:
            self.finish(0)
        else:
            self.finish((self.size[1] << 16) | self.size[0])

    def cancel(self):
        self.finish(0)

    def finish(self, result):
        # A new size stays previewed until the guest calls SizeWindow, or
        # asks for its next event without doing so.
        if not result:
            self.view.preview_size = None
            self.view.relayout()
        super().finish(result)
