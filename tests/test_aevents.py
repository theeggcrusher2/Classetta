"""The Apple Event Manager's descriptor data and its own coercions."""

import struct

from macrt.m68k.glue_aevents import Composite, coerce


class Glue:
    """Enough of a Runner for coercions the Apple Event Manager does
    itself: there are no handlers to call."""
    class Process:
        pass

    def __init__(self):
        self.process = self.Process()


def test_a_composites_data_survives_a_round_trip():
    c = Composite(b"aevt")
    c.put(b"evcl", b"type", b"aevt", attr=True)
    c.put(b"----", b"TEXT", b"odd")                 # padded to an even length
    c.put(b"rslt", b"long", struct.pack(">l", 7))
    c.put(b"----", b"TEXT", b"replaced")
    back = Composite.parse(c.flatten())
    assert back == c
    assert back.find(b"----") == [b"----", b"TEXT", b"replaced"]
    assert back.find(b"evcl") is None
    assert back.find(b"evcl", attr=True)[2] == b"aevt"
    assert Composite.parse(b"TEXT") is None
    assert Composite.parse(c.flatten()[:-3]) is None        # cut short


def test_numbers_and_text_coerce_to_each_other():
    t = Glue()
    assert coerce(t, b"shor", struct.pack(">h", -2), b"long") == (b"long", struct.pack(">l", -2))
    assert coerce(t, b"TEXT", b" -12 ", b"shor") == (b"shor", struct.pack(">h", -12))
    assert coerce(t, b"long", struct.pack(">l", 99), b"TEXT") == (b"TEXT", b"99")
    assert coerce(t, b"true", b"", b"bool") == (b"bool", b"\x01")
    assert coerce(t, b"long", struct.pack(">l", 1), b"bool") == (b"bool", b"\x01")
    assert coerce(t, b"enum", b"yes ", b"type") == (b"type", b"yes ")
    for dtype, data, to in [(b"long", struct.pack(">l", 70000), b"shor"),     # too big
                            (b"long", struct.pack(">l", 2), b"bool"),
                            (b"TEXT", b"--12", b"long"), (b"TEXT", b"1_2", b"long"),
                            (b"TEXT", b"twelve", b"long"), (b"long", bytes(4), b"type")]:
        assert coerce(t, dtype, data, to) is None, (dtype, data, to)


def test_records_take_any_type_and_items_become_lists():
    t = Glue()
    record = Composite(b"reco", [[b"want", b"type", b"cwin"]]).flatten()
    assert coerce(t, b"reco", record, b"obj ") == (b"obj ", record)
    assert coerce(t, b"obj ", record, b"reco") == (b"reco", record)
    assert coerce(t, b"reco", record, b"list") is None
    assert coerce(t, b"long", b"\0\0\0\5", b"****") == (b"long", b"\0\0\0\5")
    dtype, data = coerce(t, b"long", b"\0\0\0\5", b"list")
    assert dtype == b"list"
    assert Composite.parse(data).items == [[bytes(4), b"long", b"\0\0\0\5"]]
