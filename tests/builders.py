"""Resources built byte by byte, as ResEdit or Rez would leave them, and
the host-side wrappers Mac files travel in."""

import binascii
import struct

from macrt.macfile import BINHEX_CHARS
from macrt.toolbox import constants as k


def pstr(s):
    b = s.encode("mac_roman")
    return bytes([len(b)]) + b


def wind(bounds, title, proc_id=k.documentProc, visible=True, go_away=True, ref_con=0,
         position=None):
    d = struct.pack(">4hhBxBxI", *bounds, proc_id, visible, go_away, ref_con) + pstr(title)
    if position is not None:
        d += bytes(len(d) & 1) + struct.pack(">H", position)
    return d


def dlog(bounds, title, items_id, proc_id=k.dBoxProc, visible=True, go_away=False, ref_con=0):
    return struct.pack(">4hhBxBxIh", *bounds, proc_id, visible, go_away, ref_con,
                       items_id) + pstr(title)


def alrt(bounds, items_id, stages=0x5555, position=None):
    d = struct.pack(">4hhH", *bounds, items_id, stages)
    return d + (struct.pack(">H", position) if position is not None else b"")


def ditl_item(kind, rect, body):
    return bytes(4) + struct.pack(">4h", *rect) + bytes([kind, len(body)]) + body + \
        bytes(len(body) & 1)


def ditl(*items):
    return struct.pack(">h", len(items) - 1) + b"".join(ditl_item(*i) for i in items)


def menu(menu_id, title, items, mdef=0):
    """items: (text, command key) pairs; text starting with ( is disabled."""
    flags = 1
    body = b""
    for i, (text, key) in enumerate(items, start=1):
        if not text.startswith("("):
            flags |= 1 << i
        body += pstr(text.lstrip("(")) + bytes([0, ord(key) if key else 0, 0, 0])
    return struct.pack(">hhhhhI", menu_id, 0, 0, mdef, 0, flags) + pstr(title) + body + b"\0"


def size_res(preferred, minimum, flags=0):
    return struct.pack(">HII", flags, preferred, minimum)


def vers(short, long):
    return bytes([1, 0, 0x80, 0, 0, 0]) + pstr(short) + pstr(long)


def binhex(name, data, rsrc, ftype=b"APPL", creator=b"TEST", flags=0):
    """A BinHex 4.0 file, with runs packed so the decoder's RLE is used."""
    def crc(b):
        return b + struct.pack(">H", binascii.crc_hqx(b, 0))
    n = name.encode("mac_roman")
    raw = crc(bytes([len(n)]) + n + b"\0" + ftype + creator +
              struct.pack(">HII", flags, len(data), len(rsrc))) + crc(data) + crc(rsrc)
    packed, i = bytearray(), 0
    while i < len(raw):
        c = raw[i]
        run = 1
        while i + run < len(raw) and raw[i + run] == c and run < 255:
            run += 1
        if c == 0x90:
            packed += b"\x90\x00"
            run = 1
        elif run >= 3:
            packed += bytes([c, 0x90, run])
        else:
            packed += bytes([c]) * run
        i += run
    packed += bytes(-len(packed) % 3)
    chars = bytearray()
    for j in range(0, len(packed), 3):
        v = int.from_bytes(packed[j:j + 3], "big")
        chars += bytes(BINHEX_CHARS[v >> s & 63] for s in (18, 12, 6, 0))
    lines = [bytes(chars[j:j + 64]) for j in range(0, len(chars), 64)]
    return (b"(This file must be converted with BinHex 4.0)\r\r:" + b"\r".join(lines) + b":\r")


def mfs_image(volume_name, files, blocks=391, block_size=1024):
    """A 400K MFS floppy, laid out the way the Mac's Initialize left one:
    boot blocks, the MDB and its block map at block 2, the directory at 4,
    the allocation blocks from 16.  files are (name, type, creator, data,
    rsrc, flags, (v, h)), and then the entry's own flags if not $80.
    Blocks are handed out from the end of the disk backwards, so every
    fork longer than a block is a chain the reader has to follow through
    the map."""
    img = bytearray(800 * 512)
    links = {}
    free = list(range(blocks + 1, 1, -1))

    def put(fork):
        if not fork:
            return 0
        chain = [free.pop(0) for _ in range(-(-len(fork) // block_size))]
        for n, nxt in zip(chain, chain[1:] + [1]):
            links[n] = nxt
            at = (16 * 512) + (n - 2) * block_size
            img[at:at + block_size] = fork[:block_size].ljust(block_size, b"\0")
            fork = fork[block_size:]
        return chain[0]

    def physical(fork):
        return -(-len(fork) // block_size) * block_size

    entries = bytearray()
    for num, (name, ftype, creator, data, rsrc, flags, where, *entry) in enumerate(files, 1):
        n = name.encode("mac_roman")
        e = (bytes([entry[0] if entry else 0x80, 0]) + ftype + creator
             + struct.pack(">Hhhh", flags, *where, 0)
             + struct.pack(">IHII", num, put(data), len(data), physical(data))
             + struct.pack(">HII", put(rsrc), len(rsrc), physical(rsrc))
             + struct.pack(">II", 0xA0000000 + num, 0xA0001000 + num) + bytes([len(n)]) + n)
        e += bytes(len(e) & 1)
        if len(entries) // 512 != (len(entries) + len(e) - 1) // 512:
            entries += bytes(-len(entries) % 512)     # entries never cross a block
        entries += e
    img[4 * 512:4 * 512 + len(entries)] = entries
    vn = volume_name.encode("mac_roman")
    mdb = (b"\xd2\xd7"
           + struct.pack(">IIHHHHHIIHIH", 0x9A000000, 0, 0, len(files), 4, 12, blocks,
                         block_size, 8192, 16, len(files) + 1, len(free))
           + bytes([len(vn)]) + vn.ljust(27, b"\0"))
    vabm = bytearray(-(-blocks * 3 // 2))
    for n, nxt in links.items():
        i = n - 2
        at = i * 3 // 2
        if i % 2 == 0:
            vabm[at] = nxt >> 4
            vabm[at + 1] |= (nxt & 0xF) << 4
        else:
            vabm[at] |= nxt >> 8
            vabm[at + 1] = nxt & 0xFF
    img[1024:1024 + 64 + len(vabm)] = mdb + vabm
    return bytes(img)


# The 64 bytes a GCR floppy holds, for 0 to 63.
GCR = bytes.fromhex("96979A9B9D9E9FA6A7ABACADAEAFB2B3B4B5B6B7B9BABBBCBDBEBFCBCDCECFD3"
                    "D6D7D9DADBDCDDDEDFE5E6E7E9EAEBECEDEEEFF2F3F4F5F6F7F9FAFBFCFDFEFF")
SYNC = "1111111100" * 6                     # the drive's self-sync bytes: $FF and two zeros


def gcr_sector(track, side, number, data, tag=bytes(12), marks=b"\xd5\xaa\xad",
               end=b"\xde\xaa"):
    """One sector as the Sony driver writes it, as bits: its header, then
    its tag and data, three bytes to four of the disk's, each mixed with
    one of three running sums.  The data field's marks before and after
    it can be others, as a key disk's are."""
    def disk(values):
        return "".join(format(b, "08b") for b in values)

    fields = (track & 0x3F, number, track >> 6 | side << 5, 0x22 if side else 0x02)
    header = b"\xd5\xaa\x96" + bytes(GCR[v] for v in fields + (fields[0] ^ fields[1]
                                     ^ fields[2] ^ fields[3],)) + b"\xde\xaa"
    src = tag + data
    a = b = c = 0
    six = []
    for g in range(175):
        x, y = src[3 * g], src[3 * g + 1]
        z = src[3 * g + 2] if g < 174 else 0
        c = c << 1 | c >> 7
        carry, c = c & 1, c & 0xFF
        ex = x ^ c
        a += x + carry
        carry, a = a >> 8, a & 0xFF
        ey = y ^ a
        b += y + carry
        carry, b = b >> 8, b & 0xFF
        ez = z ^ b
        if g < 174:
            c = (c + z + carry) & 0xFF
        six += [ex >> 2 & 0x30 | ey >> 4 & 0x0C | ez >> 6, ex & 0x3F, ey & 0x3F, ez & 0x3F]
        if g == 174:
            six.pop()
    six += [a >> 2 & 0x30 | b >> 4 & 0x0C | c >> 6, a & 0x3F, b & 0x3F, c & 0x3F]
    body = marks + bytes([GCR[number]]) + bytes(GCR[v] for v in six) + end
    return SYNC + disk(header) + SYNC[:50] + disk(body) + SYNC


def moof_image(raw, sides=1, kind=None, tracks=None, crc=True, flux=None):
    """An Applesauce MOOF image of a 400K (or 800K, sides=2) floppy holding
    raw's sectors, each track's sectors in reverse and the track begun in
    the middle of one.  tracks maps (track, side) to bits to put in place
    of what's written there, or None to leave the track out; flux maps
    them to flux changes to keep as well, each the 125 ns ticks since the
    last."""
    tmap, fmap, trks, blocks = (bytearray(b"\xff" * 160), bytearray(b"\xff" * 160),
                                bytearray(160 * 8), bytearray())
    n = 0

    def put(where, track, side, data, count):
        nonlocal n
        data += bytes(-len(data) % 512)
        where[track * 2 + side] = n
        struct.pack_into("<HHI", trks, n * 8, 3 + len(blocks) // 512, len(data) // 512, count)
        blocks.extend(data)
        n += 1

    at = 0
    for track in range(80):
        for side in range(sides):
            count = 12 - track // 16
            bits = "".join(gcr_sector(track, side, s, raw[(at + s) * 512:(at + s + 1) * 512])
                           for s in reversed(range(count)))
            at += count
            bits = bits[3000:] + bits[:3000]
            bits = (tracks or {}).get((track, side), bits)
            if bits is not None:
                data = int(bits + "0" * (-len(bits) % 8), 2).to_bytes(-(-len(bits) // 8), "big")
                put(tmap, track, side, data, len(bits))
    largest = 0
    for (track, side), gaps in (flux or {}).items():
        data = b"".join(b"\xff" * (g // 255) + bytes([g % 255]) for g in gaps)
        put(fmap, track, side, data, len(data))
        largest = max(largest, -(-len(data) // 512))
    where = 3 + len(blocks) // 512 if flux else 0          # the FLUX chunk follows the tracks
    info = (bytes([1, kind or sides, 0, 0, 16]) + b"macrt tests".ljust(32) + bytes(3)
            + struct.pack("<HH", where, largest) + bytes(16))

    def chunk(name, body):
        return name + struct.pack("<I", len(body)) + body

    rest = (chunk(b"INFO", info) + chunk(b"TMAP", bytes(tmap))
            + chunk(b"TRKS", bytes(trks) + bytes(blocks)))
    assert len(rest) - len(blocks) + 12 == 3 * 512          # the tracks start at block 3
    if flux:
        rest += chunk(b"FLUX", bytes(fmap))
    return b"MOOF\xff\n\r\n" + struct.pack("<I", binascii.crc32(rest) if crc else 0) + rest


def font_rec(first, last, glyphs, missing, ascent, descent, kern_max=0, leading=0,
             final_ow=True):
    """A FONT or NFNT: a FontRec with the characters first to last.
    glyphs maps a character code to (rows, offset, width): its image as
    strings of # and ., ascent + descent of them, how far right of
    kernMax it's drawn, and how far it moves the pen; codes it hasn't got
    are the missing glyph, missing.  Without final_ow the owTable stops
    at the missing glyph's entry, as many real fonts' do."""
    height = ascent + descent
    order = [glyphs.get(c) for c in range(first, last + 1)] + [missing]
    locs, x, placed = [], 0, []
    for g in order:
        locs.append(x)
        if g:
            placed.append((x, g[0]))
            x += len(g[0][0])
    locs.append(x)
    row_words = max(-(-x // 16), 1)
    strike = bytearray(row_words * 2 * height)
    for left, rows in placed:
        for y, row in enumerate(rows):
            for i, ch in enumerate(row):
                if ch == "#":
                    strike[y * row_words * 2 + (left + i) // 8] |= 0x80 >> ((left + i) % 8)
    ows = [g[1] << 8 | g[2] if g else 0xFFFF for g in order] + [0xFFFF] * final_ow
    n = last - first + 3
    ow_loc = (26 + len(strike) + 2 * n - 16) // 2
    header = struct.pack(">HHHhhhhhHhhhH", 0x9000, first, last, max(g[2] for g in order if g),
                         kern_max, -descent, x, height, ow_loc, ascent, descent, leading,
                         row_words)
    return (header + strike + struct.pack(f">{n}H", *locs)
            + struct.pack(f">{len(ows)}H", *ows))
