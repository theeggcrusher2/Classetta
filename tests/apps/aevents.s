| AEvents: an application that says it's high-level-event aware.  It
| installs handlers and opens a PPC port, as System 7's applications do,
| looks at the Open Application event with GetSpecificHighLevelEvent,
| and processes it.  Its handler sends itself an event with a list in
| it, gets back the reply its other handler made, and then sends itself
| Quit.

        .include "traps.inc"

        .macro  ae name, selector       | the Apple Event Manager's, in Pack8
        .macro  _\name
        move.w  #\selector, d0
        _Pack8
        .endm
        .endm

        ae      AEInstallEventHandler,  0x091F
        ae      AEProcessAppleEvent,    0x021B
        ae      AEGetAttributePtr,      0x0E15
        ae      AECreateList,           0x0706
        ae      AEPutPtr,               0x0A08
        ae      AECreateDesc,           0x0825
        ae      AECreateAppleEvent,     0x0B14
        ae      AEPutParamDesc,         0x0610
        ae      AEPutParamPtr,          0x0A0F
        ae      AESend,                 0x0D17
        ae      AEGetParamPtr,          0x0E11
        ae      AEGetParamDesc,         0x0812
        ae      AECountItems,           0x0407
        ae      AEGetNthPtr,            0x100A
        ae      AEDisposeDesc,          0x0204
        ae      AEGetTheCurrentEvent,   0x021A

        | An OSErr result, left on the stack, that ought to be noErr
        .macro  check
        move.w  (sp)+, d0
        or.w    d0, ERRS(a5)
        .endm

ERRS            = -2            | the results above, ORed together
FILTERED        = -4            | calls to the GetSpecificHighLevelEvent filter
FOUND           = -6            | GetSpecificHighLevelEvent's result
SEEN            = -14           | the event class and ID the filter saw
REFCON          = -18           | the Open Application handler's
ESRC            = -20           | the Open Application event's source
MISSED          = -22           | asking for the parameters it didn't get
RESULT          = -26           | the reply's 'rslt'
ERRNUM          = -28           | and its 'errn'
COUNT           = -32           | items in the list, as the other handler sees it
ITEM2           = -36           | the second, "56", as a long
ITEM1           = -38           | the first, 1234, as a short
SUM             = -42
QUIT            = -44           | the Quit handler has run
CURRENT         = -52           | the current event, as the Quit handler sees it
EVENT           = -68
LIST            = -76           | AEDescs
TARGET          = -84
APPLEV          = -92
REPLY           = -100
GOT             = -108
SCRATCH         = -112
TYPE            = -116
SIZE            = -120
KEYWORD         = -124
REFNUM          = -126          | the port PPCOpen opened
DUP             = -128          | opening it again
CLOSED2         = -130          | closing it again
PORTREC         = -202          | GetPortNameFromProcessSerialNumber's
PB              = -258          | a PPCOpenPBRec
WHOSE           = -266          | GetProcessSerialNumberFromPortName's answer
GLOBALS         = 266

AEVT            = 0x61657674    | 'aevt'
OAPP            = 0x6F617070    | 'oapp'
QUIT_ID         = 0x71756974    | 'quit'
MACR            = 0x4D414352    | 'MACR', this application's own class
TEST            = 0x74657374    | 'test'
ANY             = 0x2A2A2A2A    | '****'
REFCON_OAPP     = 0x4F415050    | 'OAPP'
KEY_ESRC        = 0x65737263    | 'esrc'
KEY_MISS        = 0x6D697373    | 'miss'
DIRECT          = 0x2D2D2D2D    | '----'
RSLT            = 0x72736C74    | 'rslt'
ERRN            = 0x6572726E    | 'errn'
SHOR            = 0x73686F72    | 'shor'
LONG            = 0x6C6F6E67    | 'long'
TEXT            = 0x54455854    | 'TEXT'
PSN             = 0x70736E20    | 'psn '
LIST_TYPE       = 0x6C697374    | 'list'

kHighLevelEvent = 23
kAENoReply      = 1
kAEWaitReply    = 3
kAENeverInteract = 0x10
HANDLER_ERR     = -1234         | what the 'MACR' handler returns

start:
        .macro  install class, id, handler, refcon
        clr.w   -(sp)
        move.l  #\class, -(sp)
        move.l  #\id, -(sp)
        pea     \handler(pc)
        move.l  #\refcon, -(sp)
        clr.b   -(sp)                   | not a system handler
        _AEInstallEventHandler
        check
        .endm
        install AEVT, OAPP, open_app, REFCON_OAPP
        install AEVT, QUIT_ID, quit, 0
        install MACR, ANY, any_macr, 0

        | Its own port's name, then a port of its own
        moveq   #0, d0
        _PPC                            | PPCInit
        or.w    d0, ERRS(a5)
        clr.w   -(sp)
        pea     PORTREC(a5)
        pea     current_process(pc)
        move.w  #0x46, -(sp)
        _OSDispatch                     | GetPortNameFromProcessSerialNumber
        check
        clr.w   -(sp)
        pea     PORTREC(a5)
        pea     WHOSE(a5)
        move.w  #0x35, -(sp)
        _OSDispatch                     | GetProcessSerialNumberFromPortName
        check
        lea     PB(a5), a0
        moveq   #56/4-1, d0
1:      clr.l   (a0)+
        dbra    d0, 1b
        lea     PB(a5), a0
        move.b  #1, 44(a0)              | serviceType: ppcServiceRealTime
        lea     port(pc), a1
        move.l  a1, 46(a0)              | portName
        moveq   #1, d0
        _PPC                            | PPCOpen
        or.w    d0, ERRS(a5)
        lea     PB(a5), a0
        move.w  38(a0), REFNUM(a5)      | portRefNum
        moveq   #1, d0
        _PPC
        move.w  d0, DUP(a5)
        .rept   2
        lea     PB(a5), a0
        move.w  REFNUM(a5), 38(a0)
        moveq   #9, d0
        _PPC                            | PPCClose
        .endr
        move.w  d0, CLOSED2(a5)

        clr.w   -(sp)
        pea     filter(pc)
        pea     SEEN(a5)                | its context
        pea     SCRATCH(a5)             | the error
        move.w  #0x45, -(sp)
        _OSDispatch                     | GetSpecificHighLevelEvent
        move.w  (sp)+, FOUND(a5)
        move.w  SCRATCH(a5), d0
        or.w    d0, ERRS(a5)

loop:   tst.w   QUIT(a5)
        bne.s   done
        clr.w   -(sp)
        move.w  #-1, -(sp)              | everyEvent
        pea     EVENT(a5)
        moveq   #1, d0
        move.l  d0, -(sp)
        clr.l   -(sp)
        _WaitNextEvent
        tst.w   (sp)+
        beq.s   loop
        cmpi.w  #kHighLevelEvent, EVENT(a5)
        bne.s   loop
        clr.w   -(sp)
        pea     EVENT(a5)
        _AEProcessAppleEvent
        check
        bra.s   loop
done:   _ExitToShell

| pascal Boolean filter(Ptr context, HighLevelEventMsgPtr msg, const TargetID *sender)
| Notes the event's class and ID, and leaves it to AEProcessAppleEvent.
filter:
        movea.l (sp)+, a1
        addq.l  #4, sp                  | the sender
        movea.l (sp)+, a0               | the message, its EventRecord at 8
        move.l  8+2(a0), d0             | message: the class
        move.l  8+10(a0), d1            | where: the ID
        movea.l (sp)+, a0               | the context
        move.l  d0, (a0)+
        move.l  d1, (a0)
        addq.w  #1, FILTERED(a5)
        clr.w   (sp)                    | FALSE
        jmp     (a1)

| pascal OSErr handler(const AppleEvent *event, AppleEvent *reply, long refcon)
EV              = 16
RE              = 12
RC              = 8

open_app:
        link    a6, #0
        move.l  RC(a6), REFCON(a5)
        clr.w   -(sp)
        move.l  EV(a6), -(sp)
        move.l  #KEY_ESRC, -(sp)
        move.l  #SHOR, -(sp)
        pea     TYPE(a5)
        pea     ESRC(a5)
        moveq   #2, d0
        move.l  d0, -(sp)
        pea     SIZE(a5)
        _AEGetAttributePtr
        check
        | As a handler checks that it's seen every required parameter:
        | there being none, there's no 'miss' attribute.
        clr.w   -(sp)
        move.l  EV(a6), -(sp)
        move.l  #KEY_MISS, -(sp)
        move.l  #ANY, -(sp)
        pea     TYPE(a5)
        pea     SCRATCH(a5)
        moveq   #4, d0
        move.l  d0, -(sp)
        pea     SIZE(a5)
        _AEGetAttributePtr
        move.w  (sp)+, MISSED(a5)

        | A list of 1234 and "56"
        clr.w   -(sp)
        clr.l   -(sp)
        clr.l   -(sp)
        clr.b   -(sp)                   | a list, not a record
        pea     LIST(a5)
        _AECreateList
        check
        move.l  #1234, SCRATCH(a5)
        clr.w   -(sp)
        pea     LIST(a5)
        clr.l   -(sp)                   | at the end
        move.l  #LONG, -(sp)
        pea     SCRATCH(a5)
        moveq   #4, d0
        move.l  d0, -(sp)
        _AEPutPtr
        check
        clr.w   -(sp)
        pea     LIST(a5)
        clr.l   -(sp)
        move.l  #TEXT, -(sp)
        pea     fifty_six(pc)
        moveq   #2, d0
        move.l  d0, -(sp)
        _AEPutPtr
        check

        | MACR/test, to itself, with the list as its direct parameter
        clr.w   -(sp)
        move.l  #PSN, -(sp)
        pea     current_process(pc)
        moveq   #8, d0
        move.l  d0, -(sp)
        pea     TARGET(a5)
        _AECreateDesc
        check
        clr.w   -(sp)
        move.l  #MACR, -(sp)
        move.l  #TEST, -(sp)
        pea     TARGET(a5)
        move.w  #-1, -(sp)              | kAutoGenerateReturnID
        clr.l   -(sp)                   | kAnyTransactionID
        pea     APPLEV(a5)
        _AECreateAppleEvent
        check
        clr.w   -(sp)
        pea     APPLEV(a5)
        move.l  #DIRECT, -(sp)
        pea     LIST(a5)
        _AEPutParamDesc
        check
        moveq   #kAEWaitReply+kAENeverInteract, d0
        bsr     send
        clr.w   -(sp)
        pea     REPLY(a5)
        move.l  #RSLT, -(sp)
        move.l  #LONG, -(sp)
        pea     TYPE(a5)
        pea     RESULT(a5)
        moveq   #4, d0
        move.l  d0, -(sp)
        pea     SIZE(a5)
        _AEGetParamPtr
        check
        clr.w   -(sp)
        pea     REPLY(a5)
        move.l  #ERRN, -(sp)
        move.l  #SHOR, -(sp)
        pea     TYPE(a5)
        pea     ERRNUM(a5)
        moveq   #2, d0
        move.l  d0, -(sp)
        pea     SIZE(a5)
        _AEGetParamPtr
        check
        pea     LIST(a5)
        bsr     dispose
        pea     APPLEV(a5)
        bsr     dispose
        pea     REPLY(a5)
        bsr     dispose

        | Then Quit, not waiting for a reply
        clr.w   -(sp)
        move.l  #AEVT, -(sp)
        move.l  #QUIT_ID, -(sp)
        pea     TARGET(a5)
        move.w  #-1, -(sp)
        clr.l   -(sp)
        pea     APPLEV(a5)
        _AECreateAppleEvent
        check
        moveq   #kAENoReply+kAENeverInteract, d0
        bsr     send
        pea     APPLEV(a5)
        bsr     dispose
        pea     REPLY(a5)
        bsr     dispose
        pea     TARGET(a5)
        bsr     dispose
        moveq   #0, d0
        bra     return

| AESend APPLEV, with the reply in REPLY, in the mode in D0
send:
        clr.w   -(sp)
        pea     APPLEV(a5)
        pea     REPLY(a5)
        move.l  d0, -(sp)
        clr.w   -(sp)                   | kAENormalPriority
        moveq   #-1, d0
        move.l  d0, -(sp)               | kAEDefaultTimeout
        clr.l   -(sp)                   | no idle procedure
        clr.l   -(sp)                   | or filter
        _AESend
        check
        rts

| AEDisposeDesc the descriptor on the stack
dispose:
        movea.l (sp)+, a0
        move.l  (sp), d0
        move.l  a0, (sp)
        clr.w   -(sp)
        move.l  d0, -(sp)
        _AEDisposeDesc
        check
        rts

| Every MACR event: adds up the items of the list it's sent, puts the sum
| in the reply, and returns an error of its own.
any_macr:
        link    a6, #0
        clr.w   -(sp)
        move.l  EV(a6), -(sp)
        move.l  #DIRECT, -(sp)
        move.l  #LIST_TYPE, -(sp)
        pea     GOT(a5)
        _AEGetParamDesc
        check
        clr.w   -(sp)
        pea     GOT(a5)
        pea     COUNT(a5)
        _AECountItems
        check
        .macro  nth index, type, to, size
        clr.w   -(sp)
        pea     GOT(a5)
        moveq   #\index, d0
        move.l  d0, -(sp)
        move.l  #\type, -(sp)
        pea     KEYWORD(a5)
        pea     TYPE(a5)
        pea     \to(a5)
        moveq   #\size, d0
        move.l  d0, -(sp)
        pea     SIZE(a5)
        _AEGetNthPtr
        check
        .endm
        nth     2, LONG, ITEM2, 4
        nth     1, SHOR, ITEM1, 2
        move.w  ITEM1(a5), d0
        ext.l   d0
        add.l   ITEM2(a5), d0
        move.l  d0, SUM(a5)
        clr.w   -(sp)
        move.l  RE(a6), -(sp)
        move.l  #RSLT, -(sp)
        move.l  #LONG, -(sp)
        pea     SUM(a5)
        moveq   #4, d0
        move.l  d0, -(sp)
        _AEPutParamPtr
        check
        pea     GOT(a5)
        bsr     dispose
        move.w  #HANDLER_ERR, d0
        bra     return

quit:
        link    a6, #0
        move.w  #1, QUIT(a5)
        clr.w   -(sp)
        pea     CURRENT(a5)
        _AEGetTheCurrentEvent
        check
        moveq   #0, d0

| A handler's result, in D0
return:
        unlk    a6
        movea.l (sp)+, a0
        lea     12(sp), sp
        move.w  d0, (sp)
        jmp     (a0)

current_process:
        .long   0, 2                    | kCurrentProcess
fifty_six:
        .ascii  "56"
port:   .short  0                       | a PPCPortRec: smRoman,
        .byte   7                       | a Str32,
        .ascii  "AEvents"
        .space  32-7+1                  | (padded to an even length)
        .short  2                       | ppcByString,
        .byte   4                       | and a Str32
        .ascii  "Test"
        .space  32-4+1
