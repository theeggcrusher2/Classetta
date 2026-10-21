"""
The Printing Manager's records and its printer (Inside Macintosh II,
chapter 5): a print record's layout and defaults, the printer resource
file whose PDEFs early applications' own glue calls, and a job's pages
written to a PDF.

macrt's printer prints to a PDF file.  It says it's an ImageWriter-like
printer at 72 dots per inch, so programs draw everything with QuickDraw,
and each page they draw is recorded as a picture, which is drawn four
times the size, at 288 dots per inch, onto the PDF's page.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass, field
from pathlib import Path

from PyQt6.QtCore import QMarginsF, QRect, QSizeF, Qt
from PyQt6.QtGui import QImage, QPageSize, QPainter, QPdfWriter

from ..macfile import MacFile
from ..resfork import Resource, build_fork
from .types import Rect

PRINTER_NAME = "macrt Printer"      # the printer resource file, STR -8192 in the System
PRINT_DRIVER = -3                   # the .Print driver's reference number, DRVR 2
DRIVER_VERSION = 2

SCALE = 4                           # how much finer the PDF's pages are than 72 dpi
DPI = 72
BLACK, WHITE = 0xFF000000, 0xFFFFFFFF

# TPrint (IM II-149): 120 bytes.
PRINT_SIZE = 120
iPrVersion = 0
prInfo = 2                          # TPrInfo: iDev, iVRes, iHRes, rPage
rPaper = 16
prStl = 24                          # TPrStl: wDev, iPageV, iPageH, bPort, feed
prInfoPT = 32
prXInfo = 46
prJob = 62                          # TPrJob
iFstPage = prJob
iLstPage = prJob + 2
iCopies = prJob + 4
bJDocLoop = prJob + 6
fFromUsr = prJob + 7
pIdleProc = prJob + 8
pFileName = prJob + 12

PRINT_VERSION = 3
bDraftLoop, bSpoolLoop = 0, 1
iPrPgMax = 9999
DEV_IMAGEWRITER = 0x0100            # wDev: its high byte, bDevCItoh
feedCut, feedFanfold = 0, 1         # TFeed: by hand a sheet at a time, which Word asks for
iPfMaxPgs = 128

# TPrPort (IM II-152): a GrafPort, its QDProcs, four longs, fOurPtr, fOurBits.
PRPORT_SIZE = 178
gProcs = 108
fOurPtr = 176

# TPrStatus (IM II-161) and TGnlData (IM V-410).
STATUS_SIZE = 26
opNotImpl = 2
iPrAbort = 128
iPrSavPFil = -1
iMemFullErr = -108


# =========================================================================
# Paper
# =========================================================================
@dataclass(frozen=True)
class Paper:
    name: str
    width: int                      # in points, upright
    height: int


PAPERS = (Paper("US Letter", 612, 792), Paper("US Legal", 612, 1008),
          Paper("A4", 595, 842))
SIDE, TOP = 18, 36                  # the margins round the page: a quarter and a half inch


@dataclass
class Setup:
    """What Page Setup chooses."""
    paper: Paper = PAPERS[0]
    wide: bool = False              # landscape

    @property
    def size(self) -> tuple[int, int]:
        """(width, height) in points."""
        p = self.paper
        return (p.height, p.width) if self.wide else (p.width, p.height)

    @property
    def page(self) -> Rect:
        """rPage: what can be printed on, with (0, 0) at its top left."""
        w, h = self.size
        side, top = (TOP, SIDE) if self.wide else (SIDE, TOP)
        return Rect(0, 0, h - 2 * top, w - 2 * side)

    @property
    def paper_rect(self) -> Rect:
        """rPaper, round rPage."""
        w, h = self.size
        side, top = (TOP, SIDE) if self.wide else (SIDE, TOP)
        return Rect(-top, -side, h - top, w - side)


def _rect(r: Rect) -> bytes:
    return struct.pack(">hhhh", *r)


def default_record(setup: Setup | None = None) -> bytes:
    """PrintDefault's print record."""
    setup = setup or Setup()
    page, paper = setup.page, setup.paper_rect
    info = struct.pack(">hhh", 0, DPI, DPI) + _rect(page)
    w, h = setup.size
    stl = struct.pack(">HhhBB", DEV_IMAGEWRITER, h * 120 // 72, w * 120 // 72, 0, feedFanfold)
    row_bytes = (page.width + 15) // 16 * 2
    band = 16
    x_info = struct.pack(">hhhhhBBBBBB", row_bytes, band, page.width, row_bytes * band,
                         -(-page.height // band), 1, 1, 1, 1, 0, 0)
    job = struct.pack(">hhhBBIIhBB", 1, iPrPgMax, 1, bDraftLoop, 0, 0, 0, 0, 0, 0)
    rec = struct.pack(">h", PRINT_VERSION) + info + _rect(paper) + stl + info + x_info + job
    return rec + bytes(PRINT_SIZE - len(rec))


def setup_of(rec: bytes) -> Setup | None:
    """The Setup a print record was made with, or None if it isn't one
    of macrt's."""
    if len(rec) < PRINT_SIZE or struct.unpack_from(">h", rec, iPrVersion)[0] != PRINT_VERSION:
        return None
    if struct.unpack_from(">H", rec, prStl)[0] != DEV_IMAGEWRITER:
        return None
    paper = Rect(*struct.unpack_from(">hhhh", rec, rPaper))
    w, h = paper.width, paper.height
    for p in PAPERS:
        for wide in (False, True):
            s = Setup(p, wide)
            if s.size == (w, h) and s.paper_rect == paper:
                return s
    return None


def valid(rec: bytes) -> bytes:
    """PrValidate: the record as it should be, its job kept and the rest
    made from its setup (or the default one if it has none)."""
    setup = setup_of(rec)
    out = bytearray(default_record(setup))
    if setup is not None:
        first, last, copies = struct.unpack_from(">hhh", rec, prJob)
        struct.pack_into(">hhh", out, prJob, max(first, 1), max(min(last, iPrPgMax), 1),
                         max(copies, 1))
        out[fFromUsr:pFileName + 4] = rec[fFromUsr:pFileName + 4]
    return bytes(out)


# =========================================================================
# The printer resource file
# =========================================================================
# Each PDEF's routines: (offset, selector) for those macrt does, (offset,
# argument bytes) for the rest, which return doing nothing.  The selectors
# are PrGlue's, whose routines are glue_print's.
DOC_LOOP = ((0x04000C00,), (0x08000484,), (0x10000808,), (0x1800040C,))
PDEFS = {
    0: DOC_LOOP,                    # draft printing
    1: DOC_LOOP,                    # spooling: the same, here
    4: ((0x20040480,), (0x2A040484,), (0x32040488,), (0x3C04040C,), (0x44040410,),
        (0x4A040894,), (0x52040498,), (0x5804089C,)),
    5: ((0x60051480,), (None, 20)),
}
PR_GLUE = 0xACFD                    # _PrGlue, auto-pop


def pdef(entries) -> bytes:
    """A PDEF's code: a BRA.W per routine, four bytes apart, to code that
    calls PrGlue with the routine's selector.  The selector goes under
    the return address, where PrGlue finds it, and the auto-pop bit has
    PrGlue return straight to the caller."""
    jumps = bytearray()
    bodies = bytearray()
    start = 4 * len(entries)
    for i, (sel, *rest) in enumerate(entries):
        offset = start + len(bodies) - (4 * i + 2)
        jumps += struct.pack(">Hh", 0x6000, offset)                 # BRA.W
        if sel is not None:
            bodies += struct.pack(">HHIHH", 0x2F17,                 # MOVE.L (SP),-(SP)
                                  0x2F7C, sel, 0x0004,              # MOVE.L #sel,4(SP)
                                  PR_GLUE)
        else:
            bodies += struct.pack(">HHhH", 0x205F,                  # MOVEA.L (SP)+,A0
                                  0x4FEF, rest[0],                  # LEA n(SP),SP
                                  0x4ED0)                           # JMP (A0)
    return bytes(jumps + bodies)


def printer_file() -> MacFile:
    """The printer resource file the System's STR -8192 names."""
    resources = [Resource(b"PDEF", n, pdef(e)) for n, e in sorted(PDEFS.items())]
    return MacFile(PRINTER_NAME, rsrc=build_fork(resources), type=b"PRES", creator=b"MCRT")


def driver() -> bytes:
    """DRVR 2, .Print: a driver header whose routines do nothing.  The
    Device Manager here is Python and never calls them; some programs
    get the resource to make it purgeable or not."""
    name = b".Print"
    code_at = 18 + 1 + len(name)
    code_at += code_at & 1
    header = struct.pack(">HHHH5H", 0x4F00, 0, 0, 0, *[code_at] * 5)
    header += bytes([len(name)]) + name
    header += bytes(code_at - len(header))
    return header + struct.pack(">HH", 0x7000, 0x4E75)            # MOVEQ #0,D0; RTS


# =========================================================================
# A job: pages to a PDF
# =========================================================================
@dataclass
class Job:
    """A document being printed: its pages so far, black and white at
    SCALE times 72 dpi."""
    setup: Setup
    path: Path
    first: int = 1
    last: int = iPrPgMax
    copies: int = 1
    page_number: int = 0
    pages: list[QImage] = field(default_factory=list)

    def wanted(self) -> bool:
        """Whether the page being drawn is one to print."""
        return self.first <= self.page_number <= self.last

    def blank_page(self) -> QImage:
        w, h = self.setup.size
        img = QImage(w * SCALE, h * SCALE, QImage.Format.Format_RGB32)
        img.fill(Qt.GlobalColor.white)
        return img

    def add(self, img: QImage) -> None:
        # Black first: a PDF keeps such an image as one bit a pixel.
        self.pages.append(img.convertToFormat(QImage.Format.Format_Mono, [BLACK, WHITE],
                                              Qt.ImageConversionFlag.ThresholdDither))

    def write(self) -> Path | None:
        """The PDF, if any pages were printed: each page as many times as
        there are copies."""
        if not self.pages:
            return None
        w, h = self.setup.size
        writer = QPdfWriter(str(self.path))
        writer.setCreator("macrt")
        writer.setResolution(DPI * SCALE)
        writer.setPageSize(QPageSize(QSizeF(w, h), QPageSize.Unit.Point, "",
                                     QPageSize.SizeMatchPolicy.ExactMatch))
        writer.setPageMargins(QMarginsF(0, 0, 0, 0))
        p = QPainter(writer)
        p.setRenderHint(QPainter.RenderHint.LosslessImageRendering)    # not JPEG
        try:
            first = True
            for img in self.pages:
                for _ in range(max(self.copies, 1)):
                    if not first:
                        writer.newPage()
                    first = False
                    p.drawImage(QRect(0, 0, w * SCALE, h * SCALE), img)
        finally:
            p.end()
        return self.path


def unique(path: Path) -> Path:
    """path, or the first of "name 2.pdf", "name 3.pdf"... that isn't there."""
    n = 1
    out = path
    while out.exists():
        n += 1
        out = path.with_name(f"{path.stem} {n}{path.suffix}")
    return out


def pdf_name(title: str) -> str:
    """A file name for a document's PDF, from its window's title."""
    name = "".join("-" if ch in '/\\:*?"<>|' or ord(ch) < 32 else ch for ch in title).strip()
    return (name or "Printout") + ".pdf"
