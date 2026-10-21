"""The Time Manager's bookkeeping: when tasks are due."""

import pytest

from macrt.m68k import timemgr
from macrt.m68k.timemgr import TimeTasks


class Clock:
    def __init__(self):
        self.now = 100.0

    def __call__(self):
        return self.now


@pytest.mark.parametrize("count, seconds", [(250, 0.25), (-500, 0.0005), (0, 0.0)])
def test_tmcount_is_milliseconds_or_microseconds_negated(count, seconds):
    assert timemgr.delay(count) == pytest.approx(seconds)


def test_what_rmvtime_leaves_is_microseconds_negated_if_they_fit():
    assert timemgr.remaining(0.25) == -250_000
    assert timemgr.remaining(0) == 0
    assert timemgr.remaining(3000.0) == 3_000_000         # too many: milliseconds


def test_tasks_are_due_in_the_order_they_were_primed_for():
    clock = Clock()
    tasks = TimeTasks(clock)
    tasks.install(0x1000)
    tasks.install(0x2000)
    tasks.prime(0x1000, 30)
    tasks.prime(0x2000, 10)
    assert tasks.next_due(clock.now + 0.005) is None
    assert tasks.next_due(clock.now + 0.05) == (0x2000, pytest.approx(100.01))
    assert tasks.next_due(clock.now + 0.05) == (0x1000, pytest.approx(100.03))
    assert tasks.next_due(clock.now + 0.05) is None and not tasks.due


def test_a_task_primed_from_its_routine_counts_from_when_it_was_due():
    clock = Clock()
    tasks = TimeTasks(clock)
    tasks.install(0x1000)
    tasks.prime(0x1000, 5)
    clock.now += 0.016                      # a tick later
    runs = 0
    while (due := tasks.next_due(clock.now)) is not None:
        runs += 1
        tasks.running = due[1]
        tasks.prime(0x1000, 5)
        tasks.running = None
    assert runs == 3                        # at 5, 10 and 15 ms
    assert tasks.due[0x1000] == pytest.approx(100.02)


def test_a_task_due_long_ago_runs_as_if_due_now():
    clock = Clock()
    tasks = TimeTasks(clock)
    tasks.install(0x1000, extended=True)
    tasks.prime(0x1000, 5)
    clock.now += 2.0                        # held up: in a host dialog, say
    assert tasks.next_due(clock.now) == (0x1000, clock.now)
    tasks.prime(0x1000, 5, woken=True)
    assert tasks.due[0x1000] == pytest.approx(clock.now + 0.005)     # not 2 s of catching up


def test_an_extended_task_with_tmwakeup_set_doesnt_drift():
    clock = Clock()
    tasks = TimeTasks(clock)
    tasks.install(0x1000, extended=True)
    tasks.prime(0x1000, 10)                 # tmWakeUp 0: from now
    clock.now += 0.012
    assert tasks.next_due(clock.now) == (0x1000, pytest.approx(100.01))
    clock.now += 0.003                      # primed again later, not from its routine
    assert tasks.prime(0x1000, 10, woken=True) == pytest.approx(100.02)
    assert tasks.prime(0x1000, 10, woken=False) == pytest.approx(clock.now + 0.01)


def test_rmvtime_gives_what_was_left_and_the_task_doesnt_run():
    clock = Clock()
    tasks = TimeTasks(clock)
    tasks.install(0x1000)
    tasks.prime(0x1000, 1000)
    clock.now += 0.25
    assert tasks.remove(0x1000) == pytest.approx(0.75)
    assert tasks.next_due(clock.now + 10) is None
    assert tasks.remove(0x1000) == 0.0      # not waiting
