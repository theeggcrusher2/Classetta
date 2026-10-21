"""MultiFinder's _OSDispatch calls, made for Hello (tests/apps/hello.s)
while it runs alongside a Python application."""

import pytest
from test_m68k_apps import launch_hello

from macrt.m68k import dispatch
from macrt.m68k.glue_os import Gestalt, trap_address
from macrt.m68k.glue_process import (GetCurrentProcess, GetFrontProcess, GetNextProcess,
                                     GetProcessInformation, SameProcess, TempDisposeHandle,
                                     TempFreeMem, TempHLock, TempNewHandle, kCurrentProcess,
                                     procNotFound)
from macrt.toolbox.memmgr import memWZErr, noErr

OSDISPATCH, UNIMPLEMENTED = 0xA88F, 0xA89F
gestaltTempMemSupport = 4


@pytest.fixture
def hello(harness):
    h = harness()
    proc, _ = launch_hello(h)
    t = proc.runner
    with proc.system.lock:
        scratch = proc.heap.new_ptr(128, clear=True)
    return h, proc, t, scratch


def psn(t, p):
    return t.mem.r32(p), t.mem.r32(p + 4)


def test_multifinder_is_there_but_hidden_from_get_trap_address(hello):
    _, _, t, _ = hello
    assert dispatch.implemented(OSDISPATCH)
    assert trap_address(t, OSDISPATCH) == trap_address(t, UNIMPLEMENTED)
    err, attrs = Gestalt(t, 0x6F732020)                 # 'os  '
    assert err == noErr and attrs & (1 << gestaltTempMemSupport)


def test_the_processes(hello):
    h, proc, t, p = hello
    other = next(q for q in h.d.launch_order if q is not proc)
    assert GetCurrentProcess(t, p) == noErr
    assert psn(t, p) == (0, proc.serial_number)
    assert GetFrontProcess(t, p + 8, -1) == noErr
    assert psn(t, p + 8) == (0, h.d.front_process.serial_number)

    seen = []
    t.mem.fill(p, 8)                                    # kNoProcess: start at the first
    while GetNextProcess(t, p) == noErr:
        seen.append(psn(t, p)[1])
    assert seen == [other.serial_number, proc.serial_number]
    assert psn(t, p) == (0, 0)

    t.mem.w32(p + 4, proc.serial_number)
    t.mem.w32(p + 12, kCurrentProcess)
    assert SameProcess(t, p, p + 8, p + 16) == noErr and t.mem.r8(p + 16) == 1
    t.mem.w32(p + 4, other.serial_number)
    assert SameProcess(t, p, p + 8, p + 16) == noErr and t.mem.r8(p + 16) == 0
    t.mem.w32(p + 4, 12345)
    assert GetProcessInformation(t, p, p + 16) == procNotFound


def test_process_information(hello):
    _, proc, t, p = hello
    info, name = p + 8, p + 80
    t.mem.w32(p + 4, kCurrentProcess)
    t.mem.w32(info, 56)                                 # no FSSpec
    t.mem.w32(info + 4, name)
    assert GetProcessInformation(t, p, info) == noErr
    assert t.mem.r_pstr(name) == b"Hello"
    assert psn(t, info + 8) == (0, proc.serial_number)
    assert t.mem.read(info + 16, 8) == b"APPLHELO"
    part = proc.partition
    assert (t.mem.r32(info + 28), t.mem.r32(info + 32)) == (part.start, part.end - part.start)
    assert 0 < t.mem.r32(info + 36) < part.end - part.start


def test_temporary_memory(hello):
    h, proc, t, p = hello
    heap = proc.system.sys_heap
    free = TempFreeMem(t)
    th = TempNewHandle(t, 4000, p)
    assert th and t.mem.r16s(p) == noErr and heap.contains(th)
    assert TempFreeMem(t) <= free - 4000
    TempHLock(t, th, p)
    assert t.mem.r16s(p) == noErr and heap.get_state(th) & 0x80
    TempHLock(t, proc.heap.new_handle(4), p)            # not a temporary one
    assert t.mem.r16s(p) == memWZErr
    TempDisposeHandle(t, th, p)
    assert t.mem.r16s(p) == noErr and not t.temp_handles

    # What the application leaves allocated goes when it quits.
    left = TempNewHandle(t, 4000, p)
    assert t.temp_handles == {left}
    proc.kill()
    h.wait_until(lambda: proc not in h.d.processes, what="Hello to quit")
    proc.thread.join(2)
    assert not t.temp_handles and TempFreeMem(t) >= free - 64
