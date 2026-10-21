| Launcher: answers a Gestalt selector of its own, asks for it, then
| launches Hello in its place, as a program launcher would.

        .include "traps.inc"

NEW_ERR         = -2            | NewGestalt's result
GESTALT_ERR     = -4            | Gestalt's
GESTALT_VALUE   = -8            | and its response
GLOBALS         = 8

SELECTOR        = 0x6D637274    | 'mcrt'

start:
        move.l  #SELECTOR, d0
        lea     answer(pc), a0
        _NewGestalt
        move.w  d0, NEW_ERR(a5)
        move.l  #SELECTOR, d0
        _Gestalt
        move.w  d0, GESTALT_ERR(a5)
        move.l  a0, GESTALT_VALUE(a5)

        | The 64K ROM's parameter block: the name and a configuration
        | word, then (zero) where a LaunchParamBlockRec says 'LC'.
        clr.l   -(sp)
        clr.w   -(sp)
        pea     hello(pc)
        movea.l sp, a0
        _Launch
        _ExitToShell                    | Launch came back: it failed

| pascal OSErr answer(OSType selector, long *response)
answer:
        movea.l 4(sp), a0
        move.l  #42, (a0)
        movea.l (sp)+, a0
        addq.l  #8, sp
        clr.w   (sp)
        jmp     (a0)

hello:  .byte   5
        .ascii  "Hello"
        .even
