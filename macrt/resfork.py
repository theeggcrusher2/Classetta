"""
The resource fork file format (Inside Macintosh: More Macintosh Toolbox,
1-121), on the host side.

Reading and writing are both here.  open_fork checks every offset and
count in a map before anything follows them, since the Resource Manager
copies maps into guest memory and walks them there.  rsrcfork expands
System 7 compressed resources.

    header   data offset, map offset, data length, map length; padded to 256
    data     each resource's length, then its bytes
    map      a copy of the header and two in-memory fields (zero on disk),
             file attributes, offsets to the type and name lists, then the
             type list, the reference lists and the name list
"""

from __future__ import annotations

import struct
from dataclasses import dataclass

import rsrcfork

HEADER_SIZE = 256
MAP_HEADER = 28             # header copy (16), nextMap (4), fileRef (2), attrs, typeList, nameList
TYPE_ENTRY = 8
REF_ENTRY = 12
NO_NAME = 0xFFFF

# Resource attributes
resSysHeap = 0x40
resPurgeable = 0x20
resLocked = 0x10
resProtected = 0x08
resPreload = 0x04
resChanged = 0x02
resCompressed = 0x01

COMPRESSED_TAG = b"\xa8\x9f\x65\x72"     # how a compressed resource's data starts

# Resource file attributes
mapReadOnly = 0x80
mapCompact = 0x40
mapChanged = 0x20


class InvalidForkError(ValueError):
    pass


class DecompressError(ValueError):
    pass


@dataclass
class Resource:
    type: bytes
    id: int
    data: bytes = b""
    name: bytes | None = None
    attrs: int = 0


def restype(t: str | bytes | int) -> bytes:
    """A four-character resource type from 'WIND', b'WIND' or 0x57494E44."""
    if isinstance(t, int):
        return (t & 0xFFFFFFFF).to_bytes(4, "big")
    if isinstance(t, str):
        t = t.encode("mac_roman")
    if len(t) != 4:
        raise ValueError(f"resource types are four characters, not {t!r}")
    return bytes(t)


@dataclass
class MapEntry:
    """A reference list entry, as stored."""
    type: bytes
    id: int
    name: bytes | None
    attrs: int
    offset: int                 # of the resource's length word, in the data


@dataclass
class ForkMap:
    """Where everything in a fork is, checked."""
    fork: bytes
    data_offset: int
    data_length: int
    map_offset: int
    map_length: int
    attrs: int
    entries: list[MapEntry]

    def find(self, rtype: bytes, rid: int) -> MapEntry | None:
        return next((e for e in self.entries if e.type == rtype and e.id == rid), None)

    def raw(self, e: MapEntry) -> bytes:
        """The resource's bytes as stored (compressed, if it is)."""
        if e.offset + 4 <= self.data_length:
            at = self.data_offset + e.offset
            n, = struct.unpack_from(">I", self.fork, at)
            if e.offset + 4 + n <= self.data_length:
                return self.fork[at + 4:at + 4 + n]
        raise InvalidForkError(f"the data of {e.type!r} {e.id} is outside the fork")


def is_fork(data: bytes) -> bool:
    try:
        open_fork(data)
    except InvalidForkError:
        return False
    return True


def open_fork(data: bytes) -> ForkMap:
    """A fork's map, checked so that nothing following its offsets reads
    or writes outside it.  Its lists are inside the map, past the header's
    in-memory fields, and don't overlap: type list, reference lists (in any
    order), names."""
    data = bytes(data)
    if len(data) < 16:
        raise InvalidForkError("too short for a resource fork")
    data_at, map_at, data_len, map_len = struct.unpack_from(">IIII", data, 0)
    if data_at + data_len > len(data) or map_at + map_len > len(data):
        raise InvalidForkError("the map or data runs past the end of the fork")
    if map_len < MAP_HEADER:
        raise InvalidForkError("the map is too short")
    m = data[map_at:map_at + map_len]
    attrs, types_at, names_at = struct.unpack_from(">HHH", m, 22)

    def need(ok: bool, what: str) -> None:
        if not ok:
            raise InvalidForkError(what)

    need(MAP_HEADER <= types_at and types_at + 2 <= map_len and names_at <= map_len,
         "the map's lists are outside it")
    n_types = (struct.unpack_from(">H", m, types_at)[0] + 1) & 0xFFFF
    end = types_at + 2 + TYPE_ENTRY * n_types
    need(end <= map_len, "the type list runs out of the map")
    lists = []
    for i in range(n_types):
        rtype, count, refs = struct.unpack_from(">4sHH", m, types_at + 2 + TYPE_ENTRY * i)
        lists.append((types_at + refs, (count + 1) & 0xFFFF, rtype))
    for at, count, rtype in sorted(lists):
        need(at >= end and at + REF_ENTRY * count <= map_len,
             f"the {rtype!r} references overlap something else or run out of the map")
        end = at + REF_ENTRY * count
    need(names_at >= end, "the name list overlaps the references")
    entries = []
    for refs, count, rtype in lists:
        for j in range(count):
            rid, name_off, word = struct.unpack_from(">hHI", m, refs + REF_ENTRY * j)
            name = None
            if name_off != NO_NAME:
                at = names_at + name_off
                need(at < map_len and at + 1 + m[at] <= map_len,
                     f"the name of {rtype!r} {rid} is outside the map")
                name = m[at + 1:at + 1 + m[at]]
            entries.append(MapEntry(rtype, rid, name, word >> 24, word & 0xFFFFFF))
    return ForkMap(data, data_at, data_len, map_at, map_len, attrs, entries)


def read_fork(data: bytes) -> list[Resource]:
    """Every resource in a fork, in map order.  Compressed ones are
    expanded, except any that won't, which stay as they were stored."""
    fm = open_fork(data)
    out = []
    for e in fm.entries:
        body, attrs = fm.raw(e), e.attrs
        if attrs & resCompressed and body[:4] == COMPRESSED_TAG:
            try:
                body, attrs = decompress(body), attrs & ~resCompressed
            except DecompressError:
                pass
        out.append(Resource(e.type, e.id, body, e.name, attrs))
    return out


def decompress(data: bytes) -> bytes:
    """Expand a resource stored with resCompressed set."""
    try:
        return rsrcfork.compress.decompress(data)
    except Exception as e:      # noqa: BLE001 - as in open_fork
        raise DecompressError(str(e)) from e


def build_fork(resources: list[Resource], file_attrs: int = 0) -> bytes:
    """A resource fork holding these resources, types in first-seen order."""
    by_type: dict[bytes, list[Resource]] = {}
    for r in resources:
        by_type.setdefault(restype(r.type), []).append(r)

    data = bytearray()
    names = bytearray()
    refs = bytearray()
    types = bytearray()
    ref_lists_at = 2 + TYPE_ENTRY * len(by_type)        # from the start of the type list
    if MAP_HEADER + ref_lists_at + REF_ENTRY * len(resources) > 0xFFFF:
        raise ValueError("too many resources for one fork")
    for rtype, items in by_type.items():
        types += struct.pack(">4sHH", rtype, (len(items) - 1) & 0xFFFF,
                             ref_lists_at + len(refs))
        for r in items:
            name_off = NO_NAME
            if r.name is not None:
                if len(names) >= NO_NAME:
                    raise ValueError("too many resource names for one fork")
                name_off = len(names)
                names += bytes([len(r.name[:255])]) + r.name[:255]
            offset = len(data)
            if offset > 0xFFFFFF:
                raise ValueError("more than 16 MB of resources for one fork")
            data += struct.pack(">I", len(r.data)) + r.data
            attrs = r.attrs & ~resChanged & 0xFF
            refs += struct.pack(">hHII", r.id, name_off, (attrs << 24) | offset, 0)
    type_list = struct.pack(">H", (len(by_type) - 1) & 0xFFFF) + types + refs
    map_len = MAP_HEADER + len(type_list) + len(names)
    map_off = HEADER_SIZE + len(data)
    header = struct.pack(">IIII", HEADER_SIZE, map_off, len(data), map_len)
    rmap = (header + bytes(6) + struct.pack(">HHH", file_attrs & ~mapChanged & 0xFFFF,
                                            MAP_HEADER, MAP_HEADER + len(type_list))
            + type_list + names)
    return header.ljust(HEADER_SIZE, b"\0") + bytes(data) + rmap
