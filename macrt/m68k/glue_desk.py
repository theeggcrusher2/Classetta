"""
The Desk Manager (IM I, chapter 14): desk accessories, the DRVR
resources an Apple menu lists from the System file, or the application's
own.  OpenDeskAcc opens one as a driver (glue_devices) for the
application that asks.

A desk accessory's open routine makes its window, marks it its own by
setting windowKind to its reference number, which is negative, and keeps
it in dCtlWindow.  Then it hears from the application's event loop
through Control calls, each with the application's port put back after:

- GetNextEvent and WaitNextEvent pass it, through SystemEvent, the events
  for its window: updates and activates, and keys and mouse-ups while it's
  in front, but not Command keys, which are for MenuKey.  It gets those
  its dCtlEMask asks for as accEvent, and the application none of them.
- FindWindow says inSysWindow for its window, and SystemClick does what a
  click there does: brings the window to the front, drags it, closes the
  desk accessory from its close box, or passes the click on.
- SystemTask gives it accRun every dCtlDelay ticks, if its dNeedTime flag
  says it wants the time, and accCursor while it's in front.
  WaitNextEvent does as SystemTask does, as under MultiFinder.
- SystemEdit passes it the Edit menu's Undo, Cut, Copy, Paste and Clear
  while it's in front, and MenuSelect and MenuKey pass a pick from its own
  menu, whose ID is negative, through SystemMenu, as accMenu.

An application's desk accessories close when it quits, as on a Mac: Note
Pad saves what was typed in it then.
"""

from __future__ import annotations

import logging

from ..memory import ADDRESS_MASK
from ..toolbox import constants as k, lowmem
from . import glue_devices as dev
from .dispatch import trap
from .glue_devices import _dce

log = logging.getLogger(__name__)

accEvent, accRun, accCursor, accMenu, accUndo = 64, 65, 66, 67, 68


def _s16(v: int) -> int:
    v &= 0xFFFF
    return v - 0x10000 if v & 0x8000 else v


def _accessory(t, w) -> int | None:
    """The unit of the application's desk accessory w is the window of, if
    it's one's."""
    kind = w.window_kind if w is not None else 0
    if kind >= 0:
        return None
    unit = ~kind
    return unit if t.machine.drivers.get(unit) is t.process else None


def _send(t, unit: int, code: int, param: int = 0) -> int:
    """A Control call to the desk accessory, the application's port as it
    was after, unless it was a window that's gone."""
    m = t.mem
    var = m.r32(t.process.partition.a5) & ADDRESS_MASK      # thePort's address
    port = m.r32(var) & ADDRESS_MASK if var else 0
    was_window = t.window(port) is not None
    try:
        return dev.control(t, unit, code, param)
    finally:
        if var and (not was_window or t.window(port) is not None):
            m.w32(var, port)


def _wants(t, unit: int, what: int) -> bool:
    return bool(t.mem.r16(_dce(t, unit) + dev.dCtlEMask) & (1 << what))


def system_event(t, event_p: int) -> bool:
    """SystemEvent: give a desk accessory the event if it's for its window;
    True if it was, and so isn't the application's."""
    m = t.mem
    if not event_p or not m.r8(lowmem.SEvtEnb) or not t.machine.drivers:
        return False
    what, w = m.r16(event_p), None
    if what in (k.keyDown, k.autoKey) and m.r16(event_p + 14) & k.cmdKey:
        return False
    if what in (k.mouseUp, k.keyDown, k.keyUp, k.autoKey):
        w = t.tb.FrontWindow()
    elif what in (k.updateEvt, k.activateEvt):
        w = t.window(m.r32(event_p + 2) & ADDRESS_MASK)
    unit = _accessory(t, w)
    if unit is None:
        return False
    if _wants(t, unit, what):
        _send(t, unit, accEvent, event_p)
    elif what == k.updateEvt:
        t.tb.BeginUpdate(w)                 # it doesn't draw: it doesn't need to again
        t.tb.EndUpdate(w)
    return True


def system_task(t) -> None:
    """SystemTask: accRun for the drivers that want the time, once their
    delay is up, and accCursor for the desk accessory in front."""
    units = dev.units_of(t)
    if not units:
        return
    m, now = t.mem, t.tb.TickCount()
    for unit in units:
        dce = _dce(t, unit)
        flags = m.r16(dce + dev.dCtlFlags) if dce else 0
        if (flags & dev.dNeedTime and flags & dev.dOpened
                and (now - m.r32(dce + dev.dCtlCurTicks)) & 0xFFFFFFFF
                >= m.r16(dce + dev.dCtlDelay)):
            m.w32(dce + dev.dCtlCurTicks, now)
            _send(t, unit, accRun)
    unit = _accessory(t, t.tb.FrontWindow())
    if unit is not None:
        _send(t, unit, accCursor)


def time_wanted(t, sleep: int) -> int:
    """How long WaitNextEvent can sleep for and still give the drivers
    that want time theirs."""
    m = t.mem
    for unit in dev.units_of(t):
        dce = _dce(t, unit)
        if dce and m.r16(dce + dev.dCtlFlags) & dev.dNeedTime:
            sleep = min(sleep, max(m.r16s(dce + dev.dCtlDelay), 1))
    return sleep


def menu_picked(t, result: int) -> int:
    """What MenuSelect and MenuKey return for the pick: 0 if it's from a
    desk accessory's menu, which has it instead."""
    menu_id = _s16(result >> 16)
    if menu_id >= 0 or not t.machine.drivers:
        return result
    if _menu_owner(t, menu_id) is None:
        return result
    SystemMenu(t, result)
    return 0


def _menu_owner(t, menu_id: int) -> int | None:
    for unit in dev.units_of(t):
        dce = _dce(t, unit)
        if dce and t.mem.r16s(dce + dev.dCtlMenu) == menu_id:
            return unit
    return None


def close_all(t) -> None:
    """The application is quitting: close its desk accessories."""
    from .runtime import SystemError68k
    for unit in dev.units_of(t):
        if t.mem.r16(_dce(t, unit) + dev.dCtlFlags) & dev.dOpened and _is_accessory(t, unit):
            try:
                dev.close_driver(t, unit)
            except SystemError68k as e:
                log.warning("%s: a desk accessory didn't close: %s", t.process.name, e)


def _is_accessory(t, unit: int) -> bool:
    """Desk accessories' names don't start with a period, other drivers' do."""
    name = t.mem.r_pstr(dev._driver(t, _dce(t, unit)) + dev.drvrName)
    return not name.startswith(b".")


# =========================================================================
# The traps
# =========================================================================
@trap
def OpenDeskAcc(t, name_p):
    from .glue_files import DRIVERS
    name = t.mem.r_pstr(name_p) if name_p else b""
    if not name or name.startswith(b"."):
        return 0
    unit = dev._find(t, name, t.process)
    if unit is not None:
        w = t.window(t.mem.r32(_dce(t, unit) + dev.dCtlWindow) & ADDRESS_MASK)
        if w is not None:
            t.tb.SelectWindow(w)
        return ~unit
    m = t.mem
    var = m.r32(t.process.partition.a5) & ADDRESS_MASK
    port = m.r32(var) & ADDRESS_MASK if var else 0
    unit = dev.open_driver(t, name, {~r for r in DRIVERS.values()})
    if unit < 0:
        return 0
    w = t.window(m.r32(_dce(t, unit) + dev.dCtlWindow) & ADDRESS_MASK)
    if w is not None:                       # Calculator makes it hidden, for this to show
        t.tb.ShowWindow(w)
        t.tb.SelectWindow(w)
    if var:
        m.w32(var, port)
    return ~unit


@trap
def CloseDeskAcc(t, refnum):
    unit = ~_s16(refnum)
    if t.machine.drivers.get(unit) is t.process:
        dev.close_driver(t, unit)


@trap
def SystemEvent(t, event_p):
    return system_event(t, event_p)


@trap
def SystemClick(t, event_p, p):
    w = t.window(p)
    unit = _accessory(t, w)
    if unit is None or not event_p:
        return
    pt = t.mem.r_point(event_p + 10)
    part, _ = t.tb.FindWindow(pt)
    if part == k.inDrag:
        with t.yielding():
            t.tb.DragWindow(w, pt)
    elif part == k.inGoAway:
        with t.yielding():
            close = t.tb.TrackGoAway(w, pt)
        if close:
            dev.close_driver(t, unit)
    elif w is not t.tb.FrontWindow():
        t.tb.SelectWindow(w)
    elif _wants(t, unit, k.mouseDown):
        _send(t, unit, accEvent, event_p)


@trap
def SystemTask(t):
    system_task(t)


@trap
def SystemEdit(t, cmd):
    unit = _accessory(t, t.tb.FrontWindow())
    if unit is None:
        return False
    _send(t, unit, accUndo + cmd)
    return True


@trap
def SystemMenu(t, result):
    unit = _menu_owner(t, _s16(result >> 16))
    if unit is not None:
        _send(t, unit, accMenu, result)
