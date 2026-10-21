"""
Icons: PlotIcon, the Icon Utilities ($ABC9, IconDispatch) and colour
icons (GetCIcon, PlotCIcon, DisposeCIcon).  The icons themselves are
macrt.toolbox.icons'.

An icon suite is a handle of our own making, as the real one is
private: its label, how many icons, then each icon's type and handle.
"""

from __future__ import annotations

import struct

from ..memory import ADDRESS_MASK
from ..toolbox.icons import (ICON_TYPES, LARGE, SELECTORS, SIZES, SMALL, CIcon, Icon, atNone,
                             best_size, bitmap_icon, place, suite_icon, ttNone)
from ..toolbox.masks import mask_region
from ..toolbox.memmgr import RESOURCE, memFullErr, noErr
from ..toolbox.pict import PictError
from ..toolbox.qdraw import Region, srcCopy
from ..toolbox.resources import resNotFound
from ..toolbox.types import Rect
from .dispatch import trap
from .glue_toolbox import _port, _set_rgn

paramErr = -50
noSuchIconErr = -2582
SUITE_HEADER = 4            # label, count
SUITE_ENTRY = 8             # type, handle


def _data(t, h: int) -> bytes | None:
    """What a handle holds, loading it again if it's a purged resource;
    None for NIL or an empty handle."""
    if not h:
        return None
    if not t.mem.r32(h) & ADDRESS_MASK:
        if not t.system.heap_of(h).get_state(h) & RESOURCE:
            return None
        t.process.resources.LoadResource(h)
        if not t.mem.r32(h) & ADDRESS_MASK:
            return None
    return t.tb.contents(h) or None


def _plot(t, r: Rect, align: int, transform: int, icon: Icon | None) -> None:
    """Draw an icon in r of the current port: srcCopy through its mask."""
    port = _port(t)
    if port is None or icon is None or r.empty:
        return
    icon = icon.transformed(transform)
    dst = place(r, icon.width, icon.height, align)
    mask = icon.mask_image()
    rgn = mask_region(mask, dst) if mask is not None else None
    if rgn is None or not rgn.empty:
        t.tb.qd.draw_bits(port.addr + 2, dst, icon.image(), srcCopy, rgn, port)


def _region(icon: Icon | None, r: Rect, align: int) -> Region:
    """Where an icon would draw in r: its mask, placed."""
    if icon is None or r.empty:
        return Region()
    dst = place(r, icon.width, icon.height, align)
    mask = icon.mask_image()
    return mask_region(mask, dst) if mask is not None else Region.rect(dst)


# =========================================================================
# Plain icons (Inside Macintosh I-473)
# =========================================================================
@trap
def PlotIcon(t, r, h):
    """CopyBits of a 32-by-32 ICON into r, srcCopy, scaled to fit."""
    try:
        data = _data(t, h)
    except ValueError:
        # The ROM only dereferences the handle: Leisure Suit Larry's is a
        # pointer to its bits on the stack.
        data = t.mem.read(t.mem.r32(h) & ADDRESS_MASK, 128) if t.mem.r32(h) else None
    icon = bitmap_icon(data, LARGE, masked=False) if data else None
    _plot(t, t.rect(r), atNone, ttNone, icon)


@trap
def PlotIconHandle(t, r, align, transform, h):
    """An ICON (no mask) or an ICN# (with one), by the handle's size."""
    data = _data(t, h)
    icon = bitmap_icon(data, LARGE, masked=len(data) >= 256) if data else None
    if icon is None:
        return paramErr
    _plot(t, t.rect(r), align, transform, icon)
    return noErr


@trap
def PlotSICNHandle(t, r, align, transform, h):
    """A SICN's first icon, with its second as the mask if there is one."""
    data = _data(t, h)
    icon = bitmap_icon(data, SMALL) if data else None
    if icon is None:
        return paramErr
    _plot(t, t.rect(r), align, transform, icon)
    return noErr


# =========================================================================
# Icon families, by resource ID or as suites (More Macintosh Toolbox 5)
# =========================================================================
def _family(get, r: Rect) -> Icon | None:
    """The icon of a family to draw in r; get(type) gives the data."""
    cache: dict[str, bytes | None] = {}

    def cached(kind: str) -> bytes | None:
        if kind not in cache:
            cache[kind] = get(kind)
        return cache[kind]
    size = best_size(r, lambda size: any(cached(kind) for kind in SIZES[size]))
    return suite_icon(cached, size) if size is not None else None


def _id_family(t, rid: int):
    return lambda kind: _data(t, t.process.resources.GetResource(kind, rid))


def _suite(t, h: int) -> dict[str, int] | None:
    """A suite's icons: type -> handle; None if h isn't a suite."""
    data = _data(t, h)
    if data is None or len(data) < SUITE_HEADER:
        return None
    count = struct.unpack_from(">H", data, 2)[0]
    out = {}
    for i in range(min(count, (len(data) - SUITE_HEADER) // SUITE_ENTRY)):
        kind, handle = struct.unpack_from(">4sL", data, SUITE_HEADER + SUITE_ENTRY * i)
        out[kind.decode("mac_roman")] = handle
    return out


def _set_suite(t, h: int, icons: dict[str, int], label: int | None = None) -> bool:
    heap = t.system.heap_of(h)
    if label is None:
        label = t.mem.r16(heap.deref(h))
    data = struct.pack(">hH", label, len(icons)) + b"".join(
        struct.pack(">4sL", kind.encode("mac_roman"), handle) for kind, handle in icons.items())
    if not heap.set_handle_size(h, len(data)):
        return False
    t.mem.write(heap.deref(h), data)
    return True


def _suite_family(t, h: int):
    icons = _suite(t, h) or {}
    return lambda kind: _data(t, icons.get(kind, 0))


@trap
def PlotIconID(t, r, align, transform, rid):
    rect = t.rect(r)
    icon = _family(_id_family(t, rid), rect)
    if icon is None:
        return resNotFound
    _plot(t, rect, align, transform, icon)
    return noErr


@trap
def PlotIconSuite(t, r, align, transform, suite):
    rect = t.rect(r)
    if _suite(t, suite) is None:
        return paramErr
    _plot(t, rect, align, transform, _family(_suite_family(t, suite), rect))
    return noErr


@trap
def NewIconSuite(t, suite_ptr):
    h = t.process.heap.new_handle(SUITE_HEADER, clear=True)
    t.w32(suite_ptr, h)
    return noErr if h else memFullErr


@trap
def GetIconSuite(t, suite_ptr, rid, selector):
    """A suite of the icons of an ID the selector asks for that there are."""
    h = t.process.heap.new_handle(SUITE_HEADER, clear=True)
    t.w32(suite_ptr, h)
    if not h:
        return memFullErr
    rm = t.process.resources
    icons = {}
    for kind in ICON_TYPES:
        if selector & SELECTORS[kind]:
            res = rm.GetResource(kind, rid)
            if res:
                icons[kind] = res
    _set_suite(t, h, icons)
    return noErr


@trap
def AddIconToSuite(t, data, suite, kind):
    icons = _suite(t, suite)
    if icons is None:
        return paramErr
    icons[t.ostype(kind).decode("mac_roman")] = data
    return noErr if _set_suite(t, suite, icons) else memFullErr


@trap
def GetIconFromSuite(t, data_ptr, suite, kind):
    icons = _suite(t, suite)
    h = icons.get(t.ostype(kind).decode("mac_roman"), 0) if icons is not None else 0
    t.w32(data_ptr, h)
    return noErr if h else paramErr


@trap
def DisposeIconSuite(t, suite, dispose_data):
    """The suite, and with dispose_data the icons in it that aren't
    resources (those stay the Resource Manager's)."""
    icons = _suite(t, suite)
    if icons is None:
        return paramErr
    if dispose_data:
        for h in set(icons.values()):
            if h and not t.system.heap_of(h).get_state(h) & RESOURCE:
                t.system.heap_of(h).dispose_handle(h)
    t.system.heap_of(suite).dispose_handle(suite)
    return noErr


@trap
def SetSuiteLabel(t, suite, label):
    icons = _suite(t, suite)
    if icons is None:
        return paramErr
    _set_suite(t, suite, icons, label)
    return noErr


@trap
def GetSuiteLabel(t, suite):
    return t.mem.r16(t.system.heap_of(suite).deref(suite)) if _suite(t, suite) is not None else 0


@trap
def IconIDToRgn(t, rgn, r, align, rid):
    rect = t.rect(r)
    _set_rgn(t, rgn, _region(_family(_id_family(t, rid), rect), rect, align))
    return noErr


@trap
def IconSuiteToRgn(t, rgn, r, align, suite):
    rect = t.rect(r)
    _set_rgn(t, rgn, _region(_family(_suite_family(t, suite), rect), rect, align))
    return noErr


@trap
def PtInIconID(t, pt, r, align, rid):
    rect = t.rect(r)
    return _region(_family(_id_family(t, rid), rect), rect, align).contains(pt)


@trap
def PtInIconSuite(t, pt, r, align, suite):
    rect = t.rect(r)
    return _region(_family(_suite_family(t, suite), rect), rect, align).contains(pt)


def _touches(rgn: Region, r: Rect) -> bool:
    return not rgn.sect(Region.rect(r)).empty


@trap
def RectInIconID(t, test_r, r, align, rid):
    rect = t.rect(r)
    return _touches(_region(_family(_id_family(t, rid), rect), rect, align), t.rect(test_r))


@trap
def RectInIconSuite(t, test_r, r, align, suite):
    rect = t.rect(r)
    return _touches(_region(_family(_suite_family(t, suite), rect), rect, align),
                    t.rect(test_r))


# =========================================================================
# Colour icons (Inside Macintosh V-78)
# =========================================================================
ICON_PMAP_TABLE, ICON_MASK_BASE, ICON_BMAP_BASE, ICON_DATA = 42, 50, 64, 78


@trap
def GetCIcon(t, rid):
    """A CIcon record in a new handle, from a cicn: the PixMap, the two
    BitMaps and their bits, with the colour table and the pixels each
    in a handle of their own, as GetCIcon leaves them."""
    data = _data(t, t.process.resources.GetResource("cicn", rid))
    if data is None:
        return 0
    try:
        c = CIcon(data)
    except (PictError, struct.error):
        return 0
    heap = t.process.heap
    h = heap.new_handle(c.ctab_at)
    if not h:
        return 0
    pixels = heap.new_handle(max(len(data[c.pixels_at:c.pixels_end]), 0))
    ctab = heap.new_handle(c.pixels_at - c.ctab_at)
    p = heap.deref(h)
    t.mem.write(p, data[:c.ctab_at])
    if pixels:
        t.mem.write(heap.deref(pixels), data[c.pixels_at:c.pixels_end])
    if ctab:
        t.mem.write(heap.deref(ctab), data[c.ctab_at:c.pixels_at])
    t.mem.w32(p, 0)
    t.mem.w32(p + ICON_PMAP_TABLE, ctab)
    t.mem.w32(p + ICON_MASK_BASE, p + c.mask_at)
    t.mem.w32(p + ICON_BMAP_BASE, p + c.bmap_at)
    t.mem.w32(p + ICON_DATA, pixels)
    return h


def _cicon(t, h: int) -> Icon | None:
    """The icon of a CIcon record GetCIcon made: put back together as
    its cicn and read again."""
    record = _data(t, h)
    if record is None or len(record) < 82:
        return None
    table, = struct.unpack_from(">L", record, ICON_PMAP_TABLE)
    pixels, = struct.unpack_from(">L", record, ICON_DATA)
    ctab = _data(t, table) or struct.pack(">lHh", 0, 0, -1)
    try:
        return CIcon(record + ctab + (_data(t, pixels) or b"")).icon()
    except (PictError, struct.error):
        return None


@trap
def PlotCIcon(t, r, h):
    _plot(t, t.rect(r), atNone, ttNone, _cicon(t, h))


@trap
def PlotCIconHandle(t, r, align, transform, h):
    icon = _cicon(t, h)
    if icon is None:
        return paramErr
    _plot(t, t.rect(r), align, transform, icon)
    return noErr


@trap
def DisposeCIcon(t, h):
    if not h:
        return
    heap = t.system.heap_of(h)
    p = heap.deref(h)
    if p:
        for field in (ICON_PMAP_TABLE, ICON_DATA):
            sub = t.mem.r32(p + field) & ADDRESS_MASK
            if sub:
                t.system.heap_of(sub).dispose_handle(sub)
    heap.dispose_handle(h)
