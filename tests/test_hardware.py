"""What programs from the Mac's first years reach past the Toolbox for:
the VIA, the 512K Mac's screen where it was, and the Event Manager's
low-memory record of the mouse and keyboard."""

import time
from types import SimpleNamespace

import pytest
from PyQt6.QtCore import QEvent, QPoint, Qt
from PyQt6.QtGui import QKeyEvent
from PyQt6.QtTest import QTest

from macrt.m68k import hardware
from macrt.m68k.cpu import CPU
from macrt.m68k.hardware import (CA0_ON, CA1, CA1_ON, CA2, CA2_ON, IWM, IWM_BASE, LSTRB,
                                 MOTOR_ON, Q6_HIGH, Q6_LOW, Q7_HIGH, SR, T1, T2, VIA, VIA_BASE,
                                 map_test_rom, vACR, vBufA, vBufB, vDirA, vDirB, vIER, vIFR,
                                 vPage2, vSW, vT1C, vT1CH, vT1L, vT1LH, vT2C, vT2CH)
from macrt.m68k.runtime import machine_for
from macrt.memory import RAM
from macrt.toolbox import lowmem
from macrt.toolbox.events import InputGlobals, tick_count
from macrt.toolbox.system import (CLASSIC_ALTERNATE, CLASSIC_SCREEN, PLUS_SCREENS,
                                  PLUS_SCREENS_END, System)
from macrt.toolbox.types import Point
from macrt.ui.keymap import key_message

CODE = 0x10000
READ_VBUFB = bytes.fromhex("1039 00efe1fe"     # move.b $EFE1FE,d0
                           "4e71")             # nop


def wait_for_tick():
    t = tick_count()
    while tick_count() == t:
        time.sleep(0.002)


# -- the VIA -------------------------------------------------------------------
def test_vbufb_has_the_mouse_button_in_vsw():
    down = [False]
    via = VIA(button=lambda: down[0])
    assert via.read(vBufB) & vSW
    down[0] = True
    assert not via.read(vBufB) & vSW


def test_the_vertical_blanking_flag_is_set_each_tick_until_cleared():
    via = VIA()
    via.write(vIFR, 0x7F)
    assert not via.read(vIFR) & CA1
    wait_for_tick()
    assert via.read(vIFR) & CA1 == 0x02
    via.write(vIFR, CA2)                # the one-second flag's: not the tick's
    assert via.read(vIFR) & CA1
    via.write(vIFR, CA1)                # writing a 1 clears it
    assert not via.read(vIFR) & CA1


def test_vier_sets_and_clears_by_bit_7():
    via = VIA()
    assert via.read(vIER) & 0x7F == CA1 | CA2 | SR     # as the ROM leaves it
    via.write(vIER, 0x7F)
    via.write(vIER, 0x80 | CA1 | T2)
    assert via.read(vIER) & 0x7F == CA1 | T2
    via.write(vIER, T2)                 # bit 7 clear: the 1s are cleared
    assert via.read(vIER) & 0x7F == CA1


def test_what_interrupts_is_what_is_flagged_and_enabled():
    via = VIA()
    via.write(vIER, 0x7F)
    via.write(vIER, 0x80 | CA1)
    via.write(vIFR, 0x7F)
    wait_for_tick()
    assert via.pending() == CA1
    via.write(vIER, CA1)
    assert via.pending() == 0 and via.read(vIFR) & CA1


def test_timer_2_runs_out():
    via = VIA()
    via.write(vT2C, 0xFF)
    via.write(vT2CH, 0x0F)              # 4095 counts: about 5 ms
    assert not via.read(vIFR) & T2
    time.sleep(0.02)
    assert via.read(vIFR) & T2
    via.read(vT2C)                      # reading the low byte clears it
    assert not via.read(vIFR) & T2


class StoppedClock:
    """The machine's time, standing still until a test moves it on."""

    def __init__(self):
        self.t = 100.0

    def now(self) -> float:
        return self.t


def timer_1_free(enabled: bool) -> tuple[VIA, StoppedClock]:
    clock = StoppedClock()
    via = VIA(clock=clock)
    via.write(vIER, 0x7F)
    if enabled:
        via.write(vIER, 0x80 | T1)
    via.write(vACR, 0x40)               # running free
    via.write(vT1C, 0xE6)
    via.write(vT1CH, 0x03)              # 998 counts: 1000 each time round
    return via, clock


def flagged(via: VIA) -> int:
    """How many times timer 1's flag is set, clearing it each time."""
    times = 0
    while via.read(vIFR) & T1 and times < 100:
        via.read(vT1C)                  # reading the low byte clears it
        times += 1
    return times


@pytest.mark.parametrize("enabled, times", [(True, 3), (False, 1)])
def test_timer_1_running_free_flags_each_time_round_its_interrupt_was_late_for(enabled, times):
    """Master Tracks Pro counts timer 1's interrupts as its tempo's clock,
    and macrt may come to them late: with the interrupt enabled, each time
    round that went by is flagged in turn.  A program only looking at the
    flag lets those that went by go."""
    via, clock = timer_1_free(enabled)
    clock.t += 3.5 * 1000 / hardware.HZ
    assert flagged(via) == times
    clock.t += 2 * hardware.CATCH_UP    # too far behind to catch up
    assert flagged(via) == 1


def test_timer_1_running_free_goes_round_again_from_its_latches():
    """As Master Tracks Pro changes its tempo, without starting the timer
    again: the next time round counts what's in the latches then."""
    via, clock = timer_1_free(True)
    clock.t += 1.5 * 1000 / hardware.HZ
    via.write(vT1L, 0xCE)
    via.write(vT1LH, 0x07)              # 1998 counts: 2000 each time round
    assert flagged(via) == 1            # the time round gone by
    clock.t += 1000 / hardware.HZ
    assert flagged(via) == 0            # 1500 counts into the next
    clock.t += 600 / hardware.HZ
    assert flagged(via) == 1


def test_vbufa_bit_6_chooses_the_screen_buffer_shown():
    via, shown = VIA(), []
    via.on_page = shown.append
    assert via.read(vBufA) & vPage2                     # the main buffer, as the ROM leaves it
    for value in (0x6F, 0x2F, 0x2F, 0x6F, 0x2F):        # Dark Castle flipping
        via.write(vBufA, value)
    via.main_page()                                     # as an application quits
    assert shown == [True, False, True, False]
    assert via.read(vBufA) & vPage2


def test_vbufa_bit_7_is_high_as_no_serial_data_waits():
    """The SCC's wait/request for channel A, which Excel 1.0 watches as it
    reads its key disk."""
    via = VIA()
    via.write(vBufA, 0x2F)
    assert via.read(vBufA) == 0xAF
    via.write(vDirA, 0xFF)                  # an output, if a program makes it one
    assert via.read(vBufA) == 0x2F


def test_68k_code_reads_the_via_where_low_memory_says():
    mem = RAM(0x20000)
    mem.write(CODE, READ_VBUFB)
    cpu = CPU(mem)
    down = [True]
    via = VIA(button=lambda: down[0])
    via.map(cpu.uc)
    via.write(vDirB, 0)                 # all inputs
    try:
        for is_down in (True, False):
            down[0] = is_down
            cpu.pc = CODE
            assert cpu.run(CODE + 6).kind == "until"
            assert bool(cpu.d(0) & vSW) is not is_down
    finally:
        cpu.close()


# -- the IWM -------------------------------------------------------------------
def test_the_iwm_reads_the_bytes_under_the_head_its_status_and_its_handshake():
    iwm = IWM()
    assert iwm.read(Q6_LOW) == 0            # no disk turning
    iwm.put(b"\xd5\xaa\xad")
    assert [iwm.read(Q6_LOW) for _ in range(4)] == [0xD5, 0xAA, 0xAD, 0xD5]    # and round
    assert iwm.read(Q6_HIGH) == 0x1F        # the status register: the motor off
    iwm.read(MOTOR_ON)
    assert iwm.read(Q6_HIGH) == 0x3F
    assert iwm.read(Q7_HIGH) == 0xFF        # the handshake register: ready
    iwm.read(Q7_HIGH - 1)
    assert iwm.read(Q6_LOW) == 0xAA         # the data register again, where it was


def test_the_disk_driver_looks_ahead_on_the_track_and_turns_on_past_it():
    iwm = IWM()
    assert iwm.ahead(4) == b""
    iwm.put(b"\xd5\xaa\xad")
    iwm.read(Q6_LOW)
    assert iwm.ahead(5) == b"\xaa\xad\xd5\xaa\xad"     # round, and none read
    iwm.skip(4)
    assert iwm.read(Q6_LOW) == 0xAD


def test_once_the_head_steps_the_iwm_reads_the_track_from_where_the_disk_has_turned_to(
        monkeypatch):
    """PageMaker's key-track check: step a track in, wait, and count the
    bytes from there."""
    clock = [0.0]
    monkeypatch.setattr(hardware, "time", SimpleNamespace(monotonic=lambda: clock[0]))
    iwm = IWM()
    # Each track has 100 bytes of 8 bits round, numbered from 100 times the track's.
    iwm.surface = lambda drive, track, side: (
        bytes(100 * track + n % 100 for n in range(200)), [8 * n for n in range(200)], 800)
    iwm.select(1)
    assert iwm.read(Q6_LOW) == 0            # track 0's first byte, once it has come by
    turn = iwm.drive.turn_time()
    clock[0] = turn * 0.245                 # and nearly a quarter turn on
    for switch in (CA0_ON - 1, CA1_ON - 1, CA2_ON - 1, LSTRB, LSTRB - 1, CA0_ON, LSTRB, LSTRB - 1):
        iwm.touch(switch)                   # inward, and a step
    assert iwm.drive.track == 1
    assert iwm.read(Q6_LOW) == 126          # the next byte to begin, at 0.26 turn
    clock[0] += hardware.IDLE / 2           # macrt taking its time over the instructions between
    assert iwm.read(Q6_LOW) == 127          # each byte as it comes by
    clock[0] += turn * 0.505                # half a turn unread, and half a byte
    assert iwm.read(Q6_LOW) == 179
    iwm.surface = lambda drive, track, side: (b"", [], 0)
    iwm.touch(LSTRB)
    iwm.touch(LSTRB - 1)
    assert iwm.drive.track == 2 and iwm.read(Q6_LOW) == 0    # a track with nothing on it
    iwm.put(b"\xd5")                        # the disk driver's own: no turning on
    clock[0] += turn
    assert iwm.read(Q6_LOW) == 0xD5


def test_the_iwms_status_has_the_chosen_drive_registers_sense_in_its_top_bit(monkeypatch):
    """Moebius's copy protection senses the drive's registers through the
    IWM's status, as the disk driver does: which way the head steps, that
    a step is over, the motor, the head at track 0 and the tachometer's
    60 pulses a turn.  A strobe with CA1 high and CA0 low turns the motor
    off with CA2 high and on with it low."""
    clock = [0.0]
    monkeypatch.setattr(hardware, "time", SimpleNamespace(monotonic=lambda: clock[0]))
    sel = [False]
    iwm = IWM(head_select=lambda: sel[0])
    iwm.select(1)

    def sense(ca2, ca0, ca1, head=False):
        sel[0] = head
        for switch, on in ((CA2_ON, ca2), (CA0_ON, ca0), (CA1_ON, ca1)):
            iwm.touch(switch if on else switch - 1)
        return iwm.read(Q6_HIGH) >> 7

    def strobe(ca2, ca0, ca1):
        sense(ca2, ca0, ca1)
        iwm.touch(LSTRB)
        iwm.touch(LSTRB - 1)

    assert sense(0, 0, 0) == 0              # DIRTN: inward
    strobe(1, 0, 0)
    assert sense(0, 0, 0) == 1              # outward
    assert sense(0, 1, 0) == 1              # STEP: no step going on
    assert sense(0, 0, 1) == 0              # MOTORON: on
    strobe(1, 0, 1)
    assert sense(0, 0, 1) == 1              # off
    assert iwm.read(Q6_HIGH) & 0x20         # the IWM's own motor-on, which the driver set
    strobe(0, 0, 1)
    assert sense(0, 0, 1) == 0
    assert sense(0, 0, 1, head=True) == 0   # TK0: at track 0
    strobe(0, 0, 0)
    strobe(0, 1, 0)                         # a step in
    assert iwm.drive.track == 1 and sense(0, 0, 1, head=True) == 1
    turn = iwm.drive.turn_time()
    pulses = []
    for n in range(480):
        clock[0] = turn * (n + 0.5) / 480
        pulses.append(sense(0, 1, 1, head=True))     # TACH
    assert sum(a < b for a, b in zip(pulses, pulses[1:])) == 60


def test_working_out_a_tracks_bits_doesnt_turn_the_disk(monkeypatch):
    """PageMaker counts the bytes from where the disk has turned to when
    the head reaches its key track: the host's time macrt takes to work
    out that track's bits the first time isn't the disk's."""
    host = [5.0]
    monkeypatch.setattr(hardware, "time", SimpleNamespace(monotonic=lambda: host[0]))
    clock = hardware.Clock()
    via, iwm = VIA(clock=clock), IWM(clock=clock)
    turn = iwm.drive.turn_time()

    def surface(drive, track, side):
        host[0] += turn * 0.3
        return bytes(100 * track + n % 100 for n in range(200)), [8 * n for n in range(200)], 800
    iwm.surface = surface
    iwm.select(1)

    def timer():
        return via.read(vT1CH) << 8 | via.read(vT1C)
    via.write(vT1C, 0xFF)
    via.write(vT1CH, 0xFF)
    before = timer()
    assert iwm.read(Q6_LOW) == 0            # the first byte, as when the head got there
    byte = turn / 100 * hardware.HZ         # and the VIA's timer counted it coming by, no more
    assert round((before - timer()) / byte) == 1


def test_the_via_times_the_bytes_as_they_come_by(monkeypatch):
    """Jazz's copy protection: the sync bytes on its key track are 10 bits
    or 9, which the VIA's timer 1 tells apart.  However long macrt takes
    over the instructions between, the clock stands still while a program
    keeps reading, but for the bytes it waits for."""
    host = [5.0]
    monkeypatch.setattr(hardware, "time", SimpleNamespace(monotonic=lambda: host[0]))
    clock = hardware.Clock()
    via, iwm = VIA(clock=clock), IWM(clock=clock)
    lengths = [10, 10, 9, 9, 8, 8] * 10     # bits each byte is, 600 round
    starts = [sum(lengths[:n]) for n in range(len(lengths))]
    iwm.surface = lambda drive, track, side: (
        bytes(range(120)), starts + [600 + n for n in starts], 600)
    iwm.select(1)

    def timer():
        return via.read(vT1CH) << 8 | via.read(vT1C)
    iwm.read(Q6_LOW)
    via.write(vT1C, 0xFF)
    via.write(vT1CH, 0xFF)                  # count down from $FFFF
    gone = []
    for n in range(6):
        before = timer()
        host[0] += 0.001                    # the host's time, which doesn't count
        iwm.read(Q6_LOW)
        gone.append(before - timer())
    ticks = iwm.drive.turn_time() / 600 * hardware.HZ
    assert [round(g / ticks) for g in gone] == [10, 10, 9, 9, 8, 8]
    still = clock.now()
    host[0] += 0.01                         # the program stops reading: the clock goes on
    assert clock.now() == pytest.approx(still + 0.01)


def test_while_the_clock_counts_it_has_only_what_its_told(monkeypatch):
    """While a program's own routine reads a sector, the time is what its
    instructions would take a Mac, and what it waits for, not the
    host's."""
    host = [5.0]
    monkeypatch.setattr(hardware, "time", SimpleNamespace(monotonic=lambda: host[0]))
    clock = hardware.Clock()
    with clock.counting():
        start = clock.now()
        host[0] += 1                        # macrt's time over the instructions
        assert clock.now() == start
        clock.counted += 0.001              # what they would take a Mac
        clock.wait(start + 0.0005)          # past already
        clock.sleep(0.002)                  # and no host's sleep
        assert clock.now() == pytest.approx(start + 0.003)
    assert clock.now() == pytest.approx(start + 0.003)     # on from there
    host[0] += 1
    assert clock.now() == pytest.approx(start + 1.003)


# D5, AA and 96, and round, with a zero between the first two
TRACK = "00" "11010101" "0" "10101010" "10010110" "0000000"


def test_the_iwm_shifts_a_byte_in_bit_by_bit_and_holds_it_until_its_read():
    bit = 2e-6
    shifting = hardware.Shifting(TRACK, 0, 0.0, bit)
    assert shifting.read(9.9 * bit) == 0           # the D5's last bit still to come
    assert shifting.read(10 * bit) == 0xD5
    assert shifting.read(10.5 * bit) == 0          # cleared as it's read
    assert shifting.read(19 * bit) == 0xAA         # the zero before it passed over
    assert [(byte, round(when / bit)) for byte, when in shifting.ahead(2)] == [
        (0x96, 27), (0xD5, 44)]                     # and round


def test_with_q6_high_the_iwm_misses_the_bits_going_by_and_frames_the_bytes_from_those_after():
    """Shufflepuck Cafe's copy protection: it sets Q6 high part way
    through a byte, waits, and reads on, and the bytes it reads are its
    key only if the bits missed meanwhile are as many as a Mac's would
    be."""
    bit = 2e-6
    shifting = hardware.Shifting(TRACK, 0, 0.0, bit)
    assert shifting.read(10 * bit) == 0xD5
    shifting.shift(False, 12.5 * bit)              # the AA's first two bits shifted in, 01
    shifting.shift(True, 15.5 * bit)               # and three missed
    assert shifting.read(22 * bit) == 0b1_1010100  # its 1, and seven more after those
    assert shifting.read(30 * bit) == 0xB0         # and on from there


def test_the_bytes_latched_wait_to_be_read_but_not_for_long():
    """macrt's count of the time a routine's instructions take is near
    enough, not exact: a byte latched before the last is read waits for
    it, but not once it has waited longer than IDLE."""
    bit = hardware.IDLE / 10
    shifting = hardware.Shifting(TRACK, 0, 0.0, bit)
    assert shifting.read(19.5 * bit) == 0xD5       # latched 0.95 IDLE ago, and AA since
    assert shifting.read(19.6 * bit) == 0xAA
    assert shifting.read(53.5 * bit) == 0xAA       # 96, D5 and AA since: the last
    assert shifting.read(53.6 * bit) == 0


def test_while_the_clock_counts_the_iwm_shifts_the_disks_bits_in_from_where_its_turned_to(
        monkeypatch):
    host = [5.0]
    monkeypatch.setattr(hardware, "time", SimpleNamespace(monotonic=lambda: host[0]))
    clock = hardware.Clock()
    iwm = IWM(clock=clock)
    iwm.bits = lambda drive, track, side: TRACK
    iwm.select(1)
    bit = iwm.drive.turn_time() / len(TRACK)
    with clock.counting():
        iwm.follow((b"", [], 0), 0, clock.now())
        host[0] += 1                        # the host's time doesn't count
        assert iwm.read(Q6_LOW) == 0
        clock.counted += 10 * bit
        assert iwm.read(Q6_LOW) == 0xD5
        clock.counted += 2.5 * bit
        assert iwm.read(Q6_HIGH) == 0x3F    # the status, and no shifting
        clock.counted += 3 * bit
        iwm.touch(Q6_LOW)
        clock.counted += 6.5 * bit
        assert iwm.read(Q6_LOW) == 0xD4
        assert iwm.ahead(2) == b"\xb0\xd5"
        start = clock.now()
        iwm.skip(2)                         # the disk driver's, waiting for them
        assert (clock.now() - start) / bit == pytest.approx(22)
        assert iwm.read(Q6_LOW) == 0
        clock.counted += 9 * bit
        assert iwm.read(Q6_LOW) == 0xAA
    iwm.put(b"\xd5")                       # the disk driver's own again
    assert iwm.read(Q6_LOW) == 0xD5


def test_68k_code_reads_the_iwm_on_the_low_byte_of_the_bus():
    mem = RAM(0x20000)
    data = IWM_BASE + Q6_LOW * 512
    mem.write(CODE, bytes.fromhex(f"207c {data:08x}"             # movea.l #q6L,a0
                                  "1010"                        # move.b (a0),d0
                                  "1210"                        # move.b (a0),d1
                                  "3428 ffff"                   # move.w -1(a0),d2
                                  "4e71"))                      # nop
    cpu = CPU(mem)
    iwm = IWM()
    iwm.map(cpu.uc)
    iwm.put(b"\xd5\xaa\x96")
    try:
        cpu.pc = CODE
        assert cpu.run(CODE + 14).kind == "until"
        assert (cpu.d(0) & 0xFF, cpu.d(1) & 0xFF, cpu.d(2) & 0xFFFF) == (0xD5, 0xAA, 0xFF96)
    finally:
        cpu.close()


def test_68k_code_sets_the_iwms_mode_and_reads_it_back_in_the_status():
    """The Print Shop's copy protection: with the motor off and Q6 high
    it writes the mode, the IWM's clock 7 MHz, at Q7 high, and reads it
    back in the status register.  With the motor on, what's written is
    for the disk."""
    mem = RAM(0x20000)
    mem.write(CODE, bytes.fromhex(f"207c {IWM_BASE:08x}"     # movea.l #iwm,a0
                                  "4a28 1000"               # tst.b $1000(a0): the motor off
                                  "4a28 1a00"               # tst.b $1a00(a0): Q6 high
                                  "117c 000f 1e00"          # move.b #$0F,$1E00(a0): Q7 high
                                  "1028 1c00"               # move.b $1C00(a0),d0: Q7 low
                                  "4a28 1200"               # tst.b $1200(a0): the motor on
                                  "117c 001f 1e00"          # move.b #$1F,$1E00(a0)
                                  "1228 1c00"               # move.b $1C00(a0),d1
                                  "4e71"))                  # nop
    cpu = CPU(mem)
    iwm = IWM()
    iwm.map(cpu.uc)
    try:
        cpu.pc = CODE
        assert cpu.run(CODE + 38).kind == "until"
        assert (cpu.d(0) & 0xFF, cpu.d(1) & 0xFF) == (0x0F, 0x2F)
    finally:
        cpu.close()


def test_there_is_no_test_rom_and_no_bus_error_looking_for_one():
    """Dark Castle looks for a test ROM at $F80030, as a Mac Plus's ROM
    does at $F80000."""
    mem = RAM(0x20000)
    mem.write(CODE, bytes.fromhex("207c 00f80030"     # movea.l #$F80030,a0
                                  "2010"              # move.l (a0),d0
                                  "2081"              # move.l d1,(a0)
                                  "2410"              # move.l (a0),d2
                                  "4e71"))            # nop
    cpu = CPU(mem)
    map_test_rom(cpu.uc)
    try:
        for n in range(3):
            cpu.set_d(n, 0xDEADFACE)
        cpu.pc = CODE
        assert cpu.run(CODE + 12).kind == "until"
        assert cpu.d(0) == 0 and cpu.d(2) == 0
    finally:
        cpu.close()


def test_the_rest_of_the_roms_space_reads_0(qapp):
    """Downhill Racer keeps ROMBase's high word in $E4 when it has no handle
    there, and reads through it: a Mac II's ROM space is a megabyte."""
    system = System((512, 342))
    m = machine_for(system)
    try:
        mem = system.mem
        code = 0x100000
        mem.w16(0xE4, mem.r16(lowmem.ROMBase))
        mem.w16(0xE6, 0x404C)
        mem.write(code, bytes.fromhex("2078 00e4"        # movea.l $E4.w,a0
                                      "2010"             # move.l (a0),d0
                                      "2081"             # move.l d1,(a0)
                                      "2410"             # move.l (a0),d2
                                      "4e71"))           # nop
        for n in range(3):
            m.cpu.set_d(n, 0xDEADFACE)
        m.cpu.pc = code
        assert m.cpu.run(code + 10).kind == "until"
        assert m.cpu.a(0) == mem.r32(lowmem.ROMBase) + 0x404C
        assert m.cpu.d(0) == 0 and m.cpu.d(2) == 0
    finally:
        m.cpu.close()


def test_low_memory_says_where_the_via_and_the_iwm_are(qapp):
    system = System()
    m = machine_for(system)
    try:
        assert system.mem.r32(lowmem.VIA) == VIA_BASE
        assert system.mem.r32(lowmem.IWM) == IWM_BASE
    finally:
        m.cpu.close()


# -- the screen where a Mac 512K had it ----------------------------------------
def test_a_512x342_screen_is_where_a_mac_512k_has_it(qapp):
    system = System((512, 342))
    bits = system.screen_bits
    assert bits.base == CLASSIC_SCREEN == system.mem.r32(lowmem.ScrnBase)
    assert bits.row_bytes == 64
    assert system.sys_heap.end <= CLASSIC_ALTERNATE     # clear of both screens
    p = system.open_partition("Game")
    assert p.start >= CLASSIC_SCREEN + 64 * 342 or p.end <= CLASSIC_ALTERNATE


def test_a_mac_pluss_screens_at_the_top_of_4mb_are_the_classic_ones(qapp):
    """A Plus's RAM repeats through its 4 MB of addresses, so its screen is
    at $3FA700 and its alternate at $3F2700 whatever RAM it has: Apache
    Strike draws its flight there.  No partition is made under them."""
    system = System((512, 342))
    mem = system.mem
    mem.w32(0x3FA700, 0x12345678)
    mem.w32(0x3F2700, 0x9ABCDEF0)
    assert mem.r32(CLASSIC_SCREEN) == 0x12345678
    assert mem.r32(CLASSIC_ALTERNATE) == 0x9ABCDEF0
    parts = [system.open_partition(f"Game {n}", 1 << 20) for n in range(6)]
    assert all(p.end <= PLUS_SCREENS or p.start >= PLUS_SCREENS_END for p in parts)
    assert system.largest_free() == 1 << 20         # the last megabyte, at the top


def test_the_vbl_queue_holds_a_task_of_the_systems_own(qapp):
    """As a Mac's does, the disk driver's: Prince of Persia looks for the
    task before the one it installs, to move its own to the front.  The
    System's never comes due."""
    system = System()
    p = system.open_partition("Game")
    queue = p.world.read(lowmem.VBLQueue, 10)
    head, tail = int.from_bytes(queue[2:6], "big"), int.from_bytes(queue[6:], "big")
    assert head == tail == p.vbl_task and system.sys_heap.contains(head)
    mem = system.mem
    assert mem.r32(head) == 0 and mem.r16(head + 4) == 1        # the last; vType
    assert mem.r16(head + 10) == 0                              # vblCount
    assert mem.r16(mem.r32(head + 6)) == 0x4E75                 # RTS, were it called
    q = system.open_partition("Other")
    assert q.vbl_task != p.vbl_task                             # each queue has its own
    system.close_partition(p)
    assert q.world.read(lowmem.VBLQueue + 2, 4) == q.vbl_task.to_bytes(4, "big")


def test_the_rom_is_above_ram_and_has_the_vectors_handlers(qapp):
    """Flight Simulator looks for the System's handlers in the exception
    vectors above the screen, as they are on a Mac, in its ROM."""
    system = System((512, 342))
    m = machine_for(system)
    try:
        mem = system.mem
        rom = mem.r32(lowmem.ROMBase)
        assert rom >= mem.r32(lowmem.MemTop) and mem.r16(rom + 8) == 0x0075
        assert mem.r32(4 * 5) == m.bomb_stub and rom <= m.bomb_stub < rom + 0x1000
        assert mem.r32(4 * 25) == m.level_1 > mem.r32(lowmem.ScrnBase)
    finally:
        m.cpu.close()


def test_the_a_line_vector_is_the_roms_dispatcher(qapp):
    """Lotus Jazz looks in the ROM for the MOVE.L A0,$28 that puts the
    A-line dispatcher in its vector, and takes the address from the LEA
    before it, the 68020's or (6 bytes further back) the 68000's."""
    system = System((512, 342))
    m = machine_for(system)
    try:
        mem = system.mem
        rom = mem.r32(lowmem.ROMBase)
        assert mem.r32(0x28) == m.a_line and rom <= m.a_line < rom + 0x1000
        image = mem.read(rom, 0x1000)
        at = rom + image.index(bytes.fromhex("21C80028"))
        for lea in (at - 4, at - 10):
            assert mem.r16(lea) == 0x41FA                       # LEA d16(PC),A0
            assert lea + 2 + mem.r16s(lea + 2) == m.a_line
    finally:
        m.cpu.close()


def test_low_memory_has_the_roms_vectors_to_its_routines(qapp):
    """The Alarm Clock shields the cursor through JShieldCursor and Key
    Caps clears Key1Trans's dead-key flag, a word into its routine."""
    system = System((512, 342))
    m = machine_for(system)
    try:
        mem = system.mem
        rom = mem.r32(lowmem.ROMBase)
        vectors = [mem.r32(v) for v in (lowmem.JHideCursor, lowmem.JShowCursor,
                                        lowmem.JInitCrsr, lowmem.JCrsrObscure,
                                        lowmem.JShieldCursor)]
        assert vectors == [m.cursor_vectors + 4 * i for i in range(5)]
        assert all(rom <= v < rom + 0x1000 for v in vectors)
        assert mem.r16(mem.r32(lowmem.Key1Trans) + 2) == 0xFFFF
        assert mem.r8(lowmem.AlarmState) & 0x80             # the Apple icon isn't turned over
    finally:
        m.cpu.close()


# The ROM's handler for the VIA's interrupts, as The Chessmaster 2000 has
# its own copy: it calls the routine in Lvl1DT for the lowest flag set.
LEVEL_1_DISPATCH = bytes.fromhex("41f8 018e"       # lea Lvl1DT-4,a0
                                 "2278 01d4"       # movea.l VIA,a1
                                 "1029 1a00"       # move.b vIFR(a1),d0
                                 "c029 1c00"       # and.b vIER(a1),d0
                                 "08c0 0007"       # bset #7,d0
                                 "5888"            # 1: addq.l #4,a0
                                 "e208"            # lsr.b #1,d0
                                 "64fa"            # bcc.s 1b
                                 "2050"            # movea.l (a0),a0
                                 "4e90"            # jsr (a0)
                                 "4e71")           # nop


def test_lvl1dt_has_a_routine_in_the_rom_for_each_of_the_vias_flags(qapp):
    """The Chessmaster 2000's own interrupt handler calls the ROM's
    routine for the lowest flag set, which clears it; with none set, the
    last, which clears nothing.  Here the two timers' have run out."""
    system = System((512, 342))
    m = machine_for(system)
    try:
        mem, cpu, via = system.mem, m.cpu, m.via
        rom = mem.r32(lowmem.ROMBase)
        assert all(rom <= mem.r32(lowmem.Lvl1DT + 4 * n) < rom + 0x1000 for n in range(8))
        code = system.sys_heap.new_ptr(len(LEVEL_1_DISPATCH))
        mem.write(code, LEVEL_1_DISPATCH)
        cpu.sp = system.sys_heap.new_ptr(0x100) + 0x100
        via.write(vIER, 0x7F)
        via.write(vIER, 0x80 | T1 | T2)
        for low, high in ((vT1C, vT1CH), (vT2C, vT2CH)):
            via.write(low, 0x10)
            via.write(high, 0)
        time.sleep(0.01)
        cleared = []
        for _ in range(3):
            before = via.read(vIFR)
            cpu.pc = code
            while cpu.run(code + len(LEVEL_1_DISPATCH) - 2).kind != "until":
                pass
            cleared.append(before & ~via.read(vIFR) & (T1 | T2))
        assert cleared == [T2, T1, 0]
    finally:
        m.cpu.close()


def test_other_screens_are_at_the_top_of_ram(qapp):
    system = System((640, 480))
    bits = system.screen_bits
    assert bits.base + bits.row_bytes * 480 <= system.mem.size
    assert bits.base > CLASSIC_SCREEN


# -- the mouse and keyboard in low memory --------------------------------------
def test_keylast_is_the_last_key_down_until_it_comes_up():
    mem = RAM(0x2000)
    keys = InputGlobals(mem)
    keys.key(0x2249, True)              # I
    assert mem.r16(lowmem.KeyLast) == 0x2249
    assert abs(mem.r32(lowmem.KeyTime) - tick_count()) <= 1
    assert mem.r8(lowmem.KeyMap + 4) & 0x04                 # key code $22
    keys.key(0x2649, True)              # J, with I still down
    keys.key(0x2249, False)
    assert mem.r16(lowmem.KeyLast) == 0x2649                # still J's
    assert not mem.r8(lowmem.KeyMap + 4) & 0x04
    keys.key(0x2649, False)
    assert mem.r16(lowmem.KeyLast) == 0
    keys.key(0x461C, True)              # a Mac Plus's Left arrow is ADB's $7B too
    assert mem.r8(lowmem.KeyMap + 8) & 0x40 and mem.r8(lowmem.KeyMap + 15) & 0x08
    keys.key(0x461C, False)
    keys.key(0x462B, True)              # but not its keypad's +
    assert not mem.r8(lowmem.KeyMap + 15)


def test_arrows_and_the_keypad_have_the_keyboards_own_codes(qapp):
    """The Duel steers by a Mac Plus keyboard's arrow keys or the keypad's
    8, 2, 4 and 6, looking for their codes in the KeyMap.  An ADB
    keyboard's arrows have codes of their own; the Plus's keypad sends its
    arrows' codes for +, *, / and = (IM IV-250)."""
    K, pad = Qt.Key, Qt.KeyboardModifier.KeypadModifier

    def code(key, text="", mods=Qt.KeyboardModifier.NoModifier, keyboard=3):
        return key_message(QKeyEvent(QEvent.Type.KeyPress, key, mods, text), keyboard) >> 8
    arrows = (K.Key_Left, K.Key_Right, K.Key_Up, K.Key_Down)
    assert [code(a) for a in arrows] == [0x46, 0x42, 0x4D, 0x48]
    assert [code(a, keyboard=5) for a in arrows] == [0x7B, 0x7C, 0x7E, 0x7D]
    assert (code(K.Key_8, "8", pad), code(K.Key_8, "8")) == (0x5B, 0x1C)
    assert (code(K.Key_Period, ".", pad), code(K.Key_Minus, "-", pad)) == (0x41, 0x4E)
    assert (code(K.Key_Plus, "+", pad), code(K.Key_Plus, "+", pad, 5)) == (0x46, 0x45)


def test_the_mouse_and_its_button_are_in_low_memory():
    mem = RAM(0x2000)
    mouse = InputGlobals(mem)
    assert mem.r8(lowmem.MBState) == 0x80
    mouse.mouse(Point(100, 200), True)
    assert mem.r8(lowmem.MBState) == 0
    for addr in (lowmem.Mouse, lowmem.MTemp, lowmem.RawMouse):
        assert (mem.r16(addr), mem.r16(addr + 2)) == (100, 200)


def test_mtemp_counts_the_mouses_movement_up():
    """Flight Simulator clears MTemp and takes what it grows by for how far
    the mouse has moved."""
    mem = RAM(0x2000)
    mouse = InputGlobals(mem)
    mouse.mouse(Point(100, 200), False)
    mem.w32(lowmem.MTemp, 0)
    mouse.mouse(Point(90, 203), False)
    mouse.mouse(Point(88, 210), False)
    assert (mem.r16s(lowmem.MTemp), mem.r16s(lowmem.MTemp + 2)) == (-12, 10)
    assert (mem.r16(lowmem.Mouse), mem.r16(lowmem.Mouse + 2)) == (88, 210)


def test_a_program_moves_the_mouse_by_rawmouse():
    """Crystal Quest hides the cursor, puts the mouse back in the middle of
    the screen each frame and steers by how far it's got from there."""
    mem = RAM(0x2000)
    mem.w8(lowmem.CrsrCouple, 0xFF)
    mouse = InputGlobals(mem)
    mouse.cursor(-1)                                        # HideCursor
    mouse.mouse(Point(100, 200), False)
    for addr in (lowmem.RawMouse, lowmem.MTemp):
        mem.w32(addr, 171 << 16 | 256)
    mem.w8(lowmem.CrsrNew, 0xFF)
    mouse.tick()                                            # the cursor's task
    assert mem.r8(lowmem.CrsrNew) == 0
    assert (mem.r16(lowmem.Mouse), mem.r16(lowmem.Mouse + 2)) == (171, 256)
    mouse.mouse(Point(97, 205), False)
    for addr in (lowmem.RawMouse, lowmem.Mouse):
        assert (mem.r16(addr), mem.r16(addr + 2)) == (168, 261)
    assert mouse.moved == Point(71, 56)
    mem.w32(lowmem.RawMouse, 171 << 16 | 256)               # without CrsrNew this time
    mouse.mouse(Point(99, 205), False)
    assert (mem.r16(lowmem.Mouse), mem.r16(lowmem.Mouse + 2)) == (173, 256)
    mouse.cursor(1)                                         # ShowCursor: under the pointer again
    assert mouse.moved == Point(0, 0)
    assert (mem.r16(lowmem.Mouse), mem.r16(lowmem.Mouse + 2)) == (99, 205)


def test_with_the_cursor_shown_the_mouse_is_back_under_the_pointer():
    """Pirates! moves the mouse with the cursor to be seen, and waits for
    the cursor's task to clear CrsrNew."""
    mem = RAM(0x2000)
    mem.w8(lowmem.CrsrCouple, 0xFF)
    mouse = InputGlobals(mem)
    mouse.mouse(Point(100, 200), False)
    for addr in (lowmem.MTemp, lowmem.RawMouse, lowmem.Mouse):
        mem.w32(addr, 150 << 16 | 250)
    mem.w8(lowmem.CrsrNew, 0xFF)
    mouse.tick()
    assert mem.r8(lowmem.CrsrNew) == 0
    assert (mem.r16(lowmem.Mouse), mem.r16(lowmem.Mouse + 2)) == (150, 250)
    mouse.mouse(Point(101, 200), False)
    assert (mem.r16(lowmem.Mouse), mem.r16(lowmem.Mouse + 2)) == (101, 200)


def test_the_display_keeps_them(harness):
    h = harness()
    mem = h.d.system.mem
    QTest.keyPress(h.d.screen_widget, "I", Qt.KeyboardModifier.ShiftModifier)
    assert mem.r16(lowmem.KeyLast) & 0xFF == ord("I")
    assert mem.r8(lowmem.KeyMap + 7) & 0x01                 # shift, key code $38
    QTest.keyRelease(h.d.screen_widget, "I", Qt.KeyboardModifier.ShiftModifier)
    assert mem.r16(lowmem.KeyLast) == 0
    QTest.keyPress(h.d.screen_widget, Qt.Key.Key_Up)        # a Mac Plus's, key code $4D
    assert mem.r16(lowmem.KeyLast) == 0x4D1E
    assert mem.r8(lowmem.KeyMap + 9) & 0x20 and mem.r8(lowmem.KeyMap + 15) & 0x40  # and ADB's $7E
    QTest.keyRelease(h.d.screen_widget, Qt.Key.Key_Up)
    assert not any(mem.read(lowmem.KeyMap, 16))
    # With the button up too: Qt gives a widget the mouse's moves then only
    # if it tracks the mouse (QTest's own moves come through either way).
    assert h.d.screen_widget.hasMouseTracking()
    QTest.mouseMove(h.d.screen_widget, QPoint(300, 200))
    assert (mem.r16(lowmem.Mouse), mem.r16(lowmem.Mouse + 2)) == (200, 300)
