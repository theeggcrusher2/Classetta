"""
The A-line trap dispatcher: what happens when 68k code executes a word
from $A000 to $AFFF.

Toolbox traps ($A800-$AFFF) take Pascal arguments on the stack: space
for the result first, then the arguments left to right, so the last is
on top.  The routine pops its arguments and leaves the result.  Bit 10
is auto-pop: the trap was reached by a JSR to glue, and it returns to
the address on top of the stack instead.  The trap number is the low 10
bits.

OS traps ($A000-$A7FF) take their arguments in registers, mostly A0 and
D0, and return a result in D0 (and in A0 when bit 8 is set).  Bits 9 and
10 are flags for the routine: SYS and CLEAR for the Memory Manager, HFS
and ASYNC for the File Manager.  The trap number is the low 8 bits.
After the routine the dispatcher tests D0.W, so code can branch on the
result.

Every trap's layout comes from trapdefs, generated from the Multiversal
Interfaces.  An implementation is a Python function registered with
@trap under its Inside Macintosh name.  It gets the trap context (see
runtime.Runner) and the arguments as Python ints, a Point for a Point,
in the order Inside Macintosh lists them, and returns the result.  An
Out<reg> argument isn't passed; the function returns (result, out...)
instead.  @raw registers a function that takes the trap context and the
trap word and works the registers and stack itself.

A trap with no implementation still has its arguments popped by its
signature and a zero result, unimpErr for an OSErr, and is logged once
by name.  One the interfaces don't describe stops the application: there
is no telling how much of the stack is its.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Callable

from ..memory import BusError
from ..toolbox import lowmem
from ..toolbox.types import Point
from . import trapdefs

log = logging.getLogger("macrt.traps")

SIZES = {"b": 2, "B": 2, "h": 2, "H": 2, "e": 2, "l": 4, "L": 4, "p": 4, "P": 4, "E": 4}
TRAP_BITS = {"SYSBIT": 0x400, "CLRBIT": 0x200}
unimpErr = -4

# Traps the interfaces leave out or describe only as glue.
EXTRA_FUNCTIONS = {
    "LoadSeg": (0xA9F0, None, None, "h", "", (), None, ""),
    "UnloadSeg": (0xA9F1, None, None, "p", "", (), None, ""),
    "Chain": (0xA9F3, None, None, "", "e", (("A0", "p"),), "D0", ""),
    # Its parameter block's in A0 (IM II-29).
    "InitZone": (0xA019, None, None, "", "", (("A0", "p"),), None, "ReturnMemErr<D0>"),
    "PostEvent": (0xA02F, None, None, "", "e", (("A0", "h"), ("D0", "l")), "D0", ""),
    "Unimplemented": (0xA89F, None, None, "", "", (), None, ""),
    # The 64K ROM's, gone from later interfaces (IM I-121).
    "AddReference": (0xA9AC, None, None, "php", "", (), None, ""),
    "RmveReference": (0xA9AE, None, None, "p", "", (), None, ""),
    # Its glue pushes -1 after the pointer (MOVEQ #-1,D0; MOVE.L D0,-(SP)).
    "GetFrontProcess": (0xA88F, "OSDispatch", 0x39, "pl", "e", (), None, ""),
    # Not a trap: what GetStdFilterProc's procedure calls (see Machine).
    "StdFilterProc": (0xAA68, "DialogDispatch", 0x067F, "ppp", "b", (), None, ""),
    # Not traps either: what the System's standard definition procedures'
    # code calls (selectors as toolbox.system.STANDARD_DEFPROCS has them).
    "SystemWDEF0": (0xAA68, "DialogDispatch", 0x067E, "hphL", "l", (), None, ""),
    "SystemWDEF1": (0xAA68, "DialogDispatch", 0x067D, "hphL", "l", (), None, ""),
    "SystemMDEF0": (0xAA68, "DialogDispatch", 0x097C, "hppPp", "", (), None, ""),
    "SystemCDEF0": (0xAA68, "DialogDispatch", 0x067B, "hphL", "l", (), None, ""),
    "SystemCDEF1": (0xAA68, "DialogDispatch", 0x067A, "hphL", "l", (), None, ""),
    "SystemLDEF0": (0xAA68, "DialogDispatch", 0x0A79, "hbpPhhp", "", (), None, ""),
    "SystemMBDF0": (0xAA68, "DialogDispatch", 0x0578, "hhhL", "l", (), None, ""),
    # The interfaces leave out textLength, which the selector counts.
    "HiliteText": (0xA8B5, "ScriptUtil", 0x800E001C, "phhhp", "", (), None, ""),
    # LowercaseText and the rest push the routine's word last.
    "TextUtilFunctions": (0xA8B5, "ScriptUtil", 0x800AFFB6, "phhh", "", (), None, ""),
}
STD_FILTER_SELECTOR = 0x067F
EXTRA_NAMES = {0xA02F: "PostEvent", 0xA89F: "Unimplemented", 0xA9F2: "Launch",
               0xA9EB: "FP68K", 0xA9EC: "Elems68K"}
# Inside Macintosh's names for routines the interfaces name otherwise.
RENAMED = {"ROMlib_Fpstr2dec": "PStr2Dec", "ROMlib_Fcstr2dec": "CStr2Dec",
           "ROMlib_Fdec2str": "Dec2Str", "IsMetric": "IUMetric",
           "GetIntlResource": "IUGetIntl", "SetIntlResource": "IUSetIntl",
           "ClearIntlResourceCache": "IUClearCache", "GetIntlResourceTable": "IUGetItlTable"}


def is_toolbox(word: int) -> bool:
    return bool(word & 0x0800)


def trap_key(word: int) -> int:
    """The trap word with its flag bits cleared: which routine it is."""
    return (0xA800 | (word & 0x3FF)) if word & 0x0800 else (0xA000 | (word & 0xFF))


@dataclass(frozen=True)
class Signature:
    name: str
    trap: int
    dispatcher: str | None
    selector: int | None
    stack: str                  # argument codes, declaration order
    result: str
    regs: tuple                 # (register, code) pairs
    result_reg: str | None
    extras: str

    @property
    def stack_bytes(self) -> int:
        return sum(SIZES[c] for c in self.stack)

    @property
    def outs(self) -> list[str]:
        return [r[4:-1] for r, _ in self.regs if r.startswith("Out<") or r.startswith("InOut<")]

    @property
    def os_style(self) -> bool:
        """Registers in, D0 out: everything but Pascal stack calls."""
        return bool(self.regs) or self.result_reg is not None or not is_toolbox(self.trap)


def _signatures() -> dict[str, Signature]:
    table = {**trapdefs.FUNCTIONS, **EXTRA_FUNCTIONS}
    return {RENAMED.get(name, name): Signature(RENAMED.get(name, name), *row)
            for name, row in table.items()}


SIGNATURES = _signatures()
BY_TRAP: dict[int, Signature] = {}
# key -> (name, location, selectors)
DISPATCHERS: dict[int, tuple[str, str, dict[int, Signature]]] = {}
for _d_name, (_d_trap, _d_loc) in trapdefs.DISPATCHERS.items():
    DISPATCHERS[trap_key(_d_trap)] = (_d_name, _d_loc, {})
for _sig in SIGNATURES.values():
    if _sig.dispatcher:
        _d = DISPATCHERS.get(trap_key(trapdefs.DISPATCHERS[_sig.dispatcher][0]))
        if _d is not None and _sig.selector is not None:
            _d[2].setdefault(_sig.selector, _sig)
    else:
        BY_TRAP.setdefault(trap_key(_sig.trap), _sig)
NAMES = {trap_key(t): n for t, n in trapdefs.TRAP_NAMES.items()}
NAMES.update({trap_key(s.trap): s.name for s in BY_TRAP.values()})
NAMES.update({trap_key(t): n for t, n in EXTRA_NAMES.items()})
for _key, (_d_name, _, _) in DISPATCHERS.items():
    NAMES.setdefault(_key, _d_name)


def trap_name(word: int) -> str:
    return NAMES.get(trap_key(word), f"${word:04X}")


# =========================================================================
# The registry
# =========================================================================
Impl = Callable[..., object]
IMPLEMENTATIONS: dict[str, Impl] = {}
RAW: dict[int, Callable] = {}
NAMERS: dict[int, Callable] = {}
RESERR_FLAGS: set[int] = set()      # trap keys that leave the flags as ResErr sets them


def trap(fn: Impl) -> Impl:
    """Register fn as the implementation of the trap it's named after."""
    if fn.__name__ not in SIGNATURES:
        raise KeyError(f"no trap called {fn.__name__}")
    IMPLEMENTATIONS[fn.__name__] = fn
    return fn


def raw(word: int):
    """Register a function that handles a trap word on its own: it gets
    the trap context and the trap word, with PC already past the trap."""
    def register(fn):
        RAW[trap_key(word)] = fn
        return fn
    return register


def namer(word: int):
    """Register a function that names the routine a call to a @raw
    package is for; it gets memory and the address of the arguments."""
    def register(fn):
        NAMERS[trap_key(word)] = fn
        return fn
    return register


def implemented(key: int) -> bool:
    """Is there anything behind this trap?  (What GetTrapAddress tells
    TrapAvailable.)"""
    if key in RAW:
        return True
    sig = BY_TRAP.get(key)
    if sig is not None:
        return sig.name in IMPLEMENTATIONS
    d = DISPATCHERS.get(key)
    return d is not None and any(s.name in IMPLEMENTATIONS for s in d[2].values())


# =========================================================================
# Calling an implementation
# =========================================================================
class UnknownTrap(Exception):
    def __init__(self, word: int, pc: int, detail: str = ""):
        self.word, self.pc = word, pc
        super().__init__(f"unimplemented trap {trap_name(word)} (${word:04X}) at ${pc:06X}"
                         + (f": {detail}" if detail else ""))


def _read_reg(cpu, reg: str, code: str, word: int):
    if reg.startswith("TrapBit<"):
        mask = reg[8:-1]
        return bool(word & (TRAP_BITS[mask] if mask in TRAP_BITS else int(mask, 0)))
    if reg.startswith("InOut<"):
        reg = reg[6:-1]
    if reg == "D0LowWord":
        v = cpu.d(0) & 0xFFFF
        return v - 0x10000 if code == "h" and v & 0x8000 else v
    if reg == "D0HighWord":
        v = cpu.d(0) >> 16
        return v - 0x10000 if code == "h" and v & 0x8000 else v
    v = cpu.d(int(reg[1])) if reg[0] == "D" else cpu.a(int(reg[1]))
    return _from_long(v, code)


def _from_long(v: int, code: str):
    if code in "hHe":
        v &= 0xFFFF
        return v - 0x10000 if code != "H" and v & 0x8000 else v
    if code in "bB":
        v &= 0xFF
        return v - 0x100 if code == "b" and v & 0x80 else v
    if code == "P":
        return Point(_sign16(v >> 16), _sign16(v))
    if code in "lE":
        return v - 0x100000000 if v & 0x80000000 else v
    return v


def _to_long(v, code: str) -> int:
    if v is None or v is False:
        return 0
    if v is True:
        return 1
    if code == "P":
        return ((v.v & 0xFFFF) << 16) | (v.h & 0xFFFF)
    if hasattr(v, "addr"):
        v = v.addr
    return int(v) & 0xFFFFFFFF


def _sign16(v: int) -> int:
    v &= 0xFFFF
    return v - 0x10000 if v & 0x8000 else v


def _write_reg(cpu, reg: str, code: str, value) -> None:
    v = _to_long(value, code)
    if code in "hHe" and v & 0x8000 and code != "H":
        v |= 0xFFFF0000             # an OSErr comes back sign-extended, as from MOVEQ
    elif code in "hHe":
        v &= 0xFFFF
    if reg[0] == "D":
        cpu.set_d(int(reg[1]), v)
    else:
        cpu.set_a(int(reg[1]), v)


def pop_args(mem, sp: int, codes: str) -> tuple[list, int]:
    """Pascal arguments from the stack, in declaration order, and the
    address just past them (where the result goes)."""
    args = [None] * len(codes)
    at = sp
    for i in range(len(codes) - 1, -1, -1):
        c = codes[i]
        if c in "hHe":
            args[i] = mem.r16(at) if c == "H" else mem.r16s(at)
            at += 2
        elif c == "b":
            args[i] = mem.r8s(at)
            at += 2
        elif c == "B":
            args[i] = mem.r8(at)
            at += 2
        elif c == "P":
            args[i] = Point(mem.r16s(at), mem.r16s(at + 2))
            at += 4
        elif c in "lE":
            args[i] = mem.r32s(at)
            at += 4
        else:
            args[i] = mem.r32(at)
            at += 4
    return args, at


def put_result(mem, at: int, code: str, value) -> None:
    v = _to_long(value, code)
    if code in "bB":
        # The high byte of the word, and the low byte cleared: code that
        # leaves the slot as it found it tests the whole word (MacWrite
        # does after IsDialogEvent), and the ROM's routines clear it.
        mem.w16(at, (v & 0xFF) << 8)
    elif code in "hHe":
        mem.w16(at, v)
    elif code:
        mem.w32(at, v)


def call(t, sig: Signature, word: int) -> None:
    """Run sig's implementation, or skip it, with the CPU at the trap's
    arguments and PC already where the trap returns to."""
    cpu, mem = t.cpu, t.mem
    impl = IMPLEMENTATIONS.get(sig.name)
    sp = cpu.sp
    args, result_at = pop_args(mem, sp, sig.stack)
    for reg, code in sig.regs:
        if not reg.startswith("Out<"):
            args.append(_read_reg(cpu, reg, code, word))
    if impl is None:
        t.unimplemented(sig.name, word)
        value = unimpErr if sig.result in ("e", "E") else 0
        outs = [0] * len(sig.outs)
    else:
        value = impl(t, *args)
        outs = []
        if sig.outs:
            value, *outs = value
    # Pascal: drop the arguments, leave the result
    if sig.stack:
        cpu.sp = result_at
    if sig.result and not sig.os_style:
        put_result(mem, result_at, sig.result, value)
    out_regs = [r for r, _ in sig.regs if r.startswith("Out<") or r.startswith("InOut<")]
    for reg, out in zip(out_regs, outs):
        code = dict(sig.regs)[reg]
        _write_reg(cpu, reg[reg.index("<") + 1:-1], code, out)
    if sig.result_reg == "D0Minus1Boolean":
        cpu.set_d(0, 0 if value else 0xFFFFFFFF)      # 0 for TRUE, as the OS returns it
    elif sig.result and (sig.result_reg or sig.os_style):
        _write_reg(cpu, sig.result_reg or "D0", sig.result, value)
    if "MoveA1ToA0" in sig.extras:
        cpu.set_a(0, cpu.a(1))
    if "ReturnMemErrConditional" in sig.extras:
        if t.mem_err:                                   # a size on success, else the error
            cpu.set_d(0, t.mem_err & 0xFFFFFFFF)
    elif "ReturnMemErr" in sig.extras:
        cpu.set_d(0, t.mem_err & 0xFFFFFFFF)
    elif "ClearD0" in sig.extras:
        cpu.set_d(0, 0)
    if "CCFromD0" in sig.extras:
        # HandToHand and the like are numbered as Toolbox traps but end as
        # the OS's do, with the flags set from D0: Radical Castle branches on
        # them straight after.
        cpu.set_cc_from_word(cpu.d(0))


def _read_selector(t, location: str, word: int, sp: int) -> tuple[int, int]:
    """A dispatcher's selector, and how many bytes of stack it takes up."""
    cpu, mem = t.cpu, t.mem
    if location == "StackW":
        return mem.r16(sp), 2
    if location == "StackL":
        return mem.r32(sp), 4
    if location.startswith("StackWLookahead<"):
        return mem.r16(sp) & int(location[16:-1], 0), 0
    if location.startswith("StackWMasked<"):
        return mem.r16(sp) & int(location[13:-1], 0), 2
    if location == "D0W":
        return cpu.d(0) & 0xFFFF, 0
    if location == "D0L":
        return cpu.d(0), 0
    if location.startswith("D0<"):
        return cpu.d(0) & int(location[3:-1], 0), 0
    if location == "TrapBits":
        return word & 0x0600, 0
    raise ValueError(f"selector location {location}")


def selector(t, location: str, word: int) -> int:
    """Read (and, from the stack, pop) a dispatcher's selector."""
    sel, size = _read_selector(t, location, word, t.cpu.sp)
    if size:
        t.cpu.sp = t.cpu.sp + size
    return sel


def _routine(location: str, table: dict[int, Signature], sel: int) -> Signature | None:
    sig = table.get(sel)
    if sig is None and location == "D0W":
        sig = next((s for s in table.values() if (s.selector & 0xFF) == (sel & 0xFF)), None)
    return sig


def describe(t, word: int) -> str:
    """The trace's name for a trap about to be dispatched: for a package,
    the routine its selector picks."""
    key = trap_key(word)
    sp = t.cpu.sp + (4 if is_toolbox(word) and word & 0x400 else 0)
    try:
        if key in NAMERS:
            return NAMERS[key](t.mem, sp)
        d = DISPATCHERS.get(key)
        if d is not None and key not in RAW and key not in BY_TRAP:
            sig = _routine(d[1], d[2], _read_selector(t, d[1], word, sp)[0])
            if sig is not None:
                return sig.name
    except BusError:        # a stack pointer it can't read from: just the trap
        pass
    return trap_name(word)


def dispatch(t, word: int, pc: int) -> None:
    """Handle an A-line trap whose implementation is in Python.  PC is
    already where it returns to; for an auto-pop trap the return address
    has been popped."""
    key = trap_key(word)
    handler = RAW.get(key)
    if handler is not None:
        handler(t, word)
        return
    sig = BY_TRAP.get(key)
    if sig is None:
        d = DISPATCHERS.get(key)
        if d is None:
            raise UnknownTrap(word, pc)
        name, location, table = d
        sel = selector(t, location, word)
        sig = _routine(location, table, sel)
        if sig is None:
            if location == "D0W" and is_toolbox(word):
                # Most D0W packages put the size of the arguments, in words,
                # in the high byte, so the stack can still be cleaned up.
                t.unimplemented(f"{name} selector ${sel:04X}", word)
                t.cpu.sp = t.cpu.sp + 2 * (sel >> 8)
                t.cpu.set_d(0, unimpErr & 0xFFFFFFFF)
                return
            if name == "ScriptUtil":
                # The selector has the size of the arguments, and bit 30 for
                # a script code after them, and the size of the result.
                t.unimplemented(f"{name} selector ${sel:08X}", word)
                t.cpu.sp = t.cpu.sp + (sel >> 16 & 0xFF) + (2 if sel & 0x40000000 else 0)
                t.mem.write(t.cpu.sp, bytes(sel >> 24 & 0x3F))
                return
            raise UnknownTrap(word, pc, f"{name} selector ${sel:X}")
    call(t, sig, word)
    if key in RESERR_FLAGS:
        t.cpu.set_cc_from_word(t.process.world.r16(lowmem.ResErr))
