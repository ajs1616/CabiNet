"""
Companion daemon tests: the tap pipeline (debounce -> queue -> report ->
ack-drop) against an in-thread http.server fake hub, plus the PN532 frame
layer against a scripted dummy bus (guards the VERBATIM port of the
live-proven scratchpad driver — checksums and offsets must never drift).

No hardware, no sleeps beyond the server thread: all time is injected
monotonic seconds (the same injected-clock idiom as the SAS watchdog tests).
"""

import http.server
import json
import logging
import sys
import threading
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from companion_host import DEBOUNCE_SEC, CompanionHost
from reader import MockRfidReader, PN532Reader

UID = "6CB16F06"          # the first live card (S50 1K, 2026-07-10)
UID2 = "04A1B2C3"


# ---------------------------------------------------------------------------
# fake hub: real HTTP on 127.0.0.1, records bodies, replies a settable ack
# ---------------------------------------------------------------------------

class _HubHandler(http.server.BaseHTTPRequestHandler):
    def do_POST(self):
        n = int(self.headers.get("Content-Length", 0))
        body = json.loads(self.rfile.read(n) or b"{}")
        self.server.hub_bodies.append((self.path, body))
        reply = json.dumps(self.server.hub_reply).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(reply)))
        self.end_headers()
        self.wfile.write(reply)

    def log_message(self, *args):     # keep pytest output clean
        pass


@pytest.fixture
def hub():
    srv = http.server.HTTPServer(("127.0.0.1", 0), _HubHandler)
    srv.hub_bodies = []
    srv.hub_reply = {"ok": True, "ackTapId": -1}
    srv.url = "http://127.0.0.1:%d" % srv.server_address[1]
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    yield srv
    srv.shutdown()
    srv.server_close()


def make_host(hub_url, script=()):
    return CompanionHost(
        MockRfidReader(script), hub_url, "companion-test",
        g2s_egm="WMS_00:a0:a5:79:2d:a8", sas_smib="smib-bb2",
        sas_address=1, report_sec=1.0)


# ---------------------------------------------------------------------------
# debounce + tapId
# ---------------------------------------------------------------------------

def test_held_card_is_one_tap():
    # Card sighted on 4 consecutive ~5Hz polls = ONE tap.
    h = make_host("http://127.0.0.1:1",
                  script=[(0, UID), (1, UID), (2, UID), (3, UID), (5, UID)])
    taps = [h.poll_reader(now) for now in (0.0, 0.2, 0.4, 0.6)]
    assert [t for t in taps if t] and len(h.taps) == 1
    # Every sighting RE-ARMS the window: 1.9s after the LAST sighting is
    # still the same tap-hold...
    assert h.poll_reader(0.6 + DEBOUNCE_SEC - 0.1) is None
    # ...but a sighting after a 2s+ gap is a fresh tap (the re-tap gesture).
    tap = h.poll_reader(10.0)
    assert tap is not None and len(h.taps) == 2


def test_different_uid_taps_through_immediately():
    h = make_host("http://127.0.0.1:1", script=[(0, UID), (1, UID2)])
    assert h.poll_reader(0.0)["uid"] == UID
    assert h.poll_reader(0.2)["uid"] == UID2       # no debounce across uids


def test_two_alternating_cards_do_not_machine_gun():
    # Two cards in the field at once: PN532 anticollision alternates which
    # target it returns. Per-uid debounce = ONE tap each, not a tap storm
    # at poll rate (which would churn card-out/card-in at the hub).
    h = make_host("http://127.0.0.1:1",
                  script=[(0, UID), (1, UID2), (2, UID), (3, UID2),
                          (4, UID), (5, UID2)])
    taps = [h.poll_reader(now) for now in (0.0, 0.2, 0.4, 0.6, 0.8, 1.0)]
    assert [t["uid"] for t in taps if t] == [UID, UID2]
    assert len(h.taps) == 2


def test_reader_error_freezes_debounce_window():
    # Card parked on the antenna, then reader trouble LONGER than
    # DEBOUNCE_SEC (I2C hiccup + self-heal), then a clean poll: still the
    # SAME tap — a phantom re-tap here would be the card-out gesture.
    errs = [(n, OSError("i2c hiccup")) for n in range(1, 15)]
    h = make_host("http://127.0.0.1:1",
                  script=[(0, UID)] + errs + [(15, UID)])
    assert h.poll_reader(0.0) is not None
    for n in range(1, 15):                 # 0.2..2.8s: errors re-arm windows
        assert h.poll_reader(n * 0.2) is None
    assert h.poll_reader(3.0) is None      # parked card: one tap, no phantom
    assert len(h.taps) == 1


def test_tap_id_monotonic():
    h = make_host("http://127.0.0.1:1",
                  script=[(0, UID), (1, UID2), (2, UID)])
    ids = [h.poll_reader(now)["tapId"] for now in (0.0, 10.0, 20.0)]
    assert ids == [0, 1, 2]


# ---------------------------------------------------------------------------
# report body + ack watermark (real HTTP round-trip)
# ---------------------------------------------------------------------------

def test_report_body_shape_and_ack_drop(hub):
    h = make_host(hub.url, script=[(0, UID), (1, UID2), (2, UID)])
    for now in (0.0, 10.0, 20.0):
        h.poll_reader(now)
    hub.hub_reply = {"ok": True, "ackTapId": 1}
    assert h.report(21.0) is True
    path, body = hub.hub_bodies[-1]
    assert path == "/api/companion/report"
    # The exact wire contract the hub's companion_report ingests:
    assert body["companionId"] == "companion-test"
    assert body["startedAt"] == h.started_at
    assert isinstance(body["uptimeSec"], (int, float))
    assert body["readerOk"] is True
    assert body["g2sEgmId"] == "WMS_00:a0:a5:79:2d:a8"
    assert body["sasSmib"] == "smib-bb2"
    assert body["sasAddress"] == "1"               # string, per contract
    assert body["lastError"] is None
    assert [t["tapId"] for t in body["taps"]] == [0, 1, 2]
    assert all(set(t) == {"tapId", "uid", "at"} for t in body["taps"])
    # ackTapId=1 -> taps 0 and 1 dropped, 2 still queued for the next report
    assert [t["tapId"] for t in h.taps] == [2]


def test_garbage_ack_drops_nothing(hub):
    h = make_host(hub.url, script=[(0, UID)])
    h.poll_reader(0.0)
    for reply in ({"ok": False, "ackTapId": 5}, {"ackTapId": "0"},
                  {"ok": True, "ackTapId": True}, ["nope"]):
        hub.hub_reply = reply
        assert h.report(1.0) is True
        assert len(h.taps) == 1                    # nothing shed on junk


def test_hub_rejection_edge_logged(hub, caplog):
    # A 200 reply with ok=false (the hub's registry-full rejection) must
    # leave a journal trace — once, not per cycle — and taps stay queued.
    h = make_host(hub.url, script=[(0, UID)])
    h.poll_reader(0.0)
    hub.hub_reply = {"ok": False, "error": "companion registry full"}
    with caplog.at_level(logging.WARNING, logger="companion"):
        assert h.report(1.0) is True
        assert h.report(2.0) is True                # second cycle: no re-log
    rejects = [r for r in caplog.records if "hub rejected report" in r.message]
    assert len(rejects) == 1
    assert "companion registry full" in rejects[0].getMessage()
    assert [t["tapId"] for t in h.taps] == [0]      # nothing shed
    # Acceptance clears the edge so a LATER rejection would log again.
    hub.hub_reply = {"ok": True, "ackTapId": 0}
    assert h.report(3.0) is True
    assert h._rejected is False and len(h.taps) == 0


def test_hub_down_taps_stay_queued():
    # Port 1 is never listening — every report fails, queue survives.
    h = make_host("http://127.0.0.1:1", script=[(0, UID), (1, UID2)])
    h.poll_reader(0.0)
    h.poll_reader(5.0)
    assert h.report(6.0) is False and h.failing is True
    assert h.report(7.0) is False                  # second failure, no crash
    assert [t["tapId"] for t in h.taps] == [0, 1]


def test_hub_recovery_delivers_queued_taps(hub):
    h = make_host("http://127.0.0.1:1", script=[(0, UID)])
    h.poll_reader(0.0)
    assert h.report(1.0) is False
    h.url = hub.url.rstrip("/") + "/api/companion/report"   # hub comes back
    hub.hub_reply = {"ok": True, "ackTapId": 0}
    assert h.report(2.0) is True and h.failing is False
    assert len(h.taps) == 0
    assert [t["tapId"] for t in hub.hub_bodies[-1][1]["taps"]] == [0]


# ---------------------------------------------------------------------------
# restart semantics + reader health + cadence
# ---------------------------------------------------------------------------

def test_restart_resets_tap_ids_and_changes_started_at():
    h1 = make_host("http://127.0.0.1:1", script=[(0, UID)])
    assert h1.poll_reader(0.0)["tapId"] == 0
    time.sleep(0.002)          # startedAt carries microseconds — keep the
    #                            two constructions distinguishable
    h2 = make_host("http://127.0.0.1:1", script=[(0, UID)])
    assert h2.started_at != h1.started_at          # the hub's reset signal
    assert h2.poll_reader(0.0)["tapId"] == 0       # counter is RAM-only


def test_reader_error_flips_reader_ok(hub):
    h = make_host(hub.url,
                  script=[(0, OSError("i2c bus wedged")), (1, UID)])
    assert h.poll_reader(0.0) is None
    assert h.reader_ok is False and "i2c bus wedged" in h.last_error
    assert h.report(0.5) is True
    assert hub.hub_bodies[-1][1]["readerOk"] is False
    # A clean poll heals the flag (lastError stays as the last-known cause).
    assert h.poll_reader(1.0)["uid"] == UID
    assert h.reader_ok is True


def test_report_cadence():
    h = make_host("http://127.0.0.1:1")
    assert h.report_due(0.0, tapped=True) is True      # tap -> immediate
    assert h.report_due(0.0, tapped=False) is True     # boot announcement
    h._last_report_mono = 10.0
    assert h.report_due(10.4, False) is False          # idle, inside 5s
    assert h.report_due(15.1, False) is True           # idle heartbeat
    h.taps.append({"tapId": 0, "uid": UID, "at": "x"})
    assert h.report_due(10.5, False) is False          # unacked, inside 1s
    assert h.report_due(11.1, False) is True           # unacked retry at 1s


# ---------------------------------------------------------------------------
# PN532 frame layer (verbatim-port guard, no hardware)
# ---------------------------------------------------------------------------

class DummyBus:
    """Scripted i2c file: read() pops queued chunks, write() records."""

    def __init__(self, reads=()):
        self.reads = list(reads)
        self.writes = []

    def write(self, b):
        self.writes.append(bytes(b))

    def read(self, n):
        return self.reads.pop(0) if self.reads else b"\x01"

    def close(self):
        pass


def test_pn532_frame_bytes_verbatim():
    # SAMConfiguration frame, byte-for-byte vs the live-proven driver:
    # 00 00 FF | LEN=5 LCS=FB | D4 14 01 00 00 | DCS=17 | 00
    r = PN532Reader()
    r._f = DummyBus()
    r._wr([0x14, 0x01, 0x00, 0x00])
    assert r._f.writes[0].hex() == "0000ff05fbd41401000017" + "00"


def test_pn532_poll_parses_uid():
    # RDY -> ACK frame -> RDY -> InListPassiveTarget response carrying the
    # real first-card UID at the proven offsets (NbTg at +2, len at +7).
    resp = (b"\x01\x00\x00\xff\x0c\xf4"
            b"\xd5\x4b\x01\x01\x00\x04\x08\x04\x6c\xb1\x6f\x06"
            + b"\x00" * 22)
    r = PN532Reader()
    r._f = DummyBus(reads=[
        b"\x01", b"\x00\x00\xff\x00\xff\x00\x00",   # ready + ACK
        b"\x01", resp,                              # ready + response
    ])
    assert r.poll() == UID


def test_pn532_no_card_returns_none():
    # ACK ok, then a response with NbTg=0 -> None (and no error counted).
    resp = b"\x01\x00\x00\xff\x03\xfd\xd5\x4b\x00" + b"\x00" * 31
    r = PN532Reader()
    r._f = DummyBus(reads=[
        b"\x01", b"\x00\x00\xff\x00\xff\x00\x00",
        b"\x01", resp,
    ])
    assert r.poll() is None
    assert r._io_errors == 0


def test_pn532_self_heal_after_five_errors():
    class DeadBus(DummyBus):
        def write(self, b):
            raise OSError("remote I/O error")

    r = PN532Reader(bus="/dev/null-nonexistent-i2c")
    healed = []
    r._self_heal = lambda: healed.append(True)      # count, don't touch /dev
    r._f = DeadBus()
    for n in range(1, 5):
        with pytest.raises(OSError):
            r.poll()
        assert r._io_errors == n and not healed
    with pytest.raises(OSError):
        r.poll()                                    # 5th consecutive error
    assert healed == [True] and r._io_errors == 0


def test_pn532_mid_run_mute_raises_after_ack_misses():
    # Chip goes mute AFTER bring-up (writes complete, no ACK ever): a run
    # of missed ACKs must surface as OSError so the _io_errors/self-heal
    # path covers the wedge instead of an eternal quiet "no card".
    r = PN532Reader()
    r._f = DummyBus()
    r._ack = lambda: False              # mute: commands never acknowledged
    assert r.poll() is None and r._io_errors == 0
    assert r.poll() is None
    with pytest.raises(OSError):
        r.poll()                        # 3rd consecutive miss = trouble
    assert r._io_errors == 1            # counts toward the self-heal
    with pytest.raises(OSError):
        r.poll()                        # keeps raising until an ack lands
    assert r._io_errors == 2


def test_pn532_response_wedge_self_heals():
    # THE 07-14 AVP incident class: the chip ACKS every command (alive on
    # the bus, i2cdetect sees it) but never delivers a response frame —
    # zero errors, zero taps, readerOk lying green. A healthy idle chip
    # answers EVERY poll with D5 4B NbTg=0, so a run of ack-but-no-frame
    # polls must trip the dead-poll watchdog: self-heal + raise so the
    # hub shows readerOk=false until a clean poll proves recovery.
    class WedgedBus(DummyBus):
        def read(self, n):
            if n == 7:
                return b"\x00\x00\xff\x00\xff\x00\x00"   # ACK, always
            return b"\x01"          # RDY / then garbage with no D5 4B

    r = PN532Reader()
    healed = []
    r._self_heal = lambda: healed.append(True)
    r._f = WedgedBus()
    for n in range(1, PN532Reader.MAX_DEAD_POLLS):
        assert r.poll() is None                 # quiet — looks like no card
        assert r._dead_polls == n and not healed
    with pytest.raises(OSError) as e:
        r.poll()                                # threshold poll trips it
    assert "wedged" in str(e.value)
    assert healed == [True] and r._dead_polls == 0


def test_pn532_healthy_idle_never_counts_dead_polls():
    # NbTg=0 (no card) is a HEALTHY answer — the watchdog counter must not
    # move, or a quiet floor would heal-cycle the reader forever.
    resp = b"\x01\x00\x00\xff\x03\xfd\xd5\x4b\x00" + b"\x00" * 31
    r = PN532Reader()
    r._dead_polls = PN532Reader.MAX_DEAD_POLLS - 1   # one shy of the trip
    r._f = DummyBus(reads=[
        b"\x01", b"\x00\x00\xff\x00\xff\x00\x00",
        b"\x01", resp,
    ])
    assert r.poll() is None
    assert r._dead_polls == 0                        # good frame resets it


def test_mock_reader_script():
    m = MockRfidReader([(1, UID), (3, RuntimeError("boom"))])
    assert m.poll() is None
    assert m.poll() == UID
    assert m.poll() is None
    with pytest.raises(RuntimeError):
        m.poll()
    m.close()
    assert m.closed is True
