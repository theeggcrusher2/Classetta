| OSPatch: patches _GetHandleSize, an OS trap, twice.  The first patch
| keeps what the trap dispatcher gave it, in registers and on the stack,
| spoils the registers the dispatcher saved, and goes on to the original;
| the second goes back to the caller itself, taking the dispatcher's
| frame off the stack, as Microsoft Word's does.

        .include "traps.inc"

ORIGINAL        = -4            | _GetHandleSize's own address
HANDLE          = -8            | a 10-byte handle
ENTRY_D1        = -10           | D1.w at the first patch's entry: the trap word
ENTRY_D2        = -12           | D2.w: the trap's place in the table, times 4
ENTRY_A2        = -16           | A2: the patch's address
FRAME_A0        = -20           | A0 as the dispatcher stacked it
FRAME_A1        = -24           | A1
FRAME_WORD      = -26           | the trap word
FRAME_PC        = -30           | the address to go back to
BACK            = -34           | which should be this
SIZE            = -38           | the handle's size, through the first patch
KEPT            = -40           | -1 if A0-A2, D1 and D2 came back as they went in
FLAGS           = -42           | -1 if the flags were D0's: zero, then not
MADE            = -46           | D0 from the second patch
KEPT_TOO        = -48           | -1 if the registers and SP came back from that one
LOOK            = -52           | the first patch's address
GLOBALS         = 52

GET_HANDLE_SIZE = 0xA025
MARK            = 0x12345678

start:
        move.w  #GET_HANDLE_SIZE, d0
        _GetOSTrapAddress
        move.l  a0, ORIGINAL(a5)
        moveq   #10, d0
        _NewHandle
        move.l  a0, HANDLE(a5)

        lea     look(pc), a0
        move.l  a0, LOOK(a5)
        move.w  #GET_HANDLE_SIZE, d0
        _SetOSTrapAddress
        moveq   #0, d0
        _NewHandle                      | an empty one
        _GetHandleSize
        seq     d4
        bsr.s   marks
        movea.l HANDLE(a5), a0
        _GetHandleSize
back:   sne     d3
        and.b   d4, d3
        ext.w   d3
        move.w  d3, FLAGS(a5)
        move.l  d0, SIZE(a5)
        bsr.s   check
        move.w  d0, KEPT(a5)
        lea     back(pc), a0
        move.l  a0, BACK(a5)

        lea     own(pc), a0
        move.w  #GET_HANDLE_SIZE, d0
        _SetOSTrapAddress
        bsr.s   marks
        movea.l HANDLE(a5), a0
        movea.l sp, a3
        _GetHandleSize
        move.l  d0, MADE(a5)
        bsr.s   check
        cmpa.l  sp, a3
        beq.s   1f
        moveq   #0, d0
1:      move.w  d0, KEPT_TOO(a5)

        movea.l ORIGINAL(a5), a0
        move.w  #GET_HANDLE_SIZE, d0
        _SetOSTrapAddress
        _ExitToShell

marks:
        movea.l #MARK, a1
        movea.l a1, a2
        move.l  a1, d1
        move.l  a1, d2
        rts

| D0.w -1 if A0 is the handle and A1, A2, D1 and D2 are MARK, else 0
check:
        moveq   #0, d0
        cmpa.l  HANDLE(a5), a0
        bne.s   1f
        cmpa.l  #MARK, a1
        bne.s   1f
        cmpa.l  #MARK, a2
        bne.s   1f
        cmpi.l  #MARK, d1
        bne.s   1f
        cmpi.l  #MARK, d2
        bne.s   1f
        moveq   #-1, d0
1:      rts

| Under it: A0, A1, D1, D2 and A2 as they were, the trap word, SR and the
| address to go back to
look:
        move.w  d1, ENTRY_D1(a5)
        move.w  d2, ENTRY_D2(a5)
        move.l  a2, ENTRY_A2(a5)
        move.l  4(sp), FRAME_A0(a5)
        move.l  8(sp), FRAME_A1(a5)
        move.w  24(sp), FRAME_WORD(a5)
        move.l  28(sp), FRAME_PC(a5)
        suba.l  a1, a1                  | the dispatcher puts these back
        moveq   #0, d2
        suba.l  a2, a2
        move.l  ORIGINAL(a5), -(sp)     | the rest is the original's
        rts

own:
        addq.l  #4, sp                  | the dispatcher's return,
        movea.l (sp)+, a0               | and what it saved
        movea.l (sp)+, a1
        move.l  (sp)+, d1
        move.l  (sp)+, d2
        movea.l (sp)+, a2
        addq.l  #4, sp                  | the trap word and SR
        move.l  #0x1234, d0
        rts
