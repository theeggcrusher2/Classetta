"""Mac files from the host: resource forks, MacBinary, AppleSingle and
AppleDouble, the side files emulators leave, and HFS and MFS disk images."""

import binascii
import bisect
import io
import random
import struct
import time

import machfs
import pytest
import rsrcfork

import builders
from builders import SYNC, binhex, gcr_sector, mfs_image, moof_image
from macrt import moof
from macrt.macfile import (
    HostFolder, MacFile, MemoryFolder, VolumeFolder, is_disk_image, load_file, load_volume,
    open_application, open_applications, open_disks, volume_files,
)
from macrt.resfork import (
    InvalidForkError, Resource, build_fork, is_fork, read_fork, resLocked, resPurgeable,
    restype,
)
from macrt.ui.display import Display, DisplayConfig

FORK = build_fork([
    Resource(b"STR ", 128, b"\x05Hello", b"Greeting", resPurgeable),
    Resource(b"STR ", 129, b"\x03Bye"),
    Resource(b"WIND", 128, bytes(30), None, resLocked),
])


def macbinary(name, data, rsrc, ftype=b"APPL", creator=b"TEST", flags=0x2100):
    h = bytearray(128)
    h[1] = len(name)
    h[2:2 + len(name)] = name.encode("mac_roman")
    h[65:69], h[69:73], h[73] = ftype, creator, flags >> 8
    struct.pack_into(">II", h, 83, len(data), len(rsrc))
    h[101], h[122], h[123] = flags & 0xFF, 129, 129
    struct.pack_into(">H", h, 124, binascii.crc_hqx(bytes(h[:124]), 0))
    pad = lambda b: b + bytes(-len(b) % 128)    # noqa: E731
    return bytes(h) + pad(data) + pad(rsrc)


def apple_single(entries, double=False):
    head = struct.pack(">II16sH", 0x00051607 if double else 0x00051600, 0x00020000, b"",
                       len(entries))
    at = len(head) + 12 * len(entries)
    descs, body = b"", b""
    for eid, blob in entries.items():
        descs += struct.pack(">III", eid, at + len(body), len(blob))
        body += blob
    return head + descs + body


def finder_info(ftype=b"APPL", creator=b"TEST", flags=0x2000):
    return ftype + creator + struct.pack(">H", flags) + bytes(22)


# -- the fork format ----------------------------------------------------------
def test_built_fork_reads_back_through_rsrcfork():
    rf = rsrcfork.ResourceFile(io.BytesIO(FORK))
    assert list(rf) == [b"STR ", b"WIND"]
    res = read_fork(FORK)
    assert [(r.type, r.id, r.name, r.attrs, r.data) for r in res] == [
        (b"STR ", 128, b"Greeting", resPurgeable, b"\x05Hello"),
        (b"STR ", 129, None, 0, b"\x03Bye"),
        (b"WIND", 128, None, resLocked, bytes(30)),
    ]


def test_empty_fork_and_garbage():
    assert read_fork(build_fork([])) == []
    assert is_fork(FORK) and not is_fork(b"hello" * 100)
    with pytest.raises(InvalidForkError):
        read_fork(FORK[:300])


def _map_at(fork):
    return struct.unpack_from(">I", fork, 4)[0]


def _poke16(fork, at, value):
    b = bytearray(fork)
    struct.pack_into(">H", b, at, value)
    return bytes(b)


def test_a_map_whose_lists_go_astray_is_refused():
    m = _map_at(FORK)
    wind_refs = m + 28 + 2 + 8 + 6                  # the second type entry's list offset
    for bad in (_poke16(FORK, m + 24, 16),        # type list over the in-memory fields
                _poke16(FORK, m + 24, 0x7000),     # type list outside the map
                _poke16(FORK, wind_refs, 0x4000),  # reference list outside the map
                _poke16(FORK, wind_refs, 10),      # reference list over the type list
                _poke16(FORK, m + 26, 0x8000),     # name list outside the map
                _poke16(FORK, m + 26, 40),         # name list over the references
                _poke16(FORK, 14, 28)):            # map length: just its header
        assert not is_fork(bad)


def test_reference_lists_in_any_order_and_duplicate_ids():
    fork = build_fork([Resource(b"STR ", 128, b"\x05Hello", b"Greeting"),
                       Resource(b"WIND", 128, bytes(30)), Resource(b"WIND", 128, b"twin")])
    m = _map_at(fork)
    entry = m + 28 + 2                              # swap where the two lists are said to be
    first, second = (struct.unpack_from(">H", fork, entry + 8 * i + 6)[0] for i in (0, 1))
    b = bytearray(fork)
    lists = fork[m + 28 + first:m + 28 + second + 2 * 12]
    b[m + 28 + first:m + 28 + second + 24] = lists[12:] + lists[:12]
    struct.pack_into(">H", b, entry + 6, first + 24)
    struct.pack_into(">H", b, entry + 8 + 6, first)
    assert [(r.type, r.name, r.data) for r in read_fork(bytes(b))] == [
        (b"STR ", b"Greeting", b"\x05Hello"), (b"WIND", None, bytes(30)), (b"WIND", None, b"twin")]


def test_data_need_not_start_at_256():
    fork = build_fork([Resource(b"STR ", 1, b"\x02hi")])
    moved = bytearray(fork[:16]) + bytes(0x200 - 16) + fork[256:]
    struct.pack_into(">II", moved, 0, 0x200, _map_at(fork) + 0x100)
    assert [(r.type, r.data) for r in read_fork(bytes(moved))] == [(b"STR ", b"\x02hi")]


def test_a_fork_too_big_for_its_offsets_isnt_built():
    with pytest.raises(ValueError, match="16 MB"):
        build_fork([Resource(b"BIG ", i, bytes(0x800000)) for i in range(3)])
    with pytest.raises(ValueError, match="too many resources"):
        build_fork([Resource(b"STR ", i, b"") for i in range(6000)])
    with pytest.raises(ValueError, match="names"):
        build_fork([Resource(b"STR ", i, b"", b"n" * 255) for i in range(260)])
    long = read_fork(build_fork([Resource(b"STR ", 1, b"", b"n" * 300)]))
    assert long[0].name == b"n" * 255


def test_restype_forms():
    assert restype("WIND") == restype(b"WIND") == restype(0x57494E44) == b"WIND"
    with pytest.raises(ValueError):
        restype("WIN")


# -- single files ------------------------------------------------------------
def test_macbinary(tmp_path):
    p = tmp_path / "app.bin"
    p.write_bytes(macbinary("Mac App", b"some data", FORK))
    f = load_file(p)
    assert (f.name, f.data, f.rsrc, f.type, f.creator, f.flags) == \
        ("Mac App", b"some data", FORK, b"APPL", b"TEST", 0x2100)
    assert f.is_application


def test_macbinary_edge_cases(tmp_path):
    whole = macbinary("App", b"X", FORK)
    unpadded = whole[:128] + b"X" + FORK                # some writers don't pad the data fork
    assert load_file(_write(tmp_path / "a.bin", unpadded)).rsrc == FORK
    with pytest.raises(ValueError, match="cut short"):
        load_file(_write(tmp_path / "b.bin", whole[:300]))
    # A data file that happens to start like MacBinary I keeps its data.
    lookalike = b"\x00\x0c" + bytes(4000)
    assert load_file(_write(tmp_path / "c.dat", lookalike)).data == lookalike


def _write(path, data):
    path.write_bytes(data)
    return path


def test_binhex(tmp_path):
    data = b"\x90\x90 a run: " + b"x" * 40 + bytes(10)      # the RLE marker, then runs
    p = tmp_path / "app.hqx"
    p.write_bytes(b"From: a mailing list\r\n\r\n" + binhex("Hex App", data, FORK, flags=0x2100))
    f = load_file(p)
    assert (f.name, f.data, f.rsrc, f.type, f.creator, f.flags) == \
        ("Hex App", data, FORK, b"APPL", b"TEST", 0x2100)


def test_a_big_binhex_file_decodes_quickly(tmp_path):
    data = random.Random(1).randbytes(400_000)
    p = _write(tmp_path / "big.hqx", binhex("Big", data, b""))
    start = time.perf_counter()
    assert load_file(p).data == data
    assert time.perf_counter() - start < 5          # it took 45 s when decoding was quadratic


def test_damaged_binhex(tmp_path):
    hqx = bytearray(binhex("Hex App", b"data", FORK))
    at = hqx.index(b":") + 40
    hqx[at] = ord("!") if hqx[at] != ord("!") else ord("#")
    p = tmp_path / "app.hqx"
    p.write_bytes(hqx)
    with pytest.raises(ValueError, match="BinHex"):
        load_file(p)


def test_apple_single(tmp_path):
    p = tmp_path / "app.as"
    p.write_bytes(apple_single({1: b"data!", 2: FORK, 3: b"Real Name", 9: finder_info()}))
    f = load_file(p)
    assert (f.name, f.data, f.rsrc, f.type, f.creator) == \
        ("Real Name", b"data!", FORK, b"APPL", b"TEST")


@pytest.mark.parametrize("side", ["._App", ".AppleDouble/App"])
def test_apple_double(tmp_path, side):
    (tmp_path / "App").write_bytes(b"the data fork")
    (tmp_path / side).parent.mkdir(exist_ok=True)
    (tmp_path / side).write_bytes(apple_single({2: FORK, 9: finder_info()}, double=True))
    f = load_file(tmp_path / "App")
    assert (f.data, f.rsrc, f.type, f.creator) == (b"the data fork", FORK, b"APPL", b"TEST")


def test_apple_double_odd_cases(tmp_path):
    (tmp_path / "App").write_bytes(b"data fork")
    (tmp_path / "._App").write_bytes(apple_single({2: FORK}, double=True)[:40])   # cut short
    f = load_file(tmp_path / "App")
    assert (f.data, f.rsrc) == (b"data fork", b"")
    assert [g.name for g in HostFolder(tmp_path).files()] == ["App"]
    header = _write(tmp_path / "Header.ad", apple_single({2: FORK}, double=True))
    assert (load_file(header).data, load_file(header).rsrc) == (b"", FORK)
    (tmp_path / "%Ex").write_bytes(apple_single({2: FORK, 9: finder_info()}, double=True))
    g = HostFolder(tmp_path).find("Ex")
    assert (g.name, g.rsrc, g.type) == ("Ex", FORK, b"APPL")


def test_basilisk_and_executor_side_files(tmp_path):
    (tmp_path / ".rsrc").mkdir()
    (tmp_path / ".finf").mkdir()
    (tmp_path / "B2 App").write_bytes(b"")
    (tmp_path / ".rsrc" / "B2 App").write_bytes(FORK)
    (tmp_path / ".finf" / "B2 App").write_bytes(finder_info(b"APPL", b"BII!"))
    f = load_file(tmp_path / "B2 App")
    assert (f.rsrc, f.creator) == (FORK, b"BII!")

    (tmp_path / "%Ex App").write_bytes(FORK)
    g = HostFolder(tmp_path).find("ex app")
    assert (g.name, g.rsrc) == ("Ex App", FORK)


def test_unar_side_file(tmp_path):
    # The Unarchiver writes the resource fork as AppleDouble in App.rsrc.
    (tmp_path / "App").write_bytes(b"data fork")
    (tmp_path / "App.rsrc").write_bytes(apple_single({2: FORK, 9: finder_info()}, double=True))
    f = load_file(tmp_path / "App")
    assert (f.data, f.rsrc, f.type) == (b"data fork", FORK, b"APPL")
    assert [g.name for g in HostFolder(tmp_path).files()] == ["App"]
    (tmp_path / "App").unlink()
    g = HostFolder(tmp_path).find("app")
    assert (g.name, g.data, g.rsrc, g.creator) == ("App", b"", FORK, b"TEST")
    # With an empty data fork there's only App.rsrc, and App still names it.
    for h in (load_file(tmp_path / "App"), open_application(tmp_path / "App")):
        assert (h.name, h.data, h.rsrc, h.creator) == ("App", b"", FORK, b"TEST")
    with pytest.raises(FileNotFoundError):
        load_file(tmp_path / "Other")


def test_bare_resource_fork(tmp_path):
    (tmp_path / "Prefs.rsrc").write_bytes(FORK)
    f = HostFolder(tmp_path).find("Prefs")
    assert (f.name, f.data, f.rsrc) == ("Prefs", b"", FORK)


def test_host_folder_finds_names_case_insensitively(tmp_path):
    (tmp_path / "Sub").mkdir()
    (tmp_path / "Sub" / "data.bin").write_bytes(macbinary("Data File", b"x", FORK, b"TEXT"))
    folder = HostFolder(tmp_path)
    assert folder.find(":sub:DATA") is not None
    assert folder.find(":Sub:data.bin").type == b"TEXT"
    assert folder.find("missing") is None
    assert folder.find("Disk:") is None and folder.find(":") is None
    assert [f.name for f in HostFolder(tmp_path / "Sub").files()] == ["Data File"]


def test_memory_folder():
    folder = MemoryFolder([MacFile("Alpha", rsrc=FORK)])
    assert folder.find("ALPHA").rsrc == FORK
    assert folder.find("Disk:Folder:alpha").name == "Alpha"
    assert folder.find("Beta") is None
    assert folder.find("Disk:") is None


# -- disk images ---------------------------------------------------------------
def hfs_image():
    v = machfs.Volume()
    v.name = "Test Disk"
    app = machfs.File()
    app.type, app.creator, app.rsrc, app.data = b"APPL", b"TEST", bytearray(FORK), bytearray(b"d")
    prefs = machfs.File()
    prefs.type, prefs.creator, prefs.rsrc = b"pref", b"TEST", bytearray(FORK)
    games = machfs.Folder()
    games["Test App"] = app
    games["Test Prefs"] = prefs
    v["Games"] = games
    return v.write(800 * 1024)


def test_hfs_volume():
    img = hfs_image()
    assert is_disk_image(img) and not is_disk_image(FORK)
    vol = load_volume(img)
    files = {f.key: f for f in volume_files(vol)}
    app = files["Test Disk:Games:Test App"]
    assert (app.rsrc, app.data, app.type, app.creator) == (FORK, b"d", b"APPL", b"TEST")
    # Its folder finds its neighbours, relatively or by full path.
    assert app.folder.find("test prefs").type == b"pref"
    assert app.folder.find("Test Disk:Games:Test App").rsrc == FORK
    assert VolumeFolder(vol).find(":Games:Test Prefs") is not None
    assert app.folder.find("nothing") is None
    # Found in any case, a file has the volume's spelling, so it's one file to OpenResFile.
    shouted = VolumeFolder(vol).find("TEST DISK:GAMES:TEST APP")
    assert (shouted.name, shouted.key) == ("Test App", app.key)


def test_hfs_catalog_flags():
    # What machfs drops: Microsoft File's master disk sets bit 6 on two files.
    img = bytearray(hfs_image())
    for name, flags in ((b"Test App", 0x40), (b"Test Prefs", 0x01)):
        rec = img.find(bytes([len(name)]) + name) - 6            # the catalog key's start
        value = rec + ((img[rec] + 2) & ~1)
        assert img[value] == 2                                   # a file record
        img[value + 2] = flags                                   # its filFlags
    games = load_volume(bytes(img))["Games"]
    assert games["Test App"].attributes == 0x40 and not games["Test App"].locked
    assert games["Test Prefs"].locked and games["Test Prefs"].attributes == 0


def test_diskcopy_image():
    img = hfs_image()
    header = bytearray(84)
    header[0] = 9
    header[1:10] = b"Test Disk"
    struct.pack_into(">I", header, 64, len(img))
    header[82:84] = b"\x01\x00"
    dc = bytes(header) + img
    assert is_disk_image(dc)
    vol = load_volume(dc)
    assert [f.name for f in volume_files(vol)] == ["Test App", "Test Prefs"]
    assert vol.disk.sectors == img and vol.disk.tags == b""
    # With its tags after the sectors, for the disk driver.
    tags = bytes(range(256)) * (len(img) // 512 * 12 // 256 + 1)
    tags = tags[:len(img) // 512 * 12]
    struct.pack_into(">I", header, 68, len(tags))
    vol = load_volume(bytes(header) + img + tags)
    assert vol.disk.sectors == img and vol.disk.tags == tags and vol.disk.tracks is None


def mfs_disk():
    """A 400K floppy as MacPaint shipped on: the app, the System and a document."""
    big = bytes(random.Random(7).randrange(256) for _ in range(5000))
    return big, mfs_image("MacPaint", [
        ("System", b"ZSYS", b"MACS", b"", FORK, 0, (0, 0)),
        ("MacPaint", b"APPL", b"MPNT", b"", big, 0x2100, (40, 60)),
        ("Read Me", b"TEXT", b"MACA", b"Paint!" * 300, b"", 0, (5, 6)),
    ])


def test_mfs_volume():
    big, img = mfs_disk()
    assert is_disk_image(img)
    vol = load_volume(img)
    assert vol.name == "MacPaint" and vol.crdate == 0x9A000000
    files = {f.key: f for f in volume_files(vol)}
    assert list(files) == ["MacPaint:System", "MacPaint:MacPaint", "MacPaint:Read Me"]
    paint = files["MacPaint:MacPaint"]
    # Five blocks, in a chain that runs down the disk.
    assert (paint.rsrc, paint.data, paint.type, paint.creator) == (big, b"", b"APPL", b"MPNT")
    assert paint.flags == 0x2100 and (vol["MacPaint"].y, vol["MacPaint"].x) == (40, 60)
    assert files["MacPaint:Read Me"].data == b"Paint!" * 300
    assert paint.folder.find("read me").type == b"TEXT"
    assert vol["Read Me"].crdate == 0xA0000003 and vol["Read Me"].mddate == 0xA0001003


def test_mfs_in_diskcopy_and_from_the_command_line(tmp_path):
    big, img = mfs_disk()
    header = bytearray(84)
    header[0:9] = b"\x08MacPaint"
    struct.pack_into(">I", header, 64, len(img))
    header[82:84] = b"\x01\x00"
    path = _write(tmp_path / "MacPaint.image", bytes(header) + img)
    app = open_application(path)
    assert (app.key, app.rsrc) == ("MacPaint:MacPaint", big)
    assert open_application(path, "MacPaint:Read Me").type == b"TEXT"


def test_moof_image_of_a_floppy(tmp_path):
    """Applesauce's images of a disk's tracks, bit by bit, as the
    moof-a-day collection keeps its floppies."""
    big, img = mfs_disk()
    data = moof_image(img)
    assert moof.read(data) == img
    path = _write(tmp_path / "MacPaint.moof", data)
    app = open_application(path)
    assert (app.key, app.rsrc) == ("MacPaint:MacPaint", big)


def test_moof_image_of_an_800k_floppy():
    raw = random.Random(3).randbytes(1600 * 512)
    assert moof.read(moof_image(raw, sides=2)) == raw


def test_a_moof_sector_that_cant_be_read_is_zeros():
    """Copy protection puts sectors on a track that a copier can't read, or
    that say they're of another track: they're left as zeros, and the
    rest of the disk read."""
    raw = random.Random(5).randbytes(800 * 512)
    other = gcr_sector(9, 0, 1, bytes(range(256)) * 2)          # on track 0, says it's on 9
    broken = gcr_sector(0, 0, 2, raw[1024:1536])
    at = len(broken) - len(SYNC) - 200 * 8
    broken = broken[:at] + "10101010" + broken[at + 8:]         # $AA: a byte not GCR
    track0 = "".join(gcr_sector(0, 0, s, raw[s * 512:(s + 1) * 512])
                     for s in range(12) if s not in (1, 2)) + other + broken
    got = moof.read(moof_image(raw, tracks={(0, 0): track0}))
    assert got[:512] == raw[:512] and got[512:1536] == bytes(1024)
    assert got[1536:] == raw[1536:]


def test_a_moof_disk_says_why_a_sector_cant_be_read_and_keeps_the_tags():
    """The disk driver tells a program reading a sector why it can't, and
    gives it the tag the File Manager wrote with it."""
    raw = random.Random(6).randbytes(800 * 512)
    header_only = gcr_sector(0, 0, 3, raw[1536:2048])[:len(SYNC) + 80] + SYNC * 10
    broken = gcr_sector(0, 0, 2, raw[1024:1536])
    at = len(broken) - len(SYNC) - 200 * 8
    broken = broken[:at] + "10101010" + broken[at + 8:]
    track0 = "".join(gcr_sector(0, 0, s, raw[s * 512:(s + 1) * 512], b"tag %8d" % s)
                     for s in range(12) if s not in (1, 2, 3)) + broken + header_only
    disk = moof.Disk(moof_image(raw, tracks={(0, 0): track0}))
    assert disk.unreadable == {1: moof.NO_ADDRESS, 2: moof.BAD_DATA, 3: moof.NO_DATA}
    assert disk.tags[:12] == b"tag        0" and disk.tags[12:48] == bytes(36)
    assert disk.tags[11 * 12:12 * 12] == b"tag       11" and disk.tags[12 * 12:] == bytes(788 * 12)


def test_where_a_sector_is_on_a_moof_disk_and_what_follows_its_address_field():
    raw = random.Random(8).randbytes(1600 * 512)
    one, two = moof.Disk(moof_image(raw[:800 * 512])), moof.Disk(moof_image(raw, sides=2))
    assert [one.place(n) for n in (0, 11, 12, 191, 192, 799)] == [
        (0, 0, 0), (0, 0, 11), (1, 0, 0), (15, 0, 11), (16, 0, 0), (79, 0, 7)]
    assert [two.place(n) for n in (0, 12, 24, 1599)] == [
        (0, 0, 0), (0, 1, 0), (1, 0, 0), (79, 1, 7)]
    with pytest.raises(IndexError):
        one.place(800)
    # Where sector 13's address field ends, on its track's second side:
    # some self-sync bytes after it, then the data field, its marks and
    # the sector's number.
    out, starts, count = two.framed(0, 1)
    end = two.address_end(13)
    at = bisect.bisect_left(starts, end)
    assert end < count and out[at - 10:at - 7] == b"\xd5\xaa\x96" and out[at - 2:at] == b"\xde\xaa"
    assert out[at:at + 9] == b"\xff" * 5 + b"\xd5\xaa\xad" + bytes([builders.GCR[1]])
    gone = moof.Disk(moof_image(raw[:800 * 512], tracks={(0, 0): SYNC * 100}))
    assert gone.address_end(5) is None and gone.unreadable[5] == moof.NO_ADDRESS


def test_where_a_track_goes_quiet_the_drive_reads_noise():
    """Q-Sheet's key disk has a run with no flux change after a sector:
    what's read there differs each time, and the sectors still read."""
    raw = random.Random(9).randbytes(800 * 512)
    track0 = "".join(gcr_sector(0, 0, s, raw[s * 512:(s + 1) * 512]) for s in range(12))
    disk = moof.Disk(moof_image(raw, tracks={(0, 0): track0 + "0" * 400}))
    disk.random.seed(1)
    assert disk.image == raw and disk.unreadable == {}
    one, two = disk.bits(0, 0), disk.bits(0, 0)
    assert one != two and one[:-400] == two[:-400]
    end = disk.address_end(11)
    for bits in (one, two):                         # the data field before the noise
        after = moof._bytes(bits[end:])
        assert moof.data_field(after[9:9 + moof.NIBBLES]) == bytes(12) + raw[11 * 512:12 * 512]
    assert disk.bits(1, 0) == disk.bits(1, 0)       # a track without


def test_seven_zeros_together_read_as_written_and_eight_as_noise():
    """The Ancient Art of War's key track has bytes four zeros apart, and
    counts them as a turn goes by: the drive reads such a run as it was
    written, the same each time, up to seven zeros, as long as the head
    sees at once.  Past that it reads noise."""
    raw = random.Random(11).randbytes(800 * 512)
    seven = moof.Disk(moof_image(raw, tracks={(79, 0): ("11110000" + "11111111") * 3000
                                              + ("1" + "0" * 7 + "11111111") * 3000}))
    seven.random.seed(1)
    one = seven.framed(79, 0)
    assert seven.framed(79, 0) == one and set(one[0]) == {0x80, 0xF0, 0xFF}
    eight = moof.Disk(moof_image(raw, tracks={(79, 0): ("1" + "0" * 8 + "11111111") * 3000}))
    eight.random.seed(1)
    assert eight.framed(79, 0)[0] != eight.framed(79, 0)[0]


def test_a_track_the_image_hasnt_got_reads_as_noise():
    """The Print Shop's disk's last track was never written: its sectors
    can't be read, and what the drive reads there is noise, as many bits
    round as a track in its zone has, different each time."""
    raw = random.Random(10).randbytes(800 * 512)
    disk = moof.Disk(moof_image(raw, tracks={(79, 0): None}))
    assert disk.image[:792 * 512] == raw[:792 * 512] and disk.image[792 * 512:] == bytes(4096)
    assert disk.unreadable == {n: moof.NO_ADDRESS for n in range(792, 800)}
    disk.random.seed(1)
    one, starts, count = disk.framed(79, 0)
    assert count == 49790 and len(one) > 1000 and len(starts) == len(one)
    assert disk.framed(79, 0)[0] != one
    assert disk.framed(0, 1)[2] == 74558    # the side a 400K disk hasn't got, too


def _flux(bits, ticks=16):
    """The flux changes a track's bits are, each the ticks since the last,
    ticks to a bit cell."""
    ones = [i for i, b in enumerate(bits) if b == "1"]
    return [(b - a) * ticks for a, b in zip([ones[-1] - len(bits)] + ones, ones)]


def test_a_moof_track_kept_as_flux_changes():
    """Applesauce keeps some tracks as the flux changes it timed going by,
    not as bits: they read as far apart as a Mac's drive turning at its
    speed would have them, whatever speed Applesauce's turned at, and
    where there's been none for too long the drive reads noise."""
    raw = random.Random(12).randbytes(800 * 512)
    sectors = "".join(gcr_sector(0, 0, s, raw[s * 512:(s + 1) * 512]) for s in range(12))
    track0 = sectors + "0" * (74558 - len(sectors) - 1) + "1"      # a turn, quiet after them
    disk = moof.Disk(moof_image(raw, tracks={(0, 0): None}, flux={(0, 0): _flux(track0, 17)}))
    assert disk.image == raw and disk.unreadable == {}
    disk.random.seed(1)
    one, two = disk.bits(0, 0), disk.bits(0, 0)
    assert len(one) == 74558 and one != two and one[:len(sectors)] == two[:len(sectors)] == sectors
    assert disk.framed(0, 0)[0] != disk.framed(0, 0)[0]


def test_flux_changes_a_cell_and_a_half_apart_read_differently_each_time():
    """Fraction Action's key track has flux changes a bit cell and a half
    apart: the IWM reads each as one cell after the last or two, as it's
    read a little early or late, so the track reads differently each
    time where a copy of its bits reads the same.  The changes are what's
    there, if the image has both them and the bits."""
    raw = random.Random(13).randbytes(800 * 512)
    gaps = [32] * 24145 + [24] * 1000                   # two cells, then a cell and a half
    disk = moof.Disk(moof_image(raw, flux={(79, 0): gaps}))
    assert disk.image[:792 * 512] == raw[:792 * 512]
    assert disk.unreadable == {n: moof.NO_ADDRESS for n in range(792, 800)}
    disk.random.seed(1)
    one, two = disk.bits(79, 0), disk.bits(79, 0)
    assert one[:48290] == two[:48290] == "01" * 24145 and one != two
    for rest in (one[48290:], two[48290:]):
        assert rest.count("1") == 1000 and "00" not in rest and "11" in rest and "01" in rest
    assert disk.framed(79, 0)[0] != disk.framed(79, 0)[0]
    assert disk.bits(78, 0) == disk.bits(78, 0)         # a track kept as bits
    # Without INFO saying where they are, the changes aren't read.
    data = bytearray(moof_image(raw, flux={(79, 0): gaps}, crc=False))
    data[60:64] = bytes(4)                              # INFO's FLUX block and its largest track
    assert moof.Disk(bytes(data)).image == raw


def test_a_moof_image_macrt_cant_read_says_why(tmp_path):
    _, img = mfs_disk()
    hd = _write(tmp_path / "hd.moof", moof_image(img, kind=3))
    with pytest.raises(ValueError, match="1.4 MB"):
        open_application(hd)
    damaged = bytearray(moof_image(img))
    damaged[5000] ^= 1
    with pytest.raises(ValueError, match="damaged"):
        open_application(_write(tmp_path / "damaged.moof", bytes(damaged)))


def test_a_disk_that_starts_up_into_its_program(tmp_path):
    """Some early games took the Finder's place, so their disk started up
    straight into them: the startup program its boot blocks name runs,
    unless it's Apple's Finder, before any application on the disk."""
    game = build_fork([Resource(b"CODE", 0, bytes(16)), Resource(b"MENU", 100, bytes(14))])

    def disk(startup, rsrc, *more):
        img = bytearray(mfs_image("The Quest", [
            ("System", b"ZSYS", b"MACS", b"", FORK, 0, (0, 0)),
            ("Finder", b"FNDR", b"MACS", b"", rsrc, 0, (0, 0)), *more,
        ]))
        img[0:2] = b"LK"
        img[90:91 + len(startup)] = bytes([len(startup)]) + startup
        return _write(tmp_path / "quest.img", bytes(img))

    assert open_application(disk(b"Finder", game)).key == "The Quest:Finder"
    # Mac Attack's disk has an application for starting it on a Lisa.
    lisa = ("Lisa Boot", b"APPL", b"\0" * 4, b"", game, 0, (0, 0))
    assert open_application(disk(b"Finder", game, lisa)).key == "The Quest:Finder"
    assert open_application(disk(b"Finder", FORK, lisa)).key == "The Quest:Lisa Boot"
    for signature in (b"FNDR", b"MACS"):        # System 1 to 5's Finder, then 6 and 7's
        finder = build_fork([Resource(b"CODE", 0, bytes(16)), Resource(signature, 0, b"")])
        with pytest.raises(ValueError, match="0 applications"):
            open_application(disk(b"Finder", finder))
    with pytest.raises(ValueError, match="0 applications"):
        open_application(disk(b"MiniFinder", game))     # not on the disk
    with pytest.raises(ValueError, match="0 applications"):
        open_application(disk(b"Finder", FORK))         # no code


def test_an_image_with_the_sector_tags(tmp_path):
    """Some imagers keep each sector's 12-byte tag with it, before or after."""
    big, img = mfs_disk()
    sectors = [img[i:i + 512] for i in range(0, len(img), 512)]
    for tagged in (b"".join(s + bytes(range(12)) for s in sectors),
                   b"".join(bytes(range(12)) + s for s in sectors)):
        assert is_disk_image(tagged) and len(tagged) == len(sectors) * 524
        path = _write(tmp_path / "MacPaint.dsk", tagged)
        assert open_application(path).rsrc == big
        disk = load_volume(tagged).disk
        assert disk.sectors == img and disk.tags == bytes(range(12)) * len(sectors)


def test_a_damaged_mfs_disk():
    img = bytearray(mfs_disk()[1])
    img[1024 + 20:1024 + 24] = bytes(4)                 # drAlBlkSiz
    with pytest.raises(ValueError, match="damaged"):
        load_volume(bytes(img))
    img = bytearray(mfs_disk()[1])
    img[1024 + 64:1024 + 64 + 600] = bytes(600)         # the block map, gone
    with pytest.raises(ValueError, match="damaged .*runs off"):
        load_volume(bytes(img))


def partitioned(volume, start):
    """A hard disk image: driver descriptor, partition map, a driver, the volume."""
    def entry(first, blocks, kind):
        e = bytearray(512)
        e[0:2] = b"PM"
        struct.pack_into(">III", e, 4, 3, first, blocks)
        e[48:48 + len(kind)] = kind
        return bytes(e)
    blocks = len(volume) // 512
    ddm = b"ER" + struct.pack(">HI", 512, start + blocks)
    head = (ddm.ljust(512, b"\0") + entry(1, 63, b"Apple_partition_map")
            + entry(32, start - 32, b"Apple_Driver43") + entry(start, blocks, b"Apple_HFS"))
    return head.ljust(512 * start, b"\0") + volume


def test_a_partitioned_hard_disk_image(tmp_path):
    img = _write(tmp_path / "hd.img", partitioned(hfs_image(), start=96))
    assert is_disk_image(img.read_bytes())
    assert open_application(img).key == "Test Disk:Games:Test App"


def test_a_damaged_disk_image(tmp_path):
    junk = bytearray(64 * 1024)
    junk[1024:1026] = b"BD"
    with pytest.raises(ValueError, match="junk.dsk: the disk image is damaged"):
        open_application(_write(tmp_path / "junk.dsk", bytes(junk)))
    # A Mac file that only looks like an image is still read as one.
    lucky = bytearray(macbinary("Lucky", bytes(2000), FORK))
    lucky[1024:1026] = b"BD"
    assert open_application(_write(tmp_path / "lucky.bin", bytes(lucky))).rsrc == FORK


# -- what the command line opens ---------------------------------------------
def test_open_application_on_a_disk_image(tmp_path):
    img = tmp_path / "disk.dsk"
    img.write_bytes(hfs_image())
    assert open_application(img).key == "Test Disk:Games:Test App"
    assert open_application(img, "test prefs").type == b"pref"
    assert open_application(img, "Test Disk:Games:Test App").name == "Test App"
    assert open_application(img, "Games:Test App").name == "Test App"
    with pytest.raises(ValueError, match="nothing called 'Nope'"):
        open_application(img, "Nope")


def test_a_disk_image_with_several_applications(tmp_path):
    v = machfs.Volume()
    v.name = "Two Apps"
    v["A Folder"] = machfs.Folder()
    for where, name in ((v["A Folder"], "Two"), (v, "One"), (v, "Two")):
        f = machfs.File()
        f.type, f.rsrc = b"APPL", bytearray(FORK)
        where[name] = f
    img = tmp_path / "two.img"
    img.write_bytes(v.write(800 * 1024))
    with pytest.raises(ValueError, match="3 applications") as e:
        open_application(img)
    assert "Two Apps:One" in str(e.value) and "--app" in str(e.value)
    # Of two with the same name, the one nearer the top of the disk.
    assert open_application(img, "two").key == "Two Apps:Two"
    assert open_application(img, "A Folder:Two").key == "Two Apps:A Folder:Two"


def test_several_applications_at_once(tmp_path):
    v = machfs.Volume()
    v.name = "Two Apps"
    for name in ("One", "Two"):
        f = machfs.File()
        f.type, f.rsrc = b"APPL", bytearray(FORK)
        v[name] = f
    two = _write(tmp_path / "two.img", v.write(800 * 1024))
    lone = _write(tmp_path / "lone.bin", macbinary("Lone", b"", FORK))
    apps = open_applications([lone, two], ["two", "one"])
    assert [f.name for f in apps] == ["Lone", "Two", "One"]
    assert apps[1].key == "Two Apps:Two"
    with pytest.raises(ValueError, match="2 applications"):
        open_applications([lone, two])
    with pytest.raises(ValueError, match="nothing called 'Three' on two.img"):
        open_applications([two], ["Three"])
    with pytest.raises(ValueError, match="no.* disk image|none here"):
        open_applications([lone], ["Lone"])


def test_a_disk_of_documents_goes_in_a_drive(tmp_path, qapp):
    """Beyond Dark Castle's second disk has only its data on it, and the
    game asks for it by name."""
    data = machfs.Volume()
    data.name = "Disk 2"
    f = machfs.File()
    f.type, f.creator, f.data = b"dc2d", b"dc2j", b"level two"
    data["Data B"] = f
    two = _write(tmp_path / "two.img", data.write(400 * 1024))
    lone = _write(tmp_path / "lone.bin", macbinary("Lone", b"", FORK))
    apps, disks = open_disks([lone, two])
    assert [f.name for f in apps] == ["Lone"] and [d.name for d in disks] == ["Disk 2"]
    with pytest.raises(ValueError, match="0 applications"):
        open_disks([two])
    # With names, an image none of them is on is a disk of documents.
    v = machfs.Volume()
    v.name = "Apps"
    for name in ("One", "Two"):
        f = machfs.File()
        f.type, f.rsrc = b"APPL", bytearray(FORK)
        v[name] = f
    one = _write(tmp_path / "one.img", v.write(800 * 1024))
    lone_img = machfs.Volume()
    lone_img.name = "Other"
    lone_img["Three"] = f
    other = _write(tmp_path / "other.img", lone_img.write(400 * 1024))
    apps, disks = open_disks([one, other, two], ["Two"])
    assert [f.key for f in apps] == ["Apps:Two"]
    assert [d.name for d in disks] == ["Apps", "Other", "Disk 2"]
    # In the drives, the first disk first; the volume the application is
    # on is the one in the drive.
    d = Display(DisplayConfig(host="screen", screen_size=(512, 342), disks=disks))
    try:
        fs = d.system.files
        floppies = [v for v in fs.volumes if v.floppy]
        assert [(v.name, v.drive) for v in floppies[:2]] == [("Apps", 1), ("Other", 2)]
        assert fs.node_for(apps[0]).volume is floppies[0]
        assert fs.volume_named("Disk 2").root.child("Data B") is not None
    finally:
        d.shutdown(quit_app=False)


def test_archives_are_refused(tmp_path):
    sit = b"SIT!" + bytes(6) + b"rLau" + bytes(100)
    (tmp_path / "Game.sit").write_bytes(sit)
    with pytest.raises(ValueError, match="StuffIt archive: unpack it first"):
        open_application(tmp_path / "Game.sit")
    (tmp_path / "Game.sit.hqx").write_bytes(binhex("Game.sit", sit, b"", b"SIT!", b"SIT!"))
    with pytest.raises(ValueError, match="StuffIt"):
        open_application(tmp_path / "Game.sit.hqx")
    (tmp_path / "game.zip").write_bytes(b"PK\x03\x04" + bytes(100))
    with pytest.raises(ValueError, match="zip archive"):
        open_application(tmp_path / "game.zip")


def test_open_application_reads_single_files(tmp_path):
    p = tmp_path / "app.bin"
    p.write_bytes(macbinary("Mac App", b"", FORK))
    assert open_application(p, "ignored for files").rsrc == FORK
