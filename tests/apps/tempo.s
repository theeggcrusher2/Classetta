| Tempo: a routine of its own for the VIA's timer 1, in Lvl1DT, as Master
| Tracks Pro has for its tempo's clock.  It leaves the ROM's handler in
| the vector, runs timer 1 free at 100 Hz with its interrupt enabled, and
| waits for the routine to run 30 times.  The routine looks for the VIA
| in A1, clears the flag and changes D2 and A2, which the ROM's handler
| keeps for the code it interrupted.  (macrt once looked for a program's
| own routines only at its traps, so it makes one after putting its in.)

        .include "traps.inc"

TIMES           = -2            | the routine's runs
WRONG_A1        = -4            | runs without the VIA in A1
CLOBBERED       = -6            | times the loop found D2 or A2 changed
START           = -10           | Ticks as timer 1 starts
LAST            = -14           | Ticks after the 30th
GLOBALS         = 14

RUNS            = 30
VIA             = 0x1D4
TICKS           = 0x16A
CURRENT_A5      = 0x904
LVL1DT_T1       = 0x192 + 4 * 6
vT1C            = 0x0800
vT1CH           = 0x0A00
vACR            = 0x1600
vIER            = 0x1C00
T1              = 0x40
FREE_RUNNING    = 0x40
PERIOD          = 7832          | counts, 2 more each time round: 100 Hz
KEPT            = 0x12345678

start:
        lea     on_timer_1(pc), a0
        move.l  a0, LVL1DT_T1
        moveq   #0, d0                  | a trap, after which the routine is looked for
        _FlushEvents
        movea.l VIA, a1
        move.b  #FREE_RUNNING, vACR(a1)
        move.l  TICKS, START(a5)
        move.b  #PERIOD & 0xFF, vT1C(a1)
        move.b  #PERIOD >> 8, vT1CH(a1)
        move.b  #0x80 + T1, vIER(a1)
        move.l  #KEPT, d2
        movea.l d2, a2
1:      cmpi.l  #KEPT, d2
        bne.s   2f
        cmpa.l  #KEPT, a2
        beq.s   3f
2:      addq.w  #1, CLOBBERED(a5)
        move.l  #KEPT, d2
        movea.l d2, a2
3:      cmpi.w  #RUNS, TIMES(a5)
        blt.s   1b
        move.l  TICKS, LAST(a5)
        movea.l VIA, a1
        move.b  #T1, vIER(a1)           | off again
        _ExitToShell

on_timer_1:
        movea.l CURRENT_A5, a0
        addq.w  #1, TIMES(a0)
        cmpa.l  VIA, a1
        beq.s   1f
        addq.w  #1, WRONG_A1(a0)
1:      tst.b   vT1C(a1)                | reading the low byte clears the flag
        moveq   #-1, d2
        movea.l d2, a2
        rts
