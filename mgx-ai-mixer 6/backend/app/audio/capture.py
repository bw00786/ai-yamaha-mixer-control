"""Multitrack capture from the Yamaha MGX16 USB MAIN interface.

The MGX16 exposes a 22-in / 22-out USB audio device (32-bit / 96 kHz on the
MAIN port). We open it as a normal multichannel input and keep a rolling
ring buffer per channel for metering + analysis.

If no MGX is connected, we fall back to the default input device (or a
synthetic test-signal generator) so the whole stack can be developed
without hardware.

DSP takeover: when a `processor` callback is attached and the device supports
output, the engine opens a full-duplex stream — every input block is run
through the processor and written to the MGX's USB return channels. The ring
buffer stores the POST-processing audio, so meters and analysis always show
what the desk actually receives.
"""
from __future__ import annotations

import threading
import numpy as np

try:
    import sounddevice as sd
except OSError:          # no PortAudio on this machine — allow import for tests
    sd = None

DEFAULT_SAMPLE_RATE = 48000       # 96k works too; 48k halves CPU for analysis
BUFFER_SECONDS = 8


def find_mgx_device() -> tuple[int | None, int, int]:
    """Return (device_index, in_channels, out_channels) for the MGX USB MAIN."""
    if sd is None:
        return None, 2, 0
    for idx, dev in enumerate(sd.query_devices()):
        name = dev["name"].lower()
        if "mgx" in name and dev["max_input_channels"] >= 2:
            return idx, dev["max_input_channels"], dev["max_output_channels"]
    return None, 2, 0


class CaptureEngine:
    def __init__(self, samplerate: int = DEFAULT_SAMPLE_RATE,
                 channel_names: dict[int, str] | None = None):
        self.samplerate = samplerate
        self.device, self.n_channels, self.n_out = find_mgx_device()
        self.channel_names = channel_names or {}
        self.simulated = self.device is None
        if self.simulated:
            self.n_channels = 8  # simulate an 8-channel session
            self.n_out = 8
        self.processor = None       # callable (channels, frames) -> same shape
        self.duplex = False

        self._buf = np.zeros((self.n_channels, samplerate * BUFFER_SECONDS),
                             dtype=np.float32)
        self._write = 0
        self._lock = threading.Lock()
        self._stream = None
        self._sim_thread = None
        self._running = False

    # ---------------------------------------------------------------- stream
    def start(self):
        self._running = True
        if self.simulated:
            self._sim_thread = threading.Thread(target=self._simulate, daemon=True)
            self._sim_thread.start()
            return
        if self.processor is not None and self.n_out >= 2:
            # full-duplex: input -> DSP -> USB return to the desk
            self.duplex = True
            ch = min(self.n_channels, self.n_out)
            self._stream = sd.Stream(
                device=(self.device, self.device),
                channels=(ch, ch),
                samplerate=self.samplerate,
                dtype="float32",
                blocksize=256,          # ~5.3 ms @48k per direction
                latency="low",
                callback=self._on_duplex,
            )
        else:
            self._stream = sd.InputStream(
                device=self.device,
                channels=self.n_channels,
                samplerate=self.samplerate,
                dtype="float32",
                blocksize=1024,
                callback=self._on_audio,
            )
        self._stream.start()

    def stop(self):
        self._running = False
        if self._stream:
            self._stream.stop()
            self._stream.close()

    def _on_duplex(self, indata, outdata, frames, time_info, status):
        block = indata.T.astype(np.float32)      # (channels, frames)
        processed = self.processor(block) if self.processor else block
        outdata[:] = processed.T[:, : outdata.shape[1]]
        self._store(processed, frames)

    def _on_audio(self, indata, frames, time_info, status):
        block = indata.T.astype(np.float32)
        if self.processor is not None:           # simulator path: process too
            block = self.processor(block)
        self._store(block, frames)

    def _store(self, data, frames):
        with self._lock:
            # In full-duplex mode the stream channel count is min(in, out),
            # which can be fewer than the ring buffer's row count (sized off
            # the device's input channels). Pad/truncate so shapes always
            # line up instead of crashing the audio callback.
            rows = self._buf.shape[0]
            if data.shape[0] < rows:
                padded = np.zeros((rows, data.shape[1]), dtype=data.dtype)
                padded[: data.shape[0]] = data
                data = padded
            elif data.shape[0] > rows:
                data = data[:rows]
            n = self._buf.shape[1]
            end = self._write + frames
            if end <= n:
                self._buf[:, self._write:end] = data
            else:
                k = n - self._write
                self._buf[:, self._write:] = data[:, :k]
                self._buf[:, :frames - k] = data[:, k:]
            self._write = end % n

    # ------------------------------------------------------------ simulation
    def _simulate(self):
        """Generate a plausible fake band so the UI works with no hardware."""
        import time
        rng = np.random.default_rng(7)
        t0 = 0.0
        block = 1024
        freqs = [110, 220, 330, 500, 800, 1200, 2400, 60]  # per channel
        while self._running:
            t = t0 + np.arange(block) / self.samplerate
            sig = np.stack([
                0.25 * np.sin(2 * np.pi * f * t) *
                (0.6 + 0.4 * np.sin(2 * np.pi * 0.2 * (i + 1) * t)) +
                0.01 * rng.standard_normal(block)
                for i, f in enumerate(freqs[: self.n_channels])
            ]).astype(np.float32)
            self._on_audio(sig.T, block, None, None)
            t0 += block / self.samplerate
            time.sleep(block / self.samplerate)

    # --------------------------------------------------------------- reading
    def latest(self, seconds: float) -> np.ndarray:
        """Return the most recent `seconds` of audio, shape (channels, samples)."""
        n = int(seconds * self.samplerate)
        with self._lock:
            w = self._write
            buf = self._buf
            if n >= buf.shape[1]:
                n = buf.shape[1]
            start = (w - n) % buf.shape[1]
            if start < w:
                return buf[:, start:w].copy()
            return np.concatenate([buf[:, start:], buf[:, :w]], axis=1)

    def name_of(self, ch: int) -> str:
        return self.channel_names.get(ch, f"CH {ch}")
