"""
OS traps: the Memory Manager, the trap table, Gestalt and SysEnvirons,
OS events, time, and the Segment Loader.  Names and arguments are as in
Inside Macintosh; see dispatch for how they're called.
"""

from __future__ import annotations

import calendar
import struct
import sys
import time

from ..memory import ADDRESS_MASK
from ..toolbox import lowmem, script
from ..toolbox.events import EventRecord, ProcessKilled, tick_count
from ..toolbox.files import FileError
from ..toolbox.memmgr import MIN_BLOCK, ZONE_HEADER, memWZErr, nilHandleErr, noErr
from ..toolbox.system import RAM_SIZE
from . import dispatch, glue_desk, glue_sony, timemgr
from .dispatch import raw, trap

memFullErr = -108
envBadVers = -5501
envVersTooBig = -5502
gestaltUndefSelectorErr = -5551
gestaltDupSelectorErr = -5552
fnfErr = -43
LC = 0x4C43                     # launchBlockID: a LaunchParamBlockRec
launchContinue = 0x4000
SYSENV_SIZE = 16


# =========================================================================
# Memory Manager.  Every call leaves its result in MemErr.
# =========================================================================
def _zone(t, sys: bool):
    if sys:
        return t.system.sys_heap
    try:
        return t.system.heap_of(t.process.world.r32(lowmem.TheZone))
    except ValueError:
        return t.process.heap


def _heap_of(t, addr: int):
    """The heap a block is in, None (with MemErr set) if it's in none."""
    try:
        return t.system.heap_of(addr)
    except ValueError:
        t.set_mem_err(memWZErr)
        return None


def _done(t, heap, value=None):
    t.set_mem_err(heap.error if heap is not None else memWZErr)
    return value


def _handle_op(t, h: int, op) -> None:
    if not h:
        t.set_mem_err(nilHandleErr)
        return
    heap = _heap_of(t, h)
    if heap is not None:
        heap.error = noErr
        op(heap)
        _done(t, heap)


@trap
def NewPtr(t, size, sys, clear):
    # A negative size is one too big to have, not none: Run for the Money
    # works out its offscreen buffer's size in 16 bits, and on a 640-pixel
    # screen gets NIL back and gives up, where 0 bytes it would draw over.
    heap = _zone(t, sys)
    return _done(t, heap, heap.new_ptr(size & 0xFFFFFFFF, clear))


@trap
def NewHandle(t, size, sys, clear):
    heap = _zone(t, sys)
    return _done(t, heap, heap.new_handle(size & 0xFFFFFFFF, clear))


@trap
def NewEmptyHandle(t, sys):
    heap = _zone(t, sys)
    return _done(t, heap, heap.new_empty_handle())


@trap
def DisposePtr(t, p):
    heap = _heap_of(t, p) if p else None
    if heap is not None:
        heap.dispose_ptr(p)
        _done(t, heap)


@trap
def DisposeHandle(t, h):
    if h:
        _handle_op(t, h, lambda heap: heap.dispose_handle(h))
    else:
        t.set_mem_err(nilHandleErr)


@trap
def GetPtrSize(t, p):
    heap = _heap_of(t, p)
    return _done(t, heap, heap.ptr_size(p)) if heap else 0


@trap
def SetPtrSize(t, p, size):
    heap = _heap_of(t, p)
    if heap is not None:
        heap.set_ptr_size(p, size)
        _done(t, heap)


@trap
def GetHandleSize(t, h):
    if not h:
        t.set_mem_err(nilHandleErr)
        return 0
    heap = _heap_of(t, h)
    if heap is None:
        return 0
    if heap.is_empty(h):
        t.set_mem_err(nilHandleErr)
        return 0
    t.set_mem_err(noErr)
    return heap.handle_size(h)


@trap
def SetHandleSize(t, h, size):
    _handle_op(t, h, lambda heap: heap.set_handle_size(h, size)
               if not heap.is_empty(h) else setattr(heap, "error", nilHandleErr))


@trap
def ReallocateHandle(t, h, size):
    _handle_op(t, h, lambda heap: heap.realloc_handle(h, size))


@trap
def EmptyHandle(t, h):
    _handle_op(t, h, lambda heap: heap.empty_handle(h))


@trap
def RecoverHandle(t, p, sys):
    heap = _heap_of(t, p)
    return _done(t, heap, heap.recover_handle(p)) if heap else 0


@trap
def HLock(t, h):
    _handle_op(t, h, lambda heap: heap.lock(h))


@trap
def HUnlock(t, h):
    _handle_op(t, h, lambda heap: heap.unlock(h))


@trap
def HPurge(t, h):
    _handle_op(t, h, lambda heap: heap.set_purgeable(h, True))


@trap
def HNoPurge(t, h):
    _handle_op(t, h, lambda heap: heap.set_purgeable(h, False))


@trap
def HGetState(t, h):
    if not h:
        t.set_mem_err(nilHandleErr)
        return 0
    t.set_mem_err(noErr)
    return t.mem.r8(h)


@trap
def HSetState(t, h, flags):
    _handle_op(t, h, lambda heap: heap.set_state(h, flags))


@trap
def HSetRBit(t, h):
    _handle_op(t, h, lambda heap: heap.set_state(h, heap.get_state(h) | 0x20))


@trap
def HClrRBit(t, h):
    _handle_op(t, h, lambda heap: heap.set_state(h, heap.get_state(h) & ~0x20))


@trap
def MoveHHi(t, h):
    t.set_mem_err(noErr if h else nilHandleErr)


@trap
def MoreMasters(t):
    heap = _zone(t, False)
    heap.more_masters()
    t.set_mem_err(heap.error)


@trap
def MaxApplZone(t):
    t.process.heap.grow(t.process.heap.limit)
    t.set_mem_err(noErr)


@trap
def InitZone(t, pb):
    """A zone from the parameter block's startPtr to its limitPtr, made
    the current zone."""
    start, limit, masters, grow_zone = struct.unpack(">IIHI", t.mem.read(pb, 14))
    start, limit = start & ADDRESS_MASK, limit & ADDRESS_MASK
    if limit - start < ZONE_HEADER + MIN_BLOCK + 8 + 4 * masters:   # no room for anything
        t.set_mem_err(memFullErr)
        return
    t.system.init_zone(start, limit, masters, grow_zone, t.process.partition)
    t.process.world.w32(lowmem.TheZone, start)
    t.set_mem_err(noErr)


@trap
def InitApplZone(t):
    t.set_mem_err(noErr)


@trap
def SetApplLimit(t, p):
    """How far the application heap may grow.  Below where it already
    ends is too low (memFullErr), though ApplLimit still says what was
    asked for; above the stack's base it grows no further than that."""
    proc, p = t.process, p & ADDRESS_MASK
    proc.world.w32(lowmem.ApplLimit, p)
    proc.heap.set_limit(min(p, proc.partition.stack_base))
    t.set_mem_err(noErr if p >= proc.heap.end else memFullErr)


@trap
def SetGrowZone(t, p):
    t.set_mem_err(noErr)


@trap
def GetZone(t):
    t.set_mem_err(noErr)
    return t.process.world.r32(lowmem.TheZone)


@trap
def SetZone(t, zone):
    t.process.world.w32(lowmem.TheZone, zone)
    t.set_mem_err(noErr)


@trap
def HandleZone(t, h):
    heap = _heap_of(t, h) if h else None
    return _done(t, heap, heap.start if heap is not None else 0)


@trap
def PtrZone(t, p):
    heap = _heap_of(t, p) if p else None
    return _done(t, heap, heap.start if heap is not None else 0)


@trap
def FreeMem(t, sys):
    return _zone(t, sys).free_bytes()


@trap
def MaxBlock(t, sys):
    return _zone(t, sys).max_block()


@trap
def MaxMem(t, sys):
    """The zone compacted, its largest free block and how far it can grow
    (Inside Macintosh II-38; it doesn't purge yet)."""
    heap = _zone(t, sys)
    heap.compact()
    t.set_mem_err(noErr)
    return heap.max_block(grow=False), heap.room_to_grow()     # D0 largest block, A0 growth


@trap
def CompactMem(t, size, sys):
    """The zone compacted, unless it already has a free block of size
    (blocks move only when they have to); its largest free block."""
    heap = _zone(t, sys)
    if heap.max_block(grow=False) < size:
        heap.compact()
    return heap.max_block()


@trap
def PurgeMem(t, size, sys):
    heap = _zone(t, sys)
    t.set_mem_err(noErr if heap.max_block() >= size else memFullErr)


@trap
def ReserveMem(t, size, sys):
    """Room for a block of SIZE as low in the heap as it can be; blocks
    don't move, so it's whether there's room at all.  The zone grows for it,
    as far as it can when that's not enough, as a Mac's does: programs of
    the 64K ROM's day asked for more than there was in place of MaxApplZone
    (The Fool's Errand asks for 8 MB, then sizes its buffers by MaxMem)."""
    heap = _zone(t, sys)
    if heap.max_block(grow=False) < size:
        heap.grow_for(size)
    t.set_mem_err(noErr if heap.max_block(grow=False) >= size else memFullErr)


@trap
def PurgeSpace(t, sys):
    heap = _zone(t, sys)
    return None, heap.free_bytes(), heap.max_block()     # D0 total, A0 contiguous


@trap
def StackSpace(t):
    return max(t.cpu.sp - t.process.heap.end, 0)


@trap
def FlushCodeCache(t):
    t.cpu.flush_code()


hwParamErr = -502
# HWPriv's selectors for the caches: swapping the instruction or data
# cache on or off, flushing either, the external cache's, and flushing a
# range of the code cache
CACHE_SELECTORS = {0: "SwapInstructionCache", 1: "FlushInstructionCache",
                   2: "SwapDataCache", 3: "FlushDataCache", 4: "EnableExtCache",
                   5: "DisableExtCache", 6: "FlushExtCache", 9: "FlushCodeCacheRange"}


@raw(0xA198)
def HWPriv(t, word):
    """The selector in D0.  Flushing the instruction cache, or a range
    of it, is FlushCodeCache; the other caches have nothing in them to
    flush, and turning one on or off changes nothing.  The swaps leave A0
    as it was, so the old state is the one asked for."""
    selector = t.cpu.d(0) & 0xFFFF
    if selector in CACHE_SELECTORS:
        if selector in (1, 9):
            t.cpu.flush_code()
        t.cpu.set_d(0, noErr)
    else:
        t.unimplemented(f"HWPriv selector {selector}", word)
        t.cpu.set_d(0, hwParamErr & 0xFFFFFFFF)


@trap
def BlockMove(t, src, dst, n, no_flush):
    if n > 0:
        t.mem.move(src, dst, n)
    t.set_mem_err(noErr)


def _copy_to_new_handle(t, data: bytes) -> int:
    heap = t.process.heap
    h = heap.new_handle(len(data))
    if h:
        t.mem.write(heap.deref(h), data)
    t.set_mem_err(heap.error)
    return h


def _contents(t, h: int) -> bytes | None:
    heap = _heap_of(t, h) if h else None
    if heap is None or heap.is_empty(h):
        return None
    return t.mem.read(heap.deref(h), heap.handle_size(h))


@trap
def HandToHand(t, h):
    data = _contents(t, h)
    if data is None:
        return nilHandleErr, h
    new = _copy_to_new_handle(t, data)
    return (noErr, new) if new else (memFullErr, h)


@trap
def PtrToHand(t, src, size):
    new = _copy_to_new_handle(t, t.mem.read(src, max(size, 0)))
    return (noErr, new) if new else (memFullErr, 0)


def _set_contents(t, h: int, data: bytes) -> int:
    heap = _heap_of(t, h) if h else None
    if heap is None:
        return nilHandleErr
    if not heap.set_handle_size(h, len(data)):
        return heap.error or memFullErr
    t.mem.write(heap.deref(h), data)
    return noErr


@trap
def PtrToXHand(t, src, dst, size):
    return _set_contents(t, dst, t.mem.read(src, max(size, 0)))


@trap
def HandAndHand(t, a, b):
    data, existing = _contents(t, a), _contents(t, b)
    if data is None or existing is None:
        return nilHandleErr
    return _set_contents(t, b, existing + data)


@trap
def PtrAndHand(t, p, h, size):
    existing = _contents(t, h)
    if existing is None:
        return nilHandleErr
    return _set_contents(t, h, existing + t.mem.read(p, max(size, 0)))


# =========================================================================
# The trap table
# =========================================================================
UNIMPLEMENTED = 0xA89F
# Traps that work but that GetTrapAddress calls unimplemented.  Programs
# that look for MultiFinder this way, Microsoft's among them, go on to
# make its private _OSDispatch calls, and work without them.  Programs
# that want temporary memory or the Process Manager ask Gestalt, or just
# call them.
HIDDEN = {0xA88F}           # _OSDispatch


def _trap_key(num: int, new: bool, toolbox: bool) -> int:
    if not new:
        # The 64K ROM's single table: $00-$4F were OS traps, the rest Toolbox.
        toolbox = (num & 0x1FF) > 0x4F
        return (0xA800 | (num & 0x1FF)) if toolbox else (0xA000 | (num & 0xFF))
    return (0xA800 | (num & 0x3FF)) if toolbox else (0xA000 | (num & 0xFF))


def trap_address(t, key: int) -> int:
    patch = t.patches.get(key)
    if patch is not None:
        return patch
    if (dispatch.implemented(key) and key not in HIDDEN) or key == UNIMPLEMENTED:
        return t.machine.stub_for(key)
    return t.machine.stub_for(UNIMPLEMENTED)


@trap
def SwapMMUMode(t, mode):
    """The addressing-mode flip QuickDraw and some games bracket graphics
    calls with.  macrt's memory is 24-bit and its drawing is the host's,
    so there's nothing to switch; keep MMU32Bit telling the truth and
    hand back the previous mode in D0.B, as the trap does."""
    prev = t.mem.r8(lowmem.MMU32Bit)
    t.mem.w8(lowmem.MMU32Bit, mode & 0xFF)
    return 0, prev


@trap
def GetTrapAddress(t, num, new, toolbox):
    return trap_address(t, _trap_key(num, new, toolbox))


@trap
def SetTrapAddress(t, addr, num, new, toolbox):
    key = _trap_key(num, new, toolbox)
    if (addr & 0xFFFFFF) == t.machine.stub_for(key):
        t.patches.pop(key, None)            # the original put back
    else:
        t.patches[key] = addr & 0xFFFFFF


@raw(UNIMPLEMENTED)
def _unimplemented(t, word):
    t.system_error(12, "the application called _Unimplemented")


# =========================================================================
# What the machine is
# =========================================================================
def _ostype(s: str) -> int:
    return struct.unpack(">I", s.encode("mac_roman"))[0]


def gestalt_table(t) -> dict[int, int]:
    from .runtime import SYSTEM_VERSION
    model = t.system.model
    return {_ostype(k): v for k, v in {
        "sysv": SYSTEM_VERSION,
        "sysa": 1,                  # 68k
        "proc": model.cpu + 1,      # 1 a 68000, 3 a 68020
        "fpu ": int(model.fpu),     # 1 a 68881
        "mmu ": 0,
        "qd  ": 0,                  # original QuickDraw
        "mach": model.gestalt,
        "romv": model.rom,
        "ram ": RAM_SIZE,
        "lram": RAM_SIZE,
        "addr": 0,                  # 24-bit
        "vm  ": 0,
        "os  ": 0x7A,               # Launch can return; temporary memory, tracked
        "fs  ": 2,                  # gestaltHasFSSpecCalls
        "fold": 1,                  # gestaltFindFolderPresent
        "stdf": 1,                  # gestaltStandardFile58: StandardGetFile and the rest
        "evnt": 1,                  # gestaltAppleEventsPresent
        "tmgr": 3,                  # gestaltExtendedTimeMgr: InsXTime
        "scri": script.VERSION,
        "kbd ": model.keyboard,     # 3 a Mac Plus's, 5 Apple Standard ADB
        "dbug": 0,
        "misc": 0,
        "hdwr": 0,
    }.items()}


@trap
def Gestalt(t, selector):
    selector &= 0xFFFFFFFF
    proc = t.system.gestalt_procs.get(selector)
    if proc is not None:
        # pascal OSErr fn(OSType selector, long *response), with the
        # response on our side of the stack.
        cpu = t.cpu
        cpu.sp = response = cpu.sp - 4
        err = t.call(proc, (("l", selector), ("p", response)), "e")
        value = t.mem.r32(response)
        cpu.sp = response + 4
        return err, value
    value = gestalt_table(t).get(selector)
    if value is None:
        return gestaltUndefSelectorErr, 0
    return noErr, value


@trap
def NewGestalt(t, selector, proc):
    """Add a selector answered by 68k code.  The function is the whole
    system's, as on a Mac, so it lives in the system heap."""
    selector &= 0xFFFFFFFF
    if selector in t.system.gestalt_procs or selector in gestalt_table(t):
        return gestaltDupSelectorErr
    t.system.gestalt_procs[selector] = proc
    return noErr


@trap
def ReplaceGestalt(t, selector, proc):
    selector &= 0xFFFFFFFF
    old = t.system.gestalt_procs.get(selector)
    if old is None and selector not in gestalt_table(t):
        return gestaltUndefSelectorErr, 0
    t.system.gestalt_procs[selector] = proc
    return noErr, old or 0


@trap
def SysEnvirons(t, version, p):
    from .runtime import SYSTEM_VERSION
    if version <= 0:
        return envBadVers
    model = t.system.model
    sys_vref = t.system.file_manager.get_vol()[1]     # the System Folder's working directory
    rec = struct.pack(">hhhhBBhhh", min(version, 2), model.environs, SYSTEM_VERSION,
                      model.cpu + 1, model.fpu, 0, model.keyboard, 0, sys_vref)
    t.mem.write(p, rec)
    return envVersTooBig if version > 2 else noErr


# =========================================================================
# OS Event Manager
# =========================================================================
evtNotEnb = 1


@trap
def PostEvent(t, what, message):
    where, mods = t.process.events.input_state()
    if t.process.events.post(EventRecord(what, message & 0xFFFFFFFF, 0, where, mods)):
        return noErr
    return evtNotEnb                        # SysEvtMask leaves it out


@trap
def GetOSEvent(t, mask, p):
    glue_sony.put_back(t)
    got, ev = t.process.events.get(mask & 0xFFFF, 0, lambda: None)
    t.write_event(p, ev)
    return got


@trap
def OSEventAvail(t, mask, p):
    glue_sony.put_back(t)
    got, ev = t.process.events.peek(mask & 0xFFFF, lambda: None)
    t.write_event(p, ev)
    return got


@trap
def FlushEvents(t, mask, stop_mask):
    t.process.events.flush(mask & 0xFFFF, stop_mask & 0xFFFF)


# =========================================================================
# The Notification Manager: a background application asks for the user.
# An NMRec is a queue element: qLink, qType (8), nmFlags, nmPrivate,
# nmReserved, nmMark, nmIcon, nmSound, nmStr, nmResp, nmRefCon.
# =========================================================================
nmType = 8
nmTypErr = -299
NM_STR, NM_RESP = 24, 28


@trap
def NMInstall(t, rec):
    if t.mem.r16(rec + 4) != nmType:
        return nmTypErr
    p = t.mem.r32(rec + NM_STR) & 0xFFFFFF
    if p:
        t.tb._d.notify(t.process.name, t.str255(p))
    # A response procedure of -1 asks for the request to be removed once
    # it's been seen; any other is never called, the request just stays.
    t.process.notifications.add(rec)
    if t.mem.r32(rec + NM_RESP) == 0xFFFFFFFF:
        t.process.notifications.discard(rec)
    return noErr


@trap
def NMRemove(t, rec):
    if t.mem.r16(rec + 4) != nmType:
        return nmTypErr
    t.process.notifications.discard(rec)
    return noErr


# =========================================================================
# Time
# =========================================================================
@trap
def Delay(t, ticks):
    t.sleep(ticks)
    return None, tick_count()


@trap
def ReadDateTime(t, p):
    t.mem.write(p, t.mem.read(lowmem.Time, 4))
    return noErr


@trap
def SetDateTime(t, secs):
    return noErr                        # the host's clock stays as it is


@trap
def SecondsToDate(t, secs, p):
    from .cpu import MAC_1904
    tm = time.gmtime((secs & 0xFFFFFFFF) - MAC_1904)
    t.mem.write(p, struct.pack(">7h", tm.tm_year, tm.tm_mon, tm.tm_mday, tm.tm_hour,
                               tm.tm_min, tm.tm_sec, (tm.tm_wday + 1) % 7 + 1))


@trap
def DateToSeconds(t, p):
    from .cpu import MAC_1904
    year, month, day, hour, minute, sec, _ = struct.unpack(">7h", t.mem.read(p, 14))
    try:
        secs = calendar.timegm((year, month, day, hour, minute, sec, 0, 0, 0)) + MAC_1904
    except (ValueError, OverflowError):
        secs = 0
    return None, secs & 0xFFFFFFFF


@raw(0xA193)
def Microseconds(t, word):
    us = int(time.monotonic() * 1_000_000)
    t.cpu.set_a(0, us >> 32)
    t.cpu.set_d(0, us)


@trap
def StripAddress(t, addr):
    return addr & 0x00FFFFFF


# =========================================================================
# Strings the OS compares.  Bit 9 of the trap word: ignore diacriticals;
# bit 10: case matters.
# =========================================================================
def _fold(data: bytes, word: int) -> bytes:
    """Byte for byte, so the length stays: UprString works in place."""
    if word & 0x200:
        data = data.translate(script.STRIP)
    if not word & 0x400:
        data = data.translate(script.UPPER)
    return data


@raw(0xA03C)
def CmpString(t, word):
    """EqualString: A0, A1 the strings, D0 their lengths (high word, low word)."""
    cpu = t.cpu
    d0 = cpu.d(0)
    a = t.mem.read(cpu.a(0), d0 >> 16)
    b = t.mem.read(cpu.a(1), d0 & 0xFFFF)
    cpu.set_d(0, 0 if _fold(a, word) == _fold(b, word) else 1)


@raw(0xA050)
def RelString(t, word):
    """-1, 0 or 1, as the first string sorts before, with or after the second."""
    cpu = t.cpu
    d0 = cpu.d(0)
    a = _fold(t.mem.read(cpu.a(0), d0 >> 16), word)
    b = _fold(t.mem.read(cpu.a(1), d0 & 0xFFFF), word)
    cpu.set_d(0, 0 if a == b else (0xFFFFFFFF if a < b else 1))


@raw(0xA054)
def UprString(t, word):
    """Upper-case the string at A0, length D0.W, in place."""
    cpu = t.cpu
    p, n = cpu.a(0), cpu.d(0) & 0xFFFF
    t.mem.write(p, _fold(t.mem.read(p, n), (word & 0x200) | 0))


# =========================================================================
# Queues: the OS keeps them for VBL tasks, drivers and the like.
# =========================================================================
QTYPE = 4                           # an element's qType
qErr = -1


@trap
def Enqueue(t, elem, header):
    mem = t.mem
    mem.w32(elem, 0)
    tail = mem.r32(header + 6)
    if tail:
        mem.w32(tail, elem)
    else:
        mem.w32(header + 2, elem)
    mem.w32(header + 6, elem)


@trap
def Dequeue(t, elem, header):
    mem = t.mem
    prev, cur = 0, mem.r32(header + 2)
    while cur:
        if cur == elem:
            nxt = mem.r32(cur)
            if prev:
                mem.w32(prev, nxt)
            else:
                mem.w32(header + 2, nxt)
            if mem.r32(header + 6) == elem:
                mem.w32(header + 6, prev)
            return noErr
        prev, cur = cur, mem.r32(cur)
    return qErr


# =========================================================================
# VBL tasks: each application has its own queue, as under MultiFinder,
# and the runner runs it while the application has the processor.
# =========================================================================
vType = 1
vTypErr = -2


@trap
def VInstall(t, task):
    if t.mem.r16(task + QTYPE) != vType:
        return vTypErr
    Enqueue(t, task, lowmem.VBLQueue)
    return noErr


@trap
def VRemove(t, task):
    if t.mem.r16(task + QTYPE) != vType:
        return vTypErr
    return Dequeue(t, task, lowmem.VBLQueue)


@trap
def SlotVInstall(t, task, slot):
    """For one video card's retrace; there's one screen, so the same."""
    return VInstall(t, task)


@trap
def SlotVRemove(t, task, slot):
    return VRemove(t, task)


# =========================================================================
# The Time Manager: each application's tasks, which the runner runs as it
# does VBL tasks (see timemgr).
# =========================================================================
def _wait(t, task: int, waiting: bool) -> None:
    q = t.mem.r16(task + QTYPE)
    t.mem.w16(task + QTYPE, q | timemgr.ACTIVE if waiting else q & ~timemgr.ACTIVE)


@trap
def InsTime(t, task):
    t.time_tasks.install(task)
    _wait(t, task, False)
    return noErr


@trap
def InsXTime(t, task):
    t.time_tasks.install(task, extended=True)
    _wait(t, task, False)
    return noErr


@trap
def PrimeTime(t, task, count):
    if count & 0x80000000:
        count -= 1 << 32
    wake_up = task + timemgr.TM_WAKE_UP
    tasks = t.time_tasks
    extended = task in tasks.extended
    when = tasks.prime(task, count, woken=extended and t.mem.r32(wake_up) != 0)
    if extended:                    # when it's due, in milliseconds; never 0
        t.mem.w32(wake_up, int(when * 1000) & 0xFFFFFFFF or 1)
    _wait(t, task, True)
    return noErr


@trap
def RmvTime(t, task):
    t.mem.w32(task + timemgr.TM_COUNT, timemgr.remaining(t.time_tasks.remove(task)) & 0xFFFFFFFF)
    _wait(t, task, False)
    return noErr


@trap
def InitUtil(t):
    return noErr


@trap
def WriteParam(t):
    return noErr


@raw(0xA051)
def ReadXPRam(t, word):
    """Parameter RAM: A0 the buffer, D0 count (high word) and offset."""
    cpu = t.cpu
    n = (cpu.d(0) >> 16) & 0xFFFF
    t.mem.fill(cpu.a(0), n)
    cpu.set_d(0, 0)


@raw(0xA052)
def WriteXPRam(t, word):
    t.cpu.set_d(0, 0)


# =========================================================================
# Segment Loader and the Process Manager's part of it
# =========================================================================
@raw(0xA9F0)
def LoadSeg(t, word):
    t.segments.trap_load_seg(t.cpu.pc)


@trap
def UnloadSeg(t, routine):
    t.segments.unload(routine)


@trap
def ExitToShell(t):
    glue_desk.close_all(t)
    raise ProcessKilled()


dsReinsert = 30                 # "Please insert the disk:", with no buttons


@raw(0xA9C9)
def SysError(t, word):
    """The System Error Handler, with the ID in D0 (Inside Macintosh II,
    "The System Error Handler").  Its alert comes back only with no
    buttons: the disk-switch alert, and the startup alerts, whose IDs are
    negative.  Any other is a bomb, its buttons Restart and, with a resume
    procedure, Resume; macrt stops the application, as for any other
    system error.  Downhill Racer's and The Surgeon's exception handlers
    call it and then jump to the ROM's restart."""
    error_id = t.cpu.d(0) & 0xFFFF
    error_id -= error_id >> 15 << 16
    t.mem.w16(lowmem.DSErrCode, error_id & 0xFFFF)
    if error_id < 0 or error_id == dsReinsert:
        return
    t.system_error(error_id, f"SysError at ${t.cpu.pc - 2 & ADDRESS_MASK:06X}")


@raw(0xA9F2)
def Launch(t, word):
    """Launch, and LaunchApplication, the same trap: start another
    application.  A0 points at the application's name and a word of
    configuration, or, marked 'LC', a LaunchParamBlockRec with an FSSpec.
    The configuration is the page option, for CurPageOption: Dark Castle
    asks for the alternate screen with it.  Unless launchContinue is set,
    this application quits for it."""
    mem, pb = t.mem, t.cpu.a(0)
    if mem.r16(pb + 6) == LC:
        name = t.str255((mem.r32(pb + 16) & 0xFFFFFF) + 6)      # the FSSpec's name
        _start(t, name, 0, mem.r16(pb + 14))
    else:
        _start(t, t.str255(mem.r32(pb) & 0xFFFFFF), mem.r16(pb + 4), 0)


@raw(0xA9F3)
def Chain(t, word):
    """Start another application in this one's place, as Launch does, A0
    pointing at its name and the configuration word.  On a Mac it got
    this one's heap, and what was left in it; here it gets a partition of
    its own.  Skyfox's first application chains to the game, asking for
    the alternate screen."""
    mem, pb = t.mem, t.cpu.a(0)
    _start(t, t.str255(mem.r32(pb) & 0xFFFFFF), mem.r16(pb + 4), 0)


def _start(t, name: str, page_option: int, flags: int) -> None:
    cpu = t.cpu
    folder = t.process.app.folder if t.process.app is not None else None
    on_disk = ":" in name.lstrip(":")       # Falcon's launcher names "Falcon 2:Falcon"
    app = folder.find(name) if folder is not None and name and not on_disk else None
    if app is None and name:
        try:
            node = t.process.files.lookup_file(0, 0, name)
        except FileError:
            node = None
        app = t.system.files.macfile_of(node) if node is not None else None
    if app is None:
        cpu.set_d(0, fnfErr & 0xFFFFFFFF)
        return
    d = t.tb._d
    with t.yielding():
        started = d.bridge.call(d.launch_application, app, page_option)
        if not isinstance(started, str) and not flags & launchContinue:
            # It quits from here, not after waiting for the processor,
            # which the new application may never give up.
            raise ProcessKilled()
    if isinstance(started, str):
        print(f"macrt: {t.process.name} couldn't launch {name}: {started}", file=sys.stderr)
        cpu.set_d(0, memFullErr & 0xFFFFFFFF)
        return
    cpu.set_d(0, 0)


@trap
def GetAppParms(t, name_p, refnum_p, parm_p):
    world = t.process.world
    t.mem.write(name_p, world.read(lowmem.CurApName, 32))
    t.w16(refnum_p, world.r16(lowmem.CurApRefNum))
    t.w32(parm_p, world.r32(lowmem.AppParmHandle))


# =========================================================================
# Object Pascal
# =========================================================================
@raw(0xA9F8)
def MethodDispatch(t, word):
    """A method's call, as the Mac Plus's ROM makes it.  A call jumps to
    its own stub, which calls this, its table after it, with the object,
    a handle, under the caller's return address.  The table is a count
    of entries, less one; the entry last found, for the class it was
    found for; then an entry for each class with the method, highest
    numbered first, and a 0.  An entry is the class and the method's
    offset: an even one is the method's jump table entry, an odd one is
    from the entry.  An object's class is its first word, and a class's
    superclass is the word that far into the class table, which the
    pointer ClassTable points to points to.  If no class of the object's
    has the method, it goes to MethodNotFound instead.  Q-Sheet sets
    them; on a 64K ROM it uses its own copy of the same code."""
    cpu, m = t.cpu, t.mem
    table = cpu.pc
    cls = m.r16(m.r32(m.r32(cpu.sp + 4) & ADDRESS_MASK) & ADDRESS_MASK)
    count = m.r16(table)
    first = entry = table + 2

    def search(entry: int, count: int, step: bool) -> tuple[int, int, bool]:
        """The entries from here, as DBCC goes through them: where it
        stopped, the count left, and whether that's the class's."""
        while True:
            if step:
                entry += 4
            step = True
            if cls >= m.r16(entry):
                break
            count = (count - 1) & 0xFFFF
            if count == 0xFFFF:
                break
        return entry, count, cls == m.r16(entry)

    found = m.r16(entry) == cls
    if not found:
        m.w16(entry, cls)
        entry, count, found = search(entry, count, True)
        classes = m.r32(m.r32(lowmem.ClassTable) & ADDRESS_MASK) & ADDRESS_MASK
        for _ in range(0x8000):         # as many classes as there can be
            if found:
                break
            cls = m.r16(classes + _signed16(cls) & ADDRESS_MASK)
            if not cls:
                break
            entry, count, found = search(entry, count, False)
        if found:
            m.w16(first + 2, m.r16(entry + 2))
        else:
            m.w32(first, 0)
            cpu.set_d(0, 0)
            cpu.pc = m.r32(lowmem.MethodNotFound) & ADDRESS_MASK
        cpu.set_d(1, cpu.d(1) & ~0xFFFF | count)
        cpu.set_d(2, first)
    cpu.set_a(0, entry)
    if found:
        offset = m.r16(entry + 2)
        cpu.set_d(0, cpu.d(0) & ~0xFFFF | offset & ~1)
        base = entry + 2 if offset & 1 else cpu.a(5)
        cpu.pc = base + _signed16(offset & ~1) & ADDRESS_MASK


def _signed16(v: int) -> int:
    return v - 0x10000 if v & 0x8000 else v
