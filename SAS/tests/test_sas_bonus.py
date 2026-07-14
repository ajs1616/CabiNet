"""
Tests for core/sas_bonus.py (legacy bonus 0x8A).

Golden vector: the exact frame that paid the first-ever host->machine money
on the bench BB2 (2026-07-08) — 10 credits, tax 00:
    01 8A 00 00 00 10 00 E8 26
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from core.sas_bonus import (
    MAX_BONUS_CREDITS, TAX_DEDUCTIBLE, TAX_NON_DEDUCTIBLE,
    build_legacy_bonus_poll, classify_bonus_reply,
)


class TestBuildFrame:
    def test_golden_live_frame(self):
        """Byte-for-byte the frame the real BB2 ACKed and paid."""
        assert build_legacy_bonus_poll(1, 10) == bytes.fromhex(
            "018a000000100" "0e826".replace(" ", ""))

    def test_amount_is_bcd_msb_first(self):
        frame = build_legacy_bonus_poll(1, 1234)
        assert frame[2:6] == bytes.fromhex("00001234")   # BCD, MSB first

    def test_tax_status_byte(self):
        frame = build_legacy_bonus_poll(1, 5, TAX_NON_DEDUCTIBLE)
        assert frame[6] == 0x01

    def test_crc_is_kermit_lsb_first(self):
        from core.sas_protocol import sas_crc
        frame = build_legacy_bonus_poll(3, 250)
        assert frame[-2:] == sas_crc(frame[:-2]).to_bytes(2, "little")

    @pytest.mark.parametrize("credits", [0, -1, MAX_BONUS_CREDITS + 1,
                                         10 ** 9, "10", 1.5, True, None])
    def test_hostile_amounts_refused(self, credits):
        with pytest.raises((ValueError, TypeError)):
            build_legacy_bonus_poll(1, credits)

    @pytest.mark.parametrize("address", [0, 0x80, 255, -1])
    def test_bad_address_refused(self, address):
        with pytest.raises(ValueError):
            build_legacy_bonus_poll(address, 10)

    def test_unknown_tax_status_refused(self):
        with pytest.raises(ValueError):
            build_legacy_bonus_poll(1, 10, 0x42)


class TestClassifyReply:
    def test_ack_is_bare_address(self):
        assert classify_bonus_reply(bytes([0x01]), 1) == "ack"

    def test_nack_is_address_or_80(self):
        assert classify_bonus_reply(bytes([0x81]), 1) == "nack"

    def test_busy_per_spec_4_1(self):
        assert classify_bonus_reply(bytes([0x01, 0x00]), 1) == "busy"

    def test_silence(self):
        assert classify_bonus_reply(b"", 1) == "silence"

    def test_garbage_is_unexpected(self):
        assert classify_bonus_reply(bytes([0x1F]), 1) == "unexpected"
