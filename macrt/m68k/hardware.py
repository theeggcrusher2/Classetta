"""
The Mac's VIA and IWM, for programs that reach past the Toolbox to them.

Games from the Mac's first years poll the 6522 VIA themselves.  They read
the mouse button from its port B rather than call Button, and they watch
the vertical blanking flag in its interrupt flag register to keep time
with the screen.  On a Mac 128K, 512K or Plus the VIA is at $EFE1FE,
with its sixteen registers 512 bytes apart, on the high byte of the data
bus (Inside Macintosh III, "Hardware").  Low memory's VIA says where it
is, and programs take the address from there or have it built in.

What's here is what a program reads:

  vBufB     bit 3, vSW, is the mouse button: 0 while it's down.  vH4,
            horizontal blanking, changes with every read.
  vBufA     what was written to it.  Bit 6, vPage2, chooses which screen
            buffer is shown: while it's 0, the alternate one, $8000 below
            the main one on a 512x342 screen (Dark Castle flips between
            them).  The sound buffer stays where it is.  Bit 7, an input,
            is the SCC's wait/request for channel A: high, as no serial
            data comes in.
  vIFR      CA1, the vertical blanking interrupt, is flagged at each tick
            and CA2 once a second, until the program clears them; and
            timer 1 and timer 2 when they run out.
  vIER      what's enabled, as written: at first, as the ROM leaves it,
            the vertical blanking, one-second and keyboard interrupts.
  timers    count down at 783.36 kHz from what was loaded into them.
            Timer 1, running free, goes round again from its latches.
            While its interrupt is enabled, each time round that went by
            before its flag was cleared, up to a quarter of a second's, is
            flagged again in turn: macrt runs a program's instructions in
            bursts, between traps it takes longer over, and a program
            counting them, as Master Tracks Pro counts its tempo's, keeps
            time.

The other registers keep what's written to them.  The shift register,
the keyboard's, reads 0: macrt gives programs the keyboard through
events and the KeyMap.

The IWM, the floppy disk controller, is at $DFE1FF, its sixteen switches
512 bytes apart on the low byte of the bus.  Touching one sets it: the
phases, the motor, the drive, and Q6 and Q7, which choose what a read
gets.  With both low it's the data register, the next byte off the disk,
its top bit set: the disk driver puts the bytes on a track under the
head when it calls a program's own routine to read them (glue_sony).
Once the head has been moved to another track, or the other drive
chosen, it reads that track from where the disk has turned to, as
PageMaker's copy protection does, a track in from its key sector,
counting the bytes between the marks there.  Each byte comes once the
disk has turned its bits under the head, and the VIA's timers and the
disks keep one clock, the machine's (Clock below): a program that times
a track byte by byte with timer 2, as Jazz's copy protection does,
counts what a Mac would, though macrt takes longer to run its
instructions.  While a program's own routine reads a sector, the clock
counts what each of its instructions would take a Mac's 68000 instead
(cycles), and the IWM shifts the disk's bits in one by one, as the real
one does: a byte is latched once a one reaches its top bit, reading it
clears it, and with Q6 or Q7 high none is shifted in, so the bits going
by then are missed (Shifting below).  Shufflepuck Cafe's copy
protection counts on that.  With Q6 high it's the status register, the mode in its
low five bits and whether the motor is on, and with Q7 high the
handshake register, ready as nothing's being written.  The mode is
written with both high and the motor off, as The Print Shop's copy
protection does to read a track with the IWM's clock set to 7 MHz; the
bytes read the same.  What's written with the motor on, to the disk, is
dropped.

Phases 0-2 are CA0-CA2, which with the VIA's SEL, vBufA's bit 5, choose
one of the drive's registers, and phase 3 is LSTRB: going high, it writes
CA2 to the register.  Two of them are kept, which way the head steps and
the step itself, so a program can move the head from track to track
(Inside Macintosh III, "Hardware"; Guide to the Macintosh Family
Hardware, "Floppy Disk Interface").  Each drive's disk turns all the
while, slower further out, as the drive turns a 400K or 800K disk: copy
protection times how far round it is from one track's sector to the
next's.

From $F80000 up is where a Mac Plus's ROM looks for a test ROM as it
starts.  None is fitted, and a read there finds nothing, with no bus
error, nor does a write: Dark Castle looks for one too.
"""

from __future__ import annotations

import bisect
import math
import time
from collections import deque
from contextlib import contextmanager
from typing import Callable, Iterator

from ..moof import RPM, TRACKS
from ..toolbox.events import tick_count

VIA_BASE = 0xEFE1FE
STRIDE = 512
MAP_START, MAP_END = 0xEFE000, 0xF00000     # pages around its registers, for mapping
TEST_ROM_START, TEST_ROM_END = 0xF80000, 0x1000000
HZ = 783_360                                # what the timers count
CATCH_UP = 0.25         # seconds timer 1's interrupts may fall behind and be caught up

(vBufB, _ORA, vDirB, vDirA, vT1C, vT1CH, vT1L, vT1LH,
 vT2C, vT2CH, vSR, vACR, vPCR, vIFR, vIER, vBufA) = range(16)

vSW = 0x08              # vBufB: the mouse button, 0 while down
vH4 = 0x40              # vBufB: horizontal blanking
vPage2 = 0x40           # vBufA: the main screen buffer is shown, 0 for the alternate
CA2, CA1, SR, T2, T1 = 0x01, 0x02, 0x04, 0x20, 0x40   # vIFR and vIER bits
ANY = 0x80
vSCCWrReq = 0x80        # vBufA: the SCC's wait/request, low while it has data

IWM_BASE = 0xDFE1FF
IWM_START, IWM_END = 0xDFE000, 0xE00000
CA0_ON, CA1_ON, CA2_ON, LSTRB = 1, 3, 5, 7  # some of its switches: each phase, on
MOTOR_ON, DRIVE_2, Q6_LOW, Q6_HIGH, Q7_HIGH = 9, 11, 12, 13, 15
vHeadSel = 0x20         # vBufA: SEL, which head reads and which drive register is chosen
# Some of a drive's registers, numbered from CA2, SEL, CA0 and CA1 as
# the disk driver's AdrDisk chooses them (NetBSD's iwmreg.h has them so):
# the way the head steps, 1 outward; 0 while it's stepping; the motor,
# 0 on; the head at track 0, 0; and the tachometer, 60 pulses a turn
DIRTN, STEP, MOTORON, TK0, TACH = 0x0, 0x4, 0x8, 0xA, 0xE
IDLE = 0.005                                # seconds unread that the disk turns on through


class Clock:
    """The machine's time, in seconds, which the VIA's timers count and
    the disks turn by: the host's, and what's been waited for on top of
    it.  A program reading a disk byte by byte keeps pace with it, as a
    Mac's processor does, and times the bytes with the VIA: a byte the
    data register hasn't got yet it waits for, and the clock moves on
    that far, not the host's time it took macrt to run the instructions
    in between.  Until the program stops reading, the clock stands
    still but for the waiting.

    While it's counting, it's the time the instructions run would take
    a Mac, added up as they run (counted), and what's waited for: the
    host's doesn't count at all."""

    def __init__(self):
        self._ahead = 0.0               # of the host's time
        self._paced = -IDLE * 2         # the host's time at the last byte read
        self.counted: float | None = None   # the time, while it's counting

    def now(self) -> float:
        if self.counted is not None:
            return self.counted
        host = time.monotonic()
        return (self._paced if host - self._paced <= IDLE else host) + self._ahead

    def wait(self, until: float) -> None:
        """Wait until then, if that's still to come."""
        if self.counted is not None:
            self.counted = max(self.counted, until)
            return
        self._ahead += max(0.0, until - self.now())

    def sleep(self, seconds: float) -> None:
        """Wait that long, the host too, and stand still there for a
        program that goes on to read the disk byte by byte."""
        if self.counted is not None:
            self.counted += max(0.0, seconds)
            return
        until = self.now() + seconds
        time.sleep(max(0.0, seconds))
        self._paced = time.monotonic()
        self._ahead = until - self._paced

    @contextmanager
    def held(self) -> Iterator[None]:
        """Stand still while macrt works something out that a Mac doesn't
        take time over, the bits on a track the head has just reached, say:
        the disk doesn't turn on through it (PageMaker counts the bytes
        from there)."""
        if self.counted is not None:
            yield
            return
        start = time.monotonic()
        try:
            yield
        finally:
            gone = time.monotonic() - start
            if start - self._paced <= IDLE:
                self._paced += gone
            self._ahead -= gone

    def pace(self) -> None:
        """A byte from the disk is being read, on from the last: the host's
        time since then, if it was no more than IDLE ago, doesn't count."""
        if self.counted is not None:
            return
        host = time.monotonic()
        if host - self._paced <= IDLE:
            self._ahead -= host - self._paced
        self._paced = host

    @contextmanager
    def counting(self) -> Iterator[None]:
        """Count the time from now, adding to counted what each instruction
        run takes, and go on from there afterwards, standing still as a
        byte has just been read."""
        if self.counted is not None:
            yield
            return
        self.counted = self.now()
        try:
            yield
        finally:
            host = time.monotonic()
            self._paced, self._ahead = host, self.counted - host
            self.counted = None


class VIA:
    def __init__(self, button: Callable[[], bool] = lambda: False, clock: Clock | None = None):
        self.button = button            # whether the mouse button is down
        self.clock = clock or Clock()
        self.regs = bytearray(16)
        # The main screen and sound buffers, and SEL low, as reading the
        # boot blocks from a disk's first side leaves it.
        self.regs[vBufA], self.regs[vDirA] = 0x4F, 0x7F
        self.regs[vBufB], self.regs[vDirB] = 0x87, 0x87     # sound and clock off
        self.regs[vIER] = CA1 | CA2 | SR
        self._h4 = 0
        self._tick = tick_count()       # when CA1 was last cleared
        self._second = int(self.clock.now())
        # Each timer: when it was started and from what, or None while stopped;
        # and whether it's still to flag running out (a one-shot flags once).
        self._t1: tuple[float, int] | None = None
        self._t2: tuple[float, int] | None = None
        self._armed = 0                 # T1, T2
        self._flags = 0                 # flags set by writes, and timers that ran out
        self._alternate = False         # whether the alternate screen buffer is shown
        # Told when the buffer that's shown changes: True for the alternate.
        self.on_page: Callable[[bool], None] | None = None

    # -- the registers -------------------------------------------------------
    def read(self, reg: int) -> int:
        regs = self.regs
        if reg == vBufB:
            self._h4 ^= vH4
            inputs = (0 if self.button() else vSW) | self._h4
            return regs[vBufB] & regs[vDirB] | inputs & ~regs[vDirB] & 0xFF
        if reg in (vBufA, _ORA):
            return regs[vBufA] & regs[vDirA] | vSCCWrReq & ~regs[vDirA] & 0xFF
        if reg in (vT1C, vT1CH):
            value = self._count(self._t1, continuous=bool(regs[vACR] & 0x40))
            if reg == vT1C:
                self._clear(T1)
            return value & 0xFF if reg == vT1C else value >> 8
        if reg in (vT2C, vT2CH):
            value = self._count(self._t2)
            if reg == vT2C:
                self._clear(T2)
            return value & 0xFF if reg == vT2C else value >> 8
        if reg == vSR:
            return 0
        if reg == vIFR:
            flags = self.flags()
            return flags | (ANY if flags & regs[vIER] & 0x7F else 0)
        if reg == vIER:
            return regs[vIER] | ANY
        return regs[reg]

    def write(self, reg: int, value: int) -> None:
        value &= 0xFF
        regs = self.regs
        if reg == _ORA:
            reg = vBufA
        if reg == vIFR:
            self._clear(value & 0x7F)
        elif reg == vIER:
            regs[vIER] = regs[vIER] | value & 0x7F if value & ANY else regs[vIER] & ~value & 0x7F
        elif reg in (vT1C, vT1L):
            regs[vT1L] = value
        elif reg == vT1CH:
            regs[vT1LH] = value
            self._t1 = (self.clock.now(), regs[vT1LH] << 8 | regs[vT1L])
            self._flags &= ~T1
            self._armed |= T1
        elif reg == vT2CH:
            self._t2 = (self.clock.now(), value << 8 | regs[vT2C])
            self._flags &= ~T2
            self._armed |= T2
        else:
            regs[reg] = value
            if reg in (vBufA, vDirA):
                self._page()

    def main_page(self) -> None:
        """Show the main screen buffer again, as when an application quits."""
        self.regs[vBufA] |= vPage2
        self._page()

    def _page(self) -> None:
        regs = self.regs
        alternate = bool(regs[vDirA] & vPage2) and not regs[vBufA] & vPage2
        if alternate != self._alternate:
            self._alternate = alternate
            if self.on_page is not None:
                self.on_page(alternate)

    def pending(self) -> int:
        """The flags that interrupt the processor: those that are enabled."""
        return self.flags() & self.regs[vIER] & 0x7F

    def flags(self) -> int:
        flags, now = self._flags, self.clock.now()
        if tick_count() != self._tick:
            flags |= CA1
        if int(now) != self._second:
            flags |= CA2
        for timer, bit in ((self._t1, T1), (self._t2, T2)):
            if (self._armed & bit and timer is not None
                    and (now - timer[0]) * HZ > timer[1]):
                flags |= bit
        return flags

    def _clear(self, bits: int) -> None:
        if bits & CA1:
            self._tick = tick_count()
        if bits & CA2:
            self._second = int(self.clock.now())
        if bits & T1 and self._t1 is not None:
            if self.regs[vACR] & 0x40:          # free-running: it goes round again
                start, count = self._t1
                periods = int((self.clock.now() - start) * HZ) // (count + 2)
                if self.regs[vIER] & T1 and periods * (count + 2) <= CATCH_UP * HZ:
                    periods = min(periods, 1)   # the next time round it was late for
                if periods:                     # from the latches, from then on
                    self._t1 = (start + periods * (count + 2) / HZ,
                                self.regs[vT1LH] << 8 | self.regs[vT1L])
            elif self.flags() & T1:             # a one-shot that has run out
                self._armed &= ~T1
        if bits & T2 and self.flags() & T2:
            self._armed &= ~T2
        self._flags &= ~bits

    def _count(self, timer: tuple[float, int] | None, continuous: bool = False) -> int:
        if timer is None:
            return 0xFFFF
        start, count = timer
        gone = int((self.clock.now() - start) * HZ)
        if continuous:
            return (count - gone % (count + 2)) & 0xFFFF
        return (count - gone) & 0xFFFF

    # -- on the bus ----------------------------------------------------------
    def map(self, uc) -> None:
        """Map the VIA's pages into the processor's address space."""
        uc.mmio_map(MAP_START, MAP_END - MAP_START, self._bus_read, None, self._bus_write, None)

    def _bus_read(self, uc, offset: int, size: int, _data) -> int:
        value = 0
        for n in range(size):
            value = value << 8 | self._byte(MAP_START + offset + n)
        return value

    def _bus_write(self, uc, offset: int, size: int, value: int, _data) -> None:
        for n in range(size):
            addr = MAP_START + offset + n
            if not addr & 1:
                self.write(addr >> 9 & 15, value >> 8 * (size - 1 - n))

    def _byte(self, addr: int) -> int:
        """The VIA is on the high byte: an even address reads a register
        (A9-A12 pick which); an odd one, nothing."""
        return self.read(addr >> 9 & 15) if not addr & 1 else 0xFF


class Drive:
    """A floppy drive: the track its head is over, which way it steps, and
    how far round its disk has turned."""

    def __init__(self, clock: Clock):
        self.clock = clock
        self.track, self.inward = 0, True
        self.motor = True
        self._angle, self._when = 0.0, clock.now()

    def turn_time(self) -> float:
        """How long the disk takes to turn once, in seconds, with the head
        where it is."""
        return 60 / RPM[self.track // 16]

    def angle(self) -> float:
        """How far round the disk is from its index, in turns."""
        return (self._angle + (self.clock.now() - self._when) / self.turn_time()) % 1

    def seek(self, track: int) -> None:
        """Put the head over the track; the disk turns on as it was."""
        self._angle, self._when = self.angle(), self.clock.now()
        self.track = max(0, min(TRACKS - 1, track))

    def step(self) -> None:
        self.seek(self.track + (1 if self.inward else -1))


class Shifting:
    """The bits going by under the head as the IWM reads them, one at a
    time, as its clock is counted: each is shifted in once it has gone by.
    A byte is latched into the data register when a one has been shifted
    up to its top bit, and the shifting starts again from nothing; reading
    the data register clears it, as the IWM does in its asynchronous mode
    (the IWM's specification, as the 68kmla's "Cloning the IWM" has it).
    With Q6 or Q7 high it doesn't shift: what it has shifted in stays
    there, and the bits going by meanwhile are missed, so the bytes read
    on from there are framed from the bits that come after them, as Clock
    Signal's IWM has it.  Shufflepuck Cafe's copy protection reads a key
    sector so, and looks for the byte its bits make up.

    A byte latched while another is still to be read is kept for it, as
    long as the program goes on reading: macrt's count of the time its
    instructions take is near enough, not exact.  What has waited longer
    than IDLE goes, but the last."""

    def __init__(self, bits: str, bit: float, when: float, bit_time: float):
        self._bits, self._count = bits * 3, len(bits)   # round and round
        self._bit_time = bit_time
        self._origin = when - bit * bit_time    # when bit 0 began to go by
        self._at = math.ceil(bit)               # the next bit to shift in, from bit 0
        self._shifted = 0
        self._latched: deque[tuple[int, float]] = deque()   # each byte and when
        self.shifting = True

    def _gone(self, now: float) -> int:
        """How many bits have gone by, from bit 0."""
        return math.floor((now - self._origin) / self._bit_time + 1e-9)

    def _next(self, at: int, shifted: int) -> tuple[int, int, int] | None:
        """The byte shifted in from bit at on, with what was shifted in
        before; where its bits begin, and where they end: None if no one
        goes by."""
        here = at % self._count
        if shifted:
            start = at
        else:
            one = self._bits.find("1", here, here + self._count)
            if one < 0:
                return None
            start = at + one - here
        end = start + 8 - shifted.bit_length()
        return (shifted << end - start | int(self._bits[start - at + here:end - at + here], 2),
                start, end)

    def run_to(self, now: float) -> None:
        """Shift in what has gone by until now."""
        if not self.shifting:
            return
        gone = self._gone(now)
        if gone - self._at > 2 * self._count:   # turns of the disk unread
            self._at += ((gone - self._at) // self._count - 1) * self._count
        while self._at < gone:
            found = self._next(self._at, self._shifted)
            if found is None:
                self._at = gone
                break
            byte, start, end = found
            if end > gone:
                if start < gone:
                    here = self._at % self._count
                    self._shifted = self._shifted << gone - start | int(
                        self._bits[start - self._at + here:gone - self._at + here], 2)
                self._at = gone
                break
            self._latched.append((byte, self._origin + end * self._bit_time))
            self._shifted, self._at = 0, end

    def shift(self, on: bool, now: float) -> None:
        """Shift the bits in from now, or stop."""
        if on == self.shifting:
            return
        if on:
            self._at = max(self._at, self._gone(now))
        else:
            self.run_to(now)
        self.shifting = on

    def read(self, now: float) -> int:
        """The data register: the byte latched, or 0 if there's none."""
        self.run_to(now)
        latched = self._latched
        if not latched:
            return 0
        if now - latched[0][1] > IDLE:
            while len(latched) > 1:
                latched.popleft()
        return latched.popleft()[0]

    def ahead(self, count: int) -> list[tuple[int, float]]:
        """The next count bytes the data register will have, and when."""
        out = list(self._latched)[:count]
        at, shifted = self._at, self._shifted
        while len(out) < count:
            found = self._next(at, shifted)
            if found is None:
                break
            byte, _, at = found
            out.append((byte, self._origin + at * self._bit_time))
            shifted = 0
        return out

    def drop(self, count: int) -> None:
        """Pass over the next count bytes latched."""
        for _ in range(min(count, len(self._latched))):
            self._latched.popleft()


class IWM:
    """The floppy disk controller: a switch is picked by A9-A12, as the
    VIA's registers are."""

    def __init__(self, head_select: Callable[[], bool] = lambda: False,
                 clock: Clock | None = None):
        self.clock = clock or Clock()
        self.switches = 0               # a bit for each switch that's set
        self.mode = 0x1F                # as the disk driver sets it
        self._turn, self._n = b"", 0    # the bytes on the track under the head, and the next's
        self._under = (0, 0)            # the drive and track they're from
        # When they come by as the disk turns, where each of them begins in
        # bits from the first, how many bits go round once, how long a bit
        # takes to go by, when the first began to, and when the data
        # register was last read.
        self._starts: list[int] = []
        self._bit_time = self._since = self._read_at = 0.0
        self._bits = 0
        self.head_select = head_select  # SEL
        self.drives = (Drive(self.clock), Drive(self.clock))
        # What the disk in drive 1 or 2 has on a side of a track: what's
        # read from it twice round, where each byte begins in bits from the
        # index, and how many bits go round once; None with no such disk.
        self.surface: Callable[[int, int, int], tuple[bytes, list[int], int] | None] = \
            lambda drive, track, side: None
        # And the bits that go by there once round, as the drive reads them
        # this time, for the IWM to shift in one by one while the clock is
        # counting, as it is while a program's own routine reads a sector.
        self.bits: Callable[[int, int, int], str | None] = lambda drive, track, side: None
        self._shifting: Shifting | None = None

    @property
    def drive(self) -> Drive:
        """The drive that's selected."""
        return self.drives[self.switches >> DRIVE_2 & 1]

    def select(self, drive: int) -> None:
        """Select drive 1 or 2, the internal or the external, as the disk
        driver does, and turn its motor on."""
        self.touch(DRIVE_2 if drive == 2 else DRIVE_2 - 1)
        self.touch(MOTOR_ON)

    def put(self, turn: bytes) -> None:
        """Turn the disk to the start of turn, the bytes it holds from
        there round: what the data register reads, in turn, from now, as
        fast as it's read."""
        self._turn, self._n, self._starts = turn, 0, []
        self._under = self._head()
        self._shifting = None

    def follow(self, found: tuple[bytes, list[int], int], bit: float, when: float) -> None:
        """Have the disk turned so that the bit, from the index, is under
        the head then: what the data register reads, from the bytes found
        on the surface, is from the first to begin there or after, each
        once it has come by.  While the clock counts, it's the bits that
        go by from there, shifted in one by one (Shifting)."""
        out, starts, count = found
        self._shifting = None
        if self.clock.counted is not None:
            drive, track = self._head()
            bits = self.bits(drive, track, int(self.head_select()))
            if bits:
                self._turn, self._n, self._starts = b"", 0, []
                self._under = drive, track
                self._shifting = Shifting(bits, bit % len(bits), when,
                                          self.drive.turn_time() / len(bits))
                self._shifting.shift(self._reading(), when)
                return
        # A turn, the second, the drive found where its bytes begin, and
        # the end of the first, past where that stopped
        first = bisect.bisect_left(starts, count)
        tail = bisect.bisect_right(starts, starts[-1] - count, 0, first) if starts else 0
        out = out[first:] + out[tail:first]
        starts = [n - count for n in starts[first:]] + starts[tail:first]
        self._turn, self._n, self._starts = b"", 0, []
        self._under = self._head()
        if not starts:
            return
        bit %= count
        at = bisect.bisect_left(starts, bit)
        begins = starts[at] if at < len(starts) else starts[0] + count
        at %= len(starts)
        self._turn = out[at:] + out[:at]
        self._starts = ([n - starts[at] for n in starts[at:]]
                        + [n + count - starts[at] for n in starts[:at]])
        self._bits, self._bit_time = count, self.drive.turn_time() / count
        self._since = self._read_at = when + (begins - bit) * self._bit_time

    def _reading(self) -> bool:
        """Whether a read reads the data register: Q6 and Q7 low."""
        return not self.switches & (1 << Q6_HIGH | 1 << Q7_HIGH)

    def _head(self) -> tuple[int, int]:
        """The drive that's selected, 1 or 2, and the track its head is on."""
        return (self.switches >> DRIVE_2 & 1) + 1, self.drive.track

    def _turned_to(self) -> None:
        """Read the track the head has been moved to from where the disk
        has turned to, on the side SEL picks: nothing from a side with
        no bits on it."""
        drive, track = self._under = self._head()
        with self.clock.held():
            found = self.surface(drive, track, int(self.head_select()))
        self._turn, self._n, self._starts, self._shifting = b"", 0, [], None
        if found and found[2]:
            self.follow(found, self.drive.angle() * found[2], self.clock.now())

    def _turned_on(self) -> None:
        """A byte is being read, as the disk turns: pass over what has gone
        by unread if the data register hasn't been read for a while.  A
        program reading it byte after byte reads every byte, as a Mac's
        processor keeps up with the disk."""
        self.clock.pace()
        now = self.clock.now()
        if now - self._read_at > IDLE:
            turns, bit = divmod((now - self._since) / self._bit_time, self._bits)
            self._n = max(self._n, int(turns) * len(self._turn)
                          + bisect.bisect_left(self._starts, bit))

    def _come_by(self, n: int) -> float:
        """When the nth byte from the first will have come by, all 8 bits."""
        turns, at = divmod(n, len(self._turn))
        return self._since + (turns * self._bits + self._starts[at] + 8) * self._bit_time

    def ahead(self, count: int) -> bytes:
        """The next count bytes the data register will read."""
        if self._shifting is not None:
            self._shifting.run_to(self.clock.now())
            return bytes(byte for byte, _ in self._shifting.ahead(count))
        if not self._turn:
            return b""
        if self._starts:
            self._turned_on()
        at = self._n % len(self._turn)
        turn = self._turn * (1 + (at + count) // len(self._turn))
        return turn[at:at + count]

    def skip(self, count: int) -> None:
        """Turn the disk on past count bytes."""
        if self._shifting is not None:
            coming = self._shifting.ahead(count)
            if coming:
                self.clock.wait(coming[-1][1])
                self._shifting.run_to(self.clock.now())
                self._shifting.drop(len(coming))
            return
        if self._turn and count > 0:
            self._n += count
            if self._starts:
                self.clock.wait(self._come_by(self._n - 1))
                self._read_at = self.clock.now()

    def read(self, switch: int) -> int:
        self.touch(switch)
        q6, q7 = self.switches >> Q6_HIGH & 1, self.switches >> Q7_HIGH & 1
        if q7:
            return 0xFF                 # handshake: ready, and no underrun
        if q6:                          # status
            return (self.sense() << 7 | (0x20 if self.switches >> MOTOR_ON & 1 else 0)
                    | self.mode)
        if self._under != self._head():
            self._turned_to()
        if self._shifting is not None:
            return self._shifting.read(self.clock.now())
        if not self._turn:
            return 0
        if self._starts:
            self._turned_on()
            self.clock.wait(self._come_by(self._n))
            self._read_at = self.clock.now()
        byte = self._turn[self._n % len(self._turn)]
        self._n += 1
        return byte

    def write(self, switch: int, value: int) -> None:
        """Touch the switch, writing the value: the mode, with Q6 and Q7
        high and the motor off."""
        self.touch(switch)
        if (self.switches >> Q6_HIGH & 1 and self.switches >> Q7_HIGH & 1
                and not self.switches >> MOTOR_ON & 1):
            self.mode = value & 0x1F

    def touch(self, switch: int) -> None:
        """Set the switch's state: each even and odd pair is one, off and on."""
        on = 1 << (switch | 1)
        if switch == LSTRB and not self.switches & on:
            self._strobe()
        self.switches = self.switches | on if switch & 1 else self.switches & ~on
        if self._shifting is not None:
            self._shifting.shift(self._reading(), self.clock.now())

    def register(self) -> int:
        """The number of the drive register CA2, SEL, CA0 and CA1 choose."""
        on = self.switches
        return (on >> CA2_ON & 1 | int(self.head_select()) << 1 | (on >> CA0_ON & 1) << 2
                | (on >> CA1_ON & 1) << 3)

    def sense(self) -> int:
        """What the selected drive's register chosen says, which the
        status register has in its top bit.  A step here is over at once.
        The others read 0: a disk in the drive, which is there and ready."""
        drive, register = self.drive, self.register()
        if register == DIRTN:
            return int(not drive.inward)
        if register == STEP:
            return 1
        if register == MOTORON:
            return int(not drive.motor)
        if register == TK0:
            return int(drive.track != 0)
        if register == TACH:
            return int(drive.angle() * 120) & 1
        return 0

    def _strobe(self) -> None:
        """Write CA2 to the drive register CA0, CA1 and SEL choose: which
        way the head steps, a step as CA2 goes low, or the motor, on as
        it's low.  The eject's is dropped."""
        on = self.switches
        if self.head_select():
            return
        if on & 1 << CA1_ON:
            if not on & 1 << CA0_ON:
                self.drive.motor = not on & 1 << CA2_ON
        elif not on & 1 << CA0_ON:
            self.drive.inward = not on & 1 << CA2_ON
        elif not on & 1 << CA2_ON:
            self.drive.step()

    def map(self, uc) -> None:
        uc.mmio_map(IWM_START, IWM_END - IWM_START, self._bus_read, None, self._bus_write, None)

    def _bus_read(self, uc, offset: int, size: int, _data) -> int:
        value = 0
        for n in range(size):
            addr = IWM_START + offset + n
            value = value << 8 | (self.read(addr >> 9 & 15) if addr & 1 else 0xFF)
        return value

    def _bus_write(self, uc, offset: int, size: int, value: int, _data) -> None:
        for n in range(size):
            addr = IWM_START + offset + n
            if addr & 1:
                self.write(addr >> 9 & 15, value >> 8 * (size - 1 - n) & 0xFF)


def map_empty(uc, start: int, end: int) -> None:
    """Map addresses with nothing at them: they read 0, and what's written
    there is dropped."""
    uc.mmio_map(start, end - start,
                lambda uc, offset, size, _data: 0, None,
                lambda uc, offset, size, value, _data: None, None)


def map_test_rom(uc) -> None:
    """Map the test ROM's space, with no test ROM in it."""
    map_empty(uc, TEST_ROM_START, TEST_ROM_END)
