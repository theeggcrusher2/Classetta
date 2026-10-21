"""
The Sound Manager (Inside Macintosh V, "The Sound Manager"), playing
through the sound buffer the Sound Driver plays through (macrt.m68k.sound).

A channel is a SndChannel record, the program's own or one SndNewChannel
makes, with a queue of the commands its synthesizer hasn't come to yet:
qHead is the next, qTail where the next one goes.  At each tick, each
channel's synthesizer makes the next frame, taking commands from its
queue as it comes to them, and the frames are added to the Sound
Driver's, if it has one playing, in the sound buffer.  While any channel
is making sound, the speaker is on.  SndDoImmediate's command goes to the
synthesizer at once, ahead of the queue.

The synthesizers:
  note (1)        noteCmd, freqCmd, restCmd, ampCmd and timbreCmd: a
                  waveform from a sine, at timbre 0, to a square at 255.
  wave table (3)  the same, with waveTableCmd's waveform.
  sampled (5)     bufferCmd plays a sound header's samples once, at its
                  rate times rateCmd's, and the commands after it wait
                  for its end.  soundCmd sets the instrument that noteCmd
                  and freqCmd play at a pitch, from its base note, looped
                  between its loop points while the note lasts.
Pitches are MIDI notes, 60 middle C, or Hz in 16.8; durations are in
half-milliseconds.  A noteCmd's amplitude is param2's high byte, where 0
is the channel's (ampCmd's, 255 to begin with), so that noteCmds with
just a pitch, as IM V's own example has, are heard.  waitCmd holds the
commands after it for its duration while what's sounding goes on;
restCmd ends the note first.  callBackCmd calls the channel's callback,
with the channel and the command (copied to cmdInProg), when the
synthesizer comes to it.  A program's own modifiers, from SndAddModifier,
see each command before the synthesizer, and may change it, drop it or
ask to send more (requestNextCmd).

SndPlay plays a 'snd ' resource's commands on a channel, with data
offsets made addresses in the resource, which it locks until the
channel has played it.  A soundCmd with no notes after it is played, as
the Sound Manager of System 6.0.7 on does: format 2 resources, sounds to
be played as they are, mostly have one.  Given no channel, SndPlay makes
one with the resource's synthesizer (the sampled one, if it names none),
plays the sound and waits for it, then frees the channel; SndPlay(chan,
snd, FALSE) waits too, as does SndDisposeChannel(chan, FALSE).  The
program waits in 68k code, so that ticks go on.

The samples are a Plus's, 22,254.5 a second, as the Sound Driver's are:
a sound recorded at rate22khz plays a sample a sample.  Not done: 'snth'
resources, as synthesizers or modifiers (those here stand in for
Apple's); MIDI; syncCmd's wait for other channels; tickleCmd; stereo,
whose sides are added; compressed sounds, which are silent.  Commands
past a full queue wait outside it, so a program never waits for room.
A channel whose application isn't the one running holds at a command
that calls its code (a callBackCmd, or any, if it has modifiers) until
it is.
"""

from __future__ import annotations

import collections
import functools
import math
import struct

from ..memory import ADDRESS_MASK, BusError
from ..toolbox.memmgr import memFullErr, noErr
from .sound import FRAME, SILENCE, SILENT_FRAME, fill

badChannel = -205
queueFull = -203
resProblem = -204
badFormat = -206

# Commands (IM V-482)
nullCmd, initCmd, freeCmd, quietCmd, flushCmd = 0, 1, 2, 3, 4
waitCmd, pauseCmd, resumeCmd, callBackCmd, syncCmd = 10, 11, 12, 13, 14
requestNextCmd, availableCmd, versionCmd = 21, 24, 25
noteCmd, restCmd, freqCmd, ampCmd, timbreCmd = 40, 41, 42, 43, 44
waveTableCmd, phaseCmd = 60, 61
soundCmd, bufferCmd, rateCmd = 80, 81, 82
dataPointerFlag = 0x8000

noteSynth, waveTableSynth, sampledSynth = 1, 3, 5
SYNTHS = (noteSynth, waveTableSynth, sampledSynth)
initStereo = 0xC0           # what availableCmd says isn't there: one speaker
SYNTH_VERSION = 0x02008000  # versionCmd's, the Sound Manager's own

# A SndChannel's fields
FIRST_MOD, CALLBACK, CMD_IN_PROG, QLENGTH, QHEAD, QTAIL, QUEUE = 4, 8, 20, 30, 32, 34, 36
STD_QLENGTH = 128
CHANNEL_SIZE = QUEUE + 8 * STD_QLENGTH
# A ModifierStub's
STUB_NEXT, STUB_CODE, STUB_SIZE = 0, 4, 22

# Sound headers
STD_HEADER, EXT_HEADER = 22, 64     # where the samples start
extSH = 0xFF

rate22khz = 0x56EE8BA3              # a Plus's samples a second, a Fixed
MAC_RATE = rate22khz / 0x10000
MAX_COMMANDS = 256                  # a frame's, for callbacks that queue more callbacks
MAX_REQUESTS = 64                   # requestNextCmds in a row
MAX_SAMPLES = 1 << 24               # a sound header's that isn't nonsense

Command = tuple[int, int, int]      # cmd, param1 (as a word), param2


def half_ms(duration: int) -> int:
    """Samples in a duration of half-milliseconds."""
    return round((duration & 0xFFFF) * MAC_RATE / 2000)


def hz(pitch: int) -> float:
    """A pitch's frequency: a MIDI note up to 127, else Hz in 16.8."""
    pitch &= 0xFFFFFF
    return 440 * 2 ** ((pitch - 69) / 12) if pitch < 128 else pitch / 256


@functools.lru_cache(maxsize=None)
def note_wave(timbre: int) -> bytes:
    """A period of the note synthesizer's waveform: a sine at timbre 0,
    clipped squarer as it goes up to 255."""
    gain = 1 + (timbre & 0xFF) / 8
    return bytes(SILENCE + round(127 * max(-1.0, min(1.0, gain * math.sin(2 * math.pi * i / 256))))
                 for i in range(256))


@functools.lru_cache(maxsize=None)
def _volume(amplitude: int) -> bytes:
    return bytes(SILENCE + (x - SILENCE) * amplitude // 255 for x in range(256))


def scaled(samples: bytes, amplitude: int) -> bytes:
    return samples if amplitude >= 255 else samples.translate(_volume(max(amplitude, 0)))


def mix(frames: list[bytes]) -> bytes:
    """Frames played together: their samples added, about silence."""
    if len(frames) == 1:
        return frames[0]
    off = SILENCE * (len(frames) - 1)
    return bytes(min(255, max(0, s - off)) for s in map(sum, zip(*frames)))


# =========================================================================
# What a synthesizer plays
# =========================================================================
class Samples:
    """Samples played from the start, step of them a sample, looped
    between loop's two if it has them, else once."""

    def __init__(self, data: bytes, step: float, amplitude: int = 255,
                 loop: tuple[int, int] | None = None, per_hz: float = 0.0):
        self.data, self.step, self.amplitude, self.loop = data, step, amplitude, loop
        self.per_hz = per_hz            # an instrument's: its step for a note of 1 Hz
        self.position = 0.0

    def tune(self, frequency: float) -> None:
        if self.per_hz:
            self.step = self.per_hz * frequency

    def take(self, n: int) -> tuple[bytes, bool]:
        """Up to n samples, fewer if they end; and whether they have."""
        data, pos, step = self.data, self.position, self.step
        end = len(data)
        if self.loop:
            start, stop = self.loop
            out = bytearray(n)
            for i in range(n):
                if pos >= stop:
                    pos = start + (pos - stop) % (stop - start)
                out[i] = data[int(pos)]
                pos += step
            self.position = pos
            return scaled(bytes(out), self.amplitude), False
        if pos >= end or step <= 0:
            return b"", True
        if step == 1.0:
            at = int(pos)
            out = data[at:at + n]
            self.position = pos + len(out)
        else:
            count = min(n, math.ceil((end - pos) / step))
            last = end - 1
            out = bytes(data[min(int(pos + i * step), last)] for i in range(count))
            self.position = pos + count * step
        return scaled(out, self.amplitude), self.position >= end


class Tone:
    """A waveform played over and over, at a frequency."""

    def __init__(self, wave: bytes, frequency: float, amplitude: int = 255):
        self.wave, self.amplitude = wave, amplitude
        self.phase = 0.0
        self.tune(frequency)

    def tune(self, frequency: float) -> None:
        self.increment = frequency * len(self.wave) / MAC_RATE

    def take(self, n: int) -> tuple[bytes, bool]:
        wave, size, phase, inc = self.wave, len(self.wave), self.phase, self.increment
        out = bytes(wave[int(phase + i * inc) % size] for i in range(n))
        self.phase = (phase + n * inc) % size
        return scaled(out, self.amplitude), False


def read_header(mem, at: int) -> tuple[bytes, int, tuple[int, int] | None, int] | None:
    """A sound header's samples (as offset binary bytes, one channel),
    rate, loop and base note; None for a compressed sound, or one that
    isn't in memory."""
    at &= ADDRESS_MASK
    try:
        ptr, length, rate, loop_start, loop_end = struct.unpack(">5I", mem.read(at, 20))
        encode, base = mem.r8(at + 20), mem.r8(at + 21)
        if length > MAX_SAMPLES:
            return None
        if encode == 0:
            data = mem.read(ptr & ADDRESS_MASK or at + STD_HEADER, length)
        elif encode == extSH:
            channels, frames = max(length, 1), mem.r32(at + 22)
            width = 2 if mem.r16(at + 48) == 16 else 1
            if frames * channels * width > MAX_SAMPLES:
                return None
            raw = mem.read(ptr & ADDRESS_MASK or at + EXT_HEADER, frames * channels * width)
            if width == 2:                  # two's complement: the high bytes, as offset binary
                raw = bytes(b ^ 0x80 for b in raw[0::2])
            data = raw if channels == 1 else bytes(
                sum(raw[i:i + channels]) // channels for i in range(0, len(raw), channels))
        else:
            return None
    except BusError:
        return None
    loop = (loop_start, loop_end) if loop_start < loop_end <= len(data) and \
        loop_end - loop_start > 1 else None
    return data, rate, loop, base


# =========================================================================
# Channels
# =========================================================================
class Channel:
    """A SndChannel record's synthesizer, and what it's doing."""

    def __init__(self, addr: int, process, synth: int, owned: bool):
        self.addr = addr
        self.process = process
        self.synth = synth
        self.owned = owned              # the record is the Sound Manager's to free
        self.amplitude = 255
        self.timbre = 0
        self.rate = 0x10000             # rateCmd's, for the buffers after it
        self.wave: bytes | None = None  # waveTableCmd's
        self.instrument: int | None = None  # soundCmd's sound header
        self.voice: Samples | Tone | None = None
        self.held = 0                   # samples before the next command
        self.note_ends = False          # the voice stops when they're done
        self.buffer = False             # the voice is a buffer: the next command waits for it
        self.paused = False
        self.overflow: collections.deque[Command] = collections.deque()
        self.locked: list[tuple[object, int, int]] = []     # SndPlay's: heap, handle, state
        self.dispose = False            # free it once it's done
        self.sounded = False

    # -- the queue, in the record ---------------------------------------------
    def queued(self, mem) -> bool:
        return bool(self.overflow) or mem.r16(self.addr + QHEAD) != mem.r16(self.addr + QTAIL)

    def _room(self, mem) -> bool:
        a = self.addr
        length, tail = mem.r16s(a + QLENGTH), mem.r16(a + QTAIL)
        return 0 <= tail < length and (tail + 1) % length != mem.r16(a + QHEAD)

    def _append(self, mem, cmd: Command) -> None:
        a = self.addr
        tail = mem.r16(a + QTAIL)
        mem.write(a + QUEUE + 8 * tail, struct.pack(">HHI", *cmd))
        mem.w16(a + QTAIL, (tail + 1) % mem.r16s(a + QLENGTH))

    def put(self, mem, cmd: Command, wait: bool = True) -> bool:
        """Queue cmd.  If there's no room, it waits outside the queue, or
        if it's not to wait, isn't queued: False."""
        if not self.overflow and self._room(mem):
            self._append(mem, cmd)
        elif wait:
            self.overflow.append(cmd)
        else:
            return False
        return True

    def peek(self, mem) -> Command | None:
        a = self.addr
        head, tail, length = mem.r16(a + QHEAD), mem.r16(a + QTAIL), mem.r16s(a + QLENGTH)
        if head != tail:
            if 0 <= head < length:
                return struct.unpack(">HHI", mem.read(a + QUEUE + 8 * head, 8))
            mem.w16(a + QHEAD, tail)        # nonsense: start again
        return self.overflow[0] if self.overflow else None

    def pop(self, mem) -> None:
        a = self.addr
        head = mem.r16(a + QHEAD)
        if head != mem.r16(a + QTAIL):
            mem.w16(a + QHEAD, (head + 1) % mem.r16s(a + QLENGTH))
        elif self.overflow:
            self.overflow.popleft()
        while self.overflow and self._room(mem):
            self._append(mem, self.overflow.popleft())

    def flush(self, mem) -> None:
        mem.w16(self.addr + QHEAD, mem.r16(self.addr + QTAIL))
        self.overflow.clear()

    # -- state -----------------------------------------------------------------
    def busy(self, mem) -> bool:
        """Whether it has commands to come to, or one it's still doing."""
        return bool(self.held or self.buffer or self.queued(mem))

    def quiet(self) -> None:
        """What's sounding stops, and the commands after a note or a
        buffer needn't wait for it; a waitCmd's wait goes on."""
        self.voice = None
        if self.note_ends:
            self.held = 0
        self.note_ends = self.buffer = False

    def sound(self, n: int) -> bytes:
        """n samples of what's sounding, or silence."""
        if self.voice is None:
            return bytes([SILENCE]) * n
        self.sounded = True
        out, ended = self.voice.take(n)
        if ended:
            self.voice = None
        return out + bytes([SILENCE]) * (n - len(out))


class SoundManager:
    """The Machine's: the channels of every application."""

    def __init__(self, machine):
        self.machine = machine
        self.mem = machine.mem
        self.channels: dict[int, Channel] = {}
        self.active = False             # it has the speaker on
        self.waiting: Channel | None = None     # the channel the program's waiting for
        self.wait_flag = 0              # a word that's 1 while it waits
        self._in_callback = 0

    # -- what the traps do -----------------------------------------------------
    def channel(self, addr: int) -> Channel | None:
        return self.channels.get(addr & ADDRESS_MASK)

    def new_channel(self, t, addr: int, synth: int, owned: bool) -> Channel:
        m = self.mem
        for field in (QHEAD, QTAIL):
            m.w16(addr + field, 0)
        m.w32(addr + FIRST_MOD, 0)
        ch = self.channels[addr] = Channel(addr, t.process, synth, owned)
        return ch

    def add_modifier(self, t, ch: Channel, code: int, init: int) -> None:
        """A modifier of the program's own, in front of the others, sent
        initCmd.  One that's a 'snth' resource is left out."""
        if not code:
            return
        heap = t.system.sys_heap
        stub = heap.new_ptr(STUB_SIZE, clear=True)
        if not stub:
            return
        m = self.mem
        m.w32(stub + STUB_NEXT, m.r32(ch.addr + FIRST_MOD))
        m.w32(stub + STUB_CODE, code)
        m.w32(ch.addr + FIRST_MOD, stub)
        self._modify(t, ch, code, stub, (initCmd, 0, init & 0xFFFFFFFF))

    def do_immediate(self, t, ch: Channel, cmd: Command) -> None:
        if cmd[0] & ~dataPointerFlag == bufferCmd:
            ch.quiet()                  # in place of what's playing
        self.send(t, ch, cmd)

    def dispose(self, t, ch: Channel, quiet_now: bool) -> None:
        """Quiet it, now or after what's queued, then free it: now, or
        when it's done, with the program waiting."""
        m = self.mem
        if quiet_now:
            ch.flush(m)
            ch.quiet()
            ch.held = 0
        if ch.busy(m):
            ch.dispose = True
            self.wait(t, ch)
        else:
            self._free(t, ch)

    def play(self, t, ch: Channel | None, snd: int, asynch: bool) -> int:
        """SndPlay: the resource's commands queued on ch, or on a channel
        of its own; noErr, or the error."""
        m = self.mem
        try:
            heap = t.system.heap_of(snd)
            data_at = heap.deref(snd) & ADDRESS_MASK
            size = heap.handle_size(snd)
        except (ValueError, KeyError, BusError):
            return resProblem
        parsed = parse_snd(m.read(data_at, size)) if data_at else None
        if parsed is None:
            return badFormat
        synth, _, commands = parsed
        made = ch is None
        if made:
            addr = t.system.sys_heap.new_ptr(CHANNEL_SIZE, clear=True)
            if not addr:
                return memFullErr
            ch = self.new_channel(t, addr, synth or sampledSynth, owned=True)
            m.w16(addr + QLENGTH, STD_QLENGTH)
            ch.dispose = True
        elif not ch.synth:
            ch.synth = synth
        ch.locked.append((heap, snd, heap.get_state(snd)))
        heap.lock(snd)
        for cmd, p1, p2 in commands:
            if cmd & dataPointerFlag:
                cmd, p2 = cmd & ~dataPointerFlag, (data_at + p2) & 0xFFFFFFFF
            ch.put(m, (cmd, p1, p2))
        if made or not asynch:
            if ch.busy(m):
                self.wait(t, ch)
            else:
                self._settle(t, ch)
        return noErr

    def status(self, ch: Channel) -> bytes:
        """An SCStatus: whether it's busy or paused."""
        busy = ch.busy(self.mem) or ch.voice is not None
        return struct.pack(">3IBBBBII", 0, 0, 0, busy, 0, ch.paused, 0, 0, 0)

    def forget(self, t) -> None:
        """t's application is quitting: its channels go, with no word to it."""
        for ch in list(self.channels.values()):
            if ch.process is t.process:
                self._free(t, ch, call=False)

    # -- waiting, in 68k code --------------------------------------------------
    def wait(self, t, ch: Channel) -> None:
        """The program waits for ch to be done: at the trap's return, a
        loop on a word that's 1 until it is, then on to the return
        address.  A0, A1 are free to use: a trap may change them."""
        m = self.mem
        if not self.wait_flag:
            self.wait_flag = t.system.sys_heap.new_ptr(2, clear=True)
        self.waiting = ch
        m.w16(self.wait_flag, 1)
        cpu = t.cpu
        cpu.set_a(1, cpu.pc)
        cpu.set_a(0, self.wait_flag)
        cpu.pc = t.machine.sound_wait

    def _settle(self, t, ch: Channel) -> None:
        """If ch is done: the program waiting for it goes on, the handles
        SndPlay locked are as they were, and if it's to be freed, it is."""
        if ch.busy(self.mem):
            return
        for heap, h, state in reversed(ch.locked):
            try:
                heap.set_state(h, state)
            except (ValueError, KeyError):
                pass
        ch.locked.clear()
        if self.waiting is ch:
            self.waiting = None
            self.mem.w16(self.wait_flag, 0)
        if ch.dispose:
            self._free(t, ch)

    def _free(self, t, ch: Channel, call: bool = True) -> None:
        """Free ch, its modifiers sent freeCmd if call."""
        m = self.mem
        stub = m.r32(ch.addr + FIRST_MOD) & ADDRESS_MASK
        for _ in range(64):
            if not stub:
                break
            after = m.r32(stub + STUB_NEXT) & ADDRESS_MASK
            code = m.r32(stub + STUB_CODE) & ADDRESS_MASK
            if code and call and ch.process is t.process:
                self._modify(t, ch, code, stub, (freeCmd, 0, 0))
            if t.system.sys_heap.contains(stub):
                t.system.sys_heap.dispose_ptr(stub)
            stub = after
        m.w32(ch.addr + FIRST_MOD, 0)
        self.channels.pop(ch.addr, None)
        if self.waiting is ch:
            self.waiting = None
            m.w16(self.wait_flag, 0)
        if ch.owned and (call or t.system.sys_heap.contains(ch.addr)):
            t.system.heap_of(ch.addr).dispose_ptr(ch.addr)

    # -- the synthesizers ------------------------------------------------------
    def send(self, t, ch: Channel, cmd: Command) -> None:
        """cmd through the channel's modifiers to its synthesizer."""
        for c in self._through(t, ch, cmd, self.mem.r32(ch.addr + FIRST_MOD) & ADDRESS_MASK):
            self._synth(t, ch, c)

    def _through(self, t, ch: Channel, cmd: Command, stub: int, depth: int = 0) -> list[Command]:
        if not stub or depth > 16:
            return [cmd]
        m = self.mem
        code = m.r32(stub + STUB_CODE) & ADDRESS_MASK
        after = m.r32(stub + STUB_NEXT) & ADDRESS_MASK
        out: list[Command] = []
        for count in range(1, MAX_REQUESTS + 1):
            more = False
            if code:
                cmd, more = self._modify(t, ch, code, stub, cmd)
            if cmd[0] != nullCmd:
                out += self._through(t, ch, cmd, after, depth + 1)
            if not more:
                break
            cmd = (requestNextCmd, count, 0)
        return out

    def _modify(self, t, ch: Channel, code: int, stub: int, cmd: Command) -> tuple[Command, bool]:
        """A modifier's call: the command it passes on, and whether it
        wants to send another."""
        m = self.mem
        at = ch.addr + CMD_IN_PROG
        m.write(at, struct.pack(">HHI", *cmd))
        more = t.call(code, (("p", ch.addr), ("p", at), ("p", stub)), "b")
        return struct.unpack(">HHI", m.read(at, 8)), bool(more)

    def _synth(self, t, ch: Channel, cmd: Command) -> None:
        m = self.mem
        c, p1, p2 = cmd
        c &= ~dataPointerFlag
        if c == quietCmd:
            ch.quiet()
        elif c == flushCmd:
            ch.flush(m)
        elif c == waitCmd:
            ch.held, ch.note_ends = half_ms(p1), False
        elif c == pauseCmd:
            ch.paused = True
        elif c == resumeCmd:
            ch.paused = False
        elif c == callBackCmd:
            proc = m.r32(ch.addr + CALLBACK) & ADDRESS_MASK
            if proc and self._in_callback < 4:
                m.write(ch.addr + CMD_IN_PROG, struct.pack(">HHI", c, p1, p2))
                self._in_callback += 1
                try:
                    t.call(proc, (("p", ch.addr), ("p", ch.addr + CMD_IN_PROG)))
                finally:
                    self._in_callback -= 1
        elif c == noteCmd:
            amplitude = p2 >> 24 or ch.amplitude
            ch.voice = self._voice(ch, p2, amplitude)
            ch.held, ch.note_ends, ch.buffer = half_ms(p1), True, False
        elif c == restCmd:
            ch.voice = None
            ch.held, ch.note_ends, ch.buffer = half_ms(p1), False, False
        elif c == freqCmd:
            if not p2 & 0xFFFFFF:
                ch.voice = None
            elif ch.voice is not None and not ch.buffer and (
                    isinstance(ch.voice, Tone) or ch.voice.per_hz):
                ch.voice.tune(hz(p2))
            else:
                ch.voice = self._voice(ch, p2, ch.amplitude)
                ch.buffer = False
        elif c == ampCmd:
            ch.amplitude = max(0, min(255, p1 if p1 < 0x8000 else 0))
            if ch.voice is not None:
                ch.voice.amplitude = ch.amplitude
        elif c == timbreCmd:
            ch.timbre = p1 & 0xFF
        elif c == waveTableCmd:
            if 0 < p1 < 0x8000:
                ch.wave = m.read(p2 & ADDRESS_MASK, p1)
        elif c == soundCmd:
            ch.instrument = p2 & ADDRESS_MASK
        elif c == bufferCmd:
            sound = read_header(m, p2)
            if sound is not None and sound[0]:
                data, rate, _, _ = sound
                ch.voice = Samples(data, rate / rate22khz * ch.rate / 0x10000, ch.amplitude)
                ch.buffer = True
                ch.held = 0
        elif c == rateCmd:
            old, ch.rate = ch.rate, p2 or 0x10000
            if ch.buffer and isinstance(ch.voice, Samples) and old:
                ch.voice.step *= ch.rate / old

    def _voice(self, ch: Channel, pitch: int, amplitude: int) -> Samples | Tone | None:
        """What ch's synthesizer plays for a note at pitch."""
        if ch.synth == sampledSynth:
            sound = read_header(self.mem, ch.instrument) if ch.instrument else None
            if sound is None or not sound[0]:
                return None
            data, rate, loop, base = sound
            per_hz = rate / rate22khz / hz(base or 60)
            return Samples(data, per_hz * hz(pitch), amplitude, loop, per_hz)
        if ch.synth == waveTableSynth:
            return Tone(ch.wave or note_wave(0), hz(pitch), amplitude)
        if ch.synth == noteSynth:
            return Tone(note_wave(ch.timbre), hz(pitch), amplitude)
        return None

    # -- each tick -------------------------------------------------------------
    def busy(self) -> bool:
        """Whether a channel has a command to come to, or sound to make."""
        m = self.mem
        return self.active or any(ch.voice is not None or ch.busy(m)
                                  for ch in self.channels.values())

    def retrace(self, t) -> None:
        """A tick: each channel's next frame, added to the Sound Driver's
        in the sound buffer."""
        if not self.channels and not self.active:
            return
        frames = []
        for ch in list(self.channels.values()):
            frame = self._frame(t, ch)
            if frame is not None:
                frames.append(frame)
            self._settle(t, ch)
        driver = self.machine.sound
        if frames:
            if not self.active:
                self.active = True
                driver.switch.claim(self)
            if driver.active:
                frames.append(driver.frame)
            fill(self.mem, mix(frames))
        elif self.active:
            self.active = False
            if not driver.active:
                fill(self.mem, SILENT_FRAME)
            driver.switch.release(self)

    def _frame(self, t, ch: Channel) -> bytes | None:
        """ch's next frame, coming to its commands as it goes; None if it
        made no sound."""
        m = self.mem
        ch.sounded = False
        parts = []
        need = FRAME
        for _ in range(MAX_COMMANDS):
            if not need:
                break
            if ch.held:
                n = min(ch.held, need)
                parts.append(ch.sound(n))
                need -= n
                ch.held -= n
                if not ch.held and ch.note_ends:
                    ch.voice, ch.note_ends = None, False
                continue
            if ch.buffer:
                if ch.voice is None:
                    ch.buffer = False
                    continue
                ch.sounded = True
                out, ended = ch.voice.take(need)
                parts.append(out)
                need -= len(out)
                if ended:
                    ch.voice, ch.buffer = None, False
                continue
            if ch.paused:
                break
            cmd = ch.peek(m)
            if cmd is None or not self._may_do(t, ch, cmd):
                break
            ch.pop(m)
            self.send(t, ch, cmd)
        if need:
            parts.append(ch.sound(need))
        return b"".join(parts) if ch.sounded else None

    def _may_do(self, t, ch: Channel, cmd: Command) -> bool:
        """Whether ch can come to cmd now: not if it would call code of an
        application that isn't the one running."""
        if ch.process is t.process:
            return True
        return cmd[0] != callBackCmd and not self.mem.r32(ch.addr + FIRST_MOD)


# =========================================================================
# 'snd ' resources
# =========================================================================
def parse_snd(data: bytes) -> tuple[int, int, list[Command]] | None:
    """A 'snd ' resource's synthesizer (0 for none), its init, and its
    commands, with a soundCmd that no note follows played too; None if
    it isn't one."""
    try:
        fmt, = struct.unpack_from(">h", data)
        at = 2
        synth = init = 0
        if fmt == 1:
            n, = struct.unpack_from(">h", data, at)
            at += 2
            for i in range(max(n, 0)):
                mod_id, mod_init = struct.unpack_from(">hI", data, at)
                if i == 0:              # the synthesizer comes first
                    synth, init = mod_id, mod_init
                at += 6
        elif fmt == 2:
            at += 2                     # its reference count
        else:
            return None
        n, = struct.unpack_from(">h", data, at)
        at += 2
        commands = [struct.unpack_from(">HHI", data, at + 8 * i) for i in range(max(n, 0))]
    except struct.error:
        return None
    for i, (cmd, p1, p2) in enumerate(commands):
        if cmd & ~dataPointerFlag == soundCmd and not any(
                c & ~dataPointerFlag in (noteCmd, freqCmd, bufferCmd) for c, _, _ in commands[i:]):
            commands.insert(i + 1, (cmd & dataPointerFlag | bufferCmd, p1, p2))
            break
    return synth, init, commands
