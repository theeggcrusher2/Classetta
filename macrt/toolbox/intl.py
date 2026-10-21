"""
The International Utilities' formats: the US international resources
(Inside Macintosh I-495 and I-497) for a System file without them, and
dates, times and string order as those resources describe them.  Text is
Mac Roman bytes throughout.
"""

from __future__ import annotations

import datetime
import struct
import unicodedata
from typing import NamedTuple

# Intl0Rec, 'INTL' 0: numbers, short dates and times
DATE_ORDER = 7
SHRT_DATE_FMT = 8
DATE_SEP = 9
TIME_CYCLE = 10
TIME_FMT = 11
MORN_STR = 12
EVE_STR = 16
TIME_SEP = 20
TIME1_SUFF = 21
TIME5_SUFF = 25
METRIC_SYS = 29
INTL0_SIZE = 32

# Intl1Rec, 'INTL' 1: long dates
DAYS_AT = 0
MONTHS_AT = 112
SUPPRESS_DAY = 304
LNG_DATE_FMT = 305
DAY_LEADING0 = 306
ABBR_LEN = 307
ST0 = 308
INTL1_SIZE = 332

century, mntLdingZ, dayLdingZ = 0x80, 0x40, 0x20        # shrtDateFmt
hrLeadingZ, minLeadingZ, secLeadingZ = 0x80, 0x40, 0x20  # timeFmt
timeCycle24, timeCycleZero, timeCycle12 = 0, 1, 255
shortDate, longDate, abbrevDate = 0, 1, 2
# dateOrder: mdy, dmy, ymd, myd, dym, ydm
DATE_ORDERS = ("mdy", "dmy", "ymd", "myd", "dym", "ydm")

DAYS = ("Sunday", "Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday")
MONTHS = ("January", "February", "March", "April", "May", "June", "July", "August",
          "September", "October", "November", "December")
EPOCH = datetime.datetime(1904, 1, 1)
FIRST, LAST = datetime.datetime(1, 1, 1), datetime.datetime(9999, 12, 31, 23, 59, 59)


def _str15(s: str) -> bytes:
    return bytes([len(s)]) + s.encode("mac_roman").ljust(15, b"\0")


def us_intl0() -> bytes:
    """The US Intl0Rec: 1,234.56, $, 12/21/83 and 1:05:09 PM."""
    return (b".,;$\0\0" + bytes([0xD0, 0, 0, ord("/"), timeCycle12, minLeadingZ | secLeadingZ])
            + b" AM\0 PM\0:" + bytes(8) + b"\0" + struct.pack(">H", 1))


def us_intl1() -> bytes:
    """The US Intl1Rec: Wednesday, December 21, 1983."""
    names = b"".join(map(_str15, DAYS + MONTHS))
    return (names + bytes([0, 0xFF, 0, 3]) + b"\0\0\0\0, \0\0 \0\0\0, \0\0\0\0\0\0"
            + struct.pack(">HH", 1, 0x4E75))


DEFAULTS = {0: us_intl0, 1: us_intl1}


class DateTime(NamedTuple):
    year: int
    month: int
    day: int
    hour: int
    minute: int
    second: int
    day_of_week: int        # 1 for Sunday


def date_time(secs: int) -> DateTime:
    """Seconds since 1904, as SecondsToDate (or, for a LongDateTime, its
    64-bit form) gives them."""
    try:
        d = EPOCH + datetime.timedelta(seconds=secs)
    except OverflowError:
        d = FIRST if secs < 0 else LAST
    return DateTime(d.year, d.month, d.day, d.hour, d.minute, d.second, d.isoweekday() % 7 + 1)


def _number(n: int, leading_zero: int) -> bytes:
    return b"%02d" % n if leading_zero else b"%d" % n


def _packed(data: bytes) -> bytes:
    """A PACKED ARRAY[1..4] OF CHAR: up to the first NUL."""
    return data.split(b"\0", 1)[0]


def date_string(secs: int, form: int, intl: bytes) -> bytes:
    """IUDateString's text: intl is an Intl0Rec for shortDate, an Intl1Rec
    for longDate and abbrevDate."""
    dt = date_time(secs)
    if form == shortDate:
        fmt = intl[SHRT_DATE_FMT]
        fields = {"m": _number(dt.month, fmt & mntLdingZ), "d": _number(dt.day, fmt & dayLdingZ),
                  "y": b"%d" % dt.year if fmt & century else b"%02d" % (dt.year % 100)}
        order = DATE_ORDERS[intl[DATE_ORDER]] if intl[DATE_ORDER] < len(DATE_ORDERS) else "mdy"
        return _packed(intl[DATE_SEP:DATE_SEP + 1]).join(fields[c] for c in order)
    # The abbreviation is the first abbrLen bytes of the name's field (I-505)
    abbr = intl[ABBR_LEN] if form != longDate else 0

    def name(at: int) -> bytes:
        return intl[at + 1:at + 1 + (abbr or intl[at])]

    st = [_packed(intl[ST0 + 4 * i:ST0 + 4 * i + 4]) for i in range(5)]
    month, day = name(MONTHS_AT + 16 * (dt.month - 1)), _number(dt.day, intl[DAY_LEADING0])
    out = st[0]
    if not intl[SUPPRESS_DAY]:
        out += name(DAYS_AT + 16 * (dt.day_of_week - 1)) + st[1]
    out += month + st[2] + day if intl[LNG_DATE_FMT] else day + st[2] + month
    return out + st[3] + b"%d" % dt.year + st[4]


def time_string(secs: int, seconds: bool, intl: bytes) -> bytes:
    """IUTimeString's text, from an Intl0Rec."""
    dt = date_time(secs)
    fmt, cycle, sep = intl[TIME_FMT], intl[TIME_CYCLE], intl[TIME_SEP:TIME_SEP + 1]
    hour = dt.hour if cycle == timeCycle24 else dt.hour % 12 or (12 if cycle == timeCycle12 else 0)
    out = _number(hour, fmt & hrLeadingZ) + sep + _number(dt.minute, fmt & minLeadingZ)
    if seconds:
        out += sep + _number(dt.second, fmt & secLeadingZ)
    pm = dt.hour >= 12
    if cycle == timeCycle24:
        # The suffix's first four bytes are for the morning, the last four
        # for the afternoon.
        at = TIME5_SUFF if pm else TIME1_SUFF
    else:
        at = EVE_STR if pm else MORN_STR
    return out + _packed(intl[at:at + 4])


def is_metric(intl0: bytes) -> bool:
    return bool(intl0[METRIC_SYS])


# Sorting: letters compare first without case or accents ("primary"),
# then, if all of that is equal, upper case before lower and plain before
# accented.  Ligatures sort as their letters.
_LIGATURES = {"Æ": "AE", "æ": "ae", "Œ": "OE", "œ": "oe", "ß": "ss", "ﬁ": "fi", "ﬂ": "fl"}
_VARIANTS = {"Ø": "O", "ø": "o", "ı": "i", "“": '"', "”": '"', "«": '"', "»": '"',
             "‘": "'", "’": "'", "\xa0": " "}
_MARKS = "̧́̀̂̈̃̊"   # acute grave circumflex umlaut...
OTHER_MARK, VARIANT, LIGATURE = len(_MARKS) + 1, len(_MARKS) + 2, len(_MARKS) + 3
_LOWER = 0x80


def _sort_keys(code: int) -> tuple[tuple[int, int], ...]:
    """Each letter's (primary, secondary) key; a ligature has two."""
    c = bytes([code]).decode("mac_roman")
    keys = []
    for i, ch in enumerate(_LIGATURES.get(c, c)):
        variant = LIGATURE if c in _LIGATURES and i == 0 else 0
        if ch in _VARIANTS:
            ch, variant = _VARIANTS[ch], VARIANT
        base, *marks = unicodedata.normalize("NFD", ch)
        if marks:
            variant = _MARKS.index(marks[0]) + 1 if marks[0] in _MARKS else OTHER_MARK
        upper = base.upper()
        try:
            primary = upper.encode("mac_roman")
        except UnicodeEncodeError:
            primary = b""
        if len(primary) != 1:
            keys.append((code, 0))
        else:
            keys.append((primary[0], variant | (_LOWER if base != upper else 0)))
    return tuple(keys)


SORT_KEYS = tuple(_sort_keys(code) for code in range(256))


def _keys(text: bytes) -> list[tuple[int, int]]:
    return [k for code in text for k in SORT_KEYS[code]]


def compare(a: bytes, b: bytes) -> int:
    """IUMagString: -1, 0 or 1 as a sorts before, with or after b."""
    ka, kb = _keys(a), _keys(b)
    for x, y in (([p for p, _ in ka], [p for p, _ in kb]), (ka, kb)):
        if x != y:
            return -1 if x < y else 1
    return 0


def equal(a: bytes, b: bytes) -> int:
    """IUMagIDString: 0 if a and b are the same text, case and accents
    included, else 1."""
    return 0 if _keys(a) == _keys(b) else 1
