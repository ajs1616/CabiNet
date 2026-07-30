#!/usr/bin/env python3
"""
test_companion_rfid.py — standalone gate for the hub side of RFID phase 1
(Companion tap ingest + the G2S idReader carded-session machinery in
g2s_host.py).

In-process, no live host, no sockets, stdlib only — lightweight fakes
(FakeAssoc / FakeHubStore / FakeAccounts) stand in for the association,
the hub.db fobs table and the AccountStore, and _enqueue is captured so
every would-be wire send is asserted on instead of POSTed.

Covers:
  * set_id_validation_xml — ALL 21 Table 18.9 attributes present by name
    (the MSX005 regression trap), correct card-in vs card-out values,
    html escaping, well-formedness.
  * enqueue_set_id_validation — the enqueue_set_bonus_award clone: idReader
    class wrapper, deviceId/sessionType/timeToLive framing, label.
  * companion_report — registration, heartbeat, tap clamps (max 32, junk
    dropped), ack watermark dedupe, startedAt-reset replay, registry cap.
  * piTail — the board-identity join key, end to end: the Companion daemon
    omits it off a Pi, the hub clamps both daemons' reports to 6 lowercase
    hex or stores nothing, and a hand-named SAS leg (smib-bb2) pairs with
    its reader on the tail alone.
  * the announce — sshUser / model / uartReady / i2cBus: the daemon says who
    it runs as and what the board is, the hub keeps only what it was actually
    told (junk and silence both read "unknown", never a capability), and the
    reported login survives find_companions into deploy/update.py's ssh.
  * the tap -> carded-session state machine — card-in / re-tap card-out /
    card-switch (out then in, FIFO order), plus every honest-skip
    precondition (unknown fob, no binding, offline machine, no owned
    idReader).
  * session lifetime — CARD_SESSION_MAX_SEC class default is 0 (NO
    auto-logout: the session is the wallet link, logout is explicit
    only), sweep expiry via injected clock is a bench-knob instance
    override, and /api/command cardOut is the UI's explicit logout.
  * reset-tier fob — the queued handpay_reset matches _handle_sas_command's
    exact entry shape ({id, type} + _queuedAt, no amount).

Run from G2S/:  python3 tools/test_companion_rfid.py
Must end "RESULT: N passed, 0 failed".
"""

import getpass
import itertools
import json
import logging
import os
import re
import sys
import tempfile
import threading
import time
import xml.etree.ElementTree as ET

G2S_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, G2S_DIR)
import g2s_host as gh  # noqa: E402
# The REAL v14 registry for the devices fold: that fold reads back what the
# report path wrote, so a fake on both ends would only prove the fake. Built
# on a tempfile — GR-04, G2S/data is never opened here.
from hub_store import HubStore  # noqa: E402

# The Companion DAEMON's half of the wire, asserted here alongside the hub's:
# piTail only joins a board's two legs if the sender and the receiver agree on
# it, and this is the one gate that sees both ends. Imported AFTER g2s_host so
# the hub tree keeps first claim on every module name; safe to import at all
# because the daemon is stdlib-only (reader.py included) — the
# test_cabinetconfig cross-tree idiom.
sys.path.insert(0, os.path.join(os.path.dirname(G2S_DIR), "Companion"))
import companion_host as ch  # noqa: E402

# The UPDATER's half: the reported login only matters if it survives all the
# way to the ssh call, and find_companions is the one hop that used to drop it
# (leaving every reader adopted under another username "NOT updated" forever).
# Same cross-tree idiom, same reason it is safe: update.py is stdlib-only and
# does nothing at import but define constants and functions.
sys.path.insert(0, os.path.join(os.path.dirname(G2S_DIR), "deploy"))
import update as upd  # noqa: E402

logging.disable(logging.CRITICAL)   # the checks print, the engine doesn't

EGM = "WMS_00:11:22:33:44:55"       # example egmId — a MAC, never a game/theme name
UID = "6CB16F06"                    # first live card (S50 1K, 2026-07-10)

_p = _f = 0


def check(name, ok, detail=""):
    global _p, _f
    if ok:
        _p += 1
        print(f"  ✅ {name}")
    else:
        _f += 1
        print(f"  ❌ {name} {detail}")


# ---------------------------------------------------------------- fakes

class FakeAssoc:
    """Just enough association for build_inner_request + the ownership
    resolver: egm_id/comms_state/lock, the config-sync + commHostList
    ownership sources, and the two id sequences."""

    def __init__(self, egm_id=EGM, owned=("1",), state="onLine"):
        self.egm_id = egm_id
        self.comms_state = state
        self.lock = threading.Lock()
        self.config_devices = {
            f"G2S_idReader/{d}": {"deviceClass": "G2S_idReader",
                                  "deviceId": str(d)} for d in owned}
        self.host_items = {"1": {"owned": [f"G2S_idReader/{d}"
                                           for d in owned]}}
        self._sid = itertools.count(100)
        self._cid = itertools.count(500)

    def next_session_id(self):
        return next(self._sid)

    def next_command_id(self):
        return next(self._cid)


class FakeHubStore:
    """The fobs-table surface _process_companion_tap uses (fob_get /
    fob_touch), with call recording for the dedupe assertions."""

    def __init__(self, fobs=None):
        self.by_uid = {k: dict(v) for k, v in (fobs or {}).items()}
        self.gets = []
        self.touched = []
        self.nicks = {}          # machineKey -> nickname (registered-name signal)
        self.registry = []       # machines() rows (boot-seed source)
        self.settings = {}       # hub-wide host settings (gameroom_name, …)
        self.devices = {}        # v14 device rows (deviceTail -> wire dict)
        self.sats = {}           # v14 satellites rows the board attach writes

    def fob_get(self, uid):
        self.gets.append(uid)
        rec = self.by_uid.get(uid)
        return dict(rec) if rec else None

    def fob_touch(self, uid, when_iso):
        self.touched.append((uid, when_iso))

    def names(self):
        # {machineKey: nickname} — glass_state reads it for the tile "nick",
        # and _is_registered treats a non-empty nickname as a REGISTERED
        # machine. Empty by default; the reaper test seeds a nick.
        return dict(self.nicks)

    def machines(self):
        # the registered-fleet registry seed_registered_machines() reads at
        # boot; each row is {machine_key, protocol, ...}. Empty by default.
        return [dict(r) for r in self.registry]

    def host_setting(self, key, default=None):
        # hub-wide settings surface (gameroom_name is read by glass_state +
        # /api/status hostOptions). Returns default when unset.
        v = self.settings.get(key)
        return default if v is None else v

    def set_host_setting(self, key, value):
        self.settings[key] = value

    # --- the sas_report surface (piTail rides the SAS leg too) --------------
    def machine_prefs(self, machine_key):
        # operator prefs per SAS machine key; empty = every default
        return {}

    def touch_machine(self, *a, **kw):
        # registered-fleet upsert — a no-op here, the ledger is hub_store's
        pass

    def ticket_header(self, *a, **kw):
        # hub-wide printed-voucher branding; propName unset = never pushed
        return {"propName": None, "rev": 0}

    # --- the v14 device registry surface _touch_device writes through -------
    # Every report now carries a board touch, so this stands in for the real
    # table (which test_hub_store.py gates against real SQL). Same contract:
    # None = keep, seen=False leaves lastSeen alone.
    def device_set(self, device_tail, label=None, model=None, ssh_user=None,
                   last_ip=None, meta=None, seen=True):
        row = self.devices.setdefault(
            device_tail, {"deviceTail": device_tail, "label": "",
                          "model": None, "sshUser": None, "lastIp": None,
                          "firstSeen": "2026-07-30T00:00:00",
                          "lastSeen": "2026-07-30T00:00:00", "meta": {}})
        for k, v in (("label", label), ("model", model),
                     ("sshUser", ssh_user), ("lastIp", last_ip)):
            if v is not None:
                row[k] = v
        if meta is not None:
            row["meta"] = dict(meta)
        if seen:
            row["lastSeen"] = "2026-07-30T00:00:01"
        return dict(row), None

    def device_all(self):
        return [dict(r) for r in self.devices.values()]

    def sat_set(self, sat_id, device_tail=None, **kw):
        row = self.sats.setdefault(sat_id, {"satId": sat_id, "label": "",
                                            "deviceTail": None})
        if device_tail is not None:
            row["deviceTail"] = device_tail or None
        row.update(kw)
        return dict(row), None


class FakeConfigStore:
    """The ConfigInventoryStore surface _is_registered uses: get(egm) returns a
    persisted record (truthy) only for a machine that JOINED + had its config
    read. Empty by default; the reaper test marks a machine registered."""
    def __init__(self):
        self.egms = {}
        self.path = "(fake config store)"   # assoc() logs this on a config reload
    def add(self, egm_id):
        self.egms[egm_id] = {"configDevices": {"x": 1}}
    def get(self, egm_id):
        return self.egms.get(egm_id)


class FakeAccounts:
    def __init__(self, accounts=None):
        self.accounts = {k: dict(v) for k, v in (accounts or {}).items()}

    def get(self, account_id):
        rec = self.accounts.get(str(account_id or ""))
        return dict(rec) if rec else None


def make_engine(fobs=None, accounts=None, assoc=None):
    """A G2SHost carrying ONLY the companion/idReader surface — __new__
    sidesteps the constructor's real stores/files (the test never touches
    G2S/data). _enqueue is replaced by an immediate build+capture, so
    eng.sent holds (inner_xml, label) per would-be send in FIFO order."""
    eng = gh.G2SHost.__new__(gh.G2SHost)
    eng.host_id = "1"
    eng.companions = {}
    eng.companion_lock = threading.Lock()
    eng.card_sessions = {}
    # Admin-overlay surface (supervisor-tap-over-a-carded-friend): the parallel
    # map the tap path + glass_state consult. Empty = no overlay in play, the
    # behavior every pre-overlay case here locks in; the overlay cases add to it.
    eng.admin_overlays = {}
    eng._fob_seq = itertools.count(1)
    eng.hub_store = FakeHubStore(fobs)
    eng.account_store = FakeAccounts(accounts)
    eng.associations = {}
    eng.assoc_lock = threading.Lock()
    eng.sas_machines = {}
    eng.sas_lock = threading.Lock()
    eng.sas_commands = {}
    # v11 satellite assignments: the in-memory binding map companion_report
    # consults (empty = every companion resolves to its REPORTED binding, the
    # pre-v11 behavior this suite locks in). Assignment-override cases set it.
    eng.sat_bindings = {}
    # v14 device registry surface: the per-board hub.db touch throttle every
    # report now runs through, and the lock its satellites half writes under.
    eng._device_touched = {}
    eng._sat_write_lock = threading.Lock()
    # Glass-nav v1 surface the carded-session tap path now touches (added to
    # production after this harness was first written). Real GlassSessionStore
    # (mint/revoke are self-contained + cheap) + the cash-out-pin state
    # _glass_card_out sweeps. The SPA-WINDOW push effects (_glass_recovery_push,
    # _glass_follow) are stubbed to no-ops: they need a modeled resident SPA
    # (assoc.glass_push) and are out of scope for the setIdValidation/binding
    # assertions here — stubbing them keeps `sent` counting exactly the
    # setIdValidation enqueues.
    eng.glass_sessions = gh.GlassSessionStore()
    eng._glass_spa_seen = {}
    eng._glass_peer_seen = {}   # glass_state's per-peer journal (overlay render tests)
    eng._glass_pin_lock = threading.Lock()
    eng._glass_cash_out_pin = {}
    eng._glass_recovery_push = lambda assoc: None
    eng._glass_follow = lambda assoc, visible: None
    eng.sas_links = {}
    # never-joined association reaper surface: config store (registered-by-
    # config signal), the registry-touch throttle map the teardown clears.
    eng.config_store = FakeConfigStore()
    eng._registry_touched = {}
    # sas_report surface (the piTail cases drive a SAS leg through the same
    # engine): the two TTL caches it reads on the per-second report path. Both
    # seeded EMPTY so the first read goes to FakeHubStore, like production.
    eng._sysval_cache = (None, 0.0)
    eng._ticket_cache = (None, 0.0)
    eng.sent = []
    eng._enqueue = lambda assoc, build, settle=0.0, epoch=None: \
        eng.sent.append(build(assoc))
    if assoc is not None:
        eng.associations[assoc.egm_id] = assoc
    return eng


#: pi_tail=ABSENT means the key never appears in the payload at all — the
#: shape a pre-1.1 daemon (and any daemon off a Pi) sends, and the DEFAULT
#: here so every pre-existing case keeps asserting that shape.
ABSENT = object()


def report(eng, taps=(), comp="companion-bb2",
           started="2026-07-10T00:00:00Z", g2s=EGM, smib=None,
           reader_ok=True, peer="192.168.60.87", pi_tail=ABSENT, **fields):
    """One daemon report. **fields go onto the payload VERBATIM under their
    wire names (sshUser / model / uartReady / i2cBus …) and are absent by
    default, so every pre-existing case keeps asserting the older shape."""
    body = {"companionId": comp, "startedAt": started, "uptimeSec": 12.5,
            "readerOk": reader_ok, "g2sEgmId": g2s, "sasSmib": smib,
            "sasAddress": "1", "taps": list(taps), "lastError": None}
    if pi_tail is not ABSENT:
        body["piTail"] = pi_tail
    body.update(fields)
    return eng.companion_report(body, peer)


def tap(n, uid=UID, at="2026-07-10T01:02:03.000Z"):
    return {"tapId": n, "uid": uid, "at": at}


# The Table 18.9 census — the MSX005 law says ALL of these, every time.
ATTRS_21 = ["idNumber", "idType", "idValidDateTime", "idValidSource",
            "idState", "idPreferName", "idFullName", "idClass", "localeId",
            "playerId", "idLossLimit", "idTripEnd", "idValidExpired",
            "idVIP", "idBirthday", "idAnniversary", "idBanned", "idPrivacy",
            "idGender", "idRank", "idAge"]


def xml_attrs(xml):
    return re.findall(r'g2s:([A-Za-z]+)="', xml)


def attr(xml, name):
    m = re.search(rf'g2s:{name}="([^"]*)"', xml)
    return m.group(1) if m else None


def devices_fold():
    """The /api/status 'devices' fold: ONE row per physical Pi.

    A collector's box runs several role daemons, each reporting under its own
    id (companion-a3f2b1 beside a hand-named smib-bb2), so the floor showed one
    box as several strangers. These cases pin the join — on piTail, never on an
    id string — and the two rules that make the page honest: a board that stops
    reporting keeps its row, and a board nothing is set up on is still a row.
    """
    eng = make_engine()
    eng.hub_store = HubStore(os.path.join(tempfile.mkdtemp(), "hub.db"))
    eng.sat_bindings = eng.hub_store.sat_bindings()

    def fold(e=None):
        e = e or eng
        return e.devices_snapshot(e.companion_snapshot(), e.sas_snapshot())

    print("— one box, one row: the devices fold")
    # AJ's BB2: a reader named from the silicon beside a HAND-NAMED SAS leg,
    # both on one Pi. Nothing but the tail can pair them.
    report(eng, comp="companion-a3f2b1", pi_tail="a3f2b1",
           peer="192.168.50.102", sshUser="gameroom",
           model="Raspberry Pi Zero 2 W Rev 1.0", uartReady=True,
           i2cBus="/dev/i2c-11")
    eng.sas_report({"smibId": "smib-bb2", "address": "1", "online": True,
                    "piTail": "A3F2B1", "sshUser": "gameroom"},
                   "192.168.50.102")
    d = fold()
    check("two daemons, two ids, ONE row — the box is the row",
          list(d) == ["a3f2b1"], list(d))
    row = d["a3f2b1"]
    check("both roles hang off that one row",
          sorted(row["roles"]) == ["reader", "sas"], sorted(row["roles"]))
    check("the SAS role carries the hand-named leg and its machine",
          row["roles"]["sas"]["id"] == "smib-bb2"
          and row["roles"]["sas"]["machines"] == ["smib-bb2/1"]
          and row["roles"]["sas"]["ok"] is True)
    check("the reader role carries its own id and its own health",
          row["roles"]["reader"]["id"] == "companion-a3f2b1"
          and row["roles"]["reader"]["ok"] is True)
    check("the board's own facts land ONCE, on the board",
          row["model"] == "Raspberry Pi Zero 2 W Rev 1.0"
          and row["ip"] == "192.168.50.102" and row["sshUser"] == "gameroom"
          and row["uartReady"] is True and row["i2cBus"] == "/dev/i2c-11")
    check("an unnamed board reads as a Pi, never as a bare serial",
          row["label"] == "New Pi (a3f2b1)" and row["named"] is False)
    check("a board that is reporting is not stale", row["stale"] is False)
    eng.sas_report({"smibId": "smib-nopi", "address": "1", "online": True},
                   "192.168.50.109")
    check("a leg that reports no tail joins NOTHING — a guess would fuse "
          "two boards forever",
          list(fold()) == ["a3f2b1"] and "smib-nopi/1" in eng.sas_snapshot())

    saved = eng.hub_store.device_get("a3f2b1")
    check("the report persists what the board said about itself",
          saved is not None
          and saved["model"] == "Raspberry Pi Zero 2 W Rev 1.0"
          and saved["sshUser"] == "gameroom"
          and saved["lastIp"] == "192.168.50.102"
          and saved["meta"] == {"uartReady": True, "i2cBus": "/dev/i2c-11"},
          saved)
    check("a report never NAMES a board — naming is the operator's",
          saved["label"] == "")

    print("— the row never vanishes: power pulled, then the hub restarted")
    with eng.companion_lock:
        eng.companions["companion-a3f2b1"]["lastSeen"] -= 3600
    with eng.sas_lock:
        eng.sas_machines["smib-bb2/1"]["receivedAt"] -= 3600
    d = fold()
    check("power pulled: the row STAYS, marked stale",
          list(d) == ["a3f2b1"] and d["a3f2b1"]["stale"] is True
          and sorted(d["a3f2b1"]["roles"]) == ["reader", "sas"]
          and d["a3f2b1"]["roles"]["sas"]["stale"] is True)
    check("...and a dark board still knows which board it is",
          d["a3f2b1"]["model"] == "Raspberry Pi Zero 2 W Rev 1.0"
          and d["a3f2b1"]["ip"] == "192.168.50.102")
    # a hub restart with the Pi still unplugged: the role registries are
    # memory, so the hub.db row is the ONLY reason the board is still there
    eng2 = make_engine()
    eng2.hub_store = HubStore(eng.hub_store.path)
    eng2.sat_bindings = eng2.hub_store.sat_bindings()
    d2 = eng2.devices_snapshot({}, {})
    check("a dark board survives a hub restart with NO roles reporting",
          list(d2) == ["a3f2b1"] and d2["a3f2b1"]["roles"] == {}
          and d2["a3f2b1"]["stale"] is True
          and d2["a3f2b1"]["reportAgeSec"] is None)
    check("a board with no roles yet is a valid row, dated by hub.db",
          d2["a3f2b1"]["ip"] == "192.168.50.102"
          and isinstance(d2["a3f2b1"]["lastSeen"], str))

    print("— the name: the board's own, then the legacy leg's, then neither")
    eng.hub_store.sat_set("companion-a3f2b1", kind="companion",
                          label="The Bluebird")
    eng.sat_bindings = eng.hub_store.sat_bindings()
    check("a leg named before v14 is attached to no board yet",
          eng.hub_store.sat_get("companion-a3f2b1")["deviceTail"] is None)
    eng._device_touched.clear()            # a minute passes
    report(eng, comp="companion-a3f2b1", pi_tail="a3f2b1",
           peer="192.168.50.102")
    check("the next report attaches that leg to its board, id untouched",
          eng.hub_store.sat_get("companion-a3f2b1")["deviceTail"] == "a3f2b1"
          and eng.hub_store.sat_get("companion-a3f2b1")["satId"]
          == "companion-a3f2b1")
    check("a name given to the LEG becomes the board's name",
          fold()["a3f2b1"]["label"] == "The Bluebird"
          and fold()["a3f2b1"]["named"] is True)
    eng.hub_store.device_set("a3f2b1", label="Bar Top Pi", seen=False)
    check("the board's OWN name outranks the legacy leg name",
          fold()["a3f2b1"]["label"] == "Bar Top Pi")

    print("— the shapes a collector's floor actually makes")
    engn = make_engine()
    report(engn, comp="companion-a1b2c3", pi_tail="a1b2c3",
           peer="192.168.50.147", reader_ok=False,
           model="Raspberry Pi 3 Model A Plus Rev 1.0")
    dn = fold(engn)["a1b2c3"]
    check("a freshly flashed Pi is a row: powered on, nothing set up yet",
          dn["label"] == "New Pi (a1b2c3)" and dn["stale"] is False
          and dn["roles"]["reader"]["ok"] is False
          and dn["model"] == "Raspberry Pi 3 Model A Plus Rev 1.0")
    engm = make_engine()
    for a in ("1", "2"):
        engm.sas_report({"smibId": "smib-bar", "address": a, "online": True,
                         "piTail": "d0d0d0"}, "192.168.50.55")
    dm = fold(engm)
    check("a multidrop Pi is ONE board with ONE role serving two machines",
          list(dm) == ["d0d0d0"]
          and sorted(dm["d0d0d0"]["roles"]) == ["sas"]
          and dm["d0d0d0"]["roles"]["sas"]["machines"]
          == ["smib-bar/1", "smib-bar/2"])

    print("— bounded, ordered, and it never takes the poll down with it")
    engc = make_engine()
    engc.hub_store = HubStore(os.path.join(tempfile.mkdtemp(), "hub.db"))
    for i in range(gh.G2SHost.COMPANION_MAX):
        engc.hub_store.device_set("%06x" % i, label="dark %d" % i)
    report(engc, comp="companion-ffffff", pi_tail="ffffff",
           peer="192.168.50.9")
    d3 = fold(engc)
    check("bounded at COMPANION_MAX — the companion registry's own ceiling",
          len(d3) == gh.G2SHost.COMPANION_MAX, len(d3))
    check("...and the board that is REPORTING is the one that survives",
          "ffffff" in d3 and d3["ffffff"]["stale"] is False)
    check("rows come out in tail order (the ETag must not churn on order)",
          list(d3) == sorted(d3))
    body = json.dumps(d3, separators=(",", ":"))
    check("a full section stays a rounding error beside one machine tile",
          len(body) < 4096, len(body))
    engf = make_engine()
    engf.hub_store.device_all = lambda: (_ for _ in ()).throw(
        RuntimeError("db down"))
    report(engf, comp="companion-b0b0b0", pi_tail="b0b0b0")
    d4 = fold(engf)
    check("a registry read fault costs the persisted half, never the poll",
          list(d4) == ["b0b0b0"] and d4["b0b0b0"]["named"] is False)


def main():
    print("— set_id_validation_xml: the 21-attribute census (MSX005 law)")
    x_in = gh.G2SHost.set_id_validation_xml(
        True, id_number=UID, id_type="G2S_player", prefer_name="Sam",
        full_name="Sam Sample", player_id="p1", id_class="player")
    got = xml_attrs(x_in)
    check("card-in carries ALL 21 attributes, no dupes",
          sorted(got) == sorted(ATTRS_21),
          f"missing={set(ATTRS_21) - set(got)} extra={set(got) - set(ATTRS_21)}")
    x_out = gh.G2SHost.set_id_validation_xml(False)
    check("card-out carries the SAME 21 attributes",
          sorted(xml_attrs(x_out)) == sorted(ATTRS_21))
    for x, label in ((x_in, "card-in"), (x_out, "card-out")):
        try:
            ET.fromstring(f'<r xmlns:g2s="urn:x">{x}</r>')
            check(f"{label} XML is well-formed", True)
        except ET.ParseError as e:
            check(f"{label} XML is well-formed", False, str(e))

    print("— card-in values")
    check("idNumber/idType/playerId as passed",
          attr(x_in, "idNumber") == UID and
          attr(x_in, "idType") == "G2S_player" and
          attr(x_in, "playerId") == "p1")
    check("host-sourced active state",
          attr(x_in, "idValidSource") == "G2S_host" and
          attr(x_in, "idState") == "G2S_active" and
          attr(x_in, "idValidExpired") == "false")
    check("names + class as passed",
          attr(x_in, "idPreferName") == "Sam" and
          attr(x_in, "idFullName") == "Sam Sample" and
          attr(x_in, "idClass") == "player")
    check("neutral tail (locale/gender/limits/flags)",
          attr(x_in, "localeId") == "en_US" and
          attr(x_in, "idGender") == "G2S_Unknown" and
          attr(x_in, "idLossLimit") == "0" and
          attr(x_in, "idRank") == "0" and attr(x_in, "idAge") == "0" and
          attr(x_in, "idVIP") == "false" and
          attr(x_in, "idBanned") == "false")

    print("— card-out values (spec no-ID-present defaults)")
    check("empty identity + G2S_none",
          attr(x_out, "idNumber") == "" and
          attr(x_out, "idType") == "G2S_none" and
          attr(x_out, "idValidSource") == "G2S_none" and
          attr(x_out, "idState") == "G2S_inactive")
    check("names/class/player cleared",
          attr(x_out, "idPreferName") == "" and
          attr(x_out, "idFullName") == "" and
          attr(x_out, "idClass") == "" and attr(x_out, "playerId") == "")
    check("idValidExpired=true on card-out",
          attr(x_out, "idValidExpired") == "true")
    x_out2 = gh.G2SHost.set_id_validation_xml(
        False, id_number="ZZZ", prefer_name="ghost", rank=7)
    check("card-out ignores stray identity args (rank forced 0)",
          attr(x_out2, "idNumber") == "" and
          attr(x_out2, "idPreferName") == "" and
          attr(x_out2, "idRank") == "0")

    print("— html escaping + clamps")
    x_esc = gh.G2SHost.set_id_validation_xml(
        True, prefer_name='A"J <&> B', full_name="x" * 100)
    check("quotes/angles/amps escaped in idPreferName",
          "&quot;" in x_esc and "&lt;" in x_esc and "&amp;" in x_esc and
          '<&' not in attr(x_esc, "idPreferName"))
    root = ET.fromstring(f'<r xmlns:g2s="urn:x">{x_esc}</r>')
    check("escaped value round-trips through an XML parser",
          root[0].get("{urn:x}idPreferName") == 'A"J <&> B')
    check("idFullName clamped to 64", len(attr(x_esc, "idFullName")) == 64)
    x_rank = gh.G2SHost.set_id_validation_xml(True, rank="junk")
    check("junk rank coerces to 0", attr(x_rank, "idRank") == "0")

    print("— enqueue_set_id_validation (the bonus-award clone)")
    a = FakeAssoc()
    eng = make_engine(assoc=a)
    eng.enqueue_set_id_validation(a, "1", True, id_number=UID,
                                  prefer_name="AJ")
    check("one send captured", len(eng.sent) == 1)
    inner, label = eng.sent[0]
    check("idReader class wrapper with deviceId",
          '<g2s:idReader g2s:deviceId="1"' in inner)
    check("G2S_request framing, timeToLive 30000",
          'g2s:sessionType="G2S_request"' in inner and
          'g2s:timeToLive="30000"' in inner)
    check("label names the command + presence",
          label.startswith("setIdValidation(present=True,dev=1,cid="))
    check("all 21 attributes rode the wire body",
          sorted(set(xml_attrs(inner)) & set(ATTRS_21)) == sorted(ATTRS_21))

    print("— ownership resolver")
    check("lowest owned idReader wins",
          eng.default_id_reader_device(FakeAssoc(owned=("10", "2"))) == "2")
    check("no owned idReader -> None (no blind '1')",
          eng.default_id_reader_device(FakeAssoc(owned=())) is None)

    print("— companion_report: registration + heartbeat")
    a = FakeAssoc()
    eng = make_engine(assoc=a)
    r = report(eng)
    check("heartbeat ok with watermark -1",
          r == {"ok": True, "ackTapId": -1}, r)
    snap = eng.companion_snapshot()
    c = snap.get("companion-bb2") or {}
    check("snapshot entry: readerOk/bindings/fresh",
          c.get("readerOk") is True and c.get("stale") is False and
          (c.get("bindings") or {}).get("g2sEgmId") == EGM and
          c.get("ackTapId") == -1 and c.get("lastTap") is None)

    print("— unknown fob: recorded, NOTHING else")
    r = report(eng, taps=[tap(0, uid="6c:b1:6f:06")])
    check("tap acked", r == {"ok": True, "ackTapId": 0}, r)
    lt = (eng.companion_snapshot()["companion-bb2"] or {}).get("lastTap")
    check("lastTap known=False with the NORMALIZED uid",
          lt == {"uid": UID, "at": "2026-07-10T01:02:03.000Z",
                 "known": False, "tier": None, "label": None}, lt)
    check("no session, no wire send, no auto-create",
          eng.card_sessions == {} and eng.sent == [] and
          eng.hub_store.by_uid == {})
    check("fob_touch still stamped (no-op if absent is the store's job)",
          eng.hub_store.touched and eng.hub_store.touched[0][0] == UID)

    print("— dedupe: ack watermark + startedAt reset")
    gets_before = len(eng.hub_store.gets)
    r = report(eng, taps=[tap(0, uid="6c:b1:6f:06")])
    check("replayed tapId ≤ watermark is NOT reprocessed",
          r == {"ok": True, "ackTapId": 0} and
          len(eng.hub_store.gets) == gets_before)
    r = report(eng, taps=[tap(0)], started="2026-07-10T09:00:00Z")
    check("new startedAt resets the watermark — tapId 0 replays",
          r == {"ok": True, "ackTapId": 0} and
          len(eng.hub_store.gets) == gets_before + 1)

    print("— tap clamps (attacker-typed wire)")
    r = report(eng, taps=[{"tapId": True, "uid": UID},
                          {"tapId": "9", "uid": UID},
                          {"tapId": -1, "uid": UID},
                          {"tapId": 5, "uid": 12345},
                          "junk", None,
                          {"tapId": 7, "uid": "  "}],
               started="2026-07-10T09:00:00Z")   # same run — no reset
    check("junk taps all dropped silently (watermark unmoved)",
          r == {"ok": True, "ackTapId": 0}, r)
    big = [tap(i) for i in range(1, 41)]
    r = report(eng, taps=big)
    check("taps capped at 32 per report (33..40 ride the next one)",
          r == {"ok": True, "ackTapId": 32}, r)
    r = report(eng, taps="notalist")
    check("non-list taps -> heartbeat", r == {"ok": True, "ackTapId": 32})

    print("— piTail on the wire: the Companion daemon's half")
    d = ch.CompanionHost(reader=None, hub_url="http://hub:8081",
                         companion_id="companion-a3f2b1")
    d.pi_tail = "a3f2b1"
    check("the daemon puts its board's tail on every report",
          d.snapshot(0.0).get("piTail") == "a3f2b1")
    d.pi_tail = None
    check("off a Pi the key is ABSENT — never null, never the hostname",
          "piTail" not in d.snapshot(0.0))
    check("the tail is derived, never minted: no tail, no invented id",
          d.snapshot(0.0)["companionId"] == "companion-a3f2b1")

    print("— piTail on the wire: the hub clamps, or stores nothing")
    engp = make_engine()
    report(engp, pi_tail="A3F2B1")
    check("a reported tail lands canonical (lowercase 6 hex)",
          engp.companion_snapshot()["companion-bb2"]["piTail"] == "a3f2b1")
    report(engp, pi_tail="  a3f2b1  ")
    check("surrounding space stripped — still the same board",
          engp.companion_snapshot()["companion-bb2"]["piTail"] == "a3f2b1")
    # A GUESS here is worse than a blank: the tail is a device PRIMARY KEY, so
    # a hostname or a truncated id admitted once would bind two boards into one
    # row forever. Everything that is not exactly 6 hex stores nothing.
    for junk, why in (("smib-bb2", "a hand-named id"),
                      ("Office-PC", "a hostname"),
                      ("a3f2b", "5 hex"),
                      ("a3f2b1c", "7 hex"),
                      ("g3f2b1", "not hex"),
                      ("", "empty"),
                      (None, "null"),
                      (123456, "not a string"),
                      (["a3f2b1"], "a list")):
        report(engp, pi_tail=junk)
        check("%s stores NOTHING rather than a guess" % why,
              engp.companion_snapshot()["companion-bb2"]["piTail"] is None,
              repr(junk))
    engq = make_engine()
    r = report(engq)                     # pre-1.1 daemon: no piTail key at all
    check("a report with no piTail is still a normal report",
          r == {"ok": True, "ackTapId": -1} and
          engq.companion_snapshot()["companion-bb2"]["piTail"] is None)

    print("— piTail on the wire: one board, two daemons, one key")
    # AJ's BB2: a HAND-NAMED SAS leg (smib-bb2 — its id carries no tail
    # anywhere) beside a reader named from the silicon. Nothing but piTail can
    # pair them, which is the whole reason this field exists.
    engj = make_engine()
    report(engj, comp="companion-a3f2b1", pi_tail="a3f2b1",
           peer="192.168.50.102")
    engj.sas_report({"smibId": "smib-bb2", "address": "1", "online": True,
                     "piTail": "A3F2B1", "sshUser": "gameroom"},
                    "192.168.50.102")
    csnap = engj.companion_snapshot()["companion-a3f2b1"]
    ssnap = engj.sas_snapshot()["smib-bb2/1"]
    check("the SAS leg's tail lands canonical on its snapshot too",
          ssnap.get("piTail") == "a3f2b1", ssnap.get("piTail"))
    check("the hand-named leg and the reader carry the SAME tail",
          csnap["piTail"] == ssnap["piTail"] == "a3f2b1" and
          "a3f2b1" not in ssnap["smibId"])
    engj.sas_report({"smibId": "smib-bb2", "address": "1", "online": True,
                     "piTail": "smib-bb2"}, "192.168.50.102")
    check("a junk tail leaves the SAS entry with NO piTail at all",
          "piTail" not in engj.sas_snapshot()["smib-bb2/1"])
    engj.sas_report({"smibId": "smib-nopi", "address": "1", "online": True},
                    "192.168.50.109")
    check("a SAS leg that reports no piTail is still a normal machine",
          "piTail" not in engj.sas_snapshot()["smib-nopi/1"] and
          engj.sas_snapshot()["smib-nopi/1"]["stale"] is False)

    print("— the announce: who this Pi is, and what a role would find")
    d = ch.CompanionHost(reader=None, hub_url="http://hub:8081",
                         companion_id="companion-a3f2b1")
    check("the daemon reports the login it runs as — the hub stops guessing",
          d.snapshot(0.0)["sshUser"] == getpass.getuser())
    # These four are NOT piTail: they ride EVERY report, null when unknown,
    # because "we looked and could not tell" is an answer a role prompt has to
    # be able to show. Only the join key is absent-when-unknown.
    d.ssh_user = d.model = d.i2c_bus = None
    d.uart_ready = False
    snap = d.snapshot(0.0)
    check("found nothing -> the keys still ride, as null (never absent)",
          all(k in snap for k in ("sshUser", "model", "uartReady", "i2cBus"))
          and snap["sshUser"] is None and snap["model"] is None
          and snap["i2cBus"] is None)
    d.reader_ok = False
    check("a board with NO PN532 attached still announces what it is",
          d.snapshot(0.0)["readerOk"] is False and "model" in d.snapshot(0.0))
    # The real probes, on whatever this gate runs on (the hub IS a Pi; a bench
    # box is not): the contract is that they never raise and never invent.
    m, u, b = ch._pi_model(), ch._uart_ready(), ch._i2c_bus()
    check("the probes never raise off a Pi, and never invent a value",
          (m is None or isinstance(m, str)) and isinstance(u, bool)
          and (b is None or b.startswith("/dev/i2c-")), (m, u, b))

    print("— the announce: the hub keeps the truth, or keeps nothing")
    enga = make_engine()
    report(enga, sshUser="gameroom", model="Raspberry Pi Zero 2 W Rev 1.0",
           uartReady=True, i2cBus="/dev/i2c-11")
    c = enga.companion_snapshot()["companion-bb2"]
    check("the reported login lands on the snapshot update.py reads",
          c["sshUser"] == "gameroom", c.get("sshUser"))
    check("model / uartReady / i2cBus land as reported",
          c["model"] == "Raspberry Pi Zero 2 W Rev 1.0"
          and c["uartReady"] is True and c["i2cBus"] == "/dev/i2c-11")
    report(enga, sshUser="  gameroom  ")
    check("surrounding space stripped off the login",
          enga.companion_snapshot()["companion-bb2"]["sshUser"] == "gameroom")
    for junk, why in (("", "an empty login"),
                      ("   ", "a whitespace-only login"),
                      (None, "a null login"),
                      (1000, "a uid instead of a name"),
                      (["pi"], "a list where the name goes")):
        report(enga, sshUser=junk)
        check("%s stores NOTHING rather than a guess" % why,
              enga.companion_snapshot()["companion-bb2"]["sshUser"] is None,
              repr(junk))
    # uartReady is the one field a collector would ACT on ("wire the machine
    # to this one"), so only a real bool counts — everything else is unknown.
    for junk in ("true", 1, "yes", [], {}):
        report(enga, uartReady=junk)
        check("uartReady=%r reads as unknown, never as ready" % (junk,),
              enga.companion_snapshot()["companion-bb2"]["uartReady"] is None)
    report(enga, uartReady=False)
    check("uartReady=false SURVIVES — 'no' is not 'we never asked'",
          enga.companion_snapshot()["companion-bb2"]["uartReady"] is False)
    report(enga, sshUser="u" * 200, model="m" * 200,
           i2cBus="/dev/i2c-" + "9" * 200)
    c = enga.companion_snapshot()["companion-bb2"]
    check("an oversized report is clamped, not refused",
          len(c["sshUser"]) == 64 and len(c["model"]) == 64
          and len(c["i2cBus"]) == 32)
    engb = make_engine()
    r = report(engb)                     # pre-0.2 daemon: none of the keys
    c = engb.companion_snapshot()["companion-bb2"]
    check("a pre-0.2 daemon is still a normal reader, all four null",
          r == {"ok": True, "ackTapId": -1} and c["sshUser"] is None
          and c["model"] is None and c["uartReady"] is None
          and c["i2cBus"] is None)

    print("— the announce: the login reaches deploy/update.py")
    engu = make_engine()
    report(engu, comp="companion-a3f2b1", sshUser="gameroom",
           peer="192.168.50.101")
    found = upd.find_companions({"companions": engu.companion_snapshot()})
    check("find_companions carries the reported login through",
          len(found) == 1 and found[0]["user"] == "gameroom", found)
    check("...and sat_user hands THAT to ssh, not the hub's own login",
          upd.sat_user(found[0]) == "gameroom"
          and upd.sat_user({}) == upd.SAT_USER)
    was = upd.SAT_USER_OVERRIDE
    upd.SAT_USER_OVERRIDE = "operator"
    try:
        check("an explicit --ssh-user still outranks it (authority order)",
              upd.sat_user(found[0]) == "operator")
    finally:
        upd.SAT_USER_OVERRIDE = was
    engv = make_engine()
    report(engv, comp="companion-old", peer="192.168.50.109")
    older = upd.find_companions({"companions": engv.companion_snapshot()})
    check("a reader too old to report one falls back, never crashes",
          older[0]["user"] is None
          and upd.sat_user(older[0]) == upd.SAT_USER)

    print("— carded session: in / re-tap out / switch / values")
    a = FakeAssoc()
    eng = make_engine(
        fobs={UID: {"uid": UID, "tier": "player", "label": "AJ fob",
                    "accountId": "p1"},
              "AA11BB22": {"uid": "AA11BB22", "tier": "attendant",
                           "label": "", "accountId": ""}},
        accounts={"p1": {"id": "p1", "name": "AJ", "kind": "player"}},
        assoc=a)
    report(eng, taps=[tap(0)])
    check("card-IN enqueued once", len(eng.sent) == 1 and
          eng.sent[0][1].startswith("setIdValidation(present=True"))
    check("account name wins idPreferName; playerId = accountId",
          attr(eng.sent[0][0], "idPreferName") == "AJ" and
          attr(eng.sent[0][0], "playerId") == "p1" and
          attr(eng.sent[0][0], "idNumber") == UID and
          attr(eng.sent[0][0], "idType") == "G2S_player" and
          attr(eng.sent[0][0], "idClass") == "player")
    sess = eng.card_sessions.get(EGM) or {}
    check("session recorded {uid, name, since..., deviceId}",
          sess.get("uid") == UID and sess.get("name") == "AJ" and
          sess.get("deviceId") == "1" and bool(sess.get("sinceIso")))
    cs = eng.card_sessions_snapshot()
    check("cardSessions snapshot shape {uid, name, since}",
          set((cs.get(EGM) or {}).keys()) == {"uid", "name", "since"} and
          cs[EGM]["name"] == "AJ")
    report(eng, taps=[tap(1)])
    check("re-tap = card-OUT + session cleared",
          len(eng.sent) == 2 and
          eng.sent[1][1].startswith("setIdValidation(present=False") and
          eng.card_sessions == {})
    report(eng, taps=[tap(2)])                       # AJ (player) back in
    check("AJ re-carded in (card-IN #3)",
          len(eng.sent) == 3 and
          eng.sent[2][1].startswith("setIdValidation(present=True") and
          (eng.card_sessions.get(EGM) or {}).get("uid") == UID)

    print("— admin overlay: tap OVER a carded friend (non-destructive)")
    # an attendant fob (admin via the legacy bridge) tapping over the carded
    # non-admin player AJ RAISES the supervisor overlay — it must NOT card AJ
    # out/in (no setIdValidation), AJ's body stays intact, and the overlay is
    # recorded with the ADMIN's identity. The glass token is now the admin's
    # (mint supersedes), so the next poll flips the glass to the admin stack.
    prev_player_tok, _ = eng.glass_sessions.peek_egm(EGM)
    report(eng, taps=[tap(3, uid="AA11BB22")])
    check("admin-over-player = OVERLAY, no card-out/in (friend untouched)",
          len(eng.sent) == 3 and
          (eng.card_sessions.get(EGM) or {}).get("uid") == UID and
          (eng.admin_overlays.get(EGM) or {}).get("uid") == "AA11BB22")
    admin_tok, arec = eng.glass_sessions.peek_egm(EGM)
    check("overlay minted an admin glass token superseding the player's",
          admin_tok is not None and admin_tok != prev_player_tok and
          arec.get("uid") == "AA11BB22")
    st = eng.glass_state(EGM)
    check("glass_state renders the admin overlay stack, credits SUPPRESSED",
          st.get("carded") is True and st.get("overlay") is True and
          st.get("admin") is True and st.get("sess") == admin_tok and
          "credits" not in st and "creditsMc" not in st)
    check("overlay names WHOSE session is supervised (friend's name, not wallet)",
          st.get("supervising") == "AJ")

    print("— admin overlay: ANY tap dismisses it, friend stays carded")
    # AJ (the friend) re-tapping while the overlay is up POPS ONLY THE OVERLAY
    # (AJ's rule) — AJ is NOT carded out, and the player token is re-minted so
    # the glass hands back to AJ. Still no setIdValidation on the wire.
    report(eng, taps=[tap(4, uid=UID)])
    check("dismiss tap pops overlay, friend still carded, no wire",
          len(eng.sent) == 3 and eng.admin_overlays == {} and
          (eng.card_sessions.get(EGM) or {}).get("uid") == UID)
    restored_tok, rrec = eng.glass_sessions.peek_egm(EGM)
    check("player token re-minted on dismiss (friend's own session back)",
          restored_tok is not None and restored_tok != admin_tok and
          rrec.get("uid") == UID and rrec.get("tier") == "player")
    st2 = eng.glass_state(EGM)
    check("glass_state back to the player: no overlay, credits restored",
          st2.get("carded") is True and not st2.get("overlay") and
          st2.get("credits") == "$0.00")

    print("— admin overlay: a genuine card-out drops the overlay too")
    report(eng, taps=[tap(5, uid="AA11BB22")])       # admin overlays AJ again
    check("overlay back up", (eng.admin_overlays.get(EGM) or {}).get("uid")
          == "AA11BB22")
    eng._glass_card_out(EGM)                          # AJ's session truly ends
    check("card-out pops BOTH the player body and the stale overlay",
          eng.card_sessions == {} and eng.admin_overlays == {})

    print("— admin overlay: sweep reclaims an abandoned overlay")
    a2 = FakeAssoc()
    eng.associations[EGM] = a2
    report(eng, taps=[tap(6, uid=UID)])               # AJ (player) in
    report(eng, taps=[tap(7, uid="AA11BB22")])        # admin overlays AJ
    check("overlay up before sweep",
          (eng.admin_overlays.get(EGM) or {}).get("uid") == "AA11BB22")
    # not yet idle -> survives
    eng.sweep_admin_overlays(now=time.time())
    check("fresh overlay survives the sweep",
          (eng.admin_overlays.get(EGM) or {}).get("uid") == "AA11BB22")
    # past the idle horizon -> reclaimed, friend restored (not carded out)
    eng.sweep_admin_overlays(now=time.time() + gh.ADMIN_OVERLAY_IDLE_SEC + 5)
    check("abandoned overlay reclaimed, friend stays carded",
          eng.admin_overlays == {} and
          (eng.card_sessions.get(EGM) or {}).get("uid") == UID)

    print("— honest skips (no send, no session)")
    eng2 = make_engine(fobs={UID: {"uid": UID, "tier": "player",
                                   "label": "AJ fob", "accountId": ""}},
                       assoc=FakeAssoc(state="offline"))
    report(eng2, taps=[tap(0)])
    check("offline machine: skipped", eng2.sent == [] and
          eng2.card_sessions == {})
    eng3 = make_engine(fobs={UID: {"uid": UID, "tier": "player",
                                   "label": "L", "accountId": ""}},
                       assoc=FakeAssoc(owned=()))
    report(eng3, taps=[tap(0)])
    check("no owned idReader: skipped", eng3.sent == [] and
          eng3.card_sessions == {})
    eng4 = make_engine(fobs={UID: {"uid": UID, "tier": "player",
                                   "label": "L", "accountId": ""}})
    report(eng4, taps=[tap(0)], g2s=None)
    check("no --g2s-egm binding: skipped", eng4.sent == [] and
          eng4.card_sessions == {})
    eng5 = make_engine(fobs={UID: {"uid": UID, "tier": "player",
                                   "label": "My label", "accountId": "p9"}},
                       assoc=FakeAssoc())
    report(eng5, taps=[tap(0)])
    check("unresolvable accountId falls back to the fob label",
          attr(eng5.sent[0][0], "idPreferName") == "My label" and
          attr(eng5.sent[0][0], "playerId") == "p9")

    print("— v11 zero-config assignment: the HUB owns the binding")
    # A flagless zero-config Companion (reports NO g2s-egm) that the operator
    # ASSIGNED to EGM in the UI -> the hub resolves the assignment and the tap
    # lands, even though the daemon reported nothing. This is the whole point:
    # flash one identical image, appear unassigned, bind from the UI.
    enga = make_engine(fobs={UID: {"uid": UID, "tier": "player",
                                   "label": "AJ fob", "accountId": "p1"}},
                       accounts={"p1": {"id": "p1", "name": "AJ",
                                        "kind": "player"}},
                       assoc=FakeAssoc())
    enga.sat_bindings["companion-bb2"] = {
        "satId": "companion-bb2", "g2sEgmId": EGM, "sasSmib": None,
        "sasAddress": None, "label": "Bar-top AVP"}
    report(enga, taps=[tap(0)], g2s=None)          # daemon reports NO binding
    check("assigned binding routes a flagless companion",
          len(enga.sent) == 1 and
          enga.sent[0][1].startswith("setIdValidation(present=True"))
    snap = enga.companion_snapshot()["companion-bb2"]
    check("snapshot: assigned=True + label + resolved binding, reported=None",
          snap["assigned"] is True and snap["label"] == "Bar-top AVP" and
          snap["bindings"]["g2sEgmId"] == EGM and
          snap["reported"]["g2sEgmId"] is None)

    # The assignment WINS over a stale reported flag — a legacy unit's flag is
    # only the fallback; once assigned, the hub's binding is authority.
    engb = make_engine(fobs={UID: {"uid": UID, "tier": "player",
                                   "label": "AJ", "accountId": ""}},
                       assoc=FakeAssoc())
    engb.sat_bindings["companion-bb2"] = {
        "satId": "companion-bb2", "g2sEgmId": EGM, "sasSmib": None,
        "sasAddress": None, "label": ""}
    report(engb, taps=[tap(0)], g2s="IGT_STALE_FLAG")
    check("assignment overrides a stale reported flag",
          engb.companions["companion-bb2"]["bindings"]["g2sEgmId"] == EGM and
          len(engb.sent) == 1)

    # A LABEL-ONLY row (named in the UI, not yet given a machine) must NOT zero
    # a reported binding — a legacy unit stays routable on its flag until it is
    # actually assigned, and assigned stays False.
    engc = make_engine(fobs={UID: {"uid": UID, "tier": "player",
                                   "label": "AJ", "accountId": ""}},
                       assoc=FakeAssoc())
    engc.sat_bindings["companion-bb2"] = {
        "satId": "companion-bb2", "g2sEgmId": None, "sasSmib": None,
        "sasAddress": None, "label": "Named but unassigned"}
    report(engc, taps=[tap(0)])                    # daemon reports g2s=EGM
    snap = engc.companion_snapshot()["companion-bb2"]
    check("label-only row keeps the reported binding, assigned=False",
          snap["assigned"] is False and
          snap["label"] == "Named but unassigned" and
          snap["bindings"]["g2sEgmId"] == EGM and len(engc.sent) == 1)

    # The reset-fob SAS leg is DERIVED from the machine's live SAS link, not
    # stored — so re-linking the machine re-routes the reader with no re-assign
    # (the stale-leg fix). Assigned to EGM + EGM linked to smib-bb2/1 ->
    # bindings.sasSmib resolves to the smibId, sasAddress to the multidrop addr.
    engd = make_engine(fobs={UID: {"uid": UID, "tier": "player",
                                   "label": "AJ", "accountId": ""}},
                       assoc=FakeAssoc())
    engd.sat_bindings["companion-bb2"] = {
        "satId": "companion-bb2", "g2sEgmId": EGM, "sasSmib": None,
        "sasAddress": None, "label": "Bar-top"}
    engd.sas_links = {EGM: "smib-bb2/1"}           # machine's live SAS leg
    report(engd, taps=[tap(0)], g2s=None)
    snap = engd.companion_snapshot()["companion-bb2"]
    check("reset-fob leg DERIVED from the machine's SAS link (smib + addr)",
          snap["bindings"]["sasSmib"] == "smib-bb2" and
          snap["bindings"]["sasAddress"] == "1")
    # Re-link the SAME machine to a different leg -> the reader follows with no
    # re-assign (proves the derivation is live, not a snapshot at assign time).
    engd.sas_links = {EGM: "smib-avp/2"}
    report(engd, taps=[])
    snap = engd.companion_snapshot()["companion-bb2"]
    check("re-linking the machine re-routes reset fobs live (no re-assign)",
          snap["bindings"]["sasSmib"] == "smib-avp" and
          snap["bindings"]["sasAddress"] == "2")

    print("— co-location AUTO-BIND: a reader ON a SAS SMIB serves that machine")
    # A flagless reader that reports from the SAME source IP as a SAS SMIB IS
    # that SMIB's reader — the hub binds it to the SMIB's machine with no
    # operator pick. The leg is linked to a G2S cabinet -> taps card over G2S.
    enge = make_engine(fobs={UID: {"uid": UID, "tier": "player",
                                   "label": "AJ", "accountId": ""}},
                       assoc=FakeAssoc())
    enge.sas_machines = {"smib-bb2/1": {"smibId": "smib-bb2",
                                        "peer": "192.168.50.102"}}
    enge.sas_links = {EGM: "smib-bb2/1"}           # the leg is linked to EGM
    report(enge, taps=[tap(0)], g2s=None, smib=None, peer="192.168.50.102")
    snap = enge.companion_snapshot()["companion-bb2"]
    check("co-located reader auto-binds to the linked machine (+auto flag)",
          snap["auto"] is True and snap["assigned"] is True and
          snap["bindings"]["g2sEgmId"] == EGM and
          snap["bindings"]["sasSmib"] == "smib-bb2")
    check("the tap cards a player in on the auto-bound machine",
          len(enge.sent) == 1 and
          enge.sent[0][1].startswith("setIdValidation(present=True"))

    # A reader that REPORTS its own binding (a legacy --g2s-egm flag) is honored
    # as-is even if it shares an IP with a SAS leg — the reported flag wins over
    # co-location (co-location is a FALLBACK for flagless readers only). This is
    # the exact case an all-localhost replay exercises.
    enge2 = make_engine(fobs={UID: {"uid": UID, "tier": "player",
                                    "label": "AJ", "accountId": ""}},
                        assoc=FakeAssoc())
    enge2.sas_machines = {"smib-x/1": {"smibId": "smib-x",
                                       "peer": "127.0.0.1"}}
    enge2.sas_links = {"IGT_OTHER": "smib-x/1"}
    report(enge2, taps=[tap(0)], g2s=EGM, peer="127.0.0.1")  # FLAGGED reader
    snap = enge2.companion_snapshot()["companion-bb2"]
    check("a FLAGGED reader honors its flag, co-location does NOT override",
          snap["auto"] is False and
          snap["bindings"]["g2sEgmId"] == EGM and
          enge2.sent[0][1].startswith("setIdValidation(present=True"))

    # An EXPLICIT operator assignment to another machine WINS over co-location
    # (auto is only the default when the operator hasn't chosen).
    engf = make_engine(fobs={UID: {"uid": UID, "tier": "player",
                                   "label": "AJ", "accountId": ""}},
                       assoc=FakeAssoc())
    engf.sas_machines = {"smib-bb2/1": {"smibId": "smib-bb2",
                                        "peer": "192.168.50.102"}}
    engf.sat_bindings["companion-bb2"] = {
        "satId": "companion-bb2", "g2sEgmId": "IGT_OTHER", "sasSmib": None,
        "sasAddress": None, "label": ""}
    report(engf, taps=[], g2s=None, peer="192.168.50.102")
    snap = engf.companion_snapshot()["companion-bb2"]
    check("explicit assignment beats co-location (auto False, honors the pick)",
          snap["auto"] is False and snap["bindings"]["g2sEgmId"] == "IGT_OTHER")

    # A reader whose IP matches NO SMIB does not auto-bind (stays unassigned).
    engg = make_engine(assoc=FakeAssoc())
    engg.sas_machines = {"smib-bb2/1": {"smibId": "smib-bb2",
                                        "peer": "192.168.50.102"}}
    report(engg, taps=[], g2s=None, peer="192.168.50.199")
    snap = engg.companion_snapshot()["companion-bb2"]
    check("non-co-located reader does NOT auto-bind",
          snap["auto"] is False and snap["assigned"] is False)

    # AMBIGUOUS co-location: a peer IP that maps to MORE than one SAS leg (a
    # multidrop Pi polling two machines, or a stale un-pruned entry) must NOT
    # auto-bind to a guessed cabinet — it falls through to require an explicit
    # pick, so a tap can't card a player into the WRONG machine.
    engh = make_engine(fobs={UID: {"uid": UID, "tier": "player",
                                   "label": "AJ", "accountId": ""}},
                       assoc=FakeAssoc())
    engh.sas_machines = {"smib5/1": {"smibId": "smib5", "peer": "10.0.0.5"},
                         "smib5/2": {"smibId": "smib5", "peer": "10.0.0.5"}}
    engh.sas_links = {"IGT_A": "smib5/1", "IGT_B": "smib5/2"}
    report(engh, taps=[tap(0)], g2s=None, peer="10.0.0.5")
    snap = engh.companion_snapshot()["companion-bb2"]
    check("AMBIGUOUS peer (2 legs, 1 IP) does NOT auto-bind — no wrong-machine",
          snap["auto"] is False and snap["bindings"]["g2sEgmId"] is None and
          len(engh.sent) == 0)

    print("— session lifetime: NO auto-logout by default (design rule)")
    check("class default CARD_SESSION_MAX_SEC == 0 (explicit logout only)",
          gh.G2SHost.CARD_SESSION_MAX_SEC == 0,
          gh.G2SHost.CARD_SESSION_MAX_SEC)
    a = FakeAssoc()
    eng6 = make_engine(fobs={UID: {"uid": UID, "tier": "player",
                                   "label": "AJ fob", "accountId": ""}},
                       assoc=a)
    report(eng6, taps=[tap(0)])
    t0 = eng6.card_sessions[EGM]["sinceTs"]
    eng6.sweep_card_sessions(now=t0 + 10 ** 9)
    check("default sweep is a NO-OP — the wallet-link session persists",
          EGM in eng6.card_sessions and len(eng6.sent) == 1)

    print("— auto-logout sweep (bench-knob instance override + clock)")
    eng6.CARD_SESSION_MAX_SEC = 300      # bench knob; the class stays 0
    eng6.sweep_card_sessions(now=t0 + 299)
    check("young session survives the sweep",
          EGM in eng6.card_sessions and len(eng6.sent) == 1)
    eng6.sweep_card_sessions(now=t0 + 301)
    check("expired session -> card-OUT + cleared",
          eng6.card_sessions == {} and len(eng6.sent) == 2 and
          eng6.sent[1][1].startswith("setIdValidation(present=False"))

    print("— explicit logout: /api/command cardOut")
    a = FakeAssoc()
    eng10 = make_engine(fobs={UID: {"uid": UID, "tier": "player",
                                    "label": "AJ fob", "accountId": "p1"}},
                        accounts={"p1": {"id": "p1", "name": "AJ",
                                         "kind": "player"}},
                        assoc=a)
    r = eng10.card_out(a)
    check("no session: honest ok:false, nothing sent",
          r == {"ok": False, "error": "no active card session"} and
          eng10.sent == [], r)
    report(eng10, taps=[tap(0)])
    r = eng10.card_out(a)
    check("card-OUT enqueued + session cleared + who was logged out",
          r == {"name": "AJ", "uid": UID} and
          eng10.card_sessions == {} and len(eng10.sent) == 2 and
          eng10.sent[1][1].startswith("setIdValidation(present=False"), r)
    check("card-out rode the session's deviceId", "dev=1" in eng10.sent[1][1])

    print("— reset-tier fob: the _handle_sas_command queue-entry shape")
    eng7 = make_engine(fobs={"DEADBEEF": {"uid": "DEADBEEF",
                                          "tier": "reset",
                                          "label": "Reset fob",
                                          "accountId": ""}})
    eng7.sas_machines["smib-bb2/1"] = {"smibId": "smib-bb2"}
    report(eng7, taps=[tap(0, uid="DE:AD:BE:EF")], g2s=None,
           smib="smib-bb2")
    q = list(eng7.sas_commands.get("smib-bb2") or [])
    check("exactly one command queued", len(q) == 1, q)
    cmd = q[0] if q else {}
    check("shape matches handpay_reset exactly (id/type/_queuedAt, "
          "no amount)",
          set(cmd) == {"id", "type", "_queuedAt"} and
          cmd.get("type") == "handpay_reset" and
          str(cmd.get("id", "")).startswith("fob"))
    check("no G2S traffic for a reset fob", eng7.sent == [])
    eng8 = make_engine(fobs={"DEADBEEF": {"uid": "DEADBEEF",
                                          "tier": "reset", "label": "",
                                          "accountId": ""}})
    r = report(eng8, taps=[tap(0, uid="DEADBEEF")], g2s=None,
               smib="smib-unknown")
    check("unknown smib: honest no-op, tap still acked",
          r == {"ok": True, "ackTapId": 0} and eng8.sas_commands == {})

    print("— registry cap (fresh entries are never evicted)")
    eng9 = make_engine()
    for i in range(eng9.COMPANION_MAX):
        report(eng9, comp=f"companion-{i}", g2s=None)
    r = report(eng9, comp="companion-overflow", g2s=None)
    check("full registry of LIVE companions rejects a new one",
          r.get("ok") is False and
          len(eng9.companions) == eng9.COMPANION_MAX, r)
    # age one out: a stale slot is evicted to admit the newcomer
    eng9.companions["companion-0"]["lastSeen"] -= 1000
    r = report(eng9, comp="companion-overflow", g2s=None)
    check("stale slot evicted to admit a live newcomer",
          r.get("ok") is True and "companion-0" not in eng9.companions and
          "companion-overflow" in eng9.companions)

    print("— stale-association reaper: anonymous phantoms only, REGISTERED spared")
    engR = make_engine()
    NOW = time.time()
    OLD = NOW - (gh.ASSOC_REAP_SEC + 30)     # silent past the reap horizon
    FRESH = NOW - 20                          # still talking (a live join)

    def mkassoc(egm, last_seen, online_seen=0, joined=None):
        a = gh.EgmAssociation(egm)
        a.last_seen = last_seen
        a.comms_online_seen = online_seen
        a.joined_at = joined
        engR.associations[egm] = a
        return a

    mkassoc("PHANTOM", OLD)                    # anon, never joined, silent -> REAP
    mkassoc("CONNECTING", FRESH)               # never joined but still talking -> keep
    mkassoc("WENT_DARK", OLD, online_seen=1)   # JOINED then offline -> keep (never touched)
    mkassoc("REG_BY_CONFIG", OLD)              # never joined but has config record -> keep
    engR.config_store.add("REG_BY_CONFIG")
    mkassoc("REG_BY_NAME", OLD)                # never joined but operator-named -> keep
    engR.hub_store.nicks["REG_BY_NAME"] = "Zeus"
    mkassoc("NEVER_STAMPED", 0.0)              # last_seen never set -> too-new-to-judge, keep

    engR.reap_stale_associations(now=NOW)
    left = set(engR.associations)
    check("the anonymous never-joined phantom is reaped",
          "PHANTOM" not in left)
    check("a join still in progress (recent contact) is spared",
          "CONNECTING" in left)
    check("a JOINED machine gone offline is never reaped",
          "WENT_DARK" in left)
    check("a REGISTERED machine (config record) is spared while offline",
          "REG_BY_CONFIG" in left)
    check("a REGISTERED machine (operator-named) is spared while offline",
          "REG_BY_NAME" in left)
    check("an association with no stamped contact is not reaped",
          "NEVER_STAMPED" in left)
    check("_is_registered: config record and nickname both count, else not",
          engR._is_registered("REG_BY_CONFIG") is True and
          engR._is_registered("REG_BY_NAME") is True and
          engR._is_registered("PHANTOM") is False)
    # the join-progress flag the floor renders as "Connecting…"
    check("snapshot.joining True only for never-joined + recently talking",
          engR.associations["CONNECTING"].snapshot().get("joining") is True and
          engR.associations["WENT_DARK"].snapshot().get("joining") is False and
          gh.EgmAssociation("x").snapshot().get("joining") is False)

    print("— boot seed: registered G2S cabinets present as OFFLINE tiles")
    engS = make_engine()
    engS.hub_store.registry = [
        {"machine_key": "IGT_AVP1", "protocol": "G2S"},   # registered G2S
        {"machine_key": "WMS_BB2",  "protocol": "G2S"},   # registered G2S
        {"machine_key": "smib-x/1", "protocol": "SAS"},   # SAS leg — NOT seeded
    ]
    engS.seed_registered_machines()
    check("every registered G2S cabinet is seeded as a placeholder",
          "IGT_AVP1" in engS.associations and "WMS_BB2" in engS.associations)
    check("a SAS leg is NOT seeded as a G2S association",
          "smib-x/1" not in engS.associations)
    check("a seeded placeholder is OFFLINE, never-joined, un-stamped",
          engS.associations["IGT_AVP1"].comms_state == "offline" and
          not engS.associations["IGT_AVP1"].comms_online_seen and
          not engS.associations["IGT_AVP1"].last_seen)
    # the seeded placeholder shows as a dark, registered tile (not "joining")
    ssnap = engS.associations["IGT_AVP1"].snapshot()
    check("seeded tile: offline, not joining (renders 'registered · offline')",
          ssnap.get("offline") is True and ssnap.get("joining") is False)
    # the reaper must NOT sweep a seeded placeholder (un-stamped last_seen)
    engS.reap_stale_associations(now=time.time() + gh.ASSOC_REAP_SEC + 99)
    check("the boot-seeded placeholder survives the reaper",
          "IGT_AVP1" in engS.associations and "WMS_BB2" in engS.associations)
    # a real re-handshake adopts the SAME placeholder (assoc returns it)
    same = engS.assoc("IGT_AVP1")
    check("a rejoin adopts the same placeholder object (join in place)",
          same is engS.associations["IGT_AVP1"])

    print("— gameroom name: collector's brand rides the glass_state poll")
    # The player-facing glass/kiosk (glass.html/smib.html) read s.gameroom from
    # every state poll and paint it "in lights" (attract + footer), falling back
    # to a neutral label when empty. So the wire contract is: glass_state emits
    # "gameroom" = the hub_store host_setting, "" until the collector sets one.
    engG = make_engine()
    engG.associations[EGM] = gh.EgmAssociation(EGM)
    gst = engG.glass_state(EGM)
    check("glass_state emits gameroom, empty by default (neutral fallback)",
          gst.get("gameroom") == "")
    engG.hub_store.set_host_setting("gameroom_name", "AJ's Casino")
    gst2 = engG.glass_state(EGM)
    check("glass_state reflects the set gameroom name live (no restart)",
          gst2.get("gameroom") == "AJ's Casino")
    # a db fault reading the setting must not kill the poll — degrades to ""
    engG.hub_store.host_setting = lambda *a, **k: (_ for _ in ()).throw(
        RuntimeError("db down"))
    gst3 = engG.glass_state(EGM)
    check("a host_setting fault degrades gameroom to '' (poll never 500s)",
          gst3.get("ok") is True and gst3.get("gameroom") == "")

    devices_fold()

    print(f"\nRESULT: {_p} passed, {_f} failed")
    return 1 if _f else 0


if __name__ == "__main__":
    sys.exit(main())
