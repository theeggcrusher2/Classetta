"""The Dialog Manager, driven by Dialogs (tests/apps/dialogs.s): a 68k
application with a modal dialog and a stop alert, answered with synthetic
clicks and keys."""

from pathlib import Path
from types import SimpleNamespace

import pytest
from conftest import Recorder
from PyQt6.QtCore import Qt
from PyQt6.QtTest import QTest

from macrt.m68k import glue_dialogs
from macrt.macfile import open_application
from macrt.toolbox import constants as k
from macrt.toolbox.controls import Control
from macrt.toolbox.dialogs import HIDDEN, Dialog, items, substitute
from macrt.toolbox.templates import editText, statText
from macrt.toolbox.types import Rect

APPS = Path(__file__).parent / "apps"
DIALOG, ITEM, COUNT, TEXT, ALERTED, STAGE, USERCALLS = -230, -232, -234, -504, -506, -508, -510


class App:
    def __init__(self, h):
        f = open_application(APPS / "Dialogs.bin")
        h.launch(Recorder("Other"))
        self.h = h
        self.proc = h.d.launch(f.name, None, app=f)
        self.mem = self.proc.system.mem
        self.a5 = self.proc.partition.a5
        h.wait_until(lambda: self.word(STAGE) == 1 and self.word(USERCALLS) > 0, timeout=10,
                     what="the dialog")
        self.window = self.proc.windows[0]
        h.wait_until(lambda: self.black(210, 150), what="the OK button drawn")

    def word(self, offset):
        return self.mem.r16s(self.a5 + offset)

    def items(self, w=None):
        return items(self.mem, Dialog(self.mem, (w or self.window).addr).items)

    def black(self, h, v, w=None):
        w = w or self.window
        with w.backing.lock:
            return w.backing.image.pixel(h, v) & 0xFFFFFF == 0

    def any_black(self, r, w=None):
        return any(self.black(h, v, w) for v in range(r.top, r.bottom)
                   for h in range(r.left, r.right))

    def click(self, h, v, w=None):
        c = (w or self.window).content
        self.h.click(c.left + h, c.top + v)

    def finish(self, key=Qt.Key.Key_Return):
        """Dismiss the dialog with a key; the alert comes up."""
        self.h.key(key)
        self.h.wait_until(lambda: self.word(STAGE) == 2, what="the dialog dismissed")
        self.h.wait_until(lambda: self.proc.windows and self.proc.windows[0] is not self.window
                          and self.proc.windows[0].visible, what="the alert")
        return self.proc.windows[0]


@pytest.fixture
def app(harness):
    return App(harness())


def test_the_dialog_is_made_and_drawn(app):
    w = app.window
    assert w.window_kind == k.dialogKind
    assert app.mem.r32(app.a5 + DIALOG) == w.addr
    found = app.items()
    assert len(found) == 7
    ok, cancel, hello, edit, check, user, hidden = found
    assert Control(app.mem, ok.handle).title == "OK"
    assert Control(app.mem, cancel.handle).title == "Cancel"
    assert w.control_list in (ok.handle, cancel.handle, check.handle)
    assert hello.type == statText and app.proc.runner.tb.contents(hello.handle) == b"Hello, ^0!"
    assert edit.type == editText and app.proc.runner.tb.contents(edit.handle) == b"Edit me"
    assert not user.enabled and user.handle != 0           # SetDialogItem put the procedure there
    assert hidden.hidden and hidden.rect.left == 10 + HIDDEN
    # The fixture saw the OK button drawn; the items after it may still be drawing.
    app.h.wait_until(lambda: app.any_black(hello.rect)      # its text, with ParamText's
                     and app.black(220, 95)                 # the user item painted itself
                     and app.black(10, 50) and app.black(202, 50),  # the edit field's frame
                     what="the items drawn")
    assert not app.any_black(hidden.rect.offset(-HIDDEN, 0))
    assert app.proc.runner.unimplemented_calls == {}


def test_the_controls_are_drawn_over_the_text(monkeypatch):
    """Static text erases its rect before it's drawn, and Font Mover's
    help has the last of it run into the OK button: the button's drawn
    after, and whole."""
    ok = SimpleNamespace(number=1, is_control=True, rect=Rect(239, 171, 255, 261))
    hints = [SimpleNamespace(number=n, is_control=False, rect=Rect(top, 5, bottom, 425))
             for n, top, bottom in ((2, 13, 29), (3, 42, 95), (4, 91, 170), (5, 158, 250))]
    drawn = []
    monkeypatch.setattr(glue_dialogs, "_items", lambda t, w: [ok, *hints])
    monkeypatch.setattr(glue_dialogs, "_draw_item", lambda t, w, item: drawn.append(item.number))
    w = SimpleNamespace(dialog=SimpleNamespace(icon=-1, outline=False))
    glue_dialogs.draw_dialog(None, w)
    assert drawn == [2, 3, 4, 5, 1]
    drawn.clear()
    glue_dialogs.draw_dialog(None, w, Rect(240, 0, 260, 200))
    assert drawn == [5, 1]


def test_param_text_substitution():
    assert substitute(b"Hello, ^0! ^1^3^9", [b"World", b"a", b"", b"c"]) == b"Hello, World! ac^9"


def test_the_check_box_returns_its_item(app):
    check = Control(app.mem, app.items()[4].handle)
    app.click(30, 84)
    app.h.wait_until(lambda: app.word(COUNT) == 1, what="ModalDialog to return")
    assert app.word(ITEM) == 5
    app.h.wait_until(lambda: check.value == 1, what="the application to check it")
    app.h.wait_until(lambda: app.black(15, 81), what="the X")
    app.click(220, 95)                                      # the disabled user item
    app.click(5, 170)                                       # no item
    app.click(30, 84)
    app.h.wait_until(lambda: app.word(COUNT) == 2, what="it again")
    app.h.wait_until(lambda: check.value == 0, what="the application to uncheck it")
    assert app.word(STAGE) == 1


def test_clicking_ok(app):
    app.click(245, 150)
    app.h.wait_until(lambda: app.word(STAGE) == 2, what="the dialog dismissed")
    assert app.word(ITEM) == 1
    assert app.mem.r_pstr(app.a5 + TEXT) == b"Edit me"


def test_typing_into_the_edit_field(app):
    """TextEdit edits the field; ModalDialog returns its item for the click
    and each key, and the application reads the text back on OK."""
    app.click(195, 53)                                      # past the end of "Edit me"
    QTest.keyClicks(app.h.d.screen_widget, " too")
    app.h.wait_until(lambda: app.word(COUNT) == 5, what="the click, then a key at a time")
    assert app.proc.runner.tb.contents(app.items()[3].handle) == b"Edit me too"
    assert app.word(ITEM) == 4 and app.word(STAGE) == 1
    app.h.key(Qt.Key.Key_Backspace)
    app.finish(Qt.Key.Key_Return)
    assert app.word(ITEM) == 1
    assert app.mem.r_pstr(app.a5 + TEXT) == b"Edit me to"
    assert app.proc.runner.unimplemented_calls == {}


def test_return_presses_the_default_item(app):
    app.finish(Qt.Key.Key_Return)
    assert app.word(ITEM) == 1 and app.word(COUNT) == 1
    assert app.window not in list(app.proc.windows)


def test_escape_presses_the_cancel_item(app):
    app.finish(Qt.Key.Key_Escape)
    assert app.word(ITEM) == 2


def test_the_stop_alert(app):
    alert = app.finish()
    assert alert.dialog is not None and alert.dialog.icon == 0
    assert alert.window_kind == k.dialogKind
    app.h.wait_until(lambda: app.black(25, 26, alert), what="the stop icon")
    assert not app.black(36, 20, alert)                     # the white bar in it
    assert app.black(217, 80, alert)                        # the bold outline around OK
    assert app.any_black(app.items(alert)[1].rect, alert)  # "All done."
    app.click(255, 80, alert)
    app.h.wait_until(lambda: app.word(STAGE) == 3, what="the alert dismissed")
    assert app.word(ALERTED) == 1
    assert list(app.proc.windows) == []
    assert app.proc.runner.unimplemented_calls == {}


def test_return_answers_the_alert(app):
    app.finish()
    app.h.key(Qt.Key.Key_Return)
    app.h.wait_until(lambda: app.word(STAGE) == 3, what="the alert dismissed")
    assert app.word(ALERTED) == 1
