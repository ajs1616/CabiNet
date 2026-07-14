"""
SAS 9-bit serial transport — the wakeup-bit layer.

SAS frames ride RS-232 at 19,200 baud with 11 bits per character:
1 start + 8 data + 1 WAKE-UP bit + 1 stop. The wake-up (9th) bit is SET on
the first byte of every host message (the address byte / general poll) and
CLEAR on every other byte; machines always respond with it clear. A machine
ignores everything on the loop until it sees its own address with the
wake-up bit set — which is why a host that never sets it gets dead silence.

Linux has no native 9-bit UART API; the standard emulation is mark/space
parity (termios CMSPAR), which pyserial exposes as PARITY_MARK/PARITY_SPACE:

    address byte  -> PARITY_MARK   (parity bit forced 1 == wake-up set)
    rest of frame -> PARITY_SPACE  (parity bit forced 0 == wake-up clear)

TIMING — the two hazards the adversarial review caught (both real):

1. INTER-BYTE GAP (SAS spec §2.3.2: max 5 ms between bytes WITHIN a message,
   both directions; >5 ms => the message is invalid and silently dropped).
   Switching parity mid-frame (write addr byte, drain, reconfigure termios,
   write rest) inserts a gap. LIVE-PROVEN 2026-07-08 on the Zero 2W PL011:
   tcdrain()/flush() sleeps in whole scheduler ticks — ~20 ms per call —
   so draining after the address byte put ~19 ms of dead air inside every
   multi-byte frame and the WMS BB2 discarded ALL long polls while
   answering every (single-byte, gapless) general poll. The fix is below in
   send_frame: never tcdrain mid-frame — busy-wait exactly 1.5 char times
   (~0.86 ms) for the address byte to leave the shifter, then switch parity
   and write the body in one contiguous burst. With that change the same
   machine answered 0x1A/0x1F/0x54/0x73 on the first try.

2. READ TIMING. pyserial's inter_byte_timeout maps to termios VTIME in
   DECISECONDS: int(0.01*10)==0, so a 10 ms setting is silently disabled and
   read(n) blocks for the whole overall timeout. And the spec's 20 ms is the
   machine's deadline to START its response, NOT the host's read-completion
   budget. So we: wait up to first_byte_timeout (default 50 ms, lenient — a
   forgiving host is safe point-to-point) for byte 1, then read byte-by-byte
   stopping on a >gap_timeout silence (the spec's own 5 ms inter-byte limit
   means a real gap = end of frame).

Frame facts: [address][command][data...][crc-lo][crc-hi]; NO sync byte;
two-byte (type R read) polls carry no CRC; general poll = single 0x80|addr
answered by ONE exception-code byte; machine has 20 ms to START responding.
"""

import time
from typing import Optional

try:
    import serial  # pyserial
except ImportError:  # allow import on boxes without pyserial (mock-only use)
    serial = None

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from core.sas_protocol import SAS_BAUD, SAS_RESPONSE_TIMEOUT_MS


class SASSerialPort:
    """A SAS-correct serial port: every frame goes out with the wake-up bit
    set on its first byte only, and reads terminate on the spec's inter-byte
    gap rather than a fixed total timeout."""

    def __init__(self, port: str, baudrate: int = SAS_BAUD,
                 first_byte_timeout: float = 0.050,
                 gap_timeout: float = 0.006):
        if serial is None:
            raise RuntimeError("pyserial is required: pip install pyserial")
        self.port_path = port
        self.baudrate = baudrate
        self.first_byte_timeout = first_byte_timeout
        self.gap_timeout = gap_timeout
        self._tx_busy_until = 0.0     # when the last TX burst clears the wire
        self.ser = None
        self._open()

    def _open(self) -> None:
        """(Re)open the device with SAS framing defaults."""
        self.ser = serial.Serial(
            port=self.port_path,
            baudrate=self.baudrate,
            bytesize=serial.EIGHTBITS,
            parity=serial.PARITY_SPACE,   # wake-up clear by default
            stopbits=serial.STOPBITS_ONE,
            timeout=self.first_byte_timeout,
        )
        # Critical for USB adapters: drop the latency timer from 16 ms to 1 ms
        # so the mid-frame parity switch stays under the 5 ms inter-byte limit
        # and the read window isn't eaten. Harmless / no-op on the Pi PL011.
        self.low_latency = False
        try:
            self.ser.set_low_latency_mode(True)
            self.low_latency = True
        except (ValueError, NotImplementedError, AttributeError, OSError):
            pass  # PL011 and some drivers don't support/need it

    def reopen(self) -> None:
        """Close and reopen the device — the machine-reboot RX-wedge cure
        (live-diagnosed 2026-07-10): a machine power-cycle can deafen the
        PL011's RX side while TX keeps flowing, so the link looks dead from
        the host while the machine answers unheard (fingerprint:
        /proc/tty/driver/ttyAMA tx climbing, rx frozen). A fresh open()
        restores RX — this is the manual `systemctl restart casinonet-sas`
        fix, in-process. Raises on failure (device vanished); callers
        catch and retry on their own cadence."""
        try:
            if self.ser is not None:
                self.ser.close()
        except Exception:  # noqa: BLE001 — a wedged fd may refuse to close
            pass
        self._tx_busy_until = 0.0
        self._open()

    # One 11-bit SAS character (start + 8 data + wake-up + stop) on the wire.
    _CHAR_SECONDS = 11.0 / SAS_BAUD

    def send_frame(self, frame: bytes) -> None:
        """Send one host->machine frame with correct wake-up framing: byte 0
        with the wake-up bit set (mark parity), the rest clear (space).

        NEVER tcdrain/flush between the address byte and the body: on Pi
        kernels tcdrain sleeps ~20 ms (whole scheduler ticks), which blows
        the spec's 5 ms inter-byte limit (§2.3.2) and makes machines discard
        every multi-byte poll (live-proven on the BB2, 2026-07-08). Instead
        busy-wait exactly long enough for the address byte to clear the
        shifter, then switch parity and burst the body contiguously."""
        if not frame:
            return
        # Previous frame's body may still be draining (we no longer tcdrain
        # after it) — switching parity mid-drain would corrupt its tail.
        while time.perf_counter() < self._tx_busy_until:
            pass
        # First byte: wake-up bit SET (mark parity)
        self.ser.parity = serial.PARITY_MARK
        t0 = time.perf_counter()
        self.ser.write(frame[:1])
        deadline = t0 + self._CHAR_SECONDS * 1.5   # ~0.86 ms, gap stays <2 ms
        while time.perf_counter() < deadline:
            pass
        # Remaining bytes: wake-up bit CLEAR (space parity), one contiguous
        # burst — the kernel FIFO keeps them gapless.
        self.ser.parity = serial.PARITY_SPACE
        if len(frame) > 1:
            self.ser.write(frame[1:])
            self._tx_busy_until = (time.perf_counter()
                                   + self._CHAR_SECONDS * (len(frame) - 1)
                                   + 0.001)
        else:
            self._tx_busy_until = time.perf_counter() + 0.001
        # leave the port at SPACE for the read (machines reply wake-up clear)

    def read_response(self, max_bytes: int = 256) -> bytes:
        """Read a machine response: wait up to first_byte_timeout for the
        first byte (the machine has 20 ms to START), then read byte-by-byte,
        ending when no further byte arrives within gap_timeout (the spec's
        >5 ms inter-byte gap means the frame is complete). Length-agnostic, so
        it works for 1-byte exception codes, 2-byte busy, and full frames."""
        self.ser.timeout = self.first_byte_timeout
        first = self.ser.read(1)
        if not first:
            return b""
        out = bytearray(first)
        self.ser.timeout = self.gap_timeout
        while len(out) < max_bytes:
            b = self.ser.read(1)
            if not b:
                break          # inter-byte gap exceeded -> end of frame
            out += b
        self.ser.timeout = self.first_byte_timeout
        return bytes(out)

    def transact(self, frame: bytes, max_bytes: int = 256) -> bytes:
        """Send a frame and collect the response (one poll cycle)."""
        self.ser.reset_input_buffer()
        self.send_frame(frame)
        return self.read_response(max_bytes)

    def listen(self, duration: float = 1.0) -> bytes:
        """Passively read whatever the machine emits without polling — used
        to catch the 'chirp' (a lone address byte every ~200 ms after 5 s of
        host silence), which proves the RX path and wake-up framing are good
        independent of our TX."""
        self.ser.timeout = duration
        return self.ser.read(64)

    def close(self) -> None:
        self.ser.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


class MockSASSerialPort:
    """In-memory stand-in for SASSerialPort, for dev-box tests without
    hardware. A `machine` callback receives (frame_bytes, wakeup_flags) and
    returns the machine's response bytes. wakeup_flags is a list of bools —
    one per byte — recording the emulated 9th bit, so tests can assert the
    wake-up framing is correct.

    NOTE: this models the HAPPY path only. It cannot reproduce the real 9-bit
    failure modes (a machine ignoring a frame whose wake-up bit didn't make it
    onto the wire, the >5 ms inter-byte drop, the latency-timer truncation, or
    the chirp). Those are bench-only — green mock tests prove self-consistency,
    not interop. See SAS/smib/PI5_FIRST_POLL.md."""

    def __init__(self, machine=None):
        self.machine = machine or (lambda frame, wakeup: b"")
        self.sent_frames = []          # [(bytes, [wakeup_flags])]
        self._pending = b""

    def send_frame(self, frame: bytes) -> None:
        if not frame:
            return
        wakeup = [True] + [False] * (len(frame) - 1)
        self.sent_frames.append((frame, wakeup))
        self._pending = self.machine(frame, wakeup)

    def read_response(self, max_bytes: int = 256) -> bytes:
        resp, self._pending = self._pending[:max_bytes], b""
        return resp

    def transact(self, frame: bytes, max_bytes: int = 256) -> bytes:
        self.send_frame(frame)
        return self.read_response(max_bytes)

    def listen(self, duration: float = 1.0) -> bytes:
        return b""

    def close(self) -> None:
        pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
