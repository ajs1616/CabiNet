"""
Tests for the poller servicing exception 0x51 by reading LP 0x1B (Send Handpay
Information) — the fix for the BB2's endless 0x51 stream (SAS 6.01 §7.8.1).

0x51 is a PRIORITY/interactive exception: the machine re-issues it every 15 s
until the host reads its paired long poll (0x1B) to collect the queued handpay
record. The read is the implied ACK that advances the queue. Our poller used to
only log the 0x51 and never read 1B, so a machine re-asked forever. These tests
use a scripted machine that models the handpay QUEUE + implied-ACK semantics.
No hardware, no real time.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from core.sas_protocol import SASProtocol, sas_crc
from core.sas_poller import (
    SASPoller, HandpayInfo, HANDPAY_INFO_PENDING, CMD_SEND_HANDPAY_INFO,
)
from transport.serial.sas_serial import MockSASSerialPort


def _record_frame(address, record):
    """Build a valid LP 0x1B response frame: [addr][1B][record...][crc]."""
    body = bytes([address, CMD_SEND_HANDPAY_INFO]) + record
    return body + sas_crc(body).to_bytes(2, "little")


class HandpayMachine:
    """A pretend SAS machine that holds a handpay QUEUE and reports it exactly
    like §7.8.1: it re-issues priority exception 0x51 while an unreported record
    remains, answers LP 0x1B with the oldest unreported record (all-zeros once
    the queue is reported), and honors the implied-ACK rule (a repeated general
    poll re-sends the pending exception; a long poll or a poll to another
    address ACKs it).

    `record_len` matches the BB2's observed 20-byte record. `stuck=True` models
    the §7.8.1 'final handpay reported but not reset' case: 0x51 keeps coming
    even after every record reads back all-zeros (the case only the machine's
    operator config can silence)."""

    def __init__(self, address=1, records=None, stuck=False, record_len=20):
        self.address = address
        self.proto = SASProtocol()
        self.records = list(records or [])   # unreported handpay records
        self.reported = 0                    # how many have been read via 1B
        self.stuck = stuck
        self.record_len = record_len
        self.pending = None                  # exception sent, not yet ACKed
        self.b1_reads = 0                    # count of 1B long polls received
        self.general_polls = 0

    def _has_unreported(self):
        return self.reported < len(self.records)

    def _next_exception(self):
        if self._has_unreported() or self.stuck:
            return HANDPAY_INFO_PENDING
        return 0x00

    def __call__(self, frame, wakeup):
        if not wakeup[0]:
            return b""
        is_general = len(frame) == 1
        polled = (frame[0] & 0x7F) if is_general else frame[0]

        if is_general and polled == self.address:
            self.general_polls += 1
            if self.pending is not None:
                return bytes([self.pending])          # implied NACK -> re-send
            code = self._next_exception()
            if code == 0x00:
                return b"\x00"
            self.pending = code
            return bytes([code])

        # a long poll to us, or a poll to another address (incl. addr 0):
        # ACKs any pending exception (implied ACK).
        self.pending = None
        if not is_general and frame[0] == self.address \
                and frame[1] == CMD_SEND_HANDPAY_INFO:
            self.b1_reads += 1
            if self._has_unreported():
                record = self.records[self.reported]
                self.reported += 1
            else:
                record = bytes(self.record_len)       # all zeros: nothing left
            return _record_frame(self.address, record)
        return b""


def _real_record(fill=0xA5, length=20):
    # a non-zero record: first byte set so `any(data)` is True
    r = bytearray(length)
    r[0] = fill
    r[4] = 0x02
    return bytes(r)


class TestHandpayServicing:
    def test_0x51_serviced_by_1b_read(self):
        rec = _real_record()
        machine = HandpayMachine(records=[rec])
        port = MockSASSerialPort(machine)
        collected = []
        poller = SASPoller(port, address=1,
                           on_handpay_info=lambda a, i: collected.append((a, i)))

        poller.poll_once()

        # the machine got an actual 0x1B read (not just a logged 0x51)
        assert machine.b1_reads >= 1
        # exactly one real record captured, with the right bytes
        assert len(collected) == 1
        addr, info = collected[0]
        assert addr == 1
        assert isinstance(info, HandpayInfo)
        assert info.data == rec
        assert info.empty is False
        # the 1B poll frame actually went on the wire
        sent = [f for f, _ in port.sent_frames]
        assert bytes([1, CMD_SEND_HANDPAY_INFO]) in sent

    def test_queue_drains_and_0x51_stops(self):
        machine = HandpayMachine(records=[_real_record(0x11),
                                          _real_record(0x22),
                                          _real_record(0x33)])
        port = MockSASSerialPort(machine)
        collected = []
        poller = SASPoller(port, address=1,
                           on_handpay_info=lambda a, i: collected.append(i))

        # a handful of cycles drains the whole queue (drain loop reads one
        # record per 0x51 it sees, re-polling for the next)
        for _ in range(6):
            poller.poll_once()

        assert len(collected) == 3
        assert [i.data[0] for i in collected] == [0x11, 0x22, 0x33]
        # once every record is reported, the machine stops issuing 0x51
        assert machine._next_exception() == 0x00

    def test_empty_record_not_reported_but_read(self):
        # the diagnostic 'stuck' case: 0x51 keeps coming, 1B reads back all
        # zeros. We must still READ 1B (attempt to drain), NOT fire the
        # callback for an empty record, and NOT loop unbounded.
        machine = HandpayMachine(records=[], stuck=True)
        port = MockSASSerialPort(machine)
        collected = []
        poller = SASPoller(port, address=1,
                           on_handpay_info=lambda a, i: collected.append(i))

        code = poller.poll_once()

        assert code == HANDPAY_INFO_PENDING
        assert machine.b1_reads >= 1          # we attempted to service it
        assert collected == []                # nothing real to report

    def test_stuck_case_is_bounded(self):
        # even when the machine never drains, one poll_once must terminate (the
        # drain loop is capped) and — thanks to the per-cycle empty guard —
        # cost exactly ONE unproductive 1B read, not one per drain iteration.
        machine = HandpayMachine(records=[], stuck=True)
        port = MockSASSerialPort(machine)
        poller = SASPoller(port, address=1)

        poller.poll_once()                    # must return, not hang
        assert machine.b1_reads == 1          # guarded to a single empty read

        poller.poll_once()                    # next cycle re-attempts once more
        assert machine.b1_reads == 2

    def test_service_handpay_false_disables_read(self):
        machine = HandpayMachine(records=[_real_record()])
        port = MockSASSerialPort(machine)
        typed = []
        poller = SASPoller(port, address=1, service_handpay=False,
                           on_typed_event=lambda a, i: typed.append(i.code))

        poller.poll_once()

        # the 0x51 is still dispatched (visible), just never serviced
        assert HANDPAY_INFO_PENDING in typed
        assert machine.b1_reads == 0
        sent = [f for f, _ in port.sent_frames]
        assert bytes([1, CMD_SEND_HANDPAY_INFO]) not in sent

    def test_reset_path_does_not_autoservice(self):
        # the operator reset flow dispatches exceptions via _dispatch_event
        # (not _dispatch_and_service), so a 0x51 during the confirm window is
        # NOT answered with a stray 1B read. Verify the two entry points differ.
        machine = HandpayMachine(records=[_real_record()])
        port = MockSASSerialPort(machine)
        collected = []
        poller = SASPoller(port, address=1,
                           on_handpay_info=lambda a, i: collected.append(i))

        poller._dispatch_event(HANDPAY_INFO_PENDING)   # reset-path entry point
        assert machine.b1_reads == 0
        assert collected == []

        poller._dispatch_and_service(HANDPAY_INFO_PENDING)  # poll-path entry
        assert machine.b1_reads == 1
        assert len(collected) == 1

    def test_crc_fail_1b_yields_no_record(self):
        # a 1B answer whose CRC does not validate must not fabricate a record
        class CorruptMachine(HandpayMachine):
            def __call__(self, frame, wakeup):
                r = super().__call__(frame, wakeup)
                if len(frame) == 2 and frame[1] == CMD_SEND_HANDPAY_INFO and r:
                    r = bytearray(r)
                    r[-1] ^= 0xFF          # corrupt the CRC
                    return bytes(r)
                return r

        machine = CorruptMachine(records=[_real_record()])
        port = MockSASSerialPort(machine)
        collected = []
        poller = SASPoller(port, address=1,
                           on_handpay_info=lambda a, i: collected.append(i))

        poller.poll_once()

        assert machine.b1_reads >= 1
        assert collected == []              # corrupt frame -> nothing captured

    def test_service_returns_true_only_for_real_record(self):
        machine = HandpayMachine(records=[_real_record()])
        port = MockSASSerialPort(machine)
        poller = SASPoller(port, address=1)

        assert poller._service_handpay_info() is True    # real record
        assert poller._service_handpay_info() is False   # now all-zeros
