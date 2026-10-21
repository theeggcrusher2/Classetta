"""
The Sound Manager's traps (Inside Macintosh V-473): the channels and
their synthesizers are macrt.m68k.sound_manager's, which plays them.
SysBeep is the Toolbox's, ErrorSound the Dialog Manager's.
"""

from __future__ import annotations

import struct

from ..memory import ADDRESS_MASK
from ..toolbox.memmgr import memFullErr, noErr
from . import sound_manager as sm
from .dispatch import trap

badChannel = sm.badChannel
SCSTATUS_SIZE = 24
VERSION = 0x02008000        # a NumVersion: 2.0, the Sound Manager of System 6.0.7 and System 7


def _command(t, cmd: int) -> sm.Command:
    return struct.unpack(">HHI", t.mem.read(cmd & ADDRESS_MASK, 8))


@trap
def SndSoundManagerVersion(t):
    return VERSION


@trap
def SndNewChannel(t, chan_ptr, synth, init, user_routine):
    """A channel record, unless the program passed one of its own, which
    has to say how long its queue is."""
    chan = t.mem.r32(chan_ptr) & ADDRESS_MASK
    owned = not chan
    if owned:
        chan = t.process.heap.new_ptr(sm.CHANNEL_SIZE, clear=True)
        if not chan:
            return memFullErr
        t.mem.w16(chan + sm.QLENGTH, sm.STD_QLENGTH)
        t.w32(chan_ptr, chan)
    elif t.mem.r16s(chan + sm.QLENGTH) <= 0:
        return badChannel
    t.mem.w32(chan + sm.CALLBACK, user_routine)
    t.machine.sound_manager.new_channel(t, chan, synth, owned)
    return noErr


@trap
def SndAddModifier(t, chan, modifier, id, init):
    ch = t.machine.sound_manager.channel(chan)
    if ch is None:
        return badChannel
    t.machine.sound_manager.add_modifier(t, ch, modifier & ADDRESS_MASK, init)
    return noErr


@trap
def SndDoCommand(t, chan, cmd, no_wait):
    ch = t.machine.sound_manager.channel(chan)
    if ch is None:
        return badChannel
    return noErr if ch.put(t.mem, _command(t, cmd), wait=not no_wait) else sm.queueFull


@trap
def SndDoImmediate(t, chan, cmd):
    ch = t.machine.sound_manager.channel(chan)
    if ch is None:
        return badChannel
    t.machine.sound_manager.do_immediate(t, ch, _command(t, cmd))
    return noErr


@trap
def SndPlay(t, chan, snd, asynch):
    manager = t.machine.sound_manager
    ch = None
    if chan:
        ch = manager.channel(chan)
        if ch is None:
            return badChannel
    if not snd:
        return sm.resProblem
    return manager.play(t, ch, snd & ADDRESS_MASK, bool(asynch))


@trap
def SndControl(t, synth, cmd):
    """availableCmd: whether a synthesizer can do what init asks, which
    is anything but stereo; versionCmd: its version."""
    c, p1, p2 = _command(t, cmd)
    if c == sm.availableCmd:
        p1 = int(synth in sm.SYNTHS and p2 & sm.initStereo != sm.initStereo)
    elif c == sm.versionCmd:
        p2 = sm.SYNTH_VERSION
    t.mem.write(cmd & ADDRESS_MASK, struct.pack(">HHI", c, p1, p2))
    return noErr


@trap
def SndChannelStatus(t, chan, length, status):
    ch = t.machine.sound_manager.channel(chan)
    if ch is None:
        return badChannel
    t.mem.write(status, t.machine.sound_manager.status(ch)[:min(max(length, 0), SCSTATUS_SIZE)])
    return noErr


@trap
def SndDisposeChannel(t, chan, quiet_now):
    ch = t.machine.sound_manager.channel(chan)
    if ch is None:
        return badChannel
    t.machine.sound_manager.dispose(t, ch, bool(quiet_now))
    return noErr
