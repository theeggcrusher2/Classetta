"""The Alias Manager: records in System 7's format, and finding their
targets again after they've moved, from Python and through the traps."""

import struct

from guest import Guest

from macrt.m68k import glue_aevents
from macrt.resfork import Resource, build_fork
from macrt.toolbox import aliases as al
from macrt.toolbox import files as fm


def disk(t):
    """The boot volume, with a Folder holding a Doc, and an Other folder."""
    files, boot = t.process.files, t.system.files.boot
    folder = files.create(boot.vref, fm.ROOT, "Folder", directory=True)
    doc = files.create(boot.vref, folder.cnid, "Doc")
    doc.type, doc.creator = b"TEXT", b"MACR"
    other = files.create(boot.vref, fm.ROOT, "Other", directory=True)
    return boot, folder, doc, other


def spec(t, vref, parid, name):
    p = t.ptr(bytes(70))
    t.mem.w16(p, vref)
    t.mem.w32(p + 2, parid)
    t.mem.w_pstr(p + 6, name.encode("mac_roman"))
    return p


def read_spec(t, p):
    return t.mem.r16s(p), t.mem.r32(p + 2), t.str255(p + 6)


def record(t, h):
    return t.contents(h)


def test_a_record_is_in_system_7s_format(qapp):
    t = Guest()
    boot, folder, doc, _ = disk(t)
    data = al.make(doc).pack()
    assert struct.unpack_from(">HHH", data, 4) == (len(data), 2, al.kFile)
    assert data[10:11 + len("Macintosh HD")] == b"\x0cMacintosh HD"
    assert struct.unpack_from(">I2sHI", data, 38) == (boot.crdate, b"BD", 0, folder.cnid)
    assert data[50:54] == b"\x03Doc"
    assert struct.unpack_from(">II4s4shh", data, 114) == (
        doc.cnid, doc.crdate, b"TEXT", b"MACR", -1, -1)
    extras, at = [], 150
    while True:
        tag, n = struct.unpack_from(">HH", data, at)
        if tag == 0xFFFF:
            break
        extras.append((tag, data[at + 4:at + 4 + n]))
        at += 4 + n + (n & 1)
    assert extras == [(0, b"Folder"), (1, struct.pack(">I", folder.cnid)),
                      (2, b"Macintosh HD:Folder:Doc")]
    assert at + 4 == len(data)
    assert al.Alias.parse(data) == al.make(doc)
    minimal = al.make(doc, minimal=True)
    assert minimal.minimal and minimal.cnid == 0 and len(minimal.pack()) == 154


def test_new_alias_resolves_and_follows_its_target(qapp):
    t = Guest()
    boot, folder, doc, other = disk(t)
    files = t.process.files
    alias_p, changed, target = t.ptr(bytes(4)), t.ptr(bytes(2)), t.ptr(bytes(70))
    assert t("NewAlias", 0, spec(t, boot.vref, folder.cnid, "Doc"), alias_p) == 0
    h = t.mem.r32(alias_p)
    assert t("ResolveAlias", 0, h, target, changed) == 0
    assert read_spec(t, target) == (boot.vref, folder.cnid, "Doc")
    assert t.mem.r8(changed) == 0

    files.rename(boot.vref, folder.cnid, "Doc", "Renamed")
    files.move(boot.vref, folder.cnid, "Renamed", other.cnid, None)
    assert t("ResolveAlias", 0, h, target, changed) == 0     # by its file number
    assert read_spec(t, target) == (boot.vref, other.cnid, "Renamed")
    assert t.mem.r8(changed) == 1
    assert al.Alias.parse(record(t, h)).path == "Macintosh HD:Other:Renamed"
    assert t("ResolveAlias", 0, h, target, changed) == 0
    assert t.mem.r8(changed) == 0                           # up to date now

    files.delete(boot.vref, other.cnid, "Renamed")
    assert t("ResolveAlias", 0, h, target, changed) == fm.fnfErr
    assert read_spec(t, target) == (boot.vref, other.cnid, "Renamed")   # where it was


def test_a_file_number_is_believed_only_if_the_name_or_date_agrees(qapp):
    t = Guest()
    boot, folder, doc, other = disk(t)
    a = al.make(doc)
    a.parid, a.extras = 999, {}
    imposter = t.process.files.create(boot.vref, other.cnid, "Imposter")
    imposter.crdate = a.crdate + 60
    a.cnid = imposter.cnid
    assert al.candidates(t.system.files, a) == []
    imposter.crdate = a.crdate
    assert al.candidates(t.system.files, a) == [imposter]


def test_a_relative_alias_is_found_from_the_file_it_was_made_for(qapp):
    t = Guest()
    boot, folder, doc, other = disk(t)
    files = t.process.files
    sub = files.create(boot.vref, folder.cnid, "Sub", directory=True)
    target = files.create(boot.vref, sub.cnid, "Target")
    a = al.make(target, doc)
    assert (a.levels_from, a.levels_to) == (1, 2)           # up to Folder, then Sub:Target
    a.volume, a.vol_crdate, a.cnid, a.parid = "Elsewhere", 1, 0, 0  # as if copied to another disk
    assert al.resolve(t.system.files, a, doc).node is target
    assert al.candidates(t.system.files, a) == []


def test_minimal_aliases_and_full_paths(qapp):
    t = Guest()
    boot, folder, doc, other = disk(t)
    alias_p, changed, target = t.ptr(bytes(4)), t.ptr(bytes(2)), t.ptr(bytes(70))
    assert t("NewAliasMinimal", spec(t, boot.vref, folder.cnid, "Doc"), alias_p) == 0
    h = t.mem.r32(alias_p)
    assert t("ResolveAlias", 0, h, target, changed) == 0
    assert read_spec(t, target) == (boot.vref, folder.cnid, "Doc")

    path = b"Macintosh HD:Other:Later"
    assert t("NewAliasMinimalFromFullPath", len(path), t.ptr(path), 0, 0, alias_p) == 0
    h = t.mem.r32(alias_p)
    assert t("ResolveAlias", 0, h, target, changed) == fm.fnfErr   # not there yet
    assert read_spec(t, target) == (boot.vref, other.cnid, "Later")
    t.process.files.create(boot.vref, other.cnid, "Later")
    assert t("ResolveAlias", 0, h, target, changed) == 0

    path = b"Nowhere:Doc"
    assert t("NewAliasMinimalFromFullPath", len(path), t.ptr(path), 0, 0, alias_p) == 0
    assert t("ResolveAlias", 0, t.mem.r32(alias_p), target, changed) == fm.nsvErr


def test_get_alias_info(qapp):
    t = Guest()
    boot, folder, doc, _ = disk(t)
    sub = t.process.files.create(boot.vref, folder.cnid, "Sub", directory=True)
    h = t.handle(al.make(t.process.files.create(boot.vref, sub.cnid, "Deep")).pack())
    s = t.ptr(bytes(256))
    names = []
    for index in (-3, -2, -1, 0, 1, 2, 3):
        assert t("GetAliasInfo", h, index, s) == 0
        names.append(t.str255(s))
    assert names == ["", "", "Macintosh HD", "Deep", "Sub", "Folder", ""]
    assert t("GetAliasInfo", h, -4, s) == fm.paramErr


def test_match_alias_offers_each_place_to_the_filter(qapp):
    t = Guest()
    boot, folder, doc, other = disk(t)
    files = t.process.files
    moved = files.create(boot.vref, other.cnid, "Doc")
    a = al.make(doc)
    a.cnid = moved.cnid
    moved.crdate = a.crdate                                 # so its number finds it too
    h = t.handle(a.pack())
    count, found, needs = t.ptr(struct.pack(">h", 4)), t.ptr(bytes(4 * 70)), t.ptr(bytes(2))
    assert t("MatchAlias", 0, 0, h, count, found, needs, 0, 0) == 0
    assert t.mem.r16(count) == 2
    assert read_spec(t, found) == (boot.vref, folder.cnid, "Doc")
    assert read_spec(t, found + 70) == (boot.vref, other.cnid, "Doc")
    assert t.mem.r8(needs) == 1                             # that Doc has another number

    seen = []

    def leave_out_the_first(pb, quit_p, data):
        seen.append((t.str255(t.mem.r32(pb + 18)), t.mem.r32(pb + 100), data))
        return len(seen) == 1
    t.procs[0x5000] = leave_out_the_first
    t.mem.w16(count, 4)
    assert t("MatchAlias", 0, 0, h, count, found, needs, 0x5000, 1234) == 0
    assert seen == [("Doc", folder.cnid, 1234), ("Doc", other.cnid, 1234)]
    assert t.mem.r16(count) == 1
    assert read_spec(t, found) == (boot.vref, other.cnid, "Doc")
    assert t.mem.r8(needs) == 1                             # and this one is somewhere else

    t.mem.w16(count, 4)
    t.procs[0x5000] = lambda pb, quit_p, data: True
    assert t("MatchAlias", 0, 0, h, count, found, needs, 0x5000, 0) == fm.fnfErr
    assert t.mem.r16(count) == 0


def alias_file(t, d, name, target):
    """A Finder alias file in d for target."""
    f = t.process.files.create(d.volume.vref, d.cnid, name)
    f.flags = 0x8000
    f.fork(True)[:] = build_fork([Resource(b"alis", 0, al.make(target, f).pack())])
    return f


def test_resolve_alias_file_follows_finder_alias_files(qapp):
    t = Guest()
    boot, folder, doc, other = disk(t)
    alias_file(t, other, "Doc alias", doc)
    alias_file(t, boot.root, "Doc alias alias", other.child("Doc alias"))
    alias_file(t, boot.root, "Folder alias", folder)
    folder_p, aliased = t.ptr(bytes(2)), t.ptr(bytes(2))

    p = spec(t, boot.vref, fm.ROOT, "Doc alias alias")
    assert t("ResolveAliasFile", p, 1, folder_p, aliased) == 0
    assert read_spec(t, p) == (boot.vref, folder.cnid, "Doc")
    assert (t.mem.r8(folder_p), t.mem.r8(aliased)) == (0, 1)

    p = spec(t, boot.vref, fm.ROOT, "Doc alias alias")
    assert t("ResolveAliasFile", p, 0, folder_p, aliased) == 0     # one step only
    assert read_spec(t, p) == (boot.vref, other.cnid, "Doc alias")

    p = spec(t, boot.vref, fm.ROOT, "Folder alias")
    assert t("ResolveAliasFile", p, 1, folder_p, aliased) == 0
    assert read_spec(t, p) == (boot.vref, fm.ROOT, "Folder")
    assert (t.mem.r8(folder_p), t.mem.r8(aliased)) == (1, 1)

    p = spec(t, boot.vref, folder.cnid, "Doc")
    assert t("ResolveAliasFile", p, 1, folder_p, aliased) == 0     # not an alias file
    assert read_spec(t, p) == (boot.vref, folder.cnid, "Doc")
    assert t.mem.r8(aliased) == 0

    t.process.files.delete(boot.vref, folder.cnid, "Doc")
    p = spec(t, boot.vref, other.cnid, "Doc alias")
    assert t("ResolveAliasFile", p, 1, folder_p, aliased) == fm.fnfErr


def test_apple_events_coerce_an_alias_to_an_fsspec_and_back(qapp):
    t = Guest()
    boot, folder, doc, _ = disk(t)
    got = glue_aevents.coerce(t, b"alis", al.make(doc).pack(), b"fss ")
    fss = struct.pack(">hIB", boot.vref, folder.cnid, 3) + b"Doc".ljust(63, b"\0")
    assert got == (b"fss ", fss)
    dtype, data = glue_aevents.coerce(t, *got, b"alis")
    assert dtype == b"alis" and al.Alias.parse(data) == al.make(doc)
