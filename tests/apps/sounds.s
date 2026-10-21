| Sounds: the Sound Driver's three synthesizers, and the sound buffer
| written straight.  A square-wave sound, written synchronously; a
| four-tone sound, asynchronously, with a completion routine; a
| free-form sound with a count of 0, which holds until KillIO stops it;
| and then the speaker turned on with $40 in each of the buffer's samples.

        .include "traps.inc"

PB              = -50           | a parameter block
PB2             = -100          | and another, for KillIO
SQUARE_RESULT   = -102          | the square-wave write's D0
SQUARE_TICKS    = -106          | the ticks it took
SQUARE_ACT      = -110          | its ioActCount
FT_DONE         = -112          | the four-tone write's completion routine's runs
FT_RESULT       = -114          | and its D0
FF_DONE         = -116          | the free-form write's
FF_RESULT       = -118
FF_BEFORE       = -120          | its runs before KillIO
KILL_RESULT     = -122          | KillIO's D0
FF_IORESULT     = -124          | the free-form write's ioResult after it
RECORD          = -174          | a four-tone record: 50 bytes
SYNTH           = -180          | the four-tone write: its mode and the record
FREE            = -196          | the free-form write: its mode, count and 10 bytes
WAVE            = -452          | a waveform: 0 to 255
GLOBALS         = 452

TICKS           = 0x16A
VIA             = 0x1D4
SOUND_BASE      = 0x266
CURRENT_A5      = 0x904
SOUND_DRIVER    = -4
ioCompletion    = 12
ioResult        = 16
ioRefNum        = 24
ioBuffer        = 32
ioReqCount      = 36
ioActCount      = 40
vSndEnb         = 7             | in vBufB, the VIA's register 0

start:
        | A square wave, waited for.
        lea     PB(a5), a0
        bsr     clear_pb
        lea     square(pc), a1
        move.l  a1, ioBuffer(a0)
        move.l  #square_end - square, ioReqCount(a0)
        move.l  TICKS, d3
        _Write
        move.w  d0, SQUARE_RESULT(a5)
        move.l  TICKS, d0
        sub.l   d3, d0
        move.l  d0, SQUARE_TICKS(a5)
        move.l  PB + ioActCount(a5), SQUARE_ACT(a5)

        | Four tones, the first stepping through the waveform a byte a
        | sample and the others still, for 4 ticks.
        lea     WAVE(a5), a1
        moveq   #0, d0
1:      move.b  d0, (a1)+
        addq.b  #1, d0
        bne.s   1b
        lea     RECORD(a5), a1
        moveq   #24, d0
2:      clr.w   (a1)+
        dbra    d0, 2b
        lea     RECORD(a5), a1
        move.w  #4, (a1)
        move.l  #0x10000, 2(a1)
        lea     WAVE(a5), a2
        move.l  a2, 34(a1)
        move.l  a2, 38(a1)
        move.l  a2, 42(a1)
        move.l  a2, 46(a1)
        lea     SYNTH(a5), a2
        move.w  #1, (a2)
        move.l  a1, 2(a2)
        lea     PB(a5), a0
        bsr     clear_pb
        move.l  a2, ioBuffer(a0)
        moveq   #6, d0
        move.l  d0, ioReqCount(a0)
        lea     four_tone_done(pc), a1
        move.l  a1, ioCompletion(a0)
        _WriteAsync
3:      tst.w   FT_DONE(a5)
        beq.s   3b

        | A free-form sound that doesn't go anywhere, until it's stopped.
        lea     FREE(a5), a2
        clr.w   (a2)
        clr.l   2(a2)
        move.b  #0xC0, 6(a2)
        lea     PB(a5), a0
        bsr     clear_pb
        move.l  a2, ioBuffer(a0)
        moveq   #16, d0
        move.l  d0, ioReqCount(a0)
        lea     free_form_done(pc), a1
        move.l  a1, ioCompletion(a0)
        _WriteAsync
        bsr     five_ticks
        move.w  FF_DONE(a5), FF_BEFORE(a5)
        lea     PB2(a5), a0
        bsr     clear_pb
        _KillIO
        move.w  d0, KILL_RESULT(a5)
        move.w  PB + ioResult(a5), FF_IORESULT(a5)

        | The buffer, written straight, with the speaker on.
        movea.l SOUND_BASE, a1
        move.w  #369, d1
4:      move.b  #0x40, (a1)
        addq.l  #2, a1
        dbra    d1, 4b
        movea.l VIA, a1
        bclr    #vSndEnb, (a1)
        bsr     five_ticks
        bset    #vSndEnb, (a1)
        _ExitToShell

clear_pb:
        movea.l a0, a1
        moveq   #24, d0
1:      clr.w   (a1)+
        dbra    d0, 1b
        move.w  #SOUND_DRIVER, ioRefNum(a0)
        rts

five_ticks:
        move.l  TICKS, d0
        addq.l  #5, d0
1:      cmp.l   TICKS, d0
        bhi.s   1b
        rts

four_tone_done:
        movea.l CURRENT_A5, a1
        addq.w  #1, FT_DONE(a1)
        move.w  d0, FT_RESULT(a1)
        rts

free_form_done:
        movea.l CURRENT_A5, a1
        addq.w  #1, FF_DONE(a1)
        move.w  d0, FF_RESULT(a1)
        rts

        | 400 Hz for 3 ticks, and 800 Hz more quietly for 2
square: .short  -1
        .short  1958, 200, 3
        .short  979, 100, 2
        .short  0, 0, 0
square_end:
