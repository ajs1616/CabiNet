"""
Tests for core/hub_ticket_client.py — the satellite's handle on the hub
validation authority (hub.db phase 2).

A stub hub (in-thread http.server) proves the happy paths byte-for-byte at
the JSON edge; a dead-port client proves every fallback: sid-02 local mint,
authorize reject ("hub unreachable"), journaled closes surviving a restart,
outage pending-reuse, and the sync payload draining it all.
"""

import json
import os
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from core.hub_ticket_client import (FALLBACK_SYSTEM_ID,
                                    HubTicketAuthority, derive_fallback_sid)
from core.sas_ticket_store import TicketStore

SMIB_SID = derive_fallback_sid("smib-bb2")     # stable per-satellite 02..09


class StubHub:
    """Records every /api/tito POST; per-op canned replies, overridable."""

    def __init__(self):
        self.hits = []
        self.replies = {
            "mint": {"ok": True, "validationNumber": "0100000000000042",
                     "systemId": 1, "seq": 42, "reused": False},
            "issued": {"ok": True, "state": "issued", "duplicate": False},
            "authorize": {"ok": True, "authorized": True, "amountCents": 25,
                          "reason": "authorized", "retry": False},
            "commit": {"ok": True, "state": "redeemed"},
            "sync": {"ok": True, "inserted": 0, "updated": 0,
                     "conflicts": 0, "closesApplied": 0},
        }
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                body = json.loads(self.rfile.read(
                    int(self.headers["Content-Length"])))
                op = self.path.rsplit("/", 1)[-1]
                outer.hits.append((op, body))
                out = json.dumps(outer.replies.get(
                    op, {"ok": False, "error": "unknown op"})).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(out)))
                self.end_headers()
                self.wfile.write(out)

            def log_message(self, *a):
                pass

        self.server = HTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.server.serve_forever,
                         daemon=True).start()

    @property
    def url(self):
        return f"http://127.0.0.1:{self.server.server_port}"

    def last(self, op):
        return next(b for o, b in reversed(self.hits) if o == op)

    def stop(self):
        self.server.shutdown()


@pytest.fixture()
def hub():
    h = StubHub()
    yield h
    h.stop()


@pytest.fixture()
def local(tmp_path):
    return TicketStore(str(tmp_path / "tickets.json"))


def make_client(hub_url, local, tmp_path, **kw):
    kw.setdefault("timeout", 0.4)
    return HubTicketAuthority(hub_url, "smib-bb2", local,
                              journal_path=str(tmp_path / "journal.json"),
                              start_sync_thread=False, **kw)


DEAD = "http://127.0.0.1:1"     # nothing listens on port 1


# -- happy paths --------------------------------------------------------------

def test_mint_maps_hub_reply_to_ticketstore_shape(hub, local, tmp_path):
    c = make_client(hub.url, local, tmp_path)
    m = c.mint_validation_number(25, 1)
    assert m["validation_number"] == "0100000000000042"
    assert m["system_id"] == 1
    assert m["amount_cents"] == 25
    sent = hub.last("mint")
    assert sent == {"smibId": "smib-bb2", "address": 1, "amountCents": 25}


def test_record_issued_records_locally_and_pushes(hub, local, tmp_path):
    c = make_client(hub.url, local, tmp_path)
    out = c.record_issued("0100000000000042", 25, 1, ticket_number=2,
                          source="egm_4D")
    assert out["duplicate"] is False
    assert out["record"]["validationNumber"] == "0100000000000042"
    # the satellite keeps its own forensic copy
    assert local.get("0100000000000042")["state"] == "issued"
    sent = hub.last("issued")
    assert sent["validationNumber"] == "0100000000000042"
    assert sent["amountCents"] == 25
    assert sent["ticketNumber"] == 2


def test_hub_reissue_verdict_mirrors_locally(hub, local, tmp_path):
    # COLLISION containment e2e (2026-07-12): the local mirror holds the vn
    # as a still-'issued' old ticket (it never learns the hub redeemed it at
    # another machine). A fresh capture of a DIFFERENT amount hits the local
    # CONFLICT branch — but the hub, which knows the prior paper is spent,
    # answers collision=reissued, and the facade must mirror that verdict so
    # the returned record/tape/forensics describe the FRESH paper.
    local.record_issued("0054502480006616", 300500, 1)   # old $3,005 mirror
    hub.replies["issued"] = {"ok": True, "state": "issued",
                             "duplicate": False, "collision": "reissued"}
    c = make_client(hub.url, local, tmp_path)
    out = c.record_issued("0054502480006616", 2500, 1, source="egm_4D")
    assert out["duplicate"] is False
    assert out["collision"] == "reissued"
    rec = local.get("0054502480006616")
    assert rec["amountCents"] == 2500                 # fresh paper's amount
    assert rec["state"] == "issued"
    assert rec["reissuedFrom"]["by"] == "hub"
    assert rec["reissuedFrom"]["amountCents"] == 300500
    # the hub saw the capture's REAL amount (never the stale mirror's)
    assert hub.last("issued")["amountCents"] == 2500


def test_collision_print_during_outage_journals_and_replays(hub, local,
                                                            tmp_path):
    # The outage leg: a collision print while the hub is DOWN keeps the OLD
    # local row (conservative conflict branch), so the ledger sync can never
    # carry the fresh ticket — the raw capture must journal and replay
    # through /api/tito/issued when the hub returns.
    local.record_issued("0054502480006616", 300500, 1)   # old mirror row
    dead = make_client(DEAD, local, tmp_path)
    out = dead.record_issued("0054502480006616", 2500, 1, source="egm_4D")
    assert out["duplicate"] is True                   # local kept the old row
    # ... but the raw capture survived in the journal, across a restart:
    c2 = make_client(hub.url, local, tmp_path)        # same journal_path
    assert c2.sync_now() is True
    sent = hub.last("issued")
    assert sent["validationNumber"] == "0054502480006616"
    assert sent["amountCents"] == 2500                # the REAL fresh amount
    assert c2._issued == []                           # journal drained
    # a second sync must not replay it again
    hub.hits.clear()
    assert c2.sync_now() is True
    assert not [h for h in hub.hits if h[0] == "issued"]


def test_authorize_sends_canonical_from_raw(hub, local, tmp_path):
    c = make_client(hub.url, local, tmp_path)
    raw = bytes.fromhex("473829105628374651")     # an AVP voucher's 9 bytes
    dec = c.authorize_redemption(1, "3829105628374651", 0,
                                 validation_raw=raw)
    assert dec == {"authorized": True, "amount_cents": 25,
                   "reason": "authorized", "retry": False}
    sent = hub.last("authorize")
    assert sent["canonical"] == "473829105628374651"
    assert sent["vn16"] == "3829105628374651"


def test_authorize_without_raw_sends_vn_only(hub, local, tmp_path):
    c = make_client(hub.url, local, tmp_path)
    c.authorize_redemption(1, "0100000000000042", 10)
    sent = hub.last("authorize")
    assert "canonical" not in sent
    assert sent["reportedAmountCents"] == 10


def test_non_bcd_raw_falls_back_to_vn(hub, local, tmp_path):
    c = make_client(hub.url, local, tmp_path)
    # 0xFF nibbles hex to 'f' characters — not barcode digits
    c.authorize_redemption(1, "0100000000000042",
                           validation_raw=b"\xff" * 9)
    assert "canonical" not in hub.last("authorize")


def test_close_posts_commit(hub, local, tmp_path):
    c = make_client(hub.url, local, tmp_path)
    raw = bytes.fromhex("010100000000000042")
    st = c.close_redemption(1, "0100000000000042", True, validation_raw=raw)
    assert st == "redeemed"
    sent = hub.last("commit")
    assert sent["canonical"] == "010100000000000042"
    assert sent["redeemed"] is True


def test_hub_refusal_is_a_fallback_not_a_crash(hub, local, tmp_path):
    hub.replies["mint"] = {"ok": False, "error": "mint unavailable"}
    c = make_client(hub.url, local, tmp_path)
    m = c.mint_validation_number(30, 1)
    assert m["system_id"] == SMIB_SID


def test_fallback_sid_is_stable_and_per_satellite():
    assert derive_fallback_sid("smib-bb2") == derive_fallback_sid("smib-bb2")
    assert FALLBACK_SYSTEM_ID <= derive_fallback_sid("anything") <= 9


# -- hub-down fallbacks --------------------------------------------------------

def test_mint_falls_back_to_local_derived_sid(local, tmp_path):
    c = make_client(DEAD, local, tmp_path)
    m = c.mint_validation_number(30, 1)
    assert m["system_id"] == SMIB_SID == c.fallback_sid
    assert m["validation_number"].startswith(f"{SMIB_SID:02d}")
    # persisted in the local store's pending mints
    assert local.find_open_pending(1, 30) is not None


def test_outage_pending_reuse(local, tmp_path):
    c = make_client(DEAD, local, tmp_path)
    m = c.mint_validation_number(30, 1)
    again = c.find_open_pending(1, 30)
    assert again is not None
    assert again["validationNumber"] == m["validation_number"]


def test_no_outage_no_local_pending_consult(hub, local, tmp_path):
    c = make_client(hub.url, local, tmp_path)
    assert c.find_open_pending(1, 30) is None   # hub reuses atomically


def test_authorize_rejects_when_hub_down(local, tmp_path):
    c = make_client(DEAD, local, tmp_path)
    # even a ticket the LOCAL store knows must NOT locally authorize —
    # split authority is how a ticket pays twice
    local.record_issued("0100000000000042", 25, 1)
    dec = c.authorize_redemption(1, "0100000000000042")
    assert dec["authorized"] is False
    assert "hub unreachable" in dec["reason"]


def test_close_journals_when_hub_down(local, tmp_path):
    c = make_client(DEAD, local, tmp_path)
    raw = bytes.fromhex("010100000000000042")
    st = c.close_redemption(1, "0100000000000042", True, validation_raw=raw)
    assert st is None
    data = json.load(open(tmp_path / "journal.json"))
    assert data["closes"] == [{"validation": "010100000000000042",
                               "redeemed": True, "address": 1}]


def test_journal_survives_restart(local, tmp_path):
    c = make_client(DEAD, local, tmp_path)
    c.close_redemption(1, "0100000000000042", True,
                       validation_raw=bytes.fromhex("010100000000000042"))
    c2 = make_client(DEAD, local, tmp_path)
    assert c2._closes == [{"validation": "010100000000000042",
                           "redeemed": True, "address": 1}]
    assert c2._dirty.is_set()


def test_record_issued_survives_hub_down(local, tmp_path):
    c = make_client(DEAD, local, tmp_path)
    out = c.record_issued("0100000000000042", 25, 1)
    assert out["duplicate"] is False
    assert local.get("0100000000000042") is not None


# -- sync -----------------------------------------------------------------------

def test_sync_pushes_ledger_seq_and_closes(hub, local, tmp_path):
    c = make_client(hub.url, local, tmp_path)
    local.mint_validation_number(30, 1, system_id=2)
    local.record_issued("0200000000000001", 30, 1, ticket_number=7)
    with c._jlock:
        c._closes.append({"validation": "473829105628374651",
                          "redeemed": True, "address": 1})
    assert c.sync_now() is True
    sent = hub.last("sync")
    assert sent["validationSeq"] == 1
    assert [t["validationNumber"] for t in sent["tickets"]] \
        == ["0200000000000001"]
    assert sent["closes"] == [{"validation": "473829105628374651",
                               "redeemed": True, "address": 1}]
    # journal drained on success
    assert c._closes == []
    assert not c._dirty.is_set()


def test_sync_failure_keeps_journal(local, tmp_path):
    c = make_client(DEAD, local, tmp_path)
    c.close_redemption(1, "0100000000000042", True,
                       validation_raw=bytes.fromhex("010100000000000042"))
    assert c.sync_now() is False
    assert len(c._closes) == 1
    assert c._dirty.is_set()


def test_reporter_snapshot_proxies_local_state(hub, local, tmp_path):
    c = make_client(hub.url, local, tmp_path)
    local.record_issued("0100000000000042", 25, 1)
    with c.lock:                                   # HubReporter's exact idiom
        tickets = list(c.state.get("tickets", {}).values())
    assert len(tickets) == 1
    assert c.MAX_TICKET_CENTS == local.MAX_TICKET_CENTS


def test_dirty_mark_during_sync_survives(hub, local, tmp_path):
    """The review's check-then-clear race: a fallback landing during the
    unlocked sync round trip must keep its dirty mark."""
    c = make_client(hub.url, local, tmp_path)
    c._dirty.set()
    real_post = c._post

    def post_with_midflight_fallback(op, payload):
        out = real_post(op, payload)
        if op == "sync":
            c._mark_dirty()          # fallback lands mid-round-trip
        return out

    c._post = post_with_midflight_fallback
    assert c.sync_now() is True
    assert c._dirty.is_set()         # NOT clobbered by the clear


def test_void_posts_to_hub_and_mirrors_locally(hub, local, tmp_path):
    hub.replies["void"] = {"ok": True, "prior": "issued"}
    c = make_client(hub.url, local, tmp_path)
    local.record_issued("0100000000000042", 25, 1)
    st = c.void_ticket("0100000000000042")
    assert st == "issued"                     # prior state, per TicketStore
    assert local.get("0100000000000042")["state"] == "void"
    assert hub.last("void")["vn16"] == "0100000000000042"


def test_void_survives_hub_down(local, tmp_path):
    c = make_client(DEAD, local, tmp_path)
    local.record_issued("0100000000000042", 25, 1)
    assert c.void_ticket("0100000000000042") == "issued"   # prior state
    assert local.get("0100000000000042")["state"] == "void"
