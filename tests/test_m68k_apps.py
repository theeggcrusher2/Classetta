"""Real 68k applications (built by tests/apps/build.py from assembly
source) running on the emulated CPU, driven with synthetic input."""

import wave
from pathlib import Path
from types import SimpleNamespace

import pytest
from PyQt6.QtCore import QPoint, Qt
from PyQt6.QtGui import QFont, QFontMetrics
from PyQt6.QtTest import QTest
from PyQt6.QtWidgets import QApplication

from macrt.m68k import dispatch
from macrt.m68k.hardware import vBufA
from macrt.m68k.runtime import LEVEL_1_ROUTINE, Runner
from macrt.macfile import MacFile, MemoryFolder, open_application
from macrt.resfork import Resource, build_fork
from macrt.toolbox import constants as k
from macrt.toolbox import lowmem
from macrt.toolbox import qdraw
from macrt.toolbox import textedit as te
from macrt.toolbox.events import EventRecord
from macrt.toolbox.types import Point, Rect
from macrt.toolbox.windows import describe_proc
from macrt.ui import chrome, theme

from apps.build import LOCKED_PRELOAD, code_resources
from conftest import Recorder

APPS = Path(__file__).parent / "apps"
CLICKS = -236               # hello.s's click count, below A5


def launch_hello(h):
    f = open_application(APPS / "Hello.bin")
    # A Python app alongside, so the display stays up when Hello quits.
    h.launch(Recorder("Other"))
    proc = h.d.launch(f.name, None, app=f)
    h.wait_until(lambda: proc.windows and proc.windows[0].visible, timeout=10,
                 what="Hello's window")
    rec = proc.windows[0]
    h.wait_until(lambda: black(rec, 70, 65), what="Hello's drawing")
    return proc, rec


def black(rec, h, v):
    """Is the window's pixel at (h, v), local, black?"""
    with rec.backing.lock:
        return rec.backing.image.pixel(h, v) & 0xFFFFFF == 0


def clicks(proc):
    return proc.system.mem.r16(proc.partition.a5 + CLICKS)


def test_hello_draws_its_window(harness):
    h = harness()
    proc, rec = launch_hello(h)
    assert rec.title == "Hello"
    # All at once: it frames the rectangle after painting the oval, and
    # erases everything first when it redraws.
    h.wait_until(lambda: (black(rec, 70, 65)                    # inside the painted oval
                          and not black(rec, 31, 41)            # outside it, at a corner
                          and black(rec, 130, 40) and black(rec, 209, 89)   # the frame
                          and not black(rec, 170, 65)),         # and not inside it
                 what="Hello's oval and rectangle")
    assert proc.runner.unimplemented_calls == {}


def test_the_cache_flush_vector_is_code(harness):
    h = harness()
    proc, _ = launch_hello(h)
    mem = proc.system.mem
    flush = mem.r32(lowmem.jCacheFlush)
    assert mem.read(flush, 4) == b"\xA0\xBD\x4E\x75"      # _FlushCodeCache; RTS
    assert dispatch.implemented(0xA0BD)


def test_the_sound_buffer_is_out_of_the_way(harness):
    """Games write samples to SoundBase directly; it mustn't be low memory."""
    h = harness()
    proc, _ = launch_hello(h)
    system = proc.system
    buffer = system.mem.r32(lowmem.SoundBase)
    assert system.sys_heap.contains(buffer)
    assert system.sys_heap.ptr_size(buffer) >= 370 * 2


def test_the_system_event_mask_says_what_is_posted(harness):
    """As on a Mac, keyUp isn't posted until a program puts it in SysEvtMask."""
    h = harness()
    proc, _ = launch_hello(h)
    mem = proc.system.mem
    up = EventRecord(k.keyUp, 0x2561, 0, Point(0, 0), 0)
    assert mem.r16(lowmem.SysEvtMask) == k.everyEvent & ~k.event_mask(k.keyUp)
    assert not proc.events.post(up)
    mem.w16(lowmem.SysEvtMask, k.everyEvent)
    assert proc.events.post(up)


def test_parameter_ram_has_the_timing_the_globals_do(harness):
    """Wizardry takes its double-click time from volClik, not DoubleTime."""
    h = harness()
    proc, _ = launch_hello(h)
    mem = proc.system.mem
    kbd_print, vol_clik = mem.r16(lowmem.SysParam + 14), mem.r16(lowmem.SysParam + 16)
    assert mem.r8(lowmem.SysParam) == 0xA8                         # valid
    assert (vol_clik >> 4 & 0xF) * 4 == mem.r32(lowmem.DoubleTime)
    assert (vol_clik & 0xF) * 4 == mem.r32(lowmem.CaretTime)
    assert (kbd_print >> 12) * 4 == mem.r16(lowmem.KeyThresh)
    assert (kbd_print >> 8 & 0xF) * 2 == mem.r16(lowmem.KeyRepThresh)
    # The speaker's volume, which Dark Castle reads from SdVolume.
    via = proc.runner.machine.via
    assert vol_clik >> 8 & 7 == mem.r8(lowmem.SdVolume) == via.regs[vBufA] & 7 == 7


def test_the_random_seed_in_low_memory_is_not_zero(harness):
    """Leisure Suit Larry seeds QuickDraw's randSeed from RndSeed: from 0,
    every Random is 0, and its age quiz never picks a question."""
    h = harness()
    proc, _ = launch_hello(h)
    assert proc.system.mem.r32(lowmem.RndSeed) != 0


def test_hello_counts_clicks_in_its_window(harness):
    h = harness()
    proc, rec = launch_hello(h)
    c = rec.content
    h.click(c.left + 200, c.top + 140)
    h.wait_until(lambda: clicks(proc) == 1, what="a click counted")
    h.click(c.left + 200, c.top + 140)
    h.wait_until(lambda: clicks(proc) == 2, what="another")


@pytest.mark.parametrize("how", ["menu", "command key", "close box"])
def test_hello_quits(harness, how):
    h = harness()
    proc, rec = launch_hello(h)
    if how == "menu":
        h.choose(h.d.default_bar(proc), "File", 1)
    elif how == "command key":
        h.key(Qt.Key.Key_Q, Qt.KeyboardModifier.ControlModifier)
    else:
        h.click(*h.box_point(rec, k.wInGoAway))
    h.wait_until(lambda: proc not in h.d.processes, timeout=5, what="Hello to quit")
    assert rec not in h.d.views


def test_a_crash_stops_only_that_app(harness):
    """An illegal instruction is a system error, ID 3: the app goes, the
    user is told, and everything else carries on."""
    h = harness()
    other = h.launch(Recorder("Other"))
    crash = MacFile("Crash", rsrc=build_fork(code_resources(bytes.fromhex("4AFC"), 0)),
                    type=b"APPL", creator=b"CRSH")
    proc = h.d.launch(crash.name, None, app=crash)
    h.wait_until(lambda: proc not in h.d.processes, timeout=5, what="Crash to stop")
    assert h.d.system_errors[-1][:2] == ("Crash", 3)
    h.wait_until(lambda: any(a.isVisible() for a in h.d._alerts), what="the alert")
    assert other in h.d.processes


def test_sys_error_is_a_bomb_but_for_the_alerts_with_no_buttons(harness):
    """A startup alert (a negative ID) and the disk-switch alert (30) come
    back; zero divide (4) doesn't, as Downhill Racer's handler expects,
    jumping to the ROM's restart after it."""
    h = harness()
    code = bytes.fromhex("70FF A9C9"        # moveq #-1,d0; _SysError
                         "701E A9C9"        # moveq #30,d0; _SysError
                         "7004 A9C9"        # moveq #4,d0; _SysError
                         "60FE")            # bra.s *
    bomb = MacFile("Bomb", rsrc=build_fork(code_resources(code, 0)), type=b"APPL", creator=b"BOMB")
    proc = h.d.launch(bomb.name, None, app=bomb)
    h.wait_until(lambda: proc not in h.d.processes, timeout=5, what="Bomb to stop")
    assert h.d.system_errors[-1][:2] == ("Bomb", 4)
    assert proc.system.mem.r16(lowmem.DSErrCode) == 4


def test_the_alert_outlives_the_last_app(harness):
    """With nothing else running, the display stays up until the user has
    read the alert, then closes."""
    h = harness()
    crash = MacFile("Crash", rsrc=build_fork(code_resources(bytes.fromhex("4AFC"), 0)),
                    type=b"APPL", creator=b"CRSH")
    proc = h.d.launch(crash.name, None, app=crash)
    h.wait_until(lambda: proc not in h.d.processes and h.d._alerts, timeout=5,
                 what="Crash to stop")
    assert not h.d.shutting_down
    h.d._alerts[0].accept()
    h.wait_until(lambda: h.d.shutting_down, what="the display to close")


def test_an_app_launches_another_in_its_place(harness):
    """Launcher answers a Gestalt selector of its own (NewGestalt), then
    Launches Hello, which is in the same folder, and quits for it."""
    h = harness()
    folder = MemoryFolder([open_application(APPS / "Launcher.bin"),
                           open_application(APPS / "Hello.bin")])
    launcher = folder.find("Launcher")
    proc = h.d.launch(launcher.name, None, app=launcher)
    mem, a5 = proc.system.mem, proc.partition.a5
    h.wait_until(lambda: proc not in h.d.processes, timeout=5, what="Launcher to quit")
    assert (mem.r16s(a5 - 2), mem.r16s(a5 - 4), mem.r32(a5 - 8)) == (0, 0, 42)
    hello = next(p for p in h.d.processes if p.name == "Hello")
    h.wait_until(lambda: hello.windows and hello.windows[0].visible, what="Hello's window")


def test_a_launcher_quits_even_if_what_it_launched_keeps_the_processor(harness):
    """An application that never calls WaitNextEvent keeps the processor,
    as it would under MultiFinder; the launcher that started it goes
    anyway, without waiting for its turn."""
    h = harness()
    spin = MacFile("Hello", rsrc=build_fork(code_resources(bytes.fromhex("60FE"), 0)),
                   type=b"APPL", creator=b"SPIN")                   # BRA.S *
    folder = MemoryFolder([open_application(APPS / "Launcher.bin"), spin])
    launcher = folder.find("Launcher")
    proc = h.d.launch(launcher.name, None, app=launcher)
    h.wait_until(lambda: proc not in h.d.processes, timeout=5, what="Launcher to quit")
    assert [p.name for p in h.d.processes] == ["Hello"]


def test_an_app_chains_to_another(harness):
    """Skyfox's first application Chains to the game: the other starts in
    its place, as with Launch."""
    h = harness()
    chainer = MacFile("Chainer", rsrc=build_fork(code_resources(bytes.fromhex(
        "4267"                  # CLR.W -(SP): the configuration
        "487A 0006"             # PEA name(PC)
        "204F"                  # MOVEA.L SP,A0
        "A9F3"                  # _Chain
        "0548656C6C6F"), 0)),   # name: "Hello"
        type=b"APPL", creator=b"CHAN")
    folder = MemoryFolder([chainer, open_application(APPS / "Hello.bin")])
    chainer = folder.find("Chainer")
    proc = h.d.launch(chainer.name, None, app=chainer)
    h.wait_until(lambda: proc not in h.d.processes, timeout=5, what="Chainer to go")
    hello = next(p for p in h.d.processes if p.name == "Hello")
    h.wait_until(lambda: hello.windows and hello.windows[0].visible, what="Hello's window")


def test_unloadseg_by_a_jump_table_entry(harness):
    """UnloadSeg given the address Pascal's @ gives for a routine in
    another segment, its jump table entry's JMP (Microsoft BASIC 2 unloads
    its segments so): the entry goes back to _LoadSeg, and loads again."""
    h = harness()
    h.launch(Recorder("Other"))
    entry = lambda seg: bytes.fromhex(f"00003F3C{seg:04X}A9F0")     # noqa: E731
    table = entry(1) + entry(2)
    code0 = bytes.fromhex(f"{32 + len(table):08X}00000004{len(table):08X}00000020") + table
    main = bytes.fromhex("0000 0001"            # first entry 0, one entry
                         "4EAD 002A"            # JSR 42(A5): entry 1, in CODE 2
                         "486D 002A A9F1"       # PEA 42(A5); _UnloadSeg
                         "3B6D 002A FFFE"       # MOVE.W 42(A5),-2(A5)
                         "4EAD 002A"            # JSR 42(A5) again
                         "3B6D 002A FFFC"       # MOVE.W 42(A5),-4(A5)
                         "A9F4")                # _ExitToShell
    sub = bytes.fromhex("0008 0001 4E75")       # first entry 8, one entry; RTS
    app = MacFile("Segs", rsrc=build_fork([
        Resource(b"CODE", 0, code0, b"", 0x20 | LOCKED_PRELOAD),
        Resource(b"CODE", 1, main, b"Main", 0x20),
        Resource(b"CODE", 2, sub, b"Sub", 0x20)]), type=b"APPL", creator=b"SEGS")
    proc = h.d.launch(app.name, None, app=app)
    mem, a5 = proc.system.mem, proc.partition.a5
    h.wait_until(lambda: proc not in h.d.processes, timeout=5, what="Segs to quit")
    assert not h.d.system_errors
    assert mem.r16(a5 - 2) == 0x3F3C            # unloaded: MOVE.W #2,-(SP); _LoadSeg
    assert mem.r16(a5 - 4) == 0x4EF9            # loaded again: JMP


def test_a_jump_table_entry_called_with_flags_in_its_high_byte(harness):
    """A 68000 ignores an address's high byte: Macintosh Pascal calls jump
    table entries through addresses with a master pointer's flags in it,
    and _LoadSeg must still find the entry it came from."""
    h = harness()
    h.launch(Recorder("Other"))
    entry = lambda seg: bytes.fromhex(f"00003F3C{seg:04X}A9F0")     # noqa: E731
    table = entry(1) + entry(2)
    code0 = bytes.fromhex(f"{32 + len(table):08X}00000010{len(table):08X}00000020") + table
    main = bytes.fromhex("0000 0001"            # first entry 0, one entry
                         "41ED 002A"            # LEA 42(A5),A0: entry 2, in CODE 2
                         "2008 08C0 001F 2040"  # with bit 31 set
                         "4E90"                 # JSR (A0)
                         "3B40 FFF0"            # MOVE.W D0,-16(A5)
                         "A9F4")                # _ExitToShell
    sub = bytes.fromhex("0008 0001 7007 4E75")  # first entry 8, one entry; MOVEQ #7,D0; RTS
    app = MacFile("Segs", rsrc=build_fork([
        Resource(b"CODE", 0, code0, b"", 0x20 | LOCKED_PRELOAD),
        Resource(b"CODE", 1, main, b"Main", 0x20),
        Resource(b"CODE", 2, sub, b"Sub", 0x20)]), type=b"APPL", creator=b"SEGS")
    proc = h.d.launch(app.name, None, app=app)
    mem, a5 = proc.system.mem, proc.partition.a5
    h.wait_until(lambda: proc not in h.d.processes, timeout=5, what="Segs to quit")
    assert not h.d.system_errors
    assert mem.r16s(a5 - 16) == 7                   # CODE 2 ran


def test_a_trap_through_the_roms_dispatcher(harness):
    """Lotus Jazz finds the ROM's A-line dispatcher by the code that puts
    it in vector $28, puts it back there, and a trap's exception frame
    stacked and jumped to there does the trap."""
    h = harness()
    h.launch(Recorder("Other"))
    code0 = bytes.fromhex("00000028 00000010 00000008 00000020"
                          "0000 3F3C 0001 A9F0")
    main = bytes.fromhex("0000 0001"            # first entry 0, one entry
                         "2278 02AE"            # MOVEA.L ROMBase,A1
                         "203C 21C8 0028"       # MOVE.L #$21C80028,D0
                         "72FF"                 # MOVEQ #-1,D1
                         "B091 6706"            # find: CMP.L (A1),D0; BEQ.S found
                         "5449 51C9 FFF8"       # ADDQ.W #2,A1; DBRA D1,find
                         "3021 5D49"            # found: MOVE.W -(A1),D0; SUBQ.W #6,A1
                         "3011 D2C0"            # MOVE.W (A1),D0; ADDA.W D0,A1: a 68000's
                         "2B49 FFF4"            # MOVE.L A1,-12(A5)
                         "42A7"                 # CLR.L -(A7)
                         "2F3C 0000 F0F0"       # MOVE.L #$F0F0,-(A7)
                         "2F3C 0000 FF00"       # MOVE.L #$FF00,-(A7)
                         "487A 0006"            # PEA trap(PC)
                         "40E7 4ED1"            # MOVE.W SR,-(A7); JMP (A1)
                         "A858"                 # trap: _BitAnd
                         "2B5F FFF0"            # MOVE.L (A7)+,-16(A5)
                         "A9F4")                # _ExitToShell
    app = MacFile("Jazzy", rsrc=build_fork([
        Resource(b"CODE", 0, code0, b"", 0x20 | LOCKED_PRELOAD),
        Resource(b"CODE", 1, main, b"Main", 0x20)]), type=b"APPL", creator=b"JAZY")
    proc = h.d.launch(app.name, None, app=app)
    mem, a5 = proc.system.mem, proc.partition.a5
    h.wait_until(lambda: proc not in h.d.processes, timeout=5, what="Jazzy to quit")
    assert not h.d.system_errors
    assert mem.r32(a5 - 12) == mem.r32(0x28)
    assert mem.r32(a5 - 16) == 0xF000


def test_a_segment_is_read_through_a_patch_on_getresource(harness):
    """FullPaint and Q-Sheet patch _GetResource to decrypt their segments
    as the Segment Loader reads them: the patch is called for CODE 2, and
    goes on to the original."""
    h = harness()
    h.launch(Recorder("Other"))
    entry = lambda seg: bytes.fromhex(f"00003F3C{seg:04X}A9F0")     # noqa: E731
    table = entry(1) + entry(2)
    code0 = bytes.fromhex(f"{32 + len(table):08X}00000010{len(table):08X}00000020") + table
    main = bytes.fromhex("0000 0001"            # first entry 0, one entry
                         "303C 01A0 A146"       # MOVE.W #$1A0,D0; _GetTrapAddress
                         "2B48 FFF4"            # MOVE.L A0,-12(A5): GetResource
                         "41FA 0012"            # LEA patch(PC),A0
                         "303C 01A0 A047"       # MOVE.W #$1A0,D0; _SetTrapAddress
                         "4EAD 002A"            # JSR 42(A5): entry 2, in CODE 2
                         "3B40 FFF0"            # MOVE.W D0,-16(A5)
                         "A9F4"                 # _ExitToShell
                         # patch:
                         "526D FFFE"            # ADDQ.W #1,-2(A5)
                         "2B6F 0006 FFFA"       # MOVE.L 6(SP),-6(A5): the type
                         "3B6F 0004 FFF8"       # MOVE.W 4(SP),-8(A5): the ID
                         "2F2D FFF4 4E75")      # MOVE.L -12(A5),-(SP); RTS: to GetResource
    sub = bytes.fromhex("0008 0001 7007 4E75")  # first entry 8, one entry; MOVEQ #7,D0; RTS
    app = MacFile("Segs", rsrc=build_fork([
        Resource(b"CODE", 0, code0, b"", 0x20 | LOCKED_PRELOAD),
        Resource(b"CODE", 1, main, b"Main", 0x20),
        Resource(b"CODE", 2, sub, b"Sub", 0x20)]), type=b"APPL", creator=b"SEGS")
    proc = h.d.launch(app.name, None, app=app)
    mem, a5 = proc.system.mem, proc.partition.a5
    h.wait_until(lambda: proc not in h.d.processes, timeout=5, what="Segs to quit")
    assert not h.d.system_errors
    assert (mem.r16s(a5 - 2), mem.read(a5 - 6, 4), mem.r16s(a5 - 8)) == (1, b"CODE", 2)
    assert mem.r16s(a5 - 16) == 7                   # CODE 2 ran


def test_an_rte_follows_the_traps_code_as_in_the_rom(harness):
    """Downhill Racer looks past Dequeue's code, from GetTrapAddress, for
    the ROM's next RTE, to make the interrupts it doesn't want do nothing,
    and then checks it hasn't taken 3 seconds.  There's one soon after
    the traps' code, and it returns as an interrupt's handler does."""
    h = harness()
    h.launch(Recorder("Other"))
    code0 = bytes.fromhex("00000028 00000010 00000008 00000020"
                          "0000 3F3C 0001 A9F0")
    main = bytes.fromhex("0000 0001"            # first entry 0, one entry
                         "303C A96E A146"       # MOVE.W #$A96E,D0; _GetTrapAddress
                         "2B48 FFFC"            # MOVE.L A0,-4(A5): Dequeue's
                         "303C 4E73"            # MOVE.W #$4E73,D0: RTE
                         "B058 66FC"            # loop: CMP.W (A0)+,D0; BNE loop
                         "5588"                 # SUBQ.L #2,A0
                         "2B48 FFF8"            # MOVE.L A0,-8(A5)
                         "487A 0006"            # PEA back(PC)
                         "40E7 4ED0"            # MOVE.W SR,-(SP); JMP (A0): as an interrupt
                         "3B7C 0001 FFF6"       # back: MOVE.W #1,-10(A5)
                         "A9F4")                # _ExitToShell
    app = MacFile("Rte", rsrc=build_fork([
        Resource(b"CODE", 0, code0, b"", 0x20 | LOCKED_PRELOAD),
        Resource(b"CODE", 1, main, b"Main", 0x20)]), type=b"APPL", creator=b"RTE ")
    proc = h.d.launch(app.name, None, app=app)
    mem, a5 = proc.system.mem, proc.partition.a5
    h.wait_until(lambda: proc not in h.d.processes, timeout=5, what="Rte to quit")
    assert not h.d.system_errors
    dequeue, rte = mem.r32(a5 - 4), mem.r32(a5 - 8)
    assert 0 < rte - dequeue < 0x1000 and mem.r16(rte) == 0x4E73
    assert mem.r16s(a5 - 10) == 1


def test_the_user_says_which_disk_goes_in_for_one_unmounted(harness):
    """Wizardry VI ejects and unmounts its disk and asks for another: the
    disks there are are listed, the next in turn chosen, and the one the
    user chooses is the one MountVol mounts."""
    import machfs

    from macrt.macfile import load_volume
    from macrt.ui import diskdlg

    disks = []
    for name in ("One", "Two", "Three"):
        v = machfs.Volume()
        v.name = name
        disks.append(load_volume(v.write(400 * 1024)))
    h = harness(disks=disks)
    h.launch(Recorder("Other"))
    code0 = bytes.fromhex("00000028 00000100 00000008 00000020"
                          "0000 3F3C 0001 A9F0")
    pb = "41ED FF80 317C 0001 0016"         # LEA -$80(A5),A0; MOVE.W #1,ioVRefNum(A0)
    main = bytes.fromhex("0000 0001"
                         + pb + "42A8 0012"     # CLR.L ioNamePtr(A0)
                         "A017 3B40 FFFE"       # _Eject; MOVE.W D0,-2(A5)
                         + pb + "A00E 3B40 FFF8"    # _UnmountVol; MOVE.W D0,-8(A5)
                         "303C 0080 41ED FF60"  # loop: MOVE.W #diskMask,D0; LEA -$A0(A5),A0
                         "A031 66F4"            # _GetOSEvent; BNE loop
                         + pb + "A00F 3B40 FFFC"    # _MountVol; MOVE.W D0,-4(A5)
                         "3B68 0016 FFFA"       # MOVE.W ioVRefNum(A0),-6(A5)
                         "A9F4")                # _ExitToShell
    app = MacFile("Swap", rsrc=build_fork([
        Resource(b"CODE", 0, code0, b"", 0x20 | LOCKED_PRELOAD),
        Resource(b"CODE", 1, main, b"Main", 0x20)]), type=b"APPL", creator=b"SWAP")
    proc = h.d.launch(app.name, None, app=app)
    mem, a5, fs = proc.system.mem, proc.partition.a5, proc.system.files
    one, two, three = fs.floppies()
    h.wait_until(lambda: h.d.disk_dialogs, timeout=5, what="the disk dialog")
    dlg = h.d.disk_dialogs[0]
    assert isinstance(dlg, diskdlg.DiskDialog)
    assert [dlg.list.item(i).text() for i in range(3)] == ["One", "Two", "Three"]
    assert dlg.list.currentRow() == 1
    dlg.list.setCurrentRow(2)
    QTest.mouseClick(dlg.ok, Qt.MouseButton.LeftButton)
    h.wait_until(lambda: proc not in h.d.processes, timeout=5, what="Swap to quit")
    assert not h.d.system_errors and not h.d.disk_dialogs
    assert [mem.r16s(a5 - i) for i in (2, 8, 4)] == [0, 0, 0]
    assert mem.r16s(a5 - 6) == three.vref
    assert (three.drive, one.drive) == (1, 4) and fs.offline == [one]


def test_vbl_tasks_run_every_tick_until_removed(harness):
    """VBL counts its VBL task's runs while it spins (it calls no trap, so
    only the vertical retrace gets the task in), and while it waits in
    Delay, the host having the processor; then it masks interrupts, then
    removes the task, and the task doesn't run in either case.  A task
    that doesn't set its count again runs once and is taken out of the
    queue, and can be installed again."""
    h = harness()
    h.launch(Recorder("Other"))
    f = open_application(APPS / "VBL.bin")
    proc = h.d.launch(f.name, None, app=f)
    mem, a5 = proc.system.mem, proc.partition.a5
    h.wait_until(lambda: proc not in h.d.processes, timeout=10, what="VBL to finish")
    assert not h.d.system_errors
    assert mem.r16s(a5 - 16) >= 10                  # its runs
    assert mem.r32(a5 - 26) == 0x12345678           # the spin's D1, as it left it
    assert 12 <= mem.r16s(a5 - 28) <= 16            # every other tick of 30
    assert mem.r16s(a5 - 18) == 0                   # none while masked
    assert mem.r16s(a5 - 20) == 0                   # VRemove: noErr
    assert mem.r16s(a5 - 22) == 0                   # none after
    assert mem.r16s(a5 - 30) == 2                   # once for each VInstall
    assert mem.r16s(a5 - 32) == -1                  # VRemove: qErr, it's gone
    assert proc.runner.unimplemented_calls == {}


def test_get_next_event_with_nothing_to_give_returns_at_once(harness):
    """Poll waits for the Ticks to change and then calls GetNextEvent, 30
    times: as on a Mac, where it returns at once with nothing to give, that
    takes 30 ticks, not 60.  Its 30 calls one after another take about 30
    ticks too: asked again in the same tick, it waits for the next."""
    h = harness()
    h.launch(Recorder("Other"))
    f = open_application(APPS / "Poll.bin")
    proc = h.d.launch(f.name, None, app=f)
    mem, a5 = proc.system.mem, proc.partition.a5
    h.wait_until(lambda: proc not in h.d.processes, timeout=10, what="Poll to finish")
    assert not h.d.system_errors
    assert mem.r32(a5 - 20) <= 45                   # a tick each, not two
    assert 25 <= mem.r32(a5 - 24) <= 45             # and no more than one a tick
    assert proc.runner.unimplemented_calls == {}


def test_a_task_that_waits_at_a_tick_keeps_the_processor():
    """A VBL task the ticker runs while its application waits, calling a
    trap that waits too (Delay): the ticker keeps the processor, so the
    application doesn't go on under the task, and leaves it waiting."""
    calls = []
    machine = SimpleNamespace(ticking=lambda: True, release=lambda: calls.append("release"),
                              idle=lambda runner: calls.append("idle"))
    runner = SimpleNamespace(machine=machine, holding=False)
    with Runner.yielding(runner):
        pass
    assert calls == []
    assert runner.holding is False


def test_time_manager_tasks_run_when_due(harness):
    """Timer's extended task primes itself every 10 ms: its 30 runs take
    about 18 ticks, not 30 (one a tick), and none come once it's removed.
    One primed for a second and removed at once never runs, and is left
    with most of the second, in microseconds negated."""
    h = harness()
    h.launch(Recorder("Other"))
    f = open_application(APPS / "Timer.bin")
    proc = h.d.launch(f.name, None, app=f)
    mem, a5 = proc.system.mem, proc.partition.a5
    h.wait_until(lambda: proc not in h.d.processes, timeout=10, what="Timer to finish")
    assert not h.d.system_errors
    assert mem.r16(a5 - 48) & 0x8000                # waiting, once primed
    assert 15 <= mem.r32(a5 - 60) < 28              # the ticks its 30 runs took
    assert mem.r16s(a5 - 62) == 0                   # none once removed
    assert -1_000_000 <= mem.r32s(a5 - 52) < -800_000   # what the other had left
    assert not mem.r16(a5 - 54) & 0x8000            # and it's not waiting
    assert mem.r16s(a5 - 56) == 0                   # nor did it run
    assert proc.runner.unimplemented_calls == {}


# -- Driver: an application's own driver ----------------------------------------------
def test_an_applications_own_driver(harness):
    """Driver opens its DRVR 20, .Echo, and writes to it and reads back;
    leaves a control call going, asynchronously, for a status call to
    finish through IODone, then synchronously, waiting while a VBL task
    makes the status call; kills one left going; closes it, writes to it
    closed, and opens it again.  When it quits, the driver's gone."""
    h = harness()
    h.launch(Recorder("Other"))
    f = open_application(APPS / "Driver.bin")
    proc = h.d.launch(f.name, None, app=f)
    mem, a5 = proc.system.mem, proc.partition.a5
    machine = proc.runner.machine
    h.wait_until(lambda: proc not in h.d.processes, timeout=10, what="Driver to finish")
    assert not h.d.system_errors
    word = lambda at: mem.r16s(a5 - at)    # noqa: E731
    assert (word(182), word(184), word(186)) == (0, -21, -21)      # Open; unit 20's refnum
    assert (word(188), mem.r32(a5 - 192), word(194)) == (0, 5, 0)   # Write, 5 bytes; Read
    assert mem.read(a5 - 210, 5) == b"Hello"
    assert (word(212), word(214)) == (0, 1)             # queued, and going
    assert (word(216), word(218)) == (-36, -36)         # finished: ioErr, and completed
    assert word(220) == -36                             # the synchronous call's, waited for
    assert (word(222), word(224)) == (0, -27)           # KillIO; abortErr
    assert (word(232), word(226), mem.r32(a5 - 230)) == (2, -27, a5 - 100)
    assert (word(234), word(236)) == (0, -28)           # Close; notOpenErr after it
    assert (word(238), word(240)) == (0, -21)           # open again, the same unit
    assert word(242) == -43                             # .Nope: no such driver, or file
    table = mem.r32(lowmem.UTableBase)
    assert mem.r32(table + 4 * 20) == 0 and machine.drivers == {}
    assert proc.runner.unimplemented_calls == {}


# -- Desk: a desk accessory ----------------------------------------------------------
def test_a_desk_accessory(harness):
    """Desk's Apple menu lists its DRVR 12, Tally, which it opens.  Tally's
    window comes up in front, and GetNextEvent gives Tally the events for
    it, not Desk; SystemTask gives it time and the cursor, SystemEvent a
    key but not a Command key, SystemEdit Copy, SystemClick the click
    FindWindow says is in a system window, and MenuKey Command-T, from
    its own menu.  Each time, Desk's port is put back.  With Desk's window
    in front, it gets none of that but time.  Desk closes it, opens it
    again and quits, which closes it too."""
    h = harness()
    h.launch(Recorder("Other"))
    f = open_application(APPS / "Desk.bin")
    proc = h.d.launch(f.name, None, app=f)
    mem, a5 = proc.system.mem, proc.partition.a5
    machine = proc.runner.machine
    h.wait_until(lambda: proc not in h.d.processes, timeout=10, what="Desk to finish")
    assert not h.d.system_errors
    word = lambda at: mem.r16s(a5 - at)    # noqa: E731
    long = lambda at: mem.r32(a5 - at)     # noqa: E731
    flag = lambda at: mem.r8(a5 - at)      # noqa: E731
    assert word(492) == 1 and mem.read(a5 - 486, 7) == b"\x06\x00Tally"     # the Apple menu
    assert (word(494), word(500)) == (-13, -13)         # its refnum, and its window's kind
    assert word(502) == 1 and word(552) == 1            # Desk's port after OpenDeskAcc, SystemTask
    assert word(530) == 0                               # no events for its window were Desk's
    assert (word(514), word(516), word(518)) == (1, 1, 1)   # an update, an activate, a deactivate
    assert (word(506), word(508), word(550)) == (2, 1, 1)   # accRun each SystemTask; accCursor once
    assert (flag(532), flag(534), word(510), word(512)) == (0, 1, 1, 0x0778)
    assert (flag(536), word(526), flag(548)) == (1, 71, 0)  # SystemEdit: accCopy; not Desk's window
    assert (word(538), long(542)) == (2, long(498))     # inSysWindow, Tally's
    assert word(520) == 1                               # the click
    assert (long(546), long(524)) == (0, 0xFFF30001)    # MenuKey: Tally's, Count
    assert long(556) == 0 and long(560) == long(230)    # closed: gone, and Desk's window in front
    assert (word(504), word(528)) == (2, 2)             # opened twice, and closed when Desk quit
    table = mem.r32(lowmem.UTableBase)
    assert mem.r32(table + 4 * 12) == 0 and machine.drivers == {}
    assert proc.runner.unimplemented_calls == {}


# -- Vectors: a program's own exception handlers -------------------------------------
def test_a_programs_own_exception_handlers(harness):
    """Vectors sets off TRAP #1 and divides by zero, twice each, and CHK
    once, with its own handlers in their vectors: each goes back past the
    instruction.
    Then the VIA interrupts its own handler for it, which goes on to the
    ROM's with TRAP #0, as Flight Simulator's does; and when it quits, the
    vectors that pointed into it are the System's again."""
    h = harness()
    h.launch(Recorder("Other"))
    f = open_application(APPS / "Vectors.bin")
    proc = h.d.launch(f.name, None, app=f)
    mem, a5 = proc.system.mem, proc.partition.a5
    machine = proc.runner.machine
    h.wait_until(lambda: proc not in h.d.processes, timeout=10, what="Vectors to finish")
    assert not h.d.system_errors
    assert (mem.r16s(a5 - 2), mem.r16s(a5 - 4), mem.r16s(a5 - 6)) == (2, 2, -1)
    assert mem.r16(a5 - 8) == 7                     # what was there: no quotient
    assert mem.r16s(a5 - 26) == 1                   # CHK's
    assert mem.r16s(a5 - 12) >= 5                   # the VIA's interrupts
    assert mem.r16s(a5 - 14) == mem.r16s(a5 - 12)   # each with a flag to clear
    assert mem.r16s(a5 - 16) >= 1                   # which the ROM's handler did
    rom = mem.r32(a5 - 24)
    assert rom == mem.r32(lowmem.ROMBase) and mem.r16(rom + 8) == 0x0075
    assert mem.r32(a5 - 20) == machine.bomb_stub
    assert rom <= machine.bomb_stub and machine.bomb_stub > mem.r32(lowmem.ScrnBase)
    assert mem.r32(0x14) == mem.r32(0x18) == mem.r32(0x84) == machine.bomb_stub
    assert mem.r32(0x64) == machine.level_1
    assert proc.runner.unimplemented_calls == {}


# -- Tempo: a routine of its own in Lvl1DT ----------------------------------------
def test_a_programs_own_routine_in_lvl1dt_runs_each_time_the_via_flags_it(harness):
    """Tempo puts its own routine for timer 1 in Lvl1DT, as Master Tracks
    Pro does for its tempo's clock, and runs the timer free at 100 Hz: the
    ROM's handler calls it each time round, with A1 pointing at the VIA,
    and keeps D0-D3 and A0-A3 for the code it interrupted."""
    h = harness()
    h.launch(Recorder("Other"))
    f = open_application(APPS / "Tempo.bin")
    proc = h.d.launch(f.name, None, app=f)
    mem, a5 = proc.system.mem, proc.partition.a5
    machine = proc.runner.machine
    h.wait_until(lambda: proc not in h.d.processes, timeout=10, what="Tempo to finish")
    assert not h.d.system_errors
    assert mem.r16s(a5 - 2) >= 30                       # its runs
    assert mem.r16s(a5 - 4) == 0                        # each with the VIA in A1
    assert mem.r16s(a5 - 6) == 0                        # D2 and A2 as they were
    ticks = mem.r32(a5 - 14) - mem.r32(a5 - 10)
    assert 16 <= ticks <= 120                           # 30 times round: 18 ticks
    assert mem.r32(0x64) == machine.level_1             # the ROM's handler all along
    assert mem.r32(lowmem.Lvl1DT + 24) == machine.level_1_routines + 6 * LEVEL_1_ROUTINE
    assert proc.runner.unimplemented_calls == {}


# -- Sounds: the Sound Driver and the sound buffer -----------------------------------
def test_the_sound_driver_plays_its_three_synthesizers_and_the_buffer_is_heard(
        harness, tmp_path):
    """Sounds writes the Sound Driver a square wave, synchronously; four
    tones, asynchronously; and a free-form sound with a count of 0, which
    holds until KillIO stops it.  Then it fills the sound buffer itself
    and turns the speaker on.  What the speaker played is in the WAV
    file, a frame a tick."""
    wav = tmp_path / "sounds.wav"
    h = harness(record_sound=str(wav))
    h.launch(Recorder("Other"))
    f = open_application(APPS / "Sounds.bin")
    proc = h.d.launch(f.name, None, app=f)
    mem, a5 = proc.system.mem, proc.partition.a5
    h.wait_until(lambda: proc not in h.d.processes, timeout=10, what="Sounds to finish")
    assert not h.d.system_errors
    assert proc.runner.unimplemented_calls == {}
    word = lambda at: mem.r16s(a5 + at)             # noqa: E731
    assert word(-102) == 0                          # the square wave: noErr
    # Its 5 ticks, the first of them perhaps just after StartSound, as on a Mac.
    assert 4 <= mem.r32(a5 - 106) <= 60
    assert mem.r32(a5 - 110) == 20                  # all of it
    assert (word(-112), word(-114)) == (1, 0)       # four tones: done, noErr
    assert (word(-120), word(-116), word(-118)) == (0, 1, -27)  # held, then abortErr
    assert (word(-122), word(-124)) == (0, -27)     # KillIO, and the write's ioResult
    record = a5 - 174
    assert mem.r16s(record) == 0                    # its duration, counted down
    assert mem.r32(record + 6) == (4 * 370 & 0xFF) << 16    # the first tone's phase
    assert mem.r8(lowmem.SoundActive) == 0
    h.d.shutdown(quit_app=False)

    with wave.open(str(wav)) as w:
        assert (w.getnchannels(), w.getsampwidth(), w.getframerate()) == (1, 1, 22200)
        samples = w.readframes(w.getnframes())
    frames = [samples[i:i + 370] for i in range(0, len(samples), 370)]
    levels = [frozenset(f) for f in frames]
    assert levels.count({28, 228}) == 3             # 200 about the middle, for 3 ticks
    assert levels.count({78, 178}) == 2             # then 100, for 2
    ramp = bytes((i & 0xFF) >> 2 for i in range(4 * 370))
    tones = [ramp[i:i + 370] for i in range(0, len(ramp), 370)]
    assert [f for f in frames if f in tones] == tones
    # The free-form sound, held for its five ticks: one may go by with no
    # frame of its own when the host is late with a tick.
    assert levels.count({0xC0}) >= 3
    assert levels.count({0x40}) >= 4                # the buffer, written straight


# -- ResStub: a patch on _ResourceStub ---------------------------------------------
def test_a_patch_on_the_resource_stub_reads_resources_in(harness):
    """ResStub patches _ResourceStub and gets STR 128, which its patch
    leaves to the original; STR 129, which the patch makes; and STR 130
    with ResLoad off, which it then loads."""
    h = harness()
    h.launch(Recorder("Other"))
    f = open_application(APPS / "ResStub.bin")
    proc = h.d.launch(f.name, None, app=f)
    mem, a5 = proc.system.mem, proc.partition.a5
    h.wait_until(lambda: proc not in h.d.processes, timeout=10, what="ResStub to finish")
    assert not h.d.system_errors
    assert mem.r16s(a5 - 30) == 4                       # one call for each, LoadResource too
    assert mem.r32(a5 - 8) == mem.r32(a5 - 12) != 0     # A4: the application's map
    assert mem.r16s(a5 - 32) == 128                     # A2: the entry
    assert mem.r16s(a5 - 34) == -1                      # A3 too
    assert mem.r16s(a5 - 36) == -1                      # and they all came back
    assert mem.read(a5 - 40, 4) == b"\xff\xff\x00\xff"   # ResLoad: on, on, off, LoadResource's on
    assert mem.read(a5 - 16, 4) == b"\x04rea"            # read
    assert mem.read(a5 - 20, 4) == b"\x04mad"            # made
    assert mem.r32(a5 - 28) == 0                        # empty with ResLoad off,
    assert mem.read(a5 - 24, 4) == b"\x06loa"            # and then loaded
    assert proc.runner.unimplemented_calls == {}


# -- OSPatch: patches on an OS trap ----------------------------------------------
def test_an_os_trap_patch_is_called_as_the_dispatcher_calls_it(harness):
    """OSPatch patches _GetHandleSize twice: once to look at what the trap
    dispatcher gives it and go on to the original, and once to go back to
    the caller itself, taking the dispatcher's frame off, as Microsoft
    Word's patch does."""
    h = harness()
    h.launch(Recorder("Other"))
    f = open_application(APPS / "OSPatch.bin")
    proc = h.d.launch(f.name, None, app=f)
    mem, a5 = proc.system.mem, proc.partition.a5
    h.wait_until(lambda: proc not in h.d.processes, timeout=10, what="OSPatch to finish")
    assert not h.d.system_errors
    assert mem.r16(a5 - 10) == 0xA025                       # D1: the trap word
    assert mem.r16(a5 - 12) == 0x25 << 2                    # D2: its place in the table
    assert mem.r32(a5 - 16) == mem.r32(a5 - 52)             # A2: the patch
    assert mem.r32(a5 - 20) == mem.r32(a5 - 8)              # stacked: A0,
    assert mem.r32(a5 - 24) == 0x12345678                   # A1,
    assert mem.r16(a5 - 26) == 0xA025                       # the trap word,
    assert mem.r32(a5 - 30) & 0xFFFFFF == mem.r32(a5 - 34) & 0xFFFFFF    # where it goes back
    assert mem.r32(a5 - 38) == 10
    assert mem.r16s(a5 - 40) == -1                          # all put back
    assert mem.r16s(a5 - 42) == -1                          # the flags D0's
    assert mem.r32(a5 - 46) == 0x1234                       # the second patch's answer,
    assert mem.r16s(a5 - 48) == -1                          # all as they were
    assert proc.runner.unimplemented_calls == {}


# -- AEvents: the Apple Event Manager ---------------------------------------------
def test_apple_events_reach_their_handlers(harness):
    """AEvents says in its SIZE resource that it takes high-level events,
    so it's sent Open Application when it starts.  It opens a PPC port,
    looks at the event with GetSpecificHighLevelEvent and then has
    AEProcessAppleEvent call its handler, which sends itself an event
    with a list in it, waiting for the reply, and then Quit."""
    h = harness()
    h.launch(Recorder("Other"))
    f = open_application(APPS / "AEvents.bin")
    proc = h.d.launch(f.name, None, app=f)
    mem, a5 = proc.system.mem, proc.partition.a5
    h.wait_until(lambda: proc not in h.d.processes, timeout=10, what="AEvents to quit")
    assert not h.d.system_errors
    assert proc.runner.unimplemented_calls == {}
    assert mem.r16s(a5 - 2) == 0                    # every call that should work did
    assert mem.r16(a5 - 4) == 1                     # the filter saw one event
    assert mem.r16(a5 - 6) == 0                     # and turned it down
    assert mem.read(a5 - 14, 8) == b"aevtoapp"      # Open Application
    assert mem.read(a5 - 18, 4) == b"OAPP"          # its handler's refcon
    assert mem.r16s(a5 - 20) == 3                   # from another process: kAELocalProcess
    assert mem.r16s(a5 - 22) == -1701               # no 'miss': errAEDescNotFound
    assert mem.r32(a5 - 32) == 2                    # the list's items
    assert mem.r32(a5 - 36) == 56                   # "56" as a long
    assert mem.r16s(a5 - 38) == 1234                # 1234 as a short
    assert mem.r32s(a5 - 26) == 1290                # the reply's 'rslt'
    assert mem.r16s(a5 - 28) == -1234               # and its 'errn', the handler's result
    assert mem.r16(a5 - 44) == 1                    # Quit came
    assert mem.read(a5 - 52, 4) == b"aevt"          # as the current event
    port = mem.read(a5 - 202, 72)                   # the Process Manager's name for its port
    assert port[:10] == b"\0\0\x07AEvents" and port[36:46] == b"\0\x01MACRep01"
    assert (mem.r32(a5 - 266), mem.r32(a5 - 262)) == (0, proc.serial_number)    # and back
    assert mem.r16s(a5 - 126) > 0                   # PPCOpen's port
    assert mem.r16s(a5 - 128) == -910               # opened again: portNameExistsErr
    assert mem.r16s(a5 - 130) == -903               # closed again: noPortErr


# -- Menus: an application's own menu bar definition procedure ------------------
MBDF_LOG, MBDF_SAVED, CHOICE = -300, -310, -238      # menus.s's globals, below A5


def mbdf_log(proc):
    """What Menus's MBDF has been sent: (message, param1, param2)."""
    mem, log = proc.system.mem, proc.partition.a5 + MBDF_LOG
    return [(mem.r16(log + 2 + 8 * i), mem.r16(log + 4 + 8 * i), mem.r32(log + 6 + 8 * i))
            for i in range(mem.r16(log))]


@pytest.mark.parametrize("how", ["drag", "click"])
def test_an_applications_mbdf_hears_each_menu_pulled_down(harness, how):
    """InitProcMenu installs Menus's MBDF.  As File is pulled down the
    MBDF is asked where it goes and told to save what's under it, and it
    enables an item there, which can then be chosen; when the menu's
    gone it's told to restore the screen.  A quick click leaves the menu
    open, as it does without an MBDF."""
    h = harness()
    h.launch(Recorder("Other"))
    f = open_application(APPS / "Menus.bin")
    proc = h.d.launch(f.name, None, app=f)
    h.wait_until(lambda: proc.windows and proc.windows[0].visible, timeout=10,
                 what="Menus's window")
    mem, a5 = proc.system.mem, proc.partition.a5
    assert mbdf_log(proc) == [(k.mbInit, 0, 0)]
    assert proc.menus.mbdf_id == 128
    title = h.menu_title_point(h.d.default_bar(proc), "File")
    if how == "drag":
        h.press(*title)
    else:
        h.click(*title)
    h.wait_until(lambda: QApplication.activePopupWidget() is not None, what="pulldown")
    pd = QApplication.activePopupWidget()
    assert pd.menu.selectable(2)                    # (Later, enabled by the MBDF
    log = mbdf_log(proc)
    assert log[:2] == [(k.mbInit, 0, 0), (k.mbRect, 0, 6)]       # File's entry is at 6
    assert log[2][:2] == (k.mbSave, 6) and len(log) == 3
    top_left = h.d.mac_from_global(pd.geometry().topLeft())
    assert mem.r_rect(a5 + MBDF_SAVED) == Rect(
        top_left.v, top_left.h, top_left.v + pd.height() - theme.MENU_SHADOW,
        top_left.h + pd.width() - theme.MENU_SHADOW)
    x, y = h.item_point(2)
    if how == "drag":
        h.move(x, y)
        h.release(x, y)
    else:
        assert h.d.tracker.sticky
        h.click(x, y)
    h.wait_until(lambda: mem.r32(a5 + CHOICE) == 129 << 16 | 2, what="File > Later")
    assert mbdf_log(proc)[3:] == [(k.mbRestore, 0, 0)]
    assert proc.runner.unimplemented_calls == {}


# -- Frames: an application's own WDEF -------------------------------------------
WDEF_LOG = -700             # wdef.s's, below A5
FRAMES_CLICKS = -236


def wdef_log(proc):
    """What Frames's WDEF has been sent: (message, param)."""
    mem, log = proc.system.mem, proc.partition.a5 + WDEF_LOG
    return [(mem.r16(log + 2 + 6 * i), mem.r32(log + 4 + 6 * i)) for i in range(mem.r16(log))]


def launch_frames(h):
    h.launch(Recorder("Other"))
    f = open_application(APPS / "Frames.bin")
    proc = h.d.launch(f.name, None, app=f)
    h.wait_until(lambda: proc.windows and proc.windows[0].frame_image is not None
                 and h.d.views[proc.windows[0]].isVisible(), timeout=10,
                 what="Frames's window, shaped and drawn by its WDEF")
    rec = proc.windows[0]
    h.wait_until(lambda: rec.hilited and striped(rec), what="its frame drawn active")
    return proc, rec


def striped(rec):
    """Does the frame its WDEF drew have an active window's title bar?"""
    f, _, (w, _) = frames_geometry(rec)
    img = rec.frame_image[2]
    return all(img.pixel(w // 2 + 40, y) & 0xFFFFFF == 0 for y in (4, 6))


def frames_geometry(rec):
    """The host's frame for a documentProc window, which WDEF 0 gives
    Frames's, and its structure's top left."""
    f = chrome.Frame.for_window(rec, definition=describe_proc(k.documentProc))
    l, t, _, _ = chrome.insets(f)
    c = rec.content
    return f, Point(c.top - t, c.left - l), chrome.structure_size(f, c.width, c.height)


def test_an_applications_wdef_shapes_and_draws_its_window(harness):
    """Frames's WDEF is told its window is new, then asked for its regions
    and to draw it; it leaves those to WDEF 0, as Excel's do.  The window
    on the host takes the structure region's shape and shows what was
    drawn, and a click in it is where the WDEF says it is."""
    h = harness()
    proc, rec = launch_frames(h)
    mem = proc.system.mem
    assert wdef_log(proc)[:3] == [(k.wNew, 0), (k.wCalcRgns, 0), (k.wDraw, 0)]
    assert rec.own_wdef and rec.wdef_shaped
    f, o, (w, ht) = frames_geometry(rec)
    s = chrome.shadow(f)
    struc = qdraw.read_rgn(mem, rec.struc_rgn)
    assert struc.bbox == Rect(o.v, o.h, o.v + ht + s, o.h + w + s)
    assert qdraw.read_rgn(mem, rec.cont_rgn) == qdraw.Region.rect(rec.content)
    view = h.d.views[rec]
    assert h.mac(view, QPoint(0, 0)) == (o.h, o.v)
    assert (view.width(), view.height()) == (w + s, ht + s)
    assert not view.mask().contains(QPoint(w, 0))       # the shadow's offset
    assert view.bar is None
    left, top, img = rec.frame_image
    assert (left, top) == (o.h - rec.content.left, o.v - rec.content.top)
    assert img.pixel(0, 0) & 0xFFFFFF == 0              # the frame
    h.click(rec.content.left + 10, rec.content.top + 10)
    h.wait_until(lambda: mem.r16(proc.partition.a5 + FRAMES_CLICKS) == 1, what="a click")
    assert (k.wHit, (rec.content.top + 10) << 16 | rec.content.left + 10) in wdef_log(proc)
    assert proc.runner.unimplemented_calls == {}


def test_an_applications_wdef_window_drags_and_closes(harness):
    """Its WDEF says where its title bar and close box are: the window
    drags by the one, with its regions, and the other is highlighted by
    the WDEF as it's clicked, then the window goes, telling its WDEF."""
    h = harness()
    proc, rec = launch_frames(h)
    mem = proc.system.mem
    before, struc = rec.content, qdraw.read_rgn(mem, rec.struc_rgn)
    f, o, (w, _) = frames_geometry(rec)
    th, tv = o.h + w // 2 + 40, o.v + 5
    h.press(th, tv)
    h.wait_until(lambda: h.d.tracker is not None, what="the drag")
    h.move(th + 30, tv + 20)
    h.move(th + 60, tv + 40)
    h.release(th + 60, tv + 40)
    h.wait_until(lambda: rec.content == before.offset(60, 40), what="the window dragged")
    h.wait_until(lambda: qdraw.read_rgn(mem, rec.struc_rgn) == struc.offset(60, 40),
                 what="its structure region with it")
    assert h.mac(h.d.views[rec], QPoint(0, 0)) == (o.h + 60, o.v + 40)
    f, o, (w, _) = frames_geometry(rec)
    box = chrome.close_box_rect(f, w).center()
    h.click(o.h + box.x(), o.v + box.y())
    h.wait_until(lambda: not proc.windows, what="the window closed")
    log = wdef_log(proc)
    assert log.count((k.wDraw, k.wInGoAway)) == 2       # the box highlighted and back
    assert log[-1] == (k.wDispose, 0)
    assert rec not in h.d.views
    assert proc.runner.unimplemented_calls == {}


# -- Edit: TextEdit and the scrap ------------------------------------------------
TEH = -238                  # edit.s's edit record, below A5
TOP, LEFT = 4, 4            # its destRect's top left, local


def edit_record(proc):
    mem = proc.system.mem
    return te.TERec(mem, mem.r32(mem.r32(proc.partition.a5 + TEH)))


def edit_text(proc):
    rec = edit_record(proc)
    mem = proc.system.mem
    return mem.read(mem.r32(rec.h_text), rec.te_length)


def launch_edit(h):
    f = open_application(APPS / "Edit.bin")
    h.launch(Recorder("Other"))
    proc = h.d.launch(f.name, None, app=f)
    h.wait_until(lambda: proc.windows and proc.windows[0].visible, timeout=10,
                 what="Edit's window")
    h.wait_until(lambda: proc.system.mem.r32(proc.partition.a5 + TEH)
                 and edit_record(proc).active, what="Edit's record to be active")
    return proc, proc.windows[0]


def typed(h, proc, text, expect):
    QTest.keyClicks(h.d.screen_widget, text)
    h.wait_until(lambda: edit_text(proc) == expect, what=f"{expect!r} in the record")


@pytest.fixture
def clipboard(qapp):
    cb = QApplication.clipboard()
    cb.clear()
    yield cb
    cb.clear()


def test_edit_types_into_its_record(harness, clipboard):
    h = harness()
    proc, rec = launch_edit(h)
    typed(h, proc, "Hello world", b"Hello world")
    r = edit_record(proc)
    assert (r.sel_start, r.sel_end, r.n_lines) == (11, 11, 1)
    width = QFontMetrics(qdraw.Fonts(QFont()).font(4, 9, 0)).horizontalAdvance
    line = range(TOP, TOP + r.line_height)
    h.wait_until(lambda: any(black(rec, x, y) for x in range(LEFT, LEFT + width("H"))
                             for y in line), what="the text drawn")
    assert not any(black(rec, x, y) for x in range(LEFT + width("Hello world") + 2, 150)
                   for y in line)

    h.key(Qt.Key.Key_Backspace)
    h.key(Qt.Key.Key_Left)
    h.key(Qt.Key.Key_Left)
    typed(h, proc, "a", b"Hello woarl")
    h.key(Qt.Key.Key_Return)
    h.wait_until(lambda: edit_record(proc).n_lines == 2, what="a second line")
    assert edit_text(proc) == b"Hello woa\rrl"
    assert proc.runner.unimplemented_calls == {}


def test_edit_cuts_copies_and_pastes_through_the_host_clipboard(harness, clipboard):
    h = harness()
    proc, rec = launch_edit(h)
    typed(h, proc, "Hello world", b"Hello world")
    width = QFontMetrics(qdraw.Fonts(QFont()).font(4, 9, 0)).horizontalAdvance
    c, mid = rec.content, TOP + edit_record(proc).line_height // 2
    word = c.left + LEFT + (width("Hello ") + width("Hello wor")) // 2
    h.click(word, c.top + mid)
    h.click(word, c.top + mid)                         # a double-click selects the word
    h.wait_until(lambda: (edit_record(proc).sel_start, edit_record(proc).sel_end) == (6, 11),
                 what="the word selected")

    h.key(Qt.Key.Key_C, Qt.KeyboardModifier.ControlModifier)
    h.wait_until(lambda: clipboard.text() == "world", what="the copy on the host clipboard")
    h.click(c.left + 200, c.top + mid)                 # past the end of the line
    h.wait_until(lambda: edit_record(proc).sel_start == 11, what="the insertion point")
    h.key(Qt.Key.Key_V, Qt.KeyboardModifier.ControlModifier)
    h.wait_until(lambda: edit_text(proc) == b"Hello worldworld", what="the paste")

    clipboard.setText("café\nau lait")                 # from another host application
    h.key(Qt.Key.Key_V, Qt.KeyboardModifier.ControlModifier)
    h.wait_until(lambda: edit_text(proc) == b"Hello worldworldcaf\x8e\rau lait",
                 what="the host's text pasted")
    assert edit_record(proc).n_lines == 2

    h.click(c.left + LEFT + 1, c.top + mid)            # select "Hello" and cut it
    h.click(c.left + LEFT + 1, c.top + mid)
    h.wait_until(lambda: edit_record(proc).sel_end == 5, what="Hello selected")
    h.key(Qt.Key.Key_X, Qt.KeyboardModifier.ControlModifier)
    h.wait_until(lambda: clipboard.text() == "Hello", what="the cut on the host clipboard")
    assert edit_text(proc) == b" worldworldcaf\x8e\rau lait"
    assert proc.runner.unimplemented_calls == {}


def test_edit_reads_the_scrap_again_when_it_comes_forward(harness, clipboard):
    """The resume event's convertClipboard: the scrap changed while Edit
    was in the background, so it brings it into TextEdit's scrap."""
    h = harness()
    proc, rec = launch_edit(h)
    other = next(p for p in h.d.processes if p is not proc)
    h.d.switch_to(other)
    h.wait_until(lambda: not edit_record(proc).active, what="Edit to be suspended")
    clipboard.setText("from the host")
    h.d.switch_to(proc)
    h.wait_until(lambda: te.te_scrap(proc.world, proc.system.mem) == b"from the host",
                 what="TextEdit's scrap")
    assert edit_text(proc) == b""
