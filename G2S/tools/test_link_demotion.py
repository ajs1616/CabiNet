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
  * GR-31/GPE201 (D10) — the event-driven denom freshness hook
    (_game_play_event_refresh) is comms-gated: a GPE201 while OFFLINE
    enqueues nothing; back onLine it draws getGamePlayStatus + the scoped
    getGameDenoms, and a non-201 gamePlay event never re-reads denoms.
    (Self-contained twin of avp_replay Step 100 — the replay needs the
    private wire captures, this file is the public-repo net.)

Run from G2S/:
    python3 tools/test_link_demotion.py
Exits 0 if all checks pass. No network beyond 127.0.0.1; no host process.
"""

import html
import json
import logging
import os
import socket
import sqlite3
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
    """Minimal fake EGM endpoint: clean message-level g2sAck to any POST.
    Records every received body (class attr, arrival order) so slices can
    assert WHICH commands the host sent — the GR-31/GPE201 checks grep it."""

    seen = []  # raw decoded POST bodies, in arrival order

    def do_POST(self):
        body_in = self.rfile.read(
            int(self.headers.get("Content-Length", 0) or 0))
        AckingEgm.seen.append(body_in.decode("utf-8", "replace"))
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


def refuse_live_data():
    """Refuse to run against a REAL deployment's data directory.

    This gate is the one that builds an honest host — `G2SHost(...)` below, not
    a `__new__` stub — and `G2SHost.__init__` hardcodes every store path to
    `G2S/data/`. Run it inside a live hub's clone and it opens that operator's
    actual hub.db, voucher, WAT and account stores, runs the schema migration
    against the live database while the service holds it open, and puts the
    auto-registration path (`_aft_autoreg` -> `aft_register`) in reach of their
    real AFT/WAT keys. Re-registering those breaks cashless transfers with the
    machines until they are paired again — and GR-04 already proved this class
    of accident is not theoretical: a replay run once wrote 145 fixture voucher
    ids into a production voucher_state.json.

    FAIL-SAFE ON PURPOSE: it stops, rather than trusting a flag or a path
    parameter that a caller might forget. A throwaway checkout (`git worktree`,
    a fresh clone, CI) has an empty data dir and sails straight through, so the
    honest way to run this on a hub is to run it somewhere else."""
    data = os.path.join(os.path.dirname(os.path.abspath(gh.__file__)), "data")
    live = []
    db = os.path.join(data, "hub.db")
    if os.path.isfile(db):
        try:
            con = sqlite3.connect("file:%s?mode=ro" % db, uri=True, timeout=5)
            try:
                for tbl, what in (("machines", "registered machine"),
                                  ("aft_registrations", "AFT registration"),
                                  ("tito_tickets", "TITO ticket")):
                    try:
                        n = con.execute("SELECT count(*) FROM %s" % tbl).fetchone()[0]
                        if n:
                            live.append("%d %s row(s)" % (n, what))
                    except sqlite3.Error:
                        pass
            finally:
                con.close()
        except sqlite3.Error:
            pass
    # CONTENT, never file size: a store that has merely been INITIALISED (an
    # empty ledger, the default zero-balance "house" account) is what a
    # throwaway checkout looks like after one run, and refusing on that would
    # cry wolf until someone deleted the guard.
    def _json(name):
        p = os.path.join(data, name)
        if not os.path.isfile(p):
            return None
        try:
            with open(p, encoding="utf-8") as fh:
                return json.load(fh)
        except Exception:
            return None

    acct = _json("account_state.json") or {}
    accounts = acct.get("accounts") or {}
    real_accts = [a for a in accounts if a != "house"]
    house = accounts.get("house") or {}
    if real_accts:
        live.append("%d player wallet(s)" % len(real_accts))
    elif acct.get("ledger") or any(int(house.get(k) or 0) for k in
                                   ("cashableMillicents", "promoMillicents",
                                    "nonCashMillicents")):
        live.append("a funded/used House wallet")
    for name, what in (("wat_state.json", "WAT transfer records"),
                       ("voucher_state.json", "issued vouchers")):
        blob = _json(name)
        if isinstance(blob, dict) and any(
                v for v in blob.values() if isinstance(v, (list, dict))):
            live.append(what)
    if live:
        print("REFUSING TO RUN — %s looks like a LIVE deployment:" % data)
        for l in live:
            print("    · %s" % l)
        print("\n  This gate constructs a real G2SHost, whose stores are "
              "hardcoded to that\n  directory. Running here would open your "
              "hub.db, wallets, vouchers and WAT\n  state, and can reach the "
              "AFT auto-registration path — re-registering those\n  keys "
              "breaks cashless transfers until the machines are paired again."
              "\n\n  Run it against a throwaway checkout instead:\n"
              "    git worktree add --detach /tmp/cabinet-gate HEAD\n"
              "    cd /tmp/cabinet-gate/G2S && python3 tools/%s\n"
              "    git worktree remove --force /tmp/cabinet-gate\n"
              "\n  (deploy/update.py already does exactly this for you.)"
              % os.path.basename(__file__))
        sys.exit(2)


def main():
    refuse_live_data()
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

    print("— GR-31/GPE201 (D10): the freshness hook is comms-gated — "
          "offline enqueues NOTHING")
    # Seed a known gamePlay device (the hook keys on store membership). A
    # LIVE GPE201 for it while the link is DOWN must enqueue no reads: the
    # rejoin's own sweep refreshes state, and wire jobs must never pile up
    # against a dead EGM (dormancy). A regressed hook would either leave a
    # job queued (qsize) or drain it against the dead endpoint and bump
    # the offline-suppressed counter — both sides are pinned.
    assoc.game_play["77"] = {"themeId": "IGT_testTheme"}
    q_before = assoc.send_queue.qsize()
    sup_before = assoc.offline_suppressed
    engine._game_play_event_refresh(assoc, {
        "deviceClass": "G2S_gamePlay", "deviceId": "77",
        "eventCode": "G2S_GPE201"})
    time.sleep(0.3)
    check("offline GPE201 enqueued no reads (queue + suppressed counter "
          "both unchanged)",
          assoc.send_queue.qsize() == q_before
          and assoc.offline_suppressed == sup_before,
          f"qsize {assoc.send_queue.qsize()} suppressed "
          f"{assoc.offline_suppressed}")

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

    print("— GR-31/GPE201 (D10): back onLine the hook re-reads — status + "
          "SCOPED getGameDenoms; non-201 events never re-read denoms")
    # Same seeded dev 77 as the offline slice. Extra traffic (keepAlive
    # pings, the re-armed setMeterSub) may interleave — the checks grep
    # the recorded bodies rather than pinning exact counts.
    seen_mark = len(AckingEgm.seen)
    engine._game_play_event_refresh(assoc, {
        "deviceClass": "G2S_gamePlay", "deviceId": "77",
        "eventCode": "G2S_GPE201"})
    deadline = time.time() + 5
    while time.time() < deadline:
        if any("getGameDenoms" in b for b in AckingEgm.seen[seen_mark:]):
            break
        time.sleep(0.05)
    time.sleep(0.2)  # let the read pair finish draining
    # the host-originated wrap html-escapes the inner g2sMessage (quotes
    # included) — unescape before substring-matching attributes
    bodies = [html.unescape(b) for b in AckingEgm.seen[seen_mark:]]
    i_status = next((i for i, b in enumerate(bodies)
                     if "getGamePlayStatus" in b), None)
    i_denoms = next((i for i, b in enumerate(bodies)
                     if "getGameDenoms" in b), None)
    check("GPE201 for a known device drew the SCOPED getGameDenoms "
          "re-read (dev 77)",
          i_denoms is not None
          and 'g2s:deviceId="77"' in bodies[i_denoms],
          f"got {len(bodies)} POST(s), no dev-77 getGameDenoms")
    check("GR-31 getGamePlayStatus rode AHEAD of the denom re-read "
          "(hook enqueue order)",
          i_status is not None and i_denoms is not None
          and i_status < i_denoms,
          f"status@{i_status} denoms@{i_denoms}")
    # A non-201 gamePlay event (GPE103 game-start) re-reads STATUS only —
    # the denom re-read is GPE201-conditional, not a blanket ride-along.
    seen_mark = len(AckingEgm.seen)
    engine._game_play_event_refresh(assoc, {
        "deviceClass": "G2S_gamePlay", "deviceId": "77",
        "eventCode": "G2S_GPE103"})
    deadline = time.time() + 5
    while time.time() < deadline:
        if any("getGamePlayStatus" in b
               for b in AckingEgm.seen[seen_mark:]):
            break
        time.sleep(0.05)
    time.sleep(0.3)  # a wrongly-enqueued denom read would drain right behind
    bodies = AckingEgm.seen[seen_mark:]
    check("non-201 gamePlay event re-read status WITHOUT touching denoms",
          any("getGamePlayStatus" in b for b in bodies)
          and not any("getGameDenoms" in b for b in bodies),
          f"got {len(bodies)} POST(s)")

    print("— linked-leg census nudge: a G2S denom change (GPE201 / the "
          "accept fold) queues ONE read_machine_info to the LINKED SAS "
          "satellite — SAS is the denom authority on a linked cabinet")
    # Deterministic link state: whatever the dev hub.db seeded, this slice
    # owns the in-memory map (the sas_links idiom — no db write).
    engine.sas_links.clear()
    engine.__dict__.setdefault("_linked_census_asked", {}).clear()
    check("unlinked cabinet: the GPE201s above queued nothing SAS-side",
          not engine.sas_commands.get("smib-lk"),
          dict(engine.sas_commands))
    engine.sas_links[EGM_ID] = "smib-lk/1"
    with engine.sas_lock:
        engine.sas_machines["smib-lk/1"] = {"smibId": "smib-lk",
                                            "receivedAt": time.time()}
    engine._game_play_event_refresh(assoc, {
        "deviceClass": "G2S_gamePlay", "deviceId": "77",
        "eventCode": "G2S_GPE201"})
    q = list(engine.sas_commands.get("smib-lk") or [])
    check("GPE201 on a linked cabinet queued EXACTLY ONE read_machine_info "
          "for the SAS leg's satellite",
          len(q) == 1 and q[0].get("type") == "read_machine_info", q)
    engine._game_play_event_refresh(assoc, {
        "deviceClass": "G2S_gamePlay", "deviceId": "77",
        "eventCode": "G2S_GPE201"})
    q = list(engine.sas_commands.get("smib-lk") or [])
    check("a second GPE201 inside LINKED_CENSUS_DEBOUNCE_SEC does NOT "
          "double-fire", len(q) == 1, q)
    # the ACCEPT fold path: a sessionId-paired gameDenomList answering a
    # pending setActiveDenoms must nudge the leg too (debounce cleared so
    # the wiring — not the shared debounce — is what's under test). The
    # GR-28 persist is stubbed: this is a wiring pin, and the flusher
    # would write the REAL dev config inventory from a test.
    with assoc.lock:
        assoc.game_play["77"]["denomChange"] = {
            "denoms": ["1000"], "sessionId": "888", "sentAt": now_iso(),
            "sentTs": time.time(), "result": "sent"}
    engine._linked_census_asked.clear()
    _persist_real = engine._schedule_game_play_persist
    engine._schedule_game_play_persist = lambda a: None
    try:
        engine.handle_g2s_message(
            f'<g2s:g2sMessage xmlns:g2s="{gh.SCHEMA_NS}">\n'
            f' <g2s:g2sBody g2s:egmId="{EGM_ID}" '
            f'g2s:dateTimeSent="{now_iso()}">\n'
            f'  <g2s:gamePlay g2s:deviceId="77" g2s:commandId="600" '
            f'g2s:sessionType="G2S_response" g2s:sessionId="888" '
            f'g2s:timeToLive="30000" g2s:dateTime="{now_iso()}">\n'
            f'   <g2s:gameDenomList><g2s:gameDenom g2s:denomId="1000" '
            f'g2s:active="true"/></g2s:gameDenomList>\n'
            f"  </g2s:gamePlay>\n"
            f" </g2s:g2sBody>\n"
            f"</g2s:g2sMessage>", EGM_ID)
    finally:
        engine._schedule_game_play_persist = _persist_real
    with assoc.lock:
        dc_res = assoc.game_play["77"]["denomChange"]["result"]
    q = list(engine.sas_commands.get("smib-lk") or [])
    check("the fold advanced the verdict to accepted",
          dc_res == "accepted", dc_res)
    check("the ACCEPT fold nudged the linked leg too (a second "
          "read_machine_info queued)",
          len(q) == 2 and q[1].get("type") == "read_machine_info", q)
    engine.sas_links.pop(EGM_ID, None)     # leave the map as found

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
