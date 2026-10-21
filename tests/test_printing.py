"""The Printing Manager: print records, the printer resource file whose
PDEFs early programs' own glue calls, the .Print driver, and the pages
programs print written to a PDF."""

import re
import struct

import pytest
from guest import Guest, Processor

from macrt.m68k import glue_files, glue_print
from macrt.memory import to_mac
from macrt.toolbox import printing as pr
from macrt.toolbox.types import Rect

BLACK = 0


def pdf_pages(path) -> list[tuple[float, float]]:
    """A PDF's pages' sizes, from their MediaBoxes."""
    data = path.read_bytes()
    assert data.startswith(b"%PDF")
    boxes = re.findall(rb"/Type\s*/Page\b.*?/MediaBox\s*\[([^\]]*)\]", data, re.S)
    return [tuple(float(x) for x in box.split()[2:]) for box in boxes]


def black(img, h, v) -> bool:
    """Whether the page's point (h, v) is black."""
    return img.pixel(h * pr.SCALE + 1, v * pr.SCALE + 1) & 0xFFFFFF == BLACK


def answer(monkeypatch, *answers):
    """The printing dialogs' answers, in turn: None for Cancel."""
    left = list(answers)
    monkeypatch.setattr(glue_print, "_ask", lambda t, opener, arg: left.pop(0))


def start(g):
    h = g.heap.new_handle(pr.PRINT_SIZE)
    g("PrOpen")
    g("PrintDefault", h)
    assert g("PrJobDialog", h)
    return h, g("PrOpenDoc", h, 0, 0)


def page(g, port):
    g("PrOpenPage", port, 0)
    g("PenSize", 3, 3)
    g("FrameRect", g.rect_ptr((10, 10, 100, 200)))
    g("PrClosePage", port)


# =========================================================================
# Print records
# =========================================================================
def test_the_default_record_is_an_imagewriters_letter_page():
    rec = pr.default_record()
    assert len(rec) == pr.PRINT_SIZE
    assert struct.unpack_from(">h", rec, pr.iPrVersion)[0] == pr.PRINT_VERSION
    vres, hres = struct.unpack_from(">hh", rec, pr.prInfo + 2)
    assert (vres, hres) == (72, 72)
    assert Rect(*struct.unpack_from(">4h", rec, pr.prInfo + 6)) == Rect(0, 0, 720, 576)
    assert Rect(*struct.unpack_from(">4h", rec, pr.rPaper)) == Rect(-36, -18, 756, 594)
    w_dev, = struct.unpack_from(">H", rec, pr.prStl)
    feed = rec[pr.prStl + 7]
    assert (w_dev, feed) == (pr.DEV_IMAGEWRITER, pr.feedFanfold)   # Word asks for no sheets
    first, last, copies = struct.unpack_from(">hhh", rec, pr.prJob)
    assert (first, last, copies) == (1, pr.iPrPgMax, 1)
    assert pr.setup_of(rec) == pr.Setup()
    assert pr.valid(rec) == rec


def test_a_record_knows_its_setup_and_keeps_its_job():
    setup = pr.Setup(pr.PAPERS[2], wide=True)
    assert setup.size == (842, 595)
    rec = bytearray(pr.default_record(setup))
    assert pr.setup_of(bytes(rec)) == setup
    struct.pack_into(">hhh", rec, pr.prJob, 2, 3, 4)
    assert struct.unpack_from(">hhh", pr.valid(bytes(rec)), pr.prJob) == (2, 3, 4)
    # Someone else's printer's record is made one of these.
    assert pr.valid(bytes(pr.PRINT_SIZE)) == pr.default_record()


# =========================================================================
# The printer
# =========================================================================
def test_the_system_names_its_printer_and_has_its_driver(qapp):
    g = Guest()
    res = g.system.resources
    name = g.contents(res.GetResource(b"STR ", -8192))
    assert name[1:1 + name[0]] == to_mac(pr.PRINTER_NAME)
    assert g.system.files.system_folder.child(pr.PRINTER_NAME).type == b"PRES"
    driver = g.contents(res.GetResource(b"DRVR", 2))
    assert driver[18:25] == b"\x06.Print"


def test_early_glue_reaches_the_printing_manager_through_the_printers_pdefs(qapp):
    """MacWrite's glue opens the file the System's STR -8192 names, with
    the System Folder searched, and calls the routines of its PDEF 4 with
    their arguments on the stack; what macrt doesn't do takes its
    arguments off and returns."""
    m = Processor()
    name = m.ptr(bytes([len(pr.PRINTER_NAME)]) + to_mac(pr.PRINTER_NAME))
    h = m.heap.new_handle(pr.PRINT_SIZE)

    def pdef(n):
        return f"598F2F3C504445463F3C{n:04X}A9A0285F"     # GetResource('PDEF', n) in A4

    m.run(bytes.fromhex(
        f"42672F3C{name:08X}A9973E1F"                     # OpenResFile -> D7
        + pdef(4)
        + f"2F3C{h:08X}20544E90"                          # PrintDefault(h): JSR 0(A0)
        + f"42672F3C{h:08X}20544EA800183C1F"              # PrValidate(h): JSR $18(A0) -> D6
        + pdef(5)
        + "48780001" * 5 + "20544EA80004"))                # twenty bytes of something
    assert m.cpu.d(7) & 0xFFFF not in (0, 0xFFFF)
    assert m.contents(h) == pr.default_record()
    assert m.cpu.d(6) & 0xFFFF == 0                       # FALSE: nothing to change
    assert m.cpu.sp == m.stack_top


def test_the_print_driver_answers_the_device_manager(qapp, tmp_path, monkeypatch):
    """MacPaint opens .Print itself, asks for its DCE to find its version,
    and prints its document a bitmap at a time with Control calls."""
    monkeypatch.chdir(tmp_path)
    m = Processor()
    pb = m.ptr(bytes(80))
    m.mem.w32(pb + glue_files.ioNamePtr, m.ptr(b"\x06.Print"))
    m.run(bytes.fromhex(f"207C{pb:08X}A0003E00"))         # PBOpen -> D7
    assert m.cpu.d(7) & 0xFFFF == 0
    assert m.mem.r16s(pb + glue_files.ioRefNum) == pr.PRINT_DRIVER

    m.mem.w16(pb + glue_files.csCode, 1)
    m.run(bytes.fromhex(f"207C{pb:08X}A0053E00"))         # PBStatus: the DCE
    assert m.cpu.d(7) & 0xFFFF == 0
    dce = m.mem.r32(pb + glue_files.csParam)
    assert m.mem.r8(m.system.sys_heap.deref(dce) + 7) == pr.DRIVER_VERSION

    row_bytes, rows = 4, 8
    bits = m.ptr(b"\xFF" * row_bytes * 4 + bytes(row_bytes * 4))   # black, then white
    bm = m.ptr(struct.pack(">IH4h", bits, row_bytes, 0, 0, rows, 32))

    def control(cs_code, *params):
        m.mem.w16(pb + glue_files.csCode, cs_code)
        for i, p in enumerate(params):
            m.mem.w32(pb + glue_files.csParam + 4 * i, p)
        m.run(bytes.fromhex(f"207C{pb:08X}A0043E00"))     # PBControl
        assert m.cpu.d(7) & 0xFFFF == 0

    for _ in range(2):
        control(glue_print.iPrBitsCtl, bm, m.rect_ptr((0, 0, rows, 32)), 0)
    st = glue_print._state(m)
    img = st.raw_page
    assert black(img, 18, 36) and black(img, 49, 39)       # the first rows at the margins
    assert not black(img, 18, 40) and black(img, 18, 44)   # the second bitmap under it
    assert not black(img, 50, 36)
    control(glue_print.iPrDevCtl, glue_print.lPrPageEnd)
    m.run(bytes.fromhex(f"207C{pb:08X}A0013E00"))         # PBClose: the end of the job
    assert pdf_pages(tmp_path / "Test App.pdf") == [(612, 792)]
    assert st.printed == [tmp_path / "Test App.pdf"]


# =========================================================================
# Printing a document
# =========================================================================
def test_pages_are_drawn_and_written_to_the_pdf_the_print_dialog_names(qapp, tmp_path,
                                                                       monkeypatch):
    path = tmp_path / "Doc.pdf"
    answer(monkeypatch, ("ok", 0, 0, 1, path))
    g = Guest()
    h, port = start(g)
    assert port and g("PrError") == 0
    pport = g.system.port_at(port)
    assert pport.port_rect == Rect(0, 0, 720, 576)
    for _ in range(2):
        page(g, port)
    st = glue_print._state(g)
    img = st.job.pages[0]
    assert (img.width(), img.height()) == (612 * pr.SCALE, 792 * pr.SCALE)
    assert black(img, 18 + 10, 36 + 50)                     # the frame's left side,
    assert black(img, 18 + 100, 36 + 98)                    # its bottom,
    assert not black(img, 18 + 100, 36 + 50)                # and inside it
    assert not black(img, 18 + 5, 36 + 50)
    g("PrCloseDoc", port)
    assert g("PrError") == 0
    assert pdf_pages(path) == [(612, 792)] * 2
    assert st.printed == [path]
    assert g.system.port_at(port) is None


def test_only_the_pages_asked_for_are_printed_as_many_times_as_asked(
        qapp, tmp_path, monkeypatch):
    path = tmp_path / "Some.pdf"
    answer(monkeypatch, ("ok", 2, 3, 2, path))
    g = Guest()
    h, port = start(g)
    first, last, copies = struct.unpack_from(">hhh", g.contents(h), pr.prJob)
    assert (first, last, copies) == (2, 3, 2)
    for _ in range(4):
        page(g, port)
    assert len(glue_print._state(g).job.pages) == 2
    g("PrCloseDoc", port)
    assert len(pdf_pages(path)) == 4


def test_page_setup_chooses_the_paper(qapp, tmp_path, monkeypatch):
    path = tmp_path / "Wide.pdf"
    setup = pr.Setup(pr.PAPERS[2], wide=True)
    answer(monkeypatch, ("ok", setup), ("ok", 0, 0, 1, path))
    g = Guest()
    h = g.heap.new_handle(pr.PRINT_SIZE)
    g("PrintDefault", h)
    assert g("PrStlDialog", h)
    assert pr.setup_of(g.contents(h)) == setup
    assert g("PrJobDialog", h)
    port = g("PrOpenDoc", h, 0, 0)
    assert g.system.port_at(port).port_rect == setup.page
    page(g, port)
    g("PrCloseDoc", port)
    assert pdf_pages(path) == [(842, 595)]


def test_cancelling_prints_nothing(qapp, monkeypatch):
    answer(monkeypatch, None, None)
    g = Guest()
    h = g.heap.new_handle(pr.PRINT_SIZE)
    g("PrintDefault", h)
    assert not g("PrStlDialog", h)
    assert not g("PrJobDialog", h)
    assert g.contents(h) == pr.default_record()


def test_a_record_on_the_stack(qapp):
    """MacWrite's print record is a local variable, and its handle a
    pointer to a pointer to it, in no heap."""
    g = Guest()
    a5 = g.process.partition.a5
    rec, ptr = a5 - 300, a5 - 304
    with pytest.raises(ValueError):
        g.system.heap_of(ptr)
    g.mem.w32(ptr, rec)
    g("PrintDefault", ptr)
    assert g.mem.read(rec, pr.PRINT_SIZE) == pr.default_record()
    assert not g("PrValidate", ptr)
