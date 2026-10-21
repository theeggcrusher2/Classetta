"""Window frame geometry and the wHit codes, without any widgets."""

import pytest

from macrt.toolbox import constants as k
from macrt.toolbox.windows import WindowKind
from macrt.ui import chrome, theme
from macrt.ui.chrome import Frame

DOC = Frame(WindowKind.DOCUMENT, title="Doc", go_away=True, zoom=True, grow=True)
W, H = chrome.structure_size(DOC, 300, 200)


def test_document_insets_and_size():
    l, t, r, b = chrome.insets(DOC)
    assert (l, r, b) == (theme.BORDER,) * 3
    assert t == theme.BORDER + theme.TITLE_HEIGHT
    assert (W, H) == (300 + 2 * theme.BORDER, 200 + t + theme.BORDER)
    assert chrome.content_rect(DOC, W, H).size().width() == 300


def test_menu_bar_strip_sits_between_title_and_content():
    f = Frame(WindowKind.DOCUMENT, title="Doc", menu_bar=True)
    w, h = chrome.structure_size(f, 300, 200)
    bar = chrome.menu_bar_rect(f, w)
    assert bar.top() == theme.BORDER + theme.TITLE_HEIGHT
    assert bar.height() == theme.MENU_BAR_HEIGHT
    assert chrome.content_rect(f, w, h).top() == bar.bottom() + 1
    assert chrome.hit(f, w, h, 50, bar.center().y()) == k.wInMenuBar


@pytest.mark.parametrize("where, code", [
    ("close", k.wInGoAway),
    ("zoom", k.wInZoomOut),
    ("title", k.wInDrag),
    ("content", k.wInContent),
    ("grow", k.wInGrow),
    ("outside", k.wNoHit),
])
def test_hit_codes(where, code):
    point = {
        "close": chrome.close_box_rect(DOC, W).center(),
        "zoom": chrome.zoom_box_rect(DOC, W).center(),
        "title": chrome.title_rect(DOC, W).center(),
        "content": chrome.content_rect(DOC, W, H).center(),
        "grow": chrome.grow_box_rect(DOC, W, H).center(),
    }.get(where)
    x, y = (point.x(), point.y()) if point else (W + 5, 5)
    assert chrome.hit(DOC, W, H, x, y) == code


def test_zoom_box_reports_zoom_in_when_zoomed_out():
    f = Frame(WindowKind.DOCUMENT, zoom=True, zoomed_out=True)
    p = chrome.zoom_box_rect(f, W).center()
    assert chrome.hit(f, W, H, p.x(), p.y()) == k.wInZoomIn


def test_inactive_window_has_no_boxes():
    f = Frame(WindowKind.DOCUMENT, go_away=True, zoom=True, grow=True, hilited=False)
    assert chrome.close_box_rect(f, W) is None
    assert chrome.zoom_box_rect(f, W) is None
    g = chrome.grow_box_rect(f, W, H).center()
    assert chrome.hit(f, W, H, g.x(), g.y()) == k.wInContent
    assert chrome.hit(f, W, H, theme.BOX_INSET + 3, 10) == k.wInDrag


@pytest.mark.parametrize("kind", [WindowKind.DBOX, WindowKind.PLAIN, WindowKind.ALT_DBOX])
def test_dialog_frames_are_all_content(kind):
    f = Frame(kind)
    w, h = chrome.structure_size(f, 200, 100)
    assert chrome.hit(f, w, h, 0, 0) == k.wInContent
    assert chrome.hit(f, w, h, w // 2, h // 2) == k.wInContent


def test_dbox_frame_layers_add_up():
    l, t, r, b = chrome.insets(Frame(WindowKind.DBOX))
    assert l == t == r == b == sum(n for n, _ in theme.DBOX_LAYERS)


def test_mask_includes_shadow_only_where_offset():
    m = chrome.mask(DOC, W, H)
    s = chrome.shadow(DOC)
    assert m.contains(chrome.QRect(W, s, s, 1).topLeft())
    assert not m.contains(chrome.QRect(W, 0, 1, 1).topLeft())     # shadow is offset down


def test_rounded_window_corners(qapp):
    f = Frame(WindowKind.ROUNDED, corner=16, go_away=True)
    w, h = chrome.structure_size(f, 200, 120)
    m = chrome.mask(f, w, h)
    assert not m.contains(chrome.QRect(0, 0, 1, 1).topLeft())
    assert m.contains(chrome.QRect(w // 2, h // 2, 1, 1).topLeft())
    # The content is rounded at the bottom too, so the frame's arc shows.
    c = chrome.content_rect(f, w, h)
    cm = chrome.content_mask(f, w, h)
    assert not cm.contains(chrome.QRect(0, c.height() - 1, 1, 1).topLeft())
    assert cm.contains(chrome.QRect(c.width() // 2, c.height() - 1, 1, 1).topLeft())


def test_paint_produces_one_bit_pixels(qapp):
    from PyQt6.QtGui import QImage, QPainter
    img = QImage(W + 1, H + 1, QImage.Format.Format_RGB32)
    img.fill(0xFF808080)
    p = QPainter(img)
    chrome.paint(p, Frame(WindowKind.DOCUMENT, go_away=True, zoom=True), W, H)
    p.end()
    title = chrome.title_rect(DOC, W)
    colors = {img.pixel(x, y) & 0xFFFFFF
              for x in range(W) for y in range(title.top(), title.bottom())
              if not (W // 2 - 40 < x < W // 2 + 40)}      # skip the (antialiased) title text
    assert colors <= {0x000000, 0xFFFFFF}
