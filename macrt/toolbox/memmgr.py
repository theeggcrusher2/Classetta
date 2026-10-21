"""
Memory Manager: heap zones holding nonrelocatable blocks (pointers) and
relocatable ones (handles, reached through master pointers).

Every block starts with the 8-byte header of the 24-bit Memory Manager:
a tag byte (block type in the top two bits, size correction in the low
nibble), the physical size in the next three bytes, then the relative
handle (relocatable) or the zone's address (nonrelocatable).  Code that
peeks at a header finds what it expects there.  The free list and block
table live on the Python side, and are read back from the headers when
a program has moved blocks itself (MacWrite 4.5 slides the heap's blocks
down to move a segment to the top before it loads it, setting their
master pointers as the Memory Manager would).  The free master pointers
are a list in the zone, as on a Mac: hFstFree in the zone record is the
first, and each holds the address of the next.  Excel 1.0 calls
MoreMasters with an empty list and its own moreMast, and keeps the block
of master pointers it finds there as its table of handles; then it adds
zcbFree, the free bytes the record counts, to what it could purge, to see
if it has room to start.

Blocks move to compact the heap only when a program asks, with CompactMem
or MaxMem.  Otherwise a handle moves only when SetHandleSize can't grow it
in place, or when a nonrelocatable block has no free block to go in: then
the handles in the lowest stretch of the zone that can hold it move up out
of its way, the zone growing for them if it must, so that nonrelocatable
blocks stay together at the bottom of the zone, as on a Mac (Inside
Macintosh II-12).  The Angelsoft adventures' p-System counts on it: it
takes the largest free block MaxMem finds to be the one that ends the
zone.  Mac programs already have to allow for all of these.  When an
allocation doesn't fit, a zone with room to grow (the application heap,
up to ApplLimit) grows, as far as it can when even that isn't enough;
failing that, unlocked purgeable blocks are purged, oldest address first,
until it does: the master pointer becomes NIL and the handle stays valid,
as on a Mac.
"""

from __future__ import annotations

import bisect
import threading
from typing import Callable

from ..memory import ADDRESS_MASK, RAM

noErr = 0
memFullErr = -108
nilHandleErr = -109
memWZErr = -111
memPurErr = -112
memLockedErr = -117

HEADER = 8
MIN_BLOCK = 12
ZONE_HEADER = 52            # the zone record; bkLim, hFstFree, zcbFree and moreMast are kept
H_FST_FREE = 8              # hFstFree's offset in the record
ZCB_FREE = 12               # zcbFree's
MORE_MAST = 20              # moreMast's
MASTERS_PER_BLOCK = 32
GROW_STEP = 0x1000          # the least a zone grows by

FREE, NONRELOCATABLE, RELOCATABLE = 0, 1, 2

# Flags in a master pointer's high byte (24-bit mode)
LOCKED = 0x80
PURGEABLE = 0x40
RESOURCE = 0x20


class Heap:
    def __init__(self, mem: RAM, start: int, end: int, limit: int | None = None,
                 on_grow: Callable[[int], None] | None = None, align: int = 4):
        """A zone from start to end.  With a limit it can grow that far,
        and on_grow hears of each new end (the application heap's HeapEnd).
        Its blocks' sizes are multiples of align: 2 on a 68000, 4 on a
        68020 and later (Inside Macintosh: Memory, "Organization of
        Memory").  Macintosh Pascal checksums its own code segment to the
        end of its block as a Mac Plus has it."""
        self.mem = mem
        self.align = align
        self.start = start
        self.end = end & ~3
        self.limit = self.end if limit is None else max(limit & ~3, self.end)
        self.on_grow = on_grow
        self.error = noErr
        self._lock = threading.RLock()
        first = (start + ZONE_HEADER + 3) & ~3
        self._free: list[list[int]] = []                # [address, size], sorted
        self._blocks: dict[int, list[int]] = {}         # data address -> [physical, logical, type]
        self._handles: set[int] = set()
        self._masters: set[int] = set()                 # every master pointer MoreMasters made
        self._borrowed: set[int] = set()               # master pointers a program made itself
        mem.fill(start, ZONE_HEADER)
        mem.w32(start, self.end)                        # bkLim
        # Multiplan calls MoreMasters until it has as many master pointers
        # as it wants, counting moreMast for each call.
        mem.w16(start + MORE_MAST, MASTERS_PER_BLOCK)
        self._add_free(first, self.end - first)

    def contains(self, addr: int) -> bool:
        return self.start <= (addr & ADDRESS_MASK) < self.end

    def free_bytes(self) -> int:
        """FreeMem: what's free, counting what the zone could still grow by."""
        return sum(n for _, n in self._free) + self.room_to_grow()

    def max_block(self, grow: bool = True) -> int:
        """MaxBlock: the largest block that could be allocated now, the
        zone growing for it if it may (programs size their buffers by
        this before they call MaxApplZone), or not if grow is False."""
        sizes = [n for _, n in self._free]
        if grow and self.room_to_grow():
            last = self._free[-1] if self._free and sum(self._free[-1]) == self.end else None
            sizes.append((last[1] if last else 0) + self.room_to_grow())
        return max(max(sizes, default=0) - HEADER, 0)

    def room_to_grow(self) -> int:
        return self.limit - self.end

    def grow(self, end: int) -> None:
        """Move the zone's end up toward end, as far as its limit allows
        (MaxApplZone, and what an allocation does when it needs room)."""
        with self._lock:
            self._check_free()
            end = min((end + 3) & ~3, self.limit)       # what a block needs, at least
            joins = bool(self._free) and sum(self._free[-1]) == self.end
            if end <= self.end or (end - self.end < MIN_BLOCK and not joins):
                return
            old, self.end = self.end, end
            self.mem.w32(self.start, end)               # bkLim
            self._add_free(old, end - old)
            if self.on_grow is not None:
                self.on_grow(end)

    def grow_for(self, size: int) -> None:
        """Grow the zone, if it may, by what its last free block lacks for a
        block of size; or as far as it can go when that's not enough, as a
        Mac's does.  Before MaxApplZone, programs asked for more than there
        was to get all of it (MacWrite asks for a megabyte, then counts
        what's free)."""
        with self._lock:
            last = self._free[-1] if self._free and sum(self._free[-1]) == self.end else None
            need = self._physical(size) - (last[1] if last else 0)
            self.grow(self.end + max(need, GROW_STEP))

    def set_limit(self, limit: int) -> None:
        """How far the zone may grow; never below where it already ends."""
        with self._lock:
            self.limit = max(limit & ~3, self.end)

    # =====================================================================
    # Pointers
    # =====================================================================
    def new_ptr(self, size: int, clear: bool = False) -> int:
        """A nonrelocatable block; clear says the caller needs zeros, as
        NewPtrClear does, though every new block starts as zeros (_fit)."""
        with self._lock:
            return self._result(self._alloc(size, NONRELOCATABLE, self.start))

    def dispose_ptr(self, p: int) -> None:
        with self._lock:
            if self._kind(p) != NONRELOCATABLE:
                self.error = memWZErr
                return
            self._release(p & ADDRESS_MASK)
            self.error = noErr

    def ptr_size(self, p: int) -> int:
        with self._lock:
            if self._kind(p) != NONRELOCATABLE:
                self.error = memWZErr
                return 0
            self.error = noErr
            return self._block(p & ADDRESS_MASK)[1]

    def set_ptr_size(self, p: int, size: int) -> bool:
        with self._lock:
            if self._kind(p) != NONRELOCATABLE:
                self.error = memWZErr
                return False
            ok = self._resize_in_place(p & ADDRESS_MASK, size)
            self.error = noErr if ok else memFullErr
            return ok

    # =====================================================================
    # Handles
    # =====================================================================
    def new_handle(self, size: int, clear: bool = False) -> int:
        """A relocatable block; clear as for new_ptr."""
        with self._lock:
            mp = self._master()
            if not mp:
                return 0
            data = self._alloc(size, RELOCATABLE, mp - self.start)
            if not data:
                self._free_master(mp)
                self.error = memFullErr
                return 0
            self.mem.w32(mp, data)
            self._handles.add(mp)
            self.error = noErr
            return mp

    def new_empty_handle(self) -> int:
        with self._lock:
            mp = self._master()
            if mp:
                self.mem.w32(mp, 0)
                self._handles.add(mp)
                self.error = noErr
            return mp

    def is_handle(self, h: int) -> bool:
        return (h & ADDRESS_MASK) in self._handles

    def deref(self, h: int) -> int:
        return self.mem.r32(h) & ADDRESS_MASK

    def dispose_handle(self, h: int) -> None:
        with self._lock:
            h &= ADDRESS_MASK
            if h not in self._handles:
                self.error = nilHandleErr if h == 0 else memWZErr
                return
            data = self.deref(h)
            freed = not data or self._release_if_block(data)
            self._handles.remove(h)
            self.mem.w32(h, 0)
            if h in self._borrowed:
                self._borrowed.discard(h)       # the program's, not the zone's to hand out
            else:
                self._free_master(h)
            self.error = noErr if freed else memWZErr

    def empty_handle(self, h: int) -> None:
        """EmptyHandle: free the block, keep the handle; its master pointer
        becomes NIL."""
        with self._lock:
            h &= ADDRESS_MASK
            if h not in self._handles:
                self.error = nilHandleErr if h == 0 else memWZErr
                return
            data = self.deref(h)
            if data:
                if self.mem.r8(h) & LOCKED:
                    self.error = memPurErr
                    return
                self._release(data)
            self.mem.w32(h, 0)
            self.error = noErr

    def realloc_handle(self, h: int, size: int) -> bool:
        """ReallocHandle: a new block of the given size for h; the old
        contents (if any) are gone.  h may be a master pointer the program
        made itself, anywhere in the zone, so long as it's NIL: a Mac
        takes any (Microsoft Word 1.0 makes one in its first segment for
        the block it loads the rest of itself into)."""
        with self._lock:
            h &= ADDRESS_MASK
            borrowed = h not in self._handles
            if borrowed and (not h or h & 1 or not self.contains(h) or self.deref(h)):
                self.error = nilHandleErr if h == 0 else memWZErr
                return False
            flags = self.mem.r8(h)
            data = self.deref(h)
            if data:
                if flags & LOCKED:
                    self.error = memLockedErr
                    return False
                self._release(data)
                self.mem.w32(h, 0)
            new = self._alloc(size, RELOCATABLE, h - self.start)
            if not new:
                self.error = memFullErr
                return False
            self.mem.w32(h, (flags << 24) | new)
            if borrowed:
                self._handles.add(h)
                self._borrowed.add(h)
            self.error = noErr
            return True

    def is_empty(self, h: int) -> bool:
        """A purged or emptied handle: valid, but its master pointer is NIL."""
        return self.is_handle(h) and not self.deref(h)

    def handle_size(self, h: int) -> int:
        with self._lock:
            data = self._handle_data(h)
            return self._block(data)[1] if data else 0

    def set_handle_size(self, h: int, size: int) -> bool:
        with self._lock:
            data = self._handle_data(h)
            if not data:
                return False
            if self._resize_in_place(data, size):
                self.error = noErr
                return True
            flags = self.mem.r8(h)
            if flags & LOCKED:
                self.error = memFullErr
                return False
            new = self._alloc(size, RELOCATABLE, (h & ADDRESS_MASK) - self.start)
            if not new:
                self.error = memFullErr
                return False
            self.mem.move(data, new, min(self._block(data)[1], size))
            self._release(data)
            self.mem.w32(h, (flags << 24) | new)
            self.error = noErr
            return True

    def recover_handle(self, p: int) -> int:
        """RecoverHandle: the handle whose block starts at p."""
        with self._lock:
            if self._kind(p) != RELOCATABLE:
                self.error = memWZErr
                return 0
            self.error = noErr
            return self.start + self.mem.r32((p & ADDRESS_MASK) - 4)

    def get_state(self, h: int) -> int:
        """HGetState: the master pointer's flag byte."""
        return self.mem.r8(h)

    def set_state(self, h: int, flags: int) -> None:
        self.mem.w8(h, flags & 0xE0)

    def lock(self, h: int) -> None:
        self.mem.w8(h, self.mem.r8(h) | LOCKED)

    def unlock(self, h: int) -> None:
        self.mem.w8(h, self.mem.r8(h) & ~LOCKED)

    def set_purgeable(self, h: int, on: bool = True) -> None:
        """HPurge / HNoPurge"""
        flags = self.mem.r8(h)
        self.mem.w8(h, (flags | PURGEABLE) if on else (flags & ~PURGEABLE))

    def compact(self) -> None:
        """CompactMem: slide the unlocked relocatable blocks down over the
        free space below them, as far as the nonrelocatable and locked
        blocks they come to allow, leaving it together above them."""
        with self._lock:
            if not self._rescan():
                return
            mem = self.mem
            blocks: dict[int, list[int]] = {}
            gaps = []
            to = (self.start + ZONE_HEADER + 3) & ~3    # where the next block that can move goes
            for data in sorted(self._blocks):
                block, start = self._blocks[data], data - HEADER
                h = self.start + mem.r32(data - 4)
                if (block[2] == RELOCATABLE and h in self._handles and self.deref(h) == data
                        and not mem.r8(h) & LOCKED):
                    if to < start:
                        mem.move(start, to, block[0])
                        mem.w32(h, mem.r8(h) << 24 | to + HEADER)
                    start = to
                elif to < start:
                    gaps.append([to, start - to])
                blocks[start + HEADER] = block
                to = start + block[0]
            if to < self.end:
                gaps.append([to, self.end - to])
            self._blocks, self._free = blocks, gaps
            for addr, size in gaps:
                self._write_header(addr, FREE, size, 0, 0)
            mem.w32(self.start + ZCB_FREE, sum(n for _, n in gaps))

    def purge(self, h: int) -> None:
        """Purge one handle now, if it may be purged."""
        with self._lock:
            flags = self.mem.r8(h)
            if self.is_handle(h) and flags & PURGEABLE and not flags & LOCKED:
                self.empty_handle(h)

    # =====================================================================
    # Internals
    # =====================================================================
    def _result(self, addr: int) -> int:
        self.error = noErr if addr else memFullErr
        return addr

    def _kind(self, p: int) -> int | None:
        try:
            return self._block(p & ADDRESS_MASK)[2]
        except KeyError:
            return None

    def _block(self, data: int) -> list[int]:
        """[physical, logical, type] of the block whose data is at data,
        read back from the headers if a program has moved or resized it."""
        block = self._blocks.get(data)
        if ((block is None or self.mem.r32(data - HEADER) & 0xFFFFFF != block[0])
                and (self.contains(data) or self._bk_lim() != self.end) and self._rescan()):
            block = self._blocks.get(data)
        if block is None:
            raise KeyError(data)
        return block

    def _physical(self, size: int) -> int:
        return max((size + HEADER + self.align - 1) & -self.align, MIN_BLOCK)

    def _check_free(self) -> None:
        """Read the zone back from its headers if a program has written
        over a free block's, before what's free is merged or used:
        Macintosh Pascal moves its handles to the top of the zone itself,
        taking the room from the free block below them."""
        if any(self.mem.r32(addr) != size for addr, size in self._free):
            self._rescan()

    def _bk_lim(self) -> int:
        return self.mem.r32(self.start) & ADDRESS_MASK

    def _rescan(self) -> bool:
        """Read the zone's blocks and free space back from their headers.
        False, with nothing changed, if they don't make up the zone.  The
        zone ends at its bkLim if a program has moved that and the blocks
        reach it: the Angelsoft adventures' p-System makes a block of its
        own at the stack's end of the free space above the heap, then moves
        bkLim and HeapEnd past it, and the free block below up to it."""
        mem = self.mem
        for end in dict.fromkeys((self._bk_lim(), self.end)):
            found = self._walk(end)
            if found:
                break
        else:
            return False
        if end != self.end:
            self.end, self.limit = end, max(self.limit, end)
        self._free, self._blocks = found
        for addr, size in self._free:
            self._write_header(addr, FREE, size, 0, 0)
        mem.w32(self.start + ZCB_FREE, sum(n for _, n in self._free))
        return True

    def _walk(self, end: int) -> tuple[list[list[int]], dict[int, list[int]]] | None:
        """The free space and blocks from the zone's first block to end, if
        their headers make it up exactly."""
        mem = self.mem
        free: list[list[int]] = []
        blocks: dict[int, list[int]] = {}
        addr = (self.start + ZONE_HEADER + 3) & ~3
        if end < addr or end & 1:
            return None
        while addr < end:
            word = mem.r32(addr)
            kind, phys = word >> 30, word & 0xFFFFFF
            if kind > RELOCATABLE or phys < HEADER or phys & 1 or addr + phys > end:
                return None
            if kind != FREE:
                blocks[addr + HEADER] = [phys, phys - HEADER - (word >> 24 & 15), kind]
            elif free and sum(free[-1]) == addr:
                free[-1][1] += phys
            else:
                free.append([addr, phys])
            addr += phys
        return free, blocks

    def _handle_data(self, h: int) -> int:
        h &= ADDRESS_MASK
        if h not in self._handles:
            self.error = nilHandleErr if h == 0 else memWZErr
            return 0
        data = self.deref(h)
        if not data:
            self.error = nilHandleErr
        else:
            self.error = noErr
        return data

    def more_masters(self) -> bool:
        """MoreMasters: a block of moreMast master pointers, put on the
        front of the free list in order, so the list starts at the last."""
        with self._lock:
            count = self.mem.r16(self.start + MORE_MAST)
            count = count if 0 < count < 0x8000 else MASTERS_PER_BLOCK
            block = self._alloc(4 * count, NONRELOCATABLE, self.start)
            if not block:
                self.error = memFullErr
                return False
            for mp in range(block, block + 4 * count, 4):
                self._masters.add(mp)
                self._free_master(mp)
            self.error = noErr
            return True

    def _master(self) -> int:
        """A master pointer from the front of the free list, which gets
        another block when it's empty.  A list a program has written over
        (it stored through a handle it had disposed of) is let go."""
        head = self.start + H_FST_FREE
        mp = self.mem.r32(head) & ADDRESS_MASK
        if mp and not self._may_be_free(mp):
            mp = 0
        if not mp:
            self.mem.w32(head, 0)
            if not self.more_masters():
                return 0
            mp = self.mem.r32(head)
        nxt = self.mem.r32(mp) & ADDRESS_MASK
        self.mem.w32(head, nxt if self._may_be_free(nxt) else 0)
        return mp

    def _may_be_free(self, mp: int) -> bool:
        return mp in self._masters and mp not in self._handles

    def _free_master(self, mp: int) -> None:
        head = self.start + H_FST_FREE
        self.mem.w32(mp, self.mem.r32(head))
        self.mem.w32(head, mp)

    def _write_header(self, phys_addr: int, kind: int, phys: int, logical: int, link: int) -> None:
        correction = min(phys - HEADER - logical, 15) if kind != FREE else 0
        self.mem.w32(phys_addr, (((kind << 6) | correction) << 24) | phys)
        self.mem.w32(phys_addr + 4, link)

    def _add_free(self, addr: int, size: int) -> None:
        """Return a range to the free list, merging it with its neighbours."""
        self._counted_free(size)
        i = bisect.bisect_left(self._free, [addr, 0])
        if i < len(self._free) and self._free[i][0] == addr + size:
            size += self._free.pop(i)[1]
        if i > 0 and self._free[i - 1][0] + self._free[i - 1][1] == addr:
            i -= 1
            addr = self._free[i][0]
            size += self._free.pop(i)[1]
        self._free.insert(i, [addr, size])
        self._write_header(addr, FREE, size, 0, 0)

    def _counted_free(self, change: int) -> None:
        """Add change to zcbFree, as the Memory Manager does, over what a
        program may have done to it itself."""
        at = self.start + ZCB_FREE
        self.mem.w32(at, (self.mem.r32(at) + change) & 0xFFFFFFFF)

    def _alloc(self, size: int, kind: int, link: int) -> int:
        if size < 0:
            return 0
        data = self._fit(size, kind, link)
        if data:
            return data
        if kind == NONRELOCATABLE and self._room_low(self._physical(size)):
            data = self._fit(size, kind, link)
            if data:
                return data
        if self.room_to_grow():
            self.grow_for(size)
            data = self._fit(size, kind, link)
            if data:
                return data
        # Purge until it fits, never the block the space is for.
        owner = self.start + link if kind == RELOCATABLE else 0
        for h in self._purgeable(owner):
            self.empty_handle(h)
            data = self._fit(size, kind, link)
            if data:
                return data
        return 0

    def _purgeable(self, owner: int) -> list[int]:
        found = []
        for h in self._handles:
            word = self.mem.r32(h)
            flags, data = word >> 24, word & ADDRESS_MASK
            if data in self._blocks and h != owner and flags & PURGEABLE and not flags & LOCKED:
                found.append((data, h))
        return [h for _, h in sorted(found)]

    def _room_low(self, phys: int) -> bool:
        """Make a free block of phys bytes as low in the zone as one can be
        had, by moving the unlocked relocatable blocks in the way up into
        free blocks above, and growing the zone for them if there's no other
        room for them: the Memory Manager makes room for a nonrelocatable
        block "near the bottom of the zone, by moving other blocks upward,
        expanding the zone, or purging blocks from it" (Inside Macintosh
        II-12).  The stretch it takes runs from a block that can't move (or
        the zone's start) over free and movable blocks; the first one whose
        blocks all find room above it, without growing if any does.  False
        if there's none."""
        self._check_free()
        spans = self._spans()
        if spans is None:
            return False
        for grow in (False, True):
            if grow and not self.room_to_grow():
                break
            i = 0
            while i < len(spans):
                if spans[i][2] < 0:
                    i += 1
                    continue
                total, j = 0, i
                while j < len(spans) and total < phys and spans[j][2] >= 0:
                    total += spans[j][1]
                    j += 1
                if total < phys:
                    i = j                   # a block that can't move, or the end
                    continue
                k = j                       # the free blocks just past it join it
                while k < len(spans) and spans[k][2] == 0:
                    k += 1
                movers = [s for s in spans[i:j] if s[2] > 0]
                stuck, growth = self._plan(movers, spans[k:], grow)
                if stuck is None:
                    hi = spans[k][0] if k < len(spans) else self.end
                    return self._move_up(spans[i][0], hi, movers, growth)
                i = spans.index(stuck) + 1
        return False

    def _spans(self, rescanned: bool = False) -> list[tuple[int, int, int]] | None:
        """The zone's free and allocated blocks in address order, as
        (address, physical size, what): what is 0 for a free block, the data
        address of a block that can move, -1 for one that can't.  None if
        their headers don't make up the zone."""
        mem = self.mem
        spans = [(addr, n, 0) for addr, n in self._free]
        for data, (phys, _, kind) in self._blocks.items():
            if mem.r32(data - HEADER) & 0xFFFFFF != phys:
                return None if rescanned or not self._rescan() else self._spans(rescanned=True)
            h = self.start + mem.r32(data - 4)
            movable = (kind == RELOCATABLE and h in self._handles and self.deref(h) == data
                       and not mem.r8(h) & LOCKED)
            spans.append((data - HEADER, phys, data if movable else -1))
        spans.sort()
        at = (self.start + ZONE_HEADER + 3) & ~3
        for addr, n, _ in spans:
            if addr != at:
                return None
            at += n
        return spans if at == self.end else None

    def _plan(self, movers: list[tuple[int, int, int]], above: list[tuple[int, int, int]],
              grow: bool) -> tuple[tuple[int, int, int] | None, int]:
        """Whether the movers fit, first fit, in the free blocks above, and
        how much the zone must grow for those that don't, if it may: (None,
        growth), or (the first mover that can't go, 0)."""
        room = [n for _, n, what in above if what == 0]
        growth = 0
        for mover in movers:
            need = self._physical(self._blocks[mover[2]][1])
            for i, n in enumerate(room):
                if n >= need:
                    room[i] = n - need if n - need >= MIN_BLOCK else 0
                    break
            else:
                if not grow or growth + need > self.room_to_grow():
                    return mover, 0
                growth += need
        return None, growth

    def _move_up(self, lo: int, hi: int, movers: list[tuple[int, int, int]], growth: int) -> bool:
        """Move the movers, which are between lo and hi, to free blocks above
        hi, growing the zone by growth first; then lo to hi is free."""
        mem = self.mem
        held = [f for f in self._free if lo <= f[0] < hi]      # kept out, so nothing goes there
        self._free = [f for f in self._free if not lo <= f[0] < hi]
        self._counted_free(-sum(n for _, n in held))
        if growth:
            self.grow(self.end + max(growth, GROW_STEP))
        moved = True
        for _, phys, data in movers:
            size = self._blocks[data][1]
            h = self.start + mem.r32(data - 4)
            flags = mem.r8(h)
            new = self._fit(size, RELOCATABLE, h - self.start, above=hi)
            if not new:
                moved = False
                break
            mem.move(data, new, size)
            mem.w32(h, flags << 24 | new)
            del self._blocks[data]
            held.append([data - HEADER, phys])
        for at, n in held:
            self._add_free(at, n)
        return moved

    def _fit(self, size: int, kind: int, link: int, rescanned: bool = False,
             above: int = 0) -> int:
        """The first free block size fits in, made a block of kind.  Its
        bytes start as zeros, since programs read what they never wrote and
        first fit hands a block just freed straight back, which a Mac's
        Memory Manager often doesn't.  The Falcon launcher plays an
        animation table it fills only for the CLIP resources it finds, and
        "Falcon Scene" has none: over the music it had just freed, its blits
        went into the jump table.  Only free blocks from above on count."""
        phys = self._physical(size)
        for i, (addr, n) in enumerate(self._free):
            if n < phys or addr < above:
                continue
            if self.mem.r32(addr) != n and not rescanned and self._rescan():
                return self._fit(size, kind, link, rescanned=True, above=above)   # not free now
            rest = n - phys
            if rest < MIN_BLOCK:
                phys = n
                del self._free[i]
            else:
                self._free[i] = [addr + phys, rest]
                self._write_header(addr + phys, FREE, rest, 0, 0)
            self._write_header(addr, kind, phys, size, link)
            self._counted_free(-phys)
            data = addr + HEADER
            self._blocks[data] = [phys, size, kind]
            self.mem.fill(data, size)
            return data
        return 0

    def _release(self, data: int) -> None:
        self._check_free()
        phys = self._block(data)[0]
        del self._blocks[data]
        self._add_free(data - HEADER, phys)

    def _release_if_block(self, data: int) -> bool:
        """Free the block at data; False, with nothing freed, if there's no
        block there: the program wrote over the master pointer that led to
        it (Radical Castle, once it had crashed, over its GCOD 0's)."""
        try:
            self._release(data)
        except KeyError:
            return False
        return True

    def _clear_after(self, addr: int, room: int, rescanned: bool = False) -> bool:
        """Free the room bytes from addr by moving the unlocked relocatable
        blocks there elsewhere, or purging them when they may be and there's
        nowhere for them, as the Memory Manager does to grow a block in
        place: Macintosh Pascal grows a pointer before each run.  False if
        a block there can't go, or the zone ends first; none moves if the
        room has one that can't, as is found before any moves.  The blocks
        are read back from their headers first if a program has made its
        own in one of ours: Microsoft File reads its p-code file, laid out
        as blocks, over the one it gets with ReallocHandle, headers and all."""
        mem, end = self.mem, addr
        in_way = []
        while end < addr + room:
            if end >= self.end:
                return False
            i = bisect.bisect_left(self._free, [end, 0])
            if i < len(self._free) and self._free[i][0] == end:
                end += self._free[i][1]
                continue
            data = end + HEADER
            block = self._blocks.get(data)
            if (block is None or mem.r32(end) & 0xFFFFFF != block[0]) and not rescanned:
                return self._rescan() and self._clear_after(addr, room, rescanned=True)
            if block is None or block[2] != RELOCATABLE:
                return False
            h = self.start + mem.r32(end + 4)
            if h not in self._handles or self.deref(h) != data or mem.r8(h) & LOCKED:
                return False
            in_way.append((data, h))
            end += block[0]
        if not in_way:
            return True
        # Hold the free blocks in the room back, so nothing moves into it.
        held = [f for f in self._free if addr <= f[0] < end]
        self._free = [f for f in self._free if not addr <= f[0] < end]
        self._counted_free(-sum(n for _, n in held))
        for data, h in in_way:
            phys, size, _ = self._blocks[data]
            flags = mem.r8(h)
            new = self._fit(size, RELOCATABLE, h - self.start)
            if new:
                mem.move(data, new, size)
                mem.w32(h, flags << 24 | new)
            elif flags & PURGEABLE:
                mem.w32(h, 0)
            else:
                break
            del self._blocks[data]
            held.append([data - HEADER, phys])
        for at, n in held:
            self._add_free(at, n)
        return sum(n for _, n in held) >= end - addr

    def _resize_in_place(self, data: int, size: int) -> bool:
        self._check_free()
        block = self._block(data)
        phys, _, kind = block
        start = data - HEADER
        link = self.mem.r32(start + 4)
        need = self._physical(size)
        if need > phys:
            # Grow into the free block right after this one, if there is one,
            # and the zone toward its limit when that's where the zone ends:
            # Pyramid of Peril, in MasterFORTH, grows its locked first segment
            # for room it keeps its stacks in.
            nxt = start + phys

            def free_after() -> int:
                i = bisect.bisect_left(self._free, [nxt, 0])
                return self._free[i][1] if i < len(self._free) and self._free[i][0] == nxt else 0
            avail = free_after()
            if phys + avail < need and self._clear_after(nxt, need - phys):
                avail = free_after()
            if phys + avail < need and nxt + avail == self.end and self.room_to_grow():
                self.grow(max(start + need, self.end + GROW_STEP))
                avail = free_after()
            if phys + avail < need:
                return False
            self._free.pop(bisect.bisect_left(self._free, [nxt, 0]))
            self._counted_free(-avail)
            rest = phys + avail - need
            if rest < MIN_BLOCK:
                need += rest
            else:
                self._add_free(start + need, rest)
            block[0] = need
        elif phys - need >= MIN_BLOCK:
            self._add_free(start + need, phys - need)
            block[0] = need
        block[1] = size
        self._write_header(start, kind, block[0], size, link)
        return True
