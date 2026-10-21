"""The Script Manager for the Roman script: character types and case,
words and line breaks, measuring with slop, the variables, and the
ScriptUtil dispatcher."""

import struct

from macrt.m68k import dispatch
from macrt.toolbox import lowmem
from macrt.toolbox import script as sm

from guest import Guest, Processor

E_ACUTE, CAP_E_ACUTE, A_RING, CAP_A_RING, SHARP_S, NBSP = 0x8E, 0x83, 0x8C, 0x81, 0xA7, 0xCA


def mono(text: bytes) -> int:
    """A font six pixels wide."""
    return 6 * len(text)


# =========================================================================
# The tables
# =========================================================================
def test_character_types():
    assert sm.char_type(ord("a")) == sm.smCharAscii
    assert sm.char_type(ord("A")) == sm.smCharAscii | sm.smCharUpper
    assert sm.char_type(E_ACUTE) == sm.smCharEuro
    assert sm.char_type(CAP_E_ACUTE) == sm.smCharEuro | sm.smCharUpper
    assert sm.char_type(SHARP_S) == sm.smCharEuro
    assert sm.char_type(ord("7")) == sm.smPunctNumber
    assert sm.char_type(ord(" ")) == sm.char_type(NBSP) == sm.smPunctBlank
    assert sm.char_type(ord(".")) == sm.char_type(ord("(")) == sm.smPunctNormal
    assert sm.char_type(ord("$")) == sm.char_type(ord("+")) == sm.smPunctSymbol
    assert sm.char_type(0xB5) == sm.smPunctSymbol                      # µ


def test_case_and_accents_stay_one_byte_each():
    text = bytes([ord("r"), E_ACUTE, ord("s"), A_RING, SHARP_S, ord("1")])
    assert text.translate(sm.UPPER) == bytes([ord("R"), CAP_E_ACUTE, ord("S"), CAP_A_RING,
                                              SHARP_S, ord("1")])
    assert text.translate(sm.UPPER).translate(sm.LOWER) == text
    assert text.translate(sm.STRIP) == b"resa" + bytes([SHARP_S]) + b"1"
    assert bytes([0xAF]).translate(sm.STRIP) == bytes([0xAF])           # Ø is a letter


def test_words_are_letters_and_digits_or_blanks():
    text = b"Don't stop,  now 42!"
    assert sm.find_word(text, 0, True) == (0, 5)
    assert sm.find_word(text, 5, False) == (0, 5)
    assert sm.find_word(text, 5, True) == (5, 6)
    assert sm.find_word(text, 10, True) == (10, 11)                     # the comma alone
    assert sm.find_word(text, 11, True) == (11, 13)                     # both blanks
    assert sm.find_word(text, 17, True) == (17, 19)
    assert sm.find_word(text, len(text), True) == (len(text), len(text))
    assert sm.find_word(text, 0, False) == (0, 0)


def test_transliterating_roman_changes_only_case():
    text = b"Caf" + bytes([E_ACUTE])
    assert sm.transliterate(text, sm.smTransUpper, -1) == b"CAF" + bytes([CAP_E_ACUTE])
    assert sm.transliterate(text, sm.smTransUpper, sm.smMaskAscii) == b"CAF" + bytes([E_ACUTE])
    assert sm.transliterate(text, sm.smTransLower, -1) == b"caf" + bytes([E_ACUTE])
    assert sm.transliterate(text, sm.smTransAscii, -1) == text
    assert sm.transliterate(text, sm.smTransLower | sm.smTransUpper, -1) is None


def test_visible_length_leaves_out_trailing_blanks():
    assert sm.visible_length(b"two words \t\r") == 9
    assert sm.visible_length(b"   ") == 0


def test_slop_goes_to_the_spaces():
    assert sm.char_locations(b"a b c", 0, mono) == [0, 6, 12, 18, 24, 30]
    assert sm.char_locations(b"a b c", 10, mono) == [0, 6, 17, 23, 34, 40]
    assert sm.char_locations(b"abc", 10, mono)[-1] == 18                # no spaces to take it


def test_pixel_to_char_finds_the_nearest_boundary():
    assert sm.pixel_to_char(b"abc", 0, 2, mono) == (0, True, -1)        # left half of a
    assert sm.pixel_to_char(b"abc", 0, 4, mono) == (1, False, -1)       # right half
    assert sm.pixel_to_char(b"abc", 0, 13, mono) == (2, True, -1)
    assert sm.pixel_to_char(b"abc", 0, 25, mono) == (3, False, 7)       # past the end
    assert sm.pixel_to_char(b"abc", 0, -3, mono) == (0, True, -1)


def test_line_breaks():
    text = b"the quick brown fox"
    # All of it fits: the width left is passed on to the next run
    assert sm.line_break(text, 0, 19, 200, True, mono) == (sm.smBreakOverflow, 19, 200 - 114)
    # After "quick ", whose blank can hang past the edge
    assert sm.line_break(text, 0, 19, 60, True, mono) == (sm.smBreakWord, 10, 60)
    assert sm.line_break(text, 0, 19, 54, True, mono) == (sm.smBreakWord, 10, 54)
    # A word longer than the line: by character on the line's first run...
    assert sm.line_break(b"abcdefgh", 0, 8, 20, True, mono) == (sm.smBreakChar, 3, 20)
    # ...and before the run on a later one
    assert sm.line_break(b"abcdefgh", 2, 8, 20, False, mono) == (sm.smBreakWord, 2, 20)


def test_truncation():
    assert sm.truncate(b"short", 30, sm.smTruncEnd, mono) is None
    assert sm.truncate(b"truncated", 30, sm.smTruncEnd, mono) == b"trun\xc9"
    assert sm.truncate(b"truncated", 30, sm.smTruncMiddle, mono) == b"tr\xc9ed"
    assert sm.truncate(b"truncated", 3, sm.smTruncEnd, mono) == b""


def test_the_variables():
    settings = {}
    assert sm.environs(sm.smVersion, settings) == sm.VERSION == 0x0700
    assert sm.environs(sm.smEnabled, settings) == 1
    assert sm.environs(sm.smKeyScript, settings) == sm.smRoman
    assert sm.environs(sm.smKeyDisableState + 2, settings) is None
    assert sm.script_variable(sm.smScriptSysFond) == 0
    assert sm.script_variable(sm.smScriptMonoFondSize) == 4 << 16 | 9
    assert sm.script_variable(sm.smScriptCreator) == int.from_bytes(b"ZSYS", "big")
    assert sm.script_variable(sm.smScriptName + 2) is None


# =========================================================================
# The traps
# =========================================================================
class ScriptGuest(Guest):
    def __init__(self):
        super().__init__()
        self.mem.w16(lowmem.ApFontID, 3)
        port, _ = self.port(200, 40)
        self.set_port(port)

    def text(self, data: bytes) -> int:
        return self.ptr(data)

    def width(self, data: bytes) -> int:
        return self.tb.StringWidth(data.decode("mac_roman"))


def test_variables_through_the_traps(qapp):
    g = ScriptGuest()
    assert g("GetScriptManagerVariable", sm.smVersion) == 0x0700
    assert g("GetScriptVariable", sm.smRoman, sm.smScriptAppFond) == 3
    assert g("GetScriptVariable", 1, sm.smScriptAppFond) == 0               # Japanese: none
    assert g("SetScriptManagerVariable", sm.smFontForce, 1) == 0
    assert g("GetScriptManagerVariable", sm.smFontForce) == 1
    assert g("SetScriptManagerVariable", sm.smVersion, 1) == sm.smVerbNotFound
    assert g("FontToScript", 21) == g("FontScript") == g("IntlScript") == sm.smRoman


def test_transliterate_sizes_the_destination(qapp):
    g = ScriptGuest()
    src, dst = g.handle(b"Caf" + bytes([E_ACUTE])), g.handle(b"")
    assert g("Transliterate", src, dst, sm.smTransUpper, -1) == 0
    assert g.contents(dst) == b"CAF" + bytes([CAP_E_ACUTE])
    assert g("Transliterate", src, dst, sm.smTransUpper | sm.smTransLower, -1) == -1


def test_text_util_functions_work_in_place(qapp):
    g = ScriptGuest()
    word = b"R" + bytes([E_ACUTE]) + b"sum" + bytes([E_ACUTE])
    for routine, want in ((0x0000, b"r" + bytes([E_ACUTE]) + b"sum" + bytes([E_ACUTE])),
                          (0x0200, b"Resume"),
                          (0x0400, b"R" + bytes([CAP_E_ACUTE]) + b"SUM" + bytes([CAP_E_ACUTE])),
                          (0x0600, b"RESUME")):
        p = g.text(word)
        g("TextUtilFunctions", p, len(word), sm.smRoman, routine)
        assert g.mem.read(p, len(word)) == want


def test_character_traps(qapp):
    g = ScriptGuest()
    p = g.text(b"a1 " + bytes([CAP_E_ACUTE]))
    assert [g("CharType", p, i) for i in range(4)] == [
        sm.smCharAscii, sm.smPunctNumber, sm.smPunctBlank, sm.smCharEuro | sm.smCharUpper]
    assert g("CharByte", p, 0) == 0
    table = g.ptr(b"\xff" * 256)
    assert g("ParseTable", table) and g.mem.read(table, 256) == bytes(256)


def test_find_word_and_hilite_text_fill_an_offset_table(qapp):
    g = ScriptGuest()
    text = b"one two"
    p, table = g.text(text), g.ptr(b"\xff" * 12)
    g("FindWord", p, len(text), 5, 1, 0, table)
    assert struct.unpack(">6h", g.mem.read(table, 12)) == (4, 7, 0, 0, 0, 0)
    g("HiliteText", p, len(text), 6, 2, table)
    assert struct.unpack(">6h", g.mem.read(table, 12)) == (2, 6, 0, 0, 0, 0)


def test_measuring_in_the_port(qapp):
    g = ScriptGuest()
    text = b"Hi there"
    p = g.text(text)
    assert g("Char2Pixel", p, len(text), 0, 3, 0) == g.width(b"Hi ")
    assert g("Char2Pixel", p, len(text), 8, 3, 0) == g.width(b"Hi ") + 8
    edge = g.ptr(b"\xff")
    x = g.width(b"Hi t") + 1
    assert g("Pixel2Char", p, len(text), 0, x, edge) == 4 and g.mem.r8(edge) == 1
    locs = g.ptr(bytes(2 * (len(text) + 1)))
    g("MeasureJust", p, len(text), 0, locs)
    got = struct.unpack(f">{len(text) + 1}h", g.mem.read(locs, 2 * (len(text) + 1)))
    assert got == tuple(g.width(text[:i]) for i in range(len(text) + 1))
    assert g("VisibleLength", g.text(b"end  "), 5) == 3


def test_truncstring_and_styledlinebreak_in_the_port(qapp):
    g = ScriptGuest()
    s = g.ptr(b"\x0bA long name" + bytes(244))
    assert g("TruncString", 1000, s, sm.smTruncEnd) == sm.NotTruncated
    assert g("TruncString", g.width(b"A lo\xc9"), s, sm.smTruncEnd) == sm.Truncated
    assert g.pstr(s) == b"A lo\xc9"
    text = b"several words here"
    p, w, off = g.text(text), g.ptr(struct.pack(">l", g.width(b"several words") << 16)), \
        g.ptr(struct.pack(">l", 1))
    assert g("StyledLineBreak", p, len(text), 0, len(text), 0, w, off) == sm.smBreakWord
    assert g.mem.r32(off) == len(b"several words ")


def test_drawjust_widens_the_spaces(qapp):
    g = ScriptGuest()
    port, bits = g.port(200, 20)
    g.set_port(port)
    text = b"i i"
    g("MoveTo", 2, 14)
    g("DrawJust", g.text(text), len(text), 20)
    assert port.pn_loc.h == 2 + g.width(text) + 20
    assert port.sp_extra == 0                                   # as it was
    rows = g.rows(port)
    lit = [x for x in range(200) if any(row[x] != " " for row in rows)]
    assert lit[-1] - lit[0] >= g.width(text) + 20 - 4


# =========================================================================
# The dispatcher
# =========================================================================
def test_every_scriptutil_signature_takes_what_its_selector_says():
    for sig in dispatch.SIGNATURES.values():
        if sig.dispatcher == "ScriptUtil":
            sel = sig.selector
            args = (sel >> 16 & 0xFF) + (2 if sel & 0x40000000 else 0)
            assert (sig.stack_bytes, dispatch.SIZES.get(sig.result, 0)) == \
                (args, sel >> 24 & 0x3F), sig.name


def test_an_unknown_scriptutil_selector_is_skipped(qapp):
    m = Processor()
    m.cpu.push16(0x1234)                # the result
    m.cpu.push32(0)                     # six bytes of arguments, then a script code
    m.cpu.push16(0)
    m.cpu.push16(0)
    m.cpu.push32(0xC2060070)
    m.run(b"\xa8\xb5")
    assert m.missing == ["ScriptUtil selector $C2060070"]
    assert m.cpu.sp == m.stack_top - 2 and m.mem.r16(m.cpu.sp) == 0


def test_lowercasetext_through_the_processor(qapp):
    m = Processor()
    p = m.ptr(b"MiXeD")
    m.cpu.push32(p)
    m.cpu.push16(5)
    m.cpu.push16(sm.smRoman)
    m.cpu.push16(0x0000)                # LowercaseText
    m.cpu.push32(0x800AFFB6)
    m.run(b"\xa8\xb5")
    assert m.mem.read(p, 5) == b"mixed" and m.cpu.sp == m.stack_top


def test_uprstring_keeps_the_length(qapp):
    m = Processor()
    word = b"Stra" + bytes([SHARP_S, E_ACUTE]) + b"!"
    p = m.ptr(word + b"\xff")
    m.cpu.set_a(0, p)
    m.cpu.set_d(0, len(word))
    m.run(b"\xa0\x54")                                  # _UprString
    assert m.mem.read(p, len(word) + 1) == b"STRA" + bytes([SHARP_S, CAP_E_ACUTE]) + b"!\xff"
    m.cpu.set_a(0, p)
    m.run(b"\xa2\x54")                                  # _UprString ,MARKS
    assert m.mem.read(p, len(word) + 1) == b"STRA" + bytes([SHARP_S]) + b"E!\xff"
