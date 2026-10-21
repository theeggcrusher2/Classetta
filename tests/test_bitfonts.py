"""An application's own bitmap fonts, drawn from their strikes; the
font records the Font Manager leaves in low memory; and the System's
pattern list."""

import struct

from PyQt6.QtGui import QFontMetrics

from macrt.resfork import Resource
from macrt.toolbox import lowmem, sysres
from macrt.toolbox.bitfont import Strike
from macrt.toolbox.menus import Menu, parse_menu_items
from macrt.ui import theme
from macrt.ui.menubar import Pulldown, _title_width

from builders import font_rec
from guest import Guest

A = ([".##.",
      "#..#",
      "####",
      "#..#",
      "#..#",
      "...."], 0, 5)
C = ([".###",
      "#...",
      "#...",
      "#...",
      ".###",
      "...."], 0, 5)
BOX = (["###",
        "#.#",
        "#.#",
        "#.#",
        "###",
        "..."], 0, 4)


def blocky(**kw) -> bytes:
    """A and C, B missing; ascent 5, descent 1."""
    return font_rec(65, 67, {65: A, 67: C}, BOX, 5, 1, **kw)


def pixels(img) -> list[str]:
    return ["".join("#" if img.pixelColor(x, y).alpha() else "." for x in range(img.width()))
            for y in range(img.height())]


def test_a_strike_whose_ow_table_stops_at_the_missing_glyph(qapp):
    s = Strike.parse(blocky(final_ow=False))
    assert (s.first, s.last, s.ascent, s.descent, s.wid_max) == (65, 67, 5, 1, 5)
    assert s.width(b"AC") == 10
    assert s.char_width(66) == 4 and s.char_width(32) == 4         # the missing glyph
    assert s.width(b"AC", face=1) == 12                             # bold: a pixel more each
    assert Strike.parse(blocky()[:40]) is None


def test_a_strike_renders_its_glyphs_side_by_side(qapp):
    img, pen, baseline = Strike.parse(blocky()).render(b"AB")
    assert (pen, baseline) == (0, 5)
    assert pixels(img) == [".##..###.",
                           "#..#.#.#.",
                           "####.#.#.",
                           "#..#.#.#.",
                           "#..#.###.",
                           "........."]


def test_kern_max_and_the_glyph_offset_move_it_from_the_pen(qapp):
    data = font_rec(65, 65, {65: (A[0], 2, 5)}, BOX, 5, 1, kern_max=-1)
    img, pen, _ = Strike.parse(data).render(b"A")
    assert pen == 0 and pixels(img)[2] == ".####"                    # at pen - 1 + 2


def drawing(resources, family=200, size=12, v=7):
    g = Guest(resources)
    port, _ = g.port(48, 24)
    g("TextFont", family)
    g("TextSize", size)
    g("MoveTo", 2, v)
    g("DrawString", g.ptr(b"\x02AC"))
    return g, port


def test_drawing_in_an_applications_font(qapp):
    g, port = drawing([Resource(b"FONT", 200 << 7, b"", b"Blocky"),
                       Resource(b"FONT", 200 << 7 | 12, blocky())])
    assert [row[:14] for row in g.rows(port)[2:9]] == ["...##...###...",
                                                       "..#..#.#......",
                                                       "..####.#......",
                                                       "..#..#.#......",
                                                       "..#..#..###...",
                                                       "..............",
                                                       ".............."]
    assert port.pn_loc.h == 12
    assert g("StringWidth", g.ptr(b"\x02AC")) == 10
    info = g.ptr(bytes(8))
    g("GetFontInfo", info)
    assert struct.unpack(">4h", g.mem.read(info, 8)) == (5, 1, 5, 0)


def test_measure_text_and_text_box_go_by_the_applications_strike(qapp):
    # The Oregon Trail breaks its messages into lines with MeasureText and
    # labels its thermometer with TETextBox, both in its own font.
    g, port = drawing([Resource(b"FONT", 200 << 7, b"", b"Blocky"),
                       Resource(b"FONT", 200 << 7 | 12, blocky())])
    locs = g.ptr(bytes(8))
    g("MeasureText", 3, g.ptr(b"ACA"), locs)
    assert struct.unpack(">4h", g.mem.read(locs, 8)) == (0, 5, 10, 15)
    g("TextFace", 1)
    g("MeasureText", 2, g.ptr(b"AC"), locs)
    assert struct.unpack(">3h", g.mem.read(locs, 6)) == (0, 6, 12)
    g("TextFace", 0)
    g("TETextBox", g.ptr(b"AC AC"), 5, g.ptr(struct.pack(">4h", 0, 0, 24, 12)), 0)
    rows = [row[:12] for row in g.rows(port)]
    assert rows[0][:9] == rows[6][:9] == ".##...###"    # a line each, 6 pixels apart
    assert rows[12] == "." * 12


def test_an_nfnt_a_fond_lists(qapp):
    g, port = drawing([Resource(b"FOND", 300, sysres.fond(300, (9,), ids=(3000,)), b"Blocky"),
                       Resource(b"NFNT", 3000, blocky())], family=300, size=9)
    assert g.rows(port)[4][:8] == "..####.#"
    assert port.pn_loc.h == 12


def test_a_size_it_hasnt_got_is_scaled_from_one_it_has(qapp):
    g, port = drawing([Resource(b"FONT", 200 << 7 | 9, blocky())], size=18, v=12)
    assert g("StringWidth", g.ptr(b"\x02AC")) == 20
    rows = g.rows(port)
    assert rows[2][:20] == "....####......######" and rows[3] == rows[2]   # doubled
    assert port.pn_loc.h == 22


def test_the_standard_families_stay_host_fonts(qapp):
    """but are measured with a FONT of theirs if there is one: a real
    System file's Chicago 12, whose digits the Alarm Clock adds up."""
    g = Guest()
    rm = g.process.resources
    rm.UseResFile(0)
    rm.AddResource(g.handle(blocky()), "FONT", 3 << 7 | 12)
    port, _ = g.port(48, 24)
    g("TextFont", 3)
    g("TextSize", 12)
    g("MoveTo", 2, 7)
    g("DrawString", g.ptr(b"\x02AC"))
    assert g.tb.qd.strike(port) is None
    assert port.pn_loc.h == 2 + 10
    assert g("StringWidth", g.ptr(b"\x02AC")) == 10
    assert g("CharWidth", ord("B")) == 4            # the missing glyph's
    info = g.ptr(bytes(8))
    g("GetFontInfo", info)
    assert struct.unpack(">4h", g.mem.read(info, 8)) == (5, 1, 5, 0)
    assert output(g)["handle"] == rm.GetResource("FONT", 3 << 7 | 12)
    g("TextFace", 1)
    assert g("StringWidth", g.ptr(b"\x02AC")) == 12  # bold: a pixel more each
    g("TextFace", 0)
    g("TextFont", 1)                                # the application font: Geneva
    assert g("StringWidth", g.ptr(b"\x02AC")) == 10
    g("TextSize", 9)                                # a size it hasn't: the host font's
    assert g("StringWidth", g.ptr(b"\x02AC")) == sum(g.tb.qd.fonts.advances(3, 9, 0, "AC"))


def test_what_the_font_has_no_width_for_draws_nothing(qapp):
    """NUL and return are blank glyphs of no width in Chicago, Geneva and
    Monaco, where the host font standing in would draw boxes: Social
    Climber pads its scores with NULs."""
    def drawn(text, strike=None):
        g = Guest()
        if strike is not None:
            rm = g.process.resources
            rm.UseResFile(0)
            rm.AddResource(g.handle(strike), "FONT", 3 << 7 | 12)
        port, _ = g.port(64, 24)
        g("TextFont", 3)
        g("TextSize", 12)
        g("MoveTo", 2, 14)
        g("DrawString", g.ptr(bytes([len(text)]) + text))
        return g.rows(port), port.pn_loc.h

    assert drawn(b"A\x00B\rC") == drawn(b"ABC")
    blank = ([""] * 6, 0, 0)
    strike = font_rec(0, 67, {0: blank, 13: blank, 65: A, 67: C}, BOX, 5, 1)
    assert drawn(b"A\x00C\r", strike) == drawn(b"AC", strike)


def test_a_standard_family_the_application_has_its_own_strike_of(qapp):
    """is drawn from it, as on a Mac, where the application's file is
    looked in first: Balance of Power traces its New York 36 title's
    letters from where they are in its own strike."""
    g, port = drawing([Resource(b"FONT", 2 << 7 | 12, blocky())], family=2)
    rm = g.process.resources
    assert g.rows(port)[4][:8] == "..####.#"
    assert port.pn_loc.h == 12
    assert output(g)["handle"] == rm.GetResource("FONT", 2 << 7 | 12)
    g("TextSize", 9)                                # a size it hasn't: the host font
    assert g.tb.qd.strike(port) is None
    g, port = drawing([Resource(b"FONT", 12, blocky())], family=0)
    assert g.rows(port)[4][:8] == "..####.#"        # the system font's too
    assert port.pn_loc.h == 12


def test_the_application_font_is_the_family_apfontid_says(qapp):
    """Gemstone Warrior sets ApFontID to 0 and DlgFont to 1: its dialogs
    are in the Chicago 12 it brings, whose $B9 is a double arrow."""
    g = Guest([Resource(b"FONT", 12, blocky())])
    port, _ = g.port(48, 24)
    g("TextFont", 1)
    assert g.tb.qd.strike(port) is None             # Geneva, a host font
    g.mem.w16(lowmem.ApFontID, 0)
    g("MoveTo", 2, 7)
    g("DrawString", g.ptr(b"\x02AC"))
    assert g.rows(port)[4][:8] == "..####.#"
    assert g("StringWidth", g.ptr(b"\x02AC")) == 10


def test_the_menus_are_drawn_in_the_applications_own_chicago(qapp):
    """A Mac draws the menus in the system font, and the application's own
    Chicago 12 comes first: The Fool's Errand's has Roman numerals and an
    eye where its menus' titles have $80 to $84 and a tilde."""
    g = Guest([Resource(b"FONT", 12, blocky())])
    g.tb._gui = lambda fn, *args: fn(*args)
    g.tb.InsertMenu(g.tb.NewMenu(5, "AC"), 0)
    strike = g.process.system_strike
    assert strike is not None and strike.width(b"AC") == 10
    menu = Menu(5, "AC", parse_menu_items("CA"))
    assert _title_width(menu, QFontMetrics(theme.menu_bar_font()), strike) == 10
    pd = Pulldown(menu, None, strike)
    try:
        img = pd.grab().toImage()
    finally:
        pd.deleteLater()
    x, top = 1 + theme.MENU_LEFT_PAD, 1 + (theme.MENU_ITEM_HEIGHT - 6) // 2
    rows = ["".join("#" if img.pixelColor(x + i, top + j).lightness() < 128 else "."
                    for i in range(10)) for j in range(5)]
    assert rows == [
        ".###..##..",
        "#....#..#.",
        "#....####.",
        "#....#..#.",
        ".###.#..#.",
    ]
    g = Guest()                                     # with none of its own: the host's
    g.tb._gui = lambda fn, *args: fn(*args)
    g.tb.InsertMenu(g.tb.NewMenu(5, "AC"), 0)
    assert g.process.system_strike is None


def test_outline_is_every_pixel_round_the_glyph(qapp):
    """QuickDraw ORs the glyph one pixel each way, corners too, and takes
    the glyph out; shadow reaches a pixel further down and right."""
    dot = (["...", ".#.", "...", "..."], 0, 3)
    s = Strike.parse(font_rec(65, 65, {65: dot}, dot, 3, 1))
    img, _, _ = s.render(b"A", face=8)
    assert pixels(img)[1:4] == [".###..", ".#.#..", ".###.."]
    img, _, _ = s.render(b"A", face=16)
    assert pixels(img)[1:5] == [".####..", ".#.##..", ".####..", ".####.."]


def test_a_family_with_no_host_font_is_drawn_from_its_bitmap(qapp):
    """Cairo is pictures, not letters: Font Mover shows a sample of it with
    the FONT from the Fonts file it's opened."""
    g, port = drawing([Resource(b"FONT", 11 << 7 | 12, blocky())], family=11)
    assert g.rows(port)[4][:8] == "..####.#"
    g("TextFont", 9)                                # Toronto, which it hasn't
    assert g.tb.qd.strike(port) is None


def output(g) -> dict:
    """The Font Manager's output record, FMOutput, at FMgrOutRec."""
    fields = struct.unpack(">hIBBBBBBbBBBbBhhhh", g.mem.read(lowmem.FMgrOutRec, 26))
    names = ("err handle bold italic ul_offset ul_shadow ul_thick shadow extra ascent descent "
             "wid_max leading unused numer_v numer_h denom_v denom_h").split()
    return dict(zip(names, fields))


def test_a_host_fonts_record_has_the_widths_it_draws_with(qapp):
    """Word 1.0 measures text from the FONT whose handle the Font Manager
    leaves in low memory, not with CharWidth: for a family macrt draws with
    a host font, that's a FONT with the host font's widths."""
    g = Guest()
    g.port()
    g("TextFont", 2)
    g("TextSize", 12)
    widths = {c: g("CharWidth", c) for c in b"aWi .\x11"}
    out = output(g)
    font = Strike.parse(g.contents(out["handle"]))
    assert (font.first, font.last) == (0, 255)
    assert {c: font.char_width(c) for c in widths} == widths
    info = g.ptr(bytes(8))
    g("GetFontInfo", info)
    assert (out["ascent"], out["descent"]) == (font.ascent, font.descent) == struct.unpack(
        ">2h", g.mem.read(info, 4))
    assert (out["extra"], out["numer_h"], out["denom_h"]) == (0, 1, 1)
    g("TextFace", 1)                                # bold: its own record, its own widths
    width = g("CharWidth", ord("W"))
    bold = output(g)
    assert bold["handle"] != out["handle"] and bold["bold"] == 1 and bold["extra"] == 0
    assert Strike.parse(g.contents(bold["handle"])).char_width(ord("W")) == width
    g("TextFace", 0)
    g("CharWidth", ord("W"))
    assert output(g)["handle"] == out["handle"]     # made once


def test_an_applications_font_is_its_own_record(qapp):
    g, port = drawing([Resource(b"FONT", 200 << 7 | 9, blocky())], size=9)
    rm = g.process.resources
    assert output(g)["handle"] == rm.GetResource("FONT", 200 << 7 | 9)
    g("TextFace", 1)
    g("CharWidth", 65)
    assert output(g)["extra"] == 1                  # bold's pixel, which the widths haven't got
    # FMSwapFont itself, scaling the 9-point font to 18.
    fm_input = g.ptr(struct.pack(">hhBBhII", 200, 18, 0, 0, 0, 0x10001, 0x10001))
    assert g("FMSwapFont", fm_input) == lowmem.FMgrOutRec
    out = output(g)
    assert out["handle"] == rm.GetResource("FONT", 200 << 7 | 9)
    assert (out["numer_h"], out["denom_h"], out["ascent"], out["extra"]) == (18, 9, 10, 0)


def test_the_system_pattern_list(qapp):
    g = Guest()
    h = g.process.resources.GetResource("PAT#", 0)
    data = g.contents(h)
    count = struct.unpack(">H", data[:2])[0]
    patterns = [data[2 + 8 * i:10 + 8 * i] for i in range(count)]
    assert count == 38 and len(data) == 2 + 8 * count
    assert patterns[0] == b"\xff" * 8 and patterns[-1] == bytes(8)
    assert len(set(patterns)) == count


def test_the_systems_desktop_patterns(qapp):
    """'PAT ' 16, the desktop's gray, and 17, light gray: Jazz looks for
    the System's at startup, with its other resources."""
    g = Guest()
    rm = g.process.resources
    assert g.contents(rm.GetResource("PAT ", 16)) == b"\xaa\x55" * 4
    assert g.contents(rm.GetResource("PAT ", 17)) == b"\x88\x22" * 4
