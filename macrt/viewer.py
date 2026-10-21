"""
Opening a real application before there's a 68k core to run it.

ResourceViewer stands in for the application's code.  It does what the
app's own startup would: puts up its menu bar and first window from its
resources, through the same Toolbox calls (GetNewMBar, GetMenu,
GetNewWindow).  A Resources menu opens the app's other windows, dialogs
and alerts, with dialog items sketched in until there's a Dialog Manager.

describe() is the listing the command line prints.
"""

from __future__ import annotations

import struct

from .macfile import MacFile
from .memory import from_mac
from .resfork import (
    Resource, open_fork, read_fork, resCompressed, resLocked, resPreload, resProtected,
    resPurgeable, resSysHeap,
)
from .toolbox import constants as k
from .toolbox.api import Toolbox
from .toolbox.templates import (
    alertPositionMainScreen, btnCtrl, centerMainScreen, chkCtrl, ctrlItem, editText,
    noAutoCenter, parse_alrt, parse_ditl, parse_dlog, parse_mbar, parse_str, parse_str_list,
    parse_wind, radCtrl, statText,
)
from .toolbox.types import Rect

VIEWER_MENU = 32100
MAX_ENTRIES = 60            # windows, dialogs and alerts listed in the Resources menu

ATTR_NAMES = [(resSysHeap, "sysheap"), (resPurgeable, "purgeable"), (resLocked, "locked"),
              (resProtected, "protected"), (resPreload, "preload"),
              (resCompressed, "compressed")]


def lo_word(x: int) -> int:
    return x & 0xFFFF


def hi_word(x: int) -> int:
    v = (x >> 16) & 0xFFFF
    return v - 0x10000 if v & 0x8000 else v


# ---------------------------------------------------------------------------
# The listing
# ---------------------------------------------------------------------------
def _menu_title(title: str) -> str:
    return "Apple" if title == k.APPLE_MENU_TITLE else title


def _menu_summary(d: bytes) -> str:
    n = d[14]
    title, at = from_mac(d[15:15 + n]), 15 + n
    items = []
    while at < len(d) and d[at]:
        items.append(from_mac(d[at + 1:at + 1 + d[at]]))
        at += 1 + d[at] + 4
    shown = ", ".join(items[:8]) + (", …" if len(items) > 8 else "")
    return f"{_menu_title(title)}: {shown}" if items else _menu_title(title)


def _what(r: Resource) -> str:
    """A few words on what a resource holds, for the kinds we know."""
    d, t = r.data, r.type
    try:
        if t == b"WIND":
            w = parse_wind(d)
            return f"{w.title!r}, {w.bounds.width}x{w.bounds.height}, procID {w.proc_id}"
        if t == b"DLOG":
            g = parse_dlog(d)
            return f"{g.title!r}, {g.bounds.width}x{g.bounds.height}, items in DITL {g.items_id}"
        if t == b"ALRT":
            a = parse_alrt(d)
            return f"{a.bounds.width}x{a.bounds.height}, items in DITL {a.items_id}"
        if t == b"DITL":
            return f"{len(parse_ditl(d))} items"
        if t == b"MENU":
            return _menu_summary(d)
        if t == b"MBAR":
            return "menus " + ", ".join(map(str, parse_mbar(d)))
        if t == b"STR ":
            return repr(parse_str(d))
        if t == b"STR#":
            strings = parse_str_list(d)
            return f"{len(strings)} strings" + (f", first {strings[0]!r}" if strings else "")
        if t == b"SIZE":
            pref, least = struct.unpack_from(">II", d, 2)
            return f"{pref // 1024}K preferred, {least // 1024}K minimum"
        if t == b"vers":
            short = d[6]
            return from_mac(d[7 + short + 1:7 + short + 1 + d[7 + short]]) or \
                from_mac(d[7:7 + short])
        if t == b"CODE" and r.id == 0:
            return f"jump table, {struct.unpack_from('>I', d, 8)[0] // 8} entries"
    except (struct.error, IndexError, ValueError):
        return "(malformed)"
    return ""


def describe(f: MacFile, full: bool = False) -> str:
    """What's in a Mac file: a summary, or with full, every resource."""
    kind = "application" if f.is_application else "file"
    out = [f"{f.name}: {kind}, type {from_mac(f.type)!r}, creator {from_mac(f.creator)!r}"]
    try:
        resources = read_fork(f.rsrc) if f.rsrc else []
    except ValueError as e:
        out.append(f"  resource fork unreadable: {e}")
        return "\n".join(out)
    out.append(f"  data fork {len(f.data):,} bytes, resource fork {len(f.rsrc):,} bytes, "
               f"{len(resources)} resources")
    if not full:
        by_type: dict[bytes, list[int]] = {}
        for r in resources:
            by_type.setdefault(r.type, []).append(r.id)
        for t, ids in by_type.items():
            shown = " ".join(map(str, ids[:12])) + (f" … ({len(ids)})" if len(ids) > 12 else "")
            out.append(f"  {from_mac(t):4} {shown}")
        return "\n".join(out)
    stored = open_fork(f.rsrc).entries      # read_fork drops resCompressed once expanded
    for r, e in zip(resources, stored):
        flags = e.attrs
        attrs = ",".join(name for bit, name in ATTR_NAMES if flags & bit)
        name = f"{from_mac(r.name)!r} " if r.name is not None else ""
        out.append(f"  {from_mac(r.type):4} {r.id:6} {len(r.data):8,}  {attrs:18} "
                   f"{name}{_what(r)}".rstrip())
    return "\n".join(out)


# ---------------------------------------------------------------------------
# The stand-in
# ---------------------------------------------------------------------------
class ResourceViewer:
    """A guest that shows an application's resources the way the app
    would: its menu bar, its windows, its dialogs and alerts."""

    def __init__(self, tb: Toolbox, log=print):
        self.tb = tb
        self.log = log
        self.shown: dict = {}       # window -> (type, id, dialog items)
        self.entries: list[tuple[str, int]] = []
        self.quit = False

    def ids(self, rtype: str) -> list[int]:
        """The IDs of the app's resources of this type, in order."""
        tb = self.tb
        tb.SetResLoad(False)
        try:
            found = {tb.GetResInfo(tb.Get1IndResource(rtype, i))[0]
                     for i in range(1, tb.Count1Resources(rtype) + 1)}
        finally:
            tb.SetResLoad(True)
        return sorted(found)

    def run(self):
        tb = self.tb
        menus = self.ids("MENU")
        self.menu_bar(menus)
        self.viewer_menu(menus)
        tb.DrawMenuBar()
        winds = [e for e in self.entries if e[0] == "WIND"]
        if winds:
            self.open(*winds[0])
        while not self.quit:
            got, ev = tb.WaitNextEvent(k.everyEvent, 30)
            if got:
                self.handle(ev)

    def menu_bar(self, menus: list[int]) -> None:
        tb = self.tb
        bars = self.ids("MBAR")
        if bars:
            tb.SetMenuBar(tb.GetNewMBar(128 if 128 in bars else bars[0]))
            return
        for menu_id in menus:                   # no MBAR: every MENU, in ID order
            m = tb.GetMenu(menu_id)
            if m is not None:
                tb.InsertMenu(m, 0)

    def viewer_menu(self, menus: list[int]) -> None:
        tb = self.tb
        self.menu_id = VIEWER_MENU
        while self.menu_id in menus:
            self.menu_id += 1
        labels = []
        for rtype in ("WIND", "DLOG", "ALRT"):
            for rid in self.ids(rtype):
                if len(self.entries) == MAX_ENTRIES:
                    break
                self.entries.append((rtype, rid))
                labels.append(self.label(rtype, rid))
        m = tb.NewMenu(self.menu_id, "Resources")
        # Placeholders first, then the text: SetItem doesn't read ( ; / ! < ^ as AppendMenu does.
        tb.AppendMenu(m, ";".join(["x"] * len(labels) or ["(No windows or dialogs"]) +
                      ";(-;Quit Viewer")
        for i, label in enumerate(labels, start=1):
            tb.SetItem(m, i, label)
        tb.InsertMenu(m, 0)
        self.menu = m

    def label(self, rtype: str, rid: int) -> str:
        h = self.tb.GetResource(rtype, rid)
        title = ""
        try:
            if rtype == "WIND":
                title = parse_wind(self.tb.contents(h)).title
            elif rtype == "DLOG":
                title = parse_dlog(self.tb.contents(h)).title
        except (struct.error, IndexError):
            pass
        return f"{rtype} {rid}" + (f": {title}" if title else "")

    # -- windows, dialogs, alerts ------------------------------------------
    def open(self, rtype: str, rid: int) -> None:
        tb = self.tb
        try:
            if rtype == "WIND":
                w = tb.GetNewWindow(rid)
                if w is not None:
                    r = self.place(w.content, noAutoCenter, w.proc_id)
                    if r != w.content:
                        tb.MoveWindow(w, r.left, r.top, False)
                    if not w.visible:
                        tb.ShowWindow(w)        # the app would show it when ready
                items = None
            elif rtype == "DLOG":
                t = parse_dlog(tb.contents(tb.GetResource("DLOG", rid)))
                items = self.items(t.items_id)
                w = tb.NewWindow(self.place(t.bounds, t.position, t.proc_id), t.title, True,
                                 t.proc_id, go_away=t.go_away, ref_con=t.ref_con)
            else:
                t = parse_alrt(tb.contents(tb.GetResource("ALRT", rid)))
                items = self.items(t.items_id)
                w = tb.NewWindow(self.place(t.bounds, t.position, k.dBoxProc, alert=True), "",
                                 True, k.dBoxProc, go_away=False)
        except (struct.error, IndexError, ValueError) as e:
            self.log(f"{rtype} {rid} can't be opened: {e}")
            return
        if w is not None:
            self.shown[w] = (rtype, rid, items)

    def place(self, bounds: Rect, position: int, proc_id: int, alert: bool = False) -> Rect:
        """Where the window goes.  Some templates leave a window under the
        menu bar or off the screen for the app's code to move before showing
        it; those are centered, as an alert if it's an alert."""
        tb = self.tb
        screen = tb.ScreenBounds()
        if position == noAutoCenter and (bounds.top < tb.GetMBarHeight() + 2
                                         or bounds.left < screen.left
                                         or bounds.left >= screen.right
                                         or bounds.top >= screen.bottom):
            position = alertPositionMainScreen if alert else centerMainScreen
        return tb.placed(bounds, position, proc_id)

    def items(self, ditl_id: int) -> list:
        h = self.tb.GetResource("DITL", ditl_id)
        if not h:
            self.log(f"DITL {ditl_id} is missing")
            return []
        return parse_ditl(self.tb.contents(h))

    def draw(self, w) -> None:
        tb = self.tb
        rtype, rid, items = self.shown.get(w, ("", 0, None))
        tb.SetPort(w)
        r = w.content
        tb.EraseRect(Rect(0, 0, r.height, r.width))
        if items is None:
            for i, line in enumerate([f"{rtype} {rid}", "",
                                      "macrt can't run this app's code yet:",
                                      "the 68k core comes next."]):
                tb.MoveTo(10, 20 + 16 * i)
                tb.DrawString(line)
            tb.DrawGrowIcon(w)
            return
        for item in items:
            self.draw_item(item)

    def draw_item(self, item) -> None:
        """A sketch of a dialog item, until the Dialog Manager draws them."""
        tb, r, kind = self.tb, item.rect, item.type
        text_top = r.top + max((r.height - 16) // 2, 0)
        if kind == ctrlItem + btnCtrl:
            tb.FrameRect(r)
            tb.TextBox(item.text, Rect(text_top, r.left + 2, r.bottom - 1, r.right - 2), 1)
        elif kind in (ctrlItem + chkCtrl, ctrlItem + radCtrl):
            box = r.top + max((r.height - 12) // 2, 0)
            tb.FrameRect(Rect(box, r.left, box + 12, r.left + 12))
            tb.TextBox(item.text, Rect(text_top, r.left + 16, r.bottom, r.right), 0)
        elif kind == statText:
            tb.TextBox(item.text, r, 0)
        elif kind == editText:
            tb.FrameRect(r.inset(-3, -3))
            tb.TextBox(item.text, r, 0)
        else:
            tb.FrameRect(r)
            name = {32: "ICON", 64: "PICT", 7: "CNTL", 1: "help"}.get(kind, "user item")
            label = f"{name} {item.res_id}" if item.res_id else name
            if r.height >= 18 and r.width >= 30:
                tb.TextBox(label, Rect(text_top, r.left + 1, r.bottom - 1, r.right - 1), 1)

    def close(self, w) -> None:
        self.shown.pop(w, None)
        self.tb.DisposeWindow(w)

    # -- events --------------------------------------------------------------
    def handle(self, ev) -> None:
        tb = self.tb
        if ev.what == k.mouseDown:
            part, w = tb.FindWindow(ev.where)
            if part == k.inMenuBar:
                self.do_menu(tb.MenuSelect(ev.where))
            elif part == k.inDrag:
                tb.DragWindow(w, ev.where, None)
            elif part == k.inGoAway:
                if tb.TrackGoAway(w, ev.where):
                    self.close(w)
            elif part in (k.inZoomIn, k.inZoomOut):
                if tb.TrackBox(w, ev.where, part):
                    tb.ZoomWindow(w, part, False)
            elif part == k.inGrow:
                size = tb.GrowWindow(w, ev.where, Rect(60, 80, 2000, 2000))
                if size:
                    tb.SizeWindow(w, lo_word(size), hi_word(size), True)
            elif part == k.inContent:
                if w is not tb.FrontWindow():
                    tb.SelectWindow(w)
                elif self.shown.get(w, (None, 0, None))[2] is not None:
                    self.close(w)               # a dialog or alert: a click dismisses it
        elif ev.what in (k.keyDown, k.autoKey) and ev.modifiers & k.cmdKey:
            ch = chr(ev.message & 0xFF)
            result = tb.MenuKey(ch)
            if hi_word(result):
                self.do_menu(result)
            elif ch.lower() == "q":
                self.quit = True
        elif ev.what == k.updateEvt:
            tb.BeginUpdate(ev.message)
            self.draw(ev.message)
            tb.EndUpdate(ev.message)
        elif ev.what == k.activateEvt:
            tb.SetPort(ev.message)
            tb.DrawGrowIcon(ev.message)

    def do_menu(self, result: int) -> None:
        tb = self.tb
        menu_id, item = hi_word(result), lo_word(result)
        if menu_id == self.menu_id:
            if item <= len(self.entries):
                self.open(*self.entries[item - 1])
            elif item == tb.CountMItems(self.menu):
                self.quit = True
        elif menu_id:
            m = tb.GetMHandle(menu_id)
            text = tb.GetItem(m, item) if m is not None else ""
            if text.lower().startswith("quit"):
                self.quit = True
            else:
                title = _menu_title(m.title) if m is not None else str(menu_id)
                self.log(f"{title} > {text}: no code to run yet")
        tb.HiliteMenu(0)


def viewer_main(tb: Toolbox) -> None:
    ResourceViewer(tb).run()
