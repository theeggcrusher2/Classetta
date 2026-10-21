"""
The floppy disk driver, .Sony, reference number -5 (Inside Macintosh II,
"The Disk Driver"): a disk image's sectors in drive 1 or 2, read and
written by number rather than through files, as copy protection and disk
tools do.  ioVRefNum is the drive; ioPosOffset and ioReqCount are
multiples of 512.

Besides its 512 bytes each sector has a 12-byte tag, where the File
Manager wrote the file and block it's part of.  The driver puts the tag
of each sector it reads at TagData+2, and in the buffer Control 8 gives
it, one after another, if a program has given it one.  An image that
kept no tags has zeros there.  A MOOF image's sector that can't be read
is an error, as on a Mac: copy protection reads one to see that it can't.
What's written goes to the image in memory, not to the files the File
Manager has from it nor back to the image's file.

The 64K ROM's driver read a sector's data field through a vector in low
memory, RdData, once RdAddr had found its address field, and a program
can put its own routine there to read what follows on the disk itself:
Excel 1.0 reads the sector after its key disk's mark so.  If a program
has, the routine is called for each sector of a disk whose tracks the
image has, a MOOF image's, with the IWM about to read the bytes after
the sector's address field.  A4 is the IWM's data register; A5 the VIA's
port A, its bit 7 high, so no serial data waits at A6, the SCC's channel
A; A0 where the sector goes and A1 the data field's marks; and A3 the
ROM's table of the six bits each byte from the disk stands for, the byte
an index from A3, $FF for one that stands for none, through which
MacGammon's routine, a copy of the ROM's, decodes.  It leaves the
sector's result in D0.  It can go on to the ROM's own routine, here
rd_data, as Dollars & Sense's does with marks of its own, the ones its
key disk's sector has.  The File Manager reads an MFS file's sectors
through such a routine too, as the 64K ROM's did, so a program that
reads its serial number from a file it wrote on its own track, as Mac
Attack does, reads it.

RdAddr and AdrDisk, the vectors beside it, are the ROM's own routines
here as well.  AdrDisk chooses one of the drive's registers, by the
phases and the VIA's head select, from D0's low four bits; a strobe then
sets the direction the head steps in, steps it a track or turns the
motor on or off, and the IWM's status has what the register says in its
top bit.  RdAddr waits
for the next address field to come round under the head and leaves its
track, sector, side and format in D1 to D4, its result in D0 and A3 at
the table above: the disk turns at the speed its zone does, as the
timing checks of copy protection measure, and the IWM reads on from
there as it turns.  So are the routines through the vectors after them,
which Jazz's copy protection calls: Seek moves the head to D6's track
and Recal out to track 0; SetUpPoll points A4 to A6 as above and keeps
the caller's stack in PollStack; WakeUp calls the routine on the stack
once D0's microseconds have gone by; MakeSpdTbl and SetSpeed, which set
the drive's speed, succeed, the drives here turning as they should.
SonyVars points at the driver's variables, where a program finds the
drive it was last used on, the track each drive's head is over, and how
long a step and the head's settling take, which it waits for itself.

A program can put its own routine in RdAddr too, and the driver calls
that to find the sector, with A2 at the address field's marks and A4 to
A6 as above, again for each field it says is another sector's (D2), as
many times as two turns of the track have fields; then RdData reads on.
Dragonworld puts its own in both: its RdAddr says it has found the
sector at once, and its RdData counts what its key track has instead.
"""

from __future__ import annotations

import bisect
import struct
from concurrent.futures import Future
from pathlib import Path

from .. import moof
from ..memory import ADDRESS_MASK
from ..toolbox import lowmem
from ..toolbox.constants import diskEvt
from ..toolbox.events import EventRecord, tick_count
from ..toolbox.files import SONY, FileError
from ..ui import diskdlg
from .cpu import INTERRUPT_MASK
from .hardware import (CA0_ON, CA1_ON, CA2_ON, IWM_BASE, LSTRB, Q6_LOW, STRIDE, VIA_BASE, vBufA,
                       vHeadSel)

SECTOR, TAG = 512, 12
SONY_VARS = 0x200               # how much room the driver's variables have
CURRENT_DRIVE = 0x12            # the drive it was last used on
SETTLE_TIME = 0x34              # how long it waits for the head to settle on a track, in µs
STEP_TIME = 0x3C                # and for a step to be over
DRIVE_VARS = {1: 0x4A, 2: 0x8C}     # each drive's: the track its head is over first
SETTLE, STEPPING = 30_000, 12_000   # the times here
SCC_A_DATA = 0x9FFFFE           # where the SCC's channel A data is read
SEARCH = 64                     # how far past the address field the data marks may be
OUT_FOR = 60                    # ticks an ejected disk is out of its drive

controlErr = -17
statusErr = -18
ioErr = -36                     # what the File Manager says when the driver fails it
paramErr = -50
nsDrvErr = -56                  # no such drive
offLinErr = -65                 # no disk in the drive
dataVerErr = -68                # it isn't what the buffer has, in verifying
noNybErr = -66                  # the address field has a byte a disk can't hold
noAdrMkErr = -67                # no address field comes round
badCksmErr = -69                # it doesn't check
badBtSlpErr = -70               # the marks that end it aren't there
noDtaMkErr = -71                # the sector's data field isn't there
badDCksum = -72                 # its data field doesn't check
badDBtSlp = -73                 # the marks that end it aren't there
sectNFErr = -81                 # no address field says it's that sector
WHY = {"no address field": sectNFErr, "no data field": noDtaMkErr,
       "a damaged data field": badDCksum}

# The parameter block: IOParam's, and CntrlParam's for Control and Status
ioVRefNum = 22
ioBuffer = 32
ioReqCount = 36
ioActCount = 40
ioPosMode = 44
ioPosOffset = 46
csCode = 26
csParam = 28
fsAtMark, fsFromStart, fsFromMark = 0, 1, 3

killCode, verifyCode, ejectCode, tagBufferCode, trackCacheCode = 1, 5, 7, 8, 9
driveStatusCode = 8


def _volume(t, drive: int):
    """The volume on the disk in the drive, mounted or not, None if
    there's none; an error if there's no such drive."""
    if drive not in (1, 2):
        raise FileError(nsDrvErr)
    vol = t.system.files.in_drive(drive)
    return vol if vol is not None and vol.floppy else None


def _tracks(t, drive: int):
    """The tracks of the disk in drive 1 or 2, None unless it's a disk
    whose tracks are known."""
    vol = _volume(t, drive)
    disk = getattr(getattr(vol.store, "volume", None), "disk", None) if vol else None
    return disk.tracks if disk is not None and disk.tracks else None


def surface(machine, drive: int, track: int, side: int):
    """What the IWM reads from a side of a track of the disk in the drive
    (IWM.surface)."""
    tracks = _tracks(machine, drive) if drive not in machine.ejected else None
    return tracks.framed(track, side) if tracks else None


def bits(machine, drive: int, track: int, side: int):
    """The bits that go by under the head there, once round (IWM.bits)."""
    tracks = _tracks(machine, drive) if drive not in machine.ejected else None
    return tracks.bits(track, side) if tracks else None


def _disk(t, pb: int):
    drive = t.mem.r16s(pb + ioVRefNum)
    vol = _volume(t, drive) if drive not in t.machine.ejected else None
    disk = getattr(getattr(vol.store, "volume", None), "disk", None) if vol else None
    if disk is None:
        raise FileError(offLinErr)
    return disk


def _span(t, pb: int, disk) -> tuple[int, int]:
    """The first sector the call is for, and how many."""
    m = t.mem
    machine = t.machine
    mode, offset, count = m.r16(pb + ioPosMode) & 3, m.r32s(pb + ioPosOffset), \
        m.r32s(pb + ioReqCount)
    at = {fsFromStart: offset, fsFromMark: machine.disk_position + offset}.get(
        mode, machine.disk_position)
    if at < 0 or count < 0 or at % SECTOR or count % SECTOR \
            or at + count > len(disk.sectors):
        raise FileError(paramErr)
    return at // SECTOR, count // SECTOR


def read(t, pb: int) -> int:
    m = t.mem
    m.w32(pb + ioActCount, 0)
    disk = _disk(t, pb)
    first, count = _span(t, pb, disk)
    buffer, tags = m.r32(pb + ioBuffer) & ADDRESS_MASK, t.machine.tag_buffer
    err = 0
    for i in range(count):
        n, at = first + i, buffer + i * SECTOR
        _seek(t, m.r16s(pb + ioVRefNum), disk, n)
        routines = _routines(t, disk)
        if routines:
            err = _read_through(t, disk, n, routines, at)
        else:
            why = disk.unreadable(n)
            err = WHY[why] if why else 0
            m.write(at, bytes(disk.sectors[n * SECTOR:(n + 1) * SECTOR]))
            m.write(lowmem.TagData + 2, bytes(disk.tags[n * TAG:(n + 1) * TAG]).ljust(TAG, b"\0"))
        if tags:
            m.write(tags + i * TAG, m.read(lowmem.TagData + 2, TAG))
        if err:
            break
        m.w32(pb + ioActCount, (i + 1) * SECTOR)
        t.machine.disk_position = (n + 1) * SECTOR
    m.w16(lowmem.DskErr, err & 0xFFFF)
    return err


def _routines(t, disk) -> tuple[int, int] | None:
    """The RdAddr and RdData the driver reads a sector through, if the
    program has put its own in either vector and the disk's tracks are
    known; None if both are the ROM's, whose reading of every sector is
    known.  The program's routine can put the ROM's back, or clear the
    vector, which is taken for the ROM's."""
    if not disk.tracks:
        return None
    m, machine = t.mem, t.machine
    finder = m.r32(lowmem.RdAddr) & ADDRESS_MASK or machine.rd_addr
    reader = m.r32(lowmem.RdData) & ADDRESS_MASK or machine.rd_data
    if finder == machine.rd_addr and reader == machine.rd_data:
        return None
    return finder, reader


def read_file(t, p, pos: int, count: int, buffer: int) -> int:
    """Read what an access path to a file on a floppy is about to read
    from pos on into buffer, as the File Manager does, through the
    driver: ioErr if a sector of it can't be read, the driver's own
    error in DskErr.  King's Quest's Formaster check reads a file whose
    sector has a damaged checksum, and counts on ioErr.  A sector that a
    program's own RdData reads is what the file has there from then on.
    Mac Attack reads its serial number from a file whose sector has data
    marks of its own.  A whole sector of the file the read wants goes
    straight to buffer, where the routine finds it in A0, and a part of
    one through a buffer of the File Manager's own: Uninvited's routine
    is in the buffer it reads the first sector of its own file to, and
    leaves there the code it decrypts with what follows the sector's
    marks.  Only a disk image's sectors are read so, and only a file the
    image's volume says where it is, an MFS one's, unless it has been
    written."""
    node = p.node
    item, disk = node.ref, getattr(node.volume.store.source, "disk", None)
    sectors = getattr(item, "sectors", None)
    if disk is None or not node.volume.floppy or sectors is None or node.dirty \
            or node.mddate != item.mddate:
        return 0
    sectors, fork, m = sectors[p.rsrc], p.fork, t.mem
    end = min(pos + count, len(fork), len(sectors) * SECTOR)
    for i in range(pos // SECTOR, -(-end // SECTOR)):
        n, routines = sectors[i], _routines(t, disk)
        _seek(t, node.volume.drive, disk, n)
        if not routines:
            why = disk.unreadable(n)
            if why:
                return _file_error(t, WHY[why])
            continue                    # the file has it already
        at = i * SECTOR
        if pos <= at and at + SECTOR <= end:
            into = buffer + at - pos
        else:
            if not t.machine.sector_buffer:
                t.machine.sector_buffer = t.system.sys_heap.new_ptr(SECTOR)
            into = t.machine.sector_buffer
        err = _read_through(t, disk, n, routines, into)
        if err:
            return _file_error(t, err)
        fork[at:min(at + SECTOR, len(fork))] = m.read(into, min(SECTOR, len(fork) - at))
    return 0


def _file_error(t, err: int) -> int:
    t.mem.w16(lowmem.DskErr, err & 0xFFFF)
    return ioErr


def _seek(t, drive: int, disk, sector: int) -> None:
    """Move the drive's head to the sector's track, as reading it does,
    select the drive, which SonyVars says too, and the head on the
    sector's side with SEL: a program can go on from there itself.  Only
    a disk whose tracks are known has them."""
    sony = t.mem.r32(lowmem.SonyVars) & ADDRESS_MASK
    if sony:
        t.mem.w16(sony + CURRENT_DRIVE, drive)
    if disk.tracks:
        iwm, via = t.machine.iwm, t.machine.via
        track, side, _ = disk.tracks.place(sector)
        iwm.select(drive)
        _seek_head(t, track)
        via.write(vBufA, via.regs[vBufA] & ~vHeadSel | (vHeadSel if side else 0))


def _seek_head(t, track: int) -> None:
    """Move the selected drive's head to the track, and say so in its
    variables, as the driver does."""
    iwm, sony = t.machine.iwm, t.mem.r32(lowmem.SonyVars) & ADDRESS_MASK
    iwm.drive.seek(track)
    if sony:
        t.mem.w16(sony + DRIVE_VARS[2 if iwm.drive is iwm.drives[1] else 1], iwm.drive.track)


def set_up_vars(mem, sony: int) -> None:
    """The driver's variables, as it starts: drive 1's the drive it was
    last used on, its head over track 0, and the times it waits for."""
    mem.w16(sony + CURRENT_DRIVE, 1)
    mem.w16(sony + SETTLE_TIME, SETTLE)
    mem.w16(sony + STEP_TIME, STEPPING)


def _read_through(t, disk, sector: int, routines: tuple[int, int], at: int) -> int:
    """Read the sector with RdData, the IWM at its data field, as the
    address field has just gone by: what the routine says of it.  If the
    program has its own RdAddr, that finds the field.  The IWM shifts in
    the disk's bits one by one, and the clock counts the time the
    routines' instructions would take a Mac, as a routine may count them
    (Shufflepuck Cafe does).  Interrupts are masked meanwhile, as the
    Disk Driver masks them during disk accesses (Inside Macintosh II-212):
    a VBL task's instructions mustn't count as the routine's (Downhill
    Racer's)."""
    finder, reader = routines
    iwm, cpu, machine = t.machine.iwm, t.cpu, t.machine
    track, side, number = disk.tracks.place(sector)
    sr = cpu.sr
    cpu.sr = sr | INTERRUPT_MASK
    try:
        with iwm.clock.counting(), cpu.timed(iwm.clock):
            if finder != machine.rd_addr:
                err = _find(t, finder, disk.tracks.framed(track, side), track, number)
                if err:
                    return err
            else:
                bit = disk.tracks.address_end(sector)
                if bit is None:
                    return sectNFErr        # RdAddr never finds it
                iwm.follow(disk.tracks.framed(track, side), bit, iwm.clock.now())
            d0 = t.call_with(reader, a0=at, a1=machine.data_marks,
                             a3=machine.denibble - moof.GCR[0], **_POLL)
    finally:
        cpu.sr = sr
        iwm.put(b"")
    return _signed(d0)


# Where the ROM's routines read the disk and poll the serial ports
# through, as SetUpPoll has them
_POLL = {"a4": IWM_BASE + Q6_LOW * STRIDE, "a5": VIA_BASE + vBufA * STRIDE, "a6": SCC_A_DATA}


def _find(t, finder: int, found, track: int, number: int) -> int:
    """Have the program's own RdAddr find the address field of the
    sector, number on the track, from wherever the disk has turned to, as
    the ROM's driver does: again for each field it says is another's, two
    turns' worth at most.  Its error if it has one.  Dragonworld's says
    it has found it at once, and its RdData reads what the key track has
    instead."""
    iwm = t.machine.iwm
    iwm.follow(found, iwm.drive.angle() * found[2], iwm.clock.now())
    err = sectNFErr
    for _ in range(2 * moof.sectors_on(track)):
        d0, d2 = t.call_for(finder, ("d0", "d2"), a2=t.machine.address_marks, **_POLL)
        err = _signed(d0)
        if not err and d2 & 0xFF == number:
            return 0
    return err or sectNFErr


def _signed(d0: int) -> int:
    return (d0 & 0xFFFF) - ((d0 & 0x8000) << 1)


def rd_data(t) -> None:
    """The ROM's RdData, called with JSR: the data field the IWM is coming
    to, its marks the five bytes at A1, three before it and two after,
    read into the 512 bytes at A0 and its tag to TagData+2, or compared
    with what's at A0 if DskVerify is set.  The result in D0 and the
    condition codes."""
    cpu, m, iwm = t.cpu, t.mem, t.machine.iwm
    marks = m.read(cpu.a(1) & ADDRESS_MASK, 5)
    ahead = iwm.ahead(SEARCH + 4 + moof.NIBBLES + 2)
    at = ahead.find(marks[:3], 0, SEARCH + 3)
    if at < 0:
        err = noDtaMkErr
        iwm.skip(SEARCH)
    else:
        at += 4                         # past the marks and the sector's number
        field = moof.data_field(ahead[at:at + moof.NIBBLES])
        at += moof.NIBBLES
        iwm.skip(at + 2)
        if field is None:
            err = badDCksum
        elif ahead[at:at + 2] != marks[3:]:
            err = badDBtSlp
        else:
            data, buffer = field[TAG:], cpu.a(0) & ADDRESS_MASK
            if m.r8(lowmem.DskVerify):
                err = 0 if m.read(buffer, SECTOR) == data else dataVerErr
            else:
                err = 0
                m.write(buffer, data)
            m.write(lowmem.TagData + 2, field[:TAG])
    cpu.set_d(0, err)
    cpu.sr = cpu.sr & ~0xF | (0x8 if err < 0 else 0) | (0x4 if not err else 0)
    cpu.pc = cpu.pop32()


def adr_disk(t) -> None:
    """The ROM's AdrDisk, called with JSR: choose the register of the
    selected drive that D0's low four bits say, CA2, SEL, CA0 and CA1 from
    the lowest, with LSTRB low, for the program to strobe or sense
    through the IWM, whose address it's left in A0.  D1's low word is
    left at where the variables of the drive it was last used on begin
    in SonyVars (Moebius puts the track it's stepped to there)."""
    cpu, iwm, via, m = t.cpu, t.machine.iwm, t.machine.via, t.mem
    d0 = cpu.d(0)
    for bit, phase in ((0, CA2_ON), (2, CA0_ON), (3, CA1_ON)):
        iwm.touch(phase if d0 >> bit & 1 else phase - 1)
    iwm.touch(LSTRB - 1)
    via.write(vBufA, via.regs[vBufA] & ~vHeadSel | (vHeadSel if d0 & 2 else 0))
    cpu.set_a(0, IWM_BASE)
    sony = m.r32(lowmem.SonyVars) & ADDRESS_MASK
    drive = m.r16(sony + CURRENT_DRIVE) if sony else 1
    cpu.set_d(1, cpu.d(1) & ~0xFFFF | DRIVE_VARS[2 if drive == 2 else 1])
    cpu.pc = cpu.pop32()


def rd_addr(t) -> None:
    """The ROM's RdAddr, called with JSR: the next address field to come
    under the selected drive's head, on the side SEL chooses, its marks
    the five bytes at A2, three before it and two after.  It returns once
    the field has gone by, its track in D1, its sector in D2, its side in
    D3 and its format in D4, and the IWM about to read what follows it;
    the result in D0 and the condition codes.  A3 is left at the table it
    decodes the field's bytes through, as RdData has it (Jazz reads the
    sectors after through it itself).  Only a disk whose tracks are
    known has address fields to find."""
    cpu, m, iwm = t.cpu, t.mem, t.machine.iwm
    marks = m.read(cpu.a(2) & ADDRESS_MASK, 5)
    drive = iwm.drive
    with iwm.clock.held():
        tracks = _tracks(t, 2 if drive is iwm.drives[1] else 1)
        found = tracks.framed(drive.track, 1 if t.machine.via.regs[vBufA] & vHeadSel else 0) \
            if tracks else None
    err, track, sector, side, form = noAdrMkErr, 0, 0, 0, 0
    if found:
        out, starts, count = found
        here = drive.angle() * count
        at = out.find(marks[:3], bisect.bisect_left(starts, here),
                      bisect.bisect_left(starts, here + count)) if count else -1
        to = here + count               # a turn, with none coming round
        if at >= 0:
            header = [moof.SIX.get(b, -1) for b in out[at + 3:at + 8]]
            if -1 in header:
                err = noNybErr
            elif header[0] ^ header[1] ^ header[2] ^ header[3] != header[4]:
                err = badCksmErr
            elif out[at + 8:at + 10] != marks[3:]:
                err = badBtSlpErr
            else:
                err = 0
                low, sector, high, form = header[:4]
                track, side = low | (high & 0x1F) << 6, high >> 5 & 1
            to = starts[min(at + 10, len(starts) - 1)]
        iwm.clock.sleep((to - here) / count * drive.turn_time() if count else drive.turn_time())
        if count:
            iwm.follow(found, to, iwm.clock.now())
    for n, value in enumerate((err, track, sector, side, form)):
        cpu.set_d(n, value)
    cpu.set_a(3, t.machine.denibble - moof.GCR[0])
    cpu.sr = cpu.sr & ~0xF | (0x8 if err < 0 else 0) | (0x4 if not err else 0)
    cpu.pc = cpu.pop32()


def seek(t) -> None:
    """The ROM's Seek, called with JSR: move the selected drive's head to
    the track D6's low 11 bits say.  D0 is the result, 0."""
    _seek_head(t, t.cpu.d(6) & 0x7FF)
    _done(t)


def recal(t) -> None:
    """The ROM's Recal: move the head out to track 0."""
    _seek_head(t, 0)
    _done(t)


def set_up_poll(t) -> None:
    """The ROM's SetUpPoll: point A4 at the IWM's data register, A5 at the
    VIA's port A and A6 at the SCC's channel A data, which the ROM's
    routines read the disk and poll the serial ports through, and keep
    the caller's stack in PollStack, which a routine that's done puts
    back, however deep it's got (Jazz)."""
    cpu = t.cpu
    cpu.set_a(4, IWM_BASE + Q6_LOW * STRIDE)
    cpu.set_a(5, VIA_BASE + vBufA * STRIDE)
    cpu.set_a(6, SCC_A_DATA)
    cpu.pc = cpu.pop32()
    t.mem.w32(lowmem.PollStack, cpu.sp)


def wake_up(t) -> None:
    """The ROM's WakeUp: call the routine whose address is on the stack
    once D0's microseconds have gone by, as the driver's timer would
    (Inside Macintosh II-197 has it use the VIA's timer 2).  It's how the
    driver waits for a step to be over and for the head to settle, the
    times in its variables; Moebius's copy protection waits for them so
    itself, after stepping the head from the sector 0 of a track, and
    looks for sector 2 to come by next on the next."""
    t.machine.iwm.clock.sleep((t.cpu.d(0) & 0xFFFF) / 1e6)
    _done(t)


def _done(t) -> None:
    """Return from one of the ROM's routines with no error: D0 and the
    condition codes 0.  MakeSpdTbl, which times the drive's speeds, and
    SetSpeed, which sets it for the track, do no more: the drives here
    turn at the speed each track's zone does."""
    cpu = t.cpu
    cpu.set_d(0, 0)
    cpu.sr = cpu.sr & ~0xF | 0x4
    cpu.pc = cpu.pop32()


# The 64K ROM disk driver's other routines, through their vectors in low
# memory, as copy protection calls them (Jazz)
ROUTINES = ((lowmem.Seek, seek), (lowmem.SetUpPoll, set_up_poll), (lowmem.Recal, recal),
            (lowmem.WakeUp, wake_up), (lowmem.MakeSpdTbl, _done), (lowmem.SetSpeed, _done))


def write(t, pb: int) -> int:
    m = t.mem
    m.w32(pb + ioActCount, 0)
    disk = _disk(t, pb)
    first, count = _span(t, pb, disk)
    buffer, tags = m.r32(pb + ioBuffer) & ADDRESS_MASK, t.machine.tag_buffer
    for i in range(count):
        n = first + i
        disk.sectors[n * SECTOR:(n + 1) * SECTOR] = m.read(buffer + i * SECTOR, SECTOR)
        tag = m.read(tags + i * TAG if tags else lowmem.TagData + 2, TAG)
        if disk.tags:
            disk.tags[n * TAG:(n + 1) * TAG] = tag
        if disk.tracks:                 # it reads back as written
            disk.tracks.unreadable.pop(n, None)
        m.w32(pb + ioActCount, (i + 1) * SECTOR)
        t.machine.disk_position = (n + 1) * SECTOR
    m.w16(lowmem.DskErr, 0)
    return 0


def control(t, pb: int) -> None:
    m = t.mem
    code = m.r16s(pb + csCode)
    if code == tagBufferCode:
        t.machine.tag_buffer = m.r32(pb + csParam) & ADDRESS_MASK
    elif code in (verifyCode, ejectCode):
        _disk(t, pb)
        if code == ejectCode:
            eject(t, _volume(t, m.r16s(pb + ioVRefNum)))
    elif code not in (killCode, trackCacheCode):
        raise FileError(controlErr)


def status(t, pb: int) -> None:
    """Drive Status, the only one there is: the drive's queue element and
    its disk.  diskInPlace is 1 for a disk just put in and 2 once the
    driver has read it, as it has a mounted one: only then is twoSideFmt
    good (Inside Macintosh II-215).  Shufflepuck Cafe waits for the 2
    before it looks at twoSideFmt to find its disk's last sector."""
    m, machine = t.mem, t.machine
    if m.r16s(pb + csCode) != driveStatusCode:
        raise FileError(statusErr)
    drive = m.r16s(pb + ioVRefNum)
    put_back(t)
    vol = _volume(t, drive) if drive not in machine.ejected else None
    disk = getattr(getattr(vol.store, "volume", None), "disk", None) if vol else None
    two_sided = -1 if disk is not None and len(disk.sectors) > 800 * SECTOR else 0
    in_place = (0 if vol is None else
                1 if drive in machine.put_back or drive == machine.inserting else 2)
    m.write(pb + csParam, struct.pack(
        ">hbbbbIhhhhbbh", 0,            # track
        0,                              # writeProt: no
        in_place,                       # diskInPlace
        1,                              # installed
        -1,                             # sides: a double-sided drive
        0, 0, drive, SONY, 0,           # the drive queue element: qLink, qType, drive, driver, FS
        two_sided, 0, 0))               # twoSideFmt, needsFlush, diskErrs


# =========================================================================
# A disk out of its drive.  Eject leaves the drive empty for a second, as
# long as someone takes to put the disk back, and then it's back: the
# drive posts a disk-inserted event, the drive in its message, and the
# next MountVol for the drive, the one GetNextEvent calls, mounts it
# again.  Seven Cities of Gold ejects its disk and waits for one so,
# polling GetOSEvent.  Only the driver's own calls find the drive empty;
# the File Manager reads the disk's files still.  A disk whose volume was
# unmounted as well can come back as another of the disk images: a game
# on several disks asks for each so (Wizardry VI).  The user says which
# goes in, or with no one to ask it's the next in turn.
# =========================================================================
def eject(t, vol) -> None:
    t.system.files.flush_volume(vol)
    if vol.floppy:
        t.machine.ejected[vol.drive] = tick_count()


def put_back(t) -> None:
    """Put back each disk that has been out a second: the event for it
    goes to the program running."""
    machine = t.machine
    if not machine.ejected:
        return
    now = tick_count()
    for drive, when in list(machine.ejected.items()):
        if now - when >= OUT_FOR and _disk_to_put_in(t, drive):
            del machine.ejected[drive]
            machine.put_back.add(drive)
            where, mods = t.process.events.input_state()
            t.process.events.post(EventRecord(diskEvt, drive, 0, where, mods))


def _disk_to_put_in(t, drive: int) -> bool:
    """Whether there's a disk to put back in the drive: the one taken out,
    or another the user has chosen."""
    files, asking = t.system.files, t.machine.choosing
    if not files.changing(drive):
        return True
    from .glue_stdfile import display_of
    display = display_of(t)
    if display is None:
        files.change_disk(drive)
        return True
    if drive not in asking:             # the disks as the user is shown them
        floppies = files.floppies()
        asking[drive] = Future(), floppies
        display.bridge.post(diskdlg.ask, display, drive, [_disk_name(v) for v in floppies],
                            floppies.index(files.next_disk(drive)), asking[drive][0])
    future, floppies = asking[drive]
    if not future.done():
        return False
    del asking[drive]
    choice = future.result()
    if choice is not None and 0 <= choice < len(floppies):
        files.change_disk(drive, floppies[choice])
    return True


def _disk_name(vol) -> str:
    path = vol.store.source
    return f"{vol.name}  ({path.name})" if isinstance(path, Path) else vol.name
