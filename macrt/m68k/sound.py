"""
The Sound Driver, and the speaker it plays through (Inside Macintosh II,
"The Sound Driver").

A Mac 128K, 512K or Plus makes its sound from a buffer of 370 words in
memory, at SoundBase.  As the screen is drawn, each line's horizontal
blanking sends the high byte of the next word to the speaker: 370 samples
a frame, about 22,255 a second.  They're heard while vBufB's bit 7,
vSndEnb, is 0, at the volume in vBufA's low three bits.  Games write the
buffer themselves, as Dark Castle and Crystal Quest do, at each tick;
other programs have the Sound Driver fill it.

The Speaker here takes the buffer at each of macrt's ticks, after the VBL
tasks have run, and sends the frame, at its volume, to where the sound
goes: the host's sound output (macrt.ui.audio), a WAV file (WaveFile), or
nowhere.  macrt's ticks are 60 a second, a Mac's 60.15, so the samples go
out at 22,200 a second: what the Sound Driver makes from waveforms is 4
cents flat.  The alternate sound buffer isn't kept, nor does timer 1 work
vSndEnb itself.

The Sound Driver is the device driver '.Sound', reference number -4.  A
Write (StartSound) starts a sound, or queues it behind the one playing.
The requests are in the queue of its device control entry, at SoundDCE,
the one playing at its head, and it's that queue's parameter blocks that
are done: Downhill Racer's StartSound glue copies the one playing to a
bigger handle, to keep the sounds it's to play next after it, and puts
the copy in the queue's head and tail.
An immediate one returns at once, as an asynchronous one does, rather
than wait for the sound to end: Apache Strike starts a four-tone sound
of over two minutes that way and changes it from its VBL task.
Its buffer's first word says which synthesizer makes it:

  square-wave  triplets of count, amplitude and duration, up to one all 0
               or the end of what's written: for each, a square wave of
               783,360/count Hz, for duration ticks.  CurPitch and
               SoundLevel hold the count and amplitude it's playing, and
               Control's csCode 3 changes the amplitude.
  four-tone    a pointer to a four-tone record, which goes in SoundPtr:
               four waveforms of 256 bytes, each stepped through at its
               rate (a Fixed, the bytes a sample) from its phase (a Fixed
               too, kept as it goes), the four bytes added and divided by
               4, for the record's duration, counted down a tick at a
               time.  MusicWorks keeps putting the duration back up while
               it changes the rates, for the notes of its songs.
  free-form    a count, a Fixed, and the bytes, stepped through at count
               a sample to the end of what's written.

The driver's VBL task, SoundVBL in low memory, makes each frame, and sets
its count to 1 again for the next.  A program can hold the sound by
setting the count higher, as MusicWorks does when it pauses.  The task is
the System's: it counts down at every application's ticks, not from an
application's VBL queue.  A sound that's done is taken off the queue, its
ioResult set and its completion routine called, and the next one starts
at the next tick; its last frame plays out first.  KillIO stops them all,
with abortErr, and those their completion routines write meanwhile: that
glue's StopSound disposes of the handle once KillIO returns, and its
completion routine starts the next sound it kept.  An application's
completion routines are called while it's the one running.  While it has
sounds, the driver turns the speaker on with vSndEnb, and after them off
again, unless the program had it on; the Sound Manager
(macrt.m68k.sound_manager) shares it, adding its channels' frames to the
driver's.  A program that quits takes its sounds with it.
"""

from __future__ import annotations

import collections
import struct
import threading
import wave
from dataclasses import dataclass
from operator import add

from ..memory import ADDRESS_MASK
from ..toolbox import lowmem
from . import glue_devices
from .hardware import HZ, vBufA, vBufB

FRAME = 370                     # samples a tick
RATE = FRAME * 60               # samples a second, at macrt's ticks
SILENCE = 0x80
SOUND_DRIVER = -4
swMode, ffMode, ftMode = -1, 0, 1
vSndEnb = 0x80                  # vBufB: 0 while the speaker's on
VOLUME = 0x07                   # vBufA: its volume
vType = 1
VBL_ADDR, VBL_COUNT = 6, 10     # in a VBLTask record
SOUND_VBL_COUNT = lowmem.SoundVBL + VBL_COUNT
SET_AMPLITUDE = 3               # Control's csCode for the square wave's amplitude

KILL_ROUNDS = 64                # KillIO's, for completion routines that write again

# Parameter blocks (IM II, chapter 6)
ioBuffer = 32
ioReqCount = 36
ioActCount = 40

# A four-tone record: duration, then each tone's rate and phase, then its waveform
FT_PHASE = 6
FT_WAVE = 34
FT_SIZE = 50
WAVE = 256
SILENT_WAVE = bytes([SILENCE]) * WAVE
SILENT_FRAME = bytes([SILENCE]) * FRAME
PHASE_MASK = (WAVE << 16) - 1   # a byte in the waveform, and a fraction


# =========================================================================
# The synthesizers: each tick's frame
# =========================================================================
def square_wave(count: int, amplitude: int, phase: int) -> tuple[bytes, int]:
    """A frame of a square wave of HZ/count Hz, amplitude from bottom to
    top around silence, and where in its period the next frame starts.
    The phase is in samples times HZ, a period count times RATE; with a
    count of 0, the wave is still."""
    if not count:
        return SILENT_FRAME, phase
    low = SILENCE - (amplitude + 1) // 2      # 255 is from 0 to 255
    high = low + amplitude
    period = count * RATE
    half = period // 2
    frame = bytes(high if (phase + i * HZ) % period < half else low for i in range(FRAME))
    return frame, (phase + FRAME * HZ) % period


def four_tone(tones: list[tuple[int, int, bytes]]) -> tuple[bytes, list[int]]:
    """A frame of four tones, each its rate, its phase (both 16.16, in
    bytes of the waveform) and its waveform of 256 bytes: their bytes
    added and divided by 4.  And each tone's phase after it."""
    total = [0] * FRAME
    phases = []
    for rate, phase, wave_bytes in tones:
        if rate & PHASE_MASK:
            total = list(map(add, total, [wave_bytes[(phase + rate * i) >> 16 & 0xFF]
                                          for i in range(FRAME)]))
        else:
            level = wave_bytes[phase >> 16 & 0xFF]
            total = [x + level for x in total]
        phases.append((phase + rate * FRAME) & PHASE_MASK)
    return bytes(x >> 2 for x in total), phases


def free_form(data: bytes, start: int, position: int, count: int,
              length: int) -> tuple[bytes, int]:
    """A frame of a free-form sound: its bytes from position (16.16) on,
    count (16.16) a sample, to length; data holds them from byte start.
    Silence after the end.  And where the next frame starts."""
    count = max(count, 0)
    end = length << 16
    samples = []
    pos = position
    for _ in range(FRAME):
        if pos >= end:
            break
        samples.append(data[(pos >> 16) - start])
        pos += count
    return bytes(samples) + bytes([SILENCE]) * (FRAME - len(samples)), position + count * FRAME


# =========================================================================
# The Sound Driver
# =========================================================================
@dataclass
class _Sound:
    """A Write to the driver: the parameter block, whose program wrote
    it, and how far it's got."""
    pb: int
    process: object
    mode: int
    buffer: int
    size: int
    position: int = 0       # free-form: the next sample, 16.16; square-wave: the next triplet
    left: int = 0           # square-wave: ticks left of the triplet playing
    phase: int = 0          # square-wave: where in its period


class SoundDriver:
    """The Machine's: one for every application."""

    def __init__(self, machine):
        self.machine = machine
        self.mem = machine.mem
        self.playing: _Sound | None = None
        self.queue: collections.deque[_Sound] = collections.deque()
        self.owed: list[tuple[_Sound, int]] = []    # completion routines, and their results
        self.switch = SpeakerSwitch(machine)
        self.active = False         # it has the speaker on, for its sounds
        self.frame = SILENT_FRAME   # the last it made
        self._ending = False        # the last sound's last frame is playing out

    def install(self, heap) -> None:
        """Its device control entry, at SoundDCE."""
        m = self.mem
        dce = heap.new_ptr(glue_devices.DCE_SIZE, clear=True)
        m.w16(dce + glue_devices.dCtlFlags,
              glue_devices.dWritEnable | glue_devices.dCtlEnable | glue_devices.dOpened)
        m.w16(dce + glue_devices.dCtlRefNum, SOUND_DRIVER & 0xFFFF)
        m.w32(lowmem.SoundDCE, dce)

    # -- the Device Manager's calls --------------------------------------------
    def call(self, t, w: int) -> bool:
        """Do Device Manager call w, A0 its parameter block, if it's a
        Write, Control or KillIO for the Sound Driver."""
        pb = t.cpu.a(0) & ADDRESS_MASK
        if t.mem.r16s(pb + glue_devices.ioRefNum) != SOUND_DRIVER:
            return False
        n = w & 0xFF
        if n == 3:
            self._write(t, pb, w)
        elif n == 4:
            self._control(t, pb, w)
        elif n == 6:
            self._kill(t, pb, w)
        else:
            return False
        return True

    def _write(self, t, pb: int, w: int) -> None:
        m = t.mem
        m.w16(pb + glue_devices.ioTrap, w)
        m.w32(pb + ioActCount, 0)
        buffer = m.r32(pb + ioBuffer) & ADDRESS_MASK
        size = m.r32s(pb + ioReqCount)
        mode = m.r16s(buffer) if size >= 2 else None
        if mode not in (swMode, ffMode, ftMode) or mode != swMode and size < 6:
            m.w32(pb + ioActCount, max(size, 0))
            glue_devices.finish(t, pb, w, glue_devices.noErr)      # nothing to play
            return
        m.w16(pb + glue_devices.ioResult, 1)                       # in progress
        m.w16(pb + glue_devices.qType, glue_devices.ioQType)
        self._follow(m)
        self.queue.append(_Sound(pb, t.process, mode, buffer, size))
        self._link(m)
        if self.playing is None:
            self._next(t)
        if w & glue_devices.IMMEDIATE:
            t.cpu.set_d(0, glue_devices.noErr)     # it's begun: it isn't waited for
        else:
            glue_devices.wait(t, pb, w)

    def _control(self, t, pb: int, w: int) -> None:
        m = t.mem
        code = m.r16s(pb + glue_devices.csCode)
        if code == glue_devices.killCode:
            self._kill(t, pb, w)
            return
        if code == SET_AMPLITUDE:
            m.w8(lowmem.SoundLevel, min(m.r16(pb + glue_devices.csParam), 0xFF))
        glue_devices.finish(t, pb, w, glue_devices.noErr)

    def _kill(self, t, pb: int, w: int) -> None:
        """Stop the sound playing and those queued: each is done, with
        abortErr, and so are those their completion routines write.  The
        parameter block gets its result and nothing else: StopSound's glue
        passes one that begins 2 bytes below its frame, so that where
        ioTrap would be is its return address (Daleks)."""
        m = t.mem
        stopped = self._take(m)
        self._stop(m)
        for _ in range(KILL_ROUNDS):
            if not stopped:
                break
            for sound in stopped:
                self._done(t, sound, glue_devices.abortErr)
            if stopped := self._take(m):
                self._stop(m)
        m.w16(pb + glue_devices.ioResult, glue_devices.noErr)
        t.cpu.set_d(0, glue_devices.noErr)

    def _take(self, m) -> list[_Sound]:
        """Every sound, off the queue."""
        self._follow(m)
        sounds = self._sounds()
        self.playing = None
        self.queue.clear()
        self._link(m)
        return sounds

    def forget(self, t) -> None:
        """t's application is quitting: its sounds go, with no word to it."""
        self._follow(t.mem)
        self.queue = collections.deque(s for s in self.queue if s.process is not t.process)
        self.owed = [(s, err) for s, err in self.owed if s.process is not t.process]
        mine = self.playing is not None and self.playing.process is t.process
        if mine:
            self.playing = None
        self._link(t.mem)
        if mine:
            if self.queue:
                self._next(t)
            else:
                self._stop(t.mem)

    # -- playing ---------------------------------------------------------------
    def counting(self) -> bool:
        """Whether its task is counting down to a frame, or the last frame
        is playing out."""
        return self._ending or (self.playing is not None
                                and self.mem.r16s(SOUND_VBL_COUNT) > 0)

    def _next(self, t) -> None:
        """Start the next sound in the queue, at the next tick."""
        m = t.mem
        if not self.queue:
            self._ending = self.active
            return
        sound = self.playing = self.queue.popleft()
        self._ending = False
        if not self.active:
            self.switch.claim(self)
            self.active = True
            self.frame = SILENT_FRAME
        if sound.mode == ftMode:
            m.w32(lowmem.SoundPtr, m.r32(sound.buffer + 2))
        elif sound.mode == swMode:
            sound.position = 2
        m.w16(lowmem.SoundVBL + 4, vType)
        m.w32(lowmem.SoundVBL + VBL_ADDR, self.machine.sound_task)
        m.w16(SOUND_VBL_COUNT, 1)
        m.w8(lowmem.SoundActive, 1)

    # -- the queue --------------------------------------------------------------
    def _sounds(self) -> list[_Sound]:
        return ([self.playing] if self.playing else []) + list(self.queue)

    def _follow(self, m) -> None:
        """Take the sounds' parameter blocks from the DCE's queue, where a
        program may have put others."""
        dce = m.r32(lowmem.SoundDCE) & ADDRESS_MASK
        pb = m.r32(dce + glue_devices.dCtlQHead) & ADDRESS_MASK if dce else 0
        for sound in self._sounds():
            if not pb:
                break
            sound.pb = pb
            pb = m.r32(pb + glue_devices.qLink) & ADDRESS_MASK

    def _link(self, m) -> None:
        """Put the sounds' parameter blocks in the DCE's queue, the one
        playing first."""
        dce = m.r32(lowmem.SoundDCE) & ADDRESS_MASK
        if not dce:
            return
        pbs = [s.pb for s in self._sounds()]
        for pb, after in zip(pbs, pbs[1:] + [0]):
            m.w32(pb + glue_devices.qLink, after)
        m.w32(dce + glue_devices.dCtlQHead, pbs[0] if pbs else 0)
        m.w32(dce + glue_devices.dCtlQTail, pbs[-1] if pbs else 0)

    def _stop(self, m) -> None:
        """No more sounds: silence in the buffer, and the speaker as the
        program had it."""
        self._ending = False
        m.w16(SOUND_VBL_COUNT, 0)
        m.w8(lowmem.SoundActive, 0)
        if self.active:
            self.active = False
            fill(m, SILENT_FRAME)
            self.switch.release(self)

    def _done(self, t, sound: _Sound, err: int) -> None:
        """A sound's done, with result err: its completion routine is
        called when its application is the one running."""
        if sound.process is t.process:
            glue_devices.done(t, sound.pb, err)
        else:
            t.mem.w16(sound.pb + glue_devices.ioResult, err)
            self.owed.append((sound, err))

    def retrace(self, t) -> None:
        """A tick: count the task down, and at 0 make the next frame."""
        m = t.mem
        if self.owed:
            owed = [(s, err) for s, err in self.owed if s.process is t.process]
            self.owed = [(s, err) for s, err in self.owed if s.process is not t.process]
            for s, err in owed:
                glue_devices.done(t, s.pb, err)
        sound = self.playing
        if sound is None:
            if self._ending:
                self._stop(m)
            return
        count = m.r16s(SOUND_VBL_COUNT)
        if count <= 0:
            return
        m.w16(SOUND_VBL_COUNT, count - 1)
        if count > 1:
            return
        m.w16(SOUND_VBL_COUNT, 1)
        frame, done = {swMode: self._square, ftMode: self._four_tone,
                       ffMode: self._free_form}[sound.mode](m, sound)
        if frame is not None:
            self.frame = frame
            fill(m, frame)
        if done:
            self._follow(m)
            self.playing = None
            self._link(m)
            m.w32(sound.pb + ioActCount, sound.size)
            self._done(t, sound, glue_devices.noErr)
            if self.playing is None:            # its completion routine may have started one
                self._next(t)

    def _square(self, m, sound: _Sound) -> tuple[bytes | None, bool]:
        if not sound.left:
            tone, sound.position = _tone(m, sound.buffer, sound.size, sound.position)
            if tone is None:
                return None, True
            count, amplitude, sound.left = tone
            m.w16(lowmem.CurPitch, count)
            m.w8(lowmem.SoundLevel, amplitude)
        frame, sound.phase = square_wave(m.r16(lowmem.CurPitch), m.r8(lowmem.SoundLevel),
                                         sound.phase)
        sound.left -= 1
        return frame, not sound.left and _tone(m, sound.buffer, sound.size,
                                               sound.position)[0] is None

    def _four_tone(self, m, sound: _Sound) -> tuple[bytes | None, bool]:
        record = m.r32(lowmem.SoundPtr) & ADDRESS_MASK
        duration, *fields = struct.unpack(">h12I", m.read(record, FT_SIZE))
        if duration <= 0:
            return None, True
        tones = []
        for i in range(4):
            at = fields[8 + i] & ADDRESS_MASK
            tones.append((fields[2 * i], fields[2 * i + 1],
                          m.read(at, WAVE) if at else SILENT_WAVE))
        frame, phases = four_tone(tones)
        for i, phase in enumerate(phases):
            m.w32(record + FT_PHASE + 8 * i, phase)
        m.w16(record, duration - 1)
        return frame, duration == 1

    def _free_form(self, m, sound: _Sound) -> tuple[bytes | None, bool]:
        count = m.r32s(sound.buffer + 2)
        length = sound.size - 6
        start = sound.position >> 16
        if start >= length:
            return None, True
        end = min(length, ((sound.position + max(count, 0) * (FRAME - 1)) >> 16) + 1)
        data = m.read(sound.buffer + 6 + start, end - start)
        frame, sound.position = free_form(data, start, sound.position, count, length)
        return frame, sound.position >> 16 >= length


def fill(m, frame: bytes) -> None:
    """Put a frame in the sound buffer's high bytes."""
    base = m.r32(lowmem.SoundBase) & ADDRESS_MASK
    words = bytearray(m.read(base, 2 * FRAME))
    words[0::2] = frame
    m.write(base, bytes(words))


def _tone(m, buffer: int, size: int, at: int) -> tuple[tuple[int, int, int] | None, int]:
    """The next triplet of a square-wave sound that plays, from offset at:
    count, amplitude and duration, and the offset after it; None at its end."""
    while at + 6 <= size:
        count, amplitude, duration = struct.unpack(">HHh", m.read(buffer + at, 6))
        at += 6
        if not (count or amplitude or duration):
            break
        if duration > 0:
            return (count, min(amplitude, 0xFF), duration), at
    return None, at


# =========================================================================
# The speaker
# =========================================================================
class SpeakerSwitch:
    """vSndEnb, for the Sound Driver and the Sound Manager: on while
    either has sounds, then as the program had it."""

    def __init__(self, machine):
        self.machine = machine
        self.users: set[int] = set()
        self._was_on = False        # the program had it on before

    def claim(self, user) -> None:
        if not self.users:
            regs = self.machine.via.regs
            self._was_on = not regs[vBufB] & vSndEnb
            regs[vBufB] &= ~vSndEnb
        self.users.add(id(user))

    def release(self, user) -> None:
        if id(user) not in self.users:
            return
        self.users.discard(id(user))
        if not self.users and not self._was_on:
            self.machine.via.regs[vBufB] |= vSndEnb


_VOLUMES = [bytes(SILENCE + (x - SILENCE) * v // 7 for x in range(256)) for v in range(8)]


class Speaker:
    """The sound buffer, a frame a tick while vSndEnb has the speaker on,
    to each of outputs: anything with a frame(samples) method, given each
    frame's 370 samples, unsigned bytes, at the volume."""

    def __init__(self, mem, via, outputs=()):
        self.mem = mem
        self.via = via
        self.outputs = list(outputs)

    @property
    def on(self) -> bool:
        return bool(self.outputs) and not self.via.regs[vBufB] & vSndEnb

    def retrace(self) -> None:
        if not self.on:
            return
        base = self.mem.r32(lowmem.SoundBase) & ADDRESS_MASK
        frame = self.mem.read(base, 2 * FRAME)[0::2]
        volume = self.via.regs[vBufA] & VOLUME
        if volume != 7:
            frame = frame.translate(_VOLUMES[volume])
        for out in self.outputs:
            out.frame(frame)


class WaveFile:
    """Frames to a WAV file: 8-bit, at RATE.  The file is whole after each
    frame, so it can be played while the program runs, or after it's
    stopped."""

    def __init__(self, path: str):
        self._lock = threading.Lock()
        self._wav = wave.open(path, "wb")
        self._wav.setnchannels(1)
        self._wav.setsampwidth(1)
        self._wav.setframerate(RATE)

    def frame(self, samples: bytes) -> None:
        with self._lock:
            if self._wav is not None:
                self._wav.writeframes(samples)

    def close(self) -> None:
        with self._lock:
            if self._wav is not None:
                self._wav.close()
                self._wav = None
