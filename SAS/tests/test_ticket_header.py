"""
Tests for the C2 ticket-header push: hub reply "ticketData" -> rev-gated
0x7C/0x7D wire write -> snapshot {"appliedRev", "detail"}.

SPEC ADJUDICATION UNDER TEST (SAS 6.02 full text, read 2026-07-09; golden
bytes below are hand-built straight from these tables):

* 0x7C Set Extended Ticket Data — §15.3 Table 15.3a:
      [addr][7C][len][code][dataLen][data]...[crc-lo][crc-hi]
  len = bytes following excluding CRC; elements repeat in any combination.
  Table 15.3c data codes: 00=Location (ASCII 40 max), 01=Address 1 (40),
  02=Address 2 (40), 10=Restricted ticket title (16), 20=Debit ticket
  title (16). Response (Table 15.3b): [addr][7C][flag][crc] — flag 01 =
  valid data received (ACK), 00 = invalid (NACK). "Data set using this
  long poll always takes precedence over any data set using long poll 7D."

* 0x7D Set Ticket Data — §15.4 Table 15.4a (POSITIONAL):
      [addr][7D][len 02-7E][hostID 2 binary][expiration 1 binary]
      [locLen 00-28][loc][a1Len][a1][a2Len][a2][crc]
  Host ID is LSB-first (§2: "All data exchanged in the binary format are
  sent least significant byte (LSB) first"); expiration 00 = never
  expires; a length byte 00 = "do not change"; trailing fields may be
  omitted entirely. Response (Table 15.4b) mirrors 15.3b. Max inner =
  2+1+3*(1+40) = 126 = 0x7E, exactly the table's ceiling.

* The normal CASH ticket title ("CASHOUT TICKET") is NOT host-settable
  anywhere in 6.02 — only the restricted (code 10) and debit (code 20)
  titles are. So the hub's titleCash must be SKIPPED (never mapped onto
  10/20 — those print on the wrong tickets), and the applier says so.

Tiers, mirroring test_sas_host_settings.py:
* UNIT — the builders' golden bytes, the reply-shape junk matrix, the
  TicketHeaderState rev/session gate, apply_ticket_header's 7C/7D
  decision table against a scripted transport, reporter pass-through.
* END-TO-END — offline defer, park defer, apply-on-resume, same-rev
  no-resend, one-attempt-per-rev NACK, and snapshot shape in every
  state: sas_host.main() against a phased stub hub, every transition
  driven by what the satellite actually reports (never by sleeps).
"""

import http.server
import json
import os
import signal
import sys
import tempfile
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import sas_host
from core.hub_ticket_client import HubTicketAuthority
from core.sas_protocol import SASProtocol, sas_crc
from core.sas_ticket_store import DEFAULT_SYSTEM_ID, TicketStore
from core.sas_tito_host import (
    CMD_SET_EXTENDED_TICKET_DATA, CMD_SET_TICKET_DATA,
    TICKET_DATA_LOCATION, TICKET_DATA_ADDRESS1, TICKET_DATA_ADDRESS2,
    TICKET_DATA_TITLE_RESTRICTED, TICKET_DATA_TITLE_DEBIT,
    build_set_extended_ticket_data, build_set_ticket_data,
    parse_ticket_data_flag,
)
from transport.serial.sas_serial import MockSASSerialPort


def _crc(body: bytes) -> bytes:
    return body + sas_crc(body).to_bytes(2, "little")


def _resp(address, command, code):
    return _crc(bytes([address, command, code]))


def _target(rev=1, prop="FUNCINO", line1="1 COLLECTOR WAY",
            line2="GAME ROOM NV", title=None):
    """A parsed-shape target dict (what parse_ticket_data_reply emits)."""
    return {"propName": prop, "line1": line1, "line2": line2,
            "titleCash": title, "rev": rev}


def _elements(frame):
    """Decode a 0x7C frame's (code, data) element stream for asserts."""
    inner = frame[3:-2]
    assert frame[2] == len(inner)
    out, i = [], 0
    while i < len(inner):
        code, n = inner[i], inner[i + 1]
        out.append((code, inner[i + 2:i + 2 + n]))
        i += 2 + n
    return out


# ---------------------------------------------------------------------------
# UNIT — builder golden bytes vs the spec adjudication
# ---------------------------------------------------------------------------

class TestBuildSetExtendedTicketData:
    def test_golden_full_header(self):
        """Table 15.3a/15.3c byte-for-byte: three (code, len, ASCII)
        elements, length = the element bytes, Kermit CRC LSB-first."""
        frame = build_set_extended_ticket_data(
            1, location="FUNCINO", address1="123 FAKE ST",
            address2="RENO NV")
        inner = (bytes([0x00, 7]) + b"FUNCINO"
                 + bytes([0x01, 11]) + b"123 FAKE ST"
                 + bytes([0x02, 7]) + b"RENO NV")
        assert frame == _crc(bytes([0x01, 0x7C, len(inner)]) + inner)
        assert len(inner) == 31

    def test_golden_single_field(self):
        frame = build_set_extended_ticket_data(3, location="FUNCINO")
        inner = bytes([0x00, 7]) + b"FUNCINO"
        assert frame == _crc(bytes([0x03, 0x7C, 9]) + inner)

    def test_titles_use_codes_10_and_20_with_16_char_clamp(self):
        """Table 15.3c: restricted=0x10, debit=0x20, ASCII 16 max."""
        frame = build_set_extended_ticket_data(
            1, restricted_title="PLAYABLE ONLY OR ELSE",  # 21 chars
            debit_title="DEBIT TICKET")
        els = _elements(frame)
        assert els == [
            (TICKET_DATA_TITLE_RESTRICTED, b"PLAYABLE ONLY OR"),   # 16
            (TICKET_DATA_TITLE_DEBIT, b"DEBIT TICKET"),
        ]

    def test_none_fields_are_omitted_entirely(self):
        frame = build_set_extended_ticket_data(1, address2="RENO NV")
        assert _elements(frame) == [(TICKET_DATA_ADDRESS2, b"RENO NV")]

    def test_40_char_clamp_and_ascii_scrub(self):
        frame = build_set_extended_ticket_data(1, location="B" * 45)
        assert _elements(frame) == [(TICKET_DATA_LOCATION, b"B" * 40)]
        frame = build_set_extended_ticket_data(1, location="CAFÉ\tX")
        assert _elements(frame) == [(TICKET_DATA_LOCATION, b"CAF??X")]

    def test_blank_line_is_spaces_not_empty(self):
        """Spec: 'To set a blank line ... set the ASCII text to one or
        more ASCII blanks (hex 20)' — spaces are legal; ''
        (revert-to-default vs do-not-change ambiguity) is refused."""
        frame = build_set_extended_ticket_data(1, location=" ")
        assert _elements(frame) == [(TICKET_DATA_LOCATION, b"\x20")]
        try:
            build_set_extended_ticket_data(1, location="")
            assert False, "empty '' must raise"
        except ValueError:
            pass

    def test_empty_string_and_all_none_raise(self):
        for kwargs in ({}, {"location": ""}, {"address1": ""}):
            try:
                build_set_extended_ticket_data(1, **kwargs)
                assert False, f"{kwargs} must raise"
            except ValueError:
                pass


class TestBuildSetTicketData:
    def test_golden_full_positional_layout(self):
        """Table 15.4a byte-for-byte: hostID LSB-first (§2 binary rule),
        expiration, then [len][text] x3."""
        frame = build_set_ticket_data(
            1, host_id=0x0203, expiration_days=5, location="FUNCINO",
            address1="123 FAKE ST", address2="RENO NV")
        inner = (b"\x03\x02"            # host ID 0x0203 LSB-first
                 + b"\x05"              # 5-day expiration
                 + bytes([7]) + b"FUNCINO"
                 + bytes([11]) + b"123 FAKE ST"
                 + bytes([7]) + b"RENO NV")
        assert frame == _crc(bytes([0x01, 0x7D, len(inner)]) + inner)

    def test_mid_stream_none_is_len_00_do_not_change(self):
        frame = build_set_ticket_data(
            1, host_id=1, expiration_days=0, location="FUNCINO",
            address1=None, address2="RENO NV")
        inner = (b"\x01\x00" + b"\x00"
                 + bytes([7]) + b"FUNCINO"
                 + b"\x00"                        # address1: do not change
                 + bytes([7]) + b"RENO NV")
        assert frame == _crc(bytes([0x01, 0x7D, len(inner)]) + inner)

    def test_trailing_none_fields_are_dropped(self):
        frame = build_set_ticket_data(1, host_id=1, expiration_days=0,
                                      location="X")
        inner = b"\x01\x00" + b"\x00" + bytes([1]) + b"X"
        assert frame == _crc(bytes([0x01, 0x7D, 0x05]) + inner)

    def test_max_frame_hits_the_tables_7e_ceiling_exactly(self):
        """Cross-check of the adjudication: 2+1+3*(1+40) = 126 = 0x7E,
        the exact upper bound Table 15.4a gives for the length byte."""
        frame = build_set_ticket_data(1, host_id=1, expiration_days=0,
                                      location="A" * 40,
                                      address1="B" * 40,
                                      address2="C" * 40)
        assert frame[2] == 0x7E

    def test_validation_errors(self):
        cases = [
            dict(host_id=-1, location="X"),
            dict(host_id=0x10000, location="X"),
            dict(expiration_days=-1, location="X"),
            dict(expiration_days=256, location="X"),
            dict(),                                # no text at all
            dict(location=""),                     # ambiguous empty
        ]
        for kwargs in cases:
            try:
                build_set_ticket_data(1, **kwargs)
                assert False, f"{kwargs} must raise"
            except ValueError:
                pass


class TestParseTicketDataFlag:
    def test_ack_and_nack(self):
        assert parse_ticket_data_flag(b"\x01") is True
        assert parse_ticket_data_flag(b"\x00") is False

    def test_structural_lies_raise(self):
        for junk in (b"", b"\x02", b"\xff", b"\x01\x00", b"\x00\x00"):
            try:
                parse_ticket_data_flag(junk)
                assert False, f"{junk!r} must raise"
            except ValueError:
                pass


# ---------------------------------------------------------------------------
# UNIT — reply-shape junk matrix (the C2 exact-shape guard)
# ---------------------------------------------------------------------------

class TestParseTicketDataReply:
    GOOD = {"propName": "FUNCINO", "line1": "1 COLLECTOR WAY",
            "line2": "GAME ROOM NV", "titleCash": "FUN MONEY", "rev": 3}

    def test_valid_full_shape(self):
        out = sas_host.parse_ticket_data_reply(dict(self.GOOD))
        assert out == {"propName": "FUNCINO", "line1": "1 COLLECTOR WAY",
                       "line2": "GAME ROOM NV", "titleCash": "FUN MONEY",
                       "rev": 3}

    def test_title_null_absent_or_blank_all_mean_no_title(self):
        for variant in (None, "", "   "):
            d = dict(self.GOOD, titleCash=variant)
            assert sas_host.parse_ticket_data_reply(d)["titleCash"] is None
        d = dict(self.GOOD)
        del d["titleCash"]
        assert sas_host.parse_ticket_data_reply(d)["titleCash"] is None

    def test_strings_stripped_and_clamped_to_64(self):
        d = dict(self.GOOD, propName="  FUNCINO  ", line1="A" * 100)
        out = sas_host.parse_ticket_data_reply(d)
        assert out["propName"] == "FUNCINO"
        assert out["line1"] == "A" * 64

    def test_rev_zero_and_blank_lines_are_legal(self):
        d = dict(self.GOOD, line1="", line2="", rev=0, titleCash=None)
        out = sas_host.parse_ticket_data_reply(d)
        assert out["rev"] == 0 and out["line1"] == "" and out["line2"] == ""

    def test_junk_matrix_returns_none(self):
        junk = [
            None, 5, "x", [], True,                     # not a dict
            {},                                          # everything absent
            dict(self.GOOD, propName=""),                # unset prop
            dict(self.GOOD, propName="   "),
            dict(self.GOOD, propName=7),
            dict(self.GOOD, propName=None),
            dict(self.GOOD, line1=None),                 # contract: str
            dict(self.GOOD, line1=5),
            dict(self.GOOD, line2=None),
            dict(self.GOOD, titleCash=5),
            dict(self.GOOD, titleCash=[]),
            dict(self.GOOD, titleCash=True),
            dict(self.GOOD, rev=None),
            dict(self.GOOD, rev=True),                   # bool is not a rev
            dict(self.GOOD, rev="3"),
            dict(self.GOOD, rev=-1),
            dict(self.GOOD, rev=1.5),
        ]
        d = dict(self.GOOD)
        del d["line1"]
        junk.append(d)
        d = dict(self.GOOD)
        del d["rev"]
        junk.append(d)
        for j in junk:
            assert sas_host.parse_ticket_data_reply(j) is None, j


# ---------------------------------------------------------------------------
# UNIT — TicketHeaderState rev/session gating
# ---------------------------------------------------------------------------

class TestTicketHeaderStateGating:
    def _fed(self, rev=1):
        st = sas_host.TicketHeaderState()
        st.on_reply(self.reply(rev))
        return st

    @staticmethod
    def reply(rev):
        return {"propName": "FUNCINO", "line1": "", "line2": "",
                "titleCash": None, "rev": rev}

    def test_initial_state_nothing_due_snapshot_shape(self):
        st = sas_host.TicketHeaderState()
        assert st.due() is None
        snap = st.snapshot()
        assert snap["appliedRev"] is None
        assert isinstance(snap["detail"], str) and snap["detail"]

    def test_junk_reply_never_arms(self):
        st = sas_host.TicketHeaderState()
        for junk in (None, "x", {"rev": 1}, {"propName": "", "line1": "",
                                             "line2": "", "rev": 1}):
            st.on_reply(junk)
        assert st.target is None and st.due() is None

    def test_valid_reply_arms_and_success_disarms(self):
        st = self._fed(rev=1)
        due = st.due()
        assert due is not None and due["rev"] == 1
        st.mark_attempted(1)
        st.record_result(1, True, "applied")
        assert st.due() is None
        assert st.snapshot() == {"appliedRev": 1, "detail": "applied"}

    def test_same_rev_never_resends(self):
        """The hub echoes the same rev every REPORT_SEC forever — after
        one applied rev the gate must stay closed no matter how many
        echoes (or rejoins) follow."""
        st = self._fed(rev=1)
        st.mark_attempted(1)
        st.record_result(1, True, "applied")
        for _ in range(5):
            st.on_reply(self.reply(1))
            assert st.due() is None
        st.on_online()                       # rejoin: still applied
        assert st.due() is None

    def test_failure_burns_the_attempt_until_rev_bump(self):
        st = self._fed(rev=2)
        st.mark_attempted(2)
        st.record_result(2, False, "silence on both")
        for _ in range(5):
            st.on_reply(self.reply(2))
            assert st.due() is None          # no hot loop
        assert st.snapshot()["appliedRev"] is None
        st.on_reply(self.reply(3))           # rev bump re-arms
        assert st.due()["rev"] == 3

    def test_rejoin_rearms_exactly_one_attempt_for_a_failed_rev(self):
        st = self._fed(rev=2)
        st.mark_attempted(2)
        st.record_result(2, False, "silence")
        assert st.due() is None
        st.on_online()                       # new online session
        assert st.due()["rev"] == 2
        st.mark_attempted(2)
        assert st.due() is None              # and only one

    def test_mark_attempted_runs_before_the_wire(self):
        """A raising apply must not re-run: attempted is burned even if
        record_result never happens."""
        st = self._fed(rev=1)
        st.mark_attempted(1)
        assert st.due() is None


# ---------------------------------------------------------------------------
# UNIT — apply_ticket_header decision table (scripted transport)
# ---------------------------------------------------------------------------

class ScriptedTransport:
    def __init__(self, replies):
        self.replies = list(replies)
        self.frames = []

    def transact(self, frame, max_bytes=256):
        self.frames.append(bytes(frame))
        return self.replies.pop(0) if self.replies else b""


class TestApplyTicketHeader:
    def _apply(self, replies, target=None, **kw):
        tp = ScriptedTransport(replies)
        sleeps = []
        ok, detail = sas_host.apply_ticket_header(
            tp, 1, target or _target(), protocol=SASProtocol(),
            sleep=sleeps.append, **kw)
        return ok, detail, tp, sleeps

    def test_7c_ack_is_the_whole_story(self):
        ok, detail, tp, sleeps = self._apply([_resp(1, 0x7C, 0x01)])
        assert ok is True and "0x7C" in detail
        assert len(tp.frames) == 1
        assert tp.frames[0] == build_set_extended_ticket_data(
            1, location="FUNCINO", address1="1 COLLECTOR WAY",
            address2="GAME ROOM NV")
        assert sleeps == []                     # no second poll, no pacing

    def test_7c_nack_never_escalates_to_7d(self):
        ok, detail, tp, _ = self._apply([_resp(1, 0x7C, 0x00)])
        assert ok is False and "NACK" in detail
        assert len(tp.frames) == 1              # the machine refused; stop

    def test_7c_silence_falls_back_to_7d_ack_with_pacing(self):
        ok, detail, tp, sleeps = self._apply([b"", _resp(1, 0x7D, 0x01)])
        assert ok is True and "0x7D" in detail
        assert [f[1] for f in tp.frames] == [0x7C, 0x7D]
        assert tp.frames[1] == build_set_ticket_data(
            1, host_id=DEFAULT_SYSTEM_ID, expiration_days=0,
            location="FUNCINO", address1="1 COLLECTOR WAY",
            address2="GAME ROOM NV")
        assert sleeps == [sas_host.SAS_POLL_FLOOR]

    def test_both_silent_is_an_honest_failure(self):
        ok, detail, tp, _ = self._apply([b"", b""])
        assert ok is False and "silence on both" in detail
        assert [f[1] for f in tp.frames] == [0x7C, 0x7D]

    def test_corrupt_foreign_and_malformed_replies_are_silence(self):
        """The classify rule: bad CRC, a foreign address, a wrong command
        echo, or an out-of-range flag byte never count as an ACK — each
        falls through to the 7D fallback."""
        good = _resp(1, 0x7C, 0x01)
        corrupt = good[:-1] + bytes([good[-1] ^ 0xFF])
        for first in (corrupt, _resp(2, 0x7C, 0x01), _resp(1, 0x7E, 0x01),
                      _resp(1, 0x7C, 0x07), _crc(bytes([1, 0x7C, 1, 0]))):
            ok, detail, tp, _ = self._apply([first, b""])
            assert ok is False, first.hex()
            assert [f[1] for f in tp.frames] == [0x7C, 0x7D]

    def test_7d_nack_reported(self):
        ok, detail, tp, _ = self._apply([b"", _resp(1, 0x7D, 0x00)])
        assert ok is False and "0x7D NACK" in detail

    def test_title_cash_is_skipped_and_said_so(self):
        """titleCash has NO SAS mapping — the frame must carry only codes
        00/01/02 (never 10/20) and the verdict must admit the skip."""
        ok, detail, tp, _ = self._apply(
            [_resp(1, 0x7C, 0x01)], target=_target(title="FUN MONEY"))
        assert ok is True
        assert "titleCash skipped" in detail
        codes = [c for c, _ in _elements(tp.frames[0])]
        assert codes == [TICKET_DATA_LOCATION, TICKET_DATA_ADDRESS1,
                         TICKET_DATA_ADDRESS2]

    def test_blank_lines_are_omitted_not_pushed(self):
        """C1: never push empty over existing machine config — blank
        line1/line2 vanish from the frame entirely."""
        ok, _, tp, _ = self._apply(
            [_resp(1, 0x7C, 0x01)], target=_target(line1="", line2=""))
        assert ok is True
        assert _elements(tp.frames[0]) == [(TICKET_DATA_LOCATION,
                                            b"FUNCINO")]


# ---------------------------------------------------------------------------
# UNIT — HubReporter._take_commands "ticketData" pass-through
# ---------------------------------------------------------------------------

class TestTakeCommandsTicketData:
    def _reporter(self, state):
        return sas_host.HubReporter("http://127.0.0.1:9", "unit", "(mock)",
                                    1, None, None, None, None,
                                    ticket_header=state)

    def test_raw_value_reaches_the_state(self):
        st = sas_host.TicketHeaderState()
        rep = self._reporter(st)
        rep._take_commands(json.dumps({"ticketData": {
            "propName": "FUNCINO", "line1": "", "line2": "",
            "titleCash": None, "rev": 4}}).encode())
        assert st.target is not None and st.target["rev"] == 4

    def test_absent_key_and_junk_change_nothing(self):
        st = sas_host.TicketHeaderState()
        rep = self._reporter(st)
        rep._take_commands(b'{"ok": true, "commands": []}')
        rep._take_commands(b'{"ticketData": "junk"}')
        rep._take_commands(b'{"ticketData": {"rev": true}}')
        rep._take_commands(b"not json")
        assert st.target is None

    def test_raising_state_is_swallowed_commands_still_parse(self):
        class Boom:
            def on_reply(self, value):
                raise RuntimeError("scripted ingest blow-up")

        rep = self._reporter(Boom())
        rep._take_commands(json.dumps(
            {"ticketData": {"propName": "X"},
             "commands": [{"id": "c1", "type": "t"}]}).encode())
        assert [c["id"] for c in rep.pending_commands] == ["c1"]

    def test_no_state_wired_is_fine(self):
        rep = sas_host.HubReporter("http://127.0.0.1:9", "unit", "(mock)",
                                   1, None, None, None, None)
        rep._take_commands(b'{"ticketData": {"propName": "X"}}')
        assert not rep.pending_commands


# ---------------------------------------------------------------------------
# END-TO-END — offline defer / park defer / apply / no-resend / NACK
# ---------------------------------------------------------------------------

def _td(rev):
    return {"propName": "FUNCINO", "line1": "1 COLLECTOR WAY",
            "line2": "GAME ROOM NV", "titleCash": "FUN MONEY", "rev": rev}


class TicketHeaderMachine:
    """Boots DARK (answers nothing -> the satellite stays offline), then
    answers general polls with 'no activity' and 0x7C/0x7D with the
    Table 15.3b/15.4b flag response (scripted ACK/NACK). Everything else
    — meter type-R reads, AFT polls — is silence, like a machine that
    never learned them."""

    def __init__(self, address=1):
        self.address = address
        self.dark = True
        self.ack = True
        self.ticket_frames = []     # (cmd, frame) for CRC-valid 7C/7D

    def __call__(self, frame, wakeup):
        if self.dark or not wakeup[0]:
            return b""
        if len(frame) == 1:                       # general poll
            if (frame[0] & 0x7F) != self.address:
                return b""
            return b"\x00"
        if frame[0] != self.address or len(frame) < 4:
            return b""
        body, crc = frame[:-2], frame[-2:]
        if sas_crc(body).to_bytes(2, "little") != crc:
            return b""                            # type-R reads: silence
        cmd = frame[1]
        if cmd in (CMD_SET_EXTENDED_TICKET_DATA, CMD_SET_TICKET_DATA):
            self.ticket_frames.append((cmd, bytes(frame)))
            return _resp(self.address, cmd, 0x01 if self.ack else 0x00)
        return b""


class _PhasedHub(http.server.HTTPServer):
    """Reply state machine over the satellite's own reports:

      offline  -> machine dark, serve rev 1: the satellite must NOT try
                  the wire (frames sample == 0), then wake the machine;
      apply    -> serve rev 1 until a report shows appliedRev 1;
      soak     -> 4 more rev-1 echoes: the frame count must FREEZE;
      park_arm -> sasEnabled false, NO ticketData, until the polls
                  counter freezes (the poll thread is provably parked);
      park_td  -> still parked, serve rev 2 for 4 reports: no frames;
      resume   -> sasEnabled true + rev 2 until appliedRev 2, then flip
                  the machine to NACK;
      nack     -> serve rev 3 until one attempt hits the wire;
      nacksoak -> 4 more rev-3 echoes: exactly that ONE attempt, then
                  done (SIGTERM main())."""

    def setup_state(self, machine):
        self.lock = threading.Lock()
        self.machine = machine
        self.reports = []                # [(phase, report, frames), ...]
        self.phase = "offline"
        self.offline_seen = 0
        self.park_polls = None
        self.park_frozen = 0
        self.park_td_seen = 0
        self.soak_left = 4
        self.nacksoak_left = 4
        self.samples = {}
        self.park_applied = "unset"
        self.final_td = None
        self.signaled = False
        self.t0 = time.monotonic()

    def reply_for(self, rep):
        td = rep.get("ticketData") or {}
        frames = len(self.machine.ticket_frames)
        if time.monotonic() - self.t0 > 20 and self.phase != "done":
            self.phase = "done"           # watchdog: never hang the suite
        if self.phase == "offline":
            if rep.get("online") is False and "ticketData" in rep:
                self.offline_seen += 1
            if self.offline_seen >= 4:
                self.samples["after_offline"] = frames
                self.machine.dark = False
                self.phase = "apply"
            else:
                return {"ok": True, "sasEnabled": True, "ticketData": _td(1)}
        if self.phase == "apply":
            if td.get("appliedRev") == 1:
                self.samples["after_apply1"] = frames
                self.phase = "soak"
            else:
                return {"ok": True, "sasEnabled": True, "ticketData": _td(1)}
        if self.phase == "soak":
            if self.soak_left > 0:
                self.soak_left -= 1
                return {"ok": True, "sasEnabled": True, "ticketData": _td(1)}
            self.samples["after_soak"] = frames
            self.phase = "park_arm"
        if self.phase == "park_arm":
            if rep.get("sasEnabled") is False:
                if rep.get("polls") == self.park_polls:
                    self.park_frozen += 1
                self.park_polls = rep.get("polls")
            if self.park_frozen >= 2:
                self.samples["park_engaged"] = frames
                self.phase = "park_td"
            else:
                return {"ok": True, "sasEnabled": False}
        if self.phase == "park_td":
            if self.park_td_seen >= 4:
                self.samples["after_park"] = frames
                self.park_applied = td.get("appliedRev")
                self.phase = "resume"
            else:
                self.park_td_seen += 1
                return {"ok": True, "sasEnabled": False,
                        "ticketData": _td(2)}
        if self.phase == "resume":
            if td.get("appliedRev") == 2:
                self.samples["after_apply2"] = frames
                self.machine.ack = False
                self.phase = "nack"
            else:
                return {"ok": True, "sasEnabled": True, "ticketData": _td(2)}
        if self.phase == "nack":
            if frames > self.samples["after_apply2"]:
                self.phase = "nacksoak"
            else:
                return {"ok": True, "sasEnabled": True, "ticketData": _td(3)}
        if self.phase == "nacksoak":
            if self.nacksoak_left > 0:
                self.nacksoak_left -= 1
                return {"ok": True, "sasEnabled": True, "ticketData": _td(3)}
            self.samples["after_nacksoak"] = frames
            self.final_td = td
            self.phase = "done"
        if not self.signaled:
            self.signaled = True
            os.kill(os.getpid(), signal.SIGTERM)
        return {"ok": True}


class _HubHandler(http.server.BaseHTTPRequestHandler):
    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(n)
        srv = self.server
        if self.path != "/api/sas/report":
            self.send_response(404)
            self.end_headers()
            return
        try:
            rep = json.loads(body)
        except ValueError:
            rep = {}
        with srv.lock:
            srv.reports.append((srv.phase, rep,
                                len(srv.machine.ticket_frames)))
            reply = srv.reply_for(rep)
        payload = json.dumps(reply).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, *args):              # keep pytest output clean
        pass


_CACHE = {}


def _run_ticket_channel():
    """Run sas_host.main() once against the phased hub and memoize; every
    e2e test asserts on this single run (~2 s)."""
    if _CACHE:
        return _CACHE

    machine = TicketHeaderMachine(address=1)
    port = MockSASSerialPort(machine)
    tmp = tempfile.mkdtemp(prefix="sas_ticket_header_test_")

    httpd = _PhasedHub(("127.0.0.1", 0), _HubHandler)
    httpd.setup_state(machine)
    server_thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    server_thread.start()

    captured = []
    _RBase = sas_host.HubReporter

    class _CapturingReporter(_RBase):
        def __init__(self, *a, **k):
            super().__init__(*a, **k)
            captured.append(self)

    saved_signals = {s: signal.getsignal(s)
                     for s in (signal.SIGTERM, signal.SIGINT)}
    saved = {n: getattr(sas_host, n) for n in
             ("REPORT_SEC", "HubReporter", "open_port",
              "TicketStore", "HubTicketAuthority")}
    saved_argv = sys.argv
    backstop = threading.Timer(25.0,
                               lambda: os.kill(os.getpid(), signal.SIGTERM))
    backstop.daemon = True
    try:
        sas_host.REPORT_SEC = 0.05
        sas_host.HubReporter = _CapturingReporter
        sas_host.open_port = lambda path, mock, address, protocol: port
        sas_host.TicketStore = lambda: TicketStore(
            path=os.path.join(tmp, "tickets.json"))
        sas_host.HubTicketAuthority = lambda hub, smib, local: \
            HubTicketAuthority(hub, smib, local,
                               journal_path=os.path.join(tmp, "journal.json"),
                               start_sync_thread=False)
        sys.argv = ["sas_host.py", "--mock", "--interval", "0.002",
                    "--hub", f"http://127.0.0.1:{httpd.server_address[1]}",
                    "--smib-id", "pytest-ticket-smib"]
        backstop.start()
        sas_host.main()
    finally:
        backstop.cancel()
        sys.argv = saved_argv
        for name, value in saved.items():
            setattr(sas_host, name, value)
        for sig, handler in saved_signals.items():
            signal.signal(sig, handler)
        if captured:
            captured[0].stop = True
        time.sleep(0.12)                       # let a final report land
        httpd.shutdown()
        server_thread.join(timeout=2)

    with httpd.lock:
        reports = list(httpd.reports)
    _CACHE.update(machine=machine, hub=httpd, reports=reports,
                  samples=dict(httpd.samples))
    return _CACHE


class TestTicketHeaderEndToEnd:
    def test_run_completed_all_phases(self):
        run = _run_ticket_channel()
        assert run["hub"].phase == "done"
        for key in ("after_offline", "after_apply1", "after_soak",
                    "park_engaged", "after_park", "after_apply2",
                    "after_nacksoak"):
            assert key in run["samples"], key

    def test_offline_defers_no_wire_attempt(self):
        """Four reports rode with a pending rev while the machine was
        dark — zero 0x7C/0x7D frames reached it (C2: machine-online
        only)."""
        run = _run_ticket_channel()
        assert run["samples"]["after_offline"] == 0
        offline = [rep for ph, rep, _ in run["reports"]
                   if ph == "offline"]
        assert len(offline) >= 4
        assert all(rep["ticketData"]["appliedRev"] is None
                   for rep in offline)

    def test_apply_on_first_online_session(self):
        run = _run_ticket_channel()
        assert run["samples"]["after_apply1"] == 1
        cmd, frame = run["machine"].ticket_frames[0]
        assert cmd == CMD_SET_EXTENDED_TICKET_DATA
        assert b"FUNCINO" in frame

    def test_same_rev_never_resends(self):
        """Rev 1 kept echoing for 4+ reports after the apply — the frame
        count froze at 1."""
        run = _run_ticket_channel()
        assert run["samples"]["after_soak"] == \
            run["samples"]["after_apply1"] == 1

    def test_park_defers_pending_rev(self):
        """Rev 2 arrived while the poll thread was provably parked (polls
        counter frozen): no frames moved, appliedRev stayed 1; the apply
        happened only after resume."""
        run = _run_ticket_channel()
        assert run["samples"]["park_engaged"] == 1
        assert run["samples"]["after_park"] == 1
        assert run["hub"].park_applied == 1
        assert run["samples"]["after_apply2"] == 2

    def test_nack_gets_exactly_one_attempt_and_an_honest_verdict(self):
        """The machine NACKed rev 3: one 0x7C attempt (no 0x7D — a NACK
        is understanding, not silence), appliedRev pinned at 2, and the
        snapshot detail says NACK."""
        run = _run_ticket_channel()
        assert run["samples"]["after_nacksoak"] == \
            run["samples"]["after_apply2"] + 1 == 3
        assert all(cmd == CMD_SET_EXTENDED_TICKET_DATA
                   for cmd, _ in run["machine"].ticket_frames)
        final = run["hub"].final_td
        assert final["appliedRev"] == 2
        assert "NACK" in final["detail"]

    def test_snapshot_shape_stable_in_every_state(self):
        """C2/C4: every report — offline, live, parked — carries
        ticketData as {"appliedRev": int|None, "detail": str}."""
        reports = [rep for _, rep, _ in _run_ticket_channel()["reports"]]
        assert reports
        for rep in reports:
            td = rep["ticketData"]
            assert set(td) == {"appliedRev", "detail"}
            assert td["appliedRev"] is None or isinstance(
                td["appliedRev"], int)
            assert isinstance(td["detail"], str)
