"""
The Mac's speaker through the host's sound output.

The Speaker (macrt.m68k.sound) hands HostSpeaker a frame of 370 samples
at each tick, from the CPU's thread: unsigned bytes, at RATE samples a
second, silence at $80.  HostSpeaker makes them the sound output's own
format, at its own rate if it hasn't RATE, holding each sample for as
long as it lasts, and keeps them until a timer on the GUI thread writes
them out.  A Mac's speaker doesn't pass on a level that holds, only the
changes: a buffer of 0s sounds as silent as one of $80s.  So the samples
go through a filter that lets the level drift back to silence, as the
speaker's would.

Frames come in bursts, as the application's code gets the CPU, so up to
about a quarter of a second of them is kept: more, and the oldest go.
"""

from __future__ import annotations

import logging
import threading
from array import array

from PyQt6.QtCore import QObject, QTimer
from PyQt6.QtMultimedia import QAudio, QAudioFormat, QAudioSink, QMediaDevices

from ..m68k.sound import RATE, SILENCE

log = logging.getLogger("macrt.ui")

SampleFormat = QAudioFormat.SampleFormat
DRIFT = 0.994               # how fast a level that holds goes back to silence
LATENCY = 0.1               # seconds of sound the output's buffer holds
START = 0.05                # how much there must be to start, or start again
KEEP = 0.25                 # how much is kept for the output at most
PUMP_MS = 10


class HostSpeaker(QObject):
    """Frames, from any thread, to the host's default sound output.  Made
    on the GUI thread."""

    @classmethod
    def open(cls, parent: QObject | None = None) -> HostSpeaker | None:
        """The default sound output's, or None if the host has none."""
        device = QMediaDevices.defaultAudioOutput()
        if device.isNull():
            log.warning("no sound output: the Mac's sound won't be heard")
            return None
        return cls(device, parent)

    def __init__(self, device, parent: QObject | None = None):
        super().__init__(parent)
        fmt = QAudioFormat()
        fmt.setSampleRate(RATE)
        fmt.setChannelCount(1)
        fmt.setSampleFormat(SampleFormat.Int16)
        if not device.isFormatSupported(fmt):
            fmt = device.preferredFormat()
        self.convert = Converter(fmt.sampleRate(), fmt.channelCount(), fmt.sampleFormat())
        self._bytes_per_second = fmt.bytesForDuration(1_000_000)
        self.sink = QAudioSink(device, fmt, self)
        self.sink.setBufferSize(fmt.bytesForDuration(int(LATENCY * 1_000_000)))
        self._io = self.sink.start()
        self._lock = threading.Lock()
        self._pending = bytearray()
        self._timer = QTimer(self, interval=PUMP_MS)
        self._timer.timeout.connect(self._pump)
        self._timer.start()

    def frame(self, samples: bytes) -> None:
        data = self.convert(samples)
        with self._lock:
            self._pending += data
            excess = len(self._pending) - int(KEEP * self._bytes_per_second)
            if excess > 0:
                del self._pending[:self.convert.align(excess)]

    def close(self) -> None:
        self._timer.stop()
        self.sink.stop()

    # -- on the GUI thread -------------------------------------------------------
    def _pump(self) -> None:
        if self._io is None:
            return
        with self._lock:
            if (self.sink.state() != QAudio.State.ActiveState
                    and len(self._pending) < START * self._bytes_per_second):
                return
            n = self.convert.align(min(self.sink.bytesFree(), len(self._pending)))
            if not n:
                return
            data = bytes(self._pending[:n])
            del self._pending[:n]
        self._io.write(data)


_SIZES = {SampleFormat.UInt8: 1, SampleFormat.Int16: 2, SampleFormat.Int32: 4,
          SampleFormat.Float: 4}


class Converter:
    """Frames to the output's format: its rate, channels and samples."""

    def __init__(self, rate: int, channels: int, sample_format):
        self.rate = rate
        self.channels = max(channels, 1)
        self.sample_format = sample_format
        self._held = 0                      # toward the next output sample, in RATEs
        self._last_in = 0.0
        self._last_out = 0.0

    def align(self, n: int) -> int:
        """n bytes, down to whole samples for each channel."""
        size = self.channels * _SIZES.get(self.sample_format, 2)
        return n - n % size

    def __call__(self, samples: bytes) -> bytes:
        """The samples, -1 to 1 about silence, with the level drifting back
        to silence, at the output's rate, in its format."""
        levels = []
        x0, y0 = self._last_in, self._last_out
        for s in samples:
            x = (s - SILENCE) / 128
            y0 = x - x0 + DRIFT * y0
            x0 = x
            levels.append(y0)
        self._last_in, self._last_out = x0, y0
        if self.rate != RATE:
            levels = self._hold(levels)
        if self.channels > 1:
            levels = [y for y in levels for _ in range(self.channels)]
        fmt = self.sample_format
        if fmt == SampleFormat.Float:
            return array("f", (max(-1.0, min(1.0, y)) for y in levels)).tobytes()
        if fmt == SampleFormat.Int32:
            return array("i", (max(-0x7FFF0000, min(0x7FFF0000, int(y * 0x7FFF0000)))
                               for y in levels)).tobytes()
        if fmt == SampleFormat.UInt8:
            return bytes(max(0, min(255, 128 + int(y * 127))) for y in levels)
        return array("h", (max(-32767, min(32767, int(y * 32767))) for y in levels)).tobytes()

    def _hold(self, levels: list[float]) -> list[float]:
        """Each sample, at RATE, for as many of the output's samples as it lasts."""
        out = []
        held, rate = self._held, self.rate
        for y in levels:
            held += rate
            n, held = divmod(held, RATE)
            out.extend([y] * n)
        self._held = held
        return out
