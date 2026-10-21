"""Toolbox data structures that don't need a display."""

import math
import struct

import pytest
from guest import Guest, Processor

from macrt.m68k import dispatch, runtime
from macrt.toolbox import constants as k
from macrt.toolbox import lowmem
from macrt.toolbox.events import EventQueue, EventRecord, ProcessKilled
from macrt.toolbox.menus import Menu, parse_menu_items
from macrt.toolbox.types import Point, Rect
from macrt.toolbox.windows import WindowKind, describe_proc


def test_rect_basics():
    r = Rect.from_xywh(10, 20, 100, 50)
    assert (r.top, r.left, r.bottom, r.right) == (20, 10, 70, 110)
    assert (r.width, r.height) == (100, 50)
    assert r.contains(Point(20, 10))
    assert not r.contains(Point(70, 50))          # bottom/right edges are outside
    assert r.moved_to(0, 0) == Rect(0, 0, 50, 100)
    assert r.sized(30, 40) == Rect(20, 10, 60, 40)


def test_append_menu_metacharacters():
    items = parse_menu_items("Open…/O;(-;(Save/S;Bold<B;Checked!\x12;Icon^1")
    assert [i.text for i in items] == ["Open…", "-", "Save", "Bold", "Checked", "Icon"]
    assert items[0].cmd == "O" and items[0].enabled
    assert items[1].is_separator and not items[1].enabled
    assert items[2].cmd == "S" and not items[2].enabled
    assert items[3].style
    assert items[4].mark == k.checkMark
    assert items[5].icon == 1


class MenuGuest(Guest):
    menu = runtime.Runner.menu


def test_an_items_icon_marks_and_keys_read_back(qapp):
    g = MenuGuest()
    g.tb.menus_changed = lambda: None                   # no display to show them on
    h = g("NewMenu", 200, g.ptr(b"\x05Disks"))
    g.menu(h).append(parse_menu_items("Slot 6^3!\x12/D;Eject"))      # as AppendMenu does
    out = g.ptr(bytes(2))

    def get(name, item):
        g.mem.w16(out, 0xFFFF)
        g(name, h, item, out)
        return g.mem.r16(out)
    assert [get("GetItemIcon", 1), get("GetItemMark", 1), get("GetItemCmd", 1)] == \
        [3, 0x12, ord("D")]
    assert [get("GetItemIcon", 2), get("GetItemMark", 2), get("GetItemCmd", 2)] == [0, 0, 0]
    assert get("GetItemIcon", 9) == 0                   # no such item


def test_open_paren_anywhere_disables_the_item():
    # Same as the real AppendMenu, which is why the demo avoids "(".
    (item,) = parse_menu_items("Modal Dialog (dBoxProc)")
    assert item.text == "Modal Dialog dBoxProc)"
    assert not item.enabled


def test_menu_selectable():
    m = Menu(128, "File", parse_menu_items("New;(-;(Gone;Quit"))
    assert [m.selectable(i) for i in range(0, 6)] == [False, True, False, False, True, False]
    m.enabled = False
    assert not m.selectable(1)


@pytest.mark.parametrize("proc_id, kind, grow, zoom", [
    (k.documentProc, WindowKind.DOCUMENT, True, False),
    (k.zoomDocProc, WindowKind.DOCUMENT, True, True),
    (k.noGrowDocProc, WindowKind.DOCUMENT, False, False),
    (k.zoomNoGrow, WindowKind.DOCUMENT, False, True),
    (k.dBoxProc, WindowKind.DBOX, False, False),
    (k.plainDBox, WindowKind.PLAIN, False, False),
    (k.altDBoxProc, WindowKind.ALT_DBOX, False, False),
    (k.movableDBoxProc, WindowKind.MOVABLE_DBOX, False, False),
    (k.rDocProc, WindowKind.ROUNDED, False, False),
])
def test_describe_proc(proc_id, kind, grow, zoom):
    d = describe_proc(proc_id)
    assert (d.kind, d.grow, d.zoom) == (kind, grow, zoom)


def test_rounded_corner_from_variation():
    assert describe_proc(k.rDocProc).corner == 16
    assert describe_proc(k.rDocProc + 1).corner == 4


def _queue(updates=()):
    pending = list(updates)
    q = EventQueue(lambda: (Point(0, 0), 0))
    return q, (lambda: pending.pop(0) if pending else None)


def test_event_priority_activate_then_input_then_update():
    q, next_update = _queue(["win-needing-update"])
    q.post(EventRecord(k.mouseDown, 0, 0, Point(5, 5), 0))
    q.post_activate("win", True)
    order = [q.get(k.everyEvent, 0, next_update)[1].what for _ in range(3)]
    assert order == [k.activateEvt, k.mouseDown, k.updateEvt]
    got, ev = q.get(k.everyEvent, 0, next_update)
    assert not got and ev.what == k.nullEvent


def test_activate_then_deactivate_before_reading_cancels_out():
    q, next_update = _queue()
    q.post_activate("win", True)
    q.post_activate("win", False)
    assert not q.get(k.everyEvent, 0, next_update)[0]


def test_a_second_activate_the_same_way_isnt_doubled():
    q, next_update = _queue()
    q.post_activate("win", True)
    q.post_activate("win", True)
    got, ev = q.get(k.everyEvent, 0, next_update)
    assert got and ev.what == k.activateEvt and ev.modifiers & k.activeFlag
    assert not q.get(k.everyEvent, 0, next_update)[0]


def test_event_mask_leaves_other_events_queued():
    q, next_update = _queue()
    q.post(EventRecord(k.mouseDown, 0, 0, Point(0, 0), 0))
    q.post(EventRecord(k.keyDown, 0x0061, 0, Point(0, 0), 0))
    got, ev = q.get(k.event_mask(k.keyDown), 0, next_update)
    assert got and ev.what == k.keyDown
    got, ev = q.get(k.everyEvent, 0, next_update)
    assert got and ev.what == k.mouseDown


def test_flush_events_by_mask_up_to_the_stop_mask():
    q, next_update = _queue()
    for what in (k.keyDown, k.mouseDown, k.keyDown, k.mouseUp, k.keyDown):
        q.post(EventRecord(what, 0, 0, Point(0, 0), 0))
    q.flush(k.event_mask(k.keyDown), k.event_mask(k.mouseUp))
    left = [q.get(k.everyEvent, 0, next_update)[1].what for _ in range(3)]
    assert left == [k.mouseDown, k.mouseUp, k.keyDown]
    q.flush()
    assert not q.get(k.everyEvent, 0, next_update)[0]


def test_flush_events_leaves_high_level_events():
    """They're in a queue of their own on a Mac."""
    q, next_update = _queue()
    for what in (k.keyDown, k.kHighLevelEvent, k.mouseDown):
        q.post(EventRecord(what, 0, 0, Point(0, 0), 0))
    q.flush(k.everyEvent, k.highLevelEventMask)
    got, ev = q.get(k.everyEvent, 0, next_update)
    assert got and ev.what == k.kHighLevelEvent
    assert not q.get(k.everyEvent, 0, next_update)[0]


def test_killed_queue_raises_in_guest():
    q, next_update = _queue()
    q.kill()
    with pytest.raises(ProcessKilled):
        q.get(k.everyEvent, 60, next_update)


def test_key_translate_by_the_us_layout(qapp):
    g = Guest()
    state = g.ptr(b"\xFF" * 4)

    def key(code, mods=0):
        return g("KeyTranslate", 0, (mods & 0xFF00) | code, state)
    assert key(0x00) == ord("a")
    assert key(0x00, k.shiftKey) == key(0x00, k.alphaLock) == ord("A")
    assert key(0x12, k.shiftKey) == ord("!") and key(0x12, k.alphaLock) == ord("1")
    assert key(0x00, k.controlKey) == 0x01
    assert key(0x80 | 0x24) == 0x0D                         # key up: the same key
    assert (key(0x7B), key(0x53), key(0x4C)) == (0x1C, ord("1"), 0x03)
    assert (key(0x46), key(0x46, k.shiftKey)) == (0x1C, ord("+"))     # a Mac Plus's Left
    assert key(0x3F) == 0                                   # no such key
    assert g.mem.r32(state) == 0                            # no dead key pending


def test_key1trans_and_key2trans_are_keytranslate(qapp):
    # Programs that call the ROM's translation from low memory themselves:
    # the key code in D2 (a keypad key's less $40 for Key2Trans), the
    # modifiers' high byte in D1.
    m = Processor()
    key2 = m.ptr(runtime.KEY_TRANS)
    key1 = key2 + 4

    def trans(at, code, mods=0):
        m.cpu.set_d(3, 0x1234)
        m.run(struct.pack(">HHHI", 0x7200 | mods, 0x7400 | code, 0x4EB9, at))
        assert m.cpu.d(3) == 0x1234 and m.cpu.sp == m.stack_top
        return m.cpu.d(0)
    assert trans(key1, 0x12) == ord("1")
    assert trans(key1, 0x12, k.shiftKey >> 8) == ord("!")
    assert trans(key1, 0x30) == 0x09                    # Tab
    assert trans(key2, 0x53 - 0x40) == ord("1")         # the keypad's
    # Key Caps turns dead keys off with the flag word after Key1Trans's
    # first instruction while it asks about a key.
    assert m.mem.r16(key1 + 2) == 0xFFFF
    m.mem.w16(key1 + 2, 0)
    assert trans(key1, 0x00) == ord("a")


def test_the_cursor_vectors_shield_a_global_rectangle(qapp, monkeypatch):
    """JShieldCursor's caller pushes the rectangle's top left, then its
    bottom right, as the Alarm Clock does, and it's popped."""
    m = Processor()
    vectors = m.ptr(runtime.CURSOR_VECTORS)
    shielded = []
    monkeypatch.setitem(dispatch.IMPLEMENTATIONS, "ShieldCursor", lambda t, r, offset: (
        shielded.append((struct.unpack(">4h", t.mem.read(r, 8)), offset))))
    m.cpu.set_d(0, 0x1234)
    m.run(struct.pack(">HhhHhhHI", 0x2F3C, 10, 20, 0x2F3C, 30, 40, 0x4EB9, vectors + 16))
    assert shielded == [((10, 20, 30, 40), Point(0, 0))]
    assert m.cpu.sp == m.stack_top and m.cpu.d(0) == 0x1234
    for vector in range(4):                             # Hide, Show, Init and Obscure
        m.run(struct.pack(">HI", 0x4EB9, vectors + 4 * vector))
        assert m.cpu.sp == m.stack_top
    assert m.missing == []


def test_delta_point_is_a_less_b(qapp):
    g = Guest()
    assert g("DeltaPoint", Point(10, 20), Point(3, 25)) == 0x0007FFFB


def test_pt_to_angle_goes_round_from_12_oclock_as_the_rect_has_it(qapp):
    g = Guest()
    r = g.ptr(struct.pack(">4h", 0, 0, 100, 200))    # twice as wide as it's high
    angle = g.ptr(b"\xFF\xFF")

    def at(v, h):
        g("PtToAngle", r, Point(v, h), angle)
        return g.mem.r16(angle)
    assert [at(0, 100), at(0, 200), at(50, 200), at(100, 100), at(100, 0), at(50, 0)] == \
        [0, 45, 90, 180, 225, 270]
    assert at(50, 100) == 0                         # the centre
    assert at(-50, 150) == 14                       # atan(1/4), above the rect


def test_inset_rect_moves_only_the_sides(qapp):
    # PhrazeCraze insets a letter's square to nothing, then out again.
    g = Guest()
    r = g.ptr(struct.pack(">4h", 104, 261, 122, 275))

    def inset(dh, dv):
        g("InsetRect", r, dh, dv)
        return struct.unpack(">4h", g.mem.read(r, 8))
    assert inset(7, 0) == (104, 268, 122, 268)
    assert inset(0, 10) == (114, 268, 112, 268)     # inside out, as the ROM leaves it
    assert inset(-7, -10) == (104, 261, 122, 275)


def test_hwpriv_has_no_caches_to_mind(qapp):
    m = Processor()
    assert dispatch.implemented(dispatch.trap_key(0xA198))
    m.run(b"\x70\x01\xa1\x98")                  # MOVEQ #1,D0; _HWPriv: FlushInstructionCache
    assert m.cpu.d(0) == 0
    m.run(b"\x70\x07\xa1\x98")                  # selector 7, not a cache's
    assert m.cpu.d(0) == 0xFFFFFE0A                 # hwParamErr
    assert m.missing == ["HWPriv selector 7"]


def test_a_boolean_result_fills_its_whole_word(qapp):
    # MacWrite leaves the slot as it was and then tests the word, not the byte.
    m = Processor()
    r = m.ptr(bytes.fromhex("0000 0000 0010 0010"))         # not empty
    m.cpu.push16(0xFFFF)
    m.cpu.push32(r)
    m.run(b"\xa8\xae")                                      # _EmptyRect
    assert m.cpu.pop16() == 0x0000
    m.cpu.push16(0x00FF)
    m.cpu.push32(m.ptr(bytes(8)))
    m.run(b"\xa8\xae")
    assert m.cpu.pop16() == 0x0100                          # true, in the high byte


def test_packages_need_no_loading(qapp):
    m = Processor()
    m.cpu.push16(7)
    m.run(b"\xa9\xe5\xa9\xe6")                  # InitPack(7), InitAllPacks
    for selector in (2, 4):                         # DILoad, DIUnload
        m.cpu.push16(selector)
        m.run(b"\xa9\xe9")                        # _Pack2
    assert m.missing == [] and m.cpu.sp == m.stack_top


def test_init_resources_gives_the_system_files_refnum(qapp):
    m = Processor()
    rm = m.process.resources
    current = rm.CurResFile()
    m.cpu.push16(0xFFFF)
    m.run(b"\xa9\x95")                          # _InitResources
    assert m.cpu.pop16() == rm.sys_refnum & 0xFFFF
    assert rm.CurResFile() == current and m.missing == []


def test_flushing_the_code_cache_drops_translated_code(qapp, monkeypatch):
    m = Processor()
    flushed = []
    monkeypatch.setattr(m.cpu, "flush_code", lambda: flushed.append(True))
    m.run(b"\xa0\xbd")                          # _FlushCodeCache
    m.run(b"\x70\x01\xa1\x98")                  # _HWPriv: FlushInstructionCache
    m.run(b"\x70\x03\xa1\x98")                  # FlushDataCache: no code in that one
    assert len(flushed) == 2 and m.missing == []


def test_method_dispatch_finds_the_method_of_the_class_or_its_nearest_superclass(qapp):
    """Q-Sheet's Object Pascal methods are called through _MethodDispatch,
    a call's own stub followed by the classes with the method: the class
    first that's found for, then each class, highest numbered first.
    Class 2 is the root, 4 and 8 its subclasses, 6 4's."""
    m = Processor()
    mem = m.mem
    a5 = m.ptr(bytes(0x40))
    mem.write(a5 + 0x10, bytes.fromhex("7002 4e75"))        # class 2's: MOVEQ #2,D0; RTS
    mem.write(a5 + 0x20, bytes.fromhex("7004 4e75"))        # class 4's
    m.cpu.set_a(5, a5)
    classes = m.ptr(struct.pack(">5H", 0, 0, 2, 4, 2))      # each one's superclass
    mem.w32(lowmem.ClassTable, m.ptr(struct.pack(">I", classes)))
    mem.w32(lowmem.MethodNotFound, m.ptr(bytes.fromhex("70ff 4e75")))   # MOVEQ #-1,D0
    both = m.ptr(bytes.fromhex("a9f8 0001 0000 0000 0004 0020 0002 0010 0000"))
    only_4 = m.ptr(bytes.fromhex("a9f8 0000 0000 0000 0004 0020 0000"))

    def call(stub, cls):
        obj = m.handle(struct.pack(">H", cls))
        m.cpu.set_d(0, 0x1234)
        m.run(struct.pack(">HIHIH", 0x2F3C, obj, 0x4EB9, stub, 0x588F))
        assert m.cpu.sp == m.stack_top
        return m.cpu.d(0) & 0xFFFF
    assert [call(both, cls) for cls in (6, 4, 2, 8)] == [4, 4, 2, 2]
    assert mem.read(both + 2, 6) == bytes.fromhex("0001 0008 0010")   # the last found
    assert call(both, 8) == 2                               # from there
    assert call(only_4, 6) == 4
    assert call(only_4, 8) == 0xFFFF                        # none
    assert mem.r32(only_4 + 4) == 0
    assert m.missing == []


def test_slopes_and_angles_as_inside_macintosh_draws_them(qapp):
    """IM I-475: the slope is dh/dv, v growing downward, of a line that many
    degrees clockwise from 12 o'clock; each angle comes back from its slope."""
    g = Guest()
    assert g("SlopeFromAngle", 45) == -0x10000 and g("AngleFromSlope", -0x10000) == 45
    assert g("SlopeFromAngle", 0) == 0 and g("AngleFromSlope", 0) == 180
    assert g("SlopeFromAngle", -45) == g("SlopeFromAngle", 135) == 0x10000
    assert all(g("AngleFromSlope", g("SlopeFromAngle", a)) == a for a in range(1, 181))


def extended(v: float) -> bytes:
    m, e = math.frexp(abs(v))
    return struct.pack(">HQ", (v < 0) << 15 | (e + 16382 if m else 0), int(m * 2 ** 64))


def test_fixed_point_routines_give_inside_macintoshs_examples(qapp):
    """The table of examples in Inside Macintosh IV-65, its values made
    with X2Fix and X2Frac."""
    g = Guest()

    def fix(v):
        return g("R_X2Fix", g.ptr(extended(v)))

    def frac(v):
        return g("R_X2Frac", g.ptr(extended(v)))

    def u(name, *args):
        return g(name, *args) & 0xFFFFFFFF
    pi = 3.1416015625                       # 4 * FixATan2(1, 1)
    for s in (1, -1):
        assert u("FixDiv", fix(s * 1.95), fix(1.30)) == (0x00018000, 0xFFFE8000)[s < 0]
        assert u("FracDiv", frac(s * 1.95), frac(1.30)) == (0x60000000, 0xA0000000)[s < 0]
        assert u("FracMul", frac(s * 1.50), frac(1.30)) == (0x7CCCCCCD, 0x83333333)[s < 0]
        assert u("FracSin", fix(s * pi)) == 0
        assert u("FracCos", fix(s * pi)) == 0xC0000000
        assert u("Fix2Long", fix(s * 1.75)) == (2, 0xFFFFFFFE)[s < 0]
        assert u("Fix2Frac", fix(s * 1.75)) == (0x70000000, 0x90000000)[s < 0]
        assert u("Frac2Fix", frac(s * 1.75)) == (0x0001C000, 0xFFFE4000)[s < 0]
        assert u("FixATan2", fix(s * 1.0), fix(s * 1.0)) == (0x0000C910, 0xFFFDA4D0)[s < 0]
    assert u("FracSqrt", frac(1.96)) == 0x5999999A
    assert u("FracSqrt", 0xFFFFFFFF) == 0x80000000          # unsigned, just under 4
    assert u("FracSin", fix(pi / 2)) == 0x40000000
    assert u("FixMul", fix(1.5), fix(-2.0)) == 0xFFFD0000
    assert u("FixMul", 0x7FFF0000, 0x7FFF0000) == 0x7FFFFFFF  # held
    assert u("FixDiv", -1, 0) == 0x80000000 and u("FracDiv", 0, 0) == 0x7FFFFFFF
    assert u("FixRatio", 1, 3) == 0x5555 and u("FixRatio", -2, 3) == 0xFFFF5555
    assert u("Long2Fix", -3) == 0xFFFD0000 and u("Long2Fix", 0x8000) == 0x7FFFFFFF
