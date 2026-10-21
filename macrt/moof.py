"""
Applesauce MOOF images of Mac floppies: each track as the bits on the
disk, read back here as a Mac's floppy drive would, to the sectors a raw
image holds.

A 400K or 800K disk is written in GCR: the drive writes only bytes with
no more than two zero bits in a row, so six bits of the sector go in each
of them.  Each sector has a header, its track, side and number, and a
data field: its 12-byte tag and its 512 bytes, three bytes to four of the
disk's, mixed with a checksum that runs through them (Inside Macintosh
III, "The Disk Driver"; Guide to the Macintosh Family Hardware, "Disk
Formats").  The tracks hold fewer sectors further in, 12 on the outer 16
down to 8 on the inner 16.  A sector that can't be read is left as zeros,
as a disk copier would leave it, and why it can't is kept: the disk
driver says so to a program reading it.  The tags are kept beside the
sectors: the File Manager wrote the file and block each sector is of in
them, and copy protection reads them back through the disk driver.  So is
what's on each track, for programs that read the disk's bytes themselves.
Where a track goes long with no flux change the drive turns its gain up
until it reads noise, so a program that reads the bytes there reads
different ones each time; copy protection counts on that, and a copy
that reads the same each time is found out.  Long is longer than the
head sees at once, about 15 microseconds of the disk going by, seven of
a Mac disk's bit cells (the Applesauce WOZ reference, of the Apple II's
drive, whose cells are twice as long).  Fewer zeros together read as
they were written, though a disk's bytes have no more than two: The
Ancient Art of War's and Strategic Conquest's key tracks have bytes four
zeros apart, so that a turn of them is fewer bytes than a copy's, and
they count them.  A track the image doesn't
have, one never written, is all noise, as The Print Shop's disk's last
track is.

A track can be kept instead as the flux changes the drive's head sees,
how long after each other they go by (the FLUX chunk), where bits
can't say what's there: Fraction Action's, MathTalk's and KidTalk's key
tracks have changes a bit cell and a half apart.  The IWM shifts a one
in at each change and a zero at each bit cell and a half that goes by
without one, then each cell after, so such a change reads as one cell
after the last or two as it's read a little early or late, which it is
by a few hundredths of a cell each time round (the Applesauce WOZ
reference; the IWM's specification): different bits each time, from
the same disk, where a copy has the same ones.

The image is chunks, as the Applesauce reference has it: INFO (the kind
of disk), TMAP (which of TRKS's tracks each side of each track is) and
TRKS, each track's bits, first bit in each byte its top one; and FLUX,
which of them are flux changes instead, each byte the 125 ns ticks
since the last change, 255 a byte to add to the next.
"""

from __future__ import annotations

import random
import re
import struct
import zlib

MAGIC = b"MOOF\xff\n\r\n"
SSDD_GCR, DSDD_GCR = 1, 2               # INFO's disk types: 400K and 800K
TRACKS = 80
RPM = (394, 429, 472, 525, 590)         # how fast a disk turns, each 16 tracks, outer first
BIT_TIME = 16 / 7_833_600               # a bit cell: 16 of the IWM's clock's cycles, in seconds
SECTOR, TAG = 512, 12
JITTER = 0.03                           # how early or late a flux change is read, in bit cells

# The 64 bytes a GCR disk holds, in the order of the six bits each stands
# for: a top bit set, two ones together somewhere after it, never two
# pairs of zeros; $AA and $D5 are kept for the marks.
GCR = [b for b in range(0x96, 0x100)
       if "11" in format(b, "08b")[1:]
       and sum(format(b, "08b")[i:i + 2] == "00" for i in range(7)) <= 1
       and b not in (0xAA, 0xD5)]
SIX = {b: i for i, b in enumerate(GCR)}
HEADER, DATA = b"\xd5\xaa\x96", b"\xd5\xaa\xad"
GROUPS = (TAG + SECTOR + 2) // 3            # three bytes to each; the last has two
NIBBLES = GROUPS * 4 - 1 + 4                # and the checksum's four after


def is_moof(data: bytes) -> bool:
    return data[:8] == MAGIC


def sectors_on(track: int) -> int:
    return 12 - track // 16


def read(data: bytes) -> bytes:
    """The disk's sectors in order, as a raw image of it has them."""
    return Disk(data).image


class Disk:
    """A MOOF image's disk, read as the drive reads it: its sectors, their
    tags, why those that can't be read can't, and what's on each track."""

    def __init__(self, data: bytes):
        if len(data) < 12 or not is_moof(data):
            raise ValueError("it isn't a MOOF image")
        crc = struct.unpack_from("<I", data, 8)[0]
        if crc and zlib.crc32(data[12:]) != crc:
            raise ValueError("the MOOF image is damaged (its checksum doesn't match)")
        chunks = _chunks(data)
        info, tmap, trks = chunks.get(b"INFO"), chunks.get(b"TMAP"), chunks.get(b"TRKS")
        if info is None or tmap is None or trks is None or len(tmap) < 2 * TRACKS:
            raise ValueError("the MOOF image is damaged (it's missing its tracks)")
        if info[1] not in (SSDD_GCR, DSDD_GCR):
            raise ValueError("macrt reads MOOF images of 400K and 800K disks, not of 1.4 MB "
                             "ones: convert it to a raw image first")
        self.sides = 1 if info[1] == SSDD_GCR else 2
        self._data, self._tmap, self._trks = data, tmap, trks
        self._turns: dict[tuple[int, int], bytes] = {}
        self._framed: dict[tuple[int, int], tuple[bytes, list[int], int]] = {}
        self._quiet: set[tuple[int, int]] = set()   # the tracks that go long with no flux change
        self._flux = _flux_tracks(data, info, chunks.get(b"FLUX"), trks)
        self.random = random.Random()           # the noise the drive reads
        image, tags = bytearray(), bytearray()
        self.unreadable: dict[int, str] = {}    # by sector: NO_ADDRESS, NO_DATA or BAD_DATA
        for track in range(TRACKS):
            for side in range(self.sides):
                found, why = _sectors(self._turn(track, side), track, side)
                for s in range(sectors_on(track)):
                    sector = found.get(s)
                    if sector is None:
                        self.unreadable[len(image) // SECTOR] = why.get(s, NO_ADDRESS)
                        sector = bytes(TAG + SECTOR)
                    tags += sector[:TAG]
                    image += sector[TAG:]
        self.image, self.tags = bytes(image), bytes(tags)

    def place(self, sector: int) -> tuple[int, int, int]:
        """The track and side a sector of the disk is on, and its number there."""
        for track in range(TRACKS):
            here = sectors_on(track)
            if sector < here * self.sides:
                return track, sector // here, sector % here
            sector -= here * self.sides
        raise IndexError("past the end of the disk")

    def address_end(self, sector: int) -> int | None:
        """Where the sector's address field ends on its track, with the
        two bit-slip marks that end it, in bits from the index as the
        drive reads the track this time (framed): None if no address
        field on its track says it's that sector."""
        track, side, number = self.place(sector)
        out, starts, _ = self.framed(track, side)
        at = _address(out, track, side, number)
        if at < 0:
            return None
        return starts[min(at + len(HEADER) + 5 + 1, len(starts) - 1)] + 8

    def framed(self, track: int, side: int) -> tuple[bytes, list[int], int]:
        """What the drive reads from one side of a track twice round, as
        it reads it this time; where each byte of that begins, in bits
        from the index; and how many bits go round once."""
        if (track, side) in self._flux:
            bits = self._flux[track, side].read(self.random)
            if (track, side) in self._quiet:
                return (*_framed(_noisy(bits + bits, self.random)), len(bits))
            return (*_framed(bits + bits), len(bits))
        if (track, side) not in self._framed:
            bits = self._track_bits(track, side)
            self._framed[track, side] = (*_framed(bits + bits), len(bits))
        out, starts, count = self._framed[track, side]
        if (track, side) in self._quiet:
            bits = self._track_bits(track, side)
            out, starts = _framed(_noisy(bits + bits, self.random))
        return out, starts, count

    def bits(self, track: int, side: int) -> str:
        """The bits that go by under the head on one side of a track once
        round, from the index, as the drive reads them this time: noise
        where there's been no flux change for too long."""
        flux = self._flux.get((track, side))
        bits = flux.read(self.random) if flux else self._track_bits(track, side)
        return _noisy(bits, self.random) if (track, side) in self._quiet else bits

    def _turn(self, track: int, side: int) -> bytes:
        """What the drive reads from one side of a track: twice round, for a
        sector across the index; nothing where there's no flux change."""
        if (track, side) not in self._turns:
            bits = self._track_bits(track, side)
            self._turns[track, side] = _bytes(bits + bits)
            if QUIET in bits:
                self._quiet.add((track, side))
        return self._turns[track, side]

    def _track_bits(self, track: int, side: int) -> str:
        if (track, side) in self._flux:     # as the drive reads it with no change early or late
            return self._flux[track, side].bits
        t = self._tmap[track * 2 + side]
        if t == 0xFF:                   # never written: as many bits go by, with no flux change
            return "0" * round(60 / RPM[track // 16] / BIT_TIME)
        return _read_bits(self._data, self._trks, t)


NO_ADDRESS = "no address field"         # why a sector can't be read
NO_DATA = "no data field"
BAD_DATA = "a damaged data field"


def _chunks(data: bytes) -> dict[bytes, bytes]:
    out, at = {}, 12
    while at + 8 <= len(data):
        name, size = data[at:at + 4], struct.unpack_from("<I", data, at + 4)[0]
        out.setdefault(name, data[at + 8:at + 8 + size])
        at += 8 + size
    return out


def _read_bits(data: bytes, trks: bytes, t: int) -> str:
    """One turn of a track's bits, "0" and "1"."""
    if t * 8 + 8 > len(trks):
        return ""
    block, blocks, count = struct.unpack_from("<HHI", trks, t * 8)
    raw = data[block * 512:(block + blocks) * 512]
    count = min(count, len(raw) * 8)
    if not count:
        return ""
    return bin(int.from_bytes(raw, "big") | 1 << len(raw) * 8)[3:count + 3]


def _flux_tracks(data: bytes, info: bytes, fmap: bytes | None, trks: bytes) \
        -> dict[tuple[int, int], _Flux]:
    """The sides of tracks the image keeps as flux changes, by track and
    side: those the FLUX chunk has, if INFO says where it is."""
    if fmap is None or len(info) < 44 or not info[0] or 0 in struct.unpack_from("<HH", info, 40):
        return {}
    out = {}
    for at, t in enumerate(fmap[:2 * TRACKS]):
        if t != 0xFF and t * 8 + 8 <= len(trks):
            block, _, count = struct.unpack_from("<HHI", trks, t * 8)
            gaps, ticks = [], 0
            for b in data[block * 512:block * 512 + count]:
                ticks += b
                if b != 255:
                    gaps.append(ticks)
                    ticks = 0
            if gaps:
                out[at // 2, at % 2] = _Flux(gaps, round(60 / RPM[at // 32] / BIT_TIME))
    return out


class _Flux:
    """One side of a track kept as flux changes, as the drive reads it: the
    changes how far apart they go by in a Mac's drive, as many bit cells
    round as a turn of its zone takes, wherever the drive that read them
    turned at."""

    def __init__(self, gaps: list[int], cells: int):
        total = sum(gaps)
        self._gaps = [g * cells / total for g in gaps]      # in bit cells
        self._cells = [_cells(g) for g in self._gaps]
        self.bits = "".join(self._cells)
        # The changes read early or late enough to read differently
        self._weak = [i for i, g in enumerate(self._gaps) if abs(g % 1 - 0.5) < 10 * JITTER]

    def read(self, rng: random.Random) -> str:
        """The bits as the drive reads them once, each change a little
        early or late."""
        if not self._weak:
            return self.bits
        cells, late = self._cells[:], {}
        for i in self._weak:
            for j in (i - 1, i):
                if j not in late:
                    late[j] = rng.gauss(0, JITTER)
            cells[i] = _cells(self._gaps[i] + late[i] - late[i - 1])
        return "".join(cells)


def _cells(gap: float) -> str:
    """The bits the IWM shifts in for a flux change gap bit cells after the
    last: a zero each cell and a half without one, then each cell after,
    and a one for it."""
    return "0" * (max(1, int(gap + 0.5)) - 1) + "1"


HEAD_WINDOW = 15e-6      # seconds of the disk the head reads over at once
HEARD = int(HEAD_WINDOW / BIT_TIME)     # zeros together that still read as zeros: 7
QUIET = "0" * (HEARD + 1)


def _noisy(bits: str, rng: random.Random) -> str:
    """The bits as the drive reads them once: past HEARD zeros, a bit
    cell's noise is a one about three times in ten, as the Applesauce
    reference has the MC3470 amplifier's."""
    return re.sub(QUIET + "+", lambda run: "0" * HEARD + "".join(
        "1" if rng.random() < 0.3 else "0" for _ in range(len(run.group()) - HEARD)), bits)


def _address(disk: bytes, track: int, side: int, number: int) -> int:
    """Where the address field of sector number of the track is, -1 if
    there's none."""
    at = disk.find(HEADER)
    while 0 <= at and at + 9 <= len(disk):
        if _header(disk, at) == (track, side, number):
            return at
        at = disk.find(HEADER, at + 3)
    return -1


def _header(disk: bytes, at: int) -> tuple[int, int, int] | None:
    """The track, side and sector number of the address field at at, None
    if it doesn't check."""
    header = [SIX.get(b, -1) for b in disk[at + 3:at + 8]]
    if len(header) < 5 or -1 in header or header[0] ^ header[1] ^ header[2] ^ header[3] \
            != header[4]:
        return None
    low, number, high = header[:3]
    return low | (high & 0x1F) << 6, high >> 5 & 1, number


def _sectors(disk: bytes, track: int, side: int) -> tuple[dict[int, bytes], dict[int, str]]:
    """The sectors read from one side of one track, by number, each its
    tag and its data; and why those whose address field is there can't be
    read."""
    found: dict[int, bytes] = {}
    why: dict[int, str] = {}
    at = disk.find(HEADER)
    while 0 <= at and at + 9 <= len(disk):
        header = _header(disk, at)
        at, here = disk.find(HEADER, at + 3), at + 8
        if header is None or header[:2] != (track, side) or header[2] in found:
            continue                    # a header copied from elsewhere, as copy protection has
        number = header[2]
        if number >= sectors_on(track):
            continue
        mark = disk.find(DATA, here, here + 64)
        sector = data_field(disk[mark + 4:mark + 4 + NIBBLES]) if mark >= 0 else None
        if sector is not None:
            found[number] = sector
            why.pop(number, None)
        else:
            why[number] = NO_DATA if mark < 0 else BAD_DATA
    return found, why


def _bytes(bits: str) -> bytes:
    """What the drive reads from the bits: each byte begins at a one, the
    zeros that pad between them for timing skipped."""
    out = bytearray()
    at, last = bits.find("1"), len(bits) - 8
    while 0 <= at <= last:
        out.append(int(bits[at:at + 8], 2))
        at = bits.find("1", at + 8)
    return bytes(out)


def _framed(bits: str) -> tuple[bytes, list[int]]:
    """What the drive reads from the bits, as _bytes has it, and the bit
    each byte begins at."""
    out, starts = bytearray(), []
    at, last = bits.find("1"), len(bits) - 8
    while 0 <= at <= last:
        out.append(int(bits[at:at + 8], 2))
        starts.append(at)
        at = bits.find("1", at + 8)
    return bytes(out), starts


def data_field(disk: bytes) -> bytes | None:
    """The tag and the 512 bytes of a data field, from what follows its
    marks and the sector number after them, or None if it doesn't check.
    Each group of three bytes is written as the top two bits of each, then
    each one's low six; each byte is mixed with one of three sums the
    bytes before it are added into in turn, the first of them turned a bit
    to the left at each group."""
    if len(disk) < NIBBLES:
        return None
    six = [SIX.get(b, -1) for b in disk]
    if -1 in six:
        return None
    a = b = c = 0
    out = bytearray()
    at = 0
    for group in range(GROUPS):
        tops, x, y = six[at], six[at + 1], six[at + 2]
        z = six[at + 3] if group < GROUPS - 1 else 0
        at += 4 if group < GROUPS - 1 else 3
        c = c << 1 | c >> 7
        carry, c = c & 1, c & 0xFF
        x = (tops << 2 & 0xC0 | x) ^ c
        a += x + carry
        carry, a = a >> 8, a & 0xFF
        y = (tops << 4 & 0xC0 | y) ^ a
        b += y + carry
        carry, b = b >> 8, b & 0xFF
        out += bytes((x, y))
        if group == GROUPS - 1:
            break
        z = (tops << 6 & 0xC0 | z) ^ b
        c = (c + z + carry) & 0xFF
        out.append(z)
    tops, x, y, z = six[at:at + 4]
    if (tops << 2 & 0xC0 | x, tops << 4 & 0xC0 | y, tops << 6 & 0xC0 | z) != (a, b, c):
        return None
    return bytes(out)
