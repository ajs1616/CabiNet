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
        body, crc = frame[:-2], frame[-2:]
        if sas_crc(body).to_bytes(2, "little") != crc:
            return b""
        cmd = frame[1]
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
    SAME command batch on the first TWO replies — the real hub re-sends
    until it sees the result echoed, so _seen_cmd_ids dedupe must make
    the re-send harmless (exactly one execution per id)."""

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
        batch = COMMANDS if srv.replies < 2 else []
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
             ("REPORT_SEC", "HubReporter", "open_port",
              "TicketStore", "HubTicketAuthority")}
    saved_argv = sys.argv
    try:
        sas_host.REPORT_SEC = 0.05
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
