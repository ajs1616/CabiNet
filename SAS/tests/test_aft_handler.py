"""
Tests for the rebuilt host-side AFT stack (modules/aft/aft_handler.py)
against a scripted mock AFT machine over MockSASSerialPort.

Frame constants are the module's believed VERIFY_ON_BENCH values (the
Montana guide documents no AFT polls) — these tests prove self-consistency,
sequencing, and the ledger state machine, not interop. Load-bearing guards:

  * the 0x72 transfer NEVER leaves the host unless registration was read
    back REGISTERED and the game lock was confirmed LOCKED;
  * the BCD regression vectors the old suite got wrong by 10x are pinned to
    the arithmetic truth (BCD 0000123450 cents is $1,234.50, NOT $123.45);
  * frames carry NO sync byte — byte 0 is the address (the old suite
    asserted the fictitious 0x01 sync byte removed 2026-06-12);
  * a foreign transaction's status can never settle our ledger.
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from core.sas_protocol import SASProtocol, sas_crc
from core.sas_poller import SASPoller
from core.sas_meters import int_to_bcd
from modules.aft.aft_handler import (
    AFT_CMD_LOCK_STATUS, AFT_CMD_REGISTER, AFT_CMD_TRANSFER,
    AFT_TRANSFER_COMPLETE_EXCEPTION, AFTGameLockStatus, AFTHost, AFTOutcome,
    AFTRegistration, AFTStateError, AFTStatus, AFTTransferRequest,
    AFTTxnEvent, AFTTxnState, LOCK_CODE_CANCEL, LOCK_CODE_INTERROGATE,
    LOCK_CODE_REQUEST, LOCK_STATUS_LOCKED, LOCK_STATUS_NOT_LOCKED,
    LOCK_STATUS_PENDING, REG_CODE_INITIALIZE, REG_CODE_READ_CURRENT,
    REG_CODE_REGISTER, REG_STATUS_NOT_REGISTERED, REG_STATUS_READY,
    REG_STATUS_REGISTERED, TRANSFER_CODE_CANCEL, TRANSFER_CODE_FULL_ONLY,
    TRANSFER_CODE_INTERROGATE, TRANSFER_TYPE_EGM_TO_HOST,
    TRANSFER_TYPE_HOST_TO_EGM, TRANSFER_CONDITION_TO_EGM,
    TRANSFER_CONDITION_FROM_EGM,
    TRANSFER_FLAGS_NONE, AFT_FLAG_LOCK_REQUIRED,
    AVAIL_XFER_TO_EGM, AFT_ST_IN_HOUSE_ENABLED, AFT_ST_REGISTERED,
    describe_available_transfers, describe_aft_status,
    advance, aft_register, aft_transfer,
    read_asset_number,
    asset_number_bytes, bcd5_to_cents, build_aft_lock_poll,
    build_aft_register_interrogate_poll, build_aft_register_poll,
    build_aft_transfer_poll, cents_to_bcd5, is_terminal,
    make_transaction_id, parse_aft_register_response,
    parse_aft_lock_response, parse_aft_transfer_response,
)
from transport.serial.sas_serial import MockSASSerialPort

NO_SLEEP = lambda _t: None
ASSET = asset_number_bytes(4242)
KEY = bytes(range(20))
POS = b"\x01\x00\x00\x00"


def _request(cents=10000, ttype=TRANSFER_TYPE_HOST_TO_EGM, txn="TESTTXN01"):
    return AFTTransferRequest(transaction_id=txn, transfer_type=ttype,
                              cashable_cents=cents)


class AFTMachine:
    """A pretend AFT-capable machine: registration (0x73), game lock
    (0x74), transfers (0x72) with immediate / pending / refuse / stuck
    behaviors, plus implied-ACK exception FIFO semantics."""

    def __init__(self, address=1, registered=True, allow_register=True,
                 lock_mode="immediate", transfer_mode="immediate",
                 refuse_status=AFTStatus.UNABLE_TO_TRANSFER_AT_THIS_TIME,
                 cancel_ok=True, announce=True, key=KEY, echo_txn=None,
                 asset=ASSET, answer_7b=False, lock_needs_condition=False):
        self.address = address
        self.registered = registered
        self.allow_register = allow_register
        # model the real BB2 root-cause: a lock REQUEST with transfer_condition
        # 0x00 (no condition named) answers NOT_LOCKED; only a request naming a
        # condition bit (bit0 = transfer-to-machine) actually locks.
        self.lock_needs_condition = lock_needs_condition
        # the operator-set asset the machine echoes in 73/74 records; set to
        # b"\x00\x00\x00\x00" to model a machine with no asset assigned yet.
        self.asset = asset
        # when True, answer LP 0x7B (extended validation status) with the
        # asset — models the registration-independent primary source.
        self.answer_7b = answer_7b
        self.lock_mode = lock_mode          # immediate | pending | never
        self.transfer_mode = transfer_mode  # immediate|pending|refuse|stuck
        self.refuse_status = int(refuse_status)
        self.cancel_ok = cancel_ok
        self.announce = announce            # queue 0x69 on completion
        self.key = key
        self.echo_txn = echo_txn            # force a foreign txn id echo
        self.locked = False
        self.lock_polls = 0
        self.credited_cents = 0
        self.last = None                    # (status, cents, txn_id)
        self.fifo = []
        self.pending = None

    def queue(self, *codes):
        self.fifo.extend(codes)

    def __call__(self, frame, wakeup):
        if not wakeup[0]:
            return b""
        if len(frame) == 1:                 # general poll (implied-ACK FIFO)
            polled = frame[0] & 0x7F
            if polled != self.address:
                self.pending = None
                return b""
            if self.pending is not None:
                return bytes([self.pending])
            self.pending = self.fifo.pop(0) if self.fifo else None
            return bytes([self.pending]) if self.pending else b"\x00"
        self.pending = None
        if frame[0] != self.address or len(frame) < 4:
            return b""
        body, crc = frame[:-2], frame[-2:]
        if sas_crc(body).to_bytes(2, "little") != crc:
            return b""
        cmd = frame[1]
        if cmd == AFT_CMD_REGISTER:
            return self._handle_73(frame)
        if cmd == AFT_CMD_LOCK_STATUS:
            return self._handle_74(frame)
        if cmd == AFT_CMD_TRANSFER:
            return self._handle_72(frame)
        if cmd == 0x7B:
            return self._handle_7b(frame)
        return b""

    def _handle_7b(self, frame):
        """LP 0x7B extended validation status — the registration-independent
        asset source. Silent unless answer_7b (models a machine where the
        extended-validation prerequisite is off)."""
        if not self.answer_7b:
            return b""
        body = self.asset + b"\x00\x00" + b"\x00\x00" + b"\x00\x00"
        return self._frame(0x7B, body)

    def _frame(self, cmd, body):
        data = bytes([len(body)]) + body
        raw = bytes([self.address, cmd]) + data
        return raw + sas_crc(raw).to_bytes(2, "little")

    def _reg_body(self, status):
        return bytes([status]) + self.asset + self.key + POS

    def _handle_73(self, frame):
        code = frame[3]
        if code == REG_CODE_READ_CURRENT:
            status = REG_STATUS_REGISTERED if self.registered \
                else REG_STATUS_NOT_REGISTERED
            return self._frame(AFT_CMD_REGISTER, self._reg_body(status))
        if code == REG_CODE_INITIALIZE:
            return self._frame(AFT_CMD_REGISTER,
                               self._reg_body(REG_STATUS_READY))
        if code == REG_CODE_REGISTER:
            if self.allow_register:
                self.registered = True
                return self._frame(AFT_CMD_REGISTER,
                                   self._reg_body(REG_STATUS_REGISTERED))
            return self._frame(AFT_CMD_REGISTER,
                               self._reg_body(REG_STATUS_NOT_REGISTERED))
        return b""

    def _lock_body(self, status):
        return (self.asset + bytes([status, 0x01, 0x00, 0x00, 0x0A])
                + cents_to_bcd5(self.credited_cents) + cents_to_bcd5(0)
                + cents_to_bcd5(0) + cents_to_bcd5(1000000))

    def _handle_74(self, frame):
        code = frame[2]
        if code == LOCK_CODE_REQUEST:
            self.lock_polls = 0
            condition = frame[3]                 # transfer-condition bitmap
            if self.lock_needs_condition and not (condition & 0x01):
                # no condition named -> nothing to lock for -> NOT_LOCKED
                # (the exact BB2 stuck-0xFF behavior, root-caused 2026-07-09)
                self.locked = False
                return self._frame(AFT_CMD_LOCK_STATUS,
                                   self._lock_body(LOCK_STATUS_NOT_LOCKED))
            if self.lock_mode == "immediate":
                self.locked = True
            status = LOCK_STATUS_LOCKED if self.locked else (
                LOCK_STATUS_NOT_LOCKED if self.lock_mode == "never"
                else LOCK_STATUS_PENDING)
            return self._frame(AFT_CMD_LOCK_STATUS, self._lock_body(status))
        if code == LOCK_CODE_INTERROGATE:
            self.lock_polls += 1
            if self.lock_mode == "pending" and self.lock_polls >= 2:
                self.locked = True
            status = LOCK_STATUS_LOCKED if self.locked else (
                LOCK_STATUS_NOT_LOCKED if self.lock_mode == "never"
                else LOCK_STATUS_PENDING)
            return self._frame(AFT_CMD_LOCK_STATUS, self._lock_body(status))
        if code == LOCK_CODE_CANCEL:
            self.locked = False
            return self._frame(AFT_CMD_LOCK_STATUS,
                               self._lock_body(LOCK_STATUS_NOT_LOCKED))
        return b""

    def _txn_body(self, status, cents, txn_id):
        echoed = txn_id if self.echo_txn is None else self.echo_txn
        txn = echoed.encode("ascii")
        return (bytes([0x00, status, 0x00, TRANSFER_TYPE_HOST_TO_EGM])
                + cents_to_bcd5(cents) + cents_to_bcd5(0) + cents_to_bcd5(0)
                + bytes([0x00]) + self.asset + bytes([len(txn)]) + txn)

    def _handle_72(self, frame):
        d = frame[3:-2]
        code = d[0]
        if code == TRANSFER_CODE_INTERROGATE:
            if self.last is None:
                return b""
            return self._frame(AFT_CMD_TRANSFER, self._txn_body(*self.last))
        if code == TRANSFER_CODE_CANCEL:
            if self.last is not None and self.cancel_ok \
                    and self.last[0] == AFTStatus.TRANSFER_PENDING:
                self.last = (int(AFTStatus.TRANSFER_CANCELED),
                             0, self.last[2])
            if self.last is None:
                return b""
            return self._frame(AFT_CMD_TRANSFER, self._txn_body(*self.last))
        # initiate
        cents = int(d[3:8].hex())
        flags = d[18]                            # transfer-flags byte
        lock_required = bool(flags & 0x40)       # bit 6 "accept only if locked"
        key = d[23:43]
        txn_len = d[43]
        txn_id = d[44:44 + txn_len].decode("ascii")
        if not self.registered:
            self.last = (int(AFTStatus.GAMING_MACHINE_NOT_REGISTERED),
                         0, txn_id)
        elif key != self.key:
            self.last = (int(AFTStatus.REGISTRATION_KEY_DOES_NOT_MATCH),
                         0, txn_id)
        elif not self.locked and lock_required:
            # host demanded a lock (bit6=1) but the game is not locked; a
            # LOCKLESS push (bit6=0) skips this and is accepted below.
            self.last = (int(AFTStatus.UNABLE_TO_TRANSFER_AT_THIS_TIME),
                         0, txn_id)
        elif self.transfer_mode == "refuse":
            self.last = (self.refuse_status, 0, txn_id)
        elif self.transfer_mode == "immediate":
            self.credited_cents += cents
            self.locked = False
            self.last = (int(AFTStatus.FULL_TRANSFER_SUCCESSFUL),
                         cents, txn_id)
        else:                               # pending | stuck
            self.last = (int(AFTStatus.TRANSFER_PENDING), 0, txn_id)
            if self.transfer_mode == "pending":
                self.credited_cents += cents
                self.locked = False
                final = (int(AFTStatus.FULL_TRANSFER_SUCCESSFUL),
                         cents, txn_id)
                if self.announce:
                    self.fifo.append(AFT_TRANSFER_COMPLETE_EXCEPTION)
                self.last = final
                return self._frame(AFT_CMD_TRANSFER, self._txn_body(
                    int(AFTStatus.TRANSFER_PENDING), 0, txn_id))
        return self._frame(AFT_CMD_TRANSFER, self._txn_body(*self.last))


def _polls_of(port, command):
    return [f for f, _ in port.sent_frames if len(f) > 1 and f[1] == command]


class TestBCDRegression:
    """The vectors the never-green legacy suite had wrong by 10x."""

    def test_cents_to_bcd5(self):
        assert cents_to_bcd5(12345) == b"\x00\x00\x01\x23\x45"  # $123.45

    def test_bcd5_to_cents_the_old_10x_lies(self):
        # old suite: b'0000123450' == $123.45 — WRONG; it is $1,234.50
        assert bcd5_to_cents(b"\x00\x00\x12\x34\x50") == 123450
        # old suite: b'0000100000' == $100.00 — WRONG; it is $1,000.00
        assert bcd5_to_cents(b"\x00\x00\x10\x00\x00") == 100000

    def test_round_trip(self):
        for cents in (0, 1, 99, 12345, 9999999999):
            assert bcd5_to_cents(cents_to_bcd5(cents)) == cents

    def test_invalid_nibble_is_none(self):
        assert bcd5_to_cents(b"\x00\x00\x0A\x00\x00") is None

    def test_wrong_length_raises(self):
        with pytest.raises(ValueError):
            bcd5_to_cents(b"\x00\x00")

    def test_negative_raises(self):
        with pytest.raises(ValueError):
            cents_to_bcd5(-1)


class TestFrames:
    def test_no_sync_byte_byte0_is_address(self):
        """The old suite asserted command[0] == 0x01 'sync byte' — the sync
        byte is fictitious (removed 2026-06-12). Byte 0 is the ADDRESS."""
        frame = build_aft_register_poll(5, REG_CODE_REGISTER, ASSET, KEY, POS)
        assert frame[0] == 5
        assert frame[1] == AFT_CMD_REGISTER

    def test_register_interrogate_exact_bytes(self):
        frame = build_aft_register_interrogate_poll(1)
        assert frame == bytes.fromhex("017301ffa765")
        assert frame[-2:] == sas_crc(frame[:-2]).to_bytes(2, "little")

    def test_transfer_poll_structure(self):
        req = _request(12345, txn="ABC123")
        frame = build_aft_transfer_poll(1, req, ASSET, KEY)
        assert frame[0] == 1 and frame[1] == AFT_CMD_TRANSFER
        assert frame[2] == len(frame) - 5          # length byte honest
        assert frame[3] == TRANSFER_CODE_FULL_ONLY
        assert frame[6:11] == cents_to_bcd5(12345)
        assert KEY in frame
        assert b"ABC123" in frame
        assert frame[-2:] == sas_crc(frame[:-2]).to_bytes(2, "little")

    def test_transfer_poll_trailing_fields(self):
        # Adjudicated 2026-07-07 vs saspy (sas.py:1290-1309): after txn id come
        # expiration(4 BCD), pool id(2 BCD MSB-first — was binary LE), receipt
        # data length(00), then the trailing lock timeout(2 BCD). VERIFY_ON_BENCH.
        req = AFTTransferRequest(transaction_id="T", cashable_cents=100,
                                 expiration_mmddyyyy=12312025, pool_id=1234,
                                 lock_timeout_hundredths=500)
        frame = build_aft_transfer_poll(1, req, ASSET, KEY)
        body = frame[3:-2]                          # strip addr/cmd/len + crc
        # locate the txn id ("T", len byte 0x01 precedes it) then walk the tail
        i = body.index(b"\x01T") + 2                # past [txnlen=1]["T"]
        assert body[i:i + 4] == bytes.fromhex("12312025")   # expiration BCD
        assert body[i + 4:i + 6] == bytes.fromhex("1234")   # pool id BCD (!LE)
        assert body[i + 6] == 0x00                          # receipt data len
        assert body[i + 7:i + 9] == bytes.fromhex("0500")   # lock timeout BCD
        assert i + 9 == len(body)                           # nothing trails it
        assert frame[-2:] == sas_crc(frame[:-2]).to_bytes(2, "little")

    def test_lock_poll_structure(self):
        frame = build_aft_lock_poll(1, LOCK_CODE_REQUEST, 0x00, 500)
        assert frame[:4] == bytes([1, AFT_CMD_LOCK_STATUS,
                                   LOCK_CODE_REQUEST, 0x00])
        assert frame[4:6] == int_to_bcd(500, 2)
        assert frame[-2:] == sas_crc(frame[:-2]).to_bytes(2, "little")

    def test_parse_register_response_round_trip(self):
        body = bytes([REG_STATUS_REGISTERED]) + ASSET + KEY + POS
        reg = parse_aft_register_response(bytes([len(body)]) + body)
        assert reg.registered
        assert reg.asset_number == ASSET
        assert reg.registration_key == KEY
        assert reg.pos_id == POS

    def test_parse_rejects_lying_length_byte(self):
        with pytest.raises(ValueError):
            parse_aft_transfer_response(bytes([99, 0x00, 0x00]))

    def test_request_validation(self):
        with pytest.raises(ValueError):
            _request(0)                             # zero total
        with pytest.raises(ValueError):
            AFTTransferRequest(transaction_id="X", cashable_cents=-5)
        with pytest.raises(ValueError):
            AFTTransferRequest(transaction_id="Y" * 21, cashable_cents=100)
        with pytest.raises(ValueError):
            AFTTransferRequest(transaction_id="", cashable_cents=100)

    def test_transaction_ids_unique_and_wire_legal(self):
        ids = {make_transaction_id() for _ in range(1000)}
        assert len(ids) == 1000
        assert all(len(i) <= 20 and i.isascii() for i in ids)


class TestStateMachine:
    def test_happy_path(self):
        s = AFTTxnState.CREATED
        s = advance(s, AFTTxnEvent.LOCK_CONFIRMED)
        s = advance(s, AFTTxnEvent.TRANSFER_ISSUED)
        s = advance(s, AFTTxnEvent.STATUS_PENDING)
        s = advance(s, AFTTxnEvent.STATUS_FULL)
        assert s is AFTTxnState.COMPLETED_FULL
        assert is_terminal(s)

    def test_rollback_path(self):
        s = advance(AFTTxnState.CREATED, AFTTxnEvent.LOCK_CONFIRMED)
        s = advance(s, AFTTxnEvent.TRANSFER_ISSUED)
        s = advance(s, AFTTxnEvent.CANCEL_CONFIRMED)
        assert s is AFTTxnState.ROLLED_BACK and is_terminal(s)

    def test_illegal_transitions_raise(self):
        with pytest.raises(AFTStateError):
            advance(AFTTxnState.CREATED, AFTTxnEvent.STATUS_FULL)
        with pytest.raises(AFTStateError):
            advance(AFTTxnState.COMPLETED_FULL, AFTTxnEvent.TRANSFER_ISSUED)
        with pytest.raises(AFTStateError):
            advance(AFTTxnState.LOCKED, AFTTxnEvent.STATUS_FULL)


class TestRegistration:
    def test_register_happy(self):
        machine = AFTMachine(registered=False)
        port = MockSASSerialPort(machine)
        ok, reg, detail = aft_register(port, 1, ASSET, KEY, POS,
                                       sleep=NO_SLEEP)
        assert ok and reg.registered
        assert machine.registered is True

    def test_register_refused_is_typed(self):
        machine = AFTMachine(registered=False, allow_register=False)
        port = MockSASSerialPort(machine)
        ok, reg, detail = aft_register(port, 1, ASSET, KEY, POS,
                                       sleep=NO_SLEEP)
        assert not ok
        assert reg is not None and not reg.registered
        assert "refused" in detail

    def test_register_silence_is_typed(self):
        port = MockSASSerialPort(lambda f, w: b"")
        ok, reg, detail = aft_register(port, 1, ASSET, KEY, POS,
                                       sleep=NO_SLEEP)
        assert not ok and reg is None and "silence" in detail


class TestTransferSequence:
    def test_happy_immediate(self):
        machine = AFTMachine()
        port = MockSASSerialPort(machine)
        result = aft_transfer(port, 1, _request(12345), ASSET, KEY,
                              sleep=NO_SLEEP)
        assert result.outcome is AFTOutcome.COMPLETED
        assert result.ok
        assert result.state is AFTTxnState.COMPLETED_FULL
        assert machine.credited_cents == 12345
        assert result.final.cashable_cents == 12345
        # choreography order: 73 interrogate, then 74 lock, then 72
        seq = [f[1] for f, _ in port.sent_frames if len(f) > 2]
        assert seq.index(AFT_CMD_REGISTER) < seq.index(AFT_CMD_LOCK_STATUS)
        assert seq.index(AFT_CMD_LOCK_STATUS) < seq.index(AFT_CMD_TRANSFER)

    def test_pending_then_exception_then_interrogate(self):
        machine = AFTMachine(transfer_mode="pending")
        port = MockSASSerialPort(machine)
        result = aft_transfer(port, 1, _request(5000), ASSET, KEY,
                              sleep=NO_SLEEP)
        assert result.outcome is AFTOutcome.COMPLETED
        assert result.confirmed_by_exception is True
        assert machine.credited_cents == 5000
        assert any(f[3] == TRANSFER_CODE_INTERROGATE
                   for f in _polls_of(port, AFT_CMD_TRANSFER))

    def test_never_transfers_when_not_registered(self):
        machine = AFTMachine(registered=False)
        port = MockSASSerialPort(machine)
        result = aft_transfer(port, 1, _request(), ASSET, KEY,
                              sleep=NO_SLEEP)
        assert result.outcome is AFTOutcome.NOT_REGISTERED
        assert _polls_of(port, AFT_CMD_TRANSFER) == []      # THE guard
        assert _polls_of(port, AFT_CMD_LOCK_STATUS) == []
        assert machine.credited_cents == 0

    def test_never_transfers_without_lock(self):
        machine = AFTMachine(lock_mode="never")
        port = MockSASSerialPort(machine)
        result = aft_transfer(port, 1, _request(), ASSET, KEY,
                              sleep=NO_SLEEP, lock_attempts=2)
        assert result.outcome is AFTOutcome.LOCK_FAILED
        assert _polls_of(port, AFT_CMD_TRANSFER) == []      # THE guard
        # and the failed lock attempt was cancelled on the way out
        assert any(f[2] == LOCK_CODE_CANCEL
                   for f in _polls_of(port, AFT_CMD_LOCK_STATUS))
        assert machine.credited_cents == 0

    def test_lock_condition_matches_transfer_direction(self):
        # THE direction-aware lock fix (host-control cash-out): a host->EGM
        # push locks for TO_EGM (bit0); an EGM->host cash-out pull locks for
        # FROM_EGM (bit1). Naming the wrong bit — or the 0x00 default — leaves a
        # real machine nothing to lock for and it sticks at 0xFF NOT_LOCKED
        # (the 2026-07-08 root cause). Assert the lock REQUEST names the right
        # condition per direction, and the transfer still completes.
        cases = ((TRANSFER_TYPE_HOST_TO_EGM, TRANSFER_CONDITION_TO_EGM),
                 (TRANSFER_TYPE_EGM_TO_HOST, TRANSFER_CONDITION_FROM_EGM))
        for ttype, want in cases:
            machine = AFTMachine()
            port = MockSASSerialPort(machine)
            result = aft_transfer(port, 1, _request(1000, ttype), ASSET, KEY,
                                  sleep=NO_SLEEP)
            req_locks = [f for f in _polls_of(port, AFT_CMD_LOCK_STATUS)
                         if f[2] == LOCK_CODE_REQUEST]
            assert req_locks, f"0x{ttype:02X}: no lock REQUEST sent"
            assert req_locks[0][3] == want, (
                f"0x{ttype:02X}: lock condition 0x{req_locks[0][3]:02X} "
                f"!= 0x{want:02X}")
            assert result.ok, f"0x{ttype:02X}: transfer did not complete"

    def test_pending_lock_resolves_then_transfers(self):
        machine = AFTMachine(lock_mode="pending")
        port = MockSASSerialPort(machine)
        result = aft_transfer(port, 1, _request(700), ASSET, KEY,
                              sleep=NO_SLEEP)
        assert result.outcome is AFTOutcome.COMPLETED
        assert machine.credited_cents == 700

    def test_machine_refusal_is_typed_with_status_name(self):
        machine = AFTMachine(transfer_mode="refuse",
                             refuse_status=AFTStatus.UNABLE_TO_TRANSFER_AT_THIS_TIME)
        port = MockSASSerialPort(machine)
        result = aft_transfer(port, 1, _request(), ASSET, KEY,
                              sleep=NO_SLEEP)
        assert result.outcome is AFTOutcome.REFUSED
        assert result.state is AFTTxnState.FAILED
        assert "UNABLE_TO_TRANSFER_AT_THIS_TIME" in result.detail
        assert machine.credited_cents == 0

    def test_wrong_registration_key_refused(self):
        machine = AFTMachine(key=bytes(20))       # machine expects zeros
        port = MockSASSerialPort(machine)
        result = aft_transfer(port, 1, _request(), ASSET, KEY,
                              sleep=NO_SLEEP)
        assert result.outcome is AFTOutcome.REFUSED
        assert "REGISTRATION_KEY_DOES_NOT_MATCH" in result.detail

    def test_stuck_pending_rolls_back_via_cancel(self):
        machine = AFTMachine(transfer_mode="stuck", cancel_ok=True)
        port = MockSASSerialPort(machine)
        result = aft_transfer(port, 1, _request(), ASSET, KEY,
                              sleep=NO_SLEEP, confirm_polls=2)
        assert result.outcome is AFTOutcome.ROLLED_BACK
        assert result.state is AFTTxnState.ROLLED_BACK
        assert machine.credited_cents == 0
        assert any(f[3] == TRANSFER_CODE_CANCEL
                   for f in _polls_of(port, AFT_CMD_TRANSFER))

    def test_stuck_pending_cancel_ignored_is_unconfirmed(self):
        machine = AFTMachine(transfer_mode="stuck", cancel_ok=False)
        port = MockSASSerialPort(machine)
        result = aft_transfer(port, 1, _request(), ASSET, KEY,
                              sleep=NO_SLEEP, confirm_polls=2)
        assert result.outcome is AFTOutcome.UNCONFIRMED
        assert "VERIFY METERS" in result.detail

    def test_foreign_txn_status_never_settles_our_ledger(self):
        """Interrogate echoing a DIFFERENT transaction id must not complete
        our transfer."""
        machine = AFTMachine(transfer_mode="stuck", cancel_ok=False,
                             echo_txn="SOMEONEELSE")
        port = MockSASSerialPort(machine)
        result = aft_transfer(port, 1, _request(txn="OURTXN"), ASSET, KEY,
                              sleep=NO_SLEEP, confirm_polls=1)
        assert result.outcome is AFTOutcome.UNCONFIRMED
        assert result.state is not AFTTxnState.COMPLETED_FULL

    def test_foreign_cancel_status_never_rolls_back_our_ledger(self):
        """Step-5 (post-cancel) interrogate reporting a DIFFERENT
        transaction's TRANSFER_CANCELED must not settle ours as
        ROLLED_BACK — the reservation must stay held (UNCONFIRMED)."""
        machine = AFTMachine(transfer_mode="stuck", cancel_ok=True,
                             echo_txn="SOMEONEELSE")
        port = MockSASSerialPort(machine)
        result = aft_transfer(port, 1, _request(txn="OURTXN"), ASSET, KEY,
                              sleep=NO_SLEEP, confirm_polls=1)
        assert result.outcome is AFTOutcome.UNCONFIRMED
        assert result.state is not AFTTxnState.ROLLED_BACK

    def test_unidentified_txn_status_never_settles_our_ledger(self):
        """An interrogate echoing NO transaction id (txn_len=0) must not
        complete our transfer — a zeroed status byte reads as
        FULL_TRANSFER_SUCCESSFUL, so an empty id must fail the guard."""
        machine = AFTMachine(transfer_mode="pending", echo_txn="")
        port = MockSASSerialPort(machine)
        result = aft_transfer(port, 1, _request(txn="OURTXN"), ASSET, KEY,
                              sleep=NO_SLEEP, confirm_polls=1)
        assert result.outcome is AFTOutcome.UNCONFIRMED
        assert result.state is not AFTTxnState.COMPLETED_FULL

    def test_corrupt_lock_response_is_not_consent(self):
        inner = AFTMachine()

        def corruptor(frame, wakeup):
            resp = inner(frame, wakeup)
            if len(frame) > 2 and frame[1] == AFT_CMD_LOCK_STATUS:
                return resp[:-2] + b"\x00\x00"    # break the CRC
            return resp

        port = MockSASSerialPort(corruptor)
        result = aft_transfer(port, 1, _request(), ASSET, KEY,
                              sleep=NO_SLEEP, lock_attempts=1)
        assert result.outcome is AFTOutcome.LOCK_FAILED
        assert _polls_of(port, AFT_CMD_TRANSFER) == []

    def test_egm_to_host_transfer_type_accepted(self):
        req = _request(2500, ttype=TRANSFER_TYPE_EGM_TO_HOST)
        frame = build_aft_transfer_poll(1, req, ASSET, KEY)
        assert frame[5] == TRANSFER_TYPE_EGM_TO_HOST


class TestPollerIntegration:
    def test_afthost_transfer_and_callback(self):
        machine = AFTMachine()
        port = MockSASSerialPort(machine)
        results = []
        poller = SASPoller(port, address=1)
        host = AFTHost(poller, ASSET, KEY, POS, sleep=NO_SLEEP,
                       on_transfer=lambda a, r: results.append((a, r)))
        result = host.transfer(_request(300))
        assert result.ok
        assert results == [(1, result)]
        assert machine.credited_cents == 300

    def test_drained_exceptions_still_dispatch(self):
        machine = AFTMachine(transfer_mode="pending")
        machine.queue(0x11)                       # door opens mid-confirm
        port = MockSASSerialPort(machine)
        events = []
        poller = SASPoller(port, address=1,
                           on_event=lambda a, c, n: events.append(c))
        host = AFTHost(poller, ASSET, KEY, POS, sleep=NO_SLEEP)
        result = host.transfer(_request(100))
        assert result.ok
        assert 0x11 in events                     # not swallowed
        assert AFT_TRANSFER_COMPLETE_EXCEPTION not in events

    def test_host_cashout_request_surfaced(self):
        machine = AFTMachine()
        machine.queue(0x6A)
        port = MockSASSerialPort(machine)
        requests = []
        poller = SASPoller(port, address=1)
        AFTHost(poller, ASSET, KEY, POS, sleep=NO_SLEEP,
                on_cashout_request=lambda a, c: requests.append((a, c)))
        poller.poll_once()
        assert requests == [(1, 0x6A)]

    def test_registration_status_read(self):
        machine = AFTMachine(registered=True)
        port = MockSASSerialPort(machine)
        poller = SASPoller(port, address=1)
        host = AFTHost(poller, ASSET, KEY, POS, sleep=NO_SLEEP)
        reg = host.registration_status()
        assert reg is not None and reg.registered

    def test_bad_identity_rejected_at_construction(self):
        poller = SASPoller(MockSASSerialPort(), address=1)
        with pytest.raises(ValueError):
            AFTHost(poller, b"\x01", KEY)         # short asset number
        with pytest.raises(ValueError):
            AFTHost(poller, ASSET, b"short")      # short key


class TestAssetRead:
    """read_asset_number: the operator-set asset is READ (73/FF echo -> 0x7B
    -> 0x74/FF, first non-zero wins), never assigned."""

    def test_prefers_73ff_echo_when_passed(self):
        port = MockSASSerialPort(AFTMachine(registered=False, answer_7b=True))
        reg = AFTRegistration(REG_STATUS_NOT_REGISTERED, ASSET, KEY, POS)
        asset = read_asset_number(port, 1, reg=reg, sleep=NO_SLEEP)
        assert asset == ASSET
        # the echo satisfied it — no 0x7B or 0x74 poll went out
        assert port.sent_frames == []

    def test_prefers_7b_over_lock(self):
        # no echo (reg=None); 0x7B answers, so the 0x74 fallback is skipped
        machine = AFTMachine(registered=False, answer_7b=True)
        port = MockSASSerialPort(machine)
        asset = read_asset_number(port, 1, sleep=NO_SLEEP)
        assert asset == ASSET
        assert _polls_of(port, AFT_CMD_LOCK_STATUS) == []

    def test_falls_through_to_lock_when_7b_silent(self):
        # 0x7B unsupported (silence) -> 0x74/FF interrogate echoes the asset
        machine = AFTMachine(registered=False)          # answer_7b defaults off
        port = MockSASSerialPort(machine)
        asset = read_asset_number(port, 1, sleep=NO_SLEEP)
        assert asset == ASSET
        assert any(f[2] == LOCK_CODE_INTERROGATE
                   for f in _polls_of(port, AFT_CMD_LOCK_STATUS))

    def test_none_when_every_source_zero_or_silent(self):
        machine = AFTMachine(registered=False, asset=b"\x00\x00\x00\x00")
        port = MockSASSerialPort(machine)
        assert read_asset_number(port, 1, sleep=NO_SLEEP) is None


class TestAutoRegistration:
    """AFTHost.auto_register(): the ONE idempotent read-then-register path
    behind both the online auto-trigger and the manual button."""

    def test_asset_number_optional_at_construction(self):
        poller = SASPoller(MockSASSerialPort(), address=1)
        host = AFTHost(poller, asset_number=None, registration_key=KEY)
        assert host.asset_number is None            # built for registration

    def test_reads_asset_then_registers_and_adopts(self):
        machine = AFTMachine(registered=False)      # 73/FF echoes ASSET
        port = MockSASSerialPort(machine)
        poller = SASPoller(port, address=1)
        host = AFTHost(poller, asset_number=None, registration_key=KEY,
                       pos_id=POS, sleep=NO_SLEEP)
        ok, reg, detail = host.auto_register()
        assert ok and reg.registered
        assert machine.registered is True
        assert reg.asset_number == ASSET            # the READ asset
        assert host.asset_number == ASSET           # adopted for transfers
        assert host.registration_key == KEY         # echoed key adopted
        # the register poll carried the READ asset verbatim (Byte 4-7)
        reg_polls = [f for f in _polls_of(port, AFT_CMD_REGISTER)
                     if f[3] == REG_CODE_REGISTER]
        assert reg_polls and reg_polls[0][4:8] == ASSET

    def test_idempotent_when_already_registered(self):
        machine = AFTMachine(registered=True)
        port = MockSASSerialPort(machine)
        poller = SASPoller(port, address=1)
        host = AFTHost(poller, None, KEY, POS, sleep=NO_SLEEP)
        ok, reg, detail = host.auto_register()
        assert ok and reg.registered and detail == "already registered"
        # only the 73/FF interrogate was sent — never 73/00 or 73/01
        codes = [f[3] for f in _polls_of(port, AFT_CMD_REGISTER)]
        assert codes == [REG_CODE_READ_CURRENT]
        assert host.asset_number == ASSET           # read-back asset adopted

    def test_asset_unknown_never_registers_or_invents(self):
        machine = AFTMachine(registered=False, asset=b"\x00\x00\x00\x00")
        port = MockSASSerialPort(machine)
        poller = SASPoller(port, address=1)
        host = AFTHost(poller, None, KEY, POS, sleep=NO_SLEEP)
        ok, reg, detail = host.auto_register()
        assert not ok
        assert "no asset number" in detail
        assert reg is not None and not reg.registered
        assert machine.registered is False          # never registered
        codes = [f[3] for f in _polls_of(port, AFT_CMD_REGISTER)]
        assert REG_CODE_INITIALIZE not in codes
        assert REG_CODE_REGISTER not in codes

    def test_adopts_minted_key_for_a_later_transfer(self):
        # mint-mode: the machine returns its OWN key in the register record
        # (not the rig key we sent). auto_register must ADOPT it so a later
        # transfer on the same instance uses the machine's key.
        machine_key = bytes(range(100, 120))        # != the rig KEY
        machine = AFTMachine(registered=False, key=machine_key)
        port = MockSASSerialPort(machine)
        poller = SASPoller(port, address=1)
        host = AFTHost(poller, asset_number=None, registration_key=KEY,
                       pos_id=POS, sleep=NO_SLEEP)
        ok, reg, detail = host.auto_register()
        assert ok and host.registration_key == machine_key
        result = host.transfer(_request(100))       # adopted key -> accepted
        assert result.ok

    def test_transfer_with_no_asset_is_typed_not_crash(self):
        # AFTHost built for registration only (asset None): a push must return
        # a typed result, NEVER a TypeError on len(None), and send no 0x72.
        machine = AFTMachine()
        port = MockSASSerialPort(machine)
        poller = SASPoller(port, address=1)
        host = AFTHost(poller, asset_number=None, registration_key=KEY,
                       pos_id=POS, sleep=NO_SLEEP)
        result = host.transfer(_request(100), require_lock=False)
        assert result.outcome is AFTOutcome.NOT_REGISTERED
        assert "asset" in result.detail
        assert _polls_of(port, AFT_CMD_TRANSFER) == []
        assert machine.credited_cents == 0

    def test_tito_event_still_dispatches_with_afthost_on_top(self, tmp_path):
        # regression: SASTITOHost and AFTHost both monkey-patch
        # poller.on_typed_event by save-prev/call-prev. Constructed TITO-first
        # (the sas_host.py order), a TITO-relevant event must still reach the
        # base listener with an AFTHost installed on top.
        from core.sas_tito_host import SASTITOHost
        from core.sas_ticket_store import TicketStore
        machine = AFTMachine()
        machine.queue(0x11)                         # a door event mid-stream
        port = MockSASSerialPort(machine)
        codes = []
        poller = SASPoller(port, address=1,
                           on_typed_event=lambda a, info: codes.append(info.code))
        SASTITOHost(poller, TicketStore(path=str(tmp_path / "t.json")),
                    sleep=NO_SLEEP)
        AFTHost(poller, asset_number=None, registration_key=KEY, pos_id=POS,
                sleep=NO_SLEEP)
        poller.poll_once()
        assert 0x11 in codes                        # chain still forwards it


class TestLocklessTransfer:
    """The house->EGM cashable push goes LOCKLESS (require_lock=False, flags
    bit6=0): real SAS 6.02 says a bit6=0 transfer needs NO game lock, so the
    credits issue straight from CREATED with no 0x74 dance (adjudicated
    2026-07-09 — the stuck-0xFF was our transfer_condition=0x00 bug, not a
    machine requirement)."""

    def test_state_machine_lockless_issue_is_legal(self):
        s = advance(AFTTxnState.CREATED, AFTTxnEvent.TRANSFER_ISSUED)
        assert s is AFTTxnState.ISSUED
        assert advance(s, AFTTxnEvent.STATUS_FULL) is AFTTxnState.COMPLETED_FULL

    def test_lockless_sends_no_lock_poll(self):
        machine = AFTMachine()
        port = MockSASSerialPort(machine)
        result = aft_transfer(port, 1, _request(500), ASSET, KEY,
                              sleep=NO_SLEEP, require_lock=False)
        assert result.outcome is AFTOutcome.COMPLETED
        assert result.ok
        assert result.state is AFTTxnState.COMPLETED_FULL
        assert result.lock is None                     # never locked
        assert machine.credited_cents == 500
        # THE point: not a single 0x74 lock poll went out
        assert _polls_of(port, AFT_CMD_LOCK_STATUS) == []
        seq = [f[1] for f, _ in port.sent_frames if len(f) > 2]
        assert AFT_CMD_LOCK_STATUS not in seq
        assert seq.index(AFT_CMD_REGISTER) < seq.index(AFT_CMD_TRANSFER)

    def test_lockless_still_gates_on_registration(self):
        machine = AFTMachine(registered=False)
        port = MockSASSerialPort(machine)
        result = aft_transfer(port, 1, _request(), ASSET, KEY,
                              sleep=NO_SLEEP, require_lock=False)
        assert result.outcome is AFTOutcome.NOT_REGISTERED
        assert _polls_of(port, AFT_CMD_TRANSFER) == []   # THE guard survives
        assert machine.credited_cents == 0

    def test_lockless_pending_escrow_then_completes(self):
        # a bit6=0 push the machine escrows (PENDING) then completes via the
        # 0x69 exception — no lock, still lands.
        machine = AFTMachine(transfer_mode="pending")
        port = MockSASSerialPort(machine)
        result = aft_transfer(port, 1, _request(750), ASSET, KEY,
                              sleep=NO_SLEEP, require_lock=False)
        assert result.outcome is AFTOutcome.COMPLETED
        assert result.confirmed_by_exception is True
        assert machine.credited_cents == 750
        assert _polls_of(port, AFT_CMD_LOCK_STATUS) == []

    def test_lockless_contradiction_with_lock_flag_raises(self):
        # asking lockless while the wire flag demands a lock is a caller bug —
        # refuse loudly and send nothing.
        req = AFTTransferRequest(transaction_id="X", cashable_cents=100,
                                 transfer_flags=AFT_FLAG_LOCK_REQUIRED)
        port = MockSASSerialPort(AFTMachine())
        with pytest.raises(ValueError):
            aft_transfer(port, 1, req, ASSET, KEY, sleep=NO_SLEEP,
                         require_lock=False)
        assert port.sent_frames == []                  # nothing hit the wire

    def test_lockless_flags_byte_is_zero_on_the_wire(self):
        # the push request carries flags=0x00 (TRANSFER_FLAGS_NONE) so the EGM
        # sees "no lock required". Layout: addr,cmd,len,code,idx,type, then
        # 3x5-byte amounts (frame[6:21]), then the flags byte at frame[21].
        req = AFTTransferRequest(transaction_id="T", cashable_cents=100,
                                 transfer_flags=TRANSFER_FLAGS_NONE)
        frame = build_aft_transfer_poll(1, req, ASSET, KEY)
        assert frame[21] == TRANSFER_FLAGS_NONE        # flags byte


class TestLockCondition:
    """The stuck-0xFF root cause + fix: a 0x74 lock request with
    transfer_condition 0x00 names no condition, so a real machine answers
    NOT_LOCKED. The retained lock path now sends bit0 (transfer-to-machine)."""

    def test_lock_path_now_names_the_condition_bit(self):
        # a machine that ONLY locks when a condition bit is set — the fixed
        # orchestration (require_lock=True default) reaches LOCKED and transfers.
        machine = AFTMachine(lock_needs_condition=True)
        port = MockSASSerialPort(machine)
        result = aft_transfer(port, 1, _request(300), ASSET, KEY,
                              sleep=NO_SLEEP)
        assert result.outcome is AFTOutcome.COMPLETED
        assert machine.credited_cents == 300
        reqs = [f for f in _polls_of(port, AFT_CMD_LOCK_STATUS)
                if f[2] == LOCK_CODE_REQUEST]
        assert reqs and reqs[0][3] == TRANSFER_CONDITION_TO_EGM

    def test_condition_zero_never_locks_but_bit0_does(self):
        # prove the bug directly and the fix beside it.
        machine = AFTMachine(lock_needs_condition=True)
        port = MockSASSerialPort(machine)
        proto = SASProtocol()
        resp = port.transact(build_aft_lock_poll(1, LOCK_CODE_REQUEST, 0x00))
        lock = parse_aft_lock_response(proto.parse_packet(resp).data)
        assert lock.lock_status == LOCK_STATUS_NOT_LOCKED          # the bug
        resp = port.transact(build_aft_lock_poll(
            1, LOCK_CODE_REQUEST, TRANSFER_CONDITION_TO_EGM))
        lock = parse_aft_lock_response(proto.parse_packet(resp).data)
        assert lock.lock_status == LOCK_STATUS_LOCKED              # the fix


class TestRealSpecTables:
    """Constants transcribed from the REAL SAS 6.02 (Tables 8.2b/8.3d/8.3e,
    2026-07-09) — pinned against the live BB2 wire values captured the same
    day so a future 'correction' back to the old fictional enum fails loudly."""

    def test_status_0x82_is_not_a_valid_transfer_function(self):
        # the live BB2 refusal byte: 0x82 = not-a-valid-transfer-function
        # (the pre-2026-07-09 enum called this "TIMEOUT" — fiction)
        assert AFTStatus(0x82).name == "NOT_A_VALID_TRANSFER_FUNCTION"
        assert AFTStatus(0x87).name == "UNABLE_TO_TRANSFER_AT_THIS_TIME"
        assert AFTStatus(0x88).name == "GAMING_MACHINE_NOT_REGISTERED"
        assert AFTStatus(0x89).name == "REGISTRATION_KEY_DOES_NOT_MATCH"
        assert AFTStatus(0x94).name == "GAME_NOT_LOCKED"

    def test_transfer_types_match_table_83d(self):
        assert TRANSFER_TYPE_HOST_TO_EGM == 0x00
        assert TRANSFER_TYPE_EGM_TO_HOST == 0x80   # was wrongly 0x60 (debit)
        # 0x40/0x60 are DEBIT types, never in-house

    def test_live_bb2_bitmap_decode(self):
        # the exact bytes the BB2 answered post-RAM-clear (2026-07-09):
        # availXfers=0x04 (printer only — to-machine NOT available),
        # aftStatus=0x8D (registered, but in-house transfers DISABLED)
        assert describe_available_transfers(0x04) == "to-printer"
        assert not 0x04 & AVAIL_XFER_TO_EGM
        s = describe_aft_status(0x8D)
        assert "registered" in s and "in-house-DISABLED" in s
        assert not 0x8D & AFT_ST_IN_HOUSE_ENABLED
        assert 0x8D & AFT_ST_REGISTERED
        # and the pre-RAM-clear healthy values (Jul 8 capture)
        assert "to-machine" in describe_available_transfers(0x17)
        assert "in-house-ENABLED" in describe_aft_status(0xFF)
