"""
Tests for sas_host's hub->SMIB command channel: run_hub_command handling
of handpay_reset beside legacy_bonus.

run_hub_command is deliberately a CLOSURE inside sas_host.main() (it
captures args.address + poller), so it cannot be imported and unit-poked.
Rather than refactor the runner, these tests run main() itself — --mock,
tiny intervals, an in-process stub hub serving /api/sas/report — and
assert on the commandResults records the poll thread produces. That is
the exact end-to-end path the real SMIB runs: hub reply -> pending_commands
-> stop_or_heartbeat drain -> run_hub_command on the POLL thread ->
command_results ride back in the next report.

The machine is a minimal replica of test_sas_handpay_reset's
HandpayMachine plus one scripted transport blow-up, so a single ~2 s run
covers: reset_ok, exception->ok:false, no_handpay, unknown-type rejection,
legacy_bonus coexistence, hub re-send dedupe, and poll-loop survival.
"""

import collections
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
from core.sas_poller import MachineState
from core.sas_handpay_reset import (
    CMD_SET_HANDPAY_RESET_METHOD, CMD_REMOTE_HANDPAY_RESET,
    RESET_METHOD_CREDIT_METER, RESET_METHOD_STANDARD,
    METHOD_ACK_ENABLED, METHOD_ACK_NO_HANDPAY, RESET_CODE_OK,
    RESET_CODE_UNABLE, HANDPAY_RESET_EXCEPTION,
)
from core.sas_protocol import sas_crc
from core.sas_ticket_store import TicketStore
from transport.serial.sas_serial import MockSASSerialPort

CMD_LEGACY_BONUS = 0x8A

#: One drained batch exercising every run_hub_command branch, in FIFO
#: order (pending_commands drains oldest-first in stop_or_heartbeat).
COMMANDS = [
    {"id": "hp-ok", "type": "handpay_reset"},     # full happy path
    {"id": "hp-boom", "type": "handpay_reset"},   # transport raises
    {"id": "hp-idle", "type": "handpay_reset"},   # machine not locked up
    {"id": "nope", "type": "jackpot_button"},     # strict type gate
    {"id": "bonus-1", "type": "legacy_bonus", "credits": 25},
    {"id": "aft-acct", "type": "aft_transfer", "cents": 500,
     "accountId": "p3"},                       # echo on a refused/errored push
    {"id": "aft-bad", "type": "aft_transfer", "cents": 0,
     "accountId": "p3"},                       # echo PRECEDES amount validation
    {"id": "aft-plain", "type": "aft_transfer", "cents": 500},   # no echo
    # LP09 per-game enable/disable + 0x56 read-back verification
    {"id": "game-off", "type": "sas_game_state", "game": 1, "enable": False},
    {"id": "game-on2", "type": "sas_game_state", "game": 2, "enable": True},
    {"id": "game-bad", "type": "sas_game_state", "game": 0, "enable": True},
    # hub-invoked census re-read (no wire traffic in the handler itself)
    {"id": "mi-read", "type": "read_machine_info"},
]


def _resp(address, command, code):
    body = bytes([address, command, code])
    return body + sas_crc(body).to_bytes(2, "little")


class ScriptedResetMachine:
    """Minimal HandpayMachine replica (general-poll FIFO with implied-ACK
    semantics + believed 0xA8/0x94 handling) with one scripted failure:
    the SECOND 0xA8 raises, covering run_hub_command's exception path.
    Unsupported long polls (e.g. the 0x8A bonus) answer silence, per spec."""

    def __init__(self, address=1):
        self.address = address
        self.in_handpay = True
        self.method = RESET_METHOD_STANDARD
        self.step1_calls = 0
        self.fifo = []                         # queued exception codes
        self.pending = None                    # sent but not yet ACKed
        # LP09/0x56 game census: game -> enabled (Table 7.6.1 / 7.6.7)
        self.games_enabled = {1: True, 2: False}

    def __call__(self, frame, wakeup):
        if not wakeup[0]:
            return b""
        if len(frame) == 1:                    # general poll
            if (frame[0] & 0x7F) != self.address:
                self.pending = None            # implied ACK of our pending
                return b""
            if self.pending is None:
                self.pending = self.fifo.pop(0) if self.fifo else None
            if self.pending is None:
                return b"\x00"
            return bytes([self.pending])
        self.pending = None                    # long poll: implied ACK too
        if frame[0] != self.address:
            return b""
        if len(frame) == 2 and frame[1] == 0x56:
            # type-R (no CRC on the poll): enabled-game list at the current
            # denom — [addr][56][len][count][2-BCD game#...]+CRC (§4.5.6)
            games = sorted(g for g, on in self.games_enabled.items() if on)
            data = bytes([1 + 2 * len(games), len(games)]) + b"".join(
                int(f"{g:04d}", 16).to_bytes(2, "big") for g in games)
            body = bytes([self.address, 0x56]) + data
            return body + sas_crc(body).to_bytes(2, "little")
        body, crc = frame[:-2], frame[-2:]
        if sas_crc(body).to_bytes(2, "little") != crc:
            return b""
        cmd = frame[1]
        if cmd == 0x09:
            # Enable/Disable Game N (Table 7.6.1): [addr][09][game# 2-BCD]
            # [00|01][CRC]. Known game -> single-byte address ACK; unknown
            # game number -> SILENCE (§2.2.2.3 "ignore the message").
            game = int(frame[2:4].hex())       # BCD bytes read as digits
            if game in self.games_enabled:
                self.games_enabled[game] = frame[4] == 0x01
                return bytes([self.address])
            return b""
        if cmd == CMD_SET_HANDPAY_RESET_METHOD:
            self.step1_calls += 1
            if self.step1_calls == 2:          # hp-boom: scripted blow-up
                raise RuntimeError("scripted transport blow-up")
            if not self.in_handpay:
                return _resp(self.address, cmd, METHOD_ACK_NO_HANDPAY)
            self.method = frame[2]
            return _resp(self.address, cmd, METHOD_ACK_ENABLED)
        if cmd == CMD_REMOTE_HANDPAY_RESET:
            if (not self.in_handpay
                    or self.method != RESET_METHOD_CREDIT_METER):
                return _resp(self.address, cmd, RESET_CODE_UNABLE)
            self.in_handpay = False
            self.fifo.append(HANDPAY_RESET_EXCEPTION)
            return _resp(self.address, cmd, RESET_CODE_OK)
        return b""


class _HubHandler(http.server.BaseHTTPRequestHandler):
    """Stub hub: records every /api/sas/report snapshot and serves the
    SAME command batch on TWO consecutive replies — a DUPLICATE delivery
    (the hub-restart replay shape), which _seen_cmd_ids dedupe must make
    harmless (exactly one execution per id). NOTE the real hub delivers
    each command exactly ONCE (its queue pops on reply) — re-send-until-
    echo is NOT the contract, so a dropped command/verdict is lost."""

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(n)
        srv = self.server
        if self.path != "/api/sas/report":
            self.send_response(404)
            self.end_headers()
            return
        try:
            srv.reports.append(json.loads(body))
        except ValueError:
            pass
        # Two batches (8 then the rest) served TWICE each — the batch
        # split just exercises multi-reply delivery + the dedupe-under-
        # re-send contract; it is unrelated to MAX_PENDING (now 32).
        batch = (COMMANDS[:8] if srv.replies < 2
                 else COMMANDS[8:] if srv.replies < 4 else [])
        srv.replies += 1
        payload = json.dumps({"ok": True, "commands": batch}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, *args):              # keep pytest output clean
        pass


_CACHE = {}


def _run_channel():
    """Run sas_host.main() once against the stub hub and memoize the
    outcome; every test asserts on this single ~2 s end-to-end run."""
    if _CACHE:
        return _CACHE

    httpd = http.server.HTTPServer(("127.0.0.1", 0), _HubHandler)
    httpd.reports, httpd.replies = [], 0
    server_thread = threading.Thread(target=httpd.serve_forever,
                                     daemon=True)
    server_thread.start()

    machine = ScriptedResetMachine(address=1)
    port = MockSASSerialPort(machine)
    tmp = tempfile.mkdtemp(prefix="sas_host_cmd_test_")
    captured = []

    _Base = sas_host.HubReporter

    class _CapturingReporter(_Base):
        def __init__(self, *a, **k):
            super().__init__(*a, **k)
            captured.append(self)

    saved_signals = {s: signal.getsignal(s)
                     for s in (signal.SIGTERM, signal.SIGINT)}
    saved = {n: getattr(sas_host, n) for n in
             ("REPORT_SEC", "SAS_POLL_FLOOR", "HubReporter", "open_port",
              "TicketStore", "HubTicketAuthority")}
    saved_argv = sys.argv
    try:
        sas_host.REPORT_SEC = 0.05
        sas_host.SAS_POLL_FLOOR = 0.001    # mock port — pacing is wall-clock
        sas_host.HubReporter = _CapturingReporter
        sas_host.open_port = lambda path, mock, address, protocol: port
        # keep the TITO plumbing off the repo's data files / network
        sas_host.TicketStore = lambda: TicketStore(
            path=os.path.join(tmp, "tickets.json"))
        sas_host.HubTicketAuthority = lambda hub, smib, local: \
            HubTicketAuthority(hub, smib, local,
                               journal_path=os.path.join(tmp, "journal.json"),
                               start_sync_thread=False)
        sys.argv = ["sas_host.py", "--mock",
                    "--interval", "0.005", "--max-polls", "300",
                    "--hub", f"http://127.0.0.1:{httpd.server_address[1]}",
                    "--smib-id", "pytest-smib"]
        sas_host.main()
    finally:
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

    assert captured, "main() with --hub must construct a HubReporter"
    records = list(captured[0].command_results)   # newest first
    _CACHE.update(
        machine=machine, port=port, reports=httpd.reports,
        records=records, by_id={r["id"]: r for r in records})
    return _CACHE


class TestHandpayResetCommand:
    def test_success_record_contract(self):
        """RESET_OK -> the record carries the channel's existing keys
        (id/type/when/result) PLUS ok/outcome/detail, with result='ack'
        (the channel's one success verdict, same as the bonus)."""
        r = _run_channel()["by_id"]["hp-ok"]
        assert r["type"] == "handpay_reset"
        assert r["ok"] is True
        assert r["outcome"] == "reset_ok"
        assert r["result"] == "ack"
        assert "confirmed" in r["detail"]
        assert "when" in r
        assert _run_channel()["machine"].in_handpay is False

    def test_two_step_order_on_the_wire(self):
        """The 0x94 reset fired exactly once (only the acked attempt),
        strictly after its 0xA8 method-select."""
        frames = [f for f, _ in _run_channel()["port"].sent_frames]
        step1 = [i for i, f in enumerate(frames)
                 if len(f) > 1 and f[1] == CMD_SET_HANDPAY_RESET_METHOD]
        step2 = [i for i, f in enumerate(frames)
                 if len(f) > 1 and f[1] == CMD_REMOTE_HANDPAY_RESET]
        assert len(step2) == 1                 # boom + idle never reset
        assert step1 and step1[0] < step2[0]

    def test_exception_is_ok_false_not_a_dead_poll_loop(self):
        """A transport blow-up mid-reset must surface as ok:false with
        the error in detail — and the poll thread must survive to run
        the NEXT queued command."""
        run = _run_channel()
        r = run["by_id"]["hp-boom"]
        assert r["ok"] is False
        assert r["outcome"] == "error"
        assert r["result"] == "error: RuntimeError"
        assert "RuntimeError" in r["detail"]
        assert "hp-idle" in run["by_id"]       # loop lived on

    def test_no_handpay_is_typed_and_not_ack(self):
        r = _run_channel()["by_id"]["hp-idle"]
        assert r["ok"] is False
        assert r["outcome"] == "no_handpay"
        assert r["result"] == "no_handpay"     # honest verdict, never "ack"
        assert "not in a handpay" in r["detail"]


class TestChannelUnchanged:
    def test_unknown_type_still_rejected(self):
        """Strict type gate: anything but the two known types is refused,
        in the channel's legacy record shape (no ok/outcome keys)."""
        r = _run_channel()["by_id"]["nope"]
        assert r["result"] == "rejected: unknown type"
        assert "ok" not in r and "outcome" not in r

    def test_legacy_bonus_coexists(self):
        """The bonus branch is untouched: valid credits reach the wire
        (our machine answers 0x8A with silence, per spec)."""
        run = _run_channel()
        r = run["by_id"]["bonus-1"]
        assert r["credits"] == 25
        assert r["result"] == "silence"
        bonus = [f for f, _ in run["port"].sent_frames
                 if len(f) > 1 and f[1] == CMD_LEGACY_BONUS]
        assert len(bonus) == 1

    def test_hub_resend_deduped(self):
        """The stub served the batch twice (hub re-send until echoed);
        _seen_cmd_ids must yield exactly one execution per id."""
        ids = [r["id"] for r in _run_channel()["records"]]
        assert sorted(ids) == sorted(c["id"] for c in COMMANDS)

    def test_results_ride_back_in_reports(self):
        """The verdicts really do ride back to the hub: a later report
        snapshot carries the hp-ok record in commandResults."""
        seen = [rec for rep in _run_channel()["reports"]
                for rec in rep.get("commandResults", [])
                if rec.get("id") == "hp-ok"]
        assert seen and seen[0]["outcome"] == "reset_ok"


class TestAftAccountEcho:
    def test_echo_rides_a_refused_push(self):
        """R1: the mock machine has no AFT plumbing, so the push fails —
        and the accountId echo must survive exactly that (the hub's
        House-fallback settle depends on the echo being unconditional)."""
        r = _run_channel()["by_id"]["aft-acct"]
        assert r["accountId"] == "p3"
        assert r["ok"] is False               # never debitable

    def test_echo_precedes_amount_validation(self):
        r = _run_channel()["by_id"]["aft-bad"]
        assert r["outcome"] == "bad_amount"
        assert r["accountId"] == "p3"

    def test_no_accountid_means_no_echo(self):
        """Old-hub compat: a command without accountId must NOT grow one
        (the hub reads absence as 'debit House')."""
        assert "accountId" not in _run_channel()["by_id"]["aft-plain"]


class TestSasGameState:
    """LP09 per-game enable/disable: the verdict is the 0x56 READ-BACK,
    never the bare ack (§4.4 makes ack-silence ambiguous between
    unsupported and bad-game-number)."""

    def test_disable_verified_via_0x56(self):
        r = _run_channel()["by_id"]["game-off"]
        assert r["ok"] is True
        assert r["outcome"] == "ack"
        assert r["result"] == "verified via 0x56"
        assert r["game"] == 1 and r["enable"] is False

    def test_enable_verified_and_census_refreshed(self):
        c = _run_channel()
        r = c["by_id"]["game-on2"]
        assert r["ok"] is True
        assert r["result"] == "verified via 0x56"
        assert c["machine"].games_enabled == {1: False, 2: True}
        # the fresh 0x56 list rides the next report's machineInfo
        censuses = [rep["machineInfo"] for rep in c["reports"]
                    if isinstance(rep.get("machineInfo"), dict)]
        assert censuses and censuses[-1].get("enabledGames") == [2]

    def test_bad_game_number_rejected_before_the_wire(self):
        c = _run_channel()
        r = c["by_id"]["game-bad"]
        assert r["ok"] is False
        assert "rejected" in r["result"]
        lp09 = [f for f, _ in c["port"].sent_frames
                if len(f) > 2 and f[1] == 0x09]
        assert len(lp09) == 2      # only the two valid toggles hit the wire


class _StubPoller:
    """Just enough poller for HubReporter.snapshot(): a state object."""
    def __init__(self):
        self.state = MachineState(address=1)


class TestResultsRingSizing:
    """The SMIB->hub verdict ring vs the exactly-once delivery contract:
    a verdict evicted before a report POST carries it is gone forever
    (the hub never re-reads), and drain_hub_commands executes the WHOLE
    pending queue in one pass — a parked drain mints MAX_PENDING instant
    no-wire refusals in microseconds, faster than the report thread can
    wake, so the ring must hold at least a full queue's worth."""

    def test_ring_bound_invariant(self):
        """RESULTS_RING >= MAX_PENDING — a future pending bump must not
        silently reopen the verdict-loss gap."""
        assert sas_host.HubReporter.RESULTS_RING >= \
            sas_host.HubReporter.MAX_PENDING

    def test_full_pending_drain_verdicts_all_survive(self):
        """A full MAX_PENDING burst of results deposited between two
        snapshots (the parked-refusal drain shape) must ALL ride the
        next snapshot — the old maxlen=20 ring evicted the oldest 12
        verdicts unsent."""
        stats = {"polls": 0, "events": 0, "meter_changes": 0,
                 "last_meters": {}}
        tmp = tempfile.mkdtemp(prefix="sas_ring_test_")
        rep = sas_host.HubReporter(
            "http://127.0.0.1:1", "ring-test", "mock", 1, _StubPoller(),
            stats, collections.deque(),
            TicketStore(path=os.path.join(tmp, "tickets.json")))
        n = rep.MAX_PENDING
        for i in range(n):
            rep.record_result({"id": f"burst-{i:02d}",
                               "result": "rejected: sas disabled"})
        ids = {r["id"] for r in rep.snapshot()["commandResults"]}
        assert ids >= {f"burst-{i:02d}" for i in range(n)}, \
            "oldest verdicts evicted before any report could carry them"


class TestReadMachineInfoCommand:
    """Hub-invoked census re-read: the satellite handler only re-arms the
    paced census (no wire traffic of its own) and acks the command."""

    def test_rearm_acked(self):
        r = _run_channel()["by_id"]["mi-read"]
        assert r["ok"] is True
        assert r["outcome"] == "rearmed"
        assert r["result"] == "ack"


class TestCensusIndependentOf0x1F:
    """The A0/51/56 games census must not be nested under a successful
    0x1F: this run's machine NEVER answers 0x1F (silence), yet the 0x56
    answer must still publish machineInfo (the Games button stays
    reachable) — and identity reads stay armed-only, never per-poll."""

    def test_census_publishes_despite_silent_0x1f(self):
        infos = [rep["machineInfo"] for rep in _run_channel()["reports"]
                 if isinstance(rep.get("machineInfo"), dict)]
        assert infos, "silent 0x1F suppressed the whole census"
        first = infos[0]
        # published BEFORE any LP09 landed: the bringup census itself,
        # not the LP09 read-back side effect (games start {1: on, 2: off})
        assert first.get("enabledGames") == [1]
        assert "censusAt" in first     # a parsed 0x56 stamps the census
        assert "readAt" not in first   # no parsed 0x1F -> no identity stamp

    def test_identity_reads_are_armed_not_free_running(self):
        frames = [f for f, _ in _run_channel()["port"].sent_frames]
        n = sum(1 for f in frames if len(f) == 2 and f[1] == 0x1F)
        polls = sum(1 for f in frames if len(f) == 1)
        # bringup + the batch-2 re-arms (LP09s + mi-read coalesce per
        # drain pass) — bounded by the ARMED events, dormant otherwise
        assert 1 <= n <= 4, f"{n} identity reads for {polls} polls"
        assert polls > n * 10


# ---------------------------------------------------------------------------
# Census lifecycle end-to-end: busy-frame retry, staleness stamps, and the
# 0x3C/0x8C event re-arms — a second scripted main() run (own cache).
# ---------------------------------------------------------------------------

#: 0x1F identity data block (20 bytes, §4.4.13): game id "G1", additional
#: id "000", denom code 0x01 (the one bench-trusted C-4 row), max bet 3,
#: progressive group 0, options 00 00, paytable "PT0042", RTP "9250".
IDENTITY_1F = b"G1" + b"000" + bytes([0x01, 3, 0]) + b"\x00\x00" \
              + b"PT0042" + b"9250"
assert len(IDENTITY_1F) == 20


class CensusMachine:
    """General-poll FIFO machine (implied-ACK semantics, like
    ScriptedResetMachine) that answers the FULL census — with a scripted
    lifecycle: the FIRST 0x1F draws the §2.2 busy frame (the boot-busy
    window riding the first online flip), later 0x1Fs answer identity;
    after the FIRST 0x56 is served the operator changes the game mix
    (games flip + 0x3C rides the FIFO), and after the SECOND a game
    select flips it again (+0x8C). The satellite must converge on the
    final mix with NO offline drop, NO LP09, and NO hub command — pure
    event-driven re-reads."""

    def __init__(self, address=1):
        self.address = address
        self.fifo = []                         # queued exception codes
        self.pending = None                    # sent but not yet ACKed
        self.games_enabled = {1: True, 2: False}
        self.info_polls = 0                    # 0x1F polls seen (any reply)
        self.census_56 = 0                     # 0x56 polls served

    def _frame(self, command, data):
        body = bytes([self.address, command]) + data
        return body + sas_crc(body).to_bytes(2, "little")

    def __call__(self, frame, wakeup):
        if not wakeup[0]:
            return b""
        if len(frame) == 1:                    # general poll
            if (frame[0] & 0x7F) != self.address:
                self.pending = None            # implied ACK of our pending
                return b""
            if self.pending is None:
                self.pending = self.fifo.pop(0) if self.fifo else None
            if self.pending is None:
                return b"\x00"
            return bytes([self.pending])
        self.pending = None                    # long poll: implied ACK too
        if frame[0] != self.address:
            return b""
        cmd = frame[1]
        if cmd == 0x1F:
            self.info_polls += 1
            if self.info_polls == 1:           # §2.2 busy frame [addr][00]
                return bytes([self.address, 0x00])
            return self._frame(0x1F, IDENTITY_1F)
        if cmd == 0xA0:                        # enabled features, game 0000
            # feature bytes 00 80: ONLY multi-denom extensions set
            return self._frame(0xA0, b"\x00\x00\x00\x80" + b"\x00" * 4)
        if cmd == 0x51:                        # total games, 2-byte BCD
            return self._frame(0x51, b"\x00\x02")
        if cmd == 0x56:                        # enabled games at cur denom
            games = sorted(g for g, on in self.games_enabled.items() if on)
            data = bytes([1 + 2 * len(games), len(games)]) + b"".join(
                int(f"{g:04d}", 16).to_bytes(2, "big") for g in games)
            resp = self._frame(0x56, data)
            self.census_56 += 1
            if self.census_56 == 1:            # operator changes the mix
                self.games_enabled = {1: False, 2: True}
                self.fifo.append(0x3C)         # config changed
            elif self.census_56 == 2:          # player picks a game
                self.games_enabled = {1: True, 2: True}
                self.fifo.append(0x8C)         # game selected
            return resp
        return b""                             # anything else: unsupported


class _IdleHubHandler(http.server.BaseHTTPRequestHandler):
    """Stub hub that never sends commands — the lifecycle run is driven
    entirely by machine events."""

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(n)
        if self.path == "/api/sas/report":
            try:
                self.server.reports.append(json.loads(body))
            except ValueError:
                pass
        payload = json.dumps({"ok": True, "commands": []}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, *args):              # keep pytest output clean
        pass


_LIFECYCLE_CACHE = {}


def _run_lifecycle():
    """Second scripted main() run for the census lifecycle (own cache).
    Stage delays are patched WIDE relative to the report cadence so every
    census generation rides several reports; MACHINE_INFO_RETRY_SEC stays
    real (30s), so a misclassified failure cannot sneak a retry into the
    run — only the busy path and the event re-arms can move the story."""
    if _LIFECYCLE_CACHE:
        return _LIFECYCLE_CACHE

    httpd = http.server.HTTPServer(("127.0.0.1", 0), _IdleHubHandler)
    httpd.reports = []
    server_thread = threading.Thread(target=httpd.serve_forever,
                                     daemon=True)
    server_thread.start()

    machine = CensusMachine(address=1)
    port = MockSASSerialPort(machine)
    tmp = tempfile.mkdtemp(prefix="sas_host_census_test_")
    captured = []

    _Base = sas_host.HubReporter

    class _CapturingReporter(_Base):
        def __init__(self, *a, **k):
            super().__init__(*a, **k)
            captured.append(self)

    saved_signals = {s: signal.getsignal(s)
                     for s in (signal.SIGTERM, signal.SIGINT)}
    saved = {n: getattr(sas_host, n) for n in
             ("REPORT_SEC", "SAS_POLL_FLOOR", "MACHINE_INFO_BUSY_SEC",
              "MACHINE_INFO_SETTLE_SEC", "HubReporter", "open_port",
              "TicketStore", "HubTicketAuthority")}
    saved_argv = sys.argv
    try:
        sas_host.REPORT_SEC = 0.03
        sas_host.SAS_POLL_FLOOR = 0.001    # mock port — pacing is wall-clock
        sas_host.MACHINE_INFO_BUSY_SEC = 0.15
        sas_host.MACHINE_INFO_SETTLE_SEC = 0.15
        sas_host.HubReporter = _CapturingReporter
        sas_host.open_port = lambda path, mock, address, protocol: port
        sas_host.TicketStore = lambda: TicketStore(
            path=os.path.join(tmp, "tickets.json"))
        sas_host.HubTicketAuthority = lambda hub, smib, local: \
            HubTicketAuthority(hub, smib, local,
                               journal_path=os.path.join(tmp, "journal.json"),
                               start_sync_thread=False)
        sys.argv = ["sas_host.py", "--mock",
                    "--interval", "0.005", "--max-polls", "400",
                    "--hub", f"http://127.0.0.1:{httpd.server_address[1]}",
                    "--smib-id", "pytest-census-smib"]
        sas_host.main()
    finally:
        sys.argv = saved_argv
        for name, value in saved.items():
            setattr(sas_host, name, value)
        for sig, handler in saved_signals.items():
            signal.signal(sig, handler)
        if captured:
            captured[0].stop = True
        time.sleep(0.1)                        # let a final report land
        httpd.shutdown()
        server_thread.join(timeout=2)

    infos = [rep["machineInfo"] for rep in httpd.reports
             if isinstance(rep.get("machineInfo"), dict)]
    _LIFECYCLE_CACHE.update(machine=machine, port=port,
                            reports=httpd.reports, infos=infos)
    return _LIFECYCLE_CACHE


class TestCensusLifecycle:
    """End-to-end census lifecycle against CensusMachine: boot-busy retry,
    staleness stamps, and the 0x3C/0x8C event re-arms — no offline drop,
    no LP09, no hub command in the whole run."""

    def test_busy_first_read_retries_and_publishes(self):
        """The §2.2 busy frame must cost a SHORT retry, not the session:
        the old code latched done BEFORE the try, so a boot-busy 0x1F
        left machineInfo dark until the next offline drop."""
        c = _run_lifecycle()
        assert c["infos"], "busy first read latched — no machineInfo"
        last = c["infos"][-1]
        assert last.get("denomCode") == 0x01
        assert last.get("gameId") == "G1"
        assert last.get("multiDenom") is True
        assert last.get("totalGames") == 2

    def test_identity_and_census_stamps(self):
        """A PARSED 0x1F earns readAt, a PARSED 0x56 earns censusAt —
        the stamps that let the hub/UI render census AGE instead of
        presenting stale data as present-tense truth."""
        last = _run_lifecycle()["infos"][-1]
        assert "readAt" in last
        assert "censusAt" in last

    def test_config_and_game_events_rearm_census(self):
        """0x3C (operator changed configuration) and 0x8C (game selected)
        must each re-arm the census: the reports walk the game mix
        [1] -> [2] -> [1, 2] purely off machine events."""
        seq = []
        for mi in _run_lifecycle()["infos"]:
            eg = mi.get("enabledGames")
            if eg is not None and (not seq or seq[-1] != eg):
                seq.append(eg)
        assert seq == [[1], [2], [1, 2]]

    def test_idle_after_success_no_hammer(self):
        """Exactly four 0x1F polls: busy + bringup + one per event — a
        successful read goes IDLE (dormancy law); it never free-runs and
        a re-arm burst coalesces per settle window."""
        c = _run_lifecycle()
        assert c["machine"].info_polls == 4
        assert c["machine"].census_56 == 3


# ---------------------------------------------------------------------------
# LP09 verdict branches, the toggle->census re-arm, and parked verdict
# delivery (finding D11): scripted runs where the machine DECLINES — busy
# frames, unknown-game silence, identity dark all session — plus a full
# MAX_PENDING burst against a parked satellite. The always-cooperating
# mocks above never executed these branches, which is exactly how the
# denom-control feature shipped green while the floor degraded.
# ---------------------------------------------------------------------------


class Lp09Machine:
    """CensusMachine sibling for the LP09 verdict family: answers the full
    census immediately (no boot-busy) unless identity_busy pins 0x1F in
    the §2.2 busy state forever; ACKs LP09 for known games, answers the
    busy frame for busy_games (mix untouched), and silences unknown game
    numbers (§2.2.2.3) — plus the accounting-min shift: a REAL game-mix
    change bumps the RAW 0x1F denom code, so only a census re-read can
    ever see it (the code is never decoded to cents anywhere)."""

    def __init__(self, address=1, busy_games=(), identity_busy=False):
        self.address = address
        self.fifo = []                         # queued exception codes
        self.pending = None                    # sent but not yet ACKed
        self.games_enabled = {1: True, 2: False}
        self.busy_games = set(busy_games)
        self.identity_busy = identity_busy
        self.denom_code = 0x01                 # RAW C-4 code, never decoded
        self.info_polls = 0                    # 0x1F polls seen (any reply)

    def _frame(self, command, data):
        body = bytes([self.address, command]) + data
        return body + sas_crc(body).to_bytes(2, "little")

    def __call__(self, frame, wakeup):
        if not wakeup[0]:
            return b""
        if len(frame) == 1:                    # general poll
            if (frame[0] & 0x7F) != self.address:
                self.pending = None            # implied ACK of our pending
                return b""
            if self.pending is None:
                self.pending = self.fifo.pop(0) if self.fifo else None
            if self.pending is None:
                return b"\x00"
            return bytes([self.pending])
        self.pending = None                    # long poll: implied ACK too
        if frame[0] != self.address:
            return b""
        cmd = frame[1]
        if cmd == 0x1F:
            self.info_polls += 1
            if self.identity_busy:             # §2.2 busy frame [addr][00]
                return bytes([self.address, 0x00])
            return self._frame(0x1F, b"G1" + b"000"
                               + bytes([self.denom_code, 3, 0])
                               + b"\x00\x00" + b"PT0042" + b"9250")
        if cmd == 0xA0:                        # features: multi-denom only
            return self._frame(0xA0, b"\x00\x00\x00\x80" + b"\x00" * 4)
        if cmd == 0x51:                        # total games, 2-byte BCD
            return self._frame(0x51, b"\x00\x02")
        if cmd == 0x56:                        # enabled games at cur denom
            games = sorted(g for g, on in self.games_enabled.items() if on)
            data = bytes([1 + 2 * len(games), len(games)]) + b"".join(
                int(f"{g:04d}", 16).to_bytes(2, "big") for g in games)
            return self._frame(0x56, data)
        if cmd == 0x09:
            body, crc = frame[:-2], frame[-2:]
            if sas_crc(body).to_bytes(2, "little") != crc:
                return b""
            game = int(frame[2:4].hex())       # 2-byte BCD game number
            if game in self.busy_games:        # can't service right now
                return bytes([self.address, 0x00])
            if game in self.games_enabled:
                want = frame[4] == 0x01
                if self.games_enabled[game] != want:
                    self.games_enabled[game] = want
                    # a game-mix change shifts the machine's minimum
                    # (accounting) denom — new RAW code, still undecoded
                    self.denom_code = 0x05
                return bytes([self.address])
            return b""                         # unknown game: SILENCE
        return b""                             # anything else: unsupported


class DenomMachine(Lp09Machine):
    """A multi-denom cabinet modelled on the real WMS BB2 as observed
    2026-07-25 — including the part we do NOT understand.

    Answers, all of them constructed fixtures except where marked:
      * the full census, plus LP B1 (current player denom = 01 = 1c) and
        LP B2 (enabled player denoms = 01, 02) — §16.3 / §16.4,
      * bare LP B5 (§7.23) with a game name and a paytable name,
      * B0-wrapped LP 0x56 reads, per denomination,
      * B0-wrapped LP 0x2F answering the spec's OWN Table 16.2d bytes,
      * and B0-wrapped LP 09 with **Table 16.1c error 04**, exactly as the
        real cabinet did at denoms 00, 01 and 02 [WIRE]. Mechanism unknown;
        the fixture reproduces the BEHAVIOUR so the reporting contract around
        it stays pinned, and asserts nothing about why.
    """

    def __init__(self, address=1, player_denoms=(0x01, 0x02),
                 current_denom=0x01, denom_games=None):
        super().__init__(address=address)
        self.games_enabled = {12: True}
        self.player_denoms = list(player_denoms)
        self.current_denom = current_denom
        # games enabled per Table C-4 denom code
        self.denom_games = denom_games or {0x01: [12], 0x02: [12]}
        self.lp09_attempts = []            # every B0-wrapped LP09 frame seen

    def _b0(self, denom, base, data):
        body = bytes([denom, base]) + data
        return self._frame(0xB0, bytes([len(body)]) + body)

    def __call__(self, frame, wakeup):
        if not wakeup[0] or len(frame) == 1 or frame[0] != self.address:
            return super().__call__(frame, wakeup)
        cmd = frame[1]
        if cmd == 0xB1:
            return self._frame(0xB1, bytes([self.current_denom]))
        if cmd == 0xB2:
            return self._frame(0xB2, bytes([1 + len(self.player_denoms),
                                            len(self.player_denoms)])
                               + bytes(self.player_denoms))
        if cmd == 0xB5:                     # bare, type M (Table 7.23a/b)
            payload = (frame[2:4] + b"\x02\x00" + b"\x00"
                       + b"\x00\x00\x00\x00"
                       + bytes([13]) + b"JACKPOT PARTY"
                       + bytes([6]) + b"42B19D" + b"\x00\x01")
            return self._frame(0xB5, bytes([len(payload)]) + payload)
        if cmd == 0xB0:
            length = frame[2]
            denom, base = frame[3], frame[4]
            data = frame[5:3 + length]
            if base == 0x09:
                self.lp09_attempts.append(bytes(frame))
                # [WIRE] the BB2's answer at every denomination tried
                return self._b0(denom, 0x00, b"\x04")
            if base == 0x56:
                games = self.denom_games.get(denom or self.current_denom, [])
                inner = b"".join(int(f"{g:04d}", 16).to_bytes(2, "big")
                                 for g in games)
                return self._b0(denom or self.current_denom, 0x56,
                                bytes([1 + len(inner), len(games)]) + inner)
            if base == 0x2F:
                # Table 16.2d verbatim payload: game 0000, meter 00, 123456
                return self._b0(denom, 0x2F,
                                bytes.fromhex("070000000 0123456"
                                              .replace(" ", "")))
            return self._b0(denom, 0x00, b"\x03")   # not multi-denom aware
        return super().__call__(frame, wakeup)


#: One FIFO batch covering the whole 2026-07-25 per-denom wave.
DENOM_COMMANDS = [
    # FIX 3 — the machine's OWN player-denomination answers (B1 + B2)
    {"id": "dn-read", "type": "sas_read_player_denoms"},
    # FIX 6 — bare LP B5 at game 0000 (the gaming machine)
    {"id": "dn-b5", "type": "sas_read_game_info", "game": 0},
    # FIX 4 — a DISABLE at denom 00 means ALL player denominations
    {"id": "dn-all-off", "type": "sas_denom_game_state", "game": 12,
     "enable": False, "denom": 0x00},
    # FIX 4 — a guarded disable at 5c: two denoms are live, so it proceeds
    # to the wire, where the machine answers error 04
    {"id": "dn-off-5c", "type": "sas_denom_game_state", "game": 12,
     "enable": False, "denom": 0x02},
    # FIX 4 — game 0000 behind the preamble (§16.2) — previously unreachable
    {"id": "dn-g0", "type": "sas_denom_game_state", "game": 0,
     "enable": True, "denom": 0x02},
    # FIX 2 — a B5 probe with no game number must be REFUSED, not emitted
    {"id": "dn-b5-short", "type": "multidenom_probe", "baseCommand": 0xB5,
     "denom": 0x02},
    # FIX 2 — the spec's own Table 16.2c frame, now sendable
    {"id": "dn-2f", "type": "multidenom_probe", "baseCommand": 0x2F,
     "denom": 0x04, "data": "03000000"},
]


def _run_denoms():
    served = {"n": 0}

    def script(i, reports):
        landed = any(isinstance(rep.get("machineInfo"), dict)
                     and "readAt" in rep["machineInfo"] for rep in reports)
        if landed and served["n"] < 1:
            served["n"] += 1
            return {"commands": DENOM_COMMANDS}
        return {}

    return _run_scripted("denoms", DenomMachine, script, max_polls=400)


class TestPerDenomCommands:
    """End-to-end through main()'s real command path: hub reply ->
    pending_commands -> poll-thread drain -> run_hub_command -> the record
    that rides the next report. No hardware."""

    def test_player_denoms_read_back_b1_and_b2(self):
        r = _run_denoms()["by_id"]["dn-read"]
        assert r["ok"] is True
        assert r["playerDenoms"]["currentPlayerDenom"] == 0x01
        assert r["playerDenoms"]["playerDenoms"] == [0x01, 0x02]
        assert r["playerDenoms"]["currentPlayerDenomCents"] == 1
        assert r["playerDenoms"]["playerDenomsCents"] == [1, 5]

    def test_census_publishes_player_denoms_apart_from_the_accounting_denom(
            self):
        """denomCode (0x1F, ACCOUNTING) and playerDenoms/currentPlayerDenom
        (B1/B2, PLAYER) must never share a key — on the real BB2 they both
        read 01, which is exactly how a conflation stays invisible."""
        mis = [rep["machineInfo"] for rep in _run_denoms()["reports"]
               if isinstance(rep.get("machineInfo"), dict)
               and rep["machineInfo"].get("playerDenoms")]
        assert mis, "the census never published player denominations"
        assert mis[-1]["playerDenoms"] == [0x01, 0x02]
        assert mis[-1]["currentPlayerDenom"] == 0x01
        assert "denomCode" in mis[-1]

    def test_bare_b5_returns_the_game_and_paytable_names(self):
        r = _run_denoms()["by_id"]["dn-b5"]
        assert r["ok"] is True
        assert r["gameInfo"]["gameName"] == "JACKPOT PARTY"
        assert r["gameInfo"]["paytableName"] == "42B19D"
        assert r["gameInfo"]["sent"] == "01 b5 00 00 44 af"

    def test_disable_at_denom_00_is_refused_before_the_wire(self):
        run = _run_denoms()
        r = run["by_id"]["dn-all-off"]
        assert r["ok"] is False
        assert "ALL player denominations" in r["result"]
        # and it never reached the machine
        assert all(f[4] != 0x09 or f[3] != 0x00
                   for f in run["machine"].lp09_attempts)

    def test_guarded_disable_reaches_the_wire_and_reports_error_04(self):
        """Two player denominations are live, so the stranding guard allows
        it; the machine then answers Table 16.1c error 04. The record must
        read as OUR failure with the bytes, never as a machine limitation."""
        r = _run_denoms()["by_id"]["dn-off-5c"]
        assert r["ok"] is False
        assert r["denomGuard"]["allow"] is True
        assert r["denomGuard"]["basis"] == "B2"
        assert r["result"].startswith("OUR ATTEMPT FAILED")
        assert "error 0x04" in r["result"]
        assert "mechanism unknown" in r["result"]
        # the forbidden sentence. (The machine's OWN Table 16.1c
        # text does contain "not supported" — that is the machine
        # speaking about a FORMAT, which is exactly what we must
        # surface verbatim. What must never appear is our own
        # conclusion about the machine.)
        assert "machine does not support" not in r["result"]
        assert "does not support per-denom" not in r["result"]
        md = r["multidenom"]
        assert md["errorCode"] == "0x04"
        assert md["error"] == \
            "long poll not supported in that format for a specific denomination"
        assert md["denomCents"] == 5
        assert md["sent"] == "01 b0 05 02 09 00 12 00 a5 8c"

    def test_game_0000_behind_the_preamble_is_now_reachable(self):
        """§16.2 requires game 0000 with the preamble; our own validators
        forbade it in BOTH layers until 2026-07-25, so the most
        spec-indicated per-denom frame had never been on a wire."""
        run = _run_denoms()
        r = run["by_id"]["dn-g0"]
        assert "rejected" not in r["result"]
        assert r["multidenom"]["sent"] == "01 b0 05 02 09 00 00 01 0d 3b"
        assert any(f[5:7] == b"\x00\x00"
                   for f in run["machine"].lp09_attempts)

    def test_probe_refuses_a_b5_with_no_game_number(self):
        """The self-inflicted error 04 of 2026-07-25 cannot recur."""
        run = _run_denoms()
        r = run["by_id"]["dn-b5-short"]
        assert r["ok"] is False
        assert "MALFORMED" in r["result"]
        assert not [f for f in run["port"].sent_frames
                    if len(f) > 4 and f[1] == 0xB0 and f[4] == 0xB5]

    def test_probe_can_now_carry_the_specs_own_worked_example(self):
        """Table 16.2c host frame -> Table 16.2d response, both verbatim from
        SAS 6.02 §16.2, and it decodes as DATA (not the old bogus "ack")."""
        r = _run_denoms()["by_id"]["dn-2f"]
        assert r["ok"] is True
        assert r["multidenom"]["sent"] == "01 b0 06 04 2f 03 00 00 00 56 0c"
        assert r["multidenom"]["outcome"] == "data"
        assert r["multidenom"]["denomCents"] == 25
        assert r["multidenom"]["data"] == "07 00 00 00 00 12 34 56"


class _ScriptedHubHandler(http.server.BaseHTTPRequestHandler):
    """Stub hub whose report replies come from srv.script(reply_index,
    reports) — a dict merged over {"ok": True}. Handing the script the
    accumulated reports lets a run key its stages on observed STATE (a
    census rode a report, a verdict landed) instead of reply counts:
    main()'s setup runs between the reporter-thread start and the first
    poll, so clock/count-scheduled stages can all fire before the poll
    thread exists."""

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(n)
        srv = self.server
        if self.path != "/api/sas/report":
            self.send_response(404)
            self.end_headers()
            return
        try:
            srv.reports.append(json.loads(body))
        except ValueError:
            pass
        payload = json.dumps({"ok": True,
                              **srv.script(srv.replies,
                                           srv.reports)}).encode()
        srv.replies += 1
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, *args):              # keep pytest output clean
        pass


_SCRIPTED_RUNS = {}


def _run_scripted(key, machine_factory, script, max_polls):
    """One memoized main() run per key against a scripted hub — the same
    patch set as _run_lifecycle (MACHINE_INFO_RETRY_SEC stays real, so
    only the busy path and explicit re-arms can move a run's story) plus
    FAST_REPORT_SEC, so command activity keeps the report cadence tight
    instead of stretching it to the real 0.25 s."""
    if key in _SCRIPTED_RUNS:
        return _SCRIPTED_RUNS[key]

    httpd = http.server.HTTPServer(("127.0.0.1", 0), _ScriptedHubHandler)
    httpd.reports, httpd.replies, httpd.script = [], 0, script
    server_thread = threading.Thread(target=httpd.serve_forever,
                                     daemon=True)
    server_thread.start()

    machine = machine_factory()
    port = MockSASSerialPort(machine)
    tmp = tempfile.mkdtemp(prefix=f"sas_host_{key}_test_")
    captured = []

    _Base = sas_host.HubReporter

    class _CapturingReporter(_Base):
        def __init__(self, *a, **k):
            super().__init__(*a, **k)
            captured.append(self)

    saved_signals = {s: signal.getsignal(s)
                     for s in (signal.SIGTERM, signal.SIGINT)}
    saved = {n: getattr(sas_host, n) for n in
             ("REPORT_SEC", "FAST_REPORT_SEC", "SAS_POLL_FLOOR",
              "MACHINE_INFO_BUSY_SEC", "MACHINE_INFO_SETTLE_SEC",
              "HubReporter", "open_port", "TicketStore",
              "HubTicketAuthority")}
    saved_argv = sys.argv
    try:
        sas_host.REPORT_SEC = 0.03
        sas_host.FAST_REPORT_SEC = 0.03
        sas_host.SAS_POLL_FLOOR = 0.001    # mock port — pacing is wall-clock
        sas_host.MACHINE_INFO_BUSY_SEC = 0.15
        sas_host.MACHINE_INFO_SETTLE_SEC = 0.15
        sas_host.HubReporter = _CapturingReporter
        sas_host.open_port = lambda path, mock, address, protocol: port
        sas_host.TicketStore = lambda: TicketStore(
            path=os.path.join(tmp, "tickets.json"))
        sas_host.HubTicketAuthority = lambda hub, smib, local: \
            HubTicketAuthority(hub, smib, local,
                               journal_path=os.path.join(tmp, "journal.json"),
                               start_sync_thread=False)
        sys.argv = ["sas_host.py", "--mock",
                    "--interval", "0.005", "--max-polls", str(max_polls),
                    "--hub", f"http://127.0.0.1:{httpd.server_address[1]}",
                    "--smib-id", f"pytest-{key}-smib"]
        sas_host.main()
    finally:
        sys.argv = saved_argv
        for name, value in saved.items():
            setattr(sas_host, name, value)
        for sig, handler in saved_signals.items():
            signal.signal(sig, handler)
        if captured:
            captured[0].stop = True
        time.sleep(0.1)                        # let a final report land
        httpd.shutdown()
        server_thread.join(timeout=2)

    assert captured, "main() with --hub must construct a HubReporter"
    records = list(captured[0].command_results)   # newest first
    _SCRIPTED_RUNS[key] = dict(
        machine=machine, port=port, reports=httpd.reports,
        records=records, by_id={r["id"]: r for r in records})
    return _SCRIPTED_RUNS[key]


#: FIFO batch for the game-state run: a real toggle, a busy decline, and
#: a spec-valid game number this machine doesn't implement.
GAME_STATE_COMMANDS = [
    {"id": "gs-on2", "type": "sas_game_state", "game": 2, "enable": True},
    {"id": "gs-busy1", "type": "sas_game_state", "game": 1, "enable": False},
    {"id": "gs-ghost9", "type": "sas_game_state", "game": 9, "enable": True},
]


def _run_game_state():
    # serve the batch (twice — duplicate delivery, deduped) only once the
    # bringup census has RIDDEN a report, so the ordering assertions
    # (bringup 0x1F strictly before the first LP09) are state-keyed,
    # never a bet on how long main()'s setup takes
    served = {"n": 0}

    def script(i, reports):
        landed = any(isinstance(rep.get("machineInfo"), dict)
                     and "readAt" in rep["machineInfo"] for rep in reports)
        if (landed or served["n"]) and served["n"] < 2:
            served["n"] += 1
            return {"commands": GAME_STATE_COMMANDS}
        return {}

    return _run_scripted(
        "gamestate", lambda: Lp09Machine(address=1, busy_games={1}),
        script, max_polls=350)


#: A FULL pending queue of burst commands for the parked run — the drain
#: refuses them all in one no-wire pass, minting MAX_PENDING verdicts
#: between two report snapshots.
PARKED_BURST = [
    {"id": f"pb-{i:02d}", "type": "sas_game_state", "game": 1,
     "enable": False}
    for i in range(sas_host.HubReporter.MAX_PENDING)
]


def _run_parked_burst():
    ids = {c["id"] for c in PARKED_BURST}

    def script(i, reports):
        # stay PARKED until every burst verdict has ridden a report — so
        # all refusals provably mint while parked — then release so the
        # run can finish. The i >= 40 valve ends the run (as a clean test
        # FAILURE, not a hang) if verdicts go missing, which is exactly
        # the D16 eviction regression this run exists to catch.
        seen = {rec["id"] for rep in reports
                for rec in rep.get("commandResults", [])}
        if ids <= seen or i >= 40:
            return {"sasEnabled": True}
        return {"sasEnabled": False, "commands": PARKED_BURST}

    return _run_scripted("parked", Lp09Machine, script, max_polls=250)


def _run_dark_identity():
    return _run_scripted(
        "darkid", lambda: Lp09Machine(address=1, identity_busy=True),
        lambda i, reports: {"commands": [
            {"id": "dark-on2", "type": "sas_game_state",
             "game": 2, "enable": True}]} if i < 2 else {},
        max_polls=200)


class TestLp09VerdictBranches:
    """LP09 reply classification against machines that DECLINE — the
    branches the always-ACKing mock above never executes. §4.4 makes
    ack-silence ambiguous, so the 0x56 read-back is the verdict; §2.2's
    busy frame [addr][00] starts with the bare address and must never
    read as an ack."""

    def test_valid_toggle_still_verifies(self):
        r = _run_game_state()["by_id"]["gs-on2"]
        assert r["ok"] is True
        assert r["result"] == "verified via 0x56"

    def test_busy_machine_is_an_honest_busy_verdict(self):
        r = _run_game_state()["by_id"]["gs-busy1"]
        assert r["ok"] is False
        assert r["outcome"] == "busy"
        # 2026-07-25 reporting contract: OUR attempt failed, here are the
        # bytes, mechanism unknown — never "the machine does not support X".
        assert r["result"].startswith("OUR ATTEMPT FAILED")
        assert "read-back shows the game unchanged" in r["result"]
        assert "mechanism unknown" in r["result"]
        # raw bytes are recorded on EVERY bare-LP09 attempt (the 2026-07-22
        # "BB2 does not support LP09" note is unauditable for lack of them)
        assert r["sent"] and r["raw"] == "01 00"

    def test_unknown_game_silence_is_never_a_silent_success(self):
        r = _run_game_state()["by_id"]["gs-ghost9"]
        assert r["ok"] is False
        assert r["outcome"] == "silent"
        assert r["result"].startswith("OUR ATTEMPT FAILED")
        assert "(silence)" in r["result"]
        assert "mechanism unknown" in r["result"]
        assert r["sent"] and r["raw"] == ""

    def test_declined_toggles_left_the_mix_alone(self):
        """The busy and unknown-game commands must not have moved the
        machine's mix — only gs-on2 landed."""
        assert _run_game_state()["machine"].games_enabled == {1: True,
                                                             2: True}

    def test_all_three_reached_the_wire(self):
        """gs-ghost9 is spec-valid (1..9999): its refusal must come from
        the READ-BACK, not a pre-wire gate — all three LP09s go out."""
        frames = [f for f, _ in _run_game_state()["port"].sent_frames]
        assert len([f for f in frames if len(f) > 2 and f[1] == 0x09]) == 3


class TestGameStateRearmsCensus:
    """A game-mix change can shift the machine's minimum (accounting)
    denom, so a sas_game_state must re-arm the FULL census — the mock
    bumps its RAW 0x1F code on a real change, so ONLY a fresh identity
    read (not the LP09 read-back fold) can surface it."""

    def test_identity_re_read_follows_the_toggle_batch(self):
        run = _run_game_state()
        frames = [f for f, _ in run["port"].sent_frames]
        idx_1f = [i for i, f in enumerate(frames)
                  if len(f) == 2 and f[1] == 0x1F]
        idx_09 = [i for i, f in enumerate(frames)
                  if len(f) > 2 and f[1] == 0x09]
        assert idx_1f and idx_09
        assert idx_1f[0] < idx_09[0], "bringup census precedes the batch"
        assert idx_1f[-1] > idx_09[-1], (
            "no census re-read followed the LP09 batch — the accounting-"
            "min shift stays unseen until an offline drop")

    def test_re_read_publishes_the_shifted_raw_denom(self):
        infos = [rep["machineInfo"] for rep in _run_game_state()["reports"]
                 if isinstance(rep.get("machineInfo"), dict)]
        assert infos
        last = infos[-1]
        assert last.get("denomCode") == 0x05   # RAW post-change code
        assert last.get("gameId") == "G1"      # identity intact
        assert last.get("enabledGames") == [1, 2]


class TestParkedBurstVerdictDelivery:
    """D16 end-to-end: a full MAX_PENDING burst delivered to a PARKED
    satellite drains as instant C3 refusals — faster than the report
    thread can wake — and the hub's exactly-once delivery makes a lost
    verdict permanent. Every verdict must reach the hub across report
    snapshots, and the park promise holds: nothing touches the wire."""

    def test_every_refusal_verdict_reaches_the_hub(self):
        run = _run_parked_burst()
        seen = {}
        for rep in run["reports"]:
            for rec in rep.get("commandResults", []):
                seen.setdefault(rec["id"], rec)
        missing = [c["id"] for c in PARKED_BURST if c["id"] not in seen]
        assert not missing, (
            f"{len(missing)}/{len(PARKED_BURST)} parked refusal verdicts "
            f"never rode any report: {missing}")
        for c in PARKED_BURST:
            assert seen[c["id"]].get("outcome") == "sas_disabled"

    def test_parked_burst_never_touches_the_wire(self):
        frames = [f for f, _ in _run_parked_burst()["port"].sent_frames]
        assert not [f for f in frames if len(f) > 2 and f[1] == 0x09], (
            "a parked satellite put LP09 frames on the wire")


class TestLp09FoldWithDarkIdentity:
    """A machine whose 0x1F never leaves the §2.2 busy state (identity
    dark all session) must still get LP09 service, and the read-back
    fold must publish the games census WITHOUT fabricating identity
    fields it never read."""

    def test_verdict_and_census_with_identity_dark(self):
        run = _run_dark_identity()
        r = run["by_id"]["dark-on2"]
        assert r["ok"] is True
        assert r["result"] == "verified via 0x56"
        infos = [rep["machineInfo"] for rep in run["reports"]
                 if isinstance(rep.get("machineInfo"), dict)]
        assert infos, "dark identity suppressed the LP09 census fold"
        last = infos[-1]
        assert last.get("enabledGames") == [1, 2]
        assert "denomCode" not in last     # never read -> never invented
        assert "readAt" not in last        # no parsed 0x1F, no stamp
