"""
The Printing Manager (Inside Macintosh II, chapter 5): PrGlue's routines,
and the .Print driver's printer control calls.  The records and the PDF
are macrt.toolbox.printing's.

Programs reach it three ways.  Later ones call PrGlue ($A8FD) with a
selector.  Early ones link glue of their own, which opens the .Print
driver, opens the printer resource file the System's STR -8192 names and
calls the code in its PDEF resources; macrt's printer file has PDEFs
whose code calls PrGlue, so both end up here.  And a few, MacPaint
among them, talk to the driver itself with its control calls: a bitmap
at a time, then the end of the page.

Every page a program draws in its printing port is recorded as a
picture, nothing drawn, and the picture is drawn on a page of the PDF
when the page is closed.  The PDF is written when the document is.
"""

from __future__ import annotations

import logging
import struct
from dataclasses import dataclass, field
from pathlib import Path
from weakref import WeakKeyDictionary

from PyQt6.QtCore import QPoint
from PyQt6.QtGui import QPainter

from ..memory import ADDRESS_MASK
from ..toolbox import lowmem, pict
from ..toolbox import printing as pr
from ..toolbox.events import ProcessKilled
from ..toolbox.qdraw import ImageSurface, MemorySurface
from ..toolbox.quickdraw import BitMap, GrafPort
from ..toolbox.types import Rect
from .dispatch import trap
from .glue_pict import SetStdProcs
from .glue_toolbox import _the_port_var

log = logging.getLogger("macrt.print")

# The driver's control calls (IM II-163).
iPrBitsCtl, iPrIOCtl, iPrEvtCtl, iPrDevCtl, iFMgrCtl = 4, 5, 6, 7, 8
lPrReset, lPrPageEnd, lPrLineFeed = 0x00010000, 0x00020000, 0x00030000
lPrEvtAll, lPrEvtTop = 0x0002FFFD, 0x0001FFFD
LINE_FEEDS = {0x0003FFFF: 12, 0x0003FFFE: 9}    # a sixth and an eighth of an inch

# The .Print driver's DCE (IM II-190): only what a program reads of it.
DCE_SIZE = 40
dCtlQHdr = 6                    # its qFlags' low byte: the driver's version
dCtlRefNum = 24


@dataclass
class State:
    """One application's printing."""
    setup: pr.Setup = field(default_factory=pr.Setup)
    path: Path | None = None            # where the Print dialog said the next PDF goes
    job: pr.Job | None = None           # PrOpenDoc's, to PrCloseDoc
    port: int = 0                       # its printing port
    frame: Rect | None = None           # the page being recorded's picture frame
    raw: pr.Job | None = None           # the driver's, to the end of the document
    raw_page: object = None             # the driver's page so far, and how far down it's got
    raw_v: int = 0
    raw_line: int = 12
    printed: list[Path] = field(default_factory=list)


_states: WeakKeyDictionary = WeakKeyDictionary()
_dce: WeakKeyDictionary = WeakKeyDictionary()     # System -> the .Print driver's DCE handle


def _state(t) -> State:
    s = _states.get(t.process)
    if s is None:
        s = _states[t.process] = State()
    return s


def _set_error(t, err: int) -> None:
    t.process.world.w16(lowmem.PrintErr, err)


def _error(t) -> int:
    return t.process.world.r16(lowmem.PrintErr)


# =========================================================================
# The print record
# =========================================================================
def _heap(t, h: int):
    """The heap a print record's handle is in, or None: the Printing
    Manager only dereferences it, and MacWrite's is a pointer to a
    pointer to a record on its stack."""
    try:
        return t.system.heap_of(h)
    except ValueError:
        return None


def _record(t, h: int) -> bytes:
    p = t.mem.r32(h) & ADDRESS_MASK if h else 0
    if not p:
        return b""
    heap = _heap(t, h)
    size = heap.handle_size(h) if heap is not None else pr.PRINT_SIZE
    return t.mem.read(p, min(size, pr.PRINT_SIZE))


def _put_record(t, h: int, rec: bytes) -> None:
    """Into a handle at least as big as a print record."""
    heap = _heap(t, h)
    if heap is not None and heap.handle_size(h) < pr.PRINT_SIZE:
        heap.set_handle_size(h, pr.PRINT_SIZE)
    p = t.mem.r32(h) & ADDRESS_MASK
    if p:
        t.mem.write(p, rec)


def _title(t) -> str:
    """What the front document's called, to name its PDF after."""
    for w in t.process.windows:
        if w.visible and w.title:
            return w.title
    return t.process.name


def _default_path(t) -> Path:
    return pr.unique(Path.cwd() / pr.pdf_name(_title(t)))


# =========================================================================
# Opening and closing the Printing Manager (IM II-157)
# =========================================================================
@trap
def PrOpen(t):
    _set_error(t, 0)


@trap
def PrClose(t):
    _set_error(t, 0)


@trap
def PrError(t):
    return _error(t)


@trap
def PrSetError(t, err):
    _set_error(t, err)


@trap
def PrDrvrOpen(t):
    _set_error(t, 0)


@trap
def PrDrvrClose(t):
    _set_error(t, 0)
    end_raw(t)


@trap
def PrDrvrDCE(t):
    return dce(t)


@trap
def PrDrvrVers(t):
    return pr.DRIVER_VERSION


def dce(t) -> int:
    """The .Print driver's device control entry, made the first time."""
    h = _dce.get(t.system)
    if h is None:
        heap = t.system.sys_heap
        h = heap.new_handle(DCE_SIZE, clear=True)
        p = heap.deref(h)
        t.mem.w8(p + dCtlQHdr + 1, pr.DRIVER_VERSION)
        t.mem.w16(p + dCtlRefNum, pr.PRINT_DRIVER)
        _dce[t.system] = h
    return h


# =========================================================================
# Print records and the dialogs (IM II-158)
# =========================================================================
@trap
def PrintDefault(t, h_print):
    _put_record(t, h_print, pr.default_record(_state(t).setup))


@trap
def PrValidate(t, h_print):
    """TRUE if the record had to be changed."""
    rec = _record(t, h_print)
    fixed = pr.valid(rec)
    if fixed[:pr.prJob] == rec[:pr.prJob]:
        return False
    _put_record(t, h_print, fixed)
    return True


@trap
def PrJobMerge(t, h_print_src, h_print_dst):
    """The job of one record (what the Print dialog chose) into another."""
    src = _record(t, h_print_src)
    dst = bytearray(pr.valid(_record(t, h_print_dst)))
    if len(src) >= pr.PRINT_SIZE:
        dst[pr.prJob:pr.prJob + 20] = src[pr.prJob:pr.prJob + 20]
    _put_record(t, h_print_dst, bytes(dst))


@trap
def PrStlDialog(t, h_print):
    """Page Setup: TRUE, and the record changed to suit, if OK."""
    st = _state(t)
    setup = pr.setup_of(_record(t, h_print)) or st.setup
    answer = _ask(t, "open_setup", setup)
    if answer is None:
        return False
    st.setup = answer[1]
    rec = bytearray(pr.default_record(st.setup))
    old = _record(t, h_print)
    if pr.setup_of(old) is not None:
        rec[pr.prJob:pr.prJob + 20] = old[pr.prJob:pr.prJob + 20]
    _put_record(t, h_print, bytes(rec))
    return True


@trap
def PrJobDialog(t, h_print):
    """Print: TRUE, and the pages and copies in the record's job, if OK.
    The PDF's name is kept for PrOpenDoc."""
    st = _state(t)
    answer = _ask(t, "open_print", _default_path(t))
    if answer is None:
        return False
    _, first, last, copies, path = answer
    st.path = path
    rec = bytearray(pr.valid(_record(t, h_print)))
    struct.pack_into(">hhhB", rec, pr.prJob, first or 1, last or pr.iPrPgMax, copies,
                     pr.bDraftLoop)
    _put_record(t, h_print, bytes(rec))
    return True


@trap
def PrStlInit(t, h_print):
    """The dialog record of Page Setup, for a program to add items to,
    which there isn't: the dialogs are the host's.  NIL; PrDlgMain asks
    with the host's dialog."""
    return 0


@trap
def PrJobInit(t, h_print):
    return 0


@trap
def PrDlgMain(t, h_print, p_dlg_init):
    """Whichever dialog the program's init routine would have set up:
    Print, since programs that add items add them to that one most."""
    return PrJobDialog(t, h_print)


def _ask(t, opener: str, arg):
    """Show a printing dialog and wait for its answer, letting other
    applications run meanwhile; None if cancelled, or with no display."""
    display = getattr(t.tb, "_d", None)
    if display is None:
        return None
    from ..ui import printdlg
    bridge = display.bridge
    with t.tb.host_dialog():
        dlg, (fut,) = bridge.call(getattr(printdlg, opener), display, arg, t.process.name)
        try:
            with t.yielding():
                while True:
                    try:
                        answer = fut.result(timeout=0.1)
                        break
                    except TimeoutError:
                        if t.process.events.killed or bridge.closed:
                            raise ProcessKilled() from None
        finally:
            bridge.post(dlg.finish)
    return None if answer[0] == "cancel" else answer


# =========================================================================
# Printing a document (IM II-159)
# =========================================================================
def _bits(setup: pr.Setup) -> BitMap:
    """The printing port's portBits: the page, with no pixels."""
    page = setup.page
    return BitMap(0, (page.width + 15) // 16 * 2, page)


@trap
def PrOpenDoc(t, h_print, p_pr_port, p_io_buf):
    """A printing port, made the current port: in the record given, or
    a new one."""
    st = _state(t)
    _set_error(t, 0)
    rec = _record(t, h_print)
    setup = pr.setup_of(rec) or st.setup
    heap = t.process.heap
    ours = not p_pr_port
    if ours:
        p_pr_port = heap.new_ptr(pr.PRPORT_SIZE, clear=True)
        if not p_pr_port:
            _set_error(t, pr.iMemFullErr)
            return 0
    if st.port:
        _close_port(t, st)
    port = GrafPort(t.mem, p_pr_port)
    port.open(heap, _bits(setup))
    SetStdProcs(t, p_pr_port + pr.gProcs)
    port.graf_procs = p_pr_port + pr.gProcs
    t.mem.w16(p_pr_port + pr.fOurPtr, 0xFF00 if ours else 0)
    t.system.register(port)
    t.mem.w32(_the_port_var(t), p_pr_port)
    first, last, copies = (struct.unpack_from(">hhh", rec, pr.prJob)
                           if len(rec) >= pr.prJob + 6 else (1, pr.iPrPgMax, 1))
    st.job = pr.Job(setup, st.path or _default_path(t), max(first, 1), last,
                    max(min(copies, 99), 1))
    st.path = None
    st.port = p_pr_port
    return p_pr_port


@trap
def PrOpenPage(t, p_pr_port, p_page_frame):
    """Start a page: the port as it was opened, recording a picture.
    Nothing is drawn while it is."""
    st = _state(t)
    port = t.system.port_at(p_pr_port)
    if st.job is None or port is None or p_pr_port != st.port:
        _set_error(t, pr.iPrAbort)
        return
    qd = t.tb.qd
    _stop_recording(t, port)
    heap = t.system.heap_of(port.vis_rgn) if port.vis_rgn else t.process.heap
    procs = port.graf_procs
    port.close(heap)
    port.open(heap, _bits(st.job.setup))
    port.graf_procs = procs
    t.mem.w32(_the_port_var(t), p_pr_port)
    st.job.page_number += 1
    frame = t.rect(p_page_frame) if p_page_frame else st.job.setup.page
    rec = pict.Recorder(frame)
    h = t.process.heap.new_handle(10)
    if not h:
        _set_error(t, pr.iMemFullErr)
        return
    rec.handle = h
    qd.pictures[port.addr] = rec
    port.pic_save = h
    port.pn_vis = -1
    st.frame = frame


def _stop_recording(t, port: GrafPort) -> bytes | None:
    """The page's picture, if one was being recorded."""
    rec = t.tb.qd.pictures.pop(port.addr, None)
    if rec is None:
        return None
    port.pic_save = 0
    port.pn_vis = 0
    if rec.handle:
        t.system.heap_of(rec.handle).dispose_handle(rec.handle)
    return rec.picture()


@trap
def PrClosePage(t, p_pr_port):
    st = _state(t)
    port = t.system.port_at(p_pr_port)
    if st.job is None or port is None:
        return
    data = _stop_recording(t, port)
    if data is not None and st.job.wanted():
        st.job.add(render(t, st.job.setup, st.frame or st.job.setup.page, data))


def render(t, setup: pr.Setup, frame: Rect, data: bytes):
    """A page with a picture drawn on it: frame mapped to rPage, SCALE
    times the size."""
    job = pr.Job(setup, Path())
    img = job.blank_page()
    w, h = setup.size
    s = pr.SCALE
    bounds = Rect(0, 0, h * s, w * s)
    page, paper = setup.page, setup.paper_rect
    dst = Rect((page.top - paper.top) * s, (page.left - paper.left) * s,
               (page.bottom - paper.top) * s, (page.right - paper.left) * s)
    _draw_on(t, img, bounds, lambda port: pict.draw_picture(
        t.tb.qd, port, frame, pict.Reader.of(data, 10), dst))
    return img


def _draw_on(t, img, bounds: Rect, draw) -> None:
    """draw(port) with a port of the process's whose pixels are img's."""
    qd = t.tb.qd
    heap = t.process.heap
    addr = heap.new_ptr(GrafPort.SIZE)
    if not addr:
        return
    port = GrafPort(t.mem, addr)
    port.open(heap, BitMap(0, (bounds.width + 15) // 16 * 2, bounds))
    qd.surfaces[addr] = ImageSurface(img, bounds)
    t.system.register(port)
    try:
        draw(port)
    finally:
        del qd.surfaces[addr]
        t.system.unregister(port)
        port.close(heap)
        heap.dispose_ptr(addr)


@trap
def PrCloseDoc(t, p_pr_port):
    """Close the printing port and write the PDF."""
    st = _state(t)
    job, st.job = st.job, None
    if p_pr_port == st.port:
        _close_port(t, st)
    if job is not None:
        _write(t, st, job)


def _close_port(t, st: State) -> None:
    port = t.system.port_at(st.port)
    if port is not None:
        _stop_recording(t, port)
        heap = t.system.heap_of(port.vis_rgn) if port.vis_rgn else t.process.heap
        port.close(heap)
        t.system.unregister(port)
        if t.mem.r8(st.port + pr.fOurPtr):
            t.process.heap.dispose_ptr(st.port)
    st.port = 0


def _write(t, st: State, job: pr.Job) -> None:
    try:
        path = job.write()
    except OSError as e:
        log.warning("couldn't print to %s: %s", job.path, e)
        _set_error(t, pr.iPrSavPFil)
        return
    if path is None:
        return
    st.printed.append(path)
    n = len(job.pages)
    log.info("printed %d page%s to %s", n, "" if n == 1 else "s", path)
    if not path.exists():
        _set_error(t, pr.iPrSavPFil)


@trap
def PrPicFile(t, h_print, p_pr_port, p_io_buf, p_dev_buf, pr_status):
    """Spooled documents are printed as they're closed here: nothing is
    left to do."""
    if pr_status:
        t.mem.fill(pr_status, pr.STATUS_SIZE)
    _set_error(t, 0)


@trap
def PrGeneral(t, p_data):
    t.mem.w16(p_data + 2, pr.opNotImpl)         # TGnlData's iError


# =========================================================================
# The driver's control calls (IM II-163)
# =========================================================================
@trap
def PrCtlCall(t, which, p1, p2, p3):
    _set_error(t, control(t, which, p1, p2, p3))


def control(t, which: int, p1: int, p2: int, p3: int) -> int:
    """A printer control call, from PrCtlCall or a Control call to the
    .Print driver.  Its result."""
    st = _state(t)
    if which == iPrDevCtl:
        if p1 == lPrReset:
            end_raw(t)
        elif p1 == lPrPageEnd:
            _raw_page(t, st, end=True)
        elif p1 in LINE_FEEDS:
            st.raw_line = LINE_FEEDS[p1]
        elif p1 >> 16 == lPrLineFeed >> 16:
            st.raw_v += st.raw_line * (p1 & 0xFFFF or 1)
    elif which == iPrBitsCtl:
        _raw_bits(t, st, p1 & ADDRESS_MASK, t.rect(p2), p3)
    elif which == iPrEvtCtl:
        _raw_screen(t, st, p1)
    return 0


def _raw_page(t, st: State, end: bool = False):
    """The driver's page being printed, a new one if need be; with end,
    the page is done."""
    if st.raw is None:
        st.raw = pr.Job(st.setup, _default_path(t))
    if st.raw_page is None and not end:
        st.raw_page = st.raw.blank_page()
        st.raw_v = 0
    if end and st.raw_page is not None:
        st.raw.add(st.raw_page)
        st.raw_page = None
    return st.raw_page


def _raw_bits(t, st: State, bm: int, r: Rect, kind: int) -> None:
    """r of a bitmap, printed at the left margin under what's printed
    so far, each pixel a point; a new page when it won't fit."""
    if r.empty:
        return
    surface = MemorySurface.make(t.mem, t.mem.r32(bm), t.mem.r16(bm + 4), t.mem.r_rect(bm + 6))
    if surface is None:
        return
    img = _raw_page(t, st)
    page, paper = st.setup.page, st.setup.paper_rect
    if st.raw_v and st.raw_v + r.height > page.height:
        _raw_page(t, st, end=True)
        img = _raw_page(t, st)
    s = pr.SCALE
    p = QPainter(img)
    p.scale(s, s)
    p.drawImage(QPoint(page.left - paper.left, page.top - paper.top + st.raw_v),
                surface.image(r))
    p.end()
    st.raw_v += r.height


def _raw_screen(t, st: State, which: int) -> None:
    """Print the screen, or the front window, as Command-Shift-4 does."""
    screen = t.system.screen_bits
    r = screen.bounds
    if which == lPrEvtTop:
        w = next((w for w in t.process.windows if w.visible), None)
        if w is not None:
            r = w.content
    heap = t.process.heap
    bm = heap.new_ptr(14)
    t.mem.w32(bm, screen.base)
    t.mem.w16(bm + 4, screen.row_bytes)
    t.mem.w_rect(bm + 6, screen.bounds)
    t.system.screen.look()
    _raw_bits(t, st, bm, r, 0)
    heap.dispose_ptr(bm)
    _raw_page(t, st, end=True)
    end_raw(t)


def end_raw(t) -> None:
    """The end of what was printed through the driver: its PDF."""
    st = _state(t)
    if st.raw is None:
        return
    if st.raw_page is not None:
        _raw_page(t, st, end=True)
    job, st.raw = st.raw, None
    _write(t, st, job)


def driver_status(t, cs_code: int, cs_param: int) -> bool:
    """A Status call to the .Print driver: True if it's one it answers.
    csCode 1 is the Device Manager's own, the driver's DCE."""
    if cs_code == 1:
        t.mem.w32(cs_param, dce(t))
        return True
    return False
