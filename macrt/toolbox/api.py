"""
The Toolbox as a guest application sees it.

Method names, arguments and results follow Inside Macintosh, so the 68k
trap dispatcher can call these directly once it pops arguments off the
guest stack.  Until then, Python guests (see macrt.demo) call them the
same way a Pascal or C Mac program would.

Windows, menus and ports are records in the process's partition of guest
memory (see macrt.toolbox.windows and .menus); these calls read and write
them there.  Resources come from the process's resource chain (see
macrt.toolbox.resources): its application file, files it opened, and the
System file.

Guest code runs on its own thread.  Window and Menu Manager calls hop to
the GUI thread through the display's bridge.  Calls that track the mouse
(MenuSelect, TrackGoAway, DragWindow...) block until the user lets go.
Anything the guest thread reads while the GUI thread may be changing it
(the window list, update regions) is read under the System lock.
"""

from __future__ import annotations

import contextlib
import threading
from functools import partial
from typing import TYPE_CHECKING, Callable

from PyQt6.QtWidgets import QApplication

from ..memory import ADDRESS_MASK
from ..ui import theme
from . import constants as k
from . import lowmem, qdraw
from .bitfont import AppFonts
from .events import EventRecord, ProcessKilled, tick_count
from .resources import ResourceManager
from .menus import MENU_PROC, MenuInfo, new_menu_list, parse_menu_items
from .qdraw import CHICAGO_SYMBOLS
from .quickdraw import BLACK, WHITE, GrafPort
from .templates import parse_mbar, parse_str, parse_str_list, parse_wind
from .types import Point, Rect
from .windows import WindowRecord

if TYPE_CHECKING:
    from ..process import Process
    from ..ui.display import Display

IN_FRONT = -1   # NewWindow's behind = (WindowPtr)-1


class Toolbox:
    def __init__(self, display: Display, process: Process):
        self._d = display
        self._p = process
        self._sys = process.system
        self._mem = process.system.mem
        self._grown: WindowRecord | None = None     # GrowWindow's preview, until SizeWindow
        self.qd = qdraw.QuickDraw(self._sys, qdraw.Fonts(theme.menu_item_font()), self._repaint)
        self.qd.app_fonts = AppFonts(process.resources, qdraw.STANDARD_FAMILIES)
        self.qd.fonts.measured = self.qd.app_fonts.widths
        # Family 1 is ApFontID's, which a program may set: Gemstone Warrior
        # makes it Chicago, and brings a Chicago of its own.
        self.qd.fonts.app_font = self.qd.app_fonts.app_font = \
            lambda: self._mem.r16(lowmem.ApFontID)
        self._sys.screen.attach(process, self._repaint)
        self._dirty: set[WindowRecord] = set()      # windows drawn in since their views showed it
        self._dirty_lock = threading.Lock()
        # The application's own WDEFs, for a 68k application: its runtime's
        # macrt.m68k.wdefs.WindowDefinitions.
        self.wdefs = None

    def _gui(self, fn, *args):
        return self._d.bridge.call(fn, *args)

    def contents(self, h: int) -> bytes:
        """What a handle holds (68k code would just dereference it)."""
        with self._sys.lock:
            heap = self._sys.heap_of(h)
            return self._mem.read(heap.deref(h), heap.handle_size(h))

    @property
    def _the_port(self) -> int:
        """Address of the QuickDraw global thePort; 0(A5) points at it."""
        return self._mem.r32(self._p.partition.a5) & ADDRESS_MASK

    @property
    def _port(self) -> GrafPort | None:
        return self._sys.port_at(self._mem.r32(self._the_port))

    # =====================================================================
    # Event Manager
    # =====================================================================
    def WaitNextEvent(self, mask: int = k.everyEvent, sleep: int = 0,
                      mouse_rgn=None) -> tuple[bool, EventRecord]:
        self._end_grow_preview()
        return self._p.events.get(mask, sleep, self._next_update)

    def GetNextEvent(self, mask: int = k.everyEvent) -> tuple[bool, EventRecord]:
        self._end_grow_preview()
        return self._p.events.get(mask, 0, self._next_update)

    def _end_grow_preview(self) -> None:
        """Done with the event that grew a window, and it wasn't resized
        after all: it goes back to its size."""
        w, self._grown = self._grown, None
        if w is not None:
            self._gui(self._d.end_grow_preview, w)

    def _next_update(self) -> WindowRecord | None:
        """The frontmost window with some of its update region to be seen:
        one the windows in front of it cover wholly is updated once it's
        uncovered, as on a Mac, where it has nothing that can be seen to
        draw.  King's Quest makes its status window behind its picture,
        and shows it when it's updated."""
        with self._sys.lock:
            covered = None
            for w in self._p.windows:
                if not w.visible:
                    continue
                if w.update_pending and (covered is None or not w.update.diff(covered).empty):
                    return w
                covered = w.covers() if covered is None else covered.union(w.covers())
        return None

    def FlushEvents(self, mask: int = k.everyEvent, stop_mask: int = 0) -> None:
        self._p.events.flush(mask, stop_mask)

    def Button(self) -> bool:
        return self._d.input.down

    def StillDown(self) -> bool:
        return self._d.input.down

    def WaitMouseUp(self) -> bool:
        return self._d.input.down

    def GetMouse(self) -> Point:
        """Mouse location, local to the current port."""
        pt = self._d.input.mac_pos
        port = self._port
        return pt if port is None else port.global_to_local(pt)

    def TickCount(self) -> int:
        return tick_count()

    def SysBeep(self, duration: int = 1) -> None:
        self._d.bridge.post(QApplication.beep)

    # =====================================================================
    # Window Manager
    # =====================================================================
    def GetMBarHeight(self) -> int:
        return self._mem.r16s(lowmem.MBarHeight)

    def ScreenBounds(self) -> Rect:
        """qd.screenBits.bounds"""
        return self._sys.screen_bits.bounds

    def NewWindow(self, bounds: Rect, title: str, visible: bool = True,
                  proc_id: int = k.documentProc, behind=IN_FRONT,
                  go_away: bool = True, ref_con: int = 0,
                  storage: int = 0) -> WindowRecord | None:
        def gui():
            p, system = self._p, self._sys
            with system.lock:
                rec = WindowRecord.new(system, p, p.heap, storage, bounds, title, visible,
                                       proc_id, go_away, ref_con)
                if rec is None:
                    return None
                if rec.definition.zoom:
                    rec.std_state = self._d.standard_state(rec)
                    rec.user_state = bounds
                prev = p.front_window()
                if behind == IN_FRONT:
                    p.windows.insert_front(rec)
                elif isinstance(behind, WindowRecord) and behind in p.windows:
                    p.windows.insert_behind(rec, behind)
                else:
                    p.windows.insert_back(rec)
            self._d.create_view(rec)
            self._rehilite(prev)
            p.events.wake()
            return rec
        rec = self._gui(gui)
        if rec is not None and self.wdefs is not None and rec.own_wdef:
            self.wdefs.created(rec)
        return rec

    def GetNewWindow(self, window_id: int, storage: int = 0,
                     behind=IN_FRONT) -> WindowRecord | None:
        """NewWindow from a WIND resource."""
        h = self._p.resources.GetResource("WIND", window_id)
        if not h:
            return None
        t = parse_wind(self.contents(h))
        return self.NewWindow(self.placed(t.bounds, t.position, t.proc_id), t.title, t.visible,
                              t.proc_id, behind, t.go_away, t.ref_con, storage)

    def placed(self, bounds: Rect, position: int, proc_id: int) -> Rect:
        """Where a template's positioning word puts a window of this kind."""
        return self._gui(self._d.place_window, bounds, position, proc_id)

    def _close_window(self, w: WindowRecord, dispose: bool) -> None:
        if self.wdefs is not None and w.own_wdef and w in self._p.windows:
            self.wdefs.disposing(w)

        def gui():
            p = self._p
            if w not in p.windows:
                return
            prev = p.front_window()
            with self._sys.lock:
                p.windows.remove(w)
            p.events.forget(w)
            self._d.destroy_view(w)
            self._rehilite(None if prev is w else prev)
            with self._sys.lock:
                if (self._mem.r32(self._the_port) ^ w.addr) & ADDRESS_MASK == 0:
                    self._mem.w32(self._the_port, 0)
                if dispose:
                    w.dispose()
                else:
                    w.close()
        self._gui(gui)

    def CloseWindow(self, w: WindowRecord) -> None:
        self._close_window(w, dispose=False)

    def DisposeWindow(self, w: WindowRecord) -> None:
        self._close_window(w, dispose=True)

    def FrontWindow(self) -> WindowRecord | None:
        return self._p.front_window()

    def FindWindow(self, pt: Point) -> tuple[int, WindowRecord | None]:
        part, w = self._gui(self._d.find_window, self._p, pt)
        if part == k.ASK_WDEF:
            code = self.wdefs.hit(w, pt) if self.wdefs is not None else k.wInContent
            if code == k.wNoHit:
                return k.inDesk, None
            part = k.WHIT_TO_PART.get(code, k.inContent)
        return part, w

    def SelectWindow(self, w: WindowRecord) -> None:
        """Bring w to the front and activate it: and the window that was in
        front deactivated, whether or not either was highlighted, as the
        Window Manager's CurDeactive and CurActivate have it.  Multiplan
        keeps its formula bar's windows in front of the sheet, which stays
        the highlighted one, and selects the sheet when it's clicked in:
        it goes on with the click once it's had the events."""
        def gui():
            p = self._p
            prev = p.front_window()
            if prev is None or prev is w or w not in p.windows or not self._d.is_front(p):
                if w in p.windows:
                    with self._sys.lock:
                        p.windows.bring_to_front(w)
                self._rehilite(prev)
                return
            with self._sys.lock:
                p.windows.bring_to_front(w)
            for x, on in ((prev, False), (w, True)):
                if x.hilited != on:
                    x.hilited = on
                    self._d.sync(x)
                p.events.post_activate(x, on)
            self._d.restack()
            self._d.focus(w)
        self._gui(gui)

    def BringToFront(self, w: WindowRecord) -> None:
        def gui():
            if w in self._p.windows:
                with self._sys.lock:
                    self._p.windows.bring_to_front(w)
            self._d.restack()
        self._gui(gui)

    def SendBehind(self, w: WindowRecord, behind: WindowRecord | None) -> None:
        """Move w behind behind, or behind all the others for None.  If w
        was the front window the next one becomes active (Inside
        Macintosh I-286)."""
        def gui():
            p = self._p
            if w is behind or w not in p.windows:
                return
            prev = p.front_window()
            with self._sys.lock:
                p.windows.remove(w)
                if behind is not None and behind in p.windows:
                    p.windows.insert_behind(w, behind)
                else:
                    p.windows.insert_back(w)
            self._rehilite(prev)
        self._gui(gui)

    def ShowWindow(self, w: WindowRecord) -> None:
        """Show w, its content erased and to be updated, as PaintOne
        leaves it: nothing drawn in it while it was hidden is kept, and
        SimCity draws its copy protection's question in the window its
        scenario's story was in."""
        def gui():
            if w.visible:
                return
            prev = self._p.front_window()
            with self._sys.lock:
                w.visible = True
                w.update_pending = True
            if w.backing is not None:
                w.backing.clear()
            self._d.sync(w)
            self._rehilite(prev)
            self._p.events.wake()
        self._gui(gui)

    def HideWindow(self, w: WindowRecord) -> None:
        def gui():
            if not w.visible:
                return
            prev = self._p.front_window()
            w.visible = False
            self._d.sync(w)
            self._rehilite(prev)
        self._gui(gui)

    def ShowHide(self, w: WindowRecord, show: bool) -> None:
        """Show or hide w without changing which windows are hilited or
        their order, and without activate events (Inside Macintosh I-438):
        programs keep floating windows so."""
        def gui():
            if w.visible == show:
                return
            with self._sys.lock:
                w.visible = show
                if show:
                    w.update_pending = True
            if show and w.backing is not None:
                w.backing.clear()
            self._d.sync(w)
            self._d.restack()
            self._d.focus(self._p.front_window())
            if show:
                self._p.events.wake()
        self._gui(gui)

    def HiliteWindow(self, w: WindowRecord, hilite: bool) -> None:
        def gui():
            w.hilited = hilite
            self._d.sync(w)
        self._gui(gui)

    @contextlib.contextmanager
    def host_dialog(self):
        """While a dialog of macrt's own (Standard File's, the Printing
        Manager's) is up.  On a Mac it is the front window: the window it
        covers is deactivated, the dialog's ModalDialog takes that event,
        and when the dialog goes the application gets the window's
        activate event.  MacWrite 4.5 hides its scroll bar and dims its
        menus itself before SFPutFile, and shows them again then."""
        def up():
            w = self._p.front_window()
            if w is not None and w.hilited:
                w.hilited = False
                self._d.sync(w)
        self._gui(up)
        try:
            yield
        finally:
            if not self._d.bridge.closed:
                self._gui(self._rehilite, None)

    def _rehilite(self, prev: WindowRecord | None) -> None:
        """After the window list changed: deactivate the old front window,
        activate the new one, and restack.  GUI thread only."""
        p = self._p
        cur = p.front_window()
        if prev is not None and prev is not cur and prev.hilited:
            prev.hilited = False
            self._d.sync(prev)
            p.events.post_activate(prev, False)
        if cur is not None and self._d.is_front(p) and not cur.hilited:
            cur.hilited = True
            self._d.sync(cur)
            p.events.post_activate(cur, True)
        self._d.restack()
        self._d.focus(cur)

    def SetWTitle(self, w: WindowRecord, title: str) -> None:
        def gui():
            w.title = title
            self._d.sync(w)
        self._gui(gui)

    def GetWTitle(self, w: WindowRecord) -> str:
        return w.title

    def SetWRefCon(self, w: WindowRecord, ref_con: int) -> None:
        w.ref_con = ref_con

    def GetWRefCon(self, w: WindowRecord) -> int:
        return w.ref_con

    def _refit(self, w: WindowRecord, moved: bool = False) -> None:
        """The window's Backing once it has moved or changed size."""
        if w.fit_backing(moved):
            with self._sys.lock:
                w.update_pending = True
            self._p.events.wake()

    def MoveWindow(self, w: WindowRecord, h: int, v: int, front: bool = False) -> None:
        def gui():
            with self._sys.lock:
                w.content = w.content.moved_to(h, v)
            self._refit(w, moved=True)
            self._d.sync(w)
        self._gui(gui)
        if front:
            self.SelectWindow(w)

    def SizeWindow(self, w: WindowRecord, width: int, height: int, update: bool = True) -> None:
        if self._grown is w:
            self._grown = None

        def gui():
            with self._sys.lock:
                w.content = w.content.sized(width, height)
                if update:
                    w.update_pending = True
            self._refit(w)
            view = self._d.views.get(w)
            if view is not None:
                view.preview_size = None
            if update:
                self._p.events.wake()
            self._d.sync(w)
        self._gui(gui)

    def DragWindow(self, w: WindowRecord, start: Point, bounds: Rect | None = None) -> None:
        if not self._d.input.modifiers & k.cmdKey:
            self.SelectWindow(w)
        moved = self._gui(self._d.track_drag, w, bounds, start)
        if moved is not None:
            def gui():
                view = self._d.views.get(w)
                if view is None:
                    return
                l, t = view.content_offset()
                with self._sys.lock:
                    w.content = w.content.moved_to(moved.h + l, moved.v + t)
                self._refit(w, moved=True)
                self._d.sync(w)
            self._gui(gui)

    def TrackGoAway(self, w: WindowRecord, pt: Point) -> bool:
        return self._track_box(w, k.wInGoAway)

    def TrackBox(self, w: WindowRecord, pt: Point, part: int) -> bool:
        return self._track_box(w, k.wInZoomIn if part == k.inZoomIn else k.wInZoomOut)

    def _track_box(self, w: WindowRecord, whit: int) -> bool:
        if w.wdef_shaped and self.wdefs is not None:
            return self.wdefs.track(w, whit)        # its WDEF draws the box
        return bool(self._gui(self._d.track_box, w, whit))

    def ZoomWindow(self, w: WindowRecord, part: int, front: bool = False) -> None:
        def gui():
            if part == k.inZoomOut:
                w.user_state = w.content
                target = w.std_state
            else:
                target = w.user_state
            if target is None or target == w.content:
                return
            with self._sys.lock:
                w.content = target
                w.update_pending = True
            self._refit(w, moved=True)
            self._d.sync(w)
            self._p.events.wake()
        self._gui(gui)
        if front:
            self.SelectWindow(w)

    def GrowWindow(self, w: WindowRecord, pt: Point, size_rect: Rect) -> int:
        size = int(self._gui(self._d.track_grow, w, size_rect, pt) or 0)
        if size:
            self._grown = w
        return size

    def BeginUpdate(self, w: WindowRecord) -> None:
        """Until EndUpdate, draw only what needs it: the visRgn is what of
        it is in the update region, which is emptied.  A program that
        validated the rest draws only that, however much it draws."""
        with self._sys.lock:
            vis = qdraw.read_rgn(self._mem, w.vis_rgn)
            w.saved_vis = (vis, w.port_rect)
            b = w.port_bits_bounds
            qdraw.write_rgn(self._sys.heap_of(w.vis_rgn), w.vis_rgn,
                            vis.sect(w.update.offset(b.left, b.top)))
            w.update_pending = False

    def EndUpdate(self, w: WindowRecord) -> None:
        """The visRgn as it was, moved with any SetOrigin since."""
        with self._sys.lock:
            if w.saved_vis is None:
                return
            (vis, then), now = w.saved_vis, w.port_rect
            w.saved_vis = None
            qdraw.write_rgn(self._sys.heap_of(w.vis_rgn), w.vis_rgn,
                            vis.offset(now.left - then.left, now.top - then.top))

    def InvalRect(self, r: Rect | None = None) -> None:
        """Add r (local to the current port; None for all of it) to the
        window's update region."""
        if r is None:
            port = self._port
            if isinstance(port, WindowRecord):
                with self._sys.lock:
                    port.update_pending = True
                self._p.events.wake()
            return
        self.InvalRgn(qdraw.Region.rect(r))

    def InvalRgn(self, rgn: qdraw.Region) -> None:
        self._update_rgn(rgn, True)

    def ValidRect(self, r: Rect) -> None:
        """Take r (local to the current port) out of the window's update
        region: it's drawn already."""
        self._update_rgn(qdraw.Region.rect(r), False)

    def ValidRgn(self, rgn: qdraw.Region) -> None:
        self._update_rgn(rgn, False)

    def _update_rgn(self, rgn: qdraw.Region, add: bool) -> None:
        port = self._port
        if not isinstance(port, WindowRecord):
            return
        with self._sys.lock:
            b = port.port_bits_bounds
            (port.invalidate if add else port.validate)(rgn.offset(-b.left, -b.top))
        if add:
            self._p.events.wake()

    # =====================================================================
    # Menu Manager
    # =====================================================================
    def NewMenu(self, menu_id: int, title: str) -> MenuInfo | None:
        return MenuInfo.new(self._sys, self._p.heap, menu_id, title)

    def GetMenu(self, menu_id: int) -> MenuInfo | None:
        """A menu from a MENU resource.  The resource is the menu: its
        MDEF ID is replaced by a handle to the MDEF, and it's made
        unpurgeable so it stays in memory while it's in the menu bar."""
        rm = self._p.resources
        with self._sys.lock:
            h = rm.GetResource("MENU", menu_id)
            if not h:
                return None
            self._sys.heap_of(h).set_purgeable(h, False)
            menu = MenuInfo(self._sys, h)
            proc = self._mem.r32(menu.ptr + MENU_PROC)
            if rm.GetResInfo(proc)[1] != b"MDEF":         # not patched yet
                mdef_id = self._mem.r16s(menu.ptr + MENU_PROC)
                self._mem.w32(menu.ptr + MENU_PROC, rm.defproc("MDEF", mdef_id))
            rm.error = 0
            return menu

    def GetNewMBar(self, mbar_id: int) -> int:
        """A menu list (not yet the menu bar) of the menus in an MBAR."""
        h = self._p.resources.GetResource("MBAR", mbar_id)
        if not h:
            return 0
        menus = [self.GetMenu(i) for i in parse_mbar(self.contents(h))]
        with self._sys.lock:
            return new_menu_list(self._sys, self._p.heap, [m.handle for m in menus if m],
                                 self._p.menus.mbdf_id)

    def SetMenuBar(self, menu_list: int) -> None:
        self._gui(self._p.menus.set_from, menu_list)

    def GetMenuBar(self) -> int:
        with self._sys.lock:
            return self._p.menus.copy(self._p.heap)

    def DisposeMenu(self, menu: MenuInfo) -> None:
        self._gui(menu.dispose)

    def AppendMenu(self, menu: MenuInfo, data: str) -> None:
        def gui():
            menu.append(parse_menu_items(data))
            self._d.refresh_bars()
        self._gui(gui)

    def InsertMenu(self, menu: MenuInfo, before_id: int = 0) -> None:
        # A Mac draws the menus in the system font, Chicago 12, and an
        # application's own comes first: The Fool's Errand's has its tarot
        # suits for its menus' titles.
        found = self.qd.app_fonts.find(0, 12)
        self._p.system_strike = found[0] if found is not None else None
        self._gui(self._p.menus.insert, menu, before_id)

    def DeleteMenu(self, menu_id: int) -> None:
        self._gui(self._p.menus.delete, menu_id)

    def ClearMenuBar(self) -> None:
        self._gui(self._p.menus.clear)

    def GetMHandle(self, menu_id: int) -> MenuInfo | None:
        return self._p.menus.get(menu_id)

    def DrawMenuBar(self) -> None:
        def gui():
            self._d.refresh_bars()
            return self._d.bar_image(self._p)
        self._sys.screen.bar_drawn(self._gui(gui))

    def menus_changed(self) -> None:
        """Show a menu's new items.  Unlike DrawMenuBar, this leaves what a
        program has drawn over the bar."""
        self._gui(self._d.refresh_bars)

    def MenuSelect(self, pt: Point, opening: Callable[[int, Rect], None] | None = None) -> int:
        """opening(menu ID, where) is called on this thread as each menu
        is about to be pulled down, before its items are shown."""
        if opening is None:
            return int(self._gui(self._d.track_menu, self._p, pt) or 0)
        return int(self._d.bridge.call(self._d.track_menu, self._p, pt, serve=opening) or 0)

    def MenuKey(self, ch: str | int) -> int:
        if isinstance(ch, int):
            ch = bytes([ch & 0xFF]).decode("mac_roman")

        def gui():
            for menu in self._p.menus:
                for i, item in enumerate(menu.items, start=1):
                    if item.key and item.key.lower() == ch.lower() and menu.selectable(i):
                        self._d.hilite_menu(self._p, menu.id)
                        return (menu.id & 0xFFFF) << 16 | i
            return 0
        return self._gui(gui)

    def HiliteMenu(self, menu_id: int) -> None:
        self._gui(self._d.hilite_menu, self._p, menu_id)

    def _change_item(self, menu: MenuInfo, item: int, **changes) -> None:
        def gui():
            if item == 0 and "enabled" in changes:
                menu.enabled = changes["enabled"]
            elif not menu.change_item(item, **changes):
                return
            self._d.refresh_bars()
        self._gui(gui)

    def EnableItem(self, menu: MenuInfo, item: int) -> None:
        self._change_item(menu, item, enabled=True)

    def DisableItem(self, menu: MenuInfo, item: int) -> None:
        self._change_item(menu, item, enabled=False)

    def CheckItem(self, menu: MenuInfo, item: int, checked: bool) -> None:
        self._change_item(menu, item, mark=k.checkMark if checked else k.noMark)

    def SetItemMark(self, menu: MenuInfo, item: int, mark: str) -> None:
        self._change_item(menu, item, mark=mark)

    def SetItem(self, menu: MenuInfo, item: int, text: str) -> None:
        self._change_item(menu, item, text=text)

    def GetItem(self, menu: MenuInfo, item: int) -> str:
        it = menu.item(item)
        return it.text if it else ""

    def CountMItems(self, menu: MenuInfo) -> int:
        return menu.count()

    # =====================================================================
    # QuickDraw (see macrt.toolbox.qdraw).  The current port and its pen
    # are in guest memory.  Coordinates are local to the current port.
    # =====================================================================
    def SetPort(self, port: GrafPort | None) -> None:
        self._mem.w32(self._the_port, port.addr if port is not None else 0)

    def GetPort(self) -> GrafPort | None:
        return self._port

    def MovePortTo(self, left: int, top: int) -> None:
        """Where the port is on the screen: a window's goes with it, and
        what's on the screen stays where it was (as Multiplan has it, moving
        its formula bar's window and then sizing it)."""
        port = self._port
        if port is None:
            return
        b, r = port.port_bits_bounds, port.port_rect
        port.port_bits_bounds = b.offset(r.left - left - b.left, r.top - top - b.top)
        if isinstance(port, WindowRecord):
            def gui():
                self._refit(port)
                self._d.sync(port)
            self._gui(gui)

    def _repaint(self, port: GrafPort) -> None:
        """Have a window's view show what's been drawn, once however many
        calls drew in it since it last did."""
        if not isinstance(port, WindowRecord):
            return
        with self._dirty_lock:
            if port in self._dirty:
                return
            first = not self._dirty
            self._dirty.add(port)
        if first:
            self._d.bridge.post(self._show_dirty)

    def _show_dirty(self) -> None:
        with self._dirty_lock:
            dirty, self._dirty = self._dirty, set()
        for w in dirty:
            self._d.repaint_content(w)

    def MoveTo(self, h: int, v: int) -> None:
        port = self._port
        if port is not None:
            self.qd.move_to(port, h, v)

    def Move(self, dh: int, dv: int) -> None:
        port = self._port
        if port is not None:
            pen = port.pn_loc
            self.qd.move_to(port, pen.h + dh, pen.v + dv)

    def LineTo(self, h: int, v: int) -> None:
        port = self._port
        if port is not None:
            self.qd.line_to(port, h, v)

    def Line(self, dh: int, dv: int) -> None:
        port = self._port
        if port is not None:
            pen = port.pn_loc
            self.qd.line_to(port, pen.h + dh, pen.v + dv)

    def shape(self, verb: str, kind: str, r: Rect, args: tuple = (),
              pat: bytes | None = None) -> None:
        """verb is Frame, Paint, Erase, Invert or Fill; kind Rect, Oval,
        RoundRect (args: ovalWidth, ovalHeight) or Arc (startAngle,
        arcAngle)."""
        if kind == "Rect":
            path_of = qdraw.rect_path
        elif kind == "Oval":
            path_of = qdraw.oval_path
        elif kind == "RoundRect":       # partials, so a picture can tell what they draw
            path_of = partial(qdraw.round_rect_path, oval_w=args[0], oval_h=args[1])
        else:
            path_of = partial(qdraw.arc_path, start=args[0], arc=args[1])
        self.qd.shape(self._port, verb, path_of, r, pat)

    def FrameRect(self, r: Rect) -> None:
        self.shape("Frame", "Rect", r)

    def PaintRect(self, r: Rect) -> None:
        self.shape("Paint", "Rect", r)

    def EraseRect(self, r: Rect) -> None:
        self.shape("Erase", "Rect", r)

    def InvertRect(self, r: Rect) -> None:
        self.shape("Invert", "Rect", r)

    def FillRect(self, r: Rect, pat: bytes) -> None:
        self.shape("Fill", "Rect", r, pat=pat)

    def FrameOval(self, r: Rect) -> None:
        self.shape("Frame", "Oval", r)

    def PaintOval(self, r: Rect) -> None:
        self.shape("Paint", "Oval", r)

    def FrameRoundRect(self, r: Rect, oval_width: int, oval_height: int) -> None:
        self.shape("Frame", "RoundRect", r, (oval_width, oval_height))

    def ScrollRect(self, r: Rect, dh: int, dv: int) -> qdraw.Region:
        """Returns the area left behind, which ScrollRect's caller passes
        an updateRgn for."""
        return self.qd.scroll_rect(self._port, r, dh, dv)

    def DrawString(self, s: str) -> None:
        self.qd.draw_text(self._port, s.translate(CHICAGO_SYMBOLS))

    def StringWidth(self, s: str) -> int:
        return self.qd.text_width(self._port, s.translate(CHICAGO_SYMBOLS))

    def MeasureText(self, s: str) -> list[int]:
        """Where the pen is after each character of s, from 0 before it."""
        return self.qd.text_positions(self._port, s.translate(CHICAGO_SYMBOLS))

    def GetFontInfo(self) -> tuple[int, int, int, int]:
        """(ascent, descent, widMax, leading) of the port's font."""
        return self.qd.font_info(self._port)

    def TextBox(self, text: str, r: Rect, just: int = 0) -> None:
        self.qd.text_box(self._port, text.translate(CHICAGO_SYMBOLS), r, just)

    def DrawGrowIcon(self, w: WindowRecord) -> None:
        """Scroll bar outlines plus the size box; an empty box when inactive.
        They're drawn in the window's port, so its clipRgn clips them:
        Shadowgate clips to the size box's column to leave out the line
        above the bottom of its text."""
        if not w.definition.grow or w.backing is None:
            return
        # A 16-pixel scroll bar overlaps the window's frame by one, so its
        # edge, and the size box's, is 15 pixels in from the port's.
        r = w.port_rect
        g = theme.GROW_BOX
        x, y = r.right - g, r.bottom - g
        with self.qd.painting(w) as p:
            if p is None:
                return
            p.fillRect(x, r.top, 1, r.height, BLACK)
            p.fillRect(r.left, y, r.width, 1, BLACK)
            p.fillRect(x + 1, y + 1, g - 1, g - 1, WHITE)
            if w.hilited:
                for bx, by, bs in ((x + 3, y + 3, 6), (x + 5, y + 5, 8)):
                    p.fillRect(bx, by, bs, bs, WHITE)
                    p.fillRect(bx, by, bs, 1, BLACK)
                    p.fillRect(bx, by + bs - 1, bs, 1, BLACK)
                    p.fillRect(bx, by, 1, bs, BLACK)
                    p.fillRect(bx + bs - 1, by, 1, bs, BLACK)

    # =====================================================================
    # Toolbox Utilities
    # =====================================================================
    def GetString(self, string_id: int) -> int:
        """A handle to a 'STR ' resource."""
        return self._p.resources.GetResource("STR ", string_id)

    def GetIndString(self, list_id: int, index: int) -> str:
        """The index'th string (from 1) in a 'STR#', "" if there isn't one."""
        h = self._p.resources.GetResource("STR#", list_id)
        strings = parse_str_list(self.contents(h)) if h else []
        return strings[index - 1] if 1 <= index <= len(strings) else ""

    def string_of(self, h: int) -> str:
        """The Pascal string a handle holds, as from GetString."""
        return parse_str(self.contents(h)) if h else ""

    # =====================================================================
    # Process Manager / Segment Loader
    # =====================================================================
    def ExitToShell(self) -> None:
        raise ProcessKilled()


# Resource Manager calls go straight to the process's ResourceManager.
RESOURCE_CALLS = (
    "OpenResFile", "CreateResFile", "CloseResFile", "CurResFile", "UseResFile",
    "HomeResFile", "ResError", "SetResLoad", "UpdateResFile",
    "GetResFileAttrs", "SetResFileAttrs",
    "GetResource", "Get1Resource", "GetNamedResource", "Get1NamedResource",
    "CountResources", "Count1Resources", "GetIndResource", "Get1IndResource",
    "CountTypes", "Count1Types", "GetIndType", "Get1IndType", "UniqueID", "Unique1ID",
    "LoadResource", "ReleaseResource", "DetachResource",
    "GetResInfo", "SetResInfo", "GetResAttrs", "SetResAttrs", "SizeResource", "MaxSizeRsrc",
    "AddResource", "RemoveResource", "RmveResource", "ChangedResource", "WriteResource",
)


def _resource_call(name: str):
    def call(self: Toolbox, *args):
        return getattr(self._p.resources, name)(*args)
    call.__name__ = call.__qualname__ = name
    call.__doc__ = getattr(ResourceManager, name).__doc__
    return call


for _name in RESOURCE_CALLS:
    setattr(Toolbox, _name, _resource_call(_name))
