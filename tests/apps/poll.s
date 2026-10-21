| Poll: GetNextEvent with nothing to give.  The application waits for the
| Ticks to change and then calls it once, 30 times, as Hacker II does as
| it types: as on a Mac, where it returns at once, that takes 30 ticks,
| not 60.  Then it calls it 30 times one after another, which takes about
| 30 ticks too: asked again in the same tick, it waits for the next.

        .include "traps.inc"

EVENT           = -16           | an EventRecord
ONCE            = -20           | ticks for the 30 waits and looks
AGAIN           = -24           | ticks for the 30 looks one after another
GLOBALS         = 24

TICKS           = 0x16A
TIMES           = 30

start:
        move.l  TICKS, d6
        moveq   #TIMES-1, d7
1:      move.l  TICKS, d0
2:      cmp.l   TICKS, d0
        beq.s   2b
        bsr.s   look
        dbra    d7, 1b
        move.l  TICKS, d0
        sub.l   d6, d0
        move.l  d0, ONCE(a5)

        move.l  TICKS, d6
        moveq   #TIMES-1, d7
1:      bsr.s   look
        dbra    d7, 1b
        move.l  TICKS, d0
        sub.l   d6, d0
        move.l  d0, AGAIN(a5)
        _ExitToShell

look:
        clr.w   -(sp)
        move.w  #-1, -(sp)              | everyEvent
        pea     EVENT(a5)
        _GetNextEvent
        addq.l  #2, sp
        rts
