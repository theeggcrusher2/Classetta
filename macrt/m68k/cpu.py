"""
The processor: unicorn's m68k core (QEMU's) running on the guest RAM.

It is a 68020 with a 68881, in 24-bit mode: unicorn's virtual TLB drops
the top byte of every address, so a master pointer's flag byte or a
24-bit-dirty pointer works as it did.  The exception frames it stacks are
a 68000's, with short_frames, for a Mac Plus.  Code runs in
supervisor mode, as Mac applications do.  The core shares macrt's RAM
byte array, so Python and 68k code see the same memory with no copying.

There is one CPU per System, shared by the 68k applications the way a
Mac's one processor is (see runtime).  run() returns at every A-line
trap and exception, which are handled in Python between runs; a trap
costs a few microseconds that way, and the core is free to translate
straight-line code at full speed.

A clock thread keeps the low-memory Ticks and Time current, as the
vertical retrace interrupt does, so code that polls them sees them move.
While VBL tasks are queued, or the application has its own handler for
the VIA's interrupts in their vector, run() returns every SLICE
instructions, so the runner can run the tasks a tick has made due, or
interrupt the application, between two instructions.  Which it is is
looked at as run() starts: after a trap or exception.
That's unicorn's instruction count, which stops before an instruction:
its emu_stop() from another thread doesn't do for this, since the core
looks for a stop after each load and store, and goes back to the start of
the instruction it's in, which then runs again, its stores already made.
An instruction like ADDQ to memory would count twice.  emu_stop() is kept
for killing a process, where that can't matter.

The core runs code as fast as the host lets it, far faster than any Mac
did.  Most programs keep time by the clock, but some early games just
run, and are over in a moment, and some time what they do by the clock:
Downhill Racer divides by the ticks its first frame took to draw.  With
rate set, the core is held to that many of a 68000's cycles a second:
before each block of code it runs, the cycles its instructions take a
68000 (cycles.py) are counted, and once a millisecond's worth have run, it
waits until the clock has caught up; and run() goes in slices of about a
tick's worth of instructions, for the runner to look at the clock.  Only
the code between traps is counted: the traps take whatever they take in
Python.

A program may write an instruction and then run it: the core sees to that
for the blocks of code it has translated, but not for the one it's in the
middle of, which runs on as it was translated.  Microsoft Word puts a trap
word in place a few instructions ahead and runs it, and would run what was
there before.  So a page is let be run from or written to, not both at
once, and the first store to a page code is translated from comes to
_tlb_fill; if it's from that same page, and may be to the block of code
the core's in, the core starts again at the store, in a block translated
afresh (see _code_written).  _tlb_fill drops the
page's blocks, too: on a page mapped to be written to, the core drops a
block a store changes for the first store only, and MacLanding writes the
trap it calls into a stub among its stack, with pushes between.

The top byte of an address isn't looked at, but the core keeps a page for
each address it's reached by, and a program reaches its code through a
master pointer with the handle's flags in it as well as at the address it
runs at.  So _tlb_fill keeps track of which ways each page has been mapped,
and when a page is to be run from through one address and may still be
written through another, or the other way about, the core forgets every
page, to map them afresh.

The core takes a TRAPcc for an Scc with an immediate or PC-relative
operand, which there's no such thing as: it runs a TRAPF with no operand
as an SF to an immediate word, and so takes the next word with it.  A
runtime that looks for a 68020 by running one goes on in the middle of an
instruction (the 350-point Adventure's).  So each page code is run from
is looked through for the words, as it's mapped, and the core stops at
any that's run, for run() to do it as a 68020 does (see _watch).  It
doesn't know TRAPV at all, and takes an illegal instruction for it:
run() does that one too, and RTR, which it doesn't know either (the
Angelsoft adventures' p-Machine returns from its comparisons of reals
with one).  And it pushes a JSR (SP)'s return address before it works
out where to go, and goes to what it pushed: the Pascal glue for an
asynchronous file call (King's Quest's) pushes an RTS and the trap and
JSRs to them, so that's watched for as well.

Nor does it trace: with SR's trace bit, T1, set as an instruction starts,
the processor takes a trace exception once it's done, and FullPaint's
loader sets it to call the routine that decrypts each segment, its
handler.  So the core stops, too, before each instruction that would
set it, an ORI, EORI or MOVE to SR, looked for as TRAPcc is; and while
it's set, run() runs one instruction at a time and returns a trace
exception after each.

SR is read here, not through unicorn: the core keeps the condition codes
as the last instruction's operands and result while it can, and unicorn's
read of SR takes those for the flags themselves, and leaves them so, which
wrecks them for a branch still to come (see sr).
"""

from __future__ import annotations

import calendar
import ctypes
import logging
import re
import struct
import threading
import time
from contextlib import contextmanager
from typing import Callable, Iterator, NamedTuple

from unicorn import (UC_ARCH_M68K, UC_HOOK_BLOCK, UC_HOOK_CODE, UC_HOOK_INTR,
                     UC_HOOK_TLB_FILL, UC_MEM_FETCH, UC_MEM_WRITE, UC_MODE_BIG_ENDIAN,
                     UC_PROT_ALL, UC_PROT_EXEC, UC_PROT_READ, UC_PROT_WRITE, UC_TLB_VIRTUAL, Uc,
                     UcError)
from unicorn.unicorn_const import UC_CTL_IO_WRITE, UC_CTL_TLB_FLUSH
from unicorn.m68k_const import (UC_CPU_M68K_M68020, UC_M68K_REG_A0, UC_M68K_REG_A7,
                                UC_M68K_REG_D0, UC_M68K_REG_PC, UC_M68K_REG_SR)

from ..memory import ADDRESS_MASK, RAM, BusError
from ..toolbox import lowmem
from ..toolbox.events import tick_count, to_next_tick
from .cycles import RATE, cycles, untaken

log = logging.getLogger("macrt.cpu")

# Exception vector numbers.  QEMU reports exceptions by these numbers.
BUS_ERROR = 2
ADDRESS_ERROR = 3
ILLEGAL = 4
ZERO_DIVIDE = 5
CHK = 6
TRAPV = 7
PRIVILEGE = 8
TRACE = 9
LINE_A = 10
LINE_F = 11
FORMAT_ERROR = 14
LEVEL_1 = 25            # the autovector of the VIA's interrupts
TRAP_0 = 32
RTE = 0x100             # QEMU raises these internally; with a hook installed they
HALT = 0x101            # reach it instead of being handled, so they're emulated here

SUPERVISOR = 0x2000
TRACE_BITS = 0xC000
T1 = 0x8000             # trace each instruction
INTERRUPT_MASK = 0x0700
VBL_LEVEL = 0x0100      # the vertical retrace interrupt's priority, in SR's mask
MAC_1904 = 2082844800   # seconds from 1904-01-01 to 1970-01-01
CCR_X, CCR_N, CCR_Z, CCR_V, CCR_C = 0x10, 0x08, 0x04, 0x02, 0x01

# How the core keeps the condition codes (QEMU's CCOp): as flags, or as
# what the last ADD, SUB or CMP of a byte, word or long, or other
# instruction, gave, for working out when they're wanted.
CC_OP_FLAGS, CC_OP_ADD, CC_OP_SUB, CC_OP_CMP, CC_OP_LOGIC = 1, 2, 5, 8, 11
SR_WORD, CC_WORDS = 17, 22      # in CPUM68KState, after D0-D7, A0-A7 and PC; cc_op on
SLICE = 100_000         # instructions between looks at the clock, while VBL tasks wait
SETTLE = 0.0002         # seconds the clock waits past a tick, not to wake just before it
VBL_COUNT, VBL_TASK_END = 10, 14    # in a VBLTask record
MAX_VBL_TASKS = 64
PAGE_BITS = 12          # the core's pages are 4K
PAGE_SIZE = 1 << PAGE_BITS
READ_WRITE, READ_RUN = UC_PROT_READ | UC_PROT_WRITE, UC_PROT_READ | UC_PROT_EXEC
# What's known of the ways a page is mapped, since the core last forgot them:
# whether it may be run from, and written to, and through more than one address.
CODE, WRITTEN, MIXED = 1, 2, 4
# How often a page goes from being run from to being written to by code on
# another page, before it's mapped for both at once (see _tlb_fill); NEAR,
# once code that might be running from it has written to it.
BOTH_AFTER, NEAR = 32, 255
# About how many cycles a 68000 takes over an instruction of a program's,
# to slice a run held to a rate into ticks: its instructions take from 4
# cycles to well over 100.
CYCLES_EACH = 6
PACED = 1000            # how often a run held to a rate waits for the clock, a second
# TRAPcc's words, Scc's with mode 7 and register 2, 3 or 4: a word, a long
# or nothing after; how long each is, by its register field.
TRAPCC_SIZES = {2: 4, 3: 6, 4: 2}
ORI_SR, EORI_SR = 0x007C, 0x0A7C
# The words the core is stopped at, TRAPcc's, JSR (SP) and those that may
# set SR's trace bit, ORI #,SR, EORI #,SR and MOVE <ea>,SR.  No first byte
# of one is a second byte of another, so one found at an odd address hides
# none.
WATCHED = re.compile(rb"[\x50-\x5F][\xFA-\xFC]|[\x00\x0A]\x7C|\x46[\xC0-\xFF]|\x4E\x97")
JSR_SP = 0x4E97
TRAPV_WORD = 0x4E76     # which the core doesn't know: it takes an illegal instruction for it
RTR = 0x4E77            # nor that
VS = 9                  # TRAPV's condition, as TRAPcc's


def _extend(value: int, size: int) -> int:
    """value's low byte, word or long (size 0, 1, 2), sign-extended to 32 bits."""
    bits = 8 << size
    value &= (1 << bits) - 1
    if value >> (bits - 1):
        value |= 0xFFFFFFFF ^ ((1 << bits) - 1)
    return value


def _extension(mem, mode: int, reg: int, size: int, at: int) -> int:
    """The bytes of extension words an effective address of mode and reg
    has, for an operand of size bytes, the first at at."""
    if mode == 5 or mode == 7 and reg in (0, 2):
        return 2
    if mode == 7 and reg in (1, 4):
        return 4 if reg == 1 or size == 4 else 2
    if (mode == 6 or mode == 7 and reg == 3) and at + 2 <= len(mem):
        ext = struct.unpack_from(">H", mem, at)[0]
        if not ext & 0x100:                     # a 68000's brief format
            return 2
        return 2 + (0, 0, 2, 4)[ext >> 4 & 3] + (0, 0, 2, 4)[ext & 3]    # a 68020's full one
    return 0


def _length(mem, op: int, pc: int) -> int:
    """How long the MOVE, MOVEM or CLR at pc is, op its first word."""
    if op < 0x4000:
        size = (0, 1, 4, 2)[op >> 12 & 3]
        n = 2 + _extension(mem, op >> 3 & 7, op & 7, size, pc + 2)
        return n + _extension(mem, op >> 6 & 7, op >> 9 & 7, size, pc + n)
    n = 4 if op & 0xFB80 == 0x4880 else 2       # MOVEM's mask, after it
    return n + _extension(mem, op >> 3 & 7, op & 7, 0, pc + n)


SIZES = (1, 2, 4)       # the bytes of an operation by its size field, 0 to 2
LONGEST = 22            # bytes, a 68020's longest instruction
REACH = 64              # bytes a MOVEM stores, on from the first


def _step(mem, at: int) -> tuple[int, bool] | None:
    """How long the instruction at at is, and whether the core ends a block
    of code with it, as it does at each branch, jump, return and trap; or
    None if it isn't one known here."""
    if at + 2 > len(mem):
        return None
    op = struct.unpack_from(">H", mem, at)[0]
    group, mode, reg, size = op >> 12, op >> 3 & 7, op & 7, op >> 6 & 3

    def ea(n: int, operand: int) -> int:
        """n bytes, then the extension words of the effective address."""
        return n + _extension(mem, mode, reg, operand, at + n)
    if 1 <= group <= 3:                         # MOVE, MOVEA
        return _length(mem, op, at), False
    if group == 6:                              # Bcc, BRA, BSR
        return {0: 4, 0xFF: 6}.get(op & 0xFF, 2), True
    if group == 7:
        return None if op & 0x100 else (2, False)       # MOVEQ
    if group == 0xA:                            # a trap
        return 2, True
    if group == 5:
        if size != 3:                           # ADDQ, SUBQ
            return ea(2, SIZES[size]), False
        if mode == 1:                           # DBcc
            return 4, True
        return None if mode == 7 and reg > 1 else (ea(2, 1), False)     # TRAPcc, Scc
    if group in (8, 9, 0xB, 0xC, 0xD):
        if size == 3:                           # DIVx, SUBA, CMPA, MULx, ADDA
            return ea(2, 4 if op & 0x100 and group in (9, 0xB, 0xD) else 2), False
        if op & 0x100 and mode < 2:             # SBCD, PACK, UNPK, SUBX, CMPM, EOR, ABCD, EXG, ADDX
            return 4 if group == 8 and size else 2, False
        return ea(2, SIZES[size]), False        # OR, SUB, CMP, EOR, AND, ADD
    if group == 0xE:                            # shifts and rotates, and bit fields
        return (ea(4 if op & 0x800 else 2, 2) if size == 3 else 2), False
    if group == 0:
        if op & 0x100:                          # MOVEP, or BTST, BCHG, BCLR, BSET Dn
            return (4 if mode == 1 else ea(2, 1)), False
        if op >> 9 & 7 == 4:                    # BTST, BCHG, BCLR, BSET #
            return ea(4, 1), False
        if op >> 9 & 7 in (0, 1, 2, 3, 5, 6) and size != 3:     # ORI, ANDI, SUBI...
            if mode == 7 and reg == 4:          # to CCR or SR
                return 4, False
            return ea(6 if size == 2 else 4, 0), False
        return None
    if group != 4:                              # line F
        return None
    if op in (0x4E70, 0x4E71):                  # RESET, NOP
        return 2, False
    if op in (0x4E72, 0x4E73, 0x4E74, 0x4E75, 0x4E77):  # STOP, RTE, RTD, RTS, RTR
        return (4 if op in (0x4E72, 0x4E74) else 2), True
    if op & 0xFFF0 == 0x4E40:                   # TRAP
        return 2, True
    if op & 0xFF80 == 0x4E80:                   # JSR, JMP
        return ea(2, 4), True
    if op & 0xFFF8 == 0x4E50 or op & 0xFFF8 == 0x4808:   # LINK
        return (4 if op & 0xFFF8 == 0x4E50 else 6), False
    if op & 0xFFF0 == 0x4E60 or op & 0xFFF8 in (0x4E58, 0x4840, 0x49C0):  # MOVE USP, UNLK, SWAP...
        return 2, False
    if op & 0xFB80 == 0x4880:                   # EXT, MOVEM
        return (2 if mode == 0 else ea(4, 0)), False
    if op & 0xFFC0 == 0x4840 or op & 0xF1C0 in (0x41C0, 0x4100):     # PEA, LEA, CHK.L
        return ea(2, 4), False
    if op & 0xF1C0 == 0x4180:                   # CHK
        return ea(2, 2), False
    if op & 0xFF80 == 0x4C00:                   # MULx.L, DIVx.L
        return ea(4, 4), False
    if op & 0xF900 == 0x4000:                   # NEGX, CLR, NEG, NOT, MOVE from or to SR or CCR
        return ea(2, SIZES[size] if size != 3 else 2), False
    if op & 0xFFC0 == 0x4800:                   # NBCD
        return ea(2, 1), False
    if op & 0xFF00 == 0x4A00:                   # TST, TAS, ILLEGAL
        return (2, True) if op == 0x4AFC else (ea(2, SIZES[size] if size != 3 else 1), False)
    return None


def block_cycles(mem, at: int, size: int) -> int:
    """The cycles a block of code size bytes long at at takes a 68000, as
    cycles() has each instruction; past one _step doesn't know, four for
    each word."""
    end, n = at + size, 0
    while at < end:
        n += cycles(mem, at)
        step = _step(mem, at)
        if step is None:
            return n + 2 * max(0, end - at - 2)
        at += step[0]
    return n


def _run_end(mem, pc: int) -> int:
    """Where the block of code the core's running, from pc on, must end:
    after the first instruction that ends one, or else a little past the
    page's end, as the core ends a block in its page but for an instruction
    that runs over into the next."""
    end = (pc | PAGE_SIZE - 1) + 1
    at = pc
    while at < end:
        step = _step(mem, at)
        if step is None:
            break
        at += step[0]
        if step[1]:
            return at
    return end + LONGEST


def is_trapcc(op: int) -> bool:
    return op & 0xF0F8 == 0x50F8 and op & 7 in TRAPCC_SIZES


def is_move_to_sr(op: int) -> bool:
    mode, reg = op >> 3 & 7, op & 7
    return op & 0xFFC0 == 0x46C0 and mode != 1 and (mode != 7 or reg <= 4)


def condition(cc: int, ccr: int) -> bool:
    """Whether condition cc (Bcc's, DBcc's, Scc's and TRAPcc's 4 bits) holds."""
    n, z, v, c = (bool(ccr & bit) for bit in (CCR_N, CCR_Z, CCR_V, CCR_C))
    test = (True, not c and not z, not c, not z, not v, not n, n == v, n == v and not z)[cc >> 1]
    return test != bool(cc & 1)


def _ccr(op: int, x: int, n: int, v: int, c: int, z: int) -> int:
    """The condition codes from the core's cc_op, cc_x, cc_n, cc_v, cc_c
    and cc_z.  X is always X; N is in bit 31 of n, Z is n or z being 0, V
    bit 31 of v; after an ADD or SUB, n is the result and v the source, and
    C is X; after a CMP, n is the destination and v the source."""
    if CC_OP_ADD <= op < CC_OP_LOGIC:
        size = (op - CC_OP_ADD) % 3
        if op < CC_OP_SUB:
            dest = _extend(n - v, size)
            v, c, z = (n ^ dest) & ~(dest ^ v), x, n
        elif op < CC_OP_CMP:
            dest = _extend(n + v, size)
            v, c, z = (n ^ dest) & (dest ^ v), x, n
        else:
            dest, n = n, _extend(n - v, size)
            v, c, z = (n ^ dest) & (dest ^ v), dest < v, n
    elif op == CC_OP_LOGIC:
        v, c, z = 0, 0, n
    return ((CCR_X if x else 0) | (CCR_N if n >> 31 & 1 else 0) | (CCR_Z if not z else 0)
            | (CCR_V if v >> 31 & 1 else 0) | (CCR_C if c else 0))


class Event(NamedTuple):
    """Why run() returned."""
    kind: str           # "trap", "exception", "fault", "until", "interrupted"
    pc: int
    code: int = 0       # the trap word, the vector number, or unicorn's error number


def mac_time() -> int:
    """Seconds since midnight, 1 January 1904, local time, like the Time global."""
    return (calendar.timegm(time.localtime()) + MAC_1904) & 0xFFFFFFFF


class CPU:
    def __init__(self, mem: RAM):
        self.mem = mem
        self.uc = uc = Uc(UC_ARCH_M68K, UC_MODE_BIG_ENDIAN)
        # Unicorn frees the core as the interpreter exits, and an
        # application's thread still running in it then crashes the
        # interpreter.  The memory goes back with the process anyway.
        finalizer = getattr(uc, "_Uc__finalizer", None)
        if finalizer is not None:
            finalizer.atexit = False
        uc.ctl_set_cpu_model(UC_CPU_M68K_M68020)
        uc.ctl_set_tlb_mode(UC_TLB_VIRTUAL)
        self._backing = (ctypes.c_char * mem.size).from_buffer(mem._b)
        uc.mem_map_ptr(0, mem.size, UC_PROT_ALL, ctypes.addressof(self._backing))
        self._pages = bytearray(1 << (24 - PAGE_BITS))          # CODE, WRITTEN, MIXED
        self._tops = bytearray(1 << (24 - PAGE_BITS))           # the top byte it's mapped by
        self._ran = bytearray(1 << (24 - PAGE_BITS))            # may have code translated from it
        self._turns = bytearray(1 << (24 - PAGE_BITS))          # up to BOTH_AFTER, or NEAR
        self._both: dict[int, bytes] = {}   # the pages mapped for both, as last looked through
        self._rerun: int | None = None      # a store the core stopped at, to run again
        self._let_by: int | None = None     # the one it's running again
        self._fetched = b""                 # the words of the store as it was run
        self._trapcc: int | None = None     # a TRAPcc, TRAPV, RTR or JSR (SP) the core stopped at
        self._watched: set[int] = set()     # where they're watched for, and SR's writes
        self._step: int | None = None       # a write to SR to run alone, stopped at
        self._stepping = False              # running one instruction
        # Whether T1 may be set: reading SR to see is slow, and T1 is set
        # only by a write to SR, which the core is stopped at, an RTE or
        # Python.
        self._may_trace = False
        self.traced = 0                     # the instruction a trace exception is for
        self._looked: list[int | None] = [None] * len(self._pages)    # through which top byte
        uc.hook_add(UC_HOOK_TLB_FILL, self._tlb_fill)
        uc.hook_add(UC_HOOK_INTR, self._intr)
        self._ctx = uc.context_save()
        self._state = self._find_state()
        uc.reg_write(UC_M68K_REG_SR, 0x2700)    # SR before A7: it picks the stack
        for n in range(8):
            self.set_d(n, 0)
            self.set_a(n, 0)
        self._event: Event | None = None
        self.interrupt_requested = False
        self.retrace_pending = False    # a tick has gone by with VBL tasks queued
        self.timers_waiting: Callable[[], bool] = lambda: False    # Time Manager tasks
        self.sound_waiting: Callable[[], bool] = lambda: False     # the Sound Driver, the speaker
        self.rom_interrupts = 0         # what LEVEL_1's vector is until a program sets it
        self.rom_level_1_routines = (0, 0)      # where Lvl1DT's routines are until then
        self.short_frames = False       # a 68000's exception frames: SR and PC, no more
        self._counting = False          # whether the last run was in slices
        self._unsliced = False          # a run without them is going on
        self.on_tick: Callable[[], None] | None = None     # called by the clock, each tick
        self._rate: int | None = None   # a 68000's cycles a second at most, or as fast as it goes
        self._pacer: int | None = None  # the hook that counts them
        self._paced_from, self._paced = 0.0, 0                 # since when, how many
        self._owed = 0                  # run since the last wait for the clock
        self._blocks: dict[int, int] = {}   # each block's cycles, by its address and length
        self._closed = threading.Event()
        self._set_clock()           # now, not a tick on: a program may read Ticks first thing
        threading.Thread(target=self._clock, name="m68k clock", daemon=True).start()

    def close(self) -> None:
        self._closed.set()

    @property
    def rate(self) -> int | None:
        """The 68000 cycles a second the core is held to, or None for as
        fast as it goes."""
        return self._rate

    @rate.setter
    def rate(self, rate: int | None) -> None:
        if self._pacer is not None:
            self.uc.hook_del(self._pacer)
            self._pacer = None
        self._rate = rate or None
        if rate:
            self._paced_from, self._paced, self._owed = time.monotonic(), 0, 0
            self._pacer = self.uc.hook_add(UC_HOOK_BLOCK, self._paced_block)
        self.uc.ctl_flush_tb()          # blocks translated before would or wouldn't call it

    # =====================================================================
    # Registers
    # =====================================================================
    def d(self, n: int) -> int:
        return self.uc.reg_read(UC_M68K_REG_D0 + n) & 0xFFFFFFFF

    def a(self, n: int) -> int:
        return self.uc.reg_read(UC_M68K_REG_A0 + n) & 0xFFFFFFFF

    def set_d(self, n: int, value: int) -> None:
        self.uc.reg_write(UC_M68K_REG_D0 + n, value & 0xFFFFFFFF)

    def set_a(self, n: int, value: int) -> None:
        self.uc.reg_write(UC_M68K_REG_A0 + n, value & 0xFFFFFFFF)

    @property
    def pc(self) -> int:
        return self.uc.reg_read(UC_M68K_REG_PC) & 0xFFFFFFFF

    @pc.setter
    def pc(self, value: int) -> None:
        self.uc.reg_write(UC_M68K_REG_PC, value & 0xFFFFFFFF)

    @property
    def sp(self) -> int:
        return self.uc.reg_read(UC_M68K_REG_A7) & 0xFFFFFFFF

    @sp.setter
    def sp(self, value: int) -> None:
        self.uc.reg_write(UC_M68K_REG_A7, value & 0xFFFFFFFF)

    @property
    def sr(self) -> int:
        """SR, the condition codes worked out from a copy of the core's
        state, as unicorn's read of SR wouldn't, and left as they were."""
        if self._state is None:
            return self.uc.reg_read(UC_M68K_REG_SR) & 0xFFFF
        self.uc.context_update(self._ctx)
        state, at = self._state, self._state_at
        return state[at + SR_WORD] & 0xFFE0 | _ccr(*state[at + CC_WORDS:at + CC_WORDS + 6])

    @sr.setter
    def sr(self, value: int) -> None:
        sp = self.sp
        self.uc.reg_write(UC_M68K_REG_SR, value & 0xFFFF)
        self.sp = sp
        self._may_trace = bool(value & T1)

    def _find_state(self):
        """The context's copy of the core's state (QEMU's CPUM68KState), as
        32-bit words, found by the registers; None, and unicorn's SR, if it
        isn't laid out as expected."""
        uc = self.uc
        uc.reg_write(UC_M68K_REG_SR, 0x2700 | CCR_X | CCR_V)
        marks = [0x6D616300 + n for n in range(16)]
        for n in range(8):
            self.set_d(n, marks[n])
            self.set_a(n, marks[8 + n])
        uc.context_update(self._ctx)
        state = (ctypes.c_uint32 * (self._ctx.size // 4)).from_address(self._ctx.context.value)
        words = list(state)
        at = next((i for i in range(len(words) - CC_WORDS - 6) if words[i:i + 16] == marks),
                  None)
        flags = [CC_OP_FLAGS, 1, 0, 0xFFFFFFFF, 0, 1]     # X and V, as cc_op, cc_x, ... cc_z
        if at is None or words[at + SR_WORD] != 0x2700 or words[
                at + CC_WORDS:at + CC_WORDS + 6] != flags:
            log.warning("unicorn's m68k state isn't as expected: SR's condition codes may be wrong")
            return None
        self._state_at = at
        return state

    def set_cc_from_word(self, value: int) -> None:
        """TST.W: N and Z from a word, V and C clear, X kept."""
        value &= 0xFFFF
        cc = (0x8 if value & 0x8000 else 0) | (0x4 if value == 0 else 0)
        self.sr = (self.sr & ~0xF) | cc

    def context(self):
        return self.uc.context_save()

    def restore(self, ctx) -> None:
        self.uc.context_restore(ctx)
        self._may_trace = True

    def registers(self) -> str:
        """A MacsBug-style register dump, for error reports."""
        d = " ".join(f"{self.d(i):08X}" for i in range(8))
        a = " ".join(f"{self.a(i):08X}" for i in range(8))
        return f"PC {self.pc:08X}  SR {self.sr:04X}\n  D0-D7 {d}\n  A0-A7 {a}"

    # =====================================================================
    # Stack
    # =====================================================================
    def push16(self, value: int) -> None:
        sp = self.sp - 2
        self.mem.w16(sp, value)
        self.sp = sp

    def push32(self, value: int) -> None:
        sp = self.sp - 4
        self.mem.w32(sp, value)
        self.sp = sp

    def pop16(self) -> int:
        sp = self.sp
        self.sp = sp + 2
        return self.mem.r16(sp)

    def pop32(self) -> int:
        sp = self.sp
        self.sp = sp + 4
        return self.mem.r32(sp)

    # =====================================================================
    # Running
    # =====================================================================
    def run(self, until: int) -> Event:
        """Run from PC until PC reaches until, a trap or exception comes
        up, or interrupt() is called; and, while VBL tasks are queued, for
        SLICE instructions at most, or with rate set, about a tick's worth."""
        for start, end in self.mem.take_dirty():
            for page in range(start >> PAGE_BITS, ((end - 1) >> PAGE_BITS) + 1):
                if not self._ran[page]:         # nothing to drop: see _tlb_fill
                    self._looked[page] = None
                    continue
                base = page << PAGE_BITS
                lo, hi = max(start, base), min(end, base + PAGE_SIZE)
                self.uc.ctl_remove_cache(lo, hi)    # the blocks of what's written, no others
                if self._pages[page] & CODE:    # still mapped to be run from
                    self._watch(self._tops[page] << 24 | base, (lo - base, hi - base))
                else:
                    self._looked[page] = None
        for page, seen in self._both.items():
            base = page << PAGE_BITS
            if self.mem._b[base:base + PAGE_SIZE] != seen:
                self._look_again(page)
        pc = first = self.pc
        if pc == until:
            return Event("until", pc)
        if self.interrupt_requested or (self.retrace_pending and not self.sr & INTERRUPT_MASK):
            return Event("interrupted", pc)
        self._event = None
        rate = self._rate
        slicing = self._interrupted_at_ticks()
        count = SLICE if slicing else 0
        if rate:
            slicing, count = True, min(count or SLICE, max(1, rate // 60 // CYCLES_EACH))
        self._may_trace = traced = self._may_trace and bool(self.sr & T1)
        self._stepping = stepping = traced or self._step is not None
        if stepping:                    # one instruction, alone
            slicing, count = True, 1
        if slicing and not self._counting:
            self.uc.ctl_flush_tb()      # blocks translated before wouldn't count
        self._counting = slicing        # (and unicorn flushes them itself when it stops counting)
        stop = until
        while True:
            self._let_by, self._rerun = self._rerun, None
            self._unsliced = not slicing
            try:
                self.uc.emu_start(pc, stop, count=count)
            except UcError as e:
                return Event("fault", self.pc, e.errno)
            finally:
                self._unsliced = False
            if self._event is not None:
                return self._event
            if self._trapcc is not None:
                at, self._trapcc = self._trapcc, None
                op = struct.unpack_from(">H", self.mem._b, at & ADDRESS_MASK)[0]
                if op == JSR_SP:
                    pc = self.sp
                    self.push32(at + 2)
                elif op == TRAPV_WORD:
                    if condition(VS, self.sr):
                        return Event("exception", at, TRAPV)
                    pc = at + 2
                elif op == RTR:
                    sp = self.sp
                    self.sr = self.sr & 0xFF00 | self.mem.r16(sp & ADDRESS_MASK) & 0x1F
                    pc = self.mem.r32((sp + 2) & ADDRESS_MASK)
                    self.sp = sp + 6
                else:
                    if condition(op >> 8 & 0xF, self.sr):
                        return Event("exception", at, TRAPV)
                    pc = at + TRAPCC_SIZES[op & 7]
                if stepping:
                    self.pc = pc
                    break
                stop = until
                continue
            if self._rerun is None:
                if (stop == until or self.pc != stop or self.interrupt_requested
                        or self.retrace_pending):
                    break
                pc, stop = stop, until      # the store's made: on, from what's there now
                continue
            at = self._rerun & ADDRESS_MASK     # as it was, if it stored to itself
            self.mem._b[at:at + len(self._fetched)] = self._fetched
            if self.interrupt_requested:
                return Event("interrupted", self.pc)    # the store's run again next time
            pc = self._rerun                            # run the store again, alone
            stop = pc + len(self._fetched)
        if stepping:
            self._step = None
            self._may_trace = True      # as the instruction left it
            if traced:
                self.traced = first
                return Event("exception", self.pc, TRACE)
        elif self._step is not None:
            self.pc = self._step        # to run it alone, next
        pc = self.pc
        if pc == until:
            return Event("until", pc)
        return Event("interrupted", pc)

    def _paced_block(self, uc, addr: int, size: int, _data) -> None:
        """A block of code is about to run, with rate set: count the cycles
        it takes a 68000, and once a millisecond's worth have run, wait
        until they're due.  A block counted is counted as it was, if its
        code is written over: near enough."""
        key = addr << 16 | size
        took = self._blocks.get(key)
        if took is None:
            took = self._blocks[key] = block_cycles(self.mem._b, addr & ADDRESS_MASK, size)
        self._owed += took
        rate = self._rate
        if rate and self._owed * PACED >= rate:
            self._pace(rate)

    def _pace(self, rate: int) -> None:
        """The cycles owed have run: wait until they're due.  Time lost to
        traps, or to another application's turn, is let go."""
        now = time.monotonic()
        self._paced += self._owed
        self._owed = 0
        due = self._paced_from + self._paced / rate
        if due < now - 0.1:
            self._paced_from, self._paced = now, 0
        elif due > now:
            time.sleep(due - now)

    def own_interrupts(self) -> bool:
        """Whether the application has put its own handler for the VIA's
        interrupts in their vector, or its own routine for one of them in
        Lvl1DT."""
        vector = struct.unpack_from(">I", self.mem._b, 4 * LEVEL_1)[0] & ADDRESS_MASK
        return vector not in (0, self.rom_interrupts) or bool(self.own_level_1_routines())

    def own_level_1_routines(self) -> int:
        """The VIA's flags, as in its vIFR, that the application has put its
        own routine in Lvl1DT for."""
        lo, hi = self.rom_level_1_routines
        routines = struct.unpack_from(">8I", self.mem._b, lowmem.Lvl1DT)
        return sum(1 << n for n, at in enumerate(routines)
                   if at & ADDRESS_MASK and not lo <= at & ADDRESS_MASK < hi)

    def _interrupted_at_ticks(self) -> bool:
        """Whether there's something to do at each tick: a VBL task
        counting down, a Time Manager task waiting, the application's own
        interrupt handler, or a sound to make or play."""
        return (self.vbl_counting() or self.timers_waiting() or self.own_interrupts()
                or self.sound_waiting())

    def vbl_counting(self) -> bool:
        """Whether a task in the VBL queue is counting down to run.  One
        that isn't, as the System's own never is, waits for nothing."""
        b = self.mem._b
        task = struct.unpack_from(">I", b, lowmem.VBLQueue + 2)[0] & ADDRESS_MASK
        for _ in range(MAX_VBL_TASKS):
            if not task:
                return False
            if task + VBL_TASK_END > len(b) or struct.unpack_from(">h", b, task + VBL_COUNT)[0] > 0:
                return True
            task = struct.unpack_from(">I", b, task)[0] & ADDRESS_MASK
        return True                     # a queue that goes round: look at each tick

    @contextmanager
    def timed(self, clock) -> Iterator[None]:
        """Add to the clock's count, while it counts, the time each
        instruction run would take a Mac's 68000 (cycles): for a program's
        own routine that reads a disk byte by byte and times the bytes by
        its instructions.  Each is counted before it's run, so what it
        reads is read as it ends, as the 68000 reads an operand last; a
        branch that wasn't taken, once the next has come instead."""
        took: dict[int, tuple[float, float, int]] = {}
        store = self.mem._b
        after, more = -1, 0.0           # where a branch goes on if not taken, and its time then

        def hook(uc, addr, _size, _data) -> None:
            # The core says every instruction is two bytes long.
            nonlocal after, more
            if addr == after:
                clock.counted += more
            known = took.get(addr)
            if known is None:
                at = addr & ADDRESS_MASK
                extra, length = untaken(store, at)
                known = took[addr] = (cycles(store, at) / RATE, extra / RATE,
                                      addr + length if length else -1)
            clock.counted += known[0]
            after, more = known[2], known[1]

        handle = self.uc.hook_add(UC_HOOK_CODE, hook)
        self.uc.ctl_flush_tb()          # blocks translated before wouldn't call it
        try:
            yield
        finally:
            self.uc.hook_del(handle)
            self.uc.ctl_flush_tb()

    def flush_code(self) -> None:
        """Drop every block of code the core has translated, for a program
        that says it has written code (FlushCodeCache), as a 68020's
        must.  The core sees to most code that changes, but MacChess's
        loader rewrote its jump table and ran the entries as they were."""
        self.uc.ctl_flush_tb()

    def interrupt(self) -> None:
        """Make run() return soon, to kill the process: safe from any
        thread, but the instruction it stops in may run again after."""
        self.interrupt_requested = True
        self.uc.emu_stop()

    def _intr(self, uc, intno, _data) -> None:
        pc = uc.reg_read(UC_M68K_REG_PC) & 0xFFFFFFFF
        if intno == LINE_A:
            word = struct.unpack_from(">H", self.mem._b, pc & ADDRESS_MASK)[0]
            self._event = Event("trap", pc, word)
        elif intno == ILLEGAL and self.mem.r16(pc & ADDRESS_MASK) in (TRAPV_WORD, RTR):
            self._trapcc = pc               # for run() to do, as a TRAPcc
        else:
            self._event = Event("exception", pc, intno)
        uc.emu_stop()

    def _tlb_fill(self, uc, vaddr, access, entry, _data) -> bool:
        """Map a page for the core: any page may be read, written and run
        from, but it's mapped to be run from or written to, not both, so
        that the core comes back here when it changes from one to the
        other, and code that writes code can be seen to.

        Dropping a page's blocks maps it to be run from, as the core looks
        for the page's blocks by where it's run from.  So run() drops the
        blocks of a page Python has written to only if it may have some:
        if it's been mapped to be run from since it was last mapped to be
        written to, its blocks dropped.  Otherwise each trap that leaves a
        result on the stack would have its page taken for code, and each
        push after it would drop the page's blocks, there being none:
        Railroad Tycoon took minutes to make its map.

        A page that's run from and written to by turns, often, by code on
        other pages, is mapped for both at once: Word 1.0's interpreter
        keeps its variables beside the loop it runs each instruction from,
        and the core took twice for each.  A store from code on the page
        that can't be to the block it's in counts as one from another page
        (see _code_written).  Mapped to be run from, a page
        has each store to it looked at by the core itself, which drops the
        blocks the store is to.  But not the one it's running, which runs
        on as it was, so a page that code which may be running from it
        has written to stays mapped for one or the other.

        An address the memory mirrors is mapped to the page it's another
        way to (see RAM.mirror)."""
        paddr = vaddr & ADDRESS_MASK
        mem = self.mem
        if mem.mirror_lo <= paddr < mem.mirror_hi:
            paddr -= mem.mirror_by
        entry.paddr = paddr
        page = paddr >> PAGE_BITS
        top = vaddr >> 24
        state = self._pages[page]
        if not state:
            self._tops[page] = top
        elif self._tops[page] != top:
            state |= MIXED
        if self._turns[page] == BOTH_AFTER:
            return self._map_both(vaddr, page, state, entry)
        if access == UC_MEM_FETCH:
            self._ran[page] = 1
            self._watch(vaddr & ~(PAGE_SIZE - 1))
            if state & WRITTEN and state & MIXED:   # it may be written through another address
                state = self._forget(uc, page, top)
            state = (state | CODE) & ~WRITTEN       # (or through this one, mapped afresh now)
            entry.perms = READ_RUN
        elif access != UC_MEM_WRITE and state & CODE:
            entry.perms = READ_RUN                  # read: a store still comes here
        else:
            near = access == UC_MEM_WRITE and self._code_written(uc, page)
            if near:
                self._turns[page] = NEAR
            elif access == UC_MEM_WRITE and state & CODE and self._turns[page] < BOTH_AFTER:
                self._turns[page] += 1
                if self._turns[page] == BOTH_AFTER:
                    if state & MIXED:               # mapped through another address for one
                        state = self._forget(uc, page, top)
                    return self._map_both(vaddr, page, state, entry)
            if near or state & CODE:
                uc.ctl_remove_cache(vaddr, vaddr + PAGE_SIZE)
                state = self._pages[page] | (state & MIXED)     # as mapping it to drop them left it
                if state & MIXED:                   # it may be run from through another address
                    state = self._forget(uc, page, top)
                state &= ~CODE
                self._ran[page] = 0
            state |= WRITTEN
            self._looked[page] = None
            entry.perms = READ_WRITE
        self._pages[page] = state
        return True

    def _map_both(self, vaddr: int, page: int, state: int, entry) -> bool:
        """Map a page to be run from and written to: see _tlb_fill.  It's
        looked through for TRAPcc afresh, having maybe been written to."""
        entry.perms = UC_PROT_ALL
        self._ran[page] = 1
        self._looked[page] = None
        self._watch(vaddr & ~(PAGE_SIZE - 1))
        base = page << PAGE_BITS
        self._both[page] = bytes(self.mem._b[base:base + PAGE_SIZE])
        self._pages[page] = state | CODE | WRITTEN
        return True

    def _look_again(self, page: int) -> None:
        """Look through a page mapped for both again, the core having
        written to it, which it does without coming to _tlb_fill: and drop
        its blocks if there's a word to stop at in it that there wasn't, for
        the core to stop at it.  The Surgeon decrypts its ORI #$8000,SR on
        a page that its loader's loop at the end of it has made one, and
        runs it a few traps on.  Code that writes such a word and runs it
        with no trap between still isn't seen to."""
        base = page << PAGE_BITS
        watched = len(self._watched)
        self._watch(self._tops[page] << 24 | base, (0, PAGE_SIZE))
        self._both[page] = bytes(self.mem._b[base:base + PAGE_SIZE])
        if len(self._watched) != watched:
            self.uc.ctl_remove_cache(base, base + PAGE_SIZE)

    def _watch(self, base: int, written: tuple[int, int] | None = None) -> None:
        """Stop the core at each TRAPcc and JSR (SP) in the page at base, a
        virtual address, before it runs it, and each write to SR that sets
        T1.  The core is let run on through a word that's an operand, and
        that has a hook only at the instructions it translates from then
        on, so the page is looked through before it runs from it: as it's
        mapped, and after Python writes to it, if it's mapped already.
        It's looked through again only once it may have been written to:
        just the part Python wrote, from written's start to its end in the
        page, if the rest has been."""
        at = self.mem.physical(base)
        page, top = at >> PAGE_BITS, base >> 24
        if self._looked[page] != top:
            start, end = 0, PAGE_SIZE
            self._looked[page] = top
        elif written is None:
            return
        else:
            start, end = written[0] & ~1, min(written[1] + 1, PAGE_SIZE)   # and a word half written
        for found in WATCHED.finditer(self.mem._b, at + start, at + end):
            addr = base + found.start() - at
            if not addr & 1 and addr not in self._watched:
                self._watched.add(addr)
                self.uc.hook_add(UC_HOOK_CODE, self._stop_at, begin=addr, end=addr)

    def _stop_at(self, uc, addr, _size, _data) -> None:
        """Stop before the TRAPcc or JSR (SP) at addr, for run() to do:
        when the core stops, it has the condition codes as of the
        instruction, where now it may not.  Or before a write to SR that
        sets T1, for run() to run it alone, and trace from there."""
        op = struct.unpack_from(">H", self.mem._b, addr & ADDRESS_MASK)[0]
        if is_trapcc(op) or op == JSR_SP:
            self._trapcc = addr
            uc.emu_stop()
        elif (op in (ORI_SR, EORI_SR) or is_move_to_sr(op)) and not self._stepping \
                and self._sets_trace(op, addr & ADDRESS_MASK):
            self._step = addr
            uc.emu_stop()

    def _sets_trace(self, op: int, at: int) -> bool:
        """Whether the write to SR at at sets T1: by what it writes, if
        that can be told before it's run, as it can for the usual ways of
        addressing it; otherwise, it may."""
        mem = self.mem
        try:
            if op in (ORI_SR, EORI_SR):
                return bool(mem.r16(at + 2) & T1)
            mode, reg = op >> 3 & 7, op & 7
            if mode == 0:
                value = self.d(reg)
            elif mode == 7 and reg == 4:
                value = mem.r16(at + 2)
            elif mode in (2, 3, 4, 5):
                addr = self.a(reg) - (2 if mode == 4 else 0)
                if mode == 5:
                    addr += mem.r16s(at + 2)
                value = mem.r16(addr)
            else:
                return True
        except BusError:
            return True
        return bool(value & T1)

    def _forget(self, uc, page: int, top: int) -> int:
        """Drop every page's mapping, to map them afresh: but page, through
        an address with top in its top byte, the one being mapped now."""
        uc.ctl(UC_CTL_TLB_FLUSH, UC_CTL_IO_WRITE)
        self._pages[:] = bytes(len(self._pages))
        self._tops[page] = top
        return 0

    def _code_written(self, uc, page: int) -> bool:
        """A store to a page, mapped for it: whether it may be to the block
        of code the core's running, being from code on that page (or the
        one before: a block can run into the next).  The page's blocks are
        dropped, but the one the core's running runs on as it was; so if
        the store may be to an instruction further on in it, the core
        stops, its store made, to run it again alone, and what follows it
        in a block translated from what's there once it has stored: so a
        second store to the block is seen to as the first was.  The
        Angelsoft adventures' p-Machine calls each trap by storing its two
        bytes in turn in an instruction a few on: run as translated after
        the first, its $0094 was an ORI.L to the p-code.  Only an instruction
        that stores the same again is run again: a MOVE, MOVEM or CLR, not
        an ADDQ to a count a program keeps among its code.  It's run again
        as it was fetched, its words put back as they were if the store was
        to them, and its store made again: Uninvited's last loop clears the
        code it decrypted, its own instruction at last, and runs on.

        A MOVE, MOVEM or CLR to further off than the block can reach is
        taken as a store from another page: Fontographer keeps the buffer
        for a file it writes in its code's page, and stores to it a byte at
        a time.  Only the first store since the page was run from is looked
        at, as before: the block runs on through any after."""
        at = uc.reg_read(UC_M68K_REG_PC) & 0xFFFFFFFF
        pc = at & ADDRESS_MASK
        if page - (pc >> PAGE_BITS) not in (0, 1):
            return False
        if at == self._let_by:
            self._let_by = None
            return True
        op = struct.unpack_from(">H", self.mem._b, pc)[0]
        if 0x1000 <= op < 0x4000 or op & 0xFB80 == 0x4880 or op & 0xFF00 == 0x4200:
            to = self._stored_at(op, pc)
            if to is not None and not pc - REACH < to < _run_end(self.mem._b, pc) + REACH:
                return False
            self._rerun = at            # with its top byte, as the program runs it
            self._fetched = bytes(self.mem._b[pc:pc + _length(self.mem._b, op, pc)])
            uc.emu_stop()
        return True

    def _stored_at(self, op: int, pc: int) -> int | None:
        """Where the MOVE, MOVEM or CLR at pc, op its first word, stores to,
        from the registers as the core has them as it stores, an (An)+ or
        -(An) not yet counted: or None, for a 68020's full extension word.
        The core gives _tlb_fill only the page."""
        mem = self.mem._b
        if op < 0x4000:
            size = (0, 1, 4, 2)[op >> 12 & 3]
            mode, reg = op >> 6 & 7, op >> 9 & 7
            at = pc + 2 + _extension(mem, op >> 3 & 7, op & 7, size, pc + 2)
        elif op & 0xFB80 == 0x4880:
            size, mode, reg, at = 2 << (op >> 6 & 1), op >> 3 & 7, op & 7, pc + 4
        else:
            size, mode, reg, at = SIZES[op >> 6 & 3], op >> 3 & 7, op & 7, pc + 2
        if at + 4 > len(mem) or mode < 2 or mode == 7 and reg > 1:
            return None
        ext = struct.unpack_from(">H", mem, at)[0]
        if mode == 7:
            return (_extend(ext, 1) if reg == 0 else struct.unpack_from(">L", mem, at)[0]) \
                & ADDRESS_MASK
        addr = self.a(reg)
        if mode == 4:
            addr -= size
        elif mode == 5:
            addr += _extend(ext, 1)
        elif mode == 6:
            if ext & 0x100:
                return None
            index = (self.a if ext & 0x8000 else self.d)(ext >> 12 & 7)
            if not ext & 0x800:
                index = _extend(index, 1)
            addr += _extend(ext, 0) + (index << (ext >> 9 & 3))
        return addr & ADDRESS_MASK

    # =====================================================================
    # Exceptions the application handles itself
    # =====================================================================
    def take_exception(self, vector: int, pc: int, instruction: int | None = None) -> None:
        """Exception processing, as the 68020 does it: stack a frame and go
        to the handler in the vector table.  instruction, if given, makes a
        format 2 frame (CHK, TRAPV, divide by zero, trace).  With
        short_frames, as the 68000 does it: SR and PC."""
        sr = self.sr
        sp = self.sp
        if self.short_frames:
            sp -= 6
        else:
            fmt = 0
            if instruction is not None:
                sp -= 4
                self.mem.w32(sp, instruction)
                fmt = 2
            sp -= 8
            self.mem.w16(sp + 6, (fmt << 12) | (vector * 4))
        self.mem.w16(sp, sr)
        self.mem.w32(sp + 2, pc)
        self.uc.reg_write(UC_M68K_REG_SR, (sr | SUPERVISOR) & ~TRACE_BITS)
        self._may_trace = False
        self.sp = sp
        self.pc = self.mem.r32(vector * 4)

    def return_from_exception(self) -> None:
        """RTE"""
        sp = self.sp
        sr, pc = self.mem.r16(sp), self.mem.r32(sp + 2)
        if self.short_frames:
            size = 6
        else:
            fmt = self.mem.r16(sp + 6) >> 12
            size = 8 + {2: 4, 3: 4, 4: 8, 9: 12, 0xA: 24, 0xB: 84}.get(fmt, 0)
        self.uc.reg_write(UC_M68K_REG_SR, sr)
        self._may_trace = bool(sr & T1)
        self.sp = sp + size
        self.pc = pc

    # =====================================================================
    # The clock
    # =====================================================================
    def _set_clock(self) -> None:
        """Ticks and Time in low memory, straight into the bytes: these
        pages hold no code to flush."""
        b = self.mem._b
        struct.pack_into(">I", b, lowmem.Ticks, tick_count() & 0xFFFFFFFF)
        struct.pack_into(">I", b, lowmem.Time, mac_time())

    def _clock(self) -> None:
        """At each tick, just after it: Ticks is what tick_count() says,
        and a program that reads it after TickCount sees the ticks gone by
        since, no fewer."""
        while not self._closed.wait(to_next_tick() / 60 + SETTLE):
            self._set_clock()
            try:
                if self._interrupted_at_ticks():
                    self.retrace_pending = True     # for the end of run()'s slice
                    if self._unsliced:
                        self.uc.emu_stop()      # a run with none, from before there was this to do
                if self.interrupt_requested:
                    self.uc.emu_stop()      # in case the first stop came between runs
                on_tick = self.on_tick
                if on_tick is not None:
                    on_tick()
            except Exception:           # noqa: BLE001 - the clock goes on
                log.exception("at a tick")
