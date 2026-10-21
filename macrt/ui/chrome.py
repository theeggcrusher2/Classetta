"""
macrt.ui.chrome - Window frames: geometry, hit-testing and painting.

This is the runtime's stand-in for the system WDEFs (WDEF 0 for document
and dialog windows, WDEF 1 for rounded desk-accessory windows).  It works
on plain values, not widgets: a `Frame` describes the window, and every
function takes the structure size (w, h, shadow excluded) with the
structure's top-left corner at (0, 0).  Drawing is done with fillRect on
whole pixels so the output stays 1-bit crisp.
"""

from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache

from PyQt6.QtCore import QRect, QRectF, Qt
from PyQt6.QtGui import (
    QBitmap, QBrush, QFontMetrics, QImage, QPainter, QPainterPath, QPen, QRegion,
)

from ..toolbox.constants import (
    wInContent, wInDrag, wInGoAway, wInGrow, wInMenuBar, wInZoomIn, wInZoomOut, wNoHit,
)
from ..toolbox.windows import Definition, WindowKind, WindowRecord
from . import theme
from .theme import BLACK, WHITE

TITLED = (WindowKind.DOCUMENT, WindowKind.MOVABLE_DBOX)
BOXED = (WindowKind.DBOX, WindowKind.PLAIN, WindowKind.ALT_DBOX)


@dataclass(frozen=True)
class Frame:
    kind: WindowKind
    title: str = ""
    hilited: bool = True
    go_away: bool = False
    zoom: bool = False
    grow: bool = False
    zoomed_out: bool = False
    corner: int = 16
    menu_bar: bool = False      # menu bar strip under the title bar
    pressed: int = wNoHit       # box being tracked with the mouse inside it

    @classmethod
    def for_window(cls, rec: WindowRecord, *, menu_bar: bool = False,
                   pressed: int = wNoHit, definition: Definition | None = None) -> Frame:
        d = definition or rec.definition
        return cls(
            kind=d.kind,
            title=rec.title.replace("\0", ""),     # a desk accessory's name, as its title
            hilited=rec.hilited,
            go_away=rec.go_away and d.kind in (WindowKind.DOCUMENT, WindowKind.ROUNDED),
            zoom=d.zoom,
            grow=d.grow,
            zoomed_out=rec.zoomed_out,
            corner=d.corner,
            menu_bar=menu_bar and supports_menu_bar(d.kind),
            pressed=pressed,
        )


def supports_menu_bar(kind: WindowKind) -> bool:
    return kind is WindowKind.DOCUMENT


# ---------------------------------------------------------------------------
# Geometry
# ---------------------------------------------------------------------------
def insets(f: Frame) -> tuple[int, int, int, int]:
    """(left, top, right, bottom) from the content rect out to the structure."""
    b = theme.BORDER
    if f.kind in TITLED:
        top = b + theme.TITLE_HEIGHT + (theme.MENU_BAR_HEIGHT if f.menu_bar else 0)
        return b, top, b, b
    if f.kind is WindowKind.ROUNDED:
        return b, b + theme.ROUNDED_TITLE_HEIGHT, b, b
    if f.kind is WindowKind.DBOX:
        n = sum(t for t, _ in theme.DBOX_LAYERS)
        return n, n, n, n
    return b, b, b, b


def shadow(f: Frame) -> int:
    if f.kind in TITLED:
        return theme.SHADOW_OFFSET
    if f.kind is WindowKind.ALT_DBOX:
        return theme.ALT_DBOX_SHADOW
    return 0


def structure_size(f: Frame, content_w: int, content_h: int) -> tuple[int, int]:
    l, t, r, b = insets(f)
    return content_w + l + r, content_h + t + b


def content_rect(f: Frame, w: int, h: int) -> QRect:
    l, t, r, b = insets(f)
    return QRect(l, t, w - l - r, h - t - b)


def title_rect(f: Frame, w: int) -> QRect | None:
    """The title bar's interior (inside the frame, above its bottom line)."""
    b = theme.BORDER
    if f.kind in TITLED:
        return QRect(b, b, w - 2 * b, theme.TITLE_HEIGHT - 1)
    if f.kind is WindowKind.ROUNDED:
        return QRect(b, b, w - 2 * b, theme.ROUNDED_TITLE_HEIGHT - 1)
    return None


def menu_bar_rect(f: Frame, w: int) -> QRect | None:
    if not f.menu_bar:
        return None
    b = theme.BORDER
    return QRect(b, b + theme.TITLE_HEIGHT, w - 2 * b, theme.MENU_BAR_HEIGHT)


def _box_y(tr: QRect) -> int:
    return tr.y() + (tr.height() - theme.BOX_SIZE) // 2


def close_box_rect(f: Frame, w: int) -> QRect | None:
    tr = title_rect(f, w)
    if tr is None or not f.go_away or not f.hilited:
        return None
    return QRect(theme.BOX_INSET, _box_y(tr), theme.BOX_SIZE, theme.BOX_SIZE)


def zoom_box_rect(f: Frame, w: int) -> QRect | None:
    tr = title_rect(f, w)
    if tr is None or not f.zoom or not f.hilited or f.kind is not WindowKind.DOCUMENT:
        return None
    x = w - theme.BOX_INSET - theme.BOX_SIZE
    return QRect(x, _box_y(tr), theme.BOX_SIZE, theme.BOX_SIZE)


def grow_box_rect(f: Frame, w: int, h: int) -> QRect | None:
    if not f.grow:
        return None
    c = content_rect(f, w, h)
    g = theme.GROW_BOX
    return QRect(c.x() + c.width() - g, c.y() + c.height() - g, g, g)


def mask(f: Frame, w: int, h: int) -> QRegion:
    """Structure plus shadow, as a region for QWidget.setMask()."""
    if f.kind is WindowKind.ROUNDED:
        return _round_rect_region(w, h, f.corner)
    region = QRegion(0, 0, w, h)
    s = shadow(f)
    if s:
        region = region.united(QRegion(w, s, s, h)).united(QRegion(s, h, w, s))
    return region


def content_mask(f: Frame, w: int, h: int) -> QRegion | None:
    """The content's own shape, in content coordinates, if it isn't its rect.
    Rounded windows round their bottom corners inside the 1-pixel frame."""
    if f.kind is not WindowKind.ROUNDED:
        return None
    b = theme.BORDER
    c = content_rect(f, w, h)
    inner = _round_rect_region(w - 2 * b, h - 2 * b, max(f.corner - 2 * b, 0)).translated(b, b)
    return inner.intersected(QRegion(c)).translated(-c.x(), -c.y())


@lru_cache(maxsize=64)
def _round_rect_region(w: int, h: int, diameter: int) -> QRegion:
    """A w x h rounded rect as whole pixels.  Painting and masking both use
    this, so the frame's corner pixels always fall inside the mask."""
    if diameter <= 0 or w <= 0 or h <= 0:
        return QRegion(0, 0, max(w, 0), max(h, 0))
    img = QImage(w, h, QImage.Format.Format_ARGB32)
    img.fill(Qt.GlobalColor.transparent)
    p = QPainter(img)
    path = QPainterPath()
    path.addRoundedRect(QRectF(0, 0, w, h), diameter / 2, diameter / 2)
    p.fillPath(path, BLACK)
    p.end()
    return QRegion(QBitmap.fromImage(img.createAlphaMask()))


# ---------------------------------------------------------------------------
# Hit testing (the WDEF's wHit message)
# ---------------------------------------------------------------------------
def hit(f: Frame, w: int, h: int, x: int, y: int) -> int:
    if not (0 <= x < w and 0 <= y < h):
        return wNoHit
    if content_rect(f, w, h).contains(x, y):
        g = grow_box_rect(f, w, h)
        if g is not None and f.hilited and g.contains(x, y):
            return wInGrow
        return wInContent
    if f.kind in BOXED:
        return wInContent
    mb = menu_bar_rect(f, w)
    if mb is not None and mb.contains(x, y):
        return wInMenuBar
    cb = close_box_rect(f, w)
    if cb is not None and cb.contains(x, y):
        return wInGoAway
    zb = zoom_box_rect(f, w)
    if zb is not None and zb.contains(x, y):
        return wInZoomIn if f.zoomed_out else wInZoomOut
    return wInDrag


# ---------------------------------------------------------------------------
# Painting (the WDEF's wDraw message)
# ---------------------------------------------------------------------------
def frame_rect(p: QPainter, x: int, y: int, w: int, h: int, c=BLACK, t: int = 1) -> None:
    """Outline exactly w x h pixels, t pixels thick."""
    p.fillRect(x, y, w, t, c)
    p.fillRect(x, y + h - t, w, t, c)
    p.fillRect(x, y, t, h, c)
    p.fillRect(x + w - t, y, t, h, c)


def paint(p: QPainter, f: Frame, w: int, h: int) -> None:
    p.setRenderHint(QPainter.RenderHint.Antialiasing, False)
    if f.kind in TITLED:
        _paint_document(p, f, w, h)
    elif f.kind is WindowKind.ROUNDED:
        _paint_rounded(p, f, w, h)
    elif f.kind is WindowKind.DBOX:
        x = 0
        for t, c in theme.DBOX_LAYERS:
            frame_rect(p, x, x, w - 2 * x, h - 2 * x, c, t)
            x += t
    else:
        frame_rect(p, 0, 0, w, h)
    s = shadow(f)
    if s:
        p.fillRect(w, s, s, h, BLACK)
        p.fillRect(s, h, w, s, BLACK)


def _paint_document(p: QPainter, f: Frame, w: int, h: int) -> None:
    b = theme.BORDER
    tr = title_rect(f, w)
    p.fillRect(b, b, w - 2 * b, tr.height(), WHITE)
    frame_rect(p, 0, 0, w, h, BLACK, b)
    p.fillRect(b, tr.bottom() + 1, w - 2 * b, 1, BLACK)

    cb, zb = close_box_rect(f, w), zoom_box_rect(f, w)
    title_box = _title_text_rect(f, w, tr, cb, zb)
    if f.hilited:
        x0 = tr.x() + theme.STRIPE_INSET
        x1 = tr.right() - theme.STRIPE_INSET
        y = tr.y() + 3
        while y <= tr.bottom() - 3:
            p.fillRect(x0, y, x1 - x0 + 1, 1, BLACK)
            y += theme.PINSTRIPE_SPACING
        clear = theme.BOX_CLEARANCE
        for box in (cb, zb):
            if box is not None:
                p.fillRect(box.adjusted(-clear, -clear, clear, clear), WHITE)
        if title_box is not None:
            pad = theme.TITLE_CLEARANCE
            p.fillRect(title_box.x() - pad, tr.y(), title_box.width() + 2 * pad,
                       tr.height(), WHITE)
    if cb is not None:
        _paint_box(p, cb, pressed=f.pressed == wInGoAway)
    if zb is not None:
        _paint_box(p, zb, pressed=f.pressed in (wInZoomIn, wInZoomOut), zoom=True)
    if title_box is not None:
        _paint_title(p, f, title_box, BLACK)


def _title_text_rect(f: Frame, w: int, tr: QRect, cb, zb) -> QRect | None:
    if not f.title:
        return None
    font = theme.title_font()
    m = QFontMetrics(font)
    # Centred, but clear of the boxes the window has even when they're
    # hidden, so it doesn't jump around when the window is activated.  A
    # title too wide to be centred between them starts right of the close
    # box, and is cut short only if it doesn't fit there: Uninvited's
    # narrow "inventory" window has room for its name.
    pad = theme.TITLE_CLEARANCE
    box = theme.BOX_INSET + theme.BOX_SIZE + theme.BOX_CLEARANCE + pad
    left = box if f.go_away else pad
    right = w - (box if f.zoom and f.kind is WindowKind.DOCUMENT else pad)
    x = max((w - m.horizontalAdvance(f.title)) // 2, left)
    text = m.elidedText(f.title, Qt.TextElideMode.ElideRight, max(right - x, 0))
    if not text:
        return None
    return QRect(x, tr.y(), m.horizontalAdvance(text), tr.height())


def _paint_title(p: QPainter, f: Frame, r: QRect, color) -> None:
    font = theme.title_font()
    p.setFont(font)
    p.setPen(QPen(color))
    text = QFontMetrics(font).elidedText(f.title, Qt.TextElideMode.ElideRight, r.width() + 1)
    p.drawText(r.adjusted(0, 0, 1, 0), Qt.AlignmentFlag.AlignCenter, text)


def _paint_box(p: QPainter, r: QRect, *, pressed: bool, zoom: bool = False,
               fg=BLACK, bg=WHITE) -> None:
    p.fillRect(r, bg)
    frame_rect(p, r.x(), r.y(), r.width(), r.height(), fg)
    if pressed:
        _paint_burst(p, r, fg)
    elif zoom:
        dx, dy = theme.ZOOM_INNER_POS
        s = theme.ZOOM_INNER_SIZE
        frame_rect(p, r.x() + dx, r.y() + dy, s, s, fg)


def _paint_burst(p: QPainter, r: QRect, c) -> None:
    """The starburst a close or zoom box shows while it's pressed."""
    cx, cy = r.x() + r.width() // 2, r.y() + r.height() // 2
    reach = r.width() // 2 - 1
    for d in range(2, reach + 1):
        for dx, dy in ((1, 0), (-1, 0), (0, 1), (0, -1), (1, 1), (-1, -1), (1, -1), (-1, 1)):
            p.fillRect(cx + dx * d, cy + dy * d, 1, 1, c)


def _paint_rounded(p: QPainter, f: Frame, w: int, h: int) -> None:
    tr = title_rect(f, w)
    p.save()
    p.setClipRegion(_round_rect_region(w, h, f.corner))
    p.fillRect(0, 0, w, h, BLACK)
    c = content_rect(f, w, h)
    p.setClipRegion(content_mask(f, w, h).translated(c.x(), c.y()))
    p.fillRect(c, WHITE)
    p.restore()
    cb = close_box_rect(f, w)
    if cb is not None:
        _paint_box(p, cb, pressed=f.pressed == wInGoAway, fg=WHITE, bg=BLACK)
    if f.title:
        font = theme.title_font()
        m = QFontMetrics(font)
        # Centred, clear of the close box: Calculator's narrow window has
        # room for its name.
        side = theme.BOX_INSET + theme.BOX_SIZE + 2
        text = m.elidedText(f.title, Qt.TextElideMode.ElideRight, max(w - 2 * side, 0))
        p.setFont(font)
        p.setPen(QPen(WHITE))
        p.drawText(tr, Qt.AlignmentFlag.AlignCenter, text)


def paint_dither(p: QPainter, r: QRect, color=WHITE) -> None:
    """Knock out every other pixel: the classic look of disabled/dimmed things."""
    p.fillRect(r, QBrush(color, theme.DITHER))
