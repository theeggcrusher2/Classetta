"""
macrt.m68k.timemgr - the Time Manager's tasks, for one application.

A TMTask is qLink, qType, tmAddr and tmCount; an extended one, which
InsXTime installs, has tmWakeUp and tmReserved after.  PrimeTime sets a
task to run after a delay, tmCount: milliseconds if it's positive,
microseconds negated if it's negative.  The runner runs each task that's
due between two instructions, with A1 pointing at it, as it runs VBL
tasks: at the tick after.  qType's high bit is set while the task waits.

A task that primes itself again from its routine has the delay counted
from when it was due, not from when it ran.  On a Mac that's a moment;
here it's up to a tick, and a task that runs every 5 milliseconds runs
three times a tick, not once.  An extended task with tmWakeUp set has it
counted from when it was last due wherever it's primed from, so that it
doesn't drift (Inside Macintosh: Processes, 3-8).
"""

from __future__ import annotations

import time

QTYPE, TM_ADDR, TM_COUNT, TM_WAKE_UP = 4, 6, 10, 14    # in a TMTask record
ACTIVE = 0x8000         # in qType, while the task waits to run
MAX_LATE = 0.1          # seconds a task due longer ago than that runs as if due now


def delay(count: int) -> float:
    """tmCount's delay, in seconds."""
    return count / 1000 if count >= 0 else -count / 1_000_000


def remaining(seconds: float) -> int:
    """A delay as RmvTime leaves it in tmCount: microseconds negated, if
    they fit, or else milliseconds."""
    us = max(0, round(seconds * 1_000_000))
    return -us if us < 0x80000000 else round(seconds * 1000)


class TimeTasks:
    """One application's tasks: those it has installed, and when each
    that's primed is due, in seconds of clock."""

    def __init__(self, clock=time.monotonic):
        self.clock = clock
        self.extended: set[int] = set()
        self.due: dict[int, float] = {}
        self.last: dict[int, float] = {}        # when each extended task was last due
        self.running: float | None = None       # when the task running now was due

    def install(self, task: int, extended: bool = False) -> None:
        self.due.pop(task, None)
        self.last.pop(task, None)
        if extended:
            self.extended.add(task)
        else:
            self.extended.discard(task)

    def prime(self, task: int, count: int, woken: bool = False) -> float:
        """Set task to run count from now, or from when it was last due:
        see the module's doc.  woken is whether an extended task's
        tmWakeUp is set.  Returns when it's due."""
        if woken and task in self.extended and task in self.last:
            base = self.last[task]
        elif self.running is not None:
            base = self.running
        else:
            base = self.clock()
        when = self.due[task] = base + delay(count)
        if task in self.extended:
            self.last[task] = when
        return when

    def remove(self, task: int) -> float:
        """Take task out; returns how long it had still to wait, if it was
        waiting, or 0."""
        self.extended.discard(task)
        self.last.pop(task, None)
        when = self.due.pop(task, None)
        return 0.0 if when is None else max(0.0, when - self.clock())

    def next_due(self, now: float) -> tuple[int, float] | None:
        """The task due first, if one's due by now, and when it counts as
        having run: when it was due, or now if that was long ago.  It's no
        longer waiting."""
        if not self.due:
            return None
        task = min(self.due, key=self.due.__getitem__)
        when = self.due[task]
        if when > now:
            return None
        del self.due[task]
        if when < now - MAX_LATE:
            when = now
            if task in self.last:
                self.last[task] = now
        return task, when
