"""
Tests for the durable TITO TicketStore (core/sas_ticket_store.py) —
VoucherStore-pattern semantics: atomic fsync persistence, quarantine on
corruption, the issued -> redeemPending -> redeemed/void state machine, and
idempotent retries.
"""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from core.sas_ticket_store import (
    ISSUED, REDEEMED, REDEEM_PENDING, VOID, TicketStore,
)

VN = "0012345678901234"


def _store(tmp_path, name="tickets.json"):
    return TicketStore(str(tmp_path / name))


class TestRecordIssued:
    def test_record_and_get(self, tmp_path):
        store = _store(tmp_path)
        out = store.record_issued(VN, 12345, 1, ticket_number=7)
        assert out["duplicate"] is False
        rec = store.get(VN)
        assert rec["state"] == ISSUED
        assert rec["amountCents"] == 12345
        assert rec["address"] == 1
        assert rec["ticketNumber"] == 7

    def test_duplicate_is_idempotent(self, tmp_path):
        store = _store(tmp_path)
        store.record_issued(VN, 12345, 1)
        out = store.record_issued(VN, 99999, 2)   # conflicting re-report
        assert out["duplicate"] is True
        assert store.get(VN)["amountCents"] == 12345   # original wins
        assert len(store.outstanding()) == 1

    def test_duplicate_never_resurrects_a_redeemed_ticket(self, tmp_path):
        store = _store(tmp_path)
        store.record_issued(VN, 12345, 1)
        store.authorize_redemption(1, VN)
        store.close_redemption(1, VN, redeemed=True)
        out = store.record_issued(VN, 12345, 1)   # stale re-report SAME amount
        assert out["duplicate"] is True
        assert store.get(VN)["state"] == REDEEMED

    def test_collision_reissues_over_redeemed(self, tmp_path):
        # A reused validation number (enhanced self-mint after a RAM clear)
        # with a DIFFERENT amount over a SPENT row is a fresh ticket -> the
        # spent row is re-issued so the paper in hand stays redeemable, never
        # silently dropped (the ticket-stuck bug).
        store = _store(tmp_path)
        store.record_issued(VN, 300500, 1)        # old $3,005 ticket
        store.authorize_redemption(1, VN)
        store.close_redemption(1, VN, redeemed=True)
        out = store.record_issued(VN, 2500, 1)    # fresh $25 reuse of the vn
        assert out["duplicate"] is False
        assert out["collision"] == "reissued"
        rec = store.get(VN)
        assert rec["state"] == ISSUED
        assert rec["amountCents"] == 2500          # the REAL fresh amount
        assert rec["reissuedFrom"]["state"] == REDEEMED
        # and it is now redeemable for $25
        dec = store.authorize_redemption(2, VN)
        assert dec["authorized"] is True
        assert dec["amount_cents"] == 2500

    def test_collision_reissues_over_void(self, tmp_path):
        store = _store(tmp_path)
        store.record_issued(VN, 300500, 1)
        store.void_ticket(VN)
        out = store.record_issued(VN, 2500, 1)
        assert out["duplicate"] is False
        assert out["collision"] == "reissued"
        assert store.get(VN)["state"] == ISSUED
        assert store.get(VN)["amountCents"] == 2500

    def test_apply_hub_reissue_overwrites_the_mirror(self, tmp_path):
        # The hub (redemption authority) knows cross-machine truth this
        # mirror never learns: on its collision=reissued verdict the local
        # row is overwritten to the fresh paper — even over a locally-live
        # ('issued') row the local conflict branch refused to touch.
        store = _store(tmp_path)
        store.record_issued(VN, 300500, 1)          # stale 'issued' mirror
        out = store.apply_hub_reissue(VN, 2500, 1, source="egm_4D")
        assert out["duplicate"] is False
        assert out["collision"] == "reissued"
        rec = store.get(VN)
        assert rec["amountCents"] == 2500
        assert rec["state"] == ISSUED
        assert rec["reissuedFrom"] == {
            "state": ISSUED, "amountCents": 300500,
            "at": rec["reissuedFrom"]["at"], "by": "hub"}

    def test_collision_conflict_keeps_live_ticket(self, tmp_path):
        # Two UNREDEEMED tickets cannot safely share a number: keep the live
        # one, flag the collision (loud), never overwrite a valid paper.
        store = _store(tmp_path)
        store.record_issued(VN, 12345, 1)         # still live/issued
        out = store.record_issued(VN, 2500, 2)    # different amount, vn reused
        assert out["duplicate"] is True
        assert out["collision"] == "conflict"
        assert store.get(VN)["amountCents"] == 12345   # live paper untouched

    def test_all_zero_validation_number_rejected(self, tmp_path):
        store = _store(tmp_path)
        try:
            store.record_issued("0000000000000000", 500, 1)
        except ValueError:
            pass
        else:
            raise AssertionError("all-zero vn must raise")

    def test_non_positive_amount_rejected(self, tmp_path):
        store = _store(tmp_path)
        for bad in (0, -5, None):
            try:
                store.record_issued(VN, bad, 1)
            except ValueError:
                pass
            else:
                raise AssertionError(f"amount {bad!r} must raise")


class TestPersistence:
    def test_reload_from_disk(self, tmp_path):
        path = str(tmp_path / "tickets.json")
        TicketStore(path).record_issued(VN, 12345, 1)
        store2 = TicketStore(path)
        assert store2.get(VN)["amountCents"] == 12345

    def test_pending_survives_restart(self, tmp_path):
        path = str(tmp_path / "tickets.json")
        store = TicketStore(path)
        store.record_issued(VN, 12345, 1)
        store.authorize_redemption(1, VN)
        store2 = TicketStore(path)              # host restart mid-redemption
        assert store2.get(VN)["state"] == REDEEM_PENDING
        # same machine retry re-draws the same authorization
        again = store2.authorize_redemption(1, VN)
        assert again["authorized"] and again["retry"]
        assert again["amount_cents"] == 12345

    def test_corrupt_file_quarantined_not_clobbered(self, tmp_path):
        path = tmp_path / "tickets.json"
        path.write_text("{ this is not json")
        store = TicketStore(str(path))
        assert store.outstanding() == []        # fresh
        quarantine = tmp_path / "tickets.json.corrupt"
        assert quarantine.exists()
        assert "not json" in quarantine.read_text()

    def test_store_file_is_valid_json(self, tmp_path):
        path = tmp_path / "tickets.json"
        TicketStore(str(path)).record_issued(VN, 12345, 1)
        data = json.loads(path.read_text())
        assert data["tickets"][VN]["state"] == ISSUED


class TestRedemptionStateMachine:
    def test_authorize_moves_to_pending(self, tmp_path):
        store = _store(tmp_path)
        store.record_issued(VN, 12345, 1)
        d = store.authorize_redemption(1, VN)
        assert d["authorized"] and d["amount_cents"] == 12345
        assert store.get(VN)["state"] == REDEEM_PENDING

    def test_unknown_ticket_rejected(self, tmp_path):
        d = _store(tmp_path).authorize_redemption(1, VN)
        assert not d["authorized"]
        assert d["amount_cents"] == 0
        assert "unknown" in d["reason"]

    def test_second_machine_rejected_while_pending(self, tmp_path):
        store = _store(tmp_path)
        store.record_issued(VN, 12345, 1)
        store.authorize_redemption(1, VN)
        d = store.authorize_redemption(2, VN)
        assert not d["authorized"]
        assert "already in process" in d["reason"]

    def test_issued_amount_overrides_reported(self, tmp_path):
        store = _store(tmp_path)
        store.record_issued(VN, 12345, 1)
        d = store.authorize_redemption(1, VN, reported_amount_cents=99999)
        assert d["authorized"]
        assert d["amount_cents"] == 12345
        assert "overrides" in d["reason"]

    def test_close_redeemed_consumes(self, tmp_path):
        store = _store(tmp_path)
        store.record_issued(VN, 12345, 1)
        store.authorize_redemption(1, VN)
        assert store.close_redemption(1, VN, redeemed=True) == REDEEMED
        d = store.authorize_redemption(1, VN)
        assert not d["authorized"] and "already redeemed" in d["reason"]

    def test_close_rejected_resets_to_issued(self, tmp_path):
        store = _store(tmp_path)
        store.record_issued(VN, 12345, 1)
        store.authorize_redemption(1, VN)
        assert store.close_redemption(1, VN, redeemed=False) == ISSUED
        d = store.authorize_redemption(2, VN)   # now redeemable elsewhere
        assert d["authorized"]

    def test_foreign_close_cannot_clobber_live_redemption(self, tmp_path):
        store = _store(tmp_path)
        store.record_issued(VN, 12345, 1)
        store.authorize_redemption(1, VN)
        # machine 2 (never authorized) reports a rejected close
        assert store.close_redemption(2, VN, redeemed=False) == REDEEM_PENDING

    def test_foreign_redeemed_close_cannot_consume(self, tmp_path):
        store = _store(tmp_path)
        store.record_issued(VN, 12345, 1)
        store.authorize_redemption(1, VN)
        # machine 2 (never authorized) claims it redeemed the ticket
        assert store.close_redemption(2, VN, redeemed=True) == REDEEM_PENDING
        # machine 1's live redemption is intact and still retryable
        d = store.authorize_redemption(1, VN)
        assert d["authorized"] and d["retry"]

    def test_redeemed_close_without_pending_changes_nothing(self, tmp_path):
        store = _store(tmp_path)
        store.record_issued(VN, 12345, 1)
        # no redemption was ever opened: a stray redeemed=True is a no-op
        assert store.close_redemption(1, VN, redeemed=True) == ISSUED
        assert store.get(VN)["state"] == ISSUED

    def test_duplicate_close_is_idempotent(self, tmp_path):
        store = _store(tmp_path)
        store.record_issued(VN, 12345, 1)
        store.authorize_redemption(1, VN)
        assert store.close_redemption(1, VN, redeemed=True) == REDEEMED
        assert store.close_redemption(1, VN, redeemed=True) == REDEEMED

    def test_close_unknown_returns_none(self, tmp_path):
        assert _store(tmp_path).close_redemption(1, VN, True) is None

    def test_void_blocks_redemption_and_clears_pending(self, tmp_path):
        store = _store(tmp_path)
        store.record_issued(VN, 12345, 1)
        store.authorize_redemption(1, VN)
        assert store.void_ticket(VN) == REDEEM_PENDING
        assert store.get(VN)["state"] == VOID
        d = store.authorize_redemption(1, VN)
        assert not d["authorized"] and "void" in d["reason"]


class TestHousekeeping:
    def test_outstanding_ordering_and_filtering(self, tmp_path):
        store = _store(tmp_path)
        store.record_issued("1111", 100, 1)
        store.record_issued("2222", 200, 1)
        store.record_issued("3333", 300, 1)
        store.authorize_redemption(1, "2222")
        store.close_redemption(1, "2222", redeemed=True)
        live = store.outstanding()
        assert [r["validationNumber"] for r in live] == ["1111", "3333"]

    def test_closed_history_pruned_live_never(self, tmp_path):
        store = _store(tmp_path)
        store.KEEP_CLOSED = 3
        store.record_issued("keepme", 100, 1)     # stays live forever
        for i in range(6):
            vn = f"90{i:02d}"
            store.record_issued(vn, 100, 1)
            store.authorize_redemption(1, vn)
            store.close_redemption(1, vn, redeemed=True)
        # trigger a prune with one more issue
        store.record_issued("last", 100, 1)
        closed = [r for r in store.state["tickets"].values()
                  if r["state"] == REDEEMED]
        assert len(closed) <= 3
        assert store.get("keepme") is not None    # live ticket untouched
