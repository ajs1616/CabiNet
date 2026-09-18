"""
Tests for the lite-deployment identity gate (deploy/LITE_DEPLOY.md):
_looks_like_cabinet_hub and HubReporter's probe-before-post.

Full deployment: the hub owns the isolated slot VLAN and IS the default
gateway, so --hub auto (-> _gateway_hub_url) is always right. Lite
deployment: the host is a plain server on someone else's LAN, so the
default gateway is just their router — an auto-derived URL must prove
itself before this daemon ever reports live machine state to it.

Real (stdlib) HTTP servers on 127.0.0.1 stand in for "a CabiNet host" and
"not a CabiNet host" — no urllib.request mocking, no serial hardware.
"""

import collections
import http.server
import json
import os
import sys
import threading
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

import sas_host
from sas_host import HubIdentityGate, HubReporter, _looks_like_cabinet_hub
from core.hub_ticket_client import HubTicketAuthority
from core.sas_ticket_store import TicketStore


# ---------------------------------------------------------------------------
# fake peers
# ---------------------------------------------------------------------------

class _CabinetHubHandler(http.server.BaseHTTPRequestHandler):
    """Answers /api/status with the real CabiNet shape and records every
    POST path it receives (so a test can assert whether a report landed)."""

    def do_GET(self):
        if self.path == "/api/status":
            body = json.dumps({"_engine": {"version": "0.10.0"}}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        else:
            self.send_response(404)
            self.end_headers()

    def do_POST(self):
        n = int(self.headers.get("Content-Length", 0))
        self.rfile.read(n)
        self.server.post_paths.append(self.path)
        body = b"{}"
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):        # keep pytest output clean
        pass


class _NotCabinetHandler(http.server.BaseHTTPRequestHandler):
    """A plain router/other web UI: 200 OK, but not the _engine JSON shape
    — the exact lite-mode trap (the gateway answers something, just not
    a CabiNet host)."""

    def do_GET(self):
        body = b"<html>not a cabinet host</html>"
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        n = int(self.headers.get("Content-Length", 0))
        self.rfile.read(n)
        self.server.post_paths.append(self.path)
        self.send_response(200)
        self.end_headers()

    def log_message(self, *args):
        pass


class _TitoHubHandler(http.server.BaseHTTPRequestHandler):
    """A fake hub that answers BOTH /api/status (the identity probe) and
    /api/tito/mint (so a test can prove a real ticket POST reaches it once
    the gate opens) — everything else gets a bare {"ok": true}."""

    def do_GET(self):
        if self.path == "/api/status":
            body = json.dumps({"_engine": {}}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        else:
            self.send_response(404)
            self.end_headers()

    def do_POST(self):
        n = int(self.headers.get("Content-Length", 0))
        self.rfile.read(n)
        self.server.post_paths.append(self.path)
        if self.path.endswith("/mint"):
            body = json.dumps({"ok": True,
                               "validationNumber": "0100000000000099",
                               "systemId": 1, "seq": 99}).encode()
        else:
            body = json.dumps({"ok": True}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


def _start(handler_cls):
    srv = http.server.HTTPServer(("127.0.0.1", 0), handler_cls)
    srv.post_paths = []
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    return srv, "http://127.0.0.1:%d" % srv.server_address[1]


def _make_reporter(hub_url, verify_identity, identity_gate=None):
    """A HubReporter cheap enough to construct without a real SASPoller —
    snapshot() only touches poller.state.{online,last_seen}, the stats
    dict's known keys, and store.lock/store.state['tickets']."""

    class _State:
        online = False
        last_seen = None

    class _Poller:
        state = _State()

    class _Store:
        lock = threading.Lock()
        state = {"tickets": {}}

    stats = {"polls": 0, "events": 0, "meter_changes": 0, "last_meters": {},
             "aft": None}
    return HubReporter(hub_url, "smib-test", "(mock)", 1, _Poller(), stats,
                       collections.deque(maxlen=20), _Store(),
                       verify_identity=verify_identity,
                       identity_gate=identity_gate)


# ---------------------------------------------------------------------------
# _looks_like_cabinet_hub
# ---------------------------------------------------------------------------

def test_looks_like_cabinet_hub_true_on_real_engine_shape():
    srv, url = _start(_CabinetHubHandler)
    try:
        assert _looks_like_cabinet_hub(url) is True
    finally:
        srv.shutdown()
        srv.server_close()


def test_looks_like_cabinet_hub_false_on_html_reply():
    srv, url = _start(_NotCabinetHandler)
    try:
        assert _looks_like_cabinet_hub(url) is False
    finally:
        srv.shutdown()
        srv.server_close()


def test_looks_like_cabinet_hub_false_on_connection_refused():
    # Nothing listens on this port — must return False, never raise.
    assert _looks_like_cabinet_hub("http://127.0.0.1:1") is False


# ---------------------------------------------------------------------------
# HubReporter — probe-before-post
# ---------------------------------------------------------------------------

def test_hub_reporter_skips_post_while_identity_unverified(monkeypatch):
    monkeypatch.setattr(sas_host, "REPORT_SEC", 0.05)
    srv, url = _start(_NotCabinetHandler)
    reporter = _make_reporter(url, verify_identity=True)
    t = threading.Thread(target=reporter.run, daemon=True)
    t.start()
    try:
        time.sleep(0.3)                      # several probe cycles
        assert srv.post_paths == []          # no report ever POSTed
        assert reporter._identity_verified is False
    finally:
        reporter.stop = True
        t.join(timeout=2)
        srv.shutdown()
        srv.server_close()


def test_hub_reporter_verifies_and_reports_once_hub_answers(monkeypatch):
    monkeypatch.setattr(sas_host, "REPORT_SEC", 0.05)
    not_cabinet_srv, not_cabinet_url = _start(_NotCabinetHandler)
    cabinet_srv, cabinet_url = _start(_CabinetHubHandler)
    reporter = _make_reporter(not_cabinet_url, verify_identity=True)
    t = threading.Thread(target=reporter.run, daemon=True)
    t.start()
    try:
        time.sleep(0.15)
        assert not_cabinet_srv.post_paths == []
        # The satellite gets pointed at the real hub (e.g. --hub corrected,
        # or the router happens to change) — the NEXT probe verifies it.
        reporter.hub_base = cabinet_url
        reporter.url = cabinet_url + "/api/sas/report"
        # The reporter POSTs over one persistent keep-alive client bound
        # to the URL at construction (in production a --hub change is a
        # unit restart); re-point it the same way a restart would.
        reporter._http = sas_host.KeepAliveHTTPClient(reporter.url, timeout=4)
        time.sleep(0.3)
        assert reporter._identity_verified is True
        assert "/api/sas/report" in cabinet_srv.post_paths
    finally:
        reporter.stop = True
        t.join(timeout=2)
        not_cabinet_srv.shutdown()
        not_cabinet_srv.server_close()
        cabinet_srv.shutdown()
        cabinet_srv.server_close()


def test_hub_reporter_never_probes_when_verify_identity_false(monkeypatch):
    def _boom(*a, **k):
        raise AssertionError("must not probe when verify_identity=False — "
                             "an explicit --hub is trusted outright")
    monkeypatch.setattr(sas_host, "_looks_like_cabinet_hub", _boom)
    monkeypatch.setattr(sas_host, "REPORT_SEC", 0.05)
    srv, url = _start(_CabinetHubHandler)
    reporter = _make_reporter(url, verify_identity=False)
    assert reporter._identity_verified is True   # never needed a probe
    t = threading.Thread(target=reporter.run, daemon=True)
    t.start()
    try:
        time.sleep(0.15)
        assert "/api/sas/report" in srv.post_paths
    finally:
        reporter.stop = True
        t.join(timeout=2)
        srv.shutdown()
        srv.server_close()


# ---------------------------------------------------------------------------
# HubIdentityGate — shared verified-flag (HubReporter's probe flips it,
# HubTicketAuthority's allow_network reads it — see sas_host.main())
# ---------------------------------------------------------------------------

def test_hub_identity_gate_starts_closed_and_flips_once():
    gate = HubIdentityGate()
    assert gate.allow() is False
    gate.verified = True
    assert gate.allow() is True


def test_hub_reporter_success_flips_a_shared_gate(monkeypatch):
    # HubReporter's own probe is what HubTicketAuthority's gate rides on in
    # main() — a successful probe must flip the SHARED gate object too, not
    # just the reporter's own private flag.
    monkeypatch.setattr(sas_host, "REPORT_SEC", 0.05)
    srv, url = _start(_CabinetHubHandler)
    gate = HubIdentityGate()
    reporter = _make_reporter(url, verify_identity=True, identity_gate=gate)
    t = threading.Thread(target=reporter.run, daemon=True)
    t.start()
    try:
        time.sleep(0.2)
        assert reporter._identity_verified is True
        assert gate.verified is True
        assert gate.allow() is True
    finally:
        reporter.stop = True
        t.join(timeout=2)
        srv.shutdown()
        srv.server_close()


# ---------------------------------------------------------------------------
# HubTicketAuthority + allow_network — the ticket leg must hold on the SAME
# gate as HubReporter's state reports (fixes the bypass the review found:
# tito traffic used to ignore the identity gate entirely).
# ---------------------------------------------------------------------------

def test_ticket_authority_default_allow_network_is_unaffected(tmp_path):
    # No gate at all (explicit --hub / bare construction) — the class stays
    # exactly as generic as before; every existing hub_ticket_client test
    # keeps passing untouched.
    srv, url = _start(_TitoHubHandler)
    store = HubTicketAuthority(
        url, "smib-test", TicketStore(str(tmp_path / "tickets.json")),
        journal_path=str(tmp_path / "journal.json"),
        start_sync_thread=False)
    try:
        rec = store.mint_validation_number(500, 1)
        assert "/api/tito/mint" in srv.post_paths
        assert rec["system_id"] == 1
    finally:
        srv.shutdown()
        srv.server_close()


@pytest.mark.skipif(
    os.name == "nt",
    reason="local-fallback mint needs TicketStore's durable write, whose "
    "directory fsync (sas_ticket_store._save_locked) is POSIX-only — "
    "pre-existing Windows limitation, target platform is the Pi",
)
def test_ticket_authority_gate_blocks_then_allows_after_verify(tmp_path):
    srv, url = _start(_TitoHubHandler)
    gate = HubIdentityGate()
    store = HubTicketAuthority(
        url, "smib-test", TicketStore(str(tmp_path / "tickets.json")),
        journal_path=str(tmp_path / "journal.json"),
        start_sync_thread=False, allow_network=gate.allow)
    try:
        # auto-derived + unverified: the mint call must NOT reach the fake
        # hub at all — it takes the EXACT same local sid-XX fallback path
        # as a real network failure (mint -> local fallback).
        rec = store.mint_validation_number(500, 1)
        assert srv.post_paths == []
        assert rec["system_id"] == store.fallback_sid

        # The gate opens (mirrors HubReporter's probe succeeding elsewhere
        # in the same process) — the VERY NEXT call reaches the hub, no
        # restart of the ticket authority needed.
        gate.verified = True
        rec2 = store.mint_validation_number(700, 1)
        assert "/api/tito/mint" in srv.post_paths
        assert rec2["system_id"] == 1
    finally:
        srv.shutdown()
        srv.server_close()


def test_ticket_authority_gate_also_rejects_authorize_and_journals_close(
        tmp_path):
    # The gate must cover every _post caller, not just mint — authorize
    # takes its own "hub unreachable" REJECT path (no local authority, by
    # design) and close journals for later sync, identically to a real
    # network failure.
    srv, url = _start(_TitoHubHandler)
    gate = HubIdentityGate()      # never verified in this test
    store = HubTicketAuthority(
        url, "smib-test", TicketStore(str(tmp_path / "tickets.json")),
        journal_path=str(tmp_path / "journal.json"),
        start_sync_thread=False, allow_network=gate.allow)
    try:
        auth = store.authorize_redemption(1, "0100000000000001")
        assert auth["authorized"] is False
        assert "hub unreachable" in auth["reason"]

        result = store.close_redemption(1, "0100000000000001", True)
        assert result is None                 # journaled, not confirmed
        assert store._closes                  # something actually journaled

        assert srv.post_paths == []           # the fake hub was never hit
    finally:
        srv.shutdown()
        srv.server_close()
