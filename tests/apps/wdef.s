| WDEF 128, Frames's window definition procedure.  It logs each message
| it's sent and leaves the work to the System's WDEF 0, as Microsoft
| Excel's do for the messages they don't answer.
|
|   FUNCTION WDEF(varCode: INTEGER; theWindow: WindowPtr;
|                 message: INTEGER; param: LONGINT): LONGINT;

        .include "traps.inc"

CurrentA5   = 0x904
LOG         = -700              | frames.s's: a count, then 6 bytes a message
LOG_MAX     = 64

| After LINK A6: the arguments, from the last
PARAM       = 8
MESSAGE     = 12
WINDOW      = 14
VARCODE     = 18
RESULT      = 20
ARGS        = 12

        .text
wdef:
        link    a6,#0
        move.l  a5,-(sp)
        move.l  CurrentA5,a5

        lea     LOG(a5),a0
        move.w  (a0),d0
        cmp.w   #LOG_MAX,d0
        bhs.s   1f
        addq.w  #1,(a0)
        mulu.w  #6,d0
        lea     2(a0,d0.w),a1
        move.w  MESSAGE(a6),(a1)+
        move.l  PARAM(a6),(a1)
1:
        | GetResource('WDEF', 0), and call it with the same arguments.
        clr.l   -(sp)
        move.l  #0x57444546,-(sp)
        clr.w   -(sp)
        _GetResource
        move.l  (sp)+,a0
        move.l  (a0),a0
        clr.l   -(sp)
        move.w  VARCODE(a6),-(sp)
        move.l  WINDOW(a6),-(sp)
        move.w  MESSAGE(a6),-(sp)
        move.l  PARAM(a6),-(sp)
        jsr     (a0)
        move.l  (sp)+,RESULT(a6)

        move.l  (sp)+,a5
        unlk    a6
        move.l  (sp)+,a0
        lea     ARGS(sp),sp
        jmp     (a0)
