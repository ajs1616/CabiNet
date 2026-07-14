"""
Robustness tests — malformed input, tampering, and money-safety boundaries
across the rebuilt SAS stack.

Rewritten 2026-07-07. The legacy version of this file never ran green in
this venv: it imported the import-broken modules.aft (3-dot relative
imports), yaml/sqlalchemy config code that contradicts the settled
lightweight stack, and asserted a 256-byte frame "including sync" — the
0x01 sync byte is fictitious (removed 2026-06-12), so the max frame with
251 data bytes is 255: [addr][cmd][251 data][crc-lo][crc-hi].

("Security" here is robustness for an offline hobbyist LAN — malformed
frames, corrupt tickets, double-spends — NOT auth hardening, which is
explicitly out of scope for CasinoNet.)
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from core.sas_protocol import SASProtocol
from core.sas_ticket_store import TicketStore
from modules.aft.aft_handler import (
    AFTTransferRequest, make_transaction_id,
)
# (modules.player_accounts was attic'd 2026-07-15 with the casino-cage
# removal — its PIN/overdraw tests went with it.)


class TestFramingRobustness:
    def test_max_frame_is_255_no_sync_byte(self):
        """251 data bytes -> 1 addr + 1 cmd + 251 + 2 CRC = 255 bytes.
        (The legacy assertion of 256 'including sync' was the never-green
        sync-byte lie.)"""
        protocol = SASProtocol()
        packet = protocol.build_packet(0x01, 0x50, b"A" * 251)
        assert len(packet) == 255
        assert packet[0] == 0x01                  # ADDRESS, not a sync byte

    def test_oversized_garbage_parses_to_none_not_crash(self):
        protocol = SASProtocol()
        assert protocol.parse_packet(b"\x01" * 1000) is None

    def test_bcd_invalid_nibbles_return_none(self):
        protocol = SASProtocol()
        assert protocol._bcd_to_int(b"\x12\x34\x56") == 123456
        for invalid in (b"\xA0\x00\x00", b"\x0F\x00\x00", b"\x00\xBC\x00"):
            assert protocol._bcd_to_int(invalid) is None

    def test_crc_tampering_rejected(self):
        protocol = SASProtocol()
        original = protocol.build_packet(0x01, 0x1A, b"\x00\x00\x10\x00")
        for i, mask in ((0, 0x02), (1, 0x01), (2, 0xFF)):
            tampered = bytearray(original)
            tampered[i] ^= mask
            assert protocol.parse_packet(bytes(tampered)) is None

    def test_short_fragments_never_crash(self):
        protocol = SASProtocol()
        for fragment in (b"", b"\x01", b"\x01\x70", b"\x01\x70\x00"):
            assert protocol.parse_packet(fragment) is None


class TestAFTMoneySafety:
    def test_negative_amounts_rejected(self):
        with pytest.raises(ValueError, match="non-negative"):
            AFTTransferRequest(transaction_id="T1", cashable_cents=-5000)

    def test_zero_total_rejected(self):
        with pytest.raises(ValueError, match="positive"):
            AFTTransferRequest(transaction_id="T1", cashable_cents=0)

    def test_transaction_id_wire_limits(self):
        with pytest.raises(ValueError):
            AFTTransferRequest(transaction_id="X" * 21, cashable_cents=100)
        with pytest.raises(ValueError):
            AFTTransferRequest(transaction_id="ключ", cashable_cents=100)

    def test_transaction_ids_unique_under_burst(self):
        ids = {make_transaction_id() for _ in range(500)}
        assert len(ids) == 500


class TestTicketMoneySafety:
    def test_double_redemption_blocked(self, tmp_path):
        store = TicketStore(str(tmp_path / "t.json"))
        store.record_issued("1234567890123456", 5000, 1)
        assert store.authorize_redemption(1, "1234567890123456")["authorized"]
        # a second machine while in-flight
        d = store.authorize_redemption(2, "1234567890123456")
        assert not d["authorized"]
        # and after consumption
        store.close_redemption(1, "1234567890123456", redeemed=True)
        d = store.authorize_redemption(2, "1234567890123456")
        assert not d["authorized"]

    def test_fabricated_validation_number_rejected(self, tmp_path):
        store = TicketStore(str(tmp_path / "t.json"))
        d = store.authorize_redemption(1, "9999888877776666")
        assert not d["authorized"] and d["amount_cents"] == 0

    def test_machine_reported_amount_cannot_inflate_payout(self, tmp_path):
        store = TicketStore(str(tmp_path / "t.json"))
        store.record_issued("1234567890123456", 100, 1)   # a $1.00 ticket
        d = store.authorize_redemption(1, "1234567890123456",
                                       reported_amount_cents=1_000_000)
        assert d["authorized"]
        assert d["amount_cents"] == 100          # issued amount is the law


