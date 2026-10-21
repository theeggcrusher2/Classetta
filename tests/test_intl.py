"""The International Utilities: the US resources, dates, times and string
order, and the Pack6 calls."""

import datetime
import struct

import pytest

from macrt.resfork import Resource
from macrt.toolbox import intl
from macrt.toolbox.intl import abbrevDate, longDate, shortDate

from guest import Guest


def secs(*when) -> int:
    return int((datetime.datetime(*when) - intl.EPOCH).total_seconds())


def signed(v: int) -> int:
    """A long as the dispatcher pops it."""
    return v - (1 << 32) if v & 0x80000000 else v


def patched(record: bytes, **fields) -> bytes:
    data = bytearray(record)
    for name, value in fields.items():
        at = getattr(intl, name.upper())
        value = bytes([value]) if isinstance(value, int) else value
        data[at:at + len(value)] = value
    return bytes(data)


DEC_21 = secs(1983, 12, 21, 13, 5, 9)
US0, US1 = intl.us_intl0(), intl.us_intl1()


def test_the_us_records_are_inside_macintoshs():
    assert len(US0) == intl.INTL0_SIZE and len(US1) == intl.INTL1_SIZE
    assert US0[:4] == b".,;$" and US0[intl.DATE_SEP] == ord("/")
    assert US0[intl.TIME_CYCLE] == intl.timeCycle12
    assert US0[intl.MORN_STR:intl.TIME_SEP + 1] == b" AM\0 PM\0:"
    assert not intl.is_metric(US0) and US0[-2:] == b"\0\1"
    assert US1[:16] == b"\x06Sunday" + bytes(9)
    assert US1[intl.MONTHS_AT + 16 * 11:][:9] == b"\x08December"
    assert US1[intl.ABBR_LEN] == 3 and US1[-2:] == b"\x4e\x75"


def test_seconds_since_1904():
    assert intl.date_time(0) == (1904, 1, 1, 0, 0, 0, 6)            # a Friday
    assert intl.date_time(DEC_21) == (1983, 12, 21, 13, 5, 9, 4)
    assert intl.date_time(0xFFFFFFFF) == (2040, 2, 6, 6, 28, 15, 2)
    assert intl.date_time(-1) == (1903, 12, 31, 23, 59, 59, 5)      # a LongDateTime
    assert intl.date_time(1 << 62).year == 9999


def test_short_dates():
    assert intl.date_string(DEC_21, shortDate, US0) == b"12/21/83"
    feb_3 = secs(2007, 2, 3)
    assert intl.date_string(feb_3, shortDate, US0) == b"2/3/07"
    lead = intl.century | intl.mntLdingZ | intl.dayLdingZ
    german = patched(US0, date_order=1, shrt_date_fmt=lead, date_sep=b".")
    assert intl.date_string(feb_3, shortDate, german) == b"03.02.2007"
    for order, text in enumerate([b"2-3-07", b"3-2-07", b"07-2-3", b"2-07-3", b"3-07-2",
                                  b"07-3-2"]):
        record = patched(US0, date_order=order, date_sep=b"-")
        assert intl.date_string(feb_3, shortDate, record) == text


def test_long_dates():
    assert intl.date_string(DEC_21, longDate, US1) == b"Wednesday, December 21, 1983"
    assert intl.date_string(DEC_21, abbrevDate, US1) == b"Wed, Dec 21, 1983"
    day_first = patched(US1, suppress_day=0xFF, lng_date_fmt=0, day_leading0=0xFF)
    assert intl.date_string(secs(1984, 1, 2), longDate, day_first) == b"02 January, 1984"
    framed = patched(US1, st0=b"<\0\0\0" + US1[intl.ST0 + 4:intl.ST0 + 16] + b">\0\0\0")
    assert intl.date_string(DEC_21, abbrevDate, framed) == b"<Wed, Dec 21, 1983>"


def test_times():
    assert intl.time_string(DEC_21, False, US0) == b"1:05 PM"
    assert intl.time_string(DEC_21, True, US0) == b"1:05:09 PM"
    assert intl.time_string(secs(1990, 5, 1, 0, 5, 9), True, US0) == b"12:05:09 AM"
    assert intl.time_string(secs(1990, 5, 1, 12, 0), False, US0) == b"12:00 PM"
    european = patched(US0, time_cycle=intl.timeCycle24, time_fmt=intl.hrLeadingZ
                       | intl.minLeadingZ, time1_suff=b" h\0\0", time5_suff=b" h\0\0")
    assert intl.time_string(DEC_21, False, european) == b"13:05 h"
    assert intl.time_string(secs(1990, 5, 1, 9, 7, 3), True, european) == b"09:07:3 h"
    zero = patched(US0, time_cycle=intl.timeCycleZero)
    assert intl.time_string(secs(1990, 5, 1, 12, 30), False, zero) == b"0:30 PM"


def test_strings_sort_without_case_or_accents_first():
    assert intl.compare(b"apple", b"BANANA") == -1
    assert intl.compare(b"Apple", b"apple") == -1                  # then upper case first
    assert intl.compare(b"apple", b"Apple") == 1
    assert intl.compare(b"abc", b"abcd") == -1 and intl.compare(b"", b"") == 0
    e_acute = "é".encode("mac_roman")
    assert intl.compare(e_acute, b"e") == 1 and intl.compare(e_acute, b"f") == -1
    assert intl.compare(b"r" + e_acute + b"sum" + e_acute, b"resume") == 1
    ae = "æ".encode("mac_roman")
    assert intl.compare(ae, b"ad") == 1 and intl.compare(ae, b"af") == -1
    assert intl.compare(ae, b"ae") != 0
    assert intl.compare(b"10", b"9") == -1 and intl.compare(b"a b", b"ab") == -1


def test_equal_strings_match_in_case_and_accents():
    assert intl.equal(b"Mac", b"Mac") == 0
    assert intl.equal(b"Mac", b"MAC") == 1
    assert intl.equal(b"e", "é".encode("mac_roman")) == 1


@pytest.fixture
def g(qapp):
    return Guest()


def test_pack6_dates_and_times(g):
    result = g.ptr(bytes(256))
    g("IUDateString", signed(DEC_21), longDate, result)
    assert g.str255(result) == "Wednesday, December 21, 1983"
    g("IUDateString", signed(DEC_21), shortDate, result)
    assert g.str255(result) == "12/21/83"
    g("IUTimeString", signed(DEC_21), 1, result)
    assert g.str255(result) == "1:05:09 PM"
    g("IUDatePString", signed(DEC_21), abbrevDate, result, g.handle(patched(US1, abbr_len=2)))
    assert g.str255(result) == "We, De 21, 1983"
    g("IUTimePString", signed(DEC_21), 0, result, g.handle(patched(US0, eve_str=b"pm\0\0")))
    assert g.str255(result) == "1:05pm"
    long_date_time = g.ptr(struct.pack(">q", secs(2050, 7, 4, 18, 30)))
    g("IULDateString", long_date_time, shortDate, result, 0)
    assert g.str255(result) == "7/4/50"
    g("IULTimeString", long_date_time, 0, result, 0)
    assert g.str255(result) == "6:30 PM"


def test_iugetintl_finds_the_system_files_us_records(g):
    h0, h1 = g("IUGetIntl", 0), g("IUGetIntl", 1)
    assert g.contents(h0) == US0 and g.contents(h1) == US1
    rm = g.process.resources
    assert rm.get("INTL", 0) == h0 and rm.HomeResFile(h0) == rm.sys_refnum
    assert g("IUGetIntl", 2) == 0
    assert not g("IUMetric")


def test_an_applications_own_records_win(qapp):
    metric = patched(US0, metric_sys=0xFF, date_sep=b"-")
    g = Guest([Resource(b"INTL", 1, patched(US1, abbr_len=1)), Resource(b"INTL", 0, metric),
               Resource(b"itl2", 0, b"sorting")])
    assert g.contents(g("IUGetIntl", 0)) == metric and g("IUMetric")
    assert g.contents(g("IUGetIntl", 2)) == b"sorting"
    result = g.ptr(bytes(256))
    g("IUDateString", signed(DEC_21), shortDate, result)
    assert g.str255(result) == "12-21-83"
    g("IUDateString", signed(DEC_21), abbrevDate, result)
    assert g.str255(result) == "W, D 21, 1983"


def test_iusetintl_adds_or_replaces_the_files_record(g):
    rm = g.process.resources
    refnum = rm.CurResFile()
    mine = g.handle(patched(US0, metric_sys=1))
    g("IUSetIntl", refnum, 0, mine)
    assert g("IUGetIntl", 0) == mine and g("IUMetric")
    g("IUSetIntl", refnum, 0, g.handle(US0))
    assert g("IUGetIntl", 0) == mine and g.contents(mine) == US0
    assert rm.CurResFile() == refnum


def test_pack6_string_comparisons(g):
    def text(s: bytes) -> int:
        return g.ptr(s)

    a, b = text(b"apple"), text(b"Apple")
    assert g("IUMagString", a, b, 5, 5) == 1
    assert g("IUMagString", a, b, 1, 0) == 1 and g("IUMagString", a, b, 0, 0) == 0
    assert g("IUMagIDString", a, b, 5, 5) == 1
    assert g("IUMagIDString", a, text(b"apples"), 5, 5) == 0
    assert g("IUMagPString", b, a, 5, 5, 0) == -1
    assert g("IUMagIDPString", a, a, 5, 5, 0) == 0
    g("IUClearCache")
