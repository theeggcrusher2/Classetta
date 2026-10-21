"""
File Manager traps (Inside Macintosh: Files, chapter 2; IM IV, chapter 19):
the parameter-block calls, HFSDispatch, the FSSpec calls and FindFolder.
The rest of AliasDispatch is in glue_aliases.
The model is macrt.toolbox.files.  The Device Manager's calls are here
too, for macrt's own drivers: .Print, and .Sony (glue_sony).  The Sound
Driver's are macrt.m68k.sound's, drivers in 68k code, an application's
own, glue_devices'.

Every call is done by the time the trap returns, asynchronous or not:
ioResult and D0 hold the result, and an asynchronous call's ioCompletion
routine, if it has one, is called first with A0 pointing at the
parameter block and D0 holding the result, as the File Manager calls it.
"""

from __future__ import annotations

import struct

from ..memory import ADDRESS_MASK, BusError, from_mac, to_mac
from ..toolbox import files as fm
from ..toolbox import lowmem
from ..toolbox.files import Dir, File, FileError, noErr
from ..toolbox.printing import PRINT_DRIVER
from . import dispatch, glue_devices, glue_print, glue_sony
from .dispatch import trap

badUnitErr = -21                # not a driver's reference number
unitEmptyErr = -22              # a driver's reference number: no driver there
statusErr = -18
SOUND_DRIVER = -4               # the Sound Driver's reference number
wrgVolTypErr = -123

# Parameter blocks (IM IV, chapter 19; Inside Macintosh: Files, chapter 2).  The header:
ioCompletion = 12
ioResult = 16
ioNamePtr = 18
ioVRefNum = 22
# IOParam
ioRefNum = 24
ioPermssn = 27
ioMisc = 28                     # PBRename's new name; PBGetEOF's EOF
ioBuffer = 32
ioReqCount = 36
ioActCount = 40
ioPosMode = 44
ioPosOffset = 46
# FileParam and CInfoPBRec
ioFDirIndex = 28
ioFlAttrib = 30
ioFlFndrInfo = 32               # ioDrUsrWds for a directory
ioDirID = 48                    # ioFlNum on the way back
ioFlStBlk = 52                  # ioDrNmFls for a directory
ioFlLgLen = 54
ioFlPyLen = 58
ioFlRStBlk = 62
ioFlRLgLen = 64
ioFlRPyLen = 68
ioFlCrDat = 72
ioFlMdDat = 76
ioFlBkDat = 80
ioFlXFndrInfo = 84              # ioDrFndrInfo
ioFlParID = 100
ioFlClpSiz = 104
# VolumeParam and HVolumeParam
ioVolIndex = 28
ioVCrDate = 30
ioVLsMod = 34
ioVAtrb = 38
ioVNmFls = 40
ioVBitMap = 42
ioAllocPtr = 44
ioVNmAlBlks = 46
ioVAlBlkSiz = 48
ioVClpSiz = 52
ioAlBlSt = 56
ioVNxtCNID = 58
ioVFrBlk = 62
ioVSigWord = 64
ioVDrvInfo = 66
ioVDRefNum = 68
ioVFSID = 70
ioVBkUp = 72
ioVSeqNum = 76
ioVWrCnt = 78
ioVFilCnt = 82
ioVDirCnt = 86
ioVFndrInfo = 90
# WDParam
ioWDIndex = 26
ioWDProcID = 28
ioWDVRefNum = 32
ioWDDirID = 48
# FCBPBRec
ioFCBIndx = 28
ioFCBFlNm = 32
ioFCBFlags = 36
ioFCBStBlk = 38
ioFCBEOF = 40
ioFCBPLen = 44
ioFCBCrPs = 48
ioFCBVRefNum = 52
ioFCBClpSiz = 54
ioFCBParID = 58
# CMovePBRec and FIDParam
ioNewName = 28
ioNewDirID = 36
ioDestNamePtr = 28
ioDestDirID = 36

ioDirFlg = 0x10                 # ioFlAttrib: a directory
# A volume control block's
vcbVN = 44
vcbDrvNum = 72
vcbVRefNum = 78


# =========================================================================
# Running a call
# =========================================================================
def _run(t, pb: int, is_async: bool, fn, *args) -> int:
    """Do a call, leave its result in ioResult, and call the completion
    routine of an asynchronous one."""
    pb &= ADDRESS_MASK
    err = _do(t, pb, fn, args)
    if err == fm.nsvErr and _switch_disk(t, pb):
        err = _do(t, pb, fn, args)
    t.mem.w16(pb + ioResult, err)
    completion = t.mem.r32(pb + ioCompletion) & ADDRESS_MASK if is_async else 0
    if completion:
        cpu = t.cpu
        cpu.set_a(0, pb)
        cpu.set_d(0, err & 0xFFFFFFFF)
        t.call(completion)
        cpu.set_a(0, pb)
    return err


def _do(t, pb: int, fn, args) -> int:
    with t.system.lock:
        try:
            return fn(t, pb, *args) or noErr
        except FileError as e:
            return e.code


def _switch_disk(t, pb: int) -> bool:
    """Put in the disk a call is for that isn't in a drive, as the File
    Manager does for a volume whose VCB is in the queue but offline: here,
    one a program has made itself, for a disk macrt has in a drive under
    that name.  The disk is mounted through _MountVol, as the program may
    have patched it, and the call is made again from its parameter block
    as it is then.  Uninvited finds its second disk so: its patch puts the
    disk's vRefNum in the call's parameter block."""
    m, fs = t.mem, t.system.files
    vref = m.r16s(pb + ioVRefNum)
    ours = {vol.vcb for vol in fs.volumes}
    vcb = m.r32(lowmem.VCBQHdr + 2) & ADDRESS_MASK
    for _ in range(64):                     # as many as a queue gone round on itself has
        if not vcb:
            return False
        if (vcb not in ours and m.r16s(vcb + vcbVRefNum) == vref
                and not m.r16(vcb + vcbDrvNum)):
            break
        vcb = m.r32(vcb) & ADDRESS_MASK
    else:
        return False
    try:
        vol = fs.volume_named(t.str255(vcb + vcbVN))
    except FileError:
        return False
    with glue_devices.on_stack(t, glue_devices.PB_SIZE) as mount:
        m.w16(mount + ioVRefNum, vol.drive)
        t.machine.inserting = vol.drive
        try:
            t.call_with(t.machine.mount_vol, a0=mount)
        finally:
            t.machine.inserting = 0
    return True


def _os(word: int, fn, *args):
    """Register fn as the parameter-block call $A0xx: it gets whether the
    HFS bit ($200) is set.  A0 holds the parameter block.  The Device
    Manager's calls go to a driver in 68k code first, if they're for one."""
    device = word <= 0xA006

    def handler(t, w):
        if device and (t.machine.sound.call(t, w) or glue_devices.call(t, w, DRIVERS)):
            return
        err = _run(t, t.cpu.a(0), bool(w & 0x400), fn, bool(w & 0x200), *args)
        t.cpu.set_d(0, err & 0xFFFFFFFF)
    dispatch.RAW[dispatch.trap_key(word)] = handler


def _files(t) -> fm.FileManager:
    return t.process.files


def _name(t, pb: int) -> str | None:
    p = t.mem.r32(pb + ioNamePtr) & ADDRESS_MASK
    return t.str255(p) if p else None


def _put_name(t, pb: int, name: str, at: int = ioNamePtr) -> None:
    p = t.mem.r32(pb + at) & ADDRESS_MASK
    if p:
        t.mem.w_pstr(p, to_mac(name)[:255])


def _where(t, pb: int, hfs: bool) -> tuple[int, int, str | None]:
    """(ioVRefNum, ioDirID or 0, the name)."""
    dirid = t.mem.r32(pb + ioDirID) if hfs else 0
    return t.mem.r16s(pb + ioVRefNum), dirid, _name(t, pb)


def _physical(n: int) -> int:
    return -(-n // fm.BLOCK) * fm.BLOCK


# =========================================================================
# Opening, reading and writing
# =========================================================================
def _open(t, pb, hfs, rsrc):
    m = t.mem
    vref, dirid, name = _where(t, pb, hfs)
    m.w16(pb + ioRefNum, 0)
    if not rsrc and name and name.startswith("."):
        refnum = DRIVERS.get(name.lower())
        if refnum is not None:              # OpenDriver: a driver's name
            m.w16(pb + ioRefNum, refnum)
            return
    try:
        p = _files(t).open(vref, dirid, name, rsrc, m.r8(pb + ioPermssn))
    except FileError as e:
        if e.code == fm.opWrErr:
            m.w16(pb + ioRefNum, e.refnum)
        raise
    m.w16(pb + ioRefNum, p.refnum)


def _refnum(t, pb) -> int:
    refnum = t.mem.r16s(pb + ioRefNum)
    if refnum < 0:
        raise FileError(unitEmptyErr)       # a driver: no file to read or write
    return refnum


def _close(t, pb, hfs):
    refnum = t.mem.r16s(pb + ioRefNum)
    if refnum in DRIVERS.values():
        if refnum == PRINT_DRIVER:
            glue_print.end_raw(t)
        return
    refnum = _refnum(t, pb)
    p = t.system.files.fcb(refnum)
    if p.res is not None:                   # a resource file: the Resource Manager closes it
        rm = p.res.manager
        rm.CloseResFile(refnum)
        return rm.error
    _files(t).close(refnum)


def _read(t, pb, hfs):
    m = t.mem
    m.w32(pb + ioActCount, 0)
    if m.r16s(pb + ioRefNum) == fm.SONY:
        return glue_sony.read(t, pb)
    refnum = _refnum(t, pb)
    count = m.r32s(pb + ioReqCount)
    mode, offset = m.r16(pb + ioPosMode), m.r32s(pb + ioPosOffset)
    p = t.system.files.fcb(refnum)
    if count > 0:
        err = glue_sony.read_file(t, p, fm.FileManager._position(p, mode, offset), count,
                                  m.r32(pb + ioBuffer) & ADDRESS_MASK)
        if err:
            return err
    data, err = _files(t).read(refnum, count, mode, offset)
    if data:
        m.write(m.r32(pb + ioBuffer) & ADDRESS_MASK, data)
    m.w32(pb + ioActCount, len(data))
    m.w32(pb + ioPosOffset, t.system.files.fcb(refnum).mark)
    return err


def _write(t, pb, hfs):
    m = t.mem
    m.w32(pb + ioActCount, 0)
    if m.r16s(pb + ioRefNum) == fm.SONY:
        return glue_sony.write(t, pb)
    refnum = _refnum(t, pb)
    count = m.r32s(pb + ioReqCount)
    if count < 0:
        raise FileError(fm.paramErr)
    data = m.read(m.r32(pb + ioBuffer) & ADDRESS_MASK, count) if count else b""
    n = _files(t).write(refnum, data, m.r16(pb + ioPosMode), m.r32s(pb + ioPosOffset))
    m.w32(pb + ioActCount, n)
    m.w32(pb + ioPosOffset, t.system.files.fcb(refnum).mark)


def _get_eof(t, pb, hfs):
    t.mem.w32(pb + ioMisc, _files(t).get_eof(_refnum(t, pb)))


def _set_eof(t, pb, hfs):
    _files(t).set_eof(_refnum(t, pb), t.mem.r32s(pb + ioMisc))


def _get_fpos(t, pb, hfs):
    m = t.mem
    mark = _files(t).get_fpos(_refnum(t, pb))
    m.w32(pb + ioReqCount, 0)
    m.w32(pb + ioActCount, 0)
    m.w16(pb + ioPosMode, 0)
    m.w32(pb + ioPosOffset, mark)


def _set_fpos(t, pb, hfs):
    m = t.mem
    mark, err = _files(t).set_fpos(_refnum(t, pb), m.r16(pb + ioPosMode),
                                   m.r32s(pb + ioPosOffset))
    m.w32(pb + ioPosOffset, mark)
    return err


def _allocate(t, pb, hfs):
    t.mem.w32(pb + ioActCount, 0)
    t.mem.w32(pb + ioActCount, _files(t).allocate(_refnum(t, pb), t.mem.r32s(pb + ioReqCount)))


def _flush_file(t, pb, hfs):
    _files(t).flush_file(_refnum(t, pb))


# =========================================================================
# Creating, deleting, renaming
# =========================================================================
def _create(t, pb, hfs):
    vref, dirid, name = _where(t, pb, hfs)
    _files(t).create(vref, dirid, name)


def _delete(t, pb, hfs):
    _files(t).delete(*_where(t, pb, hfs))


def _rename(t, pb, hfs):
    new = t.mem.r32(pb + ioMisc) & ADDRESS_MASK
    if not new:
        raise FileError(fm.bdNamErr)
    _files(t).rename(*_where(t, pb, hfs), t.str255(new))


def _set_lock(t, pb, hfs, locked):
    _files(t).set_lock(*_where(t, pb, hfs), locked)


# =========================================================================
# File information
# =========================================================================
def _attributes(t, node: File) -> tuple[int, int]:
    """(ioFlAttrib, the first access path's reference number or 0).

    An MFS disk's directory entry has flags the manuals don't name, and
    the File Manager reports them here too: Word 1.0's master disk sets
    bit 6 on its key file, and Word looks for it."""
    paths = sorted(t.system.files.paths(node), key=lambda p: p.refnum)
    attrib = (node.attributes & 0x62 | (0x01 if node.locked else 0) |
              (0x04 if any(p.rsrc for p in paths) else 0) |
              (0x08 if any(not p.rsrc for p in paths) else 0) | (0x80 if paths else 0))
    return attrib, paths[0].refnum if paths else 0


def _put_file_info(t, pb: int, node: File) -> None:
    """What PBGetFInfo and PBGetCatInfo say about a file."""
    m = t.mem
    attrib, refnum = _attributes(t, node)
    data, rsrc = node.sizes()
    m.w16(pb + ioRefNum, refnum)
    m.w8(pb + ioFlAttrib, attrib)
    m.w8(pb + ioFlAttrib + 1, 0)
    m.write(pb + ioFlFndrInfo, bytes(node.finfo))
    m.write(pb + ioDirID, struct.pack(">IHIIHII", node.cnid, 0, data, _physical(data), 0, rsrc,
                                      _physical(rsrc)))
    m.write(pb + ioFlCrDat, struct.pack(">II", node.crdate, node.mddate))


def _get_finfo(t, pb, hfs):
    m = t.mem
    vref, dirid, name = _where(t, pb, hfs)
    index = m.r16s(pb + ioFDirIndex)
    if index > 0:
        node = _files(t).file_at(vref, dirid, index)
        _put_name(t, pb, node.name)
    else:
        node = _files(t).lookup_file(vref, dirid, name, True)
    _put_file_info(t, pb, node)


def _set_finfo(t, pb, hfs):
    m = t.mem
    node = _files(t).lookup_file(*_where(t, pb, hfs))
    if node.volume.locked:
        raise FileError(fm.vLckdErr)
    node.finfo[:] = m.read(pb + ioFlFndrInfo, 16)
    node.crdate, node.mddate = struct.unpack(">II", m.read(pb + ioFlCrDat, 8))
    _files(t).metadata_changed(node)


def _cat_node(t, pb):
    """The file or directory a PBGetCatInfo means, and whether the call
    names it back."""
    m = t.mem
    files = _files(t)
    vref, dirid, name = _where(t, pb, True)
    index = m.r16s(pb + ioFDirIndex)
    if index > 0:
        return files.item(vref, dirid, index), True
    if index < 0:
        return files.lookup(vref, dirid, None), True
    return files.lookup(vref, dirid, name), False


def _get_cat_info(t, pb):
    node, named = _cat_node(t, pb)
    if named:
        _put_name(t, pb, node.name)
    put_cat_info(t, pb, node)


def put_cat_info(t, pb: int, node: fm.Node) -> None:
    """What PBGetCatInfo says about a file or directory, after its name."""
    m = t.mem
    if isinstance(node, File):
        _put_file_info(t, pb, node)
        m.write(pb + ioFlBkDat, struct.pack(">I16sII", node.bkdate, bytes(node.xinfo),
                                            node.parid, 0))
        return
    m.w16(pb + ioRefNum, 0)
    m.w8(pb + ioFlAttrib, ioDirFlg)
    m.w8(pb + ioFlAttrib + 1, 0)
    m.write(pb + ioFlFndrInfo, bytes(node.finfo))
    m.write(pb + ioDirID, struct.pack(">IH", node.cnid, min(len(node.children), 0xFFFF)))
    m.write(pb + ioFlCrDat, struct.pack(">III16sI", node.crdate, node.mddate, node.bkdate,
                                        bytes(node.xinfo), node.parid))


def _set_cat_info(t, pb):
    m = t.mem
    node = _files(t).lookup(*_where(t, pb, True))
    if node.volume.locked:
        raise FileError(fm.vLckdErr)
    node.finfo[:] = m.read(pb + ioFlFndrInfo, 16)
    node.crdate, node.mddate, node.bkdate = struct.unpack(">III", m.read(pb + ioFlCrDat, 12))
    node.xinfo[:] = m.read(pb + ioFlXFndrInfo, 16)
    _files(t).metadata_changed(node)


# =========================================================================
# Volumes and the default directory
# =========================================================================
def _volume(t, pb, hfs) -> fm.Volume:
    """PBGetVInfo's volume: by index, by name, or by number."""
    fs = t.system.files
    index = t.mem.r16s(pb + ioVolIndex)
    if index > 0:
        if index > len(fs.volumes):
            raise FileError(fm.nsvErr)
        return fs.volumes[index - 1]
    vref = t.mem.r16s(pb + ioVRefNum)
    return _files(t).volume_for(vref, _name(t, pb) if index < 0 else None)


def _get_vinfo(t, pb, hfs):
    m = t.mem
    vol = _volume(t, pb, hfs)
    fs = t.system.files
    given = m.r16s(pb + ioVRefNum)
    _put_name(t, pb, vol.name)
    files, dirs = vol.counts()
    root_files = sum(1 for n in vol.root.items() if not n.is_dir)
    attrib = 0x8000 if vol.locked else 0
    m.write(pb + ioVCrDate, struct.pack(">IIHHHHHIIHIH", vol.crdate, vol.mddate, attrib,
                                        min(root_files, 0xFFFF), 0, 0, fm.VOLUME_BLOCKS, fm.BLOCK,
                                        fm.BLOCK, 0, vol.next_cnid, fm.FREE_BLOCKS))
    # A working directory's number stays, for PBGetVInfo (IM IV, chapter 19).
    keep = not hfs and m.r16s(pb + ioVolIndex) <= 0 and given in fs.wds
    m.w16(pb + ioVRefNum, given if keep else vol.vref)
    if hfs:
        m.write(pb + ioVSigWord, struct.pack(">HhhHIhIII32s", fm.HFS_SIGNATURE, vol.drive,
                                             vol.driver, 0, vol.bkdate, 0, 0, files, dirs,
                                             bytes(vol.finder_info)))


def _set_vinfo(t, pb):
    m = t.mem
    files = _files(t)
    vol = files.volume_for(m.r16s(pb + ioVRefNum))
    name = _name(t, pb)
    if name:
        files.rename(vol.vref, fm.ROOT, None, name)
    vol.crdate, vol.mddate = struct.unpack(">II", m.read(pb + ioVCrDate, 8))
    vol.locked = bool(m.r16(pb + ioVAtrb) & 0x8000)
    vol.bkdate = m.r32(pb + ioVBkUp)
    vol.finder_info[:] = m.read(pb + ioVFndrInfo, 32)
    t.system.files.update_volume(vol)


def _get_vol(t, pb, hfs):
    m = t.mem
    vol, given, dirid = _files(t).get_vol()
    _put_name(t, pb, vol.name)
    m.w16(pb + ioVRefNum, given)
    if hfs:
        wd = t.system.files.wds.get(given)
        m.w32(pb + ioWDProcID, wd.procid if wd else 0)
        m.w16(pb + ioWDVRefNum, vol.vref)
        m.w32(pb + ioWDDirID, dirid)


def _set_vol(t, pb, hfs):
    vref, dirid, name = _where(t, pb, hfs)
    if hfs:
        _files(t).hset_vol(vref, dirid, name)
    else:
        _files(t).set_vol(vref, name)


def _flush_vol(t, pb, hfs):
    # MacWrite flushes with a parameter block it hasn't filled in when it
    # quits after printing.  A 68000 Mac reads a name from wherever its
    # ioNamePtr points without a fault, and the garbage names no volume,
    # so the volume number decides.
    try:
        name = _name(t, pb)
    except BusError:
        name = None
    vref = t.mem.r16s(pb + ioVRefNum)
    t.system.files.flush_volume(_files(t).volume_for(vref, name))


def _set_fvers(t, pb, hfs):
    raise FileError(wrgVolTypErr)           # version numbers are MFS's


def _mount_vol(t, pb, hfs):
    """Each disk macrt has is mounted from the start: a drive's volume is
    online already, but for one a disk switch is putting in, one put
    back after Eject, or one that was unmounted."""
    m, machine, fs = t.mem, t.machine, t.system.files
    drive = m.r16s(pb + ioVRefNum)
    vol = fs.in_drive(drive)
    if vol is None:
        raise FileError(fm.nsDrvErr)
    if drive in machine.ejected:
        raise FileError(glue_sony.offLinErr)
    if vol in fs.offline:
        fs.remount(vol)
        machine.put_back.discard(drive)
    elif drive in machine.put_back:
        machine.put_back.discard(drive)
    elif drive != machine.inserting:
        raise FileError(fm.volOnLinErr)
    m.w16(pb + ioVRefNum, vol.vref)


def _eject(t, pb, hfs):
    vref = t.mem.r16s(pb + ioVRefNum)
    try:
        vol = _files(t).volume_for(vref, _name(t, pb))
    except FileError:
        vol = t.system.files.in_drive(vref) if vref > 0 else None
        if vol is None:                     # a drive's unmounted disk ejects still
            raise
    glue_sony.eject(t, vol)


def _unmount_vol(t, pb, hfs):
    vref = t.mem.r16s(pb + ioVRefNum)
    t.system.files.unmount(_files(t).volume_for(vref, _name(t, pb)))


def disk_inserted(t, event_p: int) -> None:
    """GetNextEvent mounts the disk of a disk-inserted event it returns,
    through MountVol, and puts the result in the message's high word."""
    m = t.mem
    with glue_devices.on_stack(t, glue_devices.PB_SIZE) as pb:
        m.w16(pb + ioVRefNum, m.r16(event_p + 4))
        err = t.call_with(t.machine.mount_vol, a0=pb)
    m.w16(event_p + 2, err & 0xFFFF)


@trap
def FInitQueue(t):
    """Nothing waits in the File Manager's queue: each call is done by the
    time it returns."""


# =========================================================================
# The Device Manager (IM II, chapter 6), with three drivers.  The Sound
# Driver's writes and control calls are macrt.m68k.sound's: they don't
# come here.  The .Print driver's control calls are the printer's,
# glue_print's.  The floppy disk driver, .Sony, is glue_sony.
# =========================================================================
DRIVERS = {".sound": SOUND_DRIVER, ".print": PRINT_DRIVER, ".sony": fm.SONY}
csCode = 26
csParam = 28


def _driver(t, pb) -> int:
    refnum = t.mem.r16s(pb + ioRefNum)
    if refnum not in DRIVERS.values():
        raise FileError(unitEmptyErr if refnum < 0 else badUnitErr)
    return refnum


def _kill_io(t, pb, immed):
    """There's never anything queued to stop."""
    _driver(t, pb)


def _control(t, pb, immed):
    m = t.mem
    refnum = _driver(t, pb)
    if refnum == fm.SONY:
        return glue_sony.control(t, pb)
    if refnum == PRINT_DRIVER:
        return glue_print.control(t, m.r16s(pb + csCode), m.r32(pb + csParam),
                                  m.r32(pb + csParam + 4), m.r32(pb + csParam + 8))


def _status(t, pb, immed):
    refnum = _driver(t, pb)
    if refnum == fm.SONY:
        return glue_sony.status(t, pb)
    if refnum == PRINT_DRIVER and glue_print.driver_status(
            t, t.mem.r16s(pb + csCode), pb + csParam):
        return
    raise FileError(statusErr)              # nothing to tell


for _word, _fn, *_args in (
        (0xA000, _open, False), (0xA001, _close), (0xA002, _read), (0xA003, _write),
        (0xA007, _get_vinfo), (0xA008, _create), (0xA009, _delete), (0xA00A, _open, True),
        (0xA00B, _rename), (0xA00C, _get_finfo), (0xA00D, _set_finfo), (0xA00E, _unmount_vol),
        (0xA00F, _mount_vol), (0xA010, _allocate), (0xA011, _get_eof), (0xA012, _set_eof),
        (0xA013, _flush_vol), (0xA014, _get_vol), (0xA015, _set_vol), (0xA017, _eject),
        (0xA018, _get_fpos),
        (0xA041, _set_lock, True), (0xA042, _set_lock, False), (0xA043, _set_fvers),
        (0xA044, _set_fpos), (0xA045, _flush_file), (0xA004, _control), (0xA005, _status),
        (0xA006, _kill_io)):
    _os(_word, _fn, *_args)


# =========================================================================
# HFSDispatch
# =========================================================================
def _open_wd(t, pb):
    m = t.mem
    vref, dirid, name = _where(t, pb, True)
    m.w16(pb + ioVRefNum, _files(t).open_wd(vref, dirid, name, m.r32(pb + ioWDProcID)))


def _get_wd_info(t, pb):
    m = t.mem
    index = m.r16s(pb + ioWDIndex)
    vref = m.r16s(pb + ioWDVRefNum) if index > 0 else m.r16s(pb + ioVRefNum)
    wd = _files(t).wd_info(vref, index, m.r32(pb + ioWDProcID) if index > 0 else 0)
    _put_name(t, pb, wd.volume.name)
    if index > 0:
        m.w16(pb + ioVRefNum, wd.refnum)
    m.w32(pb + ioWDProcID, wd.procid)
    m.w16(pb + ioWDVRefNum, wd.volume.vref)
    m.w32(pb + ioWDDirID, wd.dirid)


def _get_fcb_info(t, pb):
    m = t.mem
    fs = t.system.files
    index = m.r16s(pb + ioFCBIndx)
    if index > 0:
        vref = m.r16s(pb + ioVRefNum)
        vol = _files(t).volume_for(vref) if vref else None
        paths = [p for _, p in sorted(fs.fcbs.items()) if vol is None or p.node.volume is vol]
        if index > len(paths):
            raise FileError(fm.fnOpnErr)
        p = paths[index - 1]
        m.w16(pb + ioRefNum, p.refnum)
    else:
        p = fs.fcb(m.r16s(pb + ioRefNum))
    node = p.node
    _put_name(t, pb, node.name)
    eof = len(p.fork)
    m.write(pb + ioFCBFlNm, struct.pack(">IHHIIIhII", node.cnid, p.flags, 0, eof, _physical(eof),
                                        p.mark, node.volume.vref, fm.BLOCK, node.parid))
    m.w16(pb + ioVRefNum, node.volume.vref)


def _cat_move(t, pb):
    m = t.mem
    vref, dirid, name = _where(t, pb, True)
    new = m.r32(pb + ioNewName) & ADDRESS_MASK
    _files(t).move(vref, dirid, name, m.r32(pb + ioNewDirID), t.str255(new) if new else None)


def _dir_create(t, pb):
    node = _files(t).create(*_where(t, pb, True), directory=True)
    t.mem.w32(pb + ioDirID, node.cnid)


def _exchange(t, pb):
    m = t.mem
    files = _files(t)
    a = files.lookup_file(*_where(t, pb, True))
    dest = m.r32(pb + ioDestNamePtr) & ADDRESS_MASK
    b = files.lookup_file(m.r16s(pb + ioVRefNum), m.r32(pb + ioDestDirID),
                          t.str255(dest) if dest else None)
    files.exchange(a, b)


def _get_vol_parms(t, pb):
    """GetVolParmsInfoBuffer, version 2: nothing special about any volume."""
    m = t.mem
    _files(t).volume_for(m.r16s(pb + ioVRefNum), _name(t, pb))
    info = struct.pack(">HIIIIH", 2, 0, 0, 0, 0, 0)
    n = max(0, min(m.r32s(pb + ioReqCount), len(info)))
    m.write(m.r32(pb + ioBuffer) & ADDRESS_MASK, info[:n])
    m.w32(pb + ioActCount, n)


def _close_wd(t, pb):
    _files(t).close_wd(t.mem.r16s(pb + ioVRefNum))


def _lock_range(t, pb):
    """PBLockRange and PBUnlockRange: a range locks only on a shared
    volume, so on these, as on a Mac's own disks, there's nothing to do
    once the file is open.  Oregon Trail locks its records as it reads."""
    t.system.files.fcb(_refnum(t, pb))


# HFSDispatch's selectors, in D0.  PBOpenDF ($A060) and PBHOpenDF ($A260)
# share one, which is why this isn't left to dispatch: the HFS bit says
# whether ioDirID means anything.
HFS_DISPATCH = {
    0x01: _open_wd, 0x02: _close_wd, 0x05: _cat_move, 0x06: _dir_create, 0x07: _get_wd_info,
    0x08: _get_fcb_info, 0x09: _get_cat_info, 0x0A: _set_cat_info, 0x0B: _set_vinfo,
    0x10: _lock_range, 0x11: _lock_range, 0x17: _exchange, 0x1A: None, 0x30: _get_vol_parms,
}


def _hfs_dispatch(t, word):
    cpu = t.cpu
    sel = cpu.d(0) & 0xFFFF
    fn = HFS_DISPATCH.get(sel, False)
    pb = cpu.a(0)
    if fn is False:
        d = dispatch.DISPATCHERS[dispatch.trap_key(0xA060)][2].get(sel)
        t.unimplemented(d.name if d else f"HFSDispatch selector ${sel:02X}", word)
        err = _run(t, pb, bool(word & 0x400), lambda t, pb: fm.paramErr)
    elif fn is None:
        err = _run(t, pb, bool(word & 0x400), _open, bool(word & 0x200), False)
    else:
        err = _run(t, pb, bool(word & 0x400), fn)
    cpu.set_d(0, err & 0xFFFFFFFF)


dispatch.RAW[dispatch.trap_key(0xA060)] = _hfs_dispatch


# =========================================================================
# FSSpecs (Inside Macintosh: Files, chapter 2): a volume, a parent directory
# and a name.
# =========================================================================
def _spec(t, p: int) -> tuple[int, int, str]:
    m = t.mem
    return m.r16s(p), m.r32(p + 2), from_mac(m.r_pstr(p + 6))


def put_spec(t, p: int, vref: int, parid: int, name: str) -> None:
    t.mem.w16(p, vref)
    t.mem.w32(p + 2, parid)
    t.mem.w_pstr(p + 6, to_mac(name)[:63])


def _fs_call(t, fn) -> int:
    with t.system.lock:
        try:
            return fn() or noErr
        except FileError as e:
            return e.code


@trap
def FSMakeFSSpec(t, vref, dirid, name, spec):
    """The volume, parent and name a pathname leads to; fnfErr, with the
    record filled in, if there's nothing there yet."""
    def make():
        files = _files(t)
        path = t.str255(name) if name else ""
        vol, d, leaf = files.resolve(vref, dirid, path)
        err = noErr
        if leaf is None:                    # it names a directory: say it by its parent
            if d is vol.top:
                raise FileError(fm.dirNFErr)
            leaf, parid = d.name, d.parid
        else:
            if not fm.valid_name(leaf, 63):
                raise FileError(fm.bdNamErr)
            node = d.child(leaf)
            if node is None:
                err = fm.fnfErr
            else:
                leaf = node.name
            parid = d.cnid
        put_spec(t, spec, vol.vref, parid, leaf)
        return err
    return _fs_call(t, make)


def _open_spec(t, spec, perm, refnum, rsrc):
    t.w16(refnum, 0)

    def open_():
        p = _files(t).open(*_spec(t, spec), rsrc, perm)
        t.w16(refnum, p.refnum)
    return _fs_call(t, open_)


@trap
def FSpOpenDF(t, spec, perm, refnum):
    return _open_spec(t, spec, perm, refnum, False)


@trap
def FSpOpenRF(t, spec, perm, refnum):
    return _open_spec(t, spec, perm, refnum, True)


@trap
def FSpCreate(t, spec, creator, ftype, script):
    def create():
        node = _files(t).create(*_spec(t, spec))
        node.type, node.creator = t.ostype(ftype), t.ostype(creator)
        _files(t).metadata_changed(node)
    return _fs_call(t, create)


@trap
def FSpDirCreate(t, spec, script, dirid):
    def create():
        t.w32(dirid, 0)
        t.w32(dirid, _files(t).create(*_spec(t, spec), directory=True).cnid)
    return _fs_call(t, create)


@trap
def FSpDelete(t, spec):
    return _fs_call(t, lambda: _files(t).delete(*_spec(t, spec)))


@trap
def FSpGetFInfo(t, spec, info):
    return _fs_call(t, lambda: t.mem.write(info, bytes(
        _files(t).lookup_file(*_spec(t, spec)).finfo)))


@trap
def FSpSetFInfo(t, spec, info):
    def set_info():
        node = _files(t).lookup_file(*_spec(t, spec))
        node.finfo[:] = t.mem.read(info, 16)
        _files(t).metadata_changed(node)
    return _fs_call(t, set_info)


@trap
def FSpSetFLock(t, spec):
    return _fs_call(t, lambda: _files(t).set_lock(*_spec(t, spec), True))


@trap
def FSpRstFLock(t, spec):
    return _fs_call(t, lambda: _files(t).set_lock(*_spec(t, spec), False))


@trap
def FSpRename(t, spec, new):
    return _fs_call(t, lambda: _files(t).rename(*_spec(t, spec), t.str255(new)))


@trap
def FSpCatMove(t, source, dest):
    def move():
        vref, parid, name = _spec(t, dest)
        files = _files(t)
        node = files.lookup(*_spec(t, source))
        if files.volume_for(vref) is not node.volume:
            raise FileError(fm.diffVolErr)
        files.move(*_spec(t, source), parid, name)
    return _fs_call(t, move)


@trap
def FSpExchangeFiles(t, source, dest):
    files = _files(t)
    return _fs_call(t, lambda: files.exchange(files.lookup_file(*_spec(t, source)),
                                              files.lookup_file(*_spec(t, dest))))


@trap
def FSpOpenResFile(t, spec, perm):
    vref, parid, name = _spec(t, spec)
    return t.process.resources.HOpenResFile(vref, parid, name, perm)


@trap
def FSpCreateResFile(t, spec, creator, ftype, script):
    vref, parid, name = _spec(t, spec)
    t.process.resources.HCreateResFile(vref, parid, name, t.ostype(creator), t.ostype(ftype))


# =========================================================================
# FindFolder (Inside Macintosh: Files, chapter 9)
# =========================================================================
@trap
def FindFolder(t, vref, ftype, create, found_vref, found_dirid):
    t.w16(found_vref, 0)
    t.w32(found_dirid, 0)

    def find():
        d: Dir = _files(t).find_folder(vref, t.ostype(ftype), bool(create))
        t.w16(found_vref, d.volume.vref)
        t.w32(found_dirid, d.cnid)
    return _fs_call(t, find)
