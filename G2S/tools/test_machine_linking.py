#!/usr/bin/env python3
"""
test_machine_linking.py — standalone gate for dual-protocol machine linking
(task #20): the /api/settings write path + the wallet addCredits→SAS reroute,
against a REAL temp-file HubStore (schema v9 sas_link column).

In-process, no live host, no sockets, stdlib only — lightweight fakes
(FakeAssoc / FakeAccounts, the test_companion_rfid.py pattern) stand in for
the association and the money-adjacent stores; hub.db is the real thing so
the link durability under test is the production SQL, not a fake's opinion
of it.

(The hold/settlement/burst-guard slices this gate used to carry were REMOVED
2026-07-15 with the take-tracking feature itself — collector de-cage. What
remains is linking's living surface: the link write, its strictness, the
one-owner rule, and the money reroute.)

Covers:
  * link via /api/settings {machineKey, sasLink} — 200 + echo, durable in
    hub.db, in-memory map updated.
  * settings strictness — non-string sasLink, missing machineKey, and the
    preserved machineKey-alone 400 (the sasEnabled branch's exact error).
  * one leg, ONE owner — a second G2S card claiming an already-linked
    SAS key answers 400 (naming the owner) and writes nothing; the owner
    re-posting its own link stays a 200.
  * empty-string sasLink clears (echoes null).
  * degraded store — a hub.db write fault surfaces as set_sas_link's own
    raise -> 400 (never a silent 200), link state untouched.
  * /api/command addCredits on a SAS-linked machine routes over the SAS
    leg (aft_transfer in CENTS, House-funded by default, player overdraft
    courtesy-refused, promo/sub-cent refused, parked leg -> 409).

Run from G2S/:  python3 tools/test_machine_linking.py
Must end "RESULT: N passed, 0 failed".
"""

import json
import logging
import os
import sys
import tempfile
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import g2s_host as gh  # noqa: E402
from hub_store import HubStore  # noqa: E402

logging.disable(logging.CRITICAL)   # the checks print, the engine doesn't

EGM = "WMS_00:a0:a5:79:2d:a8"       # the BB2's real egmId (never a theme)
SAS = "smib-bb2/1"                  # its SAS leg (satellite key)

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
    """Just enough association for the engine maps the handlers touch."""

    def __init__(self, egm_id=EGM):
        self.egm_id = egm_id
        self.lock = threading.Lock()
        self.meters = {}
        self.comms_state = "onLine"
        self.last_meter_report = "2026-07-10T00:00:00Z"


class FakeAccounts:
    """The AccountStore surface the linked addCredits reroute touches:
    get (records dict — house preseeded) + adjust (recorded)."""

    def __init__(self):
        self.calls = []                 # (account_id, d_cash, ref)
        self.balance = 0
        self.records = {"house": {"id": "house", "kind": "house",
                                  "name": "The House",
                                  "cashableMillicents": 0}}

    def adjust(self, account_id, d_cash=0, d_promo=0, d_non=0, note="",
               ref="", once=False):
        self.calls.append((account_id, int(d_cash), ref))
        self.balance += int(d_cash)
        return {"cashableMillicents": self.balance}, None

    def snapshot(self):
        return {"accounts": []}

    def get(self, account_id):
        return self.records.get(account_id)


def make_engine():
    """A G2SHost carrying ONLY the linking/settings/command surface —
    __new__ sidesteps the constructor's real stores/files; hub.db is a
    REAL HubStore in a temp dir (link durability is the thing under
    test)."""
    eng = gh.G2SHost.__new__(gh.G2SHost)
    d = tempfile.mkdtemp()
    eng.hub_store = HubStore(os.path.join(d, "hub.db"))
    eng.account_store = FakeAccounts()
    eng.associations = {}
    eng.assoc_lock = threading.Lock()
    eng.sas_machines = {}
    eng.sas_lock = threading.Lock()
    eng.sas_links = eng.hub_store.sas_links()   # the startup read
    eng.sat_bindings = {}                       # v11 satellite assignments
    eng._sat_write_lock = threading.Lock()
    return eng


def settings(eng, payload):
    """Drive the REAL /api/settings handler method with _send captured —
    returns (http_code, decoded_body)."""
    h = gh.G2SRequestHandler.__new__(gh.G2SRequestHandler)
    h.host_engine = eng
    sent = []
    h._send = lambda code, body, ctype=None, soap=True: \
        sent.append((code, json.loads(body)))
    h._handle_settings(json.dumps(payload))
    return sent[0]


def command(eng, payload):
    """Drive the REAL /api/command handler method with _send captured —
    returns (http_code, decoded_body). The linked-addCredits reroute fires
    BEFORE the assoc/dispatch machinery, so the fake engine suffices."""
    h = gh.G2SRequestHandler.__new__(gh.G2SRequestHandler)
    h.host_engine = eng
    sent = []
    h._send = lambda code, body, ctype=None, soap=True: \
        sent.append((code, json.loads(body)))
    h._handle_command(json.dumps(payload))
    return sent[0]


def main():
    eng = make_engine()
    a = FakeAssoc()
    eng.associations[EGM] = a

    print("— link via /api/settings")
    code, body = settings(eng, {"machineKey": EGM, "sasLink": SAS})
    check("write answers 200 and echoes the pair",
          code == 200 and body.get("machineKey") == EGM and
          body.get("sasLink") == SAS, body)
    check("in-memory map updated (single writer)",
          eng.sas_links == {EGM: SAS}, eng.sas_links)
    check("durable in hub.db (machine_prefs carries sasLink)",
          (eng.hub_store.machine_prefs(EGM) or {}).get("sasLink") == SAS)

    print("— unlink")
    code, body = settings(eng, {"machineKey": EGM, "sasLink": None})
    check("unlink answers 200 and echoes sasLink null",
          code == 200 and body.get("sasLink") is None, body)
    check("map cleared + hub.db cleared",
          eng.sas_links == {} and
          (eng.hub_store.machine_prefs(EGM) or {}).get("sasLink") is None)

    print("— settings strictness (400-never-500, old errors preserved)")
    for payload, why in (
            ({"machineKey": EGM, "sasLink": 123}, "non-string sasLink"),
            ({"machineKey": EGM, "sasLink": True}, "bool sasLink"),
            ({"sasLink": SAS}, "missing machineKey"),
            ({"machineKey": ""}, "empty machineKey")):
        code, body = settings(eng, payload)
        check(f"{why} -> 400", code == 400 and body.get("ok") is False,
              (code, body))
    code, body = settings(eng, {"machineKey": EGM})
    check("machineKey alone keeps today's exact 400 (sasEnabled branch)",
          code == 400 and "sasEnabled" in body.get("error", ""), body)
    check("rejected writes never touch the map", eng.sas_links == {})
    code, body = settings(eng, {"machineKey": EGM, "sasEnabled": True,
                                "sasLink": SAS})
    check("sasEnabled + sasLink ride one write (both echoed)",
          code == 200 and body.get("sasEnabled") is True and
          body.get("sasLink") == SAS, body)

    print("— uniqueness: one SAS leg, ONE owner")
    # Two G2S cards claiming the same leg would double-face that leg's
    # credits (two tiles, one machine) — the floor double-count linking
    # exists to kill. Second claimant is refused before anything is written.
    code, body = settings(eng, {"machineKey": "IGT_00012E492815",
                                "sasLink": SAS})
    check("second claimant of a linked leg -> 400 naming the owner",
          code == 400 and EGM in body.get("error", ""), (code, body))
    check("refused claim wrote NOTHING (map + hub.db untouched)",
          eng.sas_links == {EGM: SAS} and
          (eng.hub_store.machine_prefs("IGT_00012E492815") or {}
           ).get("sasLink") is None, eng.sas_links)
    code, body = settings(eng, {"machineKey": EGM, "sasLink": SAS})
    check("the owner re-posting its own link stays a 200 (idempotent)",
          code == 200 and body.get("sasLink") == SAS, (code, body))

    code, body = settings(eng, {"machineKey": EGM, "sasLink": ""})
    check('empty-string sasLink clears too (echoes null)',
          code == 200 and body.get("sasLink") is None and
          eng.sas_links == {}, body)

    print("— degraded store: a hub.db write fault REFUSES the write")
    # set_sas_link raising (dying-SD tier / db fault) must surface as a
    # 400 with the link state untouched — never a silent 200 and never a
    # half-applied map.
    settings(eng, {"machineKey": EGM, "sasLink": SAS})   # linked again
    real_set = eng.hub_store.set_sas_link

    def broken_set(key, link):
        raise ValueError("hub store degraded (bench-injected)")
    eng.hub_store.set_sas_link = broken_set
    try:
        code, body = settings(eng, {"machineKey": EGM, "sasLink": ""})
        check("unlink over a degraded store -> 400 (never a silent 200)",
              code == 400 and body.get("ok") is False, (code, body))
        check("refused write left the link fully intact (map + hub.db)",
              eng.sas_links == {EGM: SAS} and
              (eng.hub_store.machine_prefs(EGM) or {}
               ).get("sasLink") == SAS, eng.sas_links)
    finally:
        eng.hub_store.set_sas_link = real_set
    code, body = settings(eng, {"machineKey": EGM, "sasLink": ""})
    check("store restored -> the same unlink succeeds",
          code == 200 and body.get("sasLink") is None and
          eng.sas_links == {}, body)

    print("— /api/command addCredits: a SAS-linked machine routes over SAS")
    # AJ 2026-07-11: the wallet was pushing G2S WAT at machines whose money
    # authority is the linked SAS leg (the BB2's G2S wat is a dead stub) —
    # the hub edge now reroutes addCredits to the SAS aft_transfer path.
    settings(eng, {"machineKey": EGM, "sasLink": SAS})
    with eng.sas_lock:
        eng.sas_machines[SAS] = {"receivedAt": time.time(),
                                 "smibId": "smib-bb2",
                                 "meters": {"0x1A": 500}, "denomCents": 5,
                                 "sasEnabled": True}
    sent_cmds = []
    eng.sas_enqueue_command = lambda smib, cmd: (
        sent_cmds.append((smib, cmd)) or {"ok": True, "id": cmd["id"]})
    code, body = command(eng, {"action": "addCredits", "egmId": EGM,
                               "cashableMillicents": 500000})
    check("linked addCredits -> 200 path=sas: aft_transfer queued on the "
          "leg's smib in CENTS, funded by House",
          code == 200 and body.get("ok") is True
          and body.get("path") == "sas" and body.get("sasLink") == SAS
          and len(sent_cmds) == 1 and sent_cmds[0][0] == "smib-bb2"
          and sent_cmds[0][1]["type"] == "aft_transfer"
          and sent_cmds[0][1]["cents"] == 500
          and sent_cmds[0][1]["accountId"] == "house", (code, body))
    code, body = command(eng, {"action": "addCredits", "egmId": EGM,
                               "cashableMillicents": 1000,
                               "promoMillicents": 1000})
    check("promo bucket refused (SAS AFT moves cashable only) — nothing "
          "queued",
          code == 400 and body.get("ok") is False and len(sent_cmds) == 1,
          (code, body))
    code, body = command(eng, {"action": "addCredits", "egmId": EGM,
                               "cashableMillicents": 1500})
    check("sub-cent amount refused (1500 mc is not a whole cent)",
          code == 400 and len(sent_cmds) == 1, (code, body))
    eng.account_store.records["p9"] = {
        "id": "p9", "kind": "player", "name": "Bench Player",
        "cashableMillicents": 100000}
    code, body = command(eng, {"action": "addCredits", "egmId": EGM,
                               "accountId": "p9",
                               "cashableMillicents": 500000})
    check("player overdraft courtesy-refused at the hub edge",
          code == 400 and "can't fund" in str(body.get("error"))
          and len(sent_cmds) == 1, (code, body))
    code, body = command(eng, {"action": "addCredits", "egmId": EGM,
                               "accountId": "p9",
                               "cashableMillicents": 100000})
    check("player push within balance queues with the accountId echoed "
          "(the settle debit's key)",
          code == 200 and len(sent_cmds) == 2
          and sent_cmds[-1][1]["accountId"] == "p9"
          and sent_cmds[-1][1]["cents"] == 100, (code, body))
    settings(eng, {"machineKey": SAS, "sasEnabled": False})
    code, body = command(eng, {"action": "addCredits", "egmId": EGM,
                               "cashableMillicents": 1000})
    check("parked leg refuses 409 at the hub edge (C5 parity)",
          code == 409 and len(sent_cmds) == 2, (code, body))
    settings(eng, {"machineKey": SAS, "sasEnabled": True})
    settings(eng, {"machineKey": EGM, "sasLink": None})

    print(f"\nRESULT: {_p} passed, {_f} failed")
    sys.exit(1 if _f else 0)


if __name__ == "__main__":
    main()
