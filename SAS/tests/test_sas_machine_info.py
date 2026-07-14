"""
Tests for machine identity (core/sas_machine_info.py) — the 0x1F/0x54/0x7B/
0x51/0x55/0x56/0x53 polls the fleet registry keys on. Frame-level asserts on
the polls, guide-layout fixtures for the responses.
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from core.sas_protocol import sas_crc
from core.sas_meters import SAS_POLL_FLOOR, int_to_bcd
from core.sas_machine_info import (
    GameConfig, MachineInfo, build_extended_validation_status_poll,
    parse_enabled_game_numbers, parse_extended_validation_status,
    parse_game_config, parse_machine_id, parse_sas_version,
    parse_selected_game_number, read_machine_info,
)
from transport.serial.sas_serial import MockSASSerialPort


def crc_le(body: bytes) -> bytes:
    return sas_crc(body).to_bytes(2, "little")


def no_sleep(_seconds):
    """No-op sleep for mock sweeps (real sweeps pace at SAS_POLL_FLOOR)."""


# §4.4.13 fixture: the 20 data bytes of a 0x1F response.
MACHINE_ID_DATA = (
    b"WS"            # game ID, 2 ASCII
    + b"000"         # additional game ID, ASCII zero = unused
    + b"\x01"        # denomination code (RAW — table not in guide)
    + b"\x03"        # max bet
    + b"\x00"        # progressive group
    + b"\x00\x07"    # game options (raw)
    + b"PT0091"      # paytable ID, 6 ASCII
    + b"9250"        # theoretical RTP, implied decimal
)


class TestMachineIdParse:
    def test_parse_machine_id_1f(self):
        cfg = parse_machine_id(MACHINE_ID_DATA)
        assert cfg.game_number is None          # machine-level poll
        assert cfg.game_id == "WS"
        assert cfg.additional_game_id == "000"
        assert cfg.denomination_code == 0x01
        assert cfg.max_bet == 3
        assert cfg.progressive_group == 0
        assert cfg.game_options_raw == b"\x00\x07"
        assert cfg.paytable_id == "PT0091"
        assert cfg.rtp_raw == "9250"
        assert cfg.theoretical_rtp_pct == 92.50

    def test_parse_machine_id_wrong_length(self):
        with pytest.raises(ValueError):
            parse_machine_id(MACHINE_ID_DATA[:-1])

    def test_parse_game_config_53(self):
        """§4.5.4 = leading 2-byte-BCD game number + the same block."""
        cfg = parse_game_config(int_to_bcd(2, 2) + MACHINE_ID_DATA)
        assert cfg.game_number == 2
        assert cfg.paytable_id == "PT0091"

    def test_rtp_garbage_yields_none_not_crash(self):
        cfg = parse_machine_id(MACHINE_ID_DATA[:16] + b"\xff\xff\xff\xff")
        assert cfg.theoretical_rtp_pct is None


class TestVersionAndGames:
    def test_parse_sas_version_54(self):
        """§4.4.24: length byte + '602' + variable-length serial."""
        serial = b"WMS0012345"
        data = bytes([3 + len(serial)]) + b"602" + serial
        v = parse_sas_version(data)
        assert v.sas_version == "602"
        assert v.serial_number == "WMS0012345"

    def test_parse_sas_version_bad_length_byte(self):
        with pytest.raises(ValueError):
            parse_sas_version(b"\x09" + b"602" + b"SN")

    def test_selected_game_number_55(self):
        """Decoded as BCD (guide text says ASCII but its example bytes are
        00 00 — flagged TODO(bench) in the module)."""
        assert parse_selected_game_number(int_to_bcd(1, 2)) == 1
        assert parse_selected_game_number(b"\x00\x00") == 0

    def test_enabled_game_numbers_56(self):
        data = bytes([5, 2]) + int_to_bcd(1, 2) + int_to_bcd(12, 2)
        assert parse_enabled_game_numbers(data) == [1, 12]

    def test_enabled_game_numbers_count_mismatch(self):
        with pytest.raises(ValueError):
            parse_enabled_game_numbers(bytes([5, 3]) + b"\x00" * 4)


class TestExtendedValidationStatus:
    def test_poll_frame_is_read_only_zeros(self):
        """§4.4.26: the poll must carry all-zero mask/status/expirations
        ('should always be set to 0000' in MT) so it can never reconfigure
        the machine. Exact frame: 01 7B 08 + 8 zeros + CRC."""
        frame = build_extended_validation_status_poll(1)
        body = bytes([0x01, 0x7B, 0x08]) + b"\x00" * 8
        assert frame == body + crc_le(body)

    def test_parse_response(self):
        data = (bytes([10])
                + b"\xD2\x02\x96\x49"       # asset number, raw 4 bytes
                + b"\x00\x80"               # status bits raw
                + int_to_bcd(9999, 2)       # cashable: never expire
                + int_to_bcd(30, 2))        # restricted: 30 days
        st = parse_extended_validation_status(data)
        assert st.asset_number_raw == b"\xD2\x02\x96\x49"
        assert st.status_bits_raw == b"\x00\x80"
        assert st.cashable_expiration_days == 9999
        assert st.restricted_expiration_days == 30


class IdentityMachine:
    """Mock VGM answering the identity polls with guide-layout frames."""

    def __init__(self, address=1, serial=b"WMS0012345", games=(1, 12)):
        self.address = address
        self.serial = serial
        self.games = games
        self.unsupported = set()

    def _frame(self, cmd, data):
        body = bytes([self.address, cmd]) + data
        return body + sas_crc(body).to_bytes(2, "little")

    def __call__(self, frame, wakeup):
        if not wakeup[0] or frame[0] & 0x7F != self.address:
            return b""
        if len(frame) == 1:
            return b"\x00"
        cmd = frame[1]
        if cmd in self.unsupported:
            return b""
        if cmd == 0x1F and len(frame) == 2:
            return self._frame(cmd, MACHINE_ID_DATA)
        if cmd == 0x54 and len(frame) == 2:
            return self._frame(cmd, bytes([3 + len(self.serial)]) + b"602"
                               + self.serial)
        if cmd == 0x51 and len(frame) == 2:
            return self._frame(cmd, int_to_bcd(len(self.games), 2))
        if cmd == 0x55 and len(frame) == 2:
            return self._frame(cmd, int_to_bcd(self.games[0], 2))
        if cmd == 0x56 and len(frame) == 2:
            payload = b"".join(int_to_bcd(g, 2) for g in self.games)
            return self._frame(cmd, bytes([1 + len(payload), len(self.games)])
                               + payload)
        if cmd == 0x7B and len(frame) == 13:
            # validate the poll's CRC like a real machine would
            if sas_crc(frame[:-2]) != int.from_bytes(frame[-2:], "little"):
                return b""
            return self._frame(cmd, bytes([10]) + b"\xD2\x02\x96\x49"
                               + b"\x00\x00" + int_to_bcd(9999, 2)
                               + int_to_bcd(9999, 2))
        return b""


class TestReadMachineInfo:
    def test_full_identity_sweep(self):
        machine = IdentityMachine(address=1)
        port = MockSASSerialPort(machine)
        info = read_machine_info(port, 1, sleep=no_sleep)

        assert isinstance(info, MachineInfo)
        assert isinstance(info.machine, GameConfig)
        assert info.machine.paytable_id == "PT0091"
        assert info.sas_version == "602"
        assert info.serial_number == "WMS0012345"
        assert info.total_games == 2
        assert info.selected_game == 1
        assert info.enabled_games == [1, 12]
        assert info.asset_number_raw == b"\xD2\x02\x96\x49"
        assert info.registry_key == "sas:WMS0012345"

    def test_identity_polls_on_the_wire(self):
        """Frame-level: 1F/54/51/55/56 go out as bare 2-byte type R polls;
        7B goes out as the 13-byte CRC frame."""
        machine = IdentityMachine(address=1)
        port = MockSASSerialPort(machine)
        read_machine_info(port, 1, sleep=no_sleep)
        frames = [f for f, _ in port.sent_frames]
        assert frames[:5] == [bytes([0x01, c])
                              for c in (0x1F, 0x54, 0x51, 0x55, 0x56)]
        assert len(frames[5]) == 13 and frames[5][:3] == b"\x01\x7B\x08"

    def test_identity_sweep_paces_polls_at_sas_floor(self):
        """SAS forbids >1 poll per 200 ms to one machine, and this sweep
        reads silence as 'unsupported' (§2.7.4) — so it must sleep
        SAS_POLL_FLOOR between its 6 polls by default (5 naps)."""
        machine = IdentityMachine(address=1)
        port = MockSASSerialPort(machine)
        naps = []
        read_machine_info(port, 1, sleep=naps.append)
        assert naps == [SAS_POLL_FLOOR] * 5

    def test_silent_machine_yields_empty_info(self):
        port = MockSASSerialPort()          # answers nothing
        info = read_machine_info(port, 1, sleep=no_sleep)
        assert info.machine is None and info.serial_number is None
        assert info.enabled_games == []
        assert info.registry_key == "sas-addr:1"

    def test_partial_support_falls_back_to_paytable_key(self):
        machine = IdentityMachine(address=1)
        machine.unsupported = {0x54, 0x7B}    # no serial, no asset number
        port = MockSASSerialPort(machine)
        info = read_machine_info(port, 1, sleep=no_sleep)
        assert info.serial_number is None
        assert info.registry_key == "sas:WS:PT0091"
