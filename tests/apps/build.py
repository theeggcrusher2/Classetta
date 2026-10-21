"""Build the test applications: assemble each tests/apps/*.s with GNU as
for m68k and wrap the code, with the resources the application needs,
in a MacBinary file next to it.

    python tests/apps/build.py

The assembler is binutils for m68k (Debian/Ubuntu: binutils-m68k-linux-gnu).
The built .bin files are checked in, so the tests don't need it.

Each application is one code segment, CODE 1, whose first entry in the
jump table is where it starts; CODE 0 describes its A5 world as a Mac
linker would leave it.
"""

from __future__ import annotations

import binascii
import os
import struct
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path[:0] = [str(HERE.parent.parent), str(HERE.parent)]

from builders import alrt, ditl, dlog, menu, size_res, wind  # noqa: E402

from macrt.resfork import Resource, build_fork  # noqa: E402
from macrt.toolbox import constants as k  # noqa: E402
from macrt.toolbox.templates import (  # noqa: E402
    btnCtrl, chkCtrl, ctrlItem, editText, itemDisable, statText, userItem)

AS = os.environ.get("M68K_AS", "m68k-linux-gnu-as")
OBJCOPY = os.environ.get("M68K_OBJCOPY", "m68k-linux-gnu-objcopy")
LOCKED_PRELOAD = 0x10 | 0x04            # resLocked | resPreload


def assemble(source: Path) -> bytes:
    with tempfile.TemporaryDirectory() as d:
        obj, raw = Path(d, "a.o"), Path(d, "a.bin")
        subprocess.run([AS, "-m68000", "--register-prefix-optional", "-I", str(HERE),
                        "-o", str(obj), str(source)], check=True)
        subprocess.run([OBJCOPY, "-O", "binary", "-j", ".text", str(obj), str(raw)], check=True)
        return raw.read_bytes()


def code_resources(code: bytes, globals_size: int) -> list[Resource]:
    """CODE 0 and CODE 1 for one segment that starts at its first byte."""
    jump_table = struct.pack(">HHHH", 0, 0x3F3C, 1, 0xA9F0)   # offset 0, MOVE.W #1,-(SP), _LoadSeg
    above_a5 = 32 + len(jump_table)
    code0 = struct.pack(">IIII", above_a5, globals_size, len(jump_table), 32) + jump_table
    code1 = struct.pack(">HH", 0, 1) + code                    # first entry 0, one entry
    return [Resource(b"CODE", 0, code0, b"", 0x20 | LOCKED_PRELOAD),  # resPurgeable too
            Resource(b"CODE", 1, code1, b"Main", 0x20)]


def macbinary(name: str, data: bytes, rsrc: bytes, ftype: bytes, creator: bytes,
              flags: int = 0) -> bytes:
    """A MacBinary II file."""
    n = name.encode("mac_roman")
    h = bytearray(128)
    h[1] = len(n)
    h[2:2 + len(n)] = n
    h[65:69], h[69:73] = ftype, creator
    h[73] = flags >> 8
    struct.pack_into(">II", h, 83, len(data), len(rsrc))
    h[101] = flags & 0xFF
    h[122], h[123] = 129, 129                           # MacBinary II, readable by II
    struct.pack_into(">H", h, 124, binascii.crc_hqx(bytes(h[:124]), 0))
    pad = lambda b: b + bytes(-len(b) % 128)            # noqa: E731
    return bytes(h) + pad(data) + pad(rsrc)


def hello() -> bytes:
    code = assemble(HERE / "hello.s")
    resources = code_resources(code, globals_size=236) + [
        Resource(b"MENU", 128, menu(128, "\x14", [("About Hello…", ""), ("(-", "")])),
        Resource(b"MENU", 129, menu(129, "File", [("Quit", "Q")])),
        Resource(b"WIND", 128, wind((60, 40, 220, 300), "Hello", k.documentProc, True, True)),
        Resource(b"SIZE", -1, size_res(256 * 1024, 128 * 1024)),
    ]
    return macbinary("Hello", b"", build_fork(resources), b"APPL", b"HELO")


def cntl(bounds, value, visible, max_, min_, proc_id, ref_con=0, title="") -> bytes:
    t = title.encode("mac_roman")
    return struct.pack(">4hhBxhhhI", *bounds, value, visible, max_, min_, proc_id,
                       ref_con) + bytes([len(t)]) + t


def controls() -> bytes:
    code = assemble(HERE / "controls.s")
    resources = code_resources(code, globals_size=266) + [
        Resource(b"WIND", 128, wind((60, 40, 220, 300), "Controls", k.documentProc, True, True)),
        Resource(b"CNTL", 128, cntl((10, 200, 150, 216), 0, True, 10, 0, k.scrollBarProc)),
        Resource(b"SIZE", -1, size_res(256 * 1024, 128 * 1024)),
    ]
    return macbinary("Controls", b"", build_fork(resources), b"APPL", b"CTLS")


def dialogs() -> bytes:
    code = assemble(HERE / "dialogs.s")
    button, check = ctrlItem + btnCtrl, ctrlItem + chkCtrl
    resources = code_resources(code, globals_size=526) + [
        Resource(b"DLOG", 128, dlog((80, 60, 260, 360), "", 128)),
        Resource(b"DITL", 128, ditl(
            (button, (140, 210, 160, 280), b"OK"),
            (button, (140, 120, 160, 190), b"Cancel"),
            (statText, (10, 10, 30, 290), b"Hello, ^0!"),
            (editText, (45, 13, 61, 200), b"initial"),
            (check, (75, 10, 93, 120), b"Option"),
            (userItem | itemDisable, (75, 200, 115, 240), b""),
            (statText | itemDisable, (100, 10, 120, 120), b"Hidden"),
        )),
        Resource(b"ALRT", 129, alrt((100, 100, 200, 400), 129)),
        Resource(b"DITL", 129, ditl(
            (button, (70, 220, 90, 290), b"OK"),
            (statText | itemDisable, (10, 60, 50, 290), b"All ^1."),
        )),
        Resource(b"SIZE", -1, size_res(256 * 1024, 128 * 1024)),
    ]
    return macbinary("Dialogs", b"", build_fork(resources), b"APPL", b"DLGS")


def launcher() -> bytes:
    code = assemble(HERE / "launcher.s")
    resources = code_resources(code, globals_size=8)
    return macbinary("Launcher", b"", build_fork(resources), b"APPL", b"LNCH")


def edit() -> bytes:
    code = assemble(HERE / "edit.s")
    resources = code_resources(code, globals_size=246) + [
        Resource(b"MENU", 128, menu(128, "\x14", [("About Edit…", ""), ("(-", "")])),
        Resource(b"MENU", 129, menu(129, "File", [("Quit", "Q")])),
        Resource(b"MENU", 130, menu(130, "Edit", [("(Undo", "Z"), ("(-", ""), ("Cut", "X"),
                                                  ("Copy", "C"), ("Paste", "V"), ("Clear", "")])),
        Resource(b"WIND", 128, wind((60, 40, 220, 300), "Edit", k.documentProc, True, False)),
        Resource(b"SIZE", -1, size_res(256 * 1024, 128 * 1024)),
    ]
    return macbinary("Edit", b"", build_fork(resources), b"APPL", b"EDIT")


def lists() -> bytes:
    code = assemble(HERE / "lists.s")
    resources = code_resources(code, globals_size=262) + [
        Resource(b"WIND", 128, wind((60, 40, 220, 300), "Lists", k.documentProc, True, True)),
        Resource(b"SIZE", -1, size_res(256 * 1024, 128 * 1024)),
    ]
    return macbinary("Lists", b"", build_fork(resources), b"APPL", b"LSTS")


def vbl() -> bytes:
    code = assemble(HERE / "vbl.s")
    resources = code_resources(code, globals_size=32)
    return macbinary("VBL", b"", build_fork(resources), b"APPL", b"VBLT")


def timer() -> bytes:
    code = assemble(HERE / "timer.s")
    resources = code_resources(code, globals_size=62)
    return macbinary("Timer", b"", build_fork(resources), b"APPL", b"TIMR")


def poll() -> bytes:
    code = assemble(HERE / "poll.s")
    resources = code_resources(code, globals_size=24)
    return macbinary("Poll", b"", build_fork(resources), b"APPL", b"POLL")


def vectors() -> bytes:
    code = assemble(HERE / "vectors.s")
    resources = code_resources(code, globals_size=26)
    return macbinary("Vectors", b"", build_fork(resources), b"APPL", b"VECT")


def tempo() -> bytes:
    code = assemble(HERE / "tempo.s")
    resources = code_resources(code, globals_size=14)
    return macbinary("Tempo", b"", build_fork(resources), b"APPL", b"TMPO")


def sounds() -> bytes:
    code = assemble(HERE / "sounds.s")
    resources = code_resources(code, globals_size=452)
    return macbinary("Sounds", b"", build_fork(resources), b"APPL", b"SNDS")


def menus() -> bytes:
    code = assemble(HERE / "menus.s")
    resources = code_resources(code, globals_size=310) + [
        Resource(b"MBDF", 128, assemble(HERE / "mbdf.s")),
        Resource(b"MENU", 129, menu(129, "File", [("Quit", "Q"), ("(Later", "")])),
        Resource(b"WIND", 128, wind((60, 40, 220, 300), "Menus", k.documentProc, True, False)),
        Resource(b"SIZE", -1, size_res(256 * 1024, 128 * 1024)),
    ]
    return macbinary("Menus", b"", build_fork(resources), b"APPL", b"MNUS")


def frames() -> bytes:
    code = assemble(HERE / "frames.s")
    resources = code_resources(code, globals_size=700) + [
        Resource(b"WDEF", 128, assemble(HERE / "wdef.s")),
        Resource(b"WIND", 128, wind((60, 40, 220, 300), "Frames", 128 << 4 | k.documentProc)),
        Resource(b"SIZE", -1, size_res(256 * 1024, 128 * 1024)),
    ]
    return macbinary("Frames", b"", build_fork(resources), b"APPL", b"FRMS")


def aevents() -> bytes:
    code = assemble(HERE / "aevents.s")
    high_level_event_aware = 0x0040
    resources = code_resources(code, globals_size=266) + [
        Resource(b"SIZE", -1, size_res(256 * 1024, 128 * 1024, flags=high_level_event_aware)),
    ]
    return macbinary("AEvents", b"", build_fork(resources), b"APPL", b"MACR")


def resstub() -> bytes:
    code = assemble(HERE / "resstub.s")
    resources = code_resources(code, globals_size=40) + [
        Resource(b"STR ", 128, b"\x04read"),
        Resource(b"STR ", 129, b"\x07on disk"),
        Resource(b"STR ", 130, b"\x06loaded"),
    ]
    return macbinary("ResStub", b"", build_fork(resources), b"APPL", b"RSTB")


def ospatch() -> bytes:
    code = assemble(HERE / "ospatch.s")
    return macbinary("OSPatch", b"", build_fork(code_resources(code, globals_size=52)),
                     b"APPL", b"OSPT")


def driver() -> bytes:
    code = assemble(HERE / "driver.s")
    resources = code_resources(code, globals_size=242) + [
        Resource(b"DRVR", 20, assemble(HERE / "echo.s"), b".Echo"),
    ]
    return macbinary("Driver", b"", build_fork(resources), b"APPL", b"DRVT")


def desk() -> bytes:
    code = assemble(HERE / "desk.s")
    resources = code_resources(code, globals_size=576) + [
        Resource(b"WIND", 128, wind((60, 40, 220, 300), "Desk", k.documentProc, True, True)),
        Resource(b"DRVR", 12, assemble(HERE / "tally.s"), b"\0Tally"),
        Resource(b"SIZE", -1, size_res(256 * 1024, 128 * 1024)),
    ]
    return macbinary("Desk", b"", build_fork(resources), b"APPL", b"DESK")


APPS = {"Hello.bin": hello, "Launcher.bin": launcher, "Controls.bin": controls,
        "Dialogs.bin": dialogs, "Edit.bin": edit, "Lists.bin": lists, "Menus.bin": menus,
        "Frames.bin": frames, "VBL.bin": vbl, "Timer.bin": timer, "AEvents.bin": aevents,
        "ResStub.bin": resstub, "OSPatch.bin": ospatch, "Vectors.bin": vectors, "Poll.bin": poll,
        "Tempo.bin": tempo, "Sounds.bin": sounds, "Driver.bin": driver, "Desk.bin": desk}


def main() -> None:
    for name, build in APPS.items():
        (HERE / name).write_bytes(build())
        print(f"built {HERE / name}")


if __name__ == "__main__":
    main()
