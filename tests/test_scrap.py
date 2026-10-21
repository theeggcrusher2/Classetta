"""The Scrap Manager's desk scrap (macrt.toolbox.scrap): its entries,
each application's copy of it as ScrapStuff describes it, the host
clipboard it shares its text with, and TextEdit's private scrap."""

import struct

import pytest
from PyQt6.QtWidgets import QApplication

from macrt.toolbox import lowmem, scrap
from macrt.toolbox import textedit as te
from macrt.toolbox.scrap import TEXT
from macrt.toolbox.system import System


class Direct:
    """The bridge, for code that's on the GUI thread already."""

    def call(self, fn, *args):
        return fn(*args)


@pytest.fixture
def system(qapp):
    return System()


@pytest.fixture
def clipboard(qapp):
    cb = QApplication.clipboard()
    cb.clear()
    QApplication.processEvents()
    yield cb
    cb.clear()


def copy_of(system, world):
    """An application's copy of the scrap: its count and contents."""
    size, h = world.r32(lowmem.ScrapSize), world.r32(lowmem.ScrapHandle)
    return world.r16(lowmem.ScrapCount), system.mem.read(system.mem.r32(h), size)


def test_entries_are_laid_out_as_on_a_mac(system):
    s = scrap.desk_scrap(system)
    part = system.open_partition("App")
    s.zero(None)
    s.put(None, TEXT, b"abc")
    s.put(None, b"PICT", b"\x01\x02")
    assert s.find(TEXT) == (8, b"abc")
    assert s.find(b"PICT") == (20, b"\x01\x02")     # after TEXT, padded to an even length
    assert s.find(b"styl") is None
    assert s.sync(part.world, part.heap) == scrap.noErr
    count, data = copy_of(system, part.world)
    assert data == (TEXT + struct.pack(">I", 3) + b"abc\0"
                    + b"PICT" + struct.pack(">I", 2) + b"\x01\x02")
    assert part.world.r32(lowmem.ScrapSize) == 22 and count == s.count
    assert part.world.r16(lowmem.ScrapState) > 0             # in memory
    assert system.mem.read(part.world.r32(lowmem.ScrapName) + 1, 14) == b"Clipboard File"


def test_unload_scrap_writes_the_clipboard_file(system):
    # Programs look for it in the System Folder after: SuperPaint does.
    s = scrap.desk_scrap(system)
    s.zero(None)
    s.put(None, TEXT, b"abc")
    assert s.unload(system.files) == scrap.noErr
    f = system.files.system_folder.child("Clipboard File")
    assert (f.type, f.creator) == (b"CLIP", b"MACS")
    assert bytes(f.fork(False)) == TEXT + struct.pack(">I", 3) + b"abc\0"
    s.zero(None)
    assert s.unload(system.files) == scrap.noErr
    assert system.files.system_folder.child("Clipboard File") is f and not f.fork(False)


def test_every_copy_catches_up_with_zero_scrap(system):
    s = scrap.desk_scrap(system)
    a, b = system.open_partition("A"), system.open_partition("B")
    s.sync(a.world, a.heap)
    s.sync(b.world, b.heap)
    count = a.world.r16(lowmem.ScrapCount)
    s.zero(None)
    s.put(None, TEXT, b"hi")
    s.sync(a.world, a.heap)
    assert a.world.r16(lowmem.ScrapCount) != count
    assert s.stale(b.world, None) and not s.stale(a.world, None)
    s.sync(b.world, b.heap)
    assert copy_of(system, b.world) == copy_of(system, a.world)


def test_text_goes_to_the_host_clipboard(system, clipboard):
    s = scrap.desk_scrap(system)
    s.zero(Direct())
    s.put(Direct(), TEXT, b"Caf\x8e\rau lait")
    assert clipboard.text() == "Café\nau lait"
    s.put(Direct(), TEXT, b"another")                       # only the first 'TEXT' goes
    assert clipboard.text() == "Café\nau lait"
    s.zero(Direct())
    assert clipboard.text() == ""


def test_the_hosts_text_becomes_the_desk_scrap(system, clipboard):
    s = scrap.desk_scrap(system)
    part = system.open_partition("App")
    bridge = Direct()
    s.from_host(bridge)
    s.sync(part.world, part.heap)
    count = part.world.r16(lowmem.ScrapCount)
    assert not s.stale(part.world, bridge)
    clipboard.setText("naïve\r\nline")
    QApplication.processEvents()
    assert s.stale(part.world, bridge)
    assert s.find(TEXT) == (8, b"na\x95ve\rline")
    s.sync(part.world, part.heap)
    assert part.world.r16(lowmem.ScrapCount) != count
    assert copy_of(system, part.world)[1] == TEXT + struct.pack(">I", 10) + b"na\x95ve\rline"


def test_a_resume_converts_the_clipboard_when_it_has_changed(system, clipboard):
    part = system.open_partition("App")
    bridge = Direct()
    assert not scrap.needs_converting(system, part.world, bridge)  # no scrap at all yet
    s = scrap.desk_scrap(system)
    s.sync(part.world, part.heap)
    assert not scrap.needs_converting(system, part.world, bridge)
    s.zero(bridge)                                  # another application's ZeroScrap
    assert scrap.needs_converting(system, part.world, bridge)


def test_textedits_scrap_is_in_low_memory(system):
    part = system.open_partition("App")
    world, heap, mem = part.world, part.heap, system.mem
    assert te.set_te_scrap(world, heap, b"hello")
    h = world.r32(lowmem.TEScrpHandle)
    assert mem.read(mem.r32(h), 5) == b"hello" and world.r16(lowmem.TEScrpLength) == 5
    assert te.te_scrap(world, mem) == b"hello"
    world.w16(lowmem.TEScrpLength, 3)               # as TESetScrapLength's glue sets it
    assert te.te_scrap(world, mem) == b"hel"
    world.w32(lowmem.TEScrpLength, 4)               # or code that takes it for a long
    assert te.scrap_length(world) == 4
    assert te.set_te_scrap(world, heap, b"") and te.te_scrap(world, mem) == b""
    assert world.r32(lowmem.TEScrpHandle) == h
