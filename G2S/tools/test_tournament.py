#!/usr/bin/env python3
"""
test_tournament.py — standalone gate for TOURNAMENT MODE (board v2, 07-21):
the hub-side TournamentController in g2s_host.py — phase machine, seat
enumeration, the CLEAR -> FUND -> LOCK arm pipeline (AJ's 07-21
resolution directive: transfers never run against a locked machine),
staged countdown unlocks, wins-only scoring, the sweep-first finish with
settle-gated OVER locks, unlock-first cancel, reset, the runner thread,
and the /api surfaces (six tournament actions + the tournamentNames
roster setting).

In-process, no live host, no sockets, stdlib only — the
test_machine_linking.py recipe: G2SHost.__new__ sidesteps the constructor,
lightweight fakes stand in for the association and accounts, hub.db and
the WAT store are REAL temp-dir stores (the roster round-trip and the
funding-verdict observer read production SQL/JSON, not a fake's opinion),
_enqueue is replaced by an immediate build+capture so eng.sent holds the
REAL built XML per would-be G2S send, and sas_enqueue_command is captured
the same way. Tests drive _tournament_tick directly with injected clocks
(the sweep_card_sessions pattern) — the real runner thread gets its own
controlled slice.

Covers (the 07-21 resolution-directive choreography — transfers NEVER
run against a locked machine):
  * dormancy — a fresh engine holds an idle dict, runs no thread, and
    emits ZERO tournament bytes on either channel; the ARM itself sends
    NOTHING (the runner tick drives the pipeline).
  * the module-side hub_store extension ('tournament_names' whitelisted,
    value ceiling widened) actually took.
  * /api/settings tournamentNames — strict round-trip, junk 400s, []
    clears to defaults, echo is the EFFECTIVE roster.
  * tournamentConfigure — type junk 400 at the HTTP edge, happy path,
    phase gating (idle/armed only), reconfig re-arms nothing.
  * seat enumeration — g2s/linked/sas kinds, carded name wins, unique
    shuffled aliases + Mystery Guest exhaustion, skip rules (offline,
    stale, parked, dark, handpay lockup on either leg); machines holding
    credits SEAT (the CLEAR stage empties them — no meter-guess
    absorbedMc at enumerate).
  * the ARM PIPELINE (armStage state machine, tick-driven): CLEAR (host
    pull to the House; empty glass skips synchronously; the settled
    ACTUALS become absorbedMc) -> FUND (WAT push MILLICENTS /
    aft_transfer CENTS, House money, against a LIVE machine) -> LOCK
    (full-flag setCabinetState / sas_disable, the LAST act) -> ready;
    one WAT at a time per EGM, one satellite command per stage, no
    duplicate issues across ticks; honest stage failures park the seat.
  * tournamentStart REFUSES until every seat is ready, naming each
    laggard's stage; runner spawn; sweep-pin PRUNING at arm; run ids
    unique inside one wall-clock second.
  * countdown — sas_enable staged once at T-1.2 s, G2S unlock + running
    flip + score baselines at T0 (countdown-end is startup ONLY).
  * scoring — wonAmt deltas (g2s, mc), coinOut+jackpot × denom (sas),
    poll pacing, negative-delta re-baseline (score never lost), junk
    counter tolerance, mid-run card-in/out name refresh.
  * finish — SWEEP FIRST against the still-live floor, winner (max
    scoreMc, strict > = earliest-seated tie-break), then each seat's
    "TOURNAMENT OVER" lock only AFTER its own pull settles
    (_finishDone closes the book); the observer matches the satellite's
    REWRITTEN "aft_cashout" type; snapshot JSON-safety.
  * the sweep settle — House credited the confirmed amount ONCE, the
    pins CONSUMED with it (armed exactly once, never a standing route).
  * the finish settle LAW — a failed sweep is terminal (OVER lock still
    rides), the 60 s settle timeout locks SAS anyway ("verify by meter")
    but LEAVES a g2s seat with an open WAT unlocked (the freeze rule),
    a denied WAT clears newest_active so the lock proceeds; the book
    (_finishDone) closes in every case.
  * observer honesty — abandoned / error:* WAT states fail the stage
    instead of pending forever; a satellite answering no_credits is a
    $0 SUCCESS; every answered non-crediting pull RETIRES the leg's
    House pin (the 600 s TTL window closed).
  * ready-but-unlocked — a refused ARM lock leaves the honest face,
    START names it ("lock not confirmed yet"), the restage pass
    re-delivers (ARMED re-locks READY seats only).
  * a cancel superseding an arm pass — the losing pass re-opens the
    locks it issued and publishes nothing onto the dead run.
  * meter units — the clear ASKS the playerCashableAmt fold (genuine
    millicents) but absorbedMc and the carded refund key to the COMMIT
    ACTUALS (the $521k lesson: a meter guess never touches the ledger).
  * staging is idempotent-per-epoch — an epoch bump re-issues the
    dropped lock, a refused SAS unlock retries, throttled per seat;
    while ARMED only a "ready" seat is ever re-locked.
  * cancel — UNLOCK FIRST then pull (the directive's reverse of the
    bench-day order), leave unlocked; an in-flight clear/fund WAT skips
    the cancel pull (G2S_WTX008 one-at-a-time), idle with
    note="cancelled", seats kept for post-mortem; reset — finished/idle
    only, sas sweep pins survive for their in-flight settles.
  * the real runner thread — spawned once, is_alive-guarded, exits on
    idle (or a settled finish) and ADOPTS a successor run.
  * legacy engines without the tournament attrs keep settling cash-outs
    (the _sas_cashout_pin getattr guard).
  * the FROZEN stake (runCreditsCents): a mid-arm reconfigure reprices
    NOTHING — the slow seat funds at the armed amount.
  * the g2s CLEAR's fresh-read gate (the stale-mirror law), BOTH
    directions: a zero cold fold draws a scoped cabinet read before a
    $0 settle, and a fresh read revealing hidden credits pulls them.
  * arm-stage verdict TIMEOUTS: queue-expired satellite commands and
    silent EGMs park the seat honestly (never a forever-pending wedge),
    retiring the House pin / local-cancelling the dead WAT.
  * cancel sweeps ONLY cleared glasses (a never-cleared glass keeps the
    floor's own money) and never pulls behind a refused unlock.
  * the refund rides its STAMPED owner (refundAccountId at clear settle)
    and pays PER BUCKET (promo comes back as promo).
  * the ingest-side tournament-pin reaper (a dead run's answered pull
    retires its pin); a re-arm keeps a still-in-flight old pull's pin.
  * the linked seat's PER-LEG SAS lock face restages independently (the
    leg that actually bites on a partial-G2S cabinet), and a fresh
    EGM cabinet report contradicting the intent re-issues a dropped
    G2S lock POST.
  * START primes the g2s race counters; the runner clears its thread
    slot on exit (the spawn TOCTOU closed).

Run from G2S/:  python3 tools/test_tournament.py
Must end "RESULT: N passed, 0 failed".
"""

import itertools
import json
import logging
import os
import sys
import tempfile
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import g2s_host as gh  # noqa: E402
import hub_store  # noqa: E402
from hub_store import HubStore  # noqa: E402

logging.disable(logging.CRITICAL)   # the checks print, the engine doesn't

EGM = "IGT_00012E492815"            # the AVP's real egmId (never a theme)
WMS = "WMS_00:a0:a5:79:2d:a8"       # the BB2's real egmId
SAS = "smib-bb2/1"                  # the BB2's SAS leg (satellite key)

_p = _f = 0


def check(name, ok, detail=""):
    global _p, _f
    if ok:
        _p += 1
        print(f"  ✅ {name}")
    else:
        _f += 1
        print(f"  ❌ {name} {detail}")
    return ok


# ---------------------------------------------------------------- fakes

class FakeAssoc:
    """Just enough association for the tournament surface: the enumerate
    reads (comms_state, handpays_pending), the meter fold (meters under
    lock), and the REAL enqueue builders (id counters + the wat/config
    maps the owner-guard and mirror walk)."""

    def __init__(self, egm_id=EGM):
        self.egm_id = egm_id
        self.lock = threading.Lock()
        self.comms_state = "onLine"
        self.handpays_pending = []
        self.meters = {}
        self.host_command_id = 1000
        self.host_session_id = 5000
        self.descriptors = []
        self.config_devices = {}
        self.host_items = {}
        self.wat_devices = {}
        self.wat_transfers = []
        self.wat_transfer_count = 0

    def next_command_id(self):
        self.host_command_id += 1
        return self.host_command_id

    def next_session_id(self):
        self.host_session_id += 1
        return self.host_session_id


class FakeAccounts:
    """The AccountStore surface the tournament touches: get() for the
    House fund pre-check and the carded-name live resolve, plus a
    ref-idempotent adjust() so the sweep-settle slice can drive the REAL
    _settle_aft_cashouts (cashable bucket only — all the sweep moves)."""

    def __init__(self):
        self.records = {"house": {"id": "house", "kind": "house",
                                  "name": "The House",
                                  "cashableMillicents": 0}}
        self.refs = set()
        self.ref_deltas = {}    # ref -> [cashable, promo, nonCash] sums

    def get(self, account_id):
        rec = self.records.get(str(account_id or ""))
        return dict(rec) if rec else None

    def adjust(self, account_id, d_cash=0, d_promo=0, d_non=0, note="",
               ref="", once=False):
        rec = self.records.get(str(account_id or ""))
        if rec is None:
            return None, "no such account"
        if once and ref and ref in self.refs:
            return dict(rec), None
        if ref:
            self.refs.add(ref)
            d = self.ref_deltas.setdefault(ref, [0, 0, 0])
            d[0] += int(d_cash)
            d[1] += int(d_promo)
            d[2] += int(d_non)
        rec["cashableMillicents"] = \
            int(rec.get("cashableMillicents") or 0) + int(d_cash)
        # the promo/non buckets ride too — the per-bucket refund slice
        # asserts promo never launders into cashable
        rec["promoMillicents"] = \
            int(rec.get("promoMillicents") or 0) + int(d_promo)
        rec["nonCashMillicents"] = \
            int(rec.get("nonCashMillicents") or 0) + int(d_non)
        return dict(rec), None

    def ref_totals(self, ref):
        """The durable-ledger consult _settle_aft_cashouts leans on to be
        restart-proof — summed deltas under ONE ref, the real
        AccountStore's (cashable, promo, nonCash) tuple."""
        return tuple(self.ref_deltas.get(ref, (0, 0, 0)))


def make_engine():
    """A G2SHost carrying ONLY the tournament surface — __new__ sidesteps
    the constructor's real stores/files; hub.db + the WAT store are REAL
    in a temp dir (roster durability + the funding observer read the
    production stores). _enqueue is an immediate build+capture: eng.sent
    holds (inner_xml, label) per would-be G2S send in FIFO order;
    eng.sas_cmds holds (smib, cmd) per would-be satellite command. The
    runner spawn is stubbed to a recorder (eng.spawn_calls) so no thread
    ever runs under an injected-clock test — the real thread gets its own
    slice via the class method."""
    eng = gh.G2SHost.__new__(gh.G2SHost)
    eng.host_id = "1"
    d = tempfile.mkdtemp()
    eng.hub_store = HubStore(os.path.join(d, "hub.db"))
    eng.wat_store = gh.WatStore(os.path.join(d, "wat_state.json"))
    eng.account_store = FakeAccounts()
    eng.associations = {}
    eng.assoc_lock = threading.Lock()
    eng.sas_machines = {}
    eng.sas_lock = threading.Lock()
    eng.sas_links = {}
    eng.card_sessions = {}
    eng.companion_lock = threading.Lock()
    eng._fob_seq = itertools.count(1)
    eng._glass_pin_lock = threading.Lock()
    eng._glass_cash_out_pin = {}
    # the tournament state quartet __init__ owns in production
    eng.tournament = eng._tournament_idle_state()
    eng.tournament_lock = threading.Lock()
    eng._tournament_thread = None
    eng._tournament_sweep_pins = {}
    # the settle memos _settle_aft_cashouts rides (the sweep-consume slice)
    eng._aft_cashouts_settled = set()
    eng._aft_cashout_unconfirmed = set()
    # wire captures
    eng.sent = []
    eng._enqueue = lambda assoc, build, settle=0.0, epoch=None: \
        eng.sent.append(build(assoc))
    eng.sas_cmds = []
    eng.sas_refuse = None           # set to a string to refuse the queue

    def cap_sas(smib, cmd):
        eng.sas_cmds.append((smib, dict(cmd)))
        if eng.sas_refuse:
            return {"ok": False, "error": eng.sas_refuse}
        return {"ok": True, "id": cmd["id"], "queuedBehind": 0}
    eng.sas_enqueue_command = cap_sas
    eng.spawn_calls = []
    eng._tournament_spawn_runner = lambda run_id: \
        eng.spawn_calls.append(run_id)
    return eng


def sas_entry(smib="smib-bb2", coin_out=1000, jackpot=0, denom=5,
              from_egm=True, credits=0, **over):
    """A live sas_machines record in the spec-recipe shape; from_egm sets
    the 0x74 from-EGM bit the host pulls gate on. credits (0x1A) rests
    at ZERO — an empty glass lets the ARM pipeline's CLEAR stage settle
    synchronously; give it credits to exercise the pull."""
    e = {"receivedAt": time.time(), "smibId": smib,
         "meters": {"0x1A": credits, "coinOut": coin_out,
                    "jackpot": jackpot},
         "denomCents": denom, "sasEnabled": True, "commandResults": []}
    if from_egm:
        e["aft"] = {"availXfers": gh.AVAIL_XFER_FROM_EGM}
    e.update(over)
    return e


def settings(eng, payload):
    """Drive the REAL /api/settings handler socket-free — returns
    (http_code, decoded_body)."""
    h = gh.G2SRequestHandler.__new__(gh.G2SRequestHandler)
    h.host_engine = eng
    sent = []
    h._send = lambda code, body, ctype=None, soap=True: \
        sent.append((code, json.loads(body)))
    h._handle_settings(json.dumps(payload))
    return sent[0]


def command(eng, payload):
    """Drive the REAL /api/command handler socket-free — the tournament
    actions dispatch hub-wide BEFORE the assoc lookup, so the fake engine
    suffices. Returns (http_code, decoded_body)."""
    h = gh.G2SRequestHandler.__new__(gh.G2SRequestHandler)
    h.host_engine = eng
    sent = []
    h._send = lambda code, body, ctype=None, soap=True: \
        sent.append((code, json.loads(body)))
    h._handle_command(json.dumps(payload))
    return sent[0]


def labels(sent, start=0):
    """Bare command names of the captured G2S sends from index `start`."""
    return [lbl.split("(", 1)[0] for _xml, lbl in sent[start:]]


def sas_types(cmds, start=0):
    return [c["type"] for _smib, c in cmds[start:]]


def wat_poke(eng, egm, rid, state="committed", cash=None, promo=None):
    """Poke a WAT record the way the real commit dispatch would — the
    observers read ONLY the store, so a state flip (plus transCashableAmt/
    transPromoAmt actuals for a pull) is exactly what the wire would
    leave behind."""
    with eng.wat_store.lock:
        for rec in eng.wat_store.state["transfers"]:
            if rec.get("egmId") == egm and rec.get("requestId") == rid:
                rec["state"] = state
                if cash is not None:
                    rec["transCashableAmt"] = int(cash)
                if promo is not None:
                    rec["transPromoAmt"] = int(promo)


def sas_result(eng, key, cmd_id, rtype, **fields):
    """Append a satellite commandResult the way the report ingest would."""
    with eng.sas_lock:
        eng.sas_machines[key]["commandResults"].append(
            dict({"id": cmd_id, "type": rtype}, **fields))


def march_ready(eng, clear_amounts=None, now=None):
    """Drive every seat's ARM pipeline to armStage='ready': tick (issues
    the stage wire), poke the stores the way the real settles would, tick
    again. clear_amounts = {seatKey: settled mc} for seats whose CLEAR
    actually pulls (credit-holding glass)."""
    now = time.time() if now is None else now
    amounts = clear_amounts or {}
    for _ in range(5):
        eng._tournament_tick(now=now)
        # the g2s CLEAR's fresh-read gate (the stale-mirror law): pretend
        # every machine answered the scoped cabinet read right after the
        # ask — meters_at is the only thing the gate trusts
        for a in eng.associations.values():
            a.meters_at = time.time()
        seats = eng.tournament["seats"]
        if all(s.get("armStage") == "ready" for s in seats.values()):
            return
        for k, s in list(seats.items()):
            tr = s.get("clear") or {}
            if s.get("armStage") == "clear" and tr.get("state") == "pulling":
                mc = int(amounts.get(k, 0))
                if s.get("kind") == "g2s":
                    wat_poke(eng, k, tr.get("requestId"), cash=mc)
                else:
                    sas_result(eng, s["sasKey"], tr.get("cmdId"),
                               "aft_cashout", ok=True, outcome="completed",
                               amountCents=mc // 1000, txnId=f"clr-{k}")
            if s.get("armStage") == "fund":
                if s.get("kind") == "g2s" and s.get("fundRequestId"):
                    wat_poke(eng, k, s["fundRequestId"])
                elif s.get("fundCmdId"):
                    sas_result(eng, s["sasKey"], s["fundCmdId"],
                               "aft_transfer", ok=True, outcome="completed")
    raise AssertionError("arm pipeline never reached ready: " + repr(
        {k: s.get("armStage") for k, s in eng.tournament["seats"].items()}))


def main():
    # ---------------------------------------------------------------
    print("— module-side hub_store extension took at import")
    check("'tournament_names' whitelisted in HOST_SETTING_KEYS",
          "tournament_names" in hub_store.HOST_SETTING_KEYS)
    check("value ceiling widened for the roster JSON (>= 4096)",
          hub_store.MAX_SETTING_VAL_LEN >= 4096,
          hub_store.MAX_SETTING_VAL_LEN)

    # ---------------------------------------------------------------
    print("— dormancy: a fresh engine is inert")
    eng = make_engine()
    check("fresh tournament dict rests idle with no seats",
          eng.tournament.get("phase") == "idle"
          and eng.tournament.get("seats") == {}
          and eng.tournament.get("id") is None, eng.tournament)
    snap = eng.tournament_snapshot()
    check("snapshot phase idle + serverNow (the board's attract gate)",
          snap.get("phase") == "idle"
          and isinstance(snap.get("serverNow"), float), snap)
    try:
        json.dumps(snap, allow_nan=False)
        check("idle snapshot is JSON-safe (allow_nan=False)", True)
    except ValueError as e:
        check("idle snapshot is JSON-safe (allow_nan=False)", False, str(e))
    check("zero sends on either channel, no runner thread",
          not eng.sent and not eng.sas_cmds
          and eng._tournament_thread is None)
    check("roster defaults when unset (12 shipped names)",
          eng.tournament_names() == list(gh.TOURNAMENT_NAME_DEFAULTS))

    # ---------------------------------------------------------------
    print("— /api/settings tournamentNames round-trip")
    code, body = settings(eng, {"tournamentNames": ["Alpha", "  Beta  "]})
    check("write answers 200 and echoes the stripped effective roster",
          code == 200 and body.get("tournamentNames") == ["Alpha", "Beta"],
          (code, body))
    check("stored as ONE compact JSON array in hub.db",
          eng.hub_store.host_setting("tournament_names")
          == '["Alpha","Beta"]',
          eng.hub_store.host_setting("tournament_names"))
    check("tournament_names() reads it back",
          eng.tournament_names() == ["Alpha", "Beta"])
    for payload, why in (
            ({"tournamentNames": "junk"}, "non-list"),
            ({"tournamentNames": [1, 2]}, "non-string entries"),
            ({"tournamentNames": ["ok", ""]}, "blank entry"),
            ({"tournamentNames": ["x" * 25]}, "over-24-char name"),
            ({"tournamentNames": ["n"] * 65}, "over-64 entries")):
        code, body = settings(eng, payload)
        check(f"{why} -> 400", code == 400 and body.get("ok") is False,
              (code, body))
    check("rejected writes leave the roster intact",
          eng.tournament_names() == ["Alpha", "Beta"])
    # a full legal roster (64 × 24 chars ≈ 1.7 KB JSON) needs the widened
    # value ceiling — this write FAILING would mean the module-side patch
    # regressed to the old 256-char bound.
    big = [f"Player Number {i:02d} Here"[:24] for i in range(64)]
    code, body = settings(eng, {"tournamentNames": big})
    check("64 × 24-char roster write lands (widened ceiling in anger)",
          code == 200 and body.get("tournamentNames") == big, code)
    code, body = settings(eng, {"tournamentNames": []})
    check("[] clears back to defaults (echo = effective roster)",
          code == 200 and body.get("tournamentNames")
          == list(gh.TOURNAMENT_NAME_DEFAULTS), (code, body))
    check("clear stores the unset sentinel (empty string)",
          eng.hub_store.host_setting("tournament_names") == "")
    code, body = settings(eng, {})
    check("no-field settings post names tournamentNames in its 400",
          code == 400 and "tournamentNames" in str(body.get("error")),
          (code, body))

    # ---------------------------------------------------------------
    print("— tournamentConfigure: type junk 400, happy path, phase gates")
    for payload, why in (
            ({"creditsCents": "500", "durationSec": 60}, "string credits"),
            ({"creditsCents": True, "durationSec": 60}, "bool credits"),
            ({"creditsCents": 0, "durationSec": 60}, "zero credits"),
            ({"creditsCents": 500.5, "durationSec": 60}, "float credits"),
            ({"creditsCents": gh.G2SHost.SAS_AFT_MAX_CENTS + 1,
              "durationSec": 60}, "credits over the wire max"),
            ({"durationSec": 60}, "missing credits"),
            ({"creditsCents": 500}, "missing duration"),
            ({"creditsCents": 500, "durationSec": 29}, "duration under 30"),
            ({"creditsCents": 500, "durationSec": 7201},
             "duration over 7200"),
            ({"creditsCents": 500, "durationSec": 60, "countdownSec": 2},
             "countdown under 3"),
            ({"creditsCents": 500, "durationSec": 60, "countdownSec": 61},
             "countdown over 60"),
            ({"creditsCents": 500, "durationSec": 60, "countdownSec": "5"},
             "string countdown")):
        code, body = command(eng, dict(payload, action="tournamentConfigure"))
        check(f"{why} -> 400", code == 400 and body.get("ok") is False,
              (code, body))
    check("rejected configures never touch the dict",
          eng.tournament.get("configuredAt") is None
          and eng.tournament.get("creditsCents") == 0)
    code, body = command(eng, {"action": "tournamentConfigure",
                               "creditsCents": 500, "durationSec": 60})
    check("happy configure (no countdown) -> 200 ok, default countdown 10",
          code == 200 and body.get("ok") is True
          and body.get("phase") == "idle"
          and body.get("creditsCents") == 500
          and body.get("durationSec") == 60
          and body.get("countdownSec") == 10, (code, body))
    code, body = command(eng, {"action": "tournamentConfigure",
                               "creditsCents": 500, "durationSec": 60,
                               "countdownSec": 5})
    check("explicit countdown lands", code == 200
          and body.get("countdownSec") == 5, (code, body))
    code, body = command(eng, {"action": "tournamentConfigure",
                               "creditsCents": 500, "durationSec": 60})
    check("omitted countdown KEEPS the prior value (not reset to 10)",
          code == 200 and body.get("countdownSec") == 5, (code, body))
    code, body = command(eng, {"action": "tournamentFoo"})
    check("unknown tournament action -> 400 advertising the real six",
          code == 400 and "tournamentArm" in (body.get("actions") or []),
          (code, body))

    # ---------------------------------------------------------------
    print("— tournamentArm refusals")
    bare = make_engine()
    code, body = command(bare, {"action": "tournamentArm"})
    check("unconfigured arm refused ok:false at 200",
          code == 200 and body.get("ok") is False
          and "configure" in str(body.get("error")), (code, body))
    # eng is configured but has no machines at all
    code, body = command(eng, {"action": "tournamentArm"})
    check("no seatable machines -> ok:false at 200, rests back idle",
          code == 200 and body.get("ok") is False
          and "no seatable" in str(body.get("error"))
          and eng.tournament.get("phase") == "idle"
          and eng.tournament.get("id") is None, (code, body))
    check("refused arms emit nothing and spawn nothing",
          not eng.sent and not eng.sas_cmds and not eng.spawn_calls)

    # ---------------------------------------------------------------
    print("— arm: seats published at 'clear', NO wire until the tick")
    eng.associations[EGM] = FakeAssoc(EGM)
    with eng.sas_lock:
        eng.sas_machines[SAS] = sas_entry()
    # sweep pins from a previous run: one in-flight on a leg NOT seated
    # here (must SURVIVE — its settle still needs a home), one on the leg
    # being re-seated (must drop — a stale pin would outrank a mid-run
    # player arm), one long past the TTL (housekeeping).
    eng._tournament_sweep_pins = {
        "smib-bb1/1": {"acct": "house", "ts": time.time()},
        SAS: {"acct": "house", "ts": time.time()},
        "smib-bb9/1": {"acct": "house",
                       "ts": time.time()
                       - gh.G2SHost.TOURNAMENT_SWEEP_PIN_TTL_SEC - 5}}
    code, body = command(eng, {"action": "tournamentArm"})
    run_id = body.get("tournamentId")
    check("arm answers 200 phase=armed with 2 seats and a run id",
          code == 200 and body.get("ok") is True
          and body.get("phase") == "armed" and body.get("seats") == 2
          and bool(run_id), (code, body))
    check("run ids carry the hub-wide sequence past 1 s resolution",
          "-" in str(run_id), run_id)
    check("arm PRUNES the sweep-pin map, never swaps it: the unseated "
          "in-flight pin survives, the re-seated + expired ones drop",
          set(eng._tournament_sweep_pins) == {"smib-bb1/1"},
          eng._tournament_sweep_pins)
    check("runner spawned exactly once, for this run",
          eng.spawn_calls == [run_id], eng.spawn_calls)
    check("the arm ITSELF emits nothing on either channel — the runner "
          "tick is the pipeline's single driver",
          not eng.sent and not eng.sas_cmds,
          (labels(eng.sent), sas_types(eng.sas_cmds)))
    seats = eng.tournament["seats"]
    g_seat = seats.get(EGM) or {}
    s_seat = seats.get("sas:" + SAS) or {}
    check("both seats published: kinds g2s + sas, armStage='clear', "
          "UNLOCKED (the lock is the pipeline's LAST act), fund pending",
          g_seat.get("kind") == "g2s" and s_seat.get("kind") == "sas"
          and g_seat.get("armStage") == "clear"
          and s_seat.get("armStage") == "clear"
          and g_seat.get("locked") is False
          and s_seat.get("locked") is False
          and g_seat.get("funded") == "pending"
          and s_seat.get("funded") == "pending", seats.keys())
    check("nobody carded: names ARE the aliases, unique, from the roster",
          g_seat.get("name") == g_seat.get("alias")
          and s_seat.get("name") == s_seat.get("alias")
          and g_seat.get("alias") != s_seat.get("alias")
          and g_seat.get("alias") in gh.TOURNAMENT_NAME_DEFAULTS
          and s_seat.get("alias") in gh.TOURNAMENT_NAME_DEFAULTS,
          (g_seat.get("alias"), s_seat.get("alias")))
    code, body = command(eng, {"action": "tournamentStart"})
    check("START mid-pipeline refused, naming every seat's stage",
          code == 200 and body.get("ok") is False
          and "not every seat is ready" in str(body.get("error"))
          and (body.get("seats") or {}).get(EGM, {}).get("armStage")
          == "clear", (code, body))

    # ---------------------------------------------------------------
    print("— ticks 1+2: the g2s CLEAR confirms its zero fold FRESH, the "
          "sas CLEAR skips on the live 0x1A, FUND rides — no lock")
    eng._tournament_tick(now=time.time())
    check("g2s: a ZERO cold fold never settles the clear — the tick asks "
          "the machine itself (scoped G2S_cabinet read; a bill fed in "
          "during the 60 s standing-sub window is invisible to the "
          "mirror) and the seat waits at 'clear'",
          labels(eng.sent) == ["getMeterInfo"]
          and 'g2s:deviceClass="G2S_cabinet"' in eng.sent[0][0]
          and eng.tournament["seats"][EGM]["armStage"] == "clear"
          and "confirming" in eng.tournament["seats"][EGM]["stageDetail"],
          (labels(eng.sent),
           eng.tournament["seats"][EGM].get("stageDetail")))
    check("sas: CLEAR skipped on 0x1A=0 (the ~1 s reports ARE fresh), "
          "aft_transfer CENTS from the House on the leg's smib — and NO "
          "sas_disable near it (the machine must be LIVE for the "
          "transfer, the 07-21 bench law)",
          sas_types(eng.sas_cmds) == ["aft_transfer"]
          and eng.sas_cmds[0][1].get("cents") == 500
          and eng.sas_cmds[0][1].get("accountId") == "house"
          and all(s == "smib-bb2" for s, _c in eng.sas_cmds),
          eng.sas_cmds)
    # the machine answers the scoped read: nothing on the glass — the
    # fold is now trustworthy and the $0 clear may settle
    eng.associations[EGM].meters_at = time.time()
    eng._tournament_tick(now=time.time())
    check("the post-ask reading confirms empty: CLEAR settles at $0 and "
          "the FUND rides out — WAT toEgm push, House, MILLICENTS "
          "(500¢ x1000)",
          labels(eng.sent) == ["getMeterInfo", "initiateRequest"]
          and 'g2s:watDirection="G2S_toEgm"' in eng.sent[1][0]
          and 'g2s:accountId="house"' in eng.sent[1][0]
          and 'g2s:reqCashableAmt="500000"' in eng.sent[1][0],
          (labels(eng.sent)))
    seats = eng.tournament["seats"]
    g_seat = seats[EGM]
    s_seat = seats["sas:" + SAS]
    check("both seats at armStage='fund', clear settled at $0 actuals",
          g_seat.get("armStage") == "fund"
          and s_seat.get("armStage") == "fund"
          and g_seat.get("absorbedMc") == 0
          and (g_seat.get("clear") or {}).get("state") == "done"
          and (s_seat.get("clear") or {}).get("state") == "done",
          {k: s.get("armStage") for k, s in seats.items()})
    check("fund tracking ids recorded (WAT requestId / sas cmd id)",
          isinstance(g_seat.get("fundRequestId"), int)
          and s_seat.get("fundCmdId") == eng.sas_cmds[0][1]["id"],
          (g_seat.get("fundRequestId"), s_seat.get("fundCmdId")))
    n_sent, n_sas = len(eng.sent), len(eng.sas_cmds)
    eng._tournament_tick(now=time.time())
    check("a verdictless tick re-issues NOTHING (stages are once-only)",
          len(eng.sent) == n_sent and len(eng.sas_cmds) == n_sas
          and eng.tournament["seats"][EGM]["funded"] == "pending")

    # ---------------------------------------------------------------
    print("— verdicts land -> LOCK is the LAST act -> ready")
    wat_poke(eng, EGM, g_seat["fundRequestId"])
    sas_result(eng, SAS, s_seat["fundCmdId"], "aft_transfer",
               ok=True, outcome="completed")
    eng._tournament_tick(now=time.time())
    check("g2s: fund committed -> the ARM lock follows (FULL-flag "
          "setCabinetState enable=false, money flags true, show line)",
          labels(eng.sent, n_sent) == ["setCabinetState",
                                       "getCabinetStatus"]
          and 'g2s:enable="false"' in eng.sent[n_sent][0]
          and 'g2s:enableMoneyIn="true"' in eng.sent[n_sent][0]
          and 'g2s:enableMoneyOut="true"' in eng.sent[n_sent][0]
          and f'g2s:disableText="{gh.TOURNAMENT_ARM_TEXT}"'
          in eng.sent[n_sent][0], labels(eng.sent, n_sent))
    check("sas: aft_transfer completed -> sas_disable follows",
          sas_types(eng.sas_cmds, n_sas) == ["sas_disable"],
          eng.sas_cmds[n_sas:])
    seats = eng.tournament["seats"]
    check("both seats REST at ready: locked, funded ok, honest detail",
          all(s.get("armStage") == "ready" and s.get("locked") is True
              and s.get("funded") == "ok"
              and s.get("stageDetail") == "cleared + funded + locked"
              for s in seats.values()),
          {k: (s.get("armStage"), s.get("locked"))
           for k, s in seats.items()})
    check("fund verdicts folded with their detail",
          seats[EGM].get("fundDetail") == "WAT committed"
          and seats["sas:" + SAS].get("fundDetail") == "AFT completed",
          (seats[EGM].get("fundDetail"),
           seats["sas:" + SAS].get("fundDetail")))
    n_sent, n_sas = len(eng.sent), len(eng.sas_cmds)
    eng._tournament_tick(now=time.time())
    check("a settled ARMED floor is QUIET (nothing re-issues on its own)",
          len(eng.sent) == n_sent and len(eng.sas_cmds) == n_sas)
    code, body = command(eng, {"action": "tournamentArm"})
    check("double-arm refused ok:false at 200, zero extra wire",
          code == 200 and body.get("ok") is False
          and len(eng.sent) == n_sent and len(eng.sas_cmds) == n_sas,
          (code, body))
    code, body = command(eng, {"action": "tournamentConfigure",
                               "creditsCents": 500, "durationSec": 120})
    check("reconfigure while armed allowed, re-arms nothing",
          code == 200 and body.get("ok") is True
          and body.get("phase") == "armed"
          and len(eng.sent) == n_sent and len(eng.sas_cmds) == n_sas,
          (code, body))

    # ---------------------------------------------------------------
    print("— the CLEAR pull in anger: loaded glasses ride home first")
    cl = make_engine()
    cl.associations[EGM] = FakeAssoc(EGM)
    with cl.associations[EGM].lock:
        cl.associations[EGM].meters[
            "G2S_cabinet/1/G2S_playerCashableAmt"] = "30000"
    with cl.sas_lock:
        cl.sas_machines[SAS] = sas_entry(credits=40)   # 40 cr × 5¢ parked
    command(cl, {"action": "tournamentConfigure",
                 "creditsCents": 500, "durationSec": 60})
    command(cl, {"action": "tournamentArm"})
    cl._tournament_tick(now=time.time())
    check("g2s CLEAR = the HOST-INITIATED WAT pull (fromEgm, House on the "
          "record, ask = the playerCashableAmt fold) — and NO fund yet",
          labels(cl.sent) == ["initiateRequest"]
          and 'g2s:watDirection="G2S_fromEgm"' in cl.sent[0][0]
          and 'g2s:accountId="house"' in cl.sent[0][0]
          and 'g2s:reqCashableAmt="30000"' in cl.sent[0][0],
          labels(cl.sent))
    check("sas CLEAR = aft_cashout_pull with the tournament pin set FIRST",
          sas_types(cl.sas_cmds) == ["aft_cashout_pull"]
          and (cl._tournament_sweep_pins.get(SAS) or {}).get("acct")
          == "house", (cl.sas_cmds, cl._tournament_sweep_pins))
    g_clear = dict(cl.tournament["seats"][EGM]["clear"])
    s_clear = dict(cl.tournament["seats"]["sas:" + SAS]["clear"])
    n_sent, n_sas = len(cl.sent), len(cl.sas_cmds)
    cl._tournament_tick(now=time.time())
    check("while the pulls are in flight NOTHING else rides (one WAT at a "
          "time per EGM — G2S_WTX008; one satellite command per stage)",
          len(cl.sent) == n_sent and len(cl.sas_cmds) == n_sas
          and cl.tournament["seats"][EGM]["armStage"] == "clear")
    # the satellite REWRITES the pull verdict's type to "aft_cashout" —
    # a record under the raw command type must NOT satisfy the observer
    sas_result(cl, SAS, s_clear["cmdId"], "aft_cashout_pull",
               ok=True, outcome="completed", amountCents=200)
    cl._tournament_tick(now=time.time())
    check("a verdict under the un-rewritten type is IGNORED (the observer "
          "keys on 'aft_cashout' — the ordering trace's catch)",
          cl.tournament["seats"]["sas:" + SAS]["armStage"] == "clear",
          cl.tournament["seats"]["sas:" + SAS])
    wat_poke(cl, EGM, g_clear["requestId"], cash=30000)
    sas_result(cl, SAS, s_clear["cmdId"], "aft_cashout",
               ok=True, outcome="completed", amountCents=200,
               txnId="clr-77")
    cl._tournament_tick(now=time.time())
    seats = cl.tournament["seats"]
    check("clear ACTUALS become absorbedMc (g2s commit actuals / sas "
          "confirmed cents×1000) and only THEN do the funds ride",
          seats[EGM]["absorbedMc"] == 30000
          and seats["sas:" + SAS]["absorbedMc"] == 200000
          and labels(cl.sent, n_sent) == ["initiateRequest"]
          and 'g2s:watDirection="G2S_toEgm"' in cl.sent[n_sent][0]
          and sas_types(cl.sas_cmds, n_sas) == ["aft_transfer"],
          {k: s.get("absorbedMc") for k, s in seats.items()})
    march_ready(cl)
    code, body = command(cl, {"action": "tournamentStart"})
    check("fund verdicts + locks later the floor RESTS ready, START opens",
          code == 200 and body.get("phase") == "countdown", (code, body))

    # ---------------------------------------------------------------
    print("— a satellite answering no_credits: a $0 success, pin retired")
    nc = make_engine()
    with nc.sas_lock:
        # the 0x1A shows 3 credits but the satellite finds an empty glass
        # (the report meters lag ~1 s behind a live cash-out) —
        # no_credits is an ANSWER, not a failure
        nc.sas_machines[SAS] = sas_entry(credits=3)
    command(nc, {"action": "tournamentConfigure",
                 "creditsCents": 500, "durationSec": 60})
    command(nc, {"action": "tournamentArm"})
    nc._tournament_tick(now=time.time())
    tr = dict(nc.tournament["seats"]["sas:" + SAS]["clear"])
    check("a credit-showing leg pulls its clear (pin set first)",
          sas_types(nc.sas_cmds) == ["aft_cashout_pull"]
          and tr.get("state") == "pulling"
          and (nc._tournament_sweep_pins.get(SAS) or {}).get("acct")
          == "house", (nc.sas_cmds, tr))
    sas_result(nc, SAS, tr["cmdId"], "aft_cashout",
               ok=False, outcome="no_credits")
    nc._tournament_tick(now=time.time())
    seat = nc.tournament["seats"]["sas:" + SAS]
    check("no_credits settles the CLEAR as a $0 success — the stage "
          "advances and the fund rides",
          (seat.get("clear") or {}).get("state") == "done"
          and seat.get("absorbedMc") == 0
          and seat.get("armStage") == "fund"
          and sas_types(nc.sas_cmds, 1) == ["aft_transfer"], seat)
    check("…and the answered non-crediting pull RETIRES the House pin "
          "(nothing will ever settle it — the 600 s TTL window where it "
          "outranked a player's own glass arm is CLOSED)",
          SAS not in nc._tournament_sweep_pins,
          nc._tournament_sweep_pins)

    # ---------------------------------------------------------------
    print("— the ask is a hint, the COMMIT ACTUALS are the ledger truth")
    aa = make_engine()
    aa.associations[EGM] = FakeAssoc(EGM)
    with aa.associations[EGM].lock:
        aa.associations[EGM].meters[
            "G2S_cabinet/1/G2S_playerCashableAmt"] = "30000"
    aa.card_sessions[EGM] = {"uid": "6CB16F06", "name": "AJ",
                             "accountId": "p1", "since": time.time()}
    aa.account_store.records["p1"] = {"id": "p1", "kind": "player",
                                      "name": "AJ",
                                      "cashableMillicents": 0}
    command(aa, {"action": "tournamentConfigure", "creditsCents": 100,
                 "durationSec": 60, "countdownSec": 3})
    command(aa, {"action": "tournamentArm"})
    aa._tournament_tick(now=time.time())
    tr = dict(aa.tournament["seats"][EGM]["clear"])
    check("the clear ASKS the playerCashableAmt fold — genuine "
          "MILLICENTS, recorded on the tracker (the 07-21 meter tracer's "
          "verdict)", tr.get("askedMc") == 30000, tr)
    # the EGM commits LESS than the ask (reduceAmts — the meter latched
    # HIGH; the $521k lesson: a meter guess never touches the ledger)
    wat_poke(aa, EGM, tr["requestId"], cash=22000)
    march_ready(aa)
    check("absorbedMc = the COMMIT ACTUALS (22000 mc), never the ask",
          aa.tournament["seats"][EGM]["absorbedMc"] == 22000,
          aa.tournament["seats"][EGM])
    command(aa, {"action": "tournamentStart"})
    aa._tournament_tick(now=aa.tournament["startsAt"])
    aa._tournament_tick(now=aa.tournament["endsAt"] + 0.1)
    check("the carded refund keys to the SAME actuals",
          aa.account_store.records["p1"]["cashableMillicents"] == 22000,
          dict(aa.account_store.records))

    # ---------------------------------------------------------------
    print("— a wat-class error parks the clear honestly (observer law)")
    ce = make_engine()
    ce.associations[EGM] = FakeAssoc(EGM)
    with ce.associations[EGM].lock:
        ce.associations[EGM].meters[
            "G2S_cabinet/1/G2S_playerCashableAmt"] = "10000"
    command(ce, {"action": "tournamentConfigure",
                 "creditsCents": 500, "durationSec": 60})
    command(ce, {"action": "tournamentArm"})
    ce._tournament_tick(now=time.time())
    rid = ce.tournament["seats"][EGM]["clear"]["requestId"]
    wat_poke(ce, EGM, rid, state="error:G2S_WAX001")
    ce._tournament_tick(now=time.time())
    seat = ce.tournament["seats"][EGM]
    check("an error:* WAT record FAILS the clear (the old observer "
          "waited on these forever) — seat parks, no fund ever rides, "
          "never locked",
          (seat.get("clear") or {}).get("state") == "failed"
          and seat.get("armStage") == "failed"
          and seat.get("stageDetail", "").startswith("clear: WAT error:")
          and labels(ce.sent) == ["initiateRequest"]
          and seat.get("locked") is False, seat)

    # ---------------------------------------------------------------
    print("— a seat whose glass CANNOT clear parks honestly")
    fc = make_engine()
    with fc.sas_lock:
        fc.sas_machines[SAS] = sas_entry(from_egm=False, credits=9)
    command(fc, {"action": "tournamentConfigure",
                 "creditsCents": 500, "durationSec": 60})
    command(fc, {"action": "tournamentArm"})
    fc._tournament_tick(now=time.time())
    seat = fc.tournament["seats"]["sas:" + SAS]
    # 07-21 bench: NO cached-availXfers pre-flight — a lock/unlock cycle
    # leaves the cached bit honestly reading 0 on a machine that IS
    # menu-set (the stranded-$1000 skip). The pull goes to the WIRE and
    # the satellite's live 0x74 adjudicates: the clear rests "pulling"
    # until the refusal verdict rides a report back.
    check("an un-menu-set leg HOLDING credits sends the pull to the wire "
          "— the satellite's LIVE 0x74 adjudicates, never a cached bit",
          seat.get("armStage") == "clear"
          and (seat.get("clear") or {}).get("state") == "pulling"
          and sas_types(fc.sas_cmds) == ["aft_cashout_pull"]
          and seat.get("locked") is False, seat)
    clr_id = (seat.get("clear") or {}).get("cmdId")
    with fc.sas_lock:
        fc.sas_machines[SAS]["commandResults"] = [
            {"id": clr_id, "type": "aft_cashout", "ok": False,
             "outcome": "not_ready",
             "detail": "from-EGM transfers not enabled"}]
    fc._tournament_tick(now=time.time() + 1)
    seat = fc.tournament["seats"]["sas:" + SAS]
    check("the satellite's refusal fails the clear honestly — no fund, "
          "no lock, parked money never mixed into the pool",
          seat.get("armStage") == "failed"
          and seat.get("funded") == "pending"
          and seat.get("locked") is False
          and sas_types(fc.sas_cmds) == ["aft_cashout_pull"], seat)
    code, body = command(fc, {"action": "tournamentStart"})
    check("START names the failed seat",
          code == 200 and body.get("ok") is False
          and "failed" in str(body.get("error")), (code, body))

    # ---------------------------------------------------------------
    print("— tournamentStart + staged countdown unlocks")
    n_prime = len(eng.sent)
    code, body = command(eng, {"action": "tournamentStart"})
    check("start -> 200 phase countdown, clocks stamped "
          "(endsAt-startsAt = the reconfigured 120 s)",
          code == 200 and body.get("phase") == "countdown"
          and abs((body.get("endsAt") or 0)
                  - (body.get("startsAt") or 0) - 120) < 0.001,
          (code, body))
    check("START primes the race counters: one scoped gamePlay read per "
          "g2s seat rides the click — the T0 baseline must never trust "
          "the 60 s standing-sub cache (a pre-arm win would ride in as "
          "a phantom lead)",
          labels(eng.sent, n_prime) == ["getMeterInfo"]
          and 'g2s:deviceClass="G2S_gamePlay"' in eng.sent[n_prime][0],
          labels(eng.sent, n_prime))
    check("start re-called the spawn (belt and suspenders)",
          eng.spawn_calls == [run_id, run_id], eng.spawn_calls)
    starts = eng.tournament["startsAt"]
    ends = eng.tournament["endsAt"]
    n_sent, n_sas = len(eng.sent), len(eng.sas_cmds)
    code, body = command(eng, {"action": "tournamentStart"})
    check("re-start refused ok:false at 200",
          code == 200 and body.get("ok") is False, (code, body))
    eng._tournament_tick(now=starts - 3)
    check("deep-countdown tick emits nothing",
          len(eng.sent) == n_sent and len(eng.sas_cmds) == n_sas)
    eng._tournament_tick(now=starts - 1.1)
    check("T-1.1 s stages the SAS unlock (pull channel head start), "
          "g2s stays locked",
          sas_types(eng.sas_cmds, n_sas) == ["sas_enable"]
          and len(eng.sent) == n_sent
          and eng.tournament["seats"]["sas:" + SAS]["locked"] is False
          and eng.tournament["seats"][EGM]["locked"] is True,
          eng.sas_cmds[n_sas:])
    eng._tournament_tick(now=starts - 1.0)
    check("the SAS stage fires exactly ONCE",
          len(eng.sas_cmds) == n_sas + 1)
    # counters on the glass just before the horn — the T0 baselines
    with eng.associations[EGM].lock:
        eng.associations[EGM].meters["G2S_gamePlay/42/G2S_egmPaidGameWonAmt"] = "1000000"
    eng._tournament_tick(now=starts)
    check("T0: g2s unlock (full-flag restore) + phase flips to running",
          labels(eng.sent, n_sent) == ["setCabinetState", "getCabinetStatus"]
          and 'g2s:enable="true"' in eng.sent[n_sent][0]
          and eng.tournament["phase"] == "running"
          and eng.tournament["seats"][EGM]["locked"] is False,
          labels(eng.sent, n_sent))
    check("baselines captured at the flip (g2s per-device win-meter map "
          "/ sas raw + denom)",
          eng.tournament["seats"][EGM]["baseScore"]
          == {"g2sWon": {"G2S_gamePlay/42/G2S_egmPaidGameWonAmt": 1000000}}
          and eng.tournament["seats"]["sas:" + SAS]["baseScore"]
          == {"coinOut": 1000, "jackpot": 0, "denomCents": 5,
              "denomAssumed": False},
          {k: v.get("baseScore") for k, v in
           eng.tournament["seats"].items()})

    # ---------------------------------------------------------------
    print("— scoring: wins only, both legs, paced polls, re-baseline")
    n_sent = len(eng.sent)
    eng._tournament_tick(now=starts + 0.1)
    check("first running tick polls gamePlay meters (scoped, not G2S_all)",
          labels(eng.sent, n_sent) == ["getMeterInfo"]
          and 'g2s:deviceClass="G2S_gamePlay"' in eng.sent[n_sent][0]
          and 'g2s:deviceId="-1"' in eng.sent[n_sent][0],
          eng.sent[n_sent:])
    check("flat counters score zero",
          eng.tournament["seats"][EGM]["scoreMc"] == 0
          and eng.tournament["seats"]["sas:" + SAS]["scoreMc"] == 0)
    with eng.associations[EGM].lock:
        eng.associations[EGM].meters["G2S_gamePlay/42/G2S_egmPaidGameWonAmt"] = "1600000"
    with eng.sas_lock:
        eng.sas_machines[SAS]["meters"]["coinOut"] = 1010
    n_sent = len(eng.sent)
    eng._tournament_tick(now=starts + 1)
    check("wins land: g2s +600000 mc, sas +10 credits x 5¢ = 50000 mc",
          eng.tournament["seats"][EGM]["scoreMc"] == 600000
          and eng.tournament["seats"]["sas:" + SAS]["scoreMc"] == 50000,
          {k: v.get("scoreMc") for k, v in eng.tournament["seats"].items()})
    check("meter poll is PACED (no second read inside SCORE_POLL_SEC)",
          len(eng.sent) == n_sent)
    eng._tournament_tick(now=starts + 5)
    check("next poll fires after the 4 s cadence",
          labels(eng.sent, n_sent) == ["getMeterInfo"])
    # counter reset / rollover: negative delta re-baselines, banked score
    # is KEPT and the score never goes negative
    with eng.associations[EGM].lock:
        eng.associations[EGM].meters["G2S_gamePlay/42/G2S_egmPaidGameWonAmt"] = "100000"
    with eng.sas_lock:
        eng.sas_machines[SAS]["meters"]["coinOut"] = 3
    eng._tournament_tick(now=starts + 6)
    check("negative deltas re-baseline silently — score never lost",
          eng.tournament["seats"][EGM]["scoreMc"] == 600000
          and eng.tournament["seats"]["sas:" + SAS]["scoreMc"] == 50000)
    with eng.associations[EGM].lock:
        eng.associations[EGM].meters["G2S_gamePlay/42/G2S_egmPaidGameWonAmt"] = "250000"
    with eng.sas_lock:
        eng.sas_machines[SAS]["meters"]["coinOut"] = 5
    eng._tournament_tick(now=starts + 7)
    check("post-rollover wins accumulate from the NEW baseline",
          eng.tournament["seats"][EGM]["scoreMc"] == 750000
          and eng.tournament["seats"]["sas:" + SAS]["scoreMc"] == 60000,
          {k: v.get("scoreMc") for k, v in eng.tournament["seats"].items()})
    with eng.associations[EGM].lock:
        eng.associations[EGM].meters["G2S_gamePlay/42/G2S_egmPaidGameWonAmt"] = "junk"
    eng._tournament_tick(now=starts + 8)
    check("an unparseable counter reads as not-reported (score untouched)",
          eng.tournament["seats"][EGM]["scoreMc"] == 750000)

    # ---------------------------------------------------------------
    print("— denom honesty: unset denom scores at 1¢ but is FLAGGED")
    # _sas_denom_cents is the single source of the SAS money-scale AND its
    # honesty: a set denom is trusted; an unset/invalid one degrades to
    # 1¢/credit but reports assumed=True, so the tile/board/glass never
    # present a confident wrong value for a non-penny cabinet (a $1/nickel/
    # quarter machine — the wider SAS crowd AJ never bench-tested).
    check("_sas_denom_cents: set trusted; unset/0/bool -> (1, assumed)",
          gh.G2SHost._sas_denom_cents({"denomCents": 25}) == (25, False)
          and gh.G2SHost._sas_denom_cents({}) == (1, True)
          and gh.G2SHost._sas_denom_cents({"denomCents": 0}) == (1, True)
          and gh.G2SHost._sas_denom_cents({"denomCents": True}) == (1, True))
    dseat = {"denomless": {"kind": "sas", "sasKey": "smib-x/9"}}
    with eng.sas_lock:
        eng.sas_machines["smib-x/9"] = sas_entry(coin_out=5, denom=None)
    dread = eng._tournament_read_meters(dseat)["denomless"]
    check("unset-denom SAS seat: scores at 1¢ AND carries denomAssumed",
          dread == {"coinOut": 5, "jackpot": 0, "denomCents": 1,
                    "denomAssumed": True}, dread)
    with eng.sas_lock:
        del eng.sas_machines["smib-x/9"]

    with eng.associations[EGM].lock:
        eng.associations[EGM].meters["G2S_gamePlay/42/G2S_egmPaidGameWonAmt"] = "250000"
    # mid-run card-in: the carded name takes the seat label on the next
    # tick; the alias stays as the subtitle and the SCORE stays put
    eng.account_store.records["p1"] = {"id": "p1", "kind": "player",
                                       "name": "AJ",
                                       "cashableMillicents": 0}
    with eng.companion_lock:
        eng.card_sessions[EGM] = {"uid": "04AABBCC", "name": "AJ",
                                  "accountId": "p1",
                                  "sinceIso": "2026-07-21T00:00:00Z"}
    alias = eng.tournament["seats"][EGM]["alias"]
    eng._tournament_tick(now=starts + 9)
    check("mid-run card-in flips the seat name (alias + score stay)",
          eng.tournament["seats"][EGM]["name"] == "AJ"
          and eng.tournament["seats"][EGM]["accountId"] == "p1"
          and eng.tournament["seats"][EGM]["alias"] == alias
          and eng.tournament["seats"][EGM]["scoreMc"] == 750000,
          eng.tournament["seats"][EGM])
    with eng.companion_lock:
        del eng.card_sessions[EGM]
    eng._tournament_tick(now=starts + 10)
    check("card-out falls back to the alias",
          eng.tournament["seats"][EGM]["name"] == alias
          and eng.tournament["seats"][EGM]["accountId"] is None)

    # ---------------------------------------------------------------
    print("— finish at endsAt: SWEEP FIRST on the live floor, locks "
          "follow each settle")
    with eng.associations[EGM].lock:
        eng.associations[EGM].meters[
            "G2S_cabinet/1/G2S_playerCashableAmt"] = "500000"
    with eng.sas_lock:
        eng.sas_machines[SAS]["meters"]["0x1A"] = 90   # 90 cr × 5¢ = $4.50
    n_sent, n_sas = len(eng.sent), len(eng.sas_cmds)
    eng._tournament_tick(now=ends + 0.1)
    check("endsAt tick flips to finished", eng.tournament["phase"]
          == "finished", eng.tournament["phase"])
    lbl = labels(eng.sent, n_sent)
    xml_pull = next((x for x, lb in eng.sent[n_sent:]
                     if lb.startswith("initiateRequest")), "")
    check("g2s sweep = the HOST-INITIATED WAT pull rides FIRST, with NO "
          "lock anywhere near it (the pull needs a LIVE machine — the "
          "07-21 bench law; fromEgm, House on the record, asked = the "
          "credit meter)",
          "initiateRequest" in lbl and "setCabinetState" not in lbl
          and 'g2s:watDirection="G2S_fromEgm"' in xml_pull
          and 'g2s:accountId="house"' in xml_pull
          and 'g2s:reqCashableAmt="500000"' in xml_pull, lbl)
    check("sas sweep = the host pull, NO sas_disable with it",
          sas_types(eng.sas_cmds, n_sas) == ["aft_cashout_pull"]
          and eng.sas_cmds[-1][1].get("accountId") == "house",
          eng.sas_cmds[n_sas:])
    w = eng.tournament.get("winner") or {}
    check("winner = max scoreMc, labelled by the seat's live name",
          w.get("seatKey") == EGM and w.get("scoreMc") == 750000
          and w.get("name") == alias, w)
    g_seat = eng.tournament["seats"][EGM]
    s_seat = eng.tournament["seats"]["sas:" + SAS]
    check("seats stay UNLOCKED while their pulls settle (the OVER lock "
          "waits for each seat's own terminal verdict)",
          g_seat.get("locked") is False and s_seat.get("locked") is False
          and not eng.tournament.get("_finishDone"),
          (g_seat.get("locked"), s_seat.get("locked")))
    check("g2s sweep rests 'pulling' — automatic, like SAS (no button, "
          "no standing arm)",
          g_seat.get("sweep", {}).get("state") == "pulling"
          and g_seat.get("sweep", {}).get("askedMc") == 500000,
          g_seat.get("sweep"))
    check("the g2s pull needs NO glass pin — House rides the WAT record",
          EGM not in eng._glass_cash_out_pin, eng._glass_cash_out_pin)
    check("unlinked sas sweep is 'pulling' with the tournament pin set",
          s_seat.get("sweep", {}).get("state") == "pulling"
          and (eng._tournament_sweep_pins.get(SAS) or {}).get("acct")
          == "house",
          (s_seat.get("sweep"), eng._tournament_sweep_pins))
    check("_sas_cashout_pin resolves the leg to the House",
          eng._sas_cashout_pin(SAS) == "house")
    with eng._glass_pin_lock:
        eng._glass_cash_out_pin[SAS] = "p1"
    check("the sweep destination is IMMUTABLE while the pull is "
          "outstanding — a racing glass arm cannot capture it",
          eng._sas_cashout_pin(SAS) == "house")
    with eng._glass_pin_lock:
        del eng._glass_cash_out_pin[SAS]
    eng._tournament_sweep_pins["smib-bb9/9"] = {
        "acct": "house",
        "ts": time.time() - gh.G2SHost.TOURNAMENT_SWEEP_PIN_TTL_SEC - 5}
    check("an EXPIRED tournament pin stops resolving (TTL — a lost "
          "pull's pin can never silently home a later cash-out)",
          eng._sas_cashout_pin("smib-bb9/9") is None)
    del eng._tournament_sweep_pins["smib-bb9/9"]
    n_sent, n_sas = len(eng.sent), len(eng.sas_cmds)
    eng._tournament_tick(now=ends + 0.6)
    check("finished ticks stay QUIET while the pulls settle — no lock "
          "until each seat's own pull is terminal",
          len(eng.sent) == n_sent and len(eng.sas_cmds) == n_sas
          and not eng.tournament["seats"][EGM].get("overLocked"))
    # the settles land: WAT commit actuals + the satellite verdict (the
    # SAME record the ledger-settle slice below consumes — txn sweep-77)
    wat_poke(eng, EGM, g_seat["sweep"]["requestId"], cash=500000)
    sas_result(eng, SAS, s_seat["sweep"]["cmdId"], "aft_cashout",
               ok=True, outcome="completed", amountCents=450,
               txnId="sweep-77", accountId="spoofed-wire-account")
    eng._tournament_tick(now=ends + 1.2)
    lbl = labels(eng.sent, n_sent)
    check("each settle draws its OVER lock: full-flag disable, money-out "
          "open, the show line — and sas_disable on the leg",
          lbl == ["setCabinetState", "getCabinetStatus"]
          and 'g2s:enable="false"' in eng.sent[n_sent][0]
          and 'g2s:enableMoneyOut="true"' in eng.sent[n_sent][0]
          and f'g2s:disableText="{gh.TOURNAMENT_OVER_TEXT}"'
          in eng.sent[n_sent][0]
          and sas_types(eng.sas_cmds, n_sas) == ["sas_disable"], lbl)
    g_seat = eng.tournament["seats"][EGM]
    s_seat = eng.tournament["seats"]["sas:" + SAS]
    check("sweeps settle at their ACTUALS, seats lock, the book closes "
          "(_finishDone — the runner may finally rest)",
          g_seat.get("sweep", {}).get("state") == "done"
          and g_seat.get("sweep", {}).get("actualMc") == 500000
          and s_seat.get("sweep", {}).get("state") == "done"
          and s_seat.get("sweep", {}).get("actualMc") == 450000
          and g_seat.get("locked") is True
          and s_seat.get("locked") is True
          and eng.tournament.get("_finishDone") is True,
          (g_seat.get("sweep"), s_seat.get("sweep")))
    code, body = command(eng, {"action": "tournamentEnd"})
    check("end after the horn refused ok:false at 200 (once-guard)",
          code == 200 and body.get("ok") is False, (code, body))
    snap = eng.tournament_snapshot()
    try:
        json.dumps(snap, allow_nan=False)
        ok_json = True
    except ValueError:
        ok_json = False
    check("finished snapshot JSON-safe, runner bookkeeping stripped",
          ok_json and "_meterPollTs" not in snap
          and "_finishAt" not in snap and "_finishDone" not in snap
          and snap.get("winner", {}).get("seatKey") == EGM, snap.keys())
    snap["seats"][EGM]["scoreMc"] = -1
    snap["seats"][EGM]["sweep"]["state"] = "hacked"
    check("snapshot is a COPY — mutating it never reaches the engine",
          eng.tournament["seats"][EGM]["scoreMc"] == 750000
          and eng.tournament["seats"][EGM]["sweep"]["state"] == "done")

    # ---------------------------------------------------------------
    print("— tournamentReset: fresh idle, no wire, sas pins survive")
    n_sent = len(eng.sent)
    code, body = command(eng, {"action": "tournamentReset"})
    check("reset from finished -> 200 phase idle",
          code == 200 and body.get("ok") is True
          and body.get("phase") == "idle", (code, body))
    check("reset keeps NOTHING (config, seats, winner all cleared)",
          eng.tournament.get("creditsCents") == 0
          and eng.tournament.get("seats") == {}
          and eng.tournament.get("winner") is None
          and eng.tournament.get("id") is None, eng.tournament)
    check("reset touches NO wire — both sweeps are host pulls already "
          "in flight, there is no standing arm to tear down (2026-07-21: "
          "the g2s WAT pull replaced the button arm)",
          EGM not in eng._glass_cash_out_pin
          and labels(eng.sent, n_sent) == [],
          (dict(eng._glass_cash_out_pin), labels(eng.sent, n_sent)))
    check("sas sweep pins SURVIVE the reset (the pull is in flight — its "
          "settle still needs a home)",
          (eng._tournament_sweep_pins.get(SAS) or {}).get("acct")
          == "house")
    n_sent = len(eng.sent)
    code, body = command(eng, {"action": "tournamentReset"})
    check("reset from idle is never refused (cleanup click), no wire",
          code == 200 and body.get("ok") is True
          and body.get("phase") == "idle"
          and len(eng.sent) == n_sent, (code, body))

    # ---------------------------------------------------------------
    print("— the sweep settle: House credited once, pins CONSUMED")
    house0 = eng.account_store.records["house"]["cashableMillicents"]
    with eng.sas_lock:
        eng.sas_machines[SAS]["commandResults"].append(
            {"id": "x-settle", "type": "aft_cashout", "ok": True,
             "outcome": "completed", "txnId": "sweep-77",
             "amountCents": 450, "accountId": "spoofed-wire-account"})
        results = [dict(r)
                   for r in eng.sas_machines[SAS]["commandResults"]
                   if isinstance(r, dict)]
    eng._settle_aft_cashouts(results, SAS)
    check("the confirmed pull credits the HOUSE by the hub's own pin — "
          "never the wire's echoed account",
          eng.account_store.records["house"]["cashableMillicents"]
          == house0 + 450 * 1000
          and "spoofed-wire-account" not in eng.account_store.records,
          eng.account_store.records["house"])
    check("…and CONSUMES the tournament pin with it — armed exactly "
          "once, never a standing route to the House",
          SAS not in eng._tournament_sweep_pins
          and eng._sas_cashout_pin(SAS) is None,
          eng._tournament_sweep_pins)
    eng._settle_aft_cashouts(results, SAS)
    check("a re-reported settle is idempotent (seen-set + once ref)",
          eng.account_store.records["house"]["cashableMillicents"]
          == house0 + 450 * 1000)
    # a HUB RESTART clears the seen-set AND every pin — but the satellite
    # (its own Pi) keeps re-reporting the settled result from its
    # commandResults ring. The durable-ledger consult must absorb it
    # SILENTLY: no false "unhomed" HOLD, no satellite disarm, no double
    # credit (the 07-21 every-boot false-alarm bug).
    eng._aft_cashouts_settled = set()          # died with the restart
    holds, disarms = [], []
    eng._flag_unhomed_cashout = lambda *a, **k: holds.append(a)
    eng._disarm_sas_leg = lambda k: disarms.append(k)
    eng._settle_aft_cashouts(results, SAS)
    check("post-restart re-report of a LEDGER-settled cash-out is "
          "absorbed silently — no hold, no disarm, no double credit",
          holds == [] and disarms == []
          and eng.account_store.records["house"]["cashableMillicents"]
          == house0 + 450 * 1000
          and "aftcashout:sweep-77" in eng._aft_cashouts_settled,
          (holds, disarms))

    # ---------------------------------------------------------------
    print("— winner tie-break: strict >, earliest-seated wins")
    tie = make_engine()

    def tie_seat(leg, score):
        return {"name": leg, "alias": leg, "accountId": None,
                "machine": leg, "kind": "sas",
                "smib": leg.split("/", 1)[0], "sasKey": leg,
                "funded": "ok", "fundDetail": "", "locked": False,
                "baseScore": None, "scoreMc": score, "sweep": None}
    tie.tournament.update({
        "phase": "running", "id": "ttie", "creditsCents": 100,
        "durationSec": 60, "armedAt": time.time(),
        "startsAt": time.time(), "endsAt": time.time(),
        "seats": {"sas:smib-bb1/1": tie_seat("smib-bb1/1", 5000),
                  "sas:smib-bb1/2": tie_seat("smib-bb1/2", 5000)}})
    res = tie._tournament_finish()
    check("dead tie -> the EARLIEST-seated seat takes it",
          (res.get("winner") or {}).get("seatKey") == "sas:smib-bb1/1",
          res)
    check("an unreporting leg's sweep is SKIPPED honestly (no blind pull)",
          tie.tournament["seats"]["sas:smib-bb1/1"]["sweep"]["state"]
          == "skipped"
          and "not reporting" in tie.tournament["seats"]["sas:smib-bb1/1"]
          ["sweep"]["detail"]
          and "aft_cashout_pull" not in sas_types(tie.sas_cmds),
          tie.tournament["seats"]["sas:smib-bb1/1"]["sweep"])
    tie.tournament.update({"phase": "running", "id": "ttie2", "seats": {
        "sas:smib-bb1/1": tie_seat("smib-bb1/1", 5000),
        "sas:smib-bb1/2": tie_seat("smib-bb1/2", 5001)}})
    res = tie._tournament_finish()
    check("one millicent more anywhere beats seat order",
          (res.get("winner") or {}).get("seatKey") == "sas:smib-bb1/2",
          res)

    # ---------------------------------------------------------------
    print("— the finish settle law: failures, timeouts, the freeze rule")
    # a sweep the satellite ANSWERS but cannot credit: terminal — the
    # OVER lock still rides, and the pin retires with the answer
    fs = make_engine()
    with fs.sas_lock:
        fs.sas_machines[SAS] = sas_entry()
    command(fs, {"action": "tournamentConfigure", "creditsCents": 100,
                 "durationSec": 60, "countdownSec": 3})
    command(fs, {"action": "tournamentArm"})
    march_ready(fs)
    command(fs, {"action": "tournamentStart"})
    starts = fs.tournament["startsAt"]
    fs._tournament_tick(now=starts - 1.1)
    fs._tournament_tick(now=starts)
    with fs.sas_lock:
        fs.sas_machines[SAS]["meters"]["0x1A"] = 60
    n_sas = len(fs.sas_cmds)
    fs._tournament_tick(now=fs.tournament["endsAt"] + 0.1)
    tr = dict(fs.tournament["seats"]["sas:" + SAS]["sweep"])
    check("the horn sweeps the loaded leg (pin + pull, no lock with it)",
          sas_types(fs.sas_cmds, n_sas) == ["aft_cashout_pull"]
          and tr.get("state") == "pulling"
          and (fs._tournament_sweep_pins.get(SAS) or {}).get("acct")
          == "house", (fs.sas_cmds[n_sas:], tr))
    sas_result(fs, SAS, tr["cmdId"], "aft_cashout", ok=False,
               outcome="lock_failed",
               detail="game lock never confirmed (0xFF)")
    n_sas = len(fs.sas_cmds)
    fs._tournament_tick(now=fs.tournament["endsAt"] + 1)
    seat = fs.tournament["seats"]["sas:" + SAS]
    check("a FAILED sweep is terminal: the seat still draws its OVER "
          "lock, the verdict rides the seat honestly, the book closes",
          seat["sweep"]["state"] == "failed"
          and "0xFF" in seat["sweep"]["detail"]
          and sas_types(fs.sas_cmds, n_sas) == ["sas_disable"]
          and seat.get("locked") is True
          and fs.tournament.get("_finishDone") is True, seat)
    check("…and the House pin retired with the answered pull",
          SAS not in fs._tournament_sweep_pins, fs._tournament_sweep_pins)
    # pulls that NEVER settle: the 60 s book-closing timeout — SAS locks
    # anyway (a late 0x01 is harmless), a g2s seat with its WAT still
    # OPEN is LEFT UNLOCKED on purpose (the freeze law)
    to = make_engine()
    to.associations[EGM] = FakeAssoc(EGM)
    with to.sas_lock:
        to.sas_machines[SAS] = sas_entry()
    command(to, {"action": "tournamentConfigure", "creditsCents": 100,
                 "durationSec": 60, "countdownSec": 3})
    command(to, {"action": "tournamentArm"})
    march_ready(to)
    command(to, {"action": "tournamentStart"})
    starts = to.tournament["startsAt"]
    to._tournament_tick(now=starts - 1.1)
    to._tournament_tick(now=starts)
    with to.associations[EGM].lock:
        to.associations[EGM].meters[
            "G2S_cabinet/1/G2S_playerCashableAmt"] = "100000"
    with to.sas_lock:
        to.sas_machines[SAS]["meters"]["0x1A"] = 50
    ends = to.tournament["endsAt"]
    to._tournament_tick(now=ends + 0.1)
    check("both sweeps ride at the horn and rest 'pulling'",
          to.tournament["seats"][EGM]["sweep"]["state"] == "pulling"
          and to.tournament["seats"]["sas:" + SAS]["sweep"]["state"]
          == "pulling",
          {k: s.get("sweep") for k, s in to.tournament["seats"].items()})
    n_sent, n_sas = len(to.sent), len(to.sas_cmds)
    to._tournament_tick(now=ends + 30)
    check("inside the settle window the finished floor stays QUIET — no "
          "locks, no re-pulls, the book stays open",
          len(to.sent) == n_sent and len(to.sas_cmds) == n_sas
          and not to.tournament.get("_finishDone"))
    to._tournament_tick(
        now=ends + 0.1
        + gh.G2SHost.TOURNAMENT_FINISH_SETTLE_TIMEOUT_SEC + 0.5)
    g_seat = to.tournament["seats"][EGM]
    s_seat = to.tournament["seats"]["sas:" + SAS]
    check("timeout: the SAS seat locks anyway (a late 0x01 is harmless), "
          "its sweep saying 'verify by meter'",
          sas_types(to.sas_cmds, n_sas) == ["sas_disable"]
          and s_seat["sweep"]["state"] == "timeout"
          and "verify by meter" in s_seat["sweep"]["detail"]
          and s_seat.get("locked") is True, s_seat)
    check("timeout: the g2s seat with its WAT still OPEN is LEFT "
          "UNLOCKED on purpose — a lock would freeze the transfer (the "
          "bench law; ceremony is never worth that), said on the seat",
          len(to.sent) == n_sent
          and g_seat.get("locked") is False
          and g_seat["sweep"]["state"] == "timeout"
          and "LEFT UNLOCKED" in g_seat["sweep"]["detail"], g_seat)
    check("…and the book still closes so the runner can rest",
          to.tournament.get("_finishDone") is True
          and g_seat.get("overLocked") is True)
    # a DENIED sweep WAT is terminal-CLOSED (not open): newest_active
    # clears, so the freeze law does not apply — the OVER lock proceeds
    dn = make_engine()
    dn.associations[EGM] = FakeAssoc(EGM)
    command(dn, {"action": "tournamentConfigure", "creditsCents": 100,
                 "durationSec": 60, "countdownSec": 3})
    command(dn, {"action": "tournamentArm"})
    march_ready(dn)
    command(dn, {"action": "tournamentStart"})
    dn._tournament_tick(now=dn.tournament["startsAt"])
    with dn.associations[EGM].lock:
        dn.associations[EGM].meters[
            "G2S_cabinet/1/G2S_playerCashableAmt"] = "70000"
    ends = dn.tournament["endsAt"]
    dn._tournament_tick(now=ends + 0.1)
    wat_poke(dn, EGM, dn.tournament["seats"][EGM]["sweep"]["requestId"],
             state="denied")
    n_sent = len(dn.sent)
    dn._tournament_tick(now=ends + 1)
    seat = dn.tournament["seats"][EGM]
    check("a DENIED sweep WAT is terminal (newest_active clears) — the "
          "OVER lock rides, the failure stays on the seat",
          seat["sweep"]["state"] == "failed"
          and "WAT denied" in seat["sweep"]["detail"]
          and labels(dn.sent, n_sent) == ["setCabinetState",
                                          "getCabinetStatus"]
          and 'g2s:enable="false"' in dn.sent[n_sent][0]
          and f'g2s:disableText="{gh.TOURNAMENT_OVER_TEXT}"'
          in dn.sent[n_sent][0]
          and seat.get("locked") is True
          and dn.tournament.get("_finishDone") is True, seat)

    # ---------------------------------------------------------------
    print("— cancel: UNLOCK FIRST then pull, leave unlocked, post-mortem "
          "kept")
    can = make_engine()
    can.associations[EGM] = FakeAssoc(EGM)
    with can.sas_lock:
        # not menu-set AND holding credits — the pull must skip honestly
        can.sas_machines[SAS] = sas_entry(from_egm=False, credits=7)
    command(can, {"action": "tournamentConfigure",
                  "creditsCents": 500, "durationSec": 60})
    command(can, {"action": "tournamentArm"})
    code, body = command(can, {"action": "tournamentReset"})
    check("reset refused while armed ('cancel first')",
          code == 200 and body.get("ok") is False
          and "cancel" in str(body.get("error")), (code, body))
    n_sent, n_sas = len(can.sent), len(can.sas_cmds)
    code, body = command(can, {"action": "tournamentCancel"})
    check("cancel -> 200 idle note=cancelled",
          code == 200 and body.get("ok") is True
          and body.get("phase") == "idle"
          and body.get("note") == "cancelled", (code, body))
    lbl = labels(can.sent, n_sent)
    check("g2s: the full restore rides (unlock FIRST); the NEVER-CLEARED "
          "glass is not pulled (the pipeline hadn't ticked — whatever "
          "sits there is the floor's own money, not the House's) and NO "
          "House glass pin",
          lbl == ["setCabinetState", "getCabinetStatus"]
          and 'g2s:enable="true"' in can.sent[n_sent][0]
          and EGM not in can._glass_cash_out_pin, lbl)
    check("sas: unlocked; the never-cleared credit-holding leg is swept "
          "as SKIPPED (no pull, no pin)",
          sas_types(can.sas_cmds, n_sas) == ["sas_enable"]
          and SAS not in can._tournament_sweep_pins,
          can.sas_cmds[n_sas:])
    seats = can.tournament["seats"]
    check("the post-mortem seat map survives on the idle dict, floor "
          "left UNLOCKED, both sweeps honestly 'skipped' (never cleared)",
          set(seats) == {EGM, "sas:" + SAS}
          and seats[EGM]["sweep"]["state"] == "skipped"
          and "never cleared" in seats[EGM]["sweep"]["detail"]
          and seats["sas:" + SAS]["sweep"]["state"] == "skipped"
          and seats[EGM]["locked"] is False
          and seats["sas:" + SAS]["locked"] is False, seats)
    code, body = command(can, {"action": "tournamentCancel"})
    check("cancel from idle refused ok:false at 200",
          code == 200 and body.get("ok") is False, (code, body))
    command(can, {"action": "tournamentReset"})
    check("reset clears the post-mortem",
          can.tournament.get("seats") == {})
    # a CLEARED-and-funded leg proves THE directive's ordering: unlock
    # rides first, the pull queues BEHIND it (the satellite executes in
    # order, so the pull runs against a LIVE machine — the 0x01-shut-down
    # BB2 refused the pull's game-lock on the bench). Only a CLEARED
    # glass is swept at all — the fund is the only House money out there.
    cn2 = make_engine()
    with cn2.sas_lock:
        cn2.sas_machines[SAS] = sas_entry(credits=20)
    command(cn2, {"action": "tournamentConfigure",
                  "creditsCents": 500, "durationSec": 60})
    command(cn2, {"action": "tournamentArm"})
    march_ready(cn2, clear_amounts={"sas:" + SAS: 100000})
    with cn2.sas_lock:
        cn2.sas_machines[SAS]["meters"]["0x1A"] = 100   # the funded stake
    n_sas = len(cn2.sas_cmds)
    command(cn2, {"action": "tournamentCancel"})
    check("a cleared+funded sas leg: the UNLOCK rides BEFORE the sweep "
          "pull (the directive's reverse of the bench-day order)",
          sas_types(cn2.sas_cmds, n_sas) == ["sas_enable",
                                             "aft_cashout_pull"]
          and (cn2._tournament_sweep_pins.get(SAS) or {}).get("acct")
          == "house",
          cn2.sas_cmds[n_sas:])
    # cancel racing the pipeline's own WAT legs: an open clear/fund must
    # SKIP the cancel pull (G2S_WTX008 — one WAT at a time per EGM)
    cw = make_engine()
    cw.associations[EGM] = FakeAssoc(EGM)
    with cw.associations[EGM].lock:
        cw.associations[EGM].meters[
            "G2S_cabinet/1/G2S_playerCashableAmt"] = "40000"
    # a CARDED seat: the orphaned refund attribution must be said LOUDLY
    cw.card_sessions[EGM] = {"uid": "6CB16F06", "name": "AJ",
                             "accountId": "p1", "since": time.time()}
    cw.account_store.records["p1"] = {"id": "p1", "kind": "player",
                                      "name": "AJ",
                                      "cashableMillicents": 0}
    command(cw, {"action": "tournamentConfigure",
                 "creditsCents": 500, "durationSec": 60})
    command(cw, {"action": "tournamentArm"})
    cw._tournament_tick(now=time.time())      # clear pull rides out
    n_sent = len(cw.sent)
    logs = []
    _cap = logging.Handler()
    _cap.emit = lambda rec: logs.append(rec.getMessage())
    logging.disable(logging.NOTSET)
    gh.log.addHandler(_cap)
    try:
        command(cw, {"action": "tournamentCancel"})
    finally:
        gh.log.removeHandler(_cap)
        logging.disable(logging.CRITICAL)
    seat = cw.tournament["seats"][EGM]
    check("cancel mid-CLEAR: unlock only — the in-flight clear IS the "
          "sweep (no second initiateRequest, no WTX008 poison)",
          labels(cw.sent, n_sent) == ["setCabinetState",
                                      "getCabinetStatus"]
          and 'g2s:enable="true"' in cw.sent[n_sent][0]
          and seat["sweep"]["state"] == "skipped"
          and "clear pull still in flight" in seat["sweep"]["detail"],
          (labels(cw.sent, n_sent), seat.get("sweep")))
    check("…the carded orphan is said LOUDLY, naming the account for a "
          "manual Players ▸ Fund square-up (the accepted home-floor "
          "tradeoff) — and no refund leg was minted",
          any("square it from" in m and "p1" in m for m in logs)
          and not any("absorbrf" in r for r in cw.account_store.refs),
          logs)
    cf = make_engine()
    cf.associations[EGM] = FakeAssoc(EGM)
    command(cf, {"action": "tournamentConfigure",
                 "creditsCents": 500, "durationSec": 60})
    command(cf, {"action": "tournamentArm"})
    cf._tournament_tick(now=time.time())      # the confirm read rides
    cf.associations[EGM].meters_at = time.time()
    cf._tournament_tick(now=time.time())      # confirmed -> FUND rides
    n_sent = len(cf.sent)
    command(cf, {"action": "tournamentCancel"})
    seat = cf.tournament["seats"][EGM]
    check("cancel mid-FUND: unlock only — the open toEgm WAT gates the "
          "pull (one at a time), said honestly; the fund settles on the "
          "reopened floor and the next arm's clear fetches it",
          labels(cf.sent, n_sent) == ["setCabinetState",
                                      "getCabinetStatus"]
          and seat["sweep"]["state"] == "skipped"
          and "one at a time" in seat["sweep"]["detail"],
          (labels(cf.sent, n_sent), seat.get("sweep")))

    # ---------------------------------------------------------------
    print("— run ids: unique even inside one wall-clock second")
    uq = make_engine()
    uq.associations[EGM] = FakeAssoc(EGM)
    command(uq, {"action": "tournamentConfigure",
                 "creditsCents": 100, "durationSec": 60})
    _c, b1 = command(uq, {"action": "tournamentArm"})
    command(uq, {"action": "tournamentCancel"})
    _c, b2 = command(uq, {"action": "tournamentArm"})
    check("cancel + instant re-arm mint DISTINCT run ids (every id-gated "
          "write-back stays run-scoped)",
          b1.get("tournamentId") and b2.get("tournamentId")
          and b1["tournamentId"] != b2["tournamentId"],
          (b1.get("tournamentId"), b2.get("tournamentId")))

    # ---------------------------------------------------------------
    print("— staging is idempotent-per-epoch: drops re-issue, refusals "
          "retry")
    rs = make_engine()
    rs.associations[EGM] = FakeAssoc(EGM)
    rs.associations[EGM].epoch = 7        # the per-assoc rejoin marker
    with rs.sas_lock:
        rs.sas_machines[SAS] = sas_entry()
    command(rs, {"action": "tournamentConfigure",
                 "creditsCents": 500, "durationSec": 60,
                 "countdownSec": 5})
    command(rs, {"action": "tournamentArm"})
    march_ready(rs)
    check("the pipeline's lock records the epoch it was queued under",
          rs.tournament["seats"][EGM]["lockEpoch"] == 7
          and rs.tournament["seats"][EGM]["armStage"] == "ready",
          rs.tournament["seats"][EGM])
    n_sent, n_sas = len(rs.sent), len(rs.sas_cmds)
    t0 = time.time()
    rs._tournament_tick(now=t0)
    check("a stable epoch re-issues nothing (an armed floor stays quiet)",
          len(rs.sent) == n_sent and len(rs.sas_cmds) == n_sas)
    rs.associations[EGM].epoch = 8        # rejoin: queued sends DROPPED
    rs._tournament_tick(now=t0 + 0.5)
    check("an epoch bump while ARMED re-issues the lock (the FIFO worker "
          "dropped the queued one at rejoin)",
          labels(rs.sent, n_sent) == ["setCabinetState",
                                      "getCabinetStatus"]
          and 'g2s:enable="false"' in rs.sent[n_sent][0]
          and f'g2s:disableText="{gh.TOURNAMENT_ARM_TEXT}"'
          in rs.sent[n_sent][0]
          and rs.tournament["seats"][EGM]["lockEpoch"] == 8,
          labels(rs.sent, n_sent))
    n_sent = len(rs.sent)
    rs._tournament_tick(now=t0 + 0.9)
    check("re-issues are throttled per seat (TOURNAMENT_RESTAGE_SEC)",
          len(rs.sent) == n_sent)
    command(rs, {"action": "tournamentStart"})
    starts = rs.tournament["startsAt"]
    n_sas = len(rs.sas_cmds)
    rs.sas_refuse = "queue full (bench)"
    rs._tournament_tick(now=starts - 1.1)
    check("a REFUSED sas unlock leaves the locked face standing (honest)",
          set(sas_types(rs.sas_cmds, n_sas)) == {"sas_enable"}
          and rs.tournament["seats"]["sas:" + SAS]["locked"] is True,
          rs.tournament["seats"]["sas:" + SAS])
    rs.sas_refuse = None
    rs._tournament_tick(now=starts)       # the horn — g2s unlocks, running
    n_sas = len(rs.sas_cmds)
    rs._tournament_tick(now=starts + 1.0)   # past the per-seat throttle
    check("…and the RUNNING tick re-issues it once the queue re-opens (a "
          "comms blip at T0 can't leave a seat locked for the race)",
          sas_types(rs.sas_cmds, n_sas) == ["sas_enable"]
          and rs.tournament["seats"]["sas:" + SAS]["locked"] is False
          and rs.tournament["phase"] == "running",
          rs.sas_cmds[n_sas:])

    # ---------------------------------------------------------------
    print("— a refused ARM lock: ready-but-unlocked, START says so, the "
          "restage re-locks READY seats only")
    lk = make_engine()
    with lk.sas_lock:
        lk.sas_machines[SAS] = sas_entry()
    command(lk, {"action": "tournamentConfigure",
                 "creditsCents": 500, "durationSec": 60})
    command(lk, {"action": "tournamentArm"})
    t0 = time.time()
    lk._tournament_tick(now=t0)               # clear skip -> fund rides
    sas_result(lk, SAS,
               lk.tournament["seats"]["sas:" + SAS]["fundCmdId"],
               "aft_transfer", ok=True, outcome="completed")
    lk.sas_refuse = "queue full (bench)"
    lk._tournament_tick(now=t0)               # the LOCK stage — refused
    seat = lk.tournament["seats"]["sas:" + SAS]
    check("a refused queue leaves the seat READY but honestly UNLOCKED "
          "(the last commanded face stands)",
          seat.get("armStage") == "ready" and seat.get("locked") is False
          and set(sas_types(lk.sas_cmds, 1)) == {"sas_disable"},
          (seat.get("armStage"), seat.get("locked"),
           sas_types(lk.sas_cmds)))
    code, body = command(lk, {"action": "tournamentStart"})
    check("START refuses the unlocked seat by name: 'lock not confirmed "
          "yet (re-issuing)'",
          code == 200 and body.get("ok") is False
          and "lock not confirmed yet" in str(body.get("error")),
          (code, body))
    lk.sas_refuse = None
    n_sas = len(lk.sas_cmds)
    lk._tournament_tick(now=t0 + gh.G2SHost.TOURNAMENT_RESTAGE_SEC + 0.1)
    seat = lk.tournament["seats"]["sas:" + SAS]
    check("the restage pass re-delivers the lock once the queue re-opens "
          "(while ARMED only a READY seat is ever re-locked — the "
          "pipeline owns the timing)",
          sas_types(lk.sas_cmds, n_sas) == ["sas_disable"]
          and seat.get("locked") is True, lk.sas_cmds[n_sas:])
    code, body = command(lk, {"action": "tournamentStart"})
    check("…and START opens",
          code == 200 and body.get("phase") == "countdown", (code, body))

    # ---------------------------------------------------------------
    print("— an arm pass a cancel superseded re-opens its own locks")
    sw = make_engine()
    with sw.sas_lock:
        sw.sas_machines[SAS] = sas_entry()
    command(sw, {"action": "tournamentConfigure",
                 "creditsCents": 500, "durationSec": 60})
    command(sw, {"action": "tournamentArm"})
    sw._tournament_tick(now=time.time())      # clear skip -> fund rides
    sas_result(sw, SAS,
               sw.tournament["seats"]["sas:" + SAS]["fundCmdId"],
               "aft_transfer", ok=True, outcome="completed")
    inner = sw.sas_enqueue_command

    def racing_cancel(smib, cmd):
        # the cancel lands in the copy-out gap: the instant the pass's
        # LOCK hits the wire the phase is already surrendered
        r = inner(smib, cmd)
        if cmd.get("type") == "sas_disable":
            with sw.tournament_lock:
                sw.tournament["phase"] = "idle"
                sw.tournament["note"] = "cancelled"
        return r
    sw.sas_enqueue_command = racing_cancel
    sw._tournament_tick(now=time.time())
    check("the losing pass detects the supersede and RE-OPENS the seat "
          "it locked (a cancelled floor can never end locked with "
          "nobody left to fix it)",
          sas_types(sw.sas_cmds)[-2:] == ["sas_disable", "sas_enable"],
          sas_types(sw.sas_cmds))
    check("…and publishes NOTHING onto the surrendered run's seats",
          sw.tournament["seats"]["sas:" + SAS]["armStage"] == "fund"
          and sw.tournament["seats"]["sas:" + SAS]["locked"] is False,
          sw.tournament["seats"]["sas:" + SAS])

    # ---------------------------------------------------------------
    print("— fund refusals park the seat honestly (armStage=failed)")
    rf = make_engine()
    with rf.sas_lock:
        rf.sas_machines[SAS] = sas_entry()
    rf.sas_refuse = "queue full (bench)"
    command(rf, {"action": "tournamentConfigure",
                 "creditsCents": 500, "durationSec": 60})
    command(rf, {"action": "tournamentArm"})
    rf._tournament_tick(now=time.time())
    seat = rf.tournament["seats"]["sas:" + SAS]
    check("a refused satellite queue -> funded=failed with the reason, "
          "seat parked at armStage=failed, never locked",
          seat.get("funded") == "failed"
          and "queue full" in seat.get("fundDetail", "")
          and seat.get("armStage") == "failed"
          and seat.get("stageDetail", "").startswith("fund:")
          and seat.get("locked") is False, seat)
    rf2 = make_engine()
    rf2.associations[EGM] = FakeAssoc(EGM)
    rf2.account_store.records.pop("house")
    command(rf2, {"action": "tournamentConfigure",
                  "creditsCents": 500, "durationSec": 60})
    command(rf2, {"action": "tournamentArm"})
    rf2._tournament_tick(now=time.time())     # the confirm read rides
    rf2.associations[EGM].meters_at = time.time()
    rf2._tournament_tick(now=time.time())     # confirmed -> fund refused
    seat = rf2.tournament["seats"][EGM]
    check("a refused WAT push -> funded=failed with the engine's reason, "
          "seat parked",
          seat.get("funded") == "failed"
          and "house" in seat.get("fundDetail", "")
          and seat.get("armStage") == "failed", seat)
    # an ACCEPTED fund whose WAT dies mid-flight (EGM reset parks the
    # record "abandoned") must fail the seat too — the old observer
    # waited on these forever
    ab = make_engine()
    ab.associations[EGM] = FakeAssoc(EGM)
    command(ab, {"action": "tournamentConfigure",
                 "creditsCents": 500, "durationSec": 60})
    command(ab, {"action": "tournamentArm"})
    ab._tournament_tick(now=time.time())      # the confirm read rides
    ab.associations[EGM].meters_at = time.time()
    ab._tournament_tick(now=time.time())      # confirmed -> fund rides
    wat_poke(ab, EGM, ab.tournament["seats"][EGM]["fundRequestId"],
             state="abandoned")
    ab._tournament_tick(now=time.time())
    seat = ab.tournament["seats"][EGM]
    check("an ABANDONED fund WAT (EGM reset mid-push) fails the seat "
          "instead of pending forever",
          seat.get("funded") == "failed"
          and seat.get("fundDetail") == "WAT abandoned"
          and seat.get("armStage") == "failed"
          and seat.get("locked") is False, seat)
    code, body = command(ab, {"action": "tournamentStart"})
    check("START names it: fund: WAT abandoned",
          code == 200 and body.get("ok") is False
          and "fund: WAT abandoned" in str(body.get("error")),
          (code, body))

    # ---------------------------------------------------------------
    print("— seat enumeration: kinds, names, skips")
    en = make_engine()
    en.associations[EGM] = FakeAssoc(EGM)
    en.associations[WMS] = FakeAssoc(WMS)
    en.sas_links = {WMS: SAS}
    en.hub_store.set_nickname(EGM, "Corner AVP", protocol="g2s")
    en.account_store.records["p1"] = {"id": "p1", "kind": "player",
                                      "name": "AJ",
                                      "cashableMillicents": 0}
    with en.companion_lock:
        en.card_sessions[EGM] = {"uid": "04AABBCC", "name": "AJ",
                                 "accountId": "p1",
                                 "sinceIso": "2026-07-21T00:00:00Z"}
    with en.sas_lock:
        en.sas_machines[SAS] = sas_entry()                 # claimed by WMS
        en.sas_machines["smib-bb1/1"] = sas_entry(
            smib="smib-bb1", nickname="Door BB1")          # a real seat
        en.sas_machines["smib-bb1/2"] = sas_entry(
            smib="smib-bb1", pendingHandpay={"amt": 1})    # jackpot moment
        en.sas_machines["smib-bb1/3"] = sas_entry(
            smib="smib-bb1",
            receivedAt=time.time() - gh.G2SHost.SAS_STALE_SEC - 10)  # stale
        en.sas_machines["smib-bb1/4"] = sas_entry(
            smib="smib-bb1", sasEnabled=False)             # Settings-parked
        en.sas_machines["smib-bb1/5"] = sas_entry(
            smib="smib-bb1", online=False)                 # dark
    seats, skipped = en._tournament_enumerate_seats()
    check("exactly the seatable floor: carded g2s + linked + one live sas",
          set(seats) == {EGM, WMS, "sas:smib-bb1/1"}, set(seats))
    check("every benched machine is NAMED with its reason (the toast-"
          "faded-too-fast lesson)",
          {s["seatKey"] for s in skipped}
          == {"sas:smib-bb1/2", "sas:smib-bb1/3", "sas:smib-bb1/4",
              "sas:smib-bb1/5"}
          and all(s.get("reason") for s in skipped), skipped)
    check("carded player owns the g2s seat's label (alias kept under it)",
          seats[EGM]["kind"] == "g2s" and seats[EGM]["name"] == "AJ"
          and seats[EGM]["accountId"] == "p1"
          and seats[EGM]["alias"] in gh.TOURNAMENT_NAME_DEFAULTS,
          seats[EGM])
    check("g2s seat wears its hub.db nickname",
          seats[EGM]["machine"] == "Corner AVP")
    check("linked cabinet seats as ONE seat riding its SAS leg",
          seats[WMS]["kind"] == "linked" and seats[WMS]["sasKey"] == SAS
          and seats[WMS]["smib"] == "smib-bb2", seats[WMS])
    check("unlinked live sas machine seats under sas:<leg>",
          seats["sas:smib-bb1/1"]["kind"] == "sas"
          and seats["sas:smib-bb1/1"]["smib"] == "smib-bb1"
          and seats["sas:smib-bb1/1"]["machine"] == "Door BB1",
          seats["sas:smib-bb1/1"])
    check("aliases unique across the floor",
          len({s["alias"] for s in seats.values()}) == len(seats))
    en.associations[EGM].handpays_pending = [{"hpId": "1"}]
    seats, skipped = en._tournament_enumerate_seats()
    check("a g2s handpay lockup sits out (somebody's jackpot moment)",
          EGM not in seats, set(seats))
    with en.sas_lock:
        en.sas_machines[SAS]["pendingHandpay"] = {"amt": 1}
    seats, skipped = en._tournament_enumerate_seats()
    check("a lockup on the LINKED LEG benches its G2S owner too",
          WMS not in seats, set(seats))
    en.associations[EGM].handpays_pending = []
    with en.sas_lock:
        en.sas_machines[SAS]["pendingHandpay"] = None
    # linked-leg admission gates — the leg IS the seat's money channel,
    # so it obeys the same live/enabled/fresh rules the unlinked seats do
    # (a parked/stale leg would make a zombie seat: a fund nobody
    # fetches, a sweep pulling a dark leg)
    with en.sas_lock:
        en.sas_machines[SAS]["sasEnabled"] = False
    seats, skipped = en._tournament_enumerate_seats()
    check("a Settings-parked linked leg benches its G2S owner",
          WMS not in seats and EGM in seats, set(seats))
    with en.sas_lock:
        en.sas_machines[SAS]["sasEnabled"] = True
        en.sas_machines[SAS]["online"] = False
    seats, skipped = en._tournament_enumerate_seats()
    check("a dark linked leg benches its G2S owner",
          WMS not in seats, set(seats))
    with en.sas_lock:
        en.sas_machines[SAS]["online"] = True
        en.sas_machines[SAS]["receivedAt"] = \
            time.time() - gh.G2SHost.SAS_STALE_SEC - 10
    seats, skipped = en._tournament_enumerate_seats()
    check("a stale linked leg (satellite rebooting) benches its owner",
          WMS not in seats, set(seats))
    with en.sas_lock:
        en.sas_machines[SAS]["receivedAt"] = time.time()
        saved_leg = en.sas_machines.pop(SAS)
    seats, skipped = en._tournament_enumerate_seats()
    check("a linked cabinet whose leg never reported is not a seat",
          WMS not in seats, set(seats))
    with en.sas_lock:
        en.sas_machines[SAS] = saved_leg
    # machines already holding credits SEAT (AJ 2026-07-21: collectors
    # park credits to silence attract-mode callouts — a floor where that
    # blocks arming is a broken feature). Enumeration records NO balance
    # (the meter-guess absorbedMc is retired): the ARM pipeline's CLEAR
    # stage pulls the glass and its SETTLED ACTUALS become absorbedMc.
    with en.sas_lock:
        en.sas_machines["smib-bb1/1"]["meters"]["0x1A"] = 40
    with en.associations[EGM].lock:
        en.associations[EGM].meters[
            "G2S_cabinet/1/G2S_playerCashableAmt"] = "50000"
    seats, skipped = en._tournament_enumerate_seats()
    check("credit-holding machines SEAT (either leg) at armStage='clear' "
          "with absorbedMc resting 0 — no meter guessing at enumerate",
          "sas:smib-bb1/1" in seats and EGM in seats
          and seats["sas:smib-bb1/1"]["absorbedMc"] == 0
          and seats[EGM]["absorbedMc"] == 0
          and seats[EGM]["armStage"] == "clear"
          and seats["sas:smib-bb1/1"]["armStage"] == "clear",
          (seats.get("sas:smib-bb1/1"), seats.get(EGM)))
    check("credits never appear in the skip list — a seat, not a blocker",
          not any(s.get("creditsMc") for s in skipped), skipped)
    with en.sas_lock:
        en.sas_machines["smib-bb1/1"]["meters"]["0x1A"] = 0
    with en.associations[EGM].lock:
        en.associations[EGM].meters.clear()
    settings(en, {"tournamentNames": ["Solo"]})
    seats, skipped = en._tournament_enumerate_seats()
    check("an exhausted roster falls back to numbered Mystery Guests",
          {s["alias"] for s in seats.values()}
          == {"Solo", "Mystery Guest 1", "Mystery Guest 2"},
          {s["alias"] for s in seats.values()})

    # ---------------------------------------------------------------
    print("— arm refusal persists on lastArm; the CLEAR's settled "
          "actuals refund the carded player at finish")
    rf = make_engine()          # empty floor: nothing to seat
    command(rf, {"action": "tournamentConfigure", "creditsCents": 100,
                 "durationSec": 60, "countdownSec": 3})
    code, body = command(rf, {"action": "tournamentArm"})
    check("an unseatable floor refuses at 200 with the skip list",
          code == 200 and body.get("ok") is False
          and isinstance(body.get("skipped"), list), (code, body))
    snap = rf.tournament_snapshot()
    check("the refusal PERSISTS on t.lastArm for the Settings card "
          "(the toast-faded-too-fast lesson)",
          (snap.get("lastArm") or {}).get("error", "").startswith(
              "no seatable machines"), snap.get("lastArm"))
    rf.associations[EGM] = FakeAssoc(EGM)
    with rf.associations[EGM].lock:
        rf.associations[EGM].meters[
            "G2S_cabinet/1/G2S_playerCashableAmt"] = "30000"
    rf.card_sessions[EGM] = {"uid": "6CB16F06", "name": "AJ",
                             "accountId": "p1", "since": time.time()}
    rf.account_store.records["p1"] = {
        "id": "p1", "kind": "player", "name": "AJ",
        "cashableMillicents": 0}
    code, body = command(rf, {"action": "tournamentArm"})
    check("a successful arm clears lastArm",
          code == 200 and body.get("seats") == 1
          and rf.tournament_snapshot().get("lastArm") is None, body)
    march_ready(rf, clear_amounts={EGM: 30000})
    check("the CLEAR's settled actuals become absorbedMc (no meter "
          "guessing — the $521k lesson)",
          rf.tournament["seats"][EGM]["absorbedMc"] == 30000,
          rf.tournament["seats"][EGM])
    command(rf, {"action": "tournamentStart"})
    starts = rf.tournament["startsAt"]
    rf._tournament_tick(now=starts)
    rf._tournament_tick(now=rf.tournament["endsAt"] + 0.1)
    check("finish refunds the CARDED seat's cleared credits from the "
          "House (paired legs, distinct once=True refs)",
          rf.account_store.records["p1"]["cashableMillicents"] == 30000
          and rf.account_store.records["house"]["cashableMillicents"]
          == -30000, dict(rf.account_store.records))
    run_id = rf.tournament["id"]
    rf._tournament_absorb_refunds(run_id, dict(rf.tournament["seats"]))
    check("a replayed refund is a no-op (per-leg refs already in the "
          "ledger)",
          rf.account_store.records["p1"]["cashableMillicents"] == 30000
          and rf.account_store.records["house"]["cashableMillicents"]
          == -30000, dict(rf.account_store.records))
    uc = make_engine()          # uncarded cleared credits stay House
    uc.associations[EGM] = FakeAssoc(EGM)
    with uc.associations[EGM].lock:
        uc.associations[EGM].meters[
            "G2S_cabinet/1/G2S_playerCashableAmt"] = "20000"
    command(uc, {"action": "tournamentConfigure", "creditsCents": 100,
                 "durationSec": 60, "countdownSec": 3})
    command(uc, {"action": "tournamentArm"})
    march_ready(uc, clear_amounts={EGM: 20000})
    command(uc, {"action": "tournamentStart"})
    uc._tournament_tick(now=uc.tournament["startsAt"])
    uc._tournament_tick(now=uc.tournament["endsAt"] + 0.1)
    check("an UNCARDED seat's anti-attract credits stay with the House "
          "(floor money, no refund leg)",
          uc.account_store.records["house"]["cashableMillicents"] == 0
          and not any("absorbrf" in r for r in uc.account_store.refs),
          dict(uc.account_store.records))

    # ---------------------------------------------------------------
    print("— linked seat: SAS money + meters, both-leg lock, pin sweep")
    ln = make_engine()
    ln.associations[WMS] = FakeAssoc(WMS)
    ln.sas_links = {WMS: SAS}
    with ln.sas_lock:
        ln.sas_machines[SAS] = sas_entry(coin_out=200)
    command(ln, {"action": "tournamentConfigure", "creditsCents": 250,
                 "durationSec": 60, "countdownSec": 3})
    code, body = command(ln, {"action": "tournamentArm"})
    check("linked floor arms as ONE seat",
          code == 200 and body.get("seats") == 1, (code, body))
    ln._tournament_tick(now=time.time())
    check("linked money is SAS: the CLEAR skips the empty 0x1A and the "
          "fund is aft_transfer CENTS (never a WAT push) — no lock yet",
          labels(ln.sent) == []
          and sas_types(ln.sas_cmds) == ["aft_transfer"]
          and ln.sas_cmds[0][1]["cents"] == 250
          and ln.sas_cmds[0][1]["accountId"] == "house",
          (labels(ln.sent), ln.sas_cmds))
    cid = ln.tournament["seats"][WMS]["fundCmdId"]
    sas_result(ln, SAS, cid, "aft_transfer", ok=True, outcome="partial")
    ln._tournament_tick(now=time.time())
    check("a partial AFT still counts as funded (the machine took what "
          "fits) — and the linked LOCK rides SAS ONLY (sas_disable; a "
          "linked cabinet's whole-machine lock is SAS, never a G2S "
          "setCabinetState that would strand a G2S_hostDisabled latch — "
          "AJ 2026-07-21)",
          ln.tournament["seats"][WMS]["funded"] == "ok"
          and ln.tournament["seats"][WMS]["fundDetail"] == "AFT partial"
          and ln.tournament["seats"][WMS]["armStage"] == "ready"
          and labels(ln.sent) == []   # NO G2S setCabinetState on a link
          and sas_types(ln.sas_cmds, 1) == ["sas_disable"]
          and ln.tournament["seats"][WMS]["locked"] is True
          and ln.tournament["seats"][WMS]["sasLocked"] is True,
          (labels(ln.sent), ln.tournament["seats"][WMS]))
    command(ln, {"action": "tournamentStart"})
    starts = ln.tournament["startsAt"]
    n_sas = len(ln.sas_cmds)
    ln._tournament_tick(now=starts - 1.1)
    check("a linked seat's SAS leg unlocks EARLY at T-1.2s (pull latency "
          "— SAS is the whole-machine lock on a linked cabinet)",
          sas_types(ln.sas_cmds, n_sas) == ["sas_enable"],
          ln.sas_cmds[n_sas:])
    ln._tournament_tick(now=starts)
    check("linked baseline reads the LEG's counters",
          ln.tournament["phase"] == "running"
          and ln.tournament["seats"][WMS]["baseScore"]
          == {"coinOut": 200, "jackpot": 0, "denomCents": 5,
              "denomAssumed": False},
          ln.tournament["seats"][WMS])
    with ln.sas_lock:
        ln.sas_machines[SAS]["meters"]["coinOut"] = 210
    ln._tournament_tick(now=starts + 1)
    check("linked scoring rides the SAS counters — no gamePlay polls",
          ln.tournament["seats"][WMS]["scoreMc"] == 50000
          and "getMeterInfo" not in labels(ln.sent),
          (ln.tournament["seats"][WMS]["scoreMc"], labels(ln.sent)))
    with ln.sas_lock:
        ln.sas_machines[SAS]["meters"]["0x1A"] = 30   # leftovers to sweep
    n_sent, n_sas = len(ln.sent), len(ln.sas_cmds)
    code, body = command(ln, {"action": "tournamentEnd"})
    check("tournamentEnd -> finished now, note says so",
          code == 200 and body.get("phase") == "finished"
          and ln.tournament["note"] == "ended early", (code, body))
    check("linked sweep = the host pull FIRST (machine still live), the "
          "tournament pin on the leg, NO glass pin needed, NO lock yet",
          sas_types(ln.sas_cmds, n_sas) == ["aft_cashout_pull"]
          and labels(ln.sent, n_sent) == []
          and WMS not in ln._glass_cash_out_pin
          and ln.tournament["seats"][WMS]["sweep"]["state"] == "pulling"
          and (ln._tournament_sweep_pins.get(SAS) or {}).get("acct")
          == "house",
          (ln._glass_cash_out_pin, ln.sas_cmds[n_sas:],
           ln._tournament_sweep_pins))
    check("the settle homes the leg to the House",
          ln._sas_cashout_pin(SAS) == "house")
    with ln._glass_pin_lock:
        ln._glass_cash_out_pin[WMS] = "p9"   # a racing cashOutToWallet arm
    check("a player arming cashOutToWallet in the pull→settle window "
          "cannot capture the House sweep (tournament pin outranks the "
          "overwritten glass pin)",
          ln._sas_cashout_pin(SAS) == "house")
    with ln._glass_pin_lock:
        del ln._glass_cash_out_pin[WMS]
    sas_result(ln, SAS, ln.tournament["seats"][WMS]["sweep"]["cmdId"],
               "aft_cashout", ok=True, outcome="completed",
               amountCents=150, txnId="lnk-1")
    ln._tournament_tick(now=time.time())
    check("the settled pull draws the linked OVER lock over SAS ONLY "
          "(sas_disable; no G2S setCabinetState on a linked cabinet) and "
          "closes the book",
          labels(ln.sent, n_sent) == []
          and sas_types(ln.sas_cmds, n_sas) == ["aft_cashout_pull",
                                                "sas_disable"]
          and ln.tournament["seats"][WMS]["sweep"]["state"] == "done"
          and ln.tournament["seats"][WMS]["sweep"]["actualMc"] == 150000
          and ln.tournament["seats"][WMS]["locked"] is True
          and ln.tournament.get("_finishDone") is True,
          (labels(ln.sent, n_sent), sas_types(ln.sas_cmds, n_sas),
           ln.tournament["seats"][WMS]))
    w = ln.tournament.get("winner") or {}
    check("solo floor still gets its ceremony",
          w.get("seatKey") == WMS and w.get("scoreMc") == 50000, w)

    # ---------------------------------------------------------------
    print("— the frozen stake: a mid-arm reconfigure reprices NOTHING")
    fz = make_engine()
    fz.associations[EGM] = FakeAssoc(EGM)
    with fz.sas_lock:
        fz.sas_machines[SAS] = sas_entry()
    command(fz, {"action": "tournamentConfigure",
                 "creditsCents": 500, "durationSec": 60})
    command(fz, {"action": "tournamentArm"})
    check("the arm claim snapshots the stake into runCreditsCents",
          fz.tournament.get("runCreditsCents") == 500,
          fz.tournament.get("runCreditsCents"))
    fz._tournament_tick(now=time.time())   # sas funds NOW; g2s confirming
    check("the fast seat funded at the armed stake (500¢)",
          sas_types(fz.sas_cmds) == ["aft_transfer"]
          and fz.sas_cmds[0][1]["cents"] == 500, fz.sas_cmds)
    code, body = command(fz, {"action": "tournamentConfigure",
                              "creditsCents": 2000, "durationSec": 60})
    check("reconfigure while armed still lands (for the NEXT round)",
          code == 200 and body.get("ok") is True
          and fz.tournament["creditsCents"] == 2000, (code, body))
    fz.associations[EGM].meters_at = time.time()
    fz._tournament_tick(now=time.time())
    xml_fund = next((x for x, lb in fz.sent
                     if lb.startswith("initiateRequest")), "")
    check("the slow seat STILL funds at the frozen 500¢ — never the "
          "edited amount (equal stacks; the configure docstring's "
          "promise made true)",
          'g2s:reqCashableAmt="500000"' in xml_fund
          and 'g2s:reqCashableAmt="2000000"' not in xml_fund, xml_fund)

    # ---------------------------------------------------------------
    print("— cancel before the first tick: never-cleared glasses stay "
          "put")
    nb = make_engine()
    nb.associations[EGM] = FakeAssoc(EGM)
    with nb.associations[EGM].lock:
        nb.associations[EGM].meters[
            "G2S_cabinet/1/G2S_playerCashableAmt"] = "5000000"   # $50
    nb.card_sessions[EGM] = {"uid": "6CB16F06", "name": "AJ",
                             "accountId": "p1", "since": time.time()}
    nb.account_store.records["p1"] = {"id": "p1", "kind": "player",
                                      "name": "AJ",
                                      "cashableMillicents": 0}
    command(nb, {"action": "tournamentConfigure",
                 "creditsCents": 500, "durationSec": 60})
    command(nb, {"action": "tournamentArm"})
    n_sent = len(nb.sent)
    command(nb, {"action": "tournamentCancel"})   # fat-finger, first 0.5 s
    seat = nb.tournament["seats"][EGM]
    check("a fat-finger cancel inside the first tick NEVER sweeps the "
          "un-cleared glass — the carded friend's $50 stays on the "
          "machine, not in the House books (unlock is the only wire)",
          seat["sweep"]["state"] == "skipped"
          and "never cleared" in seat["sweep"]["detail"]
          and labels(nb.sent, n_sent) == ["setCabinetState",
                                          "getCabinetStatus"]
          and 'g2s:enable="true"' in nb.sent[n_sent][0],
          (seat.get("sweep"), labels(nb.sent, n_sent)))
    check("…and not one ledger leg moved",
          nb.account_store.records["house"]["cashableMillicents"] == 0
          and not nb.account_store.refs, set(nb.account_store.refs))
    # the DOCUMENTED remedy after a failed clear (cancel) must not sweep
    # either — the bench's own failure mode: lock_failed mid-game
    nf = make_engine()
    with nf.sas_lock:
        nf.sas_machines[SAS] = sas_entry(credits=100)
    command(nf, {"action": "tournamentConfigure",
                 "creditsCents": 500, "durationSec": 60})
    command(nf, {"action": "tournamentArm"})
    nf._tournament_tick(now=time.time())
    tr = dict(nf.tournament["seats"]["sas:" + SAS]["clear"])
    sas_result(nf, SAS, tr["cmdId"], "aft_cashout", ok=False,
               outcome="lock_failed",
               detail="game lock never confirmed (0xFF)")
    nf._tournament_tick(now=time.time())
    check("precondition: the clear FAILED (the bench's lock_failed) and "
          "the seat parked",
          nf.tournament["seats"]["sas:" + SAS].get("armStage") == "failed",
          nf.tournament["seats"]["sas:" + SAS])
    n_sas = len(nf.sas_cmds)
    command(nf, {"action": "tournamentCancel"})
    seat = nf.tournament["seats"]["sas:" + SAS]
    check("cancel after a FAILED clear: unlock only — the parked credits "
          "stay with the floor instead of riding a refund-less pull to "
          "the House",
          sas_types(nf.sas_cmds, n_sas) == ["sas_enable"]
          and seat["sweep"]["state"] == "skipped"
          and "never cleared" in seat["sweep"]["detail"],
          (nf.sas_cmds[n_sas:], seat.get("sweep")))

    # ---------------------------------------------------------------
    print("— the ingest-side pin reaper: a dead run's answered pull "
          "cannot leave a live House pin")
    pr = make_engine()
    with pr.sas_lock:
        pr.sas_machines[SAS] = sas_entry(credits=80)
    command(pr, {"action": "tournamentConfigure",
                 "creditsCents": 500, "durationSec": 60})
    command(pr, {"action": "tournamentArm"})
    march_ready(pr, clear_amounts={"sas:" + SAS: 400000})
    with pr.sas_lock:
        pr.sas_machines[SAS]["meters"]["0x1A"] = 60
    command(pr, {"action": "tournamentCancel"})   # the runner is DEAD now
    sweep = dict(pr.tournament["seats"]["sas:" + SAS]["sweep"])
    pin = dict(pr._tournament_sweep_pins.get(SAS) or {})
    check("the cancel sweep pinned the House WITH its own pull's cmdId",
          sweep.get("state") == "pulling"
          and pin.get("acct") == "house"
          and pin.get("cmdId") == sweep.get("cmdId"), (sweep, pin))
    pr._retire_tournament_pins(
        [{"id": "someone-else", "type": "aft_cashout", "ok": False,
          "outcome": "lock_failed"}], SAS)
    check("a FOREIGN result id never retires the pin",
          SAS in pr._tournament_sweep_pins)
    pr._retire_tournament_pins(
        [{"id": sweep["cmdId"], "type": "aft_cashout", "ok": True,
          "outcome": "completed", "amountCents": 300, "txnId": "keep-1"}],
        SAS)
    check("a CREDITING answer leaves the pin for the settle to consume",
          SAS in pr._tournament_sweep_pins)
    pr._retire_tournament_pins(
        [{"id": sweep["cmdId"], "type": "aft_cashout", "ok": False,
          "outcome": "lock_failed",
          "detail": "game lock never confirmed (0xFF)"}], SAS)
    check("the non-crediting answer retires the pin at INGEST (no "
          "observer left alive) — a guest's own CASH OUT five minutes "
          "later can no longer settle to the House",
          SAS not in pr._tournament_sweep_pins,
          pr._tournament_sweep_pins)

    # ---------------------------------------------------------------
    print("— the refund rides the STAMPED owner, never the live card")
    ro = make_engine()
    ro.associations[EGM] = FakeAssoc(EGM)
    with ro.associations[EGM].lock:
        ro.associations[EGM].meters[
            "G2S_cabinet/1/G2S_playerCashableAmt"] = "3000000"   # $30
    ro.account_store.records["p1"] = {"id": "p1", "kind": "player",
                                      "name": "A",
                                      "cashableMillicents": 0}
    ro.account_store.records["p2"] = {"id": "p2", "kind": "player",
                                      "name": "B",
                                      "cashableMillicents": 0}
    with ro.companion_lock:
        ro.card_sessions[EGM] = {"uid": "04AABBCC", "name": "A",
                                 "accountId": "p1",
                                 "sinceIso": "2026-07-21T00:00:00Z"}
    command(ro, {"action": "tournamentConfigure", "creditsCents": 100,
                 "durationSec": 60, "countdownSec": 3})
    command(ro, {"action": "tournamentArm"})
    march_ready(ro, clear_amounts={EGM: 3000000})
    check("the refund owner is stamped at the clear settle — the moment "
          "the money moved",
          ro.tournament["seats"][EGM].get("refundAccountId") == "p1",
          ro.tournament["seats"][EGM])
    # friend A heads home; friend B cards onto the seat before the horn
    with ro.companion_lock:
        ro.card_sessions[EGM] = {"uid": "99AABBCC", "name": "B",
                                 "accountId": "p2",
                                 "sinceIso": "2026-07-21T01:00:00Z"}
    command(ro, {"action": "tournamentStart"})
    ro._tournament_tick(now=ro.tournament["startsAt"])
    ro._tournament_tick(now=ro.tournament["endsAt"] + 0.1)
    check("the display follows the live card (B wears the label)…",
          ro.tournament["seats"][EGM]["accountId"] == "p2"
          and ro.tournament["seats"][EGM]["name"] == "B",
          ro.tournament["seats"][EGM])
    check("…but A's $30 came home to A — B captures nothing, a card-out "
          "forfeits nothing",
          ro.account_store.records["p1"]["cashableMillicents"] == 3000000
          and ro.account_store.records["p2"]["cashableMillicents"] == 0,
          {k: v.get("cashableMillicents")
           for k, v in ro.account_store.records.items()})

    # ---------------------------------------------------------------
    print("— per-bucket refund: promo comes back as promo")
    pb = make_engine()
    pb.associations[EGM] = FakeAssoc(EGM)
    with pb.associations[EGM].lock:
        pb.associations[EGM].meters[
            "G2S_cabinet/1/G2S_playerCashableAmt"] = "1000000"
    pb.account_store.records["p1"] = {"id": "p1", "kind": "player",
                                      "name": "AJ",
                                      "cashableMillicents": 0}
    pb.card_sessions[EGM] = {"uid": "6CB16F06", "name": "AJ",
                             "accountId": "p1", "since": time.time()}
    command(pb, {"action": "tournamentConfigure", "creditsCents": 100,
                 "durationSec": 60, "countdownSec": 3})
    command(pb, {"action": "tournamentArm"})
    pb._tournament_tick(now=time.time())
    tr = dict(pb.tournament["seats"][EGM]["clear"])
    # the commit actuals: $10 cashable + $5 promo left the glass
    wat_poke(pb, EGM, tr["requestId"], cash=1000000, promo=500000)
    march_ready(pb)
    seat = pb.tournament["seats"][EGM]
    check("the clear's per-bucket actuals ride the seat "
          "(absorbedBuckets); absorbedMc stays the display sum",
          seat.get("absorbedMc") == 1500000
          and seat.get("absorbedBuckets") == [1000000, 500000, 0], seat)
    command(pb, {"action": "tournamentStart"})
    pb._tournament_tick(now=pb.tournament["startsAt"])
    pb._tournament_tick(now=pb.tournament["endsAt"] + 0.1)
    p1 = pb.account_store.records["p1"]
    house = pb.account_store.records["house"]
    check("the refund pays each bucket IN KIND — promo is never "
          "laundered into cashable and the House's buckets stay square",
          p1.get("cashableMillicents") == 1000000
          and p1.get("promoMillicents") == 500000
          and house.get("cashableMillicents") == -1000000
          and house.get("promoMillicents") == -500000,
          (dict(p1), dict(house)))

    # ---------------------------------------------------------------
    print("— arm-stage verdict timeouts: expired commands park honestly")
    t0 = time.time()
    tm = make_engine()
    with tm.sas_lock:
        tm.sas_machines[SAS] = sas_entry(credits=10)
    command(tm, {"action": "tournamentConfigure",
                 "creditsCents": 500, "durationSec": 60})
    command(tm, {"action": "tournamentArm"})
    tm._tournament_tick(now=t0)
    check("precondition: the clear pull rode and pinned the House",
          (tm.tournament["seats"]["sas:" + SAS]["clear"] or {})
          .get("state") == "pulling"
          and SAS in tm._tournament_sweep_pins,
          tm.tournament["seats"]["sas:" + SAS])
    tm._tournament_tick(now=t0 + 30)
    check("inside the verdict window the seat keeps waiting",
          tm.tournament["seats"]["sas:" + SAS]["armStage"] == "clear")
    tm._tournament_tick(
        now=t0 + gh.G2SHost.TOURNAMENT_SAS_VERDICT_TIMEOUT_SEC + 1)
    seat = tm.tournament["seats"]["sas:" + SAS]
    check("a satellite that never answers (the 30 s queue expiry drops "
          "the command with NO result record) parks the seat at failed "
          "with an honest detail — never a forever-'pulling' wedge",
          seat["armStage"] == "failed"
          and "no verdict" in (seat.get("clear") or {}).get("detail", ""),
          seat)
    check("…and the leg's House pin retired with it (a late settle draws "
          "the LOUD unhomed HOLD, never a silent House credit)",
          SAS not in tm._tournament_sweep_pins, tm._tournament_sweep_pins)
    # the FUND stage twin
    tf = make_engine()
    with tf.sas_lock:
        tf.sas_machines[SAS] = sas_entry()
    command(tf, {"action": "tournamentConfigure",
                 "creditsCents": 500, "durationSec": 60})
    command(tf, {"action": "tournamentArm"})
    tf._tournament_tick(now=t0)
    tf._tournament_tick(
        now=t0 + gh.G2SHost.TOURNAMENT_SAS_VERDICT_TIMEOUT_SEC + 1)
    seat = tf.tournament["seats"]["sas:" + SAS]
    check("a verdictless FUND times out the same way (funded=failed — "
          "START names it, cancel recovers the floor)",
          seat.get("funded") == "failed"
          and "no verdict" in seat.get("fundDetail", "")
          and seat.get("armStage") == "failed", seat)
    # the g2s twin: an EGM that never answers the pull's requestPending
    tg = make_engine()
    tg.associations[EGM] = FakeAssoc(EGM)
    with tg.associations[EGM].lock:
        tg.associations[EGM].meters[
            "G2S_cabinet/1/G2S_playerCashableAmt"] = "30000"
    command(tg, {"action": "tournamentConfigure",
                 "creditsCents": 500, "durationSec": 60})
    command(tg, {"action": "tournamentArm"})
    tg._tournament_tick(now=t0)
    rid = tg.tournament["seats"][EGM]["clear"]["requestId"]
    tg._tournament_tick(
        now=t0 + gh.G2SHost.TOURNAMENT_WAT_VERDICT_TIMEOUT_SEC + 1)
    seat = tg.tournament["seats"][EGM]
    rec = tg.wat_store.find(EGM, request_id=rid)
    check("a silent EGM times out the g2s clear: the dead WAT record is "
          "cancelled LOCALLY (no txn — it must not wedge one-at-a-time "
          "forever) and the seat parks honestly",
          seat["armStage"] == "failed"
          and "never answered" in (seat.get("clear") or {}).get("detail",
                                                                "")
          and (rec or {}).get("state") == "cancelled", (seat, rec))

    # ---------------------------------------------------------------
    print("— the stale-zero mirror: a fresh read reveals hidden credits")
    hz = make_engine()
    hz.associations[EGM] = FakeAssoc(EGM)     # cold fold: no meter at all
    command(hz, {"action": "tournamentConfigure", "creditsCents": 500,
                 "durationSec": 60})
    command(hz, {"action": "tournamentArm"})
    hz._tournament_tick(now=time.time())
    check("the zero cold fold draws the scoped confirm read, not a $0 "
          "settle", labels(hz.sent) == ["getMeterInfo"]
          and 'g2s:deviceClass="G2S_cabinet"' in hz.sent[0][0],
          labels(hz.sent))
    # the fresh read lands — and reveals the $20 bill fed in during the
    # standing sub's blind window
    with hz.associations[EGM].lock:
        hz.associations[EGM].meters[
            "G2S_cabinet/1/G2S_playerCashableAmt"] = "2000000"
    hz.associations[EGM].meters_at = time.time()
    hz._tournament_tick(now=time.time())
    seat = hz.tournament["seats"][EGM]
    xml_pull = next((x for x, lb in hz.sent[1:]
                     if lb.startswith("initiateRequest")), "")
    check("the revealed credits are PULLED home, never funded over — an "
          "unseen $20 can no longer become House take at the sweep",
          (seat.get("clear") or {}).get("state") == "pulling"
          and 'g2s:watDirection="G2S_fromEgm"' in xml_pull
          and 'g2s:reqCashableAmt="2000000"' in xml_pull,
          (seat.get("clear"), labels(hz.sent)))

    # ---------------------------------------------------------------
    print("— linked seat: the SAS leg's own face restages independently")
    l2 = make_engine()
    l2.associations[WMS] = FakeAssoc(WMS)
    l2.sas_links = {WMS: SAS}
    with l2.sas_lock:
        l2.sas_machines[SAS] = sas_entry()
    command(l2, {"action": "tournamentConfigure", "creditsCents": 250,
                 "durationSec": 60, "countdownSec": 5})
    command(l2, {"action": "tournamentArm"})
    t0 = time.time()
    l2._tournament_tick(now=t0)              # clear skip -> fund rides
    sas_result(l2, SAS, l2.tournament["seats"][WMS]["fundCmdId"],
               "aft_transfer", ok=True, outcome="completed")
    l2.sas_refuse = "queue full (bench)"
    l2._tournament_tick(now=t0)              # LOCK over SAS — REFUSED
    seat = l2.tournament["seats"][WMS]
    check("an ARM lock whose SAS enqueue is refused leaves the seat OPEN "
          "on both faces (one lock, one owner = SAS; a refused enqueue "
          "flips nothing, and NO G2S setCabinetState ever goes to a "
          "linked cabinet) — the drift stays visible for the restage",
          seat.get("locked") is False and not seat.get("sasLocked")
          and labels(l2.sent) == [],
          (seat.get("armStage"), seat.get("locked"),
           seat.get("sasLocked"), labels(l2.sent)))
    l2.sas_refuse = None
    n_sas, n_sent = len(l2.sas_cmds), len(l2.sent)
    l2._tournament_tick(now=t0 + gh.G2SHost.TOURNAMENT_RESTAGE_SEC + 0.1)
    seat = l2.tournament["seats"][WMS]
    check("the restage re-delivers the SAS lock (0x01 re-sent, never a "
          "setCabinetState on a linked cabinet) and BOTH faces close — "
          "the BB2 can no longer sit PLAYABLE through countdown with "
          "tournament credits on the glass",
          sas_types(l2.sas_cmds, n_sas) == ["sas_disable"]
          and len(l2.sent) == n_sent
          and seat.get("locked") is True
          and seat.get("sasLocked") is True, l2.sas_cmds[n_sas:])
    # direction 2: the T-1.2 s unlock refused, then T0 arrives — the
    # single SAS-owned face still reads LOCKED, and the running restage
    # re-issues the enable so the race never runs against a dead machine
    command(l2, {"action": "tournamentStart"})
    starts = l2.tournament["startsAt"]
    l2.sas_refuse = "queue full (bench)"
    l2._tournament_tick(now=starts - 1.1)    # staged sas_enable REFUSED
    l2._tournament_tick(now=starts)          # T0: running
    seat = l2.tournament["seats"][WMS]
    check("through T0 the refused unlock leaves the seat LOCKED on both "
          "faces (SAS is the only lock; nothing flipped)",
          l2.tournament["phase"] == "running"
          and seat.get("locked") is True
          and seat.get("sasLocked") is True, seat)
    l2.sas_refuse = None
    n_sas = len(l2.sas_cmds)
    l2._tournament_tick(now=starts + gh.G2SHost.TOURNAMENT_RESTAGE_SEC
                        + 0.6)
    seat = l2.tournament["seats"][WMS]
    check("the RUNNING restage re-issues the refused sas_enable — the "
          "race no longer runs against a dead machine",
          "sas_enable" in sas_types(l2.sas_cmds, n_sas)
          and seat.get("sasLocked") is False
          and seat.get("locked") is False,
          (sas_types(l2.sas_cmds, n_sas), seat))

    # ---------------------------------------------------------------
    print("— cancel never pulls behind a refused unlock")
    cu = make_engine()
    with cu.sas_lock:
        cu.sas_machines[SAS] = sas_entry()
    command(cu, {"action": "tournamentConfigure",
                 "creditsCents": 500, "durationSec": 60})
    command(cu, {"action": "tournamentArm"})
    march_ready(cu)
    with cu.sas_lock:
        cu.sas_machines[SAS]["meters"]["0x1A"] = 40   # leftovers on glass
    cu.sas_refuse = "queue full (bench)"
    n_sas = len(cu.sas_cmds)
    command(cu, {"action": "tournamentCancel"})
    seat = cu.tournament["seats"]["sas:" + SAS]
    check("a REFUSED sas_enable stops the sweep pull (it would hard-fail "
          "lock_failed against the still-shut-down machine — the exact "
          "bench-day failure): sweep skipped loudly, no pull, no pin",
          sas_types(cu.sas_cmds, n_sas) == ["sas_enable"]
          and seat["sweep"]["state"] == "skipped"
          and "unlock refused" in seat["sweep"]["detail"]
          and SAS not in cu._tournament_sweep_pins,
          (cu.sas_cmds[n_sas:], seat.get("sweep")))

    # ---------------------------------------------------------------
    print("— a re-arm keeps the pin of a still-in-flight old pull")
    pa = make_engine()
    with pa.sas_lock:
        pa.sas_machines[SAS] = sas_entry(credits=50)
    command(pa, {"action": "tournamentConfigure",
                 "creditsCents": 500, "durationSec": 60})
    command(pa, {"action": "tournamentArm"})
    pa._tournament_tick(now=time.time())      # clear pull rides + pin
    old_pin = dict(pa._tournament_sweep_pins.get(SAS) or {})
    command(pa, {"action": "tournamentCancel"})   # clear still pulling
    with pa.sas_lock:
        pa.sas_machines[SAS]["meters"]["0x1A"] = 0   # satellite ran it
    command(pa, {"action": "tournamentArm"})      # re-arm in the window
    check("the prune EXEMPTS a re-seated leg whose old pull is still in "
          "flight — its settle keeps its home instead of drawing a LOUD "
          "unhomed HOLD on a routine cancel→re-arm",
          (pa._tournament_sweep_pins.get(SAS) or {}).get("cmdId")
          == old_pin.get("cmdId") and bool(old_pin.get("cmdId")),
          (old_pin, pa._tournament_sweep_pins))

    # ---------------------------------------------------------------
    print("— the restage trusts the EGM's OWN cabinet report")
    cb = make_engine()
    cb.associations[EGM] = FakeAssoc(EGM)
    command(cb, {"action": "tournamentConfigure",
                 "creditsCents": 500, "durationSec": 60})
    command(cb, {"action": "tournamentArm"})
    march_ready(cb)
    t1 = time.time()
    n_sent = len(cb.sent)
    cb._tournament_tick(now=t1 + gh.G2SHost.TOURNAMENT_RESTAGE_SEC + 0.1)
    check("with no fresh report the armed floor stays quiet (a stale "
          "hostEnabled is never drift evidence)",
          len(cb.sent) == n_sent, labels(cb.sent, n_sent))
    # the tailing getCabinetStatus confirm lands AFTER our lock send and
    # says the cabinet is still ENABLED — the disable POST was dropped
    cb.associations[EGM].cabinet = {"hostEnabled": "true"}
    cb.associations[EGM].cabinet_at = time.time()
    cb._tournament_tick(now=t1 + 2 * gh.G2SHost.TOURNAMENT_RESTAGE_SEC
                        + 0.2)
    check("a FRESH report contradicting the intent re-issues the lock — "
          "the counted-and-dropped POST is no longer invisible",
          labels(cb.sent, n_sent) == ["setCabinetState",
                                      "getCabinetStatus"]
          and 'g2s:enable="false"' in cb.sent[n_sent][0],
          labels(cb.sent, n_sent))
    n_sent = len(cb.sent)
    cb.associations[EGM].cabinet = {"hostEnabled": "false"}
    cb.associations[EGM].cabinet_at = time.time()
    cb._tournament_tick(now=t1 + 3 * gh.G2SHost.TOURNAMENT_RESTAGE_SEC
                        + 0.3)
    check("an AGREEING fresh report rests the restage",
          len(cb.sent) == n_sent, labels(cb.sent, n_sent))

    # ---------------------------------------------------------------
    print("— a LINKED seat never restages off its limited-G2S cabinet "
          "report (the 07-21 sas_disable flood)")
    # The BB2 speaks real but LIMITED G2S — no setCabinetState
    # deserializer, so its cabinet report reads hostEnabled=true forever.
    # One leg one owner: SAS is the control authority on a linked
    # cabinet, so the G2S report must NEVER be drift evidence there.
    fl = make_engine()
    fl.associations[WMS] = FakeAssoc(WMS)
    fl.sas_links = {WMS: SAS}
    with fl.sas_lock:
        fl.sas_machines[SAS] = sas_entry()
    command(fl, {"action": "tournamentConfigure",
                 "creditsCents": 500, "durationSec": 60})
    command(fl, {"action": "tournamentArm"})
    march_ready(fl)
    fl.associations[WMS].cabinet = {"hostEnabled": "true"}   # honest: the
    fl.associations[WMS].cabinet_at = time.time()            # class isn't
    n_sent, n_sas = len(fl.sent), len(fl.sas_cmds)           # supported
    t2 = time.time()
    for i in range(1, 4):
        fl._tournament_tick(
            now=t2 + i * (gh.G2SHost.TOURNAMENT_RESTAGE_SEC + 0.1))
    check("three restage windows later: ZERO re-locks on either leg "
          "(sasLocked is the linked seat's truth; the limited-G2S "
          "report is not drift evidence)",
          len(fl.sent) == n_sent and len(fl.sas_cmds) == n_sas,
          (labels(fl.sent, n_sent), sas_types(fl.sas_cmds, n_sas)))

    # ---------------------------------------------------------------
    print("— the real runner thread (its own controlled slice)")
    rt = make_engine()
    with rt.tournament_lock:
        rt.tournament.update({"phase": "armed", "id": "tbench",
                              "creditsCents": 100, "durationSec": 60,
                              "armedAt": time.time()})
    # call the CLASS method — make_engine stubs the instance attr so the
    # injected-clock slices above never raced a live thread
    gh.G2SHost._tournament_spawn_runner(rt, "tbench")
    th = rt._tournament_thread
    check("spawn starts THE daemon thread, named for the ps line",
          th is not None and th.is_alive() and th.daemon
          and th.name == "tournament")
    gh.G2SHost._tournament_spawn_runner(rt, "tbench")
    check("a second spawn is a no-op while the runner lives "
          "(is_alive guard)", rt._tournament_thread is th)
    with rt.tournament_lock:
        rt.tournament["phase"] = "finished"
    th.join(timeout=3)
    check("runner exits on its own once the run is over", not th.is_alive())
    check("…and CLEARS the thread slot under the lock on exit — the "
          "spawn's is_alive skip can never observe a committed-to-exit "
          "thread and strand a fresh run without its clock (the TOCTOU "
          "window closed)", rt._tournament_thread is None,
          rt._tournament_thread)
    with rt.tournament_lock:
        rt.tournament.update({"phase": "armed", "id": "tbench2"})
    gh.G2SHost._tournament_spawn_runner(rt, "STALE-RUN")
    th2 = rt._tournament_thread
    time.sleep(1.2)
    check("the runner is RUN-AGNOSTIC: a thread spawned under a stale id "
          "ADOPTS the live run instead of exiting — the is_alive spawn "
          "guard can never strand a fresh run without its clock",
          th2 is not th and th2.is_alive())
    with rt.tournament_lock:
        rt.tournament["phase"] = "idle"
    th2.join(timeout=3)
    check("…and exits only when the floor rests (idle/finished)",
          not th2.is_alive())

    # ---------------------------------------------------------------
    print("— floorLock/floorUnlock: the tournament's routing, no "
          "tournament (Settings 'lock all')")
    fx = make_engine()
    fx.associations[EGM] = FakeAssoc(EGM)         # pure g2s
    fx.associations[WMS] = FakeAssoc(WMS)         # linked
    fx.sas_links = {WMS: SAS}
    with fx.sas_lock:
        fx.sas_machines[SAS] = sas_entry()                    # linked leg
        fx.sas_machines["smib-bb1/1"] = sas_entry(smib="smib-bb1")  # unlinked
        fx.sas_machines["smib-bb1/4"] = sas_entry(
            smib="smib-bb1", sasEnabled=False)                # parked
    code, body = command(fx, {"action": "floorLock"})
    check("floorLock: hub-wide 200, per-machine outcomes returned",
          code == 200 and body.get("ok") is True
          and body.get("locked") is True and body.get("sent") == 3,
          (code, body))
    check("routing per the linking law: g2s over setCabinetState, linked "
          "+ unlinked over SAS, parked SKIPPED with its reason — never a "
          "setCabinetState to the linked cabinet",
          labels(fx.sent) == ["setCabinetState", "getCabinetStatus"]
          and 'g2s:enable="false"' in fx.sent[0][0]
          and sorted(sas_types(fx.sas_cmds)) == ["sas_disable",
                                                 "sas_disable"]
          and any(r["ok"] is False and "parked" in r["detail"].lower()
                  or "dark" in r["detail"].lower()
                  for r in body["results"]),
          (labels(fx.sent), sas_types(fx.sas_cmds), body["results"]))
    n_sent, n_sas = len(fx.sent), len(fx.sas_cmds)
    code, body = command(fx, {"action": "floorUnlock"})
    check("floorUnlock mirrors it (enable + sas_enable×2)",
          code == 200 and body.get("locked") is False
          and labels(fx.sent, n_sent) == ["setCabinetState",
                                          "getCabinetStatus"]
          and 'g2s:enable="true"' in fx.sent[n_sent][0]
          and sorted(sas_types(fx.sas_cmds, n_sas)) == ["sas_enable",
                                                        "sas_enable"],
          (labels(fx.sent, n_sent), sas_types(fx.sas_cmds, n_sas)))
    with fx.tournament_lock:
        fx.tournament["phase"] = "armed"
    code, body = command(fx, {"action": "floorLock"})
    check("a tournament anywhere but idle OWNS the floor locks — "
          "floorLock refuses at 200 (two owners re-fighting is the bug "
          "class this exists to prevent)",
          code == 200 and body.get("ok") is False
          and "tournament" in str(body.get("error")), (code, body))
    with fx.tournament_lock:
        fx.tournament["phase"] = "idle"

    # ---------------------------------------------------------------
    print("— legacy engines (no tournament attrs) keep settling")
    old = gh.G2SHost.__new__(gh.G2SHost)
    old.sas_links = {}
    old._glass_cash_out_pin = {}
    try:
        pin = old._sas_cashout_pin(SAS)
        check("_sas_cashout_pin survives a pre-tournament engine "
              "(getattr guard)", pin is None, pin)
    except AttributeError as e:
        check("_sas_cashout_pin survives a pre-tournament engine "
              "(getattr guard)", False, str(e))

    print(f"\nRESULT: {_p} passed, {_f} failed")
    sys.exit(1 if _f else 0)


if __name__ == "__main__":
    main()
