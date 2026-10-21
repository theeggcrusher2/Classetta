"""
The Device Manager for drivers in 68k code (IM II, chapter 6): an
application's own DRVR resources, such as SmoothTalker's speech drivers.
macrt's own drivers are elsewhere: the Sound Driver is macrt.m68k.sound's,
.Print and .Sony glue_files'.

A driver is in the unit table at UTableBase as a handle to its device
control entry (DCE), at its unit number; its reference number is
-(unit + 1).  Opening a driver by name loads the DRVR resource of that
name, whose ID is its unit number, makes it a DCE and calls its open
routine.  Read and Write call its prime routine, Control and KillIO its
control routine, Status its status routine and Close its close routine,
each with the parameter block in A0 and the DCE in A1.

Requests wait their turn in the DCE's queue, as on a Mac.  A driver says
it's done with one by jumping to IODone through JIODone, with the result
in D0: IODone takes it off the queue, sets its ioResult, calls an
asynchronous request's completion routine and starts the driver on the
next.  A synchronous call waits for that in 68k code, the Machine's
wait_for_io, so interrupts and VBL tasks run meanwhile; an immediate one
is done when the routine returns, with the result in D0.

The System file's drivers are run only as desk accessories (glue_desk),
which the Desk Manager opens and sends Control calls to from here: the
others a System has, AppleTalk's and the like, are for hardware macrt
doesn't have, so opening one fails as it did before there were drivers.
An application's drivers go when it quits.
"""

from __future__ import annotations

from contextlib import contextmanager

from ..memory import ADDRESS_MASK, from_mac
from ..toolbox import lowmem

UNITS = 48                      # the unit table's length, as from the Mac Plus's ROM on
FIRST_SPARE = 32                # where a driver goes whose own unit is taken

noErr = 0
controlErr = -17
statusErr = -18
readErr = -19
writErr = -20
dInstErr = -26
abortErr = -27
notOpenErr = -28
unitTblFullErr = -29
memFullErr = -108

# Parameter blocks (IM II, chapter 6)
qLink = 0
qType = 4
ioTrap = 6
ioCmdAddr = 8
ioCompletion = 12
ioResult = 16
ioNamePtr = 18
ioRefNum = 24
csCode = 26
csParam = 28
ioPosMode = 44
ioPosOffset = 46
ioQType = 2
killCode = 1
fsFromStart, fsFromMark = 1, 3
PB_SIZE = 50

# The calls' trap words
OPEN, CLOSE, CONTROL = 0xA000, 0xA001, 0xA004

# Trap word bits
IMMEDIATE = 0x200
ASYNC = 0x400

# The device control entry
dCtlDriver = 0
dCtlFlags = 4
dCtlQHead = 8                   # dCtlQHdr is at 6: its flags, then the queue's head and tail
dCtlQTail = 12
dCtlPosition = 16
dCtlRefNum = 24
dCtlCurTicks = 26
dCtlWindow = 30
dCtlDelay = 34
dCtlEMask = 36
dCtlMenu = 38
DCE_SIZE = 40
dOpened, dRAMBased, drvrActive = 0x20, 0x40, 0x80

# The driver's header
drvrFlags = 0
drvrDelay = 2
drvrEMask = 4
drvrMenu = 6
drvrOpen, drvrPrime, drvrCtl, drvrStatus, drvrClose = 8, 10, 12, 14, 16
drvrName = 18
dReadEnable, dWritEnable, dCtlEnable, dStatEnable = 0x100, 0x200, 0x400, 0x800
dNeedTime = 0x2000

# Each call: the routine it calls and the bit that lets a driver be asked, with
# the error if it can't be
ROUTINES = {
    2: (drvrPrime, dReadEnable, readErr),
    3: (drvrPrime, dWritEnable, writErr),
    4: (drvrCtl, dCtlEnable, controlErr),
    5: (drvrStatus, dStatEnable, statusErr),
}


def _s16(v: int) -> int:
    v &= 0xFFFF
    return v - 0x10000 if v & 0x8000 else v


def init(machine) -> None:
    """An empty unit table, and IODone."""
    mem = machine.mem
    table = machine.system.sys_heap.new_ptr(4 * UNITS, clear=True)
    mem.w32(lowmem.UTableBase, table)
    mem.w16(lowmem.UnitNtryCnt, UNITS)
    mem.w32(lowmem.JIODone, machine.io_done)


def _dce(t, unit: int) -> int:
    """The unit's DCE, 0 if it has none: for macrt's own drivers it hasn't."""
    m = t.mem
    table = m.r32(lowmem.UTableBase) & ADDRESS_MASK
    if not table or not 0 <= unit < m.r16(lowmem.UnitNtryCnt):
        return 0
    h = m.r32(table + 4 * unit) & ADDRESS_MASK
    return m.r32(h) & ADDRESS_MASK if h else 0


def _driver(t, dce: int) -> int:
    d = t.mem.r32(dce + dCtlDriver) & ADDRESS_MASK
    if t.mem.r16(dce + dCtlFlags) & dRAMBased:
        d = t.mem.r32(d) & ADDRESS_MASK
    return d


def _dce_for(t, pb: int) -> int:
    """The DCE of the driver the call is for, 0 if it's not one of these."""
    unit = ~t.mem.r16s(pb + ioRefNum)
    return _dce(t, unit) if unit >= 0 else 0


def call(t, w: int, built_in) -> bool:
    """Do Device Manager call w, A0 its parameter block, if it's for a
    driver in 68k code; False if it isn't, for the File Manager's or
    macrt's own drivers.  built_in has those, by name: their refnums."""
    pb = t.cpu.a(0) & ADDRESS_MASK
    n = w & 0xFF
    if n == 0:
        return _open(t, pb, w, built_in)
    dce = _dce_for(t, pb)
    if not dce:
        return False
    if n == 1:
        _close(t, pb, w, dce)
    elif not t.mem.r16(dce + dCtlFlags) & dOpened:
        finish(t, pb, w, notOpenErr)
    elif n == 6:
        _kill(t, pb, w, dce)
    else:
        _request(t, pb, w, dce, *ROUTINES[n])
    return True


# =========================================================================
# Opening and closing
# =========================================================================
def _open(t, pb: int, w: int, built_in) -> bool:
    m = t.mem
    p = m.r32(pb + ioNamePtr) & ADDRESS_MASK
    name = m.r_pstr(p) if p else b""
    if not name.startswith(b".") or from_mac(name).lower() in built_in:
        return False
    unit = _find(t, name)
    if unit is None:
        unit = _install(t, name, {~r for r in built_in.values()})
        if unit is None:
            return False                    # no such driver: perhaps a file
    if unit < 0:
        finish(t, pb, w, unit)             # an error
        return True
    dce = _dce(t, unit)
    m.w16(pb + ioRefNum, ~unit)
    err = noErr
    flags = m.r16(dce + dCtlFlags)
    if not flags & dOpened:
        err = _call_routine(t, pb, w, dce, drvrOpen)
        if err == noErr:
            m.w16(dce + dCtlFlags, m.r16(dce + dCtlFlags) | dOpened)
    finish(t, pb, w, err)
    return True


def _find(t, name: bytes, owner=None) -> int | None:
    """The unit of the driver of that name, if one's in the table: one
    owner opened, if it's given."""
    want = from_mac(name).lower()
    for unit in range(t.mem.r16(lowmem.UnitNtryCnt)):
        dce = _dce(t, unit)
        if (dce and from_mac(t.mem.r_pstr(_driver(t, dce) + drvrName)).lower() == want
                and (owner is None or t.machine.drivers.get(unit) is owner)):
            return unit
    return None


def _install(t, name: bytes, taken, system_too: bool = False) -> int | None:
    """Load the application's DRVR resource of that name, or the System
    file's too if system_too, into the unit table: its unit, None if
    there's no such driver, or an error.  If its own unit is taken, or
    one of those in taken, it goes in a spare one."""
    m, system = t.mem, t.system
    rm = t.process.resources
    with system.lock:
        err = rm.error
        h = rm.GetNamedResource("DRVR", name)
        found = h and (system_too or rm.HomeResFile(h) != rm.sys_refnum)
        unit = rm.GetResInfo(h)[0] if found else 0
        rm.error = err                      # the program's ResError isn't for this
        if not found:
            return None
        table = m.r32(lowmem.UTableBase) & ADDRESS_MASK
        count = m.r16(lowmem.UnitNtryCnt)
        if not 0 <= unit < count or unit in taken or m.r32(table + 4 * unit):
            free = [u for u in range(FIRST_SPARE, count)
                    if u not in taken and not m.r32(table + 4 * u)]
            if not free:
                return unitTblFullErr
            unit = free[0]
        heap = system.heap_of(h)
        heap.set_purgeable(h, False)
        heap.lock(h)
        dh = system.sys_heap.new_handle(DCE_SIZE, clear=True)
        if not dh:
            return memFullErr
        system.sys_heap.lock(dh)
        dce = m.r32(dh) & ADDRESS_MASK
        drvr = m.r32(h) & ADDRESS_MASK
        m.w32(dce + dCtlDriver, h)
        m.w16(dce + dCtlFlags, m.r16(drvr + drvrFlags) & 0xFF00 | dRAMBased)
        m.w16(dce + dCtlRefNum, ~unit)
        m.w16(dce + dCtlDelay, m.r16(drvr + drvrDelay))
        m.w16(dce + dCtlEMask, m.r16(drvr + drvrEMask))
        m.w16(dce + dCtlMenu, m.r16(drvr + drvrMenu))
        m.w32(table + 4 * unit, dh)
        t.machine.drivers[unit] = t.process
        return unit


def _close(t, pb: int, w: int, dce: int) -> None:
    m = t.mem
    err = noErr
    if m.r16(dce + dCtlFlags) & dOpened:
        err = _call_routine(t, pb, w, dce, drvrClose)
        if err == noErr:
            m.w16(dce + dCtlFlags, m.r16(dce + dCtlFlags) & ~dOpened)
    finish(t, pb, w, err)


def forget(t) -> None:
    """t's application is quitting: take the drivers it opened out of
    the unit table.  (Its resource file, which they came from, closes.)"""
    for unit in units_of(t):
        _remove(t, unit)


def units_of(t) -> list[int]:
    """The units of the drivers t's application has in the table."""
    return [unit for unit, process in list(t.machine.drivers.items()) if process is t.process]


def _remove(t, unit: int) -> None:
    """Take the driver at unit out of the table, and let its code go: it
    can be purged once no other driver in the table runs it."""
    m, system = t.mem, t.system
    table = m.r32(lowmem.UTableBase) & ADDRESS_MASK
    with system.lock:
        t.machine.drivers.pop(unit, None)
        dh = m.r32(table + 4 * unit) & ADDRESS_MASK
        m.w32(table + 4 * unit, 0)
        if not dh:
            return
        h = m.r32((m.r32(dh) & ADDRESS_MASK) + dCtlDriver) & ADDRESS_MASK
        system.sys_heap.dispose_handle(dh)
        others = (_dce(t, u) for u in range(m.r16(lowmem.UnitNtryCnt)))
        if any(dce and m.r32(dce + dCtlDriver) & ADDRESS_MASK == h for dce in others):
            return
        try:
            heap = system.heap_of(h)
        except ValueError:
            return
        if heap.is_handle(h):
            heap.unlock(h)
            heap.set_purgeable(h, True)


# =========================================================================
# Calls from macrt: the Desk Manager's
# =========================================================================
def open_driver(t, name: bytes, taken=()) -> int:
    """Open the driver of that name, the System file's or the application's,
    for the application, as OpenDeskAcc does: its unit, or an error.  One
    it already has open is just found.  The units in taken aren't to be
    used: macrt's own drivers have them."""
    m = t.mem
    unit = _find(t, name, t.process)
    if unit is None:
        unit = _install(t, name, taken, system_too=True)
        if unit is None:
            return dInstErr
    if unit < 0:
        return unit
    dce = _dce(t, unit)
    if m.r16(dce + dCtlFlags) & dOpened:
        return unit
    with on_stack(t, PB_SIZE) as pb:
        m.w16(pb + ioRefNum, ~unit)
        err = _call_routine(t, pb, OPEN, dce, drvrOpen)
    if err != noErr:
        _remove(t, unit)
        return err
    m.w16(dce + dCtlFlags, m.r16(dce + dCtlFlags) | dOpened)
    return unit


def close_driver(t, unit: int) -> int:
    """Close the driver at unit, as CloseDeskAcc does, and take it out of
    the table: its close routine's result."""
    m = t.mem
    dce = _dce(t, unit)
    if not dce:
        return notOpenErr
    err = noErr
    if m.r16(dce + dCtlFlags) & dOpened:
        with on_stack(t, PB_SIZE) as pb:
            m.w16(pb + ioRefNum, ~unit)
            err = _call_routine(t, pb, CLOSE, dce, drvrClose)
    if err == noErr:
        _remove(t, unit)
    return err


def control(t, unit: int, code: int, param: int = 0) -> int:
    """A Control call to the driver at unit, csCode code with param at
    csParam, as the Desk Manager makes one: its result.  A driver that's
    busy isn't asked, as one isn't that's called SystemTask in the middle
    of a request."""
    m = t.mem
    dce = _dce(t, unit)
    flags = m.r16(dce + dCtlFlags) if dce else 0
    if not flags & dOpened:
        return notOpenErr
    if not flags & dCtlEnable or flags & drvrActive:
        return controlErr
    with on_stack(t, PB_SIZE) as pb:
        m.w16(pb + ioTrap, CONTROL)
        m.w16(pb + ioRefNum, m.r16(dce + dCtlRefNum))
        m.w16(pb + csCode, code)
        m.w32(pb + csParam, param)
        _queue(t, dce, pb, CONTROL, drvrCtl)
        _start(t, dce)
        if m.r32(dce + dCtlQHead) & ADDRESS_MASK != pb:
            return m.r16s(pb + ioResult)
        # Its control routine returned without going through IODone: so
        # it's done, since nothing will wait for it here.
        _dequeue(t, dce, pb)
        m.w16(dce + dCtlFlags, m.r16(dce + dCtlFlags) & ~drvrActive)
        return noErr


@contextmanager
def on_stack(t, size: int):
    """size bytes, cleared, below the stack pointer: a parameter block for
    a call made from here, which only lasts as long as it does."""
    cpu = t.cpu
    sp = cpu.sp
    p = (sp - size) & ~3
    t.mem.write(p, bytes(size))
    cpu.sp = p
    try:
        yield p
    finally:
        cpu.sp = sp


# =========================================================================
# Requests
# =========================================================================
def _request(t, pb: int, w: int, dce: int, routine: int, enable: int, refused: int) -> None:
    m = t.mem
    if not m.r16(dce + dCtlFlags) & enable:
        finish(t, pb, w, refused)
        return
    if routine == drvrPrime:
        mode, offset = m.r16(pb + ioPosMode) & 3, m.r32s(pb + ioPosOffset)
        if mode == fsFromStart:
            m.w32(dce + dCtlPosition, offset)
        elif mode == fsFromMark:
            m.w32(dce + dCtlPosition, m.r32s(dce + dCtlPosition) + offset)
    if w & IMMEDIATE:
        finish(t, pb, w, _call_routine(t, pb, w, dce, routine))
        return
    _queue(t, dce, pb, w, routine)
    if not m.r16(dce + dCtlFlags) & drvrActive:
        _start(t, dce)
    wait(t, pb, w)


def _queue(t, dce: int, pb: int, w: int, routine: int) -> None:
    """A request for the routine waits its turn in the driver's queue."""
    m = t.mem
    m.w16(pb + ioTrap, w)
    m.w32(pb + ioCmdAddr, _driver(t, dce) + m.r16(_driver(t, dce) + routine))
    m.w16(pb + ioResult, 1)                 # in progress
    m.w32(pb + qLink, 0)
    m.w16(pb + qType, ioQType)
    tail = m.r32(dce + dCtlQTail) & ADDRESS_MASK
    m.w32(tail + qLink if tail else dce + dCtlQHead, pb)
    m.w32(dce + dCtlQTail, pb)


def _start(t, dce: int) -> None:
    """Start the driver on the request at the head of its queue."""
    m = t.mem
    pb = m.r32(dce + dCtlQHead) & ADDRESS_MASK
    if pb:
        m.w16(dce + dCtlFlags, m.r16(dce + dCtlFlags) | drvrActive)
        t.call_with(m.r32(pb + ioCmdAddr) & ADDRESS_MASK, a0=pb, a1=dce)


def wait(t, pb: int, w: int) -> None:
    """The caller's D0: noErr for an asynchronous request, or a synchronous
    one's result once it's done, which it waits for in 68k code if it's
    still going."""
    cpu = t.cpu
    if w & ASYNC:
        cpu.set_d(0, noErr)
    elif t.mem.r16s(pb + ioResult) <= 0:
        cpu.set_d(0, t.mem.r16s(pb + ioResult) & 0xFFFFFFFF)
    else:
        cpu.push32(cpu.pc)
        cpu.pc = t.machine.wait_for_io


def _kill(t, pb: int, w: int, dce: int) -> None:
    """KillIO: the control routine's killCode, then every request still
    waiting is done, with abortErr."""
    m = t.mem
    m.w16(pb + csCode, killCode)
    err = _call_routine(t, pb, w | IMMEDIATE, dce, drvrCtl)
    if err == noErr:
        m.w16(dce + dCtlFlags, m.r16(dce + dCtlFlags) & ~drvrActive)
        while (head := m.r32(dce + dCtlQHead) & ADDRESS_MASK):
            _dequeue(t, dce, head)
            done(t, head, abortErr)
    finish(t, pb, w, err)


def _dequeue(t, dce: int, pb: int) -> None:
    m = t.mem
    after = m.r32(pb + qLink) & ADDRESS_MASK
    m.w32(dce + dCtlQHead, after)
    if not after:
        m.w32(dce + dCtlQTail, 0)


def io_done(t) -> None:
    """IODone, where a driver's routine goes through JIODone when it's done
    with the request at the head of its queue, with its DCE in A1 and the
    result in D0; and then back to whoever called the routine."""
    cpu, m = t.cpu, t.mem
    dce = cpu.a(1) & ADDRESS_MASK
    err = _s16(cpu.d(0))
    cpu.pc = cpu.pop32()
    flags = m.r16(dce + dCtlFlags)
    pb = m.r32(dce + dCtlQHead) & ADDRESS_MASK
    if not flags & drvrActive or not pb:
        return                              # an immediate request's: D0 has it
    _dequeue(t, dce, pb)
    m.w16(dce + dCtlFlags, flags & ~drvrActive)
    done(t, pb, err)
    _start(t, dce)


# =========================================================================
# Calling a driver
# =========================================================================
def _call_routine(t, pb: int, w: int, dce: int, routine: int) -> int:
    """Call one of the driver's routines on the spot: its result."""
    m = t.mem
    drvr = _driver(t, dce)
    addr = drvr + m.r16(drvr + routine)
    m.w16(pb + ioTrap, w)
    m.w32(pb + ioCmdAddr, addr)
    m.w16(pb + ioResult, 1)
    return _s16(t.call_with(addr, a0=pb, a1=dce))


def done(t, pb: int, err: int) -> None:
    """A request's done: its result, and its completion routine if it was asynchronous."""
    m = t.mem
    m.w16(pb + ioResult, err)
    completion = m.r32(pb + ioCompletion) & ADDRESS_MASK
    if m.r16(pb + ioTrap) & ASYNC and completion:
        t.call_with(completion, a0=pb, d0=err & 0xFFFFFFFF)


def finish(t, pb: int, w: int, err: int) -> None:
    """A call done on the spot: its result in ioResult and D0."""
    t.mem.w16(pb + ioTrap, w)
    done(t, pb, err)
    t.cpu.set_d(0, err & 0xFFFFFFFF)
