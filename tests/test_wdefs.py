"""Window definition procedures: an application's own, and the System's
WDEF 0 and 1 for the 68k code that calls them."""

from PyQt6.QtGui import QColor, QImage

from macrt.m68k import wdefs
from macrt.toolbox import constants as k
from macrt.toolbox import lowmem
from macrt.toolbox.qdraw import ImageSurface, Region, read_rgn, write_rgn
from macrt.toolbox.types import Point, Rect
from macrt.toolbox.windows import WindowRecord, describe_proc
from macrt.ui import chrome

from guest import Guest, Processor

CONTENT = Rect(80, 60, 200, 260)


def _window(g, proc_id=k.documentProc, title="Sheet"):
    rec = WindowRecord.new(g.system, g.process, g.heap, 0, CONTENT, title, True, proc_id, True, 0)
    rec.hilited = True                      # the active window, with its boxes
    return rec


def _frame(rec, proc_id):
    """The host's frame for rec as WDEF proc_id >> 4 draws it, and where
    its structure's top left is."""
    f = chrome.Frame.for_window(rec, definition=describe_proc(proc_id))
    l, t, _, _ = chrome.insets(f)
    return f, Point(CONTENT.top - t, CONTENT.left - l)


def _at(pt):
    return (pt.v & 0xFFFF) << 16 | pt.h & 0xFFFF


def test_68k_code_can_call_wdef_0_for_a_windows_regions(qapp):
    """Excel's own WDEFs pass wCalcRgns to WDEF 0: the content region is
    the content area, and the structure's the host's frame around it,
    drop shadow and all."""
    m = Processor()
    rec = _window(m, proc_id=128 << 4)
    entry = m.system.sys_heap.deref(m.system.defproc("WDEF", 0))
    m.run(bytes.fromhex("42A7"                                      # CLR.L -(SP)
                        "3F3C0000"                                  # varCode: documentProc
                        f"2F3C{rec.addr:08X}"
                        f"3F3C{k.wCalcRgns:04X}"
                        "42A7"                                      # param
                        f"207C{entry:08X}4E90"                      # JSR the code
                        "201F"))                                    # MOVE.L (SP)+,D0
    assert m.cpu.sp == m.stack_top and m.missing == []
    f, o = _frame(rec, k.documentProc)
    w, h = chrome.structure_size(f, CONTENT.width, CONTENT.height)
    s = chrome.shadow(f)
    assert read_rgn(m.mem, rec.cont_rgn) == Region.rect(CONTENT)
    struc = read_rgn(m.mem, rec.struc_rgn)
    assert struc.bbox == Rect(o.v, o.h, o.v + h + s, o.h + w + s)
    assert struc.contains(Point(o.v, o.h)) and struc.contains(Point(o.v + h, o.h + w))
    assert not struc.contains(Point(o.v, o.h + w))                 # the shadow's offset
    assert not struc.contains(Point(o.v + h, o.h))


def test_wdef_1_rounds_its_windows_corners(qapp):
    g = Guest()
    rec = _window(g, proc_id=k.rDocProc)
    g("SystemWDEF1", 0, rec.addr, k.wCalcRgns, 0)
    f, o = _frame(rec, k.rDocProc)
    struc = read_rgn(g.mem, rec.struc_rgn)
    assert not struc.contains(o) and struc.contains(Point(o.v, o.h + f.corner))
    cont = read_rgn(g.mem, rec.cont_rgn)
    assert cont.bbox == CONTENT and not cont.contains(Point(CONTENT.bottom - 1, CONTENT.left))


def test_wdef_0_says_where_a_point_is(qapp):
    g = Guest()
    rec = _window(g)
    f, o = _frame(rec, k.documentProc)
    w, h = chrome.structure_size(f, CONTENT.width, CONTENT.height)
    close = chrome.close_box_rect(f, w).center()

    def hit(pt):
        return g("SystemWDEF0", 0, rec.addr, k.wHit, _at(pt))

    assert hit(Point(CONTENT.top + 5, CONTENT.left + 5)) == k.wInContent
    assert hit(Point(CONTENT.bottom - 2, CONTENT.right - 2)) == k.wInGrow
    assert hit(Point(o.v + 5, o.h + w // 2 + 40)) == k.wInDrag
    assert hit(Point(o.v + close.y(), o.h + close.x())) == k.wInGoAway
    assert hit(Point(o.v - 1, o.h)) == k.wNoHit
    rec.hilited = False                     # an inactive window has no boxes
    assert hit(Point(o.v + close.y(), o.h + close.x())) == k.wInDrag
    assert hit(Point(CONTENT.bottom - 2, CONTENT.right - 2)) == k.wInContent


def test_wdef_0_draws_in_the_window_manager_ports_image(qapp):
    """wDraw draws the frame in the image the Window Manager port draws in
    while an own WDEF draws, and turns the close box's highlighting over
    for wDraw with wInGoAway."""
    g = Guest()
    rec = _window(g)
    g("SystemWDEF0", 0, rec.addr, k.wCalcRgns, 0)
    box = read_rgn(g.mem, rec.struc_rgn).bbox
    img = QImage(box.width, box.height, QImage.Format.Format_RGB32)
    img.fill(QColor(255, 255, 255))
    wmgr = g.mem.r32(lowmem.WMgrPort)
    f, o = _frame(rec, k.documentProc)
    w = chrome.structure_size(f, CONTENT.width, CONTENT.height)[0]
    close = chrome.close_box_rect(f, w).center()

    def black(x, y):
        return img.pixel(x, y) & 0xFFFFFF == 0

    def burst():
        """The pixel a pressed close box blackens, right of its centre."""
        return black(close.x() + 2, close.y())

    g.tb.qd.surfaces[wmgr] = ImageSurface(img, box)
    g("SystemWDEF0", 0, rec.addr, k.wDraw, 0)
    assert black(0, 0) and black(w - 1, 0) and not black(w // 2, chrome.insets(f)[1] + 30)
    assert not burst()
    g("SystemWDEF0", 0, rec.addr, k.wDraw, k.wInGoAway)
    assert burst()
    g("SystemWDEF0", 0, rec.addr, k.wDraw, k.wInGoAway)
    assert not burst()
    del g.tb.qd.surfaces[wmgr]
    g("SystemWDEF0", 0, rec.addr, k.wDraw, k.wInGoAway)          # nowhere to draw: nothing


def test_an_own_wdef_is_called_in_the_window_manager_port(qapp):
    g = Guest()
    code = g.handle(bytes.fromhex("4EF9 0010 0000"))                # JMP $100000
    rec = _window(g, proc_id=128 << 4 | 3)
    rec.window_def_proc = 3 << 24 | code
    seen = []
    g.procs[g.heap.deref(code)] = lambda *args: seen.append(
        (args, g.mem.r32(g.mem.r32(g.process.partition.a5)))) or k.wInDrag
    g.set_port(rec)
    assert rec.own_wdef
    assert wdefs.message(g, rec, k.wHit, 0x00100020) == k.wInDrag
    assert seen == [((3, rec.addr, k.wHit, 0x00100020), g.mem.r32(lowmem.WMgrPort))]
    assert g.mem.r32(g.mem.r32(g.process.partition.a5)) == rec.addr
    rts = g.handle(bytes.fromhex("4E75"))
    rec.window_def_proc = rts
    assert rec.own_wdef and wdefs.entry(g, rec) == 0                # nothing to run


def test_the_systems_wdefs_are_not_the_applications_own(qapp):
    g = Guest()
    rec = _window(g)
    assert not rec.own_wdef and wdefs.entry(g, rec) == 0
    assert wdefs.message(g, rec, k.wHit, 0) == 0 and g.calls == []


def test_a_window_its_wdef_shaped_takes_its_regions_with_it(qapp):
    g = Guest()
    rec = _window(g, proc_id=128 << 4)
    g("SystemWDEF0", 0, rec.addr, k.wCalcRgns, 0)
    rec.wdef_shaped = True
    struc = read_rgn(g.mem, rec.struc_rgn)
    rec.content = CONTENT.offset(30, -20)
    assert read_rgn(g.mem, rec.struc_rgn) == struc.offset(30, -20)
    assert read_rgn(g.mem, rec.cont_rgn) == Region.rect(CONTENT.offset(30, -20))
    rec.wdef_shaped = False                 # the host's frame: the content region's its rect
    write_rgn(g.heap, rec.cont_rgn, Region())
    rec.content = CONTENT
    assert read_rgn(g.mem, rec.cont_rgn) == Region.rect(CONTENT)


def test_a_wdefs_structure_region_is_shown_as_far_as_its_on_the_screen(qapp):
    """Whatever a WDEF makes of its window's regions, there's no more of
    it to show (or image to draw it in) than the screen."""
    g = Guest()
    rec = _window(g, proc_id=128 << 4)
    write_rgn(g.heap, rec.struc_rgn, Region.rect(Rect(41, 130, 10648, 29955)))
    b = g.system.screen_bits.bounds
    assert rec.wdef_struc() == Region.rect(Rect(41, 130, b.bottom, b.right))
