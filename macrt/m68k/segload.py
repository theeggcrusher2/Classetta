"""
The Segment Loader: an application's code is in CODE resources, and the
jump table ties them together.

CODE 0 describes the A5 world (Inside Macintosh II-61):

    long  bytes above A5: application parameters plus the jump table
    long  bytes below A5: the application's globals, QuickDraw's among them
    long  size of the jump table
    long  offset of the jump table from A5 (32)
          the jump table

Each jump table entry is 8 bytes.  While its segment isn't in memory it
is the routine's offset in the segment, then MOVE.W #segment,-(SP) and
_LoadSeg; code calls an entry by JSR to its second word.  _LoadSeg reads
CODE n, locks it, and rewrites every entry of that segment as the segment
number and a JMP to the routine.  _UnloadSeg puts them back and makes the
segment purgeable.  It reads the segment with the GetResource trap, so a
program that has patched GetResource sees it asked for: FullPaint's and
Q-Sheet's patches decrypt their segments as they're read.

CODE n starts with the offset of its first entry in the jump table and
the number of entries; its code follows.  Segments built for the "far"
model of MPW 3.2 and later start with $FFFF and a 40-byte header instead,
which this loader doesn't handle yet.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass
from typing import TYPE_CHECKING

from ..macfile import MacFile
from ..memory import ADDRESS_MASK
from ..resfork import InvalidForkError, open_fork
from ..toolbox import lowmem

if TYPE_CHECKING:
    from .runtime import Runner

MOVE_W_IMM_PUSH = 0x3F3C        # MOVE.W #imm,-(SP)
LOAD_SEG = 0xA9F0
GET_RESOURCE = 0xA9A0
CODE = struct.unpack(">I", b"CODE")[0]
JMP_ABS_L = 0x4EF9
ENTRY = 8
dsLoadErr = 15                  # system error: a segment couldn't be loaded


class SegmentError(ValueError):
    """An application whose code the Segment Loader can't use."""


@dataclass(frozen=True)
class Layout:
    """What CODE 0 says about an application's A5 world."""
    above_a5: int
    below_a5: int
    jt_offset: int
    jump_table: bytes

    @property
    def entries(self) -> int:
        return len(self.jump_table) // ENTRY


def code0(app: MacFile) -> Layout | None:
    """The application's CODE 0, None if it has no 68k code."""
    try:
        fm = open_fork(app.rsrc)
    except InvalidForkError:
        return None
    entry = fm.find(b"CODE", 0)
    if entry is None:
        return None
    data = fm.raw(entry)
    if len(data) < 16:
        raise SegmentError("CODE 0 is too short to describe an A5 world")
    above, below, jt_size, jt_offset = struct.unpack_from(">IIII", data)
    if jt_size < ENTRY or len(data) < 16 + ENTRY or jt_offset + jt_size > above \
            or above > 0x80000 or below > 0x800000:
        raise SegmentError(f"CODE 0 doesn't add up (above A5 {above}, below A5 {below}, "
                           f"jump table {jt_size} bytes at {jt_offset})")
    # A self-expanding application (one compressed by a tool such as
    # AutoDoubler) has a CODE 0 shorter than its jump table: the first
    # entry unpacks the rest.  The Segment Loader copies the full size
    # regardless; past the resource there's nothing, so zeros.
    table = data[16:16 + jt_size].ljust(jt_size, b"\0")
    if table[:8] == bytes.fromhex("0000FFFF00000000"):
        raise SegmentError("it was built for MPW's far code model (32-bit jump table), "
                           "which macrt's Segment Loader doesn't handle yet")
    return Layout(above, below, jt_offset, table)


def has_code(app: MacFile) -> bool:
    try:
        fm = open_fork(app.rsrc)
    except InvalidForkError:
        return False
    return fm.find(b"CODE", 0) is not None


class SegmentLoader:
    def __init__(self, t: Runner, layout: Layout):
        self.t = t
        self.layout = layout
        self.a5 = t.process.partition.a5
        self.table = self.a5 + layout.jt_offset
        self.loaded: dict[int, tuple[int, int, int]] = {}  # segment -> (handle, code, size)

    def install(self) -> int:
        """Copy the jump table into the A5 world; return where execution
        starts (the first entry)."""
        mem = self.t.mem
        mem.write(self.table, self.layout.jump_table)
        world = self.t.process.world
        world.w16(lowmem.CurJTOffset, self.layout.jt_offset)
        return self.table + 2

    def _entry_segment(self, e: int) -> int | None:
        mem = self.t.mem
        if mem.r16(e + 2) == MOVE_W_IMM_PUSH and mem.r16(e + 6) == LOAD_SEG:
            return mem.r16(e + 4)
        if mem.r16(e + 2) == JMP_ABS_L:
            return mem.r16(e)
        return None

    def load(self, seg: int) -> None:
        """LoadSeg: read the segment, lock it, and point its entries at it."""
        if seg in self.loaded:
            h, code, _ = self.loaded[seg]
            if self.t.mem.r32(h) & 0xFFFFFF == code:
                self._patch(seg, code)
                return
        t = self.t
        h = self._get_code(seg)
        if not h:
            t.system_error(dsLoadErr, f"CODE {seg} isn't in the application")
        heap = t.system.heap_of(h)
        heap.lock(h)
        heap.set_purgeable(h, False)
        code = heap.deref(h)
        size = heap.handle_size(h)
        if size >= 2 and t.mem.r16(code) == 0xFFFF:
            t.system_error(dsLoadErr, f"CODE {seg} uses the far code model, "
                                      "which the Segment Loader doesn't handle yet")
        self.loaded[seg] = (h, code, size)
        self._patch(seg, code)

    def _get_code(self, seg: int) -> int:
        """GetResource("CODE", seg), through the program's own GetResource
        if it has patched it."""
        t = self.t
        patch = t.patches.get(GET_RESOURCE)
        if patch is not None:
            return t.call(patch, (("l", CODE), ("h", seg)), "p") & ADDRESS_MASK
        return t.process.resources.GetResource("CODE", seg)

    def _patch(self, seg: int, code: int) -> None:
        mem = self.t.mem
        first, count = mem.r16(code), mem.r16(code + 2)
        for i in range(count):
            e = self.table + first + ENTRY * i
            if mem.r16(e + 2) == MOVE_W_IMM_PUSH and mem.r16(e + 4) == seg:
                offset = mem.r16(e)
                mem.w16(e, seg)
                mem.w16(e + 2, JMP_ABS_L)
                mem.w32(e + 4, code + 4 + offset)

    def unload(self, routine: int) -> None:
        """UnloadSeg: the segment holding routine goes back to purgeable,
        its entries back to _LoadSeg."""
        seg = self._segment_of(routine & 0xFFFFFF)
        if seg is None:
            return
        mem = self.t.mem
        h, code, _ = self.loaded[seg]
        first, count = mem.r16(code), mem.r16(code + 2)
        for i in range(count):
            e = self.table + first + ENTRY * i
            if mem.r16(e + 2) == JMP_ABS_L and mem.r16(e) == seg:
                offset = (mem.r32(e + 4) & 0xFFFFFF) - (code + 4)
                mem.w16(e, offset)
                mem.w16(e + 2, MOVE_W_IMM_PUSH)
                mem.w16(e + 4, seg)
                mem.w16(e + 6, LOAD_SEG)
        heap = self.t.system.heap_of(h)
        heap.unlock(h)
        heap.set_purgeable(h, True)

    def _segment_of(self, routine: int) -> int | None:
        """The loaded segment routine is in: an address in its code, or the
        JMP of one of its jump table entries, which is what Pascal's @ gives
        for a routine in another segment (Microsoft BASIC 2 unloads its
        segments by those)."""
        mem = self.t.mem
        entry = routine - 2 - self.table
        if (0 <= entry < len(self.layout.jump_table) and entry % ENTRY == 0
                and mem.r16(routine) == JMP_ABS_L):
            seg = mem.r16(routine - 2)
            return seg if seg in self.loaded else None
        for seg, (_, code, size) in self.loaded.items():
            if code <= routine < code + size:
                return seg
        return None

    def trap_load_seg(self, return_pc: int) -> None:
        """_LoadSeg, from a jump table entry or called directly."""
        cpu = self.t.cpu
        seg = cpu.pop16()
        self.load(seg)
        # the trap is the entry's last word; a program can call an entry
        # through an address with flags in its high byte, which a 68000
        # ignores (Macintosh Pascal does, from a master pointer)
        e = (return_pc & ADDRESS_MASK) - 8
        if self.table <= e < self.table + len(self.layout.jump_table) \
                and self._entry_segment(e) == seg:
            if self.t.mem.r16(e + 2) != JMP_ABS_L:
                self._not_in_segment(seg)
            cpu.pc = e + 2          # now a JMP to the routine

    def _not_in_segment(self, seg: int) -> None:
        """The entry called is still _LoadSeg after loading its segment:
        going on would load it forever."""
        h, code, size = self.loaded[seg]
        if self.t.mem.read(code, 4) == b"SALN":
            self.t.system_error(dsLoadErr, f"CODE {seg} is still compressed by AutoDoubler: "
                                           "the application's decompressor didn't expand it")
        self.t.system_error(dsLoadErr, f"CODE {seg} doesn't have the entry the jump table "
                                       "points to")
