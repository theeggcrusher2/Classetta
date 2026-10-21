"""
The Scrap Manager's desk scrap (Inside Macintosh I-451), shared by every
application and, for 'TEXT', with the host's clipboard.

The desk scrap is kept here as a list of (type, data) entries.  Each
application also has its own copy, laid out as on a Mac (for each entry
its type, its length and its data, padded to an even length), in a handle
in its own heap that its ScrapStuff points at.  ScrapStuff is a switched
low-memory global, as under MultiFinder, which hands each application the
scrap as it comes forward; here the copy is brought up to date whenever
the application calls the Scrap Manager.

Text goes to and comes from the host clipboard: Mac Roman with returns
here, Unicode with newlines there.  The clipboard belongs to the GUI
thread, so it's reached through the display's bridge, and watched for
changes the host makes.
"""

from __future__ import annotations

import struct
import threading
import weakref
from typing import TYPE_CHECKING

from PyQt6.QtWidgets import QApplication

from ..memory import from_mac, to_mac
from . import lowmem
from .files import File, FileError, FileSystem

if TYPE_CHECKING:
    from ..ui.bridge import GuiBridge
    from .lowmem import World
    from .memmgr import Heap
    from .system import System

noErr = 0
noScrapErr = -100
noTypeErr = -102
memFullErr = -108
TEXT = b"TEXT"
CLIPBOARD_FILE = "Clipboard File"


def to_host(data: bytes) -> str:
    return from_mac(data).replace("\r", "\n")


def from_host(text: str) -> bytes:
    return to_mac(text.replace("\r\n", "\r").replace("\n", "\r"))


def layout(entries: list[tuple[bytes, bytes]]) -> bytes:
    """The scrap as it is in memory."""
    out = bytearray()
    for rtype, data in entries:
        out += rtype + struct.pack(">I", len(data)) + data + bytes(len(data) & 1)
    return bytes(out)


class DeskScrap:
    """The desk scrap.  count is ScrapCount, which ZeroScrap changes;
    version changes with every change, PutScrap's too."""

    def __init__(self, system: System):
        self.lock = threading.RLock()
        self.entries: list[tuple[bytes, bytes]] = []
        self.count = 0
        self.version = 0
        self._copies: weakref.WeakKeyDictionary = weakref.WeakKeyDictionary()  # World -> version
        name = to_mac(CLIPBOARD_FILE)
        self.name_ptr = system.sys_heap.new_ptr(len(name) + 1)
        system.mem.w_pstr(self.name_ptr, name)
        self._mem = system.mem
        self._host_text = ""                    # the host clipboard as last seen or set
        self._host_dirty = True
        self._bridge: GuiBridge | None = None

    # -- the host clipboard ------------------------------------------------
    def _watch(self, bridge: GuiBridge) -> None:
        if self._bridge is bridge:
            return
        self._bridge = bridge
        self._host_dirty = True
        bridge.call(lambda: QApplication.clipboard().dataChanged.connect(self._host_changed))

    def _host_changed(self) -> None:            # on the GUI thread
        self._host_dirty = True

    def from_host(self, bridge: GuiBridge | None) -> None:
        """If the host's clipboard has changed, it's the desk scrap now: its
        text, if any, as 'TEXT', a new scrapCount."""
        if bridge is None:
            return
        self._watch(bridge)
        if not self._host_dirty:
            return
        self._host_dirty = False
        text = bridge.call(lambda: QApplication.clipboard().text())
        with self.lock:
            if text == self._host_text:
                return
            self._host_text = text
            self.entries = [(TEXT, from_host(text))] if text else []
            self.count += 1
            self.version += 1

    def _set_host(self, bridge: GuiBridge | None, text: str) -> None:
        if bridge is None:
            return
        self._watch(bridge)
        with self.lock:
            self._host_text = text

        def gui():
            clipboard = QApplication.clipboard()
            if text:
                clipboard.setText(text)
            else:
                clipboard.clear()
        bridge.call(gui)

    # -- the Scrap Manager -------------------------------------------------
    def zero(self, bridge: GuiBridge | None) -> None:
        """ZeroScrap"""
        with self.lock:
            self.entries = []
            self.count += 1
            self.version += 1
        self._set_host(bridge, "")

    def put(self, bridge: GuiBridge | None, rtype: bytes, data: bytes) -> None:
        """PutScrap: another entry.  The first 'TEXT' is the host's text."""
        with self.lock:
            first_text = rtype == TEXT and all(t != TEXT for t, _ in self.entries)
            self.entries.append((rtype, data))
            self.version += 1
        if first_text:
            self._set_host(bridge, to_host(data))

    def find(self, rtype: bytes) -> tuple[int, bytes] | None:
        """GetScrap's answer: the first entry of a type, as (offset of its
        data in the scrap, data)."""
        with self.lock:
            at = 0
            for t, data in self.entries:
                if t == rtype:
                    return at + 8, data
                at += 8 + len(data) + (len(data) & 1)
        return None

    def stale(self, world: World, bridge: GuiBridge | None) -> bool:
        """Whether an application's copy is out of date: someone else, or
        the host, has changed the scrap since it last looked."""
        self.from_host(bridge)
        with self.lock:
            return self._copies.get(world, 0) != self.version

    def unload(self, files: FileSystem) -> int:
        """UnloadScrap: the scrap written to the Clipboard File in the
        System Folder, as a Mac keeps it between applications (IM I-457);
        programs look for the file after (SuperPaint does).  It stays in
        memory too."""
        with self.lock:
            data = layout(self.entries)
        with files.lock:
            try:
                f = files.system_folder.child(CLIPBOARD_FILE)
                if f is None:
                    f = files.make_file(files.system_folder, CLIPBOARD_FILE, b"CLIP", b"MACS")
                if isinstance(f, File):
                    files.set_fork(f, False, data)
            except FileError as e:
                return e.code
        return noErr

    def sync(self, world: World, heap: Heap) -> int:
        """Bring an application's ScrapStuff and its copy of the scrap up to
        date.  noErr, or memFullErr if its heap has no room for the copy."""
        with self.lock:
            h = world.r32(lowmem.ScrapHandle)
            have = h and heap.is_handle(h) and not heap.is_empty(h)
            if have and self._copies.get(world) == self.version:
                return noErr
            data = layout(self.entries)
            if have:
                if heap.handle_size(h) != len(data) and not heap.set_handle_size(h, len(data)):
                    return memFullErr
            else:
                h = heap.new_handle(len(data))
                if not h:
                    return memFullErr
            self._mem.write(heap.deref(h), data)
            world.w32(lowmem.ScrapSize, len(data))
            world.w32(lowmem.ScrapHandle, h)
            world.w16(lowmem.ScrapCount, self.count)
            world.w16(lowmem.ScrapState, 1)
            world.w32(lowmem.ScrapName, self.name_ptr)
            self._copies[world] = self.version
            return noErr


def needs_converting(system: System, world: World, bridge: GuiBridge | None) -> bool:
    """A resume event's convertClipboard (Programmer's Guide to MultiFinder):
    the application coming forward should read the scrap again.  For the
    GUI thread, so without the system's lock."""
    s = getattr(system, "desk_scrap", None)
    return s is not None and s.stale(world, bridge)


def desk_scrap(system: System) -> DeskScrap:
    with system.lock:
        s = getattr(system, "desk_scrap", None)
        if s is None:
            s = system.desk_scrap = DeskScrap(system)
        return s
