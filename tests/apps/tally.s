| Tally: the desk accessory Desk opens, DRVR 12 "\0Tally".  Its window
| and its menu, TALLY_MENU with one item, Count (Command-T), come and go
| with it.  It counts what the Desk Manager sends it in Desk's globals,
| through CurrentA5, and sets its own port each time, for the Desk
| Manager to put Desk's back.

        .include "traps.inc"
        .include "desk.inc"

csCode          = 26
csParam         = 28
dCtlStorage     = 20            | its menu
dCtlRefNum      = 24
dCtlWindow      = 30
windowKind      = 108
JIODone         = 0x8FC
CurrentA5       = 0x904

        .text
header:
        .short  0x6400          | dNeedLock, dNeedTime; control calls
        .short  0               | time at every SystemTask
        .short  0x016A          | mouseDown, keyDown, autoKey, update and activate events
        .short  TALLY_MENU
        .short  on_open-header, on_other-header, on_control-header, on_other-header
        .short  on_close-header
        .byte   6
        .byte   0
        .ascii  "Tally"
        .even

on_open:
        movem.l a0-a4, -(sp)
        movea.l a1, a3
        movea.l CurrentA5, a4
        addq.w  #1, OPENS(a4)
        tst.l   dCtlWindow(a3)
        bne.s   1f
        clr.l   -(sp)
        clr.l   -(sp)                   | its storage, in the heap
        pea     bounds(pc)
        pea     title(pc)
        move.w  #0x0100, -(sp)          | visible
        move.w  #4, -(sp)               | noGrowDocProc
        moveq   #-1, d0
        move.l  d0, -(sp)               | in front
        move.w  #0x0100, -(sp)          | a close box
        clr.l   -(sp)
        _NewWindow
        movea.l (sp)+, a0
        move.w  dCtlRefNum(a3), windowKind(a0)
        move.l  a0, dCtlWindow(a3)
        clr.l   -(sp)
        move.w  #TALLY_MENU, -(sp)
        pea     title(pc)
        _NewMenu
        move.l  (sp), dCtlStorage(a3)
        pea     items(pc)
        _AppendMenu
        move.l  dCtlStorage(a3), -(sp)
        clr.w   -(sp)
        _InsertMenu
        _DrawMenuBar
1:      movem.l (sp)+, a0-a4
        moveq   #0, d0
        rts

on_control:
        movem.l a0-a4, -(sp)
        movea.l a0, a2
        movea.l a1, a3
        movea.l CurrentA5, a4
        move.l  dCtlWindow(a3), -(sp)
        _SetPort
        move.w  csCode(a2), d0
        cmpi.w  #64, d0                 | accEvent
        beq.s   event
        cmpi.w  #65, d0                 | accRun
        bne.s   1f
        addq.w  #1, RUNS(a4)
        bra.s   done
1:      cmpi.w  #66, d0                 | accCursor
        bne.s   2f
        addq.w  #1, CURSORS(a4)
        bra.s   done
2:      cmpi.w  #67, d0                 | accMenu
        bne.s   3f
        move.l  csParam(a2), PICKED(a4)
        clr.w   -(sp)
        _HiliteMenu
        bra.s   done
3:      move.w  d0, EDITED(a4)          | accUndo to accClear
        bra.s   done

event:
        movea.l csParam(a2), a0
        move.w  (a0), d0
        cmpi.w  #3, d0                  | keyDown
        bne.s   1f
        addq.w  #1, KEYS(a4)
        move.w  4(a0), LASTKEY(a4)
        bra.s   done
1:      cmpi.w  #6, d0                  | update
        bne.s   2f
        addq.w  #1, UPDATES(a4)
        move.l  dCtlWindow(a3), -(sp)
        _BeginUpdate
        move.l  dCtlWindow(a3), -(sp)
        _EndUpdate
        bra.s   done
2:      cmpi.w  #8, d0                  | activate, or deactivate
        bne.s   4f
        btst    #0, 15(a0)              | activeFlag
        beq.s   3f
        addq.w  #1, ACTIVATES(a4)
        bra.s   done
3:      addq.w  #1, DEACTIVATES(a4)
        bra.s   done
4:      cmpi.w  #1, d0                  | mouseDown
        bne.s   done
        addq.w  #1, CLICKS(a4)
done:
        movem.l (sp)+, a0-a4
        moveq   #0, d0
        move.l  JIODone, -(sp)
        rts

on_close:
        movem.l a0-a4, -(sp)
        movea.l a1, a3
        movea.l CurrentA5, a4
        addq.w  #1, CLOSES(a4)
        move.w  #TALLY_MENU, -(sp)
        _DeleteMenu
        move.l  dCtlStorage(a3), -(sp)
        _DisposeMenu
        clr.l   dCtlStorage(a3)
        _DrawMenuBar
        move.l  dCtlWindow(a3), -(sp)
        _DisposeWindow
        clr.l   dCtlWindow(a3)
        movem.l (sp)+, a0-a4
        moveq   #0, d0
        rts

on_other:                               | not asked: its flags don't say it can be
        moveq   #-17, d0
        rts

bounds: .short  100, 320, 160, 440
title:  .byte   5
        .ascii  "Tally"
items:  .byte   7
        .ascii  "Count/T"
        .even
