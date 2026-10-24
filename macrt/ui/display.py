"""
macrt.ui.display - Where Mac windows and menu bars appear on the host.

Two independent settings (DisplayConfig):

  host="desktop"     every Mac window is a frameless host window, so Mac
                     windows sit among the host's own windows.
  host="screen"      one host window holds an emulated Mac screen
                     (desktop pattern, rounded corners) with the windows
                     inside it.

  menu_bar="global"  one menu bar at the top of the Mac screen showing the
                     front app's menus.  With more than one app running it
                     switches with the front app and grows the System 7
                     Application menu at its right end.
  menu_bar="window"  each document window carries its app's menu bar under
                     its title bar, like a host app.  An app with no such
                     window open gets a small bar-only window.

The Display also routes host input.  Clicks become mouseDown events in the
owning app's queue (a click in a background app's window brings that app
forward instead), and the trackers answer blocking calls like MenuSelect.
"""

from __future__ import annotations

import sys
import time
import traceback
from concurrent.futures import Future
from dataclasses import dataclass, field

from PyQt6.QtCore import QEvent, QObject, QPoint, Qt, QTimer
from PyQt6.QtGui import QGuiApplication, QImage, QPainter
from PyQt6.QtWidgets import QApplication, QMessageBox, QVBoxLayout, QWidget

from ..m68k.sound import WaveFile
from ..macfile import Folder, MacFile, VolumeFolder, system_on
from ..process import GuestMain, Process
from ..toolbox.api import Toolbox
from ..toolbox.constants import (
    ASK_WDEF, WHIT_TO_PART, btnState, checkMark, convertClipboard, inDesk, inMenuBar,
    kApplicationMenuID, keyDown, keyUp, autoKey, mouseDown, mouseUp, osEvt, resumeFlag,
    suspendResumeMessage, wInMenuBar, wNoHit,
)
from ..toolbox.events import EventQueue, EventRecord
from ..toolbox.menus import Menu, MenuItem
from ..toolbox.qdraw import Region, ink, patCopy, qrect
from ..toolbox.scrap import needs_converting
from ..toolbox.screen import DESK, Screen
from ..toolbox.system import System
from ..toolbox.templates import place
from ..toolbox.types import Point, Rect
from ..toolbox.windows import WindowRecord, describe_proc
from . import chrome, theme
from .bridge import GuiBridge
from .keymap import key_message, modifiers_from_qt
from .menubar import MenuBarWidget, MenuTracker, PopUpTracker
from .tracking import BoxTracker, DragTracker, GrowTracker, Tracker
from .window import AppBarWindow, MacWindowView

HOSTS = ("desktop", "screen")
MENU_BARS = ("global", "window")


@dataclass
class DisplayConfig:
    host: str = "desktop"
    menu_bar: str = "global"
    screen_size: tuple[int, int] | None = None   # None: host screen / 640x480
    sticky_menus: bool = True
    application_menu: str = "auto"               # "auto" | "always" | "never"
    system_file: MacFile | None = None           # a real System file's resources, if wanted
    speed: float | None = None                   # times a Mac Plus; None: as fast as it goes
    model: str = "plus"                          # which Mac it says it is: system.MODELS
    disks: list = field(default_factory=list)    # disk images in the drives, in order
    sound: bool = False                          # play the Mac's sound on the host's output
    record_sound: str | None = None              # and write it to this WAV file
    scripted: bool = False                       # driven by --script: alerts go to the log,
                                                 # so a run never waits on one to be closed

    def __post_init__(self):
        if self.host not in HOSTS:
            raise ValueError(f"host must be one of {HOSTS}")
        if self.menu_bar not in MENU_BARS:
            raise ValueError(f"menu_bar must be one of {MENU_BARS}")


class InputState:
    """Mouse and modifier state, written on the GUI thread, read by guests."""

    def __init__(self):
        self.down = False
        self.global_pos = QPoint(0, 0)
        self.mac_pos = Point(0, 0)
        self.press_time = 0.0
        self.modifiers = btnState


class MacScreen(QWidget):
    """host="screen": the emulated screen.  Windows are its children.  The
    desk is gray, and shows the screen buffer once a program draws on it."""

    def __init__(self, size: tuple[int, int], parent: QWidget):
        super().__init__(parent)
        self.setFixedSize(*size)
        self.setProperty("macrt_role", "screen")
        self.setMouseTracking(True)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.mirror: Screen | None = None

    def paintEvent(self, event):  # noqa: N802
        p = QPainter(self)
        p.fillRect(self.rect(), ink(DESK, patCopy)[0])
        mirror = self.mirror
        if mirror is not None and mirror.desk_drawn:
            r = event.rect()
            shown = mirror.desk.sect(Region.rect(Rect(r.top(), r.left(), r.bottom() + 1,
                                                      r.right() + 1)))
            if not shown.empty:
                box = shown.bbox
                p.setClipRegion(shown.to_qregion())
                p.drawImage(QPoint(box.left, box.top), mirror.shown.image(box))
                p.setClipping(False)
        # A global menu bar draws the top corners over these.
        top_left = theme.SCREEN_CORNER
        bottom_left = list(reversed(top_left))
        h, w = len(top_left), len(top_left[0])
        for x, y, bitmap in ((0, 0, top_left), (0, self.height() - h, bottom_left)):
            theme.draw_bitmap(p, x, y, bitmap)
            theme.draw_bitmap(p, self.width() - w, y, [row[::-1] for row in bitmap])


class ScreenWindow(QWidget):
    def __init__(self, display: Display, size: tuple[int, int]):
        super().__init__()
        self.display = display
        self.setWindowTitle(f"macrt — {size[0]}×{size[1]}")
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        self.screen_widget = MacScreen(size, self)
        layout.addWidget(self.screen_widget)

    def closeEvent(self, event):  # noqa: N802
        self.display.shutdown()
        event.accept()


_MOUSE = {QEvent.Type.MouseButtonPress, QEvent.Type.MouseButtonDblClick,
          QEvent.Type.MouseMove, QEvent.Type.MouseButtonRelease}
_KEYS = {QEvent.Type.KeyPress, QEvent.Type.KeyRelease}


class InputRouter(QObject):
    """Application-wide event filter: our widgets never see raw input.
    Each widget with a macrt_role tracks the mouse, so its moves come
    through with the button up too: GetMouse and low memory's Mouse
    follow it, as on a Mac."""

    def __init__(self, display: Display):
        super().__init__()
        self.display = display

    def eventFilter(self, obj, ev):  # noqa: N802
        t = ev.type()
        if t not in _MOUSE and t not in _KEYS:
            return False
        if not isinstance(obj, QWidget) or obj.property("macrt_role") is None:
            return False
        d = self.display
        if t in _KEYS:
            d.key_event(ev)
            return True
        if t != QEvent.Type.MouseMove and ev.button() != Qt.MouseButton.LeftButton:
            return True
        d.update_mouse(ev.globalPosition().toPoint(), ev.modifiers(),
                       down={QEvent.Type.MouseButtonPress: True,
                             QEvent.Type.MouseButtonDblClick: True,
                             QEvent.Type.MouseButtonRelease: False}.get(t))
        if t == QEvent.Type.MouseMove:
            if d.tracker is not None:
                d.tracker.move(d.input.global_pos)
        elif t == QEvent.Type.MouseButtonRelease:
            d.mouse_up()
        else:
            d.mouse_down(obj)
        return True


def _report_gui_error(etype, value, tb) -> None:
    """PyQt aborts the whole runtime on an exception in a slot or event
    handler unless there's an excepthook, and every app would go with it."""
    print("macrt: error on the GUI thread", file=sys.stderr)
    traceback.print_exception(etype, value, tb)


class Display(QObject):
    def __init__(self, config: DisplayConfig | None = None):
        super().__init__()
        if sys.excepthook is sys.__excepthook__:
            sys.excepthook = _report_gui_error
        self.config = config or DisplayConfig()
        self.bridge = GuiBridge()
        self.input = InputState()
        self.processes: list[Process] = []       # front to back
        self.launch_order: list[Process] = []
        self.views: dict[WindowRecord, MacWindowView] = {}
        self.app_bars: dict[Process, AppBarWindow] = {}
        self.tracker: Tracker | MenuTracker | PopUpTracker | None = None
        self.body_font = theme.body_font()
        self._swallow_release = False
        self._last_clicked: QWidget | None = None
        self._shut_down = False
        self.system_errors: list[tuple[str, int, str]] = []    # (app, ID, what happened)
        self._alerts: list[QMessageBox] = []
        self.file_dialogs: list = []              # Standard File's, open now
        self.print_dialogs: list = []             # Page Setup and Print, open now
        self.disk_dialogs: list = []              # which disk goes in a drive, open now
        self._router = InputRouter(self)
        QApplication.instance().installEventFilter(self._router)
        self._app_bar_timer = QTimer(self, singleShot=True, interval=250)
        self._app_bar_timer.timeout.connect(self._update_app_bars)

        self.screen_widget: MacScreen | None = None
        self.host_window: ScreenWindow | None = None
        self.origin = QPoint(0, 0)
        if self.config.host == "screen":
            self.size = self.config.screen_size or (640, 480)
            self.host_window = ScreenWindow(self, self.size)
            self.screen_widget = self.host_window.screen_widget
        else:
            avail = QGuiApplication.primaryScreen().availableGeometry()
            self.origin = avail.topLeft()
            self.size = self.config.screen_size or (avail.width(), avail.height())
        has_global_bar = self.config.menu_bar == "global"
        disks = self.config.disks
        self.sound = self._sound_outputs()
        self.system = System(self.size, desk_top=theme.MENU_BAR_HEIGHT if has_global_bar else 0,
                             menu_bar_height=theme.MENU_BAR_HEIGHT,
                             system_file=self.config.system_file,
                             disk_system=system_on(disks[0]) if disks else None,
                             speed=self.config.speed, model=self.config.model,
                             sound=self.sound)
        for disk in disks:
            self.system.files.mount_folder(VolumeFolder(disk))
        self.system.screen.order = lambda: self.processes
        if self.screen_widget is not None:
            self.screen_widget.mirror = self.system.screen
            self.system.screen.on_desk = lambda box: self.bridge.post(self._desk_drawn, box)
        self.system.screen.on_layout = lambda: self.bridge.post(self._laid_out)
        # Keys let go of in another application never come up here.
        QGuiApplication.instance().applicationStateChanged.connect(self._app_state)

        self.global_bar: MenuBarWidget | None = None
        if has_global_bar:
            if self.screen_widget is not None:
                self.global_bar = MenuBarWidget(self, self.screen_widget, rounded=True)
                self.global_bar.setGeometry(0, 0, self.size[0], theme.MENU_BAR_HEIGHT)
            else:
                self.global_bar = MenuBarWidget(self, top_level=True)
                self.global_bar.setGeometry(self.origin.x(), self.origin.y(),
                                            self.size[0], theme.MENU_BAR_HEIGHT)
            # The bar the Finder would have left in the screen buffer, for
            # a game that draws over it with no InitMenus (MacLanding).
            self.system.screen.bar_drawn(self.global_bar.image())

    # =====================================================================
    # Lifecycle
    # =====================================================================
    def show(self) -> None:
        if self.host_window is not None:
            self.host_window.show()
            self.screen_widget.setFocus()
        if self.global_bar is not None:
            self.global_bar.show()

    def launch(self, name: str, main: GuestMain | None, app: MacFile | None = None,
               folder: Folder | None = None, trace: bool = False,
               trace_suppress: tuple[str, ...] = (),
               page_option: int = 0) -> Process:
        """Start a guest.  app is its application file, if it has one: the
        resource file GetNewWindow, GetMenu and friends read from.  With
        no Python main, app's own 68k code runs; trace logs its traps,
        except those named in trace_suppress.
        page_option is Launch's, for CurPageOption."""
        proc = Process(self.system, name, main,
                       EventQueue(lambda: (self.input.mac_pos, self.input.modifiers)),
                       app=app, folder=folder, page_option=page_option)
        if proc.runner is not None:
            proc.runner.trace = trace
            proc.runner.trace_suppress = trace_suppress
        self.launch_order.append(proc)
        self.processes.append(proc)
        self.switch_to(proc)
        proc.start(Toolbox(self, proc), on_exit=lambda p: self.bridge.post(self._process_exited, p))
        return proc

    def launch_application(self, app: MacFile, page_option: int = 0) -> Process | str:
        """Launch an application file for another application (the Launch
        trap): the new process, or why it couldn't start."""
        try:
            return self.launch(app.name, None, app=app, page_option=page_option)
        except (MemoryError, ValueError) as e:
            return str(e)

    def _sound_outputs(self) -> list:
        """Where the Mac's speaker goes: the host's sound output, a WAV file."""
        outputs: list = []
        if self.config.sound:
            from .audio import HostSpeaker      # only now: QtMultimedia has its say as it loads
            speaker = HostSpeaker.open(self)
            if speaker is not None:
                outputs.append(speaker)
        if self.config.record_sound:
            outputs.append(WaveFile(self.config.record_sound))
        return outputs

    def shutdown(self, quit_app: bool = True) -> None:
        if self._shut_down:
            return
        self._shut_down = True
        self.bridge.closed = True
        self.system.screen.close()
        for out in self.sound:
            out.close()
        if self.tracker is not None:
            self.tracker.cancel()
        for proc in self.processes:
            proc.kill()
        app = QApplication.instance()
        app.removeEventFilter(self._router)
        self._app_bar_timer.stop()
        widgets = [*self.views.values(), *self.app_bars.values(), self.global_bar,
                   self.host_window, *self._alerts, *self.file_dialogs,
                   *self.print_dialogs, *self.disk_dialogs]
        for w in widgets:
            if w is not None:
                w.hide()
                w.deleteLater()
        if quit_app:
            app.quit()

    def join(self, timeout: float = 2.0) -> None:
        """After shutdown, wait up to timeout seconds in all for the
        applications' threads to stop: one still in its CPU's native code
        as the interpreter exits can crash it."""
        deadline = time.monotonic() + timeout
        for proc in list(self.launch_order):
            if proc.thread is not None:
                proc.thread.join(max(0.0, deadline - time.monotonic()))

    @property
    def shutting_down(self) -> bool:
        return self._shut_down

    def guest_fault(self, proc: Process, error: Exception) -> None:
        """Something in an app's records broke the GUI side (a bad handle in
        its WindowRecord, say).  Stop that app, as a system error would,
        and keep the others running."""
        if proc.faulted:
            return
        proc.faulted = True
        print(f"macrt: stopping {proc.name}: its records are damaged ({error!r})",
              file=sys.stderr)
        proc.kill()

    def report_system_error(self, proc: Process, error_id: int, message: str) -> None:
        """An application's code stopped with a system error (called on its
        thread).  Say so, as the Mac's bomb alert did; only that
        application stops."""
        self.system_errors.append((proc.name, error_id, message))
        self.bridge.post(self._show_system_error, proc.name, error_id, message)

    def _show_system_error(self, name: str, error_id: int, message: str) -> None:
        self.show_alert(QMessageBox.Icon.Critical, "System Error",
                        f"Sorry, a system error occurred in \u201c{name}\u201d, "
                        f"so it has quit.\n\nID = {error_id}", message)

    def notify(self, name: str, message: str) -> None:
        """A Notification Manager request with a message (called on the
        application's thread): an alert that doesn't stop anyone."""
        self.bridge.post(self.show_alert, QMessageBox.Icon.Information, name, message)

    def show_alert(self, icon: QMessageBox.Icon, title: str, text: str,
                   detail: str = "") -> None:
        """A host alert that leaves the applications running.  With a
        script driving the apps, there may be no one to click it: the
        alert goes to the log instead, and nothing waits on it."""
        if self._shut_down:
            return
        if self.config.scripted:
            print(f"macrt: {title}: {text}" + (f"\n{detail}" if detail else ""),
                  file=sys.stderr)
            return
        box = QMessageBox(icon, title, text, QMessageBox.StandardButton.Ok, self.host_window)
        if detail:
            box.setDetailedText(detail)
        box.setWindowModality(Qt.WindowModality.NonModal)
        box.finished.connect(lambda _: self._alert_closed(box))
        self._alerts.append(box)
        box.show()

    def _alert_closed(self, box: QMessageBox) -> None:
        if box in self._alerts:
            self._alerts.remove(box)
        if not self._alerts and not self.processes:
            self.shutdown()             # the last app crashed; its alert kept the display up

    def _process_exited(self, proc: Process) -> None:
        if proc not in self.processes:
            return
        was_front = self.front_process is proc
        for rec in list(proc.windows):
            self.destroy_view(rec)
        with self.system.lock:
            proc.windows.clear()
            proc.close()
        bar = self.app_bars.pop(proc, None)
        if bar is not None:
            self._retire(bar)
        self.processes.remove(proc)
        self.launch_order.remove(proc)
        if not self.processes:
            if not self._alerts:
                self.shutdown()
            return
        if was_front:
            self.switch_to(self.processes[0], force=True)
        self.refresh_bars()

    # =====================================================================
    # Coordinates.  Mac global coordinates have (0,0) at the top-left of
    # the Mac screen, under the menu bar.
    # =====================================================================
    @property
    def screen_bounds(self) -> Rect:
        return self.system.screen_bits.bounds

    def mac_from_global(self, g: QPoint) -> Point:
        if self.screen_widget is not None:
            g = self.screen_widget.mapFromGlobal(g)
            return Point(g.y(), g.x())
        return Point(g.y() - self.origin.y(), g.x() - self.origin.x())

    def global_from_mac(self, pt: Point) -> QPoint:
        if self.screen_widget is not None:
            return self.screen_widget.mapToGlobal(QPoint(pt.h, pt.v))
        return QPoint(self.origin.x() + pt.h, self.origin.y() + pt.v)

    def parent_pos(self, pt: Point) -> QPoint:
        """Where a hosted widget's top-left goes for a Mac global point."""
        if self.screen_widget is not None:
            return QPoint(pt.h, pt.v)
        return QPoint(self.origin.x() + pt.h, self.origin.y() + pt.v)

    def mac_from_parent_pos(self, pos: QPoint) -> Point:
        if self.screen_widget is not None:
            return Point(pos.y(), pos.x())
        return Point(pos.y() - self.origin.y(), pos.x() - self.origin.x())

    @property
    def desk_top(self) -> int:
        """Where the desk starts: under the global menu bar, if there is one."""
        return theme.MENU_BAR_HEIGHT if self.global_bar is not None else 0

    def place_window(self, bounds: Rect, position: int, proc_id: int,
                     parent: Rect | None = None) -> Rect:
        """Where a WIND, DLOG or ALRT positioning word puts a window."""
        d = describe_proc(proc_id)
        bar = self.config.menu_bar == "window" and chrome.supports_menu_bar(d.kind)
        f = chrome.Frame(kind=d.kind, corner=d.corner, grow=d.grow, zoom=d.zoom, menu_bar=bar)
        top = chrome.insets(f)[1]
        w, h = self.size
        return place(bounds, position, Rect(self.desk_top, 0, h, w), parent, title_bar=top)

    def standard_state(self, rec: WindowRecord) -> Rect:
        """Default zoomed size: the desk, 3 pixels in."""
        f = chrome.Frame.for_window(rec, menu_bar=self.config.menu_bar == "window")
        l, t, r, b = chrome.insets(f)
        s = chrome.shadow(f)
        w, h = self.size
        return Rect(self.desk_top + t + 3, l + 3, h - b - s - 3, w - r - s - 3)

    # =====================================================================
    # Processes
    # =====================================================================
    @property
    def front_process(self) -> Process | None:
        return self.processes[0] if self.processes else None

    def is_front(self, proc: Process) -> bool:
        return self.front_process is proc

    def switch_to(self, proc: Process, force: bool = False) -> None:
        """Make proc the front process, like clicking its window under MultiFinder."""
        old = self.front_process
        if proc not in self.processes or (old is proc and not force):
            return                      # an app that has just quit can't come forward
        self.processes.remove(proc)
        self.processes.insert(0, proc)
        if proc.hidden:
            proc.hidden = False
            self._sync_process(proc)
        if old is not None and old is not proc:
            fw = old.front_window()
            if fw is not None and fw.hilited:
                fw.hilited = False
                self.sync(fw)
                old.events.post_activate(fw, False)
            old.events.post(EventRecord(osEvt, suspendResumeMessage << 24, 0,
                                        self.input.mac_pos, self.input.modifiers))
        fw = proc.front_window()
        if fw is not None and not fw.hilited:
            fw.hilited = True
            self.sync(fw)
            proc.events.post_activate(fw, True)
        message = (suspendResumeMessage << 24) | resumeFlag
        if needs_converting(proc.system, proc.world, self.bridge):
            message |= convertClipboard
        proc.events.post(EventRecord(osEvt, message, 0, self.input.mac_pos, self.input.modifiers))
        self.restack()
        self.focus(fw)
        self.refresh_bars()

    def _sync_process(self, proc: Process) -> None:
        for rec in proc.windows:
            self.sync(rec)

    def z_order(self):
        """(process, window) pairs, front to back, visible windows only."""
        for proc in self.processes:
            if proc.hidden:
                continue
            for rec in proc.windows:
                if rec.visible:
                    yield proc, rec

    def restack(self) -> None:
        self.system.screen.moved()
        for proc in reversed(self.processes):
            bar = self.app_bars.get(proc)
            if bar is not None:
                bar.raise_()
            for rec in reversed(proc.windows):
                view = self.views.get(rec)
                if view is not None:
                    view.raise_()
        if self.global_bar is not None and self.screen_widget is not None:
            self.global_bar.raise_()

    def focus(self, rec: WindowRecord | None) -> None:
        if self.screen_widget is not None:
            self.screen_widget.setFocus()
        elif rec is not None and rec in self.views and self.views[rec].isVisible():
            self.views[rec].activateWindow()

    # =====================================================================
    # Window views
    # =====================================================================
    def create_view(self, rec: WindowRecord) -> MacWindowView:
        view = MacWindowView(self, rec, menu_bar=self.config.menu_bar == "window",
                             parent=self.screen_widget)
        self.views[rec] = view
        view.sync()
        self._app_bar_timer.start()
        return view

    def destroy_view(self, rec: WindowRecord) -> None:
        view = self.views.pop(rec, None)
        if view is None:
            return
        self._retire(view)
        self._app_bar_timer.start()

    def _retire(self, widget: MacWindowView | AppBarWindow) -> None:
        """Hide a window view or bar window for good, first ending any
        tracking that's using it."""
        t = self.tracker
        tracked = getattr(t, "view", None) or getattr(t, "widget", None)
        if tracked is widget or (isinstance(t, MenuTracker) and t.bar is widget.bar):
            t.cancel()
        widget.hide()
        widget.deleteLater()

    def sync(self, rec: WindowRecord) -> None:
        view = self.views.get(rec)
        if view is not None:
            view.sync()
            self._app_bar_timer.start()
        self.system.screen.moved()

    def _laid_out(self) -> None:
        """Show the views of windows a program has made visible by setting
        their visible flags itself, as Cap'n Magneto does, and hide those
        it's made invisible."""
        for rec, view in list(self.views.items()):
            if view.isHidden() == (rec.visible and not rec.process.hidden):
                view.reshape()

    def reshape(self, rec: WindowRecord) -> None:
        """The window's own WDEF has shaped or drawn it."""
        view = self.views.get(rec)
        if view is not None:
            view.reshape()
            self._app_bar_timer.start()
        self.system.screen.moved()

    def repaint_content(self, rec: WindowRecord) -> None:
        view = self.views.get(rec)
        if view is not None:
            view.content.update()

    def _desk_drawn(self, box: Rect) -> None:
        """A program drew on the desk, in box (global), or over the menu bar."""
        if self.screen_widget is not None:
            self.screen_widget.update(qrect(box))
            if self.global_bar is not None and box.top < self.desk_top:
                self.global_bar.update(qrect(box))

    def view_moved(self, view: MacWindowView) -> None:
        """The host moved a window (e.g. a Wayland system move)."""
        l, t = view.content_offset()
        o = view.mac_origin()
        rec = view.rec
        with self.system.lock:
            rec.content = rec.content.moved_to(o.h + l, o.v + t)
        self.system.screen.moved()
        if rec.fit_backing(moved=True):
            with self.system.lock:
                rec.update_pending = True
            rec.process.events.wake()

    def find_window(self, proc: Process, pt: Point) -> tuple[int, WindowRecord | None]:
        if (self.global_bar is not None and pt.v < theme.MENU_BAR_HEIGHT
                and 0 <= pt.h < self.size[0]):
            return inMenuBar, None
        for owner, rec in self.z_order():
            view = self.views.get(rec)
            if view is None:
                continue
            if rec.wdef_shaped:
                # Its structure region is the window; its WDEF says which part.
                if not view.regions()[0].contains(pt):
                    continue
                return (ASK_WDEF, rec) if owner is proc else (inDesk, None)
            o = view.structure_origin()
            code = view.hit(QPoint(pt.h - o.h, pt.v - o.v))
            if code == wNoHit:
                continue
            if owner is not proc:
                return inDesk, None
            if code == wInMenuBar:
                return inMenuBar, None
            return WHIT_TO_PART[code], rec
        bar = self.app_bars.get(proc)
        if bar is not None and bar.isVisible():
            local = bar.mapFromGlobal(self.global_from_mac(pt))
            if bar.bar.geometry().contains(local):
                return inMenuBar, None
        return inDesk, None

    # =====================================================================
    # Trackers
    # =====================================================================
    def _start_tracker(self, tracker) -> Future:
        if self.tracker is not None:
            self.tracker.cancel()
        self.tracker = tracker
        try:
            tracker.start()
        except BaseException:
            self.tracker = None     # or it would swallow every click from now on
            raise
        return tracker.future

    def tracker_finished(self, tracker) -> None:
        if self.tracker is tracker:
            self.tracker = None

    def track_box(self, rec: WindowRecord, part: int) -> Future:
        return self._start_tracker(BoxTracker(self, Future(), self.views[rec], part))

    def track_drag(self, rec: WindowRecord, bounds: Rect, start: Point) -> Future:
        view = self.views[rec]
        return self._start_tracker(DragTracker(self, Future(), view, view.structure_origin(),
                                               self._drag_limit(bounds), start))

    def track_grow(self, rec: WindowRecord, limits: Rect, start: Point) -> Future:
        return self._start_tracker(GrowTracker(self, Future(), self.views[rec], limits, start))

    def end_grow_preview(self, rec: WindowRecord) -> None:
        """The guest didn't take the size GrowWindow gave it."""
        view = self.views.get(rec)
        if view is not None and view.preview_size is not None:
            view.preview_size = None
            view.relayout()

    def track_menu(self, proc: Process, pt: Point, ask=None) -> Future | int:
        bar = self.bar_at(pt) or self.default_bar(proc)
        if bar is None or bar.process is not proc:
            return 0
        proc.last_bar = bar
        return self._start_tracker(MenuTracker(self, bar, Future(), self.config.sticky_menus,
                                               ask))

    def track_popup(self, proc: Process, menu, top: int, left: int, item: int) -> Future | int:
        """PopUpMenuSelect's menu, pulled down with the chosen item's top
        at (top, left) in Mac global coordinates."""
        if menu is None:
            return 0
        return self._start_tracker(PopUpTracker(self, menu, Future(), top, left, item,
                                                getattr(proc, "system_strike", None)))

    def _drag_limit(self, bounds: Rect | None) -> Rect:
        screen = self.screen_bounds
        top = self.desk_top + 4
        if bounds is None:
            return Rect(top, 4, screen.bottom - 4, screen.right - 4)
        return Rect(max(bounds.top, top), bounds.left, bounds.bottom, bounds.right)

    # =====================================================================
    # Menu bars
    # =====================================================================
    def bars_for(self, proc: Process) -> list[MenuBarWidget]:
        bars = []
        if self.global_bar is not None and self.global_bar.process is proc:
            bars.append(self.global_bar)
        for rec in proc.windows:
            view = self.views.get(rec)
            if view is not None and view.bar is not None:
                bars.append(view.bar)
        if proc in self.app_bars:
            bars.append(self.app_bars[proc].bar)
        return bars

    def default_bar(self, proc: Process) -> MenuBarWidget | None:
        if self.global_bar is not None:
            return self.global_bar if self.global_bar.process is proc else None
        # A dialog in front has no bar of its own; use the next window's.
        for rec in proc.windows:
            view = self.views.get(rec)
            if rec.visible and view is not None and view.bar is not None:
                return view.bar
        bar = self.app_bars.get(proc)
        return bar.bar if bar is not None else None

    def bar_at(self, pt: Point) -> MenuBarWidget | None:
        g = self.global_from_mac(pt)

        def over(widget):
            return widget.isVisible() and widget.rect().contains(widget.mapFromGlobal(g))

        if self.global_bar is not None and over(self.global_bar):
            return self.global_bar
        for _, rec in self.z_order():
            view = self.views.get(rec)
            if view is not None and over(view):
                return view.bar if view.bar is not None and over(view.bar) else None
        for bar in self.app_bars.values():
            if over(bar.bar):
                return bar.bar
        return None

    def hilite_menu(self, proc: Process, menu_id: int) -> None:
        bars = self.bars_for(proc)
        if menu_id == 0:
            for bar in bars:
                bar.set_hilite(0)
            return
        bar = proc.last_bar if proc.last_bar in bars else self.default_bar(proc)
        if bar is not None:
            bar.set_hilite(menu_id)
            proc.last_bar = bar

    def refresh_bars(self) -> None:
        if self.global_bar is not None:
            self.global_bar.process = self.front_process
            self.global_bar.update()
        for view in self.views.values():
            if view.bar is not None:
                view.bar.update()
        for bar in self.app_bars.values():
            bar.relayout()
        self._app_bar_timer.start()

    def bar_image(self, proc: Process) -> QImage | None:
        """The menu bar as proc's DrawMenuBar draws it in the screen
        buffer; None if there's no bar on the screen showing its menus."""
        bar = self.global_bar
        if bar is None or bar.process is not proc:
            return None
        return bar.image()

    def _update_app_bars(self) -> None:
        if self.config.menu_bar != "window":
            return
        for i, proc in enumerate(self.launch_order):
            needs = not proc.hidden and not any(
                rec.visible and rec in self.views and self.views[rec].bar is not None
                for rec in proc.windows)
            bar = self.app_bars.get(proc)
            if needs and bar is None:
                bar = AppBarWindow(self, proc, self.screen_widget)
                bar.place(Point(self.desk_top + 4 + i * 50, 8))
                bar.show()
                self.app_bars[proc] = bar
            elif not needs and bar is not None:
                del self.app_bars[proc]
                self._retire(bar)
        self.restack()

    # -- the System 7 Application menu -----------------------------------
    def application_menu_for(self, bar: MenuBarWidget) -> Menu | None:
        mode = self.config.application_menu
        if bar is not self.global_bar or mode == "never":
            return None
        if mode == "auto" and len(self.processes) < 2:
            return None
        front = self.front_process
        others = len(self.processes) > 1
        items = [
            MenuItem(f"Hide {front.name}", enabled=others),
            MenuItem("Hide Others", enabled=others),
            MenuItem("Show All"),
            MenuItem("-", enabled=False),
        ]
        items += [MenuItem(p.name, mark=checkMark if p is front else "")
                  for p in self.launch_order]
        return Menu(kApplicationMenuID, "", items)

    def application_menu_choice(self, item: int) -> None:
        front = self.front_process
        if item == 1:
            nxt = next((p for p in self.processes[1:] if not p.hidden), None)
            if nxt is not None:
                self.switch_to(nxt)
                front.hidden = True
                self._sync_process(front)
        elif item == 2:
            for proc in self.processes[1:]:
                proc.hidden = True
                self._sync_process(proc)
        elif item == 3:
            for proc in self.processes:
                if proc.hidden:
                    proc.hidden = False
                    self._sync_process(proc)
            self.restack()
        elif item >= 5 and item - 5 < len(self.launch_order):
            self.switch_to(self.launch_order[item - 5])
        self.refresh_bars()

    # =====================================================================
    # Input
    # =====================================================================
    def update_mouse(self, gpos: QPoint, qt_mods, down: bool | None) -> None:
        if down is not None:
            self.input.down = down
            if down:
                self.input.press_time = time.monotonic()
        self.input.global_pos = gpos
        self.input.mac_pos = self.mac_from_global(gpos)
        self.input.modifiers = modifiers_from_qt(qt_mods, self.input.down)
        self.system.input.mouse(self.input.mac_pos, self.input.down)
        self.system.input.modifiers(self.input.modifiers)

    def mouse_down(self, obj: QWidget) -> None:
        gpos = self.input.global_pos
        if self.tracker is not None:
            self.tracker.press(gpos)
            return
        owner, widget = self._owner(obj)
        if owner is not None and owner not in self.processes:
            # A window of an app that has quit, not yet gone from the screen.
            self._swallow_release = True
            return
        self._last_clicked = widget
        if owner is not None and owner is not self.front_process:
            self.switch_to(owner)
            self._swallow_release = True
            return
        if isinstance(widget, AppBarWindow) and obj is widget:
            # The bar-only window isn't a Mac window; move it ourselves.
            self._start_tracker(DragTracker(self, Future(), widget, widget.mac_origin(),
                                            self._drag_limit(None), self.input.mac_pos))
            return
        proc = self.front_process
        if proc is not None:
            proc.events.post(EventRecord(mouseDown, 0, 0, self.input.mac_pos, self.input.modifiers))

    def mouse_up(self) -> None:
        if self.tracker is not None:
            # Tracking calls eat the mouseUp, as WaitMouseUp does.
            self.tracker.release(self.input.global_pos)
            return
        if self._swallow_release:
            self._swallow_release = False
            return
        proc = self.front_process
        if proc is not None:
            proc.events.post(EventRecord(mouseUp, 0, 0, self.input.mac_pos, self.input.modifiers))
        clicked = self._last_clicked
        if (self.screen_widget is None and isinstance(clicked, MacWindowView)
                and clicked.rec is not clicked.rec.process.front_window()):
            # The host window manager raised a window the guest hasn't
            # selected (yet); put the Mac stacking order back.
            QTimer.singleShot(0, self.restack)

    def _owner(self, obj: QWidget) -> tuple[Process | None, QWidget | None]:
        w = obj
        while w is not None:
            if isinstance(w, MenuBarWidget):
                return w.process, w
            if isinstance(w, MacWindowView):
                return w.rec.process, w
            if isinstance(w, AppBarWindow):
                return w.process, w
            w = w.parentWidget()
        return None, None

    def _app_state(self, state) -> None:
        if state != Qt.ApplicationState.ApplicationActive:
            self.system.input.release()

    def key_event(self, ev) -> None:
        self.input.modifiers = modifiers_from_qt(ev.modifiers(), self.input.down)
        self.system.input.modifiers(self.input.modifiers)
        msg = key_message(ev, self.system.model.keyboard)
        if msg is not None and not ev.isAutoRepeat():
            self.system.input.key(msg, ev.type() == QEvent.Type.KeyPress)
        if self.tracker is not None:
            if ev.key() == Qt.Key.Key_Escape and ev.type() == QEvent.Type.KeyPress:
                self.tracker.cancel()
            return
        proc = self.front_process
        if proc is None:
            return
        if ev.type() == QEvent.Type.KeyPress:
            what = autoKey if ev.isAutoRepeat() else keyDown
        elif ev.isAutoRepeat():
            return
        else:
            what = keyUp
        if msg is not None:
            proc.events.post(EventRecord(what, msg, 0, self.input.mac_pos, self.input.modifiers))
