"""
The Script Manager (Inside Macintosh V-293), through ScriptUtil ($A8B5),
on a Mac with only the Roman script.  macrt.toolbox.script has the
character tables, word and line breaks and the measuring; widths are the
current port's, as TextWidth gives them.
"""

from __future__ import annotations

import struct

from ..memory import ADDRESS_MASK, from_mac
from ..toolbox import lowmem
from ..toolbox import script as sm
from ..toolbox.memmgr import noErr
from .dispatch import trap
from .glue_toolbox import _port

memFullErr = -108


def _text(t, p: int, n: int) -> bytes:
    return t.mem.read(p, n) if n > 0 else b""


def _width(t, numer=None, denom=None):
    """TextWidth in the current port, scaled by numer/denom (Points)."""
    scale = numer.h / denom.h if numer is not None and denom is not None and denom.h else 1

    def width(text: bytes) -> int:
        return round(t.tb.StringWidth(from_mac(text)) * scale) if text else 0
    return width


def _fixed(v: float) -> int:
    return int(round(v * 65536)) & 0xFFFFFFFF


# =========================================================================
# Scripts and their variables
# =========================================================================
@trap
def FontScript(t):
    return sm.smRoman


@trap
def IntlScript(t):
    return sm.smRoman


@trap
def FontToScript(t, family):
    return sm.smRoman                   # the script of any font not installed


@trap
def KeyScript(t, code):
    pass


@trap
def GetScriptManagerVariable(t, verb):
    if verb == sm.smSysRef:
        return t.mem.r16s(lowmem.BootDrive)
    value = sm.environs(verb, t.system.script_settings)
    return 0 if value is None else value


@trap
def SetScriptManagerVariable(t, verb, value):
    if verb not in sm.SETTABLE:
        return sm.smVerbNotFound
    t.system.script_settings[verb] = value
    return noErr


@trap
def GetScriptVariable(t, script, verb):
    if script not in (sm.smRoman, sm.smSystemScript):
        return 0
    if verb == sm.smScriptAppFond:
        return t.mem.r16(lowmem.ApFontID)
    value = sm.script_variable(verb)
    return 0 if value is None else value


@trap
def SetScriptVariable(t, script, verb, value):
    return sm.smVerbNotFound


# =========================================================================
# Characters
# =========================================================================
@trap
def CharByte(t, p, offset):
    return 0                            # smSingleByte: no two-byte characters


@trap
def CharacterByteType(t, p, offset, script):
    return 0


@trap
def CharType(t, p, offset):
    return sm.char_type(t.mem.r8(p + offset))


@trap
def CharacterType(t, p, offset, script):
    return sm.char_type(t.mem.r8(p + offset))


@trap
def ParseTable(t, table):
    t.mem.write(table, bytes(256))      # every byte a character by itself
    return True


@trap
def FillParseTable(t, table, script):
    return ParseTable(t, table)


@trap
def FindScriptRun(t, p, length, used_p):
    t.w32(used_p, length)               # it's all Roman
    return sm.smRoman


def _handle_text(t, h: int) -> bytes:
    heap = t.system.heap_of(h)
    return t.mem.read(heap.deref(h), heap.handle_size(h))


@trap
def Transliterate(t, src, dst, target, mask):
    out = sm.transliterate(_handle_text(t, src), target, mask)
    if out is None:
        return -1
    heap = t.system.heap_of(dst)
    if not heap.set_handle_size(dst, len(out)):
        return memFullErr
    t.mem.write(heap.deref(dst), out)
    return noErr


@trap
def TransliterateText(t, src, dst, target, mask, script):
    return Transliterate(t, src, dst, target, mask)


@trap
def TextUtilFunctions(t, p, length, script, routine):
    """LowercaseText ($0000), StripDiacritics ($0200), UppercaseText
    ($0400) and UppercaseStripDiacritics ($0600), in place."""
    text = _text(t, p, length)
    if routine & 0x200:
        text = text.translate(sm.STRIP)
    if routine & 0x400:
        text = text.translate(sm.UPPER)
    elif not routine & 0x200:
        text = text.translate(sm.LOWER)
    t.mem.write(p, text)


# =========================================================================
# Words and lines
# =========================================================================
def _offsets(t, p: int, first: int, second: int) -> None:
    """An OffsetTable: three pairs, only the first used in Roman text."""
    t.mem.write(p, struct.pack(">6h", first, second, 0, 0, 0, 0))


@trap
def FindWord(t, p, length, offset, leading_edge, breaks, offsets):
    _offsets(t, offsets, *sm.find_word(_text(t, p, length), offset, bool(leading_edge)))


@trap
def HiliteText(t, p, length, first, second, offsets):
    _offsets(t, offsets, min(first, second), max(first, second))


@trap
def VisibleLength(t, p, length):
    return sm.visible_length(_text(t, p, length))


@trap
def StyledLineBreak(t, p, length, start, end, flags, width_p, offset_p):
    avail = t.mem.r32s(width_p) / 65536
    first = t.mem.r32(offset_p) != 0
    code, offset, left = sm.line_break(_text(t, p, length), start, end, avail, first,
                                       _width(t))
    t.w32(width_p, _fixed(left))
    t.w32(offset_p, offset)
    return code


@trap
def TruncString(t, avail, p, where):
    out = sm.truncate(t.pstr(p), avail, where, _width(t))
    if out is None:
        return sm.NotTruncated
    t.set_str255(p, out)
    return sm.Truncated


# =========================================================================
# Measuring and drawing justified text: the slop shared among the spaces
# =========================================================================
@trap
def Char2Pixel(t, p, length, slop, offset, direction):
    return round(sm.char_locations(_text(t, p, length), slop, _width(t))[
        max(0, min(offset, length))])


@trap
def CharToPixel(t, p, length, slop, offset, direction, run, numer, denom):
    return round(sm.char_locations(_text(t, p, length), slop / 65536, _width(t, numer, denom))[
        max(0, min(offset, length))])


@trap
def Pixel2Char(t, p, length, slop, x, leading_p):
    offset, leading, _ = sm.pixel_to_char(_text(t, p, length), slop, x, _width(t))
    t.mem.w8(leading_p, 1 if leading else 0)
    return offset


@trap
def PixelToChar(t, p, length, slop, x, leading_p, remaining_p, run, numer, denom):
    offset, leading, remaining = sm.pixel_to_char(_text(t, p, length), slop / 65536,
                                                  x / 65536, _width(t, numer, denom))
    t.mem.w8(leading_p, 1 if leading else 0)
    if remaining_p & ADDRESS_MASK:
        t.w32(remaining_p, _fixed(remaining))
    return offset


@trap
def MeasureJust(t, p, length, slop, locs):
    for i, x in enumerate(sm.char_locations(_text(t, p, length), slop, _width(t))):
        t.w16(locs + 2 * i, round(x))


@trap
def MeasureJustified(t, p, length, slop, locs, run, numer, denom):
    for i, x in enumerate(sm.char_locations(_text(t, p, length), slop / 65536,
                                            _width(t, numer, denom))):
        t.w16(locs + 2 * i, round(x))


def _draw_just(t, text: bytes, slop: float) -> None:
    port = _port(t)
    spaces = text.count(b" ")
    if port is None or not text:
        return
    extra = port.sp_extra
    if spaces:
        port.sp_extra = extra + int(slop * 65536 / spaces)
    try:
        t.tb.DrawString(from_mac(text))
    finally:
        port.sp_extra = extra


@trap
def DrawJust(t, p, length, slop):
    _draw_just(t, _text(t, p, length), slop)


@trap
def DrawJustified(t, p, length, slop, run, numer, denom):
    _draw_just(t, _text(t, p, length), slop / 65536)


@trap
def PortionLine(t, p, length, run, numer, denom):
    """How much of a line's slop the run takes: by its width."""
    return _fixed(_width(t, numer, denom)(_text(t, p, length)))
