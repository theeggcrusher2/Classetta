| Vectors: exception vectors a program takes for its own, as games from
| the Mac's first years do.  It puts its own handlers in the vectors for
| TRAP #1 and for dividing by zero and sets each off twice, and in CHK's
| and sets that off once, then puts its
| own handler for the VIA's interrupts in their vector, going on to the
| one that was there with TRAP #0, as Flight Simulator does, and waits
| for it to run five times.  It quits with its handlers still in them.
| (macrt once looked for a program's own interrupt handler only at its traps and
| exceptions, so it makes one after putting it in.)

        .include "traps.inc"

TRAPS           = -2            | TRAP #1's runs
DIVIDES         = -4            | the divide-by-zero handler's runs
AFTER           = -6            | -1 once the code after them has run
QUOTIENT        = -8            | D0.w after dividing by zero: as it was
ZERO            = -10           | a word of 0 to divide by
INTERRUPTS      = -12           | the VIA's interrupts, as its handler has counted them
FLAGGED         = -14           | how many came with a flag set in vIFR
CLEARED         = -16           | how many left none set, after the ROM's handler
OLD_DIVIDE      = -20           | the divide-by-zero vector as it was
ROM             = -24           | ROMBase
CHECKS          = -26           | the CHK handler's runs
GLOBALS         = 26

VIA             = 0x1D4
ROM_BASE        = 0x2AE
vIFR            = 0x1A00
ZERO_DIVIDE     = 0x14
CHK             = 0x18
LEVEL_1         = 0x64
TRAP_0          = 0x80
TRAP_1          = 0x84
CA1_CA2         = 0x03
TRAP_0_WORD     = 0x4E40        | TRAP #0 and #1, as words: "trap" is traps.inc's macro
TRAP_1_WORD     = 0x4E41

start:
        move.l  ROM_BASE, ROM(a5)
        move.l  ZERO_DIVIDE, OLD_DIVIDE(a5)
        lea     on_trap(pc), a0
        move.l  a0, TRAP_1
        lea     on_divide(pc), a0
        move.l  a0, ZERO_DIVIDE
        .word   TRAP_1_WORD
        .word   TRAP_1_WORD
        move.l  #0x00050007, d0
        moveq   #0, d1
        clr.w   ZERO(a5)
        divu    d1, d0
        divu    ZERO(a5), d0            | an extension word to go past
        move.w  d0, QUOTIENT(a5)
        lea     on_chk(pc), a0
        move.l  a0, CHK
        moveq   #-1, d0
        chk.w   #10, d0                 | out of bounds, and an extension word to go past
        move.w  #-1, AFTER(a5)

        move.l  LEVEL_1, TRAP_0         | the ROM's handler, for TRAP #0 to go on to
        lea     level_1(pc), a0
        move.l  a0, LEVEL_1
        moveq   #0, d0                  | a trap, after which the VIA's interrupts come
        _FlushEvents
1:      cmpi.w  #5, INTERRUPTS(a5)
        blt.s   1b
        _ExitToShell

on_trap:
        addq.w  #1, TRAPS(a5)
        rte

on_divide:
        addq.w  #1, DIVIDES(a5)
        rte

on_chk:
        addq.w  #1, CHECKS(a5)
        rte

level_1:
        movem.l d0/a0, -(sp)
        addq.w  #1, INTERRUPTS(a5)
        movea.l VIA, a0
        move.b  vIFR(a0), d0
        andi.b  #CA1_CA2, d0
        beq.s   1f
        addq.w  #1, FLAGGED(a5)
1:      .word   TRAP_0_WORD             | on to the ROM's, which clears them
        movea.l VIA, a0
        move.b  vIFR(a0), d0
        andi.b  #CA1_CA2, d0
        bne.s   2f
        addq.w  #1, CLEARED(a5)
2:      movem.l (sp)+, d0/a0
        rte
