"""
Control Manager traps.

Controls are records in the application heap (see
macrt.toolbox.controls).  The standard controls, CDEF 0 and CDEF 1, are
drawn and hit-tested in Python.  Any other CDEF, or code an application
puts in contrlDefProc itself, is 68k code, called as the ROM calls it:
pascal long CDEF(short varCode, ControlHandle theControl, short message,
long param), with the owner window as the current port.

TrackControl and DragControl follow the mouse on the application's
thread, a tick at a time, and call its action procedure as they go.
"""

from __future__ import annotations

from contextlib import contextmanager

from PyQt6.QtGui import QPainterPath

from ..memory import ADDRESS_MASK, to_mac
from ..toolbox import constants as k
from ..toolbox import controls, lowmem
from ..toolbox.controls import TITLE, Control
from ..toolbox.qdraw import Region, patXor, read_rgn, rect_path
from ..toolbox.quickdraw import GRAY_PAT, new_rgn, set_rect_rgn
from ..toolbox.system import RTS
from ..toolbox.templates import parse_cntl
from ..toolbox.types import Point, Rect
from .dispatch import trap

ACTION_IN_RECORD = 0xFFFFFFFF       # actionProc POINTER(-1): use contrlAction
OUTSIDE_SLOP = Point(-0x8000, -0x8000)


# =========================================================================
# Finding things
# =========================================================================
def _ctl(t, h: int) -> Control | None:
    """The control a ControlHandle is, None for NIL or an empty handle."""
    h &= ADDRESS_MASK
    if not h or not t.mem.r32(h) & ADDRESS_MASK:
        return None
    return Control(t.mem, h)


def controls_of(t, w) -> list[Control]:
    """A window's controls, in list order (newest first)."""
    out, h, seen = [], w.control_list & ADDRESS_MASK, set()
    while h and h not in seen:
        seen.add(h)
        c = _ctl(t, h)
        if c is None:
            break
        out.append(c)
        h = c.next_control & ADDRESS_MASK
    return out


def _proc_id(t, c: Control) -> int:
    """The procID the control was made with: its CDEF's ID * 16 + variation.
    A contrlDefProc that is no CDEF resource is code the application put
    there itself (Excel does), so its "ID" is -1: never a standard one."""
    h = c.def_proc & ADDRESS_MASK
    kind, def_id = t.process.resources.defproc_id(h)
    if not kind:
        kind, def_id = t.system.defproc_id(h)
    return (def_id if kind == "CDEF" else -1) << 4 | c.variation


def _entry(t, c: Control) -> int:
    """Where a 68k CDEF starts; 0 if there's no code (no System file)."""
    h = c.def_proc & ADDRESS_MASK
    p = t.mem.r32(h) & ADDRESS_MASK
    if not p:
        t.process.resources.LoadResource(h)
        p = t.mem.r32(h) & ADDRESS_MASK
    return p if p and t.mem.r16(p) != RTS else 0


def _the_port_var(t) -> int:
    return t.mem.r32(t.process.partition.a5) & ADDRESS_MASK


# =========================================================================
# Talking to the definition procedure
# =========================================================================
def message(t, c: Control, msg: int, param: int = 0) -> int:
    """Send a CDEF message.  The standard definitions answer here; an
    application's own is called in its owner's port."""
    proc_id = _proc_id(t, c)
    if controls.standard(proc_id):
        return _standard(t, c, proc_id, msg, param)
    entry = _entry(t, c)
    if not entry:
        return 0
    var = _the_port_var(t)
    saved = t.mem.r32(var)
    t.mem.w32(var, c.owner)
    try:
        return t.call(entry, (("h", c.variation), ("p", c.handle), ("h", msg), ("l", param)),
                      "l")
    finally:
        t.mem.w32(var, saved)


def _standard(t, c: Control, proc_id: int, msg: int, param: int) -> int:
    if msg == k.drawCntl:
        w = t.window(c.owner)
        if w is not None:
            controls.draw(t.tb.qd, w, c, proc_id)
    elif msg == k.testCntl:
        if c.visible and c.active:
            return controls.test(c, proc_id, Point(_s16(param >> 16), _s16(param)))
    elif msg == k.posCntl:
        c.value = controls.value_at(c, _s16(param >> 16) if _vertical(c) else _s16(param))
    elif msg == k.thumbCntl:
        start = t.point(param)
        limit, slop, axis = controls.thumb_limits(c, start)
        t.set_rect(param, limit)
        t.set_rect(param + 8, slop)
        t.w16(param + 16, axis)
    elif msg in (k.calcCRgns, k.calcCntlRgn, k.calcThumbRgn):
        thumb = msg == k.calcThumbRgn or (msg == k.calcCRgns and param & 0x80000000)
        r = controls.Geometry(c).thumb if thumb and proc_id >> 4 == 1 else c.rect
        rgn = param & ADDRESS_MASK
        set_rect_rgn(t.system.heap_of(rgn), rgn, r or Rect(0, 0, 0, 0))
    return 0


def _system_cdef(t, def_id: int, var: int, h: int, msg: int, param: int) -> int:
    c = _ctl(t, h)
    return 0 if c is None else _standard(t, c, def_id << 4 | var & 0xF, msg, param)


@trap
def SystemCDEF0(t, var, h, msg, param):
    """CDEF 0's code, for 68k code that calls it itself: Excel's own CDEF
    passes it the messages it doesn't answer."""
    return _system_cdef(t, 0, var, h, msg, param)


@trap
def SystemCDEF1(t, var, h, msg, param):
    return _system_cdef(t, 1, var, h, msg, param)


@contextmanager
def scratch(t, size: int):
    """A block of guest memory for passing a record to 68k code."""
    p = t.process.heap.new_ptr(size, clear=True)
    if not p:
        t.system_error(25, "out of memory")         # dsMemFullErr
    try:
        yield p
    finally:
        t.process.heap.dispose_ptr(p)


def _s16(v: int) -> int:
    v &= 0xFFFF
    return v - 0x10000 if v & 0x8000 else v


def _vertical(c: Control) -> bool:
    r = c.rect
    return r.height > r.width


def _pt_param(pt: Point) -> int:
    return ((pt.v & 0xFFFF) << 16) | (pt.h & 0xFFFF)


def _test(t, c: Control, pt: Point) -> int:
    if not c.visible or not c.active:
        return 0
    return message(t, c, k.testCntl, _pt_param(pt)) & 0xFFFF


def _draw(t, c: Control, part: int = 0) -> None:
    if c.visible:
        message(t, c, k.drawCntl, part)


def _erase(t, c: Control) -> None:
    """What HideControl does: erase the control and have its window
    update what was behind it."""
    w = t.window(c.owner)
    if w is None:
        return
    r = c.rect
    t.tb.qd.fill(w, rect_path(r), w.bk_pat, k.patCopy)
    with t.system.lock:
        b = w.port_bits_bounds
        w.invalidate(r.offset(-b.left, -b.top))
    t.process.events.wake()


# =========================================================================
# Making and disposing of controls
# =========================================================================
def new_control(t, w, bounds: Rect, name: bytes, visible: bool, value: int, min_: int,
                max_: int, proc_id: int, ref_con: int) -> int:
    """NewControl's work: a ControlHandle, 0 if there's no room."""
    h = t.process.heap.new_handle(TITLE + 1 + len(name), clear=True)
    if not h:
        return 0
    c = Control(t.mem, h)
    c.next_control = w.control_list
    w.control_list = h
    c.owner = w.addr
    c.rect = bounds
    c.vis = 255 if visible else 0
    c.value, c.min, c.max = value, min_, max_
    cdef = t.process.resources.defproc("CDEF", (proc_id & 0xFFFF) >> 4)
    c.def_proc = ((proc_id & 0xF) << 24) | cdef
    c.ref_con = ref_con
    t.mem.w_pstr(c.addr + TITLE, name)
    message(t, c, k.initCntl)
    _draw(t, c)
    return h


@trap
def NewControl(t, window, bounds, title, visible, value, min_, max_, proc_id, ref_con):
    w = t.window(window)
    if w is None:
        return 0
    return new_control(t, w, t.rect(bounds), t.pstr(title), bool(visible), value, min_, max_,
                       proc_id, ref_con)


@trap
def GetNewControl(t, control_id, window):
    w = t.window(window)
    h = t.process.resources.GetResource("CNTL", control_id)
    if w is None or not h:
        return 0
    c = parse_cntl(t.tb.contents(h))
    return new_control(t, w, c.bounds, to_mac(c.title), c.visible, c.value, c.min, c.max,
                       c.proc_id, c.ref_con)


def _unlink(t, c: Control) -> None:
    w = t.window(c.owner)
    if w is None:
        return
    if w.control_list & ADDRESS_MASK == c.handle:
        w.control_list = c.next_control
        return
    for other in controls_of(t, w):
        if other.next_control & ADDRESS_MASK == c.handle:
            other.next_control = c.next_control
            return


def dispose(t, c: Control, erase: bool = True) -> None:
    if erase and c.visible:
        _erase(t, c)
    message(t, c, k.dispCntl)
    _unlink(t, c)
    t.system.heap_of(c.handle).dispose_handle(c.handle)


@trap
def DisposeControl(t, h):
    c = _ctl(t, h)
    if c is not None:
        dispose(t, c)


def kill_controls(t, w, erase: bool = True) -> None:
    """KillControls, and what CloseWindow does with a window's controls."""
    for c in controls_of(t, w):
        dispose(t, c, erase)


@trap
def KillControls(t, window):
    w = t.window(window)
    if w is not None:
        kill_controls(t, w)


# =========================================================================
# Showing them
# =========================================================================
@trap
def ShowControl(t, h):
    c = _ctl(t, h)
    if c is not None and not c.visible:
        c.vis = 255
        _draw(t, c)


@trap
def HideControl(t, h):
    c = _ctl(t, h)
    if c is not None and c.visible:
        _erase(t, c)
        c.vis = 0


@trap
def DrawControls(t, window):
    w = t.window(window)
    if w is not None:
        for c in controls_of(t, w):
            _draw(t, c)


@trap
def Draw1Control(t, h):
    c = _ctl(t, h)
    if c is not None:
        _draw(t, c)


@trap
def UpdateControls(t, window, rgn):
    w = t.window(window)
    if w is None:
        return
    area = read_rgn(t.mem, rgn).bbox if rgn else None
    for c in controls_of(t, w):
        r = c.rect
        if area is None or not (r.right <= area.left or area.right <= r.left or
                                r.bottom <= area.top or area.bottom <= r.top):
            _draw(t, c)


@trap
def HiliteControl(t, h, state):
    c = _ctl(t, h)
    if c is None:
        return
    state &= 0xFF
    if c.hilite != state:
        c.hilite = state
        _draw(t, c, state if state not in (k.noHilite, k.inactiveHilite) else 0)


# =========================================================================
# Where they are
# =========================================================================
def _move(t, c: Control, r: Rect) -> None:
    shown = c.visible
    if shown:
        _erase(t, c)
        c.vis = 0
    c.rect = r
    if shown:
        c.vis = 255
        _draw(t, c)


@trap
def MoveControl(t, h, left, top):
    c = _ctl(t, h)
    if c is not None:
        _move(t, c, c.rect.moved_to(left, top))


@trap
def SizeControl(t, h, width, height):
    c = _ctl(t, h)
    if c is not None:
        _move(t, c, c.rect.sized(width, height))


@trap
def FindControl(t, pt, window, which_p):
    w = t.window(window)
    if w is not None and w.visible:
        for c in controls_of(t, w):
            part = _test(t, c, pt)
            if part:
                t.w32(which_p, c.handle)
                return part
    t.w32(which_p, 0)
    return 0


@trap
def TestControl(t, h, pt):
    c = _ctl(t, h)
    return _test(t, c, pt) if c is not None else 0


# =========================================================================
# Following the mouse
# =========================================================================
def _mouse(t, w) -> Point:
    return w.global_to_local(t.tb._d.input.mac_pos)


def _ghost(t, port, rgn: Region, pat: bytes) -> None:
    """Draw (or, again, undraw) the outline a drag shows."""
    edge = rgn.diff(rgn.inset(1, 1))
    if not edge.empty:
        path = QPainterPath()
        for r in edge.rects():
            path.addRect(r.left, r.top, r.width, r.height)
        t.tb.qd.fill(port, path, pat, patXor)


def drag_outline(t, port, start: Point, outline: Region, limit: Rect, slop: Rect, axis: int,
                 action: int = 0, pat: bytes = GRAY_PAT) -> Point:
    """DragGrayRgn: follow the mouse with the region's outline, in gray
    or pat, the mouse kept in limit and on the axis, until the button
    comes up, calling action and DragHook all the while.  Returns how far
    it moved, OUTSIDE_SLOP if it ended outside slop."""
    def offset(pt: Point) -> Point:
        if not slop.contains(pt):
            return OUTSIDE_SLOP
        h = max(limit.left, min(pt.h, limit.right - 1))
        v = max(limit.top, min(pt.v, limit.bottom - 1))
        dh = 0 if axis == k.vAxisOnly else h - start.h
        dv = 0 if axis == k.hAxisOnly else v - start.v
        return Point(dv, dh)

    moved = Point(0, 0)
    _ghost(t, port, outline, pat)
    try:
        while True:
            now = offset(_mouse(t, port))
            if now != moved:
                if moved != OUTSIDE_SLOP:
                    _ghost(t, port, outline.offset(moved.h, moved.v), pat)
                if now != OUTSIDE_SLOP:
                    _ghost(t, port, outline.offset(now.h, now.v), pat)
                moved = now
            for proc in (action, t.mem.r32(lowmem.DragHook) & ADDRESS_MASK):
                if proc:
                    t.call(proc)
            if not t.tb.StillDown():
                return moved
            t.sleep(1)
    finally:
        if moved != OUTSIDE_SLOP:
            _ghost(t, port, outline.offset(moved.h, moved.v), pat)


def _drag_indicator(t, c: Control, w, part: int, start: Point, action: int) -> int:
    if message(t, c, k.dragCntl, 1):            # the CDEF drags it itself
        return part
    with scratch(t, 18) as p:                   # limitRect, slopRect, axis
        t.set_point(p, start)
        message(t, c, k.thumbCntl, p)
        limit, slop, axis = t.rect(p), t.rect(p + 8), t.mem.r16s(p + 16)
    rgn = new_rgn(t.process.heap)
    try:
        message(t, c, k.calcCRgns, rgn | 0x80000000)
        outline = read_rgn(t.mem, rgn)
    finally:
        t.process.heap.dispose_handle(rgn)
    moved = drag_outline(t, w, start, outline, limit, slop, axis, action)
    if moved == OUTSIDE_SLOP:
        return 0
    if moved != Point(0, 0):
        message(t, c, k.posCntl, _pt_param(moved))
        _draw(t, c)
    return part


@trap
def TrackControl(t, h, start, action):
    c = _ctl(t, h)
    w = t.window(c.owner) if c is not None else None
    if w is None:
        return 0
    part = _test(t, c, start)
    if not part:
        # Some programs pass the mouse-down event's point as it came, in
        # global coordinates; where the mouse is now is what they mean.
        start = _mouse(t, w)
        part = _test(t, c, start)
        if not part:
            return 0
    auto = False
    if action == ACTION_IN_RECORD:
        action = c.action
        auto = action == ACTION_IN_RECORD
        if auto:
            action = 0
    if part >= 128:
        with_action = action if not auto else 0
        return _drag_indicator(t, c, w, part, start, with_action)
    HiliteControl(t, h, part)
    inside = True
    try:
        while True:
            now = _test_ignoring_hilite(t, c, _mouse(t, w)) == part
            if now != inside:
                inside = now
                HiliteControl(t, h, part if inside else 0)
            if inside:
                if auto:
                    message(t, c, k.autoTrack, part)
                elif callable(action):              # the List Manager's own
                    action(h, part)
                elif action:
                    t.call(action, (("p", h), ("h", part)))
            if not t.tb.StillDown():
                break
            t.sleep(1)
    finally:
        if _ctl(t, h) is not None:              # the action may have disposed of it
            HiliteControl(t, h, 0)
    return part if inside else 0


def _test_ignoring_hilite(t, c: Control, pt: Point) -> int:
    """testCntl while the control is highlighted (it's still active)."""
    if not c.visible:
        return 0
    return message(t, c, k.testCntl, _pt_param(pt)) & 0xFFFF


@trap
def DragControl(t, h, start, limit, slop, axis):
    c = _ctl(t, h)
    w = t.window(c.owner) if c is not None else None
    if w is None:
        return
    if message(t, c, k.dragCntl, 0):
        return
    moved = drag_outline(t, w, start, Region.rect(c.rect), t.rect(limit), t.rect(slop), axis)
    if moved not in (OUTSIDE_SLOP, Point(0, 0)):
        r = c.rect
        _move(t, c, r.moved_to(r.left + moved.h, r.top + moved.v))


# =========================================================================
# Settings
# =========================================================================
def _set(t, h, field: str, value: int) -> None:
    c = _ctl(t, h)
    if c is None:
        return
    setattr(c, field, value)
    c.value = c.pin(c.value)
    _draw(t, c, k.inThumb if _proc_id(t, c) >> 4 == 1 else 0)


@trap
def SetControlValue(t, h, value):
    _set(t, h, "value", value)


@trap
def SetControlMinimum(t, h, value):
    _set(t, h, "min", value)


@trap
def SetControlMaximum(t, h, value):
    _set(t, h, "max", value)


@trap
def GetControlValue(t, h):
    c = _ctl(t, h)
    return c.value if c is not None else 0


@trap
def GetControlMinimum(t, h):
    c = _ctl(t, h)
    return c.min if c is not None else 0


@trap
def GetControlMaximum(t, h):
    c = _ctl(t, h)
    return c.max if c is not None else 0


@trap
def SetControlTitle(t, h, title):
    c = _ctl(t, h)
    if c is None:
        return
    name = t.pstr(title)
    if not t.system.heap_of(c.handle).set_handle_size(c.handle, TITLE + 1 + len(name)):
        return
    t.mem.w_pstr(c.addr + TITLE, name)
    w = t.window(c.owner)
    if c.visible and w is not None:
        t.tb.qd.fill(w, rect_path(c.rect), w.bk_pat, k.patCopy)
        _draw(t, c)


@trap
def GetControlTitle(t, h, title_p):
    c = _ctl(t, h)
    t.set_str255(title_p, t.mem.r_pstr(c.addr + TITLE) if c is not None else b"")


@trap
def SetControlAction(t, h, action):
    c = _ctl(t, h)
    if c is not None:
        c.action = action


@trap
def GetControlAction(t, h):
    c = _ctl(t, h)
    return c.action if c is not None else 0


@trap
def SetControlReference(t, h, ref_con):
    c = _ctl(t, h)
    if c is not None:
        c.ref_con = ref_con


@trap
def GetControlReference(t, h):
    c = _ctl(t, h)
    return c.ref_con if c is not None else 0


@trap
def GetControlVariant(t, h):
    c = _ctl(t, h)
    return c.variation if c is not None else 0
