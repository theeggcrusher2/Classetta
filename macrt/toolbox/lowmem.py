"""
Low-memory globals: system variables at fixed addresses near the bottom
of RAM, named as in Inside Macintosh (SysEqu.a).

Under MultiFinder each application has its own copy of some of them
(SWITCHED).  A World holds one application's copy.  The process switcher
swaps it into low memory while that application runs; the Toolbox reads
and writes it through the World either way, so it always gets the copy
that belongs to the application making the call.
"""

from __future__ import annotations

import struct
import threading

from ..memory import RAM

MonkeyLives = 0x100     # >= 0 while The Monkey, Apple's random tester, runs
ScrVRes = 0x102         # screen resolution, dots per inch
ScrHRes = 0x104
ScreenRow = 0x106       # rowBytes of the screen
MemTop = 0x108          # end of RAM
BufPtr = 0x10C          # top of the application's memory
HeapEnd = 0x114         # end of the application heap
TheZone = 0x118         # the current heap zone
UTableBase = 0x11C      # the Device Manager's unit table: a DCE handle per unit
DskVerify = 0x12C       # nonzero: the disk driver compares what it reads with the buffer
ApplLimit = 0x130       # how far the application heap may grow
SonyVars = 0x134        # the disk driver's variables: from $12, the drive it was last used on
PollStack = 0x13A       # the stack, as the disk driver's SetUpPoll found it
PollProc = 0x13E        # what it calls once it's done, if anything
CPUFlag = 0x12F         # 0 68000, 1 68010, 2 68020, 3 68030, 4 68040
DskErr = 0x142          # the disk driver's last result
SysEvtMask = 0x144
RndSeed = 0x156         # a random seed, as the machine came up with it
SysVersion = 0x15A
SEvtEnb = 0x15C         # nonzero: SystemEvent is enabled
VBLQueue = 0x160        # the VBL task queue's header
Ticks = 0x16A           # ticks since startup
MBState = 0x172         # mouse button: $80 while up
KeyMap = 0x174          # 16 bytes, one bit per key down
KeyLast = 0x184         # the last key down's message, until it's up; then 0
KeyTime = 0x186         # Ticks when it went down
KeyThresh = 0x18E
KeyRepThresh = 0x190
Lvl1DT = 0x192          # the VIA's interrupts' routines, one for each of its flags
UnitNtryCnt = 0x1D2     # the unit table's length
VIA = 0x1D4             # the VIA's base address
IWM = 0x1E0             # the floppy disk controller's base address
SysParam = 0x1F8        # parameter RAM, as InitUtil copies it here: 20 bytes
Time = 0x20C            # seconds since 1904
BootDrive = 0x210
SFSaveDisk = 0x214      # Standard File's volume, as a negative vRefNum
KbdType = 0x21E
AlarmState = 0x21F      # bit 7 clear: the alarm has turned the Apple menu's icon over
SdVolume = 0x260        # the Sound Driver's volume, in its low 3 bits
SoundPtr = 0x262        # the four-tone record it's playing
SoundBase = 0x266       # the sound buffer: 370 words, a sample in each high byte
SoundVBL = 0x26A        # the Sound Driver's VBL task
SoundDCE = 0x27A        # its device control entry
SoundActive = 0x27E     # nonzero while it's making a sound
SoundLevel = 0x27F      # the square wave's amplitude
CurPitch = 0x280        # and its count
MemErr = 0x220          # last Memory Manager error
RdAddr = 0x22A          # the 64K ROM disk driver's routine that finds a sector's address field
RdData = 0x22E          # and its routine that reads the data field after it
Seek = 0x236            # and its routines that move the head to a track,
SetUpPoll = 0x23A       # point A4-A6 at the IWM, the VIA and the SCC,
Recal = 0x23E           # move the head out to track 0,
WakeUp = 0x246          # call a routine once a time has gone by,
MakeSpdTbl = 0x24E      # time its speeds,
AdrDisk = 0x252         # choose one of the drive's registers
SetSpeed = 0x256        # and set its speed for the track
ROM85 = 0x28E           # $7FFF: 128K ROM or later, without Color QuickDraw
Key1Trans = 0x29E       # the keyboard's key code to character translation
Key2Trans = 0x2A2       # the keypad's
SysZone = 0x2A6
ApplZone = 0x2AA
ROMBase = 0x2AE
RAMBase = 0x2B2
DoubleTime = 0x2F0      # double-click interval, ticks
CaretTime = 0x2F4       # insertion point blink interval, ticks
TagData = 0x2FA         # the last sector's tag the disk driver read or wrote, from 2 on
DrvQHdr = 0x308         # the drive queue
Lo3Bytes = 0x31A        # $00FFFFFF
MinStack = 0x31E        # the least room an application's stack is given
DefltStack = 0x322      # the room it's given, below CurStackBase and above ApplLimit
FCBSPtr = 0x34E         # the file control block buffer
DefVCBPtr = 0x352       # the default volume's VCB
VCBQHdr = 0x356         # the volume control block queue
FSQHdr = 0x360          # the File Manager's queue of pending calls
WDCBsPtr = 0x372        # the working directory table
DefVRefNum = 0x384      # the default volume, as last set
CurDirStore = 0x398     # Standard File's directory
ToExtFS = 0x3F2         # the external file system hook
FSFCBLen = 0x3F6        # length of a file control block; positive under HFS
jCacheFlush = 0x6F4     # vector to the routine that flushes the processor's caches
JHideCursor = 0x800     # vectors to QuickDraw's cursor routines
JShowCursor = 0x804
JShieldCursor = 0x808
JInitCrsr = 0x814
JCrsrObscure = 0x81C
JCrsrTask = 0x8EE       # QuickDraw's cursor VBL task, for VBL tasks that chain to it
ScrnBase = 0x824        # the screen buffer
MTemp = 0x828           # where the mouse is, as the hardware has it
RawMouse = 0x82C
Mouse = 0x830           # where the mouse is, global coordinates
CrsrNew = 0x8CE         # nonzero: the mouse has moved, for the cursor's task to see to
CrsrCouple = 0x8CF      # nonzero: the cursor goes where the mouse does
WWExist = 0x8F2         # 0 while the Window Manager's set up; a program may set it to say it isn't
QDExist = 0x8F3         # the same for QuickDraw
JIODone = 0x8FC         # where a driver goes when it's done with a request
CurApRefNum = 0x900
CurrentA5 = 0x904
CurStackBase = 0x908
CurApName = 0x910       # STRING[31]
CurJTOffset = 0x934     # offset of the jump table from A5
CurPageOption = 0x936
PrintVars = 0x944       # 16 bytes the Printing Manager's glue keeps
PrintErr = PrintVars    # the last printing call's result
HiliteMode = 0x938
ScrapSize = 0x960       # ScrapStuff (InfoScrap's record): the desk scrap's size
ScrapHandle = 0x964     # its contents, in the application heap
ScrapCount = 0x968      # changed by ZeroScrap
ScrapState = 0x96A      # positive: in memory
ScrapName = 0x96C       # the Clipboard file's name
ApFontID = 0x984
FMgrOutRec = 0x998      # FMSwapFont's output: the font QuickDraw last used
ToolScratch = 0x9CE     # 8 bytes
WindowList = 0x9D6      # front window of the application
SaveUpdate = 0x9DA
PaintWhite = 0x9DC
WMgrPort = 0x9DE
GrayRgn = 0x9EE         # the desktop
DragHook = 0x9F6        # called over and over while DragGrayRgn follows the mouse
Scratch8 = 0x9FA
OneOne = 0xA02          # $00010001
MinusOne = 0xA06        # $FFFFFFFF
MenuList = 0xA1C        # handle to the menu bar
TheMenu = 0xA26         # highlighted menu
DragPattern = 0xA34     # the pattern DragTheRgn draws its outline in
DeskPattern = 0xA3C
FPState = 0xA4A         # SANE's environment word, then its halt vector
TopMapHndl = 0xA50      # the most recently opened resource map
SysMapHndl = 0xA54      # the System file's resource map
SysMap = 0xA58          # the System file's reference number
CurMap = 0xA5A          # the current resource file
ResLoad = 0xA5E         # SetResLoad's flag, in the high byte
ResErr = 0xA60          # last Resource Manager error
TEDoText = 0xA70        # TextEdit's routine that hit-tests, highlights, draws and places the caret
ApplScratch = 0xA78     # 12 bytes
GhostWindow = 0xA84     # a window never counted the front one (Inside Macintosh I-287)
ResumeProc = 0xA8C      # InitDialogs' procedure for restarting after a system error
ANumber = 0xA98         # the alert last shown
ACount = 0xA9A          # its stage, less one: 0 to 3; -1 to start again
DABeeper = 0xA9C        # the alerts' sound procedure, ErrorSound's
DAStrings = 0xAA0       # ParamText's four strings, as handles
TEScrpLength = 0xAB0    # TextEdit's private scrap: its length, a word
TEScrpHandle = 0xAB4    # and its contents
SysResName = 0xAD8      # STRING[19], "System"
AppParmHandle = 0xAEC   # the Finder's message: documents to open or print
DSErrCode = 0xAF0       # the last system error's ID
DlgFont = 0xAFA         # the font of dialogs' and alerts' text
Unnamed_AFC = 0xAFC     # a long no manual here names: Word 1.0 keeps a handle in it
HWCfgFlags = 0xB22
MBDFHndl = 0xB58        # the menu bar's definition procedure, InitProcMenu's
SysFontFam = 0xBA6
SysFontSize = 0xBA8
MBarHeight = 0xBAA
MethodNotFound = 0xBE8  # Object Pascal's, which no manual here names: where
ClassTable = 0xBEC      # _MethodDispatch goes for no method, and its classes
MMU32Bit = 0xCB2        # 0: 24-bit addressing

LOW_MEMORY_END = 0x2000

SWITCHED = {
    ApplZone: 4, TheZone: 4, ApplLimit: 4, HeapEnd: 4, CurStackBase: 4,
    CurrentA5: 4, CurApName: 32, CurApRefNum: 2, MemErr: 2,
    WindowList: 4, GhostWindow: 4, MenuList: 4, TheMenu: 2, MBDFHndl: 4,
    TopMapHndl: 4, CurMap: 2, ResLoad: 2, ResErr: 2,
    CurJTOffset: 2, CurPageOption: 2, PrintVars: 16, AppParmHandle: 4, ApplScratch: 12,
    VBLQueue: 10,
    ResumeProc: 4, ANumber: 2, ACount: 2, DABeeper: 4, DAStrings: 16, DlgFont: 2,
    ScrapSize: 16, TEScrpLength: 4, TEScrpHandle: 4,
    DefVCBPtr: 4, DefVRefNum: 2, SFSaveDisk: 2, CurDirStore: 4, FPState: 6,
    MethodNotFound: 8,
}


class World:
    """One application's switched globals."""

    def __init__(self, mem: RAM):
        self.mem = mem
        self.active = False
        self._lock = threading.RLock()
        self._saved = {addr: bytearray(n) for addr, n in SWITCHED.items()}

    def _slot(self, addr: int, n: int) -> tuple[bytearray, int]:
        for start, buf in self._saved.items():
            if start <= addr and addr + n <= start + len(buf):
                return buf, addr - start
        raise KeyError(f"${addr:X} is not a switched global")

    def read(self, addr: int, n: int) -> bytes:
        with self._lock:
            if self.active:
                return self.mem.read(addr, n)
            buf, off = self._slot(addr, n)
            return bytes(buf[off:off + n])

    def write(self, addr: int, data: bytes) -> None:
        with self._lock:
            if self.active:
                self.mem.write(addr, data)
                return
            buf, off = self._slot(addr, len(data))
            buf[off:off + len(data)] = data

    def r16(self, addr: int) -> int:
        return struct.unpack(">h", self.read(addr, 2))[0]

    def r32(self, addr: int) -> int:
        return struct.unpack(">I", self.read(addr, 4))[0]

    def w16(self, addr: int, value: int) -> None:
        self.write(addr, struct.pack(">H", value & 0xFFFF))

    def w32(self, addr: int, value: int) -> None:
        self.write(addr, struct.pack(">I", value & 0xFFFFFFFF))

    def switch_in(self) -> None:
        with self._lock:
            for addr, buf in self._saved.items():
                self.mem.write(addr, bytes(buf))
            self.active = True

    def switch_out(self) -> None:
        """Save what the application left in low memory, changes included."""
        with self._lock:
            for addr, buf in self._saved.items():
                buf[:] = self.mem.read(addr, len(buf))
            self.active = False
