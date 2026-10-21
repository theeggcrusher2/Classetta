"""The Sound Manager (macrt.m68k.sound_manager, glue_sound): its
synthesizers, its channels' queues at each tick, and SndPlay's 'snd '
resources, played through the sound buffer."""

import struct

import pytest

from macrt.m68k import dispatch, sound_manager as sm
from macrt.m68k.hardware import vBufB
from macrt.m68k.sound import FRAME, SILENCE, SILENT_FRAME, vSndEnb
from macrt.toolbox import lowmem

from guest import Guest, Processor

SILENT = bytes([SILENCE])


@pytest.fixture
def g(qapp):
    return Guest()


def command(c: int, p1: int = 0, p2: int = 0) -> bytes:
    return struct.pack(">HHI", c, p1 & 0xFFFF, p2 & 0xFFFFFFFF)


def header(samples: bytes, rate: int = sm.rate22khz, loop=(0, 0), base: int = 60) -> bytes:
    """A standard sound header, its samples after it."""
    return struct.pack(">5IBB", 0, len(samples), rate, *loop, 0, base) + samples


def channel(g, synth: int = sm.sampledSynth, callback: int = 0) -> int:
    out = g.ptr(bytes(4))
    assert g("SndNewChannel", out, synth, 0, callback) == 0
    return g.mem.r32(out)


def do(g, chan: int, c: int, p1: int = 0, p2: int = 0, no_wait: bool = False) -> int:
    return g("SndDoCommand", chan, g.ptr(command(c, p1, p2)), no_wait)


def tick(g) -> bytes:
    """A tick's frame, as the speaker would play it from the sound buffer."""
    g.machine.sound_manager.retrace(g)
    base = g.mem.r32(lowmem.SoundBase)
    return g.mem.read(base, 2 * FRAME)[0::2]


def speaker_on(g) -> bool:
    return not g.machine.via.regs[vBufB] & vSndEnb


# -- what the synthesizers play --------------------------------------------------
def test_samples_play_once_at_their_step_or_loop():
    s = sm.Samples(bytes(range(10)), 1.0)
    assert s.take(4) == (bytes(range(4)), False)
    assert s.take(10) == (bytes(range(4, 10)), True)
    assert sm.Samples(bytes(range(4)), 0.5).take(10) == (bytes([0, 0, 1, 1, 2, 2, 3, 3]), True)
    # Looped, it goes back to the loop's start each time it reaches the end.
    looped = sm.Samples(bytes(range(10)), 1.0, loop=(2, 6))
    assert looped.take(12) == (bytes([0, 1, 2, 3, 4, 5, 2, 3, 4, 5, 2, 3]), False)
    # At half the amplitude, half as far from silence.
    assert sm.Samples(bytes([0x80, 0xC0, 0x40]), 1.0, 127).take(3)[0] == bytes([0x80, 0x9F, 0x60])


def test_a_tone_repeats_its_waveform_at_its_frequency():
    tone = sm.Tone(sm.note_wave(0), sm.MAC_RATE / 64)    # a period every 64 samples
    out, ended = tone.take(128)
    assert not ended and out[:64] == out[64:]
    assert max(out) == 255 and min(out) == 1                # a sine, about silence
    # Timbre 255 is all but a square wave.
    assert sum(x in (1, 255) for x in sm.note_wave(255)) > 200
    assert sm.hz(69) == 440 and sm.hz(0x01B800) == 440      # A, as a note and in 16.8


def test_frames_are_added_about_silence():
    a, b = bytes([0x90, 0xF0, 0x10]), bytes([0x90, 0xF0, 0x10])
    assert sm.mix([a]) == a
    assert sm.mix([a, b]) == bytes([0xA0, 0xFF, 0x00])
    assert sm.mix([SILENT_FRAME, SILENT_FRAME]) == SILENT_FRAME


def test_a_snd_resource_s_synthesizer_and_commands():
    sound_cmd = sm.soundCmd | sm.dataPointerFlag
    one = struct.pack(">hh hI h", 1, 1, sm.sampledSynth, 0x80, 1) + command(sound_cmd, 0, 20)
    # A soundCmd no note follows is played too, as a buffer.
    assert sm.parse_snd(one) == (sm.sampledSynth, 0x80, [
        (sound_cmd, 0, 20), (sm.bufferCmd | sm.dataPointerFlag, 0, 20)])
    two = struct.pack(">hhh", 2, 0, 1) + command(sm.bufferCmd | sm.dataPointerFlag, 0, 14)
    assert sm.parse_snd(two) == (0, 0, [(sm.bufferCmd | sm.dataPointerFlag, 0, 14)])
    # An instrument, with notes for it, isn't.
    notes = struct.pack(">hhh", 2, 0, 2) + command(sound_cmd, 0, 22) + command(sm.noteCmd, 960, 60)
    assert len(sm.parse_snd(notes)[2]) == 2
    assert sm.parse_snd(b"snd") is None and sm.parse_snd(struct.pack(">hh", 3, 0)) is None


def test_a_sound_header_s_samples(g):
    at = g.ptr(header(bytes(range(40)), loop=(10, 30), base=72))
    assert sm.read_header(g.mem, at) == (bytes(range(40)), sm.rate22khz, (10, 30), 72)
    # An extended header's 16-bit stereo: the channels' high bytes, as offset
    # binary, averaged.
    frames = struct.pack(">4h", 0x1000, 0x3000, -0x1000, -0x3000)
    ext = struct.pack(">5IBB", 0, 2, sm.rate22khz, 0, 0, sm.extSH, 60) + struct.pack(">I", 2)
    ext += bytes(48 - len(ext)) + struct.pack(">H", 16) + bytes(14) + frames
    assert sm.read_header(g.mem, g.ptr(ext))[0] == bytes([0xA0, 0x60])
    compressed = header(bytes(8))[:20] + bytes([0xFE, 60]) + bytes(8)
    assert sm.read_header(g.mem, g.ptr(compressed)) is None


# -- channels, a tick at a time --------------------------------------------------
def test_a_buffer_plays_then_the_commands_after_it(g):
    called = []
    g.procs[0x1234] = lambda chan, cmd: called.append(struct.unpack(">HHI", g.mem.read(cmd, 8)))
    chan = channel(g, callback=0x1234)
    assert g.mem.r32(chan + sm.CALLBACK) == 0x1234 and g.mem.r16(chan + sm.QLENGTH) == 128
    samples = bytes(i * 7 % 251 + 2 for i in range(500))
    assert do(g, chan, sm.bufferCmd, 0, g.ptr(header(samples))) == 0
    assert do(g, chan, sm.callBackCmd, 1, 2) == 0
    assert not called and not speaker_on(g)

    assert tick(g) == samples[:370] and speaker_on(g) and not called
    assert tick(g) == samples[370:] + SILENT * 240
    assert called == [(sm.callBackCmd, 1, 2)]
    assert tick(g) == SILENT_FRAME and not speaker_on(g)    # done: the speaker's as it was


def test_a_buffer_plays_at_its_rate_times_rate_cmd_s(g):
    chan = channel(g)
    do(g, chan, sm.rateCmd, 0, 0x8000)                       # half as fast
    do(g, chan, sm.bufferCmd, 0, g.ptr(header(bytes(range(100, 250)), rate=2 * sm.rate22khz)))
    assert tick(g)[:150] == bytes(range(100, 250))           # at twice the rate: a sample a sample


def test_notes_last_their_duration(g):
    called = []
    g.procs[0x40] = lambda chan, cmd: called.append(len(called))
    chan = channel(g, sm.noteSynth, callback=0x40)
    do(g, chan, sm.noteCmd, 200, 69)                         # A for 100 ms: 2225 samples
    do(g, chan, sm.callBackCmd)
    frames = [tick(g) for _ in range(6)]
    assert not called
    assert all(len(set(f)) > 2 for f in frames)              # a tone throughout
    frame = tick(g)
    assert called == [0] and frame[2225 - 6 * FRAME:] == SILENT * (7 * FRAME - 2225)
    assert tick(g) == SILENT_FRAME


def test_a_wait_holds_the_commands_after_it(g):
    called = []
    g.procs[0x40] = lambda chan, cmd: called.append(1)
    chan = channel(g, callback=0x40)
    do(g, chan, sm.waitCmd, 2000)                            # a second: 60.15 ticks
    do(g, chan, sm.callBackCmd)
    for _ in range(60):
        g.machine.sound_manager.retrace(g)
    assert not called
    g.machine.sound_manager.retrace(g)
    assert called == [1]


def test_a_sampled_instrument_plays_notes_at_its_pitch(g):
    chan = channel(g)
    wave = bytes(SILENCE + (40 if i < 8 else -40) for i in range(16))
    instrument = g.ptr(header(wave, loop=(0, 16), base=60))
    do(g, chan, sm.soundCmd, 0, instrument)
    do(g, chan, sm.noteCmd, 2000, 72)                        # an octave up: twice as fast
    frame = tick(g)
    assert frame[:16] == wave[0::2] * 2


def test_a_full_queue(g):
    out = g.ptr(bytes(4))
    own = g.ptr(bytes(36 + 2 * 8))
    g.mem.w32(out, own)
    assert g("SndNewChannel", out, sm.noteSynth, 0, 0) == sm.badChannel     # no qLength
    g.mem.w16(own + sm.QLENGTH, 2)                           # room for one
    assert g("SndNewChannel", out, sm.noteSynth, 0, 0) == 0 and g.mem.r32(out) == own
    assert do(g, own, sm.waitCmd, 100) == 0
    assert do(g, own, sm.waitCmd, 100, no_wait=True) == sm.queueFull
    assert do(g, own, sm.waitCmd, 100) == 0                  # it waits its turn
    g.machine.sound_manager.retrace(g)
    assert g.mem.r16(own + sm.QHEAD) != g.mem.r16(own + sm.QTAIL)    # the second, queued
    assert g("SndDisposeChannel", own, True) == 0
    assert g.heap.ptr_size(own) == 52                        # the program's own, still
    assert do(g, own, sm.waitCmd, 100) == sm.badChannel


def test_immediate_commands_go_ahead_of_the_queue(g):
    chan = channel(g, sm.noteSynth)
    do(g, chan, sm.waitCmd, 4000)
    do(g, chan, sm.noteCmd, 4000, 60)
    assert g("SndDoImmediate", chan, g.ptr(command(sm.freqCmd, 0, 69))) == 0
    assert len(set(tick(g))) > 2                             # it plays, though the queue waits
    g("SndDoImmediate", chan, g.ptr(command(sm.quietCmd)))
    assert tick(g) == SILENT_FRAME


def test_modifiers_see_each_command_first(g):
    seen = []

    def modifier(chan, cmd, stub):
        c = struct.unpack(">HHI", g.mem.read(cmd, 8))
        seen.append(c[:2])
        if c[0] == sm.callBackCmd:                           # dropped
            g.mem.write(cmd, command(sm.nullCmd))
        return c[0] == sm.waitCmd                            # and one more after a wait

    called = []
    g.procs[0x80] = modifier
    g.procs[0x40] = lambda chan, cmd: called.append(1)
    chan = channel(g, callback=0x40)
    assert g("SndAddModifier", chan, 0x80, 0, 77) == 0
    assert seen == [(sm.initCmd, 0)]
    stub = g.mem.r32(chan + sm.FIRST_MOD)
    assert g.mem.r32(stub + sm.STUB_CODE) == 0x80
    do(g, chan, sm.callBackCmd)
    do(g, chan, sm.waitCmd, 10)
    g.machine.sound_manager.retrace(g)
    assert seen[1:] == [(sm.callBackCmd, 0), (sm.waitCmd, 10), (sm.requestNextCmd, 1)]
    assert not called
    assert g("SndDisposeChannel", chan, True) == 0
    assert seen[-1] == (sm.freeCmd, 0)


def test_the_version_and_what_is_available(g):
    assert g("SndSoundManagerVersion") == 0x02008000         # 2.0, final
    cmd = g.ptr(command(sm.availableCmd))
    assert g("SndControl", sm.sampledSynth, cmd) == 0 and g.mem.r16(cmd + 2) == 1
    g.mem.write(cmd, command(sm.availableCmd, 0, sm.initStereo))
    assert g("SndControl", sm.sampledSynth, cmd) == 0 and g.mem.r16(cmd + 2) == 0
    g.mem.write(cmd, command(sm.availableCmd))
    assert g("SndControl", 2, cmd) == 0 and g.mem.r16(cmd + 2) == 0  # no synthesizer 2


def test_a_channel_s_status(g):
    chan = channel(g, sm.noteSynth)
    status = g.ptr(b"\xff" * 24)
    assert g("SndChannelStatus", chan, 24, status) == 0
    assert g.mem.read(status, 24) == bytes(24)
    do(g, chan, sm.noteCmd, 2000, 60)
    g.machine.sound_manager.retrace(g)
    g("SndChannelStatus", chan, 24, status)
    assert g.mem.r8(status + 12) == 1                        # scChannelBusy
    assert g("SndChannelStatus", 0, 24, status) == sm.badChannel


def test_the_error_sound_s_procedure_and_a_resource_that_isn_t_a_sound(g):
    g("ErrorSound", 0x4321)
    assert g.process.world.r32(0xA9C) == 0x4321
    assert g("SndPlay", 0, g.handle(b"snd"), False) == sm.badFormat
    assert g("SndPlay", 0, 0, False) == sm.resProblem
    assert g("SndPlay", 0x1234, g.handle(b"snd"), False) == sm.badChannel


# -- SndPlay, from 68k code ------------------------------------------------------
class Player(Processor):
    """A Processor whose ticks come, while the Sound Manager is busy, as
    the runtime's do."""

    def __init__(self, resources=()):
        super().__init__(resources)
        self.cpu.sound_waiting = self.machine.sound_manager.busy
        self.frames = []

    def run(self, code: bytes, ticks: int = 600) -> None:
        start = self.ptr(code)
        self.cpu.pc = start
        for _ in range(100_000):
            ev = self.cpu.run(start + len(code))
            if ev.kind == "trap":
                self.cpu.pc = ev.pc + 2
                dispatch.dispatch(self, ev.code, ev.pc)
            elif ev.kind == "interrupted":
                self.frames.append(tick(self))
                assert len(self.frames) < ticks
            else:
                break
        assert ev.kind == "until", ev


def snd_play(chan: int, handle: int, asynch: bool = False) -> bytes:
    """SndPlay(chan, handle, asynch); its result in D0."""
    return bytes.fromhex("554f"                          # subq.w #2,sp
                         "2f3c") + struct.pack(">I", chan) + bytes.fromhex(
                         "2f3c") + struct.pack(">I", handle) + bytes.fromhex(
                         "3f3c") + struct.pack(">H", 0x100 if asynch else 0) + bytes.fromhex(
                         "a805"                          # _SndPlay
                         "301f")                         # move.w (sp)+,d0


@pytest.fixture
def p(qapp):
    player = Player()
    yield player
    player.cpu.close()


def format_2(samples: bytes) -> bytes:
    return struct.pack(">hhh", 2, 0, 1) + command(sm.bufferCmd | sm.dataPointerFlag, 0, 14) \
        + header(samples)


def test_snd_play_waits_for_its_sound_on_a_channel_of_its_own(p):
    samples = bytes(i % 200 + 20 for i in range(1000))
    h = p.handle(format_2(samples))
    p.run(snd_play(0, h))
    assert p.cpu.d(0) & 0xFFFF == 0
    heard = b"".join(p.frames)
    assert heard[:1000] == samples and len(p.frames) == 3
    assert not p.machine.sound_manager.channels                 # freed once it's done
    assert not p.heap.get_state(h) & 0x80                       # and unlocked


def test_snd_play_on_a_channel_waits_only_if_it_s_synchronous(p):
    chan = channel(p, sm.sampledSynth)
    samples = bytes(i % 200 + 20 for i in range(2000))
    h = p.handle(format_2(samples))
    p.run(snd_play(chan, h, asynch=True))
    assert not p.frames                                         # queued, and on it went
    assert p.heap.get_state(h) & 0x80                           # locked, while it plays
    p.run(snd_play(chan, h))
    assert len(p.frames) == 11                                  # both, one after the other
    assert b"".join(p.frames)[:4000] == samples * 2
    assert not p.heap.get_state(h) & 0x80                       # played: as it was
    assert p.machine.sound_manager.channel(chan) is not None    # the program's, still


def test_dispose_channel_waits_for_what_s_queued(p):
    chan = channel(p, sm.noteSynth)
    do(p, chan, sm.noteCmd, 200, 60)                            # 2225 samples: 7 ticks
    code = bytes.fromhex("554f" "2f3c") + struct.pack(">I", chan) + bytes.fromhex(
        "4267"                                                  # clr.w -(sp): quietNow FALSE
        "a801" "301f")                                          # _SndDisposeChannel
    p.run(code)
    assert len(p.frames) == 7 and not p.machine.sound_manager.channels
