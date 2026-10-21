"""
The screen buffer: the 1-bit bitmap at ScrnBase, as a program sees it.

macrt shows each window's pixels from its Backing, and draws the menu
bar, the window frames and the desktop itself, so nothing on the host
needs the screen buffer.  Programs from the Mac's first years use it all
the same.  MacPaint copies its document into it with a loop of its own,
and games draw their sprites there, often by XORing them over what's
already there.  So the buffer mirrors what the windows show:

- whatever QuickDraw draws in a window is written through to the buffer,
  where the window can be seen;
- whatever a program writes in the buffer itself is drawn in the windows
  it lands on, the next time macrt looks: before QuickDraw next uses a
  window's pixels, and once a tick (at a trap, or from the clock while
  the program calls none);
- when windows move, come and go, or change order, each one's pixels are
  written where it is now, and the desk they uncover is painted gray, as
  the Window Manager paints it.

Games from 1984 often open one small window, or none, and play on the
desk itself.  So what a program writes on the desk, where no window is,
is shown there too, when the Display has a desk to show it on (host
"screen").  Some draw over the menu bar as well, as a Mac's bar is only
pixels in the buffer: that shows over the bar until the program draws
the bar again (DrawMenuBar).  So does what a window draws over the bar:
a window's visRgn leaves the bar out, as the desk (GrayRgn) does, but a
program can put it back, to cover the bar with its window.  What lies
under the window frames is kept in the buffer, and can be read back, but
isn't shown.

A 512x342 screen has an alternate buffer too, $8000 below the main one,
and the VIA chooses which of them is shown.  Dark Castle draws in one
while the other is shown, and flips between them.  While the alternate
is shown, the desk and the menu bar show it; the windows still show what
QuickDraw draws in them, which is in the main buffer.  A Mac Plus has
the two at the top of its 4 MB of addresses too, as its RAM repeats
through them, and the memory mirrors them there (see RAM.mirror).
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING, Callable, Iterable

from PyQt6.QtCore import QPoint
from PyQt6.QtGui import QImage

from . import lowmem
from .qdraw import MemorySurface, Region, WindowSurface, ink, patCopy, qrect, read_rgn
from .types import Rect

if TYPE_CHECKING:
    from ..process import Process
    from .system import System
    from .windows import WindowRecord

TICK = 1 / 60
DESK = b"\xAA\x55" * 4          # the desk's gray: DeskPattern, as the System sets it


class Screen:
    def __init__(self, system: System, desk_top: int = 0):
        self.system = system
        self.mem = system.mem
        bits = system.screen_bits
        self.base, self.row_bytes, self.bounds = bits.base, bits.row_bytes, bits.bounds
        self.size = self.row_bytes * self.bounds.height
        self.bits = MemorySurface(self.mem, self.base, self.row_bytes, self.bounds)
        self.seen = bytearray(self.mem.read(self.base, self.size))  # the buffer, last we looked
        self.repaints: dict[Process, Callable[[WindowRecord], None]] = {}
        # The processes front to back; the Display says, once there is one.
        self.order: Callable[[], Iterable[Process]] = lambda: list(self.repaints)
        self._layout: list | None = None
        self._owners: list[tuple[WindowRecord, Region]] = []    # front to back
        self._owned: dict[int, Region] = {}                     # window address -> its part
        self._stale = True
        self._last = 0.0
        self.desk_top = desk_top
        self.desk = Region()            # the screen no window covers, when last laid out
        self.desk_drawn = False         # whether a program has drawn there
        # The part of the menu bar a program has drawn over since the bar
        # was last drawn (global).
        self.over_bar = Region()
        # The Display's, to repaint part of the desk (global; called with the lock held).
        self.on_desk: Callable[[Rect], None] | None = None
        # The Display's, when windows have been laid out again: a program
        # can show or hide a window by setting its visible flag itself, with
        # no ShowWindow or HideWindow to say so (called with the lock held).
        self.on_layout: Callable[[], None] | None = None
        self.closed = False             # the Display has gone: nothing more to show
        alt = system.alternate_screen
        self.alternate = (MemorySurface(self.mem, alt, self.row_bytes, self.bounds)
                          if alt is not None else None)
        self.page2 = False              # the VIA says to show the alternate buffer
        self.shown = self.bits          # the buffer the desk and bar show
        self._alt_seen = b""            # the alternate buffer, last we looked

    def attach(self, process: Process, repaint: Callable[[WindowRecord], None]) -> None:
        self.repaints[process] = repaint

    def detach(self, process: Process) -> None:
        with self.system.lock:
            self.repaints.pop(process, None)
            self._stale = True

    def close(self) -> None:
        """The Display is going: stop looking from the clock, whose thread
        would otherwise go on painting while Qt is torn down.  (Not with
        the lock: a process may hold it, waiting on the GUI thread.)"""
        self.closed = True
        self.on_desk = self.on_layout = None

    def bar_drawn(self, img: QImage | None = None) -> None:
        """The menu bar has been drawn again, over what programs drew
        there; in the buffer too, as img shows it (DrawMenuBar's), so that
        a program drawing over the bar changes its pixels, white or black:
        MacLanding fills it white, line and all, for its radar."""
        with self.system.lock:
            self.over_bar = Region()
            rows = min(self.desk_top, img.height()) if img is not None else 0
            if rows <= 0:
                return
            b = self.bounds
            with self.bits.painter(Rect(b.top, b.left, b.top + rows, b.right)) as p:
                if p is not None:
                    p.drawImage(QPoint(b.left, b.top), img)
            end = rows * self.row_bytes
            self.seen[:end] = self.mem.read(self.base, end)

    def _bar(self) -> Region:
        """Where the menu bar is (global)."""
        b = self.bounds
        return Region.rect(Rect(b.top, b.left, b.top + self.desk_top, b.right))

    def bar_hidden(self) -> Region:
        """What of the menu bar isn't desk, in GrayRgn (global): what a
        window's visRgn leaves out, as CalcVis leaves it out.  A program
        that means its window to cover the bar sets the visRgn itself:
        Indiana Jones and the Last Crusade makes it its whole portRect."""
        bar = self._bar()
        gray = self.mem.r32(lowmem.GrayRgn)
        return bar.diff(read_rgn(self.mem, gray)) if gray and not bar.empty else bar

    def show_page(self, alternate: bool) -> None:
        """The VIA's: show the alternate buffer, or the main one, from the
        next look (from the CPU's thread, with or without the lock)."""
        self.page2 = alternate and self.alternate is not None

    def bar_shown(self) -> tuple[Region, MemorySurface]:
        """What of the menu bar shows a buffer rather than the bar
        (global), and which buffer: all of it, while the alternate buffer
        is shown, as there's no bar in it."""
        if self.shown is self.alternate:
            b = self.bounds
            return Region.rect(Rect(b.top, b.left, b.top + self.desk_top, b.right)), self.shown
        return self.over_bar, self.bits

    def moved(self) -> None:
        """Windows have moved, come or gone: lay them out now, as the
        Window Manager does, so the desk they leave is gray before the
        program goes on to draw there."""
        self._stale = True
        self.look()

    # -- looking -------------------------------------------------------------
    def tick(self) -> None:
        """Look, if a tick has gone by since the last look."""
        if time.monotonic() - self._last >= TICK:
            self.look()

    def glance(self) -> None:
        """Look, from the clock, at a tick: for a program that draws in the
        buffer and calls no trap for a while, as games do while they keep
        time by the VIA.  If the System is busy, a trap is under way, and
        it looks itself."""
        lock = self.system.lock
        if lock.acquire(blocking=False):
            try:
                if not self.closed:
                    self.tick()
            finally:
                lock.release()

    def look(self) -> None:
        """Show what's been written in the buffer since the last look, and
        lay the windows out again if they've moved.  What's been written
        goes to the windows where they are now: a program draws where its
        windows are when it draws, and if one has moved since, it's sent
        an update event to draw it again."""
        with self.system.lock:
            now = time.monotonic()
            moved, uncovered = False, Region()
            if self._stale or now - self._last >= TICK:
                self._last = now
                self._stale = False
                desk = self.desk
                moved = self._relayout()
                uncovered = self.desk.diff(desk)
            self._flip()
            self._pull(uncovered)
            if moved:
                self._paint_desk(uncovered)
                for w, r in self._owners:
                    self._put(w, r)
                self.seen[:] = self.mem.read(self.base, self.size)
                if self.on_layout is not None:
                    self.on_layout()

    def _windows(self) -> Iterable[WindowRecord]:
        for proc in list(self.order()):
            if getattr(proc, "hidden", False):
                continue
            for w in proc.windows:
                if w.visible and w.backing is not None:
                    yield w

    def _relayout(self) -> bool:
        """Which part of the screen each window has; False if that's
        the same as before."""
        windows = list(self._windows())
        layout = [(w.addr, *self._shape(w)) for w in windows]
        if layout == self._layout:
            return False
        self._layout = layout
        screen = Region.rect(self.bounds)
        covered, self._owners = Region(), []
        for w, (_, content, struc) in zip(windows, layout):
            mine = content.sect(screen).diff(covered)
            if not mine.empty:
                self._owners.append((w, mine))
            covered = covered.union(content).union(struc)
        self._owned = {w.addr: r for w, r in self._owners}
        b = self.bounds
        self.desk = Region.rect(Rect(self.desk_top, b.left, b.bottom, b.right)).diff(covered)
        return True

    def _shape(self, w: WindowRecord) -> tuple[Region, Region]:
        """What of the screen w's content area and whole window cover: as
        its own WDEF shaped them, or the rectangles of a frame the host
        draws."""
        if w.wdef_shaped:
            return (read_rgn(self.mem, w.cont_rgn).sect(Region.rect(w.content)),
                    w.wdef_struc())
        return Region.rect(w.content), Region.rect(w.struc_rect)

    def _paint_desk(self, r: Region) -> None:
        if r.empty:
            return
        box = r.bbox
        with self.bits.painter(box) as p:
            if p is not None:
                p.setClipRegion(r.to_qregion())
                p.setBrushOrigin(self.bounds.left, self.bounds.top)
                p.fillRect(qrect(box), ink(DESK, patCopy)[0])

    def _flip(self) -> None:
        """Show the buffer the VIA says to: all of it, when that's changed,
        or what's been written in the alternate since the last look."""
        alt = self.alternate
        shown = alt if self.page2 else self.bits
        changed = None
        if shown is not self.shown:
            self.shown = shown
            changed = self.bounds
            if shown is alt:
                self.desk_drawn = True
                self._alt_seen = self.mem.read(alt.base, self.size)
        elif shown is alt:
            now = self.mem.read(alt.base, self.size)
            if now != self._alt_seen:
                changed = self._changes(now, self._alt_seen).bbox
                self._alt_seen = now
        if changed is not None and self.on_desk is not None:
            self.on_desk(changed)

    # -- the program's pixels into the windows -------------------------------
    def _pull(self, uncovered: Region) -> None:
        """uncovered is desk that was under a window until now: what was
        written there is the window's, and it's about to be painted gray."""
        now = self.mem.read(self.base, self.size)
        if now == self.seen:
            return
        changed = self._changes(now, self.seen)
        self.seen[:] = now
        for w, mine in self._owners:
            r = changed.sect(mine)
            if not r.empty:
                self._show(w, r)
        on_desk = changed.sect(self.desk).diff(uncovered)
        shown = self.on_desk if self.shown is self.bits else None
        if not on_desk.empty:
            self.desk_drawn = True
            if shown is not None:
                shown(on_desk.bbox)
        self._over_bar(changed.sect(self._bar()), shown)

    def _over_bar(self, r: Region, shown: Callable[[Rect], None] | None) -> None:
        """r (global) has been drawn over the menu bar: show it there, as
        shown says, until the bar is drawn again."""
        if not r.empty:
            self.over_bar = self.over_bar.union(r)
            if shown is not None:
                shown(r.bbox)

    def _changes(self, now: bytes, seen: bytes) -> Region:
        """Where now differs from what was seen, to the byte."""
        rb = self.row_bytes
        bands: list = []
        for y in range(self.bounds.height):
            at = y * rb
            a, b = now[at:at + rb], seen[at:at + rb]
            if a == b:
                continue
            x = int.from_bytes(a, "big") ^ int.from_bytes(b, "big")
            span = ((rb - 1 - (x.bit_length() - 1) // 8) * 8,
                    (rb - ((x & -x).bit_length() - 1) // 8) * 8)
            if bands and bands[-1][1] == y and bands[-1][2] == span:
                bands[-1] = (bands[-1][0], y + 1, span)
            else:
                bands.append((y, y + 1, span))
        return Region(bands)

    def _show(self, w: WindowRecord, r: Region) -> None:
        """Draw the buffer's pixels in r (global) into w's Backing."""
        box = r.bbox
        img = self.bits.image(box)
        b = w.port_bits_bounds
        with WindowSurface(w).painter() as p:
            p.setClipRegion(r.offset(b.left, b.top).to_qregion())
            p.drawImage(QPoint(box.left + b.left, box.top + b.top), img)
        repaint = self.repaints.get(w.process)
        if repaint is not None:
            repaint(w)

    # -- the windows' pixels into the buffer ---------------------------------
    def drawn(self, w: WindowRecord, area: Rect | None, clip: Region | None) -> None:
        """QuickDraw drew in area of w (local; None for anywhere), clipped
        to clip: write it to the buffer, where w can be seen."""
        with self.system.lock:
            mine = self._owned.get(w.addr)
            if mine is None:
                return
            where = Region.rect(area if area is not None else w.port_rect)
            if clip is not None:
                where = where.sect(clip)
            b = w.port_bits_bounds
            where = where.offset(-b.left, -b.top).sect(mine)
            if where.empty:
                return
            self._put(w, where)
            box = where.bbox
            at, end = box.top * self.row_bytes, box.bottom * self.row_bytes
            self.seen[at:end] = self.mem.read(self.base + at, end - at)
            if box.top < self.desk_top:
                self._over_bar(where.sect(self._bar()),
                               self.on_desk if self.shown is self.bits else None)

    def _put(self, w: WindowRecord, r: Region) -> None:
        """Write w's pixels in r (global) to the buffer."""
        box, sb = r.bbox, self.bounds
        left = sb.left + ((box.left - sb.left) & ~7)      # from a byte of the buffer
        b = w.port_bits_bounds
        whole = Rect(box.top, left, box.bottom, box.right).offset(b.left, b.top)
        self.bits.put_mono(WindowSurface(w).mono(whole, box.offset(b.left, b.top)), left, r)
