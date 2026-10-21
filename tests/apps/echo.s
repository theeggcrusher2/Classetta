| Echo: the driver Driver opens, DRVR 20 ".Echo".  A write keeps up to
| 16 bytes and a read gives them back.  A control call with csCode 50 is
| left going until an immediate status call finishes it, with ioErr; the
| status routine goes through IODone for it.

        .include "traps.inc"

ioTrap          = 6
csCode          = 26
ioBuffer        = 32
ioReqCount      = 36
ioActCount      = 40
dCtlStorage     = 20
JIODone         = 0x8FC
PENDING         = 16            | in the storage, after the bytes: the control call left going
STORAGE         = 20

        .text
header:
        .short  0x4F00          | dNeedLock; reads, writes, control and status calls
        .short  0, 0, 0         | no delay, event mask or menu
        .short  on_open-header, on_prime-header, on_control-header, on_status-header, on_close-header
        .byte   5
        .ascii  ".Echo"
        .even

on_open:
        movem.l a0-a1, -(sp)
        moveq   #STORAGE, d0
        _NewPtrSysClear
        move.l  a0, d1
        movem.l (sp)+, a0-a1
        move.l  d1, dCtlStorage(a1)
        rts                     | NewPtr's result

on_prime:
        move.l  a3, -(sp)
        movea.l dCtlStorage(a1), a2
        movea.l ioBuffer(a0), a3
        move.l  ioReqCount(a0), d1
        cmpi.l  #16, d1
        ble.s   1f
        moveq   #16, d1
1:      move.l  d1, ioActCount(a0)
        cmpi.b  #3, ioTrap+1(a0)        | a write: from the buffer
        bne.s   3f
        exg     a2, a3
        bra.s   3f
2:      move.b  (a2)+, (a3)+
3:      dbra    d1, 2b
        movea.l (sp)+, a3
        moveq   #0, d0
        move.l  JIODone, -(sp)
        rts

on_control:
        movea.l dCtlStorage(a1), a2
        cmpi.w  #50, csCode(a0)
        bne.s   1f
        move.l  a0, PENDING(a2)         | for the status routine to finish
        rts
1:      cmpi.w  #1, csCode(a0)          | killCode, immediate: it's not going any more
        bne.s   2f
        clr.l   PENDING(a2)
        moveq   #0, d0
        rts
2:      moveq   #-17, d0                | controlErr
        move.l  JIODone, -(sp)
        rts

on_status:                              | called immediately
        movea.l dCtlStorage(a1), a2
        tst.l   PENDING(a2)
        beq.s   1f
        clr.l   PENDING(a2)
        move.l  a0, -(sp)
        moveq   #-36, d0                | ioErr
        movea.l JIODone, a2
        jsr     (a2)
        movea.l (sp)+, a0
1:      moveq   #0, d0
        rts

on_close:
        move.l  a0, -(sp)
        movea.l dCtlStorage(a1), a0
        _DisposPtr
        clr.l   dCtlStorage(a1)
        movea.l (sp)+, a0
        moveq   #0, d0
        rts
