"""
Dialog Manager data: the rest of a DialogRecord, the item list, and
ParamText's substitutions.

A dialog is a window whose record goes on past the WindowRecord's 156
bytes (Inside Macintosh I-408):

    items      156  the item list, a handle
    textH      160  TextEdit's record for the editText item being edited
    editField  164  that item's number less one; -1 when there's none
    editOpen   166  (the Dialog Manager's own)
    aDefItem   168  the default button's item number; 1 to begin with

The item list starts as a copy of a DITL resource: a count less one, then
for each item a placeholder, its display rectangle, its type and its
data.  Making the dialog fills the placeholders in: a ControlHandle for a
control, a handle to the text for static and edit text, the ICON or PICT
for an icon or picture, and (left to SetDialogItem) a procedure for a user
item.  Applications read and change it through GetDialogItem and
SetDialogItem, so it stays laid out as on a Mac.
"""

from __future__ import annotations

from dataclasses import dataclass

from ..memory import ADDRESS_MASK, BYTE, INTEGER, LONG, RAM, RECT, Field, Record
from .templates import ctrlItem, editText, itemDisable, statText
from .types import Rect

SIZE = 170                  # a DialogRecord
HIDDEN = 16384              # HideDialogItem moves an item this far right
ALERT_ICON = Rect(10, 20, 42, 52)   # where StopAlert, NoteAlert and CautionAlert put theirs
stopIcon, noteIcon, cautionIcon = 0, 1, 2


class Dialog(Record):
    """The fields after the window's, at the same address."""
    SIZE = SIZE

    items = Field(156, LONG)
    text_h = Field(160, LONG)
    edit_field = Field(164, INTEGER)
    edit_open = Field(166, INTEGER)
    a_def_item = Field(168, INTEGER)


@dataclass
class DialogState:
    """What the Dialog Manager keeps about a dialog outside its record."""
    cancel_item: int = 0        # SetDialogCancelItem's; Escape and Command-period press it
    outline: bool = False       # draw the bold outline around the default button
    icon: int = -1              # an alert's icon, ICON 0, 1 or 2
    tracks_cursor: bool = False


class _Field:
    """An item's field, read through the item list's handle."""

    def __init__(self, offset: int, codec):
        self.offset = offset
        self.codec = codec

    def __get__(self, item, owner=None):
        if item is None:
            return self
        return self.codec.read(item.mem, item.addr + self.offset)

    def __set__(self, item, value) -> None:
        self.codec.write(item.mem, item.addr + self.offset, value)


class Item:
    """One entry in an item list.  Its place in the list's block is kept,
    not its address, since the block can move."""

    handle = _Field(0, LONG)            # or a user item's procedure
    rect = _Field(4, RECT)
    kind = _Field(12, BYTE)             # the type, with itemDisable
    length = _Field(13, BYTE)

    def __init__(self, mem: RAM, list_handle: int, offset: int, number: int):
        self.mem = mem
        self.list_handle = list_handle
        self.offset = offset
        self.number = number            # from 1

    @property
    def addr(self) -> int:
        return (self.mem.r32(self.list_handle) & ADDRESS_MASK) + self.offset

    @property
    def type(self) -> int:
        return self.kind & ~itemDisable

    @property
    def enabled(self) -> bool:
        return not self.kind & itemDisable

    @property
    def is_control(self) -> bool:
        return self.type & ~3 == ctrlItem

    @property
    def is_text(self) -> bool:
        return self.type in (statText, editText)

    @property
    def hidden(self) -> bool:
        return self.rect.left >= HIDDEN

    @property
    def data(self) -> bytes:
        return self.mem.read(self.addr + 14, self.length)

    def __repr__(self) -> str:
        return f"<Item {self.number} type {self.type} {self.rect}>"


def items(mem: RAM, list_handle: int) -> list[Item]:
    """The items in an item list, item 1 first."""
    h = list_handle & ADDRESS_MASK
    p = mem.r32(h) & ADDRESS_MASK if h else 0
    if not p:
        return []
    n = (mem.r16(p) + 1) & 0xFFFF
    out, at = [], 2
    for number in range(1, n + 1):
        out.append(Item(mem, h, at, number))
        length = mem.r8(p + at + 13)
        at += 14 + length + (length & 1)
    return out


def substitute(text: bytes, params: list[bytes]) -> bytes:
    """Static text with ParamText's strings put in for ^0 to ^3."""
    out, i = bytearray(), 0
    while i < len(text):
        c = text[i]
        if c == ord("^") and i + 1 < len(text) and text[i + 1] in b"0123":
            out += params[text[i + 1] - ord("0")]
            i += 2
        else:
            out.append(c)
            i += 1
    return bytes(out)
