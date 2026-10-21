"""Toolbox records in guest memory: the System's memory map, partitions,
windows and menus, checked byte for byte against Inside Macintosh."""

import pytest
from guest import Guest, Processor

from macrt.m68k import dispatch
from macrt.macfile import MacFile
from macrt.memory import from_mac
from macrt.process import Process
from macrt.resfork import Resource, build_fork
from macrt.toolbox import constants as k
from macrt.toolbox import lowmem, sysres
from macrt.toolbox.api import Toolbox
from macrt.toolbox.events import EventQueue
from macrt.toolbox.menus import MenuInfo, parse_menu_items
from macrt.toolbox.quickdraw import (
    BLACK_PAT, GRAY_PAT, QD_ARROW, QD_BLACK, QD_GRAY, QD_SCREEN_BITS, GrafPort, rgn_bbox,
)
from macrt.toolbox.system import RAM_SIZE, ROM_SIZE, STANDARD_DEFPROCS, System
from macrt.toolbox.types import Point, Rect
from macrt.toolbox.windows import WindowRecord


def make(name="Test", **kw):
    system = System((640, 480), desk_top=20, **kw)
    return system, new_process(system, name)


def new_process(system, name):
    return Process(system, name, lambda tb: None, EventQueue(lambda: (Point(0, 0), 0)))


def new_window(system, proc, bounds=Rect(50, 40, 210, 300), title="Untitled",
               proc_id=k.zoomDocProc, ref_con=0):
    return WindowRecord.new(system, proc, proc.heap, 0, bounds, title, True, proc_id, True, ref_con)


# -- the machine ---------------------------------------------------------------
def test_low_memory_describes_the_machine():
    system, _ = make()
    m = system.mem
    assert m.r32(lowmem.MemTop) == RAM_SIZE == m.size - ROM_SIZE   # the ROM is over it
    assert m.r32(lowmem.ScrnBase) == system.screen_bits.base == RAM_SIZE - 80 * 480
    assert m.r16(lowmem.ScreenRow) == 80
    assert m.r16(lowmem.MBarHeight) == 20
    assert m.r32(lowmem.SysZone) == system.sys_heap.start
    wmgr = system.port_at(m.r32(lowmem.WMgrPort))
    assert isinstance(wmgr, GrafPort) and wmgr.port_rect == Rect(0, 0, 480, 640)
    assert rgn_bbox(m, m.r32(lowmem.GrayRgn)) == Rect(20, 0, 480, 640)


def test_init_windows_replaces_a_window_manager_port_thrown_away(qapp):
    """Rogue's start disposes of the Window Manager port and sets WWExist
    to say there's none; its InitWindows then has to make another."""
    g = Processor()
    g.cpu.set_a(5, g.process.partition.a5)
    m = g.mem
    first = g.system.port_at(m.r32(lowmem.WMgrPort))
    g("InitWindows")
    assert g.system.port_at(m.r32(lowmem.WMgrPort)) is first        # still there: kept
    m.w16(lowmem.WWExist, 0xFFFF)                                   # and QDExist
    g("DisposePtr", first.addr)
    g("InitWindows")
    wmgr = g.system.port_at(m.r32(lowmem.WMgrPort))
    assert wmgr is not first and m.r8(lowmem.WWExist) == 0
    assert rgn_bbox(m, wmgr.vis_rgn) == wmgr.port_rect == g.system.screen_bits.bounds
    assert m.r32(m.r32(g.process.partition.a5)) == wmgr.addr       # thePort
    g("InitGraf", m.r32(g.process.partition.a5))
    assert m.r8(lowmem.QDExist) == 0


def test_init_graf_puts_the_globals_at_the_a5_the_program_runs_with(qapp):
    """Fokker Triplane moves its A5 world below $70000 before InitGraf: a
    desk accessory finds QuickDraw's globals at 0(A5), and so does the rest
    of QuickDraw from then on."""
    g = Processor()
    m = g.mem
    a5 = g.ptr(bytes(0x400)) + 0x300
    g.cpu.set_a(5, a5)
    g.run(bytes.fromhex("486DFFFC" "A86E"))                    # PEA -4(A5); _InitGraf
    assert m.r32(a5) == a5 - 4 and m.r32(a5 - 4) == 0
    assert m.r32(a5 - 4 + QD_SCREEN_BITS) == g.system.screen_bits.base
    port, _ = g.port()
    assert m.r32(a5 - 4) == port.addr
    g("SetPort", g.system.port_at(m.r32(lowmem.WMgrPort)).addr)
    assert m.r32(a5 - 4) == m.r32(lowmem.WMgrPort)


def test_screen_too_big_for_ram():
    with pytest.raises(ValueError):
        System((16000, 4000))


def test_partition_layout_and_a5_world():
    system, proc = make()
    m, part = system.mem, proc.partition
    assert part.start == system.partitions_start and part.end == part.start + (1 << 20)
    assert part.start < 1 << 20                 # Excel 1.0 needs some of its heap under 1 MB
    assert part.heap.start == part.start and part.heap.end < part.stack_base < part.a5 < part.end
    w = proc.world
    assert w.r32(lowmem.ApplZone) == w.r32(lowmem.TheZone) == part.heap.start
    assert w.r32(lowmem.HeapEnd) == part.heap.end
    assert w.r32(lowmem.CurStackBase) == part.stack_base
    assert w.r32(lowmem.CurrentA5) == part.a5
    assert w.read(lowmem.CurApName, 5) == b"\x04Test"
    # QuickDraw globals, below thePort, which 0(A5) points at
    the_port = m.r32(part.a5)
    assert the_port == part.a5 - 4 and m.r32(the_port) == 0
    assert m.read(the_port + QD_BLACK, 8) == BLACK_PAT
    assert m.read(the_port + QD_GRAY, 8) == GRAY_PAT
    assert m.r16(the_port + QD_ARROW + 2) == 0x4000
    assert m.r32(the_port + QD_SCREEN_BITS) == system.screen_bits.base
    assert m.r_rect(the_port + QD_SCREEN_BITS + 6) == Rect(0, 0, 480, 640)


def test_globals_are_exactly_as_big_as_code_0_says():
    """CurStackBase is A5 minus CODE 0's size below A5, to the byte: an
    application's start-up code fills its globals from there up to A5."""
    system = System((640, 480), desk_top=20)
    part = system.open_partition("Data A", below_a5=0x7E72, qd_globals=False)
    assert part.a5 - part.stack_base == 0x7E72
    assert part.world.r32(lowmem.CurStackBase) == part.stack_base
    assert part.heap.limit <= part.stack_base


def test_partitions_are_separate_and_reused():
    system, a = make("A")
    b = new_process(system, "B")
    assert b.partition.start == a.partition.end
    assert system.heap_of(a.heap.new_ptr(4)) is a.heap
    assert system.heap_of(b.heap.new_ptr(4)) is b.heap
    a.close()
    c = new_process(system, "C")
    assert c.partition.start == system.partitions_start
    with pytest.raises(ValueError):
        system.heap_of(system.screen_bits.base)


def test_defproc_handles():
    system, _ = make()
    h = system.defproc("WDEF", 0)
    assert system.defproc("WDEF", 0) == h and system.defproc("WDEF", 1) != h
    assert system.defproc_id(h) == ("WDEF", 0)
    # MOVE.W #$067E,D0; _DialogDispatch (auto-pop): the Python WDEF 0
    assert system.mem.read(system.sys_heap.deref(h), 6) == bytes.fromhex("303C067EAE68")
    other = system.defproc("WDEF", 7)
    assert system.mem.r16(system.sys_heap.deref(other)) == 0x4E75      # RTS: there's none


@pytest.mark.parametrize("kind, def_id", list(STANDARD_DEFPROCS))
def test_68k_code_can_call_a_standard_definition(qapp, kind, def_id):
    """Each one's code takes its arguments off the stack and leaves the
    result, if it has one, as a Pascal routine does."""
    m = Processor()
    sel = STANDARD_DEFPROCS[kind, def_id]
    d = dispatch.DISPATCHERS[dispatch.trap_key(0xAA68)]
    sig = d[2][sel]
    assert sig.name == f"System{kind}{def_id}" and sel >> 8 == sum(
        dispatch.SIZES[c] for c in sig.stack) // 2
    entry = m.system.sys_heap.deref(m.system.defproc(kind, def_id))
    result = 4 if sig.result else 0
    m.run(bytes.fromhex("42A7" * (result // 4)                      # CLR.L -(SP)
                        + "4267" * (sel >> 8)                       # CLR.W -(SP): NIL and 0s
                        + f"207C{entry:08X}4E90"))                  # JSR the code
    assert m.cpu.sp == m.stack_top - result
    assert m.mem.read(m.cpu.sp, result) == bytes(result)
    if kind == "MDEF":                                              # drawn on the host
        assert m.missing == [f"the System's {kind} {def_id}, message 0"]
    else:                       # no window or control to answer for; nothing for mbDraw
        assert m.missing == []


def resource_data(system, proc, kind, rid):
    h = proc.resources.GetResource(kind, rid)
    return h and system.mem.read(system.heap_of(h).deref(h), system.heap_of(h).handle_size(h))


def test_the_system_file_has_the_standard_resources():
    system, proc = make()
    packs = [resource_data(system, proc, "PACK", n) for n in range(16)]
    assert packs[0] == b"\xAD\xE7" and packs[6] == b"\xAD\xED"     # _Pack0, _Pack6 auto-pop
    assert packs[8] == b"\xAC\x16" and packs[15] == b"\xAC\x31"
    for rid in (0, 1, 2):
        assert len(resource_data(system, proc, "ICON", rid)) == 128
    watch = resource_data(system, proc, "CURS", 4)
    assert len(watch) == 68 and watch[64:] == b"\x00\x08\x00\x06"   # hot spot (8, 6)
    rows = [int.from_bytes(watch[i:i + 2], "big") for i in range(0, 64, 2)]
    data, mask = rows[:16], rows[16:]
    assert all(d & ~m == 0 for d, m in zip(data, mask))           # the mask covers it
    assert mask[7] & 0x0800 and not data[7] & 0x0800              # and fills the face
    assert proc.resources.GetResource("CURS", 5) == 0


def test_a_real_system_files_own_resources_stay():
    stop = bytes(range(128))
    f = MacFile("System", rsrc=build_fork([Resource(b"ICON", 0, stop)]))
    system = System((640, 480), desk_top=20, system_file=f)
    proc = new_process(system, "Test")
    assert resource_data(system, proc, "ICON", 0) == stop
    assert resource_data(system, proc, "ICON", 1) == sysres.NOTE_ICON
    assert resource_data(system, proc, "PACK", 6) == sysres.package(6)


# -- windows ---------------------------------------------------------------------
def test_window_record_fields():
    system, proc = make()
    rec = new_window(system, proc, ref_con=0x1234)
    m, a = system.mem, rec.addr
    assert proc.heap.ptr_size(a) == WindowRecord.SIZE == 156
    assert system.port_at(a) is rec
    assert m.r16s(a + 108) == k.userKind
    assert (m.r8(a + 110), m.r8(a + 111), m.r8(a + 112), m.r8(a + 113)) == (1, 0, 1, 1)
    assert m.r_pstr(m.r32(m.r32(a + 134))) == b"Untitled"       # titleHandle
    wdp = m.r32(a + 126)                                         # windowDefProc
    assert wdp >> 24 == 8 and system.defproc_id(wdp) == ("WDEF", 0)
    assert rec.proc_id == k.zoomDocProc
    assert m.r32s(a + 152) == 0x1234
    # portRect is local; portBits.bounds puts it on the screen
    assert m.r_rect(a + 16) == Rect(0, 0, 160, 260)
    assert m.r_rect(a + 8) == Rect(-50, -40, 430, 600)
    assert rec.content == Rect(50, 40, 210, 300)
    assert rgn_bbox(m, m.r32(a + 118)) == rec.content           # contRgn, global
    assert rgn_bbox(m, m.r32(a + 122)) == rec.content           # updateRgn: all of it
    assert rgn_bbox(m, rec.vis_rgn) == rec.port_rect            # visRgn, local
    assert rec.local_to_global(Point(0, 0)) == Point(50, 40)


def test_window_pic_is_the_records_window_pic():
    g = Guest()
    rec = new_window(g.system, g.process)
    g("SetWindowPic", rec.addr, 0x123456)
    assert g.mem.r32(rec.addr + 148) == 0x123456                # windowPic
    assert g("GetWindowPic", rec.addr) == 0x123456
    assert g.mem.r32(rec.addr + 144) == 0                       # nextWindow untouched


def test_window_follows_its_record():
    system, proc = make()
    rec = new_window(system, proc)
    m, a = system.mem, rec.addr
    # Code that pokes portBits.bounds moves the window.
    m.w_rect(a + 8, Rect(-60, -70, 420, 570))
    assert rec.content == Rect(60, 70, 220, 330)
    m.w8(a + 111, 0xFF)                                          # any nonzero is TRUE
    assert rec.hilited is True
    rec.title = "A much longer title than before"
    assert m.r_pstr(proc.heap.deref(rec.title_handle)) == b"A much longer title than before"


def test_moving_and_sizing():
    system, proc = make()
    rec = new_window(system, proc)
    rec.update_pending = False
    rec.invalidate(Rect(60, 50, 70, 60))
    rec.content = rec.content.offset(100, 10)
    assert rec.port_rect == Rect(0, 0, 160, 260)                 # local coordinates stay
    assert rgn_bbox(system.mem, rec.update_rgn) == Rect(70, 150, 80, 160)   # moved along
    rec.content = rec.content.sized(300, 200)
    assert rec.port_rect == Rect(0, 0, 200, 300) and rec.content.top_left == Point(60, 140)
    rec.update_pending = False
    rec.invalidate(Rect(0, 0, 1000, 1000))                       # clipped to the window
    assert rgn_bbox(system.mem, rec.update_rgn) == rec.content


def test_zoom_states_in_data_handle():
    system, proc = make()
    rec = new_window(system, proc)
    rec.std_state = Rect(23, 3, 477, 637)
    rec.user_state = rec.content
    m = system.mem
    assert proc.heap.handle_size(rec.data_handle) == 16
    assert m.r_rect(proc.heap.deref(rec.data_handle) + 8) == Rect(23, 3, 477, 637)
    assert not rec.zoomed_out
    rec.content = rec.std_state
    assert rec.zoomed_out
    assert new_window(system, proc, proc_id=k.documentProc).data_handle == 0


def test_dispose_window_gives_back_its_memory():
    system, proc = make()
    proc.heap.dispose_handle(proc.heap.new_handle(4))            # masters exist already
    free = proc.heap.free_bytes()
    rec = new_window(system, proc)
    assert proc.heap.free_bytes() < free
    rec.dispose()
    assert proc.heap.free_bytes() == free
    assert system.port_at(rec.addr) is None


def test_close_window_keeps_caller_storage():
    system, proc = make()
    storage = proc.heap.new_ptr(WindowRecord.SIZE)
    rec = WindowRecord.new(system, proc, proc.heap, storage, Rect(50, 40, 210, 300), "",
                           True, k.documentProc, True, 0)
    assert rec.addr == storage
    rec.dispose()
    assert proc.heap.ptr_size(storage) == WindowRecord.SIZE      # still allocated


def test_window_list_is_chained_through_next_window():
    system, proc = make()
    a, b, c = (new_window(system, proc, title=t) for t in "abc")
    wl = proc.windows
    wl.insert_front(a)
    wl.insert_back(c)
    wl.insert_behind(b, a)
    assert list(wl) == [a, b, c] and len(wl) == 3 and wl[1] is b and b in wl
    m = system.mem
    assert proc.world.r32(lowmem.WindowList) == a.addr
    assert (m.r32(a.addr + 144), m.r32(b.addr + 144), m.r32(c.addr + 144)) == (b.addr, c.addr, 0)
    assert m.r32(lowmem.WindowList) == 0          # not switched in yet
    proc.world.switch_in()
    assert m.r32(lowmem.WindowList) == a.addr
    wl.bring_to_front(c)
    assert list(wl) == [c, a, b] and m.r32(lowmem.WindowList) == c.addr
    wl.remove(a)
    assert list(wl) == [c, b] and a.next_window == 0
    assert list(reversed(wl)) == [b, c]
    proc.world.switch_out()
    wl.clear()
    assert not wl


# -- menus ---------------------------------------------------------------------------
def test_menu_info_layout():
    system, proc = make()
    menu = MenuInfo.new(system, proc.heap, 128, "File")
    menu.append(parse_menu_items("New/N;(-;Quit/Q"))
    m, p = system.mem, menu.ptr
    data = (b"\x04File" + b"\x03New" + bytes([0, ord("N"), 0, 0]) + b"\x01-" + bytes(4)
            + b"\x04Quit" + bytes([0, ord("Q"), 0, 0]) + b"\x00")
    assert m.r16s(p) == 128
    assert system.defproc_id(m.r32(p + 6)) == ("MDEF", 0)
    assert m.read(p + 14, len(data)) == data
    assert proc.heap.handle_size(menu.handle) == 14 + len(data)
    assert m.r32(p + 10) & 0xF == 0b1011          # menu, New, (separator off), Quit
    assert [i.text for i in menu.items] == ["New", "-", "Quit"]
    assert menu.count() == 3 and menu.selectable(1) and not menu.selectable(2)


def test_menu_info_changes():
    system, proc = make()
    menu = MenuInfo.new(system, proc.heap, 129, "Edit")
    menu.append(parse_menu_items("Undo;Cut;Copy"))
    menu.change_item(1, enabled=False)
    menu.change_item(3, mark=k.checkMark, text="Copy Everything")
    assert [(i.text, i.enabled, i.mark) for i in menu.items] == [
        ("Undo", False, ""), ("Cut", True, ""), ("Copy Everything", True, k.checkMark)]
    # menuData: title, Undo + 4, Cut + 4, then Copy Everything's text, icon, key, mark
    mark_at = menu.ptr + 14 + 5 + (5 + 4) + (4 + 4) + 16 + 2
    assert system.mem.r8(mark_at) == 0x12
    menu.enabled = False
    assert not menu.selectable(2) and system.mem.r32(menu.ptr + 10) & 1 == 0
    assert MenuInfo(system, menu.handle) == menu  # views compare by handle


class _Inline:
    """Just enough display for Toolbox calls that hop to the GUI thread."""
    class bridge:
        call = staticmethod(lambda fn, *args: fn(*args))
    hilite_menu = staticmethod(lambda *args: None)


def test_hierarchical_and_icon_items_have_no_command_key(qapp):
    system, proc = make()
    menu = MenuInfo.new(system, proc.heap, 129, "File")
    menu.append(parse_menu_items("Recent;Print/P;Icon"))
    menu.change_item(1, cmd=k.hMenuCmd, mark=from_mac(bytes([135])))
    menu.change_item(3, cmd="\x1e", icon=2)                 # small icon
    menu.change_item(2, enabled=False)                       # rewrites every item
    assert [(i.key, i.submenu) for i in menu.items] == [("", 135), ("P", 0), ("", 0)]
    recent = menu.ptr + 14 + 5 + 7                           # past the title and "Recent"
    assert system.mem.read(recent, 4) == bytes([0, 0x1B, 135, 0])
    menu.change_item(2, enabled=True)
    proc.menus.insert(menu)
    tb = Toolbox(_Inline, proc)
    assert tb.MenuKey("\x1b") == 0 and tb.MenuKey("\x1e") == 0
    assert tb.MenuKey("p") == 129 << 16 | 2


def test_items_past_31_are_always_enabled():
    system, proc = make()
    menu = MenuInfo.new(system, proc.heap, 130, "Fonts")
    menu.append(parse_menu_items(";".join(f"(Item {i}" for i in range(1, 34))))
    flags = [i.enabled for i in menu.items]
    assert not any(flags[:31]) and all(flags[31:])


def test_menu_list_layout():
    system, proc = make()
    heap, menus = proc.heap, proc.menus
    apple, file, edit = (MenuInfo.new(system, heap, i, t)
                         for i, t in ((1, k.APPLE_MENU_TITLE), (128, "File"), (129, "Edit")))
    menus.insert(file)
    menus.insert(edit)
    menus.insert(apple, before_id=128)
    menus.insert(file)                             # already there: no change
    m = system.mem
    h = proc.world.r32(lowmem.MenuList)
    p = heap.deref(h)
    assert m.r16(p) == 3 * 6                       # lastMenu
    assert [m.r32(p + 6 + 6 * i) for i in range(3)] == [apple.handle, file.handle, edit.handle]
    assert heap.handle_size(h) == 6 + 3 * 6 + 6
    assert [x.title for x in menus] == [k.APPLE_MENU_TITLE, "File", "Edit"]
    assert menus.get(129) == edit and menus.get(5) is None
    menus.delete(128)
    assert list(menus) == [apple, edit] and file not in menus
    menus.clear()
    assert len(menus) == 0 and m.r16(heap.deref(h)) == 0
