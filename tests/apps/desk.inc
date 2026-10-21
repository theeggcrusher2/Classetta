| What Desk (desk.s) and its desk accessory, Tally (tally.s), keep below
| Desk's A5, after the QuickDraw globals.

EVENT       = -226              | an EventRecord for GetNextEvent
WINDOW      = -230              | Desk's window
NAME        = -486              | 256 bytes: the Apple menu's first item
MENU        = -490              | the Apple menu
ITEMS       = -492              | its items
REF         = -494              | OpenDeskAcc's result
DAWIN       = -498              | the front window after it
KIND        = -500              | its windowKind
PORTED      = -502              | 1 if thePort was still Desk's window after OpenDeskAcc
OPENS       = -504              | Tally: its open routine's calls
RUNS        = -506              | accRun
CURSORS     = -508              | accCursor
KEYS        = -510              | accEvent: keyDowns
LASTKEY     = -512              | the last one's key code and character
UPDATES     = -514              | updates
ACTIVATES   = -516              | activates
DEACTIVATES = -518              | and deactivates
CLICKS      = -520              | mouseDowns
PICKED      = -524              | accMenu's csParam: the menu ID and item
EDITED      = -526              | the last csCode from accUndo to accClear
CLOSES      = -528              | its close routine's calls
SEEN        = -530              | Desk: events GetNextEvent gave it for Tally's window
EV_CMD      = -532              | SystemEvent's result for a Command-key keyDown
EV_KEY      = -534              | and for a plain one
EDIT_DA     = -536              | SystemEdit(Copy)'s with Tally in front
FOUND       = -538              | FindWindow's in Tally's window
FOUNDW      = -542              | and the window it found
MENUKEY     = -546              | MenuKey('T')'s
EDIT_APP    = -548              | SystemEdit's with Desk's window in front
CURSORS2    = -550              | Tally's accCursors after SystemTask then
PORT2       = -552              | 1 if thePort was Desk's window after SystemTask
UNITGONE    = -556              | unit 12's entry in the unit table after CloseDeskAcc
FRONT_AFTER = -560              | the front window then
REC         = -576              | an EventRecord made by hand
GLOBALS     = 576

TALLY_MENU  = -13               | Tally's menu: its reference number
