"""SANE: the arithmetic against the Apple Numerics Manual's examples and
IEEE's special cases, and FP68K, Elems68K and DecStr68K as 68k code
calls them."""

import math
import random
import struct

import pytest

from macrt.m68k import dispatch
from macrt.m68k.cpu import CPU
from macrt.toolbox import lowmem, sane
from macrt.toolbox.sane import (C, D, DIVBYZERO, INEXACT, INVALID, OVERFLOW, UNDERFLOW, I, L, S,
                                X, Env)

from guest import Guest

NEAREST, UPWARD, DOWNWARD, TOWARDZERO = 0x0000, 0x2000, 0x4000, 0x6000
DOUBLE_PRECISION, SINGLE_PRECISION = 0x20, 0x40


def ext(hex_digits: str) -> sane.Num:
    return sane.unpack(X, bytes.fromhex(hex_digits))


def hex_x(x: sane.Num, env: Env | None = None) -> str:
    return sane.convert(x, X, env or Env()).hex()


def dbl(v: float) -> sane.Num:
    return sane.unpack(D, struct.pack(">d", v))


def to_dbl(x: sane.Num, env: Env | None = None) -> float:
    return struct.unpack(">d", sane.convert(x, D, env or Env()))[0]


def num(text: str) -> sane.Num:
    """A decimal string's value, rounded to extended."""
    d, _, _ = sane.scan(text.encode(), 0)
    x = sane.from_decimal(d)
    return sane.unpack(X, sane.convert(x, X, Env()))


ONE, TWO, THREE = sane.from_int(1), sane.from_int(2), sane.from_int(3)
INF, MINUS_INF = sane.Num(sane.INF, 0), sane.Num(sane.INF, 1)
ZERO, MINUS_ZERO = sane.Num(sane.FINITE, 0), sane.Num(sane.FINITE, 1)
QNAN = sane.nan(sane.NAN_ADD)
SNAN = ext("7fff2000000000000000")
MAX_EXT = ext("7ffeffffffffffffffff")


# =========================================================================
# Arithmetic
# =========================================================================
@pytest.mark.parametrize("rounding, third, minus_third", [
    (NEAREST, "3ffdaaaaaaaaaaaaaaab", "bffdaaaaaaaaaaaaaaab"),
    (UPWARD, "3ffdaaaaaaaaaaaaaaab", "bffdaaaaaaaaaaaaaaaa"),
    (DOWNWARD, "3ffdaaaaaaaaaaaaaaaa", "bffdaaaaaaaaaaaaaaab"),
    (TOWARDZERO, "3ffdaaaaaaaaaaaaaaaa", "bffdaaaaaaaaaaaaaaaa"),
])
def test_division_rounds_in_each_direction(rounding, third, minus_third):
    env = Env(rounding)
    assert hex_x(sane.div(ONE, THREE, env)) == third
    assert hex_x(sane.div(sane.neg(ONE), THREE, env)) == minus_third
    assert env.raised == INEXACT


def test_rounding_precision_rounds_as_if_to_the_narrower_format():
    """Results "overflow or underflow as if the destination were single
    (or double)" (chapter 4), though they're delivered as extended."""
    env = Env(SINGLE_PRECISION)
    third = sane.div(ONE, THREE, env)
    assert hex_x(third) == sane.convert(sane.unpack(S, struct.pack(">f", 1 / 3)), X, Env()).hex()
    env = Env(DOUBLE_PRECISION)
    assert sane.mul(ext("00010000000000000001"), ONE, env) == ZERO
    assert env.raised == UNDERFLOW | INEXACT
    assert sane.mul(MAX_EXT, ONE, Env(DOUBLE_PRECISION)) == INF
    # Conversions to extended narrow too; to integers they don't.
    assert sane.convert(third, X, Env(DOUBLE_PRECISION)) == sane.convert(third, X, Env())
    assert sane.convert(dbl(0.1), X, Env(SINGLE_PRECISION)) == \
        sane.convert(sane.unpack(S, struct.pack(">f", 0.1)), X, Env())
    assert sane.convert(dbl(123456789.0), L, Env(SINGLE_PRECISION)) == \
        (123456789).to_bytes(4, "big")


def test_double_precision_matches_the_hosts_doubles():
    """Rounded to double, every basic operation is IEEE's correctly rounded
    one, denormals included."""
    rng = random.Random(1)
    ops = ((sane.add, lambda a, b: a + b), (sane.sub, lambda a, b: a - b),
           (sane.mul, lambda a, b: a * b), (sane.div, lambda a, b: a / b))
    for _ in range(1500):
        if rng.random() < 0.5:
            a = math.ldexp(rng.random(), rng.randint(-1074, 1023))
            b = math.ldexp(rng.random(), rng.randint(-1074, 1023))
        else:
            a, b = (struct.unpack(">d", rng.getrandbits(64).to_bytes(8, "big"))[0]
                    for _ in range(2))
        for op, ref in ops:
            try:
                want = ref(a, b)
            except (ZeroDivisionError, OverflowError):
                continue
            got = to_dbl(op(dbl(a), dbl(b), Env(DOUBLE_PRECISION)))
            if math.isnan(want):
                assert math.isnan(got)
            else:
                assert (got, math.copysign(1, got)) == (want, math.copysign(1, want)), (a, b)
        if a >= 0:
            assert to_dbl(sane.sqrt(dbl(a), Env(DOUBLE_PRECISION))) == math.sqrt(a)


def test_invalid_operations_give_their_nan_codes():
    for result, code in ((sane.div(ZERO, ZERO, Env()), sane.NAN_DIV),
                         (sane.sub(INF, INF, Env()), sane.NAN_ADD),
                         (sane.mul(ZERO, INF, Env()), sane.NAN_MUL),
                         (sane.div(INF, MINUS_INF, Env()), sane.NAN_DIV),
                         (sane.sqrt(sane.neg(ONE), Env()), sane.NAN_SQRT),
                         (sane.rem(INF, ONE, Env())[0], sane.NAN_REM)):
        assert result.kind == sane.NAN and sane.nan_code(result) == code
    assert hex_x(sane.div(ZERO, ZERO, Env())) == "7fff4004000000000000"
    env = Env()
    sane.div(ZERO, ZERO, env)
    assert env.raised == INVALID


def test_nans_propagate_and_only_signaling_ones_are_invalid():
    env = Env()
    assert sane.add(QNAN, ONE, env) == QNAN and env.raised == 0
    r = sane.add(ONE, SNAN, env)
    assert not r.signaling and env.raised == INVALID
    bigger = sane.nan(sane.NAN_MUL)
    assert sane.add(QNAN, bigger, Env()) == bigger == sane.add(bigger, QNAN, Env())


def test_infinities_and_division_by_zero():
    env = Env()
    assert sane.div(ONE, ZERO, env) == INF and env.raised == DIVBYZERO
    assert sane.div(ONE, MINUS_ZERO, Env()) == MINUS_INF
    assert sane.add(INF, ONE, Env()) == INF
    assert sane.div(ONE, INF, Env()) == ZERO
    assert sane.div(sane.neg(ONE), INF, Env()) == MINUS_ZERO


def test_signed_zeros():
    assert sane.add(ZERO, MINUS_ZERO, Env()) == ZERO
    assert sane.add(ZERO, MINUS_ZERO, Env(DOWNWARD)) == MINUS_ZERO
    assert sane.add(MINUS_ZERO, MINUS_ZERO, Env()) == MINUS_ZERO
    assert sane.sub(ONE, ONE, Env()) == ZERO
    assert sane.sub(ONE, ONE, Env(DOWNWARD)) == MINUS_ZERO
    assert sane.mul(MINUS_ZERO, ONE, Env()) == MINUS_ZERO
    assert sane.sqrt(MINUS_ZERO, Env()) == MINUS_ZERO
    assert sane.compare(ZERO, MINUS_ZERO, Env()) == sane.EQUAL


def test_overflow_rounds_to_infinity_or_the_largest_number():
    for rounding, want in ((NEAREST, "7fff0000000000000000"), (UPWARD, "7fff0000000000000000"),
                           (DOWNWARD, "7ffeffffffffffffffff"),
                           (TOWARDZERO, "7ffeffffffffffffffff")):
        env = Env(rounding)
        assert hex_x(sane.mul(MAX_EXT, TWO, env)) == want
        assert env.raised == OVERFLOW | INEXACT


def test_denormals_and_underflow():
    smallest_normal = ext("00008000000000000000")
    env = Env()
    half = sane.div(smallest_normal, TWO, env)
    assert hex_x(half) == "00004000000000000000"
    assert env.raised == 0                      # exact, so no underflow
    assert sane.classify(half, sane.EXTENDED) == 6
    env = Env()
    assert hex_x(sane.div(ext("00000000000000000001"), TWO, env)) == "00000000000000000000"
    assert env.raised == UNDERFLOW | INEXACT
    env = Env(UPWARD)
    assert hex_x(sane.div(ext("00000000000000000001"), TWO, env)) == "00000000000000000001"
    assert to_dbl(sane.unpack(X, sane.convert(dbl(5e-324), X, Env()))) == 5e-324


def test_remainder_rounds_the_quotient_to_nearest():
    r, quo = sane.rem(sane.from_int(5), THREE, Env())
    assert (to_dbl(r), quo) == (-1.0, 2)
    r, quo = sane.rem(sane.from_int(-5), THREE, Env())
    assert (to_dbl(r), quo) == (1.0, -2)
    r, quo = sane.rem(sane.from_int(7), TWO, Env())       # 3.5 goes to 4
    assert (to_dbl(r), quo) == (-1.0, 4)
    r, quo = sane.rem(sane.from_int(-4), TWO, Env())
    assert r == MINUS_ZERO and quo == -2


def test_round_to_integral_value():
    assert to_dbl(sane.round_integral(dbl(2.5), Env())) == 2.0
    assert to_dbl(sane.round_integral(dbl(3.5), Env())) == 4.0
    assert to_dbl(sane.round_integral(dbl(2.5), Env(UPWARD))) == 3.0
    assert to_dbl(sane.round_integral(dbl(-2.7), Env(), sane.TOWARDZERO)) == -2.0
    assert sane.round_integral(dbl(-0.25), Env()) == MINUS_ZERO
    env = Env()
    sane.round_integral(dbl(4.0), env)
    assert env.raised == 0


def test_logb_and_scalb():
    assert to_dbl(sane.logb(dbl(8.0), Env())) == 3.0
    assert to_dbl(sane.logb(dbl(0.1), Env())) == -4.0
    env = Env()
    assert sane.logb(ZERO, env) == MINUS_INF and env.raised == DIVBYZERO
    assert sane.logb(MINUS_INF, Env()) == INF
    assert to_dbl(sane.scalb(-3, dbl(8.0), Env())) == 1.0
    env = Env()
    assert sane.scalb(20000, ONE, env) == INF and env.raised == OVERFLOW | INEXACT


def test_compare():
    env = Env()
    assert sane.compare(ONE, TWO, env) == sane.LESS
    assert sane.compare(TWO, ONE, env) == sane.GREATER
    assert sane.compare(MINUS_INF, ONE, env) == sane.LESS
    assert sane.compare(QNAN, ONE, env) == sane.UNORDERED and env.raised == 0
    assert sane.compare(QNAN, ONE, env, signal_unordered=True) == sane.UNORDERED
    assert env.raised == INVALID
    env = Env()
    assert sane.compare(SNAN, ONE, env) == sane.UNORDERED and env.raised == INVALID


@pytest.mark.parametrize("x, fmt, want", [
    (SNAN, X, 1), (QNAN, X, 2), (MINUS_INF, X, -3), (MINUS_ZERO, X, -4), (ONE, X, 5),
    (dbl(5e-324), D, 6), (dbl(5e-324), X, 5),
    (sane.unpack(C, bytes.fromhex("80" + "00" * 7)), C, 2),        # comp's NaN
])
def test_classify(x, fmt, want):
    assert sane.classify(x, sane.FLOATS.get(fmt)) == want


def test_next_after():
    p = sane.DOUBLE
    assert to_dbl(sane.next_after(dbl(1.0), dbl(2.0), p, Env())) == math.nextafter(1.0, 2.0)
    assert to_dbl(sane.next_after(dbl(1.0), dbl(0.0), p, Env())) == math.nextafter(1.0, 0.0)
    assert to_dbl(sane.next_after(dbl(-1.0), dbl(0.0), p, Env())) == math.nextafter(-1.0, 0.0)
    env = Env()
    assert to_dbl(sane.next_after(dbl(0.0), dbl(-1.0), p, env)) == -5e-324
    assert env.raised == UNDERFLOW | INEXACT
    env = Env()
    assert sane.next_after(dbl(1.7976931348623157e308), INF, p, env) == INF
    assert env.raised == OVERFLOW | INEXACT
    assert to_dbl(sane.next_after(INF, ZERO, p, Env())) == 1.7976931348623157e308


def test_conversions_to_integers():
    env = Env()
    assert sane.convert(dbl(2.5), I, env) == (2).to_bytes(2, "big") and env.raised == INEXACT
    assert sane.convert(dbl(-3.5), L, Env()) == (-4).to_bytes(4, "big", signed=True)
    assert sane.convert(dbl(2.5), I, Env(UPWARD)) == (3).to_bytes(2, "big")
    env = Env()
    assert sane.convert(dbl(40000.0), I, env) == bytes.fromhex("8000")
    assert env.raised == INVALID
    env = Env()
    assert sane.convert(QNAN, C, env) == bytes.fromhex("8000000000000000") and env.raised == 0
    env = Env()
    assert sane.convert(QNAN, L, env) == bytes.fromhex("80000000") and env.raised == INVALID
    comp_nan = sane.unpack(C, bytes.fromhex("8000000000000000"))
    assert comp_nan.kind == sane.NAN and sane.nan_code(comp_nan) == sane.NAN_COMP


def test_conversions_between_floating_formats():
    env = Env()
    assert sane.convert(dbl(0.1), S, env) == struct.pack(">f", 0.1) and env.raised == INEXACT
    assert sane.convert(SNAN, D, Env()).hex() == "7ffc000000000000"     # made quiet
    assert sane.convert(dbl(-0.0), X, Env()).hex() == "80000000000000000000"
    assert sane.convert(dbl(math.inf), S, Env()) == struct.pack(">f", math.inf)
    env = Env()
    assert sane.convert(MAX_EXT, D, env) == struct.pack(">d", math.inf)
    assert env.raised == OVERFLOW | INEXACT


# =========================================================================
# Decimal
# =========================================================================
def test_decimal_to_binary_is_correctly_rounded():
    tenth = sane.Decimal(0, -1, b"1")
    assert hex_x(sane.from_decimal(tenth)) == "3ffbcccccccccccccccd"
    assert hex_x(sane.from_decimal(tenth), Env(DOWNWARD)) == "3ffbcccccccccccccccc"
    assert to_dbl(sane.from_decimal(sane.Decimal(1, 3, b"125"))) == -125000.0
    assert sane.from_decimal(sane.Decimal(0, 99999, b"1")).kind == sane.FINITE
    assert hex_x(sane.from_decimal(sane.Decimal(0, 99999, b"1"))) == "7fff0000000000000000"
    assert hex_x(sane.from_decimal(sane.Decimal(1, 0, b"0"))) == "80000000000000000000"
    assert sane.from_decimal(sane.Decimal(1, 0, b"I")) == MINUS_INF
    assert sane.nan_code(sane.from_decimal(sane.Decimal(0, 0, b"N0024"))) == 0x24


def test_binary_to_decimal():
    third = sane.div(ONE, THREE, Env())
    assert sane.to_decimal(third, False, 19, Env()) == sane.Decimal(0, -19, b"3333333333333333333")
    assert sane.to_decimal(third, False, 5, Env(UPWARD)) == sane.Decimal(0, -5, b"33334")
    assert sane.to_decimal(num("1234.5678"), True, 2, Env()) == sane.Decimal(0, -2, b"123457")
    assert sane.to_decimal(num("1e30"), True, 2, Env()).sig == b"?"
    assert sane.to_decimal(MINUS_ZERO, True, 3, Env()) == sane.Decimal(1, -3, b"0")
    assert sane.to_decimal(MINUS_INF, False, 3, Env()) == sane.Decimal(1, 0, b"I")
    d = sane.to_decimal(dbl(0.1), False, 17, Env())
    assert float(d.sig + b"e%d" % d.exp) == 0.1


@pytest.mark.parametrize("text, start, end, value, valid", [
    # Table 3-3, with Pascal's 1-based index made 0-based
    ("12", 0, 2, (0, 0, b"12"), True),
    ("12E", 0, 2, (0, 0, b"12"), True),
    ("12E-", 0, 2, (0, 0, b"12"), True),
    ("12E-3", 0, 5, (0, -3, b"12"), True),
    ("12E-X", 0, 2, (0, 0, b"12"), False),
    ("12E-3X", 0, 5, (0, -3, b"12"), False),
    ("x12E-3", 1, 6, (0, -3, b"12"), True),
    ("IN", 0, 0, (0, 0, b"N0011"), True),
    ("INF", 0, 3, (0, 0, b"I"), True),
    # and more
    ("-0.0025", 0, 7, (1, -4, b"25"), True),
    (" +1.5e+2", 0, 8, (0, 1, b"15"), True),
    ("nan(36)", 0, 7, (0, 0, b"N0024"), True),
    ("1200", 0, 4, (0, 2, b"12"), True),
    ("123456789012345678901", 0, 21, (0, 2, b"12345678901234567891"), True),
    ("1e99999999999", 0, 13, (0, 32767, b"1"), True),
])
def test_scan(text, start, end, value, valid):
    assert sane.scan(text.encode(), start) == (sane.Decimal(*value), end, valid)


@pytest.mark.parametrize("fixed, digits, sgn, exp, sig, want", [
    # Table 3-6
    (False, 3, 0, -2, "123", " 1.23e+0"),
    (False, 3, 1, -4, "123", "-1.23e-2"),
    (False, 1, 0, 200, "123", " 1.23e+202"),
    (False, 5, 1, 1000, "123", "-1.2300e+1002"),
    (False, 1, 0, -30, "4", " 4e-30"),
    (False, 1, 1, 0, "0", "-0e+0"),
    (False, 30, 0, 0, "1", " 1." + "0" * 29 + "e+0"),
    (False, 76, 0, 0, "1", "?"),
    (False, 76, 1, 0, "1", "?"),
    (False, 5, 0, -98, "N0024", " NAN(036)"),
    (False, 2, 1, 103, "N0015", "-NAN(021)"),
    (False, 2, 0, 0, "I", " INF"),
    (False, 2, 1, -217, "I", "-INF"),
    (True, 3, 0, -3, "12345", "12.345"),
    (True, 3, 1, -3, "12345", "-12.345"),
    (True, 5, 0, -3, "12345", "12.34500"),
    (True, 3, 1, -5, "1234567", "-12.34567"),
    (True, 0, 0, 0, "12345", "12345"),
    (True, 0, 1, 3, "12345", "-12345000"),
    (True, -2, 0, 2, "12345", "1234500"),
    (True, -2, 1, 1, "12345", "-123450"),
    (True, 3, 0, 63, "0", "0.000"),
    (True, -3, 1, 0, "0", "-0"),
    (True, 5, 0, 74, "1", "?"),
    (True, 4, 1, 74, "1", "?"),
    (True, 5, 0, -98, "N0024", "NAN(036)"),
    (True, 2, 1, 103, "N0015", "-NAN(021)"),
    (True, 2, 0, 0, "I", "INF"),
    (True, 2, 1, -217, "I", "-INF"),
])
def test_format_decimal(fixed, digits, sgn, exp, sig, want):
    d = sane.Decimal(sgn, exp, sig.encode())
    assert sane.format_decimal(fixed, digits, d) == want.encode()


# =========================================================================
# Elementary functions
# =========================================================================
def close(x: sane.Num, want: float) -> bool:
    return math.isclose(sane.to_float(x), want, rel_tol=1e-15, abs_tol=1e-300)


def test_logarithms():
    env = Env()
    assert sane.ln(ONE, env) == ZERO and env.raised == 0
    assert sane.ln(sane.from_int(8), env, base2=True) == THREE and env.raised == 0
    assert close(sane.ln(TWO, env), math.log(2)) and env.raised == INEXACT
    x = num("1.0000001")
    assert close(sane.ln(x, Env()), math.log1p(sane.to_float(sane.sub(x, ONE, Env()))))
    env = Env()
    assert sane.ln(ZERO, env) == MINUS_INF and env.raised == DIVBYZERO
    env = Env()
    r = sane.ln(sane.neg(ONE), env)
    assert sane.nan_code(r) == sane.NAN_LOG and env.raised == INVALID
    assert sane.ln(INF, Env()) == INF
    assert close(sane.ln1(num("1e-10"), Env()), math.log1p(1e-10))
    assert sane.ln1(MINUS_ZERO, Env()) == MINUS_ZERO
    env = Env()
    assert sane.ln1(sane.neg(ONE), env) == MINUS_INF and env.raised == DIVBYZERO


def test_exponentials():
    env = Env()
    assert sane.exp(ZERO, env) == ONE and env.raised == 0
    assert hex_x(sane.exp(sane.from_int(10), env, base2=True)) == "40098000000000000000"
    assert env.raised == 0
    assert close(sane.exp(ONE, env), math.e) and env.raised == INEXACT
    assert sane.exp(MINUS_INF, Env()) == ZERO
    assert sane.exp(MINUS_INF, Env(), minus1=True) == sane.MINUS_ONE
    assert close(sane.exp(num("1e-10"), Env(), minus1=True), math.expm1(1e-10))
    env = Env()
    assert sane.exp(sane.from_int(20000), env) == INF and env.raised == OVERFLOW | INEXACT
    env = Env()
    assert sane.exp(sane.from_int(-20000), env) == ZERO and env.raised == UNDERFLOW | INEXACT
    big = sane.exp(sane.from_int(11000), Env())         # beyond a double, not extended
    assert big.kind == sane.FINITE and close(sane.ln(big, Env()), 11000.0)


def test_powers():
    env = Env()
    r = sane.xpwri(THREE, 40, env)
    assert r.sig << r.exp == 3 ** 40 and env.raised == 0
    assert to_dbl(sane.xpwri(TWO, -3, Env())) == 0.125
    env = Env()
    assert sane.xpwri(MINUS_ZERO, -3, env) == MINUS_INF and env.raised == DIVBYZERO
    assert sane.xpwri(INF, 0, Env()) == ONE
    assert sane.xpwri(QNAN, 0, Env()) == QNAN          # "if x is not a NaN" (chapter 8)
    assert close(sane.xpwry(TWO, num("0.5"), Env()), math.sqrt(2))
    for x, y in ((sane.from_int(-8), num("0.5")), (ZERO, ZERO), (ONE, INF)):
        env = Env()
        r = sane.xpwry(x, y, env)
        assert sane.nan_code(r) == sane.NAN_POWER and env.raised == INVALID
    assert to_dbl(sane.xpwry(sane.from_int(-2), THREE, Env())) == -8.0
    assert sane.xpwry(num("0.5"), INF, Env()) == ZERO


def test_financial_functions():
    rate, periods = num("0.05"), sane.from_int(10)
    assert close(sane.compound(rate, periods, Env()), 1.05 ** 10)
    assert close(sane.annuity(rate, periods, Env()), (1 - 1.05 ** -10) / 0.05)
    assert sane.compound(ZERO, periods, Env()) == ONE
    assert sane.annuity(ZERO, periods, Env()) == periods
    env = Env()
    r = sane.compound(num("-2"), periods, env)
    assert sane.nan_code(r) == sane.NAN_FINAN and env.raised == INVALID
    env = Env()
    assert sane.compound(sane.MINUS_ONE, sane.neg(ONE), env) == INF and env.raised == DIVBYZERO


def test_trigonometric_functions():
    half = num("0.5")
    for name, f in (("sin", math.sin), ("cos", math.cos), ("tan", math.tan)):
        assert close(sane.trig(name, half, Env()), f(0.5)), name
    assert close(sane.atan(half, Env()), math.atan(0.5))
    assert sane.trig("sin", MINUS_ZERO, Env()) == MINUS_ZERO
    assert sane.trig("cos", ZERO, Env()) == ONE
    env = Env()
    r = sane.trig("sin", INF, env)
    assert sane.nan_code(r) == sane.NAN_TRIG and env.raised == INVALID
    # periodic in pi as extended has it, which sin finds exactly
    pi = sane.Num(sane.FINITE, 0, sane.PI_SIG, sane.PI_EXP)
    env = Env()
    assert sane.trig("sin", pi, env).zero and env.raised == 0
    env = Env()
    assert sane.trig("tan", pi._replace(exp=pi.exp - 1), env) == INF and env.raised == DIVBYZERO
    assert hex_x(sane.atan(INF, Env())) == "3fffc90fdaa22168c235"
    tiny = "3f008000000000000000"
    assert hex_x(sane.trig("sin", ext(tiny), Env())) == tiny == hex_x(sane.atan(ext(tiny), Env()))


def test_random_is_the_park_miller_generator():
    x, seen = ONE, []
    for _ in range(4):
        x = sane.random(x, Env())
        seen.append(int(to_dbl(x)))
    assert seen == [16807, 282475249, 1622650073, 984943658]


# =========================================================================
# The packages, called through the processor
# =========================================================================
SCS_SEQ_SGT_SVS = bytes.fromhex("55d057e800015ee8000259e80003")   # Scc (A0), 1(A0), 2(A0), 3(A0)
FP68K, ELEMS68K = 0xA9EB, 0xA9EC


class Machine(Guest):
    """A Guest with a processor, for the packages that take their operands
    from the stack."""

    def __init__(self):
        super().__init__()
        self.cpu = CPU(self.mem)
        self.stack_top = self.ptr(bytes(0x1000)) + 0x1000
        self.cpu.sp = self.stack_top
        self.missing = []

    def unimplemented(self, name, word):
        self.missing.append(name)

    def run(self, code: bytes) -> None:
        """Run code to its end, the traps it makes answered as the runtime
        would."""
        start = self.ptr(code)
        self.cpu.pc = start
        while (ev := self.cpu.run(start + len(code))).kind == "trap":
            self.cpu.pc = ev.pc + 2
            dispatch.dispatch(self, ev.code, ev.pc)
        assert ev.kind == "until", ev

    def package(self, trap: int, opword: int, *operands: int, after: bytes = b"") -> None:
        """Push operands (the destination last) and opword, then call trap."""
        for addr in reversed(operands):
            self.cpu.push32(addr)
        self.cpu.push16(opword)
        self.run(trap.to_bytes(2, "big") + after)
        assert self.cpu.sp == self.stack_top

    def value(self, fmt: int, x) -> int:
        """A pointer to x in fmt (from a Num, an int or a float)."""
        if isinstance(x, float):
            x = dbl(x)
        if isinstance(x, int):
            return self.ptr(x.to_bytes(sane.SIZE[fmt], "big", signed=True))
        return self.ptr(sane.convert(x, fmt, Env()))

    def read(self, fmt: int, p: int) -> sane.Num:
        return sane.unpack(fmt, self.mem.read(p, sane.SIZE[fmt]))

    @property
    def env(self) -> int:
        return self.process.world.r16(lowmem.FPState)

    @env.setter
    def env(self, word: int) -> None:
        self.process.world.w16(lowmem.FPState, word)


@pytest.fixture
def m(qapp):
    machine = Machine()
    yield machine
    machine.cpu.close()


def op(code: int, fmt: int = X) -> int:
    return code | fmt << 11


@pytest.mark.parametrize("a, b, cs_eq_gt_vs", [
    (1.0, 2.0, "ff000000"), (2.0, 1.0, "0000ff00"), (1.0, 1.0, "00ff0000"),
    (math.nan, 1.0, "000000ff"),
])
def test_compare_leaves_the_relation_in_the_ccr(m, a, b, cs_eq_gt_vs):
    """What the manual's FBxx macros branch on: less is C, equal Z,
    greater neither, unordered V."""
    for code in (0x08, 0x0A):
        result = m.ptr(bytes(4))
        m.cpu.set_a(0, result)
        m.package(FP68K, op(code, D), m.value(X, a), m.value(D, b), after=SCS_SEQ_SGT_SVS)
        assert m.mem.read(result, 4).hex() == cs_eq_gt_vs
    assert m.env == (INVALID << 8 if math.isnan(a) else 0)      # FCPX's, not FCMP's


def test_arithmetic_writes_the_destination_and_keeps_the_registers(m):
    dst = m.value(X, 1.5)
    m.cpu.set_d(0, 0x12345678)
    m.cpu.sr = (m.cpu.sr & ~0x1F) | 0x04
    m.package(FP68K, op(0x04, S), dst, m.value(S, ext("4000c000000000000000")))   # FMULS 3
    assert to_dbl(m.read(X, dst)) == 4.5
    assert m.cpu.d(0) == 0x12345678 and m.cpu.sr & 0x1F == 0x04
    m.package(FP68K, op(0x00, I), dst, m.value(I, -5))                            # FADDI
    m.package(FP68K, op(0x06, L), dst, m.value(L, 2))                             # FDIVL
    m.package(FP68K, op(0x02, C), dst, m.value(C, 1))                             # FSUBC
    assert to_dbl(m.read(X, dst)) == -1.25
    assert m.env == 0


def test_conversions_through_fp68k(m):
    dst = m.ptr(bytes(10))
    m.package(FP68K, op(0x0E, D), dst, m.value(D, 0.1))                    # FD2X
    assert m.mem.read(dst, 10).hex() == "3ffbccccccccccccd000"
    single = m.ptr(bytes(4))
    m.package(FP68K, op(0x10, S), single, dst)                             # FX2S
    assert m.mem.read(single, 4) == struct.pack(">f", 0.1)
    assert m.env == INEXACT << 8
    m.env = TOWARDZERO
    word = m.ptr(bytes(2))
    m.package(FP68K, op(0x10, I), word, m.value(X, -2.75))                 # FX2I
    assert m.mem.r16s(word) == -2
    m.env = 0
    m.package(FP68K, op(0x16), dst)                                        # FTINTX
    m.package(FP68K, op(0x10, X), dst, m.value(X, 7.75))                   # FX2X
    m.package(FP68K, op(0x16), dst)
    assert to_dbl(m.read(X, dst)) == 7.0


def test_remainder_returns_the_quotient_in_d0(m):
    dst = m.value(X, -5.0)
    m.cpu.set_d(0, 0xABCD0000)
    m.package(FP68K, op(0x0C, D), dst, m.value(D, 3.0))
    assert to_dbl(m.read(X, dst)) == 1.0
    assert m.cpu.d(0) == 0xABCDFFFE                     # -2, in the low word


def test_one_operand_operations(m):
    x = m.value(X, 2.0)
    m.package(FP68K, op(0x12), x)                                       # FSQRTX
    assert to_dbl(m.read(X, x)) == math.sqrt(2)
    m.package(FP68K, op(0x0D), x)                                       # FNEGX
    m.package(FP68K, op(0x14), x)                                       # FRINTX
    assert to_dbl(m.read(X, x)) == -1.0
    m.package(FP68K, op(0x0F), x)                                       # FABSX
    assert to_dbl(m.read(X, x)) == 1.0
    m.package(FP68K, op(0x18), x, m.value(I, 4))                        # FSCALBX
    m.package(FP68K, op(0x1A), x)                                       # FLOGBX
    assert to_dbl(m.read(X, x)) == 4.0
    sign = m.value(X, -0.0)
    m.package(FP68K, op(0x11), sign, x)                                 # FCPYSGNX
    assert to_dbl(m.read(X, x)) == -4.0
    cls = m.ptr(bytes(2))
    m.package(FP68K, op(0x1C, D), cls, m.value(D, -5e-324))             # FCLASSD
    assert m.mem.r16s(cls) == -6
    after = m.value(D, 1.0)
    m.package(FP68K, op(0x13, D), m.value(D, 0.0), after)               # FNEXTD
    assert to_dbl(m.read(D, after)) == math.nextafter(1.0, 0.0)


def test_decimal_conversions_through_fp68k(m):
    decimal, form = m.ptr(bytes(26)), m.ptr(bytes([0, 0, 0, 10]))       # FloatDecimal, 10 digits
    m.package(FP68K, op(0x0B, D), decimal, m.value(D, 2 / 3), form)     # FD2DEC
    assert (m.mem.r8(decimal), m.mem.r16s(decimal + 2), m.mem.r_pstr(decimal + 4)) == \
        (0, -10, b"6666666667")
    dst = m.ptr(bytes(8))
    m.package(FP68K, op(0x09, D), dst, decimal)                         # FDEC2D
    assert to_dbl(m.read(D, dst)) == 0.6666666667


def test_environment_calls(m):
    word = m.ptr(struct.pack(">H", UPWARD | DIVBYZERO << 8))
    m.package(FP68K, 0x0001, word)                                      # FSETENV
    assert m.env == UPWARD | DIVBYZERO << 8
    got = m.ptr(bytes(2))
    m.package(FP68K, 0x0003, got)                                       # FGETENV
    assert m.mem.r16(got) == m.env
    test = m.ptr(struct.pack(">H", 3))                                  # divide-by-zero's number
    m.package(FP68K, 0x001B, test)                                      # FTESTXCP
    assert m.mem.r8(test) == 1
    saved = m.ptr(bytes(2))
    m.package(FP68K, 0x0017, saved)                                     # FPROCENTRY
    assert m.mem.r16(saved) == UPWARD | DIVBYZERO << 8 and m.env == 0
    m.package(FP68K, 0x0015, m.ptr(struct.pack(">H", 4)))               # FSETXCP inexact
    assert m.env == INEXACT << 8
    m.package(FP68K, 0x0019, saved)                                     # FPROCEXIT
    assert m.env == UPWARD | (DIVBYZERO | INEXACT) << 8
    vector = m.ptr(struct.pack(">I", 0x123456))
    m.package(FP68K, 0x0005, vector)                                    # FSETHV
    m.package(FP68K, 0x0007, got)                                       # FGETHV
    assert m.mem.read(got, 4) == bytes.fromhex("00123456")


def test_an_enabled_exception_halts_after_delivering_the_result(m):
    vector = 0x7000
    m.process.world.w32(lowmem.FPState + 2, vector)
    m.env = 1 << 3                                                      # halt on divide-by-zero
    dst, seen = m.value(X, 1.0), []

    def halt(misc, src2, src, dst_ptr, opword):
        seen.append((m.mem.r16(misc), dst_ptr, opword, to_dbl(m.read(X, dst_ptr))))
        m.mem.w16(misc + 2, 0x04)                                       # the CCR to return

    m.procs[vector] = halt
    m.package(FP68K, op(0x06, D), dst, m.value(D, 0.0))
    assert seen == [(DIVBYZERO, dst, op(0x06, D), math.inf)]
    assert m.cpu.sr & 0x1F == 0x04
    assert m.env == 1 << 3 | DIVBYZERO << 8
    m.package(FP68K, 0x0001, m.ptr(struct.pack(">H", 1 << 3 | DIVBYZERO << 8)))  # FSETENV
    assert len(seen) == 1                                               # doesn't halt


def test_an_unknown_opword_is_reported_and_popped(m):
    m.package(FP68K, 0x001E, m.ptr(bytes(10)), m.ptr(bytes(10)))
    assert m.missing == ["FP68K opword $001E"]


def test_elementary_functions_through_elems68k(m):
    x = m.value(X, 0.5)
    m.package(ELEMS68K, 0x0018, x)                                      # FSINX
    assert close(m.read(X, x), math.sin(0.5))
    assert m.env == INEXACT << 8
    y = m.value(X, 3.0)
    m.package(ELEMS68K, 0x8010, y, m.value(I, 3))                       # FXPWRI
    assert to_dbl(m.read(X, y)) == 27.0
    r = m.ptr(bytes(10))
    m.package(ELEMS68K, 0xC014, r, m.value(X, 10.0), m.value(X, num("0.05")))    # FCOMPOUND
    assert close(m.read(X, r), 1.05 ** 10)
    m.package(ELEMS68K, 0x8040, r, r)
    assert m.missing == ["Elems68K opword $8040"]


def test_trace_names_are_the_manuals(m):
    m.cpu.push32(0)
    m.cpu.push32(0)
    m.cpu.push16(op(0x04, D))
    assert dispatch.describe(m, FP68K) == "FP68K FMULD"
    m.mem.w16(m.cpu.sp, op(0x0E, C))
    assert dispatch.describe(m, FP68K) == "FP68K FC2X"
    m.mem.w16(m.cpu.sp, 0x0018)
    assert dispatch.describe(m, ELEMS68K) == "Elems68K FSINX"
    m.mem.w16(m.cpu.sp, 0x0003)
    assert dispatch.describe(m, 0xA9EE) == "Dec2Str"
    assert dispatch.trap_name(FP68K) == "FP68K"


# =========================================================================
# DecStr68K
# =========================================================================
@pytest.fixture
def g(qapp):
    return Guest()


def test_pstr2dec_and_cstr2dec_scan_from_the_index(g):
    index, decimal, valid = g.ptr(struct.pack(">h", 2)), g.ptr(bytes(26)), g.ptr(bytes(2))
    g("PStr2Dec", g.ptr(b"\x07x12E-3X"), index, decimal, valid)
    assert g.mem.r16s(index) == 7
    assert (g.mem.r8(decimal), g.mem.r16s(decimal + 2), g.mem.r_pstr(decimal + 4)) == \
        (0, -3, b"12")
    assert g.mem.r8(valid) == 0
    g.mem.w16(index, 1)
    g("CStr2Dec", g.ptr(b" -.5\0"), index, decimal, valid)
    assert g.mem.r16s(index) == 4
    assert (g.mem.r8(decimal), g.mem.r16s(decimal + 2), g.mem.r_pstr(decimal + 4)) == \
        (1, -1, b"5")
    assert g.mem.r8(valid) == 1


def test_dec2str(g):
    form, decimal, out = g.ptr(bytes([1, 0, 0, 2])), g.ptr(bytes(26)), g.ptr(bytes(256))
    g.mem.w16(decimal + 2, -3)
    g.mem.w_pstr(decimal + 4, b"12345")
    g("Dec2Str", form, decimal, out)
    assert g.str255(out) == "12.345"
