| Frames: an application whose window has its own window definition
| procedure, WDEF 128 (wdef.s).  It drags the window by its title bar,
| counts clicks in it, and disposes of it when its close box is clicked,
| going on without it.

        .include "traps.inc"

QD          = -4                | InitGraf(@thePort)
EVENT       = -226              | EventRecord: what, message, when, where, modifiers
WINDOW      = -230
WHICH       = -234              | FindWindow's window
CLICKS      = -236              | content clicks so far
                                | -700: the WDEF's log, to -314
GLOBALS     = 700

mouseDown   = 1
updateEvt   = 6
inDrag      = 4
inContent   = 3
inGoAway    = 6

        .text
start:
        pea     QD(a5)
        _InitGraf
        _InitFonts
        _InitWindows
        _InitMenus
        _TEInit
        clr.l   -(sp)
        _InitDialogs
        _InitCursor
        clr.w   CLICKS(a5)

        clr.l   -(sp)
        move.w  #128,-(sp)
        clr.l   -(sp)
        move.l  #-1,-(sp)
        _GetNewWindow
        move.l  (sp)+,WINDOW(a5)
        move.l  WINDOW(a5),-(sp)
        _SetPort

loop:
        clr.w   -(sp)
        move.w  #0xFFFF,-(sp)           | everyEvent
        pea     EVENT(a5)
        move.l  #30,-(sp)               | sleep
        clr.l   -(sp)                   | mouseRgn
        _WaitNextEvent
        move.b  (sp)+,d0
        beq.s   loop
        move.w  EVENT(a5),d0
        cmp.w   #updateEvt,d0
        beq.s   update
        cmp.w   #mouseDown,d0
        beq.s   mouse
        bra.s   loop

update:
        move.l  EVENT+2(a5),-(sp)
        _BeginUpdate
        move.l  EVENT+2(a5),-(sp)
        _EndUpdate
        bra.s   loop

mouse:
        clr.w   -(sp)
        move.l  EVENT+10(a5),-(sp)
        pea     WHICH(a5)
        _FindWindow
        move.w  (sp)+,d0
        cmp.w   #inGoAway,d0
        beq.s   goaway
        cmp.w   #inDrag,d0
        beq.s   drag
        cmp.w   #inContent,d0
        beq.s   content
        bra.s   loop

goaway:
        clr.w   -(sp)
        move.l  WHICH(a5),-(sp)
        move.l  EVENT+10(a5),-(sp)
        _TrackGoAway
        move.b  (sp)+,d0
        beq.s   loop
        move.l  WINDOW(a5),-(sp)
        _DisposeWindow
        clr.l   WINDOW(a5)
        bra.s   loop

drag:
        move.l  WHICH(a5),-(sp)
        move.l  EVENT+10(a5),-(sp)
        pea     dragbounds(pc)
        _DragWindow
        bra     loop

content:
        addq.w  #1,CLICKS(a5)
        bra     loop

dragbounds:
        .short  24, 4, 2000, 2000
