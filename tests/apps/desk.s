| Desk: opens the desk accessory its Apple menu lists, Tally (tally.s),
| and has the Desk Manager pass it what's its: the events for its window,
| a key, Copy from the Edit menu, a click in its window, Command-T from
| its menu, and time.  Then, with its own window in front, none of it.
| It closes Tally, opens it again and quits with it open.  Desk and Tally
| keep what happened in Desk's globals (desk.inc) for the test.

        .include "traps.inc"
        .include "desk.inc"

QD              = -4            | InitGraf(@thePort)
UTableBase      = 0x11C
mouseDown       = 1
keyDown         = 3
updateEvt       = 6
cmdKey          = 0x0100
IN_TALLY        = 0x00820168    | v 130, h 360: in Tally's window

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
        move.l  #0x0000FFFF, d0
        _FlushEvents
        lea     -GLOBALS(a5), a0        | Desk's and Tally's counts start at 0
        move.w  #(GLOBALS-206)/2-1, d0
1:      clr.w   (a0)+
        dbra    d0, 1b

        clr.l   -(sp)
        move.w  #128, -(sp)
        clr.l   -(sp)
        moveq   #-1, d0
        move.l  d0, -(sp)
        _GetNewWindow
        move.l  (sp)+, WINDOW(a5)
        move.l  WINDOW(a5), -(sp)
        _SetPort

        | The Apple menu, with the desk accessories
        clr.l   -(sp)
        move.w  #1, -(sp)
        pea     apple(pc)
        _NewMenu
        move.l  (sp)+, MENU(a5)
        move.l  MENU(a5), -(sp)
        clr.w   -(sp)
        _InsertMenu
        move.l  MENU(a5), -(sp)
        move.l  #0x44525652, -(sp)      | 'DRVR'
        _AppendResMenu
        clr.w   -(sp)
        move.l  MENU(a5), -(sp)
        _CountMItems
        move.w  (sp)+, ITEMS(a5)
        move.l  MENU(a5), -(sp)
        move.w  #1, -(sp)
        pea     NAME(a5)
        _GetMenuItemText
        _DrawMenuBar

        | Open it: its window's in front, and the events for it are its own
        clr.w   -(sp)
        pea     NAME(a5)
        _OpenDeskAcc
        move.w  (sp)+, REF(a5)
        bsr     ported
        move.w  d0, PORTED(a5)
        clr.l   -(sp)
        _FrontWindow
        movea.l (sp)+, a0
        move.l  a0, DAWIN(a5)
        move.w  108(a0), KIND(a5)
        bsr     events
        _SystemTask
        bsr     ported
        move.w  d0, PORT2(a5)

        | A key: but not a Command key, which is for MenuKey
        lea     REC(a5), a0
        move.w  #keyDown, (a0)
        move.l  #0x00000778, 2(a0)      | x
        move.w  #cmdKey, 14(a0)
        clr.w   -(sp)
        move.l  a0, -(sp)
        _SystemEvent
        move.w  (sp)+, EV_CMD(a5)
        clr.w   REC+14(a5)
        clr.w   -(sp)
        pea     REC(a5)
        _SystemEvent
        move.w  (sp)+, EV_KEY(a5)

        | Copy
        clr.w   -(sp)
        move.w  #3, -(sp)
        _SystemEdit
        move.w  (sp)+, EDIT_DA(a5)

        | A click in its window
        clr.w   -(sp)
        move.l  #IN_TALLY, -(sp)
        pea     FOUNDW(a5)
        _FindWindow
        move.w  (sp)+, FOUND(a5)
        lea     REC(a5), a0
        move.w  #mouseDown, (a0)
        clr.l   2(a0)
        move.l  #IN_TALLY, 10(a0)
        move.l  a0, -(sp)
        move.l  FOUNDW(a5), -(sp)
        _SystemClick

        | Command-T, from its menu
        clr.l   -(sp)
        move.w  #0x54, -(sp)
        _MenuKey
        move.l  (sp)+, MENUKEY(a5)

        | Desk's window in front: none of it's Tally's
        move.l  WINDOW(a5), -(sp)
        _SelectWindow
        bsr     events
        clr.w   -(sp)
        move.w  #3, -(sp)
        _SystemEdit
        move.w  (sp)+, EDIT_APP(a5)
        _SystemTask
        move.w  CURSORS(a5), CURSORS2(a5)

        | Closed, opened again, and still open when Desk quits
        move.w  REF(a5), -(sp)
        _CloseDeskAcc
        movea.l UTableBase, a0
        move.l  4*12(a0), UNITGONE(a5)
        clr.l   -(sp)
        _FrontWindow
        move.l  (sp)+, FRONT_AFTER(a5)
        clr.w   -(sp)
        pea     NAME(a5)
        _OpenDeskAcc
        addq.l  #2, sp
        _ExitToShell

| D0: 1 if thePort is Desk's window, else 0.
ported:
        movea.l (a5), a0
        moveq   #0, d0
        move.l  (a0), d1
        cmp.l   WINDOW(a5), d1
        bne.s   1f
        moveq   #1, d0
1:      rts

| Twenty turns of GetNextEvent: counting any it gives Desk for Tally's
| window, and doing Desk's own updates.
events:
        moveq   #19, d3
1:      clr.w   -(sp)
        move.w  #0xFFFF, -(sp)
        pea     EVENT(a5)
        _GetNextEvent
        move.w  (sp)+, d0
        beq.s   3f
        move.l  EVENT+2(a5), d0
        cmp.l   DAWIN(a5), d0
        bne.s   2f
        addq.w  #1, SEEN(a5)
2:      cmpi.w  #updateEvt, EVENT(a5)
        bne.s   3f
        move.l  EVENT+2(a5), -(sp)
        _BeginUpdate
        move.l  EVENT+2(a5), -(sp)
        _EndUpdate
3:      dbra    d3, 1b
        rts

apple:  .byte   1
        .byte   0x14
        .even
