"""
macrt.ui.keymap - Qt key events to Mac keyDown messages and modifiers.

The host's Ctrl key acts as Command (on macOS Qt already reports the
Command key as Ctrl), Alt is Option and Meta is Control.
"""

from __future__ import annotations

from PyQt6.QtCore import Qt
from PyQt6.QtGui import QKeyEvent

from ..toolbox.constants import alphaLock, btnState, cmdKey, controlKey, optionKey, shiftKey

K = Qt.Key

# Mac character codes for keys that don't produce text.
SPECIAL_CHARS = {
    K.Key_Return: 0x0D, K.Key_Enter: 0x03, K.Key_Backspace: 0x08, K.Key_Tab: 0x09,
    K.Key_Escape: 0x1B, K.Key_Left: 0x1C, K.Key_Right: 0x1D, K.Key_Up: 0x1E,
    K.Key_Down: 0x1F, K.Key_Delete: 0x7F, K.Key_Home: 0x01, K.Key_End: 0x04,
    K.Key_PageUp: 0x0B, K.Key_PageDown: 0x0C,
}

# Apple virtual key codes (the same on ADB keyboards and in macOS).
_KEYCODES = {
    "a": 0x00, "s": 0x01, "d": 0x02, "f": 0x03, "h": 0x04, "g": 0x05, "z": 0x06,
    "x": 0x07, "c": 0x08, "v": 0x09, "b": 0x0B, "q": 0x0C, "w": 0x0D, "e": 0x0E,
    "r": 0x0F, "y": 0x10, "t": 0x11, "1": 0x12, "2": 0x13, "3": 0x14, "4": 0x15,
    "6": 0x16, "5": 0x17, "=": 0x18, "9": 0x19, "7": 0x1A, "-": 0x1B, "8": 0x1C,
    "0": 0x1D, "]": 0x1E, "o": 0x1F, "u": 0x20, "[": 0x21, "i": 0x22, "p": 0x23,
    "l": 0x25, "j": 0x26, "'": 0x27, "k": 0x28, ";": 0x29, "\\": 0x2A, ",": 0x2B,
    "/": 0x2C, "n": 0x2D, "m": 0x2E, ".": 0x2F, " ": 0x31, "`": 0x32,
}
SPECIAL_KEYCODES = {
    K.Key_Return: 0x24, K.Key_Tab: 0x30, K.Key_Backspace: 0x33, K.Key_Enter: 0x34,
    K.Key_Escape: 0x35, K.Key_Left: 0x7B, K.Key_Right: 0x7C, K.Key_Down: 0x7D,
    K.Key_Up: 0x7E, K.Key_Delete: 0x75, K.Key_Home: 0x73, K.Key_End: 0x77,
    K.Key_PageUp: 0x74, K.Key_PageDown: 0x79,
}

# The keypad's keys, which have codes of their own (games read them from
# the KeyMap: The Duel steers by 8, 2, 4 and 6).
KEYPAD_KEYCODES = {
    "0": 0x52, "1": 0x53, "2": 0x54, "3": 0x55, "4": 0x56, "5": 0x57, "6": 0x58,
    "7": 0x59, "8": 0x5B, "9": 0x5C, ".": 0x41, "-": 0x4E,
    "*": 0x43, "+": 0x45, "/": 0x4B, "=": 0x51,
}
# The Macintosh Plus keyboard's (keyboard type 3) arrow keys, which aren't
# ADB's 0x7B-0x7E; its keypad's +, *, / and = send the same codes with
# Shift (IM IV-4, IV-250).
PLUS_KEYBOARD = 3
PLUS_ARROWS = {K.Key_Left: 0x46, K.Key_Right: 0x42, K.Key_Up: 0x4D, K.Key_Down: 0x48}
PLUS_KEYPAD = {"+": 0x46, "*": 0x42, "/": 0x4D, "=": 0x48}
_PLUS_ARROW_CHARS = {0x46: (0x1C, "+"), 0x42: (0x1D, "*"), 0x4D: (0x1E, "/"),
                     0x48: (0x1F, "=")}


# What the keys make, the other way round (KeyTranslate), on a US keyboard.
_SHIFTED = dict(zip("1234567890-=[]\\;',./`", '!@#$%^&*()_+{}|:"<>?~'))
_KEY_CHARS = {code: ch for ch, code in _KEYCODES.items()}
_SPECIAL_KEY_CHARS = {
    0x24: 0x0D, 0x30: 0x09, 0x33: 0x08, 0x34: 0x03, 0x35: 0x1B, 0x4C: 0x03,
    0x7B: 0x1C, 0x7C: 0x1D, 0x7D: 0x1F, 0x7E: 0x1E, 0x75: 0x7F, 0x73: 0x01,
    0x77: 0x04, 0x74: 0x0B, 0x79: 0x0C, 0x47: 0x1B,
}
_KEYPAD = dict(zip((0x52, 0x53, 0x54, 0x55, 0x56, 0x57, 0x58, 0x59, 0x5B, 0x5C,
                    0x41, 0x43, 0x45, 0x4E, 0x4B, 0x51), "0123456789.*+-/="))


def translate(code: int, modifiers: int) -> int:
    """The character a key makes with these modifiers (the high byte of
    an event's modifiers), or 0 for none: the US layout."""
    if code in _SPECIAL_KEY_CHARS:
        return _SPECIAL_KEY_CHARS[code]
    if code in _PLUS_ARROW_CHARS:
        arrow, shifted = _PLUS_ARROW_CHARS[code]
        return ord(shifted) if modifiers << 8 & shiftKey else arrow
    ch = _KEYPAD.get(code) or _KEY_CHARS.get(code)
    if ch is None:
        return 0
    mods = modifiers << 8
    if mods & controlKey and ch.isalpha():
        return ord(ch) & 0x1F
    if mods & shiftKey:
        ch = _SHIFTED.get(ch, ch.upper())
    elif mods & alphaLock:
        ch = ch.upper()
    return ord(ch)


def modifiers_from_qt(mods: Qt.KeyboardModifier, button_down: bool) -> int:
    m = 0 if button_down else btnState
    if mods & Qt.KeyboardModifier.ControlModifier:
        m |= cmdKey
    if mods & Qt.KeyboardModifier.ShiftModifier:
        m |= shiftKey
    if mods & Qt.KeyboardModifier.AltModifier:
        m |= optionKey
    if mods & Qt.KeyboardModifier.MetaModifier:
        m |= controlKey
    return m


def key_message(ev: QKeyEvent, keyboard: int = PLUS_KEYBOARD) -> int | None:
    """keyDown message: (key code << 8) | character code, or None to ignore.
    keyboard is the type of the Mac's keyboard, as Gestalt says it."""
    key = ev.key()
    command_like = bool(ev.modifiers() & (Qt.KeyboardModifier.ControlModifier
                                          | Qt.KeyboardModifier.MetaModifier))
    if key in SPECIAL_CHARS:
        char = SPECIAL_CHARS[key]
    else:
        text = ev.text()
        if text and text.isprintable() and not command_like:
            ch = text[0]
        elif 0x20 <= key <= 0x7E:
            ch = chr(key)
            if not ev.modifiers() & Qt.KeyboardModifier.ShiftModifier:
                ch = ch.lower()
        else:
            return None
        try:
            char = ch.encode("mac_roman")[0]
        except UnicodeEncodeError:
            return None
    plus = keyboard == PLUS_KEYBOARD
    code = PLUS_ARROWS.get(key) if plus else None
    if code is None:
        code = SPECIAL_KEYCODES.get(key)
    if code is None and ev.modifiers() & Qt.KeyboardModifier.KeypadModifier:
        code = (plus and PLUS_KEYPAD.get(chr(char))) or KEYPAD_KEYCODES.get(chr(char))
    if code is None:
        code = _KEYCODES.get(chr(char).lower() if char < 0x80 else "", 0)
    return (code << 8) | char
