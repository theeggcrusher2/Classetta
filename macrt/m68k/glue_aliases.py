"""
The Alias Manager (Inside Macintosh: Files, chapter 4): AliasDispatch's
calls, but FindFolder, which is the File Manager's, and ResolveAliasFile,
which follows the Finder's alias files.  The records are
macrt.toolbox.aliases', in AliasHandles in the application's heap.

There are no servers to mount volumes from, so a record whose volume isn't
mounted doesn't resolve, and MatchAlias looks where a record says its
target is, without searching a volume's catalog for its name.
"""

from __future__ import annotations

import struct

from ..memory import from_mac, to_mac
from ..resfork import InvalidForkError, open_fork
from ..toolbox import aliases as al
from ..toolbox import files as fm
from ..toolbox.files import FileError, noErr
from ..toolbox.memmgr import memFullErr
from . import glue_aevents
from .dispatch import trap
from .glue_files import _files, _fs_call, _spec, put_cat_info, put_spec

isAlias = 0x8000                # in fdFlags
kARMSearchRelFirst = 0x400      # MatchAlias's rules
CINFO_PB = 108
MAX_CHAIN = 10                  # alias files to an alias file


def _node(t, spec: int) -> fm.Node | None:
    return _files(t).lookup(*_spec(t, spec)) if spec else None


def _target(t, spec: int) -> fm.Node:
    if not spec:
        raise FileError(fm.paramErr)
    return _files(t).lookup(*_spec(t, spec))


def _record(t, h: int) -> al.Alias:
    if not h:
        raise FileError(fm.paramErr)
    try:
        heap = t.system.heap_of(h)
        data = t.mem.read(heap.deref(h), heap.handle_size(h))
    except ValueError:
        raise FileError(fm.paramErr) from None
    return al.Alias.parse(data)


def _new_handle(t, a: al.Alias, alias_p: int) -> int:
    t.w32(alias_p, 0)
    data = a.pack()
    heap = t.process.heap
    h = heap.new_handle(len(data))
    if not h:
        return memFullErr
    t.mem.write(heap.deref(h), data)
    t.w32(alias_p, h)
    return noErr


def _replace(t, h: int, a: al.Alias) -> int:
    data = a.pack()
    heap = t.system.heap_of(h)
    if not heap.set_handle_size(h, len(data)):
        return memFullErr
    t.mem.write(heap.deref(h), data)
    return noErr


def _put_found(t, spec: int, found: al.Found) -> None:
    put_spec(t, spec, found.volume.vref, found.parid, found.name)


@trap
def NewAlias(t, from_file, target, alias_p):
    return _fs_call(t, lambda: _new_handle(
        t, al.make(_target(t, target), _node(t, from_file)), alias_p))


@trap
def NewAliasMinimal(t, target, alias_p):
    return _fs_call(t, lambda: _new_handle(t, al.make(_target(t, target), minimal=True), alias_p))


@trap
def NewAliasMinimalFromFullPath(t, length, path, zone, server, alias_p):
    if length <= 0 or not path:
        t.w32(alias_p, 0)
        return fm.paramErr
    full = from_mac(t.mem.read(path, length))
    return _fs_call(t, lambda: _new_handle(t, al.from_path(t.system.files, full), alias_p))


@trap
def ResolveAlias(t, from_file, alias, target, was_changed):
    """The target, and the record brought up to date if it had moved; if
    it isn't there but the directory it was in is, where it would be, and
    fnfErr."""
    t.mem.w8(was_changed, False)

    def resolve():
        a, base = _record(t, alias), _node(t, from_file)
        found = al.resolve(t.system.files, a, base)
        _put_found(t, target, found)
        if found.node is None:
            return fm.fnfErr
        fresh = al.updated(a, found.node, base)
        if fresh is not None:
            err = _replace(t, alias, fresh)
            t.mem.w8(was_changed, err == noErr)
    return _fs_call(t, resolve)


@trap
def UpdateAlias(t, from_file, target, alias, was_changed):
    t.mem.w8(was_changed, False)

    def update():
        a = _record(t, alias)
        fresh = al.updated(a, _target(t, target), _node(t, from_file))
        if fresh is not None:
            err = _replace(t, alias, fresh)
            t.mem.w8(was_changed, err == noErr)
            return err
    return _fs_call(t, update)


@trap
def GetAliasInfo(t, alias, index, the_string):
    def get():
        t.set_str255(the_string, to_mac(al.info(_record(t, alias), index))[:255])
    return _fs_call(t, get)


@trap
def MatchAlias(t, from_file, rules, alias, count_p, list_p, needs_update_p, filter_, data):
    """The places the record could mean, as many as *count_p, each offered
    to the filter, pascal Boolean fn(CInfoPBPtr, Boolean *quit, Ptr data),
    which says TRUE to leave one out."""
    room = t.mem.r16s(count_p)
    t.w16(count_p, 0)
    t.mem.w8(needs_update_p, False)
    nodes: list[fm.Node] = []
    a = base = None

    def match():
        nonlocal a, base, nodes
        a, base = _record(t, alias), _node(t, from_file)
        nodes = al.candidates(t.system.files, a, base, bool(rules & kARMSearchRelFirst))
    err = _fs_call(t, match)
    if err != noErr:
        return err
    if filter_:
        nodes = _filtered(t, nodes, filter_, data)
    nodes = nodes[:max(room, 0)]
    if not nodes:
        return fm.fnfErr
    with t.system.lock:
        for i, node in enumerate(nodes):
            _put_found(t, list_p + 70 * i, al.Found.of(node))
        t.mem.w8(needs_update_p, al.updated(a, nodes[0], base) is not None)
    t.w16(count_p, len(nodes))
    return noErr


def _filtered(t, nodes, filter_, data):
    heap = t.process.heap
    pb = heap.new_ptr(CINFO_PB + 256 + 2, clear=True)
    if not pb:
        return nodes
    name, quit_p = pb + CINFO_PB, pb + CINFO_PB + 256
    kept = []
    try:
        for node in nodes:
            with t.system.lock:
                t.mem.write(pb, bytes(CINFO_PB))
                t.mem.w32(pb + 18, name)                # ioNamePtr
                t.mem.w_pstr(name, to_mac(node.name)[:255])
                t.mem.w16(pb + 22, node.volume.vref)    # ioVRefNum
                put_cat_info(t, pb, node)
            if not t.call(filter_, (("p", pb), ("p", quit_p), ("p", data)), "b"):
                kept.append(node)
            if t.mem.r8(quit_p):
                break
    finally:
        heap.dispose_ptr(pb)
    return kept


# =========================================================================
# Alias files: a file with isAlias set in its Finder flags, whose resource
# fork has the record, 'alis' 0, made relative to the alias file.
# =========================================================================
def _alias_in(node: fm.File) -> al.Alias:
    try:
        fork = open_fork(bytes(node.fork(True)))
    except InvalidForkError:
        raise FileError(fm.fnfErr) from None
    e = fork.find(b"alis", 0) or next((e for e in fork.entries if e.type == b"alis"), None)
    if e is None:
        raise FileError(fm.fnfErr)
    return al.Alias.parse(fork.raw(e))


@trap
def ResolveAliasFile(t, spec, resolve_chains, is_folder_p, was_aliased_p):
    """What a file or folder is, and if it's an alias file, what it's an
    alias of, in its place: with resolve_chains, followed until that isn't
    an alias file."""
    def resolve():
        node = _target(t, spec)
        aliased = False
        for _ in range(MAX_CHAIN):
            if node.is_dir or not node.flags & isAlias:
                break
            found = al.resolve(t.system.files, _alias_in(node), node)
            if found.node is None:
                raise FileError(fm.fnfErr)
            node, aliased = found.node, True
            _put_found(t, spec, found)
            if not resolve_chains:
                break
        else:
            if not node.is_dir and node.flags & isAlias:
                raise FileError(fm.fnfErr)
        t.mem.w8(is_folder_p, node.is_dir)
        t.mem.w8(was_aliased_p, aliased)
    t.mem.w8(was_aliased_p, False)
    return _fs_call(t, resolve)


# =========================================================================
# The Apple Event Manager's coercions between an alias and an FSSpec
# =========================================================================
typeAlias, typeFSS = b"alis", b"fss "


def _fss(vref: int, parid: int, name: str) -> bytes:
    n = to_mac(name)[:63]
    return struct.pack(">hIB", vref, parid, len(n)) + n.ljust(63, b"\0")


def _alias_to_fss(t, data: bytes) -> bytes | None:
    with t.system.lock:
        try:
            found = al.resolve(t.system.files, al.Alias.parse(data))
        except FileError:
            return None
        if found.node is None:
            return None
        return _fss(found.volume.vref, found.parid, found.name)


def _fss_to_alias(t, data: bytes) -> bytes | None:
    if len(data) < 7:
        return None
    vref, parid = struct.unpack_from(">hI", data)
    name = from_mac(data[7:7 + min(data[6], len(data) - 7)])
    with t.system.lock:
        try:
            return al.make(_files(t).lookup(vref, parid, name)).pack()
        except FileError:
            return None


glue_aevents.COERCIONS[(typeAlias, typeFSS)] = _alias_to_fss
glue_aevents.COERCIONS[(typeFSS, typeAlias)] = _fss_to_alias
