"""
Tests for the lite-deployment identity gate (deploy/LITE_DEPLOY.md):
_looks_like_cabinet_hub and CompanionHost's probe-before-post, mirroring
SAS/tests/test_hub_identity_gate.py (deliberate duplication — this daemon
already duplicates _default_gateway_ip against sas_host.py; no shared
module exists between them).

Full deployment: the hub owns the isolated slot VLAN and IS the default
gateway, so a flagless companion (-> resolve_hub_url with no --hub) always
finds it. Lite deployment: the host is a plain server on someone else's
LAN, so the default gateway is just their router — an auto-derived hub URL
must prove itself before this daemon ever reports live tap data to it.

Real (stdlib) HTTP servers on 127.0.0.1 stand in for "a CabiNet host" and
"not a CabiNet host" — no urllib.request mocking, no reader hardware.
"""

import http.server
import json
import sys
import threading
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from companion_host import CompanionHost, _looks_like_cabinet_hub
from reader import MockRfidReader

UID = "6CB16F06"


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
        raw = self.rfile.read(n)
        self.server.post_paths.append(self.path)
        # Ack everything we were sent, like the real hub: echo the highest
        # tapId in the report (ackTapId=-1 would ack nothing — tap 0 stays
        # queued because _take_ack only drops tapId <= ackTapId).
        try:
            taps = json.loads(raw).get("taps") or []
            ack = max((t.get("tapId", -1) for t in taps), default=-1)
        except Exception:
            ack = -1
        body = json.dumps({"ok": True, "ackTapId": ack}).encode()
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


@pytest.fixture
def cabinet_hub():
    srv = http.server.HTTPServer(("127.0.0.1", 0), _CabinetHubHandler)
    srv.post_paths = []
    srv.url = "http://127.0.0.1:%d" % srv.server_address[1]
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    yield srv
    srv.shutdown()
    srv.server_close()


@pytest.fixture
def not_cabinet_hub():
    srv = http.server.HTTPServer(("127.0.0.1", 0), _NotCabinetHandler)
    srv.post_paths = []
    srv.url = "http://127.0.0.1:%d" % srv.server_address[1]
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    yield srv
    srv.shutdown()
    srv.server_close()


def make_host(hub_url, hub_auto_derived=False):
    return CompanionHost(
        MockRfidReader([(0, UID)]), hub_url, "companion-test",
        g2s_egm="WMS_00:a0:a5:79:2d:a8", sas_smib="smib-bb2",
        sas_address=1, report_sec=1.0, hub_auto_derived=hub_auto_derived)


# ---------------------------------------------------------------------------
# _looks_like_cabinet_hub
# ---------------------------------------------------------------------------

def test_looks_like_cabinet_hub_true_on_real_engine_shape(cabinet_hub):
    assert _looks_like_cabinet_hub(cabinet_hub.url) is True


def test_looks_like_cabinet_hub_false_on_html_reply(not_cabinet_hub):
    assert _looks_like_cabinet_hub(not_cabinet_hub.url) is False


def test_looks_like_cabinet_hub_false_on_connection_refused():
    # Nothing listens on this port — must return False, never raise.
    assert _looks_like_cabinet_hub("http://127.0.0.1:1") is False


# ---------------------------------------------------------------------------
# CompanionHost.report() — probe-before-post
# ---------------------------------------------------------------------------

def test_report_skips_post_when_auto_derived_hub_fails_identity(
        not_cabinet_hub):
    h = make_host(not_cabinet_hub.url, hub_auto_derived=True)
    h.poll_reader(0.0)                    # queue one tap
    assert h.report(1.0) is False
    assert not_cabinet_hub.post_paths == []   # never POSTed
    assert h._identity_verified is False
    assert len(h.taps) == 1               # tap stays queued, nothing lost


def test_report_verifies_and_posts_once_hub_is_a_real_cabinet_host(
        cabinet_hub):
    h = make_host(cabinet_hub.url, hub_auto_derived=True)
    h.poll_reader(0.0)
    assert h.report(1.0) is True
    assert cabinet_hub.post_paths == ["/api/companion/report"]
    assert h._identity_verified is True
    assert len(h.taps) == 0                # the fake hub acked everything


def test_report_never_probes_when_hub_is_explicit(monkeypatch, cabinet_hub):
    def _boom(*a, **k):
        raise AssertionError("must not probe an explicit --hub — the "
                             "operator's word is trusted outright")
    import companion_host
    monkeypatch.setattr(companion_host, "_looks_like_cabinet_hub", _boom)
    h = make_host(cabinet_hub.url, hub_auto_derived=False)
    assert h._identity_verified is True   # never needed a probe
    h.poll_reader(0.0)
    assert h.report(1.0) is True
    assert cabinet_hub.post_paths == ["/api/companion/report"]
