"""
Toolbox constants, named as in Inside Macintosh.
"""

# ---------------------------------------------------------------------------
# Event Manager
# ---------------------------------------------------------------------------
nullEvent = 0
mouseDown = 1
mouseUp = 2
keyDown = 3
keyUp = 4
autoKey = 5
updateEvt = 6
diskEvt = 7
activateEvt = 8
osEvt = 15
kHighLevelEvent = 23

everyEvent = 0xFFFF
highLevelEventMask = 0x0400


def event_mask(what: int) -> int:
    return highLevelEventMask if what == kHighLevelEvent else 1 << what


# EventRecord.modifiers
activeFlag = 0x0001
btnState = 0x0080       # set while the button is UP
cmdKey = 0x0100
shiftKey = 0x0200
alphaLock = 0x0400
optionKey = 0x0800
controlKey = 0x1000

# osEvt messages (high byte of message)
suspendResumeMessage = 0x01
resumeFlag = 0x01
convertClipboard = 0x02     # the scrap changed while it was in the background

# ---------------------------------------------------------------------------
# Window Manager
# ---------------------------------------------------------------------------
# FindWindow part codes
inDesk = 0
inMenuBar = 1
inSysWindow = 2
inContent = 3
inDrag = 4
inGrow = 5
inGoAway = 6
inZoomIn = 7
inZoomOut = 8

# WDEF messages (Inside Macintosh I-299)
wDraw = 0
wHit = 1
wCalcRgns = 2
wNew = 3
wDispose = 4
wGrow = 5
wDrawGIcon = 6

# WDEF wHit results
wNoHit = 0
wInContent = 1
wInDrag = 2
wInGrow = 3
wInGoAway = 4
wInZoomIn = 5
wInZoomOut = 6
# Not a real WDEF code: the menu bar that `menu_bar="window"` puts under a
# window's title bar.  FindWindow reports it as inMenuBar.
wInMenuBar = 64
# Nor a part code: the host's answer when a click is in a window whose own
# WDEF says which part it's in.  FindWindow asks it, on the application's
# thread.
ASK_WDEF = -1

WHIT_TO_PART = {
    wNoHit: inDesk,
    wInContent: inContent,
    wInDrag: inDrag,
    wInGrow: inGrow,
    wInGoAway: inGoAway,
    wInZoomIn: inZoomIn,
    wInZoomOut: inZoomOut,
    wInMenuBar: inMenuBar,
}

# Window definition IDs (WDEF id * 16 + variation)
documentProc = 0
dBoxProc = 1
plainDBox = 2
altDBoxProc = 3
noGrowDocProc = 4
movableDBoxProc = 5
zoomDocProc = 8
zoomNoGrow = 12
rDocProc = 16

# windowKind
dialogKind = 2
userKind = 8

# ---------------------------------------------------------------------------
# QuickDraw
# ---------------------------------------------------------------------------
srcOr = 1
patCopy = 8
blackColor = 33
whiteColor = 30

# ---------------------------------------------------------------------------
# Menu Manager
# ---------------------------------------------------------------------------
APPLE_MENU_TITLE = "\x14"   # Mac Roman 0x14 is the apple glyph in Chicago
checkMark = "\x12"
commandMark = "\x11"
diamondMark = "\x13"
noMark = ""
# Item "command keys" below $20 aren't keys.  hMenuCmd makes the item
# hierarchical, with its submenu's ID in the mark; $1C-$1F say how the
# item's icon field is used (script code, reduced icon, small icon...).
hMenuCmd = "\x1b"

# The System 7 Application menu (process switcher at the right of the bar).
kApplicationMenuID = -16489

# Messages to the menu bar's definition procedure, the MBDF
mbDraw, mbHit, mbCalc, mbInit, mbDispose, mbHilite, mbHeight = range(7)
mbSave, mbRestore, mbRect, mbSaveAlt, mbResetAlt, mbMenuRgn = range(7, 13)

# ---------------------------------------------------------------------------
# Control Manager
# ---------------------------------------------------------------------------
# Control definition IDs (CDEF id * 16 + variation)
pushButProc = 0
checkBoxProc = 1
radioButProc = 2
useWFont = 8
scrollBarProc = 16

# Part codes
inButton = 10
inCheckBox = 11
inUpButton = 20
inDownButton = 21
inPageUp = 22
inPageDown = 23
inThumb = 129

# HiliteControl
noHilite = 0
inactiveHilite = 255

# CDEF messages
drawCntl = 0
testCntl = 1
calcCRgns = 2
initCntl = 3
dispCntl = 4
posCntl = 5
thumbCntl = 6
dragCntl = 7
autoTrack = 8
calcCntlRgn = 10
calcThumbRgn = 11

# DragGrayRgn and thumbCntl axes
noConstraint = 0
hAxisOnly = 1
vAxisOnly = 2

# ---------------------------------------------------------------------------
# List Manager
# ---------------------------------------------------------------------------
# listFlags
lDoHAutoscroll = 1
lDoVAutoscroll = 2
lDrawingModeOff = 8
lGrowBox = 0x20

# selFlags
lOnlyOne = 0x80
lExtendDrag = 0x40
lNoDisjoint = 0x20
lNoExtend = 0x10
lNoRect = 0x08
lUseSense = 0x04
lNoNilHilite = 0x02

# LDEF messages
lInitMsg = 0
lDrawMsg = 1
lHiliteMsg = 2
lCloseMsg = 3
