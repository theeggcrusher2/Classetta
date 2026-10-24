"""
macrt.ui.automation - Driving the running apps from a Python script.

`Controller` wraps a Display and synthesizes the same input the
InputRouter posts for real Qt events: it updates the Event Manager's
low-memory record of the mouse and keyboard (Mouse, MBState, KeyMap,
KeyLast) and posts EventRecords to the target process's queue.  The
guest sees it exactly as typed input: MenuKey, MenuSelect trackers and
programs that poll the KeyMap all hear it.

    macrt = Controller(display)
    macrt.wait_window()
    macrt.press("f+cmd")
    macrt.press("return")
    macrt.screenshot("after.png")

Everything that touches widgets or the guest's memory is marshalled onto
the GUI thread with the bridge; `sleep` and `wait` run on the script
thread and leave the GUI event loop running.  Coordinates are Mac
global, as in the test harness (conftest.Harness).
"""

from __future__ import annotations

import time

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import QApplication

from ..toolbox.constants import (btnState, cmdKey, controlKey, keyDown, keyUp, mouseDown,
                                 mouseUp, optionKey, shiftKey)
from ..toolbox.events import EventRecord
from ..toolbox.types import Point
from . import keymap
from .keymap import PLUS_KEYBOARD, PLUS_ARROWS, SPECIAL_CHARS, SPECIAL_KEYCODES, _KEYCODES

_MODS = {"cmd": cmdKey, "command": cmdKey, "shift": shiftKey,
         "option": optionKey, "alt": optionKey, "ctrl": controlKey, "control": controlKey}

# Named keys, to the Qt key the keymap tables are indexed by.
_NAMES = {
    "return": Qt.Key.Key_Return, "enter": Qt.Key.Key_Enter, "tab": Qt.Key.Key_Tab,
    "esc": Qt.Key.Key_Escape, "escape": Qt.Key.Key_Escape,
    "backspace": Qt.Key.Key_Backspace, "delete": Qt.Key.Key_Delete,
    "space": Qt.Key.Key_Space, "home": Qt.Key.Key_Home, "end": Qt.Key.Key_End,
    "pageup": Qt.Key.Key_PageUp, "pagedown": Qt.Key.Key_PageDown,
    "left": Qt.Key.Key_Left, "right": Qt.Key.Key_Right,
    "up": Qt.Key.Key_Up, "down": Qt.Key.Key_Down,
}


def parse_key(spec: str, keyboard: int = PLUS_KEYBOARD) -> tuple[int, int]:
    """A key spec like "f", "return" or "f+cmd" to its keyDown message
    ((key code << 8) | character) and the Mac modifier bits asked for."""
    mods, name = 0, None
    for part in spec.lower().split("+"):
        part = part.strip()
        if not part:
            continue
        if part in _MODS:
            mods |= _MODS[part]
        elif name is None:
            name = part
        else:
            raise ValueError(f"{spec!r}: more than one key")
    if name is None:
        raise ValueError(f"{spec!r}: no key")
    qt = _NAMES.get(name)
    if qt is not None:
        char = SPECIAL_CHARS[qt]
        code = PLUS_ARROWS.get(qt) if keyboard == PLUS_KEYBOARD else None
        if code is None:
            code = SPECIAL_KEYCODES[qt]
    elif len(name) == 1 and name in _KEYCODES:
        char, code = ord(name), _KEYCODES[name]
    else:
        raise ValueError(f"{spec!r}: no such key")
    return (code << 8) | char, mods


class Controller:
    """Script-facing input and observation for a Display.  Input goes to
    the front process by default; `attach(name)` picks another."""

    def __init__(self, display):
        self.d = display
        self._keyboard = display.system.model.keyboard
        self._target = None

    # -- time --------------------------------------------------------------
    def sleep(self, seconds: float) -> None:
        time.sleep(seconds)

    def wait(self, cond, timeout: float = 10.0, what: str = "condition") -> None:
        """Poll cond (a function, run on the GUI thread) until it's true."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self.d.bridge.call(cond):
                return
            time.sleep(0.02)
        raise AssertionError(f"timed out waiting for {what}")

    # -- processes and windows ---------------------------------------------
    def processes(self) -> list[str]:
        return self.d.bridge.call(lambda: [p.name for p in self.d.processes])

    def front(self) -> str | None:
        return self.d.bridge.call(lambda: getattr(self.d.front_process, "name", None))

    def attach(self, name: str) -> None:
        """Send input to the named app whether or not it's front."""
        def pick():
            for p in self.d.processes:
                if p.name == name:
                    self._target = p
                    return
            raise KeyError(name)
        self.d.bridge.call(pick)

    def windows(self) -> list[tuple[str, str]]:
        """(app, title) for each visible window of the target process."""
        return self.d.bridge.call(self._window_list)

    def wait_window(self, timeout: float = 30.0, title: str | None = None) -> None:
        """Wait for the target process to have a visible window (with this
        title, if given)."""
        self.wait(lambda: any(t == title for _, t in self._window_list())
                  if title is not None else bool(self._window_list()),
                  timeout, f"a window{' titled ' + repr(title) if title else ''}")

    def _window_list(self):
        proc = self._proc()
        return [(proc.name, w.title) for w in proc.windows if w.visible] if proc else []

    # -- keys --------------------------------------------------------------
    def key_down(self, spec: str) -> None:
        msg, mods = parse_key(spec, self._keyboard)
        self.d.bridge.call(self._post_key, msg, mods, True)

    def key_up(self, spec: str) -> None:
        msg, mods = parse_key(spec, self._keyboard)
        self.d.bridge.call(self._post_key, msg, mods, False)

    def press(self, spec: str, hold: float = 0.02) -> None:
        """A whole key press: down, a beat, up."""
        self.key_down(spec)
        time.sleep(hold)
        self.key_up(spec)

    def type_text(self, text: str, hold: float = 0.02) -> None:
        for ch in text:
            self.press({" ": "space", "\n": "return", "\t": "tab"}.get(ch, ch), hold)

    def _post_key(self, msg: int, mods: int, down: bool) -> None:
        d = self.d
        d.input.modifiers = mods | (0 if down else btnState)
        d.system.input.modifiers(d.input.modifiers)
        d.system.input.key(msg, down)
        if d.tracker is not None:
            return
        proc = self._proc()
        if proc is not None:
            proc.events.post(EventRecord(keyDown if down else keyUp, msg, 0,
                                         d.input.mac_pos, d.input.modifiers))

    # -- mouse, in Mac global coordinates ------------------------------------
    def mouse_move(self, h: int, v: int) -> None:
        self.d.bridge.call(self._move, h, v)

    def mouse_down(self, h: int, v: int) -> None:
        def press():
            self._set_input(h, v, True)
            g = self.d.global_from_mac(Point(v, h))
            self.d.mouse_down(QApplication.widgetAt(g))
        self.d.bridge.call(press)

    def mouse_up(self, h: int, v: int) -> None:
        self.d.bridge.call(self._set_input, h, v, False)
        self.d.bridge.call(self._release)

    def click(self, h: int, v: int, hold: float = 0.02) -> None:
        self.mouse_down(h, v)
        time.sleep(hold)
        self.mouse_up(h, v)

    def _move(self, h: int, v: int) -> None:
        d = self.d
        d.input.global_pos = d.global_from_mac(Point(v, h))
        d.input.mac_pos = Point(h, v)
        d.system.input.mouse(d.input.mac_pos, d.input.down)

    def _set_input(self, h: int, v: int, down: bool) -> None:
        d = self.d
        if down:
            d.input.press_time = time.monotonic()
        d.input.down = down
        d.input.modifiers = d.input.modifiers | (0 if down else btnState)
        self._move(h, v)
        d.system.input.modifiers(d.input.modifiers)

    def _release(self) -> None:
        d = self.d
        if d.tracker is not None:
            # Tracking calls eat the mouseUp, as WaitMouseUp does.
            d.tracker.release(d.input.global_pos)
            return
        if d._swallow_release:
            d._swallow_release = False
            return
        proc = self._proc()
        if proc is not None:
            proc.events.post(EventRecord(mouseUp, 0, 0, d.input.mac_pos, d.input.modifiers))

    # -- looking -------------------------------------------------------------
    def screenshot(self, path: str) -> None:
        """Save what's on the emulated screen to a PNG."""
        def grab():
            if self.d.screen_widget is None:
                raise RuntimeError("screenshot needs host='screen'")
            self.d.screen_widget.grab().save(path)
        self.d.bridge.call(grab)

    def quit(self) -> None:
        self.d.bridge.call(self.d.shutdown)

    # =====================================================================
    def _proc(self):
        if self._target is not None and self._target in self.d.processes:
            return self._target
        return self.d.front_process
