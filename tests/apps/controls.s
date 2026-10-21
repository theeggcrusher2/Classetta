| Controls: a window with a push button, a check box, two radio buttons
| and a scroll bar, run as an application would run them.  The button
| counts its clicks, the check box toggles, the radio buttons take turns,
| and the scroll bar's arrows and gray area scroll it through an action
| procedure.

        .include "traps.inc"

QD          = -4
EVENT       = -226
WINDOW      = -230
WHICH       = -234
BUTTON      = -238
CHECK       = -242
RADIO1      = -246
RADIO2      = -250
SCROLL      = -254
CLICKS      = -256              | push button clicks
ACTIONS     = -258              | calls to the scroll bar's action procedure
PT          = -262              | the click, local
CTL         = -266              | FindControl's control
GLOBALS     = 266

mouseDown   = 1
updateEvt   = 6
inContent   = 3
inUpButton  = 20
inDownButton = 21
inPageUp    = 22
inThumb     = 129
pushButProc = 0
checkBoxProc = 1
radioButProc = 2

| NewControl(WINDOW, rect, title, visible, 0, 0, max, proc, 0) -> dest
        .macro  control rect, title, max, proc, dest
        clr.l   -(sp)
        move.l  WINDOW(a5),-(sp)
        pea     \rect(pc)
        pea     \title(pc)
        move.b  #1,-(sp)
        clr.w   -(sp)
        clr.w   -(sp)
        move.w  #\max,-(sp)
        move.w  #\proc,-(sp)
        clr.l   -(sp)
        _NewControl
        move.l  (sp)+,\dest(a5)
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

        clr.l   -(sp)
        move.w  #128,-(sp)
        clr.l   -(sp)
        move.l  #-1,-(sp)
        _GetNewWindow
        move.l  (sp)+,WINDOW(a5)
        move.l  WINDOW(a5),-(sp)
        _SetPort

        control button_r, button_t, 1, pushButProc, BUTTON
        control check_r, check_t, 1, checkBoxProc, CHECK
        control radio1_r, radio1_t, 1, radioButProc, RADIO1
        control radio2_r, radio2_t, 1, radioButProc, RADIO2
        clr.l   -(sp)                   | the scroll bar is CNTL 128
        move.w  #128,-(sp)
        move.l  WINDOW(a5),-(sp)
        _GetNewControl
        move.l  (sp)+,SCROLL(a5)
        clr.w   CLICKS(a5)
        clr.w   ACTIONS(a5)

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
        move.l  WINDOW(a5),-(sp)
        _DrawControls
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
        clr.w   -(sp)
        move.l  PT(a5),-(sp)
        move.l  WINDOW(a5),-(sp)
        pea     CTL(a5)
        _FindControl
        move.w  (sp)+,d0
        beq     loop
        move.l  CTL(a5),d3
        cmp.l   SCROLL(a5),d3
        beq.s   scroll
        clr.w   -(sp)
        move.l  d3,-(sp)
        move.l  PT(a5),-(sp)
        clr.l   -(sp)
        _TrackControl
        move.w  (sp)+,d0
        beq     loop
        cmp.l   BUTTON(a5),d3
        beq.s   pressed
        cmp.l   CHECK(a5),d3
        beq.s   toggle
        | A radio button: it goes on, the other off.
        move.l  RADIO1(a5),d4
        cmp.l   d4,d3
        bne.s   1f
        move.l  RADIO2(a5),d4
1:      move.l  d3,-(sp)
        move.w  #1,-(sp)
        _SetControlValue
        move.l  d4,-(sp)
        clr.w   -(sp)
        _SetControlValue
        bra     loop

pressed:
        addq.w  #1,CLICKS(a5)
        bra     loop

toggle:
        clr.w   -(sp)
        move.l  d3,-(sp)
        _GetControlValue
        move.w  (sp)+,d0
        eori.w  #1,d0
        move.l  d3,-(sp)
        move.w  d0,-(sp)
        _SetControlValue
        bra     loop

scroll:
        clr.w   -(sp)
        move.l  d3,-(sp)
        move.l  PT(a5),-(sp)
        cmp.w   #inThumb,d0
        beq.s   1f
        pea     scrolled(pc)
        bra.s   2f
1:      clr.l   -(sp)                   | the thumb: no action procedure
2:      _TrackControl
        addq.l  #2,sp
        bra     loop

| pascal void scrolled(ControlHandle c, short part): a line or a page.
scrolled:
        link    a6,#0
        movem.l d3/a2,-(sp)
        addq.w  #1,ACTIONS(a5)
        move.w  8(a6),d0
        movea.l 10(a6),a2
        moveq   #-1,d3
        cmp.w   #inUpButton,d0
        beq.s   1f
        moveq   #1,d3
        cmp.w   #inDownButton,d0
        beq.s   1f
        moveq   #-5,d3
        cmp.w   #inPageUp,d0
        beq.s   1f
        moveq   #5,d3
1:      clr.w   -(sp)
        move.l  a2,-(sp)
        _GetControlValue
        add.w   (sp)+,d3
        move.l  a2,-(sp)
        move.w  d3,-(sp)
        _SetControlValue                | kept between the minimum and maximum
        movem.l (sp)+,d3/a2
        unlk    a6
        movea.l (sp)+,a0
        addq.l  #6,sp
        jmp     (a0)

button_r:   .short 20, 20, 40, 90
radio1_r:   .short 75, 20, 93, 100
radio2_r:   .short 95, 20, 113, 100
check_r:    .short 50, 20, 68, 150
button_t:   .byte 4
            .ascii "Beep"
check_t:    .byte 8
            .ascii "Check me"
radio1_t:   .byte 3
            .ascii "One"
radio2_t:   .byte 3
            .ascii "Two"
            .even
