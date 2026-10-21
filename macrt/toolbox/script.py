"""
The Script Manager's view of text (Inside Macintosh V-293, and Text,
chapter 6) for the one script macrt has, Roman: what kind of character
each byte is, its case and its letter without accents, where words and
lines break, and the values GetEnvirons and GetScript report.
"""

from __future__ import annotations

import unicodedata
from typing import Callable

VERSION = 0x0700                # Script Manager 7.0, as System 7.0.1 has

smRoman = 0
smSystemScript = -1
smVerbNotFound = -1

# CharType: the type in bits 0-3, a class in bits 8-11 (for punctuation),
# then direction, case and size.
smCharPunct, smCharAscii, smCharEuro = 0, 1, 7
smPunctNormal, smPunctNumber, smPunctSymbol, smPunctBlank = 0x000, 0x100, 0x200, 0x300
smCharUpper = 0x4000

# Transliterate
smTransAscii, smTransNative = 0, 1
smTransLower, smTransUpper = 0x4000, 0x8000
smMaskAscii, smMaskNative = 1, 2

# StyledLineBreak, and TruncString's where and what
smBreakWord, smBreakChar, smBreakOverflow = 0, 1, 2
smTruncEnd, smTruncMiddle = 0, 0x4000
NotTruncated, Truncated = 0, 1
ELLIPSIS = b"\xc9"

# GetEnvirons (GetScriptManagerVariable) verbs
smVersion, smMunged, smEnabled, smBiDirect = 0, 2, 4, 6
smFontForce, smIntlForce, smForced, smDefault = 8, 10, 12, 14
smPrint, smSysScript, smLastScript, smKeyScript = 16, 18, 20, 22
smSysRef, smKeyCache, smKeySwap, smGenFlags = 24, 26, 28, 30
smOverride, smCharPortion, smDoubleByte, smKCHRCache = 32, 34, 36, 38
smRegionCode, smKeyDisableState = 40, 42

# GetScript (GetScriptVariable) verbs
smScriptVersion, smScriptMunged, smScriptEnabled, smScriptRight = 0, 2, 4, 6
smScriptJust, smScriptRedraw, smScriptSysFond, smScriptAppFond = 8, 10, 12, 14
smScriptNumber, smScriptDate, smScriptSort, smScriptFlags = 16, 18, 20, 22
smScriptToken, smScriptEncoding, smScriptLang, smScriptNumDate = 24, 26, 28, 30
smScriptKeys, smScriptIcon, smScriptPrint, smScriptTrap = 32, 34, 36, 38
smScriptCreator, smScriptFile, smScriptName = 40, 42, 44
smScriptMonoFondSize, smScriptPrefFondSize, smScriptSmallFondSize = 78, 80, 82
smScriptSysFondSize, smScriptAppFondSize, smScriptHelpFondSize = 84, 86, 88
smScriptValidStyles, smScriptAliasStyle = 90, 92

# Families as macrt.toolbox.sysres installs them
CHICAGO, GENEVA, MONACO = 0, 3, 4


def environs(verb: int, settings: dict[int, int]) -> int | None:
    """GetEnvirons: the Script Manager's own variables, None for a verb
    there isn't.  settings has what SetEnvirons changed."""
    if verb in settings:
        return settings[verb]
    return {smVersion: VERSION, smEnabled: 1, smSysRef: 0}.get(
        verb, 0 if 0 <= verb <= smKeyDisableState and verb % 2 == 0 else None)


SETTABLE = {smFontForce, smIntlForce, smKeyScript, smGenFlags, smOverride, smCharPortion,
            smRegionCode, smKeyDisableState}


def script_variable(verb: int) -> int | None:
    """GetScript for Roman: None for a verb there isn't."""
    fond_size = {smScriptMonoFondSize: (MONACO, 9), smScriptPrefFondSize: (GENEVA, 12),
                 smScriptSmallFondSize: (GENEVA, 9), smScriptSysFondSize: (CHICAGO, 12),
                 smScriptAppFondSize: (GENEVA, 12), smScriptHelpFondSize: (GENEVA, 9)}
    if verb in fond_size:
        family, size = fond_size[verb]
        return family << 16 | size
    values = {smScriptVersion: VERSION, smScriptEnabled: 1, smScriptSysFond: CHICAGO,
              smScriptAppFond: GENEVA, smScriptCreator: int.from_bytes(b"ZSYS", "big"),
              smScriptValidStyles: 0x7F}
    if verb in values:
        return values[verb]
    return 0 if 0 <= verb <= smScriptName and verb % 2 == 0 or verb == smScriptAliasStyle else None


# =========================================================================
# Characters
# =========================================================================
def _char(code: int) -> str:
    return bytes([code]).decode("mac_roman")


def _byte(c: str, code: int) -> int:
    """c as a Mac Roman byte; code if it isn't one."""
    try:
        b = c.encode("mac_roman")
    except UnicodeEncodeError:
        return code
    return b[0] if len(b) == 1 else code


def _is_letter(c: str) -> bool:
    """Roman letters: the Latin ones, accented or not, and ligatures."""
    return c.isalpha() and unicodedata.name(c, "").startswith("LATIN")


def _type(code: int) -> int:
    c = _char(code)
    if _is_letter(c):
        return (smCharAscii if code < 0x80 else smCharEuro) | (smCharUpper if c.isupper() else 0)
    if "0" <= c <= "9":
        return smCharPunct | smPunctNumber
    if c in " \t\r\n\xa0":
        return smCharPunct | smPunctBlank
    if c.isalpha() or unicodedata.category(c).startswith("S"):     # µ, π, Ω as well as $
        return smCharPunct | smPunctSymbol
    return smCharPunct | smPunctNormal


def _mapped(fn: Callable[[str], str]) -> bytes:
    """A 256-byte table: fn of each letter where that's one Mac Roman
    character, everything else as it is."""
    return bytes(_byte(fn(_char(i)), i) if _is_letter(_char(i)) else i for i in range(256))


def _strip(c: str) -> str:
    base = unicodedata.normalize("NFD", c)
    return base[0] if len(base) > 1 and all(unicodedata.combining(m) for m in base[1:]) else c


CHAR_TYPES = tuple(_type(i) for i in range(256))
UPPER = _mapped(str.upper)
LOWER = _mapped(str.lower)
STRIP = _mapped(_strip)


def char_type(code: int) -> int:
    return CHAR_TYPES[code & 0xFF]


def is_blank(code: int) -> bool:
    return CHAR_TYPES[code] == smCharPunct | smPunctBlank


def _word_class(code: int) -> int:
    """Words are runs of letters and digits, and runs of blanks; any other
    character is a word by itself."""
    t = CHAR_TYPES[code]
    if t & 0xF or t == smPunctNumber or code == ord("'"):
        return 1
    return 2 if is_blank(code) else 0


def find_word(text: bytes, offset: int, leading_edge: bool) -> tuple[int, int]:
    """FindWord: the start and end of the word at the character offset,
    or before it if not leading_edge."""
    i = offset if leading_edge else offset - 1
    if not 0 <= i < len(text):
        offset = max(0, min(offset, len(text)))
        return offset, offset
    kind = _word_class(text[i])
    if kind == 0:
        return i, i + 1
    start, end = i, i + 1
    while start > 0 and _word_class(text[start - 1]) == kind:
        start -= 1
    while end < len(text) and _word_class(text[end]) == kind:
        end += 1
    return start, end


def transliterate(text: bytes, target: int, mask: int) -> bytes | None:
    """Roman text only changes case: to lower or upper, the characters in
    the mask (ASCII and the rest).  None if target is both."""
    if target & smTransLower and target & smTransUpper:
        return None
    table = LOWER if target & smTransLower else UPPER if target & smTransUpper else None
    if table is None:
        return text
    return bytes(table[c] if mask & (smMaskAscii if c < 0x80 else smMaskNative) else c
                 for c in text)


def visible_length(text: bytes) -> int:
    """VisibleLength: the text without the blanks at its end."""
    n = len(text)
    while n and is_blank(text[n - 1]):
        n -= 1
    return n


# =========================================================================
# Measuring.  width(text) is the port's TextWidth.  Slop, the width a
# justified line has to spare, is shared among its spaces.
# =========================================================================
def slop_before(text: bytes, offset: int, slop: float) -> float:
    spaces = text.count(b" ")
    return slop * text[:offset].count(b" ") / spaces if spaces else 0


def char_locations(text: bytes, slop: float, width: Callable[[bytes], int]) -> list[float]:
    """MeasureJust: where each character starts, and where the text ends."""
    return [width(text[:i]) + slop_before(text, i, slop) for i in range(len(text) + 1)]


def pixel_to_char(text: bytes, slop: float, x: float,
                  width: Callable[[bytes], int]) -> tuple[int, bool, float]:
    """Pixel2Char: the character boundary nearest x, whether x is on the
    leading edge of the character after it (else the trailing edge of the
    one before), and how far past the end of the text x is."""
    locs = char_locations(text, slop, width)
    if x >= locs[-1]:
        return len(text), False, x - locs[-1]
    for i in range(len(text)):
        if x < locs[i + 1]:
            if x < (locs[i] + locs[i + 1]) / 2:
                return i, True, -1
            return i + 1, False, -1
    return len(text), False, -1


def line_break(text: bytes, start: int, end: int, avail: float, first: bool,
               width: Callable[[bytes], int]) -> tuple[int, int, float]:
    """StyledLineBreak for the style run text[start:end], with avail pixels
    left on the line: (code, offset of the break, width left)."""
    run = width(text[start:end])
    if run <= avail:
        return smBreakOverflow, end, avail - run
    # After a run of blanks, which can hang past the edge
    for p in range(end, start, -1):
        if is_blank(text[p - 1]) and (p == len(text) or not is_blank(text[p])):
            q = p
            while q > start and is_blank(text[q - 1]):
                q -= 1
            if width(text[start:q]) <= avail:
                return smBreakWord, p, avail
    if not first:
        return smBreakWord, start, avail
    fit = start + 1
    while fit < end and width(text[start:fit + 1]) <= avail:
        fit += 1
    return smBreakChar, fit, avail


def truncate(text: bytes, avail: int, where: int, width: Callable[[bytes], int]) -> bytes | None:
    """TruncString: the text shortened to fit avail pixels with an ellipsis
    at its end or in its middle; None if it fits already."""
    if width(text) <= avail:
        return None
    for keep in range(len(text) - 1, -1, -1):
        if where == smTruncMiddle:
            out = text[:(keep + 1) // 2] + ELLIPSIS + text[len(text) - keep // 2:]
        else:
            out = text[:keep] + ELLIPSIS
        if width(out) <= avail:
            return out
    return b""
