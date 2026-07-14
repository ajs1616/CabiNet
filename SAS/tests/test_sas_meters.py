"""
Tests for the SAS meter suite (core/sas_meters.py) — frame-level byte
asserts (exact bytes including CRC) plus parser round-trips against a mock
machine that serves the Montana guide's documented response layouts.

Hard-coded CRC fixtures were computed independently with the CRC-16/Kermit
algorithm (catalog check value 0x2189) — they pin the builders to exact wire
bytes rather than trusting the builder's own CRC call.
"""

import sys
from datetime import datetime
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from core.sas_protocol import SASProtocol, sas_crc
from core.sas_meters import (
    MeterSnapshot, SAS_POLL_FLOOR, SINGLE_METER_COMMANDS, SNAPSHOT_POLLS,
    TYPE_R_POLLS,
    bcd_to_int, int_to_bcd,
    build_enhanced_validation_info_poll, build_game_config_poll,
    build_game_meters_poll, build_meter_poll, build_receive_date_time_poll,
    build_rom_signature_poll, build_selected_meters_poll_2f,
    build_selected_meters_poll_6f, build_set_validation_id_poll,
    build_validation_meters_poll,
    decode_response, parse_bill_meters, parse_cashout_ticket_info,
    parse_date_time, parse_enabled_features, parse_enhanced_validation_info,
    parse_game_meters, parse_games_since, parse_last_bill,
    parse_meters_10_15, parse_meters_11_15, parse_rom_signature,
    parse_selected_meters_2f, parse_selected_meters_6f, parse_single_meter,
    parse_total_games, parse_validation_meters, sweep_snapshot,
)
from core.sas_poller import SASPoller
from transport.serial.sas_serial import MockSASSerialPort


def crc_le(body: bytes) -> bytes:
    return sas_crc(body).to_bytes(2, "little")


class TestBCDHelpers:
    def test_round_trip(self):
        assert int_to_bcd(1234, 4) == b"\x00\x00\x12\x34"
        assert bcd_to_int(b"\x00\x00\x12\x34") == 1234
        assert bcd_to_int(int_to_bcd(99999999, 4)) == 99999999

    def test_invalid_bcd_returns_none(self):
        assert bcd_to_int(b"\x00\xA5") is None

    def test_int_to_bcd_overflow_raises(self):
        with pytest.raises(ValueError):
            int_to_bcd(100, 1)
        with pytest.raises(ValueError):
            int_to_bcd(-1, 2)


class TestPollBuilders:
    """Exact wire bytes, CRC included."""

    def test_type_r_polls_are_two_bytes_no_crc(self):
        for cmd in sorted(TYPE_R_POLLS):
            frame = build_meter_poll(1, cmd)
            assert frame == bytes([0x01, cmd]), f"cmd 0x{cmd:02X}"

    def test_type_r_refuses_data_bearing_commands(self):
        for cmd in (0x21, 0x52, 0x53, 0x50, 0x4D, 0x4C, 0x2F, 0x6F, 0x7F, 0x7B):
            with pytest.raises(ValueError):
                build_meter_poll(1, cmd)

    def test_game_meters_poll_hardcoded_frame(self):
        """§4.5.3: '01 52 00 05' + CRC — fixture computed independently."""
        assert build_game_meters_poll(1, 5) == bytes.fromhex("015200054d7d")

    def test_game_config_poll_hardcoded_frame(self):
        """§4.5.4: '01 53 00 01' + CRC."""
        assert build_game_config_poll(1, 1) == bytes.fromhex("01530001b561")

    def test_validation_meters_poll_hardcoded_frame(self):
        """§4.6.4: '01 50 00' + CRC."""
        assert build_validation_meters_poll(1, 0) == bytes.fromhex("0150002b89")

    def test_enhanced_validation_info_poll_hardcoded_frame(self):
        """§4.6.2: '01 4D 00' + CRC."""
        assert build_enhanced_validation_info_poll(1, 0) == \
            bytes.fromhex("014d00c2ac")

    def test_enhanced_validation_info_function_codes(self):
        assert build_enhanced_validation_info_poll(1, 0xFF)[2] == 0xFF
        assert build_enhanced_validation_info_poll(1, 0x1F)[2] == 0x1F
        with pytest.raises(ValueError):
            build_enhanced_validation_info_poll(1, 0x20)

    def test_rom_signature_poll_hardcoded_frame(self):
        """§4.3.1: '01 21 12 34' + CRC. Seed passed as raw wire bytes
        (byte order is a TODO(bench)). NOTE the RESULT does not answer this
        poll — it arrives via a LATER general poll (§4.3/§3); see
        test_sas_poller.py::test_rom_signature_result_via_general_poll."""
        assert build_rom_signature_poll(1, b"\x12\x34") == \
            bytes.fromhex("01211234da94")
        with pytest.raises(ValueError):
            build_rom_signature_poll(1, b"\x12")

    def test_set_validation_id_poll_layout(self):
        """§4.6.1: [addr][4C][id 3B][seq 3B][crc]; all-zero ID = read."""
        frame = build_set_validation_id_poll(1, b"\x00\x00\x00", b"\x00\x00\x00")
        assert frame[:8] == b"\x01\x4C" + b"\x00" * 6
        assert frame[8:] == crc_le(frame[:8])
        with pytest.raises(ValueError):
            build_set_validation_id_poll(1, b"\x00\x00", b"\x00\x00\x00")

    def test_receive_date_time_poll_bcd_layout(self):
        """§4.4.29: [addr][7F][MMDDYYYY BCD][HHMMSS BCD][crc]."""
        frame = build_receive_date_time_poll(
            1, datetime(2026, 7, 1, 13, 45, 9))
        assert frame[:2] == b"\x01\x7F"
        assert frame[2:9] == bytes.fromhex("07012026134509")   # BCD digits
        assert frame[9:] == crc_le(frame[:9])

    def test_selected_meters_2f_frame(self):
        """§4.5.1: [addr][2F][len][game BCD][1-byte codes][crc]; MT needs
        codes 00 (coin in) and 01 (coin out)."""
        frame = build_selected_meters_poll_2f(1, 1, [0x00, 0x01])
        body = bytes.fromhex("012f0400010001")   # len = 2 + 2 codes = 4
        assert frame == body + crc_le(body)
        with pytest.raises(ValueError):
            build_selected_meters_poll_2f(1, 1, [])
        with pytest.raises(ValueError):
            build_selected_meters_poll_2f(1, 1, list(range(11)))

    def test_selected_meters_6f_frame(self):
        """§4.4.25: [addr][6F][len][game BCD][2-byte codes][crc] — code
        0x0024 = Total Drop per guide table 4.1, transmitted LSB-first
        (24 00) per SAS's binary multi-byte convention; the CRC is the only
        wire-proven precedent. TODO(bench): confirm on a real machine."""
        frame = build_selected_meters_poll_6f(1, 0, [0x0024])
        body = bytes.fromhex("016f0400002400")   # len = 2 + 1 code * 2 = 4
        assert frame == body + crc_le(body)
        with pytest.raises(ValueError):
            build_selected_meters_poll_6f(1, 0, list(range(12)))


class TestSingleMeterParsers:
    def test_single_meter_4_byte_bcd(self):
        assert parse_single_meter(b"\x00\x12\x34\x56") == 123456
        with pytest.raises(ValueError):
            parse_single_meter(b"\x00\x00\x00")

    def test_single_meter_command_coverage(self):
        """§4.4.2-.22: every lone 4-byte-BCD meter the guide documents."""
        assert set(SINGLE_METER_COMMANDS) == {
            0x10, 0x11, 0x12, 0x13, 0x15, 0x16, 0x17, 0x1A, 0x20,
            0x31, 0x32, 0x33, 0x34, 0x35, 0x36, 0x37, 0x46}

    def test_composite_0f(self):
        """§4.4.1: cancelled/coin-in/coin-out/drop/jackpot/games-played."""
        data = b"".join(int_to_bcd(v, 4) for v in
                        (11, 22, 33, 44, 0, 55))
        parsed = parse_meters_10_15(data)
        assert parsed == {"cancelled_credits": 11, "coin_in": 22,
                          "coin_out": 33, "total_drop": 44, "jackpot": 0,
                          "games_played": 55}

    def test_composite_19(self):
        """§4.4.10: coin-in/coin-out/drop/jackpot/games-played."""
        data = b"".join(int_to_bcd(v, 4) for v in (1, 2, 3, 0, 4))
        assert parse_meters_11_15(data)["games_played"] == 4

    def test_games_since_0x18(self):
        """§4.4.9: two 2-byte BCD counters."""
        assert parse_games_since(int_to_bcd(12, 2) + int_to_bcd(7, 2)) == (12, 7)

    def test_bill_meters_1e(self):
        """§4.4.12: $1/$5/$10/$20/$50/$100 counts keyed by cents."""
        data = b"".join(int_to_bcd(v, 4) for v in (9, 8, 7, 6, 5, 4))
        assert parse_bill_meters(data) == {
            100: 9, 500: 8, 1000: 7, 2000: 6, 5000: 5, 10000: 4}

    def test_last_bill_48(self):
        """§4.4.23: country (1 BCD), denom code (1 BCD), count (4 BCD)."""
        lb = parse_last_bill(b"\x01\x05" + int_to_bcd(321, 4))
        assert (lb.country_code, lb.denom_code, lb.count) == (1, 5, 321)

    def test_date_time_7e(self):
        """§4.4.27: MMDDYYYY + HHMMSS BCD."""
        dt = parse_date_time(bytes.fromhex("07012026134509"))
        assert dt == datetime(2026, 7, 1, 13, 45, 9)
        with pytest.raises(ValueError):
            parse_date_time(bytes.fromhex("0A012026134509"))  # non-BCD month

    def test_enabled_features_a0(self):
        """§4.4.28 table 4.5.36 bit decode (LSB-byte-first assumption is a
        flagged TODO(bench))."""
        data = b"\x00\x00\xCD\xDD\x00\x00\x00\x00"
        f = parse_enabled_features(data)
        assert f.game_number == 0
        assert f.jackpot_multiplier and f.bonus_awards and f.tournament
        assert f.validation_style == "enhanced"
        assert f.voucher_redemption
        assert f.meter_model == 1
        assert f.vouchers_to_drop and f.extended_meters
        assert f.component_authentication and f.aft and f.multi_denom_extensions
        assert f.raw == b"\xCD\xDD"

    def test_enabled_features_all_off(self):
        f = parse_enabled_features(b"\x00\x01" + b"\x00" * 6)
        assert f.game_number == 1
        assert not f.aft and f.validation_style == "standard"


class TestMultiGameParsers:
    def test_total_games_51(self):
        assert parse_total_games(int_to_bcd(12, 2)) == 12

    def test_game_meters_52(self):
        data = int_to_bcd(3, 2) + b"".join(
            int_to_bcd(v, 4) for v in (100, 90, 0, 42))
        gm = parse_game_meters(data)
        assert (gm.game_number, gm.coin_in, gm.coin_out, gm.jackpot,
                gm.games_played) == (3, 100, 90, 0, 42)

    def test_selected_meters_6f_self_describing(self):
        """§4.4.25 response: per-meter size byte makes 6F fully parseable.
        Meter codes are read LSB-first (see the builder's TODO(bench))."""
        data = (bytes([17]) + int_to_bcd(0, 2)
                + b"\x00\x00" + b"\x04" + int_to_bcd(1234, 4)      # code 0
                + b"\x01\x00" + b"\x05" + int_to_bcd(567890, 5))   # code 1
        out = parse_selected_meters_6f(data)
        assert out["game_number"] == 0
        assert out["meters"] == {0x0000: 1234, 0x0001: 567890}

    def test_selected_meters_6f_length_mismatch_raises(self):
        with pytest.raises(ValueError):
            parse_selected_meters_6f(b"\x09" + b"\x00" * 4)

    def test_selected_meters_2f_single_meter_inferred(self):
        """No size byte in 2F — a single meter's size is inferred from the
        remaining length (4 here)."""
        data = bytes([7]) + int_to_bcd(1, 2) + b"\x00" + int_to_bcd(999, 4)
        out = parse_selected_meters_2f(data)
        assert out == {"game_number": 1, "meters": {0x00: 999}}

    def test_selected_meters_2f_multi_needs_size_table(self):
        """Honest limitation: multi-meter 2F is ambiguous without the spec's
        Appendix C size table — must raise, not guess."""
        data = (bytes([12]) + int_to_bcd(1, 2)
                + b"\x00" + int_to_bcd(11, 4)
                + b"\x01" + int_to_bcd(22, 4))
        with pytest.raises(ValueError):
            parse_selected_meters_2f(data)
        out = parse_selected_meters_2f(data, meter_sizes={0x00: 4, 0x01: 4})
        assert out["meters"] == {0x00: 11, 0x01: 22}


class TestValidationParsers:
    def test_rom_signature_raw_bytes(self):
        assert parse_rom_signature(b"\xAB\xCD") == b"\xAB\xCD"

    def test_validation_meters_50(self):
        """§4.6.4: type, count (4 BCD), amount cents (5 BCD)."""
        data = b"\x00" + int_to_bcd(15, 4) + int_to_bcd(123456, 5)
        vm = parse_validation_meters(data)
        assert (vm.validation_type, vm.count, vm.amount_cents) == (0, 15, 123456)

    def test_cashout_ticket_info_3d(self):
        """§4.6.3: std validation number (4 BCD = 8 digits) + cents (5 BCD)."""
        data = int_to_bcd(123456, 4) + int_to_bcd(2500, 5)
        ct = parse_cashout_ticket_info(data)
        assert ct.validation_number == "00123456"
        assert ct.amount_cents == 2500

    def test_enhanced_validation_record_4d(self):
        """§4.6.2: the full 31-data-byte record (guide bytes 3-33)."""
        data = (b"\x00"                                  # type 00 = cashable
                + b"\x01"                                # buffer index
                + bytes.fromhex("07012026")              # 07/01/2026
                + bytes.fromhex("134509")                # 13:45:09
                + int_to_bcd(1234567890123456, 8)        # validation number
                + int_to_bcd(2500, 5)                    # $25.00
                + b"\x34\x12"                            # ticket# raw
                + b"\x00"                                # system id: enhanced
                + b"\x00" * 6)                           # reserved
        rec = parse_enhanced_validation_info(data)
        assert rec.validation_type == 0
        assert rec.buffer_index == 1
        assert rec.when == datetime(2026, 7, 1, 13, 45, 9)
        assert rec.validation_number == "1234567890123456"
        assert rec.amount_cents == 2500
        assert rec.ticket_number_raw == b"\x34\x12"
        assert rec.ticket_number_le == 0x1234
        assert rec.system_id == 0

    def test_enhanced_validation_zeroed_record(self):
        """An empty buffer slot (all zeros) must parse with when=None, not
        blow up on month 0."""
        rec = parse_enhanced_validation_info(b"\x00" * 31)
        assert rec.when is None
        assert rec.amount_cents == 0


class GuideMachine:
    """A mock VGM that answers the §4.4 accounting polls with the guide's
    exact response layouts (addr + cmd + data + CRC LSB-first). Unlisted
    commands get dead silence, per §2.7.4 (unsupported = no response)."""

    def __init__(self, address=1, **meters):
        self.address = address
        self.m = {
            "cancelled_credits": 0, "coin_in": 0, "coin_out": 0,
            "total_drop": 0, "jackpot": 0, "games_played": 0,
            "games_won": 0, "games_lost": 0, "since_power": 0,
            "since_door": 0, "credits": 0, "dollar_bills": 0,
            "credit_bills": 0,
            "bills": (0, 0, 0, 0, 0, 0),
        }
        self.m.update(meters)
        self.unsupported = set()

    def _frame(self, cmd, data):
        body = bytes([self.address, cmd]) + data
        return body + sas_crc(body).to_bytes(2, "little")

    def __call__(self, frame, wakeup):
        if not wakeup[0]:
            return b""
        if len(frame) == 1:                       # general poll
            return b"\x00" if (frame[0] & 0x7F) == self.address else b""
        if frame[0] != self.address or len(frame) != 2:
            return b""
        cmd = frame[1]
        if cmd in self.unsupported:
            return b""
        m, bcd = self.m, int_to_bcd
        if cmd == 0x0F:
            return self._frame(cmd, b"".join(bcd(m[k], 4) for k in (
                "cancelled_credits", "coin_in", "coin_out", "total_drop",
                "jackpot", "games_played")))
        if cmd == 0x16:
            return self._frame(cmd, bcd(m["games_won"], 4))
        if cmd == 0x17:
            return self._frame(cmd, bcd(m["games_lost"], 4))
        if cmd == 0x18:
            return self._frame(cmd, bcd(m["since_power"], 2)
                               + bcd(m["since_door"], 2))
        if cmd == 0x1A:
            return self._frame(cmd, bcd(m["credits"], 4))
        if cmd == 0x1E:
            return self._frame(cmd, b"".join(bcd(v, 4) for v in m["bills"]))
        if cmd == 0x20:
            return self._frame(cmd, bcd(m["dollar_bills"], 4))
        if cmd == 0x46:
            return self._frame(cmd, bcd(m["credit_bills"], 4))
        return b""


def no_sleep(_seconds):
    """No-op sleep for mock sweeps (real sweeps pace at SAS_POLL_FLOOR)."""


class TestSweepSnapshot:
    def test_full_snapshot(self):
        machine = GuideMachine(
            address=1, coin_in=1000, coin_out=800, total_drop=200,
            cancelled_credits=5, games_played=42, games_won=17,
            games_lost=25, since_power=9, since_door=3, credits=1234,
            dollar_bills=61, credit_bills=6100, bills=(1, 2, 3, 1, 0, 0))
        port = MockSASSerialPort(machine)
        snap = sweep_snapshot(port, 1, sleep=no_sleep)

        assert isinstance(snap, MeterSnapshot)
        assert snap.address == 1
        assert snap.coin_in == 1000 and snap.coin_out == 800
        assert snap.total_drop == 200 and snap.cancelled_credits == 5
        assert snap.jackpot == 0
        assert snap.games_played == 42
        assert snap.games_won == 17 and snap.games_lost == 25
        assert snap.games_since_power_up == 9
        assert snap.games_since_door_close == 3
        assert snap.current_credits == 1234
        assert snap.dollar_value_of_bills == 61
        assert snap.credit_amount_of_bills == 6100
        assert snap.bills == {100: 1, 500: 2, 1000: 3, 2000: 1,
                              5000: 0, 10000: 0}
        assert snap.as_dict()["coin_in"] == 1000

    def test_snapshot_polls_are_type_r_frames(self):
        """Frame-level: every snapshot poll on the wire is exactly
        [addr][cmd] with NO CRC and the wake-up bit on byte 0 only."""
        machine = GuideMachine(address=1)
        port = MockSASSerialPort(machine)
        sweep_snapshot(port, 1, sleep=no_sleep)
        assert [f for f, _ in port.sent_frames] == \
            [bytes([0x01, c]) for c in SNAPSHOT_POLLS]
        for _, wakeup in port.sent_frames:
            assert wakeup[0] is True and all(w is False for w in wakeup[1:])

    def test_sweep_paces_polls_at_sas_floor(self):
        """SAS forbids >1 poll per 200 ms to one machine, and this sweep
        reads silence as 'unsupported' (§2.7.4) — so it must sleep
        SAS_POLL_FLOOR between consecutive long polls by default."""
        machine = GuideMachine(address=1)
        port = MockSASSerialPort(machine)
        naps = []
        sweep_snapshot(port, 1, sleep=naps.append)
        assert naps == [SAS_POLL_FLOOR] * (len(SNAPSHOT_POLLS) - 1)

    def test_unsupported_polls_leave_fields_none(self):
        """§2.7.4: silence on an unsupported command is normal — those
        fields stay None, the rest still fill in."""
        machine = GuideMachine(address=1, credits=77)
        machine.unsupported = {0x16, 0x17, 0x1E, 0x20, 0x46}
        port = MockSASSerialPort(machine)
        snap = sweep_snapshot(port, 1, sleep=no_sleep)
        assert snap.current_credits == 77
        assert snap.games_won is None and snap.games_lost is None
        assert snap.bills == {}
        assert snap.dollar_value_of_bills is None

    def test_wrong_address_response_ignored(self):
        """A frame echoing a different address must not populate the
        snapshot (stale/foreign frame guard)."""
        machine = GuideMachine(address=2, credits=999)
        machine.address = 2

        def foreign(frame, wakeup):
            # machine 2 wrongly answers polls addressed to 1
            return machine(bytes([2, frame[1]]) if len(frame) == 2 else frame,
                           wakeup)

        port = MockSASSerialPort(foreign)
        snap = sweep_snapshot(port, 1, sleep=no_sleep)
        assert snap.current_credits is None

    def test_poller_snapshot_helper(self):
        machine = GuideMachine(address=1, credits=555, games_played=7)
        port = MockSASSerialPort(machine)
        poller = SASPoller(port, address=1)
        snap = poller.snapshot_meters(sleep=no_sleep)
        assert snap.current_credits == 555
        assert snap.games_played == 7


class TestDecodeDispatch:
    def test_dispatch_matches_dedicated_parsers(self):
        assert decode_response(0x1A, int_to_bcd(42, 4)) == 42
        assert decode_response(0x51, int_to_bcd(3, 2)) == 3
        assert decode_response(0x7E, bytes.fromhex("07012026134509")) == \
            datetime(2026, 7, 1, 13, 45, 9)
        assert decode_response(0x99, b"") is None   # no parser -> None
