"""
The machine: guest RAM and how it's divided up.

    $000000  low-memory globals
    $003000  the system heap: the Window Manager port, the desktop region,
             the System file's resource map and its resources
    $100000  application partitions, one per running application
    top      the screen buffer, 1 bit per pixel

Each partition is laid out like a classic Mac's application space: the
application heap from the bottom, growing toward ApplLimit, the stack
below the A5 world, and the A5 world at the top.  The application's
globals sit below A5 with QuickDraw's last, so thePort is at -4(A5); the
application parameters and the jump table sit above it.
"""

from __future__ import annotations

import struct
import threading
from collections.abc import Sequence
from dataclasses import dataclass

from ..macfile import MacFile
from ..memory import ADDRESS_MASK, RAM, to_mac
from ..resfork import (InvalidForkError, build_fork, open_fork, resCompressed, resLocked,
                       resSysHeap, restype)
from . import lowmem, printing, sysres
from .files import FileManager, FileSystem
from .lowmem import World
from .bitfont import fond_ids
from .memmgr import LOCKED, MIN_BLOCK, MORE_MAST, RESOURCE, Heap
from .resources import UNWRITTEN, Item, ResourceManager, ResourceMap
from .events import InputGlobals
from .screen import Screen
from .qdraw import FAMILIES, MONO
from .quickdraw import QD_GLOBALS_SIZE, BitMap, GrafPort, init_qd_globals, new_rgn
from .types import Rect

RAM_SIZE = 8 << 20
# Over the top of RAM, where a Mac II's is with 24-bit addresses: a page of
# ROM, with the model's version at ROMBase + 8.  Either is one without
# Color QuickDraw, as ROM85 has it.  What the System leaves in the exception
# vectors is in it, above the screen, as it is on a real Mac: Flight
# Simulator takes a vector that isn't for a debugger's.
ROM_BASE, ROM_SIZE = RAM_SIZE, 0x1000
# The rest of the ROM's space, as a Mac II's is, reads 0: Downhill Racer
# keeps ROMBase's high word where it keeps a handle it has none of, and
# reads through it.
ROM_END = ROM_BASE + 0x100000
SYS_HEAP_START = 0x3000
SYS_HEAP_END = SYS_HEAP_START + 0x80000
FONT_ROOM = 0x20000             # how much of a disk's System's fonts it adds
# A 512x342 screen is where a Mac 512K's is, under the top of its RAM.
# Programs from 1984 write it there themselves, and flip to the alternate
# screen under it; the System heap stops short of both.
CLASSIC_SIZE = (512, 342)
CLASSIC_SCREEN, CLASSIC_ALTERNATE = 0x7A700, 0x72700
CLASSIC_END = 0x80000           # the top of a Mac 512K's RAM, over the screen
# A Mac Plus's RAM repeats through its 4 MB of addresses, so its screens are
# at $3FA700 and $3F2700 too, however much RAM it has, and programs from
# 1986 write them there: Apache Strike's flight.  Here those are the classic
# screens, and applications' partitions keep out from under them.
PLUS_SCREENS, PLUS_SCREENS_END = 0x3F2000, 0x400000
DEFAULT_PARTITION = 1 << 20
DEFAULT_STACK = 0x8000
MIN_STACK = 0x400           # MinStack, as a Mac has it
APP_PARAMS = 32             # above A5, before the jump table
MIN_HEAP = 0x8000
INITIAL_HEAP = 0x1800       # the application heap's size at launch
PRINTER_STR = -8192         # STR: the printer resource file's name (IM II-163)


@dataclass(frozen=True)
class Model:
    """Which Mac macrt says it is, to Gestalt, SysEnvirons and in low
    memory.  The processor runs the 68020's instructions and the 68881's
    either way; what the model changes is the exception frames it stacks.
    A 68000's are six bytes, with no format word, and programs from before
    the Mac II pop them themselves: MacWrite 4.5 returns from its TRAP
    handler with ADDQ.W #2,A7 and RTS."""
    name: str
    cpu: int            # CPUFlag: 0 for a 68000, 2 for a 68020
    fpu: bool
    gestalt: int        # Gestalt's 'mach'
    environs: int       # SysEnvirons' machineType
    rom: int            # the ROM's version
    keyboard: int       # Gestalt's and SysEnvirons' keyboard type


MODELS = {
    "plus": Model("Macintosh Plus", 0, False, gestalt=4, environs=2, rom=0x0075, keyboard=3),
    "ii": Model("Macintosh II", 2, True, gestalt=6, environs=4, rom=0x0178, keyboard=5),
}

RTS = 0x4E75
VBL_TASK_SIZE = 14
SYSTEM_FILE = "System"
# The System file's standard definition procedures, and the selectors
# their code passes to _DialogDispatch (see System.defproc).  A selector's
# high byte is the size of the arguments in words.
STANDARD_DEFPROCS = {("WDEF", 0): 0x067E, ("WDEF", 1): 0x067D, ("MDEF", 0): 0x097C,
                     ("CDEF", 0): 0x067B, ("CDEF", 1): 0x067A, ("LDEF", 0): 0x0A79,
                     ("MBDF", 0): 0x0578}


@dataclass(eq=False)
class Partition:
    name: str
    start: int
    end: int
    heap: Heap
    a5: int
    stack_base: int
    world: World
    vbl_task: int = 0       # the System's own task in its VBL queue


class System:
    def __init__(self, screen_size: tuple[int, int] = (640, 480), *, desk_top: int = 0,
                 menu_bar_height: int = 20, system_file: MacFile | None = None,
                 disk_system: MacFile | None = None, speed: float | None = None,
                 model: str = "plus", sound: Sequence = ()):
        """system_file: a real System file to use in place of macrt's own.
        disk_system: the System file on the disk the applications come
        from, whose fonts macrt's own adds (see _disk_fonts).  speed: how
        fast 68k code may run, as a multiple of a Mac Plus; None for as
        fast as it goes.  model: which of MODELS it says it is.  sound:
        where the speaker's sound goes, if anywhere (macrt.m68k.sound's
        Speaker)."""
        w, h = screen_size
        self.speed = speed
        self.sound = list(sound)
        self.model = MODELS[model]
        self.mem = mem = RAM(RAM_SIZE + ROM_SIZE)
        self.lock = threading.RLock()
        row_bytes = (w + 15) // 16 * 2
        # Applications' partitions start where the System heap stops, or
        # over the classic screen, as on a Mac: the first one's heap starts
        # under 1 MB.  Excel 1.0 keeps itself to the first megabyte, and
        # can't start if none of its heap is there.
        self._reserved: list[tuple[int, int]] = []      # what partitions keep out of
        if (w, h) == CLASSIC_SIZE:
            base, sys_heap_end = CLASSIC_SCREEN, CLASSIC_ALTERNATE
            self.partitions_start, self.partitions_end = CLASSIC_END, RAM_SIZE
            mem.mirror(PLUS_SCREENS, PLUS_SCREENS_END, CLASSIC_ALTERNATE & ~0xFFF)
            self._reserved.append((PLUS_SCREENS, PLUS_SCREENS_END))
        else:
            base = (RAM_SIZE - row_bytes * h) & ~3
            if base < SYS_HEAP_END + DEFAULT_PARTITION:
                raise ValueError(f"a {w}x{h} screen leaves no room for applications")
            sys_heap_end = self.partitions_start = SYS_HEAP_END
            self.partitions_end = base
        self.screen_bits = BitMap(base, row_bytes, Rect(0, 0, h, w))
        # The alternate screen buffer, which the VIA can show instead.
        self.alternate_screen = CLASSIC_ALTERNATE if (w, h) == CLASSIC_SIZE else None
        # What the heaps' blocks' sizes are multiples of, as the model's
        # Memory Manager has them: 2 on a 68000, 4 on a 68020 and later.
        self.align = 2 if self.model.cpu == 0 else 4
        self.sys_heap = Heap(mem, SYS_HEAP_START, sys_heap_end, align=self.align)
        self.ports: dict[int, GrafPort] = {}            # canonical views, by address
        self.gestalt_procs: dict[int, int] = {}         # NewGestalt selector -> 68k function
        self.script_settings: dict[int, int] = {}       # SetEnvirons verb -> value
        self.font_records: dict[tuple, int] = {}        # FMSwapFont's FONTs of host fonts
        self._partitions: list[Partition] = []
        self._zones: dict[int, tuple[Heap, Partition | None]] = {}     # InitZone's, by start
        self._rts = self.sys_heap.new_ptr(2)            # a routine that does nothing
        mem.w16(self._rts, RTS)

        mem.w32(lowmem.MemTop, RAM_SIZE)
        mem.w32(lowmem.ROMBase, ROM_BASE)
        mem.w16(ROM_BASE + 8, self.model.rom)
        mem.w32(lowmem.ScrnBase, base)
        # The top of applications' memory, as when nothing is installed
        # under the screen: Dark Castle asks for the alternate screen only then.
        mem.w32(lowmem.BufPtr, base)
        mem.w16(lowmem.ScreenRow, row_bytes)
        # The stack an application gets: The Fool's Errand moves ApplLimit
        # down by what it's short of 32K.
        mem.w32(lowmem.DefltStack, DEFAULT_STACK)
        mem.w32(lowmem.MinStack, MIN_STACK)
        mem.w32(lowmem.SysZone, SYS_HEAP_START)
        mem.w16(lowmem.MBarHeight, menu_bar_height)
        mem.w16(lowmem.ApFontID, 3)                     # the application font is Geneva
        self.open_wmgr_port()
        mem.w32(lowmem.GrayRgn, new_rgn(self.sys_heap, Rect(desk_top, 0, h, w)))
        self.files = FileSystem(self)
        self.fcbs = self.files.fcbs                     # open files, by reference number
        self._open_system_file(system_file, disk_system)
        self.screen = Screen(self, desk_top)            # the buffer at ScrnBase
        self.input = InputGlobals(mem)                  # the mouse and keys, in low memory

    def open_wmgr_port(self) -> None:
        """Make the Window Manager port, at startup and again for InitWindows
        when a program has thrown the last one away."""
        with self.lock:
            old = getattr(self, "_wmgr", None)
            if old is not None and self.ports.get(old.addr) is old:
                self.unregister(old)
            self._wmgr = GrafPort(self.mem, self.sys_heap.new_ptr(GrafPort.SIZE))
            self._wmgr.open(self.sys_heap, self.screen_bits)
            self.register(self._wmgr)
            self.mem.w32(lowmem.WMgrPort, self._wmgr.addr)
            self.mem.w8(lowmem.WWExist, 0)

    # =====================================================================
    # Partitions
    # =====================================================================
    def open_partition(self, name: str, size: int = DEFAULT_PARTITION,
                       stack: int = DEFAULT_STACK, *, above_a5: int = APP_PARAMS,
                       below_a5: int = QD_GLOBALS_SIZE, qd_globals: bool = True) -> Partition:
        """Set up an application's memory: heap, stack, A5 world, and the
        switched globals that describe them.  A 68k application's CODE 0
        says how big its A5 world is, and its own InitGraf call sets up
        its QuickDraw globals; for a Python one they're set up here."""
        with self.lock:
            start = self._room_for(size)
            end = start + size
            a5 = (end - above_a5) & ~3
            # Exactly below_a5 under A5, not rounded: start-up code that
            # fills the globals from DATA and ZERO resources copies from
            # CurStackBase until it reaches A5, and runs on past it if the
            # two are further apart than CODE 0 said.
            stack_base = a5 - below_a5
            appl_limit = (stack_base - stack) & ~3
            if appl_limit - start < MIN_HEAP:
                raise MemoryError(f"a {size // 1024}K partition is too small for {name}: "
                                  f"its globals and stack leave no room for a heap")
            self.mem.fill(appl_limit, end - appl_limit)
            world = World(self.mem)
            # The heap starts small and grows toward ApplLimit as it's
            # needed, or all at once when the application calls MaxApplZone.
            heap = Heap(self.mem, start, start + INITIAL_HEAP, appl_limit,
                        on_grow=lambda heap_end: world.w32(lowmem.HeapEnd, heap_end),
                        align=self.align)
            if qd_globals:
                the_port = a5 - 4
                init_qd_globals(self.mem, the_port, self.screen_bits)
                self.mem.w32(a5, the_port)              # what InitGraf leaves in 0(A5)
            for addr, value in ((lowmem.ApplZone, start), (lowmem.TheZone, start),
                                (lowmem.HeapEnd, heap.end), (lowmem.ApplLimit, appl_limit),
                                (lowmem.CurStackBase, stack_base), (lowmem.CurrentA5, a5)):
                world.w32(addr, value)
            app_name = to_mac(name)[:31]
            world.write(lowmem.CurApName, bytes([len(app_name)]) + app_name.ljust(31, b"\0"))
            # A Mac's VBL queue always holds a task of the System's own,
            # the disk driver's: Prince of Persia looks for the task before
            # its own, to move its own to the front.  This one never comes due.
            vbl = self.sys_heap.new_ptr(VBL_TASK_SIZE, clear=True)
            self.mem.w16(vbl + 4, 1)                # qType: vType
            self.mem.w32(vbl + 6, self._rts)        # vblAddr
            world.write(lowmem.VBLQueue, struct.pack(">HII", 0, vbl, vbl))
            part = Partition(name, start, end, heap, a5, stack_base, world, vbl)
            self._partitions.append(part)
            self._partitions.sort(key=lambda p: p.start)
            return part

    def close_partition(self, part: Partition) -> None:
        with self.lock:
            for addr in [a for a in self.ports if part.start <= a < part.end]:
                del self.ports[addr]
            # A Gestalt function left in the application's heap would be
            # called after it's gone; on a Mac that crashes, here it's forgotten.
            for sel in [s for s, a in self.gestalt_procs.items() if part.start <= a < part.end]:
                del self.gestalt_procs[sel]
            if part.vbl_task:
                self.sys_heap.dispose_ptr(part.vbl_task)
            for start in [s for s, (_, owner) in self._zones.items() if owner is part]:
                del self._zones[start]
            self._partitions.remove(part)

    def _taken(self) -> list[tuple[int, int]]:
        """What partitions can't be made in, in order: the partitions open,
        and what's reserved."""
        return sorted([(p.start, p.end) for p in self._partitions] + self._reserved)

    def _room_for(self, size: int) -> int:
        at = self.partitions_start
        for start, end in self._taken():
            if start - at >= size:
                break
            at = end
        if at + size > self.partitions_end:
            raise MemoryError(f"no room for a {size // 1024}K partition")
        return at

    def largest_free(self) -> int:
        """The biggest partition open_partition could make now, in whole K."""
        with self.lock:
            best, at = 0, self.partitions_start
            for start, end in self._taken():
                best, at = max(best, start - at), end
            return max(best, self.partitions_end - at) & ~0x3FF

    def init_zone(self, start: int, limit: int, more_masters: int, grow_zone: int,
                  owner: Partition | None) -> Heap:
        """InitZone: a zone from start to limit, with its 12-byte trailer,
        and its first cMoreMasters master pointers (Inside Macintosh II-29).
        It may be in a block of another zone or in memory no zone has, as
        Winter Games makes one between the alternate screen buffer and the
        screen; it replaces the zones it overlaps, and goes with the
        partition of the program that made it."""
        with self.lock:
            for s in [s for s, (h, _) in self._zones.items() if h.start < limit and start < h.end]:
                del self._zones[s]
            heap = Heap(self.mem, start, limit - MIN_BLOCK, align=self.align)
            self.mem.w32(start + 16, grow_zone)                 # gzProc
            self.mem.w16(start + MORE_MAST, more_masters)
            heap.more_masters()
            self._zones[start] = heap, owner
            return heap

    def heap_of(self, addr: int) -> Heap:
        a = addr & ADDRESS_MASK
        for heap, _ in self._zones.values():
            if heap.contains(a):
                return heap
        if self.sys_heap.contains(a):
            return self.sys_heap
        for p in self._partitions:
            if p.heap.contains(a):
                return p.heap
        raise ValueError(f"${a:06X} is not in a heap")

    # =====================================================================
    # Canonical views: one Python object per port, so a WindowPtr that
    # comes back from guest code finds the same object.
    # =====================================================================
    def register(self, port: GrafPort) -> None:
        # By the address alone: a program may pass its storage with flags
        # in the high byte, as SuperPaint passes a locked handle's master
        # pointer to NewWindow, and use it without them after.
        with self.lock:
            self.ports[port.addr & ADDRESS_MASK] = port

    def unregister(self, port: GrafPort) -> None:
        with self.lock:
            self.ports.pop(port.addr & ADDRESS_MASK, None)

    def port_at(self, addr: int) -> GrafPort | None:
        return self.ports.get(addr & ADDRESS_MASK)

    # =====================================================================
    # Files
    # =====================================================================
    def _open_system_file(self, f: MacFile | None, disk_system: MacFile | None) -> None:
        """The System file's map heads every application's resource chain
        at the bottom.  Without a real one, one with only what sysres
        gives it and the definition procedures, and the fonts of
        disk_system's that it hasn't."""
        f = f or MacFile(SYSTEM_FILE, rsrc=build_fork([]), key="<system>")
        name = to_mac(SYSTEM_FILE)
        self.mem.write(lowmem.SysResName, bytes([len(name)]) + name)
        world = World(self.mem)
        self.file_manager = FileManager(self.files, world, self.files.system_folder)
        self.resources = ResourceManager(self, world, self.sys_heap, files=self.file_manager)
        self.resources.init()
        if self.resources.open_node(self.files.system_node(f), source=f) < 0:
            raise ValueError(f"{f.name!r} has no usable resource fork")
        self.mem.w32(lowmem.SysMapHndl, self.resources.top)
        self.mem.w16(lowmem.SysMap, self.resources.cur_map)
        for kind, def_id in STANDARD_DEFPROCS:
            self.defproc(kind, def_id)
        m = self.sys_map
        self._add_system_resources([(kind, rid, data) for kind, rid, data in sysres.standard()
                                    if m.find(restype(kind), rid) is None])
        if all(next(m.entries(restype(kind)), None) is None for kind in ("FOND", "FONT", "NFNT")):
            self._add_system_resources(sysres.fonts())      # a real System file brings its own
            if disk_system is not None:
                self._add_system_resources(self._disk_fonts(disk_system))
        # The printer is macrt's whatever the System file says: its own
        # printer file's code would drive a serial port.
        name = to_mac(printing.PRINTER_NAME)
        self._add_system_resources([("STR ", PRINTER_STR, bytes([len(name)]) + name)],
                                   replace=True)
        if m.find(restype("DRVR"), 2) is None:
            self._add_system_resources([("DRVR", 2, printing.driver(), ".Print")])
        self.files.in_system_folder(printing.printer_file())

    def _disk_fonts(self, f: MacFile) -> list[tuple]:
        """The fonts in f, the System file on the disk the applications
        come from, up to FONT_ROOM's worth: of the families macrt's own
        System hasn't got, their FONTs, their FONDs and the NFNTs these
        list; and then the strikes of its monospaced ones, Monaco and
        Courier, which are still drawn with host fonts but measured with
        these (AppFonts.measure), as with a real System file.  Programs
        drew in the fonts of the System they started up with, and some
        brought their own there: Mac Attack's tanks, missiles and
        landscape are characters in four fonts of its disk's System, and
        early disks have Venice, London and Athens.  And some count on a
        monospaced font's width: Hacker types each letter of Monaco 9 and
        its cursor after it, then backs up 6 pixels, Monaco's width, to
        type the next over the cursor.  (A host font's letters set at the
        widths of a proportional one's, Geneva's, are spaced unevenly.)"""
        try:
            fork = open_fork(f.rsrc)
        except InvalidForkError:
            return []
        entries = [e for e in fork.entries if not e.attrs & resCompressed]
        fonds = [e for e in entries if e.type == b"FOND" and e.id not in sysres.FONTS]
        listed = {rid for e in fonds for rid in fond_ids(fork.raw(e))}
        picked = [e for e in entries
                  if e.type == b"FONT" and (e.id >> 7 not in sysres.FONTS or e.id in listed)
                  or e.type == b"NFNT" and e.id in listed] + fonds
        picked += [e for e in entries if e.type == b"FONT" and e.id & 0x7F
                   and FAMILIES.get(e.id >> 7) == MONO and e.id >> 7 in sysres.FONTS
                   and e not in picked]
        fonts, room = [], FONT_ROOM
        for e in picked:
            data = fork.raw(e)
            if len(data) <= room and self.sys_map.find(e.type, e.id) is None:
                room -= len(data)
                name = [e.name.decode("mac_roman")] if e.name else []
                fonts.append((e.type.decode("mac_roman"), e.id, data, *name))
        return fonts

    @property
    def sys_map(self) -> ResourceMap:
        return ResourceMap(self.mem, self.mem.r32(lowmem.SysMapHndl))

    # =====================================================================
    # Definition procedures.  The standard ones are Python, but records
    # hold a handle to one.  They're resources in the System file; without
    # a real System file a standard one's code is MOVE.W #selector,D0 and
    # an auto-pop _DialogDispatch, so 68k code can call it too (Excel's
    # own CDEF passes messages on to CDEF 0), and any other's is an RTS.
    # =====================================================================
    def defproc(self, kind: str, def_id: int) -> int:
        with self.lock:
            h = self.resources.get(kind, def_id)
            if h:
                return h
            sel = STANDARD_DEFPROCS.get((kind, def_id))
            code = (struct.pack(">HHH", 0x303C, sel, 0xAE68) if sel is not None
                    else RTS.to_bytes(2, "big"))
            return self._add_system_resources([(kind, def_id, code)])[0]

    def _add_system_resources(self, resources: list[tuple], replace: bool = False) -> list[int]:
        """Add resources, (type, ID, data) or (type, ID, data, name), to the
        System file's map, in memory only and locked in the system heap,
        in place of any with the same type and ID if replace.  Their
        handles."""
        with self.lock:
            items = []
            for kind, rid, data, *name in resources:
                h = self.sys_heap.new_handle(len(data))
                self.mem.write(self.sys_heap.deref(h), data)
                self.sys_heap.set_state(h, LOCKED | RESOURCE)
                items.append(Item(restype(kind), rid, to_mac(name[0]) if name else None,
                                  resSysHeap | resLocked, UNWRITTEN, h))
            if items:
                m = self.sys_map
                new = {(it.type, it.id) for it in items} if replace else set()
                m.rewrite(self.sys_heap,
                          [it for it in m.items() if (it.type, it.id) not in new] + items)
            return [item.handle for item in items]

    def defproc_id(self, h: int) -> tuple[str, int]:
        """(resource type, ID) for a System file definition procedure,
        ("", 0) for anything else."""
        with self.lock:
            e = self.sys_map.find_handle(h)
            return (e.type.decode("mac_roman"), e.id) if e else ("", 0)
