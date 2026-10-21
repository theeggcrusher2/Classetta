"""How long a 68000 takes over its instructions, as the M68000 User's
Manual's tables have it (Section 8), for a program that times a disk's
bytes by the instructions it runs between them."""

import pytest

from macrt.m68k.cpu import CPU
from macrt.m68k.cycles import RATE, cycles, untaken
from macrt.m68k.hardware import Clock
from macrt.memory import RAM

CODE = 0x10000


@pytest.mark.parametrize("code, taken", [
    ("1010", 8),                    # move.b (a0),d0
    ("4a10", 8),                    # tst.b (a0)
    ("3403", 4),                    # move.w d3,d2
    ("7001", 4),                    # moveq #1,d0
    ("b200", 4),                    # cmp.b d0,d1
    ("0643 000f", 8),               # addi.w #$f,d3
    ("e24b", 8),                    # lsr.w #1,d3
    ("86fc 001e", 144),             # divu.w #$1e,d3: its worst
    ("2b6e 0004 0008", 28),         # move.l 4(a6),8(a5)
    ("48e7 78f0", 72),              # movem.l d1-d4/a0-a3,-(a7)
    ("4eb9 0001 0000", 20),         # jsr $10000
    ("4e75", 16),                   # rts
    ("6afc", 10),                   # bpl.s, taken
    ("6600 ff8a", 10),              # bne.w, taken
    ("51c8 fffe", 10),              # dbra, taken
    ("a9f4", 34),                   # an A-line trap
])
def test_an_instruction_takes_what_the_manual_says(code, taken):
    assert cycles(bytes.fromhex(code), 0) == taken


@pytest.mark.parametrize("code, more", [
    ("6afc", (-2, 2)),              # bpl.s: 8
    ("6600 ff8a", (2, 4)),          # bne.w: 12
    ("51c8 fffe", (4, 4)),          # dbra, its count run out: 14
    ("56ca fff8", (2, 4)),          # dbne, its condition met: 12
    ("6100 0010", (0, 0)),          # bsr: always taken
    ("1010", (0, 0)),               # move.b (a0),d0
])
def test_a_branch_not_taken_takes_more_or_less(code, more):
    assert untaken(bytes.fromhex(code), 0) == more


def test_timed_code_counts_the_cycles_of_the_instructions_it_runs():
    """A branch of two words that isn't taken too, though the core says
    each instruction is a word long."""
    mem = RAM(0x20000)
    mem.write(CODE, bytes.fromhex("7002"            # moveq #2,d0          4
                                  "51c8 fffe"       # dbra d0,*            10 10 14
                                  "b000"            # cmp.b d0,d0          4
                                  "6600 0006"       # bne.w +6             12
                                  "6702"            # beq.s +2             10
                                  "4e71"            # nop
                                  "4e71"))          # nop                  4
    cpu, clock = CPU(mem), Clock()
    try:
        cpu.pc = CODE
        with clock.counting(), cpu.timed(clock):
            start = clock.now()
            assert cpu.run(CODE + 18).kind == "until"
            assert (clock.now() - start) * RATE == pytest.approx(68)
    finally:
        cpu.close()
