"""
SANE's packages (Apple Numerics Manual, second edition, part III):
FP68K (Pack4), the arithmetic, conversions and environment; Elems68K
(Pack5), the elementary functions; and DecStr68K, Pack7's scanners and
formatter.  The numbers are macrt.toolbox.sane's; this is the calling
convention.

An FP68K or Elems68K call pushes its operands' addresses, the
destination's last, then an opword: its low byte is the operation, bits
11-13 the source's format.  The package pops them all.  The environment
word and the halt vector live in FPState, so each application has its
own.  A comparison returns its relation in the CCR, Remainder part of
the quotient in D0; nothing else changes a register.
"""

from __future__ import annotations

import struct

from ..memory import ADDRESS_MASK
from ..toolbox import lowmem, sane
from ..toolbox.sane import FLOATS, SIZE, X, Env
from .dispatch import namer, raw, trap

# FP68K's operations, by the opword's low byte: the manual's name (? for
# the format's letter) and how many operand addresses they take.
FP_OPS = {
    0x00: ("FADD?", 2), 0x02: ("FSUB?", 2), 0x04: ("FMUL?", 2), 0x06: ("FDIV?", 2),
    0x08: ("FCMP?", 2), 0x0A: ("FCPX?", 2), 0x0C: ("FREM?", 2), 0x0E: ("F?2X", 2),
    0x10: ("FX2?", 2), 0x12: ("FSQRTX", 1), 0x14: ("FRINTX", 1), 0x16: ("FTINTX", 1),
    0x18: ("FSCALBX", 2), 0x1A: ("FLOGBX", 1), 0x1C: ("FCLASS?", 2),
    0x0D: ("FNEGX", 1), 0x0F: ("FABSX", 1), 0x11: ("FCPYSGNX", 2), 0x13: ("FNEXT?", 2),
    0x09: ("FDEC2?", 2), 0x0B: ("F?2DEC", 3),
    0x01: ("FSETENV", 1), 0x03: ("FGETENV", 1), 0x05: ("FSETHV", 1), 0x07: ("FGETHV", 1),
    0x15: ("FSETXCP", 1), 0x17: ("FPROCENTRY", 1), 0x19: ("FPROCEXIT", 1),
    0x1B: ("FTESTXCP", 1),
}
# The formats each operation takes, where it isn't any of them
FORMATS = {0x1C: (sane.X, sane.D, sane.S, sane.C), 0x13: (sane.X, sane.D, sane.S)}

ELEMS_OPS = {
    0x00: ("FLNX", 1), 0x02: ("FLOG2X", 1), 0x04: ("FLN1X", 1), 0x06: ("FLOG21X", 1),
    0x08: ("FEXPX", 1), 0x0A: ("FEXP2X", 1), 0x0C: ("FEXP1X", 1), 0x0E: ("FEXP21X", 1),
    0x10: ("FXPWRI", 2), 0x12: ("FXPWRY", 2), 0x14: ("FCOMPOUND", 3), 0x16: ("FANNUITY", 3),
    0x18: ("FSINX", 1), 0x1A: ("FCOSX", 1), 0x1C: ("FTANX", 1), 0x1E: ("FATANX", 1),
    0x20: ("FRANDX", 1),
}

# The CCR after a comparison: DST > SRC, DST < SRC (X, N and C), equal (Z), unordered (V)
RELATION_CCR = {sane.GREATER: 0x00, sane.LESS: 0x19, sane.EQUAL: 0x04, sane.UNORDERED: 0x02}
CCR = 0x1F


def fp_name(op: int) -> str | None:
    """The manual's name for an FP68K opword, such as FMULX or FD2X;
    None for one FP68K doesn't have."""
    entry = FP_OPS.get(op & 0xFF)
    if entry is None:
        return None
    name, fmt = entry[0], (op >> 11) & 7
    if "?" not in name:
        return name
    if fmt not in FORMATS.get(op & 0xFF, SIZE):
        return None
    return name.replace("?", sane.LETTER[fmt])


@namer(0xA9EB)
def _fp_trace_name(mem, sp: int) -> str:
    op = mem.r16(sp)
    return f"FP68K {fp_name(op) or f'opword ${op:04X}'}"


@namer(0xA9EC)
def _elems_trace_name(mem, sp: int) -> str:
    op = mem.r16(sp)
    entry = ELEMS_OPS.get(op & 0xFF)
    return f"Elems68K {entry[0] if entry else f'opword ${op:04X}'}"


# =========================================================================
# The environment
# =========================================================================
def _env(t) -> tuple[int, int]:
    """The environment word and the halt vector."""
    return struct.unpack(">HI", t.process.world.read(lowmem.FPState, 6))


def _set_env(t, word: int) -> None:
    t.process.world.w16(lowmem.FPState, word)


def _finish(t, env: Env, op: int, addrs: list[int], ccr: int | None = None,
            d0: int | None = None) -> None:
    """Record the exceptions an operation raised, halt if one's enabled,
    and leave the CCR and D0 as the operation says."""
    cpu = t.cpu
    word, vector = _env(t)
    word |= env.raised << 8
    _set_env(t, word)
    ccr = cpu.sr & CCR if ccr is None else ccr
    d0 = cpu.d(0) if d0 is None else d0
    halts = env.raised & word & 0x1F
    if halts and vector & ADDRESS_MASK:
        ccr, d0 = _halt(t, vector, halts, op, addrs, ccr, d0)
    cpu.sr = (cpu.sr & ~CCR) | (ccr & CCR)
    cpu.set_d(0, d0 & 0xFFFFFFFF)


def _halt(t, vector: int, halts: int, op: int, addrs: list[int], ccr: int,
          d0: int) -> tuple[int, int]:
    """Call the halt procedure as procedure(VAR misc; src2, src, dst: Ptr;
    opcode: INTEGER), the result already delivered.  misc is the halt's
    exceptions, then the CCR and D0 the operation would return, which it
    can change."""
    cpu, mem = t.cpu, t.mem
    dst, src, src2 = (addrs + [0, 0, 0])[:3]
    sp = cpu.sp
    misc = sp - 8
    cpu.sp = misc
    mem.w16(misc, halts)
    mem.w16(misc + 2, ccr)
    mem.w32(misc + 4, d0)
    t.call(vector, (("p", misc), ("p", src2), ("p", src), ("p", dst), ("h", op)))
    ccr, d0 = mem.r16(misc + 2), mem.r32(misc + 4)
    cpu.sp = sp
    return ccr, d0


# =========================================================================
# Operands in memory
# =========================================================================
def _read(t, fmt: int, addr: int) -> sane.Num:
    return sane.unpack(fmt, t.mem.read(addr, SIZE[fmt]))


def _read_decimal(t, addr: int) -> sane.Decimal:
    """A decimal record: sgn, a byte; exp, a word at 2; sig, a string at 4."""
    mem = t.mem
    return sane.Decimal(int(mem.r8(addr) != 0), mem.r16s(addr + 2), mem.r_pstr(addr + 4))


def _write_decimal(t, addr: int, d: sane.Decimal) -> None:
    mem = t.mem
    mem.w8(addr, d.sgn)
    mem.w16(addr + 2, d.exp)
    mem.w_pstr(addr + 4, d.sig)


def _pop_operands(t, count: int) -> tuple[int, list[int]]:
    """The opword and the operands' addresses, destination first; popped."""
    cpu, mem = t.cpu, t.mem
    sp = cpu.sp
    op = mem.r16(sp)
    addrs = [mem.r32(sp + 2 + 4 * i) for i in range(count)]
    cpu.sp = sp + 2 + 4 * count
    return op, addrs


# =========================================================================
# FP68K
# =========================================================================
ARITHMETIC = {0x00: sane.add, 0x02: sane.sub, 0x04: sane.mul, 0x06: sane.div}
ONE_OPERAND = {0x12: sane.sqrt, 0x14: sane.round_integral,
               0x16: lambda x, env: sane.round_integral(x, env, sane.TOWARDZERO),
               0x1A: sane.logb}


@raw(0xA9EB)
def FP68K(t, word):
    mem = t.mem
    op = mem.r16(t.cpu.sp)
    code, fmt = op & 0xFF, (op >> 11) & 7
    name = fp_name(op)
    entry = FP_OPS.get(code)
    op, addrs = _pop_operands(t, entry[1] if entry else 2)
    if name is None:
        t.unimplemented(f"FP68K opword ${op:04X}", word)
        return
    env_word, vector = _env(t)
    env = Env(env_word)
    dst, src = (addrs + [0])[:2]
    ccr = d0 = None
    if code in ARITHMETIC:
        r = ARITHMETIC[code](_read(t, X, dst), _read(t, fmt, src), env)
        mem.write(dst, sane.convert(r, X, env))
    elif code in (0x08, 0x0A):
        rel = sane.compare(_read(t, X, dst), _read(t, fmt, src), env, code == 0x0A)
        ccr = RELATION_CCR[rel]
    elif code == 0x0C:
        r, quo = sane.rem(_read(t, X, dst), _read(t, fmt, src), env)
        mem.write(dst, sane.convert(r, X, env))
        n = abs(quo) & 0x7F
        d0 = (t.cpu.d(0) & 0xFFFF0000) | ((-n if quo < 0 else n) & 0xFFFF)
    elif code == 0x0E:
        mem.write(dst, sane.convert(_read(t, fmt, src), X, env))
    elif code == 0x10:
        mem.write(dst, sane.convert(_read(t, X, src), fmt, env))
    elif code in ONE_OPERAND:
        mem.write(dst, sane.convert(ONE_OPERAND[code](_read(t, X, dst), env), X, env))
    elif code == 0x18:
        mem.write(dst, sane.convert(sane.scalb(mem.r16s(src), _read(t, X, dst), env), X, env))
    elif code == 0x1C:
        mem.w16(dst, sane.classify(_read(t, fmt, src), FLOATS.get(fmt)))
    elif code in (0x0D, 0x0F):          # the sign bit, whatever the number
        mem.w8(dst, mem.r8(dst) ^ 0x80 if code == 0x0D else mem.r8(dst) & 0x7F)
    elif code == 0x11:
        mem.w8(src, (mem.r8(src) & 0x7F) | (mem.r8(dst) & 0x80))
    elif code == 0x13:
        r = sane.next_after(_read(t, fmt, src), _read(t, fmt, dst), FLOATS[fmt], env)
        mem.write(src, sane.encode(fmt, r))
    elif code == 0x09:
        mem.write(dst, sane.convert(sane.from_decimal(_read_decimal(t, src)), fmt, env))
    elif code == 0x0B:
        form = addrs[2]
        d = sane.to_decimal(_read(t, fmt, src), mem.r8(form) != 0, mem.r16s(form + 2), env)
        _write_decimal(t, dst, d)
    else:
        _environment(t, code, dst, env_word, vector, env)
        if code in (0x01, 0x03, 0x05, 0x07, 0x17, 0x1B):
            return
    _finish(t, env, op, addrs, ccr, d0)


def _environment(t, code: int, addr: int, word: int, vector: int, env: Env) -> None:
    """The calls on the environment.  Set-Environment doesn't halt;
    Set-Exception and Procedure-Exit signal their exceptions in env."""
    mem, world = t.mem, t.process.world
    if code == 0x01:
        _set_env(t, mem.r16(addr))
    elif code == 0x03:
        mem.w16(addr, word)
    elif code == 0x05:
        world.w32(lowmem.FPState + 2, mem.r32(addr))
    elif code == 0x07:
        mem.w32(addr, vector)
    elif code == 0x15:
        n = mem.r16(addr)
        if n < 5:
            env.signal(1 << n)
    elif code == 0x17:
        mem.w16(addr, word)
        _set_env(t, 0)
    elif code == 0x19:
        _set_env(t, mem.r16(addr))
        env.signal((word >> 8) & 0x1F)
    elif code == 0x1B:
        n = mem.r16(addr)
        mem.w8(addr, int(n < 5 and bool(word & (0x100 << n))))


# =========================================================================
# Elems68K
# =========================================================================
ELEMENTARY = {
    0x00: lambda x, env: sane.ln(x, env), 0x02: lambda x, env: sane.ln(x, env, base2=True),
    0x04: lambda x, env: sane.ln1(x, env), 0x06: lambda x, env: sane.ln1(x, env, base2=True),
    0x08: lambda x, env: sane.exp(x, env),
    0x0A: lambda x, env: sane.exp(x, env, base2=True),
    0x0C: lambda x, env: sane.exp(x, env, minus1=True),
    0x0E: lambda x, env: sane.exp(x, env, base2=True, minus1=True),
    0x18: lambda x, env: sane.trig("sin", x, env), 0x1A: lambda x, env: sane.trig("cos", x, env),
    0x1C: lambda x, env: sane.trig("tan", x, env), 0x1E: sane.atan, 0x20: sane.random,
}


@raw(0xA9EC)
def Elems68K(t, word):
    mem = t.mem
    op = mem.r16(t.cpu.sp)
    code = op & 0xFF
    entry = ELEMS_OPS.get(code)
    # The opword's top bits say how many operands there are: $8000 two, $C000 three.
    op, addrs = _pop_operands(t, entry[1] if entry else 1 + (op >> 15) + ((op >> 14) & 1))
    if entry is None:
        t.unimplemented(f"Elems68K opword ${op:04X}", word)
        return
    env = Env(_env(t)[0])
    dst = addrs[0]
    x = _read(t, X, dst)
    if code == 0x10:
        r = sane.xpwri(x, mem.r16s(addrs[1]), env)
    elif code == 0x12:
        r = sane.xpwry(x, _read(t, X, addrs[1]), env)
    elif code in (0x14, 0x16):
        rate, periods = _read(t, X, addrs[2]), _read(t, X, addrs[1])
        r = (sane.compound if code == 0x14 else sane.annuity)(rate, periods, env)
    else:
        r = ELEMENTARY[code](x, env)
    mem.write(dst, sane.convert(r, X, env))
    _finish(t, env, op, addrs)


# =========================================================================
# DecStr68K
# =========================================================================
def _cstr(t, addr: int) -> bytes:
    """A C string, without its NUL."""
    mem, out = t.mem, bytearray()
    while True:
        chunk = mem.read(addr + len(out), min(256, mem.size - ((addr + len(out)) & ADDRESS_MASK)))
        n = chunk.find(0)
        if n >= 0 or not chunk:
            return bytes(out + chunk[:max(n, 0)])
        out += chunk


def _scan(t, text: bytes, index_ptr: int, d: int, valid_ptr: int, origin: int) -> None:
    start = max(t.mem.r16s(index_ptr) - origin, 0)
    dec, end, valid = sane.scan(text, start)
    t.mem.w16(index_ptr, end + origin)
    _write_decimal(t, d, dec)
    t.mem.w8(valid_ptr, int(valid))


@trap
def PStr2Dec(t, s, index_ptr, d, valid_ptr):
    """index counts from 1, the string's first character."""
    _scan(t, t.mem.read(s, t.mem.r8(s) + 1), index_ptr, d, valid_ptr, 0)


@trap
def CStr2Dec(t, s, index_ptr, d, valid_ptr):
    start = max(t.mem.r16s(index_ptr), 0)
    _scan(t, _cstr(t, s + start), index_ptr, d, valid_ptr, start)


@trap
def Dec2Str(t, f, d, s):
    fixed, digits = t.mem.r8(f) != 0, t.mem.r16s(f + 2)
    t.set_str255(s, sane.format_decimal(fixed, digits, _read_decimal(t, d)))
