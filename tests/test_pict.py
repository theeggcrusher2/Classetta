"""QuickDraw pictures: PackBits, hand-built version 1 and 2 pictures
played (scaled and clipped), pictures recorded with OpenPicture and
played back, and the masked copies (CopyMask, SeedFill, CalcMask)."""

import struct

import pytest

from macrt.resfork import Resource
from macrt.toolbox import pict
from macrt.toolbox.qdraw import srcCopy, srcXor
from macrt.toolbox.quickdraw import BLACK_PAT, GRAY_PAT
from macrt.toolbox.types import Rect

from guest import Guest, Processor

FRAME = (0, 0, 32, 64)
BLANK = ["." * 64] * 32


@pytest.fixture
def g(qapp):
    return Guest()


def rect(*r) -> bytes:
    return struct.pack(">hhhh", *r)


def v1(frame, body: bytes) -> bytes:
    """A version 1 picture: size, frame, $1101, the opcodes, $FF."""
    data = b"\x11\x01" + body + b"\xff"
    return struct.pack(">H", 10 + len(data)) + rect(*frame) + data


def v2(frame, *ops) -> bytes:
    """A version 2 picture: each (opcode, data) on a word boundary."""
    body = struct.pack(">HHH", 0x0011, 0x02FF, 0x0C00) + struct.pack(">l", -1) + bytes(20)
    for opcode, data in ops + ((0x00FF, b""),):
        body += struct.pack(">H", opcode) + data
        if len(body) & 1:
            body += b"\0"
    return struct.pack(">H", 10 + len(body)) + rect(*frame) + body


def play(g, port, data, dst=None, comment=None):
    frame = pict.picture_frame(data)
    pict.draw_picture(g.tb.qd, port, frame, pict.Reader.of(data, 10), Rect(*(dst or frame)),
                      comment)


def black(rows, top, left, bottom, right) -> bool:
    """Is every pixel of the rectangle black?"""
    return all(rows[y][x] == "#" for y in range(top, bottom) for x in range(left, right))


def ones(rows) -> int:
    return sum(r.count("#") for r in rows)


# -- PackBits -------------------------------------------------------------------
def test_pack_bits_is_inside_macintoshs_example():
    unpacked = bytes.fromhex("AAAAAA80002AAAAAAAAA80002A22" + "AA" * 10)
    packed = bytes.fromhex("FEAA0280002AFDAA0380002A22F7AA")
    assert pict.pack_bits(unpacked) == packed
    assert pict.unpack_bits(packed, len(unpacked)) == unpacked


def test_pack_bits_round_trips_long_runs_and_literals():
    data = bytes(300) + bytes(range(256)) * 2 + b"\x55" * 129 + b"\x01\x02"
    assert pict.unpack_bits(pict.pack_bits(data), len(data)) == data
    # Words, for 16-bit pixel maps: two literal words, then one word 3 times.
    assert pict.unpack_bits(bytes.fromhex("01 1234 5678 FE ABCD"), 10, 2) == bytes.fromhex(
        "12345678ABCDABCDABCD")


def test_the_traps_move_the_pointers_past_what_they_took_and_gave(g):
    """A program packs and unpacks a bitmap a row at a time, each row from
    where the last left the pointers (IM I-470)."""
    rows = [bytes.fromhex("AAAAAA80002AAAAAAAAA80002A22" + "AA" * 10), bytes(24)]
    src, packed, out = g.ptr(b"".join(rows)), g.ptr(bytes(64)), g.ptr(bytes(48))
    vars_ = g.ptr(bytes(8))
    g.mem.w32(vars_, src)
    g.mem.w32(vars_ + 4, packed)
    for row in rows:
        g("PackBits", vars_, vars_ + 4, len(row))
    assert g.mem.r32(vars_) == src + 48
    assert g.mem.r32(vars_ + 4) == packed + 15 + 2
    assert g.mem.read(packed, 17) == bytes.fromhex("FEAA0280002AFDAA0380002A22F7AA E900")
    g.mem.w32(vars_, packed)
    g.mem.w32(vars_ + 4, out)
    for row in rows:
        g("UnpackBits", vars_, vars_ + 4, len(row))
    assert (g.mem.r32(vars_), g.mem.r32(vars_ + 4)) == (packed + 17, out + 48)
    assert g.mem.read(out, 48) == b"".join(rows)


# -- playing pictures -----------------------------------------------------------
def test_version_1_shapes_lines_and_bits(g):
    port, _ = g.port()
    bits = bytes.fromhex("FFFF 8001 8001 FFFF")         # a 16-by-4 box
    data = v1(FRAME, b"".join([
        b"\x31" + rect(2, 2, 10, 20),                   # PaintRect
        b"\x07" + struct.pack(">hh", 2, 2),             # PnSize 2, 2
        b"\x20" + struct.pack(">hhhh", 20, 2, 20, 30),  # Line (20, 2) to (20, 30), as (v, h)
        b"\x90" + struct.pack(">H", 2) + rect(0, 0, 4, 16) + rect(0, 0, 4, 16)
        + rect(24, 40, 28, 56) + struct.pack(">h", srcCopy) + bits,     # BitsRect
        b"\x0B" + struct.pack(">hh", 4, 4) + b"\x41" + rect(2, 40, 12, 60),  # PaintRoundRect
    ]))
    play(g, port, data)
    rows = g.rows(port)
    assert black(rows, 2, 2, 10, 20) and rows[1][2] == rows[2][20] == "."
    assert black(rows, 20, 2, 22, 31) and rows[22][10] == "."
    assert rows[24][40:56] == "#" * 16 and rows[25][40:56] == "#" + "." * 14 + "#"
    assert rows[2][40] == "." and rows[6][40] == "#"     # a rounded corner, a straight side
    # The port's settings are back as they were.
    assert port.pn_size == (1, 1)


def test_version_2_with_packbits_comments_and_reserved_opcodes(g):
    port, _ = g.port()
    rows8 = [b"\xff" * 8, bytes(8)] * 4                 # 64 wide, 8 high: stripes
    packed = b"".join(bytes([len(p)]) + p for p in map(pict.pack_bits, rows8))
    seen = []
    data = v2(
        FRAME,
        (0x001E, b""),                                  # DefHilite
        (0x00A0, struct.pack(">h", 100)),               # ShortComment
        (0x00A1, struct.pack(">hH", 200, 3) + b"abc"),  # LongComment, odd: padded
        (0x0024, struct.pack(">H", 3) + b"xyz"),        # reserved: a length and that much
        (0x0100, b"\0\0"),                              # reserved: 2 bytes
        (0x8100, struct.pack(">L", 5) + b"12345"),      # reserved: a long length
        (0x00B0, b""),                                  # reserved: nothing
        (0x001A, struct.pack(">HHH", 0, 0, 0)),         # RGBFgCol black
        (0x0098, struct.pack(">H", 8) + rect(0, 0, 8, 64) + rect(0, 0, 8, 64)
         + rect(4, 0, 12, 64) + struct.pack(">h", srcCopy) + packed),   # PackBitsRect
    )
    play(g, port, data, comment=lambda kind, d: seen.append((kind, d)))
    assert seen == [(100, None), (200, b"abc")]
    rows = g.rows(port)
    assert [rows[y] for y in range(4, 12)] == ["#" * 64, "." * 64] * 4
    assert ones(rows) == 4 * 64


def test_a_pixel_map_with_a_colour_table_is_thresholded(g):
    port, _ = g.port()
    ctab = struct.pack(">lHh", 0, 0, 1) + struct.pack(">HHHH", 0, 0xFFFF, 0xFFFF, 0xFFFF) \
        + struct.pack(">HHHH", 1, 0x8000, 0, 0)          # white, and a dark red
    pixmap = struct.pack(">hhlllhhhhlll", 0, 0, 0, 72 << 16, 72 << 16, 0, 8, 1, 8, 0, 0, 0)
    row = bytes([1] * 8 + [0] * 8)                       # 16 pixels at 8 bits: packed
    packed = b"".join(bytes([len(p)]) + p for p in [pict.pack_bits(row)] * 4)
    data = v2(FRAME, (0x0098, struct.pack(">H", 0x8000 | 16) + rect(0, 0, 4, 16) + pixmap
                      + ctab + rect(0, 0, 4, 16) + rect(0, 0, 4, 16)
                      + struct.pack(">h", srcCopy) + packed))
    play(g, port, data)
    rows = g.rows(port)
    assert [r[:16] for r in rows[:4]] == ["#" * 8 + "." * 8] * 4
    assert ones(rows) == 32


def test_direct_bits_in_32_bits(g):
    port, _ = g.port()
    pixmap = struct.pack(">hhlllhhhhlll", 0, 4, 0, 72 << 16, 72 << 16, 16, 32, 3, 8, 0, 0, 0)
    width = 8
    # packType 4: each row's red, green and blue planes in turn, packed.
    plane = bytes([0] * 4 + [255] * 4)                   # black then white
    row = pict.pack_bits(plane * 3)
    data = v2(FRAME, (0x009A, struct.pack(">L", 0xFF) + struct.pack(">H", 0x8000 | 4 * width)
                      + rect(0, 0, 2, width) + pixmap + rect(0, 0, 2, width)
                      + rect(0, 0, 2, width) + struct.pack(">h", srcCopy)
                      + (bytes([len(row)]) + row) * 2))
    play(g, port, data)
    rows = g.rows(port)
    assert rows[0][:8] == rows[1][:8] == "####...."
    assert ones(rows) == 8


def test_a_picture_scales_to_the_destination(g):
    port, _ = g.port()
    data = v1((0, 0, 8, 16), b"\x31" + rect(0, 0, 4, 8) + b"\x51" + rect(4, 8, 8, 16))
    play(g, port, data, dst=(0, 0, 16, 32))
    rows = g.rows(port)
    assert black(rows, 0, 0, 8, 16) and rows[8][0] == rows[0][16] == "."
    assert black(rows, 11, 19, 13, 29)                  # the oval's middle, doubled
    assert rows[8][16] == rows[15][31] == "."           # not its corners


def test_a_picture_is_clipped_to_its_clip_and_the_ports(g):
    port, _ = g.port()
    g("ClipRect", g.rect_ptr((0, 0, 4, 64)))
    data = v1(FRAME, b"\x01" + struct.pack(">H", 10) + rect(0, 0, 8, 8)
              + b"\x31" + rect(0, 0, 32, 64))
    play(g, port, data)
    rows = g.rows(port)
    assert black(rows, 0, 0, 4, 8) and ones(rows) == 32
    assert g.rect(g.heap.deref(port.clip_rgn) + 2) == Rect(0, 0, 4, 64)   # put back


def test_init_port_sets_the_regions_it_keeps_afresh(g):
    """Dark Castle draws a picture in a port it has set up again with
    InitPort, after narrowing its visRgn for a smaller bitmap."""
    port, _ = g.port()
    vis, clip = port.vis_rgn, port.clip_rgn
    g("ClipRect", g.rect_ptr((0, 0, 4, 64)))
    g("RectRgn", vis, g.rect_ptr((0, 0, 8, 8)))
    g("InitPort", port.addr)
    assert (port.vis_rgn, port.clip_rgn) == (vis, clip)
    assert g.rect(g.heap.deref(vis) + 2) == g.system.screen_bits.bounds
    assert g.rect(g.heap.deref(clip) + 2) == Rect(-32767, -32767, 32767, 32767)


def test_a_bad_picture_draws_what_it_can(g):
    port, _ = g.port()
    data = v1(FRAME, b"\x31" + rect(0, 0, 2, 2) + b"\x90" + struct.pack(">H", 2))  # cut short
    play(g, port, data)
    assert ones(g.rows(port)) == 4


# -- recording ------------------------------------------------------------------
def scene(g):
    """Some of everything a picture records."""
    g("ClipRect", g.rect_ptr((0, 0, 30, 64)))
    g("PenSize", 2, 1)
    g("FrameRect", g.rect_ptr((1, 1, 12, 20)))
    g("PaintOval", g.rect_ptr((3, 3, 10, 18)))
    g("PenNormal")
    g("FillRoundRect", g.rect_ptr((14, 1, 30, 20)), 8, 8, g.ptr(GRAY_PAT))
    g("InvertArc", g.rect_ptr((14, 1, 30, 20)), 0, 90)
    g("PenPat", g.ptr(GRAY_PAT))
    g("MoveTo", 22, 2)
    g("LineTo", 40, 12)
    g("Line", 10, 0)
    g("PenPat", g.ptr(BLACK_PAT))
    poly = g("OpenPoly")
    g("MoveTo", 44, 14)
    g("LineTo", 62, 14)
    g("LineTo", 53, 28)
    g("LineTo", 44, 14)
    g("ClosePoly")
    g("PaintPoly", poly)
    g("FramePoly", poly)
    rgn = g("NewRgn")
    g("RectRgn", rgn, g.rect_ptr((20, 22, 28, 40)))
    g("InvertRgn", rgn)
    g("TextFace", 1)
    g("MoveTo", 22, 26)
    g("DrawString", g.ptr(b"\x02Hi"))
    src_bits = g.ptr(bytes.fromhex("F0F0 0F0F") * 4)
    bitmap = g.ptr(struct.pack(">LH", src_bits, 2) + rect(0, 0, 8, 16))
    port = g.tb.qd.sys.port_at(g.mem.r32(g.mem.r32(g.process.partition.a5)))
    g("CopyBits", bitmap, port.addr + 2, g.rect_ptr((0, 0, 8, 16)), g.rect_ptr((2, 44, 10, 60)),
      srcCopy, 0)
    g("PicComment", 150, 0, 0)


def test_a_recorded_picture_plays_back_as_it_was_drawn(g):
    direct, _ = g.port()
    scene(g)
    expected = g.rows(direct)
    assert ones(expected) > 300

    recording, _ = g.port()
    pic = g("OpenPicture", g.rect_ptr(FRAME))
    assert recording.pic_save and recording.pn_vis == -1
    scene(g)
    g("ClosePicture")
    assert recording.pic_save == 0 and recording.pn_vis == 0
    assert g.rows(recording) == BLANK                   # nothing drawn while it recorded
    assert g.heap.handle_size(pic) == struct.unpack(">H", g.contents(pic)[:2])[0]

    played, _ = g.port()
    g("DrawPicture", pic, g.rect_ptr(FRAME))
    assert g.rows(played) == expected


def test_the_opcodes_a_recording_makes(g):
    g.port()
    pic = g("OpenPicture", g.rect_ptr(FRAME))
    g("ClipRect", g.rect_ptr(FRAME))
    g("PaintRect", g.rect_ptr((1, 2, 3, 4)))
    g("PaintRect", g.rect_ptr((1, 2, 3, 4)))
    g("PicComment", 100, 0, 0)
    g("PicComment", 101, 2, g.handle(b"ok"))
    g("ClosePicture")
    assert g.contents(pic).hex() == (
        "002c" "000000000020" "0040"                    # size, frame
        "1101"                                          # version 1
        "01" "000a" "0000000000200040"                  # clip
        "31" "0001000200030004"                         # paintRect
        "39"                                            # paintSameRect
        "a0" "0064"                                     # shortComment
        "a1" "0065" "0002" "6f6b"                       # longComment
        "ff")


def test_a_colour_picture_is_version_2(g):
    port, _ = g.port()
    params = g.ptr(rect(*FRAME) + struct.pack(">llhhl", 72 << 16, 72 << 16, -2, 0, 0))
    pic = g("OpenCPicture", params)
    g("PaintRect", g.rect_ptr((0, 0, 4, 4)))
    g("ClosePicture")
    data = g.contents(pic)
    assert data[10:16].hex() == "001102ff0c00" and data[16:18] == b"\xff\xfe"
    assert data[-2:] == b"\x00\xff" and len(data) % 2 == 0
    g("DrawPicture", pic, g.rect_ptr(FRAME))
    assert ones(g.rows(port)) == 16


def test_get_picture_and_kill_picture(qapp):
    g = Guest([Resource(b"PICT", 128, v1(FRAME, b"\x31" + rect(0, 0, 2, 2)))])
    port, _ = g.port()
    h = g("GetPicture", 128)
    assert h
    g("DrawPicture", h, g.rect_ptr((0, 0, 64, 128)))  # twice the size
    assert ones(g.rows(port)) == 16
    g("KillPicture", h)                                 # a resource: released, not disposed
    h2 = g("GetPicture", 128)
    assert h2 and g.contents(h2)[:2] == struct.pack(">H", 10 + 2 + 9 + 1)


def test_spooled_pictures_and_comment_procs(g):
    """A port's getPicProc gives the opcodes after the size and frame,
    and its commentProc sees the comments."""
    port, _ = g.port()
    data = v1(FRAME, b"\xa0\x00\x07" + b"\x31" + rect(0, 0, 2, 2))
    stream = bytearray(data[10:])
    procs = g.ptr(bytes(52))
    g("SetStdProcs", procs)
    assert g.mem.r32(procs + 44) == g.machine.stub_for(0xA8EE)
    g.mem.w32(procs + 44, 0x1234)                       # getPicProc
    g.mem.w32(procs + 36, 0x5678)                       # commentProc
    port.graf_procs = procs
    comments = []

    def get_pic(buf, n):
        g.mem.write(buf, bytes(stream[:n]))
        del stream[:n]
    g.procs[0x1234] = get_pic
    g.procs[0x5678] = lambda *args: comments.append(args)
    header = g.handle(data[:10])
    g("DrawPicture", header, g.rect_ptr(FRAME))
    assert ones(g.rows(port)) == 4 and comments == [(7, 0, 0)]


def test_text_goes_through_the_ports_text_procs(qapp):
    """A port's own textProc draws, and its txMeasProc measures, the text
    the drawing and width calls are given, at the scale it leaves in numer
    and denom: Macintosh Pascal's interpret the formatting bytes in its
    program text."""
    g = Processor()
    port, _ = g.port()
    procs = g.ptr(bytes(52))
    g("SetStdProcs", procs)
    g.mem.w32(procs + 0, 0x1234)                        # textProc
    g.mem.w32(procs + 40, 0x5678)                       # txMeasProc
    port.graf_procs = procs
    drawn = []

    def measure(count, p, numer, denom, info):
        g.mem.w16(numer + 2, 2)                         # twice as wide
        return 5 * count
    g.procs[0x1234] = lambda count, p, numer, denom: drawn.append(g.mem.read(p, count))
    g.procs[0x5678] = measure
    text = g.ptr(b"xhello")
    g("DrawText", text, 1, 5)
    g("DrawChar", ord("!"))
    g("DrawString", g.ptr(b"\x02hi"))
    assert drawn == [b"hello", b"!", b"hi"]
    assert {tuple(v for _, v in args[2:]) for at, args in g.calls if at == 0x1234} \
        == {(0x10001, 0x10001)}                         # numer and denom: 1:1
    assert g("TextWidth", text, 1, 5) == 50
    assert g("CharWidth", ord("a")) == 10
    assert g("StringWidth", g.ptr(b"\x02hi")) == 20
    assert ones(g.rows(port)) == 0                      # nothing drawn the standard way


def test_std_tx_meas_measures_and_gives_the_font_info(g):
    port, _ = g.port()
    text = g.ptr(b"hello")
    info, numer, denom = g.ptr(bytes(8)), g.ptr(bytes(4)), g.ptr(bytes(4))
    width = g("StdTxMeas", 5, text, numer, denom, info)
    assert width == g("TextWidth", text, 0, 5) > 0
    fi = g.ptr(bytes(8))
    g("GetFontInfo", fi)
    assert g.mem.read(info, 8) == g.mem.read(fi, 8) != bytes(8)
    assert g("StdTxMeas", 0, text, numer, denom, info) == 0


def test_draw_picture_puts_the_ports_state_back(g):
    port, _ = g.port()
    g("PenSize", 3, 3)
    g("TextSize", 18)
    before = g.mem.read(port.addr, 108)
    data = v1(FRAME, b"\x07" + struct.pack(">hh", 5, 5) + b"\x0d\x00\x09"
              + b"\x09" + GRAY_PAT + b"\x31" + rect(0, 0, 1, 1))
    g("DrawPicture", g.handle(data), g.rect_ptr(FRAME))
    assert g.mem.read(port.addr, 108) == before


# -- masks ----------------------------------------------------------------------
def bitmap(g, rows: list[bytes], width: int) -> int:
    rb = len(rows[0])
    bits = g.ptr(b"".join(rows))
    return g.ptr(struct.pack(">LH", bits, rb) + rect(0, 0, len(rows), width))


def test_copy_mask_copies_only_where_the_mask_is_set(g):
    port, _ = g.port()
    g("PaintRect", g.rect_ptr((0, 0, 8, 16)))           # what's there: black
    src = bitmap(g, [b"\xff\x00"] * 8, 16)              # left half black, right white
    mask = bitmap(g, [b"\x0f\xf0"] * 8, 16)             # the middle 8 columns
    r = g.rect_ptr((0, 0, 8, 16))
    g("CopyMask", src, mask, port.addr + 2, r, r, r)
    assert g.rows(port)[0][:16] == "#" * 8 + "...." + "####"
    # Scaled: mask and source map onto the destination together.
    g("CopyMask", src, mask, port.addr + 2, r, r, g.rect_ptr((10, 0, 26, 32)))
    assert g.rows(port)[12][:32] == "." * 8 + "#" * 8 + "." * 8 + "." * 8


def test_copy_deep_mask_has_a_mode_and_a_mask_region(g):
    port, _ = g.port()
    src = bitmap(g, [b"\xff\xff"] * 8, 16)
    mask = bitmap(g, [b"\xff\xff"] * 8, 16)
    rgn = g("NewRgn")
    g("RectRgn", rgn, g.rect_ptr((0, 0, 8, 4)))
    r = g.rect_ptr((0, 0, 8, 16))
    g("PaintRect", g.rect_ptr((0, 0, 8, 2)))
    g("CopyDeepMask", src, mask, port.addr + 2, r, r, r, srcXor, rgn)
    assert g.rows(port)[0][:16] == "..##" + "." * 12


def test_seed_fill_and_calc_mask(g):
    box = [b"\x00\x00", b"\x7e\x00", b"\x42\x00", b"\x42\x00", b"\x7e\x00", b"\x00\x00",
           b"\x00\x00", b"\x00\x00"]                      # a hollow square, open space around
    src = g.ptr(b"".join(box))
    dst = g.ptr(bytes(16))
    g("SeedFill", src, dst, 2, 2, 8, 1, 3, 2)            # from inside the square
    assert g.mem.read(dst, 16) == bytes.fromhex("0000 0000 3c00 3c00 0000 0000 0000 0000")
    g("SeedFill", src, dst, 2, 2, 8, 1, 10, 6)           # from outside it
    assert g.mem.read(dst, 4) == bytes.fromhex("ffff 81ff")
    g("CalcMask", src, dst, 2, 2, 8, 1)
    assert g.mem.read(dst, 16) == bytes.fromhex("0000 7e00 7e00 7e00 7e00 0000 0000 0000")


def test_mask_regions_follow_the_mask(g):
    from macrt.toolbox.masks import calc_mask, seed_fill
    assert calc_mask([b"\x80"], 8) == [b"\x80"]
    assert seed_fill([b"\x10"], 8, 0, 0) == [b"\xe0"]
