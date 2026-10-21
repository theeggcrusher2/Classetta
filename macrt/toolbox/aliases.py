"""
Alias records (Inside Macintosh: Files, chapter 4): where a file or a
directory is, said in enough ways to find it again after it's been moved
or renamed, or its volume mounted again.

A record is in the format System 7 writes, so the Finder's alias files on
a disk resolve, and what macrt makes would resolve on a Mac: a fixed part
with the volume, the parent directory, the name and the file number, then
tagged extras, of which macrt writes the parent directory's name, the IDs
of the directories above, and the full pathname.  A minimal record has
only the fixed part.

Finding the target again tries, in turn: the parent directory and name;
the file or directory number; the full pathname; and, given the file the
record was made relative to, the pathname from there.  File numbers are
macrt's for this session, not a disk's, so one is believed only if its
name or creation date agrees.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass, field

from ..memory import from_mac, to_mac
from .files import ROOT_PARENT, Dir, FileError, FileSystem, Node, Volume, fnfErr, nsvErr, paramErr

FIXED = struct.Struct(">4sHHH28sI2sHI64sII4s4shhIH10s")
VERSION = 2
kFile, kDirectory = 0, 1
HFS_SIGNATURE = b"BD"
TAG_PARENT_NAME, TAG_DIR_IDS, TAG_PATH, TAG_ZONE, TAG_SERVER = 0, 1, 2, 3, 4
TAG_END = 0xFFFF
OURS = (TAG_PARENT_NAME, TAG_DIR_IDS, TAG_PATH)

# GetAliasInfo's indexes below 1, the parent's name
asiZoneName, asiServerName, asiVolumeName, asiAliasName = -3, -2, -1, 0


def _pstr(s: str, size: int) -> bytes:
    b = to_mac(s)[:size - 1]
    return bytes([len(b)]) + b.ljust(size - 1, b"\0")


def _unpstr(b: bytes) -> str:
    return from_mac(b[1:1 + min(b[0], len(b) - 1)])


@dataclass
class Alias:
    """What a record says."""
    user_type: bytes = bytes(4)
    is_dir: bool = False
    volume: str = ""
    vol_crdate: int = 0
    drive_type: int = 0
    parid: int = 0
    name: str = ""
    cnid: int = 0
    crdate: int = 0
    type: bytes = bytes(4)
    creator: bytes = bytes(4)
    levels_from: int = -1
    levels_to: int = -1
    vol_attrs: int = 0
    fsid: int = 0
    extras: dict[int, bytes] = field(default_factory=dict)

    @property
    def path(self) -> str | None:
        """The full pathname, "Disk:Folder:File", if the record has it."""
        p = self.extras.get(TAG_PATH)
        return None if p is None else from_mac(p)

    @property
    def minimal(self) -> bool:
        return TAG_PATH not in self.extras

    def path_names(self) -> list[str]:
        """The names on the way from the volume's root to the target, the
        target's last; for a volume, none."""
        if self.path is None:
            return []
        return [n for n in self.path.split(":")[1:] if n]

    def pack(self) -> bytes:
        tail = b""
        for tag, data in self.extras.items():
            tail += struct.pack(">HH", tag, len(data)) + data + bytes(len(data) & 1)
        tail += struct.pack(">HH", TAG_END, 0)
        return FIXED.pack(
            self.user_type, FIXED.size + len(tail), VERSION, kDirectory if self.is_dir else kFile,
            _pstr(self.volume, 28), self.vol_crdate, HFS_SIGNATURE, self.drive_type, self.parid,
            _pstr(self.name, 64), self.cnid, self.crdate, self.type, self.creator,
            self.levels_from, self.levels_to, self.vol_attrs, self.fsid, bytes(10)) + tail

    @classmethod
    def parse(cls, data: bytes) -> Alias:
        """A record; paramErr if it isn't one."""
        if len(data) < FIXED.size:
            raise FileError(paramErr)
        (user_type, size, version, kind, volume, vol_crdate, _, drive_type, parid, name, cnid,
         crdate, ftype, creator, levels_from, levels_to, vol_attrs, fsid, _) = (
            FIXED.unpack_from(data))
        if version != VERSION or size < FIXED.size:
            raise FileError(paramErr)
        a = cls(user_type, kind == kDirectory, _unpstr(volume), vol_crdate, drive_type, parid,
                _unpstr(name), cnid, crdate, ftype, creator, levels_from, levels_to, vol_attrs,
                fsid)
        at, end = FIXED.size, min(size, len(data))
        while at + 4 <= end:
            tag, n = struct.unpack_from(">HH", data, at)
            if tag == TAG_END:
                break
            a.extras[tag] = bytes(data[at + 4:at + 4 + n])
            at += 4 + n + (n & 1)
        return a

    def same_place(self, other: Alias) -> bool:
        """Whether other, made for the target now, says where it is as
        this does: what's worth updating this for."""
        mine = (self.volume, self.vol_crdate, self.parid, self.name, self.is_dir)
        if mine != (other.volume, other.vol_crdate, other.parid, other.name, other.is_dir):
            return False
        if self.cnid and self.cnid != other.cnid:
            return False
        if not self.minimal and self.path != other.path:
            return False
        return (self.levels_from, self.levels_to) == (other.levels_from, other.levels_to)


def _up(node: Node) -> list[Node]:
    """node, its parent, and so on to the volume's root."""
    chain = []
    while node is not None:
        chain.append(node)
        node = node.parent
    return chain


def path_of(node: Node) -> str:
    names = [n.name for n in reversed(_up(node))]
    return ":".join(names) + (":" if len(names) == 1 else "")


def make(target: Node, from_node: Node | None = None, minimal: bool = False) -> Alias:
    """A record for target, relative to from_node if it's given and on the
    same volume."""
    vol = target.volume
    a = Alias(is_dir=target.is_dir, volume=vol.name, vol_crdate=vol.crdate, parid=target.parid,
              name=target.name, crdate=target.crdate)
    if not target.is_dir:
        a.type, a.creator = target.type, target.creator
    if minimal:
        return a
    a.cnid = target.cnid
    chain = _up(target)
    if target.parent is not None:
        a.extras[TAG_PARENT_NAME] = to_mac(target.parent.name)
    a.extras[TAG_DIR_IDS] = b"".join(struct.pack(">I", d.cnid) for d in chain[1:-1])
    a.extras[TAG_PATH] = to_mac(path_of(target))
    if from_node is not None and from_node.volume is vol:
        start = _up(from_node)
        common = next(n for n in start if n in chain)        # the root at least
        a.levels_from, a.levels_to = start.index(common), chain.index(common)
    return a


def from_path(fs: FileSystem, path: str) -> Alias:
    """A minimal record for a full pathname, whose target needn't exist,
    nor its volume be mounted."""
    vol_name, _, rest = path.partition(":")
    names = [n for n in rest.split(":") if n]
    a = Alias(is_dir=path.endswith(":"), volume=vol_name, name=names[-1] if names else vol_name,
              parid=ROOT_PARENT if not names else 0)
    a.extras[TAG_PATH] = to_mac(path.rstrip(":") + (":" if not names else ""))
    try:
        vol = fs.volume_named(vol_name)
    except FileError:
        return a
    a.vol_crdate = vol.crdate
    parent = _walk(vol.root, names[:-1])
    if isinstance(parent, Dir) and names:
        a.parid = parent.cnid
        node = parent.child(names[-1])
        if node is not None:
            found = make(node, minimal=True)
            found.extras = a.extras
            return found
    return a


def _walk(d: Node | None, names: list[str]) -> Node | None:
    for n in names:
        if not isinstance(d, Dir):
            return None
        d = d.child(n)
    return d


@dataclass
class Found:
    """Where a record's target is, or would be: its volume, the directory
    it's in, its name and, if it's there, itself."""
    volume: Volume
    parid: int
    name: str
    node: Node | None

    @classmethod
    def of(cls, node: Node) -> Found:
        return cls(node.volume, node.parid, node.name, node)


def _volume(fs: FileSystem, a: Alias) -> Volume | None:
    """The volume by its name and creation date; by either, if it's been
    renamed or another of the same name is mounted."""
    try:
        named = fs.volume_named(a.volume)
    except FileError:
        named = None
    if named is not None and named.crdate == a.vol_crdate:
        return named
    dated = next((v for v in fs.volumes if a.vol_crdate and v.crdate == a.vol_crdate), None)
    return dated or named


def _fits(a: Alias, node: Node | None) -> bool:
    return node is not None and node.is_dir == a.is_dir and node.volume is not None and (
        node.parent is not None or node is node.volume.root)


def candidates(fs: FileSystem, a: Alias, from_node: Node | None = None,
               relative_first: bool = False) -> list[Node]:
    """Every node the record could mean, the likeliest first."""
    found: list[Node] = []

    def add(node: Node | None) -> None:
        if _fits(a, node) and node not in found:
            found.append(node)

    def relative() -> None:
        if from_node is None or a.levels_from < 0 or a.levels_to < 0:
            return
        names = a.path_names()
        if a.levels_to > len(names):
            return
        up = _up(from_node)
        if a.levels_from < len(up):
            add(_walk(up[a.levels_from], names[len(names) - a.levels_to:]))

    if relative_first:
        relative()
    vol = _volume(fs, a)
    if vol is not None:
        if a.parid == ROOT_PARENT and a.is_dir:
            add(vol.root)                       # the volume, renamed or not
        try:
            add(vol.dir(a.parid).child(a.name))
        except FileError:
            pass
        node = vol.nodes.get(a.cnid) if a.cnid else None
        if node is not None and (node.parent is not None and node.parent.child(a.name) is node
                                 or node.crdate == a.crdate):
            add(node)
        if a.path is not None:
            add(_walk(vol.root, a.path_names()))
    relative()
    return found


def resolve(fs: FileSystem, a: Alias, from_node: Node | None = None) -> Found:
    """The target; or, if it isn't there but the directory it was in is,
    where it would be, which says fnfErr; or the error."""
    nodes = candidates(fs, a, from_node)
    if nodes:
        return Found.of(nodes[0])
    vol = _volume(fs, a)
    if vol is None:
        raise FileError(nsvErr)
    d = None
    if a.parid not in (0, ROOT_PARENT):
        try:
            d = vol.dir(a.parid)
        except FileError:
            pass
    if d is None and a.path_names():
        d = _walk(vol.root, a.path_names()[:-1])
    if not isinstance(d, Dir):
        raise FileError(fnfErr)
    return Found(vol, d.cnid, a.name, None)


def updated(a: Alias, target: Node, from_node: Node | None) -> Alias | None:
    """The record a should be now that its target is target, keeping what
    macrt doesn't write; None if it says where target is already."""
    fresh = make(target, from_node, minimal=a.minimal)
    fresh.user_type = a.user_type
    if a.same_place(fresh):
        return None
    for tag, data in a.extras.items():
        if tag not in OURS:
            fresh.extras[tag] = data
    return fresh


def info(a: Alias, index: int) -> str:
    """GetAliasInfo: the target's name, the names of the directories
    above it (1, its parent's), or the volume's; no zone or server."""
    if index == asiVolumeName:
        return a.volume
    if index in (asiZoneName, asiServerName):
        return ""
    if index < asiZoneName:
        raise FileError(paramErr)
    if index == asiAliasName:
        return a.name
    names = a.path_names()
    if names:
        return names[-1 - index] if index < len(names) else ""
    if index == 1 and TAG_PARENT_NAME in a.extras:
        return from_mac(a.extras[TAG_PARENT_NAME])
    return ""
