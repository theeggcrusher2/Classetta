"""
How long a 68000 takes over an instruction, near enough, in its clock's
cycles: what the M68000 User's Manual's tables of instruction execution
times give (Section 8), for a program whose own routine reads a disk
byte by byte and times the bytes by the instructions it runs between
them.  Shufflepuck Cafe counts how many times round a loop of two
instructions it goes while a byte comes by, then waits half as long as
that, round a loop of one, before it reads on.

A branch is counted as taken, and what it takes more or less when it
isn't is added once it's known (untaken); a shift by a count in a
register is counted as by four; a multiply as by a number with half its
bits set; a divide as its worst.

A Mac 128K's, 512K's or Plus's 68000 runs at 7.8336 MHz, but the video
and the sound take their turns at its RAM, and it gets about as far as
one at 6 MHz would (RATE): its code waits.  With that, Shufflepuck's
wait comes to between three and four bits going by, as it's meant to.
"""

from __future__ import annotations

# The time to work out an effective address and fetch a byte or a word
# there, by its mode: Dn, An, (An), (An)+, -(An), d16(An), d8(An,Xn);
# and with mode 7, by its register: abs.W, abs.L, d16(PC), d8(PC,Xn), #.
# A long takes four cycles more for any that isn't a register.
_EA = (0, 0, 4, 4, 6, 8, 10)
_EA7 = (8, 12, 8, 10, 4)
# JMP, JSR, LEA and PEA by the mode of their address: (An), d16(An),
# d8(An,Xn); abs.W, abs.L, d16(PC), d8(PC,Xn).
_JMP = {2: 8, 5: 10, 6: 14, 70: 10, 71: 12, 72: 10, 73: 14}
_JSR = {2: 16, 5: 18, 6: 22, 70: 18, 71: 20, 72: 18, 73: 22}
_LEA = {2: 4, 5: 8, 6: 12, 70: 8, 71: 12, 72: 8, 73: 12}
_PEA = {2: 12, 5: 16, 6: 20, 70: 16, 71: 20, 72: 16, 73: 20}
# MOVEM by the mode of its address, to registers and from them.
_MOVEM_IN = {2: 12, 3: 12, 5: 16, 6: 18, 70: 16, 71: 20, 72: 16, 73: 18}
_MOVEM_OUT = {2: 8, 4: 8, 5: 12, 6: 14, 70: 12, 71: 16}
_SIMPLE = {0x4E71: 4, 0x4E75: 16, 0x4E73: 20, 0x4E77: 20, 0x4E76: 4, 0x4E72: 4,
           0x4E70: 132}             # NOP, RTS, RTE, RTR, TRAPV, STOP, RESET
EXCEPTION = 34                      # TRAP, an A-line or F-line trap
RATE = 6_000_000                    # cycles a second, as a Mac 512K or Plus runs them


def _ea(op: int, long: bool = False) -> int:
    mode, reg = op >> 3 & 7, op & 7
    t = _EA[mode] if mode < 7 else _EA7[reg] if reg < 5 else 0
    return t + 4 if long and t else t


def _mode(op: int) -> int:
    """The mode of the effective address, 70 to 74 for mode 7's."""
    mode = op >> 3 & 7
    return 70 + (op & 7) if mode == 7 else mode


def _memory(op: int) -> bool:
    return _mode(op) not in (0, 1, 74)


def cycles(code, at: int) -> int:
    """The cycles the instruction at at in code takes."""
    op = code[at] << 8 | code[at + 1]
    top, size = op >> 12, op >> 6 & 3
    long = size == 2
    if top == 0:
        if op & 0x0100:                 # BTST, BCHG, BCLR, BSET by a register; MOVEP
            if op >> 3 & 7 == 1:
                return 24 if op & 0x40 else 16
            if not _memory(op):
                return (6, 8, 10, 8)[size]
            return (4 if size == 0 else 8) + _ea(op)
        if op >> 8 & 15 == 8:           # by a number
            if not _memory(op):
                return (10, 12, 14, 12)[size]
            return (8 if size == 0 else 12) + _ea(op)
        if op & 0x3F == 0x3C:           # to CCR or SR
            return 20
        cmpi = op >> 9 & 7 == 6
        if not _memory(op):
            return (14 if cmpi else 16) if long else 8
        return ((12 if long else 8) if cmpi else (20 if long else 12)) + _ea(op, long)
    if top in (1, 2, 3):                # MOVE, MOVEA
        long = top == 2
        dst = (op >> 9 & 7) | (op >> 3 & 0x38)
        to = _ea(dst, long) - (2 if dst >> 3 & 7 == 4 else 0)
        return 4 + _ea(op, long) + to
    if top == 4:
        return _misc(code, at, op, size)
    if top == 5:
        if size == 3:
            return 10 if op >> 3 & 7 == 1 else (8 + _ea(op) if _memory(op) else 6)   # DBcc, Scc
        if op >> 3 & 7 == 1:
            return 8                    # ADDQ, SUBQ to an address register
        return (12 if long else 8) + _ea(op, long) if _memory(op) else (8 if long else 4)
    if top == 6:
        return 18 if op >> 8 & 15 == 1 else 10      # BSR; Bcc, BRA
    if top == 7:
        return 4                        # MOVEQ
    if top in (8, 9, 11, 12, 13):
        return _arithmetic(op, top, size)
    if top == 14:                       # shifts and rotates
        if size == 3:
            return 8 + _ea(op)
        n = ((op >> 9 & 7) or 8) if not op & 0x20 else 4
        return (8 if long else 6) + 2 * n
    return EXCEPTION


def untaken(code, at: int) -> tuple[int, int]:
    """The cycles the instruction at at takes more, or fewer, than
    cycles() says if it's a branch that isn't taken, and how long it is,
    to know it wasn't by the next coming after it: a Bcc's, or a DBcc's
    whose condition is met, or whose count runs out, as DBRA's does.
    (0, 0) for one that isn't a branch."""
    op = code[at] << 8 | code[at + 1]
    if op >> 12 == 6 and op >> 8 & 15 > 1:
        return (2, 4) if op & 0xFF == 0 else (-2, 2)    # Bcc.W 12, Bcc.S 8
    if op & 0xF0F8 == 0x50C8:
        return (4 if op >> 8 & 15 == 1 else 2), 4       # DBRA 14, DBcc 12
    return 0, 0


def _misc(code, at: int, op: int, size: int) -> int:
    if op in _SIMPLE:
        return _SIMPLE[op]
    mode = _mode(op)
    if op & 0xFFF0 == 0x4E40:
        return EXCEPTION
    if op & 0xFFF8 == 0x4E50:
        return 16                       # LINK
    if op & 0xFFF8 == 0x4E58:
        return 12                       # UNLK
    if op & 0xFFF0 == 0x4E60:
        return 4                        # MOVE USP
    if op & 0xFFC0 == 0x4E80:
        return _JSR.get(mode, 18)
    if op & 0xFFC0 == 0x4EC0:
        return _JMP.get(mode, 10)
    if op & 0xF1C0 == 0x41C0:
        return _LEA.get(mode, 8)
    if op & 0xFFF8 in (0x4840, 0x4880, 0x48C0):
        return 4                        # SWAP, EXT
    if op & 0xFFC0 == 0x4840:
        return _PEA.get(mode, 16)
    if op & 0xFB80 == 0x4880:           # MOVEM
        n = bin(code[at + 2] << 8 | code[at + 3]).count("1") * (8 if op & 0x40 else 4)
        if op & 0x0400:
            return _MOVEM_IN.get(mode, 16) + n
        return _MOVEM_OUT.get(mode, 12) + n
    if op & 0xFFC0 == 0x40C0:
        return 8 + _ea(op) if _memory(op) else 6     # MOVE from SR
    if op & 0xFDC0 == 0x44C0:
        return 12 + _ea(op)             # MOVE to CCR, SR
    if op & 0xF1C0 == 0x4180:
        return 10 + _ea(op)             # CHK
    if op & 0xFFC0 == 0x4AC0:
        return 10 + _ea(op) if _memory(op) else 4    # TAS
    if op & 0xFFC0 == 0x4800:
        return 8 + _ea(op) if _memory(op) else 6     # NBCD
    if op & 0xF900 == 0x4000 and size != 3:          # NEGX, CLR, NEG, NOT
        long = size == 2
        return (12 if long else 8) + _ea(op, long) if _memory(op) else (6 if long else 4)
    if op & 0xFF00 == 0x4A00 and size != 3:
        return 4 + _ea(op, size == 2)   # TST
    return 8


def _arithmetic(op: int, top: int, size: int) -> int:
    """OR, SUB, CMP, EOR, AND, ADD and the others alongside them."""
    opmode, mode = op >> 6 & 7, op >> 3 & 7
    if opmode in (3, 7):
        if top == 8:
            return (140 if opmode == 3 else 158) + _ea(op)      # DIVU, DIVS
        if top == 12:
            return 54 + _ea(op)                                 # MULU, MULS
        long = opmode == 7                                      # ADDA, SUBA, CMPA
        if top == 11:
            return 6 + _ea(op, long)
        return (6 if _memory(op) else 8) + _ea(op, True) if long else 8 + _ea(op)
    long = size == 2
    if opmode >= 4:
        if mode in (0, 1) and (top != 11 or mode == 1):
            if top == 12 and opmode in (5, 6):
                return 6                                        # EXG
            if top in (8, 12):
                return 18 if mode else 6                        # SBCD, ABCD
            if top == 11:
                return 20 if long else 12                       # CMPM
            return (30 if long else 18) if mode else (8 if long else 4)     # SUBX, ADDX
        if not _memory(op):
            return 8 if long else 4                             # EOR to a data register
        return (12 if long else 8) + _ea(op, long)
    if long:
        return 6 + _ea(op, True) + (0 if _memory(op) else 2)
    return 4 + _ea(op)
