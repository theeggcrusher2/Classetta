"""
File Manager (Inside Macintosh: Files, chapter 2; IM IV, chapter 19).

A volume is a tree of directories and files.  Each has a negative volume
reference number, -1 for the first, and a drive number: a floppy disk's
image is in drive 1, the internal drive, or 2, the external one, and the
rest are hard disks, from drive 3.  Its root is directory ID 2 and the
root's parent 1.  What a volume keeps its files in
is its store:

    the boot volume     memory: "Macintosh HD", whose blessed System Folder
                        holds the System file, Preferences, Extensions and
                        the rest.  It's gone when macrt quits.
    a disk image        the volume machfs read (or macfile, for MFS).
                        Changes stay in memory: the image file is never
                        written.
    a host folder       the folder an application came from, read back by
                        macfile.load_file.  A file is written in the form
                        it was found in: MacBinary as MacBinary, AppleSingle
                        as AppleSingle, and anything else as its data fork
                        under its own name with the resource fork and the
                        Finder info in an AppleDouble "._name" beside it,
                        which is also how new files are written.  Names
                        swap ":" and "/", as macOS does.  Forks are written
                        when the file is closed or flushed, metadata at once.
    a MemoryFolder      files held in Python, for tests and Python guests;
                        changes go back into its MacFiles.

An open fork is a file control block.  Its reference number is its offset
in the FCB buffer (FCBSPtr), which holds each one's record as Inside
Macintosh lays it out; the Resource Manager's open files are FCBs too, so
the two can't hand out the same number.  Each volume has a VCB in the VCB
queue.  Working directories are in a table of their own, numbered from
-32766 so they can't be taken for a volume reference number.

The FileManager is the File Manager as one application sees it: its
default volume and directory, and the files and working directories it
opened, closed when it quits.  At launch its default directory is the
application's own folder, through a working directory, as the Finder
leaves it.
"""

from __future__ import annotations

import calendar
import os
import struct
import time
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

import machfs

from ..macfile import (APPLE_DOUBLE, APPLE_SINGLE, AS_DATA, AS_FINDER_INFO, AS_NAME, AS_RSRC,
                       BINHEX_BANNER, HostFolder, MacFile, MemoryFolder, VolumeFolder, load_file)
from ..memory import ADDRESS_MASK, from_mac, to_mac
from . import lowmem

if TYPE_CHECKING:
    from .lowmem import World
    from .system import System

noErr = 0
dirFulErr = -33
dskFulErr = -34
nsvErr = -35
ioErr = -36
bdNamErr = -37
fnOpnErr = -38
eofErr = -39
posErr = -40
tmfoErr = -42
fnfErr = -43
wPrErr = -44
fLckdErr = -45
vLckdErr = -46
fBsyErr = -47
dupFNErr = -48
opWrErr = -49
paramErr = -50
rfNumErr = -51
permErr = -54
volOnLinErr = -55
nsDrvErr = -56
wrPermErr = -61
dirNFErr = -120
tmwdoErr = -121
badMovErr = -122
diffVolErr = -1303

# Positioning modes (ioPosMode) and permissions (ioPermssn)
fsAtMark, fsFromStart, fsFromLEOF, fsFromMark = 0, 1, 2, 3
NEWLINE_MODE = 0x80                     # the newline character is in the high byte
fsCurPerm, fsRdPerm, fsWrPerm, fsRdWrPerm, fsRdWrShPerm = 0, 1, 2, 3, 4

ROOT_PARENT = 1
ROOT = 2
FIRST_CNID = 16                         # below are the catalog's own files
FCB_START = 2                           # reference numbers are offsets in the FCB buffer
FCB_LEN = 94
MAX_FCBS = 200
VCB_LEN = 178
DQE_LEN = 4 + 18                        # a drive queue element and the four flag bytes before it
WD_FIRST = -32766
WD_LEN = 16
MAX_WDS = 160
BLOCK = 4096                            # what every volume says its allocation blocks are
# Inside Macintosh makes the counts INTEGERs, and programs from 1984 take
# $8000 and up for less than nothing: Word 1.0 calls its disk full.
VOLUME_BLOCKS = 0x7FFF
FREE_BLOCKS = 0x6000
ERIK = 0x4552494B                       # 'ERIK': the procID the Finder gives working directories
MAC_1904 = 2082844800
BOOT_VOLUME = "Macintosh HD"
SYSTEM_FOLDER = "System Folder"
SYSTEM_FILE = "System"
kIsInvisible = 0x4000
HFS_SIGNATURE = 0x4244
FLOPPY = 1440 * 1024                    # the most a floppy disk holds
FIRST_HARD_DISK = 3                     # the drive number after the floppy drives'
SONY = -5                               # the floppy disk driver's reference number

# FindFolder's folder types (Folders.h): what each is called, and whether
# it's in the System Folder or at the top of a volume.
FOLDERS = {
    b"pref": ("Preferences", True), b"extn": ("Extensions", True),
    b"ctrl": ("Control Panels", True), b"amnu": ("Apple Menu Items", True),
    b"strt": ("Startup Items", True), b"shdf": ("Shutdown Items", True),
    b"font": ("Fonts", True), b"prnt": ("PrintMonitor Documents", True),
    b"desk": ("Desktop Folder", False), b"trsh": ("Trash", False),
    b"empt": ("Trash", False), b"temp": ("Temporary Items", False),
}
BOOT_FOLDERS = (b"pref", b"extn", b"ctrl", b"amnu", b"strt", b"desk", b"trsh", b"temp")
kSystemFolderType = b"macs"
kOnSystemDisk = -32768

# File types for host files that carry none, by extension.
HOST_TYPES = {".txt": b"TEXT", ".text": b"TEXT", ".pict": b"PICT", ".pct": b"PICT"}


class FileError(Exception):
    """A File Manager result code.  For opWrErr, refnum is the path that
    already has the file open for writing."""

    def __init__(self, code: int, refnum: int = 0):
        super().__init__(code)
        self.code = code
        self.refnum = refnum


def now() -> int:
    """Seconds since 1904, local time, as the Mac keeps dates."""
    return (calendar.timegm(time.localtime()) + MAC_1904) & 0xFFFFFFFF


def _host_date(t: float) -> int:
    return (calendar.timegm(time.localtime(t)) + MAC_1904) & 0xFFFFFFFF


def _key(name: str) -> str:
    return name.lower()


def _order(name: str) -> bytes:
    """The catalog's order: by name, ignoring case."""
    return to_mac(name.upper())


def valid_name(name: str, limit: int = 31) -> bool:
    return 0 < len(to_mac(name)) <= limit and ":" not in name


# ---------------------------------------------------------------------------
# The catalog
# ---------------------------------------------------------------------------
class Node:
    """A file or a directory: what its catalog record says."""
    is_dir = False

    def __init__(self, name: str):
        self.name = name
        self.cnid = 0
        self.parent: Dir | None = None
        self.volume: Volume | None = None
        self.finfo = bytearray(16)          # FInfo, or DInfo for a directory
        self.xinfo = bytearray(16)          # FXInfo, or DXInfo
        self.crdate = self.mddate = now()
        self.bkdate = 0
        self.ref: object = None             # where the store keeps it

    @property
    def parid(self) -> int:
        return self.parent.cnid if self.parent is not None else ROOT_PARENT

    @property
    def flags(self) -> int:
        return struct.unpack_from(">H", self.finfo, 8)[0]

    @flags.setter
    def flags(self, value: int) -> None:
        struct.pack_into(">H", self.finfo, 8, value & 0xFFFF)

    def __repr__(self) -> str:
        return f"<{type(self).__name__} {self.name!r} #{self.cnid}>"


class Dir(Node):
    is_dir = True

    def __init__(self, name: str):
        super().__init__(name)
        self._children: dict[str, Node] = {}
        self._sorted: list[Node] | None = None
        self.scanned = True                 # a host directory isn't listed until it's looked in

    @property
    def children(self) -> dict[str, Node]:
        if not self.scanned:
            self.scanned = True
            self.volume.store.scan(self.volume, self)
        return self._children

    def child(self, name: str) -> Node | None:
        return self.children.get(_key(name))

    def items(self) -> list[Node]:
        """Files and directories in catalog order, as indexed calls count them."""
        children = self.children
        if self._sorted is None:
            self._sorted = sorted(children.values(), key=lambda n: _order(n.name))
        return self._sorted

    def contains(self, node: Node) -> bool:
        while node is not None:
            if node is self:
                return True
            node = node.parent
        return False


class File(Node):
    def __init__(self, name: str, ftype: bytes = b"\0" * 4, creator: bytes = b"\0" * 4,
                 flags: int = 0):
        super().__init__(name)
        self.finfo[0:8] = ftype + creator
        self.flags = flags
        self.locked = False
        self.attributes = 0                 # flags a disk's directory keeps, beyond locked
        self.dirty = False                  # forks changed since the store last saw them
        self._forks: list[bytearray] | None = None

    @property
    def type(self) -> bytes:
        return bytes(self.finfo[0:4])

    @type.setter
    def type(self, value: bytes) -> None:
        self.finfo[0:4] = value[:4].ljust(4, b"\0")

    @property
    def creator(self) -> bytes:
        return bytes(self.finfo[4:8])

    @creator.setter
    def creator(self, value: bytes) -> None:
        self.finfo[4:8] = value[:4].ljust(4, b"\0")

    def forks(self) -> list[bytearray]:
        if self._forks is None:
            data, rsrc = self.volume.store.load(self)
            self._forks = [bytearray(data), bytearray(rsrc)]
        return self._forks

    def fork(self, rsrc: bool) -> bytearray:
        return self.forks()[1 if rsrc else 0]

    def sizes(self) -> tuple[int, int]:
        """The logical lengths of the forks, without reading them in."""
        if self._forks is not None:
            return len(self._forks[0]), len(self._forks[1])
        return self.volume.store.sizes(self)


def _finfo_for(f: MacFile) -> tuple[bytes, bytes, int]:
    ftype, creator = f.type, f.creator
    if ftype == b"????" and creator == b"????":
        ftype = HOST_TYPES.get(os.path.splitext(f.name)[1].lower(), ftype)
    return ftype, creator, f.flags


class Volume:
    def __init__(self, name: str, vref: int, drive: int, store: Store):
        self.name = name
        self.vref = vref
        self.drive = drive
        self.store = store
        self.locked = False
        self.floppy = store.floppy
        self.crdate = self.mddate = now()
        self.bkdate = 0
        self.finder_info = bytearray(32)    # the first long is the blessed folder's ID
        self.vcb = 0                        # its VCB in the system heap
        self.dqe = 0                        # and its drive's drive queue element
        self.next_cnid = FIRST_CNID
        self.root = Dir(name)
        self.root.volume = self
        self.root.cnid = ROOT
        self.nodes: dict[int, Node] = {ROOT: self.root}
        self.top = Dir("")                  # directory 1, whose one entry is the root
        self.top.volume = self
        self.top.cnid = ROOT_PARENT
        self.top._children[_key(name)] = self.root

    @property
    def driver(self) -> int:
        """The reference number of the disk's driver: the floppy disk
        driver's, or a SCSI disk's, -33 less its SCSI ID."""
        return SONY if self.floppy else -33 - (self.drive - FIRST_HARD_DISK)

    @property
    def blessed(self) -> int:
        return struct.unpack_from(">I", self.finder_info, 0)[0]

    @blessed.setter
    def blessed(self, dirid: int) -> None:
        struct.pack_into(">I", self.finder_info, 0, dirid)

    def dir(self, dirid: int) -> Dir:
        d = self.top if dirid == ROOT_PARENT else self.nodes.get(dirid)
        if not isinstance(d, Dir):
            raise FileError(dirNFErr)
        return d

    def add(self, parent: Dir, node: Node) -> Node:
        """Put a node the store found, or that's just been made, in parent."""
        node.volume = self
        node.parent = parent
        if not node.cnid:
            node.cnid = self.next_cnid
            self.next_cnid += 1
        self.nodes[node.cnid] = node
        parent._children[_key(node.name)] = node
        parent._sorted = None
        return node

    def unlink(self, node: Node) -> None:
        parent = node.parent
        parent._children.pop(_key(node.name), None)
        parent._sorted = None
        node.parent = None

    def forget(self, node: Node) -> None:
        self.unlink(node)
        self.nodes.pop(node.cnid, None)
        if isinstance(node, Dir):
            for child in list(node._children.values()):
                self.forget(child)

    def rename_node(self, node: Node, name: str) -> None:
        if node is self.root:
            self.top._children.pop(_key(self.name), None)
            self.name = name
            self.top._children[_key(name)] = node
            node.name = name
            return
        parent = node.parent
        parent._children.pop(_key(node.name), None)
        node.name = name
        parent._children[_key(name)] = node
        parent._sorted = None

    def counts(self) -> tuple[int, int]:
        """Files and directories (other than the root) known so far."""
        files = sum(1 for n in self.nodes.values() if not n.is_dir)
        return files, len(self.nodes) - files - 1

    def __repr__(self) -> str:
        return f"<Volume {self.name!r} {self.vref}>"


# ---------------------------------------------------------------------------
# Stores: what a volume's files are kept in
# ---------------------------------------------------------------------------
class Store:
    """In memory only.  The other stores say where else changes go."""
    source: object = None
    floppy = False

    def mount(self, vol: Volume) -> None:
        pass

    def scan(self, vol: Volume, d: Dir) -> None:
        pass

    def load(self, f: File) -> tuple[bytes, bytes]:
        if isinstance(f.ref, MacFile):
            return f.ref.data, f.ref.rsrc
        return b"", b""

    def sizes(self, f: File) -> tuple[int, int]:
        data, rsrc = self.load(f)
        return len(data), len(rsrc)

    def save(self, f: File) -> None:
        """The forks and Finder info changed."""
        mf = f.ref
        if isinstance(mf, MacFile):
            data, rsrc = f.forks()
            mf.data, mf.rsrc = bytes(data), bytes(rsrc)
            mf.type, mf.creator, mf.flags = f.type, f.creator, f.flags

    def created(self, node: Node) -> None:
        pass

    def removed(self, node: Node) -> None:
        pass

    def renamed(self, node: Node, old: str) -> None:
        pass

    def moved(self, node: Node, old_parent: Dir) -> None:
        pass


class FolderStore(Store):
    """A MemoryFolder's MacFiles: the volume's root holds them."""

    def __init__(self, folder: MemoryFolder):
        self.folder = self.source = folder

    def mount(self, vol: Volume) -> None:
        for mf in self.folder.files():
            vol.add(vol.root, _file_from(mf))

    def created(self, node: Node) -> None:
        if isinstance(node, File) and node.parent is node.volume.root:
            node.ref = self.folder.add(MacFile(node.name, type=node.type, creator=node.creator))

    def removed(self, node: Node) -> None:
        if isinstance(node.ref, MacFile):
            self.folder.remove(node.ref)
            node.ref = None

    def renamed(self, node: Node, old: str) -> None:
        if isinstance(node.ref, MacFile):
            self.folder.remove(node.ref)
            node.ref.name = node.name
            self.folder.add(node.ref)

    def moved(self, node: Node, old_parent: Dir) -> None:
        if node.parent is node.volume.root:
            self.created(node)
            self.save(node)
        else:
            self.removed(node)


def _file_from(mf: MacFile) -> File:
    f = File(mf.name, *_finfo_for(mf))
    f.ref = mf
    return f


class ImageStore(Store):
    """A disk image's volume, as machfs reads one.  Nothing goes back to it."""

    def __init__(self, volume: machfs.Volume):
        self.volume = self.source = volume
        self.floppy = getattr(volume, "size", FLOPPY + 1) <= FLOPPY

    def mount(self, vol: Volume) -> None:
        def walk(folder, d: Dir) -> None:
            for name, item in folder.items():
                if isinstance(item, machfs.File):
                    node = File(name, bytes(item.type), bytes(item.creator), item.flags)
                    struct.pack_into(">HH", node.finfo, 10, item.y & 0xFFFF, item.x & 0xFFFF)
                    node.ref = item
                    node.locked = bool(getattr(item, "locked", False))
                    node.attributes = getattr(item, "attributes", 0)
                else:
                    node = Dir(name)
                node.crdate, node.mddate, node.bkdate = item.crdate, item.mddate, item.bkdate
                vol.add(d, node)
                if isinstance(node, Dir):
                    walk(item, node)

        walk(self.volume, vol.root)
        vol.crdate, vol.mddate = self.volume.crdate or vol.crdate, self.volume.mddate or vol.mddate
        # machfs doesn't keep which folder is blessed: the one with the System file in it.
        for d in [vol.root, *vol.root.items()]:
            if d.is_dir and isinstance(d.child("System"), File) and d.child("Finder"):
                vol.blessed = d.cnid
                break

    def load(self, f: File) -> tuple[bytes, bytes]:
        item = f.ref
        if isinstance(item, machfs.File):
            return bytes(item.data), bytes(item.rsrc)
        return b"", b""

    def sizes(self, f: File) -> tuple[int, int]:
        item = f.ref
        return (len(item.data), len(item.rsrc)) if isinstance(item, machfs.File) else (0, 0)

    def save(self, f: File) -> None:
        pass


@dataclass
class HostRef:
    """Where a host folder keeps a file: its name there, the form it's in
    ("macbinary", "applesingle", "binhex", "fork" for a bare resource fork,
    or "plain"), and for a plain file which side file has its resource
    fork (see SIDES)."""
    name: str
    form: str
    side: str | None = None


def _side_path(d: Path, name: str, side: str) -> Path:
    return {"._": d / ("._" + name), ".AppleDouble": d / ".AppleDouble" / name,
            ".rsrc": d / (name + ".rsrc"), ".rsrc/": d / ".rsrc" / name,
            "%": d / ("%" + name), "namedfork": d / name / "..namedfork" / "rsrc"}[side]


SIDES = ("namedfork", "._", ".AppleDouble", ".rsrc", ".rsrc/", "%")   # load_file's order
RAW_SIDES = ("namedfork", ".rsrc/")


def _entries(blob: bytes) -> dict[int, bytes] | None:
    """An AppleSingle or AppleDouble file's entries."""
    if len(blob) < 26 or struct.unpack_from(">I", blob)[0] not in (APPLE_SINGLE, APPLE_DOUBLE):
        return None
    n = struct.unpack_from(">H", blob, 24)[0]
    out = {}
    for i in range(n):
        eid, off, length = struct.unpack_from(">III", blob, 26 + 12 * i)
        out[eid] = blob[off:off + length]
    return out


def _apple_single(magic: int, entries: dict[int, bytes]) -> bytes:
    head = struct.pack(">II16xH", magic, 0x00020000, len(entries))
    at = len(head) + 12 * len(entries)
    descriptors, body = b"", b""
    for eid in sorted(entries):
        data = entries[eid]
        descriptors += struct.pack(">III", eid, at + len(body), len(data))
        body += data
    return head + descriptors + body


def _macbinary(f: File) -> bytes:
    """A MacBinary II file."""
    import binascii
    data, rsrc = f.forks()
    name = to_mac(f.name)[:63]
    h = bytearray(128)
    h[1] = len(name)
    h[2:2 + len(name)] = name
    h[65:73] = f.finfo[0:8]
    h[73] = f.flags >> 8
    h[75:81] = f.finfo[10:16]               # location, folder
    h[81] = 1 if f.locked else 0
    struct.pack_into(">IIII", h, 83, len(data), len(rsrc), f.crdate, f.mddate)
    h[101] = f.flags & 0xFF
    h[122] = h[123] = 129
    struct.pack_into(">H", h, 124, binascii.crc_hqx(bytes(h[:124]), 0))
    pad = lambda b: bytes(b) + bytes(-len(b) % 128)     # noqa: E731
    return bytes(h) + pad(data) + pad(rsrc)


def _write(path: Path, data: bytes) -> None:
    """Replace a host file all at once, so a crash can't leave half of it."""
    if path.parent.name == "..namedfork":
        path.write_bytes(data)              # macOS's own resource fork: written in place
        return
    path.parent.mkdir(exist_ok=True)
    tmp = path.with_name(f".{path.name}.macrt-tmp")
    tmp.write_bytes(data)
    os.replace(tmp, path)


def mac_name(host: str) -> str:
    """A host file name as the Mac sees it: "/" for ":", 31 characters."""
    name = from_mac(to_mac(host.replace(":", "/"))[:31])
    return name or "Untitled"


def host_name(name: str) -> str:
    return name.replace("/", ":")


class HostStore(Store):
    """A folder on the host: the volume's root."""

    def __init__(self, root: Path):
        self.root = self.source = Path(root).resolve()

    def mount(self, vol: Volume) -> None:
        vol.root.scanned = False
        try:
            vol.crdate = vol.mddate = _host_date(self.root.stat().st_mtime)
        except OSError:
            pass

    def path(self, node: Node) -> Path:
        parts = []
        while node.parent is not None:
            parts.append(node.ref.name if isinstance(node.ref, HostRef) else node.ref)
            node = node.parent
        return self.root.joinpath(*reversed(parts))

    def scan(self, vol: Volume, d: Dir) -> None:
        where = self.path(d)
        try:
            names = sorted(os.listdir(where))
        except OSError:
            return
        for n in names:
            p = where / n
            try:
                if n.startswith(("._", "%")):
                    base = n[2:] if n.startswith("._") else n[1:]
                    if not base or base in names or not p.is_file():
                        continue
                    node = self._orphan(where, n, base)     # only a resource fork
                    if node is None:
                        continue
                elif n.startswith("."):
                    continue
                elif p.is_dir():
                    node = Dir(mac_name(n))
                    node.ref = n
                    node.scanned = False
                elif n.lower().endswith(".rsrc") and n[:-5] in names:
                    continue                        # the resource fork of the file beside it
                elif p.is_file():
                    node = self._file(where, n)
                else:
                    continue
                node.crdate = node.mddate = _host_date(p.stat().st_mtime)
            except (OSError, ValueError):
                continue                            # unreadable or damaged; the rest are still here
            if _key(node.name) not in d._children:
                vol.add(d, node)

    def _file(self, where: Path, n: str) -> File:
        p = where / n
        raw = p.read_bytes()
        mf = load_file(p, HostFolder(where))
        if raw[:4] == struct.pack(">I", APPLE_SINGLE):
            form = "applesingle"
        elif BINHEX_BANNER in raw[:4096]:
            form = "binhex"
        elif mf.rsrc == raw and not mf.data:
            form = "fork"
        elif mf.data != raw:
            form = "macbinary"
        else:
            form = "plain"
        f = File(mac_name(mf.name), *_finfo_for(mf))
        f.ref = HostRef(n, form)
        if form == "plain":
            f.ref.side = next((s for s in SIDES if _side_path(where, n, s).is_file() and
                               (s != "namedfork" or _side_path(where, n, s).stat().st_size)),
                              None)
        f.locked = form == "binhex"                 # read only: it's text, not a Mac file
        f._forks = [bytearray(mf.data), bytearray(mf.rsrc)]
        return f

    def _orphan(self, where: Path, n: str, base: str) -> File | None:
        mf = load_file(where / n, HostFolder(where))
        if not mf.rsrc:
            return None
        f = File(mac_name(mf.name), *_finfo_for(mf))
        f.ref = HostRef(base, "plain", "._" if n.startswith("._") else "%")
        f._forks = [bytearray(), bytearray(mf.rsrc)]
        return f

    def save(self, f: File) -> None:
        if not isinstance(f.ref, HostRef):
            return
        ref, data, rsrc = f.ref, *f.forks()
        p = self.path(f)
        finder = bytes(f.finfo + f.xinfo)
        if ref.form == "macbinary":
            _write(p, _macbinary(f))
        elif ref.form == "applesingle":
            entries = (_entries(p.read_bytes()) if p.is_file() else None) or {}
            entries.update({AS_DATA: bytes(data), AS_RSRC: bytes(rsrc), AS_NAME: to_mac(f.name),
                            AS_FINDER_INFO: finder + entries.get(AS_FINDER_INFO, b"")[32:]})
            _write(p, _apple_single(APPLE_SINGLE, entries))
        elif ref.form == "fork":
            _write(p, bytes(rsrc))                  # a data fork has nowhere to go
        elif ref.form == "plain":
            _write(p, bytes(data))
            side = ref.side or "._"
            sp = _side_path(p.parent, ref.name, side)
            old = sp.read_bytes() if sp.is_file() else b""
            if side in RAW_SIDES or (old and _entries(old) is None):
                _write(sp, bytes(rsrc))
                side, sp = "._", _side_path(p.parent, ref.name, "._")
                old = sp.read_bytes() if sp.is_file() else b""
                rsrc = None                         # Finder info only in the "._" file
            entries = _entries(old) or {}
            entries[AS_FINDER_INFO] = finder + entries.get(AS_FINDER_INFO, b"")[32:]
            if rsrc is not None:
                entries[AS_RSRC] = bytes(rsrc)
            else:
                entries.pop(AS_RSRC, None)
            _write(sp, _apple_double_bytes(entries))

    def created(self, node: Node) -> None:
        name = host_name(node.name)
        p = self.path(node.parent) / name
        if os.path.lexists(p):
            raise FileError(dupFNErr)
        if isinstance(node, Dir):
            node.ref = name
            p.mkdir()
        else:
            node.ref = HostRef(name, "plain")
            node._forks = [bytearray(), bytearray()]
            self.save(node)

    def _paths(self, node: Node) -> list[Path]:
        """Every host file a node is kept in."""
        p = self.path(node)
        paths = [p]
        if isinstance(node.ref, HostRef) and node.ref.form == "plain":
            paths += [_side_path(p.parent, node.ref.name, s) for s in SIDES[1:]]
        return [q for q in paths if q.is_file()]

    def removed(self, node: Node) -> None:
        if isinstance(node, Dir):
            p = self.path(node)
            for junk in os.listdir(p):
                if junk.startswith("._") or junk == ".DS_Store":
                    os.unlink(p / junk)
            p.rmdir()
        else:
            for p in self._paths(node):
                p.unlink()

    def renamed(self, node: Node, old: str) -> None:
        ref = node.ref
        old_host = ref.name if isinstance(ref, HostRef) else ref
        prefix = host_name(old)
        new_host = host_name(node.name)
        if old_host.lower() != prefix.lower():
            # Hello.bin: keep what follows the name.  A file whose host name
            # is unlike its Mac name keeps it.
            new_host = (new_host + old_host[len(prefix):] if old_host.lower().startswith(
                prefix.lower()) else old_host)
        self._relocate(node, node.parent, old_host, new_host)

    def moved(self, node: Node, old_parent: Dir) -> None:
        name = node.ref.name if isinstance(node.ref, HostRef) else node.ref
        self._relocate(node, old_parent, name, name)

    def _relocate(self, node: Node, old_parent: Dir, old_host: str, new_host: str) -> None:
        src_dir, dst_dir = self.path(old_parent), self.path(node.parent)
        case_only = src_dir == dst_dir and old_host.lower() == new_host.lower()
        if not case_only and os.path.lexists(dst_dir / new_host):
            raise FileError(dupFNErr)
        moves = [(src_dir / old_host, dst_dir / new_host)]
        if isinstance(node.ref, HostRef) and node.ref.form == "plain":
            for s in SIDES[1:]:
                a = _side_path(src_dir, old_host, s)
                if a.is_file():
                    moves.append((a, _side_path(dst_dir, new_host, s)))
            if not moves[0][0].exists():
                moves.pop(0)                        # only a resource fork so far
        for a, b in moves:
            b.parent.mkdir(exist_ok=True)
            os.replace(a, b)
        if isinstance(node.ref, HostRef):
            node.ref.name = new_host
            if node.ref.form in ("macbinary", "applesingle"):
                self.save(node)                     # the name inside
        else:
            node.ref = new_host


def _apple_double_bytes(entries: dict[int, bytes]) -> bytes:
    return _apple_single(APPLE_DOUBLE, entries)


# ---------------------------------------------------------------------------
# Open files and working directories
# ---------------------------------------------------------------------------
class FCB:
    """A file control block: one access path to one fork."""

    def __init__(self, refnum: int, node: File, rsrc: bool, write: bool, shared: bool,
                 owner: FileManager | None):
        self.refnum = refnum
        self.node = node
        self.rsrc = rsrc
        self.write = write
        self.shared = shared
        self.owner = owner                  # who closes it when it quits; None: the system
        self.mark = 0
        self.res = None                     # the Resource Manager's ResFile, while it's mapped

    @property
    def fork(self) -> bytearray:
        return self.node.fork(self.rsrc)

    @property
    def flags(self) -> int:
        """fcbFlags: bit 8 writable, 9 resource fork, 12 shared, 13 locked, 15 dirty."""
        return ((0x100 if self.write else 0) | (0x200 if self.rsrc else 0) |
                (0x1000 if self.shared else 0) | (0x2000 if self.node.locked else 0) |
                (0x8000 if self.node.dirty else 0))

    def __repr__(self) -> str:
        return f"<FCB {self.refnum} {self.node.name!r}{' rsrc' if self.rsrc else ''}>"


@dataclass(eq=False)
class WD:
    refnum: int
    volume: Volume
    dirid: int
    procid: int
    owners: set[FileManager | None]     # who opened it; None: the system, which never closes it


def _physical(n: int) -> int:
    return -(-n // BLOCK) * BLOCK


class FileSystem:
    """Every volume, open file and working directory: one per System."""

    def __init__(self, system: System):
        self.system = system
        self.mem = system.mem
        self.lock = system.lock
        self.volumes: list[Volume] = []
        self.offline: list[Volume] = []     # unmounted, their disks in their drives still
        self.fcbs: dict[int, FCB] = {}
        self.wds: dict[int, WD] = {}
        size = FCB_START + FCB_LEN * MAX_FCBS
        self.fcb_buffer = system.sys_heap.new_ptr(size, clear=True)
        self.mem.w16(self.fcb_buffer, size)
        self.mem.w32(lowmem.FCBSPtr, self.fcb_buffer)
        self.boot = self.mount(BOOT_VOLUME, Store())
        self.system_folder = self.make_dir(self.boot.root, SYSTEM_FOLDER)
        self.boot.blessed = self.system_folder.cnid
        self._write_vcb(self.boot)
        for ftype in BOOT_FOLDERS:
            self.special_folder(self.boot, ftype)

    # =====================================================================
    # Volumes
    # =====================================================================
    def mount(self, name: str, store: Store) -> Volume:
        with self.lock:
            every = self.volumes + self.offline
            drive = 1 if store.floppy else FIRST_HARD_DISK
            while any(v.drive == drive for v in every):
                drive += 1
            vol = Volume(name, -(len(every) + 1), drive, store)
            store.mount(vol)
            vol.vcb = self.system.sys_heap.new_ptr(VCB_LEN, clear=True)
            self._enqueue_vcb(vol.vcb)
            self.volumes.append(vol)
            self._write_vcb(vol)
            self._add_drive(vol)
            return vol

    def unmount(self, vol: Volume) -> None:
        """UnmountVol (IM II-90): the volume flushed, the files open on it
        closed, but for those the Resource Manager has mapped, and its VCB
        out of the queue.  Its disk stays in its drive, offline, till it's
        mounted again or another disk is put in.  The startup volume's
        System file is always open."""
        with self.lock:
            if vol is self.boot:
                raise FileError(fBsyErr)
            for p in [p for p in self.fcbs.values() if p.node.volume is vol and p.res is None]:
                self.close(p.refnum)
            self.flush_volume(vol)
            self._dequeue_vcb(vol.vcb)
            self.volumes.remove(vol)
            self.offline.append(vol)

    def remount(self, vol: Volume) -> None:
        """MountVol for a disk whose volume was unmounted."""
        with self.lock:
            self.offline.remove(vol)
            self.volumes.append(vol)
            self._enqueue_vcb(vol.vcb)
            self._write_vcb(vol)

    def in_drive(self, drive: int) -> Volume | None:
        """The volume on the disk in a drive, mounted or not."""
        return next((v for v in self.volumes + self.offline if v.drive == drive), None)

    def floppies(self) -> list[Volume]:
        """The floppy disks there are, mounted or not, in the order they
        were mounted."""
        return sorted((v for v in self.volumes + self.offline if v.floppy), key=lambda v: -v.vref)

    def changing(self, drive: int) -> bool:
        """Whether the disk out of a drive can come back as another: its
        volume unmounted, and other floppies to choose from."""
        vol = self.in_drive(drive)
        return vol is not None and vol.floppy and vol in self.offline and len(self.floppies()) > 1

    def next_disk(self, drive: int) -> Volume | None:
        """The floppy after the drive's, in turn."""
        floppies = self.floppies()
        vol = self.in_drive(drive)
        return floppies[(floppies.index(vol) + 1) % len(floppies)] if vol in floppies else None

    def change_disk(self, drive: int, put_in: Volume | None = None) -> None:
        """A floppy that was unmounted and taken out of its drive is put
        back as another, the next in turn unless it's said which: the disk
        images of a game on several disks, as it asks for each.  That one
        leaves its own drive, unmounted, for the one taken out."""
        with self.lock:
            if not self.changing(drive):
                return
            out = self.in_drive(drive)
            put_in = put_in or self.next_disk(drive)
            if put_in is out:
                return
            if put_in in self.volumes:
                self.unmount(put_in)
            out.drive, put_in.drive = put_in.drive, out.drive
            out.dqe, put_in.dqe = put_in.dqe, out.dqe      # the drive's, as before
            for vol in (out, put_in):
                self._write_drive_size(vol)

    def _enqueue_vcb(self, vcb: int) -> None:
        m = self.mem
        m.w32(vcb, 0)
        tail = m.r32(lowmem.VCBQHdr + 6) & ADDRESS_MASK
        m.w32(tail if tail else lowmem.VCBQHdr + 2, vcb)
        m.w32(lowmem.VCBQHdr + 6, vcb)

    def _dequeue_vcb(self, vcb: int) -> None:
        m = self.mem
        prev, at = 0, m.r32(lowmem.VCBQHdr + 2) & ADDRESS_MASK
        for _ in range(64):                 # as many as a queue gone round on itself has
            if not at:
                return
            after = m.r32(at) & ADDRESS_MASK
            if at == vcb:
                m.w32(prev if prev else lowmem.VCBQHdr + 2, after)
                if m.r32(lowmem.VCBQHdr + 6) & ADDRESS_MASK == vcb:
                    m.w32(lowmem.VCBQHdr + 6, prev)
                m.w32(vcb, 0)
                return
            prev, at = at, after

    def _add_drive(self, vol: Volume) -> None:
        """The volume's drive's element in the drive queue at DrvQHdr (IM
        IV-181), with the flags before it: a floppy drive's disk in place
        (2, once read) and its two sides, or a hard disk's that it can't be
        ejected (8).  The Duel looks for 8 to tell a hard disk.  The size is
        in 512-byte blocks, its low word first."""
        m = self.mem
        blocks = self._blocks(vol)
        vol.dqe = self.system.sys_heap.new_ptr(DQE_LEN, clear=True) + 4
        m.write(vol.dqe - 4, struct.pack(">BBBBIhhhhHH", 0, 2 if vol.floppy else 8, 1,
                                         0xFF if vol.floppy else 0, 0, 1, vol.drive, vol.driver,
                                         0, blocks & 0xFFFF, blocks >> 16))
        tail = m.r32(lowmem.DrvQHdr + 6)
        m.w32(tail if tail else lowmem.DrvQHdr + 2, vol.dqe)
        m.w32(lowmem.DrvQHdr + 6, vol.dqe)

    @staticmethod
    def _blocks(vol: Volume) -> int:
        return getattr(getattr(vol.store, "volume", None), "size", VOLUME_BLOCKS * BLOCK) // 512

    def _write_drive_size(self, vol: Volume) -> None:
        blocks = self._blocks(vol)
        self.mem.write(vol.dqe + 12, struct.pack(">HH", blocks & 0xFFFF, blocks >> 16))

    def mount_folder(self, folder) -> Volume:
        """The volume a Folder's files are on, mounted the first time."""
        with self.lock:
            if isinstance(folder, VolumeFolder):
                image = folder.volume
                source, make = image, lambda: (image.name, ImageStore(image))
            elif isinstance(folder, HostFolder):
                root = Path(folder.root.path).resolve()
                source, make = root, lambda: (mac_name(root.name or "Host"), HostStore(root))
            elif isinstance(folder, MemoryFolder):
                source, make = folder, lambda: ("Memory", FolderStore(folder))
            else:
                return self.boot
            for vol in self.volumes + self.offline:
                if vol.store.source is source or (isinstance(source, Path) and
                                                  vol.store.source == source):
                    return vol
            return self.mount(*make())

    def volume(self, vref: int) -> Volume:
        """By volume reference number or drive number."""
        for vol in self.volumes:
            if vref in (vol.vref, vol.drive):
                return vol
        raise FileError(nsvErr)

    def volume_named(self, name: str) -> Volume:
        for vol in self.volumes:
            if _key(vol.name) == _key(name):
                return vol
        raise FileError(nsvErr)

    def _write_vcb(self, vol: Volume) -> None:
        files, dirs = vol.counts()
        root_files = sum(1 for n in vol.root.items() if not n.is_dir)
        name = to_mac(vol.name)[:27]
        rec = struct.pack(
            ">HHHIIHHHHHIIHIH28sHhHhIIHHHIHIIIHII32s",
            0, 0, HFS_SIGNATURE, vol.crdate, vol.mddate, 0x8000 if vol.locked else 0,
            root_files, 0, 0, VOLUME_BLOCKS, BLOCK, BLOCK, 0, vol.next_cnid, FREE_BLOCKS,
            bytes([len(name)]) + name.ljust(27, b"\0"), vol.drive, vol.driver, 0,
            vol.vref, 0, 0, 0, 0, 0, vol.bkdate, 0, 0, BLOCK, BLOCK,
            sum(1 for n in vol.root.items() if n.is_dir), files, dirs, bytes(vol.finder_info))
        self.mem.write(vol.vcb + 4, rec)

    def update_volume(self, vol: Volume) -> None:
        vol.mddate = now()
        self._write_vcb(vol)

    # =====================================================================
    # The catalog
    # =====================================================================
    def make_dir(self, parent: Dir, name: str, invisible: bool = False) -> Dir:
        d = Dir(name)
        d.volume = parent.volume
        d.parent = parent
        if invisible:
            d.flags = kIsInvisible
        try:
            parent.volume.store.created(d)
        except OSError:
            raise FileError(ioErr) from None
        parent.volume.add(parent, d)
        return d

    def make_file(self, parent: Dir, name: str, ftype: bytes = b"\0" * 4,
                  creator: bytes = b"\0" * 4) -> File:
        f = File(name, ftype, creator)
        f.volume, f.parent = parent.volume, parent
        f._forks = [bytearray(), bytearray()]
        try:
            parent.volume.store.created(f)
        except OSError:
            raise FileError(ioErr) from None
        parent.volume.add(parent, f)
        return f

    def special_folder(self, vol: Volume, ftype: bytes, create: bool = True) -> Dir:
        """FindFolder: the System Folder, or one of FOLDERS on vol."""
        if ftype == kSystemFolderType or ftype in FOLDERS and FOLDERS[ftype][1]:
            vol = self.boot
            system = vol.nodes.get(vol.blessed)
            if ftype == kSystemFolderType:
                return system
            where = system
        elif ftype in FOLDERS:
            where = vol.root
        else:
            raise FileError(fnfErr)
        name = FOLDERS[ftype][0]
        d = where.child(name)
        if isinstance(d, Dir):
            return d
        if not create or d is not None:
            raise FileError(fnfErr)
        return self.make_dir(where, name, invisible=not FOLDERS[ftype][1])

    def dir_for(self, folder) -> Dir | None:
        """The directory a Folder is, on the volume it's on."""
        with self.lock:
            if isinstance(folder, VolumeFolder):
                d = self.mount_folder(folder).root
                for part in folder.path:
                    d = d.child(part) if isinstance(d, Dir) else None
                return d if isinstance(d, Dir) else None
            if isinstance(folder, HostFolder):
                vol = self.mount_folder(folder)
                d = vol.root
                try:
                    parts = Path(folder.path).resolve().relative_to(vol.store.root).parts
                except ValueError:
                    return None
                for part in parts:
                    d = next((n for n in d.items() if n.is_dir and n.ref == part), None)
                    if d is None:
                        return None
                return d
            if isinstance(folder, MemoryFolder):
                return self.mount_folder(folder).root
            return None

    def node_for(self, f: MacFile) -> File:
        """The node a MacFile is, from the volume its folder is on.  One
        with no folder (an application built in Python) is put at the top
        of the boot volume."""
        with self.lock:
            d = self.dir_for(f.folder)
            node = None
            if d is not None and isinstance(f.folder, HostFolder):
                host = Path(f.key).name
                node = next((n for n in d.items() if isinstance(n.ref, HostRef) and
                             n.ref.name == host), None)
            elif d is not None and isinstance(f.folder, MemoryFolder):
                node = next((n for n in d.items() if n.ref is f), None)
            elif d is not None:
                node = d.child(f.name)
            if isinstance(node, File):
                return node
            return self._adopt(self.boot.root, f)

    def macfile_of(self, node: File) -> MacFile | None:
        """node_for the other way round: the MacFile a node is, to launch
        it; None if its store can't give one."""
        store = node.volume.store
        if isinstance(node.ref, MacFile):
            return node.ref
        if isinstance(store, ImageStore):
            path: list[str] = []
            d = node.parent
            while d is not None and d.parent is not None:
                path.append(d.name)
                d = d.parent
            return VolumeFolder(store.volume, tuple(reversed(path))).find(node.name)
        if isinstance(store, HostStore):
            p = store.path(node)
            try:
                return HostFolder(p.parent, HostFolder(store.root)).find(p.name)
            except (OSError, ValueError):
                return None
        return None

    def system_node(self, f: MacFile) -> File:
        """The System file, in the boot volume's System Folder whatever disk
        it was read from: changes to it stay in memory."""
        return self._adopt(self.system_folder, f, SYSTEM_FILE)

    def in_system_folder(self, f: MacFile) -> File:
        """A file of macrt's own in the System Folder, as the printer's is."""
        return self._adopt(self.system_folder, f)

    def _adopt(self, where: Dir, f: MacFile, name: str | None = None) -> File:
        with self.lock:
            node = next((n for n in where.items() if n.ref is f), None)
            if node is None:
                node = _file_from(f)
                base = name or f.name
                node.name, n = base, 1
                while where.child(node.name) is not None:
                    n += 1
                    node.name = f"{base} {n}"
                self.boot.add(where, node)
            return node

    # =====================================================================
    # Access paths
    # =====================================================================
    def paths(self, node: Node) -> list[FCB]:
        return [p for p in self.fcbs.values() if p.node is node]

    def fcb(self, refnum: int) -> FCB:
        p = self.fcbs.get(refnum)
        if p is None:
            raise FileError(rfNumErr)
        return p

    def open_fork(self, node: Node, rsrc: bool, perm: int, owner: FileManager | None) -> FCB:
        """An access path, with the permission asked for (Inside Macintosh: Files, PBHOpen)."""
        with self.lock:
            if not isinstance(node, File):
                raise FileError(fnfErr)
            vol = node.volume
            locked = node.locked or vol.locked
            writers = [p for p in self.paths(node) if p.rsrc == rsrc and p.write]
            shared = perm == fsRdWrShPerm
            if perm == fsRdPerm:
                write = False
            elif perm == fsCurPerm:
                write = not locked and not writers
            elif perm in (fsWrPerm, fsRdWrPerm, fsRdWrShPerm):
                if locked:
                    raise FileError(vLckdErr if vol.locked else permErr)
                if writers and not (shared and all(p.shared for p in writers)):
                    raise FileError(opWrErr, writers[0].refnum)
                write = True
            else:
                raise FileError(paramErr)
            node.forks()
            refnum = FCB_START
            while refnum in self.fcbs:
                refnum += FCB_LEN
            if refnum >= FCB_START + FCB_LEN * MAX_FCBS:
                raise FileError(tmfoErr)
            p = self.fcbs[refnum] = FCB(refnum, node, rsrc, write, shared, owner)
            self.sync(p)
            return p

    def close(self, refnum: int) -> None:
        with self.lock:
            p = self.fcbs.pop(refnum, None)
            if p is None:
                raise FileError(rfNumErr)
            self.mem.fill(self.fcb_buffer + refnum, FCB_LEN)
            self.flush(p.node)

    def flush(self, node: Node) -> None:
        if isinstance(node, File) and node.dirty:
            node.dirty = False
            try:
                node.volume.store.save(node)
            except OSError:
                node.dirty = True
                raise FileError(ioErr) from None
            for p in self.paths(node):
                self.sync(p)

    def flush_volume(self, vol: Volume) -> None:
        with self.lock:
            for node in {id(p.node): p.node for p in self.fcbs.values()}.values():
                if node.volume is vol:
                    self.flush(node)
            self._write_vcb(vol)

    def sync(self, p: FCB) -> None:
        """Write an access path's FCB record, as apps that read FCBSPtr see it."""
        node = p.node
        eof = len(p.fork)
        name = to_mac(node.name)[:31]
        rec = struct.pack(">IHHIIIIIHII12s4sII32s", node.cnid, p.flags, 0, eof, _physical(eof),
                          p.mark, node.volume.vcb, 0, 0, BLOCK, 0, bytes(12), node.type, 0,
                          node.parid, bytes([len(name)]) + name.ljust(31, b"\0"))
        self.mem.write(self.fcb_buffer + p.refnum, rec)

    def changed(self, node: File) -> None:
        """A fork's contents changed through some access path."""
        node.dirty = True
        node.mddate = now()
        for p in self.paths(node):
            self.sync(p)

    def set_fork(self, node: File, rsrc: bool, data: bytes, flush: bool = True) -> None:
        """Replace a fork's contents (the Resource Manager writing its map)."""
        with self.lock:
            node.fork(rsrc)[:] = data
            self.changed(node)
            if flush:
                self.flush(node)

    # =====================================================================
    # Working directories
    # =====================================================================
    def open_wd(self, vol: Volume, dirid: int, procid: int, owner: FileManager | None) -> int:
        """A working directory's reference number; for a root directory,
        the volume's own (Inside Macintosh: Files, PBOpenWD)."""
        if dirid == ROOT:
            return vol.vref
        with self.lock:
            for wd in self.wds.values():
                if (wd.volume, wd.dirid, wd.procid) == (vol, dirid, procid):
                    # Shared, and kept until the last who opened it is done.
                    wd.owners.add(owner)
                    return wd.refnum
            for i in range(MAX_WDS):
                refnum = WD_FIRST + WD_LEN * i
                if refnum not in self.wds:
                    self.wds[refnum] = WD(refnum, vol, dirid, procid, {owner})
                    return refnum
            raise FileError(tmwdoErr)


# ---------------------------------------------------------------------------
# The File Manager, as one application sees it
# ---------------------------------------------------------------------------
class FileManager:
    def __init__(self, fs: FileSystem, world: World | None = None, start: Dir | None = None):
        self.fs = fs
        self.world = world
        start = start or fs.boot.root
        vol = start.volume
        wd = fs.open_wd(vol, start.cnid, ERIK, self)
        self.default: tuple[int, Volume, int] = (wd, vol, start.cnid)
        self._publish()
        if world is not None:
            world.w16(lowmem.SFSaveDisk, -vol.vref)
            world.w32(lowmem.CurDirStore, start.cnid)

    def _publish(self) -> None:
        if self.world is not None:
            given, vol, _ = self.default
            self.world.w16(lowmem.DefVRefNum, given)
            self.world.w32(lowmem.DefVCBPtr, vol.vcb)

    def close_all(self) -> None:
        """When the application quits: its files, then its working directories."""
        fs = self.fs
        with fs.lock:
            for p in [p for p in fs.fcbs.values() if p.owner is self]:
                try:
                    fs.close(p.refnum)
                except FileError:
                    pass                            # the host refused; nothing more to do
            for refnum, wd in list(fs.wds.items()):
                wd.owners.discard(self)
                if not wd.owners:
                    del fs.wds[refnum]

    # =====================================================================
    # Finding things: a volume reference number (or a working directory's,
    # or a drive number, or 0 for the default), a directory ID (0: the one
    # the first implies) and a pathname, full or partial.
    # =====================================================================
    def _start(self, vref: int, dirid: int) -> tuple[Volume, Dir]:
        fs = self.fs
        if vref == 0:
            _, vol, base = self.default
        elif vref in fs.wds:
            wd = fs.wds[vref]
            vol, base = wd.volume, wd.dirid
        else:
            vol, base = fs.volume(vref), ROOT
        if vol not in fs.volumes:
            raise FileError(nsvErr)
        return vol, vol.dir(dirid or base)

    def resolve(self, vref: int, dirid: int, name: str | None) -> tuple[Volume, Dir, str | None]:
        """The directory a pathname leads to, and the name at its end, or
        None if it names that directory."""
        path = name or ""
        if ":" in path and not path.startswith(":"):
            vol_name, path = path.split(":", 1)
            vol = self.fs.volume_named(vol_name)
            d = vol.root
        else:
            vol, d = self._start(vref, dirid)
            if path.startswith(":"):
                path = path[1:]
        parts = path.split(":")
        names_dir = parts[-1] == ""
        if names_dir:
            parts.pop()
        for i, part in enumerate(parts):
            if part == "":
                if d.parent is None:
                    raise FileError(dirNFErr)
                d = d.parent
            elif i == len(parts) - 1 and not names_dir:
                return vol, d, part
            else:
                d = d.child(part)
                if not isinstance(d, Dir):
                    raise FileError(dirNFErr)
        return vol, d, None

    def lookup(self, vref: int, dirid: int, name: str | None) -> Node:
        vol, d, leaf = self.resolve(vref, dirid, name)
        if leaf is None:
            if d is vol.top:
                raise FileError(dirNFErr)
            return d
        node = d.child(leaf)
        if node is None:
            raise FileError(fnfErr)
        return node

    def lookup_file(self, vref: int, dirid: int, name: str | None, search: bool = False) -> File:
        """A file.  With search, one not in the default directory is looked
        for in the System Folder too, as opening a file or getting its
        information does: the "poor man's search path" (IM IV, chapter 19).
        The System in use is the boot volume's, so its System Folder is
        searched last, after the default volume's own, if it has one."""
        try:
            node = self.lookup(vref, dirid, name)
        except FileError as e:
            given, vol, default = self.default
            if not (search and e.code == fnfErr and name and ":" not in name and not dirid and
                    vref in (0, given)):
                raise
            boot = self.fs.boot
            folders = [(v, b) for v, b in ((vol, vol.blessed), (boot, boot.blessed))
                       if b and (v is not vol or b != default)]
            for v, b in dict.fromkeys(folders):
                try:
                    node = self.lookup(v.vref, b, name)
                    break
                except FileError as again:
                    if again.code != fnfErr:
                        raise
            else:
                raise
        if not isinstance(node, File):
            raise FileError(fnfErr)
        return node

    def lookup_dir(self, vref: int, dirid: int, name: str | None) -> Dir:
        """A directory, as PBHSetVol and PBOpenWD name one; a bare name
        that isn't one in the directory may be a volume's."""
        try:
            node = self.lookup(vref, dirid, name)
        except FileError as e:
            if name and ":" not in name and e.code == fnfErr:
                return self.fs.volume_named(name).root
            raise
        if not isinstance(node, Dir):
            raise FileError(dirNFErr)
        return node

    def volume_for(self, vref: int, name: str | None = None) -> Volume:
        """The volume a volume-level call means: by name if it has one
        ("Disk:" or "Disk:Folder"), else by number.  A name with no colon
        is a file's, which names no volume (MacWrite flushes the volume
        with its document's name in the block)."""
        if name and ":" in name and not name.startswith(":"):
            return self.fs.volume_named(name.split(":", 1)[0])
        return self._start(vref, 0)[0]

    def dir_of(self, vref: int) -> tuple[Volume, int]:
        """The volume and directory a reference number alone means."""
        vol, d = self._start(vref, 0)
        return vol, d.cnid

    def item(self, vref: int, dirid: int, index: int) -> Node:
        """The index-th file or directory in a directory, from 1."""
        d = self._start(vref, dirid)[1]
        items = d.items()
        if not 0 < index <= len(items):
            raise FileError(fnfErr)
        return items[index - 1]

    def file_at(self, vref: int, dirid: int, index: int) -> File:
        """The index-th file in a directory, from 1, directories not counted."""
        files = [n for n in self._start(vref, dirid)[1].items() if isinstance(n, File)]
        if not 0 < index <= len(files):
            raise FileError(fnfErr)
        return files[index - 1]

    # =====================================================================
    # The default volume and directory
    # =====================================================================
    def get_vol(self) -> tuple[Volume, int, int]:
        """(volume, the reference number it was set by, directory ID)."""
        given, vol, dirid = self.default
        return vol, given, dirid

    def set_vol(self, vref: int, name: str | None) -> None:
        """PBSetVol: a volume's root, or a working directory's directory."""
        fs = self.fs
        if name:
            d = self.lookup_dir(vref, 0, name)
            self.default = (d.volume.vref, d.volume, d.cnid)
        elif vref in fs.wds:
            wd = fs.wds[vref]
            self.default = (vref, wd.volume, wd.dirid)
        elif vref:
            vol = fs.volume(vref)
            self.default = (vol.vref, vol, ROOT)
        self._publish()

    def hset_vol(self, vref: int, dirid: int, name: str | None) -> None:
        d = self.lookup_dir(vref, dirid, name)
        given = vref if vref and not (name and ":" in name[1:]) else d.volume.vref
        self.default = (given, d.volume, d.cnid)
        self._publish()

    def open_wd(self, vref: int, dirid: int, name: str | None, procid: int) -> int:
        d = self.lookup_dir(vref, dirid, name)
        return self.fs.open_wd(d.volume, d.cnid, procid, self)

    def close_wd(self, vref: int) -> None:
        fs = self.fs
        with fs.lock:
            wd = fs.wds.get(vref)
            if wd is None:
                fs.volume(vref)                     # a volume: nothing to close
                return
            if wd.refnum != self.default[0]:
                wd.owners.discard(self)
                if not wd.owners or wd.owners == {None}:
                    del fs.wds[vref]

    def wd_info(self, vref: int, index: int = 0, procid: int = 0) -> WD:
        fs = self.fs
        if index > 0:
            vol = self.volume_for(vref) if vref else None
            wds = [wd for wd in fs.wds.values() if (vol is None or wd.volume is vol) and
                   (not procid or wd.procid == procid)]
            if index > len(wds):
                raise FileError(nsvErr)
            return wds[index - 1]
        if vref in fs.wds:
            return fs.wds[vref]
        if vref == 0:
            given, vol, dirid = self.default
            wd = fs.wds.get(given)
            return WD(given, vol, dirid, wd.procid if wd else 0, {self})
        vol = fs.volume(vref)
        return WD(vol.vref, vol, ROOT, 0, {None})

    # =====================================================================
    # Files
    # =====================================================================
    def open(self, vref: int, dirid: int, name: str | None, rsrc: bool, perm: int) -> FCB:
        return self.fs.open_fork(self.lookup_file(vref, dirid, name, True), rsrc, perm, self)

    def _writable(self, vol: Volume) -> None:
        if vol.locked:
            raise FileError(vLckdErr)

    def create(self, vref: int, dirid: int, name: str | None, directory: bool = False) -> Node:
        fs = self.fs
        with fs.lock:
            vol, d, leaf = self.resolve(vref, dirid, name)
            if leaf is None or d is vol.top:
                raise FileError(bdNamErr)
            if not valid_name(leaf):
                raise FileError(bdNamErr)
            self._writable(vol)
            if d.child(leaf) is not None:
                raise FileError(dupFNErr)
            if directory:
                node = fs.make_dir(d, leaf)
            else:
                node = fs.make_file(d, leaf)
            d.mddate = now()
            fs.update_volume(vol)
            return node

    def delete(self, vref: int, dirid: int, name: str | None) -> None:
        fs = self.fs
        with fs.lock:
            node = self.lookup(vref, dirid, name)
            vol = node.volume
            self._writable(vol)
            if isinstance(node, Dir):
                if node is vol.root or node.children or any(
                        wd.volume is vol and wd.dirid == node.cnid for wd in fs.wds.values()):
                    raise FileError(fBsyErr)
            else:
                if node.locked:
                    raise FileError(fLckdErr)
                if fs.paths(node):
                    raise FileError(fBsyErr)
            try:
                vol.store.removed(node)
            except OSError:
                raise FileError(ioErr) from None
            parent = node.parent
            vol.forget(node)
            parent.mddate = now()
            fs.update_volume(vol)

    def rename(self, vref: int, dirid: int, name: str | None, new: str) -> None:
        fs = self.fs
        with fs.lock:
            node = self.lookup(vref, dirid, name)
            vol = node.volume
            if node is vol.root:
                new = new[:-1] if new.endswith(":") else new
                if not valid_name(new, 27):
                    raise FileError(bdNamErr)
                vol.rename_node(node, new)
                fs.update_volume(vol)
                return
            if not valid_name(new):
                raise FileError(bdNamErr)
            self._writable(vol)
            if isinstance(node, File) and node.locked:
                raise FileError(fLckdErr)
            other = node.parent.child(new)
            if other is not None and other is not node:
                raise FileError(dupFNErr)
            old = node.name
            vol.rename_node(node, new)
            try:
                vol.store.renamed(node, old)
            except (OSError, FileError) as e:
                vol.rename_node(node, old)
                raise FileError(e.code if isinstance(e, FileError) else ioErr) from None
            for p in fs.paths(node):
                fs.sync(p)
            fs.update_volume(vol)

    def move(self, vref: int, dirid: int, name: str | None, new_dirid: int,
             new_name: str | None) -> None:
        """PBCatMove: to a directory on the same volume."""
        fs = self.fs
        with fs.lock:
            node = self.lookup(vref, dirid, name)
            vol = node.volume
            try:
                dest = self.lookup(vol.vref, new_dirid, new_name)
            except FileError as e:
                raise FileError(dirNFErr if e.code == fnfErr else e.code) from None
            if not isinstance(dest, Dir) or dest.volume is not vol:
                raise FileError(badMovErr)
            if node is vol.root or (node.is_dir and node.contains(dest)):
                raise FileError(badMovErr)
            if dest is node.parent:
                return
            if dest.child(node.name) is not None:
                raise FileError(dupFNErr)
            self._writable(vol)
            old = node.parent
            vol.unlink(node)
            vol.add(dest, node)
            try:
                vol.store.moved(node, old)
            except (OSError, FileError) as e:
                vol.unlink(node)
                vol.add(old, node)
                raise FileError(e.code if isinstance(e, FileError) else ioErr) from None
            old.mddate = dest.mddate = now()
            for p in fs.paths(node):
                fs.sync(p)

    def set_lock(self, vref: int, dirid: int, name: str | None, locked: bool) -> None:
        node = self.lookup_file(vref, dirid, name)
        self._writable(node.volume)
        node.locked = locked
        self.metadata_changed(node)

    def metadata_changed(self, node: Node) -> None:
        """Finder info, dates or the lock changed: the store hears at once."""
        fs = self.fs
        with fs.lock:
            if isinstance(node, File):
                try:
                    node.volume.store.save(node)
                except OSError:
                    raise FileError(ioErr) from None
                node.dirty = False
                for p in fs.paths(node):
                    fs.sync(p)

    def exchange(self, a: File, b: File) -> None:
        """FSpExchangeFiles: the forks and modification dates trade places;
        open paths follow the data."""
        fs = self.fs
        with fs.lock:
            if a.volume is not b.volume:
                raise FileError(diffVolErr)
            a.forks(), b.forks()
            a._forks, b._forks = b._forks, a._forks
            a.mddate, b.mddate = b.mddate, a.mddate
            for p in fs.paths(a) + fs.paths(b):
                p.node = b if p.node is a else a
            a.dirty = b.dirty = True
            fs.flush(a)
            fs.flush(b)

    def find_folder(self, vref: int, ftype: bytes, create: bool) -> Dir:
        vol = self.fs.boot if vref in (0, kOnSystemDisk) else self.volume_for(vref)
        return self.fs.special_folder(vol, ftype, create)

    # =====================================================================
    # Reading and writing
    # =====================================================================
    @staticmethod
    def _position(p: FCB, mode: int, offset: int) -> int:
        base = (p.mark, 0, len(p.fork), p.mark)[mode & 3]
        pos = offset if mode & 3 == fsFromStart else base + (offset if mode & 3 else 0)
        if pos < 0:
            raise FileError(posErr)
        return pos

    def read(self, refnum: int, count: int, mode: int = fsAtMark,
             offset: int = 0) -> tuple[bytes, int]:
        """(what was read, noErr or eofErr)."""
        fs = self.fs
        with fs.lock:
            p = fs.fcb(refnum)
            if count < 0:
                raise FileError(paramErr)
            pos = self._position(p, mode, offset)
            fork = p.fork
            data = bytes(fork[pos:pos + count])
            if mode & NEWLINE_MODE:
                stop = data.find(bytes([mode >> 8 & 0xFF]))
                if stop >= 0:
                    data = data[:stop + 1]
                    count = len(data)
            p.mark = min(pos + len(data), len(fork)) if pos <= len(fork) else len(fork)
            fs.sync(p)
            return data, (eofErr if len(data) < count else noErr)

    def write(self, refnum: int, data: bytes, mode: int = fsAtMark, offset: int = 0) -> int:
        fs = self.fs
        with fs.lock:
            p = fs.fcb(refnum)
            if not p.write:
                raise FileError(wrPermErr)
            self._writable(p.node.volume)
            pos = self._position(p, mode, offset)
            fork = p.fork
            if pos > len(fork):
                fork.extend(bytes(pos - len(fork)))
            fork[pos:pos + len(data)] = data
            p.mark = pos + len(data)
            fs.changed(p.node)
            return len(data)

    def get_eof(self, refnum: int) -> int:
        return len(self.fs.fcb(refnum).fork)

    def set_eof(self, refnum: int, eof: int) -> None:
        fs = self.fs
        with fs.lock:
            p = fs.fcb(refnum)
            if not p.write:
                raise FileError(wrPermErr)
            if eof < 0:
                raise FileError(paramErr)
            fork = p.fork
            if eof < len(fork):
                del fork[eof:]
            else:
                fork.extend(bytes(eof - len(fork)))
            for other in fs.paths(p.node):
                other.mark = min(other.mark, eof)
            fs.changed(p.node)

    def get_fpos(self, refnum: int) -> int:
        return self.fs.fcb(refnum).mark

    def set_fpos(self, refnum: int, mode: int, offset: int) -> tuple[int, int]:
        """(the new mark, noErr or eofErr: past the end it stops at the end)."""
        fs = self.fs
        with fs.lock:
            p = fs.fcb(refnum)
            pos = self._position(p, mode, offset)
            err = noErr
            if pos > len(p.fork):
                pos, err = len(p.fork), eofErr
            p.mark = pos
            fs.sync(p)
            return pos, err

    def allocate(self, refnum: int, count: int) -> int:
        p = self.fs.fcb(refnum)
        if not p.write:
            raise FileError(wrPermErr)
        return _physical(max(count, 0))

    def flush_file(self, refnum: int) -> None:
        self.fs.flush(self.fs.fcb(refnum).node)

    def close(self, refnum: int) -> None:
        self.fs.close(refnum)
