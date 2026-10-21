"""
Resource templates: the resources the Toolbox builds things from.

    WIND  a window          GetNewWindow
    DLOG  a dialog          GetNewDialog
    ALRT  an alert          Alert, StopAlert, NoteAlert, CautionAlert
    DITL  a dialog's items
    CNTL  a control         GetNewControl
    MBAR  a menu bar        GetNewMBar
    STR#  a string list     GetIndString

MENU isn't here: a MENU resource is a MenuInfo record already, and
GetMenu uses the resource itself as the menu.

WIND, DLOG and ALRT may end with a System 7 positioning word, which
place() applies.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass, field

from ..memory import from_mac
from .types import Rect

# Positioning words (Macintosh Toolbox Essentials, Window Manager): where
# in the top two bits, how in the next three, $0A in the low byte.
noAutoCenter = 0x0000
centerMainScreen = 0x280A
alertPositionMainScreen = 0x300A
staggerMainScreen = 0x380A
centerParentWindow = 0xA80A
alertPositionParentWindow = 0xB00A
staggerParentWindow = 0xB80A
centerParentWindowScreen = 0x680A
alertPositionParentWindowScreen = 0x700A
staggerParentWindowScreen = 0x780A

_HOW = 0x3800
_CENTER, _ALERT, _STAGGER = 0x2800, 0x3000, 0x3800
_ON_PARENT = 0x8000

# DITL item types
userItem = 0
helpItem = 1
ctrlItem = 4
btnCtrl, chkCtrl, radCtrl, resCtrl = 0, 1, 2, 3
statText = 8
editText = 16
iconItem = 32
picItem = 64
itemDisable = 128


def _rect(data: bytes, at: int) -> Rect:
    return Rect(*struct.unpack_from(">4h", data, at))


def _pstr(data: bytes, at: int) -> tuple[str, int]:
    """The string and the offset just past it."""
    n = data[at] if at < len(data) else 0
    return from_mac(data[at + 1:at + 1 + n]), at + 1 + n


def _position(data: bytes, at: int) -> int:
    """The positioning word, if the resource has one after word alignment."""
    at += at & 1
    return struct.unpack_from(">H", data, at)[0] if at + 2 <= len(data) else noAutoCenter


@dataclass
class WindowTemplate:
    bounds: Rect
    proc_id: int
    visible: bool
    go_away: bool
    ref_con: int
    title: str
    position: int = noAutoCenter


def parse_wind(data: bytes) -> WindowTemplate:
    bounds = _rect(data, 0)
    proc_id, = struct.unpack_from(">h", data, 8)
    ref_con, = struct.unpack_from(">I", data, 14)
    title, end = _pstr(data, 18)
    return WindowTemplate(bounds, proc_id, bool(data[10]), bool(data[12]), ref_con, title,
                          _position(data, end))


@dataclass
class DialogTemplate:
    bounds: Rect
    proc_id: int
    visible: bool
    go_away: bool
    ref_con: int
    items_id: int
    title: str
    position: int = noAutoCenter


def parse_dlog(data: bytes) -> DialogTemplate:
    bounds = _rect(data, 0)
    proc_id, = struct.unpack_from(">h", data, 8)
    ref_con, items_id = struct.unpack_from(">Ih", data, 14)
    title, end = _pstr(data, 20)
    return DialogTemplate(bounds, proc_id, bool(data[10]), bool(data[12]), ref_con, items_id,
                          title, _position(data, end))


@dataclass
class AlertStage:
    bold_item: int          # the default button: item 1 or 2
    drawn: bool             # whether the alert is shown at this stage
    sounds: int             # how many beeps, 0-3


@dataclass
class AlertTemplate:
    bounds: Rect
    items_id: int
    stages: list[AlertStage]    # stage 1 first
    position: int = noAutoCenter


def parse_alrt(data: bytes) -> AlertTemplate:
    bounds = _rect(data, 0)
    items_id, word = struct.unpack_from(">hH", data, 8)
    stages = []
    for i in range(4):                      # stage 1 is the low nibble
        nib = word >> (4 * i) & 0xF
        stages.append(AlertStage(1 + (nib >> 3 & 1), bool(nib & 4), nib & 3))
    return AlertTemplate(bounds, items_id, stages, _position(data, 12))


@dataclass
class DialogItem:
    type: int               # without itemDisable
    rect: Rect
    enabled: bool
    text: str = ""          # buttons, check boxes, radio buttons, static and edit text
    res_id: int = 0         # resCtrl, iconItem, picItem, helpItem
    data: bytes = field(default=b"", repr=False)

    @property
    def is_control(self) -> bool:
        return self.type & ~3 == ctrlItem


def parse_ditl(data: bytes) -> list[DialogItem]:
    n = (struct.unpack_from(">h", data, 0)[0] + 1) & 0xFFFF
    items, at = [], 2
    for _ in range(n):
        rect = _rect(data, at + 4)
        kind = data[at + 12]
        length = data[at + 13]
        body = data[at + 14:at + 14 + length]
        at += 14 + length + (length & 1)
        item = DialogItem(kind & ~itemDisable, rect, not kind & itemDisable, data=body)
        base = item.type
        if base in (ctrlItem + btnCtrl, ctrlItem + chkCtrl, ctrlItem + radCtrl,
                    statText, editText):
            item.text = from_mac(body)
        elif base in (ctrlItem + resCtrl, iconItem, picItem) and length >= 2:
            item.res_id, = struct.unpack_from(">h", body, 0)
        elif base == helpItem and length >= 4:
            item.res_id, = struct.unpack_from(">h", body, 2)
        items.append(item)
    return items


@dataclass
class ControlTemplate:
    bounds: Rect
    value: int
    visible: bool
    max: int
    min: int
    proc_id: int
    ref_con: int
    title: str


def parse_cntl(data: bytes) -> ControlTemplate:
    bounds = _rect(data, 0)
    value, = struct.unpack_from(">h", data, 8)
    max_, min_, proc_id, ref_con = struct.unpack_from(">hhhI", data, 12)
    title, _ = _pstr(data, 22)
    return ControlTemplate(bounds, value, bool(data[10]), max_, min_, proc_id, ref_con, title)


def parse_mbar(data: bytes) -> list[int]:
    n, = struct.unpack_from(">h", data, 0)
    return list(struct.unpack_from(f">{n}h", data, 2))


def parse_str_list(data: bytes) -> list[str]:
    n, = struct.unpack_from(">h", data, 0)
    out, at = [], 2
    for _ in range(n):
        s, at = _pstr(data, at)
        out.append(s)
    return out


def parse_str(data: bytes) -> str:
    return _pstr(data, 0)[0]


def place(rect: Rect, position: int, desk: Rect, parent: Rect | None = None,
          title_bar: int = 0) -> Rect:
    """Apply a positioning word to a content rectangle.  desk is the main
    screen below the menu bar; title_bar is the height of the frame above
    the content, so the whole window is what's centered.  Staggering is
    left alone for now."""
    how = position & _HOW
    if position == noAutoCenter or how not in (_CENTER, _ALERT):
        return rect
    area = parent if position & _ON_PARENT and parent is not None else desk
    width, height = rect.width, rect.height + title_bar
    left = area.left + (area.width - width) // 2
    if how == _CENTER:
        top = area.top + (area.height - height) // 2
    else:                                   # a third of the space above, two below
        top = area.top + max(area.height - height, 0) // 3
    return Rect(top + title_bar, left, top + title_bar + rect.height, left + width)
