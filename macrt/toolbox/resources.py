"""
Resource Manager (Inside Macintosh: More Macintosh Toolbox, chapter 1).

An open resource file's map is read into a handle in the heap, in the
same format as on disk plus the in-memory fields: a copy of the fork
header, the handle of the next map, the file's reference number, and in
each reference list entry the handle of the resource once it's loaded.
Resources load into handles flagged as resources (locked or purgeable as
their attributes say).  A purged resource is read in again, into the
same handle, the next time it's asked for.

Each application's maps are chained from its TopMapHndl, newest first;
CurMap says where searches start.  Every chain ends at the System file's
map, which lives in the system heap and is shared.  The Python side
keeps only what can't be in guest memory: the bytes of each open fork.

Without a real System file, the system map holds the stand-ins for the
definition procedures (WDEF, MDEF, ...), so GetResource('WDEF', 0)
finds one as it would on a Mac.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass
from typing import TYPE_CHECKING, Callable, Iterator

from ..macfile import Folder, MacFile
from ..memory import ADDRESS_MASK, from_mac, to_mac
from ..resfork import (COMPRESSED_TAG, MAP_HEADER, NO_NAME, REF_ENTRY, TYPE_ENTRY,
                       DecompressError, InvalidForkError,
                       Resource, build_fork, decompress, mapChanged, mapReadOnly, open_fork,
                       resChanged, resCompressed, resLocked, resPreload, resProtected,
                       resPurgeable, resSysHeap, restype)
from . import lowmem
from .files import FileError, fsCurPerm, fsRdPerm
from .lowmem import World
from .memmgr import LOCKED, PURGEABLE, RESOURCE, Heap

if TYPE_CHECKING:
    from .files import FCB, FileManager
    from .files import File as FileNode
    from .system import System

noErr = 0
eofErr = -39
fnfErr = -43
dupFNErr = -48
memFullErr = -108
CantDecompress = -186
resNotFound = -192
resFNotFound = -193
addResFailed = -194
rmvResFailed = -196
resAttrErr = -198
mapReadErr = -199

# A map in memory
M_NEXT = 16
M_REFNUM = 20
M_ATTRS = 22
M_TYPES = 24
M_NAMES = 26

UNWRITTEN = 0xFFFFFF        # data offset of a resource only in memory so far

ResType = str | bytes | int


def _same_name(a: bytes, b: bytes) -> bool:
    return from_mac(a).lower() == from_mac(b).lower()


# ---------------------------------------------------------------------------
# Open forks, host side
# ---------------------------------------------------------------------------
class ResFile:
    """An open resource fork: the File Manager's access path to it, and
    the MacFile it came from, if it was opened as one."""

    def __init__(self, fcb: FCB, manager: ResourceManager, heap: Heap, source: MacFile | None):
        self.fcb = fcb
        self.refnum = fcb.refnum
        self.name = fcb.node.name
        self.manager = manager          # whose chain it's in
        self.heap = heap                # where its map and resources live
        self.source = source

    @property
    def node(self) -> FileNode:
        return self.fcb.node

    @property
    def fork(self) -> bytearray:
        return self.fcb.fork

    @property
    def key(self) -> str:
        return self.source.key if self.source else self.name

    def raw(self, offset: int) -> bytes | None:
        base = struct.unpack_from(">I", self.fork, 0)[0]
        at = base + offset
        if offset == UNWRITTEN or at + 4 > len(self.fork):
            return None
        n = struct.unpack_from(">I", self.fork, at)[0]
        return bytes(self.fork[at + 4:at + 4 + n]) if at + 4 + n <= len(self.fork) else None

    def read(self, offset: int, attrs: int) -> bytes | None:
        data = self.raw(offset)
        if data is not None and attrs & resCompressed and data[:4] == COMPRESSED_TAG:
            data = decompress(data)
        return data


# ---------------------------------------------------------------------------
# A map in guest memory
# ---------------------------------------------------------------------------
class Entry:
    """One reference list entry, where it is in memory right now."""

    def __init__(self, rmap: ResourceMap, addr: int, rtype: bytes):
        self.map = rmap
        self.mem = rmap.mem
        self.addr = addr
        self.type = rtype

    @property
    def id(self) -> int:
        return self.mem.r16s(self.addr)

    @property
    def name_offset(self) -> int:
        return self.mem.r16(self.addr + 2)

    @property
    def attrs(self) -> int:
        return self.mem.r8(self.addr + 4)

    @attrs.setter
    def attrs(self, value: int) -> None:
        self.mem.w8(self.addr + 4, value)

    @property
    def offset(self) -> int:
        return self.mem.r32(self.addr + 4) & 0xFFFFFF

    @property
    def handle(self) -> int:
        return self.mem.r32(self.addr + 8) & ADDRESS_MASK

    @handle.setter
    def handle(self, h: int) -> None:
        self.mem.w32(self.addr + 8, h)

    @property
    def name(self) -> bytes | None:
        return self.map.name_at(self.name_offset)


@dataclass
class Item:
    """An entry taken out of the map, to put back after changing the map."""
    type: bytes
    id: int
    name: bytes | None
    attrs: int
    offset: int
    handle: int


class ResourceMap:
    def __init__(self, mem, handle: int):
        self.mem = mem
        self.handle = handle & ADDRESS_MASK

    @property
    def ptr(self) -> int:
        return self.mem.r32(self.handle) & ADDRESS_MASK

    @property
    def next(self) -> int:
        return self.mem.r32(self.ptr + M_NEXT) & ADDRESS_MASK

    @next.setter
    def next(self, h: int) -> None:
        self.mem.w32(self.ptr + M_NEXT, h)

    @property
    def refnum(self) -> int:
        return self.mem.r16s(self.ptr + M_REFNUM)

    @property
    def attrs(self) -> int:
        return self.mem.r16(self.ptr + M_ATTRS)

    @attrs.setter
    def attrs(self, value: int) -> None:
        self.mem.w16(self.ptr + M_ATTRS, value)

    def name_at(self, offset: int) -> bytes | None:
        if offset == NO_NAME:
            return None
        p = self.ptr
        return self.mem.r_pstr(p + self.mem.r16(p + M_NAMES) + offset)

    def types(self) -> list[tuple[bytes, int, int]]:
        """(type, count, address of its reference list)"""
        mem, p = self.mem, self.ptr
        tl = p + mem.r16(p + M_TYPES)
        n = (mem.r16(tl) + 1) & 0xFFFF
        return [(t, (count + 1) & 0xFFFF, tl + refs)
                for t, count, refs in struct.iter_unpack(">4sHH", mem.read(tl + 2, TYPE_ENTRY * n))]

    def entries(self, rtype: bytes | None = None) -> Iterator[Entry]:
        for t, n, refs in self.types():
            if rtype is None or t == rtype:
                for j in range(n):
                    yield Entry(self, refs + REF_ENTRY * j, t)

    def find(self, rtype: bytes, rid: int) -> Entry | None:
        for t, n, refs in self.types():
            if t == rtype:
                ids = self.mem.read(refs, REF_ENTRY * n)
                for j, (each,) in enumerate(struct.iter_unpack(">h10x", ids)):
                    if each == rid:
                        return Entry(self, refs + REF_ENTRY * j, t)
        return None

    def find_named(self, rtype: bytes, name: bytes) -> Entry | None:
        return next((e for e in self.entries(rtype)
                     if e.name is not None and _same_name(e.name, name)), None)

    def find_handle(self, h: int) -> Entry | None:
        h &= ADDRESS_MASK
        if not h:
            return None
        return next((e for e in self.entries() if e.handle == h), None)

    def items(self) -> list[Item]:
        return [Item(e.type, e.id, e.name, e.attrs, e.offset, e.handle) for e in self.entries()]

    def rewrite(self, heap: Heap, items: list[Item]) -> bool:
        """Lay the map out again with these entries, types in first-seen
        order.  The header fields stay as they are."""
        by_type: dict[bytes, list[Item]] = {}
        for it in items:
            by_type.setdefault(it.type, []).append(it)
        names, refs, types = bytearray(), bytearray(), bytearray()
        refs_at = 2 + TYPE_ENTRY * len(by_type)
        if MAP_HEADER + refs_at + REF_ENTRY * len(items) > 0xFFFF:
            return False                    # past what a map's 16-bit offsets reach
        for t, its in by_type.items():
            types += struct.pack(">4sHH", t, (len(its) - 1) & 0xFFFF, refs_at + len(refs))
            for it in its:
                name_off = NO_NAME
                if it.name is not None:
                    if len(names) >= NO_NAME:
                        return False
                    name_off = len(names)
                    names += bytes([len(it.name[:255])]) + it.name[:255]
                refs += struct.pack(">hHII", it.id, name_off,
                                    (it.attrs & 0xFF) << 24 | (it.offset & 0xFFFFFF), it.handle)
        type_list = struct.pack(">H", (len(by_type) - 1) & 0xFFFF) + types + refs
        head = self.mem.read(self.ptr, M_ATTRS + 2)
        size = MAP_HEADER + len(type_list) + len(names)
        if not heap.set_handle_size(self.handle, size):
            return False
        self.mem.write(self.ptr, head + struct.pack(">HH", MAP_HEADER, MAP_HEADER + len(type_list))
                       + type_list + names)
        return True


def load_map(system: System, heap: Heap, fork: bytes, refnum: int, next_map: int) -> int:
    """Read a fork's map into a new handle in heap.  0 if there's no room."""
    fm = open_fork(fork)                    # every list in the map is inside it
    raw = bytearray(fork[fm.map_offset:fm.map_offset + fm.map_length])
    raw[0:16] = fork[0:16]
    struct.pack_into(">Ih", raw, M_NEXT, next_map, refnum)
    h = heap.new_handle(len(raw))
    if not h:
        return 0
    system.mem.write(heap.deref(h), bytes(raw))
    for e in ResourceMap(system.mem, h).entries():
        e.handle = 0
    return h


# ---------------------------------------------------------------------------
# The Resource Manager, as one application sees it
# ---------------------------------------------------------------------------
class ResourceManager:
    """files is the File Manager it finds and opens files with."""

    def __init__(self, system: System, world: World, heap: Heap, folder: Folder | None = None,
                 files: FileManager | None = None):
        self.system = system
        self.mem = system.mem
        self.world = world
        self.heap = heap
        self.folder = folder
        self.files = files
        # What reads a resource in when an application has patched
        # _ResourceStub: given the map, the entry and force, the handle,
        # or None to read it here.
        self.load_hook: Callable[[ResourceMap, Entry, bool], int | None] | None = None

    def init(self) -> None:
        """A chain holding just the System file."""
        self.world.w32(lowmem.TopMapHndl, self.mem.r32(lowmem.SysMapHndl))
        self.world.w16(lowmem.CurMap, self.sys_refnum)
        self.SetResLoad(True)
        self.error = noErr

    # =====================================================================
    # Globals
    # =====================================================================
    @property
    def sys_refnum(self) -> int:
        return self.mem.r16s(lowmem.SysMap)

    @property
    def error(self) -> int:
        return self.world.r16(lowmem.ResErr)

    @error.setter
    def error(self, err: int) -> None:
        self.world.w16(lowmem.ResErr, err)

    @property
    def top(self) -> int:
        return self.world.r32(lowmem.TopMapHndl) & ADDRESS_MASK

    @property
    def cur_map(self) -> int:
        return self.world.r16(lowmem.CurMap)

    @property
    def res_load(self) -> bool:
        return self.world.read(lowmem.ResLoad, 1) != b"\0"

    def _refnum(self, refnum: int) -> int:
        """Reference number 0 means the System file."""
        return self.sys_refnum if refnum == 0 else refnum

    # =====================================================================
    # The chain
    # =====================================================================
    def maps(self, from_current: bool = False) -> Iterator[ResourceMap]:
        """This application's maps, newest first, or from CurMap down."""
        cur = self.cur_map
        started = not from_current
        h, seen = self.top, set()
        while h and h not in seen:
            seen.add(h)
            m = ResourceMap(self.mem, h)
            started = started or m.refnum == cur
            if started:
                yield m
            h = m.next

    def _map(self, refnum: int) -> ResourceMap | None:
        refnum = self._refnum(refnum)
        return next((m for m in self.maps() if m.refnum == refnum), None)

    def _search(self, one: bool) -> list[ResourceMap]:
        """Where GetResource looks (from CurMap down), or Get1Resource."""
        maps = list(self.maps(from_current=True))
        return maps[:1] if one else maps

    def _file(self, m: ResourceMap) -> ResFile:
        return self.system.fcbs[m.refnum].res

    def _entry(self, h: int) -> Entry | None:
        for m in self.maps():
            e = m.find_handle(h)
            if e is not None:
                return e
        return None

    # =====================================================================
    # Opening and closing
    # =====================================================================
    def open(self, f: MacFile, heap: Heap | None = None) -> int:
        """OpenResFile on a file already found: map it, make it current."""
        return self.open_node(self.system.files.node_for(f), fsCurPerm, heap, f)

    def open_node(self, node: FileNode, perm: int = fsCurPerm, heap: Heap | None = None,
                  source: MacFile | None = None) -> int:
        """Open a file's resource fork through the File Manager and map it.
        A fork opened read-only gets a read-only map.

        A file this application has open already, asked for reading only,
        is opened again with a map of its own, which can be closed without
        taking the first (Technotes TB 19 and 1120).  Asked for writing,
        its map is made current and its number answered (Technote 1120).
        Asked for whatever it can have, as OpenResFile asks, its number is
        answered and the current file stays (Inside Macintosh I-115):
        Font/DA Mover writes to the System file so, though Technote 1120
        found a later System opening it again, read-only."""
        with self.system.lock:
            mine = [m for m in self.maps() if self._file(m).node is node]
            if mine and perm != fsRdPerm:
                m = next((m for m in mine if self._file(m).fcb.write), mine[0])
                if perm != fsCurPerm:
                    self.world.w16(lowmem.CurMap, m.refnum)
                self.error = noErr
                return m.refnum
            if not node.fork(True):
                self.error = eofErr
                return -1
            try:
                fcb = self.system.files.open_fork(node, True, perm, self.files)
            except FileError as e:
                self.error = e.code
                return -1
            heap = heap or self.heap
            try:
                h = load_map(self.system, heap, bytes(fcb.fork), fcb.refnum, self.top)
            except InvalidForkError:
                h = None
            if not h:
                self.system.files.close(fcb.refnum)
                self.error = mapReadErr if h is None else memFullErr
                return -1
            fcb.res = ResFile(fcb, self, heap, source)
            self.world.w32(lowmem.TopMapHndl, h)
            self.world.w16(lowmem.CurMap, fcb.refnum)
            m = ResourceMap(self.mem, h)
            if not fcb.write:
                m.attrs |= mapReadOnly
            for e in list(m.entries()):
                if e.attrs & resPreload:
                    self._load(m, e)
            self.error = noErr
            return fcb.refnum

    def OpenResFile(self, name: str) -> int:
        return self.HOpenResFile(0, 0, name, fsCurPerm)

    def OpenRFPerm(self, name: str, vref: int, perm: int) -> int:
        return self.HOpenResFile(vref, 0, name, perm)

    def HOpenResFile(self, vref: int, dirid: int, name: str, perm: int = fsCurPerm) -> int:
        """IM More Toolbox 1-...: found by the File Manager."""
        with self.system.lock:
            try:
                node = self.files.lookup_file(vref, dirid, name, True)
            except FileError as e:
                self.error = e.code
                return -1
            return self.open_node(node, perm)

    def CreateResFile(self, name: str) -> None:
        self.HCreateResFile(0, 0, name)

    def HCreateResFile(self, vref: int, dirid: int, name: str, creator: bytes = b"????",
                       ftype: bytes = b"????") -> None:
        """An empty resource fork, in a new file if there's none of that name
        ('????' as type and creator, or what FSpCreateResFile says)."""
        with self.system.lock:
            try:
                try:
                    node = self.files.lookup_file(vref, dirid, name)
                except FileError as e:
                    if e.code != fnfErr:
                        raise
                    node = self.files.create(vref, dirid, name)
                    node.type, node.creator = ftype, creator
                    self.files.metadata_changed(node)
                if node.fork(True):
                    self.error = dupFNErr
                    return
                self.system.files.set_fork(node, True, build_fork([]))
            except FileError as e:
                self.error = e.code
                return
            self.error = noErr

    def CloseResFile(self, refnum: int) -> None:
        with self.system.lock:
            refnum = self._refnum(refnum)
            m = self._map(refnum)
            if m is None or refnum == self.sys_refnum:
                self.error = resFNotFound if m is None else noErr
                return
            err = self._update(m)
            heap = self.system.heap_of(m.handle)
            for e in m.entries():
                if e.handle:
                    self.system.heap_of(e.handle).dispose_handle(e.handle)
            self._unlink(m)
            heap.dispose_handle(m.handle)
            self.system.fcbs[refnum].res = None
            try:
                self.system.files.close(refnum)
            except FileError as e:
                err = err or e.code
            self.error = err

    def close_all(self) -> None:
        """When the application quits: close everything but the System file."""
        for m in list(self.maps()):
            if m.refnum != self.sys_refnum:
                self.CloseResFile(m.refnum)

    def _unlink(self, m: ResourceMap) -> None:
        below = m.next
        if self.top == m.handle:
            self.world.w32(lowmem.TopMapHndl, below)
        else:
            for other in self.maps():
                if other.next == m.handle:
                    other.next = below
                    break
        if self.cur_map == m.refnum:
            self.world.w16(lowmem.CurMap, ResourceMap(self.mem, below).refnum if below
                           else self.sys_refnum)

    # =====================================================================
    # The current file
    # =====================================================================
    def CurResFile(self) -> int:
        return self.cur_map

    def UseResFile(self, refnum: int) -> None:
        with self.system.lock:
            m = self._map(refnum)
            if m is None:
                self.error = resFNotFound
                return
            self.world.w16(lowmem.CurMap, m.refnum)
            self.error = noErr

    def HomeResFile(self, h: int) -> int:
        with self.system.lock:
            e = self._entry(h)
            self.error = noErr if e else resNotFound
            return e.map.refnum if e else -1

    def ResError(self) -> int:
        return self.error

    def SetResLoad(self, load: bool) -> None:
        # A byte, as the ROM's ST ResLoad and TST.B ResLoad treat it.
        self.world.write(lowmem.ResLoad, b"\xff" if load else b"\0")

    # =====================================================================
    # Getting resources
    # =====================================================================
    def _load(self, m: ResourceMap, e: Entry, force: bool = True) -> int:
        """The entry's handle, reading the resource in if it isn't, or
        just making an empty handle for it if not force.  On a Mac this
        goes through _ResourceStub, which an application can patch."""
        if self.load_hook is not None:
            h = self.load_hook(m, e, force)
            if h is not None:
                return h
        return self.read_in(m, e, force)

    def read_in(self, m: ResourceMap, e: Entry, force: bool) -> int:
        """_ResourceStub's own way of doing it."""
        h = e.handle
        if h and self.mem.r32(h) & ADDRESS_MASK:
            self.error = noErr
            return h
        attrs = e.attrs
        heap = self.system.sys_heap if attrs & resSysHeap else self._file(m).heap
        if not force:
            if not h:
                h = heap.new_empty_handle()
                e.handle = h
            self.error = noErr if h else memFullErr
            return h
        try:
            data = self._file(m).read(e.offset, attrs)
        except DecompressError:
            self.error = CantDecompress
            return 0
        if data is None:                    # added but never written, then purged
            self.error = resNotFound
            return 0
        if h:
            ok = self.system.heap_of(h).realloc_handle(h, len(data))
        else:
            h = heap.new_handle(len(data))
            ok = bool(h)
        if not ok:
            self.error = memFullErr
            return 0
        self.mem.write(self.mem.r32(h) & ADDRESS_MASK, data)
        flags = RESOURCE | (LOCKED if attrs & resLocked else 0) | \
            (PURGEABLE if attrs & resPurgeable else 0)
        self.system.heap_of(h).set_state(h, flags)
        e.handle = h
        self.error = noErr
        return h

    def get(self, rtype: ResType, rid: int, one: bool = False) -> int:
        with self.system.lock:
            rtype = restype(rtype)
            maps = self._search(one)
            for m in maps:
                e = m.find(rtype, rid)
                if e is not None:
                    return self._load(m, e, self.res_load)
            return self._missed(maps, rtype)

    def _missed(self, maps: list[ResourceMap], rtype: bytes) -> int:
        """ResErr when nothing was found: resNotFound if the last file
        looked in has the type, else noErr, as when the type is in no open
        file at all (IM I-119).  Microsoft File depends on it: its
        datafiles have a FORM 0 but not always a FORM 1, and when the
        System file has no FORMs, a nil FORM 1 with noErr means there's
        no report form."""
        last = maps[-1] if maps else None
        has = last is not None and any(t == rtype for t, _, _ in last.types())
        self.error = resNotFound if has else noErr
        return 0

    def GetResource(self, rtype: ResType, rid: int) -> int:
        return self.get(rtype, rid)

    def peek(self, rtype: ResType, rid: int, system: bool = True) -> bytes | None:
        """What GetResource would find, from CurMap down, without reading
        it into the heap or setting ResErr: what its handle holds if it's
        in memory, else what its file has.  For macrt's own use.  Without
        system, the System file isn't looked in."""
        with self.system.lock:
            rtype = restype(rtype)
            for m in self._search(False):
                if not system and m.refnum == self.sys_refnum:
                    continue
                e = m.find(rtype, rid)
                if e is None:
                    continue
                h = e.handle
                if h and self.mem.r32(h) & ADDRESS_MASK:
                    heap = self.system.heap_of(h)
                    return self.mem.read(heap.deref(h), heap.handle_size(h))
                try:
                    return self._file(m).read(e.offset, e.attrs)
                except DecompressError:
                    return None
            return None

    def peek_ids(self, rtype: ResType) -> set[int]:
        """The IDs of the resources of a type that GetResource could find."""
        with self.system.lock:
            rtype = restype(rtype)
            return {e.id for m in self._search(False) for e in m.entries(rtype)}

    def Get1Resource(self, rtype: ResType, rid: int) -> int:
        return self.get(rtype, rid, one=True)

    def get_named(self, rtype: ResType, name: str | bytes, one: bool = False) -> int:
        with self.system.lock:
            rtype = restype(rtype)
            name = to_mac(name) if isinstance(name, str) else name
            maps = self._search(one)
            for m in maps:
                e = m.find_named(rtype, name)
                if e is not None:
                    return self._load(m, e, self.res_load)
            return self._missed(maps, rtype)

    def GetNamedResource(self, rtype: ResType, name: str | bytes) -> int:
        return self.get_named(rtype, name)

    def Get1NamedResource(self, rtype: ResType, name: str | bytes) -> int:
        return self.get_named(rtype, name, one=True)

    def count(self, rtype: ResType, one: bool = False) -> int:
        with self.system.lock:
            rtype = restype(rtype)
            self.error = noErr
            return sum(n for m in self._search(one) for t, n, _ in m.types() if t == rtype)

    def CountResources(self, rtype: ResType) -> int:
        return self.count(rtype)

    def Count1Resources(self, rtype: ResType) -> int:
        return self.count(rtype, one=True)

    def get_ind(self, rtype: ResType, index: int, one: bool = False) -> int:
        """The index'th resource of the type, counting from 1 through the
        files searched."""
        with self.system.lock:
            rtype = restype(rtype)
            if index >= 1:
                i = index
                for m in self._search(one):
                    for e in m.entries(rtype):
                        i -= 1
                        if i == 0:
                            return self._load(m, e, self.res_load)
            self.error = resNotFound
            return 0

    def GetIndResource(self, rtype: ResType, index: int) -> int:
        return self.get_ind(rtype, index)

    def Get1IndResource(self, rtype: ResType, index: int) -> int:
        return self.get_ind(rtype, index, one=True)

    def types(self, one: bool = False) -> list[bytes]:
        with self.system.lock:
            out: list[bytes] = []
            for m in self._search(one):
                out += [t for t, _, _ in m.types() if t not in out]
            self.error = noErr
            return out

    def CountTypes(self) -> int:
        return len(self.types())

    def Count1Types(self) -> int:
        return len(self.types(one=True))

    def GetIndType(self, index: int) -> bytes:
        t = self.types()
        return t[index - 1] if 1 <= index <= len(t) else b"\0\0\0\0"

    def Get1IndType(self, index: int) -> bytes:
        t = self.types(one=True)
        return t[index - 1] if 1 <= index <= len(t) else b"\0\0\0\0"

    def LoadResource(self, h: int) -> None:
        with self.system.lock:
            e = self._entry(h)
            if e is None:
                self.error = resNotFound
                return
            self._load(e.map, e)

    def ReleaseResource(self, h: int) -> None:
        with self.system.lock:
            e = self._entry(h)
            if e is None:
                self.error = resNotFound
                return
            if e.attrs & resChanged:
                self.error = resAttrErr
                return
            e.handle = 0
            self.system.heap_of(h).dispose_handle(h)
            self.error = noErr

    def DetachResource(self, h: int) -> None:
        """The handle stays; it just isn't a resource any more."""
        with self.system.lock:
            e = self._entry(h)
            if e is None:
                self.error = resNotFound
                return
            if e.attrs & resChanged:
                self.error = resAttrErr
                return
            e.handle = 0
            self.mem.w8(h, self.mem.r8(h) & ~RESOURCE)
            self.error = noErr

    def UniqueID(self, rtype: ResType, one: bool = False) -> int:
        """An unused ID above the range reserved for the system."""
        with self.system.lock:
            rtype = restype(rtype)
            used = {e.id for m in self._search(one) for e in m.entries(rtype)}
            self.error = noErr
            return next(i for i in range(128, 0x8000) if i not in used)

    def Unique1ID(self, rtype: ResType) -> int:
        return self.UniqueID(rtype, one=True)

    # =====================================================================
    # Definition procedures
    # =====================================================================
    def defproc(self, kind: str, def_id: int) -> int:
        """The definition procedure NewWindow or GetMenu finds: the
        application's own if one of its files has it, else the System
        file's.  ResError is left as it was."""
        with self.system.lock:
            err = self.error
            h = self.get(kind, def_id)
            self.error = err
            return h or self.system.defproc(kind, def_id)

    def defproc_id(self, h: int) -> tuple[str, int]:
        """(resource type, ID) of a resource in this application's chain,
        ("", 0) for anything else.  ResError is left alone."""
        with self.system.lock:
            e = self._entry(h)
            return (e.type.decode("mac_roman"), e.id) if e else ("", 0)

    # =====================================================================
    # Information about resources
    # =====================================================================
    def GetResInfo(self, h: int) -> tuple[int, bytes, str]:
        """(ID, type, name)"""
        with self.system.lock:
            e = self._entry(h)
            if e is None:
                self.error = resNotFound
                return 0, b"\0\0\0\0", ""
            self.error = noErr
            return e.id, e.type, from_mac(e.name or b"")

    def SetResInfo(self, h: int, rid: int, name: str | None = None) -> None:
        """Change the ID, and the name unless it's None."""
        with self.system.lock:
            e = self._entry(h)
            if e is None:
                self.error = resNotFound
                return
            if e.attrs & resProtected:
                self.error = resAttrErr
                return
            m = e.map
            items = m.items()
            for it in items:
                if it.handle == e.handle:
                    it.id = rid
                    if name is not None:
                        it.name = to_mac(name)[:255]
            if not m.rewrite(self.system.heap_of(m.handle), items):
                self.error = memFullErr
                return
            m.attrs |= mapChanged
            self.error = noErr

    def GetResAttrs(self, h: int) -> int:
        with self.system.lock:
            e = self._entry(h)
            self.error = noErr if e else resNotFound
            return e.attrs if e else 0

    def SetResAttrs(self, h: int, attrs: int) -> None:
        """Takes effect when the resource is next read in.  resChanged is
        left alone: that's ChangedResource's job."""
        with self.system.lock:
            e = self._entry(h)
            if e is None:
                self.error = resNotFound
                return
            e.attrs = (attrs & ~resChanged) | (e.attrs & resChanged)
            self.error = noErr

    def SizeResource(self, h: int) -> int:
        """The size on disk, without reading it in; -1 if h isn't a
        resource."""
        with self.system.lock:
            e = self._entry(h)
            if e is None:
                self.error = resNotFound
                return -1
            self.error = noErr
            if e.attrs & resChanged and self.mem.r32(h) & ADDRESS_MASK:
                return self.system.heap_of(h).handle_size(h)
            data = self._file(e.map).read(e.offset, e.attrs)
            return len(data) if data is not None else 0

    def MaxSizeRsrc(self, h: int) -> int:
        return self.SizeResource(h)

    def GetResFileAttrs(self, refnum: int) -> int:
        with self.system.lock:
            m = self._map(refnum)
            self.error = noErr if m else resFNotFound
            return m.attrs if m else 0

    def SetResFileAttrs(self, refnum: int, attrs: int) -> None:
        with self.system.lock:
            m = self._map(refnum)
            if m is None:
                self.error = resFNotFound
                return
            m.attrs = attrs
            self.error = noErr

    # =====================================================================
    # Changing resources
    # =====================================================================
    def AddResource(self, h: int, rtype: ResType, rid: int, name: str = "") -> None:
        """Make h a resource in the current file.  It's written out by
        UpdateResFile or CloseResFile."""
        with self.system.lock:
            rtype = restype(rtype)
            m = self._map(self.cur_map)
            ok = (m is not None and h and self.system.heap_of(h).is_handle(h)
                  and self._entry(h) is None and not m.attrs & mapReadOnly)
            if not ok:
                self.error = addResFailed
                return
            items = m.items() + [Item(rtype, rid, to_mac(name)[:255] if name else None,
                                      resChanged, UNWRITTEN, h & ADDRESS_MASK)]
            if not m.rewrite(self.system.heap_of(m.handle), items):
                self.error = addResFailed
                return
            m.attrs |= mapChanged
            self.mem.w8(h, self.mem.r8(h) | RESOURCE)
            self.error = noErr

    def RemoveResource(self, h: int) -> None:
        """Take a resource out of the current file.  The handle stays
        allocated; dispose of it yourself."""
        with self.system.lock:
            m = self._map(self.cur_map)
            e = m.find_handle(h) if m is not None else None
            if e is None or e.attrs & resProtected:
                self.error = rmvResFailed
                return
            h = e.handle
            items = [it for it in m.items() if it.handle != h]
            m.rewrite(self.system.heap_of(m.handle), items)
            m.attrs |= mapChanged
            self.mem.w8(h, self.mem.r8(h) & ~RESOURCE)
            self.error = noErr

    RmveResource = RemoveResource

    def ChangedResource(self, h: int) -> None:
        with self.system.lock:
            e = self._entry(h)
            if e is None:
                self.error = resNotFound
                return
            if e.attrs & resProtected:
                self.error = resAttrErr
                return
            e.attrs |= resChanged
            e.map.attrs |= mapChanged
            self.error = noErr

    def WriteResource(self, h: int) -> None:
        with self.system.lock:
            e = self._entry(h)
            if e is None:
                self.error = resNotFound
                return
            if e.attrs & resChanged:
                self._update(e.map)
            self.error = noErr

    def UpdateResFile(self, refnum: int) -> None:
        with self.system.lock:
            m = self._map(refnum)
            if m is None:
                self.error = resFNotFound
                return
            self.error = self._update(m)

    def _update(self, m: ResourceMap) -> int:
        """Write the fork out again if anything in it changed: changed
        resources from their handles, the rest as they were.  The File
        Manager's result."""
        items = m.items()
        if not (m.attrs & mapChanged or any(it.attrs & resChanged for it in items)):
            return noErr
        f = self._file(m)
        if m.attrs & mapReadOnly or not f.fcb.write:
            return noErr
        out = []
        for it in items:
            loaded = it.handle and self.mem.r32(it.handle) & ADDRESS_MASK
            if it.attrs & resChanged and loaded:
                size = self.system.heap_of(it.handle).handle_size(it.handle)
                data = self.mem.read(self.mem.r32(it.handle) & ADDRESS_MASK, size)
                attrs = it.attrs & ~(resChanged | resCompressed)
            else:
                data, attrs = f.raw(it.offset) or b"", it.attrs & ~resChanged
            out.append(Resource(it.type, it.id, data, it.name, attrs))
        fork = build_fork(out, m.attrs)
        err = noErr
        try:
            self.system.files.set_fork(f.node, True, fork)
        except FileError as e:
            err = e.code
        if f.source is not None:
            f.source.rsrc = fork
        # Point the map at where everything is now.
        for it, off in zip(items, _data_offsets(fork)):
            it.attrs &= ~resChanged
            it.offset = off
        m.rewrite(self.system.heap_of(m.handle), items)
        m.attrs &= ~mapChanged
        return err


def _data_offsets(fork: bytes) -> list[int]:
    """Each reference list entry's data offset, in map order (rsrcfork
    files entries by ID, which loses duplicates)."""
    u16 = lambda at: struct.unpack_from(">H", fork, at)[0]     # noqa: E731
    map_at = struct.unpack_from(">I", fork, 4)[0]
    tl = map_at + u16(map_at + M_TYPES)
    out = []
    for i in range((u16(tl) + 1) & 0xFFFF):
        e = tl + 2 + TYPE_ENTRY * i
        refs = tl + u16(e + 6)
        for j in range((u16(e + 4) + 1) & 0xFFFF):
            out.append(struct.unpack_from(">I", fork, refs + REF_ENTRY * j + 4)[0] & 0xFFFFFF)
    return out
