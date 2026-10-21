"""The Sound Driver's synthesizers, the speaker, and where its sound goes
(macrt.m68k.sound, macrt.ui.audio).  The driver itself, called from 68k
code, is test_m68k_apps' Sounds; its immediate writes are here."""

import struct
import wave

from PyQt6.QtMultimedia import QAudioFormat

from macrt.m68k import glue_devices, sound
from macrt.m68k.hardware import HZ, vBufA, vBufB
from macrt.memory import RAM
from macrt.toolbox import lowmem
from macrt.ui.audio import Converter

from guest import Processor

FRAME = sound.FRAME


def runs(frame: bytes) -> list[int]:
    """How long each level lasts, in samples."""
    out, n = [], 1
    for a, b in zip(frame, frame[1:]):
        if a == b:
            n += 1
        else:
            out.append(n)
            n = 1
    return out + [n]


def test_a_square_wave_is_the_count_s_frequency_about_silence():
    count = 1958                                    # 783,360 / 1958: 400 Hz
    frame, phase = sound.square_wave(count, 200, 0)
    assert set(frame) == {28, 228}
    period = sound.RATE * count / HZ                # 55.5 samples
    assert all(abs(n - period / 2) <= 1 for n in runs(frame)[1:-1])
    # The next frame goes on where this one stopped.
    two = frame + sound.square_wave(count, 200, phase)[0]
    assert all(abs(n - period / 2) <= 1 for n in runs(two)[1:-1])
    assert sound.square_wave(0, 200, phase) == (sound.SILENT_FRAME, phase)
    assert set(sound.square_wave(count, 255, 0)[0]) == {0, 255}


def test_four_tones_are_added_and_divided_by_four():
    ramp = bytes(range(256))
    tones = [(0x10000, 0, ramp), (0x8000, 0, ramp), (0, 0x50000, ramp), (0, 0, sound.SILENT_WAVE)]
    frame, phases = sound.four_tone(tones)
    expect = bytes(((i & 0xFF) + (i // 2 & 0xFF) + 5 + 0x80) >> 2 for i in range(FRAME))
    assert frame == expect
    assert phases == [FRAME * 0x10000 & 0xFFFFFF, FRAME * 0x8000 & 0xFFFFFF, 0x50000, 0]


def test_a_free_form_sound_steps_through_its_bytes_then_is_silent():
    data = bytes(range(100))
    frame, position = sound.free_form(data, 0, 0, 0x8000, 100)
    assert frame[:6] == bytes([0, 0, 1, 1, 2, 2])
    assert frame[198:202] == bytes([99, 99, 0x80, 0x80])
    assert position == FRAME * 0x8000
    # From further on, with only the bytes it needs.
    frame, _ = sound.free_form(data[50:], 50, 50 << 16, 0x10000, 100)
    assert frame[:50] == data[50:] and set(frame[50:]) == {0x80}


class FakeVIA:
    def __init__(self):
        self.regs = bytearray(16)
        self.regs[vBufB] = sound.vSndEnb            # off
        self.regs[vBufA] = 7


class Frames:
    def __init__(self):
        self.frames = []

    def frame(self, samples):
        self.frames.append(samples)


def test_the_speaker_plays_the_buffer_s_high_bytes_while_it_s_on_at_its_volume():
    mem, via, out = RAM(0x10000), FakeVIA(), Frames()
    mem.w32(lowmem.SoundBase, 0x1000)
    mem.write(0x1000, bytes([0xC0, 0x11]) * FRAME)
    speaker = sound.Speaker(mem, via, [out])
    speaker.retrace()
    assert out.frames == []                         # off
    via.regs[vBufB] = 0
    speaker.retrace()
    assert out.frames == [bytes([0xC0]) * FRAME]
    via.regs[vBufA] = 0x48 | 3                      # the other bits aren't the volume
    speaker.retrace()
    assert out.frames[-1] == bytes([0x80 + 0x40 * 3 // 7]) * FRAME
    via.regs[vBufA] = 0
    speaker.retrace()
    assert out.frames[-1] == sound.SILENT_FRAME
    assert not sound.Speaker(mem, via).on           # nowhere for it to go


def test_a_wave_file_holds_each_frame(tmp_path):
    path = tmp_path / "a.wav"
    w = sound.WaveFile(str(path))
    w.frame(bytes(i & 0xFF for i in range(FRAME)))
    w.frame(sound.SILENT_FRAME)
    with wave.open(str(path)) as r:                 # whole already
        assert (r.getnframes(), r.getframerate(), r.getsampwidth()) == (2 * FRAME, 22200, 1)
    w.close()
    w.frame(sound.SILENT_FRAME)                     # after it's closed: nothing
    with wave.open(str(path)) as r:
        assert r.readframes(FRAME)[:3] == bytes([0, 1, 2])


def test_the_host_gets_the_changes_in_level_at_its_own_rate():
    fmt = QAudioFormat.SampleFormat
    c = Converter(sound.RATE, 1, fmt.Int16)
    held = struct.unpack(f"={FRAME}h", c(bytes([0xFF]) * FRAME))
    assert held[0] > 32000 and 0 < held[-1] < held[0]   # a level that holds drifts back
    assert struct.unpack("=3h", c(sound.SILENT_FRAME)[:6])[0] < 0
    # 48 kHz in stereo floats: each sample held for its time, in both channels.
    c = Converter(48000, 2, fmt.Float)
    out = c(sound.SILENT_FRAME + bytes([0xFF]) * FRAME)
    assert len(out) == 4 * 2 * 1600                 # 2 frames' worth at 48 kHz
    floats = struct.unpack(f"={2 * 1600}f", out)
    assert floats[:2 * 800] == (0.0,) * (2 * 800)
    assert floats[2 * 800] == floats[2 * 800 + 1] == floats[2 * 800 + 2] > 0.9
    assert c.align(4 * 2 * 3 + 5) == 4 * 2 * 3


def test_an_immediate_write_starts_its_sound_and_returns_at_once(qapp):
    """Apache Strike starts its engine's four tones, which last over two
    minutes, with an immediate Write, and changes them from its own VBL
    task: it isn't kept waiting for the sound to end, as a synchronous
    write's caller is."""
    p = Processor()
    machine = p.machine
    machine.sound_task = p.ptr(bytes.fromhex("4e75"))
    machine.wait_for_io = p.ptr(bytes.fromhex("4e75"))
    driver = sound.SoundDriver(machine)
    record = p.ptr(struct.pack(">H", 0x7FFF) + bytes(48))      # its duration, in ticks
    synth = p.ptr(struct.pack(">hI", sound.ftMode, record))
    try:
        for w, waits in ((0xA203, False), (0xA003, True)):     # _Write,IMMED; _Write
            pb = p.ptr(bytes(50))
            p.mem.w16(pb + glue_devices.ioRefNum, sound.SOUND_DRIVER)
            p.mem.w32(pb + sound.ioBuffer, synth)
            p.mem.w32(pb + sound.ioReqCount, 6)
            p.cpu.set_a(0, pb)
            p.cpu.set_d(0, 0xFFFF)
            p.cpu.pc = at = p.ptr(bytes(2))
            assert driver.call(p, w)
            assert p.mem.r16s(pb + glue_devices.ioResult) == 1     # playing, or queued
            assert p.cpu.pc == (machine.wait_for_io if waits else at)
            if not waits:
                assert p.cpu.d(0) == glue_devices.noErr
                assert driver.playing is not None and driver.playing.pb == pb
    finally:
        p.cpu.close()


def test_the_requests_are_in_the_dce_s_queue_and_kill_io_stops_what_completions_start(qapp):
    """The Sound Driver's requests are in the queue of its DCE, at
    SoundDCE.  Downhill Racer's StartSound glue copies the one playing
    and puts the copy in the queue's head and tail, and it's the copy
    that's done.  Its completion routine starts the next sound it kept,
    and KillIO stops that one too."""
    p = Processor()
    machine = p.machine
    machine.sound_task = p.ptr(bytes.fromhex("4e75"))
    driver = sound.SoundDriver(machine)
    driver.install(p.system.sys_heap)
    dce = p.mem.r32(lowmem.SoundDCE)
    record = p.ptr(struct.pack(">H", 0x7FFF) + bytes(48))      # its duration, in ticks
    synth = p.ptr(struct.pack(">hI", sound.ftMode, record))
    completion = p.ptr(bytes(2))
    first, copy, later, kill = (p.ptr(bytes(50)) for _ in range(4))
    called = []

    def call(pb: int, w: int) -> None:
        p.mem.w16(pb + glue_devices.ioRefNum, sound.SOUND_DRIVER)
        p.cpu.set_a(0, pb)
        assert driver.call(p, w)

    def write(pb: int) -> None:                 # an asynchronous Write
        p.mem.w32(pb + glue_devices.ioCompletion, completion)
        p.mem.w32(pb + sound.ioBuffer, synth)
        p.mem.w32(pb + sound.ioReqCount, 6)
        call(pb, 0xA403)

    def completed(addr: int, a0: int, d0: int) -> int:
        assert addr == completion
        called.append((a0, d0))
        if a0 == copy:
            write(later)
        return 0

    def queue_of(dce: int) -> tuple[int, int]:
        return p.mem.r32(dce + glue_devices.dCtlQHead), p.mem.r32(dce + glue_devices.dCtlQTail)

    p.call_with = completed
    try:
        write(first)
        assert queue_of(dce) == (first, first)
        assert p.mem.r16(first + glue_devices.qType) == glue_devices.ioQType
        p.mem.write(copy, p.mem.read(first, 50))
        p.mem.w32(dce + glue_devices.dCtlQHead, copy)
        p.mem.w32(dce + glue_devices.dCtlQTail, copy)
        call(kill, 0xA006)                      # _KillIO
        aborted = glue_devices.abortErr & 0xFFFFFFFF
        assert called == [(copy, aborted), (later, aborted)]
        assert [p.mem.r16s(pb + glue_devices.ioResult) for pb in (first, copy, later, kill)] \
            == [1, glue_devices.abortErr, glue_devices.abortErr, glue_devices.noErr]
        assert queue_of(dce) == (0, 0)
        assert driver.playing is None and not driver.queue
    finally:
        p.cpu.close()
