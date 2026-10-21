"""
Standard File (Pack3; Inside Macintosh: Files, chapter 3; IM I-519 and
IV-71): SFGetFile, SFPutFile, SFPGetFile, SFPPutFile, StandardGetFile,
StandardPutFile, CustomGetFile and CustomPutFile, all with the dialog in
macrt.ui.stdfile.

A dialog starts where the last one finished, which SFSaveDisk and
CurDirStore say (at launch, the application's folder), and leaves them
where it finishes.  What a directory shows is worked out here, on the
application's thread, because the application's file filter may leave
things out: it gets a CInfoPBRec, as PBGetCatInfo fills one in, and
returns TRUE for something not to show.  It's asked about the files whose
types are in the type list; System 7's calls ask it about folders too.

The dialog is macrt's own, a Qt one, but an application's own part of
it is kept: the items its dialog (the ID it gives, or DLOG -3999 and
-4000 of its own) has after Standard File's own, and its dialog hook.
The Dialog Manager makes the application's dialog, which is never shown;
its push buttons, check boxes, radio buttons, static text and edit text
are shown as widgets, and the hook is called as a Mac calls it, with the
item the user chose (an edit text item each time it's typed in, as
ModalDialog returns it, with what's been typed in it by then; or, for
what isn't an item here, the item number System 7 gives it), and what
it returns is what's done.  The widgets show what
the hook leaves the items as.  The modal-dialog filter, the activation
list and procedure aren't used.  Without a display (no one to ask) every
dialog is cancelled.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass

from ..memory import ADDRESS_MASK, from_mac, to_mac
from ..toolbox import files as fm
from ..toolbox import lowmem
from ..toolbox.dialogs import substitute
from ..toolbox.events import ProcessKilled
from ..toolbox.files import Dir, File, FileError
from ..toolbox.templates import btnCtrl, ctrlItem, editText, itemDisable, parse_dlog, statText
from ..toolbox.types import Rect
from ..ui import stdfile
from . import glue_dialogs as dm
from .dispatch import trap
from .glue_controls import _ctl, _proc_id
from .glue_files import ioFDirIndex, ioNamePtr, ioVRefNum, put_cat_info

CINFO_PB = 108                  # sizeof(CInfoPBRec)
MAX_TYPES = 64

# SFReply (IM I-519)
rGood, rCopy, rFType, rVRefNum, rVersion, rFName = 0, 1, 2, 6, 8, 10
SF_REPLY = 74
# StandardFileReply (Inside Macintosh: Files, chapter 3)
sfGood, sfReplacing, sfType, sfFile, sfScript, sfFlags, sfIsFolder, sfIsVolume = (
    0, 1, 2, 6, 76, 78, 80, 81)
STANDARD_REPLY = 88

MORE = object()                 # a step that leaves the dialog up, somewhere else
SAME = object()                 # one that leaves it up where it is

# What a dialog hook is told besides its items (Inside Macintosh: Files 3-30)
sfHookFirstCall, sfHookLastCall = -1, -2
sfHookNullEvent, sfHookRebuildList, sfHookFolderPopUp, sfHookOpenFolder = 100, 101, 102, 103
sfMainDialogRefCon = 0x73746466             # 'stdf'


@dataclass(frozen=True)
class Numbers:
    """A Standard File dialog's own items, as its hook knows them."""
    ok: int                     # Open or Save
    cancel: int
    eject: int
    drive: int                  # Drive; System 7's Desktop
    name: int                   # the name to save as, 0 for none
    new_folder: int             # 0 for none
    count: int                  # how many are Standard File's; the application's follow


# IM I-523 and Inside Macintosh: Files 3-19, by put.
ORIGINAL = {False: Numbers(1, 3, 5, 6, 0, 0, 9), True: Numbers(1, 2, 5, 6, 7, 0, 8)}
NEW = {False: Numbers(1, 2, 5, 6, 0, 0, 9), True: Numbers(1, 2, 5, 6, 10, 12, 12)}
DIALOG_IDS = {False: -4000, True: -3999}    # the original calls' dialogs
CDEF_KINDS = ("button", "check", "radio")   # CDEF 0's variations


def _s16(v: int) -> int:
    v &= 0xFFFF
    return v - 0x10000 if v & 0x8000 else v


def _standard_items(numbers: Numbers) -> bytes:
    """An item list for a dialog with Standard File's items and none of
    the application's, for a hook to find them in: the buttons, the name
    to save as, and user items for the rest."""
    titles = {numbers.ok: b"Open", numbers.cancel: b"Cancel", numbers.eject: b"Eject",
              numbers.drive: b"Drive"}
    out = struct.pack(">h", numbers.count - 1)
    for n in range(1, numbers.count + 1):
        if n in titles:
            kind, data = ctrlItem + btnCtrl, titles[n]
        elif n == numbers.name:
            kind, data = editText, b""
        else:
            kind, data = itemDisable, b""
        out += struct.pack(">I4hBB", 0, 0, 0, 0, 0, kind, len(data)) + data
        out += bytes(len(data) & 1)
    return out


class Custom:
    """An application's own part of a Standard File dialog: its dialog
    hook, and its dialog, which has its own items after Standard File's.
    The dialog is made as the Dialog Manager makes one, invisible."""

    def __init__(self, t, w, numbers: Numbers, hook: int, new: bool, data: int):
        self.t = t
        self.w = w
        self.numbers = numbers
        self.hook = hook & ADDRESS_MASK
        self.new = new                      # System 7's: the hook gets yourDataPtr too
        self.data = data
        self.item_rect = {}                 # each item's rect at first, to lay them out

    @classmethod
    def make(cls, t, put: bool, hook: int, dlg_id: int, new: bool = False,
             data: int = 0) -> Custom | None:
        """The application's part, if it has one: a hook, or a dialog with
        items of its own."""
        numbers = (NEW if new else ORIGINAL)[put]
        hook &= ADDRESS_MASK
        h = t.process.resources.GetResource("DLOG", dlg_id) if dlg_id else 0
        if h:
            tmpl = parse_dlog(t.tb.contents(h))
            item_list = dm._copy_resource(t, "DITL", tmpl.items_id)
            bounds = tmpl.bounds
            proc_id, go_away, ref_con, title = (tmpl.proc_id, tmpl.go_away, tmpl.ref_con,
                                                tmpl.title)
        else:
            item_list = 0
        if item_list and not hook and len(dm.items(t.mem, item_list)) <= numbers.count:
            t.system.heap_of(item_list).dispose_handle(item_list)
            return None
        if not item_list:
            if not hook:
                return None
            data_ = _standard_items(numbers)
            item_list = t.process.heap.new_handle(len(data_))
            if not item_list:
                return None
            t.mem.write(t.process.heap.deref(item_list), data_)
            bounds, proc_id, go_away, ref_con, title = Rect(0, 0, 200, 348), 1, False, 0, ""
        w = dm.new_dialog(t, 0, bounds, title, False, proc_id, dm.IN_FRONT, go_away,
                          sfMainDialogRefCon if new else ref_con, item_list)
        if w is None:
            t.system.heap_of(item_list).dispose_handle(item_list)
            return None
        out = cls(t, w, numbers, hook, new, data)
        out.item_rect = {item.number: item.rect for item in dm._items(t, w)}
        return out

    def call(self, item: int) -> int:
        """The hook's answer to an item; the item itself without a hook."""
        if not self.hook:
            return item
        t = self.t
        args = (("h", item), ("p", self.w.addr))
        if self.new:
            args += (("p", self.data),)
        with dm._in_port(t, self.w):
            return _s16(t.call(self.hook, args, "h"))

    # The name to save as, in its item for the hook to read and change.
    def _name_item(self):
        return dm._item(self.t, self.w, self.numbers.name) if self.numbers.name else None

    def set_name(self, name: str) -> None:
        item = self._name_item()
        if item is not None and item.handle & ADDRESS_MASK:
            dm._set_handle(self.t, item.handle & ADDRESS_MASK, to_mac(name)[:255])

    def name(self) -> str | None:
        item = self._name_item()
        if item is None or not item.handle & ADDRESS_MASK:
            return None
        return from_mac(dm._text(self.t, item.handle))

    def set_edits(self, edits: dict[int, str]) -> None:
        """What the user has typed in the application's edit text items,
        for its hook to read."""
        for n, text in edits.items():
            item = dm._item(self.t, self.w, n)
            if item is not None and item.type == editText and item.handle & ADDRESS_MASK:
                dm._set_handle(self.t, item.handle & ADDRESS_MASK, to_mac(text)[:255])

    def extras(self) -> list[stdfile.Extra]:
        """The application's own items, as they are now."""
        t, w = self.t, self.w
        port = w.port_rect
        params = dm._params(t)
        out = []
        for item in dm._items(t, w)[self.numbers.count:]:
            first = self.item_rect.get(item.number, item.rect)
            if item.is_control:
                c = _ctl(t, item.handle)
                if c is None:
                    continue
                proc_id = _proc_id(t, c)
                if proc_id >> 4 != 0 or proc_id & 3 == 3:
                    continue                    # a scroll bar, or a CDEF of its own
                kind, title, value = CDEF_KINDS[proc_id & 3], c.title, c.value
                enabled = item.enabled and c.active
                visible = c.visible and not item.hidden and not c.rect.sect(port).empty
            elif item.type == statText:
                kind, title, value = "text", from_mac(substitute(dm._text(t, item.handle),
                                                                 params)), 0
                enabled, visible = True, not item.hidden and not item.rect.sect(port).empty
            elif item.type == editText:             # disabled, it's still typed in
                kind, title, value = "edit", from_mac(dm._text(t, item.handle)), 0
                enabled, visible = True, not item.hidden and not item.rect.sect(port).empty
            else:
                continue
            out.append(stdfile.Extra(item.number, kind, title, tuple(first), value, enabled,
                                     visible))
        return out

    def dispose(self) -> None:
        dm.close_dialog(self.t, self.w, dispose=True)


class Dialog:
    """One Standard File dialog, from the application's side."""

    def __init__(self, t, put: bool, prompt: str = "", name: str = "", file_filter: int = 0,
                 types: set[bytes] | None = None, data: int | None = None,
                 folders: bool = False, hook: int = 0, dlg_id: int = 0, new: bool = False):
        self.t = t
        self.files: fm.FileManager = t.process.files
        self.fs: fm.FileSystem = t.system.files
        self.put = put
        self.prompt = prompt
        self.name = name
        self.filter = file_filter & ADDRESS_MASK
        self.types = types                  # None: every type
        self.data = data                    # CustomGetFile's yourDataPtr, for its filter
        self.folders = folders              # does the filter see folders
        self.dir: Dir | None = self._start()        # None: the desktop
        self.entries: list = []             # what each item in the list is
        self.message = ""
        self.custom_args = (hook, dlg_id, new)
        self.custom: Custom | None = None
        self.new_name: str | None = None    # what a hook changed the name to save as to

    def _start(self) -> Dir:
        world = self.t.process.world
        try:
            vol = self.fs.volume(-world.r16(lowmem.SFSaveDisk))
            d = vol.dir(world.r32(lowmem.CurDirStore) or fm.ROOT)
            return vol.root if d is vol.top else d
        except FileError:
            vol, _, dirid = self.files.get_vol()
            return vol.dir(dirid)

    # =====================================================================
    # What the dialog shows
    # =====================================================================
    def place(self) -> stdfile.Place:
        message, self.message = self.message, ""
        d = self.dir
        if d is None:
            self.entries = list(self.fs.volumes)
            return stdfile.Place([], [stdfile.Item(v.name, "volume") for v in self.entries],
                                 message=message)
        self.entries, items = [], []
        for index, node in enumerate(d.items(), start=1):
            if node.flags & fm.kIsInvisible:
                continue
            if isinstance(node, Dir):
                if self.folders and self._filtered(node, index):
                    continue
                item = stdfile.Item(node.name, "folder")
            else:
                if not self.put and not self._wanted(node, index):
                    continue
                kind = "application" if node.type == b"APPL" else "document"
                item = stdfile.Item(node.name, kind, enabled=not self.put)
            self.entries.append(node)
            items.append(item)
        path, n = [], d
        while n is not None:
            path.append(n.volume.name if n is n.volume.root else n.name)
            n = n.parent
        return stdfile.Place(path, items, d.volume.name, not d.volume.locked, message)

    def _wanted(self, node: File, index: int) -> bool:
        if self.types is not None and node.type not in self.types:
            return False
        return not self._filtered(node, index)

    def _filtered(self, node: fm.Node, index: int) -> bool:
        """Does the application's file filter leave it out?"""
        if not self.filter:
            return False
        t = self.t
        cpu, m = t.cpu, t.mem
        sp = cpu.sp
        pb = (sp - CINFO_PB - 64) & ~3          # the block and its name, on the stack
        cpu.sp = pb
        try:
            m.fill(pb, CINFO_PB + 64)
            m.w_pstr(pb + CINFO_PB, to_mac(node.name)[:63])
            m.w32(pb + ioNamePtr, pb + CINFO_PB)
            m.w16(pb + ioVRefNum, node.volume.vref)
            m.w16(pb + ioFDirIndex, index)
            with t.system.lock:
                put_cat_info(t, pb, node)
            args = (("p", pb),) if self.data is None else (("p", pb), ("p", self.data))
            return bool(t.call(self.filter, args, "B"))
        finally:
            cpu.sp = sp

    # =====================================================================
    # Running it
    # =====================================================================
    def run(self):
        """("open", file) or ("save", directory, name, replacing); None if
        the user cancelled."""
        t = self.t
        display = display_of(t)
        if display is None:
            return None
        hook, dlg_id, new = self.custom_args
        custom = self.custom = Custom.make(t, self.put, hook, dlg_id, new, self.data or 0)
        try:
            if custom is not None:
                custom.set_name(self.name)
                custom.call(sfHookFirstCall)
                if self.put and custom.name() is not None:
                    self.name = custom.name()
            with t.tb.host_dialog():
                result = self._run(display.bridge)
            if custom is not None and new:
                custom.call(sfHookLastCall)
        finally:
            if custom is not None:
                custom.dispose()
                self.custom = None
        self._remember()
        return result

    def _extras(self) -> list[stdfile.Extra]:
        return self.custom.extras() if self.custom is not None else []

    def _run(self, bridge):
        t = self.t
        dlg, fut = bridge.call(stdfile.open_dialog, display_of(t), t.process, self.put,
                               self.prompt, self.name, self.place(), self._extras())
        try:
            while True:
                action = self._wait(bridge, fut)
                if self.custom is not None:
                    action = self._consult(bridge, dlg, action)
                result = self._step(action)
                if result is MORE:
                    (fut,) = bridge.call(dlg.show_place, self.place(), self._extras())
                elif result is SAME:
                    name, self.new_name = self.new_name, None
                    (fut,) = bridge.call(dlg.resume, self._extras(), name, self.message)
                    self.message = ""
                else:
                    break
        except BaseException:
            bridge.post(dlg.finish)
            raise
        bridge.post(dlg.finish)
        return result

    def _consult(self, bridge, dlg, action: tuple) -> tuple:
        """What the application's hook makes of the user's step: the step
        to take."""
        custom = self.custom
        n = custom.numbers
        kind = action[0]
        if kind == "item":
            item = action[1]
            typed = dm._item(self.t, custom.w, item)
            if typed is not None and typed.type == editText and not typed.enabled:
                item = sfHookNullEvent      # ModalDialog doesn't return it; the hook sees the text
        elif kind == "save":
            item = n.ok
        elif kind == "cancel":
            item = n.cancel
        elif kind == "open":
            item = n.ok if isinstance(self.entries[action[1]], File) else sfHookOpenFolder
        elif kind == "up":
            item = sfHookFolderPopUp
        elif kind == "desktop":
            item = n.drive
        else:
            item = n.new_folder
        if not item:
            return action
        row, name, edits = bridge.call(dlg.state)
        custom.set_name(name)
        custom.set_edits(edits)
        got = custom.call(item)
        changed = custom.name()
        if changed is not None and changed != name:
            name = self.new_name = changed
        if got == item:
            return ("save", name, action[2]) if kind == "save" else action
        if got == n.ok:                     # the hook chose Open or Save itself
            if self.put:
                if name and self.dir is not None:
                    return ("save", name, isinstance(self.dir.child(name), File))
            elif 0 <= row < len(self.entries):
                return ("open", row)
        elif got == n.cancel:
            return ("cancel",)
        elif got == sfHookRebuildList:
            return ("rebuild",)
        return ("item", got)

    def _wait(self, bridge, fut):
        """The user's next step; other applications run meanwhile."""
        t = self.t
        with t.yielding():
            while True:
                try:
                    return fut.result(timeout=0.1)
                except TimeoutError:
                    if t.process.events.killed or bridge.closed:
                        raise ProcessKilled() from None

    def _step(self, action: tuple):
        kind = action[0]
        if kind == "cancel":
            return None
        if kind == "item":
            return SAME
        if kind == "desktop":
            self.dir = None
        elif kind == "up":
            for _ in range(action[1]):
                self.dir = self.dir.parent if self.dir is not None else None
        elif kind == "open":
            node = self.entries[action[1]]
            if isinstance(node, fm.Volume):
                self.dir = node.root
            elif isinstance(node, Dir):
                self.dir = node
            else:
                return ("open", node)
        elif kind == "new_folder":
            d = self.dir
            try:
                self.dir = self.files.create(d.volume.vref, d.cnid, action[1], directory=True)
            except FileError as e:
                self.message = ("That name is already taken." if e.code == fm.dupFNErr else
                                f"The folder couldn’t be created (error {e.code}).")
        elif kind == "save":
            return ("save", self.dir, action[1], action[2])
        return MORE

    def _remember(self) -> None:
        """Where the next dialog starts."""
        d = self.dir
        if d is not None:
            world = self.t.process.world
            world.w16(lowmem.SFSaveDisk, -d.volume.vref)
            world.w32(lowmem.CurDirStore, d.cnid)


def display_of(t):
    return getattr(t.tb, "_d", None)


def _types(t, count: int, p: int) -> set[bytes] | None:
    if count < 0 or not p:
        return None
    return {t.mem.read(p + 4 * i, 4) for i in range(min(count, MAX_TYPES))}


def _str(t, p: int) -> str:
    return t.str255(p) if p else ""


# =========================================================================
# The original calls: an SFReply, with a working directory for the folder
# =========================================================================
def _sf_reply(t, reply: int, result) -> None:
    m = t.mem
    if result is None:
        m.w8(reply + rGood, 0)
        return
    if result[0] == "open":
        node = result[1]
        d, name, ftype = node.parent, node.name, node.type
    else:
        _, d, name, _ = result
        ftype = bytes(4)
    files = t.process.files
    m.fill(reply, SF_REPLY)
    m.w8(reply + rGood, 1)
    m.write(reply + rFType, ftype)
    m.w16(reply + rVRefNum, files.fs.open_wd(d.volume, d.cnid, fm.ERIK, files))
    m.w_pstr(reply + rFName, to_mac(name)[:63])


def _sf_get(t, reply, file_filter, num_types, type_list, hook=0, dlg_id=DIALOG_IDS[False]):
    dialog = Dialog(t, False, file_filter=file_filter, types=_types(t, num_types, type_list),
                    hook=hook, dlg_id=dlg_id)
    _sf_reply(t, reply & ADDRESS_MASK, dialog.run())


def _sf_put(t, reply, prompt, orig_name, hook=0, dlg_id=DIALOG_IDS[True]):
    dialog = Dialog(t, True, _str(t, prompt), _str(t, orig_name), hook=hook, dlg_id=dlg_id)
    _sf_reply(t, reply & ADDRESS_MASK, dialog.run())


@trap
def SFGetFile(t, where, prompt, file_filter, num_types, type_list, dlg_hook, reply):
    _sf_get(t, reply, file_filter, num_types, type_list, dlg_hook)


@trap
def SFPGetFile(t, where, prompt, file_filter, num_types, type_list, dlg_hook, reply, dlg_id,
               filter_proc):
    _sf_get(t, reply, file_filter, num_types, type_list, dlg_hook, dlg_id)


@trap
def SFPutFile(t, where, prompt, orig_name, dlg_hook, reply):
    _sf_put(t, reply, prompt, orig_name, dlg_hook)


@trap
def SFPPutFile(t, where, prompt, orig_name, dlg_hook, reply, dlg_id, filter_proc):
    _sf_put(t, reply, prompt, orig_name, dlg_hook, dlg_id)


# =========================================================================
# System 7's: a StandardFileReply, with an FSSpec
# =========================================================================
def _standard_reply(t, reply: int, result) -> None:
    m = t.mem
    reply &= ADDRESS_MASK
    m.fill(reply, STANDARD_REPLY)
    if result is None:
        return
    if result[0] == "open":
        node = result[1]
        d, name, replacing = node.parent, node.name, False
        m.write(reply + sfType, node.type)
        m.w16(reply + sfFlags, node.flags)
    else:
        _, d, name, replacing = result
        existing = d.child(name)
        replacing = replacing or isinstance(existing, File)
        if existing is not None:
            name = existing.name
    m.w8(reply + sfGood, 1)
    m.w8(reply + sfReplacing, int(replacing))
    m.w16(reply + sfFile, d.volume.vref)
    m.w32(reply + sfFile + 2, d.cnid)
    m.w_pstr(reply + sfFile + 6, to_mac(name)[:63])


@trap
def StandardGetFile(t, file_filter, num_types, type_list, reply):
    dialog = Dialog(t, False, file_filter=file_filter, types=_types(t, num_types, type_list),
                    folders=True)
    _standard_reply(t, reply, dialog.run())


@trap
def StandardPutFile(t, prompt, default_name, reply):
    _standard_reply(t, reply, Dialog(t, True, _str(t, prompt), _str(t, default_name)).run())


@trap
def CustomGetFile(t, file_filter, num_types, type_list, reply, dlg_id, where, dlg_hook,
                  filter_proc, active_list, activate_proc, your_data):
    dialog = Dialog(t, False, file_filter=file_filter, types=_types(t, num_types, type_list),
                    data=your_data, folders=True, hook=dlg_hook, dlg_id=dlg_id, new=True)
    _standard_reply(t, reply, dialog.run())


@trap
def CustomPutFile(t, prompt, default_name, reply, dlg_id, where, dlg_hook, filter_proc,
                  active_list, activate_proc, your_data):
    dialog = Dialog(t, True, _str(t, prompt), _str(t, default_name), data=your_data,
                    hook=dlg_hook, dlg_id=dlg_id, new=True)
    _standard_reply(t, reply, dialog.run())
