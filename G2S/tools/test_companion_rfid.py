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
  * the admin menus on a machine's screen — who may ask for what (a player,
    an admin carded in normally, a tap-over), the service screen's reach
    (the friend at this machine and nobody else), money booked once, PINs,
    names, the players view, REMOVE FOB, and what reaches the logs.
  * ADD FOB — an admin arms the reader of the machine they are carded in
    at, and the next tap there is the new fob instead of a card-in: which
    fobs are taken and what the screen is told about the others, that such
    a tap cards nobody in or out, the 20 seconds, every way a wait ends,
    and that every other tap still does what it always did.

Run from G2S/:  python3 tools/test_companion_rfid.py
Must end "RESULT: N passed, 0 failed".
"""

import getpass
import http.client
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


# The OWNER'S live AVP (IGT_00012E492815), probed 2026-07-31 with
# /api/command mediaDisplayProbe rung=profile, device by device. This is the
# machine the glass works on TODAY, so it is the regression fixture: the
# picker must land on "1" — the LEFT Service Window — and dev 1 is exactly
# the window that carries NO mediaDisplayPosition attribute at all.
OWNER_WINDOWS = {
    "1": {"screenDescription": "Primary Screen",
          "mediaDisplayDescription": "Service Window",
          "xPosition": "0", "yPosition": "0",
          "mediaDisplayHeight": "1024", "mediaDisplayWidth": "256",
          "localConnectionPort": "30001"},
    "2": {"mediaDisplayPosition": "IGT_right",
          "mediaDisplayDescription": "Service Window",
          "xPosition": "1024", "yPosition": "0",
          "mediaDisplayHeight": "1024", "mediaDisplayWidth": "256"},
    "3": {"screenType": "IGT_secondary",
          "mediaDisplayPosition": "IGT_fullScreen",
          "mediaDisplayType": "IGT_overlay",
          "mediaDisplayDescription": "Digital Glass",
          "touchscreenCapable": "false",
          "mediaDisplayWidth": "1280", "mediaDisplayHeight": "1024"},
    "4": {"mediaDisplayPriority": "3",
          "mediaDisplayPosition": "IGT_fullScreen",
          "mediaDisplayType": "IGT_overlay",
          "mediaDisplayDescription": "Play Area",
          "mediaDisplayWidth": "1280", "mediaDisplayHeight": "1024"},
    "5": {"mediaDisplayPriority": "2", "mediaDisplayPosition": "IGT_bottom",
          "mediaDisplayDescription": "Player Banner",
          "mediaDisplayWidth": "1280", "mediaDisplayHeight": "32"},
    "6": {"mediaDisplayPriority": "4", "mediaDisplayPosition": "IGT_bottom",
          "mediaDisplayDescription": "Notification Window",
          "mediaDisplayWidth": "1280", "mediaDisplayHeight": "200"},
}


def glass_window(egm=EGM, windows=None, owned=("1", "2", "3", "4", "5", "6"),
                 profiled=None, state="onLine"):
    """A joined association carrying a mediaDisplay ownership map and, in
    assoc.media, the profile census exactly where the fold puts it."""
    a = gh.EgmAssociation(egm)
    a.comms_state = state
    a.host_items = {"1": {"owned": [f"IGT_mediaDisplay/{d}" for d in owned]}}
    windows = OWNER_WINDOWS if windows is None else windows
    for dev in (windows if profiled is None else profiled):
        a.media[dev] = {"lastCommand": "mediaDisplayProfile",
                        "mediaDisplayProfile": dict(windows[dev])}
    return a


def glass_window_picker():
    """The glass window is PICKED from the cabinet's own profile census,
    never assumed to be device 1.

    "Device 1 = the left Service Window" was one AVP's truth read as a law.
    A tester's cabinet exposes SEVEN mediaDisplay devices and answered every
    push at its device 1 with contentException=3 — his glass never came up.
    These cases pin the three things that make the fix safe: the owner's
    live map still resolves to "1", an explicit deviceId always wins, and
    anything the hub doesn't (yet) know falls back to GLASS_DEFAULT_DEVICE
    rather than guessing."""
    print("— glass window picker: the cabinet names its own windows")
    check("the owner's live 6-window map resolves to dev 1 (LEFT service "
          "window — the window the floor works on today)",
          gh.pick_glass_device(OWNER_WINDOWS) == "1",
          gh.pick_glass_device(OWNER_WINDOWS))
    check("dict order never decides it (same map, reversed)",
          gh.pick_glass_device(
              dict(reversed(list(OWNER_WINDOWS.items())))) == "1")
    # dev 1 carries NO mediaDisplayPosition; dev 2 says IGT_right. Absence
    # must beat a stated right — the exact case that must still win.
    check("no mediaDisplayPosition beats IGT_right (leftmost wins)",
          gh.pick_glass_device({k: OWNER_WINDOWS[k]
                                for k in ("2", "1")}) == "1")
    check("the right service window alone is still a usable page target",
          gh.pick_glass_device({"2": OWNER_WINDOWS["2"]}) == "2")
    # The tester's shape: the service window is NOT device 1.
    tester = {
        "1": {"mediaDisplayDescription": "Play Area",
              "mediaDisplayType": "IGT_overlay",
              "mediaDisplayWidth": "1920", "mediaDisplayHeight": "1080"},
        "2": {"mediaDisplayDescription": "Player Banner",
              "mediaDisplayWidth": "1920", "mediaDisplayHeight": "32"},
        "3": {"mediaDisplayDescription": "Digital Glass",
              "mediaDisplayType": "IGT_overlay",
              "mediaDisplayWidth": "1920", "mediaDisplayHeight": "1080"},
        "4": {"mediaDisplayDescription": "Service Window", "xPosition": "0",
              "mediaDisplayWidth": "256", "mediaDisplayHeight": "1080"},
        "5": {"mediaDisplayDescription": "Service Window",
              "mediaDisplayPosition": "IGT_right", "xPosition": "1664",
              "mediaDisplayWidth": "256", "mediaDisplayHeight": "1080"},
        "6": {"mediaDisplayDescription": "Notification Window",
              "mediaDisplayWidth": "1920", "mediaDisplayHeight": "200"},
        "7": {"mediaDisplayDescription": "Ticker",
              "mediaDisplayWidth": "1920", "mediaDisplayHeight": "48"},
    }
    check("a cabinet whose service window is device 4 resolves to 4 "
          "(seven windows, overlays and strips rejected)",
          gh.pick_glass_device(tester) == "4", gh.pick_glass_device(tester))
    check("a banner-only map falls back — a 1280x32 strip cannot host a UI",
          gh.pick_glass_device({"5": OWNER_WINDOWS["5"]})
          == gh.GLASS_DEFAULT_DEVICE)
    check("overlays + strips only (dev 3/4/5/6) fall back",
          gh.pick_glass_device({k: OWNER_WINDOWS[k]
                                for k in ("3", "4", "5", "6")})
          == gh.GLASS_DEFAULT_DEVICE)
    check("an empty map falls back", gh.pick_glass_device({})
          == gh.GLASS_DEFAULT_DEVICE)
    check("no map at all falls back (never None — a push always has a "
          "window to aim at)",
          gh.pick_glass_device(None) == gh.GLASS_DEFAULT_DEVICE)
    # EVERY attribute is optional: a census with nothing but a description
    # must still resolve, and junk must never raise on a push path.
    check("description alone is enough",
          gh.pick_glass_device(
              {"9": {"mediaDisplayDescription": "Service Window"}}) == "9")
    check("bare service windows tie-break on the lowest deviceId",
          gh.pick_glass_device(
              {"9": {"mediaDisplayDescription": "Service Window"},
               "2": {"mediaDisplayDescription": "Service Window"}}) == "2")
    check("the cabinet's casing is its own business",
          gh.pick_glass_device(
              {"5": {"mediaDisplayDescription": "service window"}}) == "5")
    # Junk must never raise on a push path — and a window the cabinet never
    # NAMED is not a service window, so the answer is the fallback, not the
    # last dict standing. Picking an unnamed window is how the glass ends up
    # on a game area we have never seen.
    check("junk in the census never raises, and an unnamed window is not a "
          "target",
          gh.pick_glass_device({"1": None, "2": "not-a-dict",
                                "3": {"mediaDisplayWidth": "wide"}})
          == gh.GLASS_DEFAULT_DEVICE)

    print("— glass target: explicit wins, a half-arrived census never moves it")
    eng = make_engine()
    a = glass_window()
    eng.associations[EGM] = a
    check("a settled census picks the LEFT service window",
          eng.glass_target_device(a) == "1")
    check("a complete census asks for nothing more (no wire at all)",
          eng.sent == [], eng.sent)
    check("the answer is journaled ONCE — (device, picked?) latched",
          a.glass_target_said == ("1", True), a.glass_target_said)
    check("an explicit deviceId ALWAYS wins over the picker",
          eng.glass_target_device(a, "5") == "5"
          and eng.glass_target_device(a, 6) == "6")
    # A tester's cabinet: same call, his own windows, his own answer.
    engT = make_engine()
    aT = glass_window(windows=tester, owned=tuple(tester))
    check("the same call lands on dev 4 for the tester's cabinet",
          engT.glass_target_device(aT) == "4")

    # THE regression guard: dev 2 (the RIGHT window) answering first must
    # not aim a working floor at the wrong side of the screen.
    engH = make_engine()
    aH = glass_window(profiled=("2",))
    got = engH.glass_target_device(aH)
    check("a HALF-arrived census stays on GLASS_DEFAULT_DEVICE (dev 2 alone "
          "would otherwise aim at the RIGHT window)",
          got == gh.GLASS_DEFAULT_DEVICE, got)
    check("...and it harvests exactly the windows it is still missing",
          [lbl.split(",")[0] for _, lbl in engH.sent]
          == [f"getMediaDisplayProfile(dev={d}"
              for d in ("1", "3", "4", "5", "6")],
          [lbl for _, lbl in engH.sent])
    engH.sent = []
    check("a second resolution asks nothing again — one ask per window per "
          "join, so this can never become a poll",
          engH.glass_target_device(aH) == gh.GLASS_DEFAULT_DEVICE
          and engH.sent == [], engH.sent)
    # ⚖️ A PARTIAL CENSUS NEVER DECIDES — no matter how old it is. This
    # assert used to pin the opposite (age it past a settle timer and let
    # whatever answered win), which on the owner's own cabinet means the ONE
    # window that replied is dev 2, the RIGHT service window: the glass moves
    # to the wrong side of a floor that works today. Waiting costs nothing —
    # the fallback IS the shipped behavior — while deciding early costs a
    # working cabinet. Time can never substitute for an answer.
    aH.media_profile_ts = time.time() - gh.GLASS_PROFILE_SETTLE_SEC - 1
    check("an aged PARTIAL census still refuses to decide (dev 2 alone must "
          "never move the glass to the right-hand window)",
          engH.glass_target_device(aH) == gh.GLASS_DEFAULT_DEVICE)

    print("— the census harvest: guest-safe, one ask per join, never blocking")
    engC = make_engine()
    aC = glass_window(profiled=())
    asked = engC.harvest_media_display_profiles(aC, why="a gate")
    check("every owned window is asked what it is, lowest deviceId first",
          asked == ["1", "2", "3", "4", "5", "6"], asked)
    check("the harvest sends getMediaDisplayProfile and nothing else",
          all(lbl.startswith("getMediaDisplayProfile(") for _, lbl in engC.sent)
          and len(engC.sent) == 6, [lbl for _, lbl in engC.sent])
    engC.sent = []
    check("one ask per window per join: a second harvest is a no-op",
          engC.harvest_media_display_profiles(aC) == [] and engC.sent == [])
    # A rejoin IS the retry — new_epoch clears the asked-set, so windows
    # that stayed silent get one more chance and nothing else does.
    with aC.lock:
        aC.new_epoch()
    aC.comms_state = "onLine"
    aC.media["2"] = {"mediaDisplayProfile": dict(OWNER_WINDOWS["2"])}
    engC.sent = []
    check("a rejoin re-asks the SILENT windows only (the one that answered "
          "is never asked again)",
          engC.harvest_media_display_profiles(aC)
          == ["1", "3", "4", "5", "6"])
    # ...and the map it is waiting for never delays a push.
    check("while the census is in flight the target is the fallback",
          engC.glass_target_device(aC) == gh.GLASS_DEFAULT_DEVICE)
    engOff = make_engine()
    aOff = glass_window(profiled=(), state="offline")
    check("an offline cabinet is never harvested",
          engOff.harvest_media_display_profiles(aOff) == []
          and engOff.sent == [])
    engU = make_engine()
    aU = glass_window(profiled=(), owned=())
    check("no ownership revealed yet -> nothing asked, nothing assumed",
          engU.harvest_media_display_profiles(aU) == [] and engU.sent == []
          and engU.glass_target_device(aU) == gh.GLASS_DEFAULT_DEVICE)

    print("— glassShow/glassHide aim at the picked window")
    engS = make_engine()
    engS._md_content_seq = itertools.count(1)
    aS = glass_window(windows=tester, owned=tuple(tester))
    engS.associations[EGM] = aS
    r = engS.start_glass_show(aS)
    # URI shape (2026-08-17): path-embedded /glass/<egm>/<dev>/<page>, query-
    # LESS and .html-final — the Windows-era content manager admits content by
    # file extension, and "?egm=&dev=" left the trailing token as "html?egm=…"
    # (nine wire-documented exception-3 deaths). The window still rides the
    # path's <dev> segment, so the pick assertion reads it there.
    check("glassShow with no deviceId lands on the picked service window",
          r.get("deviceId") == "4" and "/4/glass.html" in r.get("uri", "")
          and "?" not in r.get("uri", ""), r)
    check("the load rode the wire for that window",
          any(lbl.startswith("loadContent(") and ",dev=4," in lbl
              for _, lbl in engS.sent),
          [lbl for _, lbl in engS.sent])
    engS.sent = []
    r2 = engS.start_glass_hide(aS, device_id="5")
    check("an explicit deviceId still overrides on glassHide",
          r2.get("deviceId") == "5"
          and any("hideMediaDisplay(dev=5" in lbl for _, lbl in engS.sent),
          [lbl for _, lbl in engS.sent])

    print("— differential-load probe params (the console's A/B buttons)")
    # page= builds on the slot-VLAN base the CABINET can reach (never the
    # console's own address); withQuery appends the ?probe=1 twin that
    # adjudicates the Windows-era extension-admission theory.
    r3 = engS.start_media_display_probe(aS, rung="load", device_id="4",
                                        page="hello.html")
    check("probe load page= is hub-built and query-less",
          r3.get("uri") == gh.GLASS_CONTENT_URI_BASE + "hello.html", r3)
    r4 = engS.start_media_display_probe(aS, rung="load", device_id="4",
                                        page="hello.html", with_query=True)
    check("probe load withQuery appends exactly ?probe=1",
          r4.get("uri") == gh.GLASS_CONTENT_URI_BASE + "hello.html?probe=1",
          r4)
    r5 = engS.start_media_display_probe(aS, rung="load", device_id="4")
    check("probe load with nothing still defaults to the hello page",
          r5.get("uri") == gh.MD_DEFAULT_CONTENT_URI, r5)

    print("— contentException reads as words, never as a bare digit")
    check("the two seen in the wild say what happened, raw value kept",
          gh.content_exception_words("3")
          == "the cabinet refused the content (exception 3)"
          and gh.content_exception_words("1")
          == "the cabinet refused the content (exception 1)")
    check("no exception is silence (absent, empty, or the 0 no-error value)",
          gh.content_exception_words(None) == ""
          and gh.content_exception_words("") == ""
          and gh.content_exception_words("0") == "")
    check("an unknown code is still reported, never swallowed",
          gh.content_exception_words(7)
          == "the cabinet refused the content (exception 7)")


# ---------------------------------------------------------------- admin menus
# The admin menus on a machine's screen (glass.html, smib.html): the quick
# service screen of a tap-over, and the players menu of an admin carded in
# normally. These checks use the REAL AccountStore and the REAL HubStore on
# temp files (the fakes above have no adjust, create, set_pin, fob_set or
# fobs), and the lean engine for everything else.

class Screen:
    """The hub's two screen POST handlers on a lean engine, with the reply
    handed back instead of written to a socket."""
    _handle_glass_action = gh.G2SRequestHandler._handle_glass_action
    _handle_glass_admin = gh.G2SRequestHandler._handle_glass_admin

    def __init__(self, eng):
        self.host_engine = eng

    def _send(self, status, body, ctype=None, soap=False, **kw):
        return status, json.loads(body)


class WatchedLock:
    """companion_lock that can say whether it is held right now."""

    def __init__(self):
        self._lock = threading.Lock()
        self.held = False

    def __enter__(self):
        self._lock.acquire()
        self.held = True
        return self

    def __exit__(self, *exc):
        self.held = False
        self._lock.release()


def menu_engine(*egms):
    """A lean engine with real stores and one online machine per id."""
    tmp = tempfile.mkdtemp(prefix="cabinet-adminmenu-")
    eng = make_engine()
    eng.account_store = gh.AccountStore(
        os.path.join(tmp, "account_state.json"))
    eng.hub_store = HubStore(os.path.join(tmp, "hub.db"))
    eng._taps = {}
    for e in egms:
        a = FakeAssoc(egm_id=e)
        a.handpays_pending = {}
        a.meters = {}
        eng.associations[e] = a
    return eng


def player(eng, name, uid=None, admin=False, cents=0, free=0):
    """A player account, with a fob, the admin flag and money as asked."""
    acct, _ = eng.account_store.create(name)
    aid = acct["id"]
    if admin:
        eng.account_store.set_admin(aid, True)
    if uid:
        eng.hub_store.fob_set(uid, tier="player", account_id=aid)
    if cents or free:
        eng.account_store.adjust(aid, d_cash=cents * 1000,
                                 d_non=free * 1000, note="seed")
    return aid


def tap_at(eng, egm, uid, now=None, smib=None):
    """One tap of a fob on the reader of that machine. `now` is the time
    on the ADD FOB wait's clock, for the checks that set one; `smib` is
    the machine's SAS side, when its reader is bound to one too."""
    n = eng._taps.get(egm, -1) + 1
    eng._taps[egm] = n
    if now is not None:
        real = eng._process_companion_tap
        eng._process_companion_tap = lambda comp, t: real(comp, t, now)
    try:
        return report(eng, taps=[tap(n, uid=uid)], comp="companion-" + egm,
                      g2s=egm, smib=smib)
    finally:
        if now is not None:
            del eng._process_companion_tap


def tok_of(eng, egm):
    return eng.glass_sessions.peek_egm(egm)[0]


def adm(eng, tok, action, **body):
    """POST /api/glass/admin, without a server."""
    return eng.glass_admin_action(dict(body, sess=tok, action=action))


def act(eng, tok, action, **body):
    """POST /api/glass/action, without a server."""
    return Screen(eng)._handle_glass_action(
        json.dumps(dict(body, sess=tok, action=action)))


def cash(eng, aid):
    return eng.account_store.get(aid)["cashableMillicents"]


def free(eng, aid):
    return eng.account_store.get(aid)["nonCashMillicents"]


def rows(eng):
    return list(eng.account_store.state["ledger"])


def books(eng):
    """Everything an admin action could change, to compare before and
    after. Never printed: it holds PIN hashes."""
    fobs = sorted((f["uid"], f["tier"], f["accountId"])
                  for f in eng.hub_store.fobs())
    return json.dumps([eng.account_store.state, fobs], sort_keys=True)


_rid = itertools.count(1)


def rid():
    return "rid%05d" % next(_rid)


GONE = "That player is not there any more."
UNSURE = ("That may not have gone through. Check the balance before "
          "trying again.")
OWN_WALLET = "This is your own wallet. It can't be added to from this screen."
SVC = ("svcFund", "svcSetPin")
PL = ("plFund", "plTake", "plSetPin", "plClearPin", "plRename", "plCreate",
      "plAddFob", "plAddFobCancel", "plRemoveFob")
WALLET = ("walletFund", "cashOutToWallet", "cashOutNow")
GONE_VIEWS = ("tickets", "games", "settings")


def admin_menu_callers():
    print("— admin menus: the action names the hub knows")
    check("clearHandpay is the only admin action left on /api/glass/action",
          gh.GLASS_ADMIN_ACTIONS == ("clearHandpay",))
    check("the service actions and the players actions are the ones the "
          "pages use",
          gh.GLASS_SERVICE_ACTIONS == SVC and gh.GLASS_PLAYERS_ACTIONS == PL)
    check("the limits: the largest figure a keypad shows per action "
          "($99,999,999.99; not a house rule, it was $100,000.00), 16 "
          "letters in a name, a 20 second ADD FOB count",
          gh.GLASS_ADMIN_MAX_CENTS == 9_999_999_999
          and gh.GLASS_NAME_MAX == 16 and gh.ADD_FOB_WAIT_SEC == 20)

    eng = menu_engine("M_PLAYER", "M_PIN", "M_ADMIN", "M_ATT", "M_TAP",
                      "M_GONE")
    rescans = []
    eng.refresh_game_play_status = lambda assoc: rescans.append(assoc) or {}
    sam = player(eng, "Sam", "51A00001", cents=5000)
    pia = player(eng, "Pia", admin=True)
    aj = player(eng, "AJ", "A1A00001", admin=True, cents=2000)
    eng.hub_store.fob_set("A77E0001", tier="attendant", label="Floor key")
    chris = player(eng, "Christopher", "C4150001", cents=1000)
    dana = player(eng, "Dana", "DA4A0001", admin=True)
    vic = player(eng, "Vic", "B1C00001", cents=10000)
    eng.account_store.set_pin(pia, "771234")
    eng.account_store.set_pin(vic, "990011")

    tap_at(eng, "M_PLAYER", "51A00001")
    eng.pin_login("M_PIN", "771234")
    tap_at(eng, "M_ADMIN", "A1A00001")
    tap_at(eng, "M_ATT", "A77E0001")
    tap_at(eng, "M_TAP", "C4150001")
    tap_at(eng, "M_TAP", "DA4A0001")            # Dana taps over Christopher
    tap_at(eng, "M_GONE", "B1C00001")
    gone_tok = tok_of(eng, "M_GONE")
    eng._glass_card_out("M_GONE")       # a token from before a card-out
    check("the cast is on the floor: a player, a PIN login, two admins "
          "carded in normally, and a tap-over",
          all(tok_of(eng, m) for m in ("M_PLAYER", "M_PIN", "M_ADMIN",
                                       "M_ATT", "M_TAP"))
          and (eng.admin_overlays.get("M_TAP") or {}).get("uid") == "DA4A0001"
          and (eng.card_sessions.get("M_TAP") or {}).get("uid") == "C4150001"
          and tok_of(eng, "M_GONE") is None and bool(gone_tok))

    # What each action would do if it got through, aimed at Vic (who is
    # not at any machine): a caller who may not ask must leave no trace.
    def full():
        return {"svcFund": {"cents": 500, "rid": rid()},
                "svcSetPin": {"pin": "482913"},
                "plFund": {"accountId": vic, "cents": 500, "rid": rid()},
                "plTake": {"accountId": vic, "cents": 500, "rid": rid()},
                "plSetPin": {"accountId": vic, "pin": "482913"},
                "plClearPin": {"accountId": vic},
                "plRename": {"accountId": vic, "name": "Changed"},
                "plCreate": {"name": "Intruder", "rid": rid()},
                "plAddFob": {"accountId": vic},
                "plAddFobCancel": {},
                "plRemoveFob": {"accountId": vic, "fob": "C00001"}}

    def refused(tok, names, want=(403, "not authorized")):
        bad = []
        for name in names:
            if name in SVC + PL:
                st, r = adm(eng, tok, name, **full()[name])
            else:
                st, r = act(eng, tok, name, cents=500)
            if (st, r.get("error")) != want or r.get("ok") is not False:
                bad.append((name, st, r.get("error")))
        return bad

    def views(tok, names, want):
        bad = []
        for name in names:
            st, r = eng.glass_data(tok, name)
            if (st, r.get("error")) != want:
                bad.append((name, st, r.get("error")))
        return bad

    def everyone(who, tok):
        st, r = act(eng, tok, "refreshGames")
        check(f"{who}: refreshGames is gone (403 not yet) and starts no "
              "rescan of the machine's games",
              (st, r.get("error")) == (403, "not yet") and rescans == [],
              (st, r, len(rescans)))
        check(f"{who}: the tickets, games and settings views are gone "
              "(400 unknown view)",
              views(tok, GONE_VIEWS, (400, "unknown view")) == [],
              views(tok, GONE_VIEWS, (400, "unknown view")))
        st, r = adm(eng, tok, "makeAdmin", accountId=vic)
        check(f"{who}: an action /api/glass/admin does not know is 403 "
              "not yet",
              (st, r.get("error")) == (403, "not yet"), (st, r))

    print("— admin menus: no token, a made-up token, a token from before "
          "a card-out")
    before = books(eng)
    for who, tok in (("no token", None), ("an empty token", ""),
                     ("a made-up token", "gs_" + "0" * 24),
                     ("a token that is not text", ["gs_x"]),
                     ("a token from before a card-out", gone_tok)):
        bad = refused(tok, SVC + PL + ("clearHandpay", "refreshGames")
                      + WALLET, want=(401, "unknown session"))
        check(f"{who}: every action is 401 unknown session", bad == [], bad)
        bad = views(tok, ("players", "handpay", "cashout") + GONE_VIEWS,
                    (401, "unknown session"))
        check(f"{who}: every data view is 401 unknown session",
              bad == [], bad)
    check("...and none of it changed an account, a PIN, a fob or the "
          "ledger", books(eng) == before and rescans == [])

    print("— admin menus: a player who is not an admin")
    for who, egm in (("a player with a wallet", "M_PLAYER"),
                     ("an admin account logged in by PIN, with admin by "
                      "PIN switched off", "M_PIN")):
        tok = tok_of(eng, egm)
        before = books(eng)
        bad = refused(tok, SVC + PL + ("clearHandpay",))
        check(f"{who}: every service action, every players action and "
              "clearHandpay is 403 not authorized", bad == [], bad)
        bad = views(tok, ("players", "handpay"), (403, "not authorized"))
        check(f"{who}: the players and handpay views are 403", bad == [], bad)
        check(f"{who}: nothing changed", books(eng) == before)
        st, r = eng.glass_data(tok, "cashout")
        check(f"{who}: their own cash-out figures still answer",
              st == 200 and r.get("ok") is True
              and r.get("walletMc") == cash(eng, sam if egm == "M_PLAYER"
                                            else pia), (st, r))
        everyone(who, tok)

    print("— admin menus: an admin carded in normally")
    for who, egm in (("an admin with a player fob", "M_ADMIN"),
                     ("an attendant fob with no account", "M_ATT")):
        tok = tok_of(eng, egm)
        before = books(eng)
        bad = refused(tok, SVC)
        check(f"{who}: the service actions are 403 not authorized (they "
              "are for a tap-over)", bad == [], bad)
        check(f"{who}: ...and nothing changed", books(eng) == before)
        got = {n: adm(eng, tok, n) for n in PL}
        check(f"{who}: every players action is let in (none answers 401 "
              "or not authorized)",
              all(st in (200, 400, 403) and r.get("error") != "not authorized"
                  and r.get("error") != "unknown session"
                  for st, r in got.values())
              and all(got[n][0] != 403 for n in PL),
              {n: (st, r.get("error")) for n, (st, r) in got.items()})
        check(f"{who}: a players action with nothing in it is refused in "
              "words and changes nothing",
              got["plFund"] == (200, {"ok": False, "error": GONE,
                                      "action": "plFund", "egmId": egm})
              and got["plCreate"][1].get("error") == "Type a name."
              and got["plAddFob"][1].get("error") == GONE
              and got["plAddFobCancel"] == (200, {
                  "ok": True, "fobWait": None, "action": "plAddFobCancel",
                  "egmId": egm})
              and books(eng) == before and not eng._fob_waits,
              (got["plFund"], got["plCreate"], got["plAddFob"],
               got["plAddFobCancel"]))
        st, r = act(eng, tok, "clearHandpay")
        check(f"{who}: clearHandpay is let in (nothing is pending here)",
              st == 200 and r.get("ok") is False
              and r.get("error") == "no pending handpay", (st, r))
        st, r = eng.glass_data(tok, "players")
        check(f"{who}: the players view answers",
              st == 200 and r.get("ok") is True
              and [p["name"] for p in r["players"]] == [
                  "AJ", "Christopher", "Dana", "Pia", "Sam", "Vic"], (st, r))
        st, r = eng.glass_data(tok, "handpay")
        check(f"{who}: the handpay view answers",
              st == 200 and r.get("pending") == [], (st, r))
        st, r = eng.glass_data(tok, "cashout")
        check(f"{who}: their own cash-out figures still answer",
              st == 200 and r.get("ok") is True
              and r.get("walletMc") == (cash(eng, aj) if egm == "M_ADMIN"
                                        else None), (st, r))
        everyone(who, tok)

    print("— admin menus: an admin on a tap-over")
    tok = tok_of(eng, "M_TAP")
    who = "a tap-over"
    before = books(eng)
    bad = refused(tok, PL)
    check(f"{who}: every players action is 403 not authorized", bad == [],
          bad)
    bad = refused(tok, WALLET, want=(
        403, "wallet actions are disabled while supervising another player"))
    check(f"{who}: the three wallet actions are still 403", bad == [], bad)
    bad = views(tok, ("players", "cashout"), (403, "not authorized"))
    check(f"{who}: the players view and the cash-out view are 403",
          bad == [], bad)
    st, r = eng.glass_data(tok, "cashout")
    check(f"{who}: the refused cash-out view holds no wallet figure",
          not any(k in r for k in ("walletMc", "walletCredits",
                                   "walletRestrictedMc", "machineCreditsMc")),
          r)
    check(f"{who}: nothing changed", books(eng) == before)
    got = {n: adm(eng, tok, n) for n in SVC}
    check(f"{who}: the service actions are let in",
          got["svcFund"] == (200, {"ok": False, "error": "Enter an amount.",
                                   "action": "svcFund", "egmId": "M_TAP"})
          and got["svcSetPin"][1].get("error") == "A PIN is digits only.",
          got)
    st, r = act(eng, tok, "clearHandpay")
    check(f"{who}: clearHandpay is let in",
          st == 200 and r.get("error") == "no pending handpay", (st, r))
    st, r = eng.glass_data(tok, "handpay")
    check(f"{who}: the handpay view answers",
          st == 200 and r.get("pending") == [], (st, r))
    everyone(who, tok)

    print("— admin menus: HANDPAY lists every machine")
    t_admin, t_tap, t_player = (tok_of(eng, "M_ADMIN"), tok_of(eng, "M_TAP"),
                                tok_of(eng, "M_PLAYER"))
    st, r = eng.glass_data(t_admin, "handpays")
    check("an admin: the every-machine view answers, empty",
          st == 200 and r.get("ok") is True and r.get("rows") == [], (st, r))
    check("a tap-over and a player are refused the every-machine view",
          eng.glass_data(t_tap, "handpays")[0] == 403
          and eng.glass_data(t_player, "handpays")[0] == 403)
    HP = {"transactionId": "77", "deviceId": "1",
          "handpayType": "G2S_gameWin", "pendingCashableAmt": "125000000",
          "pendingPromoAmt": "0", "pendingNonCashAmt": "0",
          "handpayDateTime": "2026-11-18T03:00:00Z"}
    eng.associations["M_PLAYER"].handpays_pending = {"77": dict(HP)}
    eng.associations["M_ADMIN"].handpays_pending = {
        "5": dict(HP, transactionId="5", pendingCashableAmt="20000000")}
    st, r = eng.glass_data(t_admin, "handpays")
    got = r.get("rows") or []
    seated = eng.card_sessions_snapshot().get("M_PLAYER", {}).get("name")
    check("one card per machine with a handpay, this machine first, each "
          "with its machine, who is carded in there and the amount",
          st == 200 and [(x["key"], x["here"], x["amount"]) for x in got] == [
              ("M_ADMIN", True, "$200.00"), ("M_PLAYER", False, "$1,250.00")]
          and got[1]["player"] == seated and bool(seated)
          and got[1]["machine"] == eng._glass_machine_name("M_PLAYER")
          and all(set(x) == {"key", "here", "machine", "player", "amount",
                             "type", "canClear", "toCredits", "more"}
                  for x in got)
          and all(x["toCredits"] is True for x in got),
          (st, r))
    eng.associations["M_ADMIN"].handpays_pending = {
        "5": dict(HP, transactionId="5", remoteCredit="false",
                  pendingCashableAmt="1267200000000")}
    big = (eng.glass_data(t_admin, "handpays")[1].get("rows") or [{}])[0]
    check("a handpay the machine will not put on the credit meter says so "
          "(the reset pays it to a wallet instead)",
          big.get("key") == "M_ADMIN" and big.get("toCredits") is False
          and big.get("amount") == "$12,672,000.00", big)
    eng.associations["M_ADMIN"].handpays_pending = {}
    st, r = eng.glass_data(t_admin, "handpays")
    check("a handpay that is gone is gone from the next read",
          [x["key"] for x in r.get("rows") or []] == ["M_PLAYER"], r)
    before = books(eng)
    st, r = act(eng, t_tap, "clearHandpay", machine="M_PLAYER")
    check("a tap-over cannot reset another machine's handpay",
          (st, r.get("error")) == (403, "not authorized"), (st, r))
    st, r = act(eng, t_player, "clearHandpay", machine="M_ADMIN")
    check("a player cannot either", st == 403, (st, r))
    st, r = act(eng, t_admin, "clearHandpay", machine="NOPE")
    check("an admin naming a machine the hub does not know is refused",
          (st, r.get("error")) == (404, "machine not known"), (st, r))
    st, r = act(eng, t_admin, "clearHandpay", machine="M_PLAYER")
    check("an admin's reset goes to the machine that was tapped",
          st == 200 and r.get("egmId") == "M_PLAYER"
          and r.get("ok") is not False, (st, r))
    st, r = act(eng, t_admin, "clearHandpay", machine="M_ADMIN")
    check("naming this machine is the same as naming none",
          st == 200 and r.get("egmId") == "M_ADMIN"
          and r.get("error") == "no pending handpay", (st, r))
    check("none of that moved any money", books(eng) == before)
    eng.associations["M_PLAYER"].handpays_pending = {}

    print("— the Activity feed says how a handpay ended")
    a_ = eng.associations["M_PLAYER"]
    if not hasattr(a_, "handpays"):
        a_.handpays = []
    a_.handpays_pending = {"9": dict(HP, transactionId="9")}
    ex = {"transactionId": "9"}
    lab = eng._handpay_moment(a_, "handpay_pending", ex)
    check("a pending handpay: the plain label stays, the amount is added",
          lab is None and ex.get("amt") == 125000000, (lab, ex))
    a_.handpays_pending = {}
    want = {"G2S_remoteCredit": "🎉 Handpay reset to credits",
            "G2S_localHandpay": "🎉 Handpay paid by hand · attendant key",
            "G2S_remoteHandpay": "🎉 Handpay paid by hand · reset from the hub",
            "G2S_localCredit": "🎉 Handpay reset to credits · attendant key"}
    got = {}
    for n, (kt, _w) in enumerate(sorted(want.items())):
        a_.handpays.append(dict(HP, transactionId=str(100 + n),
                                state="keyedOff", keyOffType=kt,
                                keyOffCashableAmt=6000000000))
        ex = {"transactionId": str(100 + n)}
        got[kt] = (eng._handpay_moment(a_, "handpay_keyed_off", ex),
                   ex.get("amt"))
    check("each ending reads as what it was, with the amount keyed off",
          got == {k: (v, 6000000000) for k, v in want.items()}, got)
    ex = {"transactionId": "nope"}
    check("an ending with no record keeps the event's plain label",
          eng._handpay_moment(a_, "handpay_keyed_off", ex) is None
          and "amt" not in ex
          and eng._handpay_moment(a_, "handpay_keyed_off", {}) is None)

    print("— MY WALLET opens on the player's last amount")
    st, r = eng.glass_data(t_admin, "cashout")
    check("nothing remembered yet: the wallet view sends no last amount",
          st == 200 and "lastAddCents" not in r, r)
    for junk in (0, -5, True, "700", 7.5, None):
        eng.account_store.set_last_add(aj, junk)
    eng.account_store.set_last_add("house", 700)
    eng.account_store.set_last_add("nobody", 700)
    check("junk amounts, the house and unknown ids are ignored",
          "lastAddCents" not in eng.glass_data(t_admin, "cashout")[1]
          and "lastAddCents" not in (eng.account_store.get("house") or {}))
    money = (cash(eng, aj), free(eng, aj), len(rows(eng)))
    eng.account_store.set_last_add(aj, 1234)
    check("a remembered amount rides the wallet view and touches no money",
          eng.glass_data(t_admin, "cashout")[1].get("lastAddCents") == 1234
          and (cash(eng, aj), free(eng, aj), len(rows(eng))) == money)

    print("— admin menus: that admin after their admin flag is cleared, "
          "same token")
    eng.account_store.set_admin(dana, False)
    who = "a tap-over whose admin lost the right"
    before = books(eng)
    bad = refused(tok, SVC + PL + ("clearHandpay",))
    check(f"{who}: every service action, every players action and "
          "clearHandpay is 403 not authorized", bad == [], bad)
    bad = views(tok, ("players", "handpay", "cashout"),
                (403, "not authorized"))
    check(f"{who}: the players, handpay and cash-out views are 403",
          bad == [], bad)
    bad = refused(tok, WALLET, want=(
        403, "wallet actions are disabled while supervising another player"))
    check(f"{who}: the wallet actions are still 403", bad == [], bad)
    check(f"{who}: nothing changed", books(eng) == before)
    everyone(who, tok)
    check("in all of that, nothing started a rescan of a machine's games",
          rescans == [])

    print("— admin menus: an admin logged in by PIN, once admin by PIN is "
          "switched on")
    tok = tok_of(eng, "M_PIN")
    eng.hub_store.set_host_setting("pin_admin_allowed", "1")
    st, r = eng.glass_data(tok, "players")
    check("the same PIN session is an admin carded in normally: the "
          "players view answers, the service actions do not",
          st == 200 and r.get("ok") is True
          and refused(tok, SVC) == []
          and adm(eng, tok, "plFund")[1].get("error") == GONE, (st, r))


def admin_menu_service():
    print("— service screen: a tap-over reaches the friend and nobody else")
    eng = menu_engine("M1", "M2")
    eng.hub_store.set_nickname("M1", "Bluebird 2", protocol="g2s")
    chris = player(eng, "Christopher", "C4150001", cents=1000)
    vic = player(eng, "Vic", "B1C00001", cents=10000)
    player(eng, "Dana", "DA4A0001", admin=True)
    tap_at(eng, "M1", "C4150001")
    tap_at(eng, "M1", "DA4A0001")
    tok = tok_of(eng, "M1")
    n0 = len(rows(eng))
    house0 = cash(eng, "house")
    st, r = adm(eng, tok, "svcFund", cents=2000, rid=rid(), accountId=vic,
                egmId="M2")
    check("svcFund naming another player and another machine in its body "
          "credits the friend at THIS machine",
          st == 200 and r.get("ok") is True and r.get("egmId") == "M1"
          and r.get("msg") == "Added $20.00 to Christopher's wallet."
          and cash(eng, chris) == 3000000 and cash(eng, vic) == 10000000,
          (st, r))
    new = rows(eng)[n0:]
    by = " — by Dana at Bluebird 2 (service)"
    check("one svcFund of 2000 is the friend +2000 and the House -2000, "
          "two ledger rows under one ref",
          cash(eng, "house") == house0 - 2000000
          and [(e["accountId"], e["deltaMillicents"]) for e in new]
          == [(chris, 2000000), ("house", -2000000)]
          and new[0]["ref"] == new[1]["ref"] and bool(new[0]["ref"]), new)
    check("both notes say which admin did it, at which machine, from the "
          "service screen",
          [e["note"] for e in new] == ["house fund" + by,
                                       f"fund {chris}" + by], new)
    check("the reply carries the friend's new figures for the screen",
          r.get("service") == dict(
              eng.glass_state("M1").get("service") or {"none": 1})
          and (r.get("service") or {}).get("walletMc") == 3000000
          and (r.get("service") or {}).get("wallet") == "$30.00"
          and (r.get("service") or {}).get("name") == "Christopher", r)
    st, r = adm(eng, tok, "svcSetPin", pin="482913", accountId=vic,
                egmId="M2")
    check("svcSetPin naming another player sets the FRIEND's PIN",
          st == 200 and r.get("ok") is True
          and r.get("msg") == "Christopher's PIN is set."
          and eng.account_store.get(chris)["hasPin"] is True
          and eng.account_store.get(vic)["hasPin"] is False
          and (r.get("service") or {}).get("hasPin") is True, (st, r))
    check("...and the PIN is not in the reply", "482913" not in json.dumps(r))

    print("— service screen: who the friend is, is read with the lock held")
    # A card switch the instant after the tap-over was proven must not hand
    # the action the next player: the friend is the copy made in that hold.
    real_admin = eng._session_is_admin

    def switch_then_answer(uid, tier):
        with eng.companion_lock:
            eng.card_sessions["M1"] = dict(eng.card_sessions["M1"],
                                           uid="B1C00001", name="Vic",
                                           accountId=vic)
        return real_admin(uid, tier)
    eng._session_is_admin = switch_then_answer
    st, r = adm(eng, tok, "svcFund", cents=700, rid=rid())
    eng._session_is_admin = real_admin
    check("a card session that changes right after the check does not "
          "redirect the money",
          st == 200 and r.get("ok") is True
          and cash(eng, chris) == 3700000 and cash(eng, vic) == 10000000,
          (st, r, cash(eng, chris), cash(eng, vic)))
    with eng.companion_lock:
        eng.card_sessions["M1"].update(uid="C4150001", name="Christopher",
                                       accountId=chris)
    lock = eng.companion_lock = WatchedLock()
    held = []
    real_live = eng.glass_sessions.still_live
    eng.glass_sessions.still_live = \
        lambda egm, t: held.append(lock.held) or real_live(egm, t)
    adm(eng, tok, "svcFund")
    eng.glass_data(tok, "handpay")
    eng.glass_sessions.still_live = real_live
    eng.companion_lock = threading.Lock()
    check("the token is tested for being the machine's live one in the "
          "same lock hold that reads the card session",
          held == [True, True], held)

    print("— service screen: the friend's figures ride the state poll")
    a1 = eng.associations["M1"]
    a1.meters = {"G2S_cabinet/1/G2S_playerCashableAmt": "4750000000"}
    a1.handpays_pending = {"77": {
        "transactionId": "77", "deviceId": "1", "handpayType": "G2S_gameWin",
        "pendingCashableAmt": "125000000", "pendingPromoAmt": "0",
        "pendingNonCashAmt": "0", "handpayDateTime": "2026-11-18T03:00:00Z"}}
    eng.account_store.adjust(chris, d_cash=107886070000 - cash(eng, chris),
                             d_non=15100000, note="seed")
    st_ = eng.glass_state("M1")
    check("the service object: name, wallet, Free Slot Play, PIN, machine "
          "credits, the pending handpay and the most one action can add",
          st_.get("service") == {
              "name": "Christopher", "hasWallet": True,
              "walletMc": 107886070000, "wallet": "$1,078,860.70",
              "freePlayMc": 15100000, "freePlay": "$151.00",
              "hasPin": True,
              "machineMc": 4750000000, "machine": "$47,500.00",
              "handpay": {"pending": True, "amount": "$1,250.00",
                          "type": "G2S_gameWin", "canClear": True,
                          "more": 0, "toCredits": True},
              "maxCents": 9999999999,
              "quickCents": [2000, 5000, 10000]}, st_.get("service"))
    check("the admin's identity and the tap-over flags are as before",
          st_.get("overlay") is True and st_.get("admin") is True
          and st_.get("name") == "Dana" and st_.get("sess") == tok
          and st_.get("supervising") == "Christopher")
    check("the friend's money is still NOT in the top-level fields (an old "
          "page must not paint it as the admin's own)",
          not any(k in st_ for k in ("credits", "creditsMc",
                                     "restrictedMc")), sorted(st_))
    a1.handpays_pending = {}
    check("no handpay pending: the handpay part says so and nothing else",
          eng.glass_state("M1")["service"]["handpay"] == {"pending": False})
    eng.account_store.adjust(chris, d_non=-15100000, note="seed")
    check("Free Slot Play is sent even when it is 0",
          eng.glass_state("M1")["service"].get("freePlayMc") == 0
          and eng.glass_state("M1")["service"].get("freePlay") == "$0.00")
    real_block = eng._glass_service_block
    eng._glass_service_block = lambda *a: (_ for _ in ()).throw(
        RuntimeError("boom"))
    try:
        st_ = eng.glass_state("M1")
    except RuntimeError as e:
        st_ = {"the poll raised": str(e)}
    eng._glass_service_block = real_block
    check("a fault building the service object leaves it out and the poll "
          "still answers",
          st_.get("ok") is True and st_.get("overlay") is True
          and st_.get("sess") == tok and "service" not in st_, st_)

    print("— service screen: the wallet shown is the wallet that is funded")
    eng.hub_store.fob_set("C4150001", account_id=vic)   # re-linked mid-session
    blk = eng.glass_state("M1")["service"]
    check("after the friend's fob is linked to another player, the screen "
          "shows that player's wallet",
          blk.get("name") == "Vic" and blk.get("walletMc") == 10000000, blk)
    c0 = cash(eng, chris)
    st, r = adm(eng, tok, "svcFund", cents=300, rid=rid())
    check("...and svcFund credits the same one",
          r.get("ok") is True and cash(eng, vic) == 10300000
          and cash(eng, chris) == c0
          and (r.get("service") or {}).get("walletMc") == 10300000
          and r.get("msg") == "Added $3.00 to Vic's wallet.", r)
    eng.hub_store.fob_set("C4150001", account_id=chris)

    print("— service screen: DONE, and a new friend")
    st, r = act(eng, tok, "logout")
    check("DONE ends the tap-over and the friend stays carded in",
          st == 200 and r.get("overlayEnded") is True
          and eng.admin_overlays == {}
          and (eng.card_sessions.get("M1") or {}).get("uid") == "C4150001",
          (st, r))
    st, r = adm(eng, tok, "svcFund", cents=500, rid=rid())
    check("the tap-over's token is 401 after DONE",
          (st, r.get("error")) == (401, "unknown session"), (st, r))
    friend_tok = tok_of(eng, "M1")
    st, r = eng.glass_data(friend_tok, "cashout")
    check("the friend's own token reads their cash-out figures again",
          st == 200 and r.get("walletMc") == cash(eng, chris), (st, r))
    check("no service object once the tap-over is over",
          "service" not in eng.glass_state("M1")
          and eng.glass_state("M1").get("creditsMc") == cash(eng, chris))
    tap_at(eng, "M1", "DA4A0001")               # Dana taps over Christopher
    first = tok_of(eng, "M1")
    tap_at(eng, "M1", "DA4A0001")               # any tap ends the tap-over
    tap_at(eng, "M1", "C4150001")               # Christopher cards out
    tap_at(eng, "M1", "B1C00001")               # Vic cards in
    tap_at(eng, "M1", "DA4A0001")               # Dana taps over Vic
    v0 = cash(eng, vic)
    st, r = adm(eng, first, "svcFund", cents=500, rid=rid())
    check("a new friend under a new tap-over: the first tap-over's token "
          "is 401 and the new friend is untouched",
          (st, r.get("error")) == (401, "unknown session")
          and cash(eng, vic) == v0
          and (eng.card_sessions.get("M1") or {}).get("uid") == "B1C00001",
          (st, r))
    st, r = adm(eng, tok_of(eng, "M1"), "svcFund", cents=500, rid=rid())
    check("the new tap-over's token credits the new friend",
          r.get("ok") is True and cash(eng, vic) == v0 + 500000, r)

    print("— service screen: a friend whose fob has no player")
    eng.hub_store.fob_set("0F0B0001", tier="player", label="spare fob")
    tap_at(eng, "M2", "0F0B0001")
    tap_at(eng, "M2", "DA4A0001")
    tok2 = tok_of(eng, "M2")
    n0 = len(rows(eng))
    blk = eng.glass_state("M2").get("service") or {}
    check("the service object says there is no wallet, and holds no "
          "wallet field",
          blk.get("hasWallet") is False and blk.get("name") == "spare fob"
          and not any(k in blk for k in ("walletMc", "wallet", "freePlayMc",
                                         "freePlay", "hasPin"))
          and blk.get("maxCents") == 9999999999 and "machineMc" in blk, blk)
    st, r = adm(eng, tok2, "svcFund", cents=500, rid=rid())
    st2, r2 = adm(eng, tok2, "svcSetPin", pin="482913")
    check("both service actions say the fob is not linked to a player, "
          "and no ledger row is written",
          (st, r.get("error")) == (200, "This fob is not linked to a player.")
          and (st2, r2.get("error"))
          == (200, "This fob is not linked to a player.")
          and r.get("ok") is False and len(rows(eng)) == n0, (r, r2))

    print("— service screen: the two moments a tap-over token has no "
          "tap-over on record")
    # _admin_overlay_push mints the admin's token BEFORE it records the
    # tap-over, and _admin_overlay_pop drops the record BEFORE the friend's
    # token is minted again. In both, the token must not pass as an admin
    # carded in normally.
    eng3 = menu_engine("M1")
    sam = player(eng3, "Sam", "51A00001", cents=5000)
    vic3 = player(eng3, "Vic", "B1C00001", cents=10000)
    player(eng3, "Dana", "DA4A0001", admin=True)
    eng3.account_store.set_pin(vic3, "990011")
    tap_at(eng3, "M1", "51A00001")

    def gap_bodies():
        return {"plFund": {"accountId": vic3, "cents": 500, "rid": rid()},
                "plTake": {"accountId": vic3, "cents": 500, "rid": rid()},
                "plSetPin": {"accountId": vic3, "pin": "482913"},
                "plClearPin": {"accountId": vic3},
                "plRename": {"accountId": vic3, "name": "Changed"},
                "plCreate": {"name": "Intruder", "rid": rid()},
                "plAddFob": {"accountId": vic3},
                "plAddFobCancel": {},
                "plRemoveFob": {"accountId": vic3, "fob": "C00001"},
                "svcFund": {"cents": 500, "rid": rid()},
                "svcSetPin": {"pin": "482913"}}

    def gap(label, tok):
        before = books(eng3)
        got = {n: adm(eng3, tok, n, **b) for n, b in gap_bodies().items()}
        check(f"{label}: every players action and every service action is "
              "403 not authorized",
              all((st, r.get("error")) == (403, "not authorized")
                  for st, r in got.values()),
              {n: (st, r.get("error")) for n, (st, r) in got.items()})
        st, r = eng3.glass_data(tok, "players")
        st2, r2 = act(eng3, tok, "clearHandpay")
        check(f"{label}: the players view and clearHandpay are 403 too",
              (st, r.get("error")) == (403, "not authorized")
              and "players" not in r
              and (st2, r2.get("error")) == (403, "not authorized"),
              (st, r, st2, r2))
        check(f"{label}: nothing changed", books(eng3) == before)

    minted = eng3.glass_sessions.mint("M1", "player", "DA4A0001", "Dana")
    gap("the admin's token minted over a friend, the tap-over not recorded "
        "yet", minted)
    eng3._admin_overlay_push("M1", {"companionId": "companion-M1"},
                             "DA4A0001", "player", "Dana", "1")
    tok3 = tok_of(eng3, "M1")
    check("(the same token is a tap-over once the record is there)",
          adm(eng3, tok3, "svcFund")[1].get("error") == "Enter an amount.")
    with eng3.companion_lock:
        eng3.admin_overlays.pop("M1")
    gap("the tap-over's record dropped, its token still live", tok3)

    eng3._admin_overlay_push("M1", {"companionId": "companion-M1"},
                             "DA4A0001", "player", "Dana", "1")
    aj3 = player(eng3, "AJ", "A1A00001", admin=True)
    other = eng3.glass_sessions.mint("M1", "player", "A1A00001", "AJ")
    gap("a tap-over on record that names another token than this admin's",
        other)
    check("(AJ is an admin, and that token is the machine's live one)",
          eng3.account_store.get(aj3)["admin"] is True
          and tok_of(eng3, "M1") == other)
    with eng3.companion_lock:
        eng3.admin_overlays.pop("M1")

    print("— service screen: an admin who taps over their own card session")
    # Sam carded in as a player and was made an admin afterwards; his own
    # fob then raises a tap-over over himself. It is still a tap-over: the
    # players menu and his own cash-out figures stay shut.
    eng3.glass_sessions.mint("M1", "player", "51A00001", "Sam")
    eng3.account_store.set_admin(sam, True)
    tap_at(eng3, "M1", "51A00001")
    own_tok = tok_of(eng3, "M1")
    before = books(eng3)
    got = {n: adm(eng3, own_tok, n, **b) for n, b in gap_bodies().items()
           if n in PL}
    st, r = eng3.glass_data(own_tok, "cashout")
    st2, r2 = eng3.glass_data(own_tok, "players")
    check("the players actions, the players view and the cash-out view "
          "are 403, and nothing changed",
          (eng3.admin_overlays.get("M1") or {}).get("uid") == "51A00001"
          and all((s, x.get("error")) == (403, "not authorized")
                  for s, x in got.values())
          and (st, r.get("error")) == (403, "not authorized")
          and "walletMc" not in r
          and (st2, r2.get("error")) == (403, "not authorized")
          and books(eng3) == before, (got, r, r2))
    got = [adm(eng3, own_tok, "svcFund", cents=5000, rid=rid()),
           adm(eng3, own_tok, "svcFund", cents=5000, restricted=True,
               rid=rid())]
    check("ADD TO WALLET and ADD FREE SLOT PLAY there are refused in "
          "words: a tap-over does not pay the admin who raised it, and "
          "nothing is booked",
          all((s, x.get("ok"), x.get("error")) == (200, False, OWN_WALLET)
              for s, x in got) and books(eng3) == before, got)
    check("SET PIN is still let in there",
          adm(eng3, own_tok, "svcSetPin")[1].get("error")
          == "A PIN is digits only.")
    eng3.account_store.set_admin(sam, False)
    eng3.glass_state("M1")              # the poll hands the screen back

    print("— service screen: an admin logged in by PIN who taps their own "
          "fob")
    # With admin by PIN off (the default) an admin's PIN login is a plain
    # player's session, so their own fob raises a tap-over over it.
    eng5 = menu_engine("M1", "M2")
    aj5 = player(eng5, "AJ", "A1A00001", admin=True, cents=100)
    chris5 = player(eng5, "Christopher", "C4150001", cents=1000)
    eng5.account_store.set_pin(aj5, "424242")
    r = eng5.pin_login("M1", "424242")
    kind0 = eng5._glass_caller(tok_of(eng5, "M1"))["kind"]
    tap_at(eng5, "M1", "A1A00001")
    own5 = tok_of(eng5, "M1")
    check("the PIN login is a player's session, and the fob raises a "
          "tap-over over it",
          r.get("ok") is True and kind0 == "player"
          and (eng5.admin_overlays.get("M1") or {}).get("uid") == "A1A00001"
          and eng5._glass_caller(own5)["kind"] == "service"
          and eng5.glass_state("M1")["service"].get("name") == "AJ",
          (r, kind0, eng5._glass_caller(own5)["kind"]))
    before = books(eng5)
    got = [adm(eng5, own5, "svcFund", cents=10000000, rid=rid()),
           adm(eng5, own5, "svcFund", cents=5000, restricted=True,
               rid=rid())]
    check("ADD TO WALLET and ADD FREE SLOT PLAY from that screen are "
          "refused in the same words, and nothing is booked",
          all((s, x.get("ok"), x.get("error")) == (200, False, OWN_WALLET)
              for s, x in got) and books(eng5) == before, got)
    tap_at(eng5, "M2", "C4150001")
    tap_at(eng5, "M2", "A1A00001")              # AJ taps over Christopher
    st, r = adm(eng5, tok_of(eng5, "M2"), "svcFund", cents=5000, rid=rid())
    check("the same admin tapping over a friend still adds to the "
          "friend's wallet",
          st == 200 and r.get("ok") is True
          and r.get("msg") == "Added $50.00 to Christopher's wallet."
          and cash(eng5, chris5) == 6000000 and cash(eng5, aj5) == 100000,
          (st, r))

    print("— service screen: a token that is no longer the machine's own")
    eng3._admin_overlay_push("M1", {"companionId": "companion-M1"},
                             "DA4A0001", "player", "Dana", "1")
    tok4 = tok_of(eng3, "M1")
    store = eng3.glass_sessions
    store._by_egm["M1"] = "gs_" + "1" * 24      # another token holds the slot
    before = books(eng3)
    st, r = adm(eng3, tok4, "svcFund", cents=500, rid=rid())
    st2, r2 = eng3.glass_data(tok4, "handpay")
    st3, r3 = act(eng3, tok4, "clearHandpay")
    check("a token that still resolves but is not the machine's live one "
          "is 401 for an action, a data view and clearHandpay",
          store.resolve(tok4) is not None
          and (st, r.get("error")) == (401, "unknown session")
          and (st2, r2.get("error")) == (401, "unknown session")
          and (st3, r3.get("error")) == (401, "unknown session")
          and books(eng3) == before, (st, r, st2, r2, st3, r3))
    store._by_egm["M1"] = tok4

    print("— service screen: the 120 second hand-back")
    idle = lambda: store.idle_sec("M1", tok4)              # noqa: E731
    store._by_tok[tok4]["lastSeen"] = time.time() - 100
    for _ in range(10):
        eng3.glass_state("M1")
    check("ten state polls do not move the tap-over's idle clock",
          idle() >= 100, idle())
    adm(eng3, tok4, "svcFund")                  # refused, but it is an action
    check("a service action does move it", idle() < 5, idle())
    eng3.sweep_admin_overlays(now=time.time() + 100)
    check("...so the sweep leaves a tap-over that was used 100 s ago",
          (eng3.admin_overlays.get("M1") or {}).get("tok") == tok4)
    store._by_tok[tok4]["lastSeen"] = time.time() - 100
    for _ in range(10):
        eng3.glass_state("M1")
    eng3.sweep_admin_overlays(now=time.time() + 25)
    check("...and still hands the screen back at 120 s when only the poll "
          "touched it",
          eng3.admin_overlays == {}
          and (eng3.card_sessions.get("M1") or {}).get("uid") == "51A00001")

    print("— service screen: the handpay shown is the one CLEAR resets")
    eng4 = menu_engine("M1", "M2")
    player(eng4, "Sam", "51A00001")
    player(eng4, "Dana", "DA4A0001", admin=True)
    tap_at(eng4, "M1", "51A00001")
    tap_at(eng4, "M1", "DA4A0001")
    tok5 = tok_of(eng4, "M1")
    a = eng4.associations["M1"]

    def lockup(txn, cashable=0, non=0):
        return {"transactionId": txn, "deviceId": "1",
                "handpayType": "G2S_gameWin", "remoteCredit": "true",
                "pendingCashableAmt": str(cashable), "pendingPromoAmt": "0",
                "pendingNonCashAmt": str(non),
                "handpayDateTime": "2026-11-18T03:00:0%sZ" % txn[-1]}

    def cleared():
        eng4.sent.clear()
        st, r = act(eng4, tok5, "clearHandpay")
        return r.get("hpId"), attr(eng4.sent[-1][0], "transactionId") \
            if eng4.sent else None

    a.handpays_pending = {"11": lockup("11", cashable=125000000),
                          "12": lockup("12", non=500000)}
    hp = eng4.glass_state("M1")["service"]["handpay"]
    check("one handpay in two records (cashable, then one that is not): "
          "the cashable amount is shown, with 1 more waiting",
          hp == {"pending": True, "amount": "$1,250.00",
                 "type": "G2S_gameWin", "canClear": True, "more": 1,
                 "toCredits": True}, hp)
    check("...and that is the record clearHandpay resets",
          cleared() == ("11", "11"))
    a.handpays_pending = {"21": lockup("21", cashable=1000000),
                          "22": lockup("22", cashable=2000000)}
    hp = eng4.glass_state("M1")["service"]["handpay"]
    listed = eng4.glass_data(tok5, "handpay")[1].get("pending") or []
    check("two handpays of the same kind: the list is oldest first, the "
          "service screen shows the newer one, which is the one CLEAR "
          "resets",
          [x["amount"] for x in listed] == ["$10.00", "$20.00"]
          and hp.get("amount") == "$20.00" and hp.get("more") == 1
          and cleared() == ("22", "22"), (listed, hp))
    a.comms_state = "offline"
    check("a machine that is not online: the handpay is shown and cannot "
          "be cleared from the screen",
          eng4.glass_state("M1")["service"]["handpay"].get("canClear")
          is False)
    a.comms_state = "onLine"
    a.handpays_pending = {}
    eng4.sas_links["M1"] = "smib-bb2/1"
    eng4.sas_machines["smib-bb2/1"] = {
        "smibId": "smib-bb2", "online": True, "receivedAt": time.time(),
        "meters": {"0x1A": 4750000},
        "pendingHandpay": {"amountMc": 125000000, "kind": "win",
                           "since": "2026-11-18T03:00:00Z"}}
    blk = eng4.glass_state("M1")["service"]
    check("a machine with a SAS side: its one handpay, by its SAS name, "
          "0 more; machine credits off the SAS meter",
          blk["handpay"] == {"pending": True, "amount": "$1,250.00",
                             "type": "Game win", "canClear": True, "more": 0,
                             "toCredits": True}
          and blk.get("machine") == "$47,500.00", blk)
    eng4.sas_machines["smib-bb2/1"]["pendingHandpay"] = {"kind": "win"}
    check("a handpay with no amount known reads as the word Handpay",
          eng4.glass_state("M1")["service"]["handpay"].get("amount")
          == "Handpay")
    st, r = act(eng4, tok5, "clearHandpay")
    q = list(eng4.sas_commands.get("smib-bb2") or [])
    check("clearHandpay from the service screen goes over SAS on that "
          "machine",
          st == 200 and r.get("ok") is True and r.get("via") == "sas"
          and len(q) == 1 and q[0].get("type") == "handpay_reset", (st, r, q))


def admin_menu_money():
    print("— adding and taking back: one path, booked once")
    eng = menu_engine("M1", "M2")
    eng.hub_store.set_nickname("M1", "Bluebird 2", protocol="g2s")
    chris = player(eng, "Christopher", "C4150001", cents=1000)
    sam = player(eng, "Sam", "51A00001", cents=5000)
    aj = player(eng, "AJ", "A1A00001", admin=True)
    player(eng, "Dana", "DA4A0001", admin=True)
    tap_at(eng, "M1", "C4150001")
    tap_at(eng, "M1", "DA4A0001")               # M1: Dana over Christopher
    tap_at(eng, "M2", "A1A00001")               # M2: AJ carded in normally
    svc, adm_tok = tok_of(eng, "M1"), tok_of(eng, "M2")
    by_svc = " — by Dana at Bluebird 2 (service)"
    by_adm = " — by AJ at M2 (admin menu)"

    n0 = len(rows(eng))
    c0, f0 = cash(eng, chris), free(eng, chris)
    eng.hub_store.set_host_setting("house_allow_negative", "off")
    st, r = adm(eng, svc, "svcFund", cents=1500, restricted=True, rid=rid())
    new = rows(eng)[n0:]
    check("ADD FREE SLOT PLAY moves the Free Slot Play balance, not the "
          "cash wallet",
          st == 200 and r.get("ok") is True
          and r.get("msg") == "Added $15.00 Free Slot Play for Christopher."
          and free(eng, chris) == f0 + 1500000 and cash(eng, chris) == c0
          and (r.get("service") or {}).get("freePlayMc") == f0 + 1500000,
          (st, r))
    check("...its ledger notes say restricted promotional and who did it",
          [e["note"] for e in new]
          == ["house fund (restricted promotional)" + by_svc,
              f"fund {chris} (restricted promotional)" + by_svc]
          and all(e["kind"] == "nonCash" for e in new), new)
    check("...and it is not refused when the House may not go negative "
          "(the House has no cash here)", cash(eng, "house") <= 0)

    print("— adding and taking back: the House is out of credits")
    n0 = len(rows(eng))
    short = rid()
    st, r = adm(eng, svc, "svcFund", cents=2000, rid=short)
    check("cash from a House that may not go negative and is short: the "
          "screen's own sentence, and no ledger row",
          st == 200 and r.get("ok") is False and r.get("error")
          == ("The House is out of credits. Top up the House bankroll on "
              "the hub's Options page.")
          and len(rows(eng)) == n0 and cash(eng, chris) == c0, (st, r))
    eng.hub_store.set_host_setting("house_allow_negative", "on")
    st, r = adm(eng, svc, "svcFund", cents=2000, rid=short)
    check("a refused request, then the same request id once the cause is "
          "fixed: it goes through",
          st == 200 and r.get("ok") is True and "repeat" not in r
          and cash(eng, chris) == c0 + 2000000
          and len(rows(eng)) == n0 + 2, (st, r))

    print("— adding and taking back: a second CONFIRM cannot pay twice")
    st2, r2 = adm(eng, svc, "svcFund", cents=2000, rid=short)
    check("the same request id again: the same reply, marked repeat, and "
          "still two ledger rows",
          st2 == 200 and r2.get("repeat") is True
          and {k: v for k, v in r2.items() if k != "repeat"} == r
          and len(rows(eng)) == n0 + 2
          and cash(eng, chris) == c0 + 2000000, (st2, r2))
    st, r = adm(eng, svc, "svcFund", cents=2000, rid=rid())
    check("a different request id pays again: four rows",
          r.get("ok") is True and "repeat" not in r
          and len(rows(eng)) == n0 + 4
          and cash(eng, chris) == c0 + 4000000, r)
    n0, c0 = len(rows(eng)), cash(eng, chris)
    for label, bad in (("no request id", {}),
                       ("a 3-character request id", {"rid": "abc"}),
                       ("a request id with a space in it",
                        {"rid": "abcd efgh"}),
                       ("a request id that is a number", {"rid": 12345678}),
                       ("a 33-character request id", {"rid": "a" * 33})):
        st, r = adm(eng, svc, "svcFund", cents=2000, **bad)
        check(f"{label}: 400 request id missing, no ledger row",
              (st, r.get("error")) == (400, "request id missing")
              and len(rows(eng)) == n0, (st, r))
    st, r = adm(eng, adm_tok, "plCreate", name="Nora")
    check("NEW PLAYER without a request id: 400, nobody made",
          (st, r.get("error")) == (400, "request id missing")
          and eng.glass_data(adm_tok, "players")[1]["players"][-1]["name"]
          == "Sam", (st, r))
    # the same request id from another session, or for another action, is
    # not the same dialog: a stored reply holds a player's figures
    st, r = adm(eng, adm_tok, "plFund", accountId=sam, cents=100, rid=short)
    check("a request id is remembered for the session and the action that "
          "used it: another session with the same id is a new request",
          r.get("ok") is True and "repeat" not in r
          and (r.get("player") or {}).get("id") == sam
          and "service" not in r, r)

    n0, s0 = len(rows(eng)), cash(eng, sam)
    both = rid()
    st, r = adm(eng, adm_tok, "plFund", accountId=sam, cents=300, rid=both)
    st2, r2 = adm(eng, adm_tok, "plTake", accountId=sam, cents=100, rid=both)
    check("...and so is the same id on the same machine for another "
          "action: an add is not answered with a take-back's reply",
          r.get("ok") is True and r2.get("ok") is True
          and "repeat" not in r2
          and r2.get("msg") == "Took $1.00 back from Sam's wallet."
          and cash(eng, sam) == s0 + 200000 and len(rows(eng)) == n0 + 4,
          (r, r2))
    again = rid()
    adm(eng, svc, "svcFund", cents=100, rid=again)
    c1 = cash(eng, chris)
    act(eng, svc, "logout")                     # DONE
    tap_at(eng, "M1", "DA4A0001")               # a new tap-over, a new token
    check("(a new tap-over has a new token)",
          tok_of(eng, "M1") not in (None, svc))
    svc = tok_of(eng, "M1")
    st, r = adm(eng, svc, "svcFund", cents=100, rid=again)
    check("...and the same id under a new session on the same machine: "
          "that session's own request, not the old session's reply",
          r.get("ok") is True and "repeat" not in r
          and cash(eng, chris) == c1 + 100000, r)

    print("— adding and taking back: two at once, and the memory's limits")
    n0, c0 = len(rows(eng)), cash(eng, chris)
    real_pa = eng.player_action
    inside, go = threading.Event(), threading.Event()

    def slow(req, by=None):
        inside.set()
        go.wait(5)
        return real_pa(req, by=by)
    eng.player_action = slow
    twice, out = rid(), {}
    t = threading.Thread(target=lambda: out.update(
        first=adm(eng, svc, "svcFund", cents=900, rid=twice)))
    t.start()
    inside.wait(5)
    st, r = adm(eng, svc, "svcFund", cents=900, rid=twice)
    go.set()
    t.join(5)
    eng.player_action = real_pa
    check("the same request while the first is still being worked on: "
          "409 busy, in words",
          st == 409 and r.get("busy") is True and r.get("ok") is False
          and r.get("error") == "Still working on that. Wait a moment.",
          (st, r))
    check("...the first one pays, once",
          out["first"][1].get("ok") is True
          and cash(eng, chris) == c0 + 900000
          and len(rows(eng)) == n0 + 2, out)
    st, r = adm(eng, svc, "svcFund", cents=900, rid=twice)
    check("...and after it has finished the same request gets its reply "
          "again, with no third row",
          r.get("repeat") is True and len(rows(eng)) == n0 + 2, r)
    t0 = time.time()
    old = rid()
    st, r = eng.glass_admin_action({"sess": svc, "action": "svcFund",
                                    "cents": 100, "rid": old}, now=t0)
    st, r = eng.glass_admin_action({"sess": svc, "action": "svcFund",
                                    "cents": 100, "rid": old}, now=t0 + 599)
    check("a request id is remembered for 10 minutes",
          r.get("repeat") is True, r)
    st, r = eng.glass_admin_action({"sess": svc, "action": "svcFund",
                                    "cents": 100, "rid": old}, now=t0 + 601)
    check("...and no longer", r.get("ok") is True and "repeat" not in r, r)
    for _ in range(70):
        adm(eng, svc, "svcFund", cents=1, rid=rid())
    check("at most 64 request ids are kept per machine",
          len(eng._glass_rids["M1"]) == 64, len(eng._glass_rids["M1"]))

    print("— adding and taking back: the amount")
    n0, c0 = len(rows(eng)), cash(eng, chris)
    for bad in (0, -5, 1.5, "20", True, None, 20.0):
        st, r = adm(eng, svc, "svcFund", cents=bad, rid=rid())
        check(f"an amount of {bad!r} is refused: Enter an amount.",
              (st, r.get("error")) == (200, "Enter an amount.")
              and r.get("ok") is False and len(rows(eng)) == n0, (st, r))
    st, r = adm(eng, svc, "svcFund", cents=10_000_000_000, rid=rid())
    check("one cent over the largest figure a keypad shows is refused, "
          "and says that figure",
          (st, r.get("error")) == (200, "The most you can add at once is "
                                        "$99,999,999.99.")
          and len(rows(eng)) == n0, (st, r))
    st, r = adm(eng, adm_tok, "plTake", accountId=sam,
                cents=10_000_000_000, rid=rid())
    check("the same for TAKE BACK, in its own words",
          (st, r.get("error")) == (200, "The most you can take back at "
                                        "once is $99,999,999.99.")
          and len(rows(eng)) == n0, (st, r))
    st, r = adm(eng, svc, "svcFund", cents=10_000_001, rid=rid())
    check("what was the ceiling, $100,000.00, is no ceiling: a cent over "
          "it goes through (the owner: \"If someone wants to be a fake "
          "millionaire and hand out fat wallets let them\")",
          r.get("ok") is True and cash(eng, chris) == c0 + 10_000_001_000
          and r.get("msg") == "Added $100,000.01 to Christopher's wallet.",
          r)
    c1 = cash(eng, chris)
    st, r = adm(eng, svc, "svcFund", cents=9_999_999_999, rid=rid())
    check("and so does the largest figure itself",
          r.get("ok") is True and cash(eng, chris) == c1 + 9_999_999_999_000
          and r.get("msg") == "Added $99,999,999.99 to Christopher's "
                              "wallet.", r)
    n0 = len(rows(eng))
    for bad in ("true", 1, None, "false"):
        st, r = adm(eng, svc, "svcFund", cents=500, restricted=bad,
                    rid=rid())
        check(f"restricted of {bad!r} is a page fault: 400, no ledger row",
              (st, r.get("error")) == (400, "restricted must be true or "
                                            "false")
              and len(rows(eng)) == n0, (st, r))
    st, r = adm(eng, svc, "svcFund", cents=500, restricted=False, rid=rid())
    check("restricted false is cash",
          r.get("ok") is True and rows(eng)[-1]["kind"] == "cashable", r)

    print("— adding and taking back: the players menu")
    n0, s0, h0 = len(rows(eng)), cash(eng, sam), cash(eng, "house")
    st, r = adm(eng, adm_tok, "plFund", accountId=sam, cents=2500, rid=rid())
    new = rows(eng)[n0:]
    check("ADD FUNDS for a chosen player: their wallet up, the House down, "
          "two rows, the notes say the admin menu",
          st == 200 and r.get("msg") == "Added $25.00 to Sam's wallet."
          and cash(eng, sam) == s0 + 2500000
          and cash(eng, "house") == h0 - 2500000
          and [e["note"] for e in new] == ["house fund" + by_adm,
                                           f"fund {sam}" + by_adm], (r, new))
    row = dict(r.get("player") or {})
    fobs = row.pop("fobs", None)
    check("...the reply carries that player's row, their fob by its last "
          "6 characters",
          row == {"id": sam, "name": "Sam", "admin": False, "hasPin": False,
                  "walletMc": s0 + 2500000,
                  "wallet": f"${(s0 + 2500000) / 100000:,.2f}",
                  "freePlayMc": 0, "freePlay": "$0.00"}
          and [f.get("tail") for f in fobs or []] == ["A00001"],
          r.get("player"))
    st, r = adm(eng, adm_tok, "plFund", accountId=sam, cents=400,
                restricted=True, rid=rid())
    check("ADD FREE SLOT PLAY for a chosen player",
          r.get("msg") == "Added $4.00 Free Slot Play for Sam."
          and free(eng, sam) == 400000
          and (r.get("player") or {}).get("freePlay") == "$4.00", r)
    n0, s0, h0 = len(rows(eng)), cash(eng, sam), cash(eng, "house")
    st, r = adm(eng, adm_tok, "plTake", accountId=sam, cents=1000,
                restricted=True, rid=rid())
    new = rows(eng)[n0:]
    check("TAKE BACK takes from the cash wallet and credits the House, "
          "whatever restricted says",
          r.get("msg") == "Took $10.00 back from Sam's wallet."
          and cash(eng, sam) == s0 - 1000000
          and cash(eng, "house") == h0 + 1000000
          and free(eng, sam) == 400000
          and [(e["accountId"], e["kind"], e["deltaMillicents"], e["note"])
               for e in new]
          == [(sam, "cashable", -1000000, "pull back to house" + by_adm),
              ("house", "cashable", 1000000, f"pull back {sam}" + by_adm)],
          (r, new))
    n0, s0 = len(rows(eng)), cash(eng, sam)
    st, r = adm(eng, adm_tok, "plTake", accountId=sam, cents=99_999_00,
                rid=rid())
    check("taking back more than the wallet holds is refused in the "
          "store's words, with no ledger row",
          st == 200 and r.get("ok") is False
          and r.get("error") == f"Sam has ${s0 / 100000:,.2f} — can't take "
                                "$99,999.00. Try a smaller amount."
          and len(rows(eng)) == n0 and cash(eng, sam) == s0, (st, r))
    st, r = adm(eng, adm_tok, "plTake", accountId=sam, cents=-500, rid=rid())
    check("a negative amount for TAKE BACK is refused: the hub sets the "
          "sign, not the page",
          (st, r.get("error")) == (200, "Enter an amount.")
          and cash(eng, sam) == s0 and len(rows(eng)) == n0, (st, r))

    eng.hub_store.set_nickname("M2", "The Very Long Named Machine Nine",
                               protocol="g2s")
    adm(eng, adm_tok, "plFund", accountId=sam, cents=100, rid=rid())
    check("the machine's nickname in the note is cut to 24 characters",
          rows(eng)[-1]["note"]
          == f"fund {sam} — by AJ at The Very Long Named Mach (admin menu)",
          rows(eng)[-1]["note"])
    eng.hub_store.set_nickname("M2", "", protocol="g2s")

    print("— the players menu: the player named must be a player")
    eng.account_store.set_pin(sam, "990011")
    before = books(eng)
    for label, who in (("the House", "house"), ("an unknown id", "p999"),
                       ("an id that is not text", 7),
                       ("an id with a space before it", " " + sam),
                       ("no id", None)):
        got = {}
        for name, body in (("plFund", {"cents": 500, "rid": rid()}),
                           ("plTake", {"cents": 500, "rid": rid()}),
                           ("plSetPin", {"pin": "482913"}),
                           ("plClearPin", {}),
                           ("plRename", {"name": "Changed"}),
                           ("plAddFob", {}),
                           ("plRemoveFob", {"fob": "A00001"})):
            if who is not None:
                body = dict(body, accountId=who)
            got[name] = adm(eng, adm_tok, name, **body)
        check(f"{label}: every players action says the player is not "
              "there any more",
              all((st, r.get("error"), r.get("ok")) == (200, GONE, False)
                  for st, r in got.values()),
              {n: (st, r.get("error")) for n, (st, r) in got.items()})
    check("...and none of it changed anything (the House cannot be "
          "funded, renamed or given a PIN from a screen)",
          books(eng) == before)

    print("— adding and taking back: when the hub faults half way")
    calls = []

    def boom(req, by=None):
        calls.append(req.get("action"))
        raise RuntimeError("typed 482913")
    eng.player_action = boom
    n0, c0, bad = len(rows(eng)), cash(eng, chris), rid()
    st, r = adm(eng, svc, "svcFund", cents=700, rid=bad)
    st2, r2 = adm(eng, svc, "svcFund", cents=700, rid=bad)
    eng.player_action = real_pa
    check("a fault during an add: the screen is told it may not have gone "
          "through",
          (st, r.get("error"), r.get("ok")) == (200, UNSURE, False), (st, r))
    check("...the same request again gets the same sentence, and the work "
          "is not tried a second time",
          (st2, r2.get("error")) == (200, UNSURE)
          and r2.get("repeat") is True and calls == ["fund"], (r2, calls))
    st, r = adm(eng, svc, "svcFund", cents=700, rid=bad)
    check("...not even once the fault is gone: a new dialog is needed",
          r.get("error") == UNSURE and cash(eng, chris) == c0
          and len(rows(eng)) == n0, r)

    real_adjust = eng.account_store.adjust

    def house_leg_fails(account_id, *a, **k):
        if account_id == "house":
            return None, "the House row is locked"
        return real_adjust(account_id, *a, **k)
    eng.account_store.adjust = house_leg_fails
    r = eng.player_action({"action": "fund", "accountId": chris,
                           "cents": 100})
    check("player_action marks the one refusal that comes after money "
          "moved (the House leg failed)",
          r.get("ok") is False and r.get("partial") is True
          and cash(eng, chris) == c0 + 100000, r)
    n0, c0, half = len(rows(eng)), cash(eng, chris), rid()
    st, r = adm(eng, svc, "svcFund", cents=700, rid=half)
    st2, r2 = adm(eng, svc, "svcFund", cents=700, rid=half)
    eng.account_store.adjust = real_adjust
    st3, r3 = adm(eng, svc, "svcFund", cents=700, rid=half)
    mine = [e for e in rows(eng)[n0:] if e["accountId"] == chris]
    check("the House leg failing after the player was credited: the "
          "screen is told it may not have gone through",
          (st, r.get("error")) == (200, UNSURE), (st, r))
    check("...and the same request again does not credit the player a "
          "second time (one player row, not two)",
          r2.get("error") == UNSURE and r3.get("error") == UNSURE
          and len(mine) == 1 and cash(eng, chris) == c0 + 700000,
          (r2, r3, mine))

    print("— the players menu: a player deleted while the action runs")
    zed = player(eng, "Zed")

    def delete_first(req, by=None):
        eng.account_store.delete(zed)
        return real_pa(req, by=by)
    eng.player_action = delete_first
    n0 = len(rows(eng))
    st, r = adm(eng, adm_tok, "plFund", accountId=zed, cents=500, rid=rid())
    eng.player_action = real_pa
    check("the player goes between the check and the money: the screen's "
          "own sentence (not the store's), and no ledger row",
          (st, r.get("error"), r.get("ok")) == (200, GONE, False)
          and len(rows(eng)) == n0, (st, r))

    print("— who did it comes from the session, never from the request")
    rec = Records()
    host_log = logging.getLogger("g2s_host")
    level = host_log.level
    host_log.addHandler(rec)
    host_log.setLevel(logging.DEBUG)
    logging.disable(logging.NOTSET)
    n0 = len(rows(eng))
    try:
        st, r = adm(eng, svc, "svcFund", cents=100, rid=rid(),
                    by="Mallory at the door (web)")
        st2, r2 = adm(eng, adm_tok, "plFund", accountId=sam, cents=100,
                      rid=rid(), by="Mallory at the door (web)")
    finally:
        logging.disable(logging.CRITICAL)
        host_log.removeHandler(rec)
        host_log.setLevel(level)
    new = rows(eng)[n0:]
    check("svcFund and plFund with a by in the request body: both ledger "
          "notes of each still end with the real admin and machine",
          r.get("ok") is True and r2.get("ok") is True
          and [e["note"] for e in new]
          == ["house fund" + by_svc, f"fund {chris}" + by_svc,
              "house fund" + by_adm, f"fund {sam}" + by_adm], (r, r2, new))
    funds = [ln for ln in rec.lines if "player fund" in ln]
    check("...and so does the log line of each",
          len(funds) == 2 and funds[0].endswith(by_svc)
          and funds[1].endswith(by_adm)
          and "Mallory" not in "\n".join(rec.lines), funds)

    print("— the Players web page is unchanged")
    n0 = len(rows(eng))
    r = eng.player_action({"action": "fund", "accountId": sam, "cents": 100,
                           "by": "Mallory at the door"})
    new = rows(eng)[n0:]
    check("a fund from the web page writes its notes without a by, and a "
          "by in its body changes nothing",
          r.get("ok") is True
          and [e["note"] for e in new] == ["house fund", f"fund {sam}"], new)
    return eng, svc, adm_tok, chris, sam, aj


def admin_menu_pins_names_fobs():
    eng, svc, adm_tok, chris, sam, aj = admin_menu_money()
    store = eng.account_store
    real_pa = eng.player_action

    print("— SET PIN from a screen")
    store.clear_pin(sam)
    store.clear_pin(chris)
    set_calls = []
    real_set = store.set_pin

    def counting(*a, **k):
        set_calls.append(1)
        return real_set(*a, **k)
    store.set_pin = counting
    for least in (6, 4):
        eng.hub_store.set_host_setting("pin_min_digits", str(least))
        for name, tok, body, whose in (
                ("svcSetPin", svc, {}, chris),
                ("plSetPin", adm_tok, {"accountId": sam}, sam)):
            for pin, want in (
                    ("12ab56", "A PIN is digits only."),
                    ("", "A PIN is digits only."),
                    ("١٢٣٤٥٦", "A PIN is digits only."),
                    ("123456 ", "A PIN is digits only."),
                    (" 123456", "A PIN is digits only."),
                    ("123456\n", "A PIN is digits only."),
                    (123456, "A PIN is digits only."),
                    (None, "A PIN is digits only."),
                    ("1" * (least - 1),
                     f"A PIN needs at least {least} digits."),
                    ("123456789", "A PIN can have at most 8 digits.")):
                st, r = adm(eng, tok, name, pin=pin, **body)
                check(f"{name}, minimum {least}: {pin!r} is refused: {want}",
                      (st, r.get("error"), r.get("ok")) == (200, want, False)
                      and store.get(whose)["hasPin"] is False, (st, r))
    check("none of those reached the store",
          set_calls == [] and store.get(chris)["hasPin"] is False
          and store.get(sam)["hasPin"] is False, len(set_calls))
    store.set_pin = real_set
    st, r = adm(eng, adm_tok, "plSetPin", accountId=sam, pin="4829")
    check("with the minimum at 4, four digits are a PIN",
          r.get("ok") is True and r.get("msg") == "Sam's PIN is set."
          and (r.get("player") or {}).get("hasPin") is True, r)
    eng.hub_store.set_host_setting("pin_min_digits", "6")
    st, r = adm(eng, adm_tok, "plSetPin", accountId=sam, pin="482913")
    check("a good PIN: ok, a sentence, the player's row, and no PIN in "
          "the reply",
          st == 200 and r.get("ok") is True
          and r.get("msg") == "Sam's PIN is set."
          and (r.get("player") or {}).get("hasPin") is True
          and "482913" not in json.dumps(r), (st, r))
    eng.associations["M3"] = FakeAssoc(egm_id="M3")
    lg = eng.pin_login("M3", "482913")
    check("that PIN cards that player in",
          lg.get("ok") is True and lg.get("name") == "Sam"
          and (eng.card_sessions.get("M3") or {}).get("accountId") == sam,
          lg)
    eng._glass_card_out("M3")
    check("the PIN it replaced no longer works",
          eng.pin_login("M3", "4829").get("ok") is False
          and eng.card_sessions.get("M3") is None)
    eng._pin_lockouts = {}
    st, r = adm(eng, svc, "svcSetPin", pin="482913")
    check("a PIN another player has: Pick a different PIN.",
          (st, r.get("error"), r.get("ok"))
          == (200, "Pick a different PIN.", False)
          and store.get(chris)["hasPin"] is False, (st, r))
    check("...and the first player keeps it",
          (store.find_by_pin("482913") or {}).get("id") == sam)
    st, r = adm(eng, svc, "svcSetPin", pin="13572468")
    check("eight digits are a PIN; setting it ends nobody's session",
          r.get("ok") is True and r.get("msg") == "Christopher's PIN is set."
          and tok_of(eng, "M1") == svc and tok_of(eng, "M2") == adm_tok, r)
    st, r = adm(eng, adm_tok, "plSetPin", accountId=aj, pin="246813")
    check("SET PIN for an admin's account is allowed",
          r.get("ok") is True and store.get(aj)["hasPin"] is True, r)
    zed = player(eng, "Zed")

    def delete_first(req, by=None):
        store.delete(zed)
        return real_pa(req, by=by)
    eng.player_action = delete_first
    st, r = adm(eng, adm_tok, "plSetPin", accountId=zed, pin="975310")
    eng.player_action = real_pa
    check("the player goes while the PIN is being set: the player is not "
          "there any more", (st, r.get("error")) == (200, GONE), (st, r))

    def boom(req, by=None):
        raise RuntimeError("could not hash 975310")
    eng.player_action = boom
    st, r = adm(eng, adm_tok, "plSetPin", accountId=sam, pin="975310")
    eng.player_action = real_pa
    check("a fault while setting a PIN: a plain sentence, no fault text",
          (st, r.get("error"), r.get("ok"))
          == (200, "That did not work. Try again.", False)
          and "975310" not in json.dumps(r), (st, r))

    print("— CLEAR PIN from a screen")
    st, r = adm(eng, adm_tok, "plClearPin", accountId=sam)
    check("CLEAR PIN: the player has no PIN",
          r.get("ok") is True and r.get("msg") == "Sam's PIN is cleared."
          and (r.get("player") or {}).get("hasPin") is False
          and store.get(sam)["hasPin"] is False, r)
    check("...and the old PIN no longer logs in",
          eng.pin_login("M3", "482913").get("ok") is False
          and eng.card_sessions.get("M3") is None)
    eng._pin_lockouts = {}
    st, r = adm(eng, adm_tok, "plClearPin", accountId=sam)
    check("clearing a PIN that is not set is fine",
          r.get("ok") is True and r.get("msg") == "Sam's PIN is cleared.", r)

    print("— names typed on a screen: NEW PLAYER and RENAME")
    before = books(eng)
    for name, want in (
            ("", "Type a name."), ("   ", "Type a name."),
            (None, "Type a name."), (42, "Type a name."),
            ("A" * 17, "A name can have at most 16 letters."),
            ("Zoë", "Use letters, numbers, spaces, - and ' only."),
            ("Mary_Ann", "Use letters, numbers, spaces, - and ' only."),
            ("Tom\tTom", "Use letters, numbers, spaces, - and ' only."),
            ("<b>Al</b>", "Use letters, numbers, spaces, - and ' only."),
            ("Сэм", "Use letters, numbers, spaces, - and ' only."),
            ("sam", "There is already a player called Sam."),
            (" SAM ", "There is already a player called Sam.")):
        st, r = adm(eng, adm_tok, "plCreate", name=name, rid=rid())
        st2, r2 = adm(eng, adm_tok, "plRename", accountId=chris, name=name)
        check(f"a name of {name!r} is refused for a new player and for a "
              f"rename: {want}",
              (st, r.get("error"), r.get("ok")) == (200, want, False)
              and (st2, r2.get("error"), r2.get("ok")) == (200, want, False),
              (r, r2))
    check("none of those made or renamed anybody", books(eng) == before)
    one = rid()
    st, r = adm(eng, adm_tok, "plCreate", name="  Mary   Ann  ", rid=one)
    mary = (r.get("player") or {}).get("id")
    check("NEW PLAYER: the name is tidied (ends stripped, one space "
          "between words), and the reply carries the new player's row",
          st == 200 and r.get("ok") is True
          and r.get("msg") == "Mary Ann is set up."
          and r.get("player") == {
              "id": mary, "name": "Mary Ann", "admin": False,
              "hasPin": False, "walletMc": 0, "wallet": "$0.00",
              "freePlayMc": 0, "freePlay": "$0.00", "fobs": []}
          and store.get(mary)["name"] == "Mary Ann", (st, r))
    count = len(store.snapshot()["accounts"])
    st, r2 = adm(eng, adm_tok, "plCreate", name="  Mary   Ann  ", rid=one)
    check("NEW PLAYER twice with one request id makes one player",
          r2.get("repeat") is True
          and (r2.get("player") or {}).get("id") == mary
          and len(store.snapshot()["accounts"]) == count, r2)
    st, r = adm(eng, adm_tok, "plCreate", name="O'Neil-Smith 2nd", rid=rid())
    check("a 16-character name with a hyphen, an apostrophe and a digit "
          "passes",
          r.get("ok") is True and r.get("msg") == "O'Neil-Smith 2nd is set "
                                                  "up.", r)

    def boom_create(req, by=None):
        raise RuntimeError("disk full")
    eng.player_action = boom_create
    lost = rid()
    st, r = adm(eng, adm_tok, "plCreate", name="Nora", rid=lost)
    eng.player_action = real_pa
    st2, r2 = adm(eng, adm_tok, "plCreate", name="Nora", rid=lost)
    check("a fault while making a player: it may not have gone through, "
          "and the same request is not tried again",
          r.get("error") == UNSURE and r2.get("error") == UNSURE
          and r2.get("repeat") is True
          and not any(a["name"] == "Nora"
                      for a in store.snapshot()["accounts"]), (r, r2))
    st, r = adm(eng, adm_tok, "plRename", accountId=sam, name="SAM")
    check("a player may be renamed to their own name in another case",
          r.get("ok") is True and r.get("msg") == "Renamed to SAM."
          and store.get(sam)["name"] == "SAM"
          and (r.get("player") or {}).get("name") == "SAM", r)
    st, r = adm(eng, adm_tok, "plRename", accountId=sam,
                name="  Sam   the  2nd ")
    check("RENAME tidies the name it saves",
          r.get("msg") == "Renamed to Sam the 2nd."
          and store.get(sam)["name"] == "Sam the 2nd", r)
    adm(eng, adm_tok, "plRename", accountId=sam, name="Sam")
    house_name = store.get("house")["name"]
    st, r = adm(eng, adm_tok, "plRename", accountId="house", name="Mine")
    check("the House cannot be renamed from a screen",
          (st, r.get("error")) == (200, GONE)
          and store.get("house")["name"] == house_name, (st, r))

    print("— the players menu: when the store says no")
    for name, body in (("plRename", {"name": "Zed the 2nd"}),
                       ("plClearPin", {})):
        zed = player(eng, "Zed")
        eng.player_action = delete_first
        st, r = adm(eng, adm_tok, name, accountId=zed, **body)
        eng.player_action = real_pa
        check(f"{name}: the player goes while it runs: the player is not "
              "there any more",
              (st, r.get("error"), r.get("ok")) == (200, GONE, False),
              (st, r))
    eng.player_action = lambda req, by=None: {"ok": False,
                                              "error": "the store said no"}
    got = [adm(eng, adm_tok, "plRename", accountId=sam, name="Sam Too"),
           adm(eng, adm_tok, "plClearPin", accountId=sam),
           adm(eng, adm_tok, "plCreate", name="Nell", rid=rid())]
    eng.player_action = real_pa
    check("RENAME, CLEAR PIN and NEW PLAYER refused by the store for any "
          "other reason: a plain sentence, not the store's words",
          all((st, r.get("error"), r.get("ok"))
              == (200, "That did not work. Try again.", False)
              for st, r in got), got)

    print("— the players view")
    eng.hub_store.fob_set("51B00002", tier="player", account_id=sam,
                          label="blue fob")
    uno = player(eng, "bo", "0A0B0C0D")
    st, v = eng.glass_data(adm_tok, "players")
    names = [p["name"] for p in v.get("players") or []]
    check("every player, sorted by name without regard to case, and no "
          "House row",
          st == 200 and v.get("ok") is True and v.get("view") == "players"
          and v.get("egmId") == "M2"
          and names == sorted(names, key=str.lower)
          and names[:3] == ["AJ", "bo", "Christopher"]
          and set(names) == {a["name"] for a in store.snapshot()["accounts"]
                             if a["kind"] == "player"}
          and "house" not in [p["id"] for p in v["players"]], names)
    row = next(p for p in v["players"] if p["id"] == sam)
    check("a row: id, name, admin, PIN, wallet, Free Slot Play, and the "
          "fobs in uid order by their last 6 characters",
          {k: row[k] for k in row if k != "fobs"} == {
              "id": sam, "name": "Sam", "admin": False, "hasPin": False,
              "walletMc": cash(eng, sam),
              "wallet": f"${cash(eng, sam) / 100000:,.2f}",
              "freePlayMc": 400000, "freePlay": "$4.00"}
          and [(f["tail"], f["label"]) for f in row["fobs"]]
          == [("A00001", ""), ("B00002", "blue fob")]
          and all(set(f) == {"tail", "label", "lastSeen"}
                  for f in row["fobs"]), row)
    check("no whole fob uid anywhere in the view",
          not any(f["uid"] in json.dumps(v) for f in eng.hub_store.fobs()))
    check("the limits the page needs, and no ADD FOB wait",
          v.get("limits") == {"maxCents": 9999999999,
                              "quickCents": [2000, 5000, 10000],
                              "nameMax": 16, "fobWaitSec": 20}
          and "fobWait" in v and v["fobWait"] is None, v.get("limits"))
    real_snap = store.snapshot
    store.snapshot = lambda: (_ for _ in ()).throw(RuntimeError("db"))
    st, v = eng.glass_data(adm_tok, "players")
    store.snapshot = real_snap
    check("when the players cannot be read the view says so, and sends no "
          "list (an empty list would read as no players)",
          st == 200 and v == {"ok": False, "view": "players", "egmId": "M2",
                              "error": "players not readable"}, (st, v))

    print("— REMOVE FOB")
    eng.hub_store.fob_set("77B00002", tier="player", account_id=chris)
    eng.hub_store.fob_set("88B00002", tier="player", account_id=chris)

    def links():
        return sorted((f["uid"], f["accountId"])
                      for f in eng.hub_store.fobs())
    before = links()
    for label, body, want in (
            # bo has ONE fob: every uid "ends with" an empty string, so a
            # page fault sending none would remove the only one
            ("an empty fob", {"accountId": uno, "fob": ""},
             "That fob is not on bo any more."),
            ("no fob", {"accountId": uno},
             "That fob is not on bo any more."),
            ("a fob that is not text", {"accountId": uno, "fob": 0},
             "That fob is not on bo any more."),
            ("a fob that is not hex", {"accountId": uno, "fob": "0C0D.."},
             "That fob is not on bo any more."),
            ("a fob that is not hex (a wildcard)",
             {"accountId": uno, "fob": "*"},
             "That fob is not on bo any more."),
            ("a fob longer than 32", {"accountId": uno, "fob": "0" * 33},
             "That fob is not on bo any more."),
            ("another player's fob", {"accountId": sam, "fob": "150001"},
             "That fob is not on Sam any more."),
            ("a tail two of the player's fobs share",
             {"accountId": chris, "fob": "B00002"},
             "Remove that fob on the Players web page."),
            ("the fob this admin carded in with",
             {"accountId": aj, "fob": "A00001"},
             "That is the fob you carded in with."),
            ("a fob that is carded in at a machine",
             {"accountId": chris, "fob": "150001"},
             "That fob is carded in at Bluebird 2. Card out there first.")):
        st, r = adm(eng, adm_tok, "plRemoveFob", **body)
        check(f"REMOVE FOB, {label}: {want}",
              (st, r.get("error"), r.get("ok")) == (200, want, False)
              and links() == before, (st, r))
    dana_fob = next(f for f in eng.hub_store.fobs() if f["uid"] == "DA4A0001")
    st, r = adm(eng, adm_tok, "plRemoveFob", accountId=dana_fob["accountId"],
                fob="4A0001")
    check("REMOVE FOB, the fob of an admin who is on a tap-over: carded "
          "in there",
          (st, r.get("error")) == (200, "That fob is carded in at Bluebird "
                                        "2. Card out there first.")
          and links() == before, (st, r))
    check("the admin who was refused their own fob is still an admin, "
          "still carded in",
          eng.glass_state("M2").get("admin") is True
          and tok_of(eng, "M2") == adm_tok)
    real_fob_set = eng.hub_store.fob_set
    eng.hub_store.fob_set = lambda *a, **k: (None, "db is read-only")
    st, r = adm(eng, adm_tok, "plRemoveFob", accountId=sam, fob="B00002")
    eng.hub_store.fob_set = real_fob_set
    check("REMOVE FOB when the hub cannot write it: could not be removed",
          (st, r.get("error")) == (200, "That fob could not be removed. "
                                        "Try again.")
          and links() == before, (st, r))
    st, r = adm(eng, adm_tok, "plRemoveFob", accountId=sam, fob="b00002")
    fob = eng.hub_store.fob_get("51B00002")
    check("a good removal (the tail in either case): the link is cleared "
          "and the fob stays known to the hub as a player fob",
          st == 200 and r.get("ok") is True
          and r.get("msg") == "Fob removed from Sam."
          and fob is not None and fob["accountId"] == ""
          and fob["tier"] == "player"
          and [f["tail"] for f in (r.get("player") or {}).get("fobs")
               or []] == ["A00001"], (r, fob))
    st, r = adm(eng, adm_tok, "plRemoveFob", accountId=sam, fob="B00002")
    check("removing it again: it is not on that player any more",
          r.get("error") == "That fob is not on Sam any more.", r)

    return eng, svc, adm_tok, chris, sam


class Records(logging.Handler):
    """Every record the hub's two loggers write, as the text a file gets."""

    def __init__(self):
        super().__init__(logging.DEBUG)
        self.lines = []

    def emit(self, record):
        self.lines.append(record.getMessage())
        if record.exc_info:
            self.lines.append(logging.Formatter().formatException(
                record.exc_info))


def admin_menu_quick_amounts():
    print("— the quick amounts on the admin's add pad are the owner's to "
          "set (the owner: \"the game room owner can choose what they want "
          "for quick adds\")")
    for good, label in ((100, "1"), (2000, "20"), (999_900, "9,999"),
                        (1_000_000, "10K"), (2_500_000, "25K"),
                        (100_000_000, "1M"), (250_000_000, "2.5M"),
                        (9_990_000_000, "99.9M")):
        check(f"{good} cents can be a quick amount (a chip shows it as "
              f"{label})", gh.quick_add_error(good) is None,
              gh.quick_add_error(good))
    for bad, why in ((0, "nothing"), (50, "under 1"), (2050, "has cents"),
                     (-2000, "negative"), (True, "not a number"),
                     ("2000", "text"), (2000.0, "not whole cents"),
                     (None, "missing"),
                     (1_050_000, "10,500 cannot be written as thousands"),
                     (255_000_000, "2,550,000 is not a round hundred "
                                   "thousand"),
                     (9_999_999_900, "99,999,999 is not round"),
                     (10_000_000_000, "100,000,000 is over the most a "
                                      "keypad shows")):
        check(f"{bad!r} cannot ({why}), and the reason is a sentence",
              isinstance(gh.quick_add_error(bad), str)
              and gh.quick_add_error(bad).endswith("."),
              gh.quick_add_error(bad))

    eng = menu_engine("M1", "M2")
    player(eng, "Christopher", "C4150001", cents=1000)
    player(eng, "Dana", "DA4A0001", admin=True)
    check("a hub starts with 20, 50 and 100",
          gh.quick_add_cents(eng.hub_store) == [2000, 5000, 10000]
          and gh.QUICK_ADD_DEFAULT_CENTS == (2000, 5000, 10000))

    def settings(payload):
        h = gh.G2SRequestHandler.__new__(gh.G2SRequestHandler)
        h.host_engine = eng
        sent = []
        h._send = lambda code, body, ctype=None, soap=True: \
            sent.append((code, json.loads(body)))
        h._handle_settings(json.dumps(payload))
        return sent[0]

    st, r = settings({"quickAddCents": [500, 2_500_000, 100_000_000]})
    check("Settings takes three amounts and answers with them",
          st == 200 and r.get("ok") is True
          and r.get("quickAddCents") == [500, 2_500_000, 100_000_000],
          (st, r))
    check("and they are the ones in force from then on, in the order "
          "given", gh.quick_add_cents(eng.hub_store)
          == [500, 2_500_000, 100_000_000])
    tap_at(eng, "M1", "C4150001")
    tap_at(eng, "M1", "DA4A0001")           # Dana taps over Christopher
    blk = eng.glass_state("M1").get("service") or {}
    check("the service screen of a tap-over is told them",
          blk.get("quickCents") == [500, 2_500_000, 100_000_000], blk)
    tap_at(eng, "M2", "DA4A0001")           # ...and cards in at M2
    st, v = eng.glass_data(tok_of(eng, "M2"), "players")
    check("so is the players menu of an admin carded in",
          st == 200 and (v.get("limits") or {}).get("quickCents")
          == [500, 2_500_000, 100_000_000], v.get("limits"))
    for bad, why in (([500, 2000], "two"), ([500, 2000, 5000, 100], "four"),
                     ("500,2000,5000", "text"), (None, "nothing"),
                     ({"a": 500, "b": 2000, "c": 5000}, "a set of three "
                                                        "named ones")):
        st, r = settings({"quickAddCents": bad})
        check(f"{why} is refused: Give three quick amounts.",
              (st, r.get("error")) == (400, "Give three quick amounts.")
              and gh.quick_add_cents(eng.hub_store)
              == [500, 2_500_000, 100_000_000], (st, r))
    check("the most a quick amount can be is said as a figure",
          gh.quick_add_error(10_000_000_000)
          == "A quick amount can be 99,900,000 at most.")
    for bad, why in (([500, 2000, 10_000_000_000], "one over the most"),
                     ([500, 2000, 2050], "one with cents"),
                     ([500, 2000, 1_050_000], "one a chip cannot write"),
                     ([500, True, 2000], "one that is not a number")):
        st, r = settings({"quickAddCents": bad})
        check(f"{why} is refused in words and changes nothing",
              st == 400 and r.get("ok") is False
              and isinstance(r.get("error"), str) and r["error"]
              and gh.quick_add_cents(eng.hub_store)
              == [500, 2_500_000, 100_000_000], (st, r))
    for stored in ("", "500,2000", "500,abc,2000", "500,2000,2050",
                   "0,0,0"):
        eng.hub_store.set_host_setting("quick_add_cents", stored)
        check(f"a stored value of {stored!r} reads as what a hub starts "
              "with", gh.quick_add_cents(eng.hub_store)
              == [2000, 5000, 10000])
    real = eng.hub_store.host_setting
    eng.hub_store.host_setting = lambda *a, **k: (_ for _ in ()).throw(
        RuntimeError("db"))
    try:
        got = gh.quick_add_cents(eng.hub_store)
    finally:
        eng.hub_store.host_setting = real
    check("so does a store that cannot be read", got == [2000, 5000, 10000])
    st, r = settings({"quickAddCents": [2000, 2000, 2000]})
    check("three the same is the owner's business", st == 200
          and gh.quick_add_cents(eng.hub_store) == [2000, 2000, 2000])

    print("— the presets on a player's MY WALLET are the owner's to set "
          "too (the owner: \"do what we did for the quick select in the "
          "admin menu ... to the wallet quick transfer presets\")")
    check("a hub starts with 1, 5 and 20",
          gh.wallet_chip_cents(eng.hub_store) == [100, 500, 2000]
          and gh.WALLET_CHIP_DEFAULT_CENTS == (100, 500, 2000))
    st, v = eng.glass_data(tok_of(eng, "M2"), "cashout")
    check("the wallet's data on a machine's screen carries them",
          st == 200 and v.get("chipCents") == [100, 500, 2000], v)
    st, r = settings({"walletChipCents": [500, 2000, 10000]})
    check("Settings takes three amounts for the wallet and answers with "
          "them", st == 200 and r.get("ok") is True
          and r.get("walletChipCents") == [500, 2000, 10000], (st, r))
    st, v = eng.glass_data(tok_of(eng, "M2"), "cashout")
    check("and the screen is told the new ones",
          gh.wallet_chip_cents(eng.hub_store) == [500, 2000, 10000]
          and st == 200 and v.get("chipCents") == [500, 2000, 10000], v)
    check("the admin's quick amounts are a separate setting: untouched",
          gh.quick_add_cents(eng.hub_store) == [2000, 2000, 2000])
    st, r = settings({"quickAddCents": [100, 200, 300]})
    check("and setting those leaves the wallet's alone", st == 200
          and gh.wallet_chip_cents(eng.hub_store) == [500, 2000, 10000]
          and gh.quick_add_cents(eng.hub_store) == [100, 200, 300])
    for bad, why in (([500, 2000], "two"), ("5,20,100", "text"),
                     ([500, 2000, 2050], "one with cents"),
                     ([500, 2000, 1_050_000], "one a chip cannot write")):
        st, r = settings({"walletChipCents": bad})
        check(f"for the wallet, {why} is refused in words and changes "
              "nothing", st == 400 and r.get("ok") is False
              and isinstance(r.get("error"), str) and r["error"]
              and gh.wallet_chip_cents(eng.hub_store)
              == [500, 2000, 10000], (st, r))
    st, r = settings({"quickAddCents": [700, 800, 900],
                      "walletChipCents": [500, 2000, 2050]})
    check("both sent together with one of them bad: neither is changed",
          st == 400 and gh.quick_add_cents(eng.hub_store) == [100, 200, 300]
          and gh.wallet_chip_cents(eng.hub_store) == [500, 2000, 10000],
          (st, r))
    st, r = settings({"quickAddCents": [700, 800, 900],
                      "walletChipCents": [100, 200, 300]})
    check("both sent together and good: both are changed", st == 200
          and gh.quick_add_cents(eng.hub_store) == [700, 800, 900]
          and gh.wallet_chip_cents(eng.hub_store) == [100, 200, 300],
          (st, r))
    eng.hub_store.set_host_setting("wallet_chip_cents", "5,abc")
    check("a stored wallet value that cannot be used reads as what a hub "
          "starts with",
          gh.wallet_chip_cents(eng.hub_store) == [100, 500, 2000])
    check("with no store to read (a test stand-in) it is the same",
          gh.wallet_chip_cents(None) == [100, 500, 2000])


def admin_menu_logs():
    eng, svc, adm_tok, chris, sam = admin_menu_pins_names_fobs()
    store = eng.account_store
    real_pa = eng.player_action
    print("— who did it and where: the hub log; and no PIN in any log")
    rec = Records()
    host_log, wire_log = logging.getLogger("g2s_host"), \
        logging.getLogger("g2s_wire")
    levels = (host_log.level, wire_log.level)
    for lg in (host_log, wire_log):
        lg.addHandler(rec)
        lg.setLevel(logging.DEBUG)
    logging.disable(logging.NOTSET)
    srv = None
    try:
        eng.associations["M1"].handpays_pending = {}
        adm(eng, svc, "svcSetPin", pin="864201")              # set
        adm(eng, adm_tok, "plSetPin", accountId=sam, pin="864201")   # taken
        adm(eng, adm_tok, "plSetPin", accountId=sam, pin="86420")    # short
        adm(eng, adm_tok, "plSetPin", accountId=sam, pin="864209")   # set

        def boom(req, by=None):
            # the text of a fault can quote what was typed
            raise RuntimeError("could not store 864207 for " + str(req)
                               if req.get("action") == "setPin"
                               else "could not store 864217")
        eng.player_action = boom
        adm(eng, adm_tok, "plSetPin", accountId=sam, pin="864207")   # fault
        adm(eng, adm_tok, "plFund", accountId=sam, cents=100,
            rid=rid())                                        # fault, money
        eng.player_action = real_pa
        adm(eng, adm_tok, "plClearPin", accountId=sam)
        adm(eng, adm_tok, "plRename", accountId=sam, name="Sammy")
        adm(eng, adm_tok, "plRename", accountId=sam, name="Sam")
        adm(eng, adm_tok, "plCreate", name="Logan", rid=rid())
        adm(eng, adm_tok, "plFund", accountId=sam, cents=100, rid=rid())
        eng.hub_store.fob_set("51C00003", tier="player", account_id=sam)
        adm(eng, adm_tok, "plRemoveFob", accountId=sam, fob="C00003")
        adm(eng, adm_tok, "864205", pin="864205")   # in the wrong field
        act(eng, svc, "clearHandpay")
        act(eng, adm_tok, "clearHandpay")

        # the same over a real socket: the wire logger sees these bodies
        eng.status_cache_lock = threading.Lock()
        eng.status_state_seq = 0

        class H(gh.G2SRequestHandler):
            host_engine = eng
        srv = gh.ThreadingHTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=srv.serve_forever, daemon=True).start()

        def post(path, body):
            c = http.client.HTTPConnection("127.0.0.1",
                                           srv.server_address[1], timeout=10)
            c.request("POST", path, body=json.dumps(body).encode(),
                      headers={"Content-Type": "application/json"})
            resp = c.getresponse()
            out = resp.status, json.loads(resp.read().decode())
            c.close()
            return out
        h1 = post("/api/glass/admin", {"sess": adm_tok, "action": "plSetPin",
                                       "accountId": sam, "pin": "864211"})
        h2 = post("/api/glass/pinlogin", {"egm": "M3", "pin": "864211"})
        h3 = post("/api/players", {"action": "setPin", "accountId": chris,
                                   "pin": "864213"})
        h4 = post("/api/players", {"action": "setPin", "accountId": chris,
                                   "pin": "86"})
        h5 = post("/api/glass/admin", {"sess": adm_tok, "action": "plFund",
                                       "accountId": sam, "cents": 100,
                                       "rid": rid()})
        h6 = post("/api/players", {"action": "fund", "accountId": sam,
                                   "cents": 100})
        raw = b'{"sess": "' + adm_tok.encode() + b'", "pin": "864215"'
        c = http.client.HTTPConnection("127.0.0.1", srv.server_address[1],
                                       timeout=10)
        c.request("POST", "/api/glass/admin", body=raw,
                  headers={"Content-Type": "application/json"})
        h7 = c.getresponse()
        h7 = h7.status, json.loads(h7.read().decode())
        c.close()
        # a body nested deeper than the parser will follow
        deep = b'{"sess": ' + b"[" * 100000 + b"]" * 100000 + b"}"
        c = http.client.HTTPConnection("127.0.0.1", srv.server_address[1],
                                       timeout=10)
        c.request("POST", "/api/glass/admin", body=deep,
                  headers={"Content-Type": "application/json"})
        h9 = c.getresponse()
        h9 = h9.status, json.loads(h9.read().decode())
        c.close()
        real_action = eng.glass_admin_action
        eng.glass_admin_action = lambda req: (_ for _ in ()).throw(
            RuntimeError("could not read " + str(req)))
        h8 = post("/api/glass/admin", {"sess": adm_tok, "action": "plSetPin",
                                       "accountId": sam, "pin": "864219"})
        eng.glass_admin_action = real_action
    finally:
        if srv is not None:
            srv.shutdown()
            srv.server_close()
        eng.player_action = real_pa
        logging.disable(logging.CRITICAL)
        for lg, lvl in zip((host_log, wire_log), levels):
            lg.removeHandler(rec)
            lg.setLevel(lvl)
    text = "\n".join(rec.lines)
    check("over a real socket: POST /api/glass/admin reaches the engine "
          "and sets the PIN; the PIN login and the web page's setPin work "
          "as before",
          h1[0] == 200 and h1[1].get("msg") == "Sam's PIN is set."
          and h2[0] == 200 and h2[1].get("ok") is True
          and h2[1].get("name") == "Sam"
          and h3[0] == 200 and h3[1].get("ok") is True
          and h4[0] == 400 and h5[0] == 200 and h5[1].get("ok") is True
          and h6[0] == 200 and h7 == (400, {"ok": False,
                                            "error": "invalid JSON"}),
          (h1, h2, h3, h4, h5, h6, h7))
    check("a body nested too deep to read is answered as invalid JSON "
          "(it used to end the request with no reply at all)",
          h9 == (400, {"ok": False, "error": "invalid JSON"}), h9)
    check("a fault the engine did not catch still answers the screen in "
          "words, never a 500",
          h8 == (200, {"ok": False,
                       "error": "That did not work. Try again."})
          and "glass admin action failed on the hub side (RuntimeError)"
          in text, h8)
    pins = ("864201", "86420", "864209", "864207", "864205", "864211",
            "864213", "864215", "864217", "864219")
    check("the hub wrote something to both logs during all that",
          "IN  <<< POST /api/glass/admin" in text
          and "player setPin" in text and len(rec.lines) > 20,
          len(rec.lines))
    check("no PIN that was set, refused, faulted on, logged in with or "
          "typed into the wrong field is in any log record",
          not any(p in text for p in pins),
          [i for i, p in enumerate(pins) if p in text])
    check("no session token is in any log record",
          svc not in text and adm_tok not in text)
    wire = [ln for ln in rec.lines if ln.startswith("IN  <<< POST")]
    check("the wire log names the three kinds of PIN-carrying request "
          "and withholds their bodies",
          sum("/api/glass/admin" in ln and "body withheld" in ln
              and "\n" not in ln for ln in wire) == 5   # h1 h5 h7 h9 h8
          and sum("/api/glass/pinlogin" in ln and "body withheld" in ln
                  and "\n" not in ln for ln in wire) == 1
          and sum("/api/players" in ln and "body withheld" in ln
                  and "\n" not in ln for ln in wire) == 2, wire)
    check("...and still records the body of a web page request that "
          "carries no PIN",
          any("/api/players" in ln and '"action": "fund"' in ln
              for ln in wire), wire)
    check("a fault is logged by its class, never its text",
          "[glass:M2] plSetPin failed on the hub side (RuntimeError)" in text
          and "[glass:M2] plFund failed on the hub side (RuntimeError)"
          in text and "could not store" not in text)
    by = " — by AJ at M2 (admin menu)"
    check("SET PIN, CLEAR PIN, RENAME, NEW PLAYER, REMOVE FOB and money "
          "are each logged with which admin and which machine",
          all(any(what in ln and ln.endswith(by) for ln in rec.lines)
              for what in ("player setPin", "player clearPin",
                           "player rename", "player create", "unlinked",
                           "player fund"))
          and any("player setPin" in ln and ln.endswith(
              " — by Dana at Bluebird 2 (service)") for ln in rec.lines),
          [ln for ln in rec.lines if " — by " in ln])
    check("a handpay clear is logged with who asked, from which screen",
          any(ln.endswith("[M1] handpay clear asked by Dana at Bluebird 2 "
                          "(service)") for ln in rec.lines)
          and any(ln.endswith("[M2] handpay clear asked by AJ at M2 (admin "
                              "menu)") for ln in rec.lines),
          [ln for ln in rec.lines if "handpay clear" in ln])
    check("every refusal is logged with its machine, action, status and "
          "the words the screen got",
          "[glass:M2] plSetPin refused (200): Pick a different PIN." in text
          and "[glass:M2] plSetPin refused (200): A PIN needs at least 6 "
              "digits." in text
          and "[glass:M2] plSetPin refused (200): That did not work. Try "
              "again." in text, [ln for ln in rec.lines if "refused" in ln])
    check("an action name this endpoint does not know is not written to "
          "the log",
          "[glass:M2] (unknown) refused (403): not yet" in text)
    check("a web page fund is logged without a by",
          any("player fund" in ln and " — by " not in ln
              for ln in rec.lines))


def floor(eng):
    """Everything a tap can change on the floor: who is carded in where,
    the tap-overs, each machine's live token, how much was sent to the
    machines, and what is queued for a SAS machine."""
    with eng.companion_lock:
        cards = json.dumps(eng.card_sessions, sort_keys=True)
        overs = json.dumps(eng.admin_overlays, sort_keys=True)
    with eng.sas_lock:
        queued = {k: [c.get("type") for c in q]
                  for k, q in eng.sas_commands.items()}
    toks = {e: tok_of(eng, e) for e in sorted(eng.associations)}
    return cards, overs, toks, len(eng.sent), queued


def admin_menu_add_fob():
    print("— ADD FOB: an engine that never armed a reader")
    lean = make_engine(assoc=FakeAssoc())
    report(lean, taps=[tap(0)])
    check("the tap path of an engine with no wait holds none, and needs "
          "nothing set on it", gh.G2SHost._fob_waits is None
          and lean._fob_waits is None and "_fob_waits" not in vars(lean))
    check("the hub counts 20 seconds, allows 2 more for the tap to arrive, "
          "ignores the new fob for 5, and keeps the result 60",
          (gh.ADD_FOB_WAIT_SEC, gh.ADD_FOB_GRACE_SEC, gh.ADD_FOB_SETTLE_SEC,
           gh.ADD_FOB_KEEP_SEC) == (20, 2, 5, 60))

    eng = menu_engine("M1", "M2", "M3", "M4")
    store, hs = eng.account_store, eng.hub_store
    hs.set_nickname("M2", "Bluebird 2", protocol="g2s")
    eng.sas_machines["smib-M1/1"] = {"smibId": "smib-M1"}
    aj = player(eng, "AJ", "A1A00001", admin=True)
    sam = player(eng, "Sam", cents=5000)
    dana = player(eng, "Dana", "DA4A0001")
    player(eng, "Christopher", "C4150001")
    pia = player(eng, "Pia", admin=True)
    hs.fob_set("DEADBEEF", tier="reset", label="Reset key")
    hs.fob_set("A77E0001", tier="attendant", label="Floor key")
    hs.fob_set("0FF1CE01", tier="player")       # player fobs with no player
    hs.fob_set("0FF1CE02", tier="player")
    hs.fob_set("0FF1CE03", tier="player", account_id="p999")   # ...deleted
    store.set_pin(pia, "771234")
    hs.set_host_setting("pin_admin_allowed", "1")
    hs.fob_set("0DDBA110", tier="player")
    with hs.lock:       # a kind of fob a later build might add
        hs._conn.execute("PRAGMA ignore_check_constraints=ON")
        hs._conn.execute("UPDATE fobs SET tier='vip' WHERE uid='0DDBA110'")
        hs._conn.commit()
        hs._conn.execute("PRAGMA ignore_check_constraints=OFF")
    clock = [1_000_000.0]       # the wait's own clock: no check sleeps
    real_pa = eng.player_action

    def tap1(uid):
        return tap_at(eng, "M1", uid, now=clock[0], smib="smib-M1")

    def tap_other(egm, uid):
        return tap_at(eng, egm, uid, now=clock[0])

    def arm(aid, tok=None):
        return eng.glass_admin_action(
            {"sess": tok or tok_of(eng, "M1"), "action": "plAddFob",
             "accountId": aid}, clock[0])

    def cancel(tok=None):
        return adm(eng, tok or tok_of(eng, "M1"), "plAddFobCancel")

    def wait(tok=None):
        return eng.glass_data(tok or tok_of(eng, "M1"), "players",
                              clock[0])[1].get("fobWait")

    def held():
        return sorted(eng._fob_waits or {})

    def waiting(left, note="", aid=sam, name="Sam"):
        return {"state": "waiting", "secondsLeft": left, "accountId": aid,
                "name": name, "note": note, "msg": ""}

    def over(state, msg, aid=sam, name="Sam"):
        return {"state": state, "secondsLeft": 0, "accountId": aid,
                "name": name, "note": "", "msg": msg}

    def last_tap(egm="M1"):
        return (eng.companion_snapshot().get("companion-" + egm)
                or {}).get("lastTap") or {}

    def card(egm="M1"):
        return eng.card_sessions.get(egm) or {}

    def owner(uid):
        return (hs.fob_get(uid) or {}).get("accountId")

    def not_known(uid):
        return hs.fob_get(uid) is None and last_tap().get("uid") == uid \
            and last_tap().get("known") is False

    tap1("A1A00001")                    # AJ cards in at M1
    tap_other("M2", "0FF1CE02")         # a fob with no player, at Bluebird 2
    tap_other("M3", "C4150001")         # Christopher at M3
    tok = tok_of(eng, "M1")
    check("the cast: an admin carded in at M1 beside its reader, a fob "
          "with no player carded in at Bluebird 2, a player at M3",
          eng.glass_state("M1").get("admin") is True
          and card("M2").get("name") == "Player"
          and card("M3").get("name") == "Christopher"
          and (eng.companions["companion-M1"]["bindings"]["g2sEgmId"],
               eng.companions["companion-M1"]["readerOk"]) == ("M1", True))

    print("— ADD FOB: when the reader is not armed")
    before = books(eng)
    for label, who in (("the House", "house"), ("an unknown id", "p999"),
                       ("an id that is not text", 7), ("no id", None)):
        st, r = arm(who)
        check(f"ADD FOB for {label}: that player is not there any more",
              (st, r.get("error"), r.get("ok")) == (200, GONE, False)
              and "fobWait" not in r, (st, r))
    st, r = arm(pia)
    check("ADD FOB for an admin's account is refused: a screen must not "
          "make a new admin fob",
          (st, r) == (200, {"ok": False, "action": "plAddFob", "egmId": "M1",
                            "error": "Pia is an admin. Add an admin's fob "
                                     "on the Players web page."}), (st, r))
    no_reader = "This machine's fob reader is not answering."
    comp = eng.companions["companion-M1"]
    seen = comp["lastSeen"]
    comp["lastSeen"] = seen - 25
    st, r = arm(sam)
    check("this machine's reader last reported 25 seconds ago: the reader "
          "is not answering",
          (st, r.get("error"), r.get("ok")) == (200, no_reader, False),
          (st, r))
    comp["lastSeen"] = time.time() - 15
    st, r = arm(sam)
    cancel()
    comp["lastSeen"] = seen
    check("(one that reported 15 seconds ago is answering)",
          (st, r.get("ok")) == (200, True), (st, r))
    report(eng, comp="companion-M1", g2s="M1", smib="smib-M1",
           reader_ok=False)
    st, r = arm(sam)
    check("this machine's reader says its fob reader is not working: the "
          "reader is not answering",
          (st, r.get("error"), r.get("ok")) == (200, no_reader, False),
          (st, r))
    report(eng, comp="companion-M1", g2s="M1", smib="smib-M1")
    eng.pin_login("M4", "771234")
    pia_tok = tok_of(eng, "M4")
    st, r = arm(sam, pia_tok)
    check("an admin at a machine with no reader of its own (the readers "
          "of other machines are answering): the reader is not answering",
          eng.glass_data(pia_tok, "players")[0] == 200
          and (st, r.get("error"), r.get("ok")) == (200, no_reader, False),
          (st, r))
    st, r = arm(sam, tok_of(eng, "M3"))
    st2, r2 = cancel(tok_of(eng, "M3"))
    check("a player who is not an admin cannot arm or cancel: 403",
          (st, r.get("error")) == (403, "not authorized")
          and (st2, r2.get("error")) == (403, "not authorized"), (r, r2))
    tap_other("M3", "A77E0001")         # the floor key taps over Christopher
    st, r = arm(sam, tok_of(eng, "M3"))
    st2, r2 = cancel(tok_of(eng, "M3"))
    check("a tap-over cannot arm or cancel: 403",
          eng.admin_overlays.get("M3") is not None
          and (st, r.get("error")) == (403, "not authorized")
          and (st2, r2.get("error")) == (403, "not authorized"), (r, r2))
    tap_other("M3", "A77E0001")         # ...and hands the screen back
    check("none of those armed a reader or changed a fob", held() == []
          and books(eng) == before and wait() is None)
    was = floor(eng)
    tap1("0E0F0001")
    check("so a fob the hub has never seen, tapped at M1, is recorded and "
          "nothing else, as always", not_known("0E0F0001")
          and floor(eng) == was and books(eng) == before)

    print("— ADD FOB: armed")
    t0 = clock[0]
    st, r = arm(sam)
    check("ADD FOB for a player: ok, no sentence yet, and a wait with 20 "
          "seconds on the count",
          (st, r) == (200, {"ok": True, "fobWait": waiting(20),
                            "action": "plAddFob", "egmId": "M1"}), (st, r))
    st, r = Screen(eng)._handle_glass_admin(json.dumps(
        {"sess": tok, "action": "plAddFobCancel"}))
    st2, r2 = Screen(eng)._handle_glass_admin(json.dumps(
        {"sess": tok, "action": "plAddFob", "accountId": sam}))
    check("the same two actions through the screen's own POST handler",
          (st, r) == (200, {"ok": True, "fobWait": None,
                            "action": "plAddFobCancel", "egmId": "M1"})
          and st2 == 200
          and (r2.get("fobWait") or {}).get("state") == "waiting",
          (st, r, st2, r2))
    with eng.companion_lock:    # (the handler armed it on the wall clock)
        ((eng._fob_waits or {}).get("M1") or {})["armedTs"] = t0
    st, v = eng.glass_data(tok, "players", t0)
    check("the players view carries the wait for the screen that armed it, "
          "beside the players", st == 200 and v.get("ok") is True
          and v.get("fobWait") == waiting(20)
          and "Sam" in [p["name"] for p in v.get("players") or []],
          v.get("fobWait"))
    check("an admin at another machine reads no wait",
          eng.glass_data(pia_tok, "players", t0)[1].get("fobWait", 0) is None)
    eng._glass_card_out("M4")           # Pia leaves
    before, was = books(eng), floor(eng)
    for dt, left in ((0.4, 20), (1, 19), (1.2, 19), (10, 10), (19.5, 1),
                     (20, 0), (21.9, 0)):
        clock[0] = t0 + dt
        check(f"{dt} seconds after arming the count reads {left}",
              wait() == waiting(left), wait())
    check("in all that time nobody was carded out and nothing was sent",
          floor(eng) == was)

    print("— ADD FOB: a tap on this machine's reader that is not a new fob")
    t0 = clock[0] = t0 + 100
    arm(sam)
    for label, uid, note in (
            ("the fob the admin carded in with", "A1A00001",
             "That is the fob you carded in with."),
            ("a reset fob", "DEADBEEF",
             "That is a reset fob. It can't be a player's fob."),
            ("an attendant's fob", "A77E0001",
             "That is a staff fob. It can't be a player's fob."),
            ("a fob of a kind this build does not know", "0DDBA110",
             "That is a staff fob. It can't be a player's fob."),
            ("another player's fob", "DA4A0001",
             "That fob belongs to Dana. Remove it from Dana first."),
            ("the fob of a player who is carded in at another machine",
             "C4150001", "That fob belongs to Christopher. Remove it from "
                         "Christopher first."),
            ("a fob with no player that is carded in at another machine",
             "0FF1CE02", "That fob is carded in at Bluebird 2. Card out "
                         "there first.")):
        clock[0] += 1
        tap1(uid)
        check(f"{label} is not taken: {note}",
              wait() == waiting(20 - int(clock[0] - t0), note), wait())
        check("...the wait goes on, nobody is carded in or out, no tap-over "
              "is raised, nothing is sent to a machine or queued for one, "
              "and no fob has changed hands",
              floor(eng) == was and books(eng) == before
              and tok_of(eng, "M1") == tok and last_tap().get("uid") == uid,
              (floor(eng), was))
    with eng.companion_lock:    # by hand: its account was deleted since
        eng.admin_overlays["M4"] = {"uid": "0FF1CE01", "name": "Player",
                                    "tier": "player", "tok": "gs_none"}
    tap1("0FF1CE01")
    with eng.companion_lock:
        eng.admin_overlays.pop("M4")
    check("the fob of a tap-over that is up at another machine is not "
          "taken: That fob is carded in at M4. Card out there first.",
          wait() == waiting(20 - int(clock[0] - t0), "That fob is carded in "
                            "at M4. Card out there first.")
          and floor(eng) == was and books(eng) == before, wait())
    clock[0] += 1
    tap1("0FF1CE01")
    check("then a player fob with no player is tapped: it is added, and "
          "the wait is over (Fob added for Sam.)",
          wait() == over("linked", "Fob added for Sam.")
          and owner("0FF1CE01") == sam
          and floor(eng) == was, wait())
    check("the players view shows the new fob on the player's row, by its "
          "last 6 characters",
          [f["tail"] for p in eng.glass_data(tok, "players", clock[0])[1]
           .get("players") or [] if p["id"] == sam for f in p["fobs"]]
          == ["F1CE01"])

    print("— ADD FOB: the new fob tapped again straight away")
    linked_at = clock[0]
    clock[0] = linked_at + 3
    tap1("0FF1CE01")
    check("3 seconds after it was added, the new fob on this reader does "
          "nothing: the admin is still carded in with the same token, "
          "nothing is sent, and Sam is not carded in",
          floor(eng) == was and tok_of(eng, "M1") == tok
          and card().get("uid") == "A1A00001", floor(eng))
    tap_other("M4", "0FF1CE01")
    check("...on ANOTHER machine's reader it is an ordinary tap: Sam cards "
          "in there",
          card("M4").get("accountId") == sam
          and card("M4").get("uid") == "0FF1CE01"
          and card().get("uid") == "A1A00001")
    tap_other("M4", "0FF1CE01")         # ...and out again
    sent = len(eng.sent)
    tap1("0E0F0001")
    check("...and a fob the hub has never seen is still recorded and "
          "nothing else", not_known("0E0F0001")
          and card().get("uid") == "A1A00001"
          and len(eng.sent) == sent)
    clock[0] = linked_at + 4.9
    tap1("0FF1CE01")
    check("4.9 seconds after: still nothing",
          tok_of(eng, "M1") == tok and len(eng.sent) == sent
          and card().get("uid") == "A1A00001")
    clock[0] = linked_at + 6
    tap1("0FF1CE01")
    check("6 seconds after, it is a fob like any other: tapped here it is "
          "a card switch, the admin out and Sam in",
          card().get("uid") == "0FF1CE01"
          and card().get("accountId") == sam
          and tok_of(eng, "M1") != tok and len(eng.sent) == sent + 2
          and held() == [], card())
    tap1("0FF1CE01")                    # Sam out
    tap1("A1A00001")                    # AJ in again
    tok = tok_of(eng, "M1")

    print("— ADD FOB: fobs the hub has never seen, and when the hub cannot "
          "write")
    t0 = clock[0] = clock[0] + 100
    arm(sam)
    before, was = books(eng), floor(eng)
    tap1("0FF1CE01")
    check("a fob the player already has: That fob is already Sam's.",
          wait() == waiting(20, "That fob is already Sam's.")
          and books(eng) == before and floor(eng) == was, wait())
    real_set = hs.fob_set
    hs.fob_set = lambda *a, **k: (None, "fob registry full (256)")
    tap1("0E0F0002")
    check("the hub's fob list is full: the screen says so, and where to "
          "make room",
          wait() == waiting(20, "The fob list is full. Remove a fob on the "
                                "hub's web page first.")
          and floor(eng) == was, wait())
    hs.fob_set = lambda *a, **k: (None, "db write failed")
    tap1("0E0F0002")
    check("the hub cannot write the fob for another reason: That fob could "
          "not be added. Try again.",
          wait() == waiting(20, "That fob could not be added. Try again.")
          and floor(eng) == was, wait())
    hs.fob_set = real_set

    def boom(req, by=None):
        raise RuntimeError("link failed for " + str(req))
    eng.player_action = boom
    rec = Records()
    host_log = logging.getLogger("g2s_host")
    level = host_log.level
    host_log.addHandler(rec)
    host_log.setLevel(logging.DEBUG)
    logging.disable(logging.NOTSET)
    try:
        tap1("DA4A0001")
        tap1("0E0F0002")
        eng.player_action = real_pa
        check("a fault while adding the fob: the tap still does nothing "
              "else (no card-in, no card switch), the fob is not added, "
              "and the wait goes on",
              floor(eng) == was and books(eng) == before
              and (wait() or {}).get("state") == "waiting", wait())
        clock[0] += 2
        tap1("0e:0f:00:02")
        new = hs.fob_get("0E0F0002") or {}
        check("a fob the hub has never seen is registered as a player fob "
              "and added, in one go",
              wait() == over("linked", "Fob added for Sam.")
              and (new.get("tier"), new.get("accountId")) == ("player", sam)
              and floor(eng) == was, (wait(), new))
        check("...and the tap that added it counts as its first: the fob "
              "does not read as never used", bool(new.get("lastSeen")), new)
        clock[0] += 1
        tap1("0E0F0002")                # tapped again: ignored
        arm(sam)
        cancel()
        st, r = arm(sam)
    finally:
        eng.player_action = real_pa
        logging.disable(logging.CRITICAL)
        host_log.removeHandler(rec)
        host_log.setLevel(level)
    by = " — by AJ at M1 (admin menu)"
    check("the hub log has each result with the fob, the player and who "
          "did it at which machine",
          any(ln.endswith("[M1] ADD FOB: fob DA4A0001 not taken for Sam "
                          f"({sam}): That fob belongs to Dana. Remove it "
                          "from Dana first." + by) for ln in rec.lines)
          and any(ln.endswith(f"[M1] ADD FOB: fob 0E0F0002 added for Sam "
                              f"({sam})" + by) for ln in rec.lines)
          and any(f"fob 0E0F0002 linked to {sam} (Sam)" in ln
                  and ln.endswith(by) for ln in rec.lines)
          and any(ln.endswith("[M1] fob 0E0F0002 tapped again just after "
                              "it was added: ignored") for ln in rec.lines),
          rec.lines)
    check("arming and CANCEL are logged with the player and who did it",
          sum(ln.endswith(f"[M1] ADD FOB armed for Sam ({sam})" + by
                          + "; waiting 20 s for a tap")
              for ln in rec.lines) == 2
          and sum(ln.endswith(f"[M1] ADD FOB for Sam ({sam}) cancelled"
                              + by) for ln in rec.lines) == 1, rec.lines)
    check("a fault is logged by its class, never its text, and no session "
          "token is in the log",
          any(ln.endswith("[M1] ADD FOB: the tap of fob 0E0F0002 failed on "
                          "the hub side (RuntimeError)") for ln in rec.lines)
          and not any("link failed" in ln or tok in ln for ln in rec.lines),
          rec.lines)
    check("no whole fob uid is in anything the screen reads",
          not any(f["uid"] in json.dumps([r, eng.glass_data(
              tok, "players", clock[0])[1]]) for f in hs.fobs()))
    clock[0] += 2
    tap1("DA4A0001")
    check("with a new wait armed, another player's fob is still refused "
          "and nothing moves",
          wait() == waiting(18, "That fob belongs to Dana. Remove it from "
                                "Dana first.") and floor(eng) == was, wait())
    tap1("0FF1CE03")
    check("a fob whose player was deleted is added like a fob with no "
          "player", wait() == over("linked", "Fob added for Sam.")
          and owner("0FF1CE03") == sam, wait())
    clock[0] += 2
    tap1("DA4A0001")
    check("another player's fob in the 5 seconds after that is an ordinary "
          "tap: a card switch, the admin out and Dana in",
          card().get("uid") == "DA4A0001" and held() == [],
          card())
    tap1("DA4A0001")                    # Dana out
    tap1("A1A00001")                    # AJ in again
    tok = tok_of(eng, "M1")

    print("— ADD FOB: the player goes, or becomes an admin, while the "
          "reader waits")
    for label, change in (("is deleted", store.delete),
                          ("is made an admin",
                           lambda aid: store.set_admin(aid, True))):
        zed = player(eng, "Zed")
        arm(zed)
        change(zed)
        was = floor(eng)
        tap1("0E0F0003")
        check(f"the player {label}: at the next tap the wait ends (That "
              "player is not there any more.) and the fob is not added",
              wait() == over("ended", GONE, zed, "Zed")
              and hs.fob_get("0E0F0003") is None and floor(eng) == was,
              wait())
        tap1("0E0F0003")
        check("...and the tap after that is an ordinary one",
              not_known("0E0F0003") and floor(eng) == was
              and wait() == over("ended", GONE, zed, "Zed"))

    print("— ADD FOB: the admin loses the right while the reader waits")
    arm(sam)
    store.set_admin(aj, False)
    before, was = books(eng), floor(eng)
    tap1("0E0F0003")
    check("the next tap is an ordinary tap: a fob the hub has never seen "
          "is recorded, and is NOT registered or added",
          not_known("0E0F0003") and books(eng) == before
          and floor(eng) == was)
    check("...and the hub holds no wait for the machine any more",
          held() == [])
    store.set_admin(aj, True)
    before = books(eng)
    tap1("0E0F0003")
    check("the right coming back does not bring the wait back",
          wait() is None and not_known("0E0F0003") and books(eng) == before)

    print("— ADD FOB: a tap on another machine's reader")
    t0 = clock[0] = clock[0] + 100
    arm(sam)
    before = books(eng)
    tap_other("M4", "0E0F0003")
    check("a fob the hub has never seen, tapped at another machine, is "
          "recorded there and nothing else",
          hs.fob_get("0E0F0003") is None
          and last_tap("M4").get("uid") == "0E0F0003"
          and last_tap("M4").get("known") is False and books(eng) == before)
    tap_other("M4", "DA4A0001")
    check("a player's fob tapped at another machine cards them in there, "
          "as always",
          card("M4").get("uid") == "DA4A0001"
          and books(eng) == before)
    tap_other("M4", "DEADBEEF")
    report(eng, taps=[tap(0, uid="0E0F0003")], comp="companion-hub",
           g2s=None)
    check("a tap on a reader that is bound to no machine (the hub's own) "
          "is recorded there and nothing else",
          hs.fob_get("0E0F0003") is None and books(eng) == before
          and eng.companion_snapshot()["companion-hub"]["lastTap"]["uid"]
          == "0E0F0003")
    check("...and none of it touched the wait at this machine",
          wait() == waiting(20) and tok_of(eng, "M1") == tok, wait())
    tap1("DA4A0001")
    check("(a fob carded in elsewhere, tapped here, is still refused by "
          "its owner's name)",
          wait() == waiting(20, "That fob belongs to Dana. Remove it from "
                                "Dana first."), wait())
    tap_other("M4", "DA4A0001")         # Dana out

    print("— ADD FOB: the 20 seconds, and the 2 more the hub allows")
    clock[0] = t0 + 19
    tap1("0E0F0003")
    check("19 seconds after arming, a tap adds the fob",
          wait() == over("linked", "Fob added for Sam.")
          and owner("0E0F0003") == sam, wait())
    t0 = clock[0] = clock[0] + 100
    arm(sam)
    clock[0] = t0 + 21
    check("21 seconds after arming the count reads 0 and the hub is still "
          "waiting", wait() == waiting(0), wait())
    tap1("0E0F0004")
    check("...and a tap then still adds the fob (it was made as the count "
          "ran out)", wait() == over("linked", "Fob added for Sam.")
          and owner("0E0F0004") == sam, wait())
    clock[0] = t0 + 21 + 59.9
    check("how a wait ended stays readable for a minute",
          wait() == over("linked", "Fob added for Sam."), wait())
    clock[0] = t0 + 21 + 60
    check("...and then it is gone", wait() is None and held() == [], held())
    t0 = clock[0] = clock[0] + 100
    arm(sam)
    before, was = books(eng), floor(eng)
    clock[0] = t0 + 23
    tap1("0E0F0005")
    check("23 seconds after arming, a tap is an ordinary tap: the fob is "
          "recorded, not added", not_known("0E0F0005")
          and books(eng) == before and floor(eng) == was)
    check("...and the wait reads ended: No fob was tapped.",
          wait() == over("ended", "No fob was tapped."), wait())
    t0 = clock[0] = clock[0] + 100
    arm(sam)
    clock[0] = t0 + 22
    check("with no tap at all, the wait reads ended from 22 seconds on",
          wait() == over("ended", "No fob was tapped."), wait())
    clock[0] = t0 + 22 + 59.9
    still = wait()
    clock[0] = t0 + 22 + 60
    check("...for a minute, then it is gone",
          still == over("ended", "No fob was tapped.") and wait() is None
          and held() == [], (still, held()))

    print("— ADD FOB: CANCEL, a second ADD FOB, and the admin leaving")
    arm(sam)
    st, r = cancel()
    check("CANCEL: ok, and the wait is gone at once",
          (st, r) == (200, {"ok": True, "fobWait": None,
                            "action": "plAddFobCancel", "egmId": "M1"})
          and wait() is None and held() == [], (st, r))
    tap1("0E0F0005")
    check("...so the next tap is an ordinary tap", not_known("0E0F0005")
          and books(eng) == before and floor(eng) == was)
    st, r = cancel()
    check("CANCEL with nothing armed is fine",
          (st, r.get("ok"), r.get("fobWait", 0)) == (200, True, None), r)
    arm(sam)
    st, r = arm(dana)
    check("a second ADD FOB replaces the first: one wait, for the player "
          "picked last", r.get("fobWait") == waiting(20, aid=dana,
                                                     name="Dana")
          and held() == ["M1"], r)
    tap1("0E0F0005")
    check("...and the fob tapped next goes to that player, not the first",
          wait() == over("linked", "Fob added for Dana.", dana, "Dana")
          and owner("0E0F0005") == dana, wait())
    st, r = cancel()
    clock[0] += 2
    was = floor(eng)
    tap1("0E0F0005")
    check("leaving the page right after a fob was added takes the result "
          "off the screen, and the new fob tapped again still does nothing",
          r.get("fobWait", 0) is None and wait() is None
          and floor(eng) == was and tok_of(eng, "M1") == tok, (r, wait()))

    def link_then_rearm(req, by=None):
        arm(dana)           # the admin picks another player at that moment
        return real_pa(req, by=by)
    arm(sam)
    eng.player_action = link_then_rearm
    tap1("0E0F0006")
    eng.player_action = real_pa
    check("a result is written only to the wait the tap belonged to: one "
          "armed in the meantime is not marked done",
          wait() == waiting(20, aid=dana, name="Dana")
          and owner("0E0F0006") == sam, wait())
    cancel()

    lock = eng.companion_lock = WatchedLock()
    seen_at = []
    real_get, real_live = store.get, eng.glass_sessions.still_live
    arm(sam)
    eng.player_action = lambda req, by=None: \
        seen_at.append(("link", lock.held)) or real_pa(req, by=by)
    store.get = lambda aid: seen_at.append(("account", lock.held)) \
        or real_get(aid)
    eng.glass_sessions.still_live = lambda egm, t: \
        seen_at.append(("live", lock.held)) or real_live(egm, t)
    tap1("0E0F0007")
    eng.player_action, store.get = real_pa, real_get
    eng.glass_sessions.still_live = real_live
    eng.companion_lock = threading.Lock()
    check("the wait and \"is its token still the machine's live one\" are "
          "read in one lock hold; the accounts are read and the fob is "
          "linked outside it",
          ("live", True) in seen_at and ("link", False) in seen_at
          and ("account", False) in seen_at
          and all(not h for what, h in seen_at if what != "live")
          and owner("0E0F0007") == sam, seen_at)

    before = books(eng)
    for label, leave in (
            ("the admin presses LOG OUT",
             lambda: act(eng, tok_of(eng, "M1"), "logout")),
            ("the admin is logged out from the hub's web page",
             lambda: eng.card_out(eng.associations["M1"])),
            ("someone logs in at the machine by PIN",
             lambda: eng.pin_login("M1", "771234"))):
        arm(sam)
        armed = held()
        leave()
        check(f"{label} while the reader waits: the wait ends with the "
              "card session", armed == ["M1"] and held() == []
              and card().get("uid")
              != "A1A00001", (armed, held()))
        if eng.card_sessions.get("M1"):
            eng._glass_card_out("M1")
        was = floor(eng)
        tap1("0E0F0008")
        check("...and the next tap is an ordinary tap",
              not_known("0E0F0008") and books(eng) == before
              and floor(eng) == was)
        tap1("A1A00001")                # AJ in again
    tok = tok_of(eng, "M1")
    arm(sam)
    tok2 = eng.glass_sessions.mint("M1", "player", "A1A00001", "AJ")
    st, r = cancel(tok2)
    check("a wait belongs to the token that armed it: another token on "
          "the machine reads none, and cannot cancel it",
          eng.glass_data(tok2, "players", clock[0])[0] == 200
          and wait(tok2) is None and r.get("ok") is True
          and held() == ["M1"], (wait(tok2), held()))
    was = floor(eng)
    tap1("0E0F0008")
    check("the token that armed the wait is no longer the machine's live "
          "one: the next tap is an ordinary tap and the wait is dropped",
          not_known("0E0F0008") and books(eng) == before
          and floor(eng) == was and held() == [])

    print("— ADD FOB: after all of that, taps do what they always did")
    eng._glass_card_out("M1")
    sent = len(eng.sent)
    tap1("0E0F0008")
    check("a fob the hub has never seen: recorded, NOTHING else",
          not_known("0E0F0008") and eng.card_sessions.get("M1") is None
          and len(eng.sent) == sent and books(eng) == before)
    tap1("DA4A0001")
    check("a player's fob: card IN",
          card().get("uid") == "DA4A0001"
          and card().get("accountId") == dana
          and len(eng.sent) == sent + 1
          and eng.sent[-1][1].startswith("setIdValidation(present=True"))
    tap1("A1A00001")
    check("an admin's fob over a carded player: a tap-over, the player "
          "stays carded in",
          (eng.admin_overlays.get("M1") or {}).get("uid") == "A1A00001"
          and card().get("uid") == "DA4A0001"
          and len(eng.sent) == sent + 1)
    tap1("0FF1CE01")
    check("any tap hands the screen back",
          eng.admin_overlays.get("M1") is None
          and card().get("uid") == "DA4A0001")
    tap1("0FF1CE01")
    check("another player's fob (the one added for Sam): a card switch, "
          "out then in",
          card().get("uid") == "0FF1CE01"
          and card().get("name") == "Sam"
          and len(eng.sent) == sent + 3
          and eng.sent[-2][1].startswith("setIdValidation(present=False")
          and eng.sent[-1][1].startswith("setIdValidation(present=True"))
    tap1("0FF1CE01")
    check("the same fob again: card OUT",
          eng.card_sessions.get("M1") is None and tok_of(eng, "M1") is None
          and len(eng.sent) == sent + 4)
    tap1("DEADBEEF")
    q = list(eng.sas_commands.get("smib-M1") or [])
    check("a reset fob: one handpay reset queued for the machine's SAS "
          "side (none was queued while the reader was armed)",
          [c.get("type") for c in q] == ["handpay_reset"]
          and len(eng.sent) == sent + 4, q)
    check("...and nothing in this section moved any money",
          all(e.get("note") == "seed" for e in rows(eng))
          and cash(eng, sam) == 5000000)


def admin_menus():
    admin_menu_callers()
    admin_menu_service()
    admin_menu_logs()       # runs the money, PIN, name and fob checks first
    admin_menu_quick_amounts()
    admin_menu_add_fob()


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
    # the restricted promotional balance ("Free Slot Play" on the player's
    # screen) rides the same poll, only when the player has some
    check("no restricted balance = no restrictedMc on the glass state",
          "restrictedMc" not in st2)
    eng.account_store.accounts["p1"]["nonCashMillicents"] = 15000000
    st2r = eng.glass_state(EGM)
    check("a restricted balance rides the glass state beside the credits",
          st2r.get("restrictedMc") == 15000000 and
          st2r.get("credits") == "$0.00")

    print("— admin overlay: a genuine card-out drops the overlay too")
    report(eng, taps=[tap(5, uid="AA11BB22")])       # admin overlays AJ again
    check("overlay back up", (eng.admin_overlays.get(EGM) or {}).get("uid")
          == "AA11BB22")
    check("the overlay hides the friend's restricted balance with the credits",
          "restrictedMc" not in eng.glass_state(EGM))
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
    # v0.3.27, the linked-machine rule: the session is the hub's, and a
    # machine with a SAS link is sent no card push over G2S.
    check("the tap cards a player in on the auto-bound machine, with no "
          "card push over G2S (it has a SAS link)",
          EGM in enge.card_sessions and enge.sent == [])

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

    print("— SMIB player screen: its reload stamp rides the poll it reads")
    # smib.html has no keyboard, so it reloads itself (while idle) when the
    # stamp in ITS OWN poll changes. Until 2026-11-17 the hub only put the
    # stamp on /api/status, which that page never reads, so an update did
    # not reach the screen until its browser was restarted. Both ends are
    # pinned here: the name the page watches, and the reply carrying it.
    smib_page = open(os.path.join(G2S_DIR, "webui", "smib.html"),
                     encoding="utf-8").read()
    check("smib.html polls /api/glass/state and watches s.smibStamp",
          "'/api/glass/state?egm='" in smib_page and
          "s.smibStamp !== lastSmibStamp" in smib_page)
    check("glass_state carries smibStamp = smib.html's own mtime",
          gst.get("smibStamp") == int(os.path.getmtime(
              os.path.join(G2S_DIR, "webui", "smib.html"))) and
          gst.get("smibStamp") == gh.smib_ui_stamp())
    _real_stamp = gh.smib_ui_stamp
    gh.smib_ui_stamp = lambda: (_ for _ in ()).throw(OSError("gone"))
    try:
        gst4 = engG.glass_state(EGM)
    finally:
        gh.smib_ui_stamp = _real_stamp
    check("a missing page just drops the stamp (poll never 500s)",
          gst4.get("ok") is True and "smibStamp" not in gst4)

    devices_fold()
    glass_window_picker()
    admin_menus()

    print(f"\nRESULT: {_p} passed, {_f} failed")
    return 1 if _f else 0


if __name__ == "__main__":
    sys.exit(main())
