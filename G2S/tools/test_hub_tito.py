#!/usr/bin/env python3
"""
test_hub_tito.py — standalone gate for the hub TITO validation ledger
(hub_store schema v2) and the TitoAuthority union (g2s_host).

In-process, no live host, stdlib only. Run:  python3 tools/test_hub_tito.py
Must end "RESULT: N passed, 0 failed".
"""

import os
import sqlite3
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from hub_store import (HubStore, MAX_TICKET_MILLICENTS,   # noqa: E402
                       SCHEMA_VERSION)
from g2s_host import TitoAuthority                        # noqa: E402

_p = _f = 0


def check(name, ok, detail=""):
    global _p, _f
    if ok:
        _p += 1
        print(f"  ✅ {name}")
    else:
        _f += 1
        print(f"  ❌ {name} {detail}")


def fresh():
    d = tempfile.mkdtemp()
    return HubStore(os.path.join(d, "hub.db")), d


def main():
    hs, d = fresh()

    print("— schema migration (tito tables present)")
    check("schema version is at least 2 (tito)", SCHEMA_VERSION >= 2)
    with hs.lock:
        tables = {r["name"] for r in hs._conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
    check("tito tables created",
          {"tito_tickets", "tito_meta"} <= tables, tables)

    print("— v1 db migrates forward without losing names")
    d1 = tempfile.mkdtemp()
    p1 = os.path.join(d1, "hub.db")
    conn = sqlite3.connect(p1)
    conn.execute("CREATE TABLE schema_meta (key TEXT PRIMARY KEY, value TEXT)")
    conn.execute("INSERT INTO schema_meta VALUES ('version', '1')")
    conn.execute("CREATE TABLE machines (machine_key TEXT PRIMARY KEY, "
                 "protocol TEXT NOT NULL, nickname TEXT, first_seen TEXT "
                 "NOT NULL, last_seen TEXT NOT NULL, meta TEXT)")
    conn.execute("INSERT INTO machines VALUES ('m1','SAS','Bluebird',"
                 "'2026-01-01','2026-01-01',NULL)")
    conn.commit()
    conn.close()
    hs1 = HubStore(p1)
    check("v1 names survive the v2 migration",
          hs1.get_nickname("m1") == "Bluebird")
    check("v2 tables exist after migration",
          hs1.tito_counts() == {"total": 0}, hs1.tito_counts())
    hs1.close()

    print("— mint")
    m = hs.tito_mint("sas:smib-bb2", 1, 10_000)
    check("first mint is sid01 seq1",
          m["validationNumber"] == "0100000000000001" and not m["reused"], m)
    check("canonical = sid + vn16",
          m["canonical"] == "01" + m["validationNumber"])
    m2 = hs.tito_mint("sas:smib-bb2", 1, 10_000)
    check("same origin+addr+amount REUSES the open mint",
          m2["reused"] and m2["validationNumber"] == m["validationNumber"])
    m3 = hs.tito_mint("sas:smib-bb2", 1, 20_000)
    check("different amount mints a new number",
          not m3["reused"] and m3["seq"] == 2, m3)
    try:
        hs.tito_mint("x", 1, MAX_TICKET_MILLICENTS + 1)
        check("over-ceiling mint raises", False)
    except ValueError:
        check("over-ceiling mint raises", True)
    try:
        hs.tito_mint("x", 1, 0)
        check("zero-amount mint raises", False)
    except ValueError:
        check("zero-amount mint raises", True)

    print("— mint uniqueness vs occupied numbers (synced fallback rows)")
    with hs.lock:
        hs._conn.execute(
            "INSERT INTO tito_tickets (canonical, vn16, state, amount_mc) "
            "VALUES ('010100000000000003', '0100000000000003', 'issued', "
            "1000)")
        hs._conn.commit()
    m4 = hs.tito_mint("sas:smib-bb2", 1, 30_000)
    check("mint skips an occupied seq", m4["seq"] == 4, m4)

    print("— open-mint spam guard")
    from hub_store import MAX_OPEN_MINTS
    hs_s, _ds = fresh()
    with hs_s.lock:
        hs_s._conn.executemany(
            "INSERT INTO tito_tickets (canonical, vn16, state, amount_mc, "
            "origin, address, minted_at) VALUES (?,?,'minted',1000,'spam',"
            "1,'2099-01-01T00:00:00')",
            [(f"99{i:016d}", f"{i:016d}") for i in range(MAX_OPEN_MINTS)])
        hs_s._conn.commit()
    try:
        hs_s.tito_mint("sas:legit", 1, 1000)
        check("open-mint cap refuses new mints", False)
    except RuntimeError:
        check("open-mint cap refuses new mints", True)
    with hs_s.lock:
        hs_s._conn.execute("UPDATE tito_tickets SET amount_mc=2000 WHERE "
                           "vn16='0000000000000005'")
        hs_s._conn.commit()
    reuse = hs_s.tito_mint("spam", 1, 2000)
    check("reuse still works at the cap", reuse["reused"], reuse)
    hs_s.close()

    print("— ephemeral store refuses to mint")
    hs_e, _de = fresh()
    hs_e.ephemeral = True
    try:
        hs_e.tito_mint("x", 1, 1000)
        check("ephemeral mint refused", False)
    except RuntimeError:
        check("ephemeral mint refused", True)
    hs_e.close()

    print("— record issued")
    r = hs.tito_record_issued(m["validationNumber"], 10_000,
                              "sas:smib-bb2", 1, ticket_number=1,
                              source="egm_4D")
    check("minted row reconciles to issued",
          r["state"] == "issued" and not r["duplicate"], r)
    r2 = hs.tito_record_issued(m["validationNumber"], 10_000,
                               "sas:smib-bb2", 1)
    check("re-report is duplicate, state unchanged",
          r2["duplicate"] and r2["state"] == "issued", r2)
    r3 = hs.tito_record_issued("0200000000000009", 5_000, "sas:smib-bb2", 1)
    check("unminted capture inserts issued",
          r3["state"] == "issued" and r3["canonical"]
          == "020200000000000009", r3)
    try:
        hs.tito_record_issued("0000000000000000", 1000, "x", 1)
        check("all-zero vn raises", False)
    except ValueError:
        check("all-zero vn raises", True)

    print("— collision containment (reused validation number)")
    # A vn issued, redeemed, then reused by a fresh ticket of a DIFFERENT
    # amount must RE-ISSUE (never silently drop -> the ticket-stuck bug).
    cvn = "0055500000000042"
    hs.tito_record_issued(cvn, 300_500_000, "sas:smib-bb2", 1)   # $3,005
    ccan = hs._predict_canonical(cvn)
    hs.tito_authorize("AVP", "ct1", ccan)
    hs.tito_close("AVP", "ct1", ccan, True)                      # redeemed
    rc = hs.tito_record_issued(cvn, 2_500_000, "sas:smib-bb2", 1)  # fresh $25
    check("collision over redeemed re-issues (not dropped)",
          rc.get("collision") == "reissued" and rc["state"] == "issued"
          and not rc["duplicate"], rc)
    ac = hs.tito_authorize("BB2", "ct2", ccan)
    check("re-issued collision ticket redeems for its REAL amount",
          ac["authorized"] and ac["amountMc"] == 2_500_000, ac)
    hs.tito_close("BB2", "ct2", ccan, True)
    # Same amount over a redeemed row is a stale re-read -> unchanged.
    rc2 = hs.tito_record_issued(cvn, 2_500_000, "sas:smib-bb2", 1)
    check("same-amount re-read stays duplicate (no resurrection)",
          rc2["duplicate"] and rc2["state"] == "redeemed"
          and not rc2.get("collision"), rc2)
    # Collision over a LIVE ticket must NOT overwrite the valid paper.
    lvn = "0066600000000007"
    hs.tito_record_issued(lvn, 12_345_000, "sas:smib-bb2", 1)    # live issued
    rc3 = hs.tito_record_issued(lvn, 2_500_000, "sas:smib-bb2", 1)
    lcan = hs._predict_canonical(lvn)
    la = hs.tito_authorize("BB2", "lt1", lcan)
    check("collision over live ticket flags conflict, keeps live amount",
          rc3.get("collision") == "conflict" and rc3["duplicate"]
          and la["amountMc"] == 12_345_000, (rc3, la))

    print("— authorize / holder semantics")
    can = m["canonical"]
    a = hs.tito_authorize("IGT_AVP", "txn1", can)
    check("cross-machine authorize by canonical",
          a["authorized"] and a["amountMc"] == 10_000, a)
    a_retry = hs.tito_authorize("IGT_AVP", "txn1", can)
    check("same holder retry re-draws identically",
          a_retry["authorized"] and a_retry["retry"], a_retry)
    a_other = hs.tito_authorize("sas:smib-bb2:1", "txnX", can)
    check("second machine blocked while pending",
          not a_other["authorized"]
          and "already in process" in a_other["reason"], a_other)
    a_minted = hs.tito_authorize("M", "t", m3["canonical"])
    check("minted-but-never-printed rejects",
          not a_minted["authorized"] and "printed" in a_minted["reason"],
          a_minted)
    a_unknown = hs.tito_authorize("M", "t", "999999999999999999")
    check("unknown rejects", not a_unknown["authorized"], a_unknown)

    print("— amount is issued-authoritative")
    a_rep = hs.tito_authorize("M2", "t2", "020200000000000009",
                              reported_mc=999_000)
    check("mismatched report noted, issued amount wins",
          a_rep["authorized"] and a_rep["amountMc"] == 5_000
          and "overrides" in a_rep["reason"], a_rep)
    hs.tito_close("M2", "t2", "020200000000000009", False)

    print("— close")
    st = hs.tito_close("WRONG", "txn1", can, True)
    check("non-holder cannot consume", st == "redeemPending", st)
    st = hs.tito_close("IGT_AVP", "wrongtxn", can, True)
    check("wrong txn cannot consume", st == "redeemPending", st)
    st = hs.tito_close("IGT_AVP", "txn1", can, True)
    check("holder consumes -> redeemed", st == "redeemed")
    st = hs.tito_close("IGT_AVP", "txn1", can, True)
    check("duplicate close idempotent", st == "redeemed")
    a_after = hs.tito_authorize("M", "t9", can)
    check("redeemed ticket rejects re-authorize",
          not a_after["authorized"]
          and "already redeemed" in a_after["reason"], a_after)
    # reject-reset path
    b = hs.tito_mint("sas:smib-bb2", 1, 7_000)
    hs.tito_record_issued(b["validationNumber"], 7_000, "sas:smib-bb2", 1)
    hs.tito_authorize("M3", "t3", b["canonical"])
    st = hs.tito_close("M3", "t3", b["canonical"], False)
    check("rejected close resets to issued", st == "issued")
    a_again = hs.tito_authorize("M4", "t4", b["canonical"])
    check("reset ticket redeemable elsewhere", a_again["authorized"])
    hs.tito_close("M4", "t4", b["canonical"], True)

    print("— canonical learning (trailing-16 fallback)")
    hs.tito_record_issued("0300000000000011", 4_000, "sas:x", 2)
    a_learn = hs.tito_authorize("M5", "t5", "990300000000000011")
    check("18-digit scan with wrong predicted prefix authorizes",
          a_learn["authorized"], a_learn)
    with hs.lock:
        row = hs._conn.execute("SELECT canonical FROM tito_tickets WHERE "
                               "vn16='0300000000000011'").fetchone()
    check("row learned the scanned canonical",
          row["canonical"] == "990300000000000011", dict(row))
    hs.tito_close("M5", "t5", "990300000000000011", True)

    print("— sync merge")
    r = hs.tito_sync_merge("sas:smib-bb2", 6, [
        {"vn": "0200000000000021", "amountMc": 15_000, "state": "issued",
         "address": 1},
        {"vn": m["validationNumber"], "amountMc": 10_000, "state": "issued",
         "address": 1},                       # hub already redeemed: conflict
    ])
    check("sync inserts the new, keeps the hub's terminal state",
          r["inserted"] == 1 and r["conflicts"] == 1, r)
    a_syn = hs.tito_authorize("M6", "t6", "020200000000000021")
    check("synced fallback ticket authorizes", a_syn["authorized"]
          and a_syn["amountMc"] == 15_000, a_syn)
    hs.tito_close("M6", "t6", "020200000000000021", True)
    # never-downgrade: satellite says redeemed, hub had issued -> upgrade
    c2 = hs.tito_mint("sas:smib-bb2", 1, 3_000)
    hs.tito_record_issued(c2["validationNumber"], 3_000, "sas:smib-bb2", 1)
    r = hs.tito_sync_merge("sas:smib-bb2", 0, [
        {"vn": c2["validationNumber"], "amountMc": 3_000,
         "state": "redeemed", "redeemedBy": 1}])
    check("sync upgrades issued -> redeemed", r["updated"] == 1, r)
    # seq floor
    with hs.lock:
        cur = int(hs._conn.execute(
            "SELECT v FROM tito_meta WHERE k='seq_01'").fetchone()["v"])
    check("seq floored to the satellite's reported seq", cur >= 6, cur)
    # a replayed close (identical holder identity, as the /api/tito/sync
    # edge rebuilds it) consumes the ticket
    c3 = hs.tito_mint("sas:smib-bb2", 1, 2_000)
    hs.tito_record_issued(c3["validationNumber"], 2_000, "sas:smib-bb2", 1)
    hs.tito_authorize("sas:smib-bb2:1", f"sas:smib-bb2:1:{c3['canonical']}",
                      c3["canonical"])
    st = hs.tito_close("sas:smib-bb2:1",
                       f"sas:smib-bb2:1:{c3['canonical']}",
                       c3["canonical"], True)
    check("replayed close (same holder identity) consumes the ticket",
          st == "redeemed", st)

    print("— sync hardening (review 2026-07-08)")
    # incoming redeemPending is coerced to issued (a holderless pending
    # row would be permanently wedged)
    r = hs.tito_sync_merge("sas:smib-bb2", 0, [
        {"vn": "0400000000000031", "amountMc": 6_000,
         "state": "redeemPending", "address": 1}])
    check("incoming redeemPending inserted as issued", r["inserted"] == 1)
    a_c = hs.tito_authorize("MC", "tc", "040400000000000031")
    check("coerced ticket authorizes cleanly", a_c["authorized"], a_c)
    hs.tito_close("MC", "tc", "040400000000000031", True)
    # hostile validationSeq is capped, not honored
    from hub_store import MAX_SYNC_SEQ
    hs.tito_sync_merge("sas:evil", 10 ** 14 - 1, [])
    with hs.lock:
        cur = int(hs._conn.execute(
            "SELECT v FROM tito_meta WHERE k='seq_01'").fetchone()["v"])
    check("hostile seq floor capped", cur == MAX_SYNC_SEQ, cur)
    m_after = hs.tito_mint("sas:smib-bb2", 1, 55_000)
    check("mint still works after the capped floor",
          m_after["seq"] == MAX_SYNC_SEQ + 1, m_after)
    # a 2^63+ ticketNumber must not abort the merge (OverflowError)
    r = hs.tito_sync_merge("sas:smib-bb2", 0, [
        {"vn": "0500000000000001", "amountMc": 1_000, "state": "issued",
         "ticketNumber": 2 ** 70, "address": 1},
        {"vn": "0500000000000002", "amountMc": 1_000, "state": "issued",
         "address": 1}])
    check("oversized ticketNumber skipped, merge continues",
          r["inserted"] == 2, r)

    print("— mint reuse TTL")
    stale = hs.tito_mint("sas:ttl", 1, 9_000)
    with hs.lock:
        hs._conn.execute("UPDATE tito_tickets SET minted_at="
                         "'2026-01-01T00:00:00' WHERE canonical=?",
                         (stale["canonical"],))
        hs._conn.commit()
    fresh_m = hs.tito_mint("sas:ttl", 1, 9_000)
    check("stale open mint is NOT reused (TTL)",
          not fresh_m["reused"]
          and fresh_m["validationNumber"] != stale["validationNumber"],
          fresh_m)

    print("— seq high-water sidecar (quarantine double-mint guard)")
    check("sidecar file written on mint",
          os.path.exists(hs.seq_sidecar), hs.seq_sidecar)
    import json as _json
    side = _json.load(open(hs.seq_sidecar))
    check("sidecar carries the current seq_01",
          int(side.get("seq_01", 0)) >= MAX_SYNC_SEQ + 1, side)
    # simulate a quarantine-reset: fresh db, same sidecar path
    hs.close()
    os.remove(os.path.join(d, "hub.db"))
    for sfx in ("-wal", "-shm"):
        try:
            os.remove(os.path.join(d, "hub.db") + sfx)
        except OSError:
            pass
    hs = HubStore(os.path.join(d, "hub.db"))
    m_re = hs.tito_mint("sas:smib-bb2", 1, 60_000)
    check("fresh db after reset does NOT re-mint old numbers",
          m_re["seq"] > MAX_SYNC_SEQ + 1, m_re)

    print("— ephemeral refuses every money write")
    hs.ephemeral = True
    a_e = hs.tito_authorize("M", "t", "010100000000000001")
    check("ephemeral authorize fail-safe rejects",
          not a_e["authorized"] and "ephemeral" in a_e["reason"], a_e)
    for name, fn in (
            ("record", lambda: hs.tito_record_issued(
                "0600000000000001", 1000, "x", 1)),
            ("close", lambda: hs.tito_close("M", "t", "x" * 8, True)),
            ("sync", lambda: hs.tito_sync_merge("x", 0, [])),
            ("void", lambda: hs.tito_void("010100000000000001"))):
        try:
            fn()
            check(f"ephemeral {name} refused", False)
        except RuntimeError:
            check(f"ephemeral {name} refused", True)
    hs.ephemeral = False

    print("— void")
    v = hs.tito_mint("sas:smib-bb2", 1, 8_000)
    hs.tito_record_issued(v["validationNumber"], 8_000, "sas:smib-bb2", 1)
    prior = hs.tito_void(v["canonical"])
    check("void returns prior state", prior == "issued", prior)
    a_v = hs.tito_authorize("M", "t", v["canonical"])
    check("voided ticket rejects",
          not a_v["authorized"] and "voided" in a_v["reason"], a_v)

    print("— counts")
    counts = hs.tito_counts()
    check("counts total matches sum",
          counts["total"] == sum(v for k, v in counts.items()
                                 if k != "total"), counts)

    print("— tito_list (GET /api/tito/tickets backend)")
    hs_l, _dl = fresh()
    check("empty db lists an empty envelope",
          hs_l.tito_list() == {"total": 0, "tickets": []})
    # Four poked rows, one per interesting state, timestamps controlled
    # (and far in the past so a LIVE mint below sorts first). vn16s use
    # sid 77 so they can't collide with the live mint's sid-01 sequence.
    with hs_l.lock:
        hs_l._conn.executemany(
            "INSERT INTO tito_tickets (canonical, vn16, origin, address, "
            "amount_mc, state, ticket_number, pending_json, minted_at, "
            "issued_at, redeemed_at, redeemed_by) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)", [
                ("777700000000000001", "7700000000000001", "sas:smib-bb2",
                 1, 5_000, "minted", None, None,
                 "2020-01-03T10:00:00", None, None, None),
                ("777700000000000002", "7700000000000002", "sas:smib-bb2",
                 1, 10_000, "issued", 7, None, "2020-01-01T09:00:00",
                 "2020-01-04T11:00:00", None, None),
                ("777700000000000003", "7700000000000003", "sas:smib-bb2",
                 1, 15_000, "redeemed", None, None, "2020-01-01T09:00:00",
                 "2020-01-02T09:30:00", "2020-01-05T12:00:00", "IGT_AVP"),
                ("777700000000000004", "7700000000000004", "sas:smib-bb2",
                 1, 20_000, "redeemPending", None,
                 '{"machine":"sas:x:1","txn":"t","amountMc":20000,'
                 '"at":"2020-01-02T08:00:00"}', "2020-01-01T09:00:00",
                 "2020-01-02T08:00:00", None, None)])
        hs_l._conn.commit()
    r = hs_l.tito_list()
    check("envelope is exactly {total, tickets}",
          set(r) == {"total", "tickets"}, sorted(r))
    check("total counts every row (all states show)",
          r["total"] == 4 and len(r["tickets"]) == 4, r["total"])
    want_keys = {"canonical", "vn16", "state", "amountMc", "origin",
                 "address", "ticketNumber", "mintedAt", "issuedAt",
                 "redeemedAt", "redeemedBy", "pending"}
    check("row keys match the contract exactly",
          all(set(t) == want_keys for t in r["tickets"]),
          [sorted(t) for t in r["tickets"][:1]])
    check("newest-first by COALESCE(redeemed, issued, minted)",
          [t["canonical"][-1] for t in r["tickets"]]
          == ["3", "2", "1", "4"],
          [t["canonical"] for t in r["tickets"]])
    check("amountMc carries millicents from amount_mc",
          r["tickets"][0]["amountMc"] == 15_000, r["tickets"][0])
    check("pending only on the redeemPending row (null-safe elsewhere)",
          r["tickets"][3]["pending"] == {
              "machine": "sas:x:1", "txn": "t", "amountMc": 20000,
              "at": "2020-01-02T08:00:00"}
          and all(t["pending"] is None for t in r["tickets"][:3]),
          [t["pending"] for t in r["tickets"]])
    rs = hs_l.tito_list(state="issued")
    check("state filter is exact + total counts the filtered set",
          rs["total"] == 1
          and rs["tickets"][0]["canonical"] == "777700000000000002", rs)
    check("unknown state filters to empty, no error",
          hs_l.tito_list(state="nope") == {"total": 0, "tickets": []})
    rv = hs_l.tito_list(vid="777700000000000003")
    check("vid lookup by canonical",
          rv["total"] == 1
          and rv["tickets"][0]["vn16"] == "7700000000000003", rv)
    rv = hs_l.tito_list(vid="7700000000000003")
    check("vid lookup by vn16 hits the same row",
          rv["total"] == 1
          and rv["tickets"][0]["canonical"] == "777700000000000003", rv)
    check("vid miss is empty, not an error",
          hs_l.tito_list(vid="999999999999999999")
          == {"total": 0, "tickets": []})
    rv = hs_l.tito_list(vid="7700000000000003", state="minted", limit=0)
    check("vid short-circuits state/limit (api_vouchers parity)",
          rv["total"] == 1, rv)
    rp = hs_l.tito_list(limit=2)
    check("limit pages; total stays the full filtered count",
          rp["total"] == 4 and len(rp["tickets"]) == 2
          and rp["tickets"][0]["canonical"].endswith("3"), rp)
    check("offset continues the page",
          [t["canonical"][-1]
           for t in hs_l.tito_list(limit=2, offset=2)["tickets"]]
          == ["1", "4"])
    check("limit clamps low to 1",
          len(hs_l.tito_list(limit=0)["tickets"]) == 1)
    check("limit clamps high to 500 (no crash)",
          hs_l.tito_list(limit=10 ** 9)["total"] == 4)
    check("negative offset clamps to 0",
          hs_l.tito_list(offset=-5)
          ["tickets"][0]["canonical"].endswith("3"))
    check("uncastable limit/offset degrade to defaults",
          hs_l.tito_list(limit=None, offset="x")["total"] == 4)
    # a LIVE mint flows straight through (note: the mint's TTL sweep
    # prunes the 2020-dated poked 'minted' row — expected, still 4 rows)
    lm = hs_l.tito_mint("sas:smib-bb2", 1, 25_000)
    rl = hs_l.tito_list(vid=lm["canonical"])
    check("a live mint appears via vid",
          rl["total"] == 1 and rl["tickets"][0]["state"] == "minted"
          and rl["tickets"][0]["amountMc"] == 25_000, rl)
    check("live mint sorts first (fresh minted_at)",
          hs_l.tito_list()["tickets"][0]["canonical"] == lm["canonical"])
    # degrade path: a dead connection must answer empty, never raise
    hs_l.close()
    check("db fault degrades to an empty envelope (never raises)",
          hs_l.tito_list() == {"total": 0, "tickets": []})

    print("— TitoAuthority union")

    class FakeVS:
        def __init__(self):
            self.ids = {"473829105628374651"}
            self.calls = []

        def has_id(self, v):
            return v in self.ids

        def authorize_redemption(self, m, t, v):
            self.calls.append(("auth", m, t, v))
            return {"exc": 0, "amt": "50000", "creditType": "G2S_cashable",
                    "retry": False, "reason": "authorized"}

        def close_redemption(self, m, t, v, a):
            self.calls.append(("close", m, t, v, a))
            return "redeemed"

    vs = FakeVS()
    ta = TitoAuthority(vs, hs)
    dec = ta.authorize("E", "t1", "473829105628374651")
    check("G2S-issued id delegates to the VoucherStore",
          dec["exc"] == 0 and vs.calls[-1][0] == "auth", dec)
    d4 = hs.tito_mint("sas:smib-bb2", 1, 40_000)
    hs.tito_record_issued(d4["validationNumber"], 40_000, "sas:smib-bb2", 1)
    dec = ta.authorize("IGT_AVP", "t2", d4["canonical"])
    check("SAS ticket resolves from the hub ledger in voucher dialect",
          dec["exc"] == 0 and dec["amt"] == "40000"
          and dec["creditType"] == "G2S_cashable", dec)
    st = ta.close("IGT_AVP", "t2", d4["canonical"], "G2S_redeemed")
    check("union close consumes in the hub ledger", st == "redeemed")
    dec = ta.authorize("E", "t3", "111111111111111111")
    check("unknown to both ledgers -> exc 4", dec["exc"] == 4, dec)
    dec = ta.authorize("E2", "t4", d4["canonical"])
    check("redeemed hub ticket -> exc 2 in voucher dialect",
          dec["exc"] == 2, dec)

    hs.close()

    # -- v12 expired-ticket reaper: stamps, immortality, grace, display ----
    print("\n— v12 expired-ticket reaper")
    import sqlite3 as _sql
    import time as _time
    hs, d = fresh()

    def _issue(days_ago, expire_days, amt=10_000):
        m = hs.tito_mint("sas:smib-bb2", 1, amt)
        hs.tito_record_issued(m["validationNumber"], amt, "sas:smib-bb2", 1)
        back = _time.strftime("%Y-%m-%dT%H:%M:%S",
                              _time.localtime(_time.time()
                                              - days_ago * 86400))
        with hs.lock:
            hs._conn.execute(
                "UPDATE tito_tickets SET issued_at=?, expire_days=? "
                "WHERE canonical=?", (back, expire_days, m["canonical"]))
            hs._conn.commit()
        return m["canonical"]

    hs.set_host_setting("ticket_expire_days", "30")
    m = hs.tito_mint("sas:smib-bb2", 1, 5_000)
    hs.tito_record_issued(m["validationNumber"], 5_000, "sas:smib-bb2", 1)
    with hs.lock:
        row = hs._conn.execute(
            "SELECT expire_days FROM tito_tickets WHERE canonical=?",
            (m["canonical"],)).fetchone()
    check("issue stamps expire_days from the CURRENT setting",
          row["expire_days"] == 30, dict(row))
    hs.set_host_setting("ticket_expire_days", "")
    m0 = hs.tito_mint("sas:smib-bb2", 1, 5_000)
    hs.tito_record_issued(m0["validationNumber"], 5_000, "sas:smib-bb2", 1)
    with hs.lock:
        row = hs._conn.execute(
            "SELECT expire_days FROM tito_tickets WHERE canonical=?",
            (m0["canonical"],)).fetchone()
    check("setting 0/absent stamps NULL (paper printed no expiration)",
          row["expire_days"] is None, dict(row))

    immortal = _issue(days_ago=400, expire_days=None)   # pre-v12/never row
    fresh_exp = _issue(days_ago=10, expire_days=30)     # not yet expired
    graced = _issue(days_ago=35, expire_days=30)        # expired, in grace
    dead = _issue(days_ago=90, expire_days=30)          # expired + grace over
    with hs.lock:
        n = hs._reap_expired_locked(hs._conn)
        hs._conn.commit()
    check("reaper takes ONLY expired-past-grace rows", n == 1, n)
    with hs.lock:
        left = {r["canonical"] for r in hs._conn.execute(
            "SELECT canonical FROM tito_tickets WHERE state='issued'"
        ).fetchall()}
    check("never-expire row is immortal at any age", immortal in left)
    check("unexpired row survives", fresh_exp in left)
    check("expired-but-in-grace row survives (hub honors it a while "
          "longer than the machines)", graced in left)
    check("expired-past-grace row is GONE (unknown = what the paper "
          "promised)", dead not in left)

    lst = hs.tito_list(state="expired")
    check("?state=expired filter finds the in-grace row (display, "
          "no grace)",
          lst["total"] == 1 and lst["tickets"][0]["canonical"] == graced,
          lst["total"])
    all_rows = {t2["canonical"]: t2["state"]
                for t2 in hs.tito_list(limit=100)["tickets"]}
    check("list derives 'expired' display state for the in-grace row",
          all_rows.get(graced) == "expired")
    check("list keeps 'issued' for unexpired + immortal rows",
          all_rows.get(fresh_exp) == "issued"
          and all_rows.get(immortal) == "issued")
    dec = hs.tito_authorize("IGT_AVP", "tg", graced)
    check("in-grace expired ticket still REDEEMS (grace = money honored)",
          dec.get("authorized") is True, dec)
    hs.close()

    print("=" * 50)
    print(f"RESULT: {_p} passed, {_f} failed")
    return 1 if _f else 0


if __name__ == "__main__":
    sys.exit(main())
