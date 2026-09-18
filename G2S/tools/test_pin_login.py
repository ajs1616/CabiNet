#!/usr/bin/env python3
"""PIN login gate (2026-09-14): the carded session WITHOUT a fob.

Covers the AccountStore PIN surface (shape, uniqueness, house refusal, the
hash never leaving the store), pin_login (card-IN with the PIN uid, same-
PIN = menu back, another PIN = switch, admin gated on pin_admin_allowed,
the per-machine throttle) and the Player Maintenance setPin/clearPin
actions. Reuses test_companion_rfid's lean engine + fakes; the account
store is the REAL AccountStore on a temp file.
Must end "RESULT: N passed, 0 failed".
"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import g2s_host as gh                                          # noqa: E402
import test_companion_rfid as tc                               # noqa: E402
from test_companion_rfid import (FakeAssoc, EGM, attr,         # noqa: E402
                                 make_engine)

_p = _f = 0


def check(name, ok, detail=""):
    global _p, _f
    if ok:
        _p += 1
        print(f"  ✅ {name}")
    else:
        _f += 1
        print(f"  ❌ {name} {detail}")


def main():
    global _p, _f
    tmp = tempfile.mkdtemp(prefix="cabinet-pin-")
    path = os.path.join(tmp, "account_state.json")
    store = gh.AccountStore(path)

    print("— AccountStore: PIN shape, uniqueness, house, the hash never leaves")
    a, _ = store.create("AJ")
    b, _ = store.create("Bo")
    check("no PIN on create: hasPin False, no pinHash key",
          a.get("hasPin") is False and "pinHash" not in a)
    check("any_pin False on a fresh store", store.any_pin() is False)
    check("find_by_pin on a fresh store: None and no secret minted",
          store.find_by_pin("123456") is None
          and not store.state.get("pinSecret"))
    for bad, why in (("12ab56", "digits"), ("12345", "at least 6"),
                     ("123456789", "at most 8"), (123456, "digits"),
                     ("", "digits")):
        r, e = store.set_pin(a["id"], bad)
        check(f"set_pin refuses {bad!r} ({why})",
              r is None and why in (e or ""), e)
    r, e = store.set_pin(a["id"], "1234", 4)
    check("4 digits allowed when the floor minimum is 4",
          e is None and r["hasPin"] is True)
    r, e = store.set_pin(a["id"], "246810")
    check("set_pin ok: hasPin True, pinHash absent from the copy",
          e is None and r["hasPin"] is True and "pinHash" not in r)
    check("get() and snapshot() carry hasPin, never the hash",
          store.get(a["id"])["hasPin"] is True
          and "pinHash" not in store.get(a["id"])
          and all("pinHash" not in x for x in store.snapshot()["accounts"])
          and store.get("house")["hasPin"] is False)
    r, e = store.set_pin(b["id"], "246810")
    check("the same PIN on another account is refused (PIN = identity)",
          r is None and "already in use" in (e or ""), e)
    r, e = store.set_pin("house", "246810")
    check("the house never gets a PIN", r is None and "bank" in (e or ""), e)
    r, e = store.set_pin("p99", "246810")
    check("unknown account refused", r is None and "unknown" in (e or ""), e)
    check("find_by_pin resolves AJ",
          (store.find_by_pin("246810") or {}).get("id") == a["id"])
    check("find_by_pin: wrong / malformed PIN -> None",
          store.find_by_pin("246811") is None
          and store.find_by_pin("24681x") is None
          and store.find_by_pin(None) is None)
    disk = open(path, encoding="utf-8").read()
    check("on disk: a hash + a secret, never the digits",
          "246810" not in disk and '"pinHash"' in disk and '"pinSecret"' in disk)
    again = gh.AccountStore(path)
    check("a reload still resolves the PIN (secret persisted)",
          (again.find_by_pin("246810") or {}).get("id") == a["id"])
    r, e = store.set_pin(a["id"], "135790")
    check("changing the PIN replaces it",
          e is None and store.find_by_pin("246810") is None
          and store.find_by_pin("135790")["id"] == a["id"])
    r, e = store.clear_pin(a["id"])
    check("clear_pin: hasPin False, any_pin False",
          e is None and r["hasPin"] is False and store.any_pin() is False)
    r, e = store.clear_pin(a["id"])
    check("clear_pin twice is fine", e is None and r["hasPin"] is False)
    store.set_pin(a["id"], "135790")

    print("— pin_login: the carded session without a fob")
    assoc = FakeAssoc()
    eng = make_engine(assoc=assoc)
    eng.account_store = store
    r = eng.pin_login(EGM, "135790", peer="10.10.10.231")
    check("login ok, greets by account name",
          r.get("ok") is True and r.get("name") == "AJ", r)
    check("ONE setIdValidation card-IN: idNumber = the PIN uid, player type,"
          " playerId = the account",
          len(eng.sent) == 1
          and eng.sent[0][1].startswith("setIdValidation(present=True")
          and attr(eng.sent[0][0], "idNumber") == "PINp1"
          and attr(eng.sent[0][0], "idType") == "G2S_player"
          and attr(eng.sent[0][0], "idPreferName") == "AJ"
          and attr(eng.sent[0][0], "playerId") == "p1")
    sess = eng.card_sessions.get(EGM) or {}
    check("session record: uid PINp1, accountId, via pin, no companion, "
          "tier player",
          sess.get("uid") == "PINp1" and sess.get("accountId") == "p1"
          and sess.get("via") == "pin" and sess.get("companionId") is None
          and sess.get("tier") == "player" and sess.get("deviceId") == "1")
    tok, rec = eng.glass_sessions.peek_egm(EGM)
    check("glass token minted for the PIN uid",
          bool(tok) and (rec or {}).get("uid") == "PINp1")
    st = eng.glass_state(EGM)
    check("glass_state: carded, name + credits resolved THROUGH the PIN uid",
          st.get("carded") is True and st.get("name") == "AJ"
          and st.get("creditsMc") == 0 and st.get("sess") == tok, st)
    check("glass_state carries the pinLogin hint (available, minDigits 6)",
          (st.get("pinLogin") or {}).get("available") is True
          and st["pinLogin"].get("minDigits") == 6)
    check("_session_account: PIN uid -> account; unknown PIN uid -> nothing",
          eng._session_account("PINp1")[0] == "p1"
          and eng._session_account("PINp9") == (None, None)
          and eng._session_account("") == (None, None))
    check("pin_uid helpers round-trip; a fob uid is not a PIN uid",
          gh.pin_uid("p1") == "PINp1" and gh.pin_uid_account("PINp1") == "p1"
          and gh.pin_uid_account("8C689B9E") is None
          and gh.pin_uid_account("PIN") is None)
    r = eng.pin_login(EGM, "135790")
    check("the same PIN again = menu back, session kept, NO second card-in",
          r.get("ok") is True and r.get("already") is True
          and len(eng.sent) == 1 and eng.card_sessions[EGM] is not None)
    store.set_pin(b["id"], "998877")
    r = eng.pin_login(EGM, "998877")
    check("another player's PIN = card-OUT then card-IN (a switch)",
          r.get("ok") is True and len(eng.sent) == 3
          and eng.sent[1][1].startswith("setIdValidation(present=False")
          and attr(eng.sent[2][0], "idNumber") == "PINp2"
          and eng.card_sessions[EGM]["name"] == "Bo", eng.sent[1][1])
    popped = eng._glass_card_out(EGM)
    check("the shared card-out ends a PIN session like any other",
          popped and popped.get("uid") == "PINp2"
          and EGM not in eng.card_sessions and len(eng.sent) == 4)
    check("_player_carded_map sees a PIN session by its stamped accountId",
          (eng.pin_login(EGM, "135790").get("ok")
           and eng._player_carded_map().get("p1") == EGM))
    eng._glass_card_out(EGM)

    print("— admin by PIN: never by default, only with pin_admin_allowed")
    store.set_admin(a["id"], True)
    r = eng.pin_login(EGM, "135790")
    check("admin-flagged account by PIN: NOT admin while the setting is off",
          r.get("ok") is True and eng.card_sessions[EGM]["admin"] is False
          and eng._session_is_admin("PINp1", "player") is False
          and eng.glass_state(EGM).get("admin") is False)
    eng._glass_card_out(EGM)
    eng.hub_store.settings["pin_admin_allowed"] = "1"
    r = eng.pin_login(EGM, "135790")
    check("… admin once pin_admin_allowed = 1 (account flag decides)",
          r.get("ok") is True and eng.card_sessions[EGM]["admin"] is True
          and eng.glass_state(EGM).get("admin") is True)
    check("… a non-admin account stays non-admin with the setting on",
          eng._session_is_admin("PINp2", "player") is False)
    eng._glass_card_out(EGM)
    eng.hub_store.settings.pop("pin_admin_allowed")
    store.set_admin(a["id"], False)

    print("— refusals + the per-machine throttle")
    r = eng.pin_login(EGM, "12345")
    check("shorter than the floor minimum (6) is refused before any lookup",
          r.get("ok") is False and "at least 6" in r["error"], r)
    r = eng.pin_login(EGM, "12ab56")
    check("non-digits refused", r.get("ok") is False and "digits" in r["error"])
    r = eng.pin_login("NOPE", "135790")
    check("unknown machine: 'offline', honest ok:false",
          r.get("ok") is False and "offline" in r["error"], r)
    eng.hub_store.settings["pin_min_digits"] = "4"
    r = eng.pin_login(EGM, "12345")
    check("floor minimum 4: a wrong 5-digit PIN counts as a try (4 left)",
          r.get("ok") is False and r["error"] == "wrong PIN"
          and r.get("triesLeft") == 4, r)
    eng.hub_store.settings.pop("pin_min_digits")
    for _ in range(3):
        eng.pin_login(EGM, "000000")
    r = eng.pin_login(EGM, "000000")
    check("the 5th wrong PIN in a row locks THIS machine's keypad for 60 s",
          r.get("ok") is False and r.get("retryAfterSec") == 60
          and "try again" in r["error"], r)
    r = eng.pin_login(EGM, "135790")
    check("even the right PIN is refused while locked",
          r.get("ok") is False and r.get("retryAfterSec"), r)
    check("another machine is NOT locked",
          "try again" not in (eng.pin_login("NOPE", "135790").get("error") or ""))
    eng._pin_lockouts[EGM]["until"] = 0
    r = eng.pin_login(EGM, "135790")
    check("after the lock the right PIN logs in and clears the strikes",
          r.get("ok") is True and EGM not in eng._pin_lockouts, r)
    eng._glass_card_out(EGM)
    for _ in range(5):
        r = eng.pin_login(EGM, "000000")
    check("strike 1 again = 60 s", r.get("retryAfterSec") == 60, r)
    eng._pin_lockouts[EGM]["until"] = 0
    for _ in range(5):
        r = eng.pin_login(EGM, "000000")
    check("strike 2 = 120 s (doubling)", r.get("retryAfterSec") == 120, r)
    eng._pin_lockouts.pop(EGM, None)

    print("— player_action: setPin / clearPin honour the floor minimum")
    c, _ = store.create("Cy")
    r = eng.player_action({"action": "setPin", "accountId": c["id"],
                           "pin": "4321"})
    check("setPin refuses 4 digits while the minimum is 6",
          r["ok"] is False and "at least 6" in r["error"], r)
    r = eng.player_action({"action": "setPin", "accountId": c["id"],
                           "pin": 4321})
    check("setPin refuses a non-string", r["ok"] is False)
    eng.hub_store.settings["pin_min_digits"] = "4"
    r = eng.player_action({"action": "setPin", "accountId": c["id"],
                           "pin": " 4321 "})
    check("setPin ok at minimum 4 (stripped); the reply carries hasPin only",
          r["ok"] is True and r["player"]["hasPin"] is True
          and "pinHash" not in r["player"], r)
    r = eng.player_action({"action": "setPin", "accountId": a["id"],
                           "pin": "4321"})
    check("a duplicate is refused through the API too",
          r["ok"] is False and "already in use" in r["error"], r)
    eng.hub_store.fobs = lambda: []      # the fake has no fobs table
    players = {p["id"]: p for p in eng.build_players()["players"]}
    check("GET /api/players: hasPin per row, never a hash",
          players["p3"]["hasPin"] is True and players["p2"]["hasPin"] is True
          and all("pinHash" not in p for p in players.values()))
    r = eng.player_action({"action": "clearPin", "accountId": c["id"]})
    check("clearPin", r["ok"] is True and r["player"]["hasPin"] is False)
    r = eng.player_action({"action": "clearPin", "accountId": "zz"})
    check("clearPin on an unknown account is refused", r["ok"] is False)
    r = eng.player_action({"action": "nope"})
    check("the unknown-action hint names setPin/clearPin",
          "setPin" in r["error"] and "clearPin" in r["error"])

    print("— pin_min_digits helper")
    eng.hub_store.settings["pin_min_digits"] = "4"
    check("4 when set to 4", gh.pin_min_digits(eng.hub_store) == 4)
    eng.hub_store.settings["pin_min_digits"] = "7"
    check("anything else reads as the default 6",
          gh.pin_min_digits(eng.hub_store) == 6)
    eng.hub_store.settings.pop("pin_min_digits")
    check("absent = 6", gh.pin_min_digits(eng.hub_store) == 6)

    print(f"\nRESULT: {_p} passed, {_f} failed")
    return 1 if _f else 0


if __name__ == "__main__":
    sys.exit(main())
