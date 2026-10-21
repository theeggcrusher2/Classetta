"""
macrt.ui.menubar - The menu bar, pull-down menus and MenuSelect tracking.

A MenuBarWidget shows one process's menus.  Where it lives depends on the
display's `menu_bar` setting: one shared bar for the front app ("global"),
or one bar under each window's title bar ("window").  Neither the bar nor
the pull-downs act on a click.  The guest gets a mouseDown, calls FindWindow,
then MenuSelect, and MenuTracker answers that call.
"""

from __future__ import annotations

import time
from concurrent.futures import Future
from typing import TYPE_CHECKING, Callable

from PyQt6.QtCore import QPoint, QRect, Qt, QTimer
from PyQt6.QtGui import QColor, QFont, QFontMetrics, QImage, QPainter, QPen, QRegion
from PyQt6.QtWidgets import QWidget

from ..toolbox.constants import checkMark, kApplicationMenuID
from ..toolbox.menus import Menu, MenuItem
from ..toolbox.qdraw import char_codes
from ..toolbox.types import Point, Rect
from . import theme
from .chrome import paint_dither
from .theme import BLACK, WHITE

if TYPE_CHECKING:
    from ..process import Process
    from ..toolbox.bitfont import Strike
    from ..toolbox.screen import Screen
    from .display import Display


def _item_font(item: MenuItem) -> QFont:
    font = QFont(theme.menu_item_font())
    if item.style & theme.STYLE_ITALIC:
        font.setItalic(True)
    if item.style & theme.STYLE_UNDERLINE:
        font.setUnderline(True)
    return font


def _title_width(menu: Menu, metrics: QFontMetrics, strike: Strike | None = None) -> int:
    if menu.is_apple:
        return theme.bitmap_size(theme.APPLE_ICON)[0]
    if menu.id == kApplicationMenuID:
        return theme.bitmap_size(theme.GENERIC_APP_ICON)[0]
    if strike is not None:
        return strike.width(char_codes(menu.title))
    return metrics.horizontalAdvance(menu.title)


def _draw_strike(p: QPainter, strike: Strike, text: str, r: QRect, color: QColor,
                 face: int = 0, centered: bool = False) -> None:
    """Text in an application's own system font, centred in r or from its
    left, with its baseline where the Menu Manager puts it."""
    codes = char_codes(text)
    img, pen, baseline = strike.render(codes, face)
    if color != BLACK:
        tint = QPainter(img)
        tint.setCompositionMode(QPainter.CompositionMode.CompositionMode_SourceIn)
        tint.fillRect(img.rect(), color)
        tint.end()
    x = r.x() + ((r.width() - strike.width(codes, face)) // 2 if centered else 0)
    y = r.y() + (r.height() - strike.ascent - strike.descent) // 2 + strike.ascent
    p.drawImage(x - pen, y - baseline, img)


class MenuBarWidget(QWidget):
    def __init__(self, display: Display, parent: QWidget | None = None, *,
                 top_level: bool = False, rounded: bool = False):
        flags = Qt.WindowType.Widget
        if top_level:
            flags = (Qt.WindowType.Tool | Qt.WindowType.FramelessWindowHint
                     | Qt.WindowType.WindowStaysOnTopHint
                     | Qt.WindowType.WindowDoesNotAcceptFocus
                     | Qt.WindowType.NoDropShadowWindowHint)
        super().__init__(parent, flags)
        self.display = display
        self._process: Process | None = None
        self.hilite_id = 0
        self.rounded = rounded
        self.setProperty("macrt_role", "menubar")
        self.setMouseTracking(True)
        self.setFixedHeight(theme.MENU_BAR_HEIGHT)
        if top_level:
            self.setWindowTitle("Menu Bar")

    @property
    def process(self) -> Process | None:
        """Whose menus the bar shows."""
        return self._process

    @process.setter
    def process(self, proc: Process | None) -> None:
        if proc is not self._process:
            self._process = proc
            self.hilite_id = 0          # a highlighted title was the last app's
            mirror = self._mirror()
            if mirror is not None:
                mirror.bar_drawn()      # the next app's bar is drawn afresh

    def system_strike(self) -> Strike | None:
        """The application's own Chicago 12, to draw its menus in."""
        return getattr(self.process, "system_strike", None)

    # -- layout -----------------------------------------------------------
    def menus(self) -> list[Menu]:
        if self.process is None:
            return []
        menus = list(self.process.menus)
        app_menu = self.display.application_menu_for(self)
        if app_menu is not None:
            menus.append(app_menu)
        return menus

    def cells(self) -> list[tuple[Menu, QRect]]:
        metrics = QFontMetrics(theme.menu_bar_font())
        pad = theme.MENU_TITLE_PADDING
        h = theme.MENU_BAR_HEIGHT - 1
        x = theme.MENU_BAR_LEFT
        strike = self.system_strike()
        out = []
        for menu in self.menus():
            w = _title_width(menu, metrics, strike) + 2 * pad
            if menu.id == kApplicationMenuID:
                out.append((menu, QRect(self.width() - theme.MENU_BAR_LEFT - w, 0, w, h)))
            else:
                out.append((menu, QRect(x, 0, w, h)))
                x += w
        return out

    def natural_width(self) -> int:
        cells = self.cells()
        right = max((c.right() for m, c in cells if m.id != kApplicationMenuID), default=0)
        return right + theme.MENU_BAR_LEFT

    def cell_at(self, pos: QPoint) -> int:
        for i, (_, cell) in enumerate(self.cells()):
            if cell.contains(pos):
                return i
        return -1

    def set_hilite(self, menu_id: int) -> None:
        if menu_id != self.hilite_id:
            self.hilite_id = menu_id
            self.update()

    # -- painting ---------------------------------------------------------
    def _mirror(self) -> Screen | None:
        """The screen buffer, for the bar on an emulated screen."""
        return getattr(self.parentWidget(), "mirror", None)

    def paintEvent(self, event):  # noqa: N802
        p = QPainter(self)
        self._paint_bar(p)
        mirror = self._mirror()
        over, bits = mirror.bar_shown() if mirror is not None else (None, None)
        if over is not None and not over.empty:
            # What the program drew over the bar (the bar is at the screen's top left).
            box = over.bbox
            p.setClipRegion(over.to_qregion())
            p.drawImage(QPoint(box.left, box.top), bits.image(box))

    def image(self) -> QImage:
        """The bar as DrawMenuBar draws it in the screen buffer: without
        what a program has drawn over it."""
        img = QImage(self.width(), self.height(), QImage.Format.Format_RGB32)
        p = QPainter(img)
        self._paint_bar(p, mono=True)
        p.end()
        return img

    def _paint_bar(self, p: QPainter, mono: bool = False) -> None:
        w, h = self.width(), self.height()
        p.fillRect(0, 0, w, h, WHITE)
        p.fillRect(0, h - 1, w, 1, BLACK)
        font = theme.menu_bar_font()
        p.setFont(font)
        strike = self.system_strike()
        for menu, cell in self.cells():
            inverted = menu.id == self.hilite_id
            fg = WHITE if inverted else BLACK
            if inverted:
                p.fillRect(cell, BLACK)
            if menu.is_apple:
                rainbow = (theme.APPLE_RAINBOW if theme.RAINBOW_APPLE and not (inverted or mono)
                           else None)
                self._draw_icon(p, cell, theme.APPLE_ICON, fg, rainbow)
            elif menu.id == kApplicationMenuID:
                self._draw_icon(p, cell, theme.GENERIC_APP_ICON, fg)
            elif strike is not None:
                _draw_strike(p, strike, menu.title, cell, fg, centered=True)
            else:
                p.setPen(QPen(fg))
                p.drawText(cell, Qt.AlignmentFlag.AlignCenter, menu.title)
            if not menu.enabled:
                paint_dither(p, cell.adjusted(1, 1, -1, -1), BLACK if inverted else WHITE)
        if self.rounded:
            self._draw_screen_corners(p, w)

    @staticmethod
    def _draw_icon(p, cell, bitmap, color, rows=None):
        bw, bh = theme.bitmap_size(bitmap)
        theme.draw_bitmap(p, cell.x() + (cell.width() - bw) // 2,
                          cell.y() + (cell.height() - bh) // 2, bitmap, color, rows)

    @staticmethod
    def _draw_screen_corners(p, w):
        left = theme.SCREEN_CORNER
        right = [row[::-1] for row in left]
        theme.draw_bitmap(p, 0, 0, left)
        theme.draw_bitmap(p, w - len(right[0]), 0, right)


class Pulldown(QWidget):
    """One pulled-down menu.  A Qt popup so it can hang below the bar."""

    def __init__(self, menu: Menu, parent: QWidget, strike: Strike | None = None):
        super().__init__(parent, Qt.WindowType.Popup | Qt.WindowType.FramelessWindowHint
                         | Qt.WindowType.NoDropShadowWindowHint)
        self.menu = menu
        self.strike = strike            # the application's own Chicago 12, if it has one
        self.hilite = 0
        self.setProperty("macrt_role", "pulldown")
        self.setMouseTracking(True)
        self._inner_w = self._measure()
        h = max(len(menu.items), 0) * theme.MENU_ITEM_HEIGHT + 2
        w = self._inner_w + 2
        s = theme.MENU_SHADOW
        self.setFixedSize(w + s, h + s)
        if s:
            self.setMask(QRegion(0, 0, w, h).united(QRegion(s, h, w, s))
                         .united(QRegion(w, s, s, h)))

    def _cmd_width(self, item: MenuItem) -> int:
        if item.submenu:
            return theme.bitmap_size(theme.SUBMENU_GLYPH)[0]
        if not item.key:
            return 0
        gw = theme.bitmap_size(theme.COMMAND_GLYPH)[0]
        return gw + 2 + self._text_width(item, item.key.upper())

    def _text_width(self, item: MenuItem, text: str) -> int:
        if self.strike is not None:
            return self.strike.width(char_codes(text), item.style)
        return QFontMetrics(_item_font(item)).horizontalAdvance(text)

    def _measure(self) -> int:
        text_w = max((self._text_width(i, i.shown)
                      for i in self.menu.items if not i.is_separator), default=40)
        cmd_w = max((self._cmd_width(i) for i in self.menu.items), default=0)
        w = theme.MENU_LEFT_PAD + text_w + theme.MENU_RIGHT_PAD
        if cmd_w:
            w += theme.MENU_KEY_GAP + cmd_w
        return w

    def item_rect(self, index: int) -> QRect:
        return QRect(1, 1 + (index - 1) * theme.MENU_ITEM_HEIGHT,
                     self._inner_w, theme.MENU_ITEM_HEIGHT)

    def selectable_at(self, pos: QPoint) -> int:
        items = QRect(1, 1, self._inner_w, len(self.menu.items) * theme.MENU_ITEM_HEIGHT)
        if not items.contains(pos):
            return 0
        index = (pos.y() - 1) // theme.MENU_ITEM_HEIGHT + 1
        return index if self.menu.selectable(index) else 0

    def set_hilite(self, index: int) -> None:
        if index != self.hilite:
            self.hilite = index
            self.update()

    def paintEvent(self, event):  # noqa: N802
        p = QPainter(self)
        w = self._inner_w + 2
        h = self.height() - theme.MENU_SHADOW
        s = theme.MENU_SHADOW
        p.fillRect(0, 0, w, h, WHITE)
        p.fillRect(0, 0, w, 1, BLACK)
        p.fillRect(0, h - 1, w, 1, BLACK)
        p.fillRect(0, 0, 1, h, BLACK)
        p.fillRect(w - 1, 0, 1, h, BLACK)
        if s:
            p.fillRect(w, s, s, h, BLACK)
            p.fillRect(s, h, w, s, BLACK)
        for i, item in enumerate(self.menu.items, start=1):
            self._paint_item(p, i, item)

    def _paint_item(self, p: QPainter, index: int, item: MenuItem) -> None:
        r = self.item_rect(index)
        if item.is_separator:
            y = r.y() + r.height() // 2
            p.fillRect(r.x(), y, r.width(), 1, BLACK)
            paint_dither(p, QRect(r.x(), y, r.width(), 1))
            return
        inverted = index == self.hilite
        fg = WHITE if inverted else BLACK
        if inverted:
            p.fillRect(r, BLACK)
        font = _item_font(item)
        p.setFont(font)
        p.setPen(QPen(fg))
        mark = "" if item.submenu else item.mark      # a hierarchical item's is a menu ID
        if mark == checkMark:
            cw, ch = theme.bitmap_size(theme.CHECK_GLYPH)
            theme.draw_bitmap(p, r.x() + 3, r.y() + (r.height() - ch) // 2, theme.CHECK_GLYPH, fg)
        elif mark:
            self._draw_text(p, item, mark, QRect(r.x(), r.y(), theme.MENU_LEFT_PAD, r.height()),
                            fg, centered=True)
        text_r = r.adjusted(theme.MENU_LEFT_PAD, 0, -theme.MENU_RIGHT_PAD, 0)
        self._draw_text(p, item, item.shown, text_r, fg)
        if item.submenu:
            gw, gh = theme.bitmap_size(theme.SUBMENU_GLYPH)
            theme.draw_bitmap(p, text_r.right() - gw + 1, r.y() + (r.height() - gh) // 2,
                              theme.SUBMENU_GLYPH, fg)
        elif item.key:
            key = item.key.upper()
            kw = self._text_width(item, key)
            self._draw_text(p, item, key, QRect(text_r.right() - kw + 1, r.y(), kw, r.height()),
                            fg, centered=True)
            gw, gh = theme.bitmap_size(theme.COMMAND_GLYPH)
            theme.draw_bitmap(p, text_r.right() - kw - 2 - gw + 1,
                              r.y() + (r.height() - gh) // 2, theme.COMMAND_GLYPH, fg)
        if not (item.enabled and self.menu.enabled):
            paint_dither(p, r, BLACK if inverted else WHITE)

    def _draw_text(self, p: QPainter, item: MenuItem, text: str, r: QRect, fg: QColor,
                   centered: bool = False) -> None:
        if self.strike is not None:
            _draw_strike(p, self.strike, text, r, fg, item.style, centered)
        else:
            align = Qt.AlignmentFlag.AlignCenter if centered else (
                Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
            p.drawText(r, align, text)


class MenuTracker:
    """Answers one MenuSelect call: follow the mouse until it's released.

    Classic menus are press-drag-release.  With `sticky` set, a quick click
    on a title leaves the menu open until the next click (Mac OS 8 style).
    That also covers the guest being slow to call MenuSelect after a click.

    With ask, an application's MBDF hears about each menu before it's
    pulled down, and may change its items: ask(menu ID, where it goes)
    returns a Future that's done when it has.
    """

    def __init__(self, display: Display, bar: MenuBarWidget, future: Future, sticky: bool,
                 ask: Callable[[int, Rect], Future] | None = None):
        self.display = display
        self.bar = bar
        self.future = future
        self.sticky_allowed = sticky
        self.ask = ask
        self.sticky = False
        self.index = -1
        self.menu: Menu | None = None
        self.pulldown: Pulldown | None = None
        self.blinking = False
        self.done = False

    # -- tracker protocol (called by Display's input router) ---------------
    def start(self) -> None:
        pos = self.display.input.global_pos
        idx = self.bar.cell_at(self.bar.mapFromGlobal(pos))
        if idx >= 0:
            self._open(idx)
        if not self.display.input.down:
            if self.sticky_allowed and self.menu is not None:
                self.sticky = True
            else:
                self._finish(0)

    def move(self, gpos: QPoint) -> None:
        if self.blinking or (not self.display.input.down and not self.sticky):
            return
        local = self.bar.mapFromGlobal(gpos)
        if self.bar.rect().contains(local):
            idx = self.bar.cell_at(local)
            if idx >= 0:
                self._open(idx)
            if self.pulldown is not None:
                self.pulldown.set_hilite(0)
        elif self.pulldown is not None:
            self.pulldown.set_hilite(self.pulldown.selectable_at(self.pulldown.mapFromGlobal(gpos)))

    def press(self, gpos: QPoint) -> None:
        if self.blinking:
            return
        local = self.bar.mapFromGlobal(gpos)
        if self.bar.rect().contains(local):
            idx = self.bar.cell_at(local)
            if idx < 0 or idx == self.index:
                self._finish(0)
            else:
                self._open(idx)
        elif self.pulldown is not None and self.pulldown.geometry().contains(gpos):
            self.move(gpos)
        else:
            self._finish(0)

    def release(self, gpos: QPoint) -> None:
        if self.blinking or self.done:
            return
        item = self.pulldown.hilite if self.pulldown is not None else 0
        if item:
            self._choose(item)
            return
        over_bar = self.bar.rect().contains(self.bar.mapFromGlobal(gpos))
        quick = (time.monotonic() - self.display.input.press_time) * 1000 < theme.STICKY_CLICK_MS
        if (over_bar and self.sticky_allowed and self.menu is not None
                and (self.sticky or quick)):
            self.sticky = True
            return
        self._finish(0)

    def cancel(self) -> None:
        self._finish(0)

    # -- internals ----------------------------------------------------------
    def _open(self, idx: int) -> None:
        if idx == self.index:
            return
        cells = self.bar.cells()
        if idx >= len(cells):
            return
        menu, cell = cells[idx]
        self.index, self.menu = idx, menu
        self.bar.set_hilite(menu.id)
        old = self.pulldown
        self.pulldown = None
        if old is not None:
            old.hide()
            old.deleteLater()
        if self.ask is None:
            self._show(menu, cell)
            return
        pd = self._place(Pulldown(menu, self.bar, self.bar.system_strike()), cell)
        g = pd.geometry()
        top_left = self.display.mac_from_global(g.topLeft())
        where = Rect(top_left.v, top_left.h, top_left.v + pd.height() - theme.MENU_SHADOW,
                     top_left.h + pd.width() - theme.MENU_SHADOW)
        pd.deleteLater()
        self.ask(menu.id, where).add_done_callback(
            lambda f: self.display.bridge.post(self._asked, f, idx, menu, cell))

    def _asked(self, f: Future, idx: int, menu: Menu, cell: QRect) -> None:
        if f.exception() is not None:             # the application has stopped
            self._finish(0)
        elif not self.done and self.index == idx:
            self._show(menu, cell)

    def _place(self, pd: Pulldown, cell: QRect) -> Pulldown:
        top_left = self.bar.mapToGlobal(QPoint(cell.x(), self.bar.height() - 1))
        # Menus may hang past a window's edge, but not off the Mac screen.
        screen = self.display.screen_bounds
        left_limit = self.display.global_from_mac(Point(0, screen.left)).x()
        right_limit = self.display.global_from_mac(Point(0, screen.right)).x()
        if top_left.x() + pd.width() > right_limit:
            top_left.setX(max(right_limit - pd.width(), left_limit))
        pd.move(top_left)
        return pd

    def _show(self, menu: Menu, cell: QRect) -> None:
        self.pulldown = self._place(Pulldown(menu, self.bar, self.bar.system_strike()), cell)
        self.pulldown.show()

    def _choose(self, item: int) -> None:
        self.blinking = True
        steps = theme.MENU_BLINKS * 2
        timer = QTimer(self.pulldown)
        state = {"n": 0}

        def tick():
            state["n"] += 1
            if self.pulldown is None or state["n"] > steps:
                timer.stop()
                self._chosen(item)
                return
            self.pulldown.set_hilite(0 if state["n"] % 2 else item)

        timer.timeout.connect(tick)
        timer.start(theme.MENU_BLINK_MS)

    def _chosen(self, item: int) -> None:
        menu = self.menu
        if menu is not None and menu.id == kApplicationMenuID:
            self._finish(0)
            self.display.application_menu_choice(item)
            return
        self._finish((menu.id & 0xFFFF) << 16 | item if menu else 0)

    def _finish(self, result: int) -> None:
        if self.done:
            return
        self.done = True
        if self.pulldown is not None:
            self.pulldown.hide()
            self.pulldown.deleteLater()
            self.pulldown = None
        if result == 0:
            self.bar.set_hilite(0)
        self.display.tracker_finished(self)
        if not self.future.done():
            self.future.set_result(result)
