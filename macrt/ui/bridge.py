"""
macrt.ui.bridge - Running Toolbox calls on the GUI thread.

Guest code (later: the 68k core) runs on its own thread and blocks inside
traps.  Anything that touches widgets is shipped to the GUI thread with
`call`.  A GUI-side function can return a Future instead of a value when
the answer depends on the user (MenuSelect, TrackGoAway, DragWindow...);
the guest thread then keeps waiting while the GUI thread stays free.
While it waits it can still run guest code the GUI side asks for
(`serve`): an MBDF that hears a menu is about to be pulled down.
"""

from __future__ import annotations

import queue
import threading
from concurrent.futures import Future
from typing import Any, Callable

from PyQt6.QtCore import QObject, Qt, pyqtSignal

from ..toolbox.events import ProcessKilled


class GuiBridge(QObject):
    _invoke = pyqtSignal(object)

    def __init__(self):
        super().__init__()
        self._gui_thread = threading.current_thread()
        self.closed = False
        self._invoke.connect(self._run, Qt.ConnectionType.QueuedConnection)

    def _run(self, job: Callable[[], None]) -> None:
        job()

    def on_gui_thread(self) -> bool:
        return threading.current_thread() is self._gui_thread

    def post(self, fn: Callable, *args: Any) -> None:
        """Run fn on the GUI thread without waiting."""
        if not self.closed:
            self._invoke.emit(lambda: fn(*args))

    def call(self, fn: Callable, *args: Any, serve: Callable | None = None) -> Any:
        """Run fn on the GUI thread and wait for its result.

        With serve, fn gets one more argument, ask: ask(*a) has this
        thread run serve(*a) while it waits, and returns a Future for
        what serve returns."""
        if self.on_gui_thread():
            result = fn(*args)
            if isinstance(result, Future):
                raise RuntimeError("a tracking call can't be made from the GUI thread")
            return result
        if self.closed:
            raise ProcessKilled()
        jobs: queue.SimpleQueue | None = None
        if serve is not None:
            jobs = queue.SimpleQueue()

            def ask(*a: Any) -> Future:
                done: Future = Future()
                jobs.put((a, done))
                return done
            args = (*args, ask)
        fut: Future = Future()

        def job():
            try:
                fut.set_result(fn(*args))
            except BaseException as e:  # noqa: BLE001 - re-raised on the guest thread
                fut.set_exception(e)

        self._invoke.emit(job)
        result = self._wait(fut, jobs, serve)
        if isinstance(result, Future):
            result = self._wait(result, jobs, serve)
        return result

    def _wait(self, fut: Future, jobs: queue.SimpleQueue | None = None,
              serve: Callable | None = None) -> Any:
        if jobs is None:
            while True:
                try:
                    return fut.result(timeout=0.1)
                except TimeoutError:
                    if self.closed:
                        raise ProcessKilled() from None
        fut.add_done_callback(lambda _: jobs.put(None))
        while True:
            try:
                job = jobs.get(timeout=0.1)
            except queue.Empty:
                if self.closed:
                    raise ProcessKilled() from None
                continue
            if job is None:
                if fut.done():
                    return fut.result()
                continue
            a, done = job
            try:
                done.set_result(serve(*a))
            except BaseException as e:
                done.set_exception(e)
                raise
