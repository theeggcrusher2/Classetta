| Driver: opens its own driver, DRVR 20 ".Echo" (echo.s), and calls it:
| a write and a read; a control call made asynchronously that a status
| call finishes, and one made synchronously that a VBL task's status call
| finishes while it waits; KillIO on one left going; Close, a write after
| it, and Open again.  It keeps each result for the test to look at.

        .include "traps.inc"

PB              = -50           | a parameter block for most calls
CPB             = -100          | the control calls left going
SPB             = -150          | the VBL task's status call
TASK            = -164          | the VBL task, just before SPB
DATA            = -180          | 16 bytes to write
OPENED          = -182          | Open's result
REFNUM          = -184          | its ioRefNum
UNITREF         = -186          | dCtlRefNum of unit 20's DCE
WROTE           = -188          | Write's result
WCOUNT          = -192          | its ioActCount
READ            = -194          | Read's result
BACK            = -210          | what it read
QUEUED          = -212          | D0 after the asynchronous control call
GOING           = -214          | its ioResult then
FINISHED        = -216          | and after the status call
FIRST_D0        = -218          | the completion routine's D0 then
WAITED          = -220          | D0 after the synchronous control call
KILLED          = -222          | KillIO's result
ABORTED         = -224          | the killed call's ioResult
COMP_D0         = -226          | the completion routine's last D0
COMP_A0         = -230          | and A0
COMP_N          = -232          | its calls
CLOSED          = -234          | Close's result
LATE            = -236          | Write's after it
AGAIN           = -238          | Open's again
AGAINREF        = -240          | and its ioRefNum
NOPE            = -242          | Open's of .Nope, which isn't a driver
GLOBALS         = 242

ioCompletion    = 12
ioResult        = 16
ioNamePtr       = 18
ioRefNum        = 24
csCode          = 26
ioBuffer        = 32
ioReqCount      = 36
ioActCount      = 40
UTableBase      = 0x11C
CurrentA5       = 0x904

start:
        lea     PB(a5), a0
        bsr     clear
        lea     CPB(a5), a0
        bsr     clear
        lea     SPB(a5), a0
        bsr     clear
        clr.w   COMP_N(a5)

        lea     PB(a5), a0
        lea     echo(pc), a1
        move.l  a1, ioNamePtr(a0)
        _Open
        move.w  d0, OPENED(a5)
        move.w  ioRefNum(a0), REFNUM(a5)
        movea.l UTableBase, a1
        movea.l 4*20(a1), a1
        movea.l (a1), a1
        move.w  24(a1), UNITREF(a5)

        lea     DATA(a5), a1
        move.l  #0x48656C6C, (a1)       | "Hello"
        move.b  #0x6F, 4(a1)
        move.l  a1, ioBuffer(a0)
        moveq   #5, d0
        move.l  d0, ioReqCount(a0)
        _Write
        move.w  d0, WROTE(a5)
        move.l  ioActCount(a0), WCOUNT(a5)
        lea     BACK(a5), a1
        move.l  a1, ioBuffer(a0)
        _Read
        move.w  d0, READ(a5)

        | Asynchronously: left going until the status call
        lea     CPB(a5), a0
        move.w  REFNUM(a5), ioRefNum(a0)
        lea     completion(pc), a1
        move.l  a1, ioCompletion(a0)
        move.w  #50, csCode(a0)
        _ControlAsync
        move.w  d0, QUEUED(a5)
        move.w  ioResult(a0), GOING(a5)
        lea     SPB(a5), a0
        move.w  REFNUM(a5), ioRefNum(a0)
        _StatusImmed
        move.w  CPB+ioResult(a5), FINISHED(a5)
        move.w  COMP_D0(a5), FIRST_D0(a5)

        | Synchronously: the VBL task's status call finishes it
        lea     TASK(a5), a0
        clr.l   (a0)
        move.w  #1, 4(a0)
        lea     task(pc), a1
        move.l  a1, 6(a0)
        move.w  #1, 10(a0)
        clr.w   12(a0)
        _VInstall
        lea     CPB(a5), a0
        clr.l   ioCompletion(a0)
        _Control
        move.w  d0, WAITED(a5)
        lea     TASK(a5), a0
        _VRemove

        | KillIO: the one left going is aborted
        lea     CPB(a5), a0
        lea     completion(pc), a1
        move.l  a1, ioCompletion(a0)
        _ControlAsync
        lea     PB(a5), a0
        _KillIO
        move.w  d0, KILLED(a5)
        move.w  CPB+ioResult(a5), ABORTED(a5)

        lea     PB(a5), a0
        _Close
        move.w  d0, CLOSED(a5)
        _Write
        move.w  d0, LATE(a5)
        lea     echo(pc), a1
        move.l  a1, ioNamePtr(a0)
        clr.w   ioRefNum(a0)
        _Open
        move.w  d0, AGAIN(a5)
        move.w  ioRefNum(a0), AGAINREF(a5)
        lea     nope(pc), a1
        move.l  a1, ioNamePtr(a0)
        _Open
        move.w  d0, NOPE(a5)
        _ExitToShell

| A parameter block's 50 bytes, at A0, cleared.
clear:
        movea.l a0, a1
        moveq   #24, d0
1:      clr.w   (a1)+
        dbra    d0, 1b
        rts

completion:
        move.l  a5, -(sp)
        movea.l CurrentA5, a5
        move.w  d0, COMP_D0(a5)
        move.l  a0, COMP_A0(a5)
        addq.w  #1, COMP_N(a5)
        movea.l (sp)+, a5
        rts

| Each tick until the control call in CPB is going, then a status call to finish it.
task:
        cmpi.w  #1, CPB-TASK+ioResult(a0)
        beq.s   1f
        move.w  #1, 10(a0)
        rts
1:      lea     SPB-TASK(a0), a0
        _StatusImmed
        rts

echo:   .byte   5
        .ascii  ".Echo"
nope:   .byte   5
        .ascii  ".Nope"
        .even
