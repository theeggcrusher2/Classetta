"""
Mac files on the host: both forks plus the Finder's type, creator and
flags, from whichever form they were kept in.

    MacBinary I/II/III      app.bin
    BinHex 4.0              app.hqx
    AppleSingle             one file, both forks
    AppleDouble             the data fork, with ._app, .AppleDouble/app or
                            app.rsrc (what The Unarchiver's unar writes)
    Basilisk II             app, with .rsrc/app and .finf/app
    Executor                app, with %app
    a bare resource fork    app.rsrc, or a file that is one
    macOS                   app/..namedfork/rsrc
    HFS disk images         raw or DiskCopy 4.2, through machfs
    MFS disk images         the 400K floppies of System 1 to 4, read here
    MOOF disk images        Applesauce's, of a floppy's tracks: macrt.moof

A Folder finds files by Mac name, case-insensitively, the way the File
Manager will: OpenResFile looks up names through the folder the
application came from.

open_application() is what the command line uses: any of the above, or
an application on a disk image; open_disks() several of them, and the
disk images among them, to be put in the drives.
"""

from __future__ import annotations

import binascii
import os
import struct
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterator, Protocol

import machfs
from machfs.btree import dump_btree

from . import moof
from .resfork import InvalidForkError, is_fork, open_fork

APPLE_SINGLE = 0x00051600
APPLE_DOUBLE = 0x00051607
AS_DATA, AS_RSRC, AS_NAME, AS_FINDER_INFO = 1, 2, 3, 9

BINHEX_BANNER = b"(This file must be converted with BinHex"
BINHEX_CHARS = b"!\"#$%&'()*+,-012345689@ABCDEFGHIJKLMNPQRSTUVXYZ[`abcdefhijklmpqr"
BINHEX_RUN = 0x90

DISKCOPY_HEADER = 84
APM_SIGNATURE = b"ER"                   # block 0 of a partitioned disk
APM_ENTRY = b"PM"                       # each partition map entry
HFS_SIGNATURE = b"BD"
MFS_SIGNATURE = b"\xd2\xd7"
SECTOR, TAG = 512, 12                   # a floppy sector, and the tag the drive keeps with it


@dataclass(eq=False)
class MacFile:
    name: str
    data: bytes = b""
    rsrc: bytes = b""
    type: bytes = b"????"
    creator: bytes = b"????"
    flags: int = 0                      # Finder flags
    key: str = ""                       # identity: a host path or volume path
    folder: Folder | None = field(default=None, repr=False)

    def __post_init__(self):
        if not self.key:
            self.key = self.name

    @property
    def is_application(self) -> bool:
        return self.type == b"APPL"


class Folder(Protocol):
    def find(self, name: str) -> MacFile | None: ...

    def files(self) -> list[MacFile]: ...


def _split_path(name: str) -> tuple[bool, list[str]]:
    """A Mac pathname: (absolute, components).  "Disk:Folder:File" is
    absolute, ":Folder:File" and "File" are relative."""
    if ":" not in name:
        return False, [name]
    absolute = not name.startswith(":")
    parts = [p for p in name.split(":") if p]
    return absolute, parts[1:] if absolute else parts


# ---------------------------------------------------------------------------
# Folders
# ---------------------------------------------------------------------------
class MemoryFolder:
    """Files held in Python; handy for tests and Python guests."""

    def __init__(self, files: list[MacFile] = ()):
        self._files: dict[str, MacFile] = {}
        for f in files:
            self.add(f)

    def add(self, f: MacFile) -> MacFile:
        self._files[f.name.lower()] = f
        f.folder = self
        return f

    def remove(self, f: MacFile) -> None:
        if self._files.get(f.name.lower()) is f:
            del self._files[f.name.lower()]

    def find(self, name: str) -> MacFile | None:
        parts = _split_path(name)[1]
        return self._files.get(parts[-1].lower()) if parts else None

    def files(self) -> list[MacFile]:
        return list(self._files.values())


class HostFolder:
    """A host directory.  Each Mac file may be spread over several host
    files; find() puts it back together."""

    def __init__(self, path: str | os.PathLike, root: HostFolder | None = None):
        self.path = Path(path)
        self.root = root or self

    def _entry(self, name: str) -> Path | None:
        try:
            names = os.listdir(self.path)
        except OSError:
            return None
        want = name.lower()
        hit = next((n for n in names if n == name), None) or \
            next((n for n in names if n.lower() == want), None)
        return self.path / hit if hit else None

    def find(self, name: str) -> MacFile | None:
        absolute, parts = _split_path(name)
        if not parts:
            return None
        folder = self.root if absolute else self
        for part in parts[:-1]:
            sub = folder._entry(part)
            if sub is None or not sub.is_dir():
                return None
            folder = HostFolder(sub, self.root)
        leaf = parts[-1]
        for candidate in (leaf, leaf + ".bin", leaf + ".rsrc"):
            p = folder._entry(candidate)
            if p is not None and p.is_file():
                return load_file(p, folder)
        # Only the resource fork is here, in one of the side files.
        for side in ("._" + leaf, "%" + leaf):
            p = folder._entry(side)
            if p is not None and p.is_file():
                return load_file(p, folder)
        return None

    def files(self) -> list[MacFile]:
        names = sorted(os.listdir(self.path))
        out = []
        for n in names:
            p = self.path / n
            if n.startswith((".", "%")) or not p.is_file():
                continue
            if n.lower().endswith(".rsrc") and n[:-5] in names:
                continue                                # app's resource fork, beside app
            try:
                out.append(load_file(p, self))
            except ValueError:
                continue                                # damaged; the rest are still here
        return out


class VolumeFolder:
    """A folder on an HFS volume read by machfs."""

    def __init__(self, volume: machfs.Volume, path: tuple[str, ...] = ()):
        self.volume = volume
        self.path = path

    def _node(self, path: tuple[str, ...]):
        node = self.volume
        for part in path:
            node = node[part]
        return node

    @staticmethod
    def _child(folder, name: str) -> tuple[str, object]:
        """A folder's item and its name as the volume spells it, which is
        what keys and paths use whatever case it was asked for in."""
        node = folder[name]
        return next((n for n in folder if n.lower() == name.lower()), name), node

    def find(self, name: str) -> MacFile | None:
        absolute, parts = _split_path(name)
        if not parts:
            return None
        path = () if absolute else self.path
        try:
            folder = self._node(path)
            for part in parts[:-1]:
                stored, folder = self._child(folder, part)
                path += (stored,)
            stored, node = self._child(folder, parts[-1])
        except (KeyError, TypeError):
            return None
        if not isinstance(node, machfs.File):
            return None
        return _from_machfs(self.volume, VolumeFolder(self.volume, path), stored, node)

    def files(self) -> list[MacFile]:
        folder = self._node(self.path)
        return [_from_machfs(self.volume, self, n, f) for n, f in folder.items()
                if isinstance(f, machfs.File)]


def _from_machfs(volume: machfs.Volume, folder: VolumeFolder, name: str,
                 f: machfs.File) -> MacFile:
    key = ":".join((volume.name, *folder.path, name))
    return MacFile(name, bytes(f.data), bytes(f.rsrc), bytes(f.type), bytes(f.creator),
                   f.flags, key, folder)


# ---------------------------------------------------------------------------
# Disk images
# ---------------------------------------------------------------------------
@dataclass(eq=False)
class Disk:
    """A disk image as its driver reads it, by sector rather than by file:
    each sector's 512 bytes, its 12-byte tag if the image kept them (none
    if it didn't), and a MOOF image's tracks."""
    sectors: bytearray
    tags: bytearray
    tracks: moof.Disk | None = None

    def unreadable(self, sector: int) -> str | None:
        """Why the sector can't be read, None if it can."""
        return self.tracks.unreadable.get(sector) if self.tracks else None


def _sectors(data: bytes) -> tuple[bytes, bytes]:
    """The sectors a disk image holds, and their tags if it kept them,
    else b"": a DiskCopy 4.2 image has them after its sectors, and some
    raw images keep each with its sector, in 524-byte records."""
    if len(data) > DISKCOPY_HEADER and data[82:84] == b"\x01\x00" and data[0] < 64:
        size, tag_size = struct.unpack_from(">II", data, 64)
        if DISKCOPY_HEADER + size <= len(data):
            tags = data[DISKCOPY_HEADER + size:DISKCOPY_HEADER + size + tag_size]
            data = data[DISKCOPY_HEADER:DISKCOPY_HEADER + size]
            return data, tags if len(tags) == len(data) // SECTOR * TAG else b""
    record = SECTOR + TAG
    if not data or len(data) % record or _volume_start(data) is not None:
        return data, b""
    for skip in (0, TAG):                   # the tag after its sector, or before it
        if data[2 * record + skip:2 * record + skip + 2] in (HFS_SIGNATURE, MFS_SIGNATURE):
            tag = 0 if skip else SECTOR
            return (b"".join(data[i + skip:i + skip + SECTOR] for i in range(0, len(data), record)),
                    b"".join(data[i + tag:i + tag + TAG] for i in range(0, len(data), record)))
    return data, b""


def _hfs_partition(data: bytes) -> bytes | None:
    """The first HFS partition in an Apple partition map (a hard disk or
    CD-ROM image), if data starts with one."""
    if data[:2] != APM_SIGNATURE or len(data) < 1024:
        return None
    for size in dict.fromkeys((struct.unpack_from(">H", data, 2)[0], 512)):
        if size < 512 or data[size:size + 2] != APM_ENTRY:
            continue
        count = struct.unpack_from(">I", data, size + 4)[0]
        for i in range(1, min(count, 256) + 1):
            entry = data[i * size:i * size + 80]
            if entry[:2] != APM_ENTRY or len(entry) < 80:
                break
            start, blocks = struct.unpack_from(">II", entry, 8)
            part = data[start * size:(start + blocks) * size]
            if entry[48:80].split(b"\0")[0] == b"Apple_HFS" and part[1024:1026] == HFS_SIGNATURE:
                return part
    return None


def _volume_start(data: bytes) -> tuple[int, bytes] | None:
    """Where the volume starts (its boot blocks, two before the MDB) and
    its signature."""
    for i in range(0, min(len(data), 64 * 512), 512):
        if data[i + 1024:i + 1026] in (HFS_SIGNATURE, MFS_SIGNATURE):
            return i, data[i + 1024:i + 1026]
    return None


def _read_mfs(data: bytes, first: int = 0) -> machfs.Volume:
    """An MFS volume as a machfs one, so the rest of macrt can't tell.

    MFS (Inside Macintosh II, "Data Organization on Volumes") has one flat
    directory: the folders a user sees exist only in the Finder's Desktop
    file, and the File Manager never saw them either, so every file goes at
    the top.  Forks are chains of allocation blocks, linked by the 12-bit
    entries of the block map that follows the MDB.  Each file's sectors
    are kept, its data fork's and its resource fork's, as numbers on the
    disk, the volume starting at sector first: the File Manager reads a
    fork through the disk driver, a sector at a time."""
    mdb = 1024
    if len(data) < mdb + 64:
        raise ValueError("the disk image is damaged (it ends inside the MDB)")
    (crdate, bkdate, _atrb, files, dir_start, dir_blocks, blocks, block_size, _clump,
     first_block) = struct.unpack_from(">IIHHHHHIIH", data, mdb + 2)
    if not block_size or block_size % 512 or not blocks:
        raise ValueError("the disk image is damaged (its MDB makes no sense)")
    vol = machfs.Volume()
    vol.name = data[mdb + 37:mdb + 37 + min(data[mdb + 36], 27)].decode("mac_roman")
    vol.crdate, vol.bkdate = crdate, bkdate

    def link(n: int) -> int:                # entry n of the block map; n counts from 2
        i = n - 2
        at = mdb + 64 + i * 3 // 2
        if at + 2 > len(data):
            return 1
        if i % 2 == 0:
            return data[at] << 4 | data[at + 1] >> 4
        return (data[at] & 0xF) << 8 | data[at + 1]

    def fork(start: int, length: int) -> tuple[bytearray, list[int]]:
        out, sectors = bytearray(), []
        n, seen = start, 0
        while 2 <= n < blocks + 2 and len(out) < length and seen <= blocks:
            at = (first_block * 512) + (n - 2) * block_size
            out += data[at:at + block_size]
            sectors += range(first + at // 512, first + (at + block_size) // 512)
            n, seen = link(n), seen + 1
        if len(out) < length:
            raise ValueError("the disk image is damaged (a file runs off its blocks)")
        return out[:length], sectors[:-(-length // 512)]

    for b in range(dir_start, dir_start + dir_blocks):
        at, end = b * 512, b * 512 + 512
        while at + 51 <= min(end, len(data)) and data[at] & 0x80:
            e = data[at:at + 51 + data[at + 50]]
            if at + len(e) > end:
                break
            f = machfs.File()
            f.type, f.creator = bytes(e[2:6]), bytes(e[6:10])
            f.flags, v, h = struct.unpack_from(">Hhh", e, 10)
            f.y, f.x = v, h
            f.locked = bool(e[0] & 1)
            f.attributes = e[0] & 0x7E      # bit 7 only says the entry is in use
            dstart, dlen, _, rstart, rlen, _, f.crdate, f.mddate = \
                struct.unpack_from(">HII" "HII" "II", e, 22)
            (f.data, data_sectors), (f.rsrc, rsrc_sectors) = fork(dstart, dlen), fork(rstart, rlen)
            f.sectors = data_sectors, rsrc_sectors
            name = e[51:].decode("mac_roman")
            if name and ":" not in name:
                vol[name] = f
            at += (len(e) + 1) & ~1
    if files and not len(vol):
        raise ValueError("the disk image is damaged (its directory is empty)")
    return vol


def _startup_name(boot: bytes) -> str | None:
    """The program a Mac starting up from the volume runs, named in its
    boot blocks (bbHelloName): the Finder, unless something took its place."""
    if boot[:2] != b"LK" or len(boot) < 106:
        return None
    return boot[91:91 + min(boot[90], 15)].decode("mac_roman") or None


def is_disk_image(data: bytes) -> bool:
    if moof.is_moof(data):
        return True
    data = _sectors(data)[0]
    return _hfs_partition(data) is not None or _volume_start(data) is not None


def load_volume(source: str | os.PathLike | bytes) -> machfs.Volume:
    """The volume on a disk image; its disk, for the disk driver, is its
    .disk."""
    data = source if isinstance(source, (bytes, bytearray)) else Path(source).read_bytes()
    if moof.is_moof(data):
        tracks = moof.Disk(bytes(data))
        disk = Disk(bytearray(tracks.image), bytearray(tracks.tags), tracks)
    else:
        disk = Disk(*map(bytearray, _sectors(bytes(data))))
    data = bytes(disk.sectors)
    data = _hfs_partition(data) or data
    start = _volume_start(data)
    startup = _startup_name(data[start[0]:start[0] + 1024]) if start else None
    if start and start[1] == MFS_SIGNATURE:
        vol = _read_mfs(data[start[0]:], start[0] // SECTOR)
        vol.size, vol.startup, vol.disk = len(data), startup, disk
        return vol
    vol = machfs.Volume()
    vol.size = len(data)                # the disk's: whether it's a floppy's
    vol.startup, vol.disk = startup, disk
    try:
        vol.read(data)
    except Exception as e:      # noqa: BLE001 - machfs raises whatever it trips over
        raise ValueError(f"the disk image is damaged ({type(e).__name__}: {e})") from None
    # machfs doesn't keep the volume's name; it's drVN in the MDB.
    mdb = next((i + 1024 for i in range(0, len(data), 512)
                if data[i + 1024:i + 1026] == HFS_SIGNATURE), None)
    vol.name = (data[mdb + 37:mdb + 37 + min(data[mdb + 36], 27)].decode("mac_roman")
                if mdb is not None else "Untitled")
    if mdb is not None:
        _read_file_flags(data[mdb - 1024:], vol)
    return vol


def _read_file_flags(data: bytes, vol: machfs.Volume) -> None:
    """Each file's flags from its catalog record (filFlags), which machfs
    reads and drops, kept as the MFS reader keeps its directory entry's:
    bit 0 is locked, and the File Manager reports the rest in ioFlAttrib
    all the same.  Microsoft File's master disk sets bit 6 on two files,
    and File looks for it there."""
    mdb = 1024
    block_size, = struct.unpack_from(">I", data, mdb + 20)
    first, = struct.unpack_from(">H", data, mdb + 28)
    overflow: dict[tuple[int, int], bytes] = {}

    def fork(at: int, cnid: int) -> bytes:     # the file's first extents at, then overflow's
        size, extents = struct.unpack_from(">I12s", data, mdb + at)
        out, blocks = bytearray(), 0
        while True:
            for start, count in struct.iter_unpack(">HH", extents):
                out += data[first * 512 + start * block_size:][:count * block_size]
                blocks += count
            if len(out) >= size or (cnid, blocks) not in overflow:
                return bytes(out[:size])
            extents = overflow[cnid, blocks]

    try:
        for rec in dump_btree(fork(130, 3)):                # the extents overflow file
            kind, cnid, start, extents = struct.unpack_from(">xBLH12s", rec)
            if rec[0] == 7 and kind == 0:               # a data fork's
                overflow[cnid, start] = extents
        folders, files = {}, []
        for rec in dump_btree(fork(146, 4)):                # the catalog
            parent, length = struct.unpack_from(">LB", rec, 2)
            name, value = rec[7:7 + length], rec[(rec[0] + 2) & ~1:]
            if value[:1] == b"\x01":
                folders[struct.unpack_from(">L", value, 6)[0]] = parent, name
            elif value[:1] == b"\x02":
                files.append((parent, name, value[2]))
    except (struct.error, IndexError, KeyError):
        return

    def folder(cnid: int):
        if cnid == 2:                           # the root's
            return vol
        parent, name = folders[cnid]
        return folder(parent)[name]

    for parent, name, flags in files:
        try:
            f = folder(parent)[name]
        except (KeyError, RecursionError):
            continue
        f.locked, f.attributes = bool(flags & 1), flags & 0x7E


def volume_files(volume: machfs.Volume) -> Iterator[MacFile]:
    """Every file on the volume, folders included."""
    for path, node in volume.iter_paths():
        if isinstance(node, machfs.File):
            yield _from_machfs(volume, VolumeFolder(volume, path[:-1]), path[-1], node)


# ---------------------------------------------------------------------------
# Single files
# ---------------------------------------------------------------------------
def _parse_macbinary(data: bytes) -> MacFile | None:
    if len(data) < 128 or data[0] != 0 or data[74] != 0 or not 1 <= data[1] <= 63:
        return None
    if data[:3] == b"\x00\x05\x16":     # AppleSingle/Double can pass for MacBinary I
        return None
    h = data[:128]
    data_len, rsrc_len = struct.unpack_from(">II", h, 83)
    if max(data_len, rsrc_len) >= 0x1000000:
        return None
    crc_ok = binascii.crc_hqx(h[:124], 0) == struct.unpack_from(">H", h, 124)[0]
    if not crc_ok and (h[82] != 0 or any(h[99:126])):
        return None                                     # not MacBinary I either
    if not crc_ok and (b":" in h[2:2 + h[1]] or min(h[2:2 + h[1]]) < 0x20):
        return None                                     # no Mac name, so not MacBinary I
    sec_len = struct.unpack_from(">H", h, 120)[0] if crc_ok else 0
    pad = lambda n: (n + 127) & ~127                    # noqa: E731
    data_at = 128 + pad(sec_len)
    rsrc_at = data_at + pad(data_len)
    if rsrc_at + rsrc_len > len(data):
        if data_at + data_len + rsrc_len == len(data):
            rsrc_at = data_at + data_len                # the data fork wasn't padded
        elif crc_ok:
            raise ValueError("MacBinary file is cut short")
        else:
            return None
    if not crc_ok and len(data) > rsrc_at + pad(rsrc_len) + 1024:
        return None                                     # far too long for what it says it holds
    flags = h[73] << 8 | (h[101] if crc_ok else 0)
    name = h[2:2 + h[1]].decode("mac_roman")
    return MacFile(name, data[data_at:data_at + data_len], data[rsrc_at:rsrc_at + rsrc_len],
                   h[65:69], h[69:73], flags)


def _parse_binhex(data: bytes) -> MacFile | None:
    """A BinHex 4.0 file: six bits a character, then run-length encoding,
    then a header and the two forks, each with a CRC."""
    banner = data.find(BINHEX_BANNER, 0, 4096)
    if banner < 0:
        return None
    start = data.find(b":", banner)                     # the banner has no colon
    end = data.find(b":", start + 1)
    if start < 0 or end < 0:
        raise ValueError("BinHex file is cut short")
    table = {c: i for i, c in enumerate(BINHEX_CHARS)}
    packed, bits, nbits = bytearray(), 0, 0
    for c in data[start + 1:end]:
        if c in b" \t\r\n":
            continue
        if c not in table:
            raise ValueError(f"BinHex file has a stray {chr(c)!r}")
        bits, nbits = bits << 6 | table[c], nbits + 6
        if nbits >= 8:
            nbits -= 8
            packed.append(bits >> nbits & 0xFF)
            bits &= (1 << nbits) - 1                    # or it grows with the file
    out, i, last = bytearray(), 0, 0
    while i < len(packed):
        c = packed[i]
        i += 1
        if c != BINHEX_RUN:
            out.append(c)
            last = c
        elif i < len(packed):
            n = packed[i]
            i += 1
            if n == 0:
                out.append(BINHEX_RUN)
                last = BINHEX_RUN
            else:
                out += bytes([last]) * (n - 1)

    def take(at: int, n: int) -> tuple[bytes, int]:
        body = bytes(out[at:at + n])
        crc = struct.unpack_from(">H", out, at + n)[0] if at + n + 2 <= len(out) else None
        if len(body) < n or crc != binascii.crc_hqx(body, 0):
            raise ValueError("BinHex file is damaged (bad CRC)")
        return body, at + n + 2

    if not out:
        raise ValueError("BinHex file is empty")
    n = out[0]
    header, at = take(0, 1 + n + 1 + 4 + 4 + 2 + 4 + 4)
    name = header[1:1 + n].decode("mac_roman")
    ftype, creator = header[n + 2:n + 6], header[n + 6:n + 10]
    flags, data_len, rsrc_len = struct.unpack_from(">HII", header, n + 10)
    fork_data, at = take(at, data_len)
    fork_rsrc, at = take(at, rsrc_len)
    return MacFile(name, fork_data, fork_rsrc, ftype, creator, flags)


def _parse_apple_single(data: bytes) -> tuple[int, dict[int, bytes]] | None:
    if len(data) < 26:
        return None
    magic, = struct.unpack_from(">I", data, 0)
    if magic not in (APPLE_SINGLE, APPLE_DOUBLE):
        return None
    n, = struct.unpack_from(">H", data, 24)
    if 26 + 12 * n > len(data):
        raise ValueError("AppleSingle/AppleDouble file is cut short")
    entries = {}
    for i in range(n):
        eid, off, length = struct.unpack_from(">III", data, 26 + 12 * i)
        if off + length > len(data):
            raise ValueError("AppleSingle/AppleDouble file is cut short")
        entries[eid] = data[off:off + length]
    return magic, entries


def _apply_finder_info(f: MacFile, info: bytes) -> None:
    if len(info) >= 10:
        f.type, f.creator = info[0:4], info[4:8]
        f.flags = struct.unpack_from(">H", info, 8)[0]


def _host_finder_info(path: Path) -> bytes:
    try:
        return os.getxattr(path, "com.apple.FinderInfo")
    except (AttributeError, OSError):
        return b""


def _side_files(path: Path) -> list[tuple[Path, str | None]]:
    """Where path's resource fork may be, and in what form: None for a
    bare fork, "double" for AppleDouble, "either" for whichever it is."""
    name = path.name
    return [(path / "..namedfork" / "rsrc", None),
            (path.with_name("._" + name), "double"),
            (path.parent / ".AppleDouble" / name, "double"),
            (path.with_name(name + ".rsrc"), "either"),
            (path.parent / ".rsrc" / name, None),
            (path.with_name("%" + name), "either")]


def load_file(path: str | os.PathLike, folder: Folder | None = None) -> MacFile:
    """One Mac file from the host, whatever form it's in.  path may also
    name one of the side files (._app, %app, app.rsrc)."""
    path = Path(path)
    folder = folder or HostFolder(path.parent)
    name = path.name
    try:
        raw = path.read_bytes()
    except FileNotFoundError:
        # An empty data fork: unar writes only app.rsrc for most applications.
        if not any(side.is_file() for side, _ in _side_files(path)):
            raise
        raw = b""

    mb = _parse_macbinary(raw) or _parse_binhex(raw)
    if mb is not None:
        mb.key, mb.folder = str(path), folder
        return mb

    single = _parse_apple_single(raw)
    if single is not None:
        magic, entries = single
        if magic == APPLE_DOUBLE:                       # the header for a data file
            if name.startswith("._"):
                name = name[2:]
            elif name.startswith("%"):
                name = name[1:]
            elif name.lower().endswith(".rsrc"):
                name = name[:-5]
            data_path = path.with_name(name)
            data = (data_path.read_bytes() if data_path != path and data_path.is_file()
                    else b"")
            path = data_path
        else:
            data = entries.get(AS_DATA, b"")
        real = entries.get(AS_NAME)
        f = MacFile(real.decode("mac_roman") if real else name, data, entries.get(AS_RSRC, b""),
                    key=str(path), folder=folder)
        _apply_finder_info(f, entries.get(AS_FINDER_INFO, b""))
        return f

    if name.startswith("%"):                            # Executor's resource fork
        data_path = path.with_name(name[1:])
        data = data_path.read_bytes() if data_path.is_file() else b""
        return MacFile(name[1:], data, raw, key=str(data_path), folder=folder)

    f = MacFile(name, raw, key=str(path), folder=folder)
    for side_path, kind in _side_files(path):
        try:
            blob = side_path.read_bytes()
            parsed = _parse_apple_single(blob) if kind else None
        except (OSError, ValueError):
            continue                                    # missing, or a damaged side file
        if kind == "either":
            kind = "double" if parsed else None
        if kind == "double":
            if parsed is None:
                continue
            f.rsrc = parsed[1].get(AS_RSRC, b"")
            _apply_finder_info(f, parsed[1].get(AS_FINDER_INFO, b""))
        else:
            f.rsrc = blob
        if f.rsrc:
            break
    finf = path.parent / ".finf" / name                 # Basilisk II's Finder info
    if finf.is_file():
        _apply_finder_info(f, finf.read_bytes())
    elif f.type == b"????":
        _apply_finder_info(f, _host_finder_info(path))

    if not f.rsrc and name.lower().endswith(".rsrc") and is_fork(raw):
        f.name, f.data, f.rsrc = name[:-5], b"", raw    # a bare resource fork
    elif not f.rsrc and is_fork(raw):
        f.data, f.rsrc = b"", raw
    return f


# ---------------------------------------------------------------------------
# What the command line opens
# ---------------------------------------------------------------------------
def archive_kind(f: MacFile) -> str | None:
    """The kind of archive f is, if it's one: something to unpack first."""
    d = f.data
    if d[:4] == b"SIT!" and d[10:14] == b"rLau" or d[:16] == b"StuffIt (c)1997-":
        return "StuffIt"
    if d[:4] == b"PK\x03\x04":
        return "zip"
    return None


def open_application(path: str | os.PathLike, name: str | None = None) -> MacFile:
    """The Mac file at path, in any form load_file reads.  If path is a disk
    image, the application on it called name (or with that Mac path), or
    the only application on it."""
    volume, f = _volume_or_file(path)
    if volume is None:
        return f
    if name is None:
        return _only_application(volume, path)
    found = _find_on(volume, name)
    if found is None:
        raise ValueError(f"nothing called {name!r} on {Path(path).name}")
    return found


def open_applications(paths, names=()) -> list[MacFile]:
    return open_disks(paths, names)[0]


def open_disks(paths, names=()) -> tuple[list[MacFile], list[machfs.Volume]]:
    """The applications to launch, from several files, and the disk images
    among them, in the order they came, to be put in the drives.  Each file
    that isn't a disk image is an application; from the images, the names
    found on them (each name is looked for on the images in turn, and
    taken from the first that has it), or with no names each image's only
    application.  An image nothing is taken from, one with no application
    or, when there are names, none of them, is a disk of documents, in a
    drive for the applications to open: Beyond Dark Castle's second disk."""
    opened = [(Path(p), *_volume_or_file(p)) for p in paths]
    images = [(path, volume) for path, volume, _ in opened if volume is not None]
    if names and not images:
        raise ValueError("--app picks an application on a disk image, and there's none here")
    picked: dict[int, list[MacFile]] = {}
    for name in names:
        for i, (_, volume) in enumerate(images):
            found = _find_on(volume, name)
            if found is not None:
                picked.setdefault(i, []).append(found)
                break
        else:
            where = ", ".join(path.name for path, _ in images)
            raise ValueError(f"nothing called {name!r} on {where}")
    apps: list[MacFile] = []
    i = 0
    for path, volume, f in opened:
        if volume is None:
            apps.append(f)
            continue
        if i in picked:
            apps += picked[i]
        elif not names and not _runs_nothing(volume):
            apps.append(_only_application(volume, path))
        i += 1
    if not apps and images:
        _only_application(images[0][1], images[0][0])     # says there's nothing on it to run
    return apps, [volume for _, volume in images]


def _volume_or_file(path: str | os.PathLike) -> tuple[machfs.Volume | None, MacFile | None]:
    path = Path(path)
    raw = path.read_bytes() if path.exists() else b""       # app with only app.rsrc
    volume, problem = None, None
    if is_disk_image(raw):
        try:
            volume = load_volume(raw)
        except ValueError as e:
            problem = e
    if volume is not None:
        return volume, None
    f = load_file(path)
    if problem is not None and not f.rsrc:
        raise ValueError(f"{path.name}: {problem}")     # an image, but one that can't be read
    kind = archive_kind(f)
    if kind is not None:
        raise ValueError(f"{path.name} is a {kind} archive: unpack it first, for example "
                         f"with The Unarchiver (unar {path.name})")
    return None, f


def _find_on(volume: machfs.Volume, name: str) -> MacFile | None:
    """The file on the volume called name or with that Mac path,
    applications first, and of those the one nearest the top of the disk."""
    files = sorted(volume_files(volume), key=lambda f: f.key.count(":"))
    want = name.lower()
    for f in [f for f in files if f.is_application] + files:
        if want in (f.name.lower(), f.key.lower()) or f.key.lower().endswith(":" + want):
            return f
    return None


def _only_application(volume: machfs.Volume, image: str | os.PathLike) -> MacFile:
    """What the disk starts up into, if it isn't Apple's Finder, else its
    only application: Mac Attack's disk has the game in the Finder's
    place, and an application for starting it on a Lisa."""
    startup = _startup_program(volume)
    if startup is not None:
        return startup
    apps = [f for f in volume_files(volume) if f.is_application]
    if len(apps) == 1:
        return apps[0]
    listing = "\n".join(f"  {f.key}" for f in apps) or "  (none)"
    raise ValueError(f"{Path(image).name} holds {len(apps)} applications; pick one with "
                     f"--app:\n{listing}")


def _runs_nothing(volume: machfs.Volume) -> bool:
    return (not any(f.is_application for f in volume_files(volume))
            and _startup_program(volume) is None)


def system_on(volume: machfs.Volume) -> MacFile | None:
    """The disk's System file, the one nearest the top of the disk if it
    has more than one, or None."""
    found = [f for f in volume_files(volume) if f.name == "System" and f.type == b"ZSYS"]
    return min(found, key=lambda f: f.key.count(":"), default=None)


def _startup_program(volume: machfs.Volume) -> MacFile | None:
    """The program the disk starts up into, if it isn't Apple's Finder.
    Some early games took the Finder's place on their disk, so that a Mac
    started from it went straight into the game.  Apple's Finder has its
    signature resource, FNDR or (from System 6) MACS; they don't."""
    name = getattr(volume, "startup", None)
    f = _find_on(volume, name) if name else None
    if f is None:
        return None
    try:
        types = {e.type for e in open_fork(f.rsrc).entries}
    except InvalidForkError:
        return None
    if b"CODE" not in types or types & {b"FNDR", b"MACS"}:
        return None
    return f
