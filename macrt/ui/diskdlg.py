"""
macrt.ui.diskdlg - Which disk goes in a drive.

A program on several disks ejects one, unmounts it and asks for another:
on a Mac the user puts in the one it asks for.  Here the disk images
given are the disks there are, and this asks which of them goes in,
looking as the Standard File dialogs do.  The program's thread waits on
a Future for the answer: the index of the disk chosen, or None to put
back the one taken out.
"""

from __future__ import annotations

from concurrent.futures import Future
from typing import TYPE_CHECKING

from PyQt6.QtWidgets import QDialog, QHBoxLayout, QLabel, QListWidget, QPushButton, QVBoxLayout

from . import theme
from .stdfile import STYLE, icon

if TYPE_CHECKING:
    from .display import Display


class DiskDialog(QDialog):
    def __init__(self, display: Display | None, drive: int, names: list[str], current: int,
                 future: Future):
        super().__init__(display.host_window if display is not None else None)
        self.display = display
        self._future = future
        self.setWindowTitle(f"Drive {drive}")
        self.setModal(False)
        self.setStyleSheet(STYLE)
        self.setFont(theme.menu_item_font())
        self.list = QListWidget()
        for name in names:
            self.list.addItem(name)
            self.list.item(self.list.count() - 1).setIcon(icon("volume"))
        self.list.setCurrentRow(current)
        self.list.setMinimumSize(220, 120)
        self.list.itemDoubleClicked.connect(lambda _: self.insert())
        self.cancel_button = QPushButton("Put Back")
        self.cancel_button.setAutoDefault(False)
        self.cancel_button.clicked.connect(self.cancel)
        self.ok = QPushButton("Insert")
        self.ok.setDefault(True)
        self.ok.clicked.connect(self.insert)
        row = QHBoxLayout()
        row.addStretch(1)
        row.addWidget(self.cancel_button)
        row.addWidget(self.ok)
        col = QVBoxLayout(self)
        col.addWidget(QLabel(f"Which disk goes in drive {drive}?"))
        col.addWidget(self.list)
        col.addLayout(row)

    def insert(self) -> None:
        self._answer(self.list.currentRow())

    def cancel(self) -> None:
        """The disk taken out goes back."""
        self._answer(None)

    def reject(self) -> None:  # Escape, or the close box
        self.cancel()

    def closeEvent(self, event):  # noqa: N802
        event.ignore()
        self.cancel()

    def _answer(self, choice: int | None) -> None:
        if not self._future.done():
            self._future.set_result(choice)
        if self.display is not None and self in self.display.disk_dialogs:
            self.display.disk_dialogs.remove(self)
        self.hide()
        self.deleteLater()


def ask(display: Display, drive: int, names: list[str], current: int, future: Future) -> None:
    """On the GUI thread: ask which disk goes in the drive."""
    dlg = DiskDialog(display, drive, names, current, future)
    display.disk_dialogs.append(dlg)
    dlg.show()
    dlg.raise_()
    dlg.activateWindow()
