"""
macrt.ui.printdlg - The Printing Manager's dialogs: Page Setup (the paper
and which way up) and Print (which pages, how many copies, and the PDF
they go to), looking as the Standard File dialogs do.

As with those, the application's thread opens one and waits on a Future
for the answer:

    ("ok", Setup)                           Page Setup
    ("ok", first, last, copies, path)       Print; first and last 0 for all
    ("cancel",)
"""

from __future__ import annotations

from concurrent.futures import Future
from pathlib import Path
from typing import TYPE_CHECKING

from PyQt6.QtWidgets import (QButtonGroup, QComboBox, QDialog, QFileDialog, QGridLayout,
                             QHBoxLayout, QLabel, QLineEdit, QPushButton, QRadioButton,
                             QSpinBox, QVBoxLayout)

from ..toolbox.printing import PAPERS, Setup, iPrPgMax
from . import theme
from .stdfile import STYLE

if TYPE_CHECKING:
    from .display import Display

STYLE += """
QRadioButton { color: black; }
QSpinBox { background: white; color: black; border: 1px solid black; padding: 1px 2px; }
"""


class _Dialog(QDialog):
    def __init__(self, display: Display | None, title: str):
        super().__init__(display.host_window if display is not None else None)
        self.display = display
        self._future: Future = Future()
        self.setWindowTitle(title)
        self.setModal(False)
        self.setStyleSheet(STYLE)
        self.setFont(theme.menu_item_font())
        self.cancel_button = QPushButton("Cancel")
        self.cancel_button.clicked.connect(self.cancel)
        self.ok = QPushButton("OK")
        self.ok.setDefault(True)
        self.ok.clicked.connect(self.accept_choice)

    def _buttons(self) -> QHBoxLayout:
        row = QHBoxLayout()
        row.addStretch(1)
        row.addWidget(self.cancel_button)
        row.addWidget(self.ok)
        return row

    def _answer(self, *action) -> None:
        if not self._future.done():
            self._future.set_result(action)

    def future(self) -> tuple[Future]:
        """(A tuple, so the bridge doesn't wait for the Future itself.)"""
        return (self._future,)

    def accept_choice(self) -> None:
        raise NotImplementedError

    def cancel(self) -> None:
        self._answer("cancel")

    def reject(self) -> None:  # Escape, or the close box
        self.cancel()

    def closeEvent(self, event):  # noqa: N802
        event.ignore()
        self.cancel()

    def finish(self) -> None:
        """The application has its answer."""
        if self.display is not None and self in self.display.print_dialogs:
            self.display.print_dialogs.remove(self)
        self.hide()
        self.deleteLater()


class PageSetupDialog(_Dialog):
    def __init__(self, display: Display | None, setup: Setup, title: str = ""):
        super().__init__(display, title or "Page Setup")
        self.paper = QComboBox()
        for p in PAPERS:
            self.paper.addItem(p.name)
        self.paper.setCurrentIndex(next((i for i, p in enumerate(PAPERS)
                                         if p == setup.paper), 0))
        self.tall = QRadioButton("Tall")
        self.wide = QRadioButton("Wide")
        group = QButtonGroup(self)
        group.addButton(self.tall)
        group.addButton(self.wide)
        (self.wide if setup.wide else self.tall).setChecked(True)

        grid = QGridLayout()
        grid.addWidget(QLabel("Paper:"), 0, 0)
        grid.addWidget(self.paper, 0, 1, 1, 2)
        grid.addWidget(QLabel("Orientation:"), 1, 0)
        grid.addWidget(self.tall, 1, 1)
        grid.addWidget(self.wide, 1, 2)
        layout = QVBoxLayout(self)
        layout.addLayout(grid)
        layout.addLayout(self._buttons())

    def setup(self) -> Setup:
        return Setup(PAPERS[self.paper.currentIndex()], self.wide.isChecked())

    def accept_choice(self) -> None:
        self._answer("ok", self.setup())


class PrintDialog(_Dialog):
    def __init__(self, display: Display | None, path: Path, title: str = ""):
        super().__init__(display, title or "Print")
        self.ok.setText("Print")
        self.all = QRadioButton("All")
        self.some = QRadioButton("From:")
        group = QButtonGroup(self)
        group.addButton(self.all)
        group.addButton(self.some)
        self.all.setChecked(True)
        self.first = QSpinBox()
        self.last = QSpinBox()
        for box in (self.first, self.last):
            box.setRange(1, iPrPgMax)
            box.valueChanged.connect(lambda _: self.some.setChecked(True))
        self.copies = QSpinBox()
        self.copies.setRange(1, 99)
        self.path = QLineEdit(str(path))
        self.path.setMinimumWidth(280)
        self.choose = QPushButton("Choose…")
        self.choose.clicked.connect(self._choose)

        pages = QHBoxLayout()
        pages.addWidget(self.all)
        pages.addWidget(self.some)
        pages.addWidget(self.first)
        pages.addWidget(QLabel("To:"))
        pages.addWidget(self.last)
        pages.addStretch(1)
        where = QHBoxLayout()
        where.addWidget(self.path, 1)
        where.addWidget(self.choose)
        grid = QGridLayout()
        grid.addWidget(QLabel("Pages:"), 0, 0)
        grid.addLayout(pages, 0, 1)
        grid.addWidget(QLabel("Copies:"), 1, 0)
        grid.addWidget(self.copies, 1, 1)
        grid.addWidget(QLabel("Save as PDF:"), 2, 0)
        grid.addLayout(where, 2, 1)
        layout = QVBoxLayout(self)
        layout.addLayout(grid)
        layout.addLayout(self._buttons())

    def _choose(self) -> None:
        name, _ = QFileDialog.getSaveFileName(self, "Save the PDF as", self.path.text(),
                                              "PDF (*.pdf)")
        if name:
            self.path.setText(name)

    def accept_choice(self) -> None:
        text = self.path.text().strip()
        if not text:
            self.path.setFocus()
            return
        first, last = ((self.first.value(), self.last.value()) if self.some.isChecked()
                       else (0, 0))
        if first > last:
            first, last = last, first
        self._answer("ok", first, last, self.copies.value(), Path(text).expanduser())


def open_setup(display: Display, setup: Setup, title: str) -> tuple[PageSetupDialog, Future]:
    """On the GUI thread."""
    return _show(display, PageSetupDialog(display, setup, title))


def open_print(display: Display, path: Path, title: str) -> tuple[PrintDialog, Future]:
    return _show(display, PrintDialog(display, path, title))


def _show(display: Display, dlg: _Dialog):
    display.print_dialogs.append(dlg)
    dlg.show()
    dlg.raise_()
    dlg.activateWindow()
    (fut,) = dlg.future()
    return dlg, (fut,)
