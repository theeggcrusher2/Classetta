"""
Pack6, the International Utilities (Inside Macintosh I-493): dates and
times written out, and strings compared, as the international resources
say.  macrt.toolbox.intl has the formats; a System file without 'INTL' 0
and 1 is given the US ones (macrt.toolbox.sysres).
"""

from __future__ import annotations

from ..memory import ADDRESS_MASK
from ..toolbox import intl
from ..toolbox.memmgr import noErr
from .dispatch import trap

# The Script Manager's names for the international resources, ID 0 in a
# US System
ITL = {0: "itl0", 1: "itl1", 2: "itl2", 4: "itl4"}
SIZES = {0: intl.INTL0_SIZE, 1: intl.INTL1_SIZE}


def intl_handle(t, which: int) -> int:
    """IUGetIntl's resource: 'INTL' which from the resource chain, else
    its 'itl' equivalent.  ResError is left alone."""
    rm = t.process.resources
    err = rm.error
    h = rm.get("INTL", which)
    if not h and which in ITL:
        h = rm.get(ITL[which], 0)
    rm.error = err
    return h


def _record(t, h: int, which: int) -> bytes:
    p = t.mem.r32(h) & ADDRESS_MASK if h else 0
    return t.mem.read(p, SIZES[which]) if p else intl.DEFAULTS[which]()


def _date(t, secs: int, form: int, h: int) -> bytes:
    which = 0 if form == intl.shortDate else 1
    return intl.date_string(secs, form, _record(t, h or intl_handle(t, which), which))


def _time(t, secs: int, seconds: int, h: int) -> bytes:
    return intl.time_string(secs, bool(seconds), _record(t, h or intl_handle(t, 0), 0))


def _long_date_time(t, p: int) -> int:
    return int.from_bytes(t.mem.read(p, 8), "big", signed=True)


@trap
def IUDateString(t, secs, form, result):
    t.set_str255(result, _date(t, secs & 0xFFFFFFFF, form, 0))


@trap
def IUDatePString(t, secs, form, result, h):
    t.set_str255(result, _date(t, secs & 0xFFFFFFFF, form, h))


@trap
def IULDateString(t, p, form, result, h):
    t.set_str255(result, _date(t, _long_date_time(t, p), form, h))


@trap
def IUTimeString(t, secs, seconds, result):
    t.set_str255(result, _time(t, secs & 0xFFFFFFFF, seconds, 0))


@trap
def IUTimePString(t, secs, seconds, result, h):
    t.set_str255(result, _time(t, secs & 0xFFFFFFFF, seconds, h))


@trap
def IULTimeString(t, p, seconds, result, h):
    t.set_str255(result, _time(t, _long_date_time(t, p), seconds, h))


@trap
def IUMetric(t):
    return intl.is_metric(_record(t, intl_handle(t, 0), 0))


@trap
def IUGetIntl(t, which):
    return intl_handle(t, which)


@trap
def IUSetIntl(t, refnum, which, h):
    """Make h's record the file's 'INTL' which: copied into the one it
    has, else added as a new resource."""
    rm = t.process.resources
    current = rm.CurResFile()
    rm.UseResFile(refnum)
    if rm.error == noErr:
        old = rm.get("INTL", which, one=True)
        if old and old != h:
            data = t.mem.read(t.mem.r32(h) & ADDRESS_MASK, t.system.heap_of(h).handle_size(h))
            heap = t.system.heap_of(old)
            if heap.set_handle_size(old, len(data)):
                t.mem.write(heap.deref(old), data)
        if old:
            rm.ChangedResource(old)
        else:
            rm.AddResource(h, "INTL", which)
    rm.UseResFile(current)


@trap
def IUClearCache(t):
    pass        # nothing's cached: each call reads the resources


def _text(t, p: int, length: int) -> bytes:
    return t.mem.read(p, length) if length > 0 else b""


@trap
def IUMagString(t, a, b, a_len, b_len):
    return intl.compare(_text(t, a, a_len), _text(t, b, b_len))


@trap
def IUMagIDString(t, a, b, a_len, b_len):
    return intl.equal(_text(t, a, a_len), _text(t, b, b_len))


@trap
def IUMagPString(t, a, b, a_len, b_len, itl2):
    return intl.compare(_text(t, a, a_len), _text(t, b, b_len))


@trap
def IUMagIDPString(t, a, b, a_len, b_len, itl2):
    return intl.equal(_text(t, a, a_len), _text(t, b, b_len))
