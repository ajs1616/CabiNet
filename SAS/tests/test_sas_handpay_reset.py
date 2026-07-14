"""
Tests for the two-step handpay reset-to-credits sequence
(core/sas_handpay_reset.py) against a scripted mock machine.

The load-bearing regression guard is THE ORDER: the 0x94 reset must NEVER
leave the host unless the 0xA8 method-select was acked (AJ bench fact,
2026-07-01 — "send the byte prior to reset"). The mock machine models that
dependency: it refuses the reset unless the credit-meter method was set
first. Frame constants are the module's believed TODO(bench) values; these
tests prove self-consistency and sequencing, not interop (bench-only).
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from core.sas_protocol import SASProtocol, sas_crc
from core.sas_poller import SASPoller
from core.sas_handpay_reset import (
    CMD_SET_HANDPAY_RESET_METHOD, CMD_REMOTE_HANDPAY_RESET,
    RESET_METHOD_CREDIT_METER, RESET_METHOD_STANDARD,
    METHOD_ACK_ENABLED, METHOD_ACK_UNABLE, METHOD_ACK_NO_HANDPAY,
    RESET_CODE_OK, RESET_CODE_UNABLE, HANDPAY_RESET_EXCEPTION,
    HandpayResetOutcome, build_reset_method_poll, build_handpay_reset_poll,
    reset_handpay_to_credits,
)
from transport.serial.sas_serial import MockSASSerialPort

SAS_POLL_FLOOR = 0.200

# Exact expected wire bytes for address 1 (CRC-16/Kermit, LSB first):
STEP1_FRAME = bytes.fromhex("01a8016a2a")   # [01][A8][01][crc-lo][crc-hi]
STEP2_FRAME = bytes.fromhex("019475cb")     # [01][94][crc-lo][crc-hi]

NO_SLEEP = lambda _t: None


def _resp(address, command, code):
    body = bytes([address, command, code])
    return body + sas_crc(body).to_bytes(2, "little")


class HandpayMachine:
    """A pretend SAS machine sitting in a handpay lockup, modelling the
    believed 0xA8/0x94 semantics PLUS the real dependency AJ observed on the
    bench: the reset only pays to credits if the method byte was selected
    first. Also models implied-ACK exception FIFO semantics (pending
    exception re-sent until a poll to another address / long poll ACKs it),
    so the confirm loop is tested against real drain rules."""

    def __init__(self, address=1, in_handpay=True, allow_method=True,
                 allow_reset=True, answer_step1=True, announce_reset=True):
        self.address = address
        self.in_handpay = in_handpay
        self.allow_method = allow_method
        self.allow_reset = allow_reset
        self.answer_step1 = answer_step1
        self.announce_reset = announce_reset   # queue 0x52 after a reset
        self.method = RESET_METHOD_STANDARD    # power-up default
        self.fifo = []                         # queued exception codes
        self.pending = None                    # sent but not yet ACKed

    def queue(self, *codes):
        self.fifo.extend(codes)

    def __call__(self, frame, wakeup):
        if not wakeup[0]:
            return b""
        if len(frame) == 1:                    # general poll
            polled = frame[0] & 0x7F
            if polled != self.address:         # poll to another address:
                self.pending = None            # implied ACK of our pending
                return b""
            if self.pending is not None:       # implied NACK -> re-send
                return bytes([self.pending])
            self.pending = self.fifo.pop(0) if self.fifo else None
            if self.pending is None:
                return b"\x00"
            return bytes([self.pending])

        # long poll: also an implied ACK of any pending exception
        self.pending = None
        if frame[0] != self.address:
            return b""
        body, crc = frame[:-2], frame[-2:]
        if sas_crc(body).to_bytes(2, "little") != crc:
            return b""                         # bad CRC: silently ignored
        cmd = frame[1]
        if cmd == CMD_SET_HANDPAY_RESET_METHOD:
            if not self.answer_step1:
                return b""
            if not self.in_handpay:
                return _resp(self.address, cmd, METHOD_ACK_NO_HANDPAY)
            if not self.allow_method:
                return _resp(self.address, cmd, METHOD_ACK_UNABLE)
            self.method = frame[2]
            return _resp(self.address, cmd, METHOD_ACK_ENABLED)
        if cmd == CMD_REMOTE_HANDPAY_RESET:
            # THE dependency under test: reset pays to credits ONLY if the
            # credit-meter method was selected first.
            if (not self.in_handpay or not self.allow_reset
                    or self.method != RESET_METHOD_CREDIT_METER):
                return _resp(self.address, cmd, RESET_CODE_UNABLE)
            self.in_handpay = False
            self.method = RESET_METHOD_STANDARD
            if self.announce_reset:
                self.fifo.append(HANDPAY_RESET_EXCEPTION)
            return _resp(self.address, cmd, RESET_CODE_OK)
        return b""


def _long_polls(port, command):
    """The long-poll frames sent for a given command byte."""
    return [f for f, _ in port.sent_frames if len(f) > 1 and f[1] == command]


class TestFrames:
    def test_exact_step_frames_with_crc(self):
        assert build_reset_method_poll(1) == STEP1_FRAME
        assert build_handpay_reset_poll(1) == STEP2_FRAME
        # and the CRCs really are Kermit-LSB-first over [addr][cmd][data]
        assert STEP1_FRAME[3:] == sas_crc(STEP1_FRAME[:3]).to_bytes(2, "little")
        assert STEP2_FRAME[2:] == sas_crc(STEP2_FRAME[:2]).to_bytes(2, "little")

    def test_method_byte_validated(self):
        try:
            build_reset_method_poll(1, method=0x77)
        except ValueError:
            pass
        else:
            raise AssertionError("bad method byte must raise")


class TestTwoStepSequence:
    def test_happy_path_frames_order_and_result(self):
        machine = HandpayMachine()
        port = MockSASSerialPort(machine)
        result = reset_handpay_to_credits(port, 1, sleep=NO_SLEEP)

        assert result.outcome is HandpayResetOutcome.RESET_OK
        assert result.ok
        assert result.step1_acked and result.step2_sent
        assert result.step2_code == RESET_CODE_OK
        assert result.confirmed_by_exception
        assert machine.in_handpay is False

        # exact bytes on the wire, and step 1 strictly before step 2
        frames = [f for f, _ in port.sent_frames]
        assert STEP1_FRAME in frames and STEP2_FRAME in frames
        assert frames.index(STEP1_FRAME) < frames.index(STEP2_FRAME)
        # wake-up bit set on byte 0 only, for both long polls
        for f, wakeup in port.sent_frames:
            assert wakeup == [True] + [False] * (len(f) - 1)

    def test_reset_never_sent_without_method_ack_silence(self):
        """THE regression guard: a machine silent on step 1 must never see
        the 0x94 reset."""
        machine = HandpayMachine(answer_step1=False)
        port = MockSASSerialPort(machine)
        result = reset_handpay_to_credits(port, 1, sleep=NO_SLEEP)

        assert result.outcome is HandpayResetOutcome.TIMEOUT
        assert result.step1_acked is False
        assert result.step2_sent is False
        assert _long_polls(port, CMD_REMOTE_HANDPAY_RESET) == []
        assert machine.in_handpay is True      # lockup untouched

    def test_reset_never_sent_after_method_refusal(self):
        machine = HandpayMachine(allow_method=False)
        port = MockSASSerialPort(machine)
        result = reset_handpay_to_credits(port, 1, sleep=NO_SLEEP)

        assert result.outcome is HandpayResetOutcome.METHOD_REFUSED
        assert result.step1_acked is False and result.step2_sent is False
        assert _long_polls(port, CMD_REMOTE_HANDPAY_RESET) == []

    def test_no_handpay_condition(self):
        machine = HandpayMachine(in_handpay=False)
        port = MockSASSerialPort(machine)
        result = reset_handpay_to_credits(port, 1, sleep=NO_SLEEP)

        assert result.outcome is HandpayResetOutcome.NO_HANDPAY
        assert result.step2_sent is False
        assert _long_polls(port, CMD_REMOTE_HANDPAY_RESET) == []

    def test_step2_refusal_surfaces_typed(self):
        machine = HandpayMachine(allow_reset=False)
        port = MockSASSerialPort(machine)
        result = reset_handpay_to_credits(port, 1, sleep=NO_SLEEP)

        assert result.outcome is HandpayResetOutcome.METHOD_REFUSED
        assert result.step1_acked and result.step2_sent
        assert result.step2_code == RESET_CODE_UNABLE
        assert machine.in_handpay is True

    def test_acked_but_unconfirmed_is_timeout(self):
        """Step 2 acked but no 0x52 within the confirm window: the typed
        result must NOT claim reset_ok."""
        machine = HandpayMachine(announce_reset=False)
        port = MockSASSerialPort(machine)
        result = reset_handpay_to_credits(port, 1, sleep=NO_SLEEP,
                                          confirm_polls=3)
        assert result.outcome is HandpayResetOutcome.TIMEOUT
        assert result.step2_code == RESET_CODE_OK
        assert result.confirmed_by_exception is False

    def test_corrupt_step1_frame_is_not_consent(self):
        """A CRC-corrupt answer to step 1 must be treated as silence — the
        reset must not fire on a garbled ack."""
        inner = HandpayMachine()

        def corruptor(frame, wakeup):
            resp = inner(frame, wakeup)
            if len(frame) > 1 and frame[1] == CMD_SET_HANDPAY_RESET_METHOD:
                return resp[:-2] + b"\x00\x00"   # break the CRC
            return resp

        port = MockSASSerialPort(corruptor)
        result = reset_handpay_to_credits(port, 1, sleep=NO_SLEEP)
        assert result.outcome is HandpayResetOutcome.TIMEOUT
        assert result.step2_sent is False
        assert _long_polls(port, CMD_REMOTE_HANDPAY_RESET) == []

    def test_pacing_at_poll_floor(self):
        """Every inter-poll gap in the sequence is paced: one sleep between
        step 1 and step 2, one before each confirm general poll — all at
        SAS_POLL_FLOOR by default."""
        machine = HandpayMachine()
        port = MockSASSerialPort(machine)
        sleeps = []
        result = reset_handpay_to_credits(port, 1, sleep=sleeps.append)

        assert result.ok
        # steps gap + 1 confirm poll (0x52 arrives on the first one)
        assert sleeps == [SAS_POLL_FLOOR, SAS_POLL_FLOOR]

    def test_confirmation_acks_the_exception(self):
        """After confirmation the 0x52 must have been implied-ACKed (addr-0
        poll) so the main poll loop never sees a stale re-send."""
        machine = HandpayMachine()
        port = MockSASSerialPort(machine)
        assert reset_handpay_to_credits(port, 1, sleep=NO_SLEEP).ok
        assert machine.pending is None and machine.fifo == []
        # a follow-up general poll sees a clean FIFO
        assert port.transact(bytes([0x81])) == b"\x00"


class TestPollerIntegration:
    def test_poller_method_and_bridge_hook(self):
        """SASPoller.reset_handpay_to_credits: same typed result, plus the
        on_handpay_reset bridge hook fires, and unrelated exceptions drained
        during the confirm window still dispatch through on_event."""
        machine = HandpayMachine()
        machine.queue(0x11)                # door opens mid-confirm
        port = MockSASSerialPort(machine)
        events, hook = [], []
        poller = SASPoller(port, address=1,
                           on_event=lambda a, c, n: events.append(c),
                           on_handpay_reset=lambda a, r: hook.append((a, r)))
        result = poller.reset_handpay_to_credits(sleep=NO_SLEEP)

        assert result.outcome is HandpayResetOutcome.RESET_OK
        assert hook == [(1, result)]
        assert 0x11 in events              # not swallowed by the confirm loop
        assert HANDPAY_RESET_EXCEPTION not in events  # confirm, not event

    def test_hook_exception_is_isolated(self):
        machine = HandpayMachine()
        port = MockSASSerialPort(machine)

        def boom(*a):
            raise RuntimeError("bad bridge callback")

        poller = SASPoller(port, address=1, on_handpay_reset=boom)
        result = poller.reset_handpay_to_credits(sleep=NO_SLEEP)  # must not raise
        assert result.ok
        assert poller.state.callback_errors == 1
