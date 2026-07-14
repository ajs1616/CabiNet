#!/usr/bin/env python3
"""Standalone gate for the House-funded SAS AFT credit-push settlement
(G2SHost._settle_aft_credit_pushes) — the SAS peer of the G2S WAT escrow.

This is MONEY code: the funding account (the satellite's echoed accountId,
House when absent) is debited when a satellite reports a CONFIRMED
aft_transfer, and the satellite re-reports its last ~20 command results
every ~1 s, so the settlement MUST be idempotent (no double-debit) even
across a hub restart. These checks pin exactly that, plus the R1 funding
parity: echoed player debits, the short-player House fallback (with a
naming note), and old-satellite/junk-accountId degradation to House.

Run: python3 G2S/tools/test_aft_push.py   (expects "RESULT: N passed, 0 failed")
No live host, no network — a real AccountStore on a temp file + the real
method bound to a lightweight stand-in (the full engine init is irrelevant to
the settlement logic).
"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import g2s_host as G  # noqa: E402

_passed = 0
_failed = 0


def check(label, cond):
    global _passed, _failed
    if cond:
        _passed += 1
        print(f"  ✅ {label}")
    else:
        _failed += 1
        print(f"  ❌ {label}")


def _stub(store):
    class Stub:
        SAS_AFT_MAX_CENTS = 1_000_000
        _settle_aft_credit_pushes = G.G2SHost._settle_aft_credit_pushes

        def __init__(self):
            self.account_store = store
            self._aft_pushes_settled = set()
    return Stub()


def _house_cash(store):
    return store.get("house")["cashableMillicents"]


def main():
    tmp = tempfile.mkdtemp(prefix="aftpush_")
    store = G.AccountStore(os.path.join(tmp, "accounts.json"))
    eng = _stub(store)

    check("fresh House starts at 0", _house_cash(store) == 0)

    # a CONFIRMED $5.00 push -> House debited -$5.00 (cents*1000 millicents)
    ok5 = {"type": "aft_transfer", "ok": True, "outcome": "completed",
           "amountCents": 500, "txnId": "CN111"}
    eng._settle_aft_credit_pushes([ok5], "bb2/1")
    check("confirmed $5.00 debits House -500000 mc",
          _house_cash(store) == -500000)

    # idempotent across the satellite's ~every-5s re-report
    for _ in range(4):
        eng._settle_aft_credit_pushes([ok5], "bb2/1")
    check("4 re-reports do NOT double-debit", _house_cash(store) == -500000)

    # idempotent across a hub restart (fresh seen-set; the ledger once=True
    # ref is the hard guard)
    eng_restarted = _stub(store)
    eng_restarted._settle_aft_credit_pushes([ok5], "bb2/1")
    check("re-report after a hub restart does NOT double-debit",
          _house_cash(store) == -500000)

    # a REFUSED push must not move money
    eng._settle_aft_credit_pushes(
        [{"type": "aft_transfer", "ok": False, "outcome": "refused",
          "amountCents": 0, "txnId": "CN222"}], "bb2/1")
    check("a refused push does not debit", _house_cash(store) == -500000)

    # an UNCONFIRMED push (ok True but outcome not completed/partial) must not
    # move money — the credits may or may not have landed; debit only on a
    # positive terminal.
    eng._settle_aft_credit_pushes(
        [{"type": "aft_transfer", "ok": True, "outcome": "unconfirmed",
          "amountCents": 300, "txnId": "CN333"}], "bb2/1")
    check("an unconfirmed push does not debit", _house_cash(store) == -500000)

    # a PARTIAL success debits the amount that actually LANDED
    eng._settle_aft_credit_pushes(
        [{"type": "aft_transfer", "ok": True, "outcome": "partial",
          "amountCents": 250, "txnId": "CN444"}], "bb2/1")
    check("a distinct $2.50 partial stacks to -750000 mc",
          _house_cash(store) == -750000)

    # an over-ceiling amount is refused (report-tamper guard)
    eng._settle_aft_credit_pushes(
        [{"type": "aft_transfer", "ok": True, "outcome": "completed",
          "amountCents": 2_000_000, "txnId": "CN555"}], "bb2/1")
    check("an over-ceiling amount is NOT debited",
          _house_cash(store) == -750000)

    # a bad/negative/absent amount is skipped, not crashed
    for bad in ({"type": "aft_transfer", "ok": True, "outcome": "completed",
                 "amountCents": 0, "txnId": "CN666"},
                {"type": "aft_transfer", "ok": True, "outcome": "completed",
                 "amountCents": -5, "txnId": "CN777"},
                {"type": "aft_transfer", "ok": True, "outcome": "completed",
                 "txnId": "CN888"},
                {"type": "aft_transfer", "ok": True, "outcome": "completed",
                 "amountCents": 100, "txnId": ""}):
        eng._settle_aft_credit_pushes([bad], "bb2/1")
    check("bad/absent amounts and empty txnIds are skipped",
          _house_cash(store) == -750000)

    # non-aft records are ignored
    eng._settle_aft_credit_pushes([{"type": "legacy_bonus", "result": "ack"},
                                   {"type": "handpay_reset", "ok": True}],
                                  "bb2/1")
    check("non-aft_transfer records are ignored", _house_cash(store) == -750000)

    # malformed inputs never raise
    try:
        eng._settle_aft_credit_pushes(None, "bb2/1")
        eng._settle_aft_credit_pushes(["not a dict", 42, None], "bb2/1")
        eng._settle_aft_credit_pushes([{}], "bb2/1")
        raised = False
    except Exception:  # noqa: BLE001
        raised = True
    check("malformed commandResults never raise", not raised)

    # the ledger holds exactly the two real movements, correct refs+amounts
    led = [e for e in store.state["ledger"]
           if str(e.get("ref", "")).startswith("aftpush:")]
    check("ledger has exactly 2 aftpush movements", len(led) == 2)
    check("ledger refs + amounts are correct",
          sorted((e["ref"], e["deltaMillicents"]) for e in led)
          == [("aftpush:CN111", -500000), ("aftpush:CN444", -250000)])

    # ---- funding parity (echoed accountId) --------------------------------
    print("— funding parity (echoed accountId)")
    house_before = _house_cash(store)
    p, _ = store.create("Ruth")
    pid = p["id"]
    store.adjust(pid, d_cash=1_000_000)          # $10 bankroll

    # (a) the echoed player pays; House untouched
    eng._settle_aft_credit_pushes(
        [{"type": "aft_transfer", "ok": True, "outcome": "completed",
          "amountCents": 500, "txnId": "CNP01", "accountId": pid}], "bb2/1")
    check("echoed player debited $5.00",
          store.get(pid)["cashableMillicents"] == 500_000)
    check("House untouched when the player pays",
          _house_cash(store) == house_before)

    # (a') player-path idempotency: re-reports + a hub restart
    for _ in range(3):
        eng._settle_aft_credit_pushes(
            [{"type": "aft_transfer", "ok": True, "outcome": "completed",
              "amountCents": 500, "txnId": "CNP01", "accountId": pid}],
            "bb2/1")
    _stub(store)._settle_aft_credit_pushes(
        [{"type": "aft_transfer", "ok": True, "outcome": "completed",
          "amountCents": 500, "txnId": "CNP01", "accountId": pid}], "bb2/1")
    check("player re-settles (3x + restart) do NOT double-debit",
          store.get(pid)["cashableMillicents"] == 500_000
          and _house_cash(store) == house_before)

    # (b) short player -> House covers it, note names the player
    eng._settle_aft_credit_pushes(
        [{"type": "aft_transfer", "ok": True, "outcome": "completed",
          "amountCents": 2000, "txnId": "CNP02", "accountId": pid}], "bb2/1")
    check("short player is NOT partially debited",
          store.get(pid)["cashableMillicents"] == 500_000)
    check("House covered the short player's $20.00",
          _house_cash(store) == house_before - 2_000_000)
    rows = [e for e in store.state["ledger"]
            if e.get("ref") == "aftpush:CNP02"]
    check("exactly ONE ledger row for the fallback", len(rows) == 1)
    check("the fallback row debits House and names the short player",
          rows and rows[0]["accountId"] == "house"
          and pid in rows[0]["note"])

    # (b') fallback idempotency across a hub restart (ref reused, once=True
    # — the refused player adjust wrote no ledger row, so the single House
    # row blocks the re-run)
    _stub(store)._settle_aft_credit_pushes(
        [{"type": "aft_transfer", "ok": True, "outcome": "completed",
          "amountCents": 2000, "txnId": "CNP02", "accountId": pid}], "bb2/1")
    check("fallback re-settle after restart does NOT double-debit",
          store.get(pid)["cashableMillicents"] == 500_000
          and _house_cash(store) == house_before - 2_000_000)

    # (c) old-satellite compat: no accountId -> House, exactly as before
    eng._settle_aft_credit_pushes(
        [{"type": "aft_transfer", "ok": True, "outcome": "completed",
          "amountCents": 100, "txnId": "CNP03"}], "bb2/1")
    check("no accountId (old satellite) debits House",
          _house_cash(store) == house_before - 2_100_000)
    check("no accountId leaves the player untouched",
          store.get(pid)["cashableMillicents"] == 500_000)

    # (d) junk accountId types degrade to House, never raise
    try:
        for i, junk in enumerate((42, "", True, "   ")):
            eng._settle_aft_credit_pushes(
                [{"type": "aft_transfer", "ok": True, "outcome": "completed",
                  "amountCents": 10, "txnId": f"CNP0{4 + i}",
                  "accountId": junk}], "bb2/1")
        junk_raised = False
    except Exception:  # noqa: BLE001
        junk_raised = True
    check("junk accountId types never raise", not junk_raised)
    check("junk accountId types all debited House",
          _house_cash(store) == house_before - 2_140_000)

    # (e) unknown account -> House fallback + naming note
    eng._settle_aft_credit_pushes(
        [{"type": "aft_transfer", "ok": True, "outcome": "completed",
          "amountCents": 25, "txnId": "CNP08", "accountId": "p999"}], "bb2/1")
    check("unknown account debits House",
          _house_cash(store) == house_before - 2_165_000)
    rows = [e for e in store.state["ledger"]
            if e.get("ref") == "aftpush:CNP08"]
    check("unknown-account fallback: one House row naming p999",
          len(rows) == 1 and rows[0]["accountId"] == "house"
          and "p999" in rows[0]["note"])

    print("=" * 50)
    print(f"RESULT: {_passed} passed, {_failed} failed")
    return 1 if _failed else 0


if __name__ == "__main__":
    sys.exit(main())
