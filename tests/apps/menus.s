| Menus: an application with its own menu bar definition procedure, MBDF
| 128 (mbdf.s), which InitProcMenu installs.  File's second item starts
| disabled; the MBDF enables it as File is pulled down.  The application
| keeps the last menu choice, and quits from File > Quit.

        .include "traps.inc"

QD          = -4                | InitGraf(@thePort)
EVENT       = -226              | EventRecord: what, message, when, where, modifiers
WINDOW      = -230
WHICH       = -234              | FindWindow's window
CHOICE      = -238              | MenuSelect's last answer
                                | -300: the MBDF's log; -310: the rect mbSave got
GLOBALS     = 310

mouseDown   = 1
updateEvt   = 6
inMenuBar   = 1
MBDF_ID     = 128
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
        move.w  #MBDF_ID,-(sp)
        _InitProcMenu

        clr.l   -(sp)
        move.w  #FILE_MENU,-(sp)
        _GetMenu
        clr.w   -(sp)
        _InsertMenu
        _DrawMenuBar

        clr.l   -(sp)
        move.w  #128,-(sp)
        clr.l   -(sp)
        move.l  #-1,-(sp)
        _GetNewWindow
        move.l  (sp)+,WINDOW(a5)
        move.l  WINDOW(a5),-(sp)
        _SetPort
        clr.l   CHOICE(a5)

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
        cmp.w   #inMenuBar,d0
        bne.s   loop
        clr.l   -(sp)
        move.l  EVENT+10(a5),-(sp)
        _MenuSelect
        move.l  (sp)+,d0
        move.l  d0,CHOICE(a5)
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
