| Hello: a small Macintosh application in 68000 assembly, to test the
| runtime end to end.  It sets up the Toolbox, puts up an Apple and a File
| menu and a window, and runs the usual event loop: it draws in the window
| on an update event, follows clicks in the menu bar, the window's close
| box and its content, and quits from File > Quit (Command-Q).
|
| Assembled by tests/apps/build.py with GNU as for m68k.  Code refers to
| its globals off A5 and to itself PC-relative, as a Mac compiler's does.

        .include "traps.inc"

| Globals, below A5.  The QuickDraw globals end at -4(A5): thePort is at
| -4, randSeed at -126, 206 bytes in all.
QD          = -4                | InitGraf(@thePort)
EVENT       = -226              | EventRecord: what, message, when, where, modifiers
WINDOW      = -230              | the window
WHICH       = -234              | FindWindow's window
CLICKS      = -236              | content clicks so far
GLOBALS     = 236               | below A5, in all

| Event Manager
mouseDown   = 1
keyDown     = 3
autoKey     = 5
updateEvt   = 6
cmdKey      = 0x0100
| FindWindow
inMenuBar   = 1
inDrag      = 4
inContent   = 3
inGoAway    = 6
| Menus
APPLE_MENU  = 128
FILE_MENU   = 129
QUIT_ITEM   = 1

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
        move.l  #0x0000FFFF,d0
        _FlushEvents

        | Menus: Apple (with the desk accessories) and File.
        clr.l   -(sp)
        move.w  #APPLE_MENU,-(sp)
        _GetMenu
        move.l  (sp),-(sp)              | keep it for AppendResMenu
        clr.w   -(sp)
        _InsertMenu
        move.l  #0x44525652,-(sp)       | 'DRVR'
        _AppendResMenu
        clr.l   -(sp)
        move.w  #FILE_MENU,-(sp)
        _GetMenu
        clr.w   -(sp)
        _InsertMenu
        _DrawMenuBar

        | The window, WIND 128, in front.
        clr.l   -(sp)
        move.w  #128,-(sp)
        clr.l   -(sp)
        move.l  #-1,-(sp)
        _GetNewWindow
        move.l  (sp)+,WINDOW(a5)
        move.l  WINDOW(a5),-(sp)
        _SetPort
        clr.w   CLICKS(a5)

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
        beq     update
        cmp.w   #mouseDown,d0
        beq     mouse
        cmp.w   #keyDown,d0
        beq     key
        cmp.w   #autoKey,d0
        beq     key
        bra.s   loop

| --- updateEvt: draw the window ------------------------------------------
update:
        move.l  EVENT+2(a5),-(sp)
        _BeginUpdate
        bsr     draw
        move.l  EVENT+2(a5),-(sp)
        _EndUpdate
        bra     loop

draw:
        move.l  WINDOW(a5),a0
        pea     16(a0)                  | portRect
        _EraseRect
        move.w  #20,-(sp)
        move.w  #30,-(sp)
        _MoveTo
        pea     hello(pc)
        _DrawString
        pea     oval(pc)
        _PaintOval
        pea     box(pc)
        _FrameRect
        | The clicks so far, as text.
        move.w  #20,-(sp)
        move.w  #120,-(sp)
        _MoveTo
        moveq   #0,d0
        move.w  CLICKS(a5),d0
        lea     -32(sp),sp
        move.l  sp,a0
        _NumToString                    | Pack7 0: D0 to a string at A0
        move.l  sp,-(sp)
        _DrawString
        lea     32(sp),sp
        rts

| --- mouseDown ------------------------------------------------------------
mouse:
        clr.w   -(sp)
        move.l  EVENT+10(a5),-(sp)      | where
        pea     WHICH(a5)
        _FindWindow
        move.w  (sp)+,d0
        cmp.w   #inMenuBar,d0
        beq.s   menubar
        cmp.w   #inGoAway,d0
        beq.s   goaway
        cmp.w   #inDrag,d0
        beq.s   drag
        cmp.w   #inContent,d0
        beq.s   content
        bra     loop

menubar:
        clr.l   -(sp)
        move.l  EVENT+10(a5),-(sp)
        _MenuSelect
        move.l  (sp)+,d0
        bra     command

goaway:
        clr.w   -(sp)
        move.l  WHICH(a5),-(sp)
        move.l  EVENT+10(a5),-(sp)
        _TrackGoAway
        move.b  (sp)+,d0
        bne     quit
        bra     loop

drag:
        move.l  WHICH(a5),-(sp)
        move.l  EVENT+10(a5),-(sp)
        pea     dragbounds(pc)
        _DragWindow
        bra     loop

content:
        addq.w  #1,CLICKS(a5)
        move.l  WINDOW(a5),a0
        pea     16(a0)
        _InvalRect
        bra     loop

| --- keyDown: Command keys are menu commands ----------------------------
key:
        move.w  EVENT+14(a5),d0         | modifiers
        and.w   #cmdKey,d0
        beq     loop
        clr.l   -(sp)
        move.w  EVENT+4(a5),d0          | message: the character in the low byte
        and.w   #0xFF,d0
        move.w  d0,-(sp)
        _MenuKey
        move.l  (sp)+,d0
        | fall through

| --- a menu command: menu ID in the high word of D0, item in the low ----
command:
        move.l  d0,d1
        swap    d1
        cmp.w   #FILE_MENU,d1
        bne.s   unhilite
        cmp.w   #QUIT_ITEM,d0
        beq.s   quit
unhilite:
        clr.w   -(sp)
        _HiliteMenu
        bra     loop

quit:
        _ExitToShell

hello:      .byte 13
            .ascii "Hello, world!"
            .even
oval:       .short 40, 30, 90, 110      | top, left, bottom, right
box:        .short 40, 130, 90, 210
dragbounds: .short 24, 4, 10000, 10000
