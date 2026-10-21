"""
Running a 68k application: the Machine every 68k process shares, and
the Runner that is one process's main loop.

The Machine is the processor and what goes with it: the low-memory
globals that describe the hardware, the trap stubs GetTrapAddress hands
out, and the CPU lock.  As on a Mac under MultiFinder, one application
runs at a time.  It holds the lock while its code runs and gives it up
while it waits for an event; whoever takes it next gets its registers
and its switched low-memory globals put back.

The Runner starts the application through the Segment Loader, then runs
it: CPU.run() returns at each A-line trap, the Runner dispatches it, and
the CPU goes on.  Python code that needs to call back into 68k code (a
filter procedure, a patched trap) does it with call(), which runs a
nested loop until the routine returns.  The application's VBL tasks run
the same way, between two instructions, once a tick while its code runs
(see _vertical_retrace), with the Sound Driver's and the speaker's turn
after them (macrt.m68k.sound), and its Time Manager tasks at the tick
after they're due.  So does an application's own handler for the
VIA's interrupts, if it puts one in their vector.

An exception the application has no handler for stops it with a system
error, reported with its registers and the last traps it called, the
way MacsBug would.
"""

from __future__ import annotations

import collections
import logging
import struct
import sys
import threading
import time
import traceback
from contextlib import contextmanager
from typing import TYPE_CHECKING

from .. import moof
from ..memory import ADDRESS_MASK, BusError, from_mac, to_mac
from ..toolbox import lowmem
from ..toolbox.events import ProcessKilled, tick_count
from ..toolbox.menus import MenuInfo
from ..toolbox.quickdraw import GRAY_PAT
from ..toolbox.screen import DESK
from ..toolbox.system import ROM_BASE, ROM_END, ROM_SIZE
from ..toolbox.types import Point, Rect
from ..toolbox.windows import WindowRecord
from . import cpu as m68k
from . import dispatch, timemgr, wdefs
from .sound import SoundDriver, Speaker
from .sound_manager import SoundManager
from .cpu import CPU
from .cycles import RATE
from .hardware import (IWM, IWM_BASE, VIA, VIA_BASE, Clock, map_empty, map_test_rom, vBufA,
                       vHeadSel)
from .segload import Layout, SegmentLoader

if TYPE_CHECKING:
    from concurrent.futures import Future

    from ..process import Process
    from ..toolbox.api import Toolbox
    from ..toolbox.system import System

log = logging.getLogger("macrt.m68k")

ILLEGAL_WORD = 0x4AFC
RTS = 0x4E75
RTE = 0x4E73
TRAP_WORD = 0x4E40      # TRAP #n, with n in the low 4 bits
EXIT_TO_SHELL = 0xA9F4
N_TOOLBOX = 0x400
N_OS = 0x100

SYSTEM_VERSION = 0x0701     # what Gestalt and SysEnvirons report

# System error IDs (Inside Macintosh, "System Error Handler")
SYSTEM_ERRORS = {
    0: "macrt itself failed",           # not a Mac ID: a bug here, not in the application
    1: "bus error", 2: "address error", 3: "illegal instruction", 4: "zero divide",
    5: "check trap", 6: "overflow trap", 7: "privilege violation", 8: "trace trap",
    9: "line 1010 trap", 10: "line 1111 trap", 11: "miscellaneous exception",
    12: "unimplemented trap", 15: "segment loader error", 25: "out of memory",
    28: "stack ran into heap",
}
VBL_ADDR, VBL_COUNT = 6, 10    # in a VBLTask record
SOUND_BUFFER = 0x300            # 370 words, rounded up as the Mac Plus's is
# The ROM's handler for the VIA's interrupts: it calls the routine in
# Lvl1DT for the lowest of the flags that interrupted, with A1 pointing at
# the VIA, and keeps A0-A3 and D0-D3 for it (Inside Macintosh II,
# "Interrupts").  A flag still set after interrupts the processor again
# as it returns: here the handler goes round again.  A program puts its
# own routine in the table, as Master Tracks Pro does for timer 1, its
# tempo's clock.
LEVEL_1_HANDLER = bytes.fromhex(
    "48e7f0f0"          # movem.l d0-d3/a0-a3,-(sp)
    "41f8018e"          # 2: lea Lvl1DT-4,a0
    "227801d4"          # movea.l VIA,a1
    "10291a00"          # move.b vIFR(a1),d0
    "c0291c00"          # and.b vIER(a1),d0
    "0200007f"          # andi.b #$7F,d0
    "670c"              # beq.s 3f
    "5888"              # 1: addq.l #4,a0
    "e208"              # lsr.b #1,d0
    "64fa"              # bcc.s 1b
    "2050"              # movea.l (a0),a0
    "4e90"              # jsr (a0)
    "60de"              # bra.s 2b
    "4cdf0f0f"          # 3: movem.l (sp)+,d0-d3/a0-a3
    "4e73")             # rte
# What Lvl1DT points at, a routine for each of the VIA's flags, CA2 first,
# that the ROM's handler calls, or a program's own handler as the ROM's
# does (The Chessmaster 2000's is a copy of it).  Each clears its flag;
# the work of a tick or a second is done in Python.
LEVEL_1_ROUTINE = 12            # bytes each


def level_1_routine(flag: int) -> bytes:
    return struct.pack(">HHHHHH", 0x2078, lowmem.VIA,     # movea.l VIA,a0
                       0x117C, flag, 0x1A00,              # move.b #flag,vIFR(a0)
                       RTS)


# The ROM's keyboard translation, Key2Trans then Key1Trans, for programs
# that call it from low memory themselves (SuperPaint): D2 the key code,
# less $40 for Key2Trans's keypad keys, and D1 the modifiers' high byte;
# the character comes back in D0.  It is KeyTranslate.  Key1Trans's
# second word is a flag, as in the ROM, that Key Caps clears while it asks
# about a key, to have no dead keys: there are none here anyway.
KEY_TRANS = bytes.fromhex(
    "7040"              # Key2Trans: moveq #$40,d0
    "6006"              # bra.s translate
    "6002"              # Key1Trans: bra.s over the flag
    "ffff"              # dead keys: on
    "7000"              # moveq #0,d0
    "48e760c0"          # translate: movem.l d1-d2/a0-a1,-(sp)
    "e149"              # lsl.w #8,d1
    "1202"              # move.b d2,d1
    "d240"              # add.w d0,d1
    "42a7"              # clr.l -(sp): the state
    "42a7"              # clr.l -(sp): the result
    "42a7"              # clr.l -(sp): no transData
    "3f01"              # move.w d1,-(sp): the key code and modifiers
    "486f000a"          # pea 10(sp): the state
    "a9c3"              # _KeyTranslate
    "201f"              # move.l (sp)+,d0
    "588f"              # addq.l #4,sp
    "0280000000ff"      # andi.l #$FF,d0
    "4cdf0306"          # movem.l (sp)+,d1-d2/a0-a1
    "4e75")             # rts
# The cursor routines QuickDraw keeps vectors to in low memory, JHideCursor
# to JCrsrObscure, for programs that call them there (the Alarm Clock).
# JShieldCursor takes a rectangle in global coordinates, its top left
# pushed first, and pops it.
CURSOR_VECTORS = bytes.fromhex(
    "a8524e75"          # JHideCursor: _HideCursor; rts
    "a8534e75"          # JShowCursor: _ShowCursor; rts
    "a8504e75"          # JInitCrsr: _InitCursor; rts
    "a8564e75"          # JCrsrObscure: _ObscureCursor; rts
    "48e7e0c0"          # JShieldCursor: movem.l d0-d2/a0-a1,-(sp)
    "2f2f0018"          # move.l 24(sp),-(sp): bottom right
    "2f2f0020"          # move.l 32(sp),-(sp): top left
    "41d7"              # lea (sp),a0
    "2f08"              # move.l a0,-(sp): the rectangle
    "42a7"              # clr.l -(sp): global already
    "a855"              # _ShieldCursor
    "508f"              # addq.l #8,sp
    "4cdf0307"          # movem.l (sp)+,d0-d2/a0-a1
    "2f570008"          # move.l (sp),8(sp)
    "508f"              # addq.l #8,sp
    "4e75")             # rts
# How the Device Manager waits for a synchronous request a driver hasn't
# finished, A0 its parameter block: until ioResult isn't 1 any more.
WAIT_FOR_IO = bytes.fromhex(
    "4a680010"          # wait: tst.w ioResult(a0)
    "6efa"              # bgt.s wait
    "30280010"          # move.w ioResult(a0),d0
    "48c0"              # ext.l d0
    "4e75")             # rts

SOUND_WAIT = bytes.fromhex(
    "4a50"              # wait: tst.w (a0)
    "6efc"              # bgt.s wait
    "4ed1")             # jmp (a1)


def init_dispatcher(at: int, dispatcher: int) -> bytes:
    """Where the ROM's start-up code puts its A-line trap dispatcher in the
    vector: the LEA for a 68000 or the one for a 68020, then MOVE.L A0,$28.
    Lotus Jazz finds the dispatcher by looking for the MOVE and taking the
    LEA before it (the one 6 bytes further back, on a 68000), to put it
    back in the vector in case a debugger has taken it."""
    return struct.pack(">HhHHhHH", 0x41FA, dispatcher - (at + 2),   # LEA dispatcher(PC),A0
                       0x6004,                                      # BRA.S set
                       0x41FA, dispatcher - (at + 8),               # LEA dispatcher(PC),A0
                       0x21C8, 0x0028)                              # set: MOVE.L A0,$28


MAX_RETRACES = 6        # ticks a late retrace catches up on; after a longer gap, one
MAX_TIME_TASKS = 64     # Time Manager tasks run at one tick, at most

VECTOR_ERRORS = {m68k.BUS_ERROR: 1, m68k.ADDRESS_ERROR: 2, m68k.ILLEGAL: 3,
                 m68k.ZERO_DIVIDE: 4, m68k.CHK: 5, m68k.TRAPV: 6, m68k.PRIVILEGE: 7,
                 m68k.TRACE: 8, m68k.LINE_A: 9, m68k.LINE_F: 10}
FORMAT_2 = {m68k.ZERO_DIVIDE, m68k.CHK, m68k.TRAPV, m68k.TRACE}


def _extension(mem, at: int, ea: int, size: int) -> int:
    """How many bytes of extension words the effective address ea (mode
    and register, as in an opcode's low 6 bits) has, from at."""
    mode = ea >> 3
    if mode == 5 or ea in (0o70, 0o72):     # d16(An), abs.W, d16(PC)
        return 2
    if ea == 0o71:                          # abs.L
        return 4
    if ea == 0o74:                          # immediate
        return 4 if size == 4 else 2
    if mode == 6 or ea == 0o73:             # indexed: brief, or the 68020's full format
        ext = mem.r16(at)
        if not ext & 0x100:
            return 2
        n = 2 + (0, 0, 2, 4)[ext >> 4 & 3]  # base displacement
        if ext & 7:                         # memory indirect: an outer displacement
            n += (0, 0, 2, 4)[ext & 3]
        return n
    return 0


def after_instruction(mem, pc: int) -> int:
    """Where the DIVU, DIVS, CHK, TRAPV or TRAPcc at pc ends: where the
    exception it raises returns to."""
    op = mem.r16(pc)
    if m68k.is_trapcc(op):
        return pc + m68k.TRAPCC_SIZES[op & 7]
    if op & 0xFFC0 == 0x4C40:               # DIVU.L, DIVS.L: an extension word, then the EA
        return pc + 4 + _extension(mem, pc + 4, op & 0x3F, 4)
    if op & 0xF0C0 == 0x80C0 or op & 0xF140 == 0x4100:     # DIVx.W, CHK.W, CHK.L
        size = 4 if op & 0xF1C0 == 0x4100 else 2
        return pc + 2 + _extension(mem, pc + 2, op & 0x3F, size)
    return pc + 2                           # TRAPV


class SystemError68k(Exception):
    """The application hit something it can't come back from."""

    def __init__(self, error_id: int, detail: str = ""):
        self.error_id = error_id
        self.detail = detail
        what = SYSTEM_ERRORS.get(error_id, "system error")
        super().__init__(f"system error {error_id} ({what})" + (f": {detail}" if detail else ""))


class Machine:
    """The processor and the parts of the Mac around it, one per System."""

    def __init__(self, system: System):
        self.system = system
        self.mem = system.mem
        self.owner: Runner | None = None    # before the CPU's clock asks after its timers
        self.cpu = CPU(system.mem)
        self.clock = Clock()                # the VIA's and the drives', the same
        self.via = VIA(button=lambda: not self.mem.r8(lowmem.MBState) & 0x80, clock=self.clock)
        self.via.map(self.cpu.uc)
        self.via.on_page = system.screen.show_page
        self.iwm = IWM(head_select=lambda: bool(self.via.regs[vBufA] & vHeadSel),
                       clock=self.clock)
        self.iwm.surface = lambda drive, track, side: glue_sony.surface(self, drive, track, side)
        self.iwm.bits = lambda drive, track, side: glue_sony.bits(self, drive, track, side)
        self.iwm.map(self.cpu.uc)
        self.tag_buffer = 0                 # where the disk driver puts the tags it reads too
        self.disk_position = 0              # and where on the disk it read or wrote up to
        self.sector_buffer = 0              # the File Manager's, for a sector it reads so
        self.inserting = 0                  # the drive a disk switch is putting a disk in
        self.ejected: dict[int, int] = {}   # drive: the tick its disk was ejected at
        self.put_back: set[int] = set()     # drives whose disk is back but not mounted
        self.choosing: dict[int, tuple[Future, list]] = {}  # drive: the disk the user chooses
        map_test_rom(self.cpu.uc)
        map_empty(self.cpu.uc, ROM_BASE + ROM_SIZE, ROM_END)
        self.cpu.on_tick = self._tick
        self.cpu.timers_waiting = lambda: self.owner is not None and bool(self.owner.time_tasks.due)
        self.sound = SoundDriver(self)
        self.sound_manager = SoundManager(self)
        self.speaker = Speaker(self.mem, self.via, system.sound)
        self.cpu.sound_waiting = lambda: (self.sound.counting() or self.speaker.on
                                          or self.sound_manager.busy())
        if system.speed:
            self.cpu.rate = int(system.speed * RATE)
        self.cpu.short_frames = system.model.cpu == 0
        self._lock = threading.Lock()
        self._ticker: threading.Thread | None = None
        self.drivers: dict[int, Process] = {}      # unit: whose 68k driver it is
        self._make_stubs()
        self._init_globals()

    def _tick(self) -> None:
        """At each tick, from the clock: the cursor's task, and a look at
        the screen for code that runs a while without traps."""
        self.system.input.tick()
        self.system.screen.glance()

    def _make_stubs(self) -> None:
        """Code GetTrapAddress can point at: one stub per trap that, called
        with JSR, does that trap and returns, plus a few markers."""
        mem = self.mem
        size = 2 * N_TOOLBOX + 4 * N_OS + 20
        base = self.system.sys_heap.new_ptr(size)
        self.stubs = (base, base + size)
        self.toolbox_stubs = base
        self.os_stubs = base + 2 * N_TOOLBOX
        for n in range(N_TOOLBOX):
            mem.w16(self.toolbox_stubs + 2 * n, 0xAC00 | n)        # auto-pop
        for n in range(N_OS):
            mem.w16(self.os_stubs + 4 * n, 0xA000 | n)
            mem.w16(self.os_stubs + 4 * n + 2, RTS)
        marks = self.os_stubs + 4 * N_OS
        self.return_stub = marks            # nested calls return here
        self.exit_stub = marks + 4          # an application's main routine returns here
        self.std_filter = marks + 8         # the Dialog Manager's, for GetStdFilterProc
        # An OS trap's patch returns to the dispatcher here: A0 to put back, or A0 a result
        self.os_patch_returns = (marks + 14, marks + 16)
        # Where a program that looks past a trap's code for the ROM's next RTE,
        # to make an interrupt do nothing, finds one
        self.rte_stub = marks + 18
        self.bomb_stub = ROM_BASE + 16      # every exception vector, until replaced
        self.key_trans = ROM_BASE + 64      # Key2Trans, then Key1Trans 4 bytes on
        self.wait_for_io = ROM_BASE + 116
        self.io_done = ROM_BASE + 130       # JIODone's: glue_devices does it
        self.cursor_vectors = ROM_BASE + 132
        self.data_marks = ROM_BASE + 184    # what begins and ends a sector's data field
        self.rd_data = ROM_BASE + 190       # the ROM's RdData: glue_sony does it
        self.te_do_text = ROM_BASE + 192    # TextEdit's DoText: glue_textedit does it
        self.a_line = ROM_BASE + 194        # the A-line trap dispatcher, for vector $28
        self.init_dispatcher = ROM_BASE + 196
        self.rd_addr = ROM_BASE + 210       # the ROM's RdAddr and AdrDisk: glue_sony does them
        self.adr_disk = ROM_BASE + 212
        self.mount_vol = ROM_BASE + 214     # _MountVol, as a program may have patched it
        self.level_1_routines = ROM_BASE + 220      # Lvl1DT's, 8 of them
        self.denibble = ROM_BASE + 316      # the six bits each byte on a disk stands for, from $96
        self.sony_routines = ROM_BASE + 422     # the disk driver's other routines, 2 bytes each
        self.level_1 = ROM_BASE + 434       # the VIA's interrupts' handler
        self.sound_task = ROM_BASE + 478    # the Sound Driver's VBL task's: an RTS
        self.sound_wait = ROM_BASE + 480    # where the Sound Manager's callers wait
        self.address_marks = ROM_BASE + 486     # what begins and ends a sector's address field
        self.cpu.rom_interrupts = self.level_1
        self.cpu.rom_level_1_routines = (self.level_1_routines,
                                         self.level_1_routines + 8 * LEVEL_1_ROUTINE)
        for at in (self.return_stub, *self.os_patch_returns):
            mem.w16(at, ILLEGAL_WORD)
        mem.w16(self.exit_stub, EXIT_TO_SHELL)
        mem.w16(self.rte_stub, RTE)
        mem.w16(self.bomb_stub, ILLEGAL_WORD)
        mem.write(self.level_1, LEVEL_1_HANDLER)
        mem.w16(self.sound_task, RTS)
        mem.write(self.key_trans, KEY_TRANS)
        mem.write(self.wait_for_io, WAIT_FOR_IO)
        mem.write(self.sound_wait, SOUND_WAIT)
        mem.w16(self.io_done, ILLEGAL_WORD)
        mem.w16(self.rd_data, ILLEGAL_WORD)
        mem.w16(self.rd_addr, ILLEGAL_WORD)
        mem.w16(self.adr_disk, ILLEGAL_WORD)
        for _, at in self._sony_table():
            mem.w16(at, ILLEGAL_WORD)
        mem.w16(self.te_do_text, ILLEGAL_WORD)
        mem.w16(self.a_line, ILLEGAL_WORD)
        mem.write(self.init_dispatcher, init_dispatcher(self.init_dispatcher, self.a_line))
        mem.write(self.cursor_vectors, CURSOR_VECTORS)
        mem.write(self.mount_vol, struct.pack(">HH", 0xA00F, RTS))
        for n in range(8):
            mem.write(self.level_1_routines + LEVEL_1_ROUTINE * n, level_1_routine(1 << n))
        mem.write(self.data_marks, b"\xd5\xaa\xad\xde\xaa\xff")
        mem.write(self.address_marks, b"\xd5\xaa\x96\xde\xaa\xff")
        mem.write(self.denibble, bytes(moof.SIX.get(b, 0xFF) for b in range(moof.GCR[0], 0x100)))
        # MOVE.W #selector,D0; _DialogDispatch with auto-pop
        mem.write(self.std_filter,
                  struct.pack(">HHH", 0x303C, dispatch.STD_FILTER_SELECTOR, 0xAE68))

    def reset_vectors(self, part) -> None:
        """Put back the System's exception vectors and the disk driver's
        that an application leaving part pointed into it."""
        mem = self.mem
        for v in range(2, 64):
            if part.start <= mem.r32(4 * v) & ADDRESS_MASK < part.end:
                mem.w32(4 * v, self.vector(v))
        for vector, rom in ((lowmem.RdAddr, self.rd_addr), (lowmem.RdData, self.rd_data),
                            (lowmem.AdrDisk, self.adr_disk), (lowmem.TEDoText, self.te_do_text),
                            *self._level_1_table(), *self._sony_table()):
            if part.start <= mem.r32(vector) & ADDRESS_MASK < part.end:
                mem.w32(vector, rom)
        if part.start <= self.tag_buffer < part.end:
            self.tag_buffer = 0

    def _level_1_table(self) -> list[tuple[int, int]]:
        return [(lowmem.Lvl1DT + 4 * n, self.level_1_routines + LEVEL_1_ROUTINE * n)
                for n in range(8)]

    def _sony_table(self) -> list[tuple[int, int]]:
        return [(vector, self.sony_routines + 2 * n)
                for n, (vector, _) in enumerate(glue_sony.ROUTINES)]

    def vector(self, v: int) -> int:
        """What the System leaves in exception vector v."""
        return {m68k.LEVEL_1: self.level_1, m68k.LINE_A: self.a_line}.get(v, self.bomb_stub)

    def stub_for(self, key: int) -> int:
        if dispatch.is_toolbox(key):
            return self.toolbox_stubs + 2 * (key & 0x3FF)
        return self.os_stubs + 4 * (key & 0xFF)

    def _init_globals(self) -> None:
        """What the ROM and System leave in low memory about the machine."""
        mem = self.mem
        for v in range(2, 64):
            mem.w32(4 * v, self.vector(v))
        for entry, rom in self._level_1_table():
            mem.w32(entry, rom)
        model = self.system.model
        mem.w8(lowmem.CPUFlag, model.cpu)
        mem.w16(lowmem.ROM85, 0x7FFF)               # 128K ROM or later, no Color QuickDraw
        mem.w16(lowmem.SysVersion, SYSTEM_VERSION)
        # Nonzero, and not the same twice: Leisure Suit Larry seeds
        # QuickDraw's randSeed from it, and from 0 every Random is 0.
        mem.w32(lowmem.RndSeed, m68k.mac_time() or 1)
        # MacWrite won't print, quit or open a desk accessory while it's 0,
        # in case The Monkey would.
        mem.w16(lowmem.MonkeyLives, 0xFFFF)
        mem.w32(lowmem.Lo3Bytes, 0x00FFFFFF)
        mem.w32(lowmem.MinusOne, 0xFFFFFFFF)
        mem.w32(lowmem.OneOne, 0x00010001)
        mem.w8(lowmem.CrsrCouple, 0xFF)
        mem.w16(lowmem.FSFCBLen, 94)                # HFS
        mem.w8(lowmem.MMU32Bit, 0)
        mem.w16(lowmem.HWCfgFlags, 0x1000 if model.fpu else 0)
        mem.w16(lowmem.ScrVRes, 72)
        mem.w16(lowmem.ScrHRes, 72)
        double, caret, thresh, rate, ap_font = 32, 32, 16, 4, 3     # ticks; Geneva
        volume = 7                                  # the VIA's, as it starts (hardware.VIA)
        mem.w32(lowmem.DoubleTime, double)
        mem.w32(lowmem.CaretTime, caret)
        mem.w16(lowmem.KeyThresh, thresh)
        mem.w16(lowmem.KeyRepThresh, rate)
        mem.w16(lowmem.SysFontFam, 0)
        mem.w16(lowmem.SysFontSize, 12)
        mem.w16(lowmem.ApFontID, ap_font)           # the font family 1 means
        # Dark Castle mixes its sounds into the sound buffer only while this
        # says the volume isn't 0.
        mem.w8(lowmem.SdVolume, volume)
        # Parameter RAM saying the same, as InitUtil leaves it: Wizardry
        # takes its double-click time from volClik, not DoubleTime.
        mem.write(lowmem.SysParam, struct.pack(
            ">4B2HIh3H", 0xA8, 0, 0, 0,             # valid; AppleTalk node hints; port use
            0xCC0A, 0xCC0A, 0,                      # the serial ports: 9600 baud, 8N2; alarm
            ap_font - 1,
            (thresh // 4) << 12 | (rate // 2) << 8,  # kbdPrint: the printer on the printer port
            volume << 8 | (double // 4) << 4 | caret // 4,  # volClik
            0x4C))                                  # misc: mouse scaling; menus blink 3 times
        mem.w16(lowmem.SysEvtMask, 0xFFEF)          # everything but keyUp
        mem.w8(lowmem.SEvtEnb, 0xFF)
        mem.w8(lowmem.KbdType, 2)
        mem.w32(lowmem.Key1Trans, self.key_trans + 4)
        mem.w32(lowmem.Key2Trans, self.key_trans)
        for i, vector in enumerate((lowmem.JHideCursor, lowmem.JShowCursor, lowmem.JInitCrsr,
                                    lowmem.JCrsrObscure, lowmem.JShieldCursor)):
            mem.w32(vector, self.cursor_vectors + 4 * i)
        # The alarm isn't flashing the Apple menu's icon, has beeped and is
        # off, as the Alarm Clock leaves it: or the Alarm Clock, opening,
        # turns the icon back over.
        mem.w8(lowmem.AlarmState, 0xFE)
        mem.write(lowmem.DeskPattern, DESK)
        mem.write(lowmem.DragPattern, GRAY_PAT)
        mem.w32(lowmem.VIA, VIA_BASE)
        mem.w32(lowmem.IWM, IWM_BASE)
        mem.w32(lowmem.RdAddr, self.rd_addr)
        mem.w32(lowmem.RdData, self.rd_data)
        mem.w32(lowmem.AdrDisk, self.adr_disk)
        for vector, at in self._sony_table():
            mem.w32(vector, at)
        mem.w32(lowmem.TEDoText, self.te_do_text)
        mem.w16(lowmem.BootDrive, 1)
        # Word 1.0 takes $FFFF in this long's high word to mean it has kept
        # no handle there yet; anything else it uses as one.
        mem.w32(lowmem.Unnamed_AFC, 0xFFFFFFFF)
        # The sound buffer, which the speaker plays (macrt.m68k.sound).
        # Games write their samples straight into it, as on a Mac Plus or SE.
        mem.w32(lowmem.SoundBase, self.system.sys_heap.new_ptr(SOUND_BUFFER, clear=True))
        self.sound.install(self.system.sys_heap)
        sony = self.system.sys_heap.new_ptr(glue_sony.SONY_VARS, clear=True)
        mem.w32(lowmem.SonyVars, sony)
        glue_sony.set_up_vars(mem, sony)
        # Programs that load code call this before they run it.
        mem.w32(lowmem.jCacheFlush, self.stub_for(dispatch.trap_key(0xA0BD)))  # FlushCodeCache
        glue_devices.init(self)

    # =====================================================================
    # Who has the processor
    # =====================================================================
    def acquire(self, runner: Runner) -> None:
        self._lock.acquire()
        if self.owner is not runner:
            old = self.owner
            if old is not None:
                old.ctx = self.cpu.context()
                old.process.world.switch_out()
            if runner.ctx is not None:
                self.cpu.restore(runner.ctx)
                runner.ctx = None
            runner.process.world.switch_in()
            self.owner = runner

    def release(self) -> None:
        self._lock.release()

    def idle(self, runner: Runner) -> None:
        """runner, holding the processor, is about to wait for the host: if
        it has things to happen at the ticks while it does, the ticker
        sees to them."""
        if self._ticker is None and runner.wants_ticks():
            self._ticker = threading.Thread(target=self._tick_idle, name="macrt ticks",
                                            daemon=True)
            self._ticker.start()

    def ticking(self) -> bool:
        """Whether this is the ticker's thread, which holds the processor."""
        return threading.current_thread() is self._ticker

    def _tick_idle(self) -> None:
        """The ticker: at each tick that no application has the processor,
        the ticks of the one that had it last, which is waiting, as on a
        Mac, where interrupts go on while a program waits.  Once it has
        none to come, the ticker stops."""
        while True:
            time.sleep(1 / 60)
            if not self._lock.acquire(blocking=False):
                continue                # an application is running: it has its ticks
            try:
                owner = self.owner
                if owner is None or not owner.wants_ticks():
                    self._ticker = None
                    return
                owner.idle_tick()
            finally:
                self._lock.release()

    def leave(self, runner: Runner) -> None:
        """runner's process is done; it holds the lock."""
        if self.owner is runner:
            runner.process.world.switch_out()
            self.owner = None
        self._lock.release()


def machine_for(system: System) -> Machine:
    with system.lock:
        m = getattr(system, "machine", None)
        if m is None:
            m = system.machine = Machine(system)
        return m


class Runner:
    """One 68k application's main loop; it is the trap context the glue
    gets as t."""

    def __init__(self, process: Process, layout: Layout):
        self.process = process
        self.system = process.system
        self.mem = process.system.mem
        self.screen = process.system.screen
        self.machine = machine_for(process.system)
        self.cpu = self.machine.cpu
        self.layout = layout
        self.segments = SegmentLoader(self, layout)
        self.tb: Toolbox | None = None
        self.ctx = None
        self.holding = False
        self.patches: dict[int, int] = {}           # trap key -> address, from SetTrapAddress
        self.history: collections.deque = collections.deque(maxlen=16)
        self.reported: set[str] = set()
        self.unimplemented_calls: collections.Counter = collections.Counter()
        self.temp_handles: set[int] = set()         # TempNewHandle's, in the system heap
        self.trace = False
        self.menu_rect = 0          # mbRect's answer while MenuSelect pulls a menu down
        self.retrace_tick = 0       # when its VBL tasks were last counted down
        self.null_tick = -1         # when GetNextEvent last had nothing for it
        self.ticks_failed = False   # a tick while it waited went wrong
        self.time_tasks = timemgr.TimeTasks()
        self.in_wdef = 0            # how deep in calls to its own WDEFs
        process.events.enabled = lambda: self.mem.r16(lowmem.SysEvtMask)

    # =====================================================================
    # Life cycle
    # =====================================================================
    def main(self, toolbox: Toolbox) -> None:
        """The process's thread."""
        self.tb = toolbox
        if toolbox is not None:
            toolbox.wdefs = wdefs.WindowDefinitions(self)
        self.machine.acquire(self)
        self.holding = True
        try:
            if self.process.events.killed:
                raise ProcessKilled()
            entry = self._start()
            self.run_until(self.machine.exit_stub, entry)
            glue_desk.close_all(self)
        except SystemError68k as e:
            self._report(e)
        except ProcessKilled:
            raise
        except Exception as e:  # noqa: BLE001 - say so, and stop only this application
            traceback.print_exc()
            where = (f" in {dispatch.trap_name(self.history[-1][0])}" if self.history else "")
            self._report(SystemError68k(0, f"{type(e).__name__}{where}: {e}"))
        finally:
            glue_process.free_temp_memory(self)
            glue_devices.forget(self)
            self.machine.sound.forget(self)
            self.machine.sound_manager.forget(self)
            self.machine.reset_vectors(self.process.partition)
            self.machine.via.main_page()
            if self.holding:
                self.holding = False
                self.machine.leave(self)
            if self.unimplemented_calls:
                summary = ", ".join(f"{n} ({c})" for n, c in
                                    self.unimplemented_calls.most_common(12))
                log.info("%s called traps macrt doesn't implement: %s",
                         self.process.name, summary)

    def _start(self) -> int:
        """Registers and memory as the Segment Loader leaves them at launch."""
        cpu, part, world = self.cpu, self.process.partition, self.process.world
        for n in range(8):
            cpu.set_d(n, 0)
        for n in range(7):
            cpu.set_a(n, 0)
        cpu.uc.reg_write(m68k.UC_M68K_REG_SR, 0x2000)
        cpu.set_a(5, part.a5)
        cpu.sp = part.stack_base
        cpu.push32(self.machine.exit_stub)
        entry = self.segments.install()
        h = self.process.heap.new_handle(4, clear=True)     # Finder info: open, no documents
        world.w32(lowmem.AppParmHandle, h)
        glue_scrap.desk(self, from_host=True)               # its copy of the scrap (IM I-453)
        glue_aevents.launched(self)
        self.process.resources.load_hook = (
            lambda m, e, force: glue_toolbox.load_resource(self, m, e, force))
        return entry

    def interrupt(self) -> None:
        """Stop the application's code soon, wherever it is (it's being killed)."""
        if self.machine.owner is self:
            self.cpu.interrupt()

    def _report(self, e: SystemError68k) -> None:
        lines = [f"macrt: {self.process.name} stopped: {e}", "  " + self.cpu.registers()]
        if self.history:
            lines.append("  last traps: " + ", ".join(
                f"{dispatch.trap_name(w)} at ${pc & ADDRESS_MASK:06X}" for w, pc in self.history))
        print("\n".join(lines), file=sys.stderr)
        report = getattr(self.tb._d, "report_system_error", None) if self.tb is not None else None
        if report is not None:
            report(self.process, e.error_id, str(e))

    # =====================================================================
    # Running code
    # =====================================================================
    def run_until(self, until: int, pc: int | None = None) -> None:
        cpu = self.cpu
        if pc is not None:
            cpu.pc = pc
        events = self.process.events
        while True:
            ev = cpu.run(until)
            if events.killed:
                cpu.interrupt_requested = False
                raise ProcessKilled()
            kind = ev.kind
            if kind == "trap":
                self._trap(ev.code, ev.pc)
            elif kind == "until":
                return
            elif kind == "interrupted":
                cpu.interrupt_requested = False
                if cpu.retrace_pending:
                    self._vertical_retrace()
                elif cpu.own_interrupts():
                    self._via_interrupt()
            elif kind == "exception":
                self._exception(ev.code, ev.pc)
            else:
                raise SystemError68k(1, f"unicorn error {ev.code} at ${ev.pc:06X} "
                                        "(an access outside RAM?)")

    def _vertical_retrace(self) -> None:
        """The vertical retrace interrupt, between two instructions: count
        down each VBL task in the application's queue, once for each tick
        since the last time (if that was a moment ago: after a longer gap,
        such as another application's turn, once), and run those that
        reach 0, with A0 pointing at the task.  A task that wants to run
        again sets its count again.  After them at each tick, the Sound
        Driver's task and the Sound Manager's channels make the sound
        buffer's frame, and the speaker plays it.  Then the
        VIA may interrupt it (_via_interrupt).  While the application has
        interrupts masked, it waits."""
        cpu = self.cpu
        if cpu.sr & m68k.INTERRUPT_MASK:
            return
        cpu.retrace_pending = False
        self.ticks_failed = False
        self._retrace_tasks()
        self._time_tasks()
        self._via_interrupt()

    def wants_ticks(self) -> bool:
        """Whether something is to happen at the ticks to come: a VBL task
        counting down, a Time Manager task waiting, or a sound to make or
        play.  With its machine held."""
        cpu = self.cpu
        return not (self.ticks_failed or self.process.events.killed) and bool(
            cpu.vbl_counting() or self.time_tasks.due or cpu.sound_waiting())

    def idle_tick(self) -> None:
        """A tick while the application waits for the host (for an event, a
        menu, the end of a Delay), from the machine's ticker: its VBL
        tasks, its sound and its Time Manager tasks, as the vertical
        retrace interrupt would have them go on.  Not the VIA's interrupt,
        which mustn't come in the middle of a trap.  If something goes
        wrong, the ticks stop until the application's own code has them
        again, to meet it there."""
        cpu = self.cpu
        if cpu.sr & m68k.INTERRUPT_MASK:
            return
        ctx = cpu.context()
        try:
            self._retrace_tasks()
            self._time_tasks()
        except Exception:  # noqa: BLE001 - a system error, or the process killed
            log.debug("%s: a tick while it waited failed", self.process.name, exc_info=True)
            cpu.restore(ctx)
            self.ticks_failed = True

    def _retrace_tasks(self) -> None:
        """The VBL tasks and the sound of each tick since the last.  A task
        that hasn't set its vblCount again once it's run is taken out of the
        queue, as Inside Macintosh II-350 has it: Dragonworld's music, which
        it installs again when it plays the next tune."""
        cpu, mem = self.cpu, self.mem
        now = tick_count()
        ticks = now - self.retrace_tick
        if ticks > MAX_RETRACES:
            ticks = 1
        self.retrace_tick = now
        if ticks > 0 and (mem.r32(lowmem.VBLQueue + 2) or cpu.sound_waiting()):
            ctx = cpu.context()
            cpu.sr = cpu.sr | m68k.VBL_LEVEL
            sound, speaker = self.machine.sound, self.machine.speaker
            manager = self.machine.sound_manager
            for _ in range(ticks):
                task = mem.r32(lowmem.VBLQueue + 2) & ADDRESS_MASK
                for _ in range(m68k.MAX_VBL_TASKS):     # a queue that goes round, once
                    if not task:
                        break
                    after = mem.r32(task) & ADDRESS_MASK    # before the task can remove itself
                    count = mem.r16s(task + VBL_COUNT)
                    if count > 0:
                        mem.w16(task + VBL_COUNT, count - 1)
                        if count == 1:
                            cpu.set_a(0, task)
                            self._jsr(mem.r32(task + VBL_ADDR))
                            if not mem.r16(task + VBL_COUNT):
                                glue_os.Dequeue(self, task, lowmem.VBLQueue)
                    task = after
                sound.retrace(self)
                manager.retrace(self)
                speaker.retrace()
            cpu.restore(ctx)

    def _via_interrupt(self) -> None:
        """Interrupt the application for the VIA, between two instructions,
        if it's flagged something the application handles itself: with its
        own handler, anything enabled, and with its own routine in Lvl1DT,
        that routine's flag.  The ROM's handler calls the routine for the
        lowest flag set, which may be one of the System's first."""
        cpu = self.cpu
        if cpu.sr & m68k.INTERRUPT_MASK:
            return
        pending = self.machine.via.pending()
        vector = self.mem.r32(4 * m68k.LEVEL_1) & ADDRESS_MASK
        if pending and (vector not in (0, cpu.rom_interrupts)
                        or pending & cpu.own_level_1_routines()):
            cpu.take_exception(m68k.LEVEL_1, cpu.pc)
            cpu.sr = cpu.sr & ~m68k.INTERRUPT_MASK | m68k.VBL_LEVEL

    def _time_tasks(self) -> None:
        """Run the application's Time Manager tasks that are due, with A1
        pointing at each, in the order they're due: a task that primes
        itself again for a moment later may run again now."""
        tasks = self.time_tasks
        if not tasks.due:
            return
        cpu, mem = self.cpu, self.mem
        now = tasks.clock()
        ctx = cpu.context()
        cpu.sr = cpu.sr | m68k.VBL_LEVEL
        for _ in range(MAX_TIME_TASKS):
            due = tasks.next_due(now)
            if due is None:
                break
            task, tasks.running = due
            q = task + timemgr.QTYPE
            mem.w16(q, mem.r16(q) & ~timemgr.ACTIVE)
            routine = mem.r32(task + timemgr.TM_ADDR)
            try:
                if routine:                 # none: the task only measures time
                    cpu.set_a(1, task)
                    self._jsr(routine)
            finally:
                tasks.running = None
        cpu.restore(ctx)

    def _trap(self, word: int, pc: int) -> None:
        cpu = self.cpu
        if (self.process.frames_due and not self.in_wdef
                and not cpu.sr & m68k.INTERRUPT_MASK):
            wdefs.refresh(self)     # windows its own WDEFs are to shape and draw
        self.history.append((word, pc))
        self.screen.tick()          # show what it's written in the screen buffer
        if self.trace:
            log.debug("%s: %s at $%06X", self.process.name, dispatch.describe(self, word), pc)
        start, end = self.machine.stubs
        via_stub = start <= (pc & ADDRESS_MASK) < end
        if pc & ADDRESS_MASK == self.machine.return_stub:
            raise SystemError68k(3, "returned through a callback that wasn't called")
        key = dispatch.trap_key(word)
        if dispatch.is_toolbox(word):
            patch = None if via_stub else self.patches.get(key)
            if patch is not None:
                if not word & 0x400:
                    cpu.push32(pc + 2)
                cpu.pc = patch
                return
            cpu.pc = cpu.pop32() if word & 0x400 else pc + 2
            self._dispatch(word, pc)
            return
        if via_stub:
            # Called through its address: the flags are in D1, if anywhere.
            d1 = cpu.d(1) & 0xFFFF
            word = d1 if (d1 & 0xF800) == 0xA000 and dispatch.trap_key(d1) == key else key
        else:
            patch = self.patches.get(key)
            if patch is not None:
                self._call_os_patch(patch, word, pc + 2)
                return
        cpu.pc = pc + 2
        self._dispatch(word, pc)
        cpu.set_cc_from_word(cpu.d(0))

    def _vectored_trap(self) -> None:
        """The ROM's A-line dispatcher, jumped to as through vector $28,
        with the trap's address stacked: the trap is done as when the core
        stops at it."""
        cpu = self.cpu
        cpu.return_from_exception()
        pc = cpu.pc
        self._trap(self.mem.r16(pc & ADDRESS_MASK), pc)

    def _dispatch(self, word: int, pc: int) -> None:
        try:
            dispatch.dispatch(self, word, pc)
        except dispatch.UnknownTrap as e:
            raise SystemError68k(12, str(e)) from None
        except BusError as e:           # it passed the trap a bad pointer or handle
            raise SystemError68k(1, f"in {dispatch.trap_name(word)} at ${pc & ADDRESS_MASK:06X}: "
                                    f"{e}") from None

    def _call_os_patch(self, addr: int, word: int, ret: int) -> None:
        """What the OS trap dispatcher does for a patched trap, on the stack
        as a Mac's does it, for patches that go back to the caller
        themselves, as Microsoft Word's does: stack the return address, SR
        and the trap word, then A2, D2, D1, A1, and A0 unless bit 8 says
        it's a result; and call the patch with the trap word in D1, its
        place in the table in D2 and its address in A2.  When it returns,
        _os_patch_returned takes them off again."""
        cpu = self.cpu
        cpu.push32(ret)
        cpu.push16(cpu.sr)
        cpu.push16(word)
        for value in (cpu.a(2), cpu.d(2), cpu.d(1), cpu.a(1)):
            cpu.push32(value)
        keeps_a0 = not word & 0x100
        if keeps_a0:
            cpu.push32(cpu.a(0))
        cpu.set_d(1, (cpu.d(1) & 0xFFFF0000) | word)
        cpu.set_d(2, (word & 0xFF) << 2)
        cpu.set_a(2, addr)
        cpu.push32(self.machine.os_patch_returns[0 if keeps_a0 else 1])
        cpu.pc = addr

    def _os_patch_returned(self, keeps_a0: bool) -> None:
        cpu = self.cpu
        if keeps_a0:
            cpu.set_a(0, cpu.pop32())
        cpu.set_a(1, cpu.pop32())
        cpu.set_d(1, cpu.pop32())
        cpu.set_d(2, cpu.pop32())
        cpu.set_a(2, cpu.pop32())
        cpu.sp += 4                     # the trap word and SR
        cpu.pc = cpu.pop32()
        cpu.set_cc_from_word(cpu.d(0))

    def _jsr(self, addr: int) -> None:
        cpu = self.cpu
        pc = cpu.pc
        cpu.push32(self.machine.return_stub)
        self.run_until(self.machine.return_stub, addr)
        cpu.pc = pc

    def call(self, addr: int, args: tuple = (), result: str = "") -> int:
        """Call a Pascal routine in 68k code: args are (code, value) pairs
        as in trapdefs.  Registers come back as they were."""
        cpu, mem = self.cpu, self.mem
        ctx = cpu.context()
        if result:
            cpu.sp = cpu.sp - dispatch.SIZES[result]
        for code, value in args:
            v = dispatch._to_long(value, code)
            if dispatch.SIZES[code] == 2:
                cpu.push16(v << 8 if code in "bB" else v)
            else:
                cpu.push32(v)
        self._jsr(addr)
        value = 0
        if result:
            value = dispatch.pop_args(mem, cpu.sp, result)[0][0]
        cpu.restore(ctx)
        return value

    def call_with(self, addr: int, **regs: int) -> int:
        """Call 68k code that takes its arguments in registers, a0=pb and
        so on, as a driver's routines do: what it leaves in D0.  The other
        registers come back as they were."""
        return self.call_for(addr, ("d0",), **regs)[0]

    def call_for(self, addr: int, out: tuple[str, ...], **regs: int) -> tuple[int, ...]:
        """Call it so, for what it leaves in the registers out, "d2" and
        so on, as the ROM's RdAddr leaves the sector it found."""
        cpu = self.cpu
        ctx = cpu.context()
        for reg, value in regs.items():
            (cpu.set_a if reg[0] == "a" else cpu.set_d)(int(reg[1]), value)
        self._jsr(addr)
        found = tuple((cpu.a if reg[0] == "a" else cpu.d)(int(reg[1])) for reg in out)
        cpu.restore(ctx)
        return found

    def _exception(self, vector: int, pc: int) -> None:
        cpu = self.cpu
        if vector == m68k.RTE:
            cpu.return_from_exception()
            return
        if vector == m68k.HALT:             # STOP: nothing to do until an interrupt
            cpu.pc = pc + 4
            return
        if pc & ADDRESS_MASK == self.machine.return_stub:
            raise SystemError68k(3, "returned through a callback that wasn't called")
        returns = self.machine.os_patch_returns
        if pc & ADDRESS_MASK in returns:
            self._os_patch_returned(pc & ADDRESS_MASK == returns[0])
            return
        if pc & ADDRESS_MASK == self.machine.io_done:
            glue_devices.io_done(self)
            return
        if pc & ADDRESS_MASK == self.machine.rd_data:
            glue_sony.rd_data(self)
            return
        if pc & ADDRESS_MASK == self.machine.rd_addr:
            glue_sony.rd_addr(self)
            return
        if pc & ADDRESS_MASK == self.machine.adr_disk:
            glue_sony.adr_disk(self)
            return
        n, odd = divmod((pc & ADDRESS_MASK) - self.machine.sony_routines, 2)
        if 0 <= n < len(glue_sony.ROUTINES) and not odd:
            glue_sony.ROUTINES[n][1](self)
            return
        if pc & ADDRESS_MASK == self.machine.te_do_text:
            glue_textedit.do_text(self)
            return
        if pc & ADDRESS_MASK == self.machine.a_line:
            self._vectored_trap()
            return
        handler = self.mem.r32(4 * vector) if vector < 256 else 0
        if handler & ADDRESS_MASK not in (0, self.machine.bomb_stub):
            if (m68k.TRAP_0 <= vector < m68k.TRAP_0 + 16
                    and self.mem.r16(pc) & 0xFFF0 == TRAP_WORD):
                pc += 2                     # the core stops at a TRAP; it returns past it
            if vector == m68k.CHK:
                pc -= 2                     # and a word into a CHK, but at a divide or TRAPV
            if vector in (m68k.ZERO_DIVIDE, m68k.CHK, m68k.TRAPV):
                cpu.take_exception(vector, after_instruction(self.mem, pc), pc)
            elif vector == m68k.TRACE:      # after the instruction traced
                cpu.take_exception(vector, pc, cpu.traced)
            else:
                cpu.take_exception(vector, pc, pc if vector in FORMAT_2 else None)
            return
        error_id = VECTOR_ERRORS.get(vector, 11)
        raise SystemError68k(error_id, f"at ${pc & ADDRESS_MASK:06X}")

    # =====================================================================
    # For the glue
    # =====================================================================
    @contextmanager
    def yielding(self):
        """Let other applications run while this one waits, and its ticks
        go on.  A task that waits at a tick keeps the processor, as with
        interrupts masked: the application's own code mustn't run under it."""
        if self.machine.ticking():
            yield
            return
        self.machine.idle(self)
        self.holding = False
        self.machine.release()
        ok = False
        try:
            yield
            ok = True
        finally:
            if ok:
                self.machine.acquire(self)
                self.holding = True

    @contextmanager
    def resumed(self):
        """Inside yielding: hold the machine again to run 68k code.  If
        that fails, it stays held, as after any other trap.  The ticker,
        running the application's code at a tick, has it already."""
        if self.machine.ticking():
            yield
            return
        self.machine.acquire(self)
        self.holding = True
        yield
        self.holding = False
        self.machine.release()

    def unimplemented(self, name: str, word: int) -> None:
        self.unimplemented_calls[name] += 1
        if name not in self.reported:
            self.reported.add(name)
            log.warning("%s: %s (trap $%04X) isn't implemented; skipped",
                        self.process.name, name, word)

    def system_error(self, error_id: int, detail: str = ""):
        raise SystemError68k(error_id, detail)

    @property
    def mem_err(self) -> int:
        return self.process.world.r16(lowmem.MemErr)

    def set_mem_err(self, err: int) -> None:
        self.process.world.w16(lowmem.MemErr, err)

    # Arguments: from pointers to Python values and back.
    def rect(self, p: int) -> Rect:
        return self.mem.r_rect(p)

    def set_rect(self, p: int, r: Rect) -> None:
        if p:
            self.mem.w_rect(p, r)

    def point(self, p: int) -> Point:
        return self.mem.r_point(p)

    def set_point(self, p: int, pt: Point) -> None:
        if p:
            self.mem.w_point(p, pt)

    def str255(self, p: int) -> str:
        return from_mac(self.mem.r_pstr(p)) if p else ""

    def pstr(self, p: int) -> bytes:
        return self.mem.r_pstr(p) if p else b""

    def set_str255(self, p: int, s: str | bytes) -> None:
        if p:
            self.mem.w_pstr(p, to_mac(s) if isinstance(s, str) else s)

    def w16(self, p: int, v: int) -> None:
        if p:
            self.mem.w16(p, v)

    def w32(self, p: int, v: int) -> None:
        if p:
            self.mem.w32(p, v)

    @staticmethod
    def ostype(v: int) -> bytes:
        return (v & 0xFFFFFFFF).to_bytes(4, "big")

    def write_event(self, p: int, ev) -> None:
        """An EventRecord: what, message, when, where, modifiers."""
        if not p:
            return
        msg = ev.message
        msg = msg.addr if hasattr(msg, "addr") else int(msg or 0)
        mem = self.mem
        mem.w16(p, ev.what)
        mem.w32(p + 2, msg)
        mem.w32(p + 6, ev.when)
        mem.w_point(p + 10, ev.where)
        mem.w16(p + 14, ev.modifiers)

    def window(self, p: int) -> WindowRecord | None:
        """The window a WindowPtr points at, None for anything else."""
        port = self.system.port_at(p) if p else None
        return port if isinstance(port, WindowRecord) else None

    def menu(self, h: int) -> MenuInfo | None:
        return MenuInfo(self.system, h) if h and self.mem.r32(h) & ADDRESS_MASK else None

    def sleep(self, ticks: int) -> None:
        """Wait, letting the other applications run (Delay)."""
        events = self.process.events
        with self.yielding():
            deadline = time.monotonic() + max(ticks, 0) / 60
            while not events.killed:
                left = deadline - time.monotonic()
                if left <= 0:
                    return
                time.sleep(min(left, 0.05))
        raise ProcessKilled()


# Register the trap implementations.
from . import (  # noqa: E402,F401
    glue_aevents, glue_aliases, glue_color, glue_controls, glue_desk, glue_devices, glue_dialogs,
    glue_files, glue_icons, glue_intl, glue_lists, glue_os, glue_pict, glue_ppc, glue_print,
    glue_process, glue_sane, glue_scrap, glue_script, glue_sony, glue_sound, glue_stdfile,
    glue_textedit, glue_toolbox)
