"""
Colour calls on a black-and-white Mac.  Programs that run on both kinds
call these whether or not there's Color QuickDraw; here an RGB colour
is the classic colour (black or white) it would draw as on a 1-bit
screen, and the device is 1-bit: index 0 white, 1 black.  Ports stay
plain GrafPorts, so OpColor and InvertColor have nowhere to go.
"""

from __future__ import annotations

from ..memory import ADDRESS_MASK
from ..toolbox.color import color_rgb, is_light, rgb_color, standard_table
from ..toolbox.constants import blackColor, whiteColor
from .dispatch import trap
from .glue_toolbox import _port

HiliteRGB = 0xDA0           # the highlight colour, an RGBColor


def _rgb(t, p: int) -> tuple[int, int, int]:
    return t.mem.r16(p), t.mem.r16(p + 2), t.mem.r16(p + 4)


def _set_rgb(t, p: int, rgb: tuple[int, int, int]) -> None:
    for i, v in enumerate(rgb):
        t.mem.w16(p + 2 * i, v)


@trap
def RGBForeColor(t, p):
    port = _port(t)
    if port is not None:
        port.fg_color = rgb_color(*_rgb(t, p))


@trap
def RGBBackColor(t, p):
    port = _port(t)
    if port is not None:
        port.bk_color = rgb_color(*_rgb(t, p))


@trap
def GetForeColor(t, p):
    port = _port(t)
    _set_rgb(t, p, color_rgb(port.fg_color if port is not None else blackColor))


@trap
def GetBackColor(t, p):
    port = _port(t)
    _set_rgb(t, p, color_rgb(port.bk_color if port is not None else whiteColor))


@trap
def HiliteColor(t, p):
    """Kept in HiliteRGB, where the highlighting colour is looked for;
    highlighting is inverting here anyway."""
    _set_rgb(t, HiliteRGB, _rgb(t, p))


@trap
def OpColor(t, p):
    pass


@trap
def InvertColor(t, p):
    """Leaves the colour: it's what inverts it that the device decides,
    and inverting black and white gives white and black."""


@trap
def Color2Index(t, p):
    return 0 if is_light(*_rgb(t, p)) else 1


@trap
def Index2Color(t, index, p):
    _set_rgb(t, p, color_rgb(whiteColor if index == 0 else blackColor))


@trap
def GetCTable(t, ct_id):
    """A ColorTable handle: one of the standard ones, else a copy of the
    clut resource of that ID."""
    data = standard_table(ct_id)
    if data is None:
        res = t.process.resources.GetResource("clut", ct_id)
        if not res or not t.mem.r32(res) & ADDRESS_MASK:
            return 0
        data = t.tb.contents(res)
    heap = t.process.heap
    h = heap.new_handle(len(data))
    if h:
        t.mem.write(heap.deref(h), data)
    return h


@trap
def DisposeCTable(t, h):
    if h:
        t.system.heap_of(h).dispose_handle(h)


@trap
def GetGray(t, device, back, fore):
    """No grey between two colours on a 1-bit device: FALSE, fore unchanged."""
    return False
