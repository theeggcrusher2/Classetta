| Dialogs: a modal dialog from DLOG 128, then a stop alert from ALRT 129,
| as an application would use them.  The dialog has OK and Cancel buttons,
| static text with a ParamText string in it, an edit field, a check box,
| a user item the application draws and an item it hides.  ModalDialog
| runs until OK or Cancel; the check box toggles meanwhile.  What the
| user picked, and the edit field's text, are left in the globals.

        .include "traps.inc"

QD          = -4
DIALOG      = -230
ITEM        = -232              | ModalDialog's last item
COUNT       = -234              | how many times it's returned
TYPE        = -236              | GetDialogItem's
HANDLE      = -240
RECT        = -248
TEXT        = -504              | the edit field's text when the dialog closed
ALERTED     = -506              | StopAlert's item
STAGE       = -508              | 1 in the dialog, 2 in the alert, 3 done
USERCALLS   = -510              | calls to the user item's procedure
EVENT       = -526
GLOBALS     = 526

| GetDialogItem(DIALOG, n, TYPE, HANDLE, RECT)
        .macro  item n
        move.l  DIALOG(a5),-(sp)
        move.w  #\n,-(sp)
        pea     TYPE(a5)
        pea     HANDLE(a5)
        pea     RECT(a5)
        _GetDialogItem
        .endm

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

        pea     world_s(pc)             | ParamText("World", NIL, NIL, NIL)
        clr.l   -(sp)
        clr.l   -(sp)
        clr.l   -(sp)
        _ParamText
        clr.l   -(sp)
        move.w  #128,-(sp)
        clr.l   -(sp)
        move.l  #-1,-(sp)
        _GetNewDialog
        move.l  (sp)+,DIALOG(a5)
        move.l  DIALOG(a5),-(sp)
        _SetPort

        item    6                       | the user item: its procedure
        move.l  DIALOG(a5),-(sp)
        move.w  #6,-(sp)
        move.w  TYPE(a5),-(sp)
        pea     user(pc)
        pea     RECT(a5)
        _SetDialogItem
        item    4                       | the edit field: new text
        move.l  HANDLE(a5),-(sp)
        pea     edit_s(pc)
        _SetDialogItemText
        move.l  DIALOG(a5),-(sp)
        move.w  #7,-(sp)
        _HideDialogItem
        clr.w   -(sp)                   | SetDialogCancelItem(DIALOG, 2)
        move.l  DIALOG(a5),-(sp)
        move.w  #2,-(sp)
        move.w  #0x0305,d0
        _DialogDispatch
        addq.l  #2,sp
        move.w  #1,STAGE(a5)

modal:
        clr.l   -(sp)
        pea     ITEM(a5)
        _ModalDialog
        addq.w  #1,COUNT(a5)
        move.w  ITEM(a5),d0
        cmp.w   #5,d0
        beq.s   toggle
        cmp.w   #2,d0
        bhi.s   modal
        bra.s   done

toggle:
        item    5
        clr.w   -(sp)
        move.l  HANDLE(a5),-(sp)
        _GetControlValue
        move.w  (sp)+,d0
        eori.w  #1,d0
        move.l  HANDLE(a5),-(sp)
        move.w  d0,-(sp)
        _SetControlValue
        bra.s   modal

done:
        item    4
        move.l  HANDLE(a5),-(sp)
        pea     TEXT(a5)
        _GetDialogItemText
        move.l  DIALOG(a5),-(sp)
        _DisposeDialog
        move.w  #2,STAGE(a5)

        clr.l   -(sp)                   | ParamText(NIL, "done", NIL, NIL)
        pea     done_s(pc)
        clr.l   -(sp)
        clr.l   -(sp)
        _ParamText
        clr.w   -(sp)
        move.w  #129,-(sp)
        clr.l   -(sp)
        _StopAlert
        move.w  (sp)+,ALERTED(a5)
        move.w  #3,STAGE(a5)

idle:
        clr.w   -(sp)
        move.w  #0xFFFF,-(sp)
        pea     EVENT(a5)
        move.l  #30,-(sp)
        clr.l   -(sp)
        _WaitNextEvent
        addq.l  #2,sp
        bra.s   idle

| pascal void user(DialogPtr d, short item): paint the item's rectangle.
user:
        link    a6,#-14                 | type -2, handle -6, rect -14
        addq.w  #1,USERCALLS(a5)
        move.l  10(a6),-(sp)
        move.w  8(a6),-(sp)
        pea     -2(a6)
        pea     -6(a6)
        pea     -14(a6)
        _GetDialogItem
        pea     -14(a6)
        _PaintRect
        unlk    a6
        movea.l (sp)+,a0
        addq.l  #6,sp
        jmp     (a0)

world_s:    .byte 5
            .ascii "World"
edit_s:     .byte 7
            .ascii "Edit me"
done_s:     .byte 4
            .ascii "done"
            .even
