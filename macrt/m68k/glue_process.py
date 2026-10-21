"""
MultiFinder's calls, through _OSDispatch: temporary memory and the
Process Manager.

That _OSDispatch is there at all is how a System 6 application tells
MultiFinder is running, so the selectors any application might call
first are here even where there's little behind them.

Temporary memory comes from the system heap, as on a Mac it came from
outside every partition.  What an application leaves allocated goes
when it quits.  The processes are the running applications, the Python
guests among them, each with a ProcessSerialNumber whose high long is 0.
"""

from __future__ import annotations

import struct

from ..memory import ADDRESS_MASK
from ..resfork import InvalidForkError, open_fork
from ..toolbox.memmgr import memFullErr, memWZErr, noErr
from ..toolbox.system import RAM_SIZE
from .dispatch import trap

paramErr = -50
procNotFound = -600
kNoProcess, kCurrentProcess = 0, 2
INFO_LENGTH = 60                    # a ProcessInfoRec


# =========================================================================
# Temporary memory
# =========================================================================
def free_temp_memory(t) -> None:
    """When an application quits: what it didn't dispose of."""
    heap = t.system.sys_heap
    with t.system.lock:
        for h in t.temp_handles:
            heap.dispose_handle(h)
    t.temp_handles.clear()


def _temp_op(t, h: int, err_p: int, op) -> None:
    h &= ADDRESS_MASK
    if h not in t.temp_handles:
        t.w16(err_p, memWZErr)
        return
    with t.system.lock:
        op(t.system.sys_heap, h)
    t.w16(err_p, noErr)


@trap
def TempNewHandle(t, size, err_p):
    heap = t.system.sys_heap
    with t.system.lock:
        h = heap.new_handle(size) if 0 <= size < 0x800000 else 0
    if not h:
        t.w16(err_p, memFullErr)
        return 0
    t.temp_handles.add(h)
    t.w16(err_p, noErr)
    return h


@trap
def TempDisposeHandle(t, h, err_p):
    _temp_op(t, h, err_p, lambda heap, h: heap.dispose_handle(h))
    t.temp_handles.discard(h & ADDRESS_MASK)


@trap
def TempHLock(t, h, err_p):
    _temp_op(t, h, err_p, lambda heap, h: heap.lock(h))


@trap
def TempHUnlock(t, h, err_p):
    _temp_op(t, h, err_p, lambda heap, h: heap.unlock(h))


@trap
def TempMaxMem(t, grow_p):
    t.w32(grow_p, 0)
    with t.system.lock:
        return t.system.sys_heap.max_block()


@trap
def TempFreeMem(t):
    with t.system.lock:
        return t.system.sys_heap.free_bytes()


@trap
def TempTopMem(t):
    return RAM_SIZE


# =========================================================================
# Processes
# =========================================================================
def _processes(t) -> list:
    """The running applications, in the order they were launched."""
    return list(t.tb._d.launch_order)


def _process(t, psn_p: int):
    """The process a ProcessSerialNumber names, None if none does."""
    high, low = t.mem.r32(psn_p), t.mem.r32(psn_p + 4)
    if high:
        return None
    if low == kCurrentProcess:
        return t.process
    return next((p for p in _processes(t) if p.serial_number == low), None)


def _set_psn(t, psn_p: int, proc) -> None:
    t.w32(psn_p, 0)
    t.w32(psn_p + 4, proc.serial_number if proc is not None else kNoProcess)


def _mode(proc) -> int:
    """processMode: the flags from the application's SIZE resource."""
    if proc.app is None:
        return 0
    try:
        fm = open_fork(proc.app.rsrc)
        entry = fm.find(b"SIZE", 0) or fm.find(b"SIZE", -1)
        data = fm.raw(entry) if entry else b""
    except InvalidForkError:
        return 0
    return struct.unpack_from(">H", data)[0] if len(data) >= 2 else 0


@trap
def GetCurrentProcess(t, psn_p):
    _set_psn(t, psn_p, t.process)
    return noErr


@trap
def GetFrontProcess(t, psn_p, _minus_one):
    front = t.tb._d.front_process
    _set_psn(t, psn_p, front)
    return noErr if front is not None else procNotFound


@trap
def GetNextProcess(t, psn_p):
    order = _processes(t)
    if t.mem.r32(psn_p) == 0 and t.mem.r32(psn_p + 4) == kNoProcess:
        following = order[:1]
    else:
        proc = _process(t, psn_p)
        if proc is None or proc not in order:
            return paramErr
        following = order[order.index(proc) + 1:]
    _set_psn(t, psn_p, following[0] if following else None)
    return noErr if following else procNotFound


@trap
def SameProcess(t, psn1_p, psn2_p, result_p):
    a, b = _process(t, psn1_p), _process(t, psn2_p)
    if a is None or b is None:
        return paramErr
    t.mem.w8(result_p, a is b)
    return noErr


@trap
def GetProcessInformation(t, psn_p, info_p):
    proc = _process(t, psn_p)
    if proc is None:
        return procNotFound
    mem, part = t.mem, proc.partition
    name_p = mem.r32(info_p + 4) & ADDRESS_MASK
    if name_p:
        t.set_str255(name_p, proc.name[:31])
    _set_psn(t, info_p + 8, proc)
    creator = proc.app.creator if proc.app is not None else b"????"
    with t.system.lock:
        free = part.heap.free_bytes()
    mem.write(info_p + 16, b"APPL" + creator)
    for offset, value in ((24, _mode(proc)), (28, part.start), (32, part.end - part.start),
                          (36, free), (40, 0), (44, kNoProcess), (48, proc.launch_ticks),
                          (52, 0)):
        mem.w32(info_p + offset, value)
    spec = mem.r32(info_p + 56) & ADDRESS_MASK if mem.r32(info_p) >= INFO_LENGTH else 0
    if spec:                        # an FSSpec: the application, by name
        mem.w16(spec, 0)
        mem.w32(spec + 2, 0)
        t.set_str255(spec + 6, proc.app.name[:63] if proc.app is not None else proc.name)
    return noErr


@trap
def SetFrontProcess(t, psn_p):
    proc = _process(t, psn_p)
    if proc is None:
        return procNotFound
    d = t.tb._d
    with t.yielding():
        d.bridge.call(d.switch_to, proc)
    return noErr


@trap
def WakeUpProcess(t, psn_p):
    proc = _process(t, psn_p)
    if proc is None:
        return procNotFound
    proc.events.wake()
    return noErr
