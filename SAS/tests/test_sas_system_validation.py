"""
Tests for the SAS system-validation ticket-OUT responder (SAS 6.02 §15.8,
cross-verified 2026-07-08 — see the memory reference
reference_casinonet_sas_system_validation). CasinoNet is the HOST that mints
the validation number in real time when a SYSTEM-mode EGM raises exception
0x57 at cash-out.

Coverage, following the tests/ patterns (mock transport, golden frames,
self-consistency not interop):

  * builders/parser (0x57 read, 0x58 answer) at the byte level;
  * the host-side mint: monotonic, system-ID-prefixed, persisted, and
    round-tripping into the 0x4D 8-BCD width;
  * service_system_validation for approve (both PAYLOAD_STYLE values),
    reject, silence (SILENCE != CONSENT: no 0x58 approve), and no-data;
  * poller integration incl. the 0x57 -> mint -> 0x58 -> 0x3D reconcile so
    the printed ticket is recorded exactly once.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from core.sas_protocol import SASProtocol, sas_crc
from core.sas_poller import SASPoller
from core.sas_exceptions import lookup, ExceptionCategory
from core.sas_meters import (
    SEND_PENDING_CASHOUT, RECEIVE_VALIDATION_NUMBER, TYPE_R_POLLS,
    bcd_to_int, int_to_bcd, PendingCashout,
    build_send_pending_cashout_poll, parse_pending_cashout,
    build_receive_validation_number_poll,
)
from core.sas_ticket_store import (
    TicketStore, ISSUED, DEFAULT_SYSTEM_ID, ValidationMintError,
)
from core.sas_tito_host import (
    SYSTEM_VALIDATION_REQUEST_EXCEPTION, TICKET_PRINTED_EXCEPTION,
    VALIDATION_APPROVE, VALIDATION_REJECT,
    PAYLOAD_STYLE_BCD, PAYLOAD_STYLE_TYPE_AMOUNT, DEFAULT_PAYLOAD_STYLE,
    SystemValidationOutcome, SASTITOHost, build_validation_payload,
    service_system_validation,
)
from transport.serial.sas_serial import MockSASSerialPort

NO_SLEEP = lambda _t: None
AMOUNT = 1000            # 10 credits in cents (the memory's expected BB2 value)


def _frame(address, command, data):
    body = bytes([address, command]) + data
    return body + sas_crc(body).to_bytes(2, "little")


class SysValMachine:
    """A pretend SYSTEM-mode EGM: answers the 0x57 pending-cashout read,
    accepts the 0x58 validation number, and (on approve) prints — surfacing
    the host-supplied validation number back through a 0x4D record and queuing
    exception 0x3D, exactly as a real cabinet closes the loop. Models the
    implied-ACK FIFO like TITOMachine/HandpayMachine."""

    def __init__(self, address=1, cashout=None, answer_57=True,
                 answer_58=True, print_on_approve=True):
        self.address = address
        self.cashout = cashout          # {"type": int, "amount": cents} | None
        self.answer_57 = answer_57
        self.answer_58 = answer_58
        self.print_on_approve = print_on_approve
        self.fifo = []
        self._exc = None                # exception awaiting implied ACK
        self.approvals = []             # [(validation_id, payload_bytes)]
        self.printed = None             # {"vn": digits, "amount": cents}

    def queue(self, *codes):
        self.fifo.extend(codes)

    def _record_4d(self):
        p = self.printed
        data = bytes([0x00, 0x00])                     # type, buffer index
        data += bytes.fromhex("07082026")              # date 07/08/2026 BCD
        data += bytes.fromhex("101112")                # time 10:11:12 BCD
        data += int_to_bcd(int(p["vn"]), 8)            # validation number
        data += int_to_bcd(p["amount"], 5)             # amount cents
        data += (9).to_bytes(2, "little")              # ticket number
        data += bytes([DEFAULT_SYSTEM_ID])             # system id 01
        data += b"\x00" * 6                            # reserved
        return data

    def __call__(self, frame, wakeup):
        if not wakeup[0]:
            return b""
        if len(frame) == 1:                            # general poll
            polled = frame[0] & 0x7F
            if polled != self.address:
                self._exc = None                       # implied ACK
                return b""
            if self._exc is not None:
                return bytes([self._exc])              # implied NACK: resend
            self._exc = self.fifo.pop(0) if self.fifo else None
            return bytes([self._exc]) if self._exc else b"\x00"

        self._exc = None                               # long poll = ACK
        if frame[0] != self.address:
            return b""

        if len(frame) == 2:                            # type R read
            cmd = frame[1]
            if cmd == SEND_PENDING_CASHOUT:
                if not self.answer_57:
                    return b""                         # dead silence
                c = self.cashout or {"type": 0, "amount": 0}
                body = bytes([c["type"]]) + int_to_bcd(c["amount"], 5)
                return _frame(self.address, cmd, body)
            if cmd == 0x3D:                            # standard fallback read
                data = int_to_bcd(0, 4) + int_to_bcd(
                    self.printed["amount"] if self.printed else 0, 5)
                return _frame(self.address, cmd, data)
            return b""

        body, crc = frame[:-2], frame[-2:]
        if sas_crc(body).to_bytes(2, "little") != crc:
            return b""                                 # bad CRC: ignored
        cmd = frame[1]
        if cmd == RECEIVE_VALIDATION_NUMBER:
            if not self.answer_58:
                return b""
            validation_id = frame[2]
            payload = frame[3:11]
            self.approvals.append((validation_id, payload))
            if validation_id == VALIDATION_APPROVE and self.print_on_approve:
                self.printed = {"vn": payload.hex(),
                                "amount": (self.cashout or {}).get("amount", 0)}
                self.fifo.append(TICKET_PRINTED_EXCEPTION)
            return _frame(self.address, RECEIVE_VALIDATION_NUMBER,
                          bytes([validation_id]))
        if cmd == 0x4D:                                # enhanced record read
            if self.printed is None:
                return _frame(self.address, 0x4D, b"\x00" * 31)
            return _frame(self.address, 0x4D, self._record_4d())
        return b""


def _store(tmp_path, name="tickets.json"):
    return TicketStore(str(tmp_path / name))


def _polls_of(port, command):
    return [f for f, _ in port.sent_frames if len(f) > 1 and f[1] == command]


# --------------------------------------------------------------------------
# Builders + parser (golden bytes)
# --------------------------------------------------------------------------

class TestFrameBuilders:
    def test_send_pending_cashout_is_bare_type_r(self):
        assert build_send_pending_cashout_poll(1) == b"\x01\x57"
        assert SEND_PENDING_CASHOUT in TYPE_R_POLLS

    def test_receive_validation_number_layout_and_crc(self):
        payload = int_to_bcd(1234567890123456, 8)
        frame = build_receive_validation_number_poll(1, VALIDATION_APPROVE,
                                                     payload)
        assert len(frame) == 13
        assert frame[0] == 1 and frame[1] == RECEIVE_VALIDATION_NUMBER
        assert frame[2] == VALIDATION_APPROVE
        assert frame[3:11] == payload
        assert frame[-2:] == sas_crc(frame[:-2]).to_bytes(2, "little")

    def test_receive_validation_number_rejects_wrong_payload_len(self):
        for bad in (b"", b"\x00" * 7, b"\x00" * 9):
            try:
                build_receive_validation_number_poll(1, VALIDATION_APPROVE, bad)
            except ValueError:
                pass
            else:
                raise AssertionError(f"payload len {len(bad)} must raise")

    def test_parse_pending_cashout_round_trip(self):
        data = bytes([0x00]) + int_to_bcd(AMOUNT, 5)
        pend = parse_pending_cashout(data)
        assert pend.cashout_type == 0
        assert pend.amount_cents == AMOUNT
        assert pend.amount_raw == int_to_bcd(AMOUNT, 5)
        # tuple-unpackable per the ticket's stated signature
        ctype, cents, raw = pend
        assert (ctype, cents) == (0, AMOUNT)

    def test_parse_pending_cashout_bad_length_raises(self):
        try:
            parse_pending_cashout(bytes([0x00, 0x00]))
        except ValueError:
            pass
        else:
            raise AssertionError("wrong-length 0x57 data must raise")

    def test_parse_pending_cashout_non_bcd_amount_is_none(self):
        pend = parse_pending_cashout(bytes([0x00, 0xAB, 0, 0, 0, 0]))
        assert pend.amount_cents is None       # caller must treat as bad read


# --------------------------------------------------------------------------
# The 8-byte 0x58 payload styles
# --------------------------------------------------------------------------

class TestPayloadStyles:
    def test_bcd_number_style(self):
        vn = "0100000000000042"
        payload = build_validation_payload(PAYLOAD_STYLE_BCD, vn, 0, b"\x00" * 5)
        assert len(payload) == 8
        assert payload.hex() == vn             # packed BCD == the digit string
        assert bcd_to_int(payload) == int(vn)

    def test_type_amount_style_echoes_amount(self):
        amount_raw = int_to_bcd(AMOUNT, 5)
        payload = build_validation_payload(PAYLOAD_STYLE_TYPE_AMOUNT,
                                           "0100000000000001", 2, amount_raw)
        assert payload == bytes([2, 0, 0]) + amount_raw
        assert len(payload) == 8

    def test_unknown_style_raises(self):
        try:
            build_validation_payload("nope", "01", 0, b"\x00" * 5)
        except ValueError:
            pass
        else:
            raise AssertionError("unknown style must raise")


# --------------------------------------------------------------------------
# Host-side mint
# --------------------------------------------------------------------------

class TestMint:
    def test_mint_is_monotonic_and_prefixed(self, tmp_path):
        store = _store(tmp_path)
        a = store.mint_validation_number(AMOUNT, 1)
        b = store.mint_validation_number(AMOUNT, 1)
        assert a["seq"] + 1 == b["seq"]
        assert a["validation_number"] != b["validation_number"]
        assert a["system_id"] == DEFAULT_SYSTEM_ID
        assert a["validation_number"].startswith("01")
        assert len(a["validation_number"]) == 16

    def test_mint_custom_system_id_prefix(self, tmp_path):
        m = _store(tmp_path).mint_validation_number(AMOUNT, 1, system_id=7)
        assert m["system_id"] == 7
        assert m["validation_number"].startswith("07")

    def test_mint_round_trips_through_8_bcd(self, tmp_path):
        vn = _store(tmp_path).mint_validation_number(AMOUNT, 1)[
            "validation_number"]
        packed = int_to_bcd(int(vn), 8)
        assert len(packed) == 8
        assert packed.hex() == vn              # 8-BCD width == the 0x4D field

    def test_mint_persists_sequence_across_reload(self, tmp_path):
        path = str(tmp_path / "tickets.json")
        first = TicketStore(path).mint_validation_number(AMOUNT, 1)
        second = TicketStore(path).mint_validation_number(AMOUNT, 1)
        assert second["seq"] == first["seq"] + 1

    def test_mint_tracks_pending_then_reconciles_on_record(self, tmp_path):
        store = _store(tmp_path)
        vn = store.mint_validation_number(AMOUNT, 1)["validation_number"]
        assert [p["validationNumber"] for p in store.pending_validations()] \
            == [vn]
        # the print lands: record_issued reconciles -> recorded ONCE, pending
        # cleared, system ID folded onto the ticket.
        out = store.record_issued(vn, AMOUNT, 1, source="egm_4D")
        assert out["duplicate"] is False
        assert store.pending_validations() == []
        rec = store.get(vn)
        assert rec["state"] == ISSUED
        assert rec["systemId"] == DEFAULT_SYSTEM_ID
        assert len(store.outstanding()) == 1

    def test_mint_rejects_bad_system_id_and_amount(self, tmp_path):
        store = _store(tmp_path)
        for bad_id in (0, 100, -1):
            try:
                store.mint_validation_number(AMOUNT, 1, system_id=bad_id)
            except ValueError:
                pass
            else:
                raise AssertionError(f"system_id {bad_id} must raise")
        for bad_amt in (0, -1, None):
            try:
                store.mint_validation_number(bad_amt, 1)
            except ValueError:
                pass
            else:
                raise AssertionError(f"amount {bad_amt!r} must raise")

    def test_mint_rejects_amount_over_ceiling(self, tmp_path):
        # Finding #2 defense-in-depth: no code path mints an implausible amount.
        store = _store(tmp_path)
        try:
            store.mint_validation_number(store.MAX_TICKET_CENTS + 1, 1)
        except ValueError:
            pass
        else:
            raise AssertionError("over-ceiling amount must raise")
        assert store.pending_validations() == []
        # the ceiling amount itself is fine (boundary is inclusive)
        ok = store.mint_validation_number(store.MAX_TICKET_CENTS, 1)
        assert ok["amount_cents"] == store.MAX_TICKET_CENTS

    def test_mint_rolls_back_when_persist_fails(self, tmp_path):
        # Finding #6: a validation number we cannot DURABLY persist must never
        # be handed out — reissuing it after a restart would put two physical
        # tickets under one vn (double-redemption hole). The mint must roll the
        # sequence back and raise ValidationMintError, not swallow the failure.
        store = _store(tmp_path)
        good = store.mint_validation_number(AMOUNT, 1)          # seq persisted
        store._save_locked = lambda: False                     # next save fails
        try:
            store.mint_validation_number(AMOUNT, 1)
        except ValidationMintError:
            pass
        else:
            raise AssertionError("persist failure must raise ValidationMintError")
        # rolled back: seq unchanged, no orphan pending for the failed mint
        assert store.state["validationSeq"] == good["seq"]
        assert [p["validationNumber"] for p in store.pending_validations()] \
            == [good["validation_number"]]
        # the failed seq was NOT consumed — monotonic guarantee is preserved
        del store._save_locked                                  # restore method
        nxt = store.mint_validation_number(AMOUNT, 1)
        assert nxt["seq"] == good["seq"] + 1

    def test_find_open_pending_matches_address_and_amount(self, tmp_path):
        store = _store(tmp_path)
        a = store.mint_validation_number(AMOUNT, 1)["validation_number"]
        store.mint_validation_number(AMOUNT + 1, 1)            # diff amount
        store.mint_validation_number(AMOUNT, 2)               # diff address
        hit = store.find_open_pending(1, AMOUNT)
        assert hit is not None and hit["validationNumber"] == a
        assert store.find_open_pending(9, AMOUNT) is None

    def test_record_from_wrong_address_does_not_fold_systemid(self, tmp_path):
        # Finding #8: a ticket minted for addr A but printed from addr B must
        # NOT be silently attributed to A — flag the mismatch, do not fold.
        store = _store(tmp_path)
        vn = store.mint_validation_number(AMOUNT, address=1, system_id=5)[
            "validation_number"]
        out = store.record_issued(vn, AMOUNT, address=2, source="egm_4D")
        assert out["duplicate"] is False
        rec = store.get(vn)
        assert "systemId" not in rec
        assert rec["systemIdMismatch"]["mintedAddress"] == 1
        assert rec["systemIdMismatch"]["printedAddress"] == 2
        assert store.pending_validations() == []

    def test_duplicate_record_clears_lingering_pending(self, tmp_path):
        # Finding #13: the reconcile pop in record_issued's DUPLICATE branch.
        store = _store(tmp_path)
        vn = store.mint_validation_number(AMOUNT, 1)["validation_number"]
        store.record_issued(vn, AMOUNT, 1)                  # records, clears pend
        # simulate a lingering pending marker for an already-recorded vn
        store.state["pendingValidations"][vn] = {"validationNumber": vn,
                                                 "seq": 99}
        out = store.record_issued(vn, AMOUNT, 1)            # dup branch pops it
        assert out["duplicate"] is True
        assert store.pending_validations() == []
        assert len(store.outstanding()) == 1


# --------------------------------------------------------------------------
# service_system_validation
# --------------------------------------------------------------------------

class TestServiceSystemValidation:
    def test_approve_bcd_number_mints_and_sends_id_01(self, tmp_path):
        machine = SysValMachine(cashout={"type": 0, "amount": AMOUNT})
        port = MockSASSerialPort(machine)
        store = _store(tmp_path)
        result = service_system_validation(port, 1, store, sleep=NO_SLEEP)

        assert result.outcome is SystemValidationOutcome.SERVICED
        assert result.ok
        assert result.amount_cents == AMOUNT
        assert result.validation_id == VALIDATION_APPROVE
        assert result.payload_style == PAYLOAD_STYLE_BCD
        assert result.validation_number.startswith("01")
        assert result.ack_confirmed is True
        # exactly one 0x58 on the wire, ID=01, payload == minted vn as BCD
        fifty_eights = _polls_of(port, RECEIVE_VALIDATION_NUMBER)
        assert len(fifty_eights) == 1
        sent = fifty_eights[0]
        assert sent[2] == VALIDATION_APPROVE
        assert sent[3:11].hex() == result.validation_number
        # the mint is tracked as pending until the print reconciles it
        assert [p["validationNumber"] for p in store.pending_validations()] \
            == [result.validation_number]

    def test_approve_type_amount_echoes_amount(self, tmp_path):
        machine = SysValMachine(cashout={"type": 3, "amount": AMOUNT})
        port = MockSASSerialPort(machine)
        result = service_system_validation(
            port, 1, _store(tmp_path), sleep=NO_SLEEP,
            payload_style=PAYLOAD_STYLE_TYPE_AMOUNT)
        assert result.outcome is SystemValidationOutcome.SERVICED
        assert result.payload_style == PAYLOAD_STYLE_TYPE_AMOUNT
        sent = _polls_of(port, RECEIVE_VALIDATION_NUMBER)[0]
        assert sent[2] == VALIDATION_APPROVE
        assert sent[3:11] == bytes([3, 0, 0]) + int_to_bcd(AMOUNT, 5)

    def test_reject_sends_id_00_and_does_not_mint(self, tmp_path):
        machine = SysValMachine(cashout={"type": 0, "amount": AMOUNT})
        port = MockSASSerialPort(machine)
        store = _store(tmp_path)
        result = service_system_validation(port, 1, store, sleep=NO_SLEEP,
                                           approve=False)
        assert result.outcome is SystemValidationOutcome.REJECTED
        assert not result.ok
        sent = _polls_of(port, RECEIVE_VALIDATION_NUMBER)
        assert len(sent) == 1 and sent[0][2] == VALIDATION_REJECT
        assert store.pending_validations() == []      # no number minted

    def test_silence_is_timeout_and_never_approves(self, tmp_path):
        machine = SysValMachine(cashout={"type": 0, "amount": AMOUNT},
                                answer_57=False)
        port = MockSASSerialPort(machine)
        store = _store(tmp_path)
        result = service_system_validation(port, 1, store, sleep=NO_SLEEP)
        assert result.outcome is SystemValidationOutcome.TIMEOUT
        # SILENCE != CONSENT: no 0x58 on the wire, nothing minted
        assert _polls_of(port, RECEIVE_VALIDATION_NUMBER) == []
        assert store.pending_validations() == []

    def test_bad_crc_on_57_read_is_timeout_no_approve(self, tmp_path):
        inner = SysValMachine(cashout={"type": 0, "amount": AMOUNT})

        def corruptor(frame, wakeup):
            resp = inner(frame, wakeup)
            if len(frame) == 2 and frame[1] == SEND_PENDING_CASHOUT and resp:
                return resp[:-2] + b"\x00\x00"        # break the CRC
            return resp

        port = MockSASSerialPort(corruptor)
        store = _store(tmp_path)
        result = service_system_validation(port, 1, store, sleep=NO_SLEEP)
        assert result.outcome is SystemValidationOutcome.TIMEOUT
        assert _polls_of(port, RECEIVE_VALIDATION_NUMBER) == []
        assert store.pending_validations() == []

    def test_zero_amount_is_no_data_no_approve(self, tmp_path):
        machine = SysValMachine(cashout=None)      # answers, but amount 0
        port = MockSASSerialPort(machine)
        store = _store(tmp_path)
        result = service_system_validation(port, 1, store, sleep=NO_SLEEP)
        assert result.outcome is SystemValidationOutcome.NO_DATA
        assert _polls_of(port, RECEIVE_VALIDATION_NUMBER) == []
        assert store.pending_validations() == []

    def test_wrong_length_valid_crc_57_is_timeout_no_approve(self, tmp_path):
        # Finding #11a: a well-framed (valid-CRC) but wrong-length 0x57 must
        # NOT approve — the parser raises, the responder treats it as silence.
        def responder(frame, wakeup):
            if not wakeup[0]:
                return b""
            if len(frame) == 2 and frame[1] == SEND_PENDING_CASHOUT:
                return _frame(1, SEND_PENDING_CASHOUT, bytes([0x00, 0x00]))
            return b""
        port = MockSASSerialPort(responder)
        store = _store(tmp_path)
        result = service_system_validation(port, 1, store, sleep=NO_SLEEP)
        assert result.outcome is SystemValidationOutcome.TIMEOUT
        assert _polls_of(port, RECEIVE_VALIDATION_NUMBER) == []
        assert store.pending_validations() == []

    def test_non_bcd_amount_valid_crc_57_is_no_data_no_approve(self, tmp_path):
        # Finding #11b: a valid-CRC 0x57 carrying a non-BCD amount nibble ->
        # amount_cents is None -> NO_DATA, never an approve.
        def responder(frame, wakeup):
            if not wakeup[0]:
                return b""
            if len(frame) == 2 and frame[1] == SEND_PENDING_CASHOUT:
                return _frame(1, SEND_PENDING_CASHOUT,
                              bytes([0x00, 0xAB, 0x00, 0x00, 0x00, 0x00]))
            return b""
        port = MockSASSerialPort(responder)
        store = _store(tmp_path)
        result = service_system_validation(port, 1, store, sleep=NO_SLEEP)
        assert result.outcome is SystemValidationOutcome.NO_DATA
        assert _polls_of(port, RECEIVE_VALIDATION_NUMBER) == []
        assert store.pending_validations() == []

    def test_approve_no_ack_stays_serviced_and_keeps_pending(self, tmp_path):
        # Finding #12: approve sent but 0x58 ack dropped -> still SERVICED,
        # ack_confirmed False, and the minted number is RETAINED as pending so
        # the later 0x3D print reconciles to exactly one ticket.
        machine = SysValMachine(cashout={"type": 0, "amount": AMOUNT},
                                answer_58=False)
        port = MockSASSerialPort(machine)
        store = _store(tmp_path)
        result = service_system_validation(port, 1, store, sleep=NO_SLEEP)
        assert result.outcome is SystemValidationOutcome.SERVICED
        assert result.ack_confirmed is False
        assert len(_polls_of(port, RECEIVE_VALIDATION_NUMBER)) == 1
        assert [p["validationNumber"] for p in store.pending_validations()] \
            == [result.validation_number]

    def test_reject_no_ack_is_rejected_and_does_not_mint(self, tmp_path):
        # Finding #12 mirror: reject with no ack -> REJECTED, nothing minted.
        machine = SysValMachine(cashout={"type": 0, "amount": AMOUNT},
                                answer_58=False)
        port = MockSASSerialPort(machine)
        store = _store(tmp_path)
        result = service_system_validation(port, 1, store, sleep=NO_SLEEP,
                                           approve=False)
        assert result.outcome is SystemValidationOutcome.REJECTED
        assert result.ack_confirmed is False
        assert len(_polls_of(port, RECEIVE_VALIDATION_NUMBER)) == 1
        assert store.pending_validations() == []

    def test_ceiling_is_the_wire_max_so_no_valid_frame_can_exceed_it(self):
        # Finding #2, re-founded by the collector de-cage (2026-07-15): the
        # ceiling IS the 5-byte-BCD wire max, so the old hostile class — an
        # "implausible but BCD-valid" over-ceiling amount — is now EMPTY by
        # construction: one cent more cannot be encoded in the 0x57 reply at
        # all (int_to_bcd refuses it, as would any real EGM's frame builder).
        # The mint/approve edges still enforce the same bound symbolically
        # (test_mint_rejects_amount_over_ceiling); a CORRUPTED frame is the
        # remaining hostile input and is covered by the CRC/parse tests.
        assert TicketStore.MAX_TICKET_CENTS == 9_999_999_999  # 5-byte BCD max
        int_to_bcd(TicketStore.MAX_TICKET_CENTS, 5)           # encodes exactly
        try:
            int_to_bcd(TicketStore.MAX_TICKET_CENTS + 1, 5)
            raise AssertionError("one cent past the ceiling must not encode")
        except ValueError:
            pass                                              # physics says no

    def test_mint_persist_failure_sends_no_approve(self, tmp_path):
        # Finding #6 at the responder: a mint that cannot persist fails SAFE —
        # TIMEOUT, no 0x58 approve on the wire, no pending left behind.
        machine = SysValMachine(cashout={"type": 0, "amount": AMOUNT})
        port = MockSASSerialPort(machine)
        store = _store(tmp_path)
        store._save_locked = lambda: False
        result = service_system_validation(port, 1, store, sleep=NO_SLEEP)
        assert result.outcome is SystemValidationOutcome.TIMEOUT
        assert _polls_of(port, RECEIVE_VALIDATION_NUMBER) == []
        assert store.pending_validations() == []

    def test_reraised_0x57_reuses_same_number(self, tmp_path):
        # Findings #3/#7: idempotent per physical cash-out. A re-raised 0x57
        # (prior 0x58/ack lost) re-sends the SAME number, not a fresh mint.
        machine = SysValMachine(cashout={"type": 0, "amount": AMOUNT},
                                answer_58=False)
        port = MockSASSerialPort(machine)
        store = _store(tmp_path)
        r1 = service_system_validation(port, 1, store, sleep=NO_SLEEP)
        r2 = service_system_validation(port, 1, store, sleep=NO_SLEEP)
        assert r1.validation_number == r2.validation_number
        # only ONE number minted for the one cash-out
        assert [p["validationNumber"] for p in store.pending_validations()] \
            == [r1.validation_number]
        sent = _polls_of(port, RECEIVE_VALIDATION_NUMBER)
        assert len(sent) == 2 and sent[0][3:11] == sent[1][3:11]

    def test_type_amount_does_not_mint_no_orphan_pending(self, tmp_path):
        # Finding #10: in the type_amount regime the EGM supplies its own
        # number, so the host does NOT mint (no permanent orphan pending).
        machine = SysValMachine(cashout={"type": 2, "amount": AMOUNT})
        port = MockSASSerialPort(machine)
        store = _store(tmp_path)
        result = service_system_validation(
            port, 1, store, sleep=NO_SLEEP,
            payload_style=PAYLOAD_STYLE_TYPE_AMOUNT)
        assert result.outcome is SystemValidationOutcome.SERVICED
        assert result.validation_number is None
        assert store.pending_validations() == []


# --------------------------------------------------------------------------
# Poller integration + the 0x57 -> 0x58 -> 0x3D reconcile
# --------------------------------------------------------------------------

class TestPollerIntegration:
    def test_0x57_exception_is_named(self):
        info = lookup(SYSTEM_VALIDATION_REQUEST_EXCEPTION)
        assert info.event == "system_validation_request"
        assert info.category is ExceptionCategory.TICKET

    def test_exception_triggers_service(self, tmp_path):
        machine = SysValMachine(cashout={"type": 0, "amount": AMOUNT},
                                print_on_approve=False)
        machine.queue(SYSTEM_VALIDATION_REQUEST_EXCEPTION)
        port = MockSASSerialPort(machine)
        store = _store(tmp_path)
        serviced, seen = [], []
        poller = SASPoller(port, address=1,
                           on_typed_event=lambda a, i: seen.append(i.code))
        SASTITOHost(poller, store, sleep=NO_SLEEP,
                    on_system_validation=lambda a, r: serviced.append((a, r)))
        poller.poll_once()

        assert len(serviced) == 1
        addr, result = serviced[0]
        assert addr == 1 and result.ok
        assert machine.approvals and machine.approvals[0][0] == VALIDATION_APPROVE
        assert SYSTEM_VALIDATION_REQUEST_EXCEPTION in seen   # chained hook saw it

    def test_service_then_print_records_ticket_once(self, tmp_path):
        """End to end: 0x57 -> mint -> 0x58 approve -> the EGM prints and
        raises 0x3D -> capture reconciles the minted pending into exactly one
        recorded ticket (not duplicated)."""
        machine = SysValMachine(cashout={"type": 0, "amount": AMOUNT})
        machine.queue(SYSTEM_VALIDATION_REQUEST_EXCEPTION)
        port = MockSASSerialPort(machine)
        store = _store(tmp_path)
        issued, serviced = [], []
        poller = SASPoller(port, address=1)
        SASTITOHost(poller, store, sleep=NO_SLEEP,
                    on_ticket_issued=lambda a, r: issued.append(r),
                    on_system_validation=lambda a, r: serviced.append(r))
        poller.poll_once()          # services 0x57, drains the queued 0x3D

        assert serviced and serviced[0].ok
        vn = serviced[0].validation_number
        assert issued and issued[0].ok
        # recorded exactly once, pending reconciled away
        assert len(store.outstanding()) == 1
        assert store.get(vn)["state"] == ISSUED
        assert store.get(vn)["amountCents"] == AMOUNT
        assert store.pending_validations() == []

    def test_auto_service_can_be_disabled(self, tmp_path):
        machine = SysValMachine(cashout={"type": 0, "amount": AMOUNT},
                                print_on_approve=False)
        machine.queue(SYSTEM_VALIDATION_REQUEST_EXCEPTION)
        port = MockSASSerialPort(machine)
        store = _store(tmp_path)
        serviced = []
        poller = SASPoller(port, address=1)
        tito = SASTITOHost(poller, store, auto_service=False, sleep=NO_SLEEP,
                           on_system_validation=lambda a, r: serviced.append(r))
        poller.poll_once()
        assert serviced == []                       # nothing automatic
        assert _polls_of(port, RECEIVE_VALIDATION_NUMBER) == []
        result = tito.service_validation()          # manual trigger works
        assert result.outcome is SystemValidationOutcome.SERVICED

    def test_service_callback_exception_is_isolated(self, tmp_path):
        machine = SysValMachine(cashout={"type": 0, "amount": AMOUNT},
                                print_on_approve=False)
        machine.queue(SYSTEM_VALIDATION_REQUEST_EXCEPTION)
        port = MockSASSerialPort(machine)

        def boom(*a):
            raise RuntimeError("bad bridge callback")

        poller = SASPoller(port, address=1)
        SASTITOHost(poller, _store(tmp_path), sleep=NO_SLEEP,
                    on_system_validation=boom)
        poller.poll_once()                          # must not raise
        assert poller.state.callback_errors >= 1
