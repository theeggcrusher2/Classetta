"""
The PPC Toolbox (Inside Macintosh VI, chapter 7): ports, for the
applications that open one at startup so that others can reach them, as
System 7's applications that take Apple events do.  A port can be opened
and closed.  Nothing can connect to it, the network being out of reach
and Apple events going no further than their own application, so the
calls that would make a session aren't here, and Gestalt doesn't say
there's a PPC Toolbox.

Every application has a port of its own for its high-level events,
which the Process Manager names: the application's name, and its
creator with 'ep01'.
"""

from __future__ import annotations

import itertools
from weakref import WeakKeyDictionary

from ..memory import ADDRESS_MASK
from ..toolbox.memmgr import noErr
from .dispatch import raw, trap, unimpErr
from .glue_process import _process, _processes, procNotFound

noPortErr = -903
portNameExistsErr = -910
badPortNameErr = -919

ioCompletion, ioResult = 12, 16             # in every PPC parameter block
portRefNum = 38
portName = 46                               # a PPCOpenPBRec's
nbpRegistered = 55
PORT_REC = 72                               # a PPCPortRec: script, Str32, kind, Str32
ppcByCreatorAndType = 1
EVENT_PORT = b"ep01"                        # an application's high-level event port's type
NAMES = {0: "PPCInit", 1: "PPCOpen", 2: "PPCStart", 3: "PPCInform", 4: "PPCAccept",
         5: "PPCReject", 6: "PPCWrite", 7: "PPCRead", 8: "PPCEnd", 9: "PPCClose",
         10: "IPCListPorts", 12: "DeleteUserIdentity", 13: "GetDefaultUser",
         14: "StartSecureSession"}

_ports: WeakKeyDictionary = WeakKeyDictionary()     # process: {refnum: port name}
_refnums = itertools.count(1)


def port_key(t, p: int) -> bytes:
    return port_key_of(t.mem.read(p, PORT_REC))


def port_key_of(rec: bytes) -> bytes:
    """A PPCPortRec's name and type, which make a port what it is, but
    not its script."""
    data = rec[2:]
    name = data[1:1 + min(data[0], 32)]
    kind = data[34:36]
    rest = data[36:]
    by_creator = kind == ppcByCreatorAndType.to_bytes(2, "big")
    port_type = rest[:8] if by_creator else rest[1:1 + min(rest[0], 32)]
    return bytes([len(name)]) + name + kind + port_type


def _open(t, pb: int) -> int:
    rec = t.mem.r32(pb + portName) & ADDRESS_MASK
    if not rec or not t.mem.r8(rec + 2):
        return badPortNameErr
    key = port_key(t, rec)
    running = set(_processes(t))
    if any(key in ports.values() for proc, ports in _ports.items() if proc in running):
        return portNameExistsErr
    refnum = next(_refnums)
    _ports.setdefault(t.process, {})[refnum] = key
    t.mem.w16(pb + portRefNum, refnum)
    t.mem.w8(pb + nbpRegistered, False)
    return noErr


def _close(t, pb: int) -> int:
    ports = _ports.get(t.process, {})
    return noErr if ports.pop(t.mem.r16(pb + portRefNum), None) else noPortErr


@raw(0xA0DD)
def PPC(t, word):
    """The call D0 picks, with its parameter block at A0; an asynchronous
    one is done by the time the trap returns, and then its completion
    routine is called."""
    cpu = t.cpu
    pb, selector = cpu.a(0) & ADDRESS_MASK, cpu.d(0) & 0xFFFF
    if selector == 0:                       # PPCInit, which takes no parameter block
        cpu.set_d(0, noErr)
        return
    if selector == 1:
        err = _open(t, pb)
    elif selector == 9:
        err = _close(t, pb)
    else:
        t.unimplemented(NAMES.get(selector, f"PPC selector {selector}"), word)
        err = unimpErr
    t.mem.w16(pb + ioResult, err & 0xFFFF)
    completion = t.mem.r32(pb + ioCompletion) & ADDRESS_MASK if word & 0x400 else 0
    if completion:
        cpu.set_a(0, pb)
        cpu.set_d(0, err & 0xFFFFFFFF)
        t.call(completion)
        cpu.set_a(0, pb)
    cpu.set_d(0, err & 0xFFFFFFFF)


# =========================================================================
# The Process Manager's names for the applications' ports
# =========================================================================
def _event_port(proc) -> bytes:
    name = proc.name.encode("mac_roman", "replace")[:32]
    creator = proc.app.creator if proc.app is not None else b"????"
    name_field = (bytes([len(name)]) + name).ljust(34, b"\0")     # a Str32, padded
    return (bytes(2) + name_field + ppcByCreatorAndType.to_bytes(2, "big")
            + creator + EVENT_PORT).ljust(PORT_REC, b"\0")


@trap
def GetPortNameFromProcessSerialNumber(t, port_p, psn_p):
    proc = _process(t, psn_p)
    if proc is None:
        return procNotFound
    t.mem.write(port_p, _event_port(proc))
    return noErr


@trap
def GetProcessSerialNumberFromPortName(t, port_p, psn_p):
    key = port_key(t, port_p)
    for proc in _processes(t):
        if port_key_of(_event_port(proc)) == key:
            t.mem.w32(psn_p, 0)
            t.mem.w32(psn_p + 4, proc.serial_number)
            return noErr
    return procNotFound
