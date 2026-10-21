| VBL: a VBL task that counts its runs, asking to run every other tick.
| The application spins until it has run 10 times, counts its runs while
| it waits in Delay, then checks that it doesn't run while interrupts are
| masked or once it has been removed.  Then a task that doesn't set its
| count again, which runs once and is taken out of the queue, so that it
| can be installed again.

        .include "traps.inc"

TASK            = -14           | the VBLTask: qLink, qType, vblAddr, vblCount, vblPhase
COUNT           = -16           | the task's runs
MASKED          = -18           | runs while interrupts were masked
REMOVED         = -20           | VRemove's result
AFTER           = -22           | runs after it
KEPT            = -26           | D1 after the spin: the task's D1 mustn't show
DELAYED         = -28           | runs during a Delay of 30 ticks
ONCE            = -30           | the runs of the task that runs once
GONE            = -32           | VRemove's result for it: it's out of the queue
GLOBALS         = 32

TICKS           = 0x16A
VTYPE           = 1
EVERY           = 2

start:
        lea     TASK(a5), a0
        clr.l   (a0)
        move.w  #VTYPE, 4(a0)
        lea     task(pc), a1
        move.l  a1, 6(a0)
        move.w  #EVERY, 10(a0)
        clr.w   12(a0)
        _VInstall
        move.l  #0x12345678, d1
1:      cmpi.w  #10, COUNT(a5)
        blt.s   1b
        move.l  d1, KEPT(a5)

        | The host has the processor while it waits, and the ticks go on.
        move.w  COUNT(a5), d1
        movea.w #30, a0
        _Delay
        move.w  COUNT(a5), d0
        sub.w   d1, d0
        move.w  d0, DELAYED(a5)

        | Masked, it waits.  (On a Mac Ticks would stop too, the VBL
        | interrupt being what counts them; macrt's clock keeps going.)
        move.w  sr, -(sp)
        ori.w   #0x0700, sr
        move.w  COUNT(a5), d1
        bsr.s   five_ticks
        move.w  COUNT(a5), d0
        sub.w   d1, d0
        move.w  d0, MASKED(a5)
        move.w  (sp)+, sr

        lea     TASK(a5), a0
        _VRemove
        move.w  d0, REMOVED(a5)
        move.w  COUNT(a5), d1
        bsr.s   five_ticks
        move.w  COUNT(a5), d0
        sub.w   d1, d0
        move.w  d0, AFTER(a5)

        | Inside Macintosh II-350: a task that doesn't set its count again
        | is taken out of the queue once it has run.  Dragonworld installs
        | its music's again for each tune.
        bsr.s   install_once
        bsr.s   install_once
        lea     TASK(a5), a0
        _VRemove
        move.w  d0, GONE(a5)
        _ExitToShell

install_once:
        lea     TASK(a5), a0
        lea     once(pc), a1
        move.l  a1, 6(a0)
        move.w  #1, 10(a0)
        move.w  ONCE(a5), d3
        _VInstall
1:      cmp.w   ONCE(a5), d3
        beq.s   1b                      | then on, for any run more

five_ticks:
        move.l  TICKS, d0
        addq.l  #5, d0
1:      cmp.l   TICKS, d0
        bhi.s   1b
        rts

| A0 points at the task, which is how it finds the count without A5.
task:
        addq.w  #1, COUNT-TASK(a0)
        move.w  #EVERY, 10(a0)
        moveq   #-1, d0
        moveq   #-1, d1
        movea.l d0, a1
        rts

once:
        addq.w  #1, ONCE-TASK(a0)
        rts
