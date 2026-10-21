| Lists: a window with a list of twenty numbered cells and a vertical
| scroll bar, run through the List Manager as an application would run
| one.  It fills the list with drawing off, then turns drawing on; update
| events redraw it, and LClick follows clicks in it and counts the double
| clicks.

        .include "traps.inc"

QD          = -4
EVENT       = -226
WINDOW      = -230
WHICH       = -234
LIST        = -238
CLICKS      = -240              | calls to LClick
DOUBLES     = -242              | those that were double clicks
PT          = -246              | the click, local
TEXT        = -262              | a cell's text, made by NumToString
GLOBALS     = 262

mouseDown   = 1
updateEvt   = 6
inContent   = 3
ROWS        = 20

| List Manager selectors
lClick      = 24
lDoDraw     = 44
lNew        = 68
lSetCell    = 88
lUpdate     = 100

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

        clr.l   -(sp)
        move.w  #128,-(sp)
        clr.l   -(sp)
        move.l  #-1,-(sp)
        _GetNewWindow
        move.l  (sp)+,WINDOW(a5)
        move.l  WINDOW(a5),-(sp)
        _SetPort

| LNew(&view, &bounds, cSize, theProc, window, drawIt, hasGrow, scrollHoriz,
|      scrollVert)
        clr.l   -(sp)
        pea     view_r(pc)
        pea     bounds_r(pc)
        clr.l   -(sp)                   | (0,0): the size LDEF 0 likes
        clr.w   -(sp)                   | LDEF 0
        move.l  WINDOW(a5),-(sp)
        clr.b   -(sp)                   | drawing off while it's filled
        clr.b   -(sp)
        clr.b   -(sp)
        move.b  #1,-(sp)
        move.w  #lNew,-(sp)
        _Pack0
        move.l  (sp)+,LIST(a5)

        moveq   #0,d3                   | the row
fill:   move.l  d3,d0
        addq.l  #1,d0
        lea     TEXT(a5),a0
        _NumToString
        pea     TEXT+1(a5)              | LSetCell(text, length, (row, 0), list)
        moveq   #0,d0
        move.b  TEXT(a5),d0
        move.w  d0,-(sp)
        clr.w   -(sp)                   | the cell: h, then v
        move.w  d3,-(sp)
        move.l  LIST(a5),-(sp)
        move.w  #lSetCell,-(sp)
        _Pack0
        addq.w  #1,d3
        cmp.w   #ROWS,d3
        blt.s   fill

        move.b  #1,-(sp)                | LDoDraw(TRUE, list)
        move.l  LIST(a5),-(sp)
        move.w  #lDoDraw,-(sp)
        _Pack0
        clr.w   CLICKS(a5)
        clr.w   DOUBLES(a5)

loop:
        clr.w   -(sp)
        move.w  #0xFFFF,-(sp)
        pea     EVENT(a5)
        move.l  #30,-(sp)
        clr.l   -(sp)
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
        pea     frame_r(pc)
        _FrameRect
        movea.l WINDOW(a5),a0
        move.l  24(a0),-(sp)            | visRgn, which is what needs drawing
        move.l  LIST(a5),-(sp)
        move.w  #lUpdate,-(sp)
        _Pack0
        move.l  EVENT+2(a5),-(sp)
        _EndUpdate
        bra.s   loop

mouse:
        clr.w   -(sp)
        move.l  EVENT+10(a5),-(sp)
        pea     WHICH(a5)
        _FindWindow
        move.w  (sp)+,d0
        cmp.w   #inContent,d0
        bne.s   loop
        move.l  EVENT+10(a5),PT(a5)
        pea     PT(a5)
        _GlobalToLocal
        clr.w   -(sp)                   | LClick(pt, modifiers, list)
        move.l  PT(a5),-(sp)
        move.w  EVENT+14(a5),-(sp)
        move.l  LIST(a5),-(sp)
        move.w  #lClick,-(sp)
        _Pack0
        addq.w  #1,CLICKS(a5)
        move.b  (sp)+,d0
        beq     loop
        addq.w  #1,DOUBLES(a5)
        bra     loop

view_r:     .short 10, 20, 130, 120
frame_r:    .short 9, 19, 131, 121
bounds_r:   .short 0, 0, ROWS, 1
