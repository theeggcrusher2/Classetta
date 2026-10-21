"""The File Manager and Standard File: the model from Python, the traps
from 68k code, and the dialogs driven as the user would drive them."""

import struct

import machfs
import pytest
from PyQt6.QtTest import QTest

from apps.build import code_resources, macbinary
from builders import GCR, ditl, dlog, pstr
from macrt.m68k import glue_files as g
from macrt.m68k import glue_sony as sony_
from macrt.m68k.glue_devices import abortErr
from macrt.macfile import MacFile, MemoryFolder, load_file, open_application
from macrt.process import Process
from macrt.resfork import Resource, build_fork, open_fork
from macrt.toolbox import constants as k
from macrt.toolbox import files as fm
from macrt.toolbox import lowmem
from macrt.toolbox.api import Toolbox
from macrt.toolbox.events import EventQueue
from macrt.toolbox.files import FileError, FileManager
from macrt.toolbox.system import System
from macrt.toolbox.types import Point

GLOBALS = 4096


# =========================================================================
# Just enough 68k, assembled by hand.  Operands are offsets from A5, where
# the tests put parameter blocks, names and results.
# =========================================================================
class Asm:
    def __init__(self):
        self.code = bytearray()
        self.labels = {}
        self.fixups = []

    def w(self, *words):
        for x in words:
            self.code += struct.pack(">H", x & 0xFFFF)

    def long(self, v):
        self.code += struct.pack(">I", v & 0xFFFFFFFF)

    def label(self, name):
        self.labels[name] = len(self.code)

    def _to(self, name):
        self.fixups.append((len(self.code), name))
        self.w(0)

    def pb(self, trap, pb, result, selector=None):
        """LEA pb(A5),A0; [MOVE.W #selector,D0]; the trap; MOVE.W D0,result(A5)."""
        if selector is not None:
            self.w(0x303C, selector)
        self.w(0x41ED, pb, trap, 0x3B40, result)

    def copy_w(self, src, dst):                     # MOVE.W src(A5),dst(A5)
        self.w(0x3B6D, src, dst)

    def copy_l(self, src, dst):                     # MOVE.L src(A5),dst(A5)
        self.w(0x2B6D, src, dst)

    def result(self):                               # CLR.W -(SP)
        self.w(0x4267)

    def push_w(self, v):                            # MOVE.W #v,-(SP)
        self.w(0x3F3C, v)

    def push_l(self, v):                            # MOVE.L #v,-(SP)
        self.w(0x2F3C)
        self.long(v)

    def pea(self, off):                             # PEA off(A5)
        self.w(0x486D, off)

    def pop_w(self, off):                           # MOVE.W (SP)+,off(A5)
        self.w(0x3B5F, off)

    def call(self, trap, selector=None, d0=None):
        """A Pascal call: selector on the stack, or in D0."""
        if selector is not None:
            self.push_w(selector)
        if d0 is not None:
            self.w(0x303C, d0)
        self.w(trap)

    def lea_pc(self, name, an):                     # LEA name(PC),An
        self.w(0x41FA | an << 9)
        self._to(name)

    def store_a(self, an, off):                     # MOVE.L An,off(A5)
        self.w(0x2B48 | an, off)

    def push_a(self, an):                           # MOVE.L An,-(SP)
        self.w(0x2F08 | an)

    def mark(self, off, v=1):                       # MOVE.W #v,off(A5)
        self.w(0x3B7C, v, off)

    def bra(self, name, op=0x6000):                 # BRA.W name, or op's Bcc.W
        self.w(op)
        self._to(name)

    def rts(self):
        self.w(0x4E75)

    def assemble(self) -> bytes:
        code = bytearray(self.code)
        for at, name in self.fixups:
            struct.pack_into(">h", code, at, self.labels[name] - at)
        return bytes(code)


def ostype(s: bytes) -> int:
    return struct.unpack(">I", s)[0]


# Where things go below A5
DONE, COUNT, CRES = -2, -4, -6
LONG = -16
TYPES = -32


def R(i):
    return -128 + 2 * i


def BUF(i):
    return -900 + 64 * i


def NAME(i):
    return -2000 + 64 * i


def PB(i):
    return -4096 + 128 * i


def app_rsrc(asm: Asm, extra=()) -> bytes:
    return build_fork(code_resources(asm.assemble(), GLOBALS) + list(extra))


def host_app(folder, asm: Asm, extra=()) -> MacFile:
    """The application, as a MacBinary file in a host folder, with more
    resources if there are any."""
    path = folder / "Test.bin"
    path.write_bytes(macbinary("Test", b"", app_rsrc(asm, extra), b"APPL", b"TEST"))
    return open_application(path)


class Guest:
    """A 68k application, started without a display; it runs to the end
    of its code when run() is called."""

    def __init__(self, app: MacFile, system=None):
        self.system = system or System()
        self.proc = Process(self.system, app.name, None, EventQueue(lambda: (Point(0, 0), 0)),
                            app=app)
        self.mem = self.system.mem
        self.a5 = self.proc.partition.a5

    def at(self, off):
        return self.a5 + off

    def pstr(self, off, text):
        self.mem.w_pstr(self.at(off), text.encode("mac_roman"))

    def pb(self, i, name=None, name_at=None, **fields):
        """Parameter block i, cleared, with a name (in NAME(name_at or i))
        and fields, by their offsets' names in glue_files."""
        base = self.at(PB(i))
        self.mem.fill(base, 128)
        if name is not None:
            n = NAME(i if name_at is None else name_at)
            self.pstr(n, name)
            self.mem.w32(base + g.ioNamePtr, self.at(n))
        for field, value in fields.items():
            at = base + getattr(g, field)
            if field in ("ioPermssn",):
                self.mem.w8(at, value)
            elif field in ("ioBuffer", "ioReqCount", "ioPosOffset", "ioDirID", "ioMisc",
                           "ioCompletion", "ioNamePtr"):
                self.mem.w32(at, value)
            else:
                self.mem.w16(at, value)
        return base

    def run(self, toolbox=None):
        self.proc.runner.main(toolbox)
        assert dict(self.proc.runner.unimplemented_calls) == {}
        assert self.r16(DONE) == 1, "the code didn't run to its end"

    def r16(self, off):
        return self.mem.r16s(self.at(off))

    def result(self, i):
        return self.r16(R(i))

    def field16(self, i, field):
        return self.mem.r16s(self.at(PB(i)) + getattr(g, field))

    def field32(self, i, field):
        return self.mem.r32(self.at(PB(i)) + getattr(g, field))

    def text(self, off):
        return self.mem.r_pstr(self.at(off)).decode("mac_roman")

    def close(self):
        self.proc.close()


# =========================================================================
# The model
# =========================================================================
def test_the_boot_volume(qapp):
    s = System()
    fs = s.files
    boot = fs.boot
    assert (boot.name, boot.vref, boot.drive) == ("Macintosh HD", -1, fm.FIRST_HARD_DISK)
    assert boot.driver == -33                       # a SCSI disk's, ID 0
    assert boot.root.cnid == fm.ROOT and boot.root.parid == fm.ROOT_PARENT
    assert boot.blessed == fs.system_folder.cnid
    assert isinstance(fs.system_folder.child("System"), fm.File)
    prefs = fs.special_folder(boot, b"pref", create=False)
    assert prefs.parent is fs.system_folder and prefs.name == "Preferences"
    trash = fs.special_folder(boot, b"trsh", create=False)
    assert trash.parent is boot.root and trash.flags & fm.kIsInvisible
    assert s.mem.r32(lowmem.VCBQHdr + 2) == boot.vcb
    assert s.mem.r32(lowmem.FCBSPtr) == fs.fcb_buffer


def test_files_in_memory_read_and_write(qapp):
    s = System()
    folder = MemoryFolder([MacFile("Old", data=b"old data", type=b"TEXT", creator=b"ttxt")])
    files = FileManager(s.files, None, s.files.dir_for(folder))
    files.create(0, 0, "Notes")
    p = files.open(0, 0, "Notes", False, fm.fsRdWrPerm)
    assert (p.refnum - fm.FCB_START) % fm.FCB_LEN == 0
    assert files.write(p.refnum, b"Hello, Mac!", fm.fsFromStart, 0) == 11
    assert files.read(p.refnum, 5, fm.fsFromStart, 7) == (b"Mac!", fm.eofErr)
    assert files.read(p.refnum, 50, fm.fsFromStart | fm.NEWLINE_MODE | ord(",") << 8, 0) == (
        b"Hello,", fm.noErr)
    assert files.set_fpos(p.refnum, fm.fsFromLEOF, -4) == (7, fm.noErr)
    assert files.set_fpos(p.refnum, fm.fsFromMark, 10) == (11, fm.eofErr)
    with pytest.raises(FileError) as e:
        files.set_fpos(p.refnum, fm.fsFromStart, -1)
    assert e.value.code == fm.posErr
    files.write(p.refnum, b"!", fm.fsFromStart, 13)     # past the end: zeros between
    assert files.get_eof(p.refnum) == 14
    files.set_eof(p.refnum, 5)
    files.close(p.refnum)
    saved = folder.find("Notes")
    assert saved is not None and saved.data == b"Hello"
    old = files.lookup_file(0, 0, "Old")
    assert (old.type, old.creator) == (b"TEXT", b"ttxt")


def test_permissions_and_locks(qapp):
    s = System()
    files = FileManager(s.files, None, s.files.dir_for(MemoryFolder([])))
    files.create(0, 0, "F")
    writer = files.open(0, 0, "F", False, fm.fsRdWrPerm)
    with pytest.raises(FileError) as e:
        files.open(0, 0, "F", False, fm.fsWrPerm)
    assert (e.value.code, e.value.refnum) == (fm.opWrErr, writer.refnum)
    reader = files.open(0, 0, "F", False, fm.fsCurPerm)        # someone's writing: read only
    assert not reader.write
    with pytest.raises(FileError) as e:
        files.write(reader.refnum, b"x")
    assert e.value.code == fm.wrPermErr
    rsrc = files.open(0, 0, "F", True, fm.fsRdWrPerm)          # the other fork is separate
    assert rsrc.write
    for p in (writer, reader, rsrc):
        files.close(p.refnum)
    files.set_lock(0, 0, "F", True)
    with pytest.raises(FileError) as e:
        files.open(0, 0, "F", False, fm.fsRdWrPerm)
    assert e.value.code == fm.permErr
    with pytest.raises(FileError) as e:
        files.delete(0, 0, "F")
    assert e.value.code == fm.fLckdErr
    files.set_lock(0, 0, "F", False)
    files.delete(0, 0, "F")
    with pytest.raises(FileError) as e:
        files.lookup_file(0, 0, "F")
    assert e.value.code == fm.fnfErr


def test_paths_working_directories_and_the_search_path(qapp):
    s = System()
    fs = s.files
    files = FileManager(fs, None, fs.boot.root)
    a = files.create(0, 0, "A", directory=True)
    files.create(0, a.cnid, "Inner")
    assert files.lookup(0, 0, ":A:Inner").parent is a
    assert files.lookup(0, 0, "Macintosh HD:A:Inner").parent is a
    wd = files.open_wd(0, a.cnid, None, fm.ERIK)
    assert wd <= fm.WD_FIRST + fm.WD_LEN * fm.MAX_WDS and wd < -32000
    assert files.open_wd(0, a.cnid, None, fm.ERIK) == wd        # the same one again
    assert files.open_wd(0, fm.ROOT, None, fm.ERIK) == fs.boot.vref   # a root is its volume
    assert files.lookup(wd, 0, "Inner").name == "Inner"
    assert files.lookup(wd, 0, "::A").cnid == a.cnid
    files.set_vol(wd, None)
    assert files.get_vol()[1:] == (wd, a.cnid)
    # A file that isn't in the default directory is looked for in the System Folder.
    assert files.lookup_file(0, 0, "System", search=True).parent is fs.system_folder
    with pytest.raises(FileError):
        files.lookup_file(0, 0, "System")
    files.set_vol(fs.boot.vref, None)
    files.close_wd(wd)
    assert wd not in fs.wds


def test_a_shared_working_directory_outlives_the_application_that_opened_it(qapp):
    """An application launched from another's folder starts with the same
    working directory; the first quitting doesn't take it away."""
    s = System()
    fs = s.files
    folder = FileManager(fs, None, fs.boot.root).create(0, 0, "Games", directory=True)
    launcher = FileManager(fs, None, folder)
    game = FileManager(fs, None, folder)
    wd = game.get_vol()[1]
    assert launcher.get_vol()[1] == wd
    launcher.close_all()
    assert game.lookup(wd, 0, None) is folder
    game.close_all()
    assert wd not in fs.wds


def test_host_files_are_written_as_load_file_reads_them(qapp, tmp_path):
    (tmp_path / "Old.bin").write_bytes(macbinary("Old", b"data", b"", b"TEXT", b"ttxt"))
    (tmp_path / "notes.txt").write_bytes(b"plain")
    s = System()
    files = FileManager(s.files, None, s.files.dir_for(load_file(tmp_path / "notes.txt").folder))
    # A new file: the data fork as the file, the rest beside it.
    node = files.create(0, 0, "New")
    node.type, node.creator = b"TEXT", b"MACR"
    files.metadata_changed(node)
    for rsrc, data in ((False, b"some text"), (True, build_fork([]))):
        p = files.open(0, 0, "New", rsrc, fm.fsRdWrPerm)
        files.write(p.refnum, data)
        files.close(p.refnum)
    f = load_file(tmp_path / "New")
    assert (f.data, f.type, f.creator) == (b"some text", b"TEXT", b"MACR")
    assert open_fork(f.rsrc) is not None
    # A MacBinary file stays one.
    old = files.lookup_file(0, 0, "Old")
    assert (old.type, old.creator) == (b"TEXT", b"ttxt")
    p = files.open(0, 0, "Old", False, fm.fsRdWrPerm)
    files.write(p.refnum, b"DATA!", fm.fsFromStart, 0)
    files.close(p.refnum)
    f = load_file(tmp_path / "Old.bin")
    assert (f.name, f.data, f.type) == ("Old", b"DATA!", b"TEXT")
    # A host file with no Finder information is a text file by its extension.
    assert files.lookup_file(0, 0, "notes.txt").type == b"TEXT"
    # Renaming, moving and deleting follow on the host.
    d = files.create(0, 0, "Folder", directory=True)
    files.rename(0, 0, "New", "Renamed")
    files.move(0, 0, "Renamed", d.cnid, None)
    assert load_file(tmp_path / "Folder" / "Renamed").type == b"TEXT"
    assert not (tmp_path / "New").exists() and not (tmp_path / "._New").exists()
    files.rename(0, 0, "Old", "Older")
    assert load_file(tmp_path / "Older.bin").name == "Older"
    files.delete(0, d.cnid, "Renamed")
    files.delete(0, 0, "Folder")
    assert not (tmp_path / "Folder").exists()


def test_disk_images_change_only_in_memory(qapp, tmp_path):
    v = machfs.Volume()
    v.name = "Test Disk"
    f = machfs.File()
    f.type, f.creator, f.data = b"TEXT", b"ttxt", b"Read me first"
    v["Read Me"] = f
    image = v.write(800 * 1024, desktopdb=False, bootable=False)
    path = tmp_path / "disk.img"
    path.write_bytes(image)
    from macrt.macfile import VolumeFolder, load_volume
    s = System()
    folder = VolumeFolder(load_volume(path.read_bytes()))
    files = FileManager(s.files, None, s.files.dir_for(folder))
    vol = files.get_vol()[0]
    assert (vol.name, vol.vref) == ("Test Disk", -2)
    assert (vol.drive, vol.driver) == (1, fm.SONY)  # a floppy disk's: in the internal drive
    # Each drive is in the drive queue, the flags before its element: The
    # Duel walks it to tell a floppy (a disk in place) from a hard disk (8).
    m, drives, q = s.mem, [], s.mem.r32(lowmem.DrvQHdr + 2)
    while q:
        drives.append((m.r8(q - 3), m.r16s(q + 6), m.r16s(q + 8),
                       m.r16(q + 12) | m.r16(q + 14) << 16))
        q = m.r32(q)
    assert drives == [(8, fm.FIRST_HARD_DISK, -33, fm.VOLUME_BLOCKS * fm.BLOCK // 512),
                      (2, 1, fm.SONY, 1600)]
    assert m.r32(lowmem.DrvQHdr + 6) == vol.dqe
    p = files.open(0, 0, "Read Me", False, fm.fsRdWrPerm)
    files.write(p.refnum, b"Changed")
    files.close(p.refnum)
    files.create(0, 0, "New")
    assert path.read_bytes() == image
    assert bytes(files.lookup_file(0, 0, "Read Me").fork(False)) == b"Changed first"
    assert [n.name for n in vol.root.items()] == ["New", "Read Me"]


def test_an_application_on_another_disk_by_its_full_pathname(qapp, tmp_path):
    """Falcon's launcher Launches "Falcon 2:Falcon": the file is found on
    the other disk, and macfile_of gives the application to start."""
    from macrt.macfile import VolumeFolder, load_volume
    v = machfs.Volume()
    v.name = "Second"
    v["Game"] = machfs.Folder()
    f = machfs.File()
    f.type, f.creator, f.rsrc = b"APPL", b"GAME", build_fork([Resource(b"STR ", 1, b"\3hi!")])
    v["Game"]["Play"] = f
    s = System()
    s.files.mount_folder(VolumeFolder(load_volume(v.write(800 * 1024, desktopdb=False,
                                                          bootable=False))))
    files = FileManager(s.files, None, s.files.dir_for(s.files.boot.root))
    app = s.files.macfile_of(files.lookup_file(0, 0, "Second:Game:Play"))
    assert (app.name, app.type, app.creator) == ("Play", b"APPL", b"GAME")
    assert open_fork(app.rsrc).find(b"STR ", 1) is not None
    with pytest.raises(FileError):
        files.lookup_file(0, 0, "Second:Play")


def test_an_mfs_floppy(qapp):
    from builders import mfs_image
    from macrt.macfile import VolumeFolder, load_volume
    image = mfs_image("System Disk", [
        ("System", b"ZSYS", b"MACS", b"", build_fork([]), 0, (0, 0)),
        ("Finder", b"FNDR", b"MACS", b"", build_fork([]), 0, (20, 30)),
        ("Note Pad File", b"TEXT", b"MACA", b"Remember", b"", 0, (100, 200)),
    ])
    s = System()
    files = FileManager(s.files, None, s.files.dir_for(VolumeFolder(load_volume(image))))
    vol = files.get_vol()[0]
    # Flat, as MFS is; seen through HFS calls, its top is the System Folder.
    assert vol.name == "System Disk" and vol.blessed == fm.ROOT
    assert [n.name for n in vol.root.items()] == ["Finder", "Note Pad File", "System"]
    note = files.lookup_file(0, 0, "Note Pad File")
    assert bytes(note.fork(False)) == b"Remember" and note.type == b"TEXT"
    assert struct.unpack_from(">hh", note.finfo, 10) == (100, 200)
    # Its System Folder is searched for what isn't on it, then the boot
    # volume's, whose System is the one in use: MacWrite's printer.
    assert files.lookup_file(0, 0, "macrt Printer", search=True).parent is s.files.system_folder
    with pytest.raises(FileError):
        files.lookup_file(0, 0, "macrt Printer")


def test_what_a_program_from_1984_asks_of_its_floppy(qapp, tmp_path):
    """Word 1.0 looks for bit 6 in its key file's ioFlAttrib, which only
    the file's MFS directory entry has; takes the volume's block counts
    for INTEGERs; and reads a long at $AFC that nothing documents."""
    from builders import mfs_image
    a = Asm()
    for i in range(3):
        a.pb(0xA00C, PB(i), R(i))                   # PBGetFInfo
    a.pb(0xA007, PB(3), R(3))                       # PBGetVInfo
    a.w(0x2B78, lowmem.Unnamed_AFC, LONG)           # MOVE.L $AFC.W,LONG(A5)
    a.pb(0xA013, PB(4), R(4))                       # PBFlushVol, a garbage name,
    a.pb(0xA013, PB(5), R(5))                       # a file's,
    a.pb(0xA013, PB(6), R(6))                       # and a volume's that isn't there
    a.mark(DONE)
    a.rts()
    path = tmp_path / "Master.img"
    path.write_bytes(mfs_image("Master", [
        ("Test", b"APPL", b"TEST", b"", app_rsrc(a), 0, (0, 0)),
        ("Key", b"KEN ", b"SHAP", b"", b"", 0, (0, 0), 0xC0),
        ("Locked", b"TEXT", b"ttxt", b"", b"", 0, (0, 0), 0x81),
    ]))
    t = Guest(open_application(path, "Test"))
    t.pb(0, "Key")
    t.pb(1, "Locked")
    t.pb(2, "Test")
    t.pb(3, "", ioVolIndex=0)
    t.pb(4, ioNamePtr=0xB45354)                     # MacWrite's, quitting: no memory there
    t.pb(5, "Some Document")                        # MacWrite's, saving it
    t.pb(6, "Nowhere:")
    t.run()
    assert [t.result(i) for i in range(7)] == [0] * 6 + [fm.nsvErr]
    attrib = [t.mem.r8(t.at(PB(i)) + g.ioFlAttrib) for i in range(3)]
    assert attrib == [0x40, 0x01, 0x84]             # the program's resource fork is open
    assert t.text(NAME(3)) == "Master"
    assert t.field16(3, "ioVNmAlBlks") > t.field16(3, "ioVFrBlk") > 0
    assert t.mem.r32(t.at(LONG)) == 0xFFFFFFFF
    assert t.mem.r16(lowmem.MonkeyLives) == 0xFFFF  # MacWrite prints and quits only then
    t.close()


def test_file_and_resource_manager_share_one_fcb_table(qapp):
    s = System()
    folder = MemoryFolder([MacFile("Res", rsrc=build_fork([]), type=b"rsrc")])
    vref = s.files.dir_for(folder).volume.vref
    data = s.file_manager.open(vref, fm.ROOT, "Res", False, fm.fsRdPerm)
    rm = s.resources
    res = rm.HOpenResFile(vref, fm.ROOT, "Res", fm.fsRdPerm)
    assert res > 0 and res != data.refnum
    assert s.fcbs[res].rsrc and not s.fcbs[data.refnum].rsrc
    assert s.mem.r32(s.mem.r32(lowmem.FCBSPtr) + res) == s.fcbs[res].node.cnid
    rm.CloseResFile(res)
    assert res not in s.fcbs and data.refnum in s.fcbs


# =========================================================================
# The traps, from 68k code
# =========================================================================
def test_parameter_block_calls(qapp, tmp_path):
    a = Asm()
    a.pb(0xA208, PB(0), R(0))                       # PBHCreate
    a.pb(0xA208, PB(0), R(1))                       # again: dupFNErr
    a.pb(0xA200, PB(1), R(2))                       # PBHOpen
    for i in (2, 3, 4, 5, 6, 7):
        a.copy_w(PB(1) + g.ioRefNum, PB(i) + g.ioRefNum)
    a.pb(0xA003, PB(2), R(3))                       # PBWrite
    a.pb(0xA011, PB(5), R(4))                       # PBGetEOF
    a.pb(0xA002, PB(3), R(5))                       # PBRead, past the end: eofErr
    a.lea_pc("completion", 1)
    a.store_a(1, PB(4) + g.ioCompletion)
    a.pb(0xA402, PB(4), R(6))                       # PBReadAsync
    a.pb(0xA002, PB(6), R(7))                       # PBRead, newline mode
    a.pb(0xA012, PB(7), R(8))                       # PBSetEOF
    a.pb(0xA260, PB(7), R(13), selector=0x10)       # PBLockRange
    a.pb(0xA260, PB(7), R(14), selector=0x11)       # PBUnlockRange
    a.pb(0xA001, PB(5), R(9))                       # PBClose
    a.pb(0xA001, PB(5), R(10))                      # again: rfNumErr
    a.pb(0xA260, PB(7), R(15), selector=0x10)       # PBLockRange: rfNumErr
    a.pb(0xA20C, PB(8), R(11))                      # PBHGetFInfo
    a.pb(0xA200, PB(9), R(12))                      # PBHOpen, no such file
    a.mark(DONE)
    a.rts()
    a.label("completion")
    a.w(0x526D, COUNT)                              # ADDQ.W #1,COUNT(A5)
    a.w(0x3B40, CRES)                               # MOVE.W D0,CRES(A5)
    a.rts()

    t = Guest(host_app(tmp_path, a))
    t.pb(0, "Notes")
    t.pb(1, "Notes", ioPermssn=fm.fsRdWrPerm)
    t.pstr(BUF(0), "Hello, Mac!")
    t.pb(2, ioBuffer=t.at(BUF(0)) + 1, ioReqCount=11, ioPosMode=fm.fsFromStart)
    t.pb(3, ioBuffer=t.at(BUF(1)), ioReqCount=20, ioPosMode=fm.fsFromStart)
    t.pb(4, ioBuffer=t.at(BUF(2)), ioReqCount=4, ioPosMode=fm.fsFromStart, ioPosOffset=7)
    t.pb(5)
    t.pb(6, ioBuffer=t.at(BUF(3)), ioReqCount=20,
         ioPosMode=fm.fsFromStart | fm.NEWLINE_MODE | ord(",") << 8)
    t.pb(7, ioMisc=5, ioReqCount=5, ioPosMode=fm.fsFromStart)
    t.pb(8, "Notes")
    t.pb(9, "Missing", ioPermssn=fm.fsRdPerm)
    t.run()

    assert [t.result(i) for i in range(16)] == [
        0, fm.dupFNErr, 0, 0, 0, fm.eofErr, 0, 0, 0, 0, fm.rfNumErr, 0, fm.fnfErr,
        0, 0, fm.rfNumErr]
    assert (t.field16(0, "ioResult"), t.field16(1, "ioResult")) == (fm.dupFNErr, 0)
    assert t.field16(1, "ioRefNum") > 0
    assert (t.field32(2, "ioActCount"), t.field32(2, "ioPosOffset")) == (11, 11)
    assert t.field32(5, "ioMisc") == 11
    assert t.field32(3, "ioActCount") == 11
    assert t.mem.read(t.at(BUF(1)), 11) == b"Hello, Mac!"
    assert t.mem.read(t.at(BUF(2)), 4) == b"Mac!"
    assert (t.r16(COUNT), t.r16(CRES)) == (1, 0)     # the completion routine ran
    assert t.field32(6, "ioActCount") == 6
    assert t.field32(8, "ioFlLgLen") == 5
    assert t.field16(9, "ioRefNum") == 0
    t.close()
    assert load_file(tmp_path / "Notes").data == b"Hello"


def test_the_sound_driver_is_there(qapp, tmp_path):
    """A free-form sound with a count of 0 doesn't go anywhere: it plays
    until KillIO stops it, and its completion routine gets abortErr.
    KillIO writes only the result: StopSound's glue passes a parameter
    block whose first fields are its own stack frame."""
    a = Asm()
    a.lea_pc("completion", 1)
    a.store_a(1, PB(0) + g.ioCompletion)
    a.pb(0xA403, PB(0), R(0))                       # PBWriteAsync: a sound
    a.pb(0xA004, PB(1), R(1))                       # PBControl
    a.pb(0xA206, PB(4), R(2))                       # PBKillIO, immediately, as the glue does
    a.pb(0xA005, PB(1), R(3))                       # PBStatus
    a.pb(0xA004, PB(2), R(4))                       # PBControl, to no driver
    a.pb(0xA004, PB(3), R(5))                       # and to a file's refnum
    a.mark(DONE)
    a.rts()
    a.label("completion")
    a.w(0x526D, COUNT)                              # ADDQ.W #1,COUNT(A5)
    a.w(0x3B40, CRES)                               # MOVE.W D0,CRES(A5)
    a.rts()

    t = Guest(host_app(tmp_path, a))
    t.pb(0, ioRefNum=g.SOUND_DRIVER, ioBuffer=t.at(BUF(0)), ioReqCount=740)
    t.pb(1, ioRefNum=g.SOUND_DRIVER)
    t.pb(2, ioRefNum=-40)
    t.pb(3, ioRefNum=3)
    frame = t.pb(4, ioRefNum=g.SOUND_DRIVER, ioCompletion=0x00012345)
    t.mem.w32(frame, 0x5555AAAA)
    t.mem.w32(frame + 4, 0x6666AAAA)
    t.mem.w32(frame + 8, 0x7777AAAA)
    t.run()
    assert [t.result(i) for i in range(6)] == [
        0, 0, 0, g.statusErr, g.unitEmptyErr, g.badUnitErr]
    assert [t.mem.r32(frame + i) for i in range(0, 16, 4)] == [
        0x5555AAAA, 0x6666AAAA, 0x7777AAAA, 0x00012345]
    assert t.field32(0, "ioActCount") == 0          # none of it played
    assert t.field16(0, "ioResult") == abortErr
    assert (t.r16(COUNT), t.r16(CRES)) == (1, abortErr)
    t.close()


# Below the usual globals: room for sectors, in an application with more.
BIG = -8192


def _floppy_app(a: Asm) -> bytes:
    return build_fork(code_resources(a.assemble(), 2 * GLOBALS))


def test_the_floppy_disk_driver_reads_and_writes_sectors_and_their_tags(qapp, tmp_path):
    """Copy protection and disk tools read a floppy by sector, through
    .Sony, the tags the File Manager wrote with them too."""
    from builders import mfs_image
    a = Asm()
    a.pb(0xA004, PB(0), R(0))                       # Control: where the tags go
    a.pb(0xA002, PB(1), R(1))                       # Read sectors 2 and 3
    a.pb(0xA002, PB(2), R(2))                       # and 5, from the mark
    a.pb(0xA003, PB(3), R(3))                       # Write 2's to 700, with 5's tag
    a.pb(0xA005, PB(4), R(4))                       # Status: the drive's
    for i in range(5, 12):
        a.pb((0xA002, 0xA002, 0xA002, 0xA004, 0xA005, 0xA006, 0xA004)[i - 5], PB(i), R(i))
    a.mark(DONE)
    a.rts()
    image = mfs_image("Floppy", [("Test", b"APPL", b"TEST", b"", _floppy_app(a), 0, (0, 0))])
    sectors = [image[n:n + 512] for n in range(0, len(image), 512)]
    path = tmp_path / "Floppy.dsk"                  # each sector's tag after it
    path.write_bytes(b"".join(d + b"tag %8d" % n for n, d in enumerate(sectors)))
    t = Guest(open_application(path, "Test"))
    sony = dict(ioRefNum=fm.SONY, ioVRefNum=1)
    t.pb(0, csCode=8, **sony)
    t.mem.w32(t.at(PB(0)) + g.csParam, t.at(BUF(0)))
    t.pb(1, ioBuffer=t.at(BIG), ioReqCount=1024, ioPosMode=1, ioPosOffset=1024, **sony)
    t.pb(2, ioBuffer=t.at(BIG + 1024), ioReqCount=512, ioPosMode=3, ioPosOffset=512, **sony)
    t.pb(3, ioBuffer=t.at(BIG), ioReqCount=512, ioPosMode=1, ioPosOffset=700 * 512, **sony)
    t.pb(4, csCode=8, **sony)
    t.pb(5, ioRefNum=fm.SONY, ioVRefNum=2, ioReqCount=512, ioPosMode=1)
    t.pb(6, ioRefNum=fm.SONY, ioVRefNum=3, ioReqCount=512, ioPosMode=1)
    t.pb(7, ioBuffer=t.at(BIG), ioReqCount=512, ioPosMode=1, ioPosOffset=100, **sony)
    t.pb(8, csCode=99, **sony)
    t.pb(9, csCode=1, **sony)
    t.pb(10, **sony)
    t.pb(11, csCode=5, **sony)                      # verify
    t.run()
    assert [t.result(i) for i in range(12)] == [
        0, 0, 0, 0, 0, sony_.offLinErr, sony_.nsDrvErr, fm.paramErr, sony_.controlErr,
        sony_.statusErr, 0, 0]
    assert t.mem.read(t.at(BIG), 1536) == image[1024:2048] + sectors[5]
    assert t.field32(1, "ioActCount") == 1024 and t.field32(2, "ioActCount") == 512
    # The last sector's tag in TagData, and those of the call's in the buffer.
    assert t.mem.read(lowmem.TagData + 2, 12) == b"tag        5"
    assert t.mem.read(t.at(BUF(0)), 24) == b"tag        5tag        3"
    disk = next(v for v in t.system.files.volumes if v.floppy).store.volume.disk
    assert disk.sectors[700 * 512:701 * 512] == image[1024:1536]
    assert disk.tags[700 * 12:701 * 12] == b"tag        5"
    track, prot, there, installed, sides, *queue, two, flush, errs = struct.unpack_from(
        ">hbbbbIhhhhbbh", t.mem.read(t.at(PB(4)) + g.csParam, 22))
    # A mounted disk, which the driver has read: diskInPlace 2.
    assert (there, installed, sides, queue[2:4], two) == (2, 1, -1, [1, fm.SONY], 0)
    t.close()


def test_a_programs_own_rddata_reads_the_disk_itself(qapp, tmp_path):
    """Excel 1.0 puts its own routine in RdData and reads its key disk's
    bytes after a sector's address field from the IWM, as the ROM's driver
    reads a data field there, with interrupts masked."""
    from builders import SYNC, gcr_sector, mfs_image, moof_image
    a = Asm()
    a.w(0x31FC, 0, lowmem.ApplScratch)              # MOVE.W #0,ApplScratch: what it returns
    a.pb(0xA002, PB(0), R(0))                       # Read sector 4: no routine of its own yet
    a.pb(0xA002, PB(1), R(1))                       # 10, whose address field isn't there
    a.pb(0xA002, PB(2), R(2))                       # 11, its data field damaged
    a.w(0x3B78, lowmem.DskErr, COUNT)               # MOVE.W DskErr,COUNT(A5)
    a.lea_pc("routine", 1)
    a.w(0x21C9, lowmem.RdData)                      # MOVE.L A1,RdData
    a.pb(0xA002, PB(3), R(3))                       # Read 4 and 5 through it
    a.w(0x40F8, lowmem.ApplScratch + 4)             # MOVE SR,ApplScratch+4: unmasked again
    a.w(0x31FC, -72, lowmem.ApplScratch)
    a.pb(0xA002, PB(4), R(4))                       # Read 6: the routine says it's damaged
    a.pb(0xA002, PB(5), R(5))                       # 10: there's nothing to call it for
    a.w(0x42B8, lowmem.RdData)                      # CLR.L RdData
    a.pb(0xA003, PB(6), R(6))                       # Write 11 over
    a.pb(0xA002, PB(7), R(7))                       # and it reads back
    a.mark(DONE)
    a.rts()
    a.label("routine")
    a.w(0x20C9, 0x20CC, 0x20CD, 0x20CE)             # MOVE.L A1/A4/A5/A6,(A0)+
    a.w(0x10D5)                                     # MOVE.B (A5),(A0)+: the VIA's port A
    a.w(0x720F)                                     # MOVEQ #15,D1
    a.w(0x1014, 0x6AFC)                             # MOVE.B (A4),D0; BPL.S *-2
    a.w(0x10C0, 0x51C9, 0xFFF8)                     # MOVE.B D0,(A0)+; DBRA D1,*-6
    a.w(0x20CB)                                     # MOVE.L A3,(A0)+
    a.w(0x40D8)                                     # MOVE SR,(A0)+
    a.w(0x3038, lowmem.ApplScratch)                 # MOVE.W ApplScratch,D0
    a.rts()
    image = mfs_image("Key Disk", [("Test", b"APPL", b"TEST", b"", _floppy_app(a), 0, (0, 0))])
    broken = gcr_sector(0, 0, 11, image[11 * 512:12 * 512])
    at = len(broken) - len(SYNC) - 200 * 8
    broken = broken[:at] + "10101010" + broken[at + 8:]
    track0 = "".join(gcr_sector(0, 0, s, image[s * 512:(s + 1) * 512]) for s in range(10))
    path = tmp_path / "Key Disk.moof"
    path.write_bytes(moof_image(image, tracks={(0, 0): track0 + broken}))
    t = Guest(open_application(path, "Test"))
    sony = dict(ioRefNum=fm.SONY, ioVRefNum=1, ioPosMode=1)
    reads = [(4, 1), (10, 1), (11, 1), (4, 2), (6, 2), (10, 1), (11, 1), (11, 1)]
    for i, (sector, count) in enumerate(reads):
        t.pb(i, ioBuffer=t.at(BIG + 1024 * i), ioReqCount=512 * count,
             ioPosOffset=512 * sector, **sony)
    t.mem.w32(t.at(PB(6)) + g.ioBuffer, t.at(BIG))  # sector 4's, as read
    t.run()
    assert [t.result(i) for i in range(8)] == [
        0, sony_.sectNFErr, sony_.badDCksum, 0, -72, sony_.sectNFErr, 0, 0]
    assert t.r16(COUNT) == sony_.badDCksum          # DskErr
    assert [t.field32(i, "ioActCount") for i in range(6)] == [512, 0, 0, 1024, 0, 0]
    assert t.mem.read(t.at(BIG), 512) == image[4 * 512:5 * 512]
    assert t.mem.read(t.at(BIG + 7 * 1024), 512) == image[4 * 512:5 * 512]
    for n in (0, 1):                                # sectors 4 and 5, as the routine read them
        got = t.mem.read(t.at(BIG + 3 * 1024) + 512 * n, 39)
        marks, iwm, via, scc = struct.unpack_from(">IIII", got)
        assert t.mem.read(marks, 6) == b"\xd5\xaa\xad\xde\xaa\xff"
        assert (iwm, via, scc) == (0xDFE1FF + 12 * 512, 0xEFFFFE, sony_.SCC_A_DATA)
        assert got[16] & 0x80                       # no serial data waiting
        assert got[17:33] == b"\xff" * 5 + b"\xd5\xaa\xad" + bytes([GCR[4 + n]]) + got[26:33]
        # A3: the six bits each disk byte stands for, as MacGammon's routine decodes them.
        six = t.mem.read(struct.unpack_from(">I", got, 33)[0], 0x100)
        assert [six[b] for b in GCR] == list(range(64))
        assert all(six[b] == 0xFF for b in range(0x96, 0x100) if b not in GCR)
        assert got[37] & 0x07 == 0x07               # SR: the Disk Driver masks interrupts
    assert not t.mem.r16(lowmem.ApplScratch + 4) & 0x0700
    t.close()


def test_the_roms_rddata_reads_a_data_field_with_the_marks_its_given(qapp, tmp_path):
    """Dollars & Sense's key disk has a sector whose data field has marks
    of its own: its RdData points A1 at them and goes on to the ROM's,
    which reads the field after them, or checks it against the buffer
    while DskVerify is set."""
    from builders import gcr_sector, mfs_image, moof_image
    a = Asm()
    a.w(0x21F8, lowmem.RdData, lowmem.ApplScratch)  # MOVE.L RdData,ApplScratch: the ROM's
    a.lea_pc("routine", 1)
    a.w(0x21C9, lowmem.RdData)                      # MOVE.L A1,RdData
    a.pb(0xA002, PB(0), R(0))                       # Read 236: its own marks
    a.pb(0xA002, PB(1), R(1))                       # 237: the usual ones
    a.pb(0xA002, PB(2), R(2))                       # 238: its own, but the usual ones after
    a.w(0x50F8, lowmem.DskVerify)                   # ST DskVerify
    a.pb(0xA002, PB(3), R(3))                       # 236 against what was read
    a.pb(0xA002, PB(4), R(4))                       # and against zeros
    a.w(0x4238, lowmem.DskVerify)                   # CLR.B DskVerify
    a.pb(0xA002, PB(5), R(5))                       # 236 again
    a.mark(DONE)
    a.rts()
    a.label("routine")
    a.lea_pc("marks", 1)
    a.w(0x2F38, lowmem.ApplScratch, 0x4E75)         # MOVE.L ApplScratch,-(SP); RTS
    a.label("marks")
    a.w(0xD5AA, 0xABDE, 0xAB00)
    image = mfs_image("Key Disk", [("Test", b"APPL", b"TEST", b"", _floppy_app(a), 0, (0, 0))])
    key, other = bytes(range(256)) * 2, b"\x5a" * 512
    own = dict(marks=b"\xd5\xaa\xab", end=b"\xde\xab")
    track20 = (gcr_sector(20, 0, 0, key, b"the key sect", **own)
               + gcr_sector(20, 0, 1, other)
               + gcr_sector(20, 0, 2, other, marks=own["marks"]))
    path = tmp_path / "Key Disk.moof"
    path.write_bytes(moof_image(image, tracks={(20, 0): track20}))
    t = Guest(open_application(path, "Test"))
    sony = dict(ioRefNum=fm.SONY, ioVRefNum=1, ioPosMode=1, ioReqCount=512)
    for i, sector in enumerate((236, 237, 238, 236, 236, 236)):
        t.pb(i, ioBuffer=t.at(BIG + 512 * i), ioPosOffset=512 * sector, **sony)
    t.mem.w32(t.at(PB(3)) + g.ioBuffer, t.at(BIG))  # where 236 was read to
    t.run()
    assert [t.result(i) for i in range(6)] == [
        0, sony_.noDtaMkErr, sony_.badDBtSlp, 0, sony_.dataVerErr, 0]
    assert [t.field32(i, "ioActCount") for i in range(6)] == [512, 0, 0, 512, 0, 512]
    assert t.mem.read(t.at(BIG), 512) == key
    assert t.mem.read(t.at(BIG + 512 * 4), 512) == bytes(512)  # only compared with
    assert t.mem.read(t.at(BIG + 512 * 5), 512) == key
    assert t.mem.read(lowmem.TagData + 2, 12) == b"the key sect"
    t.close()


def test_the_file_manager_reads_a_files_sectors_through_a_programs_rddata(qapp, tmp_path):
    """Mac Attack reads its serial number from a file whose sector has
    data marks of its own, with its own RdData pointing A1 at them: the
    File Manager reads a file on a floppy through the disk driver, so a
    read that the ROM's routine can't make, the program's can.  One the
    driver can't make is ioErr, the driver's error in DskErr: King's
    Quest counts on that."""
    from builders import gcr_sector, mfs_image, moof_image
    from macrt import moof
    from macrt.macfile import load_volume
    a = Asm()
    a.w(0x21F8, lowmem.RdData, lowmem.ApplScratch)  # MOVE.L RdData,ApplScratch: the ROM's
    a.pb(0xA000, PB(0), R(0))                       # Open "Key"
    a.copy_w(PB(0) + g.ioRefNum, PB(1) + g.ioRefNum)
    a.copy_w(PB(0) + g.ioRefNum, PB(2) + g.ioRefNum)
    a.pb(0xA002, PB(1), R(1))                       # Read it: its marks aren't the usual ones
    a.w(0x3B78, lowmem.DskErr, COUNT)               # MOVE.W DskErr,COUNT(A5)
    a.lea_pc("routine", 1)
    a.w(0x21C9, lowmem.RdData)                      # MOVE.L A1,RdData
    a.pb(0xA002, PB(2), R(2))                       # Read it through the routine
    a.mark(DONE)
    a.rts()
    a.label("routine")
    a.lea_pc("marks", 1)
    a.w(0x2F38, lowmem.ApplScratch, 0x4E75)         # MOVE.L ApplScratch,-(SP); RTS
    a.label("marks")
    a.w(0xD5AA, 0xABDE, 0xAB00)
    key = b"\x1bSerial Number  1000020218.x"
    image = mfs_image("Key Disk", [("Test", b"APPL", b"TEST", b"", _floppy_app(a), 0, (0, 0)),
                                   ("Key", b"TEXT", b"TEST", key, b"", 0, (0, 0))])
    sector = next(node.sectors[0][0] for path, node in load_volume(image).iter_paths()
                  if path[-1] == "Key")
    track = first = 0                               # the track it's on, and its first sector
    while first + moof.sectors_on(track) <= sector:
        first += moof.sectors_on(track)
        track += 1
    own = dict(marks=b"\xd5\xaa\xab", end=b"\xde\xab")
    bits = "".join(gcr_sector(track, 0, s, image[(first + s) * 512:][:512],
                              **(own if first + s == sector else {}))
                   for s in range(moof.sectors_on(track)))
    path = tmp_path / "Key Disk.moof"
    path.write_bytes(moof_image(image, tracks={(track, 0): bits}))
    t = Guest(open_application(path, "Test"))
    t.pb(0, name="Key")
    for i in (1, 2):
        t.pb(i, ioBuffer=t.at(BUF(i)), ioReqCount=len(key), ioPosMode=1)
    t.run()
    assert [t.result(i) for i in range(3)] == [0, fm.ioErr, 0]
    assert t.r16(COUNT) == sony_.noDtaMkErr         # DskErr
    assert t.mem.read(t.at(BUF(2)), len(key)) == key
    t.close()


def test_a_whole_sector_a_file_read_wants_is_read_straight_to_its_buffer(qapp, tmp_path):
    """The File Manager reads each whole sector of a file that a read
    wants straight to where it's going, and part of one through a buffer
    of its own: Uninvited's RdData is in the buffer it reads the first
    sector of its file to, and leaves there the code it decrypts."""
    from builders import mfs_image, moof_image
    a = Asm()
    a.w(0x21F8, lowmem.RdData, lowmem.ApplScratch)  # MOVE.L RdData,ApplScratch: the ROM's
    a.w(0x41ED, BUF(0), 0x21C8, lowmem.ApplScratch + 4)    # where A0 goes each time
    a.pb(0xA000, PB(0), R(0))                       # Open "Data"
    a.copy_w(PB(0) + g.ioRefNum, PB(1) + g.ioRefNum)
    a.lea_pc("routine", 1)
    a.w(0x21C9, lowmem.RdData)                      # MOVE.L A1,RdData
    a.pb(0xA002, PB(1), R(1))                       # Read 1000 bytes of it from 100 on
    a.mark(DONE)
    a.rts()
    a.label("routine")
    a.w(0x2F0A,                                     # MOVE.L A2,-(SP)
        0x2478, lowmem.ApplScratch + 4,             # MOVEA.L ApplScratch+4,A2
        0x24C8,                                     # MOVE.L A0,(A2)+
        0x21CA, lowmem.ApplScratch + 4,             # MOVE.L A2,ApplScratch+4
        0x245F,                                     # MOVEA.L (SP)+,A2
        0x2F38, lowmem.ApplScratch, 0x4E75)         # MOVE.L ApplScratch,-(SP); RTS
    data = bytes(range(256)) * 5
    image = mfs_image("Disk", [("Test", b"APPL", b"TEST", b"", _floppy_app(a), 0, (0, 0)),
                               ("Data", b"TEXT", b"TEST", data, b"", 0, (0, 0))])
    path = tmp_path / "Disk.moof"
    path.write_bytes(moof_image(image))
    t = Guest(open_application(path, "Test"))
    t.pb(0, name="Data")
    t.pb(1, ioBuffer=t.at(BIG), ioReqCount=1000, ioPosMode=1, ioPosOffset=100)
    t.run()
    assert [t.result(i) for i in range(2)] == [0, 0]
    assert t.mem.read(t.at(BIG), 1000) == data[100:1100]
    first, whole, last = (t.mem.r32(t.at(BUF(0)) + 4 * i) for i in range(3))
    assert whole == t.at(BIG) + 412                 # the second sector, all of it
    assert first == last and not t.at(BIG) <= first < t.at(BIG) + 1000
    t.close()


def test_a_call_for_an_offline_volume_mounts_its_disk_and_starts_again(qapp, tmp_path):
    """A call for a volume whose VCB is in the queue but in no drive puts
    its disk in: Uninvited queues a VCB of its own for its second disk,
    under the disk's name, and patches MountVol to copy the vRefNum the
    disk is mounted under into its call, which is then made again."""
    from builders import mfs_image
    from macrt.macfile import VolumeFolder, load_volume
    vcb = BIG
    a = Asm()
    a.pb(0xA00F, PB(1), R(1))                       # MountVol: drive 2's is online
    a.pb(0xA00F, PB(2), R(2))                       # and drive 9 there's none of
    a.w(0x700F, 0xA146)                             # MOVEQ #$0F,D0; _GetTrapAddress
    a.w(0x21C8, lowmem.ApplScratch)                 # MOVE.L A0,ApplScratch
    a.lea_pc("patch", 0)
    a.w(0x700F, 0xA047)                             # MOVEQ #$0F,D0; _SetTrapAddress
    a.w(0x41ED, vcb, 0x43F8, lowmem.VCBQHdr, 0xA96F)    # Enqueue the VCB
    a.pb(0xA00C, PB(0), R(0))                       # GetFInfo "Doc" on it
    a.mark(DONE)
    a.rts()
    a.label("patch")
    a.w(0x526D, COUNT,                              # ADDQ.W #1,COUNT(A5)
        0x2F08,                                     # MOVE.L A0,-(SP)
        0x2278, lowmem.ApplScratch, 0x4E91,         # MOVEA.L ApplScratch,A1; JSR (A1)
        0x205F,                                     # MOVEA.L (SP)+,A0
        0x3B68, g.ioVRefNum, PB(0) + g.ioVRefNum,   # MOVE.W ioVRefNum(A0),ioVRefNum(PB0)
        0x4E75)
    first = tmp_path / "First.dsk"
    first.write_bytes(mfs_image("First", [("Test", b"APPL", b"TEST", b"", _floppy_app(a),
                                           0, (0, 0))]))
    second = mfs_image("Second", [("Doc", b"TEXT", b"TEST", b"text", b"", 0, (0, 0))])
    t = Guest(open_application(first, "Test"))
    vol = t.system.files.mount_folder(VolumeFolder(load_volume(second)))
    t.mem.fill(t.at(vcb), fm.VCB_LEN)
    t.mem.w16(t.at(vcb) + g.vcbVRefNum, -127)
    t.mem.w_pstr(t.at(vcb) + g.vcbVN, b"Second")
    t.pb(0, name="Doc", ioVRefNum=-127)
    t.pb(1, ioVRefNum=vol.drive)
    t.pb(2, ioVRefNum=9)
    t.run()
    assert [t.result(i) for i in range(3)] == [0, fm.volOnLinErr, fm.nsDrvErr]
    assert t.field16(0, "ioVRefNum") == vol.vref and t.r16(COUNT) == 1
    assert t.mem.read(t.at(PB(0)) + g.ioFlFndrInfo, 8) == b"TEXTTEST"
    t.close()


def test_an_ejected_disk_is_put_back_and_its_drive_posts_a_disk_inserted_event(
        qapp, tmp_path, monkeypatch):
    """Seven Cities of Gold ejects its disk, waits for one with GetOSEvent
    and mounts it.  The drive is empty till the disk is back; GetNextEvent
    mounts the disk of the event it returns itself."""
    from builders import mfs_image
    monkeypatch.setattr(sony_, "OUT_FOR", 2)
    a = Asm()
    a.pb(0xA017, PB(0), R(0))                       # Eject drive 1
    a.pb(0xA005, PB(1), R(1))                       # Status: the drive's empty
    a.pb(0xA00F, PB(2), R(2))                       # MountVol: there's nothing to mount
    a.label("os")
    a.w(0x303C, 0x80, 0x41ED, BUF(0), 0xA031)       # GetOSEvent(diskMask, BUF0)
    a.bra("os", 0x6600)                             # BNE: till there's one
    a.pb(0xA005, PB(3), R(3))                       # Status: the disk's back
    a.pb(0xA00F, PB(4), R(4))                       # MountVol
    a.pb(0xA00F, PB(5), R(5))                       # and again: it's online
    a.pb(0xA005, PB(8), R(8))                       # Status: the driver has read it
    a.pb(0xA017, PB(0), R(6))                       # Eject it again
    a.label("gne")
    a.result()
    a.push_w(0x80)
    a.pea(BUF(1))
    a.w(0xA970, 0x4A1F)                             # GetNextEvent; TST.B (SP)+
    a.bra("gne", 0x6700)                            # BEQ: till there's one
    a.pb(0xA00F, PB(7), R(7))                       # it's online: GetNextEvent mounted it
    a.mark(DONE)
    a.rts()
    path = tmp_path / "Floppy.dsk"
    path.write_bytes(mfs_image("Floppy", [("Test", b"APPL", b"TEST", b"", _floppy_app(a),
                                           0, (0, 0))]))
    t = Guest(open_application(path, "Test"))
    vol = next(v for v in t.system.files.volumes if v.floppy)
    t.pb(0, ioVRefNum=vol.vref)
    for i in (1, 3, 8):
        t.pb(i, csCode=8, ioRefNum=fm.SONY, ioVRefNum=1)
    for i in (2, 4, 5, 7):
        t.pb(i, ioVRefNum=1)
    t.run(Toolbox(None, t.proc))
    assert [t.result(i) for i in range(8)] == [
        0, 0, sony_.offLinErr, 0, 0, fm.volOnLinErr, 0, fm.volOnLinErr]
    in_place = [t.mem.r8(t.at(PB(i)) + g.csParam + 3) for i in (1, 3, 8)]
    assert in_place == [0, 1, 2] and t.result(8) == 0
    assert t.field16(4, "ioVRefNum") == vol.vref
    for ev in (BUF(0), BUF(1)):                     # diskEvt: MountVol's result, the drive
        assert (t.r16(ev), t.r16(ev + 2), t.r16(ev + 4)) == (k.diskEvt, 0, 1)
    t.close()


def test_an_unmounted_disk_comes_back_as_the_next_one(qapp, tmp_path, monkeypatch):
    """Wizardry VI ejects its first disk, unmounts it and asks for the
    next: the disk put back is the next image, out of the drive it was in,
    and MountVol mounts it.  The startup volume can't be unmounted."""
    from builders import mfs_image
    from macrt.macfile import VolumeFolder, load_volume
    monkeypatch.setattr(sony_, "OUT_FOR", 2)
    a = Asm()
    a.pb(0xA017, PB(0), R(0))                       # Eject drive 1
    a.pb(0xA00E, PB(1), R(1))                       # UnmountVol drive 1
    a.pb(0xA00E, PB(2), R(2))                       # UnmountVol the startup volume
    a.label("os")
    a.w(0x303C, 0x80, 0x41ED, BUF(0), 0xA031)       # GetOSEvent(diskMask, BUF0)
    a.bra("os", 0x6600)                             # BNE: till there's one
    a.pb(0xA00F, PB(3), R(3))                       # MountVol drive 1
    a.pb(0xA00C, PB(4), R(4))                       # GetFInfo "Doc" on it
    a.pb(0xA00F, PB(5), R(5))                       # MountVol drive 2: the first disk
    a.mark(DONE)
    a.rts()
    first = tmp_path / "First.dsk"
    first.write_bytes(mfs_image("First", [("Test", b"APPL", b"TEST", b"", _floppy_app(a),
                                           0, (0, 0))]))
    second = mfs_image("Second", [("Doc", b"TEXT", b"TEST", b"text", b"", 0, (0, 0))])
    t = Guest(open_application(first, "Test"))
    fs = t.system.files
    one = next(v for v in fs.volumes if v.floppy)
    two = fs.mount_folder(VolumeFolder(load_volume(second)))
    assert (one.drive, two.drive) == (1, 2)
    t.pb(0, ioVRefNum=1)
    t.pb(1, ioVRefNum=1)
    t.pb(2, ioVRefNum=fs.boot.vref)
    t.pb(3, ioVRefNum=1)
    t.pb(4, name="Doc", ioVRefNum=two.vref)
    t.pb(5, ioVRefNum=2)
    t.run(Toolbox(None, t.proc))
    assert [t.result(i) for i in range(6)] == [0, 0, fm.fBsyErr, 0, 0, 0]
    assert (t.field16(3, "ioVRefNum"), t.field16(5, "ioVRefNum")) == (two.vref, one.vref)
    assert (one.drive, two.drive) == (2, 1) and not fs.offline
    assert [t.mem.r16(v.dqe + 6) for v in (one, two)] == [2, 1]
    queue, at = [], t.mem.r32(lowmem.VCBQHdr + 2)
    while at:
        queue.append(at)
        at = t.mem.r32(at)
    assert queue == [fs.boot.vcb, two.vcb, one.vcb] == [v.vcb for v in fs.volumes]
    assert t.mem.r32(lowmem.VCBQHdr + 6) == one.vcb
    t.close()


def test_rdaddr_finds_each_address_field_as_it_comes_round(qapp, tmp_path, monkeypatch):
    """Mac Attack times the disk's turning: it chooses the drive's read
    register with AdrDisk, finds the next address field with RdAddr, steps
    the head in, strobing the drive's registers itself through the IWM,
    and finds the next on the track after.  RdAddr returns once the field
    has gone by, so each call finds the one after, the IWM about to read
    the sector's data field; a side with nothing on it has none."""
    from builders import mfs_image, moof_image
    from macrt.m68k import hardware

    class Clock:                                    # the disk turns only while RdAddr waits
        now = 1000.0

        @classmethod
        def monotonic(cls):
            return cls.now

        @classmethod
        def sleep(cls, seconds):
            cls.now += seconds
    monkeypatch.setattr(hardware, "time", Clock)

    def adr_disk(a, register):                      # MOVEQ #register,D0; JSR via AdrDisk
        a.w(0x7000 | register, 0x2278, lowmem.AdrDisk, 0x4E91)

    def strobe(a):                                  # TST.B LSTRB on, then off
        a.w(0x4A28, 7 * 512, 0x4A28, 6 * 512)

    def rd_addr(a, i):                              # JSR via RdAddr; D0-D4, A3 to BUF(i)
        a.lea_pc("marks", 2)
        a.w(0x2278, lowmem.RdAddr, 0x4E91)
        for n in range(5):
            a.w(0x3B40 | n, BUF(i) + 2 * n)
        a.w(0x2B4B, BUF(i) + 10)

    a = Asm()
    a.pb(0xA002, PB(0), R(0))                       # Read sector 0: the head's on track 0
    adr_disk(a, 1)                                  # the lower head's data
    for i in range(3):
        rd_addr(a, i)
    a.w(0x41ED, BIG, 0x43FA, 0)                     # LEA BIG(A5),A0; LEA marks(PC),A1
    a.fixups.append((len(a.code) - 2, "data"))
    a.w(0x2678, lowmem.RdData, 0x4E93)              # JSR via RdData: the sector's data
    a.w(0x3B40, R(1))
    adr_disk(a, 0)                                  # step in
    strobe(a)
    adr_disk(a, 4)
    strobe(a)
    adr_disk(a, 1)
    rd_addr(a, 3)
    adr_disk(a, 3)                                  # the upper head's: a 400K disk has no side 1
    rd_addr(a, 4)
    a.mark(DONE)
    a.rts()
    a.label("marks")
    a.w(0xD5AA, 0x96DE, 0xAAFF)
    a.label("data")
    a.w(0xD5AA, 0xADDE, 0xAAFF)
    image = mfs_image("Floppy", [("Test", b"APPL", b"TEST", b"", _floppy_app(a), 0, (0, 0))])
    path = tmp_path / "Floppy.moof"
    path.write_bytes(moof_image(image))
    t = Guest(open_application(path, "Test"))
    t.pb(0, ioRefNum=fm.SONY, ioVRefNum=1, ioBuffer=t.at(BIG), ioReqCount=512, ioPosMode=1)
    t.run()
    found = [struct.unpack(">5hL", t.mem.read(t.at(BUF(i)), 14)) for i in range(5)]
    # Each track's sectors are in reverse, and it begins in the middle of
    # sector 11's: from the index, 10 comes round first.  Each field gives
    # its side and its format, a 400K disk's 2, and A3 is the table RdData
    # decodes the disk's bytes through, as it is the ROM's.
    table = t.system.machine.denibble - GCR[0]
    assert found == [(0, 0, 10, 0, 2, table), (0, 0, 9, 0, 2, table), (0, 0, 8, 0, 2, table),
                     (0, 1, 7, 0, 2, table), (sony_.noAdrMkErr, 0, 0, 0, 0, table)]
    assert (t.result(0), t.result(1)) == (0, 0)
    assert t.mem.read(t.at(BIG), 512) == image[8 * 512:9 * 512]
    assert t.system.machine.iwm.drive.track == 1
    t.close()


def test_the_rom_disk_routines_jazz_calls_through_their_vectors(qapp, tmp_path, monkeypatch):
    """Jazz's copy protection calls the ROM's disk routines itself: Seek
    moves the head to D6's track, which RdAddr finds there, and Recal back
    out; SetUpPoll points A4 to A6 at the IWM, the VIA and the SCC and
    keeps the caller's stack in PollStack; WakeUp, MakeSpdTbl and SetSpeed
    succeed."""
    from builders import mfs_image, moof_image
    from macrt.m68k import hardware

    class Clock:
        now = 1000.0

        @classmethod
        def monotonic(cls):
            return cls.now

        @classmethod
        def sleep(cls, seconds):
            cls.now += seconds
    monkeypatch.setattr(hardware, "time", Clock)

    def call(a, vector):                            # MOVEA.L vector,A0; JSR (A0)
        a.w(0x2078, vector, 0x4E90)

    a = Asm()
    a.pb(0xA002, PB(0), R(0))                       # Read sector 0: the drive's on
    a.w(0x7C05)                                     # MOVEQ #5,D6
    call(a, lowmem.Seek)
    a.w(0x3B40, R(1))
    a.w(0x7001, 0x2278, lowmem.AdrDisk, 0x4E91)     # the lower head's data
    a.lea_pc("marks", 2)
    call(a, lowmem.RdAddr)
    a.w(0x3B41, R(2))                               # the track it's on
    for i, vector in enumerate((lowmem.WakeUp, lowmem.MakeSpdTbl, lowmem.SetSpeed)):
        call(a, vector)
        a.w(0x3B40, R(3 + i))
    a.w(0x2E0E, 0x264D)                             # MOVE.L A6,D7; MOVEA.L A5,A3
    call(a, lowmem.SetUpPoll)
    for i in range(4):                              # A4-A7 to BUF(0)
        a.w(0x274C | i, BUF(0) + 4 * i)
    a.w(0x2A4B, 0x2C47)                             # MOVEA.L A3,A5; MOVEA.L D7,A6
    call(a, lowmem.Recal)
    a.w(0x3B40, R(6))
    a.mark(DONE)
    a.rts()
    a.label("marks")
    a.w(0xD5AA, 0x96DE, 0xAAFF)
    image = mfs_image("Floppy", [("Test", b"APPL", b"TEST", b"", _floppy_app(a), 0, (0, 0))])
    path = tmp_path / "Floppy.moof"
    path.write_bytes(moof_image(image))
    t = Guest(open_application(path, "Test"))
    t.pb(0, ioRefNum=fm.SONY, ioVRefNum=1, ioBuffer=t.at(BIG), ioReqCount=512, ioPosMode=1)
    t.run()
    assert [t.result(i) for i in range(7)] == [0, 0, 5, 0, 0, 0, 0]  # RdAddr on track 5
    a4, a5, a6, sp = struct.unpack(">4L", t.mem.read(t.at(BUF(0)), 16))
    assert (a4, a5, a6) == (hardware.IWM_BASE + hardware.Q6_LOW * hardware.STRIDE,
                            hardware.VIA_BASE + hardware.vBufA * hardware.STRIDE,
                            sony_.SCC_A_DATA)
    assert t.mem.r32(lowmem.PollStack) == sp
    assert t.system.machine.iwm.drive.track == 0
    t.close()


def test_wakeup_waits_and_sonyvars_has_the_drives_track(qapp, tmp_path, monkeypatch):
    """Moebius's copy protection finds a sector's address field, steps,
    waits through WakeUp and finds the sector it expects to come round
    next: WakeUp calls back once D0's microseconds have gone by, so the
    disk has turned on.  AdrDisk leaves D1 at where the drive's own
    variables are in SonyVars, and the first of them is the track the
    driver has the head over."""
    from builders import mfs_image, moof_image
    from macrt.m68k import hardware

    class Clock:
        now = 1000.0

        @classmethod
        def monotonic(cls):
            return cls.now

        @classmethod
        def sleep(cls, seconds):
            cls.now += seconds
    monkeypatch.setattr(hardware, "time", Clock)

    def call(a, vector):                            # MOVEA.L vector,A0; JSR (A0)
        a.w(0x2078, vector, 0x4E90)

    def rd_addr(a, i):                              # the sector it finds to R(i)
        a.lea_pc("marks", 2)
        a.w(0x2278, lowmem.RdAddr, 0x4E91)
        a.w(0x3B42, R(i))

    sector = hardware.IWM().drive.turn_time() / 12  # how long a sector on track 0 takes
    wait = round(4.5 * sector * 1e6)
    a = Asm()
    a.pb(0xA002, PB(0), R(0))                       # Read sector 0: the head on track 0
    a.w(0x7001, 0x2278, lowmem.AdrDisk, 0x4E91)     # the lower head's data
    a.w(0x3B41, R(1))
    rd_addr(a, 2)
    a.w(0x203C, wait >> 16, wait & 0xFFFF)          # MOVE.L #wait,D0
    call(a, lowmem.WakeUp)
    rd_addr(a, 3)
    a.w(0x7C05)                                     # MOVEQ #5,D6
    call(a, lowmem.Seek)
    a.mark(DONE)
    a.rts()
    a.label("marks")
    a.w(0xD5AA, 0x96DE, 0xAAFF)
    image = mfs_image("Floppy", [("Test", b"APPL", b"TEST", b"", _floppy_app(a), 0, (0, 0))])
    path = tmp_path / "Floppy.moof"
    path.write_bytes(moof_image(image))
    t = Guest(open_application(path, "Test"))
    t.pb(0, ioRefNum=fm.SONY, ioVRefNum=1, ioBuffer=t.at(BIG), ioReqCount=512, ioPosMode=1)
    t.run()
    sony = t.mem.r32(lowmem.SonyVars) & 0xFFFFFF
    assert t.result(1) == sony_.DRIVE_VARS[1] == 0x4A
    # The sectors come round in reverse: 9, 8, 7 and 6 go by in the wait.
    assert (t.result(2), t.result(3)) == (10, 5)
    assert t.mem.r16(sony + 0x4A) == t.system.machine.iwm.drive.track == 5
    t.close()


def test_the_driver_finds_a_sector_through_a_programs_own_rdaddr(qapp, tmp_path, monkeypatch):
    """Dragonworld puts its own routine in RdAddr as well as RdData: the
    driver calls it, again for each field it says is another sector's,
    two turns' worth at most, and reads on through RdData once it has
    found the sector, or returns what it says went wrong.  Dragonworld's
    says it has found the sector at once."""
    from builders import mfs_image, moof_image
    from macrt.m68k import hardware

    class Clock:
        now = 1000.0

        @classmethod
        def monotonic(cls):
            return cls.now

        @classmethod
        def sleep(cls, seconds):
            cls.now += seconds
    monkeypatch.setattr(hardware, "time", Clock)
    rom, says, calls, marks = (lowmem.ApplScratch + i for i in (0, 4, 6, 8))
    a = Asm()
    a.w(0x21F8, lowmem.RdAddr, rom)                 # MOVE.L RdAddr,rom: the ROM's
    a.w(0x42B8, says)                               # CLR.L says: says and calls
    a.lea_pc("finder", 1)
    a.w(0x21C9, lowmem.RdAddr)                      # MOVE.L A1,RdAddr
    a.pb(0xA002, PB(0), R(0))                       # Read 4: the ROM's finds it, behind its own
    a.w(0x3B78, calls, BUF(0))                      # MOVE.W calls,BUF(0)(A5)
    a.w(0x31FC, 30, says)                           # it finds 30 each time
    a.pb(0xA002, PB(1), R(1))                       # Read 5: never
    a.w(0x3B78, calls, BUF(0) + 2)
    a.w(0x31FC, sony_.noAdrMkErr, says)             # it finds no address field
    a.pb(0xA002, PB(2), R(2))
    a.w(0x31FC, 6, says)                            # it finds 6 at once
    a.lea_pc("reader", 1)
    a.w(0x21C9, lowmem.RdData)                      # MOVE.L A1,RdData
    a.pb(0xA002, PB(3), R(3))                       # Read 6 through its own RdData
    a.mark(DONE)
    a.rts()
    a.label("finder")
    a.w(0x5278, calls,                              # ADDQ.W #1,calls
        0x21CA, marks,                              # MOVE.L A2,marks
        0x3038, says,                               # MOVE.W says,D0
        0x670A,                                     # BEQ.S rom
        0x6B06,                                     # BMI.S error
        0x3400, 0x7000, 0x4E75,                     # MOVE.W D0,D2: found; MOVEQ #0,D0; RTS
        0x4E75,                                     # error: RTS, D0 the error
        0x2F38, rom, 0x4E75)                        # rom: MOVE.L rom,-(SP); RTS
    a.label("reader")
    a.w(0x30FC, 0x1234, 0x7000, 0x4E75)             # MOVE.W #$1234,(A0)+; MOVEQ #0,D0; RTS
    image = mfs_image("Floppy", [("Test", b"APPL", b"TEST", b"", _floppy_app(a), 0, (0, 0))])
    path = tmp_path / "Floppy.moof"
    path.write_bytes(moof_image(image))
    t = Guest(open_application(path, "Test"))
    for i, sector in enumerate((4, 5, 6, 6)):
        t.pb(i, ioRefNum=fm.SONY, ioVRefNum=1, ioBuffer=t.at(BIG + 512 * i), ioReqCount=512,
             ioPosMode=1, ioPosOffset=512 * sector)
    t.run()
    assert [t.result(i) for i in range(4)] == [0, sony_.sectNFErr, sony_.noAdrMkErr, 0]
    assert t.mem.read(t.at(BIG), 512) == image[4 * 512:5 * 512]
    first, then = t.r16(BUF(0)), t.r16(BUF(0) + 2)
    assert 1 <= first <= 12 and then - first == 24  # a turn of track 0 at most; two
    assert t.mem.read(t.mem.r32(marks), 6) == b"\xd5\xaa\x96\xde\xaa\xff"
    assert t.mem.read(t.at(BIG + 512 * 3), 2) == b"\x12\x34"
    t.close()


def test_fsspec_calls_resource_files_and_find_folder(qapp, tmp_path):
    a = Asm()
    a.result()                                      # FSMakeFSSpec(0, 0, "Doc", spec)
    a.push_w(0)
    a.push_l(0)
    a.pea(NAME(0))
    a.pea(BUF(0))
    a.call(0xAA52, d0=1)
    a.pop_w(R(0))
    a.result()                                      # FSpCreate(spec, 'MACR', 'TEXT', 0)
    a.pea(BUF(0))
    a.push_l(ostype(b"MACR"))
    a.push_l(ostype(b"TEXT"))
    a.push_w(0)
    a.call(0xAA52, d0=4)
    a.pop_w(R(1))
    a.pea(BUF(0))                                   # FSpCreateResFile(spec, ...)
    a.push_l(ostype(b"MACR"))
    a.push_l(ostype(b"TEXT"))
    a.push_w(0)
    a.call(0xAA52, d0=14)
    a.result()                                      # ResError
    a.call(0xA9AF)
    a.pop_w(R(2))
    a.result()                                      # FSpOpenResFile(spec, fsRdWrPerm)
    a.pea(BUF(0))
    a.push_w(fm.fsRdWrPerm << 8)
    a.call(0xAA52, d0=13)
    a.pop_w(R(3))
    a.result()                                      # FSpOpenDF(spec, fsRdWrPerm, refnum)
    a.pea(BUF(0))
    a.push_w(fm.fsRdWrPerm << 8)
    a.pea(R(4))
    a.call(0xAA52, d0=2)
    a.pop_w(R(5))
    a.result()                                      # FindFolder(kOnSystemDisk, 'pref', ...)
    a.push_w(fm.kOnSystemDisk)
    a.push_l(ostype(b"pref"))
    a.push_w(1 << 8)
    a.pea(R(6))
    a.pea(LONG)
    a.call(0xA823, d0=0)
    a.pop_w(R(7))
    a.copy_w(R(6), PB(0) + g.ioVRefNum)             # PBGetCatInfo on what it found
    a.copy_l(LONG, PB(0) + g.ioDirID)
    a.pb(0xA260, PB(0), R(8), selector=9)
    a.mark(DONE)
    a.rts()

    t = Guest(host_app(tmp_path, a))
    t.pstr(NAME(0), "Doc")
    t.pb(0, "", name_at=5, ioFDirIndex=-1)
    t.run()
    files = t.proc.files
    vol = files.get_vol()[0]
    assert t.result(0) == fm.fnfErr                 # not there yet, but the record is made
    assert (t.r16(BUF(0)), t.mem.r32(t.at(BUF(0)) + 2), t.text(BUF(0) + 6)) == (
        vol.vref, fm.ROOT, "Doc")
    assert t.result(1) == 0 and t.result(2) == 0 and t.result(5) == 0
    res, data = t.result(3), t.result(4)
    assert res > 0 and data > 0 and res != data
    assert (res - fm.FCB_START) % fm.FCB_LEN == 0 and (data - fm.FCB_START) % fm.FCB_LEN == 0
    prefs = t.system.files.special_folder(t.system.files.boot, b"pref")
    assert (t.result(7), t.result(6), t.mem.r32(t.at(LONG))) == (0, -1, prefs.cnid)
    assert (t.result(8), t.text(NAME(5))) == (0, "Preferences")
    t.close()
    f = load_file(tmp_path / "Doc")
    assert (f.type, f.creator) == (b"TEXT", b"MACR")
    assert open_fork(f.rsrc) is not None


def test_catalog_calls_on_a_disk_image(qapp, tmp_path):
    a = Asm()
    for i in range(4):                              # PBGetCatInfo by index, 1 to 4
        a.pb(0xA260, PB(i), R(i), selector=9)
    a.pb(0xA260, PB(4), R(4), selector=9)           # by name
    a.pb(0xA260, PB(5), R(5), selector=9)           # the directory itself
    a.pb(0xA260, PB(6), R(6), selector=9)           # by partial pathname
    a.pb(0xA208, PB(7), R(7))                       # PBHCreate
    a.pb(0xA200, PB(8), R(8))                       # PBHOpen
    a.copy_w(PB(8) + g.ioRefNum, PB(9) + g.ioRefNum)
    a.copy_w(PB(8) + g.ioRefNum, PB(10) + g.ioRefNum)
    a.pb(0xA003, PB(9), R(9))                       # PBWrite
    a.pb(0xA260, PB(10), R(10), selector=8)         # PBGetFCBInfo
    a.pb(0xA001, PB(9), R(11))                      # PBClose
    a.pb(0xA207, PB(11), R(12))                     # PBHGetVInfo
    a.pb(0xA260, PB(12), R(13), selector=6)         # PBDirCreate
    a.mark(DONE)
    a.rts()

    v = machfs.Volume()
    v.name = "Test Disk"
    app = machfs.File()
    app.type, app.creator, app.rsrc = b"APPL", b"TEST", app_rsrc(a)
    v["Test"] = app
    readme = machfs.File()
    readme.type, readme.creator, readme.data = b"TEXT", b"ttxt", b"Read me first"
    v["Read Me"] = readme
    stuff = machfs.Folder()
    v["Stuff"] = stuff
    inner = machfs.File()
    inner.data = b"inside"
    stuff["Inner"] = inner
    image = v.write(800 * 1024, desktopdb=False, bootable=False)
    path = tmp_path / "disk.img"
    path.write_bytes(image)

    t = Guest(open_application(path, "Test"))
    for i in range(4):
        t.pb(i, "", ioFDirIndex=i + 1)
    t.pb(4, "Stuff")
    t.pb(5, "", ioFDirIndex=-1, ioDirID=fm.ROOT)
    t.pb(6, ":Stuff:Inner")
    t.pb(7, "New File")
    t.pb(8, "New File", ioPermssn=fm.fsRdWrPerm)
    t.pstr(BUF(0), "changed")
    t.pb(9, ioBuffer=t.at(BUF(0)) + 1, ioReqCount=7, ioPosMode=fm.fsFromStart)
    t.pb(10, "", name_at=13)
    t.pb(11, "", ioVolIndex=0)
    t.pb(12, "Made")
    t.run()

    assert [t.result(i) for i in range(4)] == [0, 0, 0, fm.fnfErr]
    assert [t.text(NAME(i)) for i in range(3)] == ["Read Me", "Stuff", "Test"]
    assert t.mem.r8(t.at(PB(1)) + g.ioFlAttrib) & g.ioDirFlg
    assert t.mem.r8(t.at(PB(0)) + g.ioFlAttrib) & g.ioDirFlg == 0
    assert t.mem.read(t.at(PB(0)) + g.ioFlFndrInfo, 8) == b"TEXTttxt"
    assert t.field32(0, "ioFlLgLen") == 13
    stuff_id = t.field32(4, "ioDirID")
    assert t.result(4) == 0 and t.mem.r16(t.at(PB(4)) + g.ioFlStBlk) == 1   # ioDrNmFls
    assert t.result(5) == 0 and t.text(NAME(5)) == "Test Disk"
    assert t.result(6) == 0 and t.field32(6, "ioFlParID") == stuff_id
    assert [t.result(i) for i in range(7, 14)] == [0] * 7
    assert t.text(NAME(13)) == "New File"
    assert t.field32(10, "ioFCBEOF") == 7 and t.field32(10, "ioFCBParID") == fm.ROOT
    assert t.text(NAME(11)) == "Test Disk"
    assert t.field16(11, "ioVRefNum") == t.proc.files.get_vol()[0].vref
    assert (t.field16(11, "ioVDrvInfo"), t.field16(11, "ioVDRefNum")) == (1, fm.SONY)
    assert t.field32(12, "ioDirID") > fm.ROOT
    vol = t.proc.files.get_vol()[0]
    assert bytes(vol.root.child("New File").fork(False)) == b"changed"
    assert vol.root.child("Made").is_dir
    t.close()
    assert path.read_bytes() == image               # the image itself is never written


# =========================================================================
# Standard File, with the display up
# =========================================================================
def _sf_folder(tmp_path):
    (tmp_path / "Letter").write_bytes(b"Dear Mac,")
    (tmp_path / "._Letter").write_bytes(fm._apple_double_bytes({9: b"TEXTttxt" + bytes(24)}))
    (tmp_path / "Picture.pict").write_bytes(bytes(600))
    (tmp_path / "Drafts").mkdir()
    (tmp_path / "Drafts" / "Old Letter.txt").write_bytes(b"Draft")


def _idle(a: Asm):
    """Wait for events forever, so the test can look at the results."""
    a.label("idle")
    a.result()
    a.push_w(0xFFFF)
    a.pea(BUF(9))
    a.push_l(30)
    a.push_l(0)
    a.call(0xA860)                                  # WaitNextEvent
    a.w(0x544F)                                     # ADDQ.W #2,SP
    a.bra("idle")


def _launch(h, app):
    proc = h.d.launch(app.name, None, app=app)
    return proc, proc.system.mem, proc.partition.a5


def _dialog(h, n=1):
    h.wait_until(lambda: len(h.d.file_dialogs) == n and h.d.file_dialogs[-1].isVisible(),
                 timeout=5, what="the Standard File dialog")
    return h.d.file_dialogs[-1]


def test_standard_get_file(harness, tmp_path):
    _sf_folder(tmp_path)
    a = Asm()
    a.w(0x2B7C)                                     # MOVE.L #'TEXT',TYPES(A5)
    a.long(ostype(b"TEXT"))
    a.w(TYPES)
    a.push_l(0)                                     # StandardGetFile(nil, 1, ['TEXT'], reply)
    a.push_w(1)
    a.pea(TYPES)
    a.pea(BUF(0))
    a.call(0xA9EA, selector=6)
    a.mark(DONE)
    _idle(a)
    app = host_app(tmp_path, a)
    h = harness()
    proc, mem, a5 = _launch(h, app)
    dlg = _dialog(h)
    assert dlg.names() == ["Drafts", "Letter"]      # folders, and the TEXT files
    dlg.choose("Drafts")
    h.wait_until(lambda: dlg.names() == ["Old Letter.txt"], what="the folder's contents")
    assert dlg.place.path[:2] == ["Drafts", tmp_path.name]
    dlg.choose("Old Letter.txt")
    h.wait_until(lambda: mem.r16(a5 + DONE) == 1, what="the reply")
    reply = a5 + BUF(0)
    drafts = proc.files.lookup(0, 0, "Drafts")
    assert mem.r8(reply) == 1 and mem.read(reply + 2, 4) == b"TEXT"
    assert (mem.r16s(reply + 6), mem.r32(reply + 8)) == (drafts.volume.vref, drafts.cnid)
    assert mem.r_pstr(reply + 12) == b"Old Letter.txt"
    assert (-proc.world.r16(lowmem.SFSaveDisk), proc.world.r32(lowmem.CurDirStore)) == (
        drafts.volume.vref, drafts.cnid)
    h.wait_until(lambda: not h.d.file_dialogs, what="the dialog to go")
    assert proc.runner.unimplemented_calls == {}


def test_put_file_filters_and_cancelling(harness, tmp_path):
    _sf_folder(tmp_path)
    a = Asm()
    a.push_l(0x00500050)                            # SFPutFile(where, prompt, name, nil, reply)
    a.lea_pc("prompt", 0)
    a.store_a(0, NAME(0))
    a.push_a(0)
    a.lea_pc("name", 0)
    a.store_a(0, NAME(1))
    a.push_a(0)
    a.push_l(0)
    a.pea(BUF(0))
    a.call(0xA9EA, selector=1)
    a.mark(DONE, 1)
    a.lea_pc("filter", 1)                           # StandardGetFile(filter, -1, nil, reply)
    a.push_a(1)
    a.push_w(-1)
    a.push_l(0)
    a.pea(BUF(2))
    a.call(0xA9EA, selector=6)
    a.mark(DONE, 2)
    a.w(0x2F2D, NAME(0))                            # StandardPutFile(prompt, name, reply)
    a.w(0x2F2D, NAME(1))                            # (MOVE.L d16(A5),-(SP))
    a.pea(BUF(4))
    a.call(0xA9EA, selector=5)
    a.mark(DONE, 3)
    _idle(a)
    a.label("filter")                               # TRUE, leave it out, for a file
    a.w(0x225F)                                     # MOVEA.L (SP)+,A1
    a.w(0x205F)                                     # MOVEA.L (SP)+,A0
    a.w(0x0828, 4, g.ioFlAttrib)                    # BTST #4,ioFlAttrib(A0)
    a.w(0x57D7)                                     # SEQ (SP)
    a.w(0x4ED1)                                     # JMP (A1)
    a.label("prompt")
    a.code += b"\x08Save as:\x00"
    a.label("name")
    a.code += b"\x08Untitled\x00"
    app = host_app(tmp_path, a)
    h = harness()
    proc, mem, a5 = _launch(h, app)

    dlg = _dialog(h)
    assert dlg.put and dlg.name.text() == "Untitled" and dlg.prompt.text() == "Save as:"
    assert dlg.names() == ["Drafts", "Letter", "Picture.pict", "Test"]   # its Mac name
    assert [i.enabled for i in dlg.place.items] == [True, False, False, False]
    dlg.save_as("Letter")                           # it's there: replace it
    h.wait_until(lambda: mem.r16(a5 + DONE) == 1, what="SFPutFile's reply")
    reply = a5 + BUF(0)
    assert mem.r8(reply) == 1 and mem.r_pstr(reply + 10) == b"Letter"
    assert mem.r16s(reply + 6) == proc.files.get_vol()[0].vref      # its root: the volume

    dlg = _dialog(h)
    assert dlg.names() == ["Drafts"]                # the filter left the files out
    dlg.cancel()
    h.wait_until(lambda: mem.r16(a5 + DONE) == 2, what="StandardGetFile's reply")
    assert mem.r8(a5 + BUF(2)) == 0

    dlg = _dialog(h)
    dlg.make_folder("Fresh")
    h.wait_until(lambda: dlg.place.path[:1] == ["Fresh"], what="the new folder")
    assert (tmp_path / "Fresh").is_dir()
    dlg.save_as("Doc")
    h.wait_until(lambda: mem.r16(a5 + DONE) == 3, what="StandardPutFile's reply")
    reply = a5 + BUF(4)
    fresh = proc.files.lookup(0, 0, "Fresh")
    assert (mem.r8(reply), mem.r8(reply + 1)) == (1, 0)
    assert (mem.r16s(reply + 6), mem.r32(reply + 8), mem.r_pstr(reply + 12)) == (
        fresh.volume.vref, fresh.cnid, b"Doc")
    assert proc.runner.unimplemented_calls == {}


BNE, BEQ = 0x6600, 0x6700


def _hook(a: Asm):
    """A dialog hook, as MacWrite's: it keeps a log of the items it's
    called with, makes radio buttons 9 and 10 the one chosen, and has
    item 11 save."""
    a.label("hook")
    a.w(0x4E56, -16)                                # LINK A6,#-16
    a.w(0x302E, 12)                                 # MOVE.W 12(A6),D0: the item
    a.w(0x3D40, 14)                                 # MOVE.W D0,14(A6): the result
    a.w(0x41ED, BUF(6))                             # LEA BUF(6)(A5),A0
    a.w(0x322D, COUNT)                              # MOVE.W COUNT(A5),D1
    a.w(0x3180, 0x1000)                             # MOVE.W D0,0(A0,D1.W)
    a.w(0x546D, COUNT)                              # ADDQ.W #2,COUNT(A5)
    a.w(0x0C40, 11)                                 # CMPI.W #11,D0
    a.bra("not_quick", BNE)
    a.w(0x3D7C, 1, 14)                              # MOVE.W #1,14(A6): Save
    a.bra("hooked")
    a.label("not_quick")
    a.w(0x0C40, 9)
    a.bra("radio", BEQ)
    a.w(0x0C40, 10)
    a.bra("hooked", BNE)
    a.label("radio")
    for n in (9, 10):
        a.w(0x2F2E, 8)                              # GetDItem(dialog, n, kind, h, r)
        a.push_w(n)
        a.w(0x486E, -2, 0x486E, -6, 0x486E, -14)
        a.call(0xA98D)
        a.w(0x2F2E, -6)                             # SetCtlValue(h, item = n)
        a.w(0x7200, 0x0C6E, n, 12, 0x57C1, 0x4401)  # MOVEQ #0,D1; CMPI.W; SEQ D1; NEG.B D1
        a.w(0x3F01)
        a.call(0xA963)
    a.label("hooked")
    a.w(0x4E5E, 0x205F, 0x5C8F, 0x4ED0)             # UNLK A6; return, popping 6 bytes


def _save_dialog() -> list[Resource]:
    """DLOG -3999 with the eight items of Standard File's and four of its
    application's: two radio buttons (one a CNTL), a button, and text."""
    btn, text, rad = 4, 8, 6
    items = ditl(
        (btn, (136, 218, 154, 300), b"Save"), (btn, (161, 218, 179, 300), b"Cancel"),
        (text, (6, 14, 22, 200), b"Save as:"), (0, (39, 209, 59, 333), b""),
        (btn, (68, 218, 86, 300), b"Eject"), (btn, (93, 218, 111, 300), b"Drive"),
        (16, (29, 17, 45, 191), b""), (128, (62, 12, 64, 200), b""),
        (btn | 3, (190, 20, 206, 150), struct.pack(">h", 876)),
        (rad, (190, 160, 206, 300), b"Text Only"),
        (btn, (212, 20, 230, 100), b"Quick"),
        (text, (172, 20, 188, 150), b"Format:"))
    radio = struct.pack(">4hhBxhhhI", 190, 20, 206, 150, 1, 1, 1, 0, 2, 0) + pstr(
        "Entire Document")
    return [Resource(b"DLOG", -3999, dlog((0, 0, 240, 348), "", -3999, visible=False)),
            Resource(b"DITL", -3999, items), Resource(b"CNTL", 876, radio)]


def test_an_applications_own_items_and_dialog_hook(harness, tmp_path):
    """The application's own items, past Standard File's, are shown as it
    sets them, and its hook is told about each and decides what's done."""
    _sf_folder(tmp_path)
    a = Asm()
    a.push_l(0x00500050)                            # SFPutFile(where, prompt, name, hook,
    a.lea_pc("prompt", 0)                           # ...reply)
    a.push_a(0)
    a.lea_pc("name", 0)
    a.push_a(0)
    a.lea_pc("hook", 1)
    a.push_a(1)
    a.pea(BUF(0))
    a.call(0xA9EA, selector=1)
    a.mark(DONE, 1)
    _idle(a)
    _hook(a)
    a.label("prompt")
    a.code += b"\x08Save as:\x00"
    a.label("name")
    a.code += b"\x08Untitled\x00"
    app = host_app(tmp_path, a, _save_dialog())
    h = harness()
    proc, mem, a5 = _launch(h, app)

    def log():
        return [mem.r16s(a5 + BUF(6) + i) for i in range(0, mem.r16(a5 + COUNT), 2)]

    dlg = _dialog(h)
    assert log() == [-1]                            # before the dialog's shown
    entire, text_only, quick, label = (dlg._extras[n] for n in (9, 10, 11, 12))
    assert (entire.text(), text_only.text(), quick.text(), label.text()) == (
        "Entire Document", "Text Only", "Quick", "Format:")
    assert entire.isChecked() and not text_only.isChecked()
    assert entire.isVisible() and quick.isVisible()
    assert entire.geometry().left() == 0 and text_only.geometry().left() == 140
    assert label.geometry().top() == 0 and entire.geometry().top() == 18

    dlg.press(10)
    h.wait_until(lambda: log() == [-1, 10] and text_only.isChecked(), what="the hook")
    assert not entire.isChecked()
    dlg.press(10)                                   # it's the hook that says
    h.wait_until(lambda: log() == [-1, 10, 10], what="the hook again")
    assert text_only.isChecked()
    assert dlg.name.text() == "Untitled"            # and the dialog stays as it was

    dlg.name.setText("Plain")
    dlg.press(11)                                   # the hook makes it Save
    h.wait_until(lambda: mem.r16(a5 + DONE) == 1, what="SFPutFile's reply")
    assert log() == [-1, 10, 10, 11]
    reply = a5 + BUF(0)
    assert mem.r8(reply) == 1 and mem.r_pstr(reply + 10) == b"Plain"
    assert not [w for w in proc.windows if w.dialog is not None]    # its dialog's gone
    assert proc.runner.unimplemented_calls == {}


def _edit_dialog() -> list[Resource]:
    """DLOG -3999 with Standard File's eight items and three of its
    application's: an edit text item, a disabled one, and a button."""
    btn, text, edit = 4, 8, 16
    items = ditl(
        (btn, (136, 218, 154, 300), b"Save"), (btn, (161, 218, 179, 300), b"Cancel"),
        (text, (6, 14, 22, 200), b"Save as:"), (0, (39, 209, 59, 333), b""),
        (btn, (68, 218, 86, 300), b"Eject"), (btn, (93, 218, 111, 300), b"Drive"),
        (16, (29, 17, 45, 191), b""), (128, (62, 12, 64, 200), b""),
        (edit, (190, 20, 206, 150), b"Alpha"), (edit | 128, (190, 160, 206, 300), b"Beta"),
        (btn, (212, 20, 230, 100), b"Quick"))
    return [Resource(b"DLOG", -3999, dlog((0, 0, 240, 348), "", -3999, visible=False)),
            Resource(b"DITL", -3999, items)]


def test_an_applications_edit_text_items(harness, tmp_path):
    """The application's edit text items are typed in, and its hook reads
    what's in them with GetIText each time: called with the item for one
    that's enabled, as ModalDialog returns it, and with sfHookNullEvent
    for one that isn't, as Microsoft File's name for a new file is."""
    _sf_folder(tmp_path)
    a = Asm()
    a.push_l(0x00500050)                            # SFPutFile(where, prompt, name, hook,
    a.lea_pc("prompt", 0)                           # ...reply)
    a.push_a(0)
    a.lea_pc("name", 0)
    a.push_a(0)
    a.lea_pc("hook", 1)
    a.push_a(1)
    a.pea(BUF(0))
    a.call(0xA9EA, selector=1)
    a.mark(DONE, 1)
    _idle(a)
    a.label("hook")                                 # logs the item, keeps item 10's text
    a.w(0x4E56, -16)                                # LINK A6,#-16
    a.w(0x302E, 12)                                 # MOVE.W 12(A6),D0: the item
    a.w(0x3D40, 14)                                 # MOVE.W D0,14(A6): the result
    a.w(0x41ED, BUF(6))                             # LEA BUF(6)(A5),A0
    a.w(0x322D, COUNT)                              # MOVE.W COUNT(A5),D1
    a.w(0x3180, 0x1000)                             # MOVE.W D0,0(A0,D1.W)
    a.w(0x546D, COUNT)                              # ADDQ.W #2,COUNT(A5)
    a.w(0x2F2E, 8)                                  # GetDItem(dialog, 10, kind, h, r)
    a.push_w(10)
    a.w(0x486E, -2, 0x486E, -6, 0x486E, -14)
    a.call(0xA98D)
    a.w(0x2F2E, -6)                                 # GetIText(h, BUF(8))
    a.pea(BUF(8))
    a.call(0xA990)
    a.w(0x0C6E, 11, 12)                             # CMPI.W #11,12(A6)
    a.bra("hooked", BNE)
    a.w(0x3D7C, 1, 14)                              # MOVE.W #1,14(A6): Save
    a.label("hooked")
    a.w(0x4E5E, 0x205F, 0x5C8F, 0x4ED0)             # UNLK A6; return, popping 6 bytes
    a.label("prompt")
    a.code += b"\x08Save as:\x00"
    a.label("name")
    a.code += b"\x08Untitled\x00"
    app = host_app(tmp_path, a, _edit_dialog())
    h = harness()
    proc, mem, a5 = _launch(h, app)

    def log():
        return [mem.r16s(a5 + BUF(6) + i) for i in range(0, mem.r16(a5 + COUNT), 2)]

    dlg = _dialog(h)
    enabled, disabled = dlg._extras[9], dlg._extras[10]
    assert (enabled.text(), disabled.text()) == ("Alpha", "Beta")
    assert enabled.isEnabled() and disabled.isEnabled()
    assert log() == [-1] and mem.r_pstr(a5 + BUF(8)) == b"Beta"
    QTest.keyClicks(disabled, "X")
    h.wait_until(lambda: mem.r_pstr(a5 + BUF(8)) == b"BetaX", what="the hook")
    assert log() == [-1, 100]
    QTest.keyClicks(enabled, "!")
    h.wait_until(lambda: log() == [-1, 100, 9], what="the hook again")
    assert enabled.text() == "Alpha!" and disabled.text() == "BetaX"
    dlg.press(11)
    h.wait_until(lambda: mem.r16(a5 + DONE) == 1, what="SFPutFile's reply")
    assert mem.r8(a5 + BUF(0)) == 1
    assert proc.runner.unimplemented_calls == {}


def _next_event(a: Asm, mask: int, at: int, label: str):
    """GetNextEvent(mask, at(A5)), round again until it has one."""
    a.label(label)
    a.result()
    a.push_w(mask)
    a.pea(at)
    a.call(0xA970)
    a.w(0x101F, 0x4A00)                             # MOVE.B (SP)+,D0; TST.B D0
    a.bra(label, BEQ)


def test_the_window_behind_is_activated_when_the_dialog_goes(harness, tmp_path):
    """A Mac's Standard File dialog is the front window while it's up, so
    the application gets its own window's activate event when it goes.
    MacWrite 4.5 hides its scroll bar and dims its menus before SFPutFile
    and counts on that event to bring them back."""
    a = Asm()
    # CLR.L -(SP); NewWindow(nil, rect, title, visible, documentProc,
    # in front, no go-away box, 0)
    a.w(0x42A7)
    a.push_l(0)
    a.lea_pc("rect", 0)
    a.push_a(0)
    a.lea_pc("title", 0)
    a.push_a(0)
    a.push_w(0x0100)
    a.push_w(0)
    a.push_l(-1)
    a.push_w(0)
    a.push_l(0)
    a.call(0xA913)
    a.w(0x2B5F, LONG)                               # MOVE.L (SP)+,LONG(A5)
    _next_event(a, 0x0100, BUF(9), "first")         # its activate event, as it opens
    a.push_l(0)                                     # StandardGetFile(nil, -1, nil, reply)
    a.push_w(-1)
    a.push_l(0)
    a.pea(BUF(0))
    a.call(0xA9EA, selector=6)
    a.mark(DONE, 1)
    _next_event(a, 0x0100, BUF(10), "again")
    a.mark(DONE, 2)
    _idle(a)
    a.label("rect")
    a.w(60, 40, 260, 340)
    a.label("title")
    a.code += b"\x04Memo\x00"
    app = host_app(tmp_path, a)
    h = harness()
    proc, mem, a5 = _launch(h, app)
    dlg = _dialog(h)
    window = proc.front_window()
    assert window is not None and window.addr == mem.r32(a5 + LONG)
    h.wait_until(lambda: not window.hilited, what="the window to be deactivated")
    dlg.cancel()
    h.wait_until(lambda: mem.r16(a5 + DONE) == 2, what="the activate event")
    ev = a5 + BUF(10)
    assert (mem.r16(ev), mem.r32(ev + 2)) == (k.activateEvt, window.addr)
    assert mem.r16(ev + 14) & k.activeFlag
    assert window.hilited
    assert proc.runner.unimplemented_calls == {}
