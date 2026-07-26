"""
SAS 6.02 §16 multi-denomination extensions — unit gate.

NO HARDWARE. Every frame here is either
  (a) a byte-for-byte fixture printed in the SAS 6.02 spec itself (Tables
      16.2a-16.2d — including the spec's own published CRC values, which are
      an INDEPENDENT check on our CRC-16/Kermit LSB-first implementation), or
  (b) bytes actually observed on the WMS BB2 (smib-bb2) on 2026-07-25 —
      labelled [WIRE] where they appear.
Anything else is a constructed fixture and is labelled as such. Fixture
evidence is NOT wire truth; the docstrings say which is which.

What this file pins:
  * FIX 1 — the §16.1 ACK/NACK data-field convention is scoped to ACK/NACK-only
    base polls (LP 09). Applying it to reads silently ate real data on the
    2026-07-25 sweep.
  * FIX 2 — the B0 probe carries the base poll's data field, and a frame short
    of MULTIDENOM_BASE_MIN_DATA is refused rather than emitted.
  * FIX 3 — LP B1 / LP B2 (§16.3 / §16.4), the PLAYER denominations, kept
    distinct from the 0x1F ACCOUNTING denomination.
  * FIX 4 — the host-side stranding guard: no write may leave the cabinet with
    no denomination a player can play.
  * FIX 6 — bare LP B5 (§7.23), the only human-readable game/paytable name in
    SAS.
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from core.sas_protocol import (
    SASProtocol, sas_crc, MULTIDENOM_ACKNACK_BASES, MULTIDENOM_BASE_MIN_DATA,
    MULTIDENOM_AWARE_POLLS, MULTIDENOM_ERRORS, DENOM_CODE_CENTS,
)
from core.sas_meters import TYPE_R_POLLS, build_meter_poll
from core.sas_machine_info import (
    parse_current_player_denom, parse_enabled_player_denoms,
    build_extended_game_info_poll, parse_extended_game_info,
    parse_enabled_game_numbers, denom_code_cents,
)
from sas_host import denom_disable_guard, read_enabled_player_denoms

PROTO = SASProtocol()
ADDR = 1


def _frame(body_hex: str) -> bytes:
    """Append the SAS CRC (LSB first) to a hex body."""
    body = bytes.fromhex(body_hex.replace(" ", ""))
    return body + sas_crc(body).to_bytes(2, "little")


def _b0_response(denom: int, base: int, data: bytes, address: int = ADDR
                 ) -> bytes:
    body = bytes([denom, base]) + data
    return _frame(bytes([address, 0xB0, len(body)]).hex() + body.hex())


def _lp56_payload(games) -> bytes:
    """LP 0x56 data: [len][count][game 2-BCD]* (Table 16.2b's shape)."""
    out = b"".join(int(f"{g:04d}", 16).to_bytes(2, "big") for g in games)
    return bytes([1 + len(out), len(games)]) + out


def _b2_frame(codes, address: int = ADDR) -> bytes:
    """§16.4 Table 16.4: [addr][B2][len][count][codes...][CRC]."""
    body = bytes([address, 0xB2, 1 + len(codes), len(codes)]) + bytes(codes)
    return _frame(body.hex())


def _b1_frame(code: int, address: int = ADDR) -> bytes:
    return _frame(bytes([address, 0xB1, code]).hex())


class FakeTransport:
    """Anything with .transact(bytes)->bytes is a SAS transport here."""

    def __init__(self, responder):
        self._responder = responder
        self.sent = []

    def transact(self, frame):
        self.sent.append(bytes(frame))
        return self._responder(bytes(frame))


def no_sleep(_):
    return None


# ---------------------------------------------------------------------------
# The spec's own worked examples — independent CRC adjudication
# ---------------------------------------------------------------------------

class TestSpecPublishedFrames:
    """SAS 6.02 §16.2 prints complete frames INCLUDING their CRC values. Our
    builder reproducing those bytes exactly is an independent check that the
    CRC (Kermit, LSB-first on the wire) and the preamble field order are both
    right — evidence that does not come from our own code."""

    def test_table_16_2a_enabled_games_at_5c(self):
        """Table 16.2a: host asks for the enabled game
        list at 5c. Spec CRC = DB63, transmitted LSB first."""
        assert PROTO.build_multidenom_poll(1, 0x02, 0x56) == \
            bytes.fromhex("01b002025663db")

    def test_table_16_2b_response_crc(self):
        """Table 16.2b: two games (0003, 0007) enabled at 5c, spec CRC 4EF3."""
        body = bytes.fromhex("01b0080256050200030007")
        assert sas_crc(body) == 0x4EF3
        dec = PROTO.parse_multidenom_response(1, body + b"\xf3\x4e")
        assert dec["outcome"] == "data"       # a READ, never "ack"
        assert dec["denom"] == 0x02 and dec["baseCommand"] == 0x56
        assert parse_enabled_game_numbers(dec["data"]) == [3, 7]

    def test_table_16_2c_selected_meters_at_25c(self):
        """Table 16.2c: total coin in at 25c, with the
        2F inner length + game 0000 + meter code. Spec CRC 0C56. THIS is the
        frame FIX 2 exists to make sendable — and PATH 0's decisive read."""
        frame = PROTO.build_multidenom_poll(1, 0x04, 0x2F,
                                            bytes.fromhex("03000000"))
        assert frame == bytes.fromhex("01b006042f03000000560c")

    def test_table_16_2d_response_decodes_as_data(self):
        """Table 16.2d: the 25c coin-in meter reads 123456. Spec CRC 1952."""
        body = bytes.fromhex("01b00a042f0700000000123456")
        assert sas_crc(body) == 0x1952
        dec = PROTO.parse_multidenom_response(1, body + b"\x52\x19")
        assert dec["outcome"] == "data"
        assert dec["denom"] == 0x04 and dec["baseCommand"] == 0x2F


# ---------------------------------------------------------------------------
# FIX 1 — ACK/NACK scoping
# ---------------------------------------------------------------------------

class TestAckNackScoping:
    """§16.1 scopes the address-in-the-data-field
    convention to base polls that answer ONLY ack/nack. Table 16.1d makes that
    LP 09 alone."""

    def test_only_lp09_is_an_acknack_base(self):
        assert MULTIDENOM_ACKNACK_BASES == frozenset({0x09})
        assert 0x09 in MULTIDENOM_AWARE_POLLS

    def test_wire_zero_games_at_50c_is_data_not_ack(self):
        """[WIRE] BB2 2026-07-25: `01 b0 04 05 56 01 00` = LP56 at denom 05
        (50c) answering inner-length 01, game-count 00 — ZERO games enabled at
        50c. The old unconditional test read the leading 0x01 as this
        machine's poll address and reported "ack", discarding the single most
        valuable result of the whole sweep."""
        dec = PROTO.parse_multidenom_response(1, _frame("01 b0 04 05 56 01 00"))
        assert dec["outcome"] == "data"
        assert dec["denom"] == 0x05 and dec["baseCommand"] == 0x56
        assert parse_enabled_game_numbers(dec["data"]) == []

    def test_wire_coin_in_at_1dollar_is_data_not_ack(self):
        """[WIRE] BB2 2026-07-25: `01 b0 06 06 11 01 06 00 00` = LP11 coin-in
        at denom 06 ($1) = 1,060,000. Same collision: BCD top byte 0x01."""
        dec = PROTO.parse_multidenom_response(
            1, _frame("01 b0 06 06 11 01 06 00 00"))
        assert dec["outcome"] == "data"
        assert dec["baseCommand"] == 0x11
        assert int(dec["data"].hex()) == 1060000

    def test_lp09_ack_and_nack_still_decode(self):
        """Constructed fixture (never seen on a wire — every BB2 LP09 attempt
        drew error 04): behind LP 09 the convention DOES apply."""
        assert PROTO.parse_multidenom_response(
            1, _b0_response(0x02, 0x09, b"\x01"))["outcome"] == "ack"
        assert PROTO.parse_multidenom_response(
            1, _b0_response(0x02, 0x09, b"\x81"))["outcome"] == "nack"

    def test_lp09_inner_busy_is_its_own_verdict(self):
        """§16.1: a busy answer aimed at the BASE
        command rides in the preamble's data field. Behind LP 09 the normal
        data field is one byte, so [addr][00] is unambiguous. Constructed
        fixture."""
        dec = PROTO.parse_multidenom_response(
            1, _b0_response(0x02, 0x09, b"\x01\x00"))
        assert dec["outcome"] == "busy_base"

    def test_read_base_never_guesses_busy(self):
        """The deliberate NON-fix: for a READ base, [addr][00] is
        indistinguishable from a real payload — `01 00` is exactly LP56's
        "zero games enabled" answer at address 1 [WIRE]. We report data and
        let the payload parser speak rather than manufacture a busy."""
        dec = PROTO.parse_multidenom_response(
            1, _b0_response(0x05, 0x56, b"\x01\x00"))
        assert dec["outcome"] == "data"

    def test_error_04_is_the_verdict(self):
        """[WIRE] BB2 2026-07-25, B0-wrapped LP09 at 5c: `01 b0 03 02 00 04`.
        Base byte 00 => Table 16.1c error; 04 = "long poll not supported in
        that format for a specific denomination"."""
        dec = PROTO.parse_multidenom_response(1, _frame("01 b0 03 02 00 04"))
        assert dec["outcome"] == "error"
        assert dec["error"] == 0x04
        assert dec["errorText"] == MULTIDENOM_ERRORS[0x04]
        assert dec["denom"] == 0x02

    def test_wire_proven_read_probe_decodes(self):
        """[WIRE] BB2 2026-07-25, the first B0 frame this project ever sent:
        sent `01 b0 02 00 56 d3 e8`, got `01 b0 06 01 56 03 01 00 12 09 35`.
        The machine RESOLVED our denom-00 default to its own selected denom
        (01 = 1c) and answered game 0012."""
        assert PROTO.build_multidenom_poll(1, 0x00, 0x56) == \
            bytes.fromhex("01b0020056d3e8")
        dec = PROTO.parse_multidenom_response(
            1, bytes.fromhex("01b0060156030100120935"))
        assert dec["outcome"] == "data"
        assert dec["denom"] == 0x01
        assert parse_enabled_game_numbers(dec["data"]) == [12]


# ---------------------------------------------------------------------------
# FIX 2 — the base poll's data field, and the malformed-frame guard
# ---------------------------------------------------------------------------

class TestBaseDataGuard:
    def test_b5_without_a_game_number_is_refused(self):
        """The 2026-07-25 self-inflicted error 04: our B5 probe omitted B5's
        MANDATORY 2-byte BCD game number (Table 7.23a). That frame was our
        bug, so the machine's refusal is uninterpretable in either
        direction — the builder now refuses to emit it."""
        with pytest.raises(ValueError, match="at least 2 data byte"):
            PROTO.build_multidenom_poll(1, 0x02, 0xB5)

    def test_b5_with_a_game_number_builds(self):
        assert PROTO.build_multidenom_poll(1, 0x02, 0xB5, b"\x00\x12") == \
            _frame("01 b0 04 02 b5 00 12")

    def test_lp09_short_frame_is_refused(self):
        with pytest.raises(ValueError):
            PROTO.build_multidenom_poll(1, 0x02, 0x09, b"\x00\x12")

    def test_allow_short_is_the_only_escape(self):
        """A deliberately-malformed probe stays POSSIBLE — it just has to say
        so, so its result can be labelled as a deliberate malformation."""
        assert PROTO.build_multidenom_poll(1, 0x02, 0xB5,
                                           allow_short=True) == \
            _frame("01 b0 02 02 b5")

    def test_every_min_data_entry_is_a_multidenom_aware_poll(self):
        for base in MULTIDENOM_BASE_MIN_DATA:
            assert base in MULTIDENOM_AWARE_POLLS, f"0x{base:02X}"

    def test_read_bases_need_no_data(self):
        """The polls that carry nothing extra must stay one-liners."""
        for base in (0x11, 0x12, 0x14, 0x15, 0x16, 0x17, 0x56):
            assert PROTO.build_multidenom_poll(1, 0x02, base)


# ---------------------------------------------------------------------------
# FIX 3 — LP B1 / LP B2, the PLAYER denominations
# ---------------------------------------------------------------------------

class TestPlayerDenoms:
    def test_b1_and_b2_are_bare_type_r_polls(self):
        """Appendix B Table B-1: "B1 R 16-6", "B2 R 16-6" — two-byte polls
        with NO CRC. build_meter_poll used to raise on both."""
        assert 0xB1 in TYPE_R_POLLS and 0xB2 in TYPE_R_POLLS
        assert build_meter_poll(1, 0xB1) == b"\x01\xb1"
        assert build_meter_poll(1, 0xB2) == b"\x01\xb2"

    def test_lp14_is_now_sendable_bare(self):
        """0x14 was multi-denom-AWARE (Table 16.1d) but not in TYPE_R_POLLS —
        we could wrap a poll we could not send. Appendix B: "14 R 7-1"."""
        assert build_meter_poll(1, 0x14) == b"\x01\x14"

    def test_parse_current_player_denom(self):
        assert parse_current_player_denom(b"\x02") == 0x02

    def test_b1_rejects_out_of_range_and_wrong_length(self):
        with pytest.raises(ValueError):
            parse_current_player_denom(b"\x00")        # Table 16.3 says 01-3F
        with pytest.raises(ValueError):
            parse_current_player_denom(b"\x01\x02")

    def test_parse_enabled_player_denoms(self):
        """Constructed from Table 16.4. Codes are the BB2's eight live G2S
        denominations expressed as Table C-4 codes."""
        codes = [0x01, 0x17, 0x02, 0x03, 0x04, 0x06, 0x0E, 0x0A]
        pkt = PROTO.parse_packet(_b2_frame(codes))
        assert parse_enabled_player_denoms(pkt.data) == codes

    def test_b2_zero_denoms_is_an_empty_list_not_unknown(self):
        """Table 16.4 allows a count of 00. That is a FINDING, not a parse
        failure — never upgrade it to "unknown"."""
        pkt = PROTO.parse_packet(_b2_frame([]))
        assert parse_enabled_player_denoms(pkt.data) == []

    def test_b2_rejects_a_lying_length(self):
        with pytest.raises(ValueError):
            parse_enabled_player_denoms(b"\x09\x02\x01\x02")

    def test_denom_code_cents_matches_table_c4(self):
        assert denom_code_cents(0x01) == 1        # 1c — bench-verified on BB2
        assert denom_code_cents(0x04) == 25       # Table 16.2c's 25c example
        assert denom_code_cents(0x06) == 100
        assert denom_code_cents(None) is None
        assert denom_code_cents(0x3F) is None     # unknown code: never a guess
        assert denom_code_cents(0x00) is None     # 00 is a selector, not money
        assert DENOM_CODE_CENTS[0x02] == 5

    def test_read_enabled_player_denoms_reports_silence_honestly(self):
        codes, detail = read_enabled_player_denoms(
            FakeTransport(lambda f: b""), ADDR, PROTO, sleep=no_sleep)
        assert codes is None
        assert "unanswered" in detail
        # §4.4 forbids the other word: silence cannot prove non-support.
        assert "unsupported" not in detail

    def test_read_enabled_player_denoms_parses(self):
        codes, detail = read_enabled_player_denoms(
            FakeTransport(lambda f: _b2_frame([0x01, 0x02])), ADDR, PROTO,
            sleep=no_sleep)
        assert codes == [0x01, 0x02]

    def test_read_enabled_player_denoms_reports_busy(self):
        codes, detail = read_enabled_player_denoms(
            FakeTransport(lambda f: b"\x01\x00"), ADDR, PROTO, sleep=no_sleep)
        assert codes is None and "BUSY" in detail


# ---------------------------------------------------------------------------
# FIX 4 — the stranding guard
# ---------------------------------------------------------------------------

def _guard(responder, game=12, denom=0x02):
    return denom_disable_guard(FakeTransport(responder), ADDR, PROTO,
                               game, denom, sleep=no_sleep)


class TestStrandingGuard:
    """The owner's hard rule: at least one denomination must remain active.
    It is NOT a SAS requirement (two exhaustive walks of the spec find none — explicitly allows a single enabled player
    denomination), so this guard is a HOST-side courtesy computed live."""

    def test_b2_single_denom_refuses(self):
        g = _guard(lambda f: _b2_frame([0x02]))
        assert g["allow"] is False and g["basis"] == "B2"
        assert "no active denomination" in g["detail"]

    def test_b2_many_denoms_allows(self):
        g = _guard(lambda f: _b2_frame([0x01, 0x02, 0x04]))
        assert g["allow"] is True and g["otherDenom"] == 0x01

    def test_b2_target_not_enabled_allows(self):
        g = _guard(lambda f: _b2_frame([0x01, 0x04]))
        assert g["allow"] is True
        assert "not in the machine's enabled player-denomination list" \
            in g["detail"]

    def test_fallback_sweep_finds_a_survivor(self):
        """B2 unanswered -> a B0+56 sweep proves game 12 is also live at 1c,
        and SHORT-CIRCUITS there (2 polls, not 31)."""
        def responder(frame):
            if frame == b"\x01\xb2":
                return b""
            dec = frame[3]                      # denom byte of the B0 poll
            games = [12] if dec == 0x01 else []
            return _b0_response(dec, 0x56, _lp56_payload(games))
        t = FakeTransport(responder)
        g = denom_disable_guard(t, ADDR, PROTO, 12, 0x02, sleep=no_sleep)
        assert g["allow"] is True and g["basis"] == "B0+56"
        assert g["otherDenom"] == 0x01
        assert len(t.sent) == 2                 # B2 + one B0+56

    def test_fallback_sweep_refuses_when_it_is_the_last_denom(self):
        def responder(frame):
            if frame == b"\x01\xb2":
                return b""
            return _b0_response(frame[3], 0x56, _lp56_payload([]))
        g = _guard(responder)
        assert g["allow"] is False and g["basis"] == "B0+56"
        assert "no active denomination" in g["detail"]

    def test_no_evidence_refuses(self):
        """Silence everywhere: we cannot PROVE the write is safe, and an
        unprovable write against the owner's hard rule is not one we make."""
        g = _guard(lambda f: b"")
        assert g["allow"] is False and g["basis"] == "none"
        assert "cannot be shown safe" in g["detail"]

    def test_game_0000_asks_whether_play_survives(self):
        """game 0000 = THE GAMING MACHINE (§2.2.2.3), not a member of the
        enabled-games list — so the sweep asks "does the player still have ANY
        game at another denomination?"."""
        def responder(frame):
            if frame == b"\x01\xb2":
                return b""
            dec = frame[3]
            return _b0_response(dec, 0x56,
                                _lp56_payload([12] if dec == 0x01 else []))
        g = denom_disable_guard(FakeTransport(responder), ADDR, PROTO,
                                0, 0x02, sleep=no_sleep)
        assert g["allow"] is True and g["otherDenom"] == 0x01

    def test_guard_never_polls_the_target_denomination(self):
        def responder(frame):
            if frame == b"\x01\xb2":
                return b""
            return _b0_response(frame[3], 0x56, _lp56_payload([]))
        t = FakeTransport(responder)
        denom_disable_guard(t, ADDR, PROTO, 12, 0x02, sleep=no_sleep)
        swept = [f[3] for f in t.sent if len(f) > 3 and f[1] == 0xB0]
        assert 0x02 not in swept
        assert 0x00 not in swept        # 00 is the selector, not a denom


# ---------------------------------------------------------------------------
# FIX 6 — bare LP B5
# ---------------------------------------------------------------------------

def _b5_frame(game=12, max_bet=200, group=0, levels=b"\x00\x00\x00\x00",
              name=b"JACKPOT PARTY", paytable=b"42B19D", categories=1,
              address=ADDR, extra=b""):
    payload = (int(f"{game:04d}", 16).to_bytes(2, "big")
               + int(f"{max_bet:04d}", 16).to_bytes(2, "big")
               + bytes([group]) + levels
               + bytes([len(name)]) + name
               + bytes([len(paytable)]) + paytable
               + int(f"{categories:04d}", 16).to_bytes(2, "big")
               + extra)
    body = bytes([address, 0xB5, len(payload)]) + payload
    return _frame(body.hex())


class TestExtendedGameInfo:
    def test_poll_is_type_m_with_a_game_number(self):
        """Table 7.23a: [addr][B5][game 2 BCD][CRC]. Never been sent."""
        assert build_extended_game_info_poll(1, 12) == _frame("01 b5 00 12")
        assert build_extended_game_info_poll(1) == _frame("01 b5 00 00")

    def test_poll_rejects_out_of_range_game(self):
        with pytest.raises(ValueError):
            build_extended_game_info_poll(1, 10000)
        with pytest.raises(ValueError):
            build_extended_game_info_poll(1, True)

    def test_parse_names_and_fields(self):
        """Constructed from Table 7.23b — NOT wire truth. The names are the
        only human-readable game/paytable strings anywhere in SAS."""
        pkt = PROTO.parse_packet(_b5_frame())
        gi = parse_extended_game_info(pkt.data)
        assert gi.game_number == 12
        assert gi.max_bet == 200
        assert gi.game_name == "JACKPOT PARTY"
        assert gi.paytable_name == "42B19D"
        assert gi.wager_categories == 1

    def test_optional_names_may_be_empty(self):
        gi = parse_extended_game_info(
            PROTO.parse_packet(_b5_frame(name=b"", paytable=b"")).data)
        assert gi.game_name == "" and gi.paytable_name == ""

    def test_progressive_level_bitmap_is_lsb_first(self):
        """Table 7.23b: "lsb = nivel 1, msb = nivel 32"; §2.2.3: binary fields
        go LSB byte first. Levels 1, 9 and 32 set."""
        gi = parse_extended_game_info(
            PROTO.parse_packet(_b5_frame(group=7,
                                         levels=b"\x01\x01\x00\x80")).data)
        assert gi.progressive_group == 7
        assert gi.progressive_levels == [1, 9, 32]
        assert gi.progressive_levels_raw == b"\x01\x01\x00\x80"

    def test_trailing_bytes_are_ignored(self):
        """§2.2.3: a host that receives a valid
        variable-length response with MORE data than it expects processes what
        it understands and ignores the rest — forward compatibility."""
        gi = parse_extended_game_info(
            PROTO.parse_packet(_b5_frame(extra=b"\xde\xad\xbe\xef")).data)
        assert gi.game_name == "JACKPOT PARTY"
        assert gi.wager_categories == 1

    def test_lying_length_byte_is_rejected(self):
        with pytest.raises(ValueError):
            parse_extended_game_info(b"\x40\x00\x12\x00\x00\x00\x00\x00"
                                     b"\x00\x00\x00\x00\x00")

    def test_name_overrun_is_rejected(self):
        data = bytes([12, 0x00, 0x12, 0x00, 0x00, 0x00,
                      0x00, 0x00, 0x00, 0x00, 40, 0x41, 0x42])
        with pytest.raises(ValueError):
            parse_extended_game_info(data)
