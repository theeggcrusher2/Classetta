| Edit: a small TextEdit application in 68000 assembly.  One window holds
| an edit record, which it types into, clicks in (shift extends), updates,
| activates and blinks the insertion point of, as the sample applications
| in Inside Macintosh do.  Edit's Cut, Copy and Paste go through the
| TextEdit scrap and, as TEToScrap and TEFromScrap would, the desk scrap,
| with which the scrap is also brought in on a resume event.
|
| Assembled by tests/apps/build.py with GNU as for m68k.

        .include "traps.inc"

| Globals, below A5.  The QuickDraw globals end at -4(A5).
QD          = -4                | InitGraf(@thePort)
EVENT       = -226              | EventRecord: what, message, when, where, modifiers
WINDOW      = -230              | the window
WHICH       = -234              | FindWindow's window
TEH         = -238              | the edit record
WHERE       = -242              | a click, local
OFFSET      = -246              | GetScrap's offset
GLOBALS     = 246               | below A5, in all

| Low memory: TextEdit's scrap
TEScrpLength = 0xAB0
TEScrpHandle = 0xAB4

| Event Manager
mouseDown   = 1
keyDown     = 3
autoKey     = 5
updateEvt   = 6
activateEvt = 8
osEvt       = 15
activeFlag  = 1
cmdKey      = 0x0100
shiftKey    = 0x0200
| FindWindow
inMenuBar   = 1
inContent   = 3
| Menus
APPLE_MENU  = 128
FILE_MENU   = 129
EDIT_MENU   = 130
QUIT_ITEM   = 1
CUT_ITEM    = 3
COPY_ITEM   = 4
PASTE_ITEM  = 5
CLEAR_ITEM  = 6
MONACO      = 4
TEXT        = 0x54455854        | 'TEXT'

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

        | Menus: Apple, File and Edit.
        moveq   #2,d3
        move.w  #APPLE_MENU,d4
menus:
        clr.l   -(sp)
        move.w  d4,-(sp)
        _GetMenu
        clr.w   -(sp)
        _InsertMenu
        addq.w  #1,d4
        dbra    d3,menus
        _DrawMenuBar

        | The window, WIND 128, and in it an edit record in Monaco 9.
        clr.l   -(sp)
        move.w  #128,-(sp)
        clr.l   -(sp)
        move.l  #-1,-(sp)
        _GetNewWindow
        move.l  (sp)+,WINDOW(a5)
        move.l  WINDOW(a5),-(sp)
        _SetPort
        move.w  #MONACO,-(sp)
        _TextFont
        move.w  #9,-(sp)
        _TextSize
        clr.l   -(sp)
        pea     textrect(pc)            | destRect
        pea     textrect(pc)            | viewRect
        _TENew
        move.l  (sp)+,TEH(a5)

loop:
        move.l  TEH(a5),-(sp)
        _TEIdle
        clr.w   -(sp)
        move.w  #0xFFFF,-(sp)           | everyEvent
        pea     EVENT(a5)
        moveq   #2,d0
        move.l  d0,-(sp)                | sleep
        clr.l   -(sp)                   | mouseRgn
        _WaitNextEvent
        move.b  (sp)+,d0
        beq.s   loop
        move.w  EVENT(a5),d0
        cmp.w   #updateEvt,d0
        beq.s   update
        cmp.w   #mouseDown,d0
        beq     mouse
        cmp.w   #keyDown,d0
        beq     key
        cmp.w   #autoKey,d0
        beq     key
        cmp.w   #activateEvt,d0
        beq.s   activate
        cmp.w   #osEvt,d0
        beq.s   resume
        bra.s   loop

| --- updateEvt: erase the window and draw the text -------------------------
update:
        move.l  EVENT+2(a5),-(sp)
        _BeginUpdate
        move.l  WINDOW(a5),a0
        pea     16(a0)                  | portRect
        _EraseRect
        move.l  WINDOW(a5),a0
        pea     16(a0)
        move.l  TEH(a5),-(sp)
        _TEUpdate
        move.l  EVENT+2(a5),-(sp)
        _EndUpdate
        bra     loop

| --- activateEvt: the record goes with the window ---------------------------
activate:
        move.l  TEH(a5),-(sp)
        btst    #0,EVENT+15(a5)         | activeFlag, in the modifiers' low byte
        beq.s   deactivate
        _TEActivate
        bra     loop
deactivate:
        _TEDeactivate
        bra     loop

| --- osEvt: on a resume, the desk scrap if it's changed ----------------------
resume:
        move.l  EVENT+2(a5),d0          | message: suspendResumeMessage, flags
        cmp.b   #1,EVENT+2(a5)
        bne     loop
        btst    #1,d0                   | convertClipboard
        beq     loop
        bsr     fromscrap
        bra     loop

| --- mouseDown --------------------------------------------------------------
mouse:
        clr.w   -(sp)
        move.l  EVENT+10(a5),-(sp)      | where
        pea     WHICH(a5)
        _FindWindow
        move.w  (sp)+,d0
        cmp.w   #inMenuBar,d0
        beq.s   menubar
        cmp.w   #inContent,d0
        beq.s   content
        bra     loop

menubar:
        clr.l   -(sp)
        move.l  EVENT+10(a5),-(sp)
        _MenuSelect
        move.l  (sp)+,d0
        bra.s   command

content:
        move.l  EVENT+10(a5),WHERE(a5)
        pea     WHERE(a5)
        _GlobalToLocal
        move.l  WHERE(a5),-(sp)         | pt
        move.w  EVENT+14(a5),d0
        and.w   #shiftKey,d0
        sne     d0
        and.b   #1,d0
        move.b  d0,-(sp)                | extend: shift-click
        move.l  TEH(a5),-(sp)
        _TEClick
        bra     loop

| --- keyDown: Command keys are menu commands, the rest go to TextEdit ------
key:
        move.w  EVENT+14(a5),d0         | modifiers
        and.w   #cmdKey,d0
        bne.s   cmdkey
        move.w  EVENT+4(a5),d0          | message: the character in the low byte
        and.w   #0xFF,d0
        move.w  d0,-(sp)
        move.l  TEH(a5),-(sp)
        _TEKey
        bra     loop

cmdkey:
        clr.l   -(sp)
        move.w  EVENT+4(a5),d0
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
        bne.s   editmenu
        cmp.w   #QUIT_ITEM,d0
        beq     quit
        bra.s   unhilite
editmenu:
        cmp.w   #EDIT_MENU,d1
        bne.s   unhilite
        move.l  TEH(a5),-(sp)
        cmp.w   #CUT_ITEM,d0
        beq.s   cut
        cmp.w   #COPY_ITEM,d0
        beq.s   copy
        cmp.w   #PASTE_ITEM,d0
        beq.s   paste
        cmp.w   #CLEAR_ITEM,d0
        beq.s   clear
        addq.l  #4,sp
        bra.s   unhilite
cut:
        _TECut
        bsr.s   toscrap
        bra.s   unhilite
copy:
        _TECopy
        bsr.s   toscrap
        bra.s   unhilite
paste:
        bsr.s   fromscrap
        _TEPaste
        bra.s   unhilite
clear:
        _TEDelete
unhilite:
        clr.w   -(sp)
        _HiliteMenu
        bra     loop

quit:
        _ExitToShell

| --- TEToScrap and TEFromScrap, as the interfaces' glue does them ----------
toscrap:
        clr.l   -(sp)
        _ZeroScrap
        addq.l  #4,sp
        clr.l   -(sp)
        moveq   #0,d0
        move.w  TEScrpLength,d0
        move.l  d0,-(sp)                | length
        move.l  #TEXT,-(sp)
        move.l  TEScrpHandle,a0
        move.l  (a0),-(sp)              | source
        _PutScrap
        addq.l  #4,sp
        rts

fromscrap:
        clr.l   -(sp)
        move.l  TEScrpHandle,-(sp)      | hDest
        move.l  #TEXT,-(sp)
        pea     OFFSET(a5)
        _GetScrap
        move.l  (sp)+,d0
        bmi.s   1f
        move.w  d0,TEScrpLength
1:      rts

textrect:   .short 4, 4, 156, 256       | top, left, bottom, right
