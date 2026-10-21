"""
SANE, the Standard Apple Numerics Environment, as the Apple Numerics
Manual (second edition) describes it: the arithmetic behind FP68K, the
elementary functions behind Elems68K, and DecStr68K's scanner and
formatter.  macrt.m68k.glue_sane connects them to the packages.

A number is kept exactly, as an integer significand and a binary
exponent, so the basic operations and the conversions between binary
and decimal are rounded once, with integers: correctly, in every
rounding direction and rounding precision.  The elementary functions
work in the host's doubles, which the manual allows them.
"""

from __future__ import annotations

import math
import struct
from typing import NamedTuple

# Exceptions, by their bit numbers in TestException and SetException.  In
# the environment word exception n's flag is bit 8 + n, its halt bit n.
INVALID, UNDERFLOW, OVERFLOW, DIVBYZERO, INEXACT = (1 << n for n in range(5))
# Rounding directions: bits 13-14 of the environment word
TONEAREST, UPWARD, DOWNWARD, TOWARDZERO = range(4)

# NaN codes
NAN_SQRT, NAN_ADD, NAN_DIV, NAN_MUL, NAN_REM = 1, 2, 4, 8, 9
NAN_ASCBIN, NAN_COMP, NAN_ZERO = 17, 20, 21
NAN_TRIG, NAN_INVTRIG, NAN_LOG, NAN_POWER, NAN_FINAN = 33, 34, 36, 37, 38
QUIET = 1 << 62                 # in an extended significand

# Formats, as bits 11-13 of an opword number them
X, D, S, I, L, C = 0, 1, 2, 4, 5, 6
SIZE = {X: 10, D: 8, S: 4, I: 2, L: 4, C: 8}
LETTER = {X: "X", D: "D", S: "S", I: "I", L: "L", C: "C"}

# Relations, as comparisons return them
GREATER, LESS, EQUAL, UNORDERED = range(4)

FINITE, INF, NAN = range(3)


class FormatError(ValueError):
    """An opword's format field names no format."""


class Precision(NamedTuple):
    bits: int           # of significand
    emin: int           # exponent of the smallest normalized number
    emax: int


EXTENDED = Precision(64, -16383, 16383)
DOUBLE = Precision(53, -1022, 1023)
SINGLE = Precision(24, -126, 127)
FLOATS = {X: EXTENDED, D: DOUBLE, S: SINGLE}
INTEGERS = {I: (-1 << 15, (1 << 15) - 1), L: (-1 << 31, (1 << 31) - 1),
            C: (-(1 << 63) + 1, (1 << 63) - 1)}


class Num(NamedTuple):
    """sig * 2**exp, an infinity, or a NaN, whose sig is an extended
    significand: the code in bits 48-55, quiet if bit 62 is set."""
    kind: int
    sign: int
    sig: int = 0
    exp: int = 0

    @property
    def zero(self) -> bool:
        return self.kind == FINITE and not self.sig

    @property
    def signaling(self) -> bool:
        return self.kind == NAN and not self.sig & QUIET


def nan(code: int) -> Num:
    return Num(NAN, 0, QUIET | (code & 0xFF) << 48)


def nan_code(x: Num) -> int:
    return (x.sig >> 48) & 0xFF


def from_int(v: int) -> Num:
    return Num(FINITE, int(v < 0), abs(v))


ZERO, ONE = Num(FINITE, 0), Num(FINITE, 0, 1)


class Env:
    """The environment word an operation runs under, and the exceptions
    it raises."""

    def __init__(self, word: int = 0):
        self.word = word
        self.raised = 0

    @property
    def rounding(self) -> int:
        return (self.word >> 13) & 3

    @property
    def precision(self) -> Precision:
        return (EXTENDED, DOUBLE, SINGLE, EXTENDED)[(self.word >> 5) & 3]

    def signal(self, flags: int) -> None:
        self.raised |= flags


# =========================================================================
# Formats in memory
# =========================================================================
def unpack(fmt: int, data: bytes) -> Num:
    if fmt == X:
        se, m = struct.unpack(">HQ", data[:10])
        sign, e = se >> 15, se & 0x7FFF
        if e == 0x7FFF:
            return Num(INF, sign) if not m & ~(1 << 63) else Num(NAN, sign, m)
        return Num(FINITE, sign, m, e - 16383 - 63)
    if fmt in (D, S):
        fbits, bias, top = (52, 1023, 0x7FF) if fmt == D else (23, 127, 0xFF)
        bits = int.from_bytes(data[:SIZE[fmt]], "big")
        sign, e, f = bits >> (fbits + (11 if fmt == D else 8)), (bits >> fbits) & top, \
            bits & ((1 << fbits) - 1)
        if e == top:
            return Num(NAN, sign, f << (63 - fbits)) if f else Num(INF, sign)
        if e == 0:
            return Num(FINITE, sign, f, 1 - bias - fbits)
        return Num(FINITE, sign, f | 1 << fbits, e - bias - fbits)
    if fmt in INTEGERS:
        v = int.from_bytes(data[:SIZE[fmt]], "big", signed=True)
        return nan(NAN_COMP) if fmt == C and v == -1 << 63 else from_int(v)
    raise FormatError(fmt)


def encode(fmt: int, x: Num) -> bytes:
    """x in a floating format, which it must already fit."""
    if fmt == X:
        se = x.sign << 15
        if x.kind != FINITE:
            return struct.pack(">HQ", se | 0x7FFF, x.sig if x.kind == NAN else 0)
        if not x.sig:
            return struct.pack(">HQ", se, 0)
        e = max(x.exp + x.sig.bit_length() - 1 + 16383, 0)
        return struct.pack(">HQ", se | e, _shift(x.sig, x.exp - (e - 16383 - 63)))
    fbits, bias, ebits = (52, 1023, 11) if fmt == D else (23, 127, 8)
    top = (1 << ebits) - 1
    if x.kind == INF:
        e, f = top, 0
    elif x.kind == NAN:
        e, f = top, (x.sig >> (63 - fbits)) & ((1 << fbits) - 1) or 1 << (fbits - 1)
    elif not x.sig:
        e, f = 0, 0
    else:
        emin = 1 - bias
        exp = x.exp + x.sig.bit_length() - 1
        if exp >= emin:
            e, f = exp + bias, _shift(x.sig, x.exp - (exp - fbits)) & ((1 << fbits) - 1)
        else:
            e, f = 0, _shift(x.sig, x.exp - (emin - fbits))
    bits = (x.sign << (fbits + ebits)) | (e << fbits) | f
    return bits.to_bytes(SIZE[fmt], "big")


def _shift(v: int, n: int) -> int:
    return v << n if n >= 0 else v >> -n


# =========================================================================
# Rounding
# =========================================================================
def _round_bits(sig: int, shift: int, sign: int, mode: int) -> tuple[int, bool]:
    """sig >> shift, rounded in direction mode, and whether that was inexact."""
    if shift <= 0:
        return sig << -shift, False
    q = sig >> shift
    rem = sig & ((1 << shift) - 1)
    if not rem:
        return q, False
    if mode == TONEAREST:
        half = 1 << (shift - 1)
        q += rem > half or (rem == half and q & 1)
    elif mode == (DOWNWARD if sign else UPWARD):
        q += 1
    return q, True


def round_to(sign: int, sig: int, exp: int, p: Precision, env: Env) -> Num:
    """sig * 2**exp rounded to precision p.  An inexact value comes with
    two or more bits beyond p and a 1 below them standing for the rest."""
    if not sig:
        return Num(FINITE, sign)
    top = exp + sig.bit_length() - 1
    q = max(top, p.emin) - p.bits + 1
    m, inexact = _round_bits(sig, q - exp, sign, env.rounding)
    if m >> p.bits:
        m >>= 1
        q += 1
    if m and q + m.bit_length() - 1 > p.emax:
        env.signal(OVERFLOW | INEXACT)
        if env.rounding in (TONEAREST, DOWNWARD if sign else UPWARD):
            return Num(INF, sign)
        return Num(FINITE, sign, (1 << p.bits) - 1, p.emax - p.bits + 1)
    if inexact:
        env.signal(INEXACT | (UNDERFLOW if top < p.emin else 0))
    return Num(FINITE, sign, m, q) if m else Num(FINITE, sign)


def _divide(sign: int, a: int, a_exp: int, b: int, b_exp: int, p: Precision,
            env: Env) -> Num:
    k = max(0, b.bit_length() - a.bit_length() + 68)
    q, r = divmod(a << k, b)
    e = a_exp - b_exp - k
    if r:
        q, e = q * 2 + 1, e - 1
    return round_to(sign, q, e, p, env)


def propagate(env: Env, *xs: Num) -> Num:
    """What an operation on NaNs delivers: the quiet form of the one with
    the larger code."""
    best = None
    for x in xs:
        if x.kind == NAN:
            if x.signaling:
                env.signal(INVALID)
            if best is None or x.sig & (QUIET - 1) > best.sig & (QUIET - 1):
                best = x
    return best._replace(sig=best.sig | QUIET)


def _invalid(env: Env, code: int) -> Num:
    env.signal(INVALID)
    return nan(code)


def _any_nan(*xs: Num) -> bool:
    return any(x.kind == NAN for x in xs)


# =========================================================================
# Arithmetic.  Results are rounded to the environment's precision.
# =========================================================================
def add(x: Num, y: Num, env: Env) -> Num:
    if _any_nan(x, y):
        return propagate(env, x, y)
    if x.kind == INF:
        if y.kind == INF and x.sign != y.sign:
            return _invalid(env, NAN_ADD)
        return x
    if y.kind == INF:
        return y
    e = min(x.exp, y.exp)
    a = x.sig << (x.exp - e)
    b = y.sig << (y.exp - e)
    s = (-a if x.sign else a) + (-b if y.sign else b)
    if not s:
        if not a and not b and x.sign == y.sign:
            return Num(FINITE, x.sign)
        return Num(FINITE, int(env.rounding == DOWNWARD))
    return round_to(int(s < 0), abs(s), e, env.precision, env)


def neg(x: Num) -> Num:
    return x._replace(sign=1 - x.sign)


def sub(x: Num, y: Num, env: Env) -> Num:
    return add(x, y if y.kind == NAN else neg(y), env)


def mul(x: Num, y: Num, env: Env) -> Num:
    if _any_nan(x, y):
        return propagate(env, x, y)
    sign = x.sign ^ y.sign
    if x.kind == INF or y.kind == INF:
        if x.zero or y.zero:
            return _invalid(env, NAN_MUL)
        return Num(INF, sign)
    return round_to(sign, x.sig * y.sig, x.exp + y.exp, env.precision, env)


def div(x: Num, y: Num, env: Env) -> Num:
    if _any_nan(x, y):
        return propagate(env, x, y)
    sign = x.sign ^ y.sign
    if x.kind == INF:
        return _invalid(env, NAN_DIV) if y.kind == INF else Num(INF, sign)
    if y.kind == INF:
        return Num(FINITE, sign)
    if y.zero:
        if x.zero:
            return _invalid(env, NAN_DIV)
        env.signal(DIVBYZERO)
        return Num(INF, sign)
    if x.zero:
        return Num(FINITE, sign)
    return _divide(sign, x.sig, x.exp, y.sig, y.exp, env.precision, env)


def rem(x: Num, y: Num, env: Env) -> tuple[Num, int]:
    """x - n*y, n being x/y rounded to the nearest integer, and n."""
    if _any_nan(x, y):
        return propagate(env, x, y), 0
    if x.kind == INF or y.zero:
        return _invalid(env, NAN_REM), 0
    if y.kind == INF or x.zero:
        return x, 0
    e = min(x.exp, y.exp)
    a = x.sig << (x.exp - e)
    b = y.sig << (y.exp - e)
    n, r = divmod(a, b)
    if 2 * r > b or (2 * r == b and n & 1):
        n, r = n + 1, r - b
    quo = -n if x.sign != y.sign else n
    if not r:
        return Num(FINITE, x.sign), quo
    return round_to(x.sign ^ (r < 0), abs(r), e, env.precision, env), quo


def sqrt(x: Num, env: Env) -> Num:
    if x.kind == NAN:
        return propagate(env, x)
    if x.zero:
        return x
    if x.sign:
        return _invalid(env, NAN_SQRT)
    if x.kind == INF:
        return x
    shift = max(0, 136 - x.sig.bit_length())
    if (x.exp - shift) & 1:
        shift += 1
    m, e = x.sig << shift, (x.exp - shift) // 2
    r = math.isqrt(m)
    if r * r != m:
        r, e = r * 2 + 1, e - 1
    return round_to(0, r, e, env.precision, env)


def round_integral(x: Num, env: Env, mode: int | None = None) -> Num:
    """Rint, or with mode TOWARDZERO, Tint."""
    if x.kind == NAN:
        return propagate(env, x)
    if x.kind == INF or x.zero or x.exp >= 0:
        return x
    m, inexact = _round_bits(x.sig, -x.exp, x.sign, env.rounding if mode is None else mode)
    if inexact:
        env.signal(INEXACT)
    return Num(FINITE, x.sign, m)


def scalb(n: int, x: Num, env: Env) -> Num:
    if x.kind == NAN:
        return propagate(env, x)
    if x.kind == INF or x.zero:
        return x
    return round_to(x.sign, x.sig, x.exp + n, env.precision, env)


def logb(x: Num, env: Env) -> Num:
    if x.kind == NAN:
        return propagate(env, x)
    if x.kind == INF:
        return Num(INF, 0)
    if x.zero:
        env.signal(DIVBYZERO)
        return Num(INF, 1)
    return from_int(x.exp + x.sig.bit_length() - 1)


def _cmp(x: Num, y: Num) -> int:
    """-1, 0 or 1 as x is less than, equal to or greater than y; neither a NaN."""
    fx = (-1 if x.sign else 1) if x.kind == INF else 0
    fy = (-1 if y.sign else 1) if y.kind == INF else 0
    if fx or fy:
        return (fx > fy) - (fx < fy)
    e = min(x.exp, y.exp)
    a = x.sig << (x.exp - e)
    b = y.sig << (y.exp - e)
    a, b = -a if x.sign else a, -b if y.sign else b
    return (a > b) - (a < b)


def compare(x: Num, y: Num, env: Env, signal_unordered: bool = False) -> int:
    """How x relates to y.  FCPX signals invalid when they're unordered,
    FCMP only for a signaling NaN."""
    if _any_nan(x, y):
        if signal_unordered or x.signaling or y.signaling:
            env.signal(INVALID)
        return UNORDERED
    return {1: GREATER, -1: LESS, 0: EQUAL}[_cmp(x, y)]


def classify(x: Num, p: Precision | None) -> int:
    """1 signaling NaN, 2 quiet NaN, 3 infinity, 4 zero, 5 normalized,
    6 denormalized; negative for a negative x."""
    if x.kind == NAN:
        c = 1 if x.signaling else 2
    elif x.kind == INF:
        c = 3
    elif x.zero:
        c = 4
    else:
        c = 5 if p is None or x.exp + x.sig.bit_length() - 1 >= p.emin else 6
    return -c if x.sign else c


def next_after(x: Num, y: Num, p: Precision, env: Env) -> Num:
    """The number of precision p next to x in the direction of y."""
    if _any_nan(x, y):
        return propagate(env, x, y)
    c = _cmp(x, y)
    if not c:
        return x
    if x.kind == INF:
        return Num(FINITE, x.sign, (1 << p.bits) - 1, p.emax - p.bits + 1)
    if x.zero:
        env.signal(UNDERFLOW | INEXACT)
        return Num(FINITE, int(c > 0), 1, p.emin - p.bits + 1)
    top = x.exp + x.sig.bit_length() - 1
    q = max(top, p.emin) - p.bits + 1
    m = _shift(x.sig, x.exp - q)
    if (c < 0) != bool(x.sign):
        m += 1
        if m >> p.bits:
            m, q = m >> 1, q + 1
    elif m == 1 << (p.bits - 1) and top > p.emin:
        m, q = m * 2 - 1, q - 1         # below a power of two the step is half
    else:
        m -= 1
    if m and q + m.bit_length() - 1 > p.emax:
        env.signal(OVERFLOW | INEXACT)
        return Num(INF, x.sign)
    if not m or q + m.bit_length() - 1 < p.emin:
        env.signal(UNDERFLOW | INEXACT)
    return Num(FINITE, x.sign, m, q)


# =========================================================================
# Conversions between binary formats
# =========================================================================
def to_integer(x: Num, fmt: int, env: Env) -> int:
    """x rounded to an integer format.  What can't be converted is invalid
    and gives the most negative value: the comp NaN for comp."""
    lo, hi = INTEGERS[fmt]
    worst = lo - 1 if fmt == C else lo
    if x.kind == NAN:
        if fmt != C or x.signaling:
            env.signal(INVALID)
        return worst
    if x.kind == INF:
        env.signal(INVALID)
        return worst
    m, inexact = _round_bits(x.sig, -x.exp, x.sign, env.rounding)
    v = -m if x.sign else m
    if not lo <= v <= hi:
        env.signal(INVALID)
        return worst
    if inexact:
        env.signal(INEXACT)
    return v


def convert(x: Num, fmt: int, env: Env) -> bytes:
    """x converted to fmt, in memory.  Single rounding precision narrows
    conversions to double and extended, double to extended."""
    if fmt in FLOATS:
        p = FLOATS[fmt]
        if env.precision.bits < p.bits:
            p = env.precision
        if x.signaling:
            env.signal(INVALID)
            x = x._replace(sig=x.sig | QUIET)
        elif x.kind == FINITE:
            x = round_to(x.sign, x.sig, x.exp, p, env)
        return encode(fmt, x)
    if fmt in INTEGERS:
        return to_integer(x, fmt, env).to_bytes(SIZE[fmt], "big", signed=True)
    raise FormatError(fmt)


# =========================================================================
# Decimal records: (-1)**sgn * sig * 10**exp, sig a string of digits, or
# '0', 'I' for an infinity, 'N' and hex digits for a NaN, '?' for a
# number too big for a fixed-style decform.
# =========================================================================
class Decimal(NamedTuple):
    sgn: int
    exp: int
    sig: bytes


MAX_DIGITS = 19
HEX = b"0123456789ABCDEFabcdef"


def _leading(s: bytes, allowed: bytes) -> bytes:
    n = 0
    while n < len(s) and s[n] in allowed:
        n += 1
    return s[:n]


def nan_significand(hex_digits: bytes) -> int:
    """The significand a decimal record's 'N...' stands for."""
    h = _leading(hex_digits, HEX)
    if len(h) < 4:
        v = int(h or b"0", 16) << 48
    else:
        v = int(h[:16].ljust(16, b"0"), 16)
    return (v or NAN_ZERO << 48) | QUIET


def from_decimal(d: Decimal) -> Num:
    """A decimal record's value, exactly or with enough bits that rounding
    it once more is right."""
    first = d.sig[:1].upper()
    sign = 1 if d.sgn else 0
    if first == b"N":
        return Num(NAN, sign, nan_significand(d.sig[1:]))
    if first == b"I":
        return Num(INF, sign)
    digits = _leading(d.sig, b"0123456789")
    if not digits or first == b"0":
        return Num(FINITE, sign)
    n = int(digits[:MAX_DIGITS])
    sticky = digits[MAX_DIGITS:].strip(b"0") != b""      # a 20th digit: there's more
    exp = max(min(d.exp, 5000), -5100)                  # beyond extended either way
    num, den = (n * 10 ** exp, 1) if exp >= 0 else (n, 10 ** -exp)
    k = max(0, den.bit_length() - num.bit_length() + 68)
    q, r = divmod(num << k, den)
    if r or sticky:
        q, k = q * 2 + 1, k + 1
    return Num(FINITE, sign, q, -k)


def _scaled(x: Num, e10: int, mode: int) -> tuple[int, bool]:
    """|x| / 10**e10 rounded to an integer, and whether that was inexact."""
    num, den = x.sig, 1
    if x.exp >= 0:
        num <<= x.exp
    else:
        den <<= -x.exp
    if e10 >= 0:
        den *= 10 ** e10
    else:
        num *= 10 ** -e10
    q, r = divmod(num, den)
    if r:
        if mode == TONEAREST:
            q += 2 * r > den or (2 * r == den and q & 1)
        elif mode == (DOWNWARD if x.sign else UPWARD):
            q += 1
    return q, bool(r)


def to_decimal(x: Num, fixed: bool, digits: int, env: Env) -> Decimal:
    """Num2Dec: digits significant digits, or for fixed style, digits
    after the point."""
    if x.kind == NAN:
        if x.signaling:
            env.signal(INVALID)
        return Decimal(x.sign, 0, b"N%016X" % (x.sig | QUIET))
    if x.kind == INF:
        return Decimal(x.sign, 0, b"I")
    if x.zero:
        return Decimal(x.sign, -digits if fixed else 0, b"0")
    if fixed:
        e10 = -digits
        n, inexact = _scaled(x, e10, env.rounding)
        if n >= 10 ** MAX_DIGITS:
            return Decimal(x.sign, 0, b"?")
    else:
        count = max(1, min(digits, MAX_DIGITS))
        e10 = math.floor((x.exp + x.sig.bit_length() - 1) * math.log10(2)) - count + 1
        while True:
            n, inexact = _scaled(x, e10, env.rounding)
            if n >= 10 ** count:
                e10 += 1
            elif n < 10 ** (count - 1) and e10 > -100000:
                e10 -= 1
            else:
                break
    if inexact:
        env.signal(INEXACT)
    return Decimal(x.sign, e10, str(n).encode())


# =========================================================================
# DecStr68K: decimal strings to decimal records and back
# =========================================================================
DIGITS = b"0123456789"
_ACCEPTING = {"int", "frac", "exp", "inf", "nan", "nancode"}


def scan(text: bytes, start: int) -> tuple[Decimal, int, bool]:
    """Str2Dec: the longest number at text[start:], where it ends (start
    if there's none), and whether text[start:] is a number or the start
    of one."""
    state, i = "lead", start
    sign, whole, frac, exp_digits, exp_sign, code = 0, b"", b"", b"", 1, b""
    accepted = None
    valid = True
    while i < len(text):
        c = text[i:i + 1]
        u = c.upper()
        if state == "lead" and c in (b" ", b"\t"):
            pass
        elif state == "lead" and c in (b"+", b"-"):
            state, sign = "sign", int(c == b"-")
        elif state in ("lead", "sign", "int") and c in DIGITS:
            state, whole = "int", whole + c
        elif state in ("lead", "sign", "int") and c == b".":
            state = "frac" if state == "int" else "point"
        elif state in ("point", "frac") and c in DIGITS:
            state, frac = "frac", frac + c
        elif state in ("int", "frac") and u == b"E":
            state = "e"
        elif state == "e" and c in (b"+", b"-"):
            state, exp_sign = "esign", -1 if c == b"-" else 1
        elif state in ("e", "esign", "exp") and c in DIGITS:
            state, exp_digits = "exp", exp_digits + c
        elif state in ("lead", "sign") and u in (b"I", b"N"):
            state = u.decode()
        elif (state, u) in (("I", b"N"), ("IN", b"F"), ("N", b"A"), ("NA", b"N")):
            state = {"I": "IN", "IN": "inf", "N": "NA", "NA": "nan"}[state]
        elif state == "nan" and c == b"(":
            state = "nanparen"
        elif state == "nanparen" and c in DIGITS:
            code += c
        elif state == "nanparen" and c == b")":
            state = "nancode"
        else:
            valid = False
            break
        i += 1
        if state in _ACCEPTING:
            accepted = (i, state, code if state == "nancode" else b"")
    if accepted is None:
        return Decimal(0, 0, b"N%04X" % NAN_ASCBIN), start, valid
    end, state, code = accepted
    if state == "inf":
        return Decimal(sign, 0, b"I"), end, valid
    if state in ("nan", "nancode"):
        n = int(code or b"0") & 0xFF
        return Decimal(sign, 0, b"N%04X" % n if n else b"N"), end, valid
    exp_digits = exp_digits.lstrip(b"0")
    exp = exp_sign * (int(exp_digits or b"0") if len(exp_digits) < 8 else 10 ** 7) - len(frac)
    digits = (whole + frac).lstrip(b"0")
    if not digits:
        return Decimal(sign, 0, b"0"), end, valid
    exp += max(len(digits) - MAX_DIGITS, 0)
    kept = digits[:MAX_DIGITS]
    if digits[MAX_DIGITS:].strip(b"0"):
        kept += b"1"                    # what's left over, for rounding
    else:
        stripped = kept.rstrip(b"0")
        exp += len(kept) - len(stripped)
        kept = stripped
    return Decimal(sign, max(-32768, min(exp, 32767)), kept), end, valid


def format_decimal(fixed: bool, digits: int, d: Decimal) -> bytes:
    """Dec2Str: [-| ]m[.nnn]e[+|-]dddd in floating style, [-]mmm[.nnn] in
    fixed; '?' for more than 80 characters."""
    minus = b"-" if d.sgn else b""
    lead = minus or (b"" if fixed else b" ")
    first = d.sig[:1].upper()
    if first == b"N":
        return lead + b"NAN(%03d)" % ((nan_significand(d.sig[1:]) >> 48) & 0xFF)
    if first == b"I":
        return lead + b"INF"
    if first == b"?":
        return b"?"
    sig = _leading(d.sig, DIGITS)
    zero = not sig or first == b"0"
    if fixed:
        digits = max(digits, 0)
        if zero:
            whole, frac = b"0", b"0" * digits
        else:
            if d.exp >= 0:
                whole, frac = sig + b"0" * d.exp, b""
            else:
                k = -d.exp
                whole, frac = sig[:-k] if len(sig) > k else b"", sig[-k:].rjust(k, b"0")
            whole = whole.lstrip(b"0") or b"0"
            frac = frac.ljust(digits, b"0")
        s = minus + whole + (b"." + frac if frac else b"")
    else:
        if zero:
            mant, e = b"0" * max(digits, 1), 0
        else:
            mant, e = sig.ljust(max(digits, len(sig)), b"0"), d.exp + len(sig) - 1
        s = lead + mant[:1] + (b"." + mant[1:] if len(mant) > 1 else b"") + \
            b"e%+d" % e
    return b"?" if len(s) > 80 else s


# =========================================================================
# Elementary functions.  They work in doubles, then round once more to the
# environment's precision, always inexact; what's exact is done exactly.
# =========================================================================
LN2 = math.log(2)
PI_SIG, PI_EXP = 0xC90FDAA22168C235, -62        # pi to extended precision
TINY = -70          # below 2**TINY, sin x is x to extended precision, and so on
MINUS_ONE = Num(FINITE, 1, 1)


def _top(x: Num) -> int:
    """The exponent of x's leading bit."""
    return x.exp + x.sig.bit_length() - 1


def to_float(x: Num) -> float:
    if x.kind == INF:
        return -math.inf if x.sign else math.inf
    if x.kind == NAN:
        return math.nan
    drop = max(x.sig.bit_length() - 64, 0)
    try:
        v = math.ldexp(float(x.sig >> drop), x.exp + drop)
    except OverflowError:
        v = math.inf
    return -v if x.sign else v


def _log2(x: Num) -> float:
    """log2 |x|, for any finite nonzero x."""
    n = x.sig.bit_length()
    m = math.ldexp(float(x.sig >> max(n - 64, 0)), 1 - min(n, 64))
    return math.log2(m) + (x.exp + n - 1)


def _near(sign: int, sig: int, exp: int, env: Env, below: bool = False) -> Num:
    """A value a hair above (or below) sig * 2**exp in magnitude, rounded."""
    sig <<= 70
    return round_to(sign, sig - 1 if below else sig + 1, exp - 70, env.precision, env)


def _approx(v: float, env: Env, scale: int = 0) -> Num:
    """A result computed as the double v, times 2**scale."""
    if math.isinf(v):
        env.signal(OVERFLOW | INEXACT)
        return Num(INF, int(v < 0))
    if v == 0:
        env.signal(UNDERFLOW | INEXACT)
        return Num(FINITE, int(math.copysign(1, v) < 0))
    m, e = math.frexp(abs(v))
    return _near(int(v < 0), int(math.ldexp(m, 53)), e - 53 + scale, env)


def _times(x: Num, c: float, env: Env, below: bool = False) -> Num:
    """x times a positive constant, for an x too small for a double."""
    m, e = math.frexp(c)
    return _near(x.sign, x.sig * int(math.ldexp(m, 53)), x.exp + e - 53, env, below)


def _pow2(t: float, env: Env, sign: int = 0) -> Num:
    """+/-2**t, over all of extended's range and beyond it."""
    if math.isnan(t):
        return _invalid(env, NAN_POWER)
    t = max(min(t, 40000.0), -40000.0)
    k = math.floor(t)
    v = 2.0 ** (t - k)
    return _approx(-v if sign else v, env, k)


def ln(x: Num, env: Env, base2: bool = False) -> Num:
    if x.kind == NAN:
        return propagate(env, x)
    if x.zero:
        env.signal(DIVBYZERO)
        return Num(INF, 1)
    if x.sign:
        return _invalid(env, NAN_LOG)
    if x.kind == INF:
        return x
    e = _top(x)
    if not x.sig & (x.sig - 1):                 # a power of two
        if base2:
            return from_int(e)
        if not e:
            return ZERO
    if e in (0, -1):                            # near 1: from x - 1, which is exact
        v = math.log1p(to_float(add(x, MINUS_ONE, Env())))
    else:
        v = _log2(x) * LN2
    return _approx(v / LN2 if base2 else v, env)


def ln1(x: Num, env: Env, base2: bool = False) -> Num:
    """ln(1 + x), or log2(1 + x)."""
    if x.kind == NAN:
        return propagate(env, x)
    if x.zero or (x.kind == INF and not x.sign):
        return x
    c = _cmp(x, MINUS_ONE)
    if c == 0:
        env.signal(DIVBYZERO)
        return Num(INF, 1)
    if c < 0:
        return _invalid(env, NAN_LOG)
    if _top(x) < TINY:
        below = not x.sign
        return _times(x, 1 / LN2, env, below) if base2 else _near(x.sign, x.sig, x.exp, env, below)
    v = to_float(x)
    r = _log2(x) * LN2 if v > 1e300 else math.log1p(v)
    return _approx(r / LN2 if base2 else r, env)


def exp(x: Num, env: Env, base2: bool = False, minus1: bool = False) -> Num:
    """e**x or 2**x, less 1 if minus1."""
    if x.kind == NAN:
        return propagate(env, x)
    if x.kind == INF:
        return (MINUS_ONE if minus1 else ZERO) if x.sign else x
    if x.zero:
        return x if minus1 else ONE
    if base2 and _integral(x) and _top(x) < 15:
        n = _shift(x.sig, x.exp) * (-1 if x.sign else 1)
        p = env.precision
        if not minus1:
            return round_to(0, 1, n, p, env)
        return round_to(0, (1 << n) - 1, 0, p, env) if n > 0 else \
            round_to(1, (1 << -n) - 1, n, p, env)
    if _top(x) < TINY:
        if not minus1:
            return _near(0, 1, 0, env, below=bool(x.sign))
        if base2:
            return _times(x, LN2, env, below=bool(x.sign))
        return _near(x.sign, x.sig, x.exp, env, below=bool(x.sign))
    v = to_float(x)
    t = v if base2 else v / LN2                 # the power of 2
    if minus1 and abs(t) < 64:
        return _approx(math.expm1(v * LN2 if base2 else v), env)
    if minus1 and t < 0:
        return _near(1, 1, 0, env, below=True)
    if not base2 and abs(t) < 1000:
        return _approx(math.exp(v), env)
    return _pow2(t, env)


def _integral(x: Num) -> bool:
    return x.kind == INF or x.zero or x.exp >= 0 or \
        not x.sig & ((1 << -x.exp) - 1)


def _odd(x: Num) -> bool:
    if x.kind != FINITE or not x.sig or not _integral(x) or x.exp > 0:
        return False
    return bool((x.sig >> -x.exp) & 1)


def xpwri(x: Num, i: int, env: Env) -> Num:
    """x**i, exactly rounded unless that takes millions of bits."""
    if x.kind == NAN:
        return propagate(env, x)
    if i == 0:
        return ONE
    sign = x.sign if i & 1 else 0
    if x.zero or x.kind == INF:
        if (i < 0) == x.zero:
            if x.zero:
                env.signal(DIVBYZERO)
            return Num(INF, sign)
        return Num(FINITE, sign)
    tz = (x.sig & -x.sig).bit_length() - 1
    sig, e, n = x.sig >> tz, x.exp + tz, abs(i)
    if sig.bit_length() * n > 2_000_000:
        return _pow2(i * _log2(x), env, sign)
    p = env.precision
    if i > 0:
        return round_to(sign, sig ** n, e * n, p, env)
    return _divide(sign, 1, 0, sig ** n, e * n, p, env)


def xpwry(x: Num, y: Num, env: Env) -> Num:
    if _any_nan(x, y):
        return propagate(env, x, y)
    if y.zero:
        return _invalid(env, NAN_POWER) if x.zero or x.kind == INF else ONE
    if _cmp(x, ONE) == 0:
        return _invalid(env, NAN_POWER) if y.kind == INF else ONE
    integral = _integral(y)
    if x.sign and not integral:
        return _invalid(env, NAN_POWER)
    if integral and y.kind == FINITE and _top(y) < 15:
        n = _shift(y.sig, y.exp)
        return xpwri(x, -n if y.sign else n, env)
    sign = x.sign if _odd(y) else 0
    if x.zero:
        if y.sign:
            env.signal(DIVBYZERO)
            return Num(INF, sign)
        return Num(FINITE, sign)
    if x.kind == INF:
        return Num(FINITE, sign) if y.sign else Num(INF, sign)
    if y.kind == INF:
        c = _cmp(x._replace(sign=0), ONE)
        if c == 0:
            return _invalid(env, NAN_POWER)
        return Num(INF, 0) if (c > 0) != bool(y.sign) else ZERO
    return _pow2(to_float(y) * _log2(x), env, sign)


def compound(r: Num, n: Num, env: Env) -> Num:
    """(1 + r)**n: r is the interest rate, n the number of periods."""
    if _any_nan(r, n):
        return propagate(env, r, n)
    c = _cmp(r, MINUS_ONE)
    if c < 0 or (r.zero and n.kind == INF):
        return _invalid(env, NAN_FINAN)
    if n.zero or r.zero:
        return ONE
    if c == 0:
        if n.sign:
            env.signal(DIVBYZERO)
            return Num(INF, 0)
        return ZERO
    if r.kind == INF or n.kind == INF:
        return Num(INF, 0) if r.sign == n.sign else ZERO
    rf = to_float(r)
    if rf == 0:
        t = to_float(mul(n, r, Env())) / LN2      # (1 + r)**n is e**(n*r)
    else:
        t = to_float(n) * math.log1p(rf) / LN2
    return _pow2(t, env)


def annuity(r: Num, n: Num, env: Env) -> Num:
    """(1 - (1 + r)**-n) / r: the present value of n payments of 1."""
    if _any_nan(r, n):
        return propagate(env, r, n)
    c = _cmp(r, MINUS_ONE)
    if c < 0:
        return _invalid(env, NAN_FINAN)
    if r.zero:
        return n
    if n.zero:
        return ZERO
    if c == 0:
        if n.sign:
            return MINUS_ONE
        env.signal(DIVBYZERO)
        return Num(INF, 0)
    if r.kind == INF:
        return _invalid(env, NAN_FINAN) if n.sign else ZERO
    rf, nf = to_float(r), to_float(n)
    if rf == 0:
        return _approx(nf, env) if n.kind == FINITE else n
    a = -nf * math.log1p(rf)                    # ln((1 + r)**-n)
    if a > 700:                                 # the 1 doesn't count
        return _pow2(a / LN2 - math.log2(abs(rf)), env, int(rf > 0))
    return _approx(-math.expm1(a) / rf, env)


def _reduce(x: Num) -> tuple[float, int]:
    """x as r + k*pi/2, |r| <= pi/4, for SANE's pi: (r, k mod 4)."""
    if _top(x) < -1:
        return to_float(x), 0
    e = min(x.exp, PI_EXP - 1)
    a, b = x.sig << (x.exp - e), PI_SIG << (PI_EXP - 1 - e)
    k, r = divmod(a, b)
    if 2 * r > b:
        k, r = k + 1, r - b
    drop = max(abs(r).bit_length() - 64, 0)
    v = math.ldexp(float(r >> drop), e + drop)
    return (-v, -k % 4) if x.sign else (v, k % 4)


def trig(name: str, x: Num, env: Env) -> Num:
    """sin, cos or tan, periodic in SANE's pi."""
    if x.kind == NAN:
        return propagate(env, x)
    if x.kind == INF:
        return _invalid(env, NAN_TRIG)
    if x.zero:
        return ONE if name == "cos" else x
    if _top(x) < TINY:
        if name == "cos":
            return _near(0, 1, 0, env, below=True)
        return _near(x.sign, x.sig, x.exp, env, below=name == "sin")
    r, k = _reduce(x)
    if name == "tan":
        if k % 2 == 0:
            v = math.tan(r)
        elif r:
            v = -1 / math.tan(r)
        else:
            env.signal(DIVBYZERO)
            return Num(INF, x.sign)               # "Tan(+-pi/2) returns +-INF"
    else:
        s, c = math.sin(r), math.cos(r)
        v = ((s, c, -s, -c) if name == "sin" else (c, -s, -c, s))[k]
    if v == 0:                                  # a multiple of pi, exactly
        return Num(FINITE, int(math.copysign(1, v) < 0))
    return _approx(v, env)


def atan(x: Num, env: Env) -> Num:
    if x.kind == NAN:
        return propagate(env, x)
    if x.zero:
        return x
    if _top(x) < TINY:
        return _near(x.sign, x.sig, x.exp, env, below=True)
    if x.kind == INF or _top(x) > 70:           # pi/2, less 1/x
        return _near(x.sign, PI_SIG, PI_EXP - 1, env, below=True)
    return _approx(math.atan(to_float(x)), env)


def random(x: Num, env: Env) -> Num:
    """RandomX: x <- 7**5 * x mod (2**31 - 1)."""
    if x.kind != FINITE:
        return x
    m = (1 << 31) - 1
    e = min(x.exp, 0)
    num = 16807 * (x.sig << (x.exp - e))        # 16807 * |x| / 2**e
    v = (-num if x.sign else num) % (m << -e)
    return round_to(0, v, e, env.precision, env)
