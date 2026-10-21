"""
Event Manager: EventRecord and the per-process event queue.

The GUI thread posts input; the guest thread blocks in WaitNextEvent.
Event priority follows Inside Macintosh: activate events, then queued
input, then update events, then a null event once the sleep time runs out.

InputGlobals keeps the Event Manager's low-memory record of the mouse
and keyboard, for programs that read it themselves.
"""

from __future__ import annotations

import struct
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Callable

from . import lowmem
from .constants import (activateEvt, activeFlag, alphaLock, cmdKey, controlKey, everyEvent,
                        event_mask, kHighLevelEvent, nullEvent, optionKey, shiftKey, updateEvt)
from .types import Point

_T0 = time.monotonic()


def tick_count() -> int:
    """Ticks (1/60 s) since startup, like the TickCount trap."""
    return int((time.monotonic() - _T0) * 60)


def to_next_tick() -> float:
    """How long, in ticks, until the next tick."""
    now = (time.monotonic() - _T0) * 60
    return int(now) + 1 - now


class ProcessKilled(Exception):
    """Raised inside a guest thread to unwind it when its process is killed."""


@dataclass
class EventRecord:
    what: int = nullEvent
    message: Any = 0          # WindowPtr for activate/update events
    when: int = 0
    where: Point = field(default_factory=lambda: Point(0, 0))
    modifiers: int = 0


# Key codes of the modifier keys, for the KeyMap.
MODIFIER_KEYS = ((cmdKey, 0x37), (shiftKey, 0x38), (alphaLock, 0x39),
                 (optionKey, 0x3A), (controlKey, 0x3B))
# The arrow keys' codes on a Mac Plus keyboard and on an ADB one, each to
# the other.  An arrow key down is in the KeyMap by both: games look for
# the one keyboard's or the other's, The Duel for the Plus's and Prince of
# Persia for ADB's.
ARROW_TWINS = {0x46: 0x7B, 0x42: 0x7C, 0x4D: 0x7E, 0x48: 0x7D}
ARROW_TWINS.update({adb: plus for plus, adb in ARROW_TWINS.items()})


def _short(n: int) -> int:
    return (n + 0x8000) % 0x10000 - 0x8000


class InputGlobals:
    """The mouse and keyboard, in low memory, as the Event Manager keeps
    them there.  Games from the Mac's first years read them rather than
    ask for events: MBState for the button; Mouse, and its copies MTemp
    and RawMouse, for where the mouse is; the KeyMap for which keys are
    down; and KeyLast and KeyTime for the last key to go down, and when.
    KeyLast goes back to 0 when that key comes up, and games wait for it
    to, so that holding a key down counts once.

    MTemp is where the mouse's interrupt handler counts its movement up,
    so it goes on by as far as the mouse moves even after a program has
    written to it: Flight Simulator clears it each frame and takes what
    it's grown by for how far to move its own cursor.

    A program moves the mouse itself by writing RawMouse (and MTemp, and
    Mouse) and setting CrsrNew for the cursor's task to see to at the next
    tick; Pirates! waits for the task to clear it.  The cursor is the
    host's pointer, which can't be moved as a Mac's can, so while the
    program has hidden the cursor the mouse goes on from where it put it,
    `moved` from the pointer: Crystal Quest puts it back in the middle of
    the screen each frame and steers by how far it's got from there.
    With the cursor to be seen, the mouse is back under the pointer when
    that moves.

    The Display tells it about input as it comes, on the GUI thread."""

    def __init__(self, mem):
        self._b = mem._b        # straight into the bytes: no code lives in these pages
        self._lock = threading.Lock()
        self._keys: set[int] = set()
        self._mods = 0
        self._where = Point(0, 0)
        self._raw = Point(*struct.unpack_from(">hh", self._b, lowmem.RawMouse))
        self.moved = Point(0, 0)
        self._hidden = 0        # the cursor level, below 0 while it's hidden
        self.mouse(Point(0, 0), False)
        self._keymap()

    def mouse(self, where: Point, down: bool) -> None:
        with self._lock:
            v, h = struct.unpack_from(">hh", self._b, lowmem.MTemp)
            struct.pack_into(">hh", self._b, lowmem.MTemp, _short(v + where.v - self._where.v),
                             _short(h + where.h - self._where.h))
            self._took()
            self._where = where
            self._put_mouse()
            self._b[lowmem.MBState] = 0 if down else 0x80

    def tick(self) -> None:
        """The cursor's task, at each tick, from the clock."""
        if self._b[lowmem.CrsrNew]:
            with self._lock:
                if self._b[lowmem.CrsrCouple] and self._took():
                    struct.pack_into(">hh", self._b, lowmem.Mouse, *self._raw)
                self._b[lowmem.CrsrNew] = 0

    def _took(self) -> bool:
        """Whether the program has moved the mouse, by RawMouse, since it
        was last put there."""
        raw = Point(*struct.unpack_from(">hh", self._b, lowmem.RawMouse))
        if raw == self._raw:
            return False
        self._raw = raw
        if self._hidden < 0:
            self.moved = Point(raw.v - self._where.v, raw.h - self._where.h)
        return True

    def _put_mouse(self) -> None:
        self._raw = Point(_short(self._where.v + self.moved.v),
                          _short(self._where.h + self.moved.h))
        for addr in (lowmem.RawMouse, lowmem.Mouse):
            struct.pack_into(">hh", self._b, addr, *self._raw)

    def cursor(self, level: int | None) -> None:
        """The cursor has been hidden (-1) or shown (+1), or InitCursor has
        shown it (None)."""
        with self._lock:
            was, self._hidden = self._hidden, 0 if level is None else min(self._hidden + level, 0)
            if self._hidden == 0 and (was or level is None) and self.moved != (0, 0):
                self.moved = Point(0, 0)
                self._put_mouse()

    def modifiers(self, mods: int) -> None:
        if mods != self._mods:
            self._mods = mods
            self._keymap()

    def key(self, message: int, down: bool) -> None:
        """A key went down or came up: message is its (key code << 8) |
        character, as in a keyDown event."""
        code = message >> 8 & 0x7F
        codes = {code}
        if 0x1C <= message & 0xFF <= 0x1F and code in ARROW_TWINS:
            codes.add(ARROW_TWINS[code])        # (not a Plus keypad's +, *, / or =)
        if down:
            self._keys |= codes
            struct.pack_into(">HI", self._b, lowmem.KeyLast, message & 0xFFFF,
                             tick_count() & 0xFFFFFFFF)
        else:
            self._keys -= codes
            if struct.unpack_from(">H", self._b, lowmem.KeyLast)[0] >> 8 == code:
                struct.pack_into(">H", self._b, lowmem.KeyLast, 0)
        self._keymap()

    def release(self) -> None:
        """Every key is up: the host has taken the keyboard away."""
        self._keys.clear()
        struct.pack_into(">H", self._b, lowmem.KeyLast, 0)
        self._keymap()

    def _keymap(self) -> None:
        keys = bytearray(16)
        for code in self._keys:
            keys[code >> 3] |= 1 << (code & 7)
        for bit, code in MODIFIER_KEYS:
            if self._mods & bit:
                keys[code >> 3] |= 1 << (code & 7)
        self._b[lowmem.KeyMap:lowmem.KeyMap + 16] = keys


class EventQueue:
    def __init__(self, input_state: Callable[[], tuple[Point, int]]):
        # input_state() -> (mouse position, modifier bits), for null events
        self._input_state = input_state
        self._cond = threading.Condition()
        self._input: deque[EventRecord] = deque()
        self._activates: deque[EventRecord] = deque()
        self._killed = False
        # The system event mask, for a 68k program's queue: the event types
        # it leaves out aren't posted, as keyUp isn't unless a program asks
        self.enabled: Callable[[], int] | None = None

    def input_state(self) -> tuple[Point, int]:
        """Where the mouse is and which modifier keys are down, now."""
        return self._input_state()

    def post(self, ev: EventRecord) -> bool:
        """Queue ev; False if the system event mask leaves its type out."""
        if self.enabled is not None and not self.enabled() & event_mask(ev.what):
            return False
        with self._cond:
            if not ev.when:
                ev.when = tick_count()
            self._input.append(ev)
            self._cond.notify_all()
        return True

    def post_activate(self, window, active: bool) -> None:
        where, mods = self._input_state()
        mods = (mods | activeFlag) if active else (mods & ~activeFlag)
        ev = EventRecord(activateEvt, window, tick_count(), where, mods)
        with self._cond:
            # A deactivate/activate pair for a window that has not been read
            # yet cancels out, like the Window Manager's CurDeactive/CurActivate;
            # a second one the same way is already pending.
            for queued in list(self._activates):
                if queued.message is window:
                    if bool(queued.modifiers & activeFlag) != active:
                        self._activates.remove(queued)
                    break
            else:
                self._activates.append(ev)
            self._cond.notify_all()

    def wake(self) -> None:
        """Re-check for update events (called when a window needs redrawing)."""
        with self._cond:
            self._cond.notify_all()

    def kill(self) -> None:
        with self._cond:
            self._killed = True
            self._cond.notify_all()

    @property
    def killed(self) -> bool:
        return self._killed

    def flush(self, mask: int = everyEvent, stop_mask: int = 0) -> None:
        """FlushEvents: drop the queued events whose types are in mask, up
        to the first event whose type is in stop_mask.  High-level events
        are in a queue of their own on a Mac, which this leaves alone."""
        with self._cond:
            kept: deque[EventRecord] = deque()
            stopped = False
            for ev in self._input:
                if ev.what == kHighLevelEvent:
                    kept.append(ev)
                    continue
                stopped = stopped or bool(stop_mask & event_mask(ev.what))
                if stopped or not mask & event_mask(ev.what):
                    kept.append(ev)
            self._input = kept

    def discard(self, ev: EventRecord) -> None:
        """Drop an event, if it's still queued."""
        with self._cond:
            for i, queued in enumerate(self._input):
                if queued is ev:
                    del self._input[i]
                    break

    def forget(self, window) -> None:
        """Drop pending activate events for a window being disposed."""
        with self._cond:
            for queued in list(self._activates):
                if queued.message is window:
                    self._activates.remove(queued)

    def get(self, mask: int, timeout_ticks: int,
            next_update: Callable[[], Any]) -> tuple[bool, EventRecord]:
        """GetNextEvent (timeout 0) / WaitNextEvent (timeout = sleep)."""
        deadline = time.monotonic() + max(timeout_ticks, 0) / 60
        with self._cond:
            while True:
                if self._killed:
                    raise ProcessKilled()
                ev = self._take(mask, next_update)
                if ev is not None:
                    return True, ev
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    where, mods = self._input_state()
                    return False, EventRecord(nullEvent, 0, tick_count(), where, mods)
                self._cond.wait(remaining)

    def peek(self, mask: int, next_update: Callable[[], Any]) -> tuple[bool, EventRecord]:
        """EventAvail: the event get would return, left in the queue."""
        with self._cond:
            if self._killed:
                raise ProcessKilled()
            ev = self._take(mask, next_update, remove=False)
            if ev is not None:
                return True, ev
            where, mods = self._input_state()
            return False, EventRecord(nullEvent, 0, tick_count(), where, mods)

    def _take(self, mask, next_update, remove: bool = True) -> EventRecord | None:
        if mask & event_mask(activateEvt) and self._activates:
            return self._activates.popleft() if remove else self._activates[0]
        for ev in self._input:
            if mask & event_mask(ev.what):
                if remove:
                    self._input.remove(ev)
                return ev
        if mask & event_mask(updateEvt):
            window = next_update()
            if window is not None:
                where, mods = self._input_state()
                return EventRecord(updateEvt, window, tick_count(), where, mods)
        return None
