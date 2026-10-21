| Timer: Time Manager tasks.  An extended one primes itself again every
| 10 ms from its routine; the application spins until it has run 30
| times, timing that in ticks, then removes it and checks it runs no
| more.  Another, primed for a second and removed at once, never runs,
| and is left with what it had still to wait.

        .include "traps.inc"

TASK            = -22           | an extended TMTask: qLink, qType, tmAddr, tmCount, tmWakeUp, tmReserved
COUNT           = -24           | its runs
TASK2           = -46           | the other
QWAIT           = -48           | TASK's qType once primed
LEFT            = -52           | TASK2's tmCount once removed
QDONE           = -54           | TASK2's qType once removed
RUNS2           = -56           | TASK2's runs
ELAPSED         = -60           | ticks for TASK's 30 runs
AFTER           = -62           | TASK's runs once removed
GLOBALS         = 62

TICKS           = 0x16A
PERIOD          = 10            | milliseconds
RUNS            = 30

start:
        lea     TASK2(a5), a0
        moveq   #-TASK2/2-1, d0
1:      clr.w   (a0)+
        dbra    d0, 1b

        lea     TASK(a5), a0
        lea     task(pc), a1
        move.l  a1, 6(a0)
        _InsXTime
        move.l  TICKS, d6
        lea     TASK(a5), a0
        moveq   #PERIOD, d0
        _PrimeTime
        move.w  TASK+4(a5), QWAIT(a5)
1:      cmpi.w  #RUNS, COUNT(a5)
        blt.s   1b
        move.l  TICKS, d0
        sub.l   d6, d0
        move.l  d0, ELAPSED(a5)
        lea     TASK(a5), a0
        _RmvTime
        move.w  COUNT(a5), d1
        bsr.s   five_ticks
        move.w  COUNT(a5), d0
        sub.w   d1, d0
        move.w  d0, AFTER(a5)

        lea     TASK2(a5), a0
        lea     task2(pc), a1
        move.l  a1, 6(a0)
        _InsTime
        lea     TASK2(a5), a0
        move.l  #1000, d0
        _PrimeTime
        lea     TASK2(a5), a0
        _RmvTime
        move.l  TASK2+10(a5), LEFT(a5)
        move.w  TASK2+4(a5), QDONE(a5)
        bsr.s   five_ticks
        _ExitToShell

five_ticks:
        move.l  TICKS, d0
        addq.l  #5, d0
1:      cmp.l   TICKS, d0
        bhi.s   1b
        rts

| A1 points at the task, which is how it finds the count without A5.
task:
        addq.w  #1, COUNT-TASK(a1)
        movea.l a1, a0
        moveq   #PERIOD, d0
        _PrimeTime
        rts

task2:
        addq.w  #1, RUNS2-TASK2(a1)
        rts
