"""
Tests for sas_host's C2/C3 hub-switch surface: the sasEnabled park, the
pendingHandpay latch, and the sysvalFallback flip.

Split in two tiers, mirroring test_sas_host_commands.py:

* UNIT — HandpayLatch is a module-level class precisely so its lifecycle
  (0x51 latch, 0x52 / reset-ok clear, no re-latch duplicate) can be poked
  directly; HubReporter._take_commands' settings pass-through likewise.

* END-TO-END — the park/resume/refuse behavior lives in main()'s closures
  (the Event, apply_hub_settings, the stop_or_heartbeat park loop), so it
  is exercised the way the commands tests do it: run main() itself against
  an in-process PHASED stub hub. Every phase transition is driven by what
  the satellite actually REPORTS (never by sleeps), so the run is
  deterministic:

    junk   -> serve junk sasEnabled/sysvalFallback ("false", 0, null)
              until the handpay latch shows AND >=5 junk replies were
              survived with sasEnabled still true (C2: exact-bool gate);
    park   -> serve sasEnabled:false + commands until 6 reports show the
              satellite parked (C3: online false, wire silent, commands
              refused with the sas_disabled record);
    resume -> serve sasEnabled:true + sysvalFallback:false until polls
              grow again (instant un-park, no process restart);
    reset  -> serve one handpay_reset; wait for ok:true AND the latch
              reported null (reset-ok clears the banner);
    done   -> SIGTERM main() (its own handler stops the loop cleanly).
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
    RESET_CODE_UNABLE, HANDPAY_PENDING_EXCEPTION, HANDPAY_RESET_EXCEPTION,
)
from core.sas_protocol import sas_crc
from core.sas_ticket_store import TicketStore
from transport.serial.sas_serial import MockSASSerialPort

#: C2 junk values that must NEVER act as a bool (string, int, null).
JUNK_VALUES = ["false", 0, None]

#: The exact C3 refusal record fields (contract-pinned strings).
SAS_DISABLED_RECORD = {
    "ok": False,
    "outcome": "sas_disabled",
    "detail": "SAS is disabled for this machine (Settings)",
    "result": "rejected: sas disabled",
}


def _resp(address, command, code):
    body = bytes([address, command, code])
    return body + sas_crc(body).to_bytes(2, "little")


class HandpayMachine:
    """test_sas_host_commands' ScriptedResetMachine minus the scripted
    blow-up: a general-poll FIFO with implied-ACK semantics plus the
    believed 0xA8/0x94 reset handling. Boots IN a handpay with 0x51
    queued; everything else (incl. the 0x1B handpay-info read) answers
    silence, so the latch is exercised purely off the exception."""

    def __init__(self, address=1):
        self.address = address
        self.in_handpay = True
        self.method = RESET_METHOD_STANDARD
        self.fifo = [HANDPAY_PENDING_EXCEPTION]   # 0x51 queued at boot
        self.pending = None                       # sent but not yet ACKed

    def __call__(self, frame, wakeup):
        if not wakeup[0]:
            return b""
        if len(frame) == 1:                       # general poll
            if (frame[0] & 0x7F) != self.address:
                self.pending = None               # implied ACK of pending
                return b""
            if self.pending is None:
                self.pending = self.fifo.pop(0) if self.fifo else None
            if self.pending is None:
                return b"\x00"
            return bytes([self.pending])
        self.pending = None                       # long poll: implied ACK
        if frame[0] != self.address:
            return b""
        body, crc = frame[:-2], frame[-2:]
        if sas_crc(body).to_bytes(2, "little") != crc:
            return b""       # type-R reads (0x1A/0x1B) land here: silence
        cmd = frame[1]
        if cmd == CMD_SET_HANDPAY_RESET_METHOD:
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


class _PhasedHub(http.server.HTTPServer):
    """Stub hub whose reply is a state machine over the satellite's own
    reports (see the module docstring for the phase plan). Collects every
    report as (phase, report, wire) where wire = len(port.sent_frames) at
    receipt — the wire-silence proof for the park phase."""

    def setup_state(self, port, tito_captured):
        self.lock = threading.Lock()
        self.port_ref = port
        self.tito_captured = tito_captured
        self.reports = []                 # [(phase, report, wire), ...]
        self.phase = "junk"
        self.junk_served = 0
        self.parked_seen = 0
        self.parked_polls = None
        self.sysval_after_junk = None
        self.sysval_after_park = None
        self.signaled = False
        self.t0 = time.monotonic()

    def _tito_auto_service(self):
        return (self.tito_captured[0].auto_service
                if self.tito_captured else None)

    def reply_for(self, rep):
        """Advance the phase off this report, then serve the (possibly
        new) phase's reply. Called under self.lock."""
        if time.monotonic() - self.t0 > 20 and self.phase != "done":
            self.phase = "done"           # watchdog: never hang the suite
        if self.phase == "junk":
            if self.junk_served >= 5 and rep.get("pendingHandpay"):
                self.sysval_after_junk = self._tito_auto_service()
                self.phase = "park"
            else:
                self.junk_served += 1
                j = JUNK_VALUES[self.junk_served % len(JUNK_VALUES)]
                return {"ok": True, "sasEnabled": j, "sysvalFallback": j}
        if self.phase == "park":
            if rep.get("sasEnabled") is False:
                self.parked_seen += 1
            if self.parked_seen >= 6:
                self.parked_polls = rep.get("polls", 0)
                self.sysval_after_park = self._tito_auto_service()
                self.phase = "resume"
            else:
                return {"ok": True, "sasEnabled": False,
                        "commands": [
                            {"id": "hp-parked", "type": "handpay_reset"},
                            {"id": "bonus-parked", "type": "legacy_bonus",
                             "credits": 5}]}
        if self.phase == "resume":
            if (rep.get("sasEnabled") is True
                    and rep.get("polls", 0) >= (self.parked_polls or 0) + 3):
                self.phase = "reset"
            else:
                return {"ok": True, "sasEnabled": True,
                        "sysvalFallback": False}
        if self.phase == "reset":
            hp_live_ok = any(r.get("id") == "hp-live" and r.get("ok")
                             for r in rep.get("commandResults", []))
            if hp_live_ok and rep.get("pendingHandpay") is None:
                self.phase = "done"
            else:
                return {"ok": True, "commands": [
                    {"id": "hp-live", "type": "handpay_reset"}]}
        # done: stop main() via its own signal handler, exactly once
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
            srv.reports.append((srv.phase, rep, len(srv.port_ref.sent_frames)))
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


def _run_settings_channel():
    """Run sas_host.main() once against the phased hub and memoize the
    outcome; every e2e test asserts on this single run (~1-2 s)."""
    if _CACHE:
        return _CACHE

    machine = HandpayMachine(address=1)
    port = MockSASSerialPort(machine)
    tmp = tempfile.mkdtemp(prefix="sas_host_settings_test_")
    captured, tito_captured = [], []

    httpd = _PhasedHub(("127.0.0.1", 0), _HubHandler)
    httpd.setup_state(port, tito_captured)
    server_thread = threading.Thread(target=httpd.serve_forever,
                                     daemon=True)
    server_thread.start()

    _RBase = sas_host.HubReporter

    class _CapturingReporter(_RBase):
        def __init__(self, *a, **k):
            super().__init__(*a, **k)
            captured.append(self)

    _TBase = sas_host.SASTITOHost

    class _CapturingTITO(_TBase):
        def __init__(self, *a, **k):
            super().__init__(*a, **k)
            tito_captured.append(self)

    saved_signals = {s: signal.getsignal(s)
                     for s in (signal.SIGTERM, signal.SIGINT)}
    saved = {n: getattr(sas_host, n) for n in
             ("REPORT_SEC", "HubReporter", "SASTITOHost", "open_port",
              "TicketStore", "HubTicketAuthority")}
    saved_argv = sys.argv
    # Last-resort backstop: if the phase machine wedges, SIGTERM anyway so
    # the suite fails on assertions instead of hanging (main()'s handler is
    # installed long before any phase can stall).
    backstop = threading.Timer(25.0,
                               lambda: os.kill(os.getpid(), signal.SIGTERM))
    backstop.daemon = True
    try:
        sas_host.REPORT_SEC = 0.05
        sas_host.HubReporter = _CapturingReporter
        sas_host.SASTITOHost = _CapturingTITO
        sas_host.open_port = lambda path, mock, address, protocol: port
        # keep the TITO plumbing off the repo's data files / network
        sas_host.TicketStore = lambda: TicketStore(
            path=os.path.join(tmp, "tickets.json"))
        sas_host.HubTicketAuthority = lambda hub, smib, local: \
            HubTicketAuthority(hub, smib, local,
                               journal_path=os.path.join(tmp, "journal.json"),
                               start_sync_thread=False)
        sys.argv = ["sas_host.py", "--mock", "--interval", "0.002",
                    "--hub", f"http://127.0.0.1:{httpd.server_address[1]}",
                    "--smib-id", "pytest-smib"]
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

    assert captured, "main() with --hub must construct a HubReporter"
    assert tito_captured, "main() must construct a SASTITOHost"
    records = list(captured[0].command_results)   # newest first
    with httpd.lock:
        reports = list(httpd.reports)
    _CACHE.update(
        machine=machine, port=port, hub=httpd, reports=reports,
        tito=tito_captured[0], records=records,
        by_id={r["id"]: r for r in records})
    return _CACHE


# ---------------------------------------------------------------------------
# UNIT — HandpayLatch lifecycle
# ---------------------------------------------------------------------------

class TestHandpayLatch:
    def test_51_latches_with_the_c3_shape(self):
        latch = sas_host.HandpayLatch()
        assert latch.pending is None
        latch.on_exception(HANDPAY_PENDING_EXCEPTION)
        p = latch.pending
        assert p is not None
        assert p["code"] == "0x51"
        assert isinstance(p["since"], str) and "T" in p["since"]

    def test_51_while_latched_does_not_duplicate(self):
        """The machine re-raises 0x51 every 15 s (§7.8.1); the latch must
        keep the ORIGINAL dict — same object, same 'since' — not re-mint."""
        latch = sas_host.HandpayLatch()
        latch.on_exception(HANDPAY_PENDING_EXCEPTION)
        p = latch.pending
        latch.on_exception(HANDPAY_PENDING_EXCEPTION)
        assert latch.pending is p              # identity: untouched

    def test_52_clears(self):
        latch = sas_host.HandpayLatch()
        latch.on_exception(HANDPAY_PENDING_EXCEPTION)
        latch.on_exception(HANDPAY_RESET_EXCEPTION)
        assert latch.pending is None

    def test_reset_ok_clears(self):
        latch = sas_host.HandpayLatch()
        latch.on_exception(HANDPAY_PENDING_EXCEPTION)
        latch.on_reset_ok()
        assert latch.pending is None

    def test_noise_is_a_no_op(self):
        """A clear when idle (0x52 after reset-ok already cleared) and
        unrelated exception codes must neither latch nor raise."""
        latch = sas_host.HandpayLatch()
        latch.on_exception(HANDPAY_RESET_EXCEPTION)
        latch.on_reset_ok()
        latch.on_exception(0x7E)               # unrelated code
        assert latch.pending is None


# ---------------------------------------------------------------------------
# UNIT — HubReporter._take_commands settings pass-through
# ---------------------------------------------------------------------------

class TestTakeCommandsSettings:
    def _reporter(self, on_settings=None):
        # snapshot()/run() are never called, so the poller/stats/store
        # slots can be inert — _take_commands touches only the queues and
        # the on_settings hook.
        return sas_host.HubReporter("http://127.0.0.1:9", "unit", "(mock)",
                                    1, None, None, None, None,
                                    on_settings=on_settings)

    def test_raw_values_reach_the_applier(self):
        seen = []
        rep = self._reporter(lambda e, s: seen.append((e, s)))
        rep._take_commands(json.dumps(
            {"sasEnabled": False, "sysvalFallback": True}).encode())
        assert seen == [(False, True)]

    def test_absent_keys_ride_as_none(self):
        """Old-hub compat: a plain commands reply hands (None, None) to
        the applier, whose exact-bool gate makes that a no-op."""
        seen = []
        rep = self._reporter(lambda e, s: seen.append((e, s)))
        rep._take_commands(b'{"ok": true, "commands": []}')
        assert seen == [(None, None)]

    def test_junk_bodies_never_raise_or_apply(self):
        seen = []
        rep = self._reporter(lambda e, s: seen.append((e, s)))
        rep._take_commands(b"not json at all")
        rep._take_commands(b"[1, 2, 3]")           # JSON, but not a dict
        assert seen == []                          # applier never reached
        rep._take_commands(b'{"sasEnabled": "false", "commands": 7}')
        assert seen == [("false", None)]           # raw junk passed through
        assert not rep.pending_commands            # bad commands still gated

    def test_raising_applier_is_swallowed_commands_still_parse(self):
        def boom(e, s):
            raise RuntimeError("scripted applier blow-up")
        rep = self._reporter(boom)
        rep._take_commands(json.dumps(
            {"sasEnabled": True,
             "commands": [{"id": "a1", "type": "t"}]}).encode())
        assert [c["id"] for c in rep.pending_commands] == ["a1"]

    def test_no_applier_is_fine(self):
        rep = self._reporter(None)
        rep._take_commands(b'{"sasEnabled": false, "commands": []}')
        assert not rep.pending_commands


# ---------------------------------------------------------------------------
# END-TO-END — park / refuse / resume / latch-through-reports
# ---------------------------------------------------------------------------

class TestSasEnabledPark:
    def test_junk_reply_values_never_park(self):
        """C2 exact-bool gate on the real reply path: >=5 junk replies
        ("false"/0/null) were served and every junk-phase report still
        said sasEnabled true."""
        run = _run_settings_channel()
        junk = [rep for ph, rep, _ in run["reports"] if ph == "junk"]
        assert run["hub"].junk_served >= 5
        assert junk and all(rep["sasEnabled"] is True for rep in junk)

    def test_parked_reports_disabled_and_offline(self):
        """C3: while parked the snapshot says sasEnabled false AND online
        false (honest — we are not talking to the machine)."""
        parked = [rep for ph, rep, _ in _run_settings_channel()["reports"]
                  if ph == "park" and rep["sasEnabled"] is False]
        assert len(parked) >= 6
        assert all(rep["online"] is False for rep in parked)

    def test_parked_port_is_wire_silent(self):
        """The park promise: zero frames leave the port while parked.
        Samples are len(port.sent_frames) at report receipt; from the 3rd
        parked report on (park fully engaged) the count must FREEZE."""
        wires = [wire for ph, rep, wire in _run_settings_channel()["reports"]
                 if ph == "park" and rep["sasEnabled"] is False]
        assert len(wires) >= 6
        assert wires[2] == wires[-1]

    def test_parked_commands_refused_with_the_c3_record(self):
        """Both a handpay_reset and a legacy_bonus drained while parked
        draw the exact sas_disabled record — and nothing raises (the
        post-park phases ran to completion)."""
        run = _run_settings_channel()
        for cmd_id in ("hp-parked", "bonus-parked"):
            r = run["by_id"][cmd_id]
            for key, want in SAS_DISABLED_RECORD.items():
                assert r[key] == want, (cmd_id, key, r)
        # the refused reset never touched the machine: it was still in its
        # handpay when the post-resume hp-live reset really cleared it
        assert run["by_id"]["hp-live"]["ok"] is True

    def test_resume_without_restart(self):
        """Flipping back true un-parks the SAME process: polls grew past
        the parked value and the run reached the done phase."""
        run = _run_settings_channel()
        assert run["hub"].phase == "done"
        last = run["reports"][-1][1]
        assert last["sasEnabled"] is True
        assert last["polls"] >= (run["hub"].parked_polls or 0) + 3


class TestPendingHandpayThroughReports:
    def test_snapshot_keys_present_in_every_state(self):
        """C3 payload-shape stability: sasEnabled and pendingHandpay ride
        EVERY report — parked or live, latched or clear (null)."""
        reports = [rep for _, rep, _ in _run_settings_channel()["reports"]]
        assert reports
        assert all("sasEnabled" in rep and "pendingHandpay" in rep
                   for rep in reports)

    def test_latch_survives_report_cycles_unchanged(self):
        """The latch is state, not an event: every latched report carries
        the SAME since/code dict, from the 0x51 until the reset."""
        latched = [rep["pendingHandpay"]
                   for _, rep, _ in _run_settings_channel()["reports"]
                   if rep["pendingHandpay"] is not None]
        assert len(latched) >= 2               # spans junk->park->resume
        assert all(p["code"] == "0x51" for p in latched)
        assert len({p["since"] for p in latched}) == 1

    def test_reset_ok_clears_the_latch_on_the_wire(self):
        """The hub only reached 'done' after seeing hp-live ok:true AND a
        report with pendingHandpay null — the full banner lifecycle."""
        run = _run_settings_channel()
        r = run["by_id"]["hp-live"]
        assert r["ok"] is True and r["outcome"] == "reset_ok"
        assert run["machine"].in_handpay is False
        assert run["reports"][-1][1]["pendingHandpay"] is None


class TestSysvalFallbackFlip:
    def test_junk_and_absent_never_flip(self):
        """Junk sysvalFallback in the junk phase and the key's absence in
        the park phase both left auto_service at its boot-true."""
        run = _run_settings_channel()
        assert run["hub"].sysval_after_junk is True
        assert run["hub"].sysval_after_park is True

    def test_real_false_reaches_the_tito_host(self):
        """The resume phase served sysvalFallback:false; SASTITOHost's
        auto_service — the 0x57 responder gate — must hold it (nothing
        served afterwards may flip it back)."""
        assert _run_settings_channel()["tito"].auto_service is False
