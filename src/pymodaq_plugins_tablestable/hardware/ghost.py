"""Reading of the GHOST serial output of a TFP-1 tandem Fabry-Perot interferometer (The Table Stable, formerly JRS).

The control unit (MCU) of the TFP-1 sends its photon counts on a one-way RS-232 link (57600 baud, 8 data bits, no
parity, 1 stop bit), as described in "GHOST communication protocol structure" (F. Scarponi, 2012):

- during the retrace (SCN signal low), an idle character 0xFF is sent about every 21.5 ms;
- during a scan (SCN signal high), each channel is sent as two bytes::

      first byte : [SH][0 ][d11 d10 d9 d8 d7 d6]
      second byte: [0 ][A ][d5  d4  d3 d2 d1 d0]

  SH is set while the shutter blanks a window (the reference beam is then measured instead of the sample), A is set
  in tandem mode and clear in alignment mode, and the two unused bits are always 0, so that 0xFF never occurs
  during a scan.

There is no frame marker: a scan starts with the first byte that is not 0xFF after idle characters, and ends with the
next 0xFF. The bit positions and the unused bits were checked on a TFP-1 in November 2026 (512 channels, tandem and
alignment modes, with and without shutter windows).

Nothing can be sent to the control unit: this module only listens.
"""
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Union

import numpy as np

IDLE = 0xFF
BAUDRATE = 57600
MAX_COUNTS = 4095  # the PMT counter has 12 bits


@dataclass
class Scan:
    """One complete scan of the interferometer"""
    counts: np.ndarray    # counts per channel (int), 0 to 4095
    shutter: np.ndarray   # bool per channel, True while the shutter blanks a window (reference beam)
    tandem: bool          # True in tandem mode, False in alignment mode
    valid: bool           # False if a byte was lost during the scan (the scan should be discarded)
    n_idle_before: int    # number of idle characters received during the retrace before this scan

    @property
    def n_channels(self) -> int:
        return len(self.counts)


class GhostDecoder:
    """Turns the byte stream into complete scans, whatever the size of the chunks it is fed with.

    States: ``'wait_idle'`` at start (the port may have been opened in the middle of a scan, so everything is ignored
    until the first idle character), ``'idle'`` during the retrace, ``'scan'`` while the bytes of a scan arrive.
    """

    def __init__(self):
        self.reset()

    def reset(self):
        """Forget the current scan and wait for the next retrace"""
        self.state = 'wait_idle'
        self._buffer = bytearray()
        self._n_idle = 0

    def feed(self, data: bytes) -> list:
        """Decode ``data`` and return the list of the scans completed by it (possibly empty)"""
        scans = []
        for byte in data:
            if self.state == 'wait_idle':
                if byte == IDLE:
                    self.state = 'idle'
                    self._n_idle = 1
            elif self.state == 'idle':
                if byte == IDLE:
                    self._n_idle += 1
                else:
                    self.state = 'scan'
                    self._buffer = bytearray([byte])
            else:  # 'scan'
                if byte == IDLE:
                    scans.append(self._decode_scan())
                    self.state = 'idle'
                    self._n_idle = 1
                else:
                    self._buffer.append(byte)
        return scans

    def _decode_scan(self) -> Scan:
        raw = np.frombuffer(bytes(self._buffer), dtype=np.uint8)
        valid = len(raw) % 2 == 0  # an odd number of bytes means that a byte was lost
        raw = raw[: len(raw) // 2 * 2].astype(np.uint16)
        first, second = raw[0::2], raw[1::2]

        counts = ((first & 0x3F) << 6) | (second & 0x3F)
        shutter = (first & 0x80) > 0
        tandem_bits = (second & 0x40) > 0
        unused_bits_clear = not np.any(first & 0x40) and not np.any(second & 0x80)
        same_mode = bool(tandem_bits.all() or not tandem_bits.any())

        return Scan(counts=counts.astype(np.int64), shutter=shutter, tandem=bool(tandem_bits.all()),
                    valid=bool(valid and unused_bits_clear and same_mode), n_idle_before=self._n_idle)


class ReplayPort:
    """Serial-like object replaying a recording of the GHOST output, in a loop, with the timing of the instrument.

    The bytes of a scan come at the rate of the serial link (57600 baud) and the idle characters of the retrace every
    ``idle_interval`` seconds (about 10 ms on the TFP-1 tested, so that a cycle lasts about 0.7 s for 512 channels).
    It has the methods of ``serial.Serial`` used by :class:`TFP1Ghost`, so that the plugin can be developed and tested
    without the instrument.
    """

    def __init__(self, path: Union[str, Path], idle_interval: float = 0.01):
        self._data = np.frombuffer(Path(path).read_bytes(), dtype=np.uint8)
        byte_time = 10 / BAUDRATE  # 10 bits per byte (start, 8 data, stop)
        self._times = np.cumsum(np.where(self._data == IDLE, idle_interval, byte_time))  # arrival of each byte
        self._start = time.monotonic()
        self._delivered = 0
        self.is_open = True

    def _available_total(self) -> int:
        """Number of bytes sent since the start of the replay"""
        loops, t = divmod(time.monotonic() - self._start, self._times[-1])
        return int(loops) * len(self._data) + int(np.searchsorted(self._times, t, side='right'))

    @property
    def in_waiting(self) -> int:
        return self._available_total() - self._delivered

    def read(self, size: int = 1) -> bytes:
        size = min(size, self.in_waiting)
        indexes = np.arange(self._delivered, self._delivered + size) % len(self._data)
        self._delivered += size
        return self._data[indexes].tobytes()

    def reset_input_buffer(self):
        self._delivered = self._available_total()

    def close(self):
        self.is_open = False


class TFP1Ghost:
    """Wrapper of the GHOST output of a TFP-1 control unit.

    Parameters
    ----------
    port: str or serial-like object
        Name of the serial port (``COM2``), or an already open object with the ``in_waiting``, ``read``,
        ``reset_input_buffer`` and ``close`` methods of ``serial.Serial`` (for instance a :class:`ReplayPort`)
    """

    def __init__(self, port: Union[str, object]):
        if isinstance(port, str):
            import serial  # imported here: the module can be read without pyserial installed
            port = serial.Serial(port, baudrate=BAUDRATE, bytesize=serial.EIGHTBITS, parity=serial.PARITY_NONE,
                                 stopbits=serial.STOPBITS_ONE, timeout=0)
        self._port = port
        self._decoder = GhostDecoder()
        self.n_invalid_scans = 0

    def read_scans(self) -> list:
        """Return the valid scans completed since the last call, without waiting (possibly an empty list).

        Invalid scans (a byte was lost) are dropped and counted in ``n_invalid_scans``.
        """
        n = self._port.in_waiting
        if n == 0:
            return []
        scans = self._decoder.feed(self._port.read(n))
        valid = [scan for scan in scans if scan.valid]
        self.n_invalid_scans += len(scans) - len(valid)
        return valid

    def restart(self):
        """Make sure that the next scan returned by ``read_scans`` starts after this call.

        What was received so far is decoded and dropped: the completed scans, and the scan in progress if any. During
        the retrace nothing is lost: the next scan has not started yet, so it will be returned. Used before each
        acquisition, so that a point of a map only contains scans started once the stage has stopped.
        """
        n = self._port.in_waiting
        if n:
            self._decoder.feed(self._port.read(n))
        if self._decoder.state == 'scan':
            self._decoder.reset()

    def wait_scan(self, timeout: float = 10.) -> Scan:
        """Return the next valid scan, or raise ``TimeoutError`` if none arrives within ``timeout`` seconds.

        Blocking: meant for test scripts, the plugin uses ``read_scans``.
        """
        t_end = time.monotonic() + timeout
        while time.monotonic() < t_end:
            scans = self.read_scans()
            if scans:
                return scans[0]
            time.sleep(0.02)
        raise TimeoutError(f'No complete scan received within {timeout} s: is the TFP-1 scanning, '
                           f'and is the cable on this port?')

    def close(self):
        """Close the serial port (safe to call twice)"""
        if getattr(self._port, 'is_open', False):
            self._port.close()
