| MBDF 128, Menus's menu bar definition procedure.  It logs each message
| it's sent, enables File's second item when it's asked where File goes
| (mbRect), and leaves the rest to the System's MBDF 0, as Microsoft
| Excel's does.
|
|   FUNCTION MBDF(selector, message, parameter1: INTEGER;
|                 parameter2: LONGINT): LONGINT;

        .include "traps.inc"

CurrentA5   = 0x904
LOG         = -300              | menus.s's: a count, then 8 bytes a message
LOG_MAX     = 8
SAVED       = -310              | the rect mbSave was given
FILE_MENU   = 129
mbSave      = 7
mbRect      = 9

| After LINK A6: the arguments, from the last
PARAM2      = 8
PARAM1      = 12
MESSAGE     = 14
SELECTOR    = 16
RESULT      = 18
ARGS        = 10

        .text
mbdf:
        link    a6,#0
        move.l  a5,-(sp)
        move.l  CurrentA5,a5

        lea     LOG(a5),a0
        move.w  (a0),d0
        cmp.w   #LOG_MAX,d0
        bhs.s   1f
        addq.w  #1,(a0)
        lsl.w   #3,d0
        lea     2(a0,d0.w),a1
        move.w  MESSAGE(a6),(a1)+
        move.w  PARAM1(a6),(a1)+
        move.l  PARAM2(a6),(a1)
1:
        cmp.w   #mbSave,MESSAGE(a6)
        bne.s   2f
        move.l  PARAM2(a6),a0
        move.l  (a0)+,SAVED(a5)
        move.l  (a0),SAVED+4(a5)
2:
        cmp.w   #mbRect,MESSAGE(a6)
        bne.s   3f
        clr.l   -(sp)
        move.w  #FILE_MENU,-(sp)
        _GetMHandle
        move.w  #2,-(sp)
        _EnableItem
3:
        | GetResource('MBDF', 0), and call it with the same arguments.
        clr.l   -(sp)
        move.l  #0x4D424446,-(sp)
        clr.w   -(sp)
        _GetResource
        move.l  (sp)+,a0
        move.l  (a0),a0
        clr.l   -(sp)
        move.w  SELECTOR(a6),-(sp)
        move.w  MESSAGE(a6),-(sp)
        move.w  PARAM1(a6),-(sp)
        move.l  PARAM2(a6),-(sp)
        jsr     (a0)
        move.l  (sp)+,RESULT(a6)

        move.l  (sp)+,a5
        unlk    a6
        move.l  (sp)+,a0
        lea     ARGS(sp),sp
        jmp     (a0)
