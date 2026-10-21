| ResStub: patches _ResourceStub, the Resource Manager's way of reading
| a resource in, and gets three resources through it: STR 128, which the
| patch leaves to the original; STR 129, which the patch makes itself;
| and STR 130 with ResLoad off, which it then loads.  The patch counts
| its calls and keeps what the Resource Manager gave it.

        .include "traps.inc"

ORIGINAL        = -4            | _ResourceStub's own address
MAP             = -8            | A4 at the first call: the map's handle
TOP             = -12           | TopMapHndl then
READ            = -16           | the start of STR 128, read by the original
MADE            = -20           | of STR 129, made by the patch
LOADED          = -24           | of STR 130, got with ResLoad off and loaded
EMPTY_MP        = -28           | its master pointer then
CALLS           = -30           | the patch's calls
ENTRY_ID        = -32           | the ID in the entry A2 points at, first call
SAME            = -34           | -1 if A3 was A2 at every call
KEPT            = -36           | -1 if A2-A4 came back from GetResource as they went in
RESLOADS        = -40           | ResLoad at each of the first four calls, a byte each
GLOBALS         = 40

RESOURCE_STUB   = 0xA0FC
ResLoad         = 0xA5E
TopMapHndl      = 0xA50
STR             = 0x53545220    | 'STR '
MARK            = 0x12345678

start:
        move.w  #RESOURCE_STUB, d0
        _GetOSTrapAddress
        move.l  a0, ORIGINAL(a5)
        move.w  #-1, SAME(a5)
        lea     patch(pc), a0
        move.w  #RESOURCE_STUB, d0
        _SetOSTrapAddress
        move.l  TopMapHndl, TOP(a5)

        movea.l #MARK, a2
        movea.l a2, a3
        movea.l a2, a4
        move.w  #128, d0
        bsr.s   get
        movea.l (a0), a0
        move.l  (a0), READ(a5)
        moveq   #0, d0
        cmpa.l  #MARK, a2
        bne.s   1f
        cmpa.l  #MARK, a3
        bne.s   1f
        cmpa.l  #MARK, a4
        bne.s   1f
        moveq   #-1, d0
1:      move.w  d0, KEPT(a5)

        move.w  #129, d0
        bsr.s   get
        movea.l (a0), a0
        move.l  (a0), MADE(a5)

        clr.w   -(sp)                   | SetResLoad(FALSE)
        _SetResLoad
        move.w  #130, d0
        bsr.s   get
        move.l  (a0), EMPTY_MP(a5)
        move.l  a0, -(sp)
        move.l  a0, -(sp)
        _LoadResource
        movea.l (sp)+, a0
        movea.l (a0), a0
        move.l  (a0), LOADED(a5)
        move.w  #0x0100, -(sp)          | SetResLoad(TRUE)
        _SetResLoad

        movea.l ORIGINAL(a5), a0
        move.w  #RESOURCE_STUB, d0
        _SetOSTrapAddress
        _ExitToShell

| GetResource('STR ', D0) in A0
get:
        subq.l  #4, sp
        move.l  #STR, -(sp)
        move.w  d0, -(sp)
        _GetResource
        movea.l (sp)+, a0
        rts

| The patch: A4 the map's handle, A2 and A3 the reference list entry; the
| resource's handle goes back in A0.  It runs during the application's
| own GetResource, so A5 is the application's.
patch:
        addq.w  #1, CALLS(a5)
        move.w  CALLS(a5), d0
        cmpi.w  #4, d0
        bhi.s   1f
        lea     RESLOADS-1(a5), a0
        move.b  ResLoad, 0(a0,d0.w)
1:      cmpi.w  #1, d0
        bne.s   2f
        move.l  a4, MAP(a5)
        move.w  (a2), ENTRY_ID(a5)
2:      cmpa.l  a2, a3
        beq.s   3f
        clr.w   SAME(a5)
3:      cmpi.w  #129, (a2)
        beq.s   make
        move.l  ORIGINAL(a5), -(sp)     | the rest is the original's
        rts

make:
        moveq   #5, d0
        _NewHandle
        movea.l (a0), a1
        move.b  #4, (a1)+
        move.b  #'m', (a1)+
        move.b  #'a', (a1)+
        move.b  #'d', (a1)+
        move.b  #'e', (a1)+
        move.l  a0, 8(a2)               | the entry's handle
        rts
