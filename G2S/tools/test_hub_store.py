#!/usr/bin/env python3
"""
test_hub_store.py — standalone gate for hub_store.HubStore (the SQLite spine).

In-process, no live host, stdlib only. Run:  python3 tools/test_hub_store.py
Must end "RESULT: N passed, 0 failed".
"""

import glob
import os
import sqlite3
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from hub_store import (FOB_TIERS, HubStore, MAX_DENOM_CENTS,  # noqa: E402
                       MAX_FOB_LABEL_LEN, MAX_FOBS, MAX_GAME_KEY_LEN,
                       MAX_GAME_NAMES, MAX_GAME_TITLE_LEN, MAX_MACHINES,
                       MAX_SATELLITES, MAX_SETTING_VAL_LEN,
                       MAX_TICKET_FIELD_LEN, SCHEMA_VERSION, _norm_fob_uid)

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
    d = tempfile.mkdtemp()
    path = os.path.join(d, "hub.db")
    hs = HubStore(path)

    print("— registry + names")
    hs.touch_machine("IGT_00012E492815", "G2S", {"vendor": "IGT",
                                                  "product": "AVP"})
    hs.touch_machine("smib-bb2/1", "SAS", {"port": "/dev/ttyAMA0"})
    check("no names until set", hs.names() == {}, hs.names())
    check("machines registered", len(hs.machines()) == 2)

    print("— set / get / clear")
    hs.set_nickname("IGT_00012E492815", "Corner cab")
    check("get_nickname returns the set name",
          hs.get_nickname("IGT_00012E492815") == "Corner cab")
    check("names() lists only named machines",
          hs.names() == {"IGT_00012E492815": "Corner cab"}, hs.names())
    hs.set_nickname("IGT_00012E492815", "")
    check("empty name clears the nickname",
          hs.get_nickname("IGT_00012E492815") is None)
    check("cleared name drops out of names()", hs.names() == {})

    print("— nickname on an unseen machine (pre-assign) + creation")
    hs.set_nickname("smib-bb1/3", "Bluebird One", "SAS")
    check("naming an unseen machine creates its row",
          hs.get_nickname("smib-bb1/3") == "Bluebird One")

    print("— nickname is preserved across touch_machine (operator-owned)")
    hs.set_nickname("smib-bb2/1", "Backup Bird")
    hs.touch_machine("smib-bb2/1", "SAS", {"port": "/dev/ttyAMA0",
                                           "address": "1"})
    check("touch_machine never clobbers the nickname",
          hs.get_nickname("smib-bb2/1") == "Backup Bird")

    print("— name length clamp + whitespace trim")
    hs.set_nickname("k", "  x" + "y" * 100 + "  ")
    check("name trimmed + clamped to 64",
          len(hs.get_nickname("k")) == 64 and
          hs.get_nickname("k").startswith("xy"))

    print("— hostile input bounds (unauth endpoint: caps + types)")
    try:
        hs.set_nickname("K" * 200, "x")
        check("oversize machine_key rejected", False)
    except ValueError:
        check("oversize machine_key rejected", True)
    try:
        hs.set_nickname("IGT_00012E492815", 123)
        check("non-string name rejected (no AttributeError)", False)
    except ValueError:
        check("non-string name rejected (no AttributeError)", True)
    hs.touch_machine("K" * 200, "SAS")            # must not raise, not insert
    check("touch_machine skips an oversize key",
          all(len(m["machine_key"]) <= 128 for m in hs.machines()))

    print("— registry cap: stalest UNNAMED row evicted, NAMED never")
    for i in range(MAX_MACHINES):                 # fill the registry to cap
        hs.touch_machine(f"cap-{i}", "SAS")
    check("registry stays capped", len(hs.machines()) == MAX_MACHINES)
    hs.set_nickname("newcomer", "New kid")        # evicts stalest unnamed
    check("naming a NEW key at cap evicts an unnamed row (no lockout)",
          hs.get_nickname("newcomer") == "New kid"
          and len(hs.machines()) == MAX_MACHINES)
    for m in hs.machines():                       # name EVERY row ->
        hs.set_nickname(m["machine_key"], "kept")  # nothing evictable
    try:
        hs.set_nickname("one-too-many", "x")
        check("all-named registry -> naming a NEW key raises", False)
    except ValueError:
        check("all-named registry -> naming a NEW key raises", True)
    hs.touch_machine("one-too-many-touch", "SAS")  # no-op, never raises
    check("all-named registry -> touching a NEW key is a no-op",
          not any(m["machine_key"] == "one-too-many-touch"
                  for m in hs.machines()))
    hs.set_nickname("smib-bb2/1", "Backup Bird")
    check("existing machine still nameable at cap",
          hs.get_nickname("smib-bb2/1") == "Backup Bird")

    print("— AFT registration (schema v3): upsert / get / snapshot")
    with hs.lock:
        ver = hs._conn.execute(
            "SELECT value FROM schema_meta WHERE key='version'").fetchone()
    check("schema migrated to v12", int(ver["value"]) == SCHEMA_VERSION == 12,
          ver["value"])
    # asset READ from the machine (1000 = e8 03 00 00 LE), registered
    hs.aft_upsert_observed("smib-bb2/1", "smib-bb2", 1, asset_number=1000,
                           status_code=0x01, reg_key_fp="0011223344ff",
                           pos_id=1)
    row = hs.aft_get("smib-bb2/1")
    check("aft_get returns the upserted row (asset READ, status registered)",
          bool(row) and row["asset_number"] == 1000
          and row["status"] == "registered")
    check("registered_at stamped on transition into registered",
          bool(row) and bool(row["registered_at"]))
    first_reg_at = row["registered_at"]
    # a later report that omits asset/key must NOT wipe the known values
    hs.aft_upsert_observed("smib-bb2/1", "smib-bb2", 1, asset_number=None,
                           status_code=0x01)
    row = hs.aft_get("smib-bb2/1")
    check("COALESCE keeps asset when a later report omits it",
          row["asset_number"] == 1000)
    check("COALESCE keeps key fingerprint when omitted",
          row["reg_key_fp"] == "0011223344ff")
    check("registered_at is stable across re-observation while registered",
          row["registered_at"] == first_reg_at)

    print("— AFT status derivation from the raw 0x73 status byte")
    hs.aft_upsert_observed("smib-bb2/2", "smib-bb2", 2, asset_number=None,
                           status_code=0x80)
    check("NOT_REGISTERED with no asset -> asset_unknown (assign an asset)",
          hs.aft_get("smib-bb2/2")["status"] == "asset_unknown")
    hs.aft_upsert_observed("smib-bb2/3", "smib-bb2", 3, asset_number=1234,
                           status_code=0x80)
    check("NOT_REGISTERED with an asset -> not_registered",
          hs.aft_get("smib-bb2/3")["status"] == "not_registered")
    hs.aft_upsert_observed("smib-bb2/4", "smib-bb2", 4, asset_number=None,
                           status_code=0x40)
    check("status 0x40 -> pending", hs.aft_get("smib-bb2/4")["status"]
          == "pending")

    print("— AFT attempt counter (auto-trigger backoff) + reset on register")
    hs.aft_mark_attempt("smib-bb2/3")
    hs.aft_mark_attempt("smib-bb2/3")
    check("aft_mark_attempt bumps attempts", hs.aft_get("smib-bb2/3")
          ["attempts"] == 2)
    hs.aft_upsert_observed("smib-bb2/3", "smib-bb2", 3, asset_number=1234,
                           status_code=0x01, reg_key_fp="aabbccddee00",
                           pos_id=1)
    check("transition into registered resets attempts to 0",
          hs.aft_get("smib-bb2/3")["attempts"] == 0)
    hs.aft_mark_attempt("smib-bb2/new")
    row = hs.aft_get("smib-bb2/new")
    check("aft_mark_attempt creates a bare row for a never-observed machine",
          bool(row) and row["attempts"] == 1 and row["asset_number"] is None)

    print("— AFT snapshot exposes only the key FINGERPRINT, never the key")
    snap = hs.aft_snapshot()
    check("aft_snapshot keys every row by machine identity",
          set(snap) >= {"smib-bb2/1", "smib-bb2/2", "smib-bb2/3",
                        "smib-bb2/4"})
    check("snapshot exposes only the fingerprint, never a full key",
          all("registration_key" not in v and "reg_key" not in v
              and "reg_key_fp" in v for v in snap.values()))

    print("— AFT hostile-input bounds (unauth report edge)")
    check("oversize machine_key upsert is a silent no-op",
          hs.aft_upsert_observed("Z" * 200, "z", 1, 1, 0x01) is None
          and hs.aft_get("Z" * 200) is None)
    hs.aft_upsert_observed("smib-bb2/junk", "smib-bb2", "notanint",
                           "notanint", "notanint")
    jr = hs.aft_get("smib-bb2/junk")
    check("non-numeric asset/address/status coerce to NULL (never raise)",
          bool(jr) and jr["asset_number"] is None
          and jr["address"] is None and jr["status"] == "unknown")

    print("— AFT cap: evict stalest NON-registered, NEVER a registered row")
    de = tempfile.mkdtemp()
    ea = HubStore(os.path.join(de, "aft.db"))
    ea.aft_upsert_observed("keep/reg", "keep", 0, asset_number=1000,
                           status_code=0x01, reg_key_fp="ffeeddccbb00",
                           pos_id=1)
    for i in range(MAX_MACHINES - 1):
        ea.aft_upsert_observed(f"n{i}/0", "n", i, asset_number=None,
                               status_code=0x80)
    check("aft table filled to the cap", len(ea.aft_snapshot())
          == MAX_MACHINES)
    ea.aft_upsert_observed("overflow/0", "of", 0, asset_number=None,
                           status_code=0x80)
    snap = ea.aft_snapshot()
    check("overflow evicts a non-registered row, spares the registered one",
          "keep/reg" in snap and "overflow/0" in snap
          and len(snap) == MAX_MACHINES)
    for k in list(ea.aft_snapshot()):        # make EVERY row registered ->
        ea.aft_upsert_observed(k, "x", 0, asset_number=1000,
                               status_code=0x01)  # nothing evictable
    ea.aft_upsert_observed("nope/0", "no", 0, asset_number=None,
                           status_code=0x80)
    check("all-registered at cap refuses a new row",
          "nope/0" not in ea.aft_snapshot()
          and len(ea.aft_snapshot()) == MAX_MACHINES)
    ea.close()

    print("— forward-only migration v2 -> v3 (existing db gains the table)")
    dm = tempfile.mkdtemp()
    pm = os.path.join(dm, "mig.db")
    old = HubStore(pm)
    old.touch_machine("legacy/1", "SAS")
    with old.lock:                          # simulate an on-disk v2 db
        old._conn.execute("DROP TABLE IF EXISTS aft_registrations")
        old._conn.execute(
            "UPDATE schema_meta SET value='2' WHERE key='version'")
        old._conn.commit()
    old.close()
    mig = HubStore(pm)                      # reopen -> _migrate runs 2 -> 3
    check("v2 db migrates to v3 (machines kept, aft table empty + usable)",
          any(m["machine_key"] == "legacy/1" for m in mig.machines())
          and mig.aft_snapshot() == {})
    mig.aft_upsert_observed("legacy/1", "legacy", 1, asset_number=5,
                            status_code=0x01)
    check("migrated v3 table is writable",
          mig.aft_get("legacy/1")["asset_number"] == 5)
    with mig.lock:
        ver = mig._conn.execute(
            "SELECT value FROM schema_meta WHERE key='version'").fetchone()
    check("schema stamped current after migration",
          int(ver["value"]) == SCHEMA_VERSION)
    mig.close()

    print("— v4 denom: set/get/clear + strict bounds")
    dv = tempfile.mkdtemp()
    hv = HubStore(os.path.join(dv, "v4.db"))
    hv.touch_machine("smib-bb2/9", "SAS", {"smibId": "smib-bb2"})
    check("set_denom returns the stored value",
          hv.set_denom("smib-bb2/9", 25) == 25)
    check("machine_prefs reads the denom back",
          hv.machine_prefs("smib-bb2/9")["denomCents"] == 25)
    check("boundary 1 accepted", hv.set_denom("smib-bb2/9", 1) == 1)
    check("boundary 10000 accepted",
          hv.set_denom("smib-bb2/9", MAX_DENOM_CENTS) == MAX_DENOM_CENTS)
    for clr in (0, None, ""):
        hv.set_denom("smib-bb2/9", 25)
        got = hv.set_denom("smib-bb2/9", clr)
        check(f"denom {clr!r} clears (returns None, prefs None)",
              got is None
              and hv.machine_prefs("smib-bb2/9")["denomCents"] is None)
    for bad in (10001, -5, True, "abc"):
        try:
            hv.set_denom("smib-bb2/9", bad)
            check(f"denom {bad!r} raises ValueError", False)
        except ValueError:
            check(f"denom {bad!r} raises ValueError", True)
    check("pre-assign denom on a never-seen key creates the row",
          hv.set_denom("unseen/7", 5, "SAS") == 5
          and hv.machine_prefs("unseen/7") is not None)
    try:
        hv.set_denom("K" * 129, 25)
        check("oversize key raises ValueError", False)
    except ValueError:
        check("oversize key raises ValueError", True)

    print("— v4 denom/lock survive touch_machine")
    hv.set_nickname("smib-bb2/9", "Denom cab")
    hv.set_denom("smib-bb2/9", 25)
    hv.set_lock_state("smib-bb2/9", "locked")
    hv.touch_machine("smib-bb2/9", "SAS", {"smibId": "x", "address": 9})
    pf = hv.machine_prefs("smib-bb2/9")
    check("touch_machine never clobbers denom/lock/nickname",
          pf == {"nickname": "Denom cab", "denomCents": 25,
                 "lockState": "locked", "sasEnabled": True,
                 "sasLink": None}, pf)

    print("— v4 lockState: write-through + junk tolerance")
    hv.set_lock_state("smib-bb2/9", "locked")
    check("set_lock_state('locked') persists",
          hv.machine_prefs("smib-bb2/9")["lockState"] == "locked")
    hv.set_lock_state("smib-bb2/9", "enabled")
    check("set_lock_state('enabled') flips it",
          hv.machine_prefs("smib-bb2/9")["lockState"] == "enabled")
    try:
        hv.set_lock_state("smib-bb2/9", "weird")
        hv.set_lock_state("smib-bb2/9", 5)
        hv.set_lock_state(None, "locked")
        junk_ok = True
    except Exception:  # noqa: BLE001
        junk_ok = False
    check("junk lock faces / keys are silent no-ops (never raise)",
          junk_ok
          and hv.machine_prefs("smib-bb2/9")["lockState"] == "enabled")

    print("— v4 prefs persistence across reopen")
    hv.set_denom("smib-bb2/9", 25)
    hv.set_lock_state("smib-bb2/9", "locked")
    hv.close()
    hv2 = HubStore(os.path.join(dv, "v4.db"))
    pf = hv2.machine_prefs("smib-bb2/9")
    check("denom + lockState survive reopen",
          bool(pf) and pf["denomCents"] == 25
          and pf["lockState"] == "locked", pf)
    hv2.close()

    print("— forward-only migration v3 -> v4 (existing db gains the columns)")
    d34 = tempfile.mkdtemp()
    p34 = os.path.join(d34, "mig34.db")
    old34 = HubStore(p34)
    old34.touch_machine("legacy34/1", "SAS")
    with old34.lock:                    # simulate an on-disk v3 db
        old34._conn.execute(
            "ALTER TABLE machines DROP COLUMN denom_cents")
        old34._conn.execute(
            "ALTER TABLE machines DROP COLUMN lock_state")
        old34._conn.execute(
            "UPDATE schema_meta SET value='3' WHERE key='version'")
        old34._conn.commit()
    old34.close()
    mig34 = HubStore(p34)               # reopen -> _migrate runs 3 -> 4
    check("v3 db migrates to v4 (machines kept)",
          any(m["machine_key"] == "legacy34/1" for m in mig34.machines()))
    check("migrated v4 columns are writable",
          mig34.set_denom("legacy34/1", 100) == 100)
    with mig34.lock:
        ver = mig34._conn.execute(
            "SELECT value FROM schema_meta WHERE key='version'").fetchone()
    check("schema stamped current after 3->4 migration",
          int(ver["value"]) == SCHEMA_VERSION)
    mig34.close()

    print("— partial-migration crash guard (the quarantine landmine)")
    dpg = tempfile.mkdtemp()
    ppg = os.path.join(dpg, "partial34.db")
    pg = HubStore(ppg)
    pg.touch_machine("crashy/1", "SAS")
    with pg.lock:                       # v3-shaped: both columns gone…
        pg._conn.execute("ALTER TABLE machines DROP COLUMN denom_cents")
        pg._conn.execute("ALTER TABLE machines DROP COLUMN lock_state")
        # …then a crash mid-migration re-added ONLY denom_cents (DDL
        # autocommits) before the version stamp could commit:
        pg._conn.execute("ALTER TABLE machines ADD COLUMN "
                         "denom_cents INTEGER")
        pg._conn.execute("UPDATE schema_meta SET value='3' "
                         "WHERE key='version'")
        pg._conn.commit()
    pg.close()
    pg2 = HubStore(ppg)                 # re-run must NOT quarantine
    check("half-migrated db reopens without a quarantine sidecar",
          glob.glob(ppg + "*corrupt*") == [] and pg2.ephemeral is False)
    check("rows kept through the guarded re-run",
          any(m["machine_key"] == "crashy/1" for m in pg2.machines()))
    pg2.set_denom("crashy/1", 25)
    pg2.set_lock_state("crashy/1", "locked")
    pf = pg2.machine_prefs("crashy/1")
    check("BOTH columns usable after the guarded re-run",
          bool(pf) and pf["denomCents"] == 25
          and pf["lockState"] == "locked", pf)
    with pg2.lock:
        ver = pg2._conn.execute(
            "SELECT value FROM schema_meta WHERE key='version'").fetchone()
    check("version stamped current after the guarded re-run",
          int(ver["value"]) == SCHEMA_VERSION)
    pg2.close()

    print("— v5 sas_enabled: default / round-trip / junk rejection")
    d5 = tempfile.mkdtemp()
    h5 = HubStore(os.path.join(d5, "v5.db"))
    with h5.lock:
        ver = h5._conn.execute(
            "SELECT value FROM schema_meta WHERE key='version'").fetchone()
        cols = {r["name"] for r in h5._conn.execute(
            "PRAGMA table_info(machines)").fetchall()}
        hset = h5._conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' "
            "AND name='host_settings'").fetchone()
    check("fresh db carries the v5 column + table (schema current)",
          int(ver["value"]) == SCHEMA_VERSION
          and "sas_enabled" in cols and hset is not None)
    h5.touch_machine("smib-bb2/1", "SAS")
    check("machine_prefs sasEnabled defaults True on NULL",
          h5.machine_prefs("smib-bb2/1")["sasEnabled"] is True)
    check("set_sas_enabled(False) round-trips",
          h5.set_sas_enabled("smib-bb2/1", False) is False
          and h5.machine_prefs("smib-bb2/1")["sasEnabled"] is False)
    h5.touch_machine("smib-bb2/1", "SAS", {"port": "/dev/ttyAMA0"})
    check("touch_machine never clobbers sas_enabled",
          h5.machine_prefs("smib-bb2/1")["sasEnabled"] is False)
    check("machines() carries the sas_enabled column",
          any(m["machine_key"] == "smib-bb2/1" and m["sas_enabled"] == 0
              for m in h5.machines()))
    check("set_sas_enabled(True) re-enables",
          h5.set_sas_enabled("smib-bb2/1", True) is True
          and h5.machine_prefs("smib-bb2/1")["sasEnabled"] is True)
    check("pre-assign on a never-seen key creates the row",
          h5.set_sas_enabled("unseen/9", False) is False
          and h5.machine_prefs("unseen/9")["sasEnabled"] is False)
    for bad_key, bad_en, why in (
            (True, True, "bool-typed key"),
            ("smib-bb2/1", 1, "int 1 is not consent"),
            ("smib-bb2/1", "on", "string enabled"),
            ("smib-bb2/1", None, "None enabled"),
            ("K" * 129, True, "oversize key")):
        try:
            h5.set_sas_enabled(bad_key, bad_en)
            check(f"set_sas_enabled junk raises ValueError ({why})", False)
        except ValueError:
            check(f"set_sas_enabled junk raises ValueError ({why})", True)
    for i in range(MAX_MACHINES):       # fill to cap, then name every row
        h5.touch_machine(f"cap5-{i}", "SAS")
    for m in h5.machines():
        h5.set_nickname(m["machine_key"], "kept")
    try:
        h5.set_sas_enabled("beyond-the-cap/1", True)
        check("unknown machine beyond a full-named cap raises", False)
    except ValueError:
        check("unknown machine beyond a full-named cap raises", True)

    print("— v5 host_settings: default / round-trip / whitelist / bounds")
    check("absent row reads the default",
          h5.host_setting("sysval_fallback") is None
          and h5.host_setting("sysval_fallback", "on") == "on")
    check("set/get round-trip",
          h5.set_host_setting("sysval_fallback", "off") == "off"
          and h5.host_setting("sysval_fallback", "on") == "off")
    try:
        h5.set_host_setting("arbitrary_key", "x")
        check("unknown setting key raises ValueError (whitelist)", False)
    except ValueError:
        check("unknown setting key raises ValueError (whitelist)", True)
    try:
        h5.set_host_setting("sysval_fallback", 5)
        check("non-string setting value raises ValueError", False)
    except ValueError:
        check("non-string setting value raises ValueError", True)
    try:
        h5.set_host_setting("sysval_fallback",
                            "x" * (MAX_SETTING_VAL_LEN + 1))
        check("oversize setting value raises ValueError", False)
    except ValueError:
        check("oversize setting value raises ValueError", True)
    check("junk read keys degrade to the default (never raise)",
          h5.host_setting(123, "d") == "d"
          and h5.host_setting("k" * 65, "d") == "d")
    check("bad writes left the stored value intact",
          h5.host_setting("sysval_fallback") == "off")
    h5.close()

    print("— forward-only migration v4 -> v5 + rerun-safety (run twice)")
    d45 = tempfile.mkdtemp()
    p45 = os.path.join(d45, "mig45.db")
    old45 = HubStore(p45)
    old45.touch_machine("legacy45/1", "SAS")
    old45.set_denom("legacy45/1", 25)
    with old45.lock:                    # simulate an on-disk v4 db
        old45._conn.execute("ALTER TABLE machines DROP COLUMN sas_enabled")
        old45._conn.execute("DROP TABLE IF EXISTS host_settings")
        old45._conn.execute(
            "UPDATE schema_meta SET value='4' WHERE key='version'")
        old45._conn.commit()
    old45.close()
    mig45 = HubStore(p45)               # reopen -> _migrate runs 4 -> 5
    check("v4 db migrates to v5 (machines + denom kept)",
          any(m["machine_key"] == "legacy45/1" for m in mig45.machines())
          and mig45.machine_prefs("legacy45/1")["denomCents"] == 25)
    check("migrated column reads enabled (NULL) and is writable",
          mig45.machine_prefs("legacy45/1")["sasEnabled"] is True
          and mig45.set_sas_enabled("legacy45/1", False) is False)
    check("migrated host_settings table is writable",
          mig45.set_host_setting("sysval_fallback", "off") == "off")
    with mig45.lock:
        ver = mig45._conn.execute(
            "SELECT value FROM schema_meta WHERE key='version'").fetchone()
    check("schema stamped current after 4->5 migration",
          int(ver["value"]) == SCHEMA_VERSION)
    # rerun-safety: a crash between the autocommitting DDL and the version
    # stamp leaves version=4 with the column + table already present — the
    # re-run must NOT raise/quarantine (the v4 landmine, v5 edition).
    with mig45.lock:
        mig45._conn.execute(
            "UPDATE schema_meta SET value='4' WHERE key='version'")
        mig45._conn.commit()
    mig45.close()
    mig45b = HubStore(p45)              # _migrate runs AGAIN over v5 DDL
    check("v5 step is rerun-safe (no quarantine on the second run)",
          glob.glob(p45 + "*corrupt*") == [] and mig45b.ephemeral is False)
    check("second run kept rows + values",
          mig45b.machine_prefs("legacy45/1")["sasEnabled"] is False
          and mig45b.host_setting("sysval_fallback") == "off")
    with mig45b.lock:
        ver = mig45b._conn.execute(
            "SELECT value FROM schema_meta WHERE key='version'").fetchone()
    check("version re-stamped current after the rerun",
          int(ver["value"]) == SCHEMA_VERSION)
    mig45b.close()

    print("— v6 game_names: set/get/delete round-trip + strip")
    d6 = tempfile.mkdtemp()
    h6 = HubStore(os.path.join(d6, "v6.db"))
    with h6.lock:
        ver = h6._conn.execute(
            "SELECT value FROM schema_meta WHERE key='version'").fetchone()
        gnt = h6._conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' "
            "AND name='game_names'").fetchone()
    check("fresh db carries the game_names table (schema current)",
          int(ver["value"]) == SCHEMA_VERSION and gnt is not None,
          ver["value"])
    check("no overrides until set", h6.game_names() == {})
    check("game_name_set returns the stored title",
          h6.game_name_set("014001H18", "Test Title") == "Test Title")
    check("game_names() round-trips",
          h6.game_names() == {"014001H18": "Test Title"})
    check("title is stripped",
          h6.game_name_set("014001H18", "  Padded  ") == "Padded")
    check("update replaces, never duplicates",
          h6.game_names() == {"014001H18": "Padded"})
    check("empty title DELETES the row",
          h6.game_name_set("014001H18", "") is None
          and h6.game_names() == {})
    h6.game_name_set("014001H18", "Back Again")
    check("None title DELETES the row too",
          h6.game_name_set("014001H18", None) is None
          and h6.game_names() == {})
    check("deleting an absent key is a clean no-op",
          h6.game_name_set("999999ZZZ", "") is None)
    h6.game_name_set("014001H18", "Back Again")
    check("whitespace-only title deletes (strips to empty)",
          h6.game_name_set("014001H18", "   ") is None
          and h6.game_names() == {})

    print("— v6 game_names: junk rejection matrix (unauth endpoint)")
    for bad_key, bad_title, why in (
            (None, "x", "None key"),
            ("", "x", "empty key"),
            ("   ", "x", "whitespace key"),
            (123, "x", "int key"),
            (True, "x", "bool key"),
            ("K" * (MAX_GAME_KEY_LEN + 1), "x", "oversize key"),
            ("014001H18", 123, "int title"),
            ("014001H18", True, "bool title"),
            ("014001H18", ["x"], "list title"),
            ("014001H18", "T" * (MAX_GAME_TITLE_LEN + 1),
             "oversize title")):
        try:
            h6.game_name_set(bad_key, bad_title)
            check(f"game_name_set junk raises ValueError ({why})", False)
        except ValueError:
            check(f"game_name_set junk raises ValueError ({why})", True)
    check("junk writes left the table empty", h6.game_names() == {})
    check("boundary key (64) + title (80) accepted",
          h6.game_name_set("K" * MAX_GAME_KEY_LEN,
                           "T" * MAX_GAME_TITLE_LEN)
          == "T" * MAX_GAME_TITLE_LEN)
    h6.game_name_set("K" * MAX_GAME_KEY_LEN, "")  # clear for the cap test

    print("— v6 game_names: the 512-row cap (update-at-cap allowed)")
    for i in range(MAX_GAME_NAMES):
        h6.game_name_set(f"cap{i:06d}", f"Title {i}")
    check("table filled to the cap",
          len(h6.game_names()) == MAX_GAME_NAMES)
    try:
        h6.game_name_set("one00more", "x")
        check("new key beyond the cap raises 'overrides full'", False)
    except ValueError as e:
        check("new key beyond the cap raises 'overrides full'",
              "game-name overrides full" in str(e), e)
    check("UPDATE of an existing key at the cap succeeds",
          h6.game_name_set("cap000000", "Renamed") == "Renamed"
          and h6.game_names()["cap000000"] == "Renamed")
    check("delete at the cap frees a slot for a new key",
          h6.game_name_set("cap000001", "") is None
          and h6.game_name_set("newcomer1", "Fits Now") == "Fits Now"
          and len(h6.game_names()) == MAX_GAME_NAMES)

    print("— v6 game_names: persistence + migration v5 -> v6 (rerun-safe)")
    h6.close()
    h6b = HubStore(os.path.join(d6, "v6.db"))
    check("overrides survive reopen",
          h6b.game_names().get("cap000000") == "Renamed")
    h6b.close()
    d56 = tempfile.mkdtemp()
    p56 = os.path.join(d56, "mig56.db")
    old56 = HubStore(p56)
    old56.set_nickname("legacy56/1", "Kept Cab", "SAS")
    with old56.lock:                    # simulate an on-disk v5 db
        old56._conn.execute("DROP TABLE IF EXISTS game_names")
        old56._conn.execute(
            "UPDATE schema_meta SET value='5' WHERE key='version'")
        old56._conn.commit()
    old56.close()
    mig56 = HubStore(p56)               # reopen -> _migrate runs 5 -> 6
    check("v5 db migrates to v6 (machines kept, table empty + usable)",
          mig56.get_nickname("legacy56/1") == "Kept Cab"
          and mig56.game_names() == {})
    check("migrated game_names table is writable",
          mig56.game_name_set("014001H18", "Migrated") == "Migrated")
    with mig56.lock:
        ver = mig56._conn.execute(
            "SELECT value FROM schema_meta WHERE key='version'").fetchone()
    check("schema stamped v6 after 5->6 migration",
          int(ver["value"]) == SCHEMA_VERSION)
    # rerun-safety: a crash between the autocommitting DDL and the version
    # stamp leaves version=5 with the table already present — the re-run
    # must NOT raise/quarantine (the v4 landmine, v6 edition).
    with mig56.lock:
        mig56._conn.execute(
            "UPDATE schema_meta SET value='5' WHERE key='version'")
        mig56._conn.commit()
    mig56.close()
    mig56b = HubStore(p56)              # _migrate runs AGAIN over v6 DDL
    check("v6 step is rerun-safe (no quarantine on the second run)",
          glob.glob(p56 + "*corrupt*") == [] and mig56b.ephemeral is False)
    check("second run kept the override",
          mig56b.game_names().get("014001H18") == "Migrated")
    with mig56b.lock:
        ver = mig56b._conn.execute(
            "SELECT value FROM schema_meta WHERE key='version'").fetchone()
    check("version re-stamped v6 after the v6 rerun",
          int(ver["value"]) == SCHEMA_VERSION)
    mig56b.close()

    print("— v7 hold tables: fresh db + forward migration v6 -> v7 "
          "(rerun-safe)")
    d7 = tempfile.mkdtemp()
    p7 = os.path.join(d7, "v7.db")
    h7 = HubStore(p7)
    with h7.lock:
        ver = h7._conn.execute(
            "SELECT value FROM schema_meta WHERE key='version'").fetchone()
        tables = {r["name"] for r in h7._conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
    check("fresh db carries meter_marks + hold_journal (schema current)",
          int(ver["value"]) == SCHEMA_VERSION
          and {"meter_marks", "hold_journal"} <= tables, tables)
    # The hold feature was REMOVED 2026-07-15 (collector de-cage): the v7
    # tables stay in the schema (dormant), but the retired tenants are OFF
    # the whitelist — a stale client's write must 400 at the edge, never
    # grow the shelf.
    for retired in ("house_hold", "pnl_baseline_mc"):
        try:
            h7.set_host_setting(retired, "on")
            check(f"retired tenant '{retired}' rejected by the whitelist",
                  False)
        except ValueError:
            check(f"retired tenant '{retired}' rejected by the whitelist",
                  True)

    print("— ticket-header tenants (C1): whitelist / bounds / clear / rev")
    check("ticket tenants whitelisted + round-trip",
          h7.set_host_setting("ticket_prop_name", "FUNCINO") == "FUNCINO"
          and h7.set_host_setting("ticket_line1", "This ain't worth shit")
          == "This ain't worth shit"
          and h7.set_host_setting("ticket_line2", "Rumpus Room, USA")
          == "Rumpus Room, USA"
          and h7.set_host_setting("ticket_title_cash", "CASH TICKET")
          == "CASH TICKET"
          and h7.host_setting("ticket_prop_name") == "FUNCINO")
    check("64-char ticket value accepted AT the bound",
          h7.set_host_setting("ticket_line2", "x" * MAX_TICKET_FIELD_LEN)
          == "x" * MAX_TICKET_FIELD_LEN)
    try:
        h7.set_host_setting("ticket_prop_name",
                            "x" * (MAX_TICKET_FIELD_LEN + 1))
        check("65-char ticket value rejected (the 64-char printed-line "
              "bound, not the generic 256)", False)
    except ValueError:
        check("65-char ticket value rejected (the 64-char printed-line "
              "bound, not the generic 256)", True)
    check("oversize write left the stored value untouched",
          h7.host_setting("ticket_prop_name") == "FUNCINO")
    h7.set_host_setting("ticket_line2", "")
    check("empty ticket value DELETES the row (absent = unset, C1)",
          h7.host_setting("ticket_line2") is None)
    # gameroom_name tenant: whitelisted round-trip + empty=DELETE (neutral
    # fallback) — the collector's player-facing brand.
    check("gameroom_name whitelisted + round-trips",
          h7.set_host_setting("gameroom_name", "AJ's Casino") == "AJ's Casino"
          and h7.host_setting("gameroom_name") == "AJ's Casino")
    h7.set_host_setting("gameroom_name", "")
    check("empty gameroom_name DELETES the row (unset = neutral fallback)",
          h7.host_setting("gameroom_name") is None)
    hdr = h7.ticket_header()
    check("ticket_header one-shot blob: set fields, cleared None, rev 0 "
          "pre-bump",
          hdr == {"propName": "FUNCINO",
                  "line1": "This ain't worth shit", "line2": None,
                  "titleCash": "CASH TICKET", "expireDays": 0,
                  "rev": 0}, hdr)
    # ticket_expire_days tenant: whitelisted, int-clamped 0..255 in the
    # blob, junk degrades to 0, empty = DELETE (never expires)
    h7.set_host_setting("ticket_expire_days", "30")
    check("ticket_expire_days rides the blob as int 30",
          h7.ticket_header()["expireDays"] == 30)
    h7.set_host_setting("ticket_expire_days", "9999")
    check("ticket_expire_days clamps to 255 on an over-range tenant",
          h7.ticket_header()["expireDays"] == 255)
    h7.set_host_setting("ticket_expire_days", "junk")
    check("junk ticket_expire_days tenant degrades to 0 (never)",
          h7.ticket_header()["expireDays"] == 0)
    h7.set_host_setting("ticket_expire_days", "45")
    h7.set_host_setting("ticket_expire_days", "")
    check("empty ticket_expire_days DELETES the row (0 = never)",
          h7.host_setting("ticket_expire_days") is None
          and h7.ticket_header()["expireDays"] == 0)
    h7.set_host_setting("ticket_expire_days", "60")
    check("bump_ticket_rev counts 1, 2 and lands in the blob",
          h7.bump_ticket_rev() == 1 and h7.bump_ticket_rev() == 2
          and h7.ticket_header()["rev"] == 2)
    h7.close()
    h7b = HubStore(p7)                  # tenants survive reopen
    check("ticket header survives reopen (tenants + rev durable)",
          h7b.ticket_header() == {"propName": "FUNCINO",
                                  "line1": "This ain't worth shit",
                                  "line2": None,
                                  "titleCash": "CASH TICKET",
                                  "expireDays": 60, "rev": 2})
    h7b.close()
    d67 = tempfile.mkdtemp()
    p67 = os.path.join(d67, "mig67.db")
    old67 = HubStore(p67)
    old67.set_nickname("legacy67/1", "Kept Cab", "SAS")
    with old67.lock:                    # simulate an on-disk v6 db
        old67._conn.execute("DROP TABLE IF EXISTS meter_marks")
        old67._conn.execute("DROP TABLE IF EXISTS hold_journal")
        old67._conn.execute(
            "UPDATE schema_meta SET value='6' WHERE key='version'")
        old67._conn.commit()
    old67.close()
    mig67 = HubStore(p67)               # reopen -> _migrate runs 6 -> 7
    with mig67.lock:
        tables67 = {r["name"] for r in mig67._conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
        ver = mig67._conn.execute(
            "SELECT value FROM schema_meta WHERE key='version'").fetchone()
    check("v6 db migrates forward (machines kept, v7 tables present)",
          mig67.get_nickname("legacy67/1") == "Kept Cab"
          and {"meter_marks", "hold_journal"} <= tables67, tables67)
    check("schema stamped current after 6->forward migration",
          int(ver["value"]) == SCHEMA_VERSION)
    # rerun-safety: a crash between the autocommitting DDL and the version
    # stamp leaves version=6 with the tables already present — the re-run
    # must NOT raise/quarantine (the v4 landmine, v7 edition). Plant a row
    # in the dormant table to prove the rerun's CREATE IF NOT EXISTS never
    # clobbers existing data.
    with mig67.lock:
        mig67._conn.execute(
            "INSERT INTO meter_marks (machine_key, meter_key, raw_value, "
            "baselined_at, updated_at) VALUES ('keep/1','k',1,'t','t')")
        mig67._conn.execute(
            "UPDATE schema_meta SET value='6' WHERE key='version'")
        mig67._conn.commit()
    mig67.close()
    mig67b = HubStore(p67)              # _migrate runs AGAIN over v7 DDL
    check("v7 step is rerun-safe (no quarantine on the second run)",
          glob.glob(p67 + "*corrupt*") == [] and mig67b.ephemeral is False)
    with mig67b.lock:
        kept = mig67b._conn.execute(
            "SELECT COUNT(*) AS n FROM meter_marks").fetchone()
        ver = mig67b._conn.execute(
            "SELECT value FROM schema_meta WHERE key='version'").fetchone()
    check("second run kept existing rows (IF NOT EXISTS, no clobber)",
          int(kept["n"]) == 1)
    check("version re-stamped current after the v7 rerun",
          int(ver["value"]) == SCHEMA_VERSION)
    mig67b.close()

    print("— v8 fobs: uid normalization (one card, many spellings)")
    d8 = tempfile.mkdtemp()
    p8 = os.path.join(d8, "v8.db")
    h8 = HubStore(p8)
    check("_norm_fob_uid canonicalizes lowercase + colons",
          _norm_fob_uid("6c:b1:6f:06") == "6CB16F06")
    check("_norm_fob_uid canonicalizes dashes + spaces",
          _norm_fob_uid("6c-b1 6f-06") == "6CB16F06")
    for junk in (None, 123, True, "", "X", "GG", "zz:zz", "6CB16F06!",
                 "F" * 33, ["6CB16F06"]):
        check(f"_norm_fob_uid rejects junk {junk!r}",
              _norm_fob_uid(junk) is None)
    row, err = h8.fob_set("6c:b1:6f:06", tier="player", label="AJ's card",
                          account_id="p1")
    check("fob_set stores the CANONICAL uid",
          err is None and row["uid"] == "6CB16F06", (row, err))
    check("fob_get finds the card by any spelling",
          h8.fob_get("6c-b1-6f-06")["uid"] == "6CB16F06"
          and h8.fob_get("6CB16F06")["label"] == "AJ's card")

    print("— v8 fobs: camelCase wire shape + defaults")
    check("fob_get returns the full camelCase shape",
          set(h8.fob_get("6CB16F06")) == {"uid", "tier", "label",
                                          "accountId", "createdAt",
                                          "lastSeen", "meta"},
          h8.fob_get("6CB16F06"))
    check("accountId/createdAt populated, lastSeen None until a tap, "
          "meta decodes to {}",
          h8.fob_get("6CB16F06")["accountId"] == "p1"
          and bool(h8.fob_get("6CB16F06")["createdAt"])
          and h8.fob_get("6CB16F06")["lastSeen"] is None
          and h8.fob_get("6CB16F06")["meta"] == {})
    row, err = h8.fob_set("AABBCCDD")   # all-default insert
    check("bare fob_set defaults tier=player, label/accountId ''",
          err is None and row["tier"] == "player" and row["label"] == ""
          and row["accountId"] == "", (row, err))

    print("— v8 fobs: tier validation + the table CHECK backstop")
    for bad in ("boss", "", "Player", 123, True):
        row, err = h8.fob_set("AABBCCDD", tier=bad)
        check(f"fob_set rejects tier {bad!r} with an error tuple",
              row is None and "tier must be one of" in (err or ""), err)
    check("bad-tier writes left the row untouched",
          h8.fob_get("AABBCCDD")["tier"] == "player")
    try:                                # a hand-edit can't sneak past either
        with h8.lock:
            h8._conn.execute(
                "INSERT INTO fobs(uid,tier,created_at) "
                "VALUES('DEADBEEF','boss','now')")
        check("table CHECK rejects a hand-edited junk tier", False)
    except sqlite3.IntegrityError:
        check("table CHECK rejects a hand-edited junk tier", True)
    for tier in FOB_TIERS:
        row, err = h8.fob_set("AABBCCDD", tier=tier)
        check(f"tier {tier!r} accepted", err is None and row["tier"] == tier)

    print("— v8 fobs: partial update (None keeps, value replaces) + the "
          "tier <-> link invariant (only player fobs hold an account)")
    row, err = h8.fob_set("6CB16F06", tier="reset", account_id="p9")
    check("linking a NON-player tier refuses (an attendant key is never "
          "a wallet card)",
          row is None and "only player-tier fobs link" in (err or ""), err)
    check("the refused write left the row untouched",
          h8.fob_get("6CB16F06")["tier"] == "player"
          and h8.fob_get("6CB16F06")["accountId"] == "p1")
    h8.fob_set("6CB16F06", tier="reset")
    fb = h8.fob_get("6CB16F06")
    check("tier-only update keeps the label and CLEARS the link "
          "(re-tier away from player = unlink, atomically)",
          fb["tier"] == "reset" and fb["label"] == "AJ's card"
          and fb["accountId"] == "", fb)
    row, err = h8.fob_set("6CB16F06", account_id="p1")
    check("re-linking while the tier is still reset refuses too "
          "(closes the linkFob check-then-write race)",
          row is None and "only player-tier fobs link" in (err or ""), err)
    h8.fob_set("6CB16F06", label="Reset fob", account_id="")
    fb = h8.fob_get("6CB16F06")
    check("label/accountId update keeps tier (empty string CLEARS, "
          "None keeps)",
          fb["tier"] == "reset" and fb["label"] == "Reset fob"
          and fb["accountId"] == "", fb)
    row, err = h8.fob_set("6CB16F06", label="  padded  " + "y" * 100)
    check("label is stripped + clamped to the bound",
          err is None and len(row["label"]) == MAX_FOB_LABEL_LEN
          and row["label"].startswith("padded"), row)
    row, err = h8.fob_set("6CB16F06", label=123)
    check("non-string label rejected with an error tuple",
          row is None and err == "label must be a string")
    row, err = h8.fob_set("6CB16F06", account_id=123)
    check("non-string accountId rejected with an error tuple",
          row is None and err == "accountId must be a string")

    print("— v8 fobs: touch (tap stamp) + delete")
    h8.fob_touch("6c:b1:6f:06", "2026-07-10T20:00:00")
    check("fob_touch stamps last_seen through normalization",
          h8.fob_get("6CB16F06")["lastSeen"] == "2026-07-10T20:00:00")
    h8.fob_touch("FEEDFACE", "2026-07-10T20:00:00")
    check("fob_touch on an unknown uid NEVER creates a row",
          h8.fob_get("FEEDFACE") is None)
    h8.fob_touch("6CB16F06", None)      # junk timestamp = silent no-op
    check("junk timestamp is a silent no-op",
          h8.fob_get("6CB16F06")["lastSeen"] == "2026-07-10T20:00:00")
    check("fobs() lists in registration order",
          [f["uid"] for f in h8.fobs()] == ["6CB16F06", "AABBCCDD"])
    check("fob_delete removes the row + reports True",
          h8.fob_delete("aa:bb:cc:dd") is True
          and h8.fob_get("AABBCCDD") is None)
    check("fob_delete on an absent/malformed uid reports False",
          h8.fob_delete("AABBCCDD") is False
          and h8.fob_delete("not-hex!") is False)

    print("— v8 fobs: the 256-row cap (update-at-cap allowed)")
    for i in range(MAX_FOBS - 1):       # 6CB16F06 is already row 1
        row, err = h8.fob_set(f"{i:08X}")
        if err is not None:
            break
    check("table filled to the cap", len(h8.fobs()) == MAX_FOBS)
    row, err = h8.fob_set("CAFED00D")
    check("new uid beyond the cap refuses with 'fob registry full'",
          row is None and f"fob registry full ({MAX_FOBS})" == err, err)
    row, err = h8.fob_set("6CB16F06", label="Still editable")
    check("UPDATE of an existing uid at the cap succeeds",
          err is None and row["label"] == "Still editable")
    check("delete at the cap frees a slot for a new uid",
          h8.fob_delete("00000000") is True
          and h8.fob_set("CAFED00D")[1] is None
          and len(h8.fobs()) == MAX_FOBS)

    print("— v8 fobs: persistence + migration v7 -> v8 (rerun-safe)")
    h8.close()
    h8b = HubStore(p8)
    check("fobs survive reopen (label + lastSeen durable)",
          h8b.fob_get("6CB16F06")["label"] == "Still editable"
          and h8b.fob_get("6CB16F06")["lastSeen"]
          == "2026-07-10T20:00:00")
    h8b.close()
    d78 = tempfile.mkdtemp()
    p78 = os.path.join(d78, "mig78.db")
    old78 = HubStore(p78)
    old78.set_nickname("legacy78/1", "Kept Cab", "SAS")
    with old78.lock:                    # simulate an on-disk v7 db
        old78._conn.execute("DROP TABLE IF EXISTS fobs")
        old78._conn.execute(
            "UPDATE schema_meta SET value='7' WHERE key='version'")
        old78._conn.commit()
    old78.close()
    mig78 = HubStore(p78)               # reopen -> _migrate runs 7 -> 8
    check("v7 db migrates to v8 (machines kept, fobs table empty + usable)",
          mig78.get_nickname("legacy78/1") == "Kept Cab"
          and mig78.fobs() == [])
    row, err = mig78.fob_set("6CB16F06", tier="player", label="Migrated")
    check("migrated fobs table is writable",
          err is None and row["label"] == "Migrated")
    with mig78.lock:
        ver = mig78._conn.execute(
            "SELECT value FROM schema_meta WHERE key='version'").fetchone()
    check("schema stamped v8 after 7->8 migration",
          int(ver["value"]) == SCHEMA_VERSION)
    # rerun-safety: a crash between the autocommitting DDL and the version
    # stamp leaves version=7 with the table already present — the re-run
    # must NOT raise/quarantine (the v4 landmine, v8 edition).
    with mig78.lock:
        mig78._conn.execute(
            "UPDATE schema_meta SET value='7' WHERE key='version'")
        mig78._conn.commit()
    mig78.close()
    mig78b = HubStore(p78)              # _migrate runs AGAIN over v8 DDL
    check("v8 step is rerun-safe (no quarantine on the second run)",
          glob.glob(p78 + "*corrupt*") == [] and mig78b.ephemeral is False)
    check("second run kept the fob",
          mig78b.fob_get("6CB16F06")["label"] == "Migrated")
    with mig78b.lock:
        ver = mig78b._conn.execute(
            "SELECT value FROM schema_meta WHERE key='version'").fetchone()
    check("version re-stamped v8 after the v8 rerun",
          int(ver["value"]) == SCHEMA_VERSION)
    mig78b.close()

    print("— v9 sas_link: set / clear / overwrite + machine_prefs")
    d9 = tempfile.mkdtemp()
    p9 = os.path.join(d9, "v9.db")
    h9 = HubStore(p9)
    with h9.lock:
        cols = {r["name"] for r in h9._conn.execute(
            "PRAGMA table_info(machines)").fetchall()}
    check("fresh db carries the v9 sas_link column", "sas_link" in cols)
    h9.touch_machine("IGT_TESTCAB01", "G2S", {"vendor": "IGT"})
    check("machine_prefs sasLink defaults None (unlinked)",
          h9.machine_prefs("IGT_TESTCAB01")["sasLink"] is None)
    check("no links until set", h9.sas_links() == {})
    check("set_sas_link stores + machine_prefs carries it",
          h9.set_sas_link("IGT_TESTCAB01", "smib-bb2/1") == "smib-bb2/1"
          and h9.machine_prefs("IGT_TESTCAB01")["sasLink"] == "smib-bb2/1")
    check("set_sas_link overwrites (re-point to another leg)",
          h9.set_sas_link("IGT_TESTCAB01", "smib-bb1/3") == "smib-bb1/3"
          and h9.machine_prefs("IGT_TESTCAB01")["sasLink"] == "smib-bb1/3")
    h9.touch_machine("IGT_TESTCAB01", "G2S", {"vendor": "IGT", "fw": "x"})
    check("touch_machine never clobbers sas_link",
          h9.machine_prefs("IGT_TESTCAB01")["sasLink"] == "smib-bb1/3")
    check("pre-assign on a never-seen key creates the row",
          h9.set_sas_link("WMS_TESTCAB02", "smib-bb2/1") == "smib-bb2/1"
          and h9.machine_prefs("WMS_TESTCAB02")["sasLink"] == "smib-bb2/1")
    check("sas_links() maps every LINKED machine (and only those)",
          h9.sas_links() == {"IGT_TESTCAB01": "smib-bb1/3",
                             "WMS_TESTCAB02": "smib-bb2/1"}, h9.sas_links())
    check("empty string CLEARS the link (stores NULL)",
          h9.set_sas_link("WMS_TESTCAB02", "") is None
          and h9.machine_prefs("WMS_TESTCAB02")["sasLink"] is None
          and h9.sas_links() == {"IGT_TESTCAB01": "smib-bb1/3"})
    check("None clears too (the JSON-null spelling)",
          h9.set_sas_link("IGT_TESTCAB01", None) is None
          and h9.sas_links() == {})
    check("sas_key is stripped (whitespace-only = clear)",
          h9.set_sas_link("IGT_TESTCAB01", "  smib-bb2/1  ") == "smib-bb2/1"
          and h9.set_sas_link("IGT_TESTCAB01", "   ") is None)
    for bad_key, bad_link, why in ((None, "smib-bb2/1", "None key"),
                                   ("", "smib-bb2/1", "empty key"),
                                   ("K" * 200, "smib-bb2/1", "long key"),
                                   (True, "smib-bb2/1", "bool key"),
                                   ("IGT_TESTCAB01", 123, "int link"),
                                   ("IGT_TESTCAB01", True, "bool link"),
                                   ("IGT_TESTCAB01", "L" * 200,
                                    "long link")):
        try:
            h9.set_sas_link(bad_key, bad_link)
            check(f"set_sas_link junk raises ValueError ({why})", False)
        except ValueError:
            check(f"set_sas_link junk raises ValueError ({why})", True)

    h9.close()

    print("— v9 migration v8 -> v9 (rerun-safe, the v4 landmine)")
    d89 = tempfile.mkdtemp()
    p89 = os.path.join(d89, "mig89.db")
    old89 = HubStore(p89)
    old89.set_nickname("legacy89/1", "Kept Cab", "G2S")
    with old89.lock:                    # simulate an on-disk v8 db
        old89._conn.execute("ALTER TABLE machines DROP COLUMN sas_link")
        old89._conn.execute(
            "UPDATE schema_meta SET value='8' WHERE key='version'")
        old89._conn.commit()
    old89.close()
    mig89 = HubStore(p89)               # reopen -> _migrate runs 8 -> 9
    check("v8 db migrates to v9 (machines kept, sasLink reads None)",
          mig89.get_nickname("legacy89/1") == "Kept Cab"
          and mig89.machine_prefs("legacy89/1")["sasLink"] is None)
    check("migrated column is writable",
          mig89.set_sas_link("legacy89/1", "smib-bb2/1") == "smib-bb2/1")
    with mig89.lock:
        ver = mig89._conn.execute(
            "SELECT value FROM schema_meta WHERE key='version'").fetchone()
    check("schema stamped v9 after 8->9 migration",
          int(ver["value"]) == SCHEMA_VERSION)
    # rerun-safety: a crash between the autocommitting ALTER and the
    # version stamp leaves version=8 with the column already present — the
    # re-run must NOT raise/quarantine (the v4 landmine, v9 edition).
    with mig89.lock:
        mig89._conn.execute(
            "UPDATE schema_meta SET value='8' WHERE key='version'")
        mig89._conn.commit()
    mig89.close()
    mig89b = HubStore(p89)              # _migrate runs AGAIN over v9 DDL
    check("v9 step is rerun-safe (no quarantine on the second run)",
          glob.glob(p89 + "*corrupt*") == [] and mig89b.ephemeral is False)
    check("second run kept the link",
          mig89b.sas_links() == {"legacy89/1": "smib-bb2/1"})
    with mig89b.lock:
        ver = mig89b._conn.execute(
            "SELECT value FROM schema_meta WHERE key='version'").fetchone()
    check("version re-stamped v9 after the v9 rerun",
          int(ver["value"]) == SCHEMA_VERSION)
    mig89b.close()

    print("— persistence across reopen")
    hs.close()
    hs2 = HubStore(path)
    check("nickname survives reopen",
          hs2.get_nickname("smib-bb2/1") == "Backup Bird")
    check("registry survives reopen", len(hs2.machines()) >= 3)
    _aft = hs2.aft_get("smib-bb2/1")
    check("AFT registration survives reopen (asset + fingerprint durable)",
          bool(_aft) and _aft["asset_number"] == 1000
          and _aft["reg_key_fp"] == "0011223344ff")

    print("— corruption quarantine (a garbage db file starts fresh)")
    hs2.close()
    path2 = os.path.join(d, "corrupt.db")
    with open(path2, "wb") as f:
        f.write(b"this is not a sqlite database at all, not even close")
    hs3 = HubStore(path2)             # must not raise
    check("unreadable db quarantined + fresh start",
          os.path.exists(path2 + ".corrupt") and hs3.names() == {})
    hs3.set_nickname("x", "y")
    check("fresh quarantined db is writable",
          hs3.get_nickname("x") == "y")
    hs3.close()

    print("— partial corruption (valid page 1, trashed btrees) — the "
          "host-boot-wedge case")
    path3 = os.path.join(d, "partial.db")
    hp = HubStore(path3)
    for i in range(200):              # push the db well past page 1
        hp.touch_machine(f"m{i:03d}", "SAS", {"pad": "x" * 64})
    hp.close()
    with open(path3, "r+b") as f:     # page 1 intact, page 2 pure garbage
        f.seek(4096)
        f.write(b"\xde\xad\xbe\xef" * 1024)
    with open(path3 + "-wal", "wb") as f:
        f.write(b"stale wal junk left by a power pull")
    hp2 = HubStore(path3)             # MUST not raise (this was the wedge)
    check("partially-corrupt db quarantined at open (host still boots)",
          os.path.exists(path3 + ".corrupt") and hp2.names() == {})
    # sqlite discards an invalid wal on open; _quarantine_files moves a real
    # one. Either way the junk must never survive next to the fresh db.
    stale_gone = True
    if os.path.exists(path3 + "-wal"):
        with open(path3 + "-wal", "rb") as f:
            stale_gone = b"stale wal junk" not in f.read()
    check("stale -wal never survives into the fresh store", stale_gone)
    hp2.set_nickname("x", "y")
    check("fresh store after partial-corruption quarantine is writable",
          hp2.get_nickname("x") == "y")
    hp2.close()
    check("reads DEGRADE (never raise) on a dead connection",
          hp2.names() == {} and hp2.get_nickname("x") is None
          and hp2.machines() == [] and hp2.game_names() == {})
    # v4 best-effort accessors degrade the same way (set_denom is the
    # raising accessor — deliberately not asserted here).
    try:
        hp2.set_lock_state("x", "locked")
        got = hp2.machine_prefs("x")
        degraded = got is None
    except Exception:  # noqa: BLE001
        degraded = False
    check("set_lock_state/machine_prefs DEGRADE on a dead connection",
          degraded)

    print("— v11 satellites: assign / partial-update / unassign / cap")
    dsat = tempfile.mkdtemp()
    hsat = HubStore(os.path.join(dsat, "sat.db"))
    row, err = hsat.sat_set("companion-a3f2b1", kind="companion",
                            label="Bar-top AVP")
    check("sat_set inserts (row, None) with camelCase shape, empty=None",
          err is None and row["satId"] == "companion-a3f2b1"
          and row["kind"] == "companion" and row["label"] == "Bar-top AVP"
          and row["g2sEgmId"] is None, err)
    row, err = hsat.sat_set("companion-a3f2b1", egm_id="IGT_00012E492815")
    check("partial update keeps label, sets egm (COALESCE None = keep)",
          err is None and row["label"] == "Bar-top AVP"
          and row["g2sEgmId"] == "IGT_00012E492815")
    check("sat_get round-trips + sat_bindings maps by id",
          hsat.sat_get("companion-a3f2b1")["g2sEgmId"] == "IGT_00012E492815"
          and hsat.sat_bindings()["companion-a3f2b1"]["g2sEgmId"]
              == "IGT_00012E492815")
    row, err = hsat.sat_set("companion-a3f2b1", egm_id="")
    check("empty egm clears the binding (unassign), label preserved",
          row["g2sEgmId"] is None and row["label"] == "Bar-top AVP")
    check("malformed satId -> error tuple (None, msg)",
          hsat.sat_set("bad id!!")[0] is None
          and hsat.sat_set("bad id!!")[1] is not None)
    check("bad kind -> error tuple",
          hsat.sat_set("companion-x", kind="bogus")[1] is not None)
    hsat2 = HubStore(os.path.join(dsat, "sat.db"))   # reopen -> persistence
    check("assignment persists across reopen",
          hsat2.sat_get("companion-a3f2b1")["label"] == "Bar-top AVP")
    check("sat_delete removes the row",
          hsat2.sat_delete("companion-a3f2b1") is True
          and hsat2.sat_get("companion-a3f2b1") is None)
    for i in range(MAX_SATELLITES):
        hsat2.sat_set("companion-%04x" % i, label="x")
    over, oerr = hsat2.sat_set("companion-over", label="y")
    check("registry cap refuses a NEW row when full (bounded, unauth edge)",
          over is None and "full" in str(oerr), oerr)
    check("...but an UPDATE of an existing id still succeeds at the cap",
          hsat2.sat_set("companion-0000", label="renamed")[1] is None)

    print("— read-only directory degrades to an ephemeral store")
    rod = os.path.join(d, "rodir")
    os.makedirs(rod)
    path4 = os.path.join(rod, "hub.db")
    with open(path4, "wb") as f:
        f.write(b"garbage, and the dir is about to go read-only")
    os.chmod(rod, 0o555)              # quarantine + fresh-create both fail
    try:
        hro = HubStore(path4)         # must not raise -> in-memory fallback
        hro.set_nickname("x", "ephemeral")
        check("read-only dir -> ephemeral in-memory store still works",
              hro.get_nickname("x") == "ephemeral")
        hro.close()
    finally:
        os.chmod(rod, 0o755)

    print("=" * 50)
    print(f"RESULT: {_p} passed, {_f} failed")
    return 1 if _f else 0


if __name__ == "__main__":
    sys.exit(main())
