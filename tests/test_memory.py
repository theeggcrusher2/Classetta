"""RAM, the Memory Manager and the low-memory globals."""

import struct

import pytest
from guest import Guest

from macrt.memory import RAM, BusError
from macrt.toolbox import lowmem
from macrt.toolbox.memmgr import (
    FREE, H_FST_FREE, HEADER, LOCKED, MORE_MAST, NONRELOCATABLE, RELOCATABLE, ZCB_FREE, Heap,
    memFullErr, memWZErr, noErr,
)
from macrt.toolbox.types import Point, Rect


def test_ram_is_big_endian_with_24_bit_addresses():
    m = RAM(0x10000)
    m.w32(0x100, 0x12345678)
    assert m.read(0x100, 4) == b"\x12\x34\x56\x78"
    assert m.r16(0x102) == 0x5678
    assert m.r32(0xFF000100) == 0x12345678       # the top byte is ignored
    m.w16(0x200, -2)
    assert (m.r16(0x200), m.r16s(0x200)) == (0xFFFE, -2)
    with pytest.raises(BusError):
        m.r32(0xFFFE)


def test_a_mirrored_range_is_another_way_to_the_memory_it_mirrors():
    """As a Mac Plus's RAM repeats through its 4 MB of addresses."""
    m = RAM(0x10000)
    m.mirror(0xE000, 0x10000, 0x2000)
    m.w32(0xF700, 0x12345678)
    assert m.r32(0x3700) == 0x12345678
    m.write(0x3800, b"abc")
    assert m.read(0xF800, 3) == b"abc" and m.r8(0xFF00F801) == ord("b")
    assert m.physical(0xF700) == 0x3700 and m.physical(0xD700) == 0xD700
    assert m.take_dirty() == [(0x3700, 0x3803)]     # the memory written, by where it is


def test_ram_toolbox_types():
    m = RAM(0x1000)
    m.w_pstr(0x10, b"Hello")
    assert m.read(0x10, 6) == b"\x05Hello" and m.r_pstr(0x10) == b"Hello"
    m.w_rect(0x20, Rect(1, 2, 3, 4))
    assert [m.r16s(0x20 + 2 * i) for i in range(4)] == [1, 2, 3, 4]
    m.w_point(0x30, Point(-5, 7))
    assert (m.r16s(0x30), m.r16s(0x32)) == (-5, 7)
    m.w_rect(0x20, Rect(0, 0, 32768, 40000))      # wraps, as the ROM's sums do
    assert [m.r16s(0x20 + 2 * i) for i in range(4)] == [0, 0, -32768, 40000 - 65536]
    m.w_point(0x30, Point(32768, -32769))
    assert (m.r16s(0x30), m.r16s(0x32)) == (-32768, 32767)
    m.write(0x40, b"abcdef")
    m.move(0x40, 0x42, 4)                         # overlapping
    assert m.read(0x40, 6) == b"ababcd"


def heap(size=0x4000):
    m = RAM(0x10000)
    return m, Heap(m, 0x1000, 0x1000 + size)


def test_pointer_blocks_and_headers():
    m, h = heap()
    free = h.free_bytes()
    p = h.new_ptr(10)
    assert h.contains(p) and p % 4 == 0 and h.error == noErr
    assert m.r8(p - HEADER) >> 6 == NONRELOCATABLE
    phys = m.r32(p - HEADER) & 0xFFFFFF
    assert phys == 20 and m.r8(p - HEADER) & 0x0F == phys - HEADER - 10
    assert m.r32(p - 4) == h.start                # nonrelocatable: the zone
    assert h.ptr_size(p) == 10
    h.dispose_ptr(p)
    assert h.free_bytes() == free                 # merged back together
    assert h.new_ptr(10) == p                     # and reused


def test_pointer_errors():
    _, h = heap()
    assert h.new_ptr(0x10000) == 0 and h.error == memFullErr
    h.dispose_ptr(0x1234)
    assert h.error == memWZErr


def test_handles_go_through_master_pointers():
    m, h = heap()
    hd = h.new_handle(16, clear=True)
    data = h.deref(hd)
    assert m.r32(hd) == data and h.handle_size(hd) == 16
    assert m.r8(data - HEADER) >> 6 == RELOCATABLE
    assert h.start + m.r32(data - 4) == hd        # the relative handle
    assert h.recover_handle(data) == hd
    h.lock(hd)
    assert m.r8(hd) & LOCKED and h.deref(hd) == data
    h.unlock(hd)
    h.dispose_handle(hd)
    assert h.new_handle(4) == hd                  # the master pointer is reused


def test_disposing_a_handle_whose_master_pointer_was_written_over():
    # Radical Castle, once it had crashed: its GCOD 0's led to an odd address.
    m, h = heap()
    hd, other = h.new_handle(16), h.new_handle(16)
    data, free = h.deref(hd), h.free_bytes()
    m.w32(hd, 0x60000000 | (data + 0x1F))
    h.dispose_handle(hd)
    assert h.error == memWZErr and not h.is_handle(hd)
    assert m.r32(h.start + H_FST_FREE) == hd      # back on the free list, at its front
    assert h.free_bytes() == free                 # nothing freed that wasn't a block
    h.dispose_handle(other)
    assert h.error == noErr and h.free_bytes() > free


def test_realloc_handle_takes_a_master_pointer_the_program_made():
    # Word 1.0 keeps one in its first segment for the rest of itself.
    m, h = heap()
    mine = h.new_ptr(8) + 4                       # NIL, in the zone, not the zone's
    assert h.realloc_handle(mine, 32) and h.error == noErr
    data = h.deref(mine)
    assert data and h.handle_size(mine) == 32 and h.start + m.r32(data - 4) == mine
    h.lock(mine)
    assert m.r8(mine) & LOCKED
    h.unlock(mine)
    h.dispose_handle(mine)
    assert h.error == noErr and h.new_handle(4) != mine     # not the zone's to reuse
    m.w32(mine, 0x1234)                                     # not NIL: not a master pointer
    assert not h.realloc_handle(mine, 8) and h.error == memWZErr


def test_set_handle_size_grows_in_place_or_moves():
    m, h = heap()
    hd = h.new_handle(8)
    m.write(h.deref(hd), b"12345678")
    first = h.deref(hd)
    assert h.set_handle_size(hd, 40) and h.deref(hd) == first     # room after it
    wall = h.new_ptr(4)                                           # now blocked
    assert h.set_handle_size(hd, 200)
    assert h.deref(hd) != first and m.read(h.deref(hd), 8) == b"12345678"
    assert h.handle_size(hd) == 200
    h.dispose_ptr(wall)


def test_locked_handle_does_not_move():
    _, h = heap()
    hd = h.new_handle(8)
    h.new_ptr(4)                                  # right after it
    h.lock(hd)
    before = h.deref(hd)
    assert not h.set_handle_size(hd, 200) and h.error == memFullErr
    assert h.deref(hd) == before
    h.unlock(hd)
    assert h.set_handle_size(hd, 200) and h.deref(hd) != before


def test_a_locked_handle_at_the_end_grows_the_zone_for_room():
    # A program that grows its locked first segment in place, as Pyramid of
    # Peril does, gets the room the application heap may still grow by.
    m = RAM(0x10000)
    h = Heap(m, 0x1000, 0x2000, 0x8000)
    hd = h.new_handle(8)
    m.write(h.deref(hd), b"12345678")
    h.lock(hd)
    before = h.deref(hd)
    assert h.set_handle_size(hd, 0x3000) and h.deref(hd) == before
    assert h.handle_size(hd) == 0x3000 and h.end >= before + 0x3000
    assert m.read(before, 8) == b"12345678"
    assert not h.set_handle_size(hd, 0x10000) and h.error == memFullErr
    assert h.deref(hd) == before and h.handle_size(hd) == 0x3000


def test_shrinking_frees_the_tail():
    _, h = heap()
    hd = h.new_handle(400)
    after = h.free_bytes()
    assert h.set_handle_size(hd, 20)
    assert h.free_bytes() == after + (408 - 28)


def test_compact_mem_slides_the_handles_down_past_the_free_space():
    m, h = heap()
    gone, a, wall, b, gap, c = (h.new_handle(64) for _ in range(6))
    h.dispose_handle(wall)
    wall = h.new_ptr(16)                          # where the handle was, and stays
    h.dispose_handle(gone)
    h.dispose_handle(gap)
    for hd, text in ((a, b"A"), (b, b"B"), (c, b"C")):
        m.write(h.deref(hd), text * 64)
    h.lock(b)
    was = {hd: h.deref(hd) for hd in (a, b, c)}
    free = h.free_bytes()
    h.compact()
    assert h.deref(a) < was[a] and h.deref(b) == was[b] and h.deref(c) < was[c]
    assert h.deref(a) + 64 + HEADER <= wall < h.deref(b)    # past the gaps only
    assert h.deref(c) == h.deref(b) + 64 + HEADER
    for hd, text in ((a, b"A"), (b, b"B"), (c, b"C")):
        assert m.read(h.deref(hd), 64) == text * 64
        assert h.start + m.r32(h.deref(hd) - 4) == hd        # its relative handle
    assert m.r8(b) & LOCKED and h.free_bytes() == free
    assert h.max_block(grow=False) == h.end - (h.deref(c) + 64) - HEADER


def filled(m, h, *sizes):
    """Handles of sizes, holding their own numbers, after a pointer at the
    bottom of the zone, and a last one taking the rest of it."""
    h.dispose_handle(h.new_handle(4))               # the master pointers first
    h.new_ptr(16)
    hs = [h.new_handle(n) for n in sizes]
    hs.append(h.new_handle(h.max_block(grow=False)))
    assert not h.max_block(grow=False)
    for i, hd in enumerate(hs):
        m.write(h.deref(hd), bytes([i]) * h.handle_size(hd))
    return hs


def contents(m, h, hs):
    """What each handle holds, if its block still leads back to it."""
    return [h.start + m.r32(h.deref(hd) - 4) == hd and m.read(h.deref(hd), h.handle_size(hd))
            for hd in hs]


def test_a_pointer_with_no_free_block_goes_low_and_the_handles_move_up():
    """And the zone grows for them, as a Mac's does to keep its
    nonrelocatable blocks together at the bottom (Inside Macintosh II-12)."""
    m = RAM(0x10000)
    h = Heap(m, 0x1000, 0x2000, 0x8000)
    hs = filled(m, h, 0x100, 0x100)
    low, end, held = h.deref(hs[0]), h.end, contents(m, h, hs)
    p = h.new_ptr(0x180)
    assert p == low and h.end > end
    assert all(h.deref(hd) > p + 0x180 for hd in hs[:2]) and h.deref(hs[2]) < end
    assert contents(m, h, hs) == held


def test_a_pointer_goes_low_into_free_blocks_the_handles_move_out_from_between():
    m = RAM(0x10000)
    h = Heap(m, 0x1000, 0x2000)
    hs = filled(m, h, 0x100, 0x100, 0x100, 0x100, 0xF0)
    h.dispose_handle(hs.pop(1))
    h.dispose_handle(hs.pop(2))                     # 0x108 bytes free twice
    low, was, held = h.deref(hs[0]), h.deref(hs[1]), contents(m, h, hs)
    p = h.new_ptr(0x180)
    assert p == low and h.deref(hs[1]) == was and h.deref(hs[0]) > was
    assert contents(m, h, hs) == held


def test_max_mem_compacts_the_zone(qapp):
    """Inside Macintosh II-38: the p-System of the Angelsoft adventures takes
    the largest free block to be the one at the zone's end."""
    g = Guest()
    heap = g.process.heap
    hs = [heap.new_handle(0x400) for _ in range(3)]
    heap.dispose_handle(hs[1])
    was = heap.deref(hs[2])
    largest, growth = g("MaxMem", 0)
    assert heap.deref(hs[2]) == was - 0x408
    addr, size = heap._free[-1]
    assert addr + size == heap.end and size - HEADER == largest


def test_blocks_a_program_moved_itself_are_found_where_they_are():
    """MacWrite 4.5 moves a segment to the top of the heap before it loads
    it, writing the blocks' headers and master pointer as the Memory
    Manager would."""
    m, h = heap()
    hd = h.new_handle(40)
    data = h.deref(hd)
    m.write(data, b"x" * 40)
    tail = data + 40                              # the free space after it
    phys = m.r32(data - HEADER) & 0xFFFFFF
    to = h.end - phys
    m.move(data - HEADER, to, phys)
    m.w32(data - HEADER, phys)                    # free, where it was
    m.w32(tail, to - tail)                        # and below where it is now
    m.w32(hd, to + HEADER)
    assert h.handle_size(hd) == 40 and m.read(h.deref(hd), 40) == b"x" * 40
    p = h.new_ptr(16)
    assert p == data                              # the space it left
    h.dispose_ptr(p)
    h.dispose_handle(hd)
    assert h.max_block(grow=False) == h.end - data


def test_freeing_a_block_keeps_the_ones_a_program_made_above_it():
    """Macintosh Pascal moves a handle to the top of the zone by taking
    the room from the free block there: freeing the block below that one
    leaves it be, and the room up to it free."""
    m, h = heap()
    hd, below = h.new_handle(40), h.new_handle(40)
    data = h.deref(hd)
    m.write(data, b"x" * 40)
    phys = m.r32(data - HEADER) & 0xFFFFFF
    tail = h.deref(below) - HEADER + phys         # the free block after both
    to = h.end - phys
    m.move(data - HEADER, to, phys)
    m.w32(data - HEADER, phys)                    # free, where it was
    m.w32(tail, to - tail)                        # the free block, short of it
    m.w32(hd, to + HEADER)
    h.dispose_handle(below)
    assert h.handle_size(hd) == 40 and m.read(h.deref(hd), 40) == b"x" * 40
    assert h.set_handle_size(hd, 0) and h.handle_size(hd) == 0
    assert h.max_block(grow=False) == to - data


def test_a_block_a_program_made_past_the_zone_and_moved_bklim_over_is_in_it():
    """The Angelsoft adventures' p-System carves a block of its own at the
    top of the room the zone can grow into, then moves bkLim past it and
    the free block at the zone's end up to it."""
    m = RAM(0x10000)
    h = Heap(m, 0x1000, 0x2000, 0x8000)
    at = h.start + 0x34
    while at + (m.r32(at) & 0xFFFFFF) != h.end:         # the zone's last block, free
        at += m.r32(at) & 0xFFFFFF
    assert m.r32(at) >> 30 == FREE
    own = 0x7000
    m.w32(own, NONRELOCATABLE << 30 | 2 << 24 | 0x100)  # $F6 bytes
    m.w32(own + 4, h.start)
    m.w32(at, own - at)
    m.w32(h.start, own + 0x100)
    p = own + HEADER
    assert h.ptr_size(p) == 0xF6 and h.end == own + 0x100
    assert h.set_ptr_size(p, 0x20) and h.ptr_size(p) == 0x20
    q = h.new_ptr(0x4000)
    assert h.start < q < own and h.ptr_size(p) == 0x20


def test_a_68000s_blocks_are_a_multiple_of_two_bytes():
    """Macintosh Pascal checksums its code with a block's size from its
    header, which on a 68000 is its logical size and 8, made even."""
    m = RAM(0x10000)
    h2, h4 = Heap(m, 0x1000, 0x3000, align=2), Heap(m, 0x4000, 0x6000)
    for zone, phys in ((h2, 0x175A), (h4, 0x175C)):
        data = zone.deref(zone.new_handle(0x1752))
        assert m.r32(data - HEADER) & 0xFFFFFF == phys
        assert zone.handle_size(zone.recover_handle(data)) == 0x1752


def test_a_68000s_zone_grows_by_all_a_block_needs():
    """The zone grows by what the free block at its end lacks, made a
    multiple of 4, even when that's 2 more than one: Klondike's first
    segment was 2 bytes too big for the room the zone grew to."""
    m = RAM(0x20000)
    h = Heap(m, 0x1000, 0x1800, limit=0x10000, align=2)
    h.new_ptr(6)                                    # 14 bytes
    last = h.max_block(grow=False) + HEADER
    assert last % 4 == 2
    size = last + 0x2002 - HEADER                   # more than the zone grows by at least
    p = h.new_ptr(size)
    assert p and h.ptr_size(p) == size


def test_a_block_grows_in_place_by_moving_the_ones_after_it():
    """SetPtrSize, and SetHandleSize on a locked handle, move the unlocked
    relocatable blocks after the block out of its way, and purge the
    purgeable ones when there's nowhere for them: Macintosh Pascal grows a
    pointer before it runs a program."""
    m, h = heap(0x1000)
    h.dispose_handle(h.new_handle(4))               # the master pointers first
    p = h.new_ptr(4)
    moved, purged = h.new_handle(16), h.new_handle(0x400)
    m.write(h.deref(moved), b"m" * 16)
    h.set_purgeable(purged)
    h.new_ptr(0x780)                                # too little left for purged
    assert h.set_ptr_size(p, 0x100) and h.ptr_size(p) == 0x100
    assert m.read(h.deref(moved), 16) == b"m" * 16 and h.deref(moved) > p + 0x100
    assert h.is_empty(purged)
    assert m.r32(h.start + ZCB_FREE) == sum(n for _, n in h._free)
    # a locked handle in the way stays, and so does the block
    q = h.new_ptr(4)
    stuck = h.new_handle(8)
    h.lock(stuck)
    assert not h.set_ptr_size(q, 0x40) and h.ptr_size(q) == 4
    # a locked handle grows the same way
    lk = h.new_handle(4)
    h.lock(lk)
    after = h.new_handle(8)
    assert h.set_handle_size(lk, 0x40) and h.deref(lk) < h.deref(after)


def test_growing_a_block_reads_back_the_blocks_a_program_made_after_it():
    """Microsoft File reads its p-code file, laid out as heap blocks, over
    a handle's block, headers and all: growing a block before it moves the
    handle's block as its header has it now, and leaves the program's be."""
    m, h = heap(0x1000)
    h.dispose_handle(h.new_handle(4))               # the master pointers first
    p = h.new_ptr(4)
    hd = h.new_handle(0x100)
    data = h.deref(hd)
    assert data == p + (m.r32(p - HEADER) & 0xFFFFFF)          # right after p
    phys = m.r32(data - HEADER) & 0xFFFFFF
    m.w32(data - HEADER, RELOCATABLE << 30 | 0x88)              # the handle's, cut short
    m.w32(data + 0x80, NONRELOCATABLE << 30 | (phys - 0x88))    # and the program's own
    m.write(data, b"h" * 0x80)
    m.write(data + 0x80 + HEADER, b"p" * 0x10)
    assert h.set_ptr_size(p, 0x40) and h.ptr_size(p) == 0x40
    assert h.handle_size(hd) == 0x80 and m.read(h.deref(hd), 0x80) == b"h" * 0x80
    assert m.read(data + 0x80 + HEADER, 0x10) == b"p" * 0x10
    assert m.r32(h.start + ZCB_FREE) == sum(n for _, n in h._free)


def test_the_zone_record_says_how_many_masters_a_block_holds():
    # Multiplan calls MoreMasters until it has what it wants, counting moreMast.
    m = RAM(0x10000)
    h = Heap(m, 0x1000, 0x2000)
    assert m.r16(h.start + 20) == 32


def test_a_zone_grows_toward_its_limit():
    m = RAM(0x10000)
    ends = []
    h = Heap(m, 0x1000, 0x2000, 0x8000, on_grow=ends.append)
    free = h.free_bytes()
    assert h.room_to_grow() == 0x6000 and free > 0x6000       # growth counts as free
    assert h.max_block(grow=False) < 0x1000 < 0x6000 < h.max_block()
    p = h.new_ptr(0x3000)
    assert p and 0x2000 < h.end < 0x8000 and ends == [h.end]
    assert m.r32(h.start) == h.end                             # bkLim
    assert h.free_bytes() == free - (0x3000 + HEADER)
    h.set_limit(0x1800)                                        # never below its end
    assert h.limit == h.end and h.new_ptr(0x6000) == 0 and h.error == memFullErr
    h.set_limit(0x8000)
    h.grow(0x9000)                                             # only as far as its limit
    assert h.end == 0x8000 and ends[-1] == 0x8000 and h.room_to_grow() == 0


def test_asking_for_more_than_there_is_grows_the_zone_all_the_way():
    # How programs written before MaxApplZone got the whole heap.
    m = RAM(0x10000)
    h = Heap(m, 0x1000, 0x2000, 0x8000)
    assert h.new_handle(0x100000) == 0 and h.error == memFullErr
    assert h.end == 0x8000 and m.r32(h.start) == 0x8000
    free = 0
    at = h.start + 0x34                     # the first block, walked as a program does
    while at != h.end:
        header = m.r32(at)
        free += header & 0xFFFFFF if header >> 30 == FREE else 0
        at += header & 0xFFFFFF
    assert free > 0x6000


def test_application_heap_grows_by_the_traps(qapp):
    g = Guest()
    proc = g.process
    heap, world = proc.heap, proc.world
    limit = world.r32(lowmem.ApplLimit)
    assert heap.end < limit == heap.limit <= proc.partition.stack_base
    largest, growth = g("MaxMem", 0)
    assert growth == limit - heap.end and largest < growth
    g("SetApplLimit", limit - 0x1000)
    assert world.r32(lowmem.ApplLimit) == heap.limit == limit - 0x1000 and g.mem_err == noErr
    g("MaxApplZone")
    assert heap.end == heap.limit == world.r32(lowmem.HeapEnd) and g("MaxMem", 0)[1] == 0
    g("SetApplLimit", heap.end - 0x100)                        # below HeapEnd: too low
    assert g.mem_err == memFullErr and heap.limit == heap.end
    assert world.r32(lowmem.ApplLimit) == heap.end - 0x100


def test_the_stack_is_what_defltstack_says(qapp):
    """The Fool's Errand moves ApplLimit down by what DefltStack is short
    of the 32K of stack it wants."""
    g = Guest()
    world = g.process.world
    stack = g.mem.r32(lowmem.DefltStack)
    assert stack >= 0x8000 and g.mem.r32(lowmem.MinStack) <= stack
    assert 0 <= world.r32(lowmem.CurStackBase) - world.r32(lowmem.ApplLimit) - stack < 4


def test_reserve_mem_says_whether_theres_room(qapp):
    g = Guest()
    g("ReserveMem", 0x1000, 0)
    assert g.mem_err == noErr
    g("ReserveMem", 0x7FFFFF, 0)
    assert g.mem_err == memFullErr


def test_reserve_mem_grows_the_application_zone(qapp):
    """For what it reserves, and as far as it can when that's not enough:
    The Fool's Errand asks for 8 MB in place of MaxApplZone, then sizes its
    buffers by what MaxMem says is free."""
    g = Guest()
    heap = g.process.heap
    largest, growth = g("MaxMem", 0)
    g("ReserveMem", largest + 0x2000, 0)
    assert g.mem_err == noErr and heap.end < heap.limit
    assert g("MaxMem", 0)[0] >= largest + 0x2000
    g("ReserveMem", 0x7A1200, 0)
    assert g.mem_err == memFullErr and heap.end == heap.limit
    largest, growth = g("MaxMem", 0)
    assert growth == 0 and largest > 0x10000


def test_world_holds_globals_until_switched_in():
    m = RAM(0x4000)
    a, b = lowmem.World(m), lowmem.World(m)
    a.w32(lowmem.WindowList, 0x1234)
    b.w32(lowmem.WindowList, 0x5678)
    assert m.r32(lowmem.WindowList) == 0
    a.switch_in()
    assert m.r32(lowmem.WindowList) == 0x1234
    m.w32(lowmem.WindowList, 0x9999)              # the app changes it itself
    assert a.r32(lowmem.WindowList) == 0x9999
    a.switch_out()
    b.switch_in()
    assert m.r32(lowmem.WindowList) == 0x5678
    assert a.r32(lowmem.WindowList) == 0x9999
    with pytest.raises(KeyError):
        a.r32(lowmem.ScrnBase)                    # not a per-application global


def test_hand_to_hand_leaves_the_flags_as_d0(qapp):
    """It's numbered as a Toolbox trap but ends as the OS's do, with the
    flags set from D0: Radical Castle branches on them straight after."""
    from guest import Processor
    m = Processor()
    h = m.handle(b"abcd")
    copy = struct.pack(">HIHHH", 0x207C, h, 0xA9E1, 0x56C7, 0x5BC6)     # ...; SNE D7; SMI D6
    m.cpu.sr = 0x2008                                       # N set, Z clear
    m.run(copy)
    assert (m.cpu.d(0), m.cpu.d(7) & 0xFF, m.cpu.d(6) & 0xFF) == (0, 0, 0)
    assert m.contents(m.cpu.a(0)) == b"abcd" and m.cpu.a(0) != h
    m.run(struct.pack(">HIHHH", 0x207C, 0, 0xA9E1, 0x56C7, 0x5BC6))     # a nil handle
    assert m.cpu.d(0) & 0xFFFF == 0x10000 - 109             # nilHandleErr
    assert (m.cpu.d(7) & 0xFF, m.cpu.d(6) & 0xFF) == (0xFF, 0xFF)


def test_a_negative_size_is_too_big_to_have(qapp):
    """Not none: Run for the Money works its offscreen buffer's size out in
    16 bits, and on a 640 by 480 screen draws over its code in 0 bytes."""
    g = Guest()
    for name in ("NewPtr", "NewHandle"):
        assert g(name, 80 * 480 - 0x10000, False, False) == 0
        assert g.mem_err == memFullErr
        assert g(name, 0, False, False) != 0 and g.mem_err == noErr


def test_the_zone_record_keeps_its_free_bytes_and_master_pointers():
    """zcbFree counts the zone's free blocks, and hFstFree heads the list
    of free master pointers, each holding the next: Excel 1.0 reads both."""
    m, h = heap()

    def counted():
        return sum(n for _, n in h._free)
    assert m.r32(h.start + ZCB_FREE) == counted() > 0
    p, hd = h.new_ptr(100), h.new_handle(50)
    assert m.r32(h.start + ZCB_FREE) == counted()
    h.set_handle_size(hd, 400)
    h.set_ptr_size(p, 20)
    assert m.r32(h.start + ZCB_FREE) == counted()
    h.dispose_ptr(p)
    h.dispose_handle(hd)
    h.compact()
    assert m.r32(h.start + ZCB_FREE) == counted()
    # Excel's MoreMasters: an empty list, and as many as it wants
    m.w32(h.start + H_FST_FREE, 0)
    m.w16(h.start + MORE_MAST, 200)
    assert h.more_masters()
    last = m.r32(h.start + H_FST_FREE)
    first = last - 199 * 4
    assert m.r32(first) == 0 and m.r32(first + 4) == first and m.r32(last) == last - 4
    assert m.r8(first - HEADER) >> 6 == NONRELOCATABLE
    assert h.new_handle(8) == last and h.new_handle(8) == last - 4
    assert m.r32(h.start + H_FST_FREE) == last - 8
    h.dispose_handle(last)
    assert m.r32(h.start + H_FST_FREE) == last and m.r32(last) == last - 8


def test_a_free_master_pointer_list_written_over_is_let_go():
    """A program that stores through a handle it has disposed of spoils the
    list; the zone stops at the spoiled link and makes more."""
    m, h = heap()
    a, b = h.new_handle(8), h.new_handle(8)
    h.dispose_handle(b)
    data = h.deref(a)
    m.w32(b, data)                              # stored through b, after it went
    c = h.new_handle(8)
    d = h.new_handle(8)
    assert c == b and d not in (a, c) and d != data and h.deref(a) == data


def test_init_zone_makes_a_zone_and_the_current_zone(qapp):
    """In a block of the application heap, or, as Winter Games makes its
    own, in memory no zone has; blocks then come from it until SetZone."""
    g = Guest()
    appl = g.process.heap.start
    block = g("NewPtr", 0x2000, 0, 0)
    free = g.process.partition.end + 0x1000
    for start, limit in ((block, block + 0x2000), (free, free + 0x2A80)):
        pb = g.ptr(struct.pack(">IIHI", start, limit, 16, 0))
        g("InitZone", pb)
        assert g.mem_err == noErr and g("GetZone") == start
        assert g.mem.r16(start + MORE_MAST) == 16
        p, h = g("NewPtr", 0x100, 0, 0), g("NewHandle", 0x100, 0, 0)
        assert start < p < limit and start < g.mem.r32(h) < limit
        assert g("HandleZone", h) == g("PtrZone", p) == start
        g("DisposePtr", p)
        assert g.mem_err == noErr
        g("SetZone", appl)
        p = g("NewPtr", 0x100, 0, 0)
        assert g("PtrZone", p) == appl
    # Too small to hold its header, trailer and master pointers.
    g("InitZone", g.ptr(struct.pack(">IIHI", free, free + 0x40, 16, 0)))
    assert g.mem_err == memFullErr and g("GetZone") == appl
