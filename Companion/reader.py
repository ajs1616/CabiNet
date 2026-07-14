"""
reader.py — PN532 RFID reader driver for the Companion daemon.

Stdlib-only I2C driver for the PN532 V3 board wired to the BB2's SAS Zero
over SOFTWARE i2c-gpio (SDA GPIO23 / SCL GPIO24 -> /dev/i2c-11; the Zero's
hardware I2C pins are taken by the MAX232 SAS HAT). The frame logic below is
a VERBATIM port of the scratchpad bring-up script that read the first live
card (UID 6CB16F06, S50 1K, 2026-07-10) — do not "improve" the byte layout,
it is live-proven against the real board:

    preamble 00 00 FF · LEN LCS · TFI D4 (host->PN532) · data · DCS · 00
    RDY = status byte bit0 (read one byte before every frame read)
    ACK frame = 00 00 FF 00 FF 00 (may arrive with junk padding around it)
    InListPassiveTarget response = D5 4B, NbTg at +2, NFCIDLength at +7,
    then the UID bytes.

Self-heal posture: the RX-wedge lesson from the SAS serial side applies to
I2C too — a wedged bus is cured by close+reopen+re-SAMConfig, so after
MAX_IO_ERRORS consecutive OSErrors the driver heals itself and lets the
error propagate (the host marks readerOk=false for that cycle; the next
poll runs on the fresh handle).
"""

import fcntl
import time

I2C_SLAVE = 0x0703                    # linux/i2c-dev.h ioctl: bind slave addr
PREAMBLE = bytes([0x00, 0x00, 0xFF])  # PN532 frame preamble + start code
HOST_TO_PN532 = 0xD4                  # TFI direction byte (host -> chip)
ACK_FRAME = b"\x00\x00\xff\x00\xff\x00"
SAM_CONFIG = [0x14, 0x01, 0x00, 0x00]   # SAMConfiguration: normal mode
LIST_PASSIVE = [0x4A, 0x01, 0x00]       # InListPassiveTarget: 1 tag, 106kbps A
RESP_D5_4B = bytes([0xD5, 0x4B])        # InListPassiveTarget response header

#: _ready() attempts for the InListPassiveTarget RESPONSE phase only:
#: 15 x 20ms = the ~300ms per-poll card window (no card -> RDY never sets
#: -> poll() returns None inside the window). ACK waits keep the proven
#: 50-try default — the chip acks in microseconds when the bus is alive.
RESP_TRIES_POLL = 15


class RfidReader:
    """Reader interface the Companion loop drives: poll() returns an
    uppercase hex UID string when a card is in the field, None otherwise
    (raising OSError = reader trouble -> readerOk=false at the hub)."""

    def poll(self):
        raise NotImplementedError

    def close(self):
        pass


class PN532Reader(RfidReader):
    """The real PN532 over /dev/i2c-N via ioctl(I2C_SLAVE) — no smbus dep."""

    MAX_IO_ERRORS = 5   # consecutive OSErrors before the close/reopen heal
    MAX_ACK_MISSES = 3  # consecutive un-acked commands = mid-run mute wedge
    #: consecutive polls whose reply never contained a well-formed D5 4B
    #: frame before the RESPONSE channel is declared wedged. A healthy idle
    #: chip answers EVERY InListPassiveTarget with D5 4B NbTg=0, so this
    #: counter only climbs in the silent wedge class the 07-14 AVP hit:
    #: the chip ACKS each command (alive on the bus, i2cdetect sees it)
    #: but never raises RDY for the response — zero errors, zero taps,
    #: readerOk lying green while every tap vanishes. 50 polls ≈ 10 s at
    #: the 5 Hz loop; on trip we self-heal AND raise so the hub shows
    #: readerOk=false until a clean poll proves the recovery.
    MAX_DEAD_POLLS = 50

    def __init__(self, bus="/dev/i2c-11", addr=0x24):
        self.bus = bus
        self.addr = addr
        self._f = None
        self._io_errors = 0
        self._ack_misses = 0
        self._dead_polls = 0

    # ---- frame layer: VERBATIM port of the proven scratchpad driver ------

    def _open(self):
        f = open(self.bus, "r+b", buffering=0)
        fcntl.ioctl(f, I2C_SLAVE, self.addr)
        self._f = f

    def _wr(self, data):
        L = len(data) + 1
        lcs = (~L + 1) & 0xFF
        fr = bytearray(PREAMBLE) + bytes([L, lcs, HOST_TO_PN532]) + bytes(data)
        s = (HOST_TO_PN532 + sum(data)) & 0xFF
        fr += bytes([(~s + 1) & 0xFF, 0x00])
        self._f.write(fr)

    def _ready(self, tries=50):
        for _ in range(tries):
            try:
                b = self._f.read(1)
                if b and (b[0] & 1):
                    return True
            except OSError:
                pass
            time.sleep(0.02)
        return False

    def _ack(self):
        time.sleep(0.01)
        if not self._ready():
            return False
        d = bytes(self._f.read(7))
        return ACK_FRAME in d

    def _cmd(self, data, rlen=40, resp_tries=50):
        self._wr(data)
        if not self._ack():
            # A live chip acks in microseconds; a RUN of missed ACKs is the
            # mid-run mute wedge (writes complete but RDY never sets). It
            # must surface as reader trouble so the _io_errors/self-heal
            # path covers it — a bare None here would read as an eternal
            # quiet "no card" and the heal would never fire. No reset on
            # raise: keep raising until the chip actually acks again.
            self._ack_misses += 1
            if self._ack_misses >= self.MAX_ACK_MISSES:
                raise OSError("PN532 stopped acknowledging commands")
            return None
        self._ack_misses = 0
        if not self._ready(resp_tries):
            return None
        return bytes(self._f.read(rlen))

    # ---- lifecycle --------------------------------------------------------

    def init(self, retries=3):
        """Open the bus and SAMConfiguration the chip, with retries — the
        board may still be powering up when the service starts. Returns
        True on success; False leaves poll() to keep retrying lazily."""
        for attempt in range(1, retries + 1):
            try:
                if self._f is None:
                    self._open()
                if self._cmd(SAM_CONFIG) is not None:
                    self._io_errors = 0
                    self._dead_polls = 0
                    return True
            except OSError:
                self.close()
            if attempt < retries:
                time.sleep(0.5)
        return False

    def close(self):
        if self._f is not None:
            try:
                self._f.close()
            except OSError:
                pass
            self._f = None

    def _self_heal(self):
        """Close, reopen, re-SAMConfig — the I2C twin of the SAS RX-wedge
        watchdog. Best-effort: a failure here just leaves _f None and the
        next poll() retries the whole bring-up."""
        self.close()
        try:
            self._open()
            self._cmd(SAM_CONFIG)
        except OSError:
            self.close()

    # ---- the one call the Companion loop makes ----------------------------

    def poll(self):
        """One InListPassiveTarget with a ~300ms card window. Returns the
        UID as uppercase hex, or None (no card / unparseable frame).
        OSErrors count toward the self-heal threshold and propagate so the
        host can report readerOk=false."""
        try:
            if self._f is None:
                self._open()
                if self._cmd(SAM_CONFIG) is None:
                    # Bus opened but the chip is mute — surface it as reader
                    # trouble, not as an eternal quiet "no card".
                    raise OSError("PN532 SAMConfiguration not acknowledged")
            resp = self._cmd(LIST_PASSIVE, rlen=40,
                             resp_tries=RESP_TRIES_POLL)
        except OSError:
            self._io_errors += 1
            if self._io_errors >= self.MAX_IO_ERRORS:
                self._io_errors = 0
                self._self_heal()
            raise
        self._io_errors = 0
        i = resp.find(RESP_D5_4B) if resp else -1
        if i < 0:
            # ACKed but no well-formed reply — the SILENT wedge class (the
            # 07-14 AVP incident). A healthy chip answers every poll with a
            # D5 4B frame even with no card present, so a run of these is
            # never "quiet floor": self-heal and surface it, don't wait.
            self._dead_polls += 1
            if self._dead_polls >= self.MAX_DEAD_POLLS:
                self._dead_polls = 0
                self._self_heal()
                raise OSError(
                    "PN532 response channel wedged (acked but no D5 4B "
                    f"for {self.MAX_DEAD_POLLS} polls) — self-healed, "
                    "re-syncing")
            return None
        self._dead_polls = 0
        if len(resp) > i + 7:
            nb = resp[i + 2]
            if nb >= 1:
                j = i + 7
                uidlen = resp[j]
                uid = resp[j + 1:j + 1 + uidlen]
                if 3 <= uidlen <= 10 and len(uid) == uidlen:
                    return uid.hex().upper()
        return None


class MockRfidReader(RfidReader):
    """Scripted reader for tests and --mock runs: script is a list of
    (at_poll_n, value) pairs — value is a hex-UID string returned at that
    poll number, or an Exception instance to RAISE there (drives the
    readerOk=false path). Every unscripted poll returns None (no card)."""

    def __init__(self, script=()):
        self._script = {int(n): v for n, v in script}
        self._n = 0
        self.closed = False

    def poll(self):
        v = self._script.get(self._n)
        self._n += 1
        if isinstance(v, BaseException):
            raise v
        return v

    def close(self):
        self.closed = True
