"""
Scrap Manager traps (Inside Macintosh I-451): the desk scrap, kept by
macrt.toolbox.scrap and shared with the host's clipboard, and each
application's copy of it, which its ScrapStuff (InfoScrap) describes.
"""

from __future__ import annotations

from ..toolbox import lowmem, scrap
from ..toolbox.scrap import DeskScrap, memFullErr, noErr, noTypeErr
from .dispatch import trap


def bridge(t):
    """The display's bridge to the GUI thread, for the host's clipboard."""
    return getattr(getattr(t.tb, "_d", None), "bridge", None)


def desk(t, from_host: bool = False) -> DeskScrap:
    """The desk scrap, the application's copy brought up to date (with
    what the host has put on its clipboard, from_host)."""
    s = scrap.desk_scrap(t.system)
    if from_host:
        s.from_host(bridge(t))
    s.sync(t.process.world, t.process.heap)
    return s


def put_scrap(t, rtype: bytes, data: bytes) -> int:
    s = scrap.desk_scrap(t.system)
    s.put(bridge(t), rtype, data)
    return s.sync(t.process.world, t.process.heap)


def zero_scrap(t) -> int:
    s = scrap.desk_scrap(t.system)
    s.zero(bridge(t))
    return s.sync(t.process.world, t.process.heap)


def get_scrap(t, rtype: bytes) -> tuple[int, bytes] | None:
    """The desk scrap's first entry of a type: (offset, data), or None."""
    return desk(t, from_host=True).find(rtype)


@trap
def InfoScrap(t):
    desk(t, from_host=True)
    return lowmem.ScrapSize


@trap
def LoadScrap(t):
    desk(t)
    return noErr


@trap
def UnloadScrap(t):
    return desk(t).unload(t.system.files)


@trap
def ZeroScrap(t):
    return zero_scrap(t)


@trap
def PutScrap(t, length, the_type, source):
    return put_scrap(t, t.ostype(the_type), t.mem.read(source, max(length, 0)))


@trap
def GetScrap(t, h_dest, the_type, offset_p):
    """The length of the entry, copied into h_dest unless it's NIL, and
    its offset in the scrap; noTypeErr if there's none."""
    found = get_scrap(t, t.ostype(the_type))
    if found is None:
        return noTypeErr
    offset, data = found
    if h_dest:
        heap = t.system.heap_of(h_dest)
        if heap.is_empty(h_dest):
            ok = heap.realloc_handle(h_dest, len(data))
        else:
            ok = heap.handle_size(h_dest) == len(data) or heap.set_handle_size(h_dest, len(data))
        if not ok:
            return memFullErr
        t.mem.write(heap.deref(h_dest), data)
    t.w32(offset_p, offset)
    return len(data)
