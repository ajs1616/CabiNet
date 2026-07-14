"""
Unit tests for SAS protocol parsing and validation
Tests packet construction, parsing, CRC validation, and error handling

CRC fixtures: the supplemental guide PDF (Montana DOJ v1.5.0) shows all CRCs
as 'XX XX', so frame-level fixtures cannot come from it. The CRC algorithm is
anchored instead on the canonical CRC-16/KERMIT catalog check value, plus
structural assertions (LSB-first wire order, no sync byte, no-CRC short
polls). Final adjudication is a live capture from a real WMS machine on the
bench — record it in COMPATIBILITY.md when done.
"""

import pytest
from decimal import Decimal
import sys
from pathlib import Path

# Add parent directory to path
sys.path.insert(0, str(Path(__file__).parent.parent))

from core.sas_protocol import (
    SASProtocol, SASPacket, SASCommand, MeterGroup, sas_crc,
    GENERAL_POLL_FLAG, SAS_BAUD, SAS_RESPONSE_TIMEOUT_MS,
)


def igt_reference_crc(data: bytes, crcval: int = 0) -> int:
    """The IGT SAS spec's published reference CRC routine (the nibble-wise C
    algorithm with constant 010201 octal / 0x1081), transliterated. Used as an
    independent oracle: our crcmod-based sas_crc must match it exactly."""
    for c in data:
        q = (crcval ^ c) & 0o17
        crcval = ((crcval >> 4) ^ (q * 0o10201)) & 0xFFFF
        q = (crcval ^ (c >> 4)) & 0o17
        crcval = ((crcval >> 4) ^ (q * 0o10201)) & 0xFFFF
    return crcval


class TestSASCRC:
    """The CRC algorithm itself — CRC-16/Kermit, LSB first on the wire."""

    def test_kermit_check_value(self):
        """Canonical catalog check value for CRC-16/KERMIT."""
        assert sas_crc(b"123456789") == 0x2189

    def test_matches_igt_spec_reference_algorithm(self):
        """Our CRC must equal the IGT SAS spec's own published C routine for
        every input — the strongest pre-hardware anchor we have (verified
        5000-input random sweep on 2026-06-12; deterministic subset here)."""
        import random
        rng = random.Random(1616)
        for _ in range(500):
            data = bytes(rng.randrange(256)
                         for _ in range(rng.randrange(1, 40)))
            assert sas_crc(data) == igt_reference_crc(data), data.hex()
        assert igt_reference_crc(b"123456789") == 0x2189

    def test_not_xmodem(self):
        """Guard against regressing to the XMODEM variant (the old bug):
        CRC-16/XMODEM of '123456789' is 0x31C3 — we must NOT produce that."""
        assert sas_crc(b"123456789") != 0x31C3

    def test_crc_lsb_first_on_wire(self):
        """The 16-bit CRC is appended low byte first."""
        protocol = SASProtocol()
        frame = protocol.build_packet(0x01, 0x21, b"\x00\x00")
        crc = sas_crc(b"\x01\x21\x00\x00")
        assert frame[-2] == (crc & 0xFF)        # low byte first
        assert frame[-1] == ((crc >> 8) & 0xFF)  # high byte second

    def test_crc_covers_address_through_data(self):
        """CRC covers address + command + data, excludes the CRC bytes."""
        protocol = SASProtocol()
        frame = protocol.build_packet(0x05, 0x52, b"\xAA\xBB")
        assert sas_crc(frame[:-2]) == int.from_bytes(frame[-2:], "little")


class TestSASProtocol:
    """Test SAS protocol implementation"""

    def setup_method(self):
        """Setup test instance"""
        self.protocol = SASProtocol()

    def test_no_sync_byte(self):
        """SAS frames start with the address byte — there is NO sync byte.
        (The old fictitious 0x01 sync byte corrupted frames from machines at
        address 1, since their address byte IS 0x01.)"""
        frame = self.protocol.build_packet(0x01, 0x21, b"\x00\x00")
        assert frame[0] == 0x01  # the ADDRESS byte, nothing before it
        assert frame[1] == 0x21

    def test_general_poll_single_byte(self):
        """General poll = single byte 0x80|address, no command, no CRC."""
        assert self.protocol.build_general_poll(0x01) == bytes([0x81])
        assert self.protocol.build_general_poll(0x7F) == bytes([0xFF])
        assert self.protocol.build_poll(0x01) == bytes([0x81])

    def test_short_poll_has_no_crc(self):
        """Two-byte polls (e.g. 01 1A current credits) carry no CRC."""
        frame = self.protocol.build_short_poll(0x01, SASCommand.CURRENT_CREDITS)
        assert frame == b"\x01\x1A"
        assert self.protocol.build_meter_request(0x01, 0x1A) == b"\x01\x1A"

    def test_command_builder_crc_by_poll_type(self):
        """Type R read polls carry NO CRC (2 bytes); type S control polls
        carry a CRC even with no data (4 bytes)."""
        from core.sas_protocol import SASCommandBuilder
        b = SASCommandBuilder(self.protocol)
        # type R: exactly addr+cmd
        assert b.request_current_credits(1) == b"\x01\x1a"
        assert b.request_machine_id(1) == bytes([0x01, SASCommand.GAMING_MACHINE_ID])
        # type S: addr+cmd+CRC (4 bytes), CRC valid
        sd = b.shutdown(1)
        assert len(sd) == 4 and sas_crc(sd[:-2]) == int.from_bytes(sd[-2:], "little")

    def test_parse_valid_packet(self):
        """Round-trip a CRC-bearing frame."""
        packet_bytes = self.protocol.build_packet(
            0x01, SASCommand.ROM_SIGNATURE, b"\x12\x34")

        parsed = self.protocol.parse_packet(packet_bytes)

        assert parsed is not None
        assert parsed.address == 0x01
        assert parsed.command == SASCommand.ROM_SIGNATURE
        assert parsed.data == b"\x12\x34"
        assert parsed.is_valid()

    def test_parse_address_one_frame_intact(self):
        """Frames from address 1 must NOT lose their first byte
        (regression test for the sync-byte-stripping bug)."""
        frame = self.protocol.build_packet(0x01, 0x1A, b"\x00\x00\x12\x34")
        parsed = self.protocol.parse_packet(frame)
        assert parsed is not None
        assert parsed.address == 0x01
        assert parsed.command == 0x1A
        assert parsed.data == b"\x00\x00\x12\x34"

    def test_parse_invalid_crc(self):
        """Test parsing packet with invalid CRC"""
        packet_bytes = bytearray(
            self.protocol.build_packet(0x01, 0x21, b"\x00\x00"))
        packet_bytes[-1] ^= 0xFF  # Corrupt CRC high byte

        parsed = self.protocol.parse_packet(bytes(packet_bytes))
        assert parsed is None

    def test_parse_short_packet(self):
        """Frames below addr+cmd+CRC length are rejected by parse_packet."""
        assert self.protocol.parse_packet(b"\x01\x01\x00") is None

    def test_parse_response_is_context_driven(self):
        """A lone byte is contextual, NOT value-classified. After a general
        poll it is an exception code (0x00 = idle); a low code like 0x12
        (door closed) must NOT be mislabeled an ACK."""
        assert self.protocol.parse_response(b"\x00") == {
            'type': 'exception', 'value': 0x00, 'idle': True}
        assert self.protocol.parse_response(b"\x12") == {
            'type': 'exception', 'value': 0x12, 'idle': False}
        assert self.protocol.parse_response(b"\x8c") == {
            'type': 'exception', 'value': 0x8C, 'idle': False}

    def test_parse_response_transfer_ack_nack(self):
        """After a host->machine transfer poll: lone bare address = ACK,
        address|0x80 = NACK."""
        ack = self.protocol.parse_response(b"\x01", last_poll='transfer',
                                           address=1)
        assert ack['type'] == 'ack'
        nack = self.protocol.parse_response(b"\x81", last_poll='transfer',
                                            address=1)
        assert nack['type'] == 'nack'

    def test_parse_response_busy(self):
        """[addr][0x00] (2 bytes, no CRC) is the machine-busy response."""
        resp = self.protocol.parse_response(b"\x01\x00")
        assert resp['type'] == 'busy' and resp['address'] == 0x01

    def test_parse_response_crc_error_only_for_real_frames(self):
        """A 4+ byte frame with a bad CRC is the ONLY crc_error case — short
        runts are 'short', not CRC evidence (avoids fabricated CRC-BAD)."""
        bad = bytearray(self.protocol.build_packet(0x01, 0x1A, b"\x00\x00\x12\x34"))
        bad[-1] ^= 0xFF
        assert self.protocol.parse_response(bytes(bad))['type'] == 'crc_error'
        assert self.protocol.parse_response(b"\x01\x1a\x00")['type'] == 'short'

    def test_parse_response_full_packet(self):
        frame = self.protocol.build_packet(0x01, 0x1A, b"\x00\x00\x12\x34")
        resp = self.protocol.parse_response(frame)
        assert resp is not None
        assert resp['type'] == 'packet'
        assert resp['packet'].command == 0x1A

    def test_bcd_conversion(self):
        """Test BCD conversion functions"""
        bcd = self.protocol._int_to_bcd(1234, 4)
        assert bcd == b'\x00\x00\x12\x34'

        value = self.protocol._bcd_to_int(b'\x00\x00\x12\x34')
        assert value == 1234

    def test_invalid_bcd_bytes(self):
        """Test BCD conversion with invalid bytes"""
        invalid_bcd = b'\x00\x00\xA5\x34'  # A is invalid in BCD
        result = self.protocol._bcd_to_int(invalid_bcd)
        assert result is None

    def test_broadcast_address(self):
        """Broadcast general poll (address 0)."""
        assert self.protocol.build_general_poll(0x00) == bytes([0x80])

    def test_max_address(self):
        """Test maximum valid address (127)"""
        frame = self.protocol.build_packet(0x7F, 0x21, b"\x00\x00")
        parsed = self.protocol.parse_packet(frame)
        assert parsed is not None
        assert parsed.address == 0x7F

    def test_packet_with_data(self):
        """Test packet with data payload"""
        test_data = b'\x01\x02\x03\x04'
        packet = self.protocol.build_packet(0x01, 0x50, test_data)

        parsed = self.protocol.parse_packet(packet)
        assert parsed is not None
        assert parsed.data == test_data
        assert parsed.is_valid()

    def test_link_constants(self):
        """Hard numbers from the implementation guide (p.6)."""
        assert SAS_BAUD == 19200
        assert SAS_RESPONSE_TIMEOUT_MS == 20
        assert GENERAL_POLL_FLAG == 0x80


class TestSASPacket:
    """Test SASPacket class"""

    def test_packet_to_dict(self):
        """Test packet dictionary conversion"""
        packet = SASPacket(
            address=0x01,
            command=SASCommand.POLL,
            data=b'\x01\x02',
            crc=0x1234,
            raw=b'\x01\x00\x01\x02\x34\x12'
        )

        packet_dict = packet.to_dict()

        assert packet_dict['address'] == '01'
        assert packet_dict['command'] == '00'
        assert packet_dict['command_name'] == 'POLL'
        assert packet_dict['data'] == '0102'
        assert packet_dict['crc'] == '1234'
        assert 'valid' in packet_dict
        assert 'raw' in packet_dict

    def test_unknown_command_name(self):
        """Test packet with unknown command"""
        packet = SASPacket(
            address=0x01,
            command=0xFF,  # Unknown command
            data=b'',
            crc=0x0000,
            raw=b'\x01\xFF\x00\x00'
        )

        packet_dict = packet.to_dict()
        assert packet_dict['command_name'] == 'UNKNOWN'


class TestMeterGroup:
    """Meter codes verified against Montana DOJ SAS Guide v1.5.0 §4.4."""

    def test_guide_cited_meter_codes(self):
        # the contested low codes the review flagged as shifted
        assert MeterGroup.get_meter_name(0x11) == "Coin In"        # §4.4.3
        assert MeterGroup.get_meter_name(0x12) == "Coin Out"       # §4.4.4
        assert MeterGroup.get_meter_name(0x13) == "Total Drop"     # §4.4.5
        assert MeterGroup.get_meter_name(0x15) == "Games Played"   # §4.4.6
        assert MeterGroup.get_meter_name(0x16) == "Games Won"      # §4.4.7
        assert MeterGroup.get_meter_name(0x1A) == "Current Credits"  # §4.4.11
        assert MeterGroup.get_meter_name(0x31) == "$1 Bills In"    # §4.4.15
        assert MeterGroup.get_meter_name(0xFF) == "Unknown Meter FF"

    def test_command_codes_guide_cited(self):
        # the systematically-wrong enum values from the review
        assert SASCommand.COIN_IN == 0x11
        assert SASCommand.COIN_OUT == 0x12
        assert SASCommand.TOTAL_DROP == 0x13
        assert SASCommand.CURRENT_CREDITS == 0x1A
        assert SASCommand.GAMING_MACHINE_ID == 0x1F   # was wrongly 0x4F
        assert SASCommand.SEND_METERS_10_15 == 0x0F


if __name__ == '__main__':
    pytest.main([__file__, '-v'])
