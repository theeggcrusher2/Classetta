"""
A running guest application.

A guest is either a real Mac application, whose 68k code runs on the
emulated CPU (see macrt.m68k.runtime), or a Python callable that takes a
Toolbox and runs a classic event loop the same way.  Traps from 68k code
end up in the same Toolbox.  Either way it owns a partition of guest
memory, and its windows and menus are records in it.

An application file, if there is one, is opened as the application's
resource file at launch, as the Segment Loader does.  Its SIZE resource
says how big a partition to give it, and its CODE 0 how big an A5 world.
"""

from __future__ import annotations

import itertools
import struct
import threading
import traceback
from typing import TYPE_CHECKING, Callable

from .m68k import segload
from .macfile import Folder, MacFile
from .memory import ADDRESS_MASK
from .resfork import InvalidForkError, open_fork
from .toolbox import lowmem
from .toolbox.events import EventQueue, ProcessKilled, tick_count
from .toolbox.files import FileManager
from .toolbox.menus import MenuList
from .toolbox.quickdraw import QD_GLOBALS_SIZE
from .toolbox.resources import ResourceManager
from .toolbox.system import APP_PARAMS, DEFAULT_PARTITION, DEFAULT_STACK, MIN_HEAP, System
from .toolbox.windows import WindowList

if TYPE_CHECKING:
    from .m68k.runtime import Runner
    from .toolbox.api import Toolbox
    from .toolbox.bitfont import Strike
    from .toolbox.windows import WindowRecord
    from .ui.menubar import MenuBarWidget

GuestMain = Callable[["Toolbox"], None]
_serial_numbers = itertools.count(0x2000)       # the low long of a ProcessSerialNumber


def _size_data(app: MacFile) -> bytes:
    """SIZE 0, the one the user set in the Finder's Get Info, or else
    SIZE -1, the developer's."""
    try:
        fm = open_fork(app.rsrc)
        entry = fm.find(b"SIZE", 0) or fm.find(b"SIZE", -1)
        return fm.raw(entry) if entry else b""
    except InvalidForkError:
        return b""


def size_resource(app: MacFile) -> tuple[int, int] | None:
    """(preferred, minimum) partition size from the SIZE resource."""
    data = _size_data(app)
    if len(data) < 10:
        return None
    return struct.unpack_from(">II", data, 2)


def size_flags(app: MacFile) -> int:
    """The SIZE resource's flags: what the application says it can do."""
    data = _size_data(app)
    return struct.unpack_from(">H", data)[0] if len(data) >= 2 else 0


class Process:
    """main is the Python guest to run; None runs app's 68k code."""

    def __init__(self, system: System, name: str, main: GuestMain | None, events: EventQueue,
                 partition_size: int | None = None, app: MacFile | None = None,
                 folder: Folder | None = None, page_option: int = 0):
        self.system = system
        self.name = name
        self.events = events
        self.app = app
        self.serial_number = next(_serial_numbers)
        self.launch_ticks = tick_count()
        layout = None
        if main is None:
            layout = segload.code0(app) if app is not None else None
            if layout is None:
                raise ValueError(f"{name} has no 68k code (no CODE resources) to run")
        self.partition = self._open_partition(partition_size, layout)
        self.heap = self.partition.heap
        self.world = self.partition.world
        self.world.w16(lowmem.CurPageOption, page_option)  # Launch's: < 0 for the alternate screen
        self.windows = WindowList(system, self.world)     # front to back
        self.frames_due: set[WindowRecord] = set()        # for their own WDEFs to shape and draw
        self.menus = MenuList(system, self.world)         # the menu bar, InsertMenu order
        self.menus.init(self.heap)
        # The application's own Chicago 12, if it brings one: a Mac draws
        # the menus in it (Toolbox.InsertMenu finds it).
        self.system_strike: Strike | None = None
        folder = folder or (app.folder if app else None)
        start = system.files.dir_for(folder)
        if app is not None and (start is None or folder is app.folder):
            start = system.files.node_for(app).parent
        self.files = FileManager(system.files, self.world, start)
        self.resources = ResourceManager(system, self.world, self.heap, folder, self.files)
        self.resources.init()
        if app is not None:
            refnum = self.resources.open(app)
            if refnum < 0:
                self.files.close_all()
                system.close_partition(self.partition)
                raise ValueError(f"{app.name!r} has no usable resource fork "
                                 f"(error {self.resources.error})")
            self.world.w16(lowmem.CurApRefNum, refnum)
        self.hidden = False
        self.faulted = False                        # its records broke the GUI; it's being stopped
        self.notifications: set[int] = set()        # NMRecs installed (NMInstall)
        self.last_bar: MenuBarWidget | None = None  # where HiliteMenu applies
        self.thread: threading.Thread | None = None
        self.runner: Runner | None = None
        if layout is not None:
            from .m68k import runtime
            self.runner = runtime.Runner(self, layout)
            main = self.runner.main
        self.main = main

    def _open_partition(self, size: int | None, layout: segload.Layout | None):
        world = {}
        if layout is not None:
            world = dict(above_a5=max(layout.above_a5, APP_PARAMS), below_a5=layout.below_a5,
                         qd_globals=False)
        if size is not None:
            return self.system.open_partition(self.name, size, **world)
        sizes = size_resource(self.app) if self.app else None
        if sizes is None:
            return self.system.open_partition(self.name, DEFAULT_PARTITION, **world)
        # As the Finder does: the preferred size if there's room, else as
        # much as there is, as long as that's at least the minimum.  The
        # stack comes on top: the sizes were worked out for a Mac that gave
        # applications 8K or 24K of stack, and this one gives DEFAULT_STACK.
        # And room for the A5 world and the least heap there is: Beyond Dark
        # Castle's launcher asks for 20K.
        # (Rounding A5 and ApplLimit down to longs can take 6 bytes.)
        least = (MIN_HEAP + world.get("above_a5", APP_PARAMS)
                 + world.get("below_a5", QD_GLOBALS_SIZE) + 8)
        preferred, minimum = (max(n, least) + DEFAULT_STACK for n in sizes)
        free = self.system.largest_free()
        if free < minimum:
            raise MemoryError(f"{self.name} needs {sizes[1] // 1024}K of memory and "
                              f"{DEFAULT_STACK // 1024}K for its stack, and only "
                              f"{free // 1024}K is free")
        return self.system.open_partition(self.name, min(max(preferred, minimum), free),
                                          **world)

    def start(self, toolbox: Toolbox, on_exit: Callable[[Process], None]) -> None:
        def run():
            try:
                self.main(toolbox)
            except ProcessKilled:
                pass
            except Exception:  # noqa: BLE001 - a crashing guest shouldn't take the runtime down
                traceback.print_exc()
            finally:
                on_exit(self)

        self.thread = threading.Thread(target=run, name=f"guest:{self.name}", daemon=True)
        self.thread.start()

    def kill(self) -> None:
        """Stop the application, wherever it is: waiting for an event,
        tracking the mouse, or in the middle of its own code."""
        self.events.kill()
        if self.runner is not None:
            self.runner.interrupt()

    def close(self) -> None:
        """Close the application's resource files and the files and working
        directories it left open, and give the partition back once the
        application is gone."""
        self.resources.close_all()
        self.files.close_all()
        self.system.screen.detach(self)
        self.system.close_partition(self.partition)

    def front_window(self) -> WindowRecord | None:
        """The first visible window but GhostWindow's, which Multiplan
        points at the window its formula bar is edited in: always in front
        of the sheet, never the active one."""
        ghost = self.world.r32(lowmem.GhostWindow) & ADDRESS_MASK
        for w in self.windows:
            if w.visible and w.addr & ADDRESS_MASK != ghost:
                return w
        return None

    def __repr__(self) -> str:
        return f"<Process {self.name!r}>"
