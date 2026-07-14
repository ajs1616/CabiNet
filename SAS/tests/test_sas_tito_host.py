"""
Tests for the host-side TITO stack (core/sas_tito_host.py) against a
scripted mock TITO machine over MockSASSerialPort.

Like the handpay-reset suite these prove SELF-CONSISTENCY and sequencing,
not interop: the guide-cited pieces (4C/4D/3D reads, exception 0x3D) are
tested to the Montana layouts, while the whole 0x67/0x70/0x71/0x68
redemption cycle is the module's believed VERIFY_ON_BENCH frames. The
load-bearing guards:

  * the 0x71 authorize NEVER leaves the host unless the TicketStore said
    yes — unknown/redeemed/void/in-flight tickets draw an explicit REJECT;
  * corrupt/foreign frames are silence, never consent;
  * capture and redemption are idempotent (retry-safe) against the store.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from core.sas_protocol import SASProtocol, sas_crc
from core.sas_poller import SASPoller
from core.sas_meters import int_to_bcd
from core.sas_ticket_store import TicketStore, ISSUED, REDEEM_PENDING, REDEEMED
from core.sas_tito_host import (
    CMD_REDEEM_TICKET, CMD_SEND_TICKET_VALIDATION_DATA,
    REDEEM_AUTHORIZE_CASHABLE, REDEEM_INTERROGATE, REDEEM_REJECT,
    REDEEM_STATUS_PENDING, REDEEM_STATUS_REDEEMED, REDEEM_STATUS_REJECTED,
    TICKET_INSERTED_EXCEPTION, TICKET_PRINTED_EXCEPTION,
    VALIDATION_ID_NOT_CONFIGURED_EXCEPTION,
    TICKET_TRANSFER_COMPLETE_EXCEPTION, PARSING_CODE_BCD,
    RedemptionOutcome, SASTITOHost, TicketCaptureOutcome,
    ValidationIdOutcome, build_redeem_interrogate_poll,
    build_redeem_ticket_poll, build_ticket_data_poll, capture_printed_ticket,
    configure_validation_id, parse_ticket_in_data, redeem_inserted_ticket,
)
from transport.serial.sas_serial import MockSASSerialPort

NO_SLEEP = lambda _t: None
VN16 = "0012345678901234"        # a 16-digit enhanced validation number
AMOUNT = 12345                   # $123.45 in cents


def _frame(address, command, data):
    body = bytes([address, command]) + data
    return body + sas_crc(body).to_bytes(2, "little")


def _bcd(value, length):
    return int_to_bcd(value, length)


class TITOMachine:
    """A pretend TITO-capable machine: serves the guide-cited 4C/4D/3D
    reads, escrows an inserted ticket for the believed 0x70/0x71 cycle, and
    models implied-ACK exception FIFO semantics (the HandpayMachine
    pattern)."""

    def __init__(self, address=1, enhanced=True, printed=None,
                 escrow=None, redeem_mode="immediate",
                 announce_complete=True, answer_71=True):
        self.address = address
        self.enhanced = enhanced            # answers 4D (and 4C)?
        # printed ticket visible to the 4D/3D reads:
        #   {"vn": digits, "amount": cents, "ticket_number": int}
        self.printed = printed
        # ticket sitting in escrow for 0x70: {"vn": digits, "amount": cents}
        self.escrow = escrow
        self.redeem_mode = redeem_mode      # 'immediate'|'pending'|'reject'
        self.announce_complete = announce_complete
        self.answer_71 = answer_71
        self.final_status = None            # settled 0x71 status
        self.credited_cents = 0
        self.returned_ticket = False
        self.fifo = []
        self.pending = None

    def queue(self, *codes):
        self.fifo.extend(codes)

    # -- response bodies ------------------------------------------------------

    def _ticket_body(self, status, amount, vn):
        # BB2 (bench 2026-07-08): the validation field is a 1-byte
        # validation-type prefix (0x01) + the 8-byte BCD number = 9 bytes.
        validation = bytes([0x01]) + _bcd(int(vn), 8)
        data = bytes([status]) + _bcd(amount, 5) \
            + bytes([PARSING_CODE_BCD]) + validation
        return bytes([len(data)]) + data

    def _record_4d(self):
        p = self.printed
        data = bytes([0x00, 0x00])                     # type, buffer index
        data += bytes.fromhex("07072026")              # date 07/07/2026 BCD
        data += bytes.fromhex("123456")                # time 12:34:56 BCD
        data += _bcd(int(p["vn"]), 8)                  # validation number
        data += _bcd(p["amount"], 5)                   # amount cents
        data += p.get("ticket_number", 7).to_bytes(2, "little")
        data += b"\x00"                                # system id 00
        data += b"\x00" * 6                            # reserved
        return data

    # -- the machine ------------------------------------------------------------

    def __call__(self, frame, wakeup):
        if not wakeup[0]:
            return b""
        if len(frame) == 1:                            # general poll
            polled = frame[0] & 0x7F
            if polled != self.address:
                self.pending = None                    # implied ACK
                return b""
            if self.pending is not None:
                return bytes([self.pending])           # implied NACK: resend
            self.pending = self.fifo.pop(0) if self.fifo else None
            return bytes([self.pending]) if self.pending else b"\x00"

        self.pending = None                            # long poll = ACK
        if frame[0] != self.address:
            return b""

        if len(frame) == 2:                            # type R read
            cmd = frame[1]
            if cmd == CMD_SEND_TICKET_VALIDATION_DATA:
                if self.escrow is None:
                    return _frame(self.address, cmd,
                                  self._ticket_body(0x00, 0, 0 * 1))
                return _frame(self.address, cmd, self._ticket_body(
                    0x00, self.escrow["amount"], self.escrow["vn"]))
            if cmd == 0x3D:                            # guide §4.6.3
                if self.enhanced or self.printed is None:
                    data = _bcd(0, 4) + _bcd(
                        self.printed["amount"] if self.printed else 0, 5)
                else:
                    data = _bcd(int(self.printed["vn"]), 4) \
                        + _bcd(self.printed["amount"], 5)
                return _frame(self.address, cmd, data)
            return b""                                 # unsupported: silence

        body, crc = frame[:-2], frame[-2:]
        if sas_crc(body).to_bytes(2, "little") != crc:
            return b""                                 # bad CRC: ignored
        cmd = frame[1]
        if cmd == 0x4C and self.enhanced:              # guide §4.6.1 echo
            return _frame(self.address, 0x4C, frame[2:8])
        if cmd == 0x4D:                                # guide §4.6.2
            if not self.enhanced:
                return b""
            if self.printed is None:
                return _frame(self.address, 0x4D, b"\x00" * 31)
            return _frame(self.address, 0x4D, self._record_4d())
        if cmd == CMD_REDEEM_TICKET:
            return self._handle_71(frame)
        return b""

    def _handle_71(self, frame):
        if not self.answer_71:
            return b""
        data = frame[2:-2]
        transfer_code = data[1]
        if transfer_code == REDEEM_INTERROGATE:
            status = self.final_status if self.final_status is not None \
                else REDEEM_STATUS_PENDING
            return _frame(self.address, CMD_REDEEM_TICKET,
                          self._ticket_body(status, self.credited_cents,
                                            self.escrow["vn"] if self.escrow
                                            else "0"))
        amount = int(data[2:7].hex())
        val_field = data[8:]                       # validation echoed by host
        vn = val_field[-8:].hex()                  # trailing 8 = the BCD number
        if transfer_code == REDEEM_REJECT:
            self.returned_ticket = True
            self.escrow = None
            return _frame(self.address, CMD_REDEEM_TICKET,
                          self._ticket_body(REDEEM_STATUS_REJECTED, 0, vn))
        # authorize: the real EGM matches the echoed validation field against
        # its escrow byte-for-byte and answers 0x81 on any mismatch (bench
        # 2026-07-08). Re-encoding/truncating the 9-byte field is THE bug.
        if self.escrow is not None:
            expected = bytes([0x01]) + _bcd(int(self.escrow["vn"]), 8)
            if val_field != expected:
                self.returned_ticket = True
                self.final_status = 0x81
                return _frame(self.address, CMD_REDEEM_TICKET,
                              self._ticket_body(0x81, 0, vn))
        if self.redeem_mode == "reject":
            self.returned_ticket = True
            self.escrow = None
            self.final_status = REDEEM_STATUS_REJECTED
            return _frame(self.address, CMD_REDEEM_TICKET,
                          self._ticket_body(REDEEM_STATUS_REJECTED, 0, vn))
        if self.redeem_mode == "pending":
            self.final_status = None
            self._complete_later(amount, vn)
            return _frame(self.address, CMD_REDEEM_TICKET,
                          self._ticket_body(REDEEM_STATUS_PENDING, 0, vn))
        # immediate
        self.credited_cents += amount
        self.escrow = None
        self.final_status = REDEEM_STATUS_REDEEMED
        return _frame(self.address, CMD_REDEEM_TICKET,
                      self._ticket_body(REDEEM_STATUS_REDEEMED, amount, vn))

    def _complete_later(self, amount, vn):
        """Model the async credit: completion lands when the host next
        drains the FIFO (exception 0x68), after which interrogate reports
        redeemed."""
        self.credited_cents += amount
        self.final_status = REDEEM_STATUS_REDEEMED
        if self.announce_complete:
            self.fifo.append(TICKET_TRANSFER_COMPLETE_EXCEPTION)


def _store(tmp_path, *tickets):
    store = TicketStore(str(tmp_path / "tickets.json"))
    for vn, amount in tickets:
        store.record_issued(vn, amount, 1)
    return store


def _polls_of(port, command):
    return [f for f, _ in port.sent_frames if len(f) > 1 and f[1] == command]


class TestFrameBuilders:
    def test_ticket_data_poll_is_bare_type_r(self):
        assert build_ticket_data_poll(1) == b"\x01\x70"

    def test_redeem_poll_layout_and_crc(self):
        # BENCH-PROVEN 2026-07-08 (WMS BB2): the 0x71 body is
        # [transfer][amount 5-BCD][parsing 00][validation field echoed from
        # the 0x70 read], NO restricted-expiration/pool-id. The machine's own
        # 0x71 response proved the 16-byte body + the 9-byte echoed validation
        # (a 1-byte validation-type prefix + the 8-byte BCD number). The
        # earlier saspy-shaped 21-byte frame drew a 0x81 reject.
        val9 = bytes.fromhex("01" + "0012345678901234")   # prefix + 8-byte BCD
        frame = build_redeem_ticket_poll(1, REDEEM_AUTHORIZE_CASHABLE,
                                         AMOUNT, val9)
        assert frame[0] == 1 and frame[1] == 0x71
        assert frame[2] == 0x10                     # length byte = 7 + 9
        assert frame[3] == REDEEM_AUTHORIZE_CASHABLE
        assert frame[4:9] == bytes.fromhex("0000012345")   # $123.45 BCD
        assert frame[9] == PARSING_CODE_BCD
        assert frame[10:19] == val9                        # validation echoed verbatim
        assert frame[-2:] == sas_crc(frame[:-2]).to_bytes(2, "little")
        assert len(frame) == 3 + 0x10 + 2                  # addr+cmd+len+body+crc

    def test_redeem_echoes_the_raw_validation_field(self):
        # A different-width validation field (8 bytes, no prefix) is echoed
        # byte-for-byte and the length byte tracks it.
        val8 = bytes.fromhex("0012345678901234")
        frame = build_redeem_ticket_poll(1, REDEEM_AUTHORIZE_CASHABLE,
                                         AMOUNT, val8)
        assert frame[2] == 7 + 8
        assert frame[10:18] == val8
        assert frame[-2:] == sas_crc(frame[:-2]).to_bytes(2, "little")

    def test_redeem_requires_validation_field(self):
        try:
            build_redeem_ticket_poll(1, REDEEM_AUTHORIZE_CASHABLE, AMOUNT, b"")
        except ValueError:
            pass
        else:
            raise AssertionError("empty validation field must raise")

    def test_interrogate_poll(self):
        frame = build_redeem_interrogate_poll(1)
        assert frame[:4] == bytes([1, 0x71, 1, 0xFF])
        assert frame[-2:] == sas_crc(frame[:-2]).to_bytes(2, "little")

    def test_bad_transfer_code_raises(self):
        try:
            build_redeem_ticket_poll(1, 0x55, AMOUNT,
                                     bytes.fromhex("01" + "00" * 8))
        except ValueError:
            pass
        else:
            raise AssertionError("unknown transfer code must raise")

    def test_parse_rejects_lying_length_byte(self):
        try:
            parse_ticket_in_data(bytes([9, 0x00, 1, 2]))
        except ValueError:
            pass
        else:
            raise AssertionError("length mismatch must raise")

    def test_parse_short_status_only_record(self):
        t = parse_ticket_in_data(bytes([1, REDEEM_STATUS_PENDING]))
        assert t.status == REDEEM_STATUS_PENDING
        assert t.validation_number is None

    def test_parse_extracts_trailing_8byte_validation_number(self):
        # BENCH 2026-07-08 (WMS BB2): a ticket minted 0100000000000001 read
        # back with a leading validation-type/length byte, i.e. the 0x70 val
        # field = 01 + the 8-byte BCD number. parse_ticket_in_data must take
        # the TRAILING 8 bytes so the machine-added prefix doesn't corrupt the
        # ledger lookup (this is the fix that unblocked live redemption).
        val = bytes.fromhex("01" + "0100000000000001")   # prefix + 8-byte BCD
        payload = bytes([0x00]) + _bcd(10, 5) + bytes([0x00]) + val
        data = bytes([len(payload)]) + payload
        t = parse_ticket_in_data(data)
        assert t.validation_number == "0100000000000001"
        assert len(t.validation_number) == 16


class TestValidationIdConfig:
    def test_accepted_echo(self):
        machine = TITOMachine()
        port = MockSASSerialPort(machine)
        result = configure_validation_id(port, 1, b"\x01\x02\x03",
                                         b"\x00\x00\x01")
        assert result.outcome is ValidationIdOutcome.ACCEPTED
        assert result.ok
        assert result.vgm_id == b"\x01\x02\x03"
        assert result.sequence == b"\x00\x00\x01"

    def test_silence_is_typed_ignored(self):
        machine = TITOMachine(enhanced=False)
        port = MockSASSerialPort(machine)
        result = configure_validation_id(port, 1, b"\x01\x02\x03",
                                         b"\x00\x00\x01")
        assert result.outcome is ValidationIdOutcome.IGNORED
        assert not result.ok


class TestTicketCapture:
    def test_enhanced_capture_records_ticket(self, tmp_path):
        machine = TITOMachine(printed={"vn": VN16, "amount": AMOUNT,
                                       "ticket_number": 42})
        port = MockSASSerialPort(machine)
        store = _store(tmp_path)
        result = capture_printed_ticket(port, 1, store, sleep=NO_SLEEP)

        assert result.outcome is TicketCaptureOutcome.RECORDED
        assert result.ok and result.via == "4D"
        assert result.validation_number == VN16
        assert result.amount_cents == AMOUNT
        rec = store.get(VN16)
        assert rec["state"] == ISSUED
        assert rec["amountCents"] == AMOUNT
        assert rec["ticketNumber"] == 42

    def test_recapture_is_idempotent_duplicate(self, tmp_path):
        machine = TITOMachine(printed={"vn": VN16, "amount": AMOUNT})
        port = MockSASSerialPort(machine)
        store = _store(tmp_path)
        assert capture_printed_ticket(port, 1, store, sleep=NO_SLEEP).ok
        result = capture_printed_ticket(port, 1, store, sleep=NO_SLEEP)
        assert result.outcome is TicketCaptureOutcome.DUPLICATE
        assert result.ok
        assert len(store.outstanding()) == 1

    def test_standard_fallback_via_3d(self, tmp_path):
        machine = TITOMachine(enhanced=False,
                              printed={"vn": "00123456", "amount": 777})
        port = MockSASSerialPort(machine)
        store = _store(tmp_path)
        result = capture_printed_ticket(port, 1, store, sleep=NO_SLEEP)
        assert result.outcome is TicketCaptureOutcome.RECORDED
        assert result.via == "3D"
        # persisted under the canonical 16-digit key, so the 0x70 escrow
        # read (8-byte field -> 16 digits) can find it again at redemption
        assert result.validation_number == "0000000000123456"
        assert store.get("0000000000123456")["amountCents"] == 777

    def test_3d_captured_ticket_redeems_at_16_digit_width(self, tmp_path):
        """The key-width regression: an 8-digit standard capture must be
        redeemable when the 0x70 read reports the same number in the
        believed 8-byte (16-digit) validation field."""
        machine = TITOMachine(enhanced=False,
                              printed={"vn": "00123456", "amount": 777})
        port = MockSASSerialPort(machine)
        store = _store(tmp_path)
        assert capture_printed_ticket(port, 1, store, sleep=NO_SLEEP).ok
        machine.escrow = {"vn": "00123456", "amount": 777}
        result = redeem_inserted_ticket(port, 1, store, sleep=NO_SLEEP)
        assert result.outcome is RedemptionOutcome.REDEEMED
        assert result.amount_cents == 777
        assert machine.credited_cents == 777

    def test_zeroed_4d_record_is_no_data(self, tmp_path):
        machine = TITOMachine(printed=None)      # empty validation buffer
        port = MockSASSerialPort(machine)
        result = capture_printed_ticket(port, 1, _store(tmp_path),
                                        sleep=NO_SLEEP)
        assert result.outcome is TicketCaptureOutcome.NO_DATA

    def test_total_silence_is_timeout(self, tmp_path):
        port = MockSASSerialPort(lambda f, w: b"")
        result = capture_printed_ticket(port, 1, _store(tmp_path),
                                        sleep=NO_SLEEP)
        assert result.outcome is TicketCaptureOutcome.TIMEOUT


class TestRedemption:
    def test_happy_immediate(self, tmp_path):
        machine = TITOMachine(escrow={"vn": VN16, "amount": AMOUNT})
        port = MockSASSerialPort(machine)
        store = _store(tmp_path, (VN16, AMOUNT))
        result = redeem_inserted_ticket(port, 1, store, sleep=NO_SLEEP)

        assert result.outcome is RedemptionOutcome.REDEEMED
        assert result.ok
        assert result.amount_cents == AMOUNT
        assert machine.credited_cents == AMOUNT
        assert store.get(VN16)["state"] == REDEEMED
        # exactly one authorize on the wire, carrying the STORED amount
        authorizes = [f for f in _polls_of(port, CMD_REDEEM_TICKET)
                      if f[3] == REDEEM_AUTHORIZE_CASHABLE]
        assert len(authorizes) == 1
        assert authorizes[0][4:9] == bytes.fromhex("0000012345")

    def test_pending_then_exception_then_interrogate(self, tmp_path):
        machine = TITOMachine(escrow={"vn": VN16, "amount": AMOUNT},
                              redeem_mode="pending")
        port = MockSASSerialPort(machine)
        store = _store(tmp_path, (VN16, AMOUNT))
        result = redeem_inserted_ticket(port, 1, store, sleep=NO_SLEEP)

        assert result.outcome is RedemptionOutcome.REDEEMED
        assert result.confirmed_by_exception is True
        assert store.get(VN16)["state"] == REDEEMED
        assert machine.credited_cents == AMOUNT
        # the interrogate went out after the 0x68 confirmation
        assert any(f[3] == REDEEM_INTERROGATE
                   for f in _polls_of(port, CMD_REDEEM_TICKET))

    def test_unknown_ticket_is_denied_with_explicit_reject(self, tmp_path):
        machine = TITOMachine(escrow={"vn": VN16, "amount": AMOUNT})
        port = MockSASSerialPort(machine)
        store = _store(tmp_path)                 # ticket never issued
        result = redeem_inserted_ticket(port, 1, store, sleep=NO_SLEEP)

        assert result.outcome is RedemptionOutcome.DENIED
        assert result.amount_cents == 0
        assert "unknown" in result.store_reason
        rejects = [f for f in _polls_of(port, CMD_REDEEM_TICKET)
                   if f[3] == REDEEM_REJECT]
        assert len(rejects) == 1
        assert rejects[0][4:9] == bytes(5)       # amount 0
        assert machine.returned_ticket is True
        assert machine.credited_cents == 0

    def test_already_redeemed_is_denied(self, tmp_path):
        machine = TITOMachine(escrow={"vn": VN16, "amount": AMOUNT})
        port = MockSASSerialPort(machine)
        store = _store(tmp_path, (VN16, AMOUNT))
        store.authorize_redemption(1, VN16)
        store.close_redemption(1, VN16, redeemed=True)
        result = redeem_inserted_ticket(port, 1, store, sleep=NO_SLEEP)
        assert result.outcome is RedemptionOutcome.DENIED
        assert "already redeemed" in result.store_reason

    def test_machine_reject_resets_ticket_to_issued(self, tmp_path):
        machine = TITOMachine(escrow={"vn": VN16, "amount": AMOUNT},
                              redeem_mode="reject")
        port = MockSASSerialPort(machine)
        store = _store(tmp_path, (VN16, AMOUNT))
        result = redeem_inserted_ticket(port, 1, store, sleep=NO_SLEEP)

        assert result.outcome is RedemptionOutcome.MACHINE_REJECTED
        assert store.get(VN16)["state"] == ISSUED   # paper still valid
        assert machine.credited_cents == 0

    def test_unconfirmed_leaves_pending_and_retry_completes(self, tmp_path):
        """Authorize acked as pending but no 0x68 and interrogate silent:
        typed UNCONFIRMED, store stays redeemPending — then a retry from
        the same machine re-draws the same authorization and completes."""
        machine = TITOMachine(escrow={"vn": VN16, "amount": AMOUNT},
                              redeem_mode="pending", announce_complete=False)
        port = MockSASSerialPort(machine)
        store = _store(tmp_path, (VN16, AMOUNT))
        # first pass: machine goes silent after the pending ack
        machine.final_status = None
        silent_after_pending = machine

        def flaky(frame, wakeup):
            resp = silent_after_pending(frame, wakeup)
            if len(frame) > 2 and frame[1] == CMD_REDEEM_TICKET \
                    and frame[3] == REDEEM_INTERROGATE:
                return b""                       # interrogate lost
            return resp

        result = redeem_inserted_ticket(MockSASSerialPort(flaky), 1, store,
                                        sleep=NO_SLEEP, confirm_polls=2)
        assert result.outcome is RedemptionOutcome.UNCONFIRMED
        assert store.get(VN16)["state"] == REDEEM_PENDING

        # retry: same machine, healthy now (escrow re-read, same auth)
        machine.escrow = {"vn": VN16, "amount": AMOUNT}
        machine.redeem_mode = "immediate"
        result = redeem_inserted_ticket(port, 1, store, sleep=NO_SLEEP)
        assert result.outcome is RedemptionOutcome.REDEEMED
        assert "retry" in result.store_reason
        assert store.get(VN16)["state"] == REDEEMED

    def test_second_machine_cannot_steal_pending_redemption(self, tmp_path):
        store = _store(tmp_path, (VN16, AMOUNT))
        store.authorize_redemption(1, VN16)      # machine 1 holds it
        machine2 = TITOMachine(address=2,
                               escrow={"vn": VN16, "amount": AMOUNT})
        port = MockSASSerialPort(machine2)
        result = redeem_inserted_ticket(port, 2, store, sleep=NO_SLEEP)
        assert result.outcome is RedemptionOutcome.DENIED
        assert "already in process" in result.store_reason
        assert store.get(VN16)["state"] == REDEEM_PENDING

    def test_corrupt_70_response_is_not_consent(self, tmp_path):
        inner = TITOMachine(escrow={"vn": VN16, "amount": AMOUNT})

        def corruptor(frame, wakeup):
            resp = inner(frame, wakeup)
            if len(frame) == 2 and frame[1] == CMD_SEND_TICKET_VALIDATION_DATA:
                return resp[:-2] + b"\x00\x00"   # break the CRC
            return resp

        port = MockSASSerialPort(corruptor)
        store = _store(tmp_path, (VN16, AMOUNT))
        result = redeem_inserted_ticket(port, 1, store, sleep=NO_SLEEP)
        assert result.outcome is RedemptionOutcome.TIMEOUT
        assert _polls_of(port, CMD_REDEEM_TICKET) == []   # no 0x71 at all
        assert store.get(VN16)["state"] == ISSUED

    def test_non_bcd_validation_is_typed_result_not_crash(self, tmp_path):
        """A 0x70 answer whose validation field carries a non-BCD nibble
        (hexed to a-f) used to CRASH the deny path building the 0x71 —
        it must be a typed result with no 0x71 on the wire."""
        def machine(frame, wakeup):
            if not wakeup[0]:
                return b""
            if len(frame) == 2 and frame[1] == CMD_SEND_TICKET_VALIDATION_DATA:
                data = bytes([0x00]) + _bcd(500, 5) \
                    + bytes([PARSING_CODE_BCD]) + b"\xab" * 8
                return _frame(1, CMD_SEND_TICKET_VALIDATION_DATA,
                              bytes([len(data)]) + data)
            return b""

        port = MockSASSerialPort(machine)
        store = _store(tmp_path, (VN16, AMOUNT))
        result = redeem_inserted_ticket(port, 1, store, sleep=NO_SLEEP)
        assert result.outcome is RedemptionOutcome.TIMEOUT
        assert "malformed validation" in result.detail
        assert _polls_of(port, CMD_REDEEM_TICKET) == []   # no 0x71 at all
        assert store.get(VN16)["state"] == ISSUED

    def test_empty_escrow_is_no_ticket(self, tmp_path):
        machine = TITOMachine(escrow=None)
        port = MockSASSerialPort(machine)
        result = redeem_inserted_ticket(port, 1, _store(tmp_path),
                                        sleep=NO_SLEEP)
        assert result.outcome is RedemptionOutcome.NO_TICKET
        assert _polls_of(port, CMD_REDEEM_TICKET) == []


class TestPollerIntegration:
    def test_printed_exception_triggers_capture(self, tmp_path):
        machine = TITOMachine(printed={"vn": VN16, "amount": AMOUNT})
        machine.queue(TICKET_PRINTED_EXCEPTION)
        port = MockSASSerialPort(machine)
        store = _store(tmp_path)
        issued, seen = [], []
        poller = SASPoller(port, address=1,
                           on_typed_event=lambda a, i: seen.append(i.code))
        SASTITOHost(poller, store, sleep=NO_SLEEP,
                    on_ticket_issued=lambda a, r: issued.append((a, r)))
        poller.poll_once()

        assert len(issued) == 1
        addr, result = issued[0]
        assert addr == 1 and result.ok
        assert store.get(VN16)["state"] == ISSUED
        # the pre-existing typed hook still saw the raw event (chained)
        assert TICKET_PRINTED_EXCEPTION in seen

    def test_inserted_exception_triggers_auto_redeem(self, tmp_path):
        machine = TITOMachine(escrow={"vn": VN16, "amount": AMOUNT})
        machine.queue(TICKET_INSERTED_EXCEPTION)
        port = MockSASSerialPort(machine)
        store = _store(tmp_path, (VN16, AMOUNT))
        redemptions = []
        poller = SASPoller(port, address=1)
        SASTITOHost(poller, store, sleep=NO_SLEEP,
                    on_redemption=lambda a, r: redemptions.append(r))
        poller.poll_once()

        assert len(redemptions) == 1
        assert redemptions[0].outcome is RedemptionOutcome.REDEEMED
        assert store.get(VN16)["state"] == REDEEMED

    def test_auto_redeem_can_be_disabled(self, tmp_path):
        machine = TITOMachine(escrow={"vn": VN16, "amount": AMOUNT})
        machine.queue(TICKET_INSERTED_EXCEPTION)
        port = MockSASSerialPort(machine)
        store = _store(tmp_path, (VN16, AMOUNT))
        redemptions = []
        poller = SASPoller(port, address=1)
        tito = SASTITOHost(poller, store, auto_redeem=False, sleep=NO_SLEEP,
                           on_redemption=lambda a, r: redemptions.append(r))
        poller.poll_once()
        assert redemptions == []                 # nothing automatic
        result = tito.redeem()                   # manual trigger works
        assert result.outcome is RedemptionOutcome.REDEEMED

    def test_callback_exception_is_isolated(self, tmp_path):
        machine = TITOMachine(printed={"vn": VN16, "amount": AMOUNT})
        machine.queue(TICKET_PRINTED_EXCEPTION)
        port = MockSASSerialPort(machine)

        def boom(*a):
            raise RuntimeError("bad bridge callback")

        poller = SASPoller(port, address=1)
        SASTITOHost(poller, _store(tmp_path), sleep=NO_SLEEP,
                    on_ticket_issued=boom)
        poller.poll_once()                       # must not raise
        assert poller.state.callback_errors >= 1

    def test_validation_not_configured_triggers_auto_seed(self, tmp_path):
        # Enhanced-validation EGM tilts with 0x3F until the host seeds its
        # validation ID (0x4C); the auto-seed clears it and the EGM then
        # self-mints. The 3-byte ID carries our system-ID namespace.
        machine = TITOMachine()                  # enhanced -> echoes 0x4C
        machine.queue(VALIDATION_ID_NOT_CONFIGURED_EXCEPTION)
        port = MockSASSerialPort(machine)
        seeded = []
        poller = SASPoller(port, address=1)
        SASTITOHost(poller, _store(tmp_path), sleep=NO_SLEEP, system_id=7,
                    on_validation_seeded=lambda a, r: seeded.append((a, r)))
        poller.poll_once()

        assert len(seeded) == 1
        addr, result = seeded[0]
        assert addr == 1 and result.ok
        assert result.vgm_id == b"\x00\x00\x07"

    def test_auto_seed_can_be_disabled(self, tmp_path):
        machine = TITOMachine()
        machine.queue(VALIDATION_ID_NOT_CONFIGURED_EXCEPTION)
        port = MockSASSerialPort(machine)
        seeded = []
        poller = SASPoller(port, address=1)
        tito = SASTITOHost(poller, _store(tmp_path), auto_seed=False,
                           sleep=NO_SLEEP,
                           on_validation_seeded=lambda a, r: seeded.append(r))
        poller.poll_once()
        assert seeded == []                      # nothing automatic
        assert tito.seed_validation_id(force=True).ok   # manual still works

    def test_auto_seed_cooldown_blocks_reseed(self, tmp_path):
        # A 0x3F lingering in the poll cycle right after a successful seed must
        # NOT re-fire 0x4C and reset the starting sequence (barcode reuse).
        machine = TITOMachine()
        port = MockSASSerialPort(machine)
        poller = SASPoller(port, address=1)
        tito = SASTITOHost(poller, _store(tmp_path), sleep=NO_SLEEP)
        assert tito.seed_validation_id().ok      # first seed lands
        assert tito.seed_validation_id() is None  # within cooldown -> skipped
        assert tito.seed_validation_id(force=True).ok   # force overrides

    def test_seed_sequence_advances_across_reseeds(self, tmp_path,
                                                   monkeypatch):
        # REPLAY GUARD: the enhanced vn is a deterministic function of
        # (validation ID, sequence) — re-seeding a RAM-cleared EGM with the
        # fixed default sequence made it REPRINT its old barcode series in
        # order (live-proven 2026-07-12: the 07-09 series replayed and
        # collided with redeemed/void tickets). The default seed now derives
        # the starting sequence from wall-clock seconds: never the fixed
        # b"\x00\x00\x01", and fresh number space on every re-seed. The mock
        # echoes frame[2:8], so result.sequence IS what went on the wire.
        machine = TITOMachine()
        port = MockSASSerialPort(machine)
        poller = SASPoller(port, address=1)
        tito = SASTITOHost(poller, _store(tmp_path), auto_seed=False,
                           sleep=NO_SLEEP)
        import core.sas_tito_host as mod
        monkeypatch.setattr(mod.time, "time", lambda: 1_783_900_000)
        r1 = tito.seed_validation_id(force=True)
        monkeypatch.setattr(mod.time, "time", lambda: 1_783_900_777)
        r2 = tito.seed_validation_id(force=True)
        assert r1.ok and r2.ok
        assert r1.sequence != b"\x00\x00\x01"    # not the replayable constant
        assert r1.sequence != r2.sequence        # re-seed -> fresh space
        assert r1.sequence == (1_783_900_000 & 0xFFFFFF).to_bytes(3, "big")
        # an EXPLICIT bench sequence still wins verbatim
        tito2 = SASTITOHost(SASPoller(MockSASSerialPort(TITOMachine()),
                                      address=1),
                            _store(tmp_path), auto_seed=False, sleep=NO_SLEEP,
                            seed_sequence=b"\x00\x10\x00")
        assert tito2.seed_validation_id(force=True).sequence == b"\x00\x10\x00"
