#!/usr/bin/env python3
"""
Regression test for the GR-01 dead-link demotion (+ GR-14/15/19/27 slices).

The gremlin: overnight 2026-07-02 the EGM was powered off for 8h and the host
kept comms_state='onLine' the whole time — 2,900 CRITICAL keepAlive failures
at a fixed 10s cadence plus 1,449 identical watchdog WARNINGs (84% of the host
log) while /api/status lied 'onLine'. The fix demotes to 'offline' after
KEEPALIVE_FAIL_DEMOTE consecutive keepAlive POST failures (or keepAliveAck
silence via the watchdog), self-silencing every spam source, and recovers via
any of three paths (EGM re-handshake / probe success / inbound commsStatus).

This is a standalone IN-PROCESS test (imports g2s_host directly) rather than
an avp_replay.py step: driving the demotion through the live-process harness
needs 6 real keepAlive failures at the pinger's fixed 10s module-constant
cadence = 60+ wall-clock seconds added to every future gate run. In-process,
post_to_egm is called directly and the whole file runs in ~6s (the one slow
bit is the real 5s black-hole timeout proof for GR-19).

Covers:
  * GR-19 — POST timeout is EGM_POST_TIMEOUT_SEC (< ping cadence) so a
    black-holing endpoint fails fast instead of matching the enqueue rate.
  * GR-01 — 6 consecutive keepAlive POST failures demote onLine -> offline
    (ONE 'LINK DOWN' WARNING banner, meter subs marked inactive), further
    failures log at DEBUG with a suppressed counter, /api/status surfaces
    offline/offlineSince, and mark_link_down is idempotent.
  * GR-01 rejoin — a commsOnLine while OFFLINE still joins (no state
    precondition; live-proven 10:57:07) and logs the LINK RESTORED banner.
  * GR-01 probe recovery — a clean keepAlive probe ack while offline promotes
    straight back to onLine (asymmetric-heal path).
  * GR-14 — EVERY commsOnLine (this one carries NO reset flags, matching both
    live joins) marks the recorded meter subs inactive.
  * GR-15 — the rejoin banner is evidence-based: offline rejoin says
    'LINK RESTORED', only the short-gap rapid signature keeps the
    ack-rejection wording; 'was not accepted' is never hardcoded.
  * GR-27 — the setKeepAliveAck line no longer promises an EGM-originated
    pulse.

Run from G2S/:
    python3 tools/test_link_demotion.py
Exits 0 if all checks pass. No network beyond 127.0.0.1; no host process.
"""

import logging
import socket
import sys
import threading
import time
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import g2s_host as gh  # noqa: E402

EGM_ID = "IGT_00012E492815"

PASS = 0
FAIL = 0


def check(label, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ✅ {label}")
    else:
        FAIL += 1
        print(f"  ❌ {label}  {detail}")
    return cond


def now_iso():
    dt = datetime.now(timezone.utc)
    return dt.strftime("%Y-%m-%dT%H:%M:%S.") + f"{dt.microsecond // 1000:03d}Z"


class LogCapture(logging.Handler):
    def __init__(self):
        super().__init__(level=logging.DEBUG)
        self.records = []
        self._lock = threading.Lock()

    def emit(self, record):
        with self._lock:
            self.records.append((record.levelno, record.getMessage()))

    def count(self, substr, level=None):
        with self._lock:
            return sum(1 for lv, msg in self.records
                       if substr in msg and (level is None or lv == level))


def free_dead_port():
    """A localhost port with nothing listening — connects get ECONNREFUSED."""
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def comms_inner(child_xml, cid, stype="G2S_request", sid=None):
    sid = cid if sid is None else sid
    return (
        f'<g2s:g2sMessage xmlns:g2s="{gh.SCHEMA_NS}">\n'
        f' <g2s:g2sBody g2s:egmId="{EGM_ID}" g2s:dateTimeSent="{now_iso()}">\n'
        f'  <g2s:communications g2s:deviceId="1" g2s:commandId="{cid}" '
        f'g2s:sessionType="{stype}" g2s:sessionId="{sid}" '
        f'g2s:timeToLive="30000" g2s:dateTime="{now_iso()}">\n'
        f"   {child_xml}\n"
        f"  </g2s:communications>\n"
        f" </g2s:g2sBody>\n"
        f"</g2s:g2sMessage>"
    )


class AckingEgm(BaseHTTPRequestHandler):
    """Minimal fake EGM endpoint: clean message-level g2sAck to any POST."""

    def do_POST(self):
        self.rfile.read(int(self.headers.get("Content-Length", 0) or 0))
        body = (f'<g2s:g2sMessage xmlns:g2s="{gh.SCHEMA_NS}">'
                f'<g2s:g2sAck g2s:egmId="{EGM_ID}"/></g2s:g2sMessage>')
        data = body.encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/xml; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *a):
        pass


def main():
    logging.basicConfig(level=logging.WARNING,
                        format="%(asctime)s %(levelname)-7s %(message)s")
    cap = LogCapture()
    host_log = logging.getLogger("g2s_host")
    host_log.setLevel(logging.DEBUG)
    host_log.addHandler(cap)
    logging.getLogger("g2s_wire").setLevel(logging.CRITICAL)

    engine = gh.G2SHost(keepalive_ms=15000)
    assoc = engine.assoc(EGM_ID)
    dead_port = free_dead_port()
    dead_url = f"http://127.0.0.1:{dead_port}/"
    assoc.egm_location = dead_url
    assoc.comms_state = "onLine"
    assoc.comms_online_seen = 1
    assoc.last_comms_online = time.time() - 8 * 3600
    assoc.joined_ts = time.time()
    assoc.meter_subs = {"G2S_onPeriodic": {"active": True}}

    print("— GR-19: POST timeout below the ping cadence; fails fast")
    check("EGM_POST_TIMEOUT_SEC < HOST_KEEPALIVE_SEC",
          gh.EGM_POST_TIMEOUT_SEC < gh.HOST_KEEPALIVE_SEC,
          f"{gh.EGM_POST_TIMEOUT_SEC} !< {gh.HOST_KEEPALIVE_SEC}")
    # Black hole: listener that never accepts/answers — getresponse must
    # time out at EGM_POST_TIMEOUT_SEC, not the old 10s (== enqueue rate).
    hole = socket.socket()
    hole.bind(("127.0.0.1", 0))
    hole.listen(1)
    assoc.egm_location = f"http://127.0.0.1:{hole.getsockname()[1]}/"
    t0 = time.time()
    ok = engine.post_to_egm(assoc, "<g2s:getDescriptor/>", "getDescriptor(cid=0)")
    elapsed = time.time() - t0
    hole.close()
    check("black-holed POST returns False", not ok)
    check(f"black-holed POST timed out in ~{gh.EGM_POST_TIMEOUT_SEC}s "
          f"(took {elapsed:.1f}s)",
          gh.EGM_POST_TIMEOUT_SEC - 1 <= elapsed <= gh.EGM_POST_TIMEOUT_SEC + 3)
    check("non-keepAlive failure did not touch the demotion streak",
          assoc.keepalive_fail_streak == 0)
    assoc.egm_location = dead_url

    print("— GR-01: consecutive keepAlive failures demote onLine -> offline")
    for i in range(1, gh.KEEPALIVE_FAIL_DEMOTE + 1):
        engine.post_to_egm(assoc, "<g2s:keepAlive/>",
                           f"keepAlive(cid={i},sid={i})")
        if i < gh.KEEPALIVE_FAIL_DEMOTE:
            check(f"still onLine after failure {i}",
                  assoc.comms_state == "onLine", assoc.comms_state) \
                if i == gh.KEEPALIVE_FAIL_DEMOTE - 1 else None
    check(f"offline after {gh.KEEPALIVE_FAIL_DEMOTE} consecutive failures",
          assoc.comms_state == "offline", assoc.comms_state)
    check("offlineSince stamped", assoc.offline_since > 0)
    check("exactly ONE 'LINK DOWN' banner (WARNING)",
          cap.count("LINK DOWN", logging.WARNING) == 1,
          f"got {cap.count('LINK DOWN', logging.WARNING)}")
    check(f"pre-demotion failures stayed CRITICAL "
          f"(x{gh.KEEPALIVE_FAIL_DEMOTE})",
          cap.count("OUTBOUND POST FAILED for keepAlive(",
                    logging.CRITICAL) == gh.KEEPALIVE_FAIL_DEMOTE,
          f"got {cap.count('OUTBOUND POST FAILED for keepAlive(', logging.CRITICAL)}")
    check("demotion marked meter subs inactive",
          all(not v.get("active") and v.get("lost")
              for v in assoc.meter_subs.values()))

    print("— GR-01: while offline, failures are suppressed to DEBUG")
    crit_before = cap.count("OUTBOUND POST FAILED", logging.CRITICAL)
    engine.post_to_egm(assoc, "<g2s:keepAlive/>", "keepAlive(cid=90,sid=90)")
    engine.post_to_egm(assoc, "<g2s:keepAlive/>", "keepAlive(cid=91,sid=91)")
    check("no new CRITICALs while offline",
          cap.count("OUTBOUND POST FAILED", logging.CRITICAL) == crit_before)
    check("suppressed attempts logged at DEBUG",
          cap.count("offline — suppressed", logging.DEBUG) == 2,
          f"got {cap.count('offline — suppressed', logging.DEBUG)}")
    check("offline_suppressed counter tracks them",
          assoc.offline_suppressed == 2, assoc.offline_suppressed)

    print("— GR-01: /api/status snapshot surfaces the dead link")
    snap = assoc.snapshot()
    check("commsState == 'offline'", snap["commsState"] == "offline")
    check("offline flag true", snap["offline"] is True)
    check("offlineSince surfaced", bool(snap["offlineSince"]))
    check("offlineSuppressed surfaced", snap["offlineSuppressed"] == 2)

    print("— GR-01/GR-14/GR-15: a commsOnLine while OFFLINE still joins")
    assoc.meter_subs = {"G2S_onPeriodic": {"active": True}}  # re-seed stale
    epoch_before = assoc.epoch
    reply = engine.handle_g2s_message(
        comms_inner(f'<g2s:commsOnLine g2s:egmLocation="{dead_url}"/>', 2),
        EGM_ID)
    check("rejoin accepted — clean g2sAck (no errorCode)",
          "g2sAck" in reply and "errorCode" not in reply, reply)
    check("epoch bumped by the rejoin", assoc.epoch == epoch_before + 1)
    check("state left offline -> opening", assoc.comms_state == "opening",
          assoc.comms_state)
    check("comms_online_seen incremented", assoc.comms_online_seen == 2)
    check("GR-14: flag-less commsOnLine STILL marked meter subs inactive",
          all(not v.get("active") and v.get("lost")
              for v in assoc.meter_subs.values()))
    check("offline bookkeeping cleared by the new epoch",
          assoc.offline_since == 0 and assoc.keepalive_fail_streak == 0)
    check("GR-15: rejoin logged as LINK RESTORED (INFO)",
          cap.count("RE-HANDSHAKE #2 — LINK RESTORED", logging.INFO) == 1)
    check("GR-15: 'was not accepted' is never hardcoded",
          cap.count("was not accepted") == 0)

    print("— GR-15: only the short-gap rapid signature keeps the "
          "ack-rejection wording")
    reply = engine.handle_g2s_message(
        comms_inner(f'<g2s:commsOnLine g2s:egmLocation="{dead_url}"/>', 3),
        EGM_ID)
    check("rapid rejoin accepted too", "g2sAck" in reply)
    check("rapid rejoin logged as WARNING with the rejection hypothesis",
          cap.count("rapid re-join", logging.WARNING) == 1)

    print("— GR-01: mark_link_down is idempotent; probe success promotes back")
    # Let the rejoins' queued commsOnLineAck jobs drain first — building one
    # sets comms_state='sync(expected)' and would race the forced 'onLine'.
    deadline = time.time() + 5
    while assoc.send_queue.qsize() > 0 and time.time() < deadline:
        time.sleep(0.05)
    time.sleep(0.3)
    assoc.comms_state = "onLine"  # simulate the join completing
    engine.mark_link_down(assoc, "test: keepAliveAck silence")
    engine.mark_link_down(assoc, "test: double call")
    check("second mark_link_down is a no-op (still ONE new banner)",
          cap.count("LINK DOWN", logging.WARNING) == 2,
          f"got {cap.count('LINK DOWN', logging.WARNING)}")
    check("offline again", assoc.comms_state == "offline")
    egm_srv = ThreadingHTTPServer(("127.0.0.1", 0), AckingEgm)
    threading.Thread(target=egm_srv.serve_forever, daemon=True).start()
    assoc.egm_location = f"http://127.0.0.1:{egm_srv.server_address[1]}/"
    ok = engine.post_to_egm(assoc, "<g2s:keepAlive/>",
                            "keepAlive(cid=99,sid=99)")
    check("offline probe POST succeeded", ok)
    check("clean probe ack promoted offline -> onLine",
          assoc.comms_state == "onLine", assoc.comms_state)
    check("probe recovery logged ONE INFO banner",
          cap.count("LINK RESTORED — keepAlive probe answered",
                    logging.INFO) == 1)
    check("offline bookkeeping cleared", assoc.offline_since == 0)
    time.sleep(0.3)  # let the re-armed setMeterSub drain against the fake EGM
    egm_srv.shutdown()

    print("— GR-27: setKeepAliveAck line no longer promises an EGM pulse")
    engine.handle_g2s_message(
        comms_inner("<g2s:setKeepAliveAck/>", 40, stype="G2S_response",
                    sid=1001), EGM_ID)
    check("reworded: health tracked via keepAliveAck to our pings",
          cap.count("tracked via keepAliveAck", logging.INFO) == 1)
    check("old 'expect a keepAlive pulse' wording is gone",
          cap.count("expect a keepAlive pulse") == 0)

    print("=" * 50)
    print(f"RESULT: {PASS} passed, {FAIL} failed")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
