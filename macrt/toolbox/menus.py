"""
Menu Manager data: menus and items, the menu bar, and AppendMenu's
metacharacter parsing.

An application's menus live in its heap as MenuInfo records (Inside
Macintosh I-346), reached through handles: the menu ID, its size, the
MDEF, the enable flags, then menuData, which is the title followed by the
items.  The menu bar is the MenuList, a handle in the application's
MenuList global with one entry per menu (Inside Macintosh V-245).

The System 7 Application menu isn't the application's, so it's a plain
Menu built on the Python side.  Both kinds answer to the same interface.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Iterator

from ..memory import ADDRESS_MASK, from_mac, to_mac
from .constants import APPLE_MENU_TITLE, hMenuCmd
from .lowmem import World, MenuList as MENU_LIST
from .memmgr import Heap

if TYPE_CHECKING:
    from .system import System

STYLE_CODES = {"B": 0x01, "I": 0x02, "U": 0x04, "O": 0x08, "S": 0x10}


@dataclass
class MenuItem:
    text: str
    icon: int = 0
    cmd: str = ""
    mark: str = ""
    style: int = 0
    enabled: bool = True

    @property
    def is_separator(self) -> bool:
        return self.text.startswith("-")

    @property
    def shown(self) -> str:
        """The text as the menu shows it: desk accessories' names start
        with a NUL, which the Mac's fonts draw as nothing."""
        return self.text.replace("\0", "")

    @property
    def key(self) -> str:
        """The Command-key equivalent: cmd, unless it's one of the codes
        below $20 that mean something else."""
        return self.cmd if self.cmd >= " " else ""

    @property
    def submenu(self) -> int:
        """The ID of the menu a hierarchical item opens, 0 for others."""
        return to_mac(self.mark)[0] if self.cmd == hMenuCmd and self.mark else 0


class MenuBase:
    """What the menu bar needs from a menu: id, title, items, enabled."""

    @property
    def is_apple(self) -> bool:
        return self.title == APPLE_MENU_TITLE

    def item(self, index: int) -> MenuItem | None:
        """1-based, like the Menu Manager."""
        items = self.items
        if 1 <= index <= len(items):
            return items[index - 1]
        return None

    def selectable(self, index: int) -> bool:
        it = self.item(index)
        return bool(self.enabled and it and it.enabled and not it.is_separator)


@dataclass(eq=False)
class Menu(MenuBase):
    id: int
    title: str
    items: list[MenuItem] = field(default_factory=list)
    enabled: bool = True


def parse_menu_items(data: str) -> list[MenuItem]:
    """Parse AppendMenu item text: ; or CR separate items, ^ icon, ! mark,
    < style, / command key, ( disabled.  "(-" is a separator line."""
    items = []
    for chunk in data.replace("\r", ";").split(";"):
        if not chunk:
            continue
        item = MenuItem("")
        text = []
        i = 0
        while i < len(chunk):
            c = chunk[i]
            nxt = chunk[i + 1] if i + 1 < len(chunk) else ""
            if c == "^" and nxt:
                item.icon = ord(nxt) - ord("0")
                i += 2
            elif c == "!" and nxt:
                item.mark = nxt
                i += 2
            elif c == "<" and nxt:
                item.style |= STYLE_CODES.get(nxt.upper(), 0)
                i += 2
            elif c == "/" and nxt:
                item.cmd = nxt
                i += 2
            elif c == "(":
                item.enabled = False
                i += 1
            else:
                text.append(c)
                i += 1
        item.text = "".join(text)
        items.append(item)
    return items


# ---------------------------------------------------------------------------
# MenuInfo
# ---------------------------------------------------------------------------
MENU_ID = 0
MENU_WIDTH = 2
MENU_HEIGHT = 4
MENU_PROC = 6
ENABLE_FLAGS = 10
MENU_DATA = 14
LAST_FLAGGED_ITEM = 31      # items past this can't be disabled


def _char(b: int) -> str:
    return from_mac(bytes([b])) if b else ""


def _byte(s: str) -> int:
    return to_mac(s)[0] if s else 0


class MenuInfo(MenuBase):
    """A view of a MenuInfo record through its handle.  The record may
    move, so every access goes through the master pointer."""

    def __init__(self, system: System, handle: int):
        self.system = system
        self.mem = system.mem
        self.handle = handle & ADDRESS_MASK

    @classmethod
    def new(cls, system: System, heap: Heap, menu_id: int, title: str) -> MenuInfo | None:
        """NewMenu: no items, everything enabled."""
        data = to_mac(title)[:255]
        h = heap.new_handle(MENU_DATA + 1 + len(data) + 1, clear=True)
        if not h:
            return None
        menu = cls(system, h)
        p = menu.ptr
        system.mem.w16(p + MENU_ID, menu_id)
        system.mem.w32(p + MENU_PROC, system.defproc("MDEF", 0))
        system.mem.w32(p + ENABLE_FLAGS, 0xFFFFFFFF)
        system.mem.w_pstr(p + MENU_DATA, data)
        return menu

    def dispose(self) -> None:
        self.system.heap_of(self.handle).dispose_handle(self.handle)

    @property
    def ptr(self) -> int:
        return self.mem.r32(self.handle) & ADDRESS_MASK

    @property
    def id(self) -> int:
        return self.mem.r16s(self.ptr + MENU_ID)

    @property
    def menu_proc(self) -> int:
        return self.mem.r32(self.ptr + MENU_PROC)

    @property
    def enable_flags(self) -> int:
        return self.mem.r32(self.ptr + ENABLE_FLAGS)

    @enable_flags.setter
    def enable_flags(self, flags: int) -> None:
        self.mem.w32(self.ptr + ENABLE_FLAGS, flags)

    @property
    def enabled(self) -> bool:
        return bool(self.enable_flags & 1)

    @enabled.setter
    def enabled(self, on: bool) -> None:
        self.enable_flags = (self.enable_flags | 1) if on else (self.enable_flags & ~1)

    @property
    def title(self) -> str:
        return from_mac(self.mem.r_pstr(self.ptr + MENU_DATA))

    def _parse(self) -> tuple[bytes, list[tuple[bytes, bytes]]]:
        """menuData: the title, then (text, icon/key/mark/style) per item."""
        mem, p = self.mem, self.ptr + MENU_DATA
        title = mem.r_pstr(p)
        p += 1 + len(title)
        items = []
        while mem.r8(p):
            text = mem.r_pstr(p)
            p += 1 + len(text)
            items.append((text, mem.read(p, 4)))
            p += 4
        return title, items

    @property
    def items(self) -> list[MenuItem]:
        """A snapshot.  Change items with set_items or change_item."""
        flags = self.enable_flags
        return [MenuItem(from_mac(text), icon, _char(key), _char(mark), style,
                         i > LAST_FLAGGED_ITEM or bool(flags >> i & 1))
                for i, (text, (icon, key, mark, style)) in enumerate(self._parse()[1], start=1)]

    def count(self) -> int:
        """CountMItems"""
        return len(self._parse()[1])

    def set_items(self, items: list[MenuItem]) -> bool:
        title = self._parse()[0]
        data = bytearray([len(title)]) + title
        flags = self.enable_flags
        for i, it in enumerate(items, start=1):
            text = to_mac(it.text)[:255]
            data += bytes([len(text)]) + text
            data += bytes([it.icon & 0xFF, _byte(it.cmd), _byte(it.mark), it.style & 0xFF])
            if i <= LAST_FLAGGED_ITEM:
                flags = (flags | 1 << i) if it.enabled else (flags & ~(1 << i))
        data.append(0)
        heap = self.system.heap_of(self.handle)
        if not heap.set_handle_size(self.handle, MENU_DATA + len(data)):
            return False
        self.mem.write(self.ptr + MENU_DATA, bytes(data))
        self.enable_flags = flags
        return True

    def append(self, items: list[MenuItem]) -> bool:
        return self.set_items(self.items + items)

    def change_item(self, index: int, **changes) -> bool:
        items = self.items
        if not 1 <= index <= len(items):
            return False
        for name, value in changes.items():
            setattr(items[index - 1], name, value)
        return self.set_items(items)

    def __eq__(self, other) -> bool:
        return isinstance(other, MenuInfo) and other.handle == self.handle

    def __hash__(self) -> int:
        return hash(self.handle)

    def __repr__(self) -> str:
        return f"<MenuInfo {self.id} {self.title!r} handle=${self.handle:06X}>"


# ---------------------------------------------------------------------------
# MenuList: lastMenu, lastRight, mbResID, then (menuOH, menuLeft) per menu,
# then the hierarchical section, which stays empty.
# ---------------------------------------------------------------------------
LIST_HEADER = 6
LIST_ENTRY = 6
LIST_MBDF_ID = 4            # mbResID: the MBDF, its variant in the low three bits
HIER_HEADER = 6             # lastHMenu, menuTitleSave


def read_menu_list(mem, h: int) -> list[int]:
    """The menu handles in a menu list, in order."""
    h &= ADDRESS_MASK
    if not h:
        return []
    p = mem.r32(h) & ADDRESS_MASK
    n = mem.r16(p) // LIST_ENTRY
    return [mem.r32(p + LIST_HEADER + LIST_ENTRY * i) & ADDRESS_MASK for i in range(n)]


def read_mbdf_id(mem, h: int) -> int:
    h &= ADDRESS_MASK
    return mem.r16s((mem.r32(h) & ADDRESS_MASK) + LIST_MBDF_ID) if h else 0


def write_menu_list(system: System, h: int, handles: list[int], mbdf_id: int = 0) -> bool:
    size = LIST_HEADER + LIST_ENTRY * len(handles) + HIER_HEADER
    if not h or not system.heap_of(h).set_handle_size(h, size):
        return False
    mem = system.mem
    p = mem.r32(h) & ADDRESS_MASK
    mem.fill(p, size)
    mem.w16(p, LIST_ENTRY * len(handles))
    mem.w16(p + LIST_MBDF_ID, mbdf_id)
    for i, mh in enumerate(handles):
        mem.w32(p + LIST_HEADER + LIST_ENTRY * i, mh)
    return True


def new_menu_list(system: System, heap: Heap, handles: list[int], mbdf_id: int = 0) -> int:
    """A menu list that isn't the menu bar (yet): GetNewMBar, GetMenuBar."""
    h = heap.new_handle(0)
    if h and not write_menu_list(system, h, handles, mbdf_id):
        heap.dispose_handle(h)
        return 0
    return h


class MenuList:
    """An application's menu bar, in InsertMenu order."""

    def __init__(self, system: System, world: World):
        self.system = system
        self.mem = system.mem
        self.world = world

    def init(self, heap: Heap) -> None:
        """InitMenus: an empty menu bar."""
        self.world.w32(MENU_LIST, new_menu_list(self.system, heap, []))

    @property
    def handle(self) -> int:
        return self.world.r32(MENU_LIST) & ADDRESS_MASK

    def handles(self) -> list[int]:
        return read_menu_list(self.mem, self.handle)

    @property
    def mbdf_id(self) -> int:
        """InitProcMenu's MBDF ID (0 is the System's)."""
        return read_mbdf_id(self.mem, self.handle)

    @mbdf_id.setter
    def mbdf_id(self, mbdf_id: int) -> None:
        self.mem.w16((self.mem.r32(self.handle) & ADDRESS_MASK) + LIST_MBDF_ID, mbdf_id)

    def _write(self, handles: list[int], mbdf_id: int | None = None) -> None:
        write_menu_list(self.system, self.handle, handles,
                        self.mbdf_id if mbdf_id is None else mbdf_id)

    def __iter__(self) -> Iterator[MenuInfo]:
        for h in self.handles():
            yield MenuInfo(self.system, h)

    def __len__(self) -> int:
        return len(self.handles())

    def __contains__(self, menu: MenuInfo) -> bool:
        return menu.handle in self.handles()

    def get(self, menu_id: int) -> MenuInfo | None:
        """GetMHandle"""
        return next((m for m in self if m.id == menu_id), None)

    def offset(self, menu_id: int) -> int:
        """Where the menu's entry is in the list, which is how the MBDF
        is told which menu; 0 if it isn't there."""
        ids = [m.id for m in self]
        return LIST_HEADER + LIST_ENTRY * ids.index(menu_id) if menu_id in ids else 0

    def insert(self, menu: MenuInfo, before_id: int = 0) -> None:
        """InsertMenu: before the menu with before_id, or at the end."""
        menus = list(self)
        if menu in menus:
            return
        ids = [m.id for m in menus]
        at = ids.index(before_id) if before_id in ids else len(menus)
        handles = [m.handle for m in menus]
        handles.insert(at, menu.handle)
        self._write(handles)

    def delete(self, menu_id: int) -> None:
        self._write([m.handle for m in self if m.id != menu_id])

    def clear(self) -> None:
        self._write([])

    def set_from(self, list_handle: int) -> None:
        """SetMenuBar: the menu bar becomes a copy of the given list."""
        self._write(read_menu_list(self.mem, list_handle), read_mbdf_id(self.mem, list_handle))

    def copy(self, heap: Heap) -> int:
        """GetMenuBar: a copy of the menu bar as a new list."""
        return new_menu_list(self.system, heap, self.handles(), self.mbdf_id)
