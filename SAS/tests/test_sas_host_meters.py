"""
Tests for the satellite's expanded meter poll rotation (house-economy design
§A2/§E): the 0x0F composite + 0x46 bills-in sweep beside 0x1A, the composite
FLATTENED satellite-side into scalar report keys (the hub's report ingest
clamps non-scalar meter values to stringified garbage), and every counter
passing through RAW — the HUB owns denom scaling, first-sight baselining and
the 8-digit BCD rollover rule; the satellite never does wrap math.

Same mock-transport pattern as test_sas_poller: a scripted machine behind
MockSASSerialPort. The sweep path is long-poll-only, so a simple frame
responder (no exception-FIFO / implied-ACK modelling) is enough here — the
ACK semantics are covered by test_sas_poller.
"""

import collections
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from sas_host import (COMPOSITE_METER_KEYS, POLLED_METERS, HubReporter,
                      ingest_meter_reading, make_on_meter, meter_report_key)
from core.sas_meters import int_to_bcd
from core.sas_poller import SASPoller
from core.sas_protocol import sas_crc
from core.sas_ticket_store import TicketStore
from transport.serial.sas_serial import MockSASSerialPort

ADDR = 1

#: What one full sweep of POLLED_METERS lands in the report, given the
#: MeterMachine defaults below — the REPORT KEY CONTRACT, spelled out.
EXPECTED_REPORT_METERS = {
    "0x1A": 500,
    "0x46": 2000,
    "cancelled": 3,
    "coinIn": 150,
    "coinOut": 75,
    "totalDrop": 100,
    "jackpot": 12,
    "gamesPlayed": 42,
}


def crc_frame(address, command, data):
    body = bytes([address, command]) + data
    return body + sas_crc(body).to_bytes(2, "little")


class MeterMachine:
    """Answers general polls with idle 0x00 and the three sweep long polls
    with CRC-valid BCD meter frames; everything else gets the spec's silence
    (§2.7.4 — unsupported command)."""

    def __init__(self, address=ADDR):
        self.address = address
        self.cancelled = 3
        self.coin_in = 150
        self.coin_out = 75
        self.drop = 100
        self.jackpot = 12
        self.games = 42
        self.credits = 500
        self.bills = 2000
        self.answer_0f = True         # False = machine snubs the composite
        self.answer_46 = True

    def __call__(self, frame, wakeup):
        if not wakeup[0]:
            return b""
        if len(frame) == 1:                        # general poll
            return b"\x00" if (frame[0] & 0x7F) == self.address else b""
        if frame[0] != self.address:
            return b""
        cmd = frame[1]
        if cmd == 0x0F and self.answer_0f:
            data = b"".join(int_to_bcd(v, 4) for v in (
                self.cancelled, self.coin_in, self.coin_out,
                self.drop, self.jackpot, self.games))
            return crc_frame(self.address, 0x0F, data)
        if cmd == 0x1A:
            return crc_frame(self.address, 0x1A, int_to_bcd(self.credits, 4))
        if cmd == 0x46 and self.answer_46:
            return crc_frame(self.address, 0x46, int_to_bcd(self.bills, 4))
        return b""


def manual_clock():
    t = {"now": 0.0}

    def clock():
        return t["now"]

    clock.advance = lambda dt: t.__setitem__("now", t["now"] + dt)
    return clock


def fresh_stats():
    return {"polls": 0, "events": 0, "meter_changes": 0, "last_meters": {}}


def make_rig(machine=None):
    """(machine, stats, clock, poller) wired exactly like sas_host.main():
    POLLED_METERS + the real make_on_meter production callback."""
    machine = machine or MeterMachine()
    stats = fresh_stats()
    clock = manual_clock()
    poller = SASPoller(MockSASSerialPort(machine), address=ADDR,
                       meters=list(POLLED_METERS), meter_interval=5.0,
                       clock=clock, on_meter=make_on_meter(stats))
    return machine, stats, clock, poller


class TestPolledMeterSet:
    def test_design_poll_set(self):
        # design §A2 recommendation: keep 0x1A, add the 0x0F composite
        # (jackpot only rides composites) and 0x46 bills-in.
        assert [s.command for s in POLLED_METERS] == [0x1A, 0x0F, 0x46]

    def test_lenient_decode_never_raises(self):
        specs = {s.command: s for s in POLLED_METERS}
        # wrong-length (but CRC-valid) answers yield None -> sweep skips,
        # poll loop lives
        assert specs[0x0F].decode(b"\x00" * 23) is None
        assert specs[0x46].decode(b"\x01") is None
        # correct shapes parse to the sas_meters labels / scalar
        good = b"".join(int_to_bcd(v, 4) for v in (1, 2, 3, 4, 5, 6))
        assert specs[0x0F].decode(good) == {
            "cancelled_credits": 1, "coin_in": 2, "coin_out": 3,
            "total_drop": 4, "jackpot": 5, "games_played": 6}
        assert specs[0x46].decode(int_to_bcd(2000, 4)) == 2000

    def test_composite_labels_all_mapped_and_clamp_safe(self):
        # every 0x0F component has a flatten target <=16 chars (hub clamps
        # meter keys to 16) that never collides with the hex-key namespace
        assert set(COMPOSITE_METER_KEYS) == {
            "cancelled_credits", "coin_in", "coin_out", "total_drop",
            "jackpot", "games_played"}
        for label in COMPOSITE_METER_KEYS.values():
            assert len(label) <= 16
            assert not label.startswith("0x")


class TestSweepFlattenSnapshot:
    def test_parse_flatten_snapshot_path(self, tmp_path):
        machine, stats, clock, poller = make_rig()
        poller.poll_once()                       # first sweep fires at t=0

        lm = stats["last_meters"]
        assert lm[(ADDR, 0x1A)] == 500
        assert lm[(ADDR, 0x46)] == 2000
        assert lm[(ADDR, "coinIn")] == 150
        assert lm[(ADDR, "coinOut")] == 75
        assert lm[(ADDR, "cancelled")] == 3
        assert lm[(ADDR, "totalDrop")] == 100
        assert lm[(ADDR, "jackpot")] == 12
        assert lm[(ADDR, "gamesPlayed")] == 42
        # the composite dict itself never lands — every stored value is a
        # scalar int (the hub stringifies anything else to garbage)
        assert all(isinstance(v, int) for v in lm.values())
        # first sight counts silently: 8 fresh keys = 8 meterChanges
        assert stats["meter_changes"] == 8

        reporter = HubReporter(
            "http://hub.test", "smib-test", "(mock)", ADDR, poller, stats,
            collections.deque(maxlen=20),
            TicketStore(str(tmp_path / "tickets.json")))
        meters = reporter.snapshot()["meters"]
        assert meters == EXPECTED_REPORT_METERS
        assert all(len(k) <= 16 for k in meters)

    def test_unchanged_resweep_reports_free(self):
        machine, stats, clock, poller = make_rig()
        poller.poll_once()
        assert stats["meter_changes"] == 8
        clock.advance(5.0)
        poller.poll_once()                       # same values re-swept
        assert stats["meter_changes"] == 8       # no phantom changes
        clock.advance(5.0)
        machine.coin_in = 151                    # one component moves
        poller.poll_once()
        assert stats["meter_changes"] == 9
        assert stats["last_meters"][(ADDR, "coinIn")] == 151

    def test_composite_silence_leaves_scalars(self, tmp_path):
        # §2.7.4: a machine that doesn't implement 0x0F/0x46 answers dead
        # silence — the sweep skips them, 0x1A keeps flowing (BENCH #5's
        # honest-degradation case)
        machine = MeterMachine()
        machine.answer_0f = False
        machine.answer_46 = False
        machine, stats, clock, poller = make_rig(machine)
        poller.poll_once()
        assert stats["last_meters"] == {(ADDR, 0x1A): 500}
        reporter = HubReporter(
            "http://hub.test", "smib-test", "(mock)", ADDR, poller, stats,
            collections.deque(maxlen=20),
            TicketStore(str(tmp_path / "tickets.json")))
        assert reporter.snapshot()["meters"] == {"0x1A": 500}

    def test_rollover_shaped_value_passes_raw(self):
        # an 8-digit BCD counter wrapping 99,999,990 -> 25 must pass through
        # RAW: the hub owns the wrap-vs-RAM-clear adjudication (design §B2);
        # the satellite reporting anything but the raw counter would break it
        machine, stats, clock, poller = make_rig()
        machine.coin_in = 99_999_990
        poller.poll_once()
        assert stats["last_meters"][(ADDR, "coinIn")] == 99_999_990
        clock.advance(5.0)
        machine.coin_in = 25                     # wrapped
        poller.poll_once()
        assert stats["last_meters"][(ADDR, "coinIn")] == 25   # raw, no math


class TestIngestUnit:
    def test_flatten_skips_none_and_unknown_labels(self):
        lm = {}
        changes = ingest_meter_reading(lm, ADDR, 0x0F, {
            "coin_in": 100, "coin_out": None, "mystery_meter": 7})
        assert lm == {(ADDR, "coinIn"): 100}
        assert changes == [("coinIn", None, 100)]

    def test_none_component_keeps_last_good_value(self):
        # a corrupt-BCD component (parser yields None) must not poison the
        # last good reading — the hub would see a phantom reset otherwise
        lm = {(ADDR, "coinOut"): 75}
        assert ingest_meter_reading(lm, ADDR, 0x0F, {"coin_out": None}) == []
        assert lm[(ADDR, "coinOut")] == 75

    def test_scalar_path_unchanged(self):
        lm = {}
        assert ingest_meter_reading(lm, ADDR, 0x1A, 500) == [(0x1A, None, 500)]
        assert ingest_meter_reading(lm, ADDR, 0x1A, 500) == []  # re-report free
        assert ingest_meter_reading(lm, ADDR, 0x1A, 480) == [(0x1A, 500, 480)]
        assert lm == {(ADDR, 0x1A): 480}

    def test_meter_report_key_formats(self):
        assert meter_report_key(0x1A) == "0x1A"
        assert meter_report_key(0x0F) == "0x0F"
        assert meter_report_key("coinIn") == "coinIn"

    def test_make_on_meter_counts_per_component(self):
        stats = fresh_stats()
        on_meter = make_on_meter(stats)
        on_meter(ADDR, 0x0F, {"coin_in": 1, "coin_out": 2, "jackpot": None})
        assert stats["meter_changes"] == 2
        on_meter(ADDR, 0x0F, {"coin_in": 1, "coin_out": 3})   # one moved
        assert stats["meter_changes"] == 3
        assert stats["last_meters"] == {
            (ADDR, "coinIn"): 1, (ADDR, "coinOut"): 3}
