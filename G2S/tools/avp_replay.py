#!/usr/bin/env python3
"""
AVP replay simulator — validates g2s_host.py against the real machine's bytes
BEFORE bench day.

Plays the IGT AVP's role:
  * replays the EXACT captured requests (extracted verbatim from
    debug-captures/igt_exchange_20250725_235016.txt) at the host's /G2S;
  * runs a fake EGM endpoint on :8080 (the capture's egmLocation port) to
    receive the host's outbound POSTs, replying with an EGM-style g2sAck;
  * walks the full spec join choreography and validates every framing rule:
      transport ack byte-shape + cert-less
      sync replies carry ONLY a message-level g2sAck
      MSX003 before commsOnLine
      commsOnLineAck: sessionId echo, host commandId=1, syncTimer >= 15000
      commsDisabledAck + setCommsState(enable=true) sequence and ordering
      epoch reset on re-handshake (host commandId restarts at 1)

Usage:  terminal A (from G2S/):  python3 g2s_host.py
        terminal B (from G2S/):  python3 tools/avp_replay.py
Exits 0 if all checks pass.

LOCAL ONLY (GR-04): this gate MUTATES host state — it joins fake associations
(IGT_00RESTARTGHOST), permanently persists fixture vouchers into the
target's data/voucher_state.json under the REAL EGM id, and CLEARS + reseeds
the target's data/config_inventory.json with fixture option inventories
(GR-10). It must only ever run against a local bench instance, never the
production Pi (a 2026-07-02 run against the live Pi polluted its voucher
store — that is the incident this guard exists for). Before any traffic, the tool fetches <host-url>/api/status
and REFUSES to run if any association looks like a live production join
(commsState onLine/sync/offline) unless --force is passed.

  --host-url  base URL of the host under test (default http://127.0.0.1:8081)
  --data-dir  the host's data/ dir holding voucher_state.json, for the
              persistence checks (default: ../data relative to this file)
  --force     override the live-host refusal (you had better be sure)
"""

import argparse
import html
import http.client
import json
import queue
import random
import re
import sys
import threading
import time
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

WSDL_NS = "http://www.gamingstandards.com/wsdl/g2s/v1.0"
SCHEMA_NS = "http://www.gamingstandards.com/g2s/schemas/v1.0.3"
SOAP_NS = "http://schemas.xmlsoap.org/soap/envelope/"
EGM_ID = "IGT_00012E492815"
# Defaults stay LOCAL (GR-04). Overridable via --host-url/--data-dir in
# main() — the globals are rebound there before any traffic is sent.
HOST_BASE = "http://127.0.0.1:8081"
HOST_URL = HOST_BASE + "/G2S"
STATUS_URL = HOST_BASE + "/api/status"
DATA_DIR = Path(__file__).resolve().parent.parent / "data"
EGM_PORT = 8080
CAPTURE = Path(__file__).resolve().parent.parent / "debug-captures" / \
    "igt_exchange_20250725_235016.txt"
# The first REAL response payloads the AVP ever delivered (post-d556ad2 live
# session 2026-07-02) — used verbatim wherever the choreography allows.
FIRST_LIGHT = Path(__file__).resolve().parent.parent / "debug-captures" / \
    "first-light-20260702"
# Fixtures the gate loads by name (first_light_reply / first_light_gameplay_ids).
# The preflight in main() verifies these exist so a missing-fixture run aborts
# with a clear message instead of a mid-gate FileNotFoundError traceback.
FIRST_LIGHT_REQUIRED = (
    "descriptorList_sid1003.xml",
    "eventHandlerProfile.xml",
    "setEventSubAck.xml",
    "setKeepAliveAck.xml",
    "supportedEvents_first30.xml",
)

PASS = 0
FAIL = 0


def check(label, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ✅ {label}")
    else:
        FAIL += 1
        print(f"  ❌ {label}  {detail}")
    return cond


def now_iso():
    dt = datetime.now(timezone.utc)
    return dt.strftime("%Y-%m-%dT%H:%M:%S.") + f"{dt.microsecond // 1000:03d}Z"


CLOCK_SKEW_TEST_SEC = 1140  # ~19min — matches the real AVP's observed drift


def skewed_iso(behind=CLOCK_SKEW_TEST_SEC):
    """A spec-form dateTime `behind` seconds in the past — a drifted EGM clock."""
    dt = datetime.now(timezone.utc) - timedelta(seconds=behind)
    return dt.strftime("%Y-%m-%dT%H:%M:%S.") + f"{dt.microsecond // 1000:03d}Z"


def localname(tag):
    return tag.rsplit("}", 1)[-1]


def attr(el, name):
    v = el.get(f"{{{SCHEMA_NS}}}{name}")
    if v is None:
        v = el.get(name)
    return v


# ----------------------------------------------------------------------------
# Extract the real AVP's request bytes from the capture
# ----------------------------------------------------------------------------

def load_captured_requests():
    if not CAPTURE.is_file():
        sys.exit(
            "ERROR: wire capture missing: " + str(CAPTURE) + "\n"
            "This gate replays a real IGT AVP's captured bytes; the captures "
            "are not\nincluded in the tester distribution. The self-contained "
            "gates\n(G2S/tools/test_*.py and pytest SAS/) are the regression "
            "net for this repo.")
    text = CAPTURE.read_text(errors="replace")
    envelopes = re.findall(
        r'(<\?xml version="1\.0" encoding="UTF-8"\?>.*?</SOAP-ENV:Envelope>)',
        text, re.DOTALL)
    transport_req = comms_online_req = None
    for env in envelopes:
        if "g2sTransportReqVersion" in env:
            transport_req = env
        elif "commsOnLine" in env and "g2sEgmId" in env \
                and "communicationsAck" not in env:
            comms_online_req = env
    if not transport_req or not comms_online_req:
        sys.exit(f"FATAL: could not extract captured requests from {CAPTURE}")
    for name, env in (("transport", transport_req),
                      ("commsOnLine", comms_online_req)):
        try:
            ET.fromstring(env)
        except ET.ParseError as e:
            sys.exit(f"FATAL: captured {name} request does not parse: {e}")
    return transport_req, comms_online_req


# ----------------------------------------------------------------------------
# First-light payloads — the machine's own bytes, re-targeted at this run
# ----------------------------------------------------------------------------

def first_light_reply(name, session_id):
    """Load a REAL captured AVP inner g2sMessage (first-light session — the
    first-ever response payloads post-d556ad2) and re-target it: the harvest
    header comments are dropped, the sessionId is patched to pair with THIS
    run's live request, and the two timestamps are refreshed so the host's
    clock-skew tracker isn't poisoned by capture age. Every other byte —
    namespaces, attribute order, the inner <?xml?> decl, the payload itself —
    is the machine's own emission."""
    raw = (FIRST_LIGHT / name).read_text()
    xml = raw[raw.index("<?xml"):]
    xml = re.sub(r'g2s:sessionId="[^"]*"',
                 f'g2s:sessionId="{session_id}"', xml, count=1)
    ts = now_iso()
    xml = re.sub(r'g2s:dateTimeSent="[^"]*"',
                 f'g2s:dateTimeSent="{ts}"', xml, count=1)
    xml = re.sub(r'g2s:dateTime="[^"]*"', f'g2s:dateTime="{ts}"', xml,
                 count=1)
    return xml


def first_light_gameplay_ids():
    """The REAL gamePlay deviceId list from the first-light descriptorList
    (119 on the live AVP), sorted numerically — the exact order the host's
    game_play_device_ids() sweeps them in."""
    raw = (FIRST_LIGHT / "descriptorList_sid1003.xml").read_text()
    inner = ET.fromstring(raw[raw.index("<?xml"):])
    ids = {attr(d, "deviceId") for d in inner.iter()
           if localname(d.tag) == "descriptor"
           and attr(d, "deviceClass") == "G2S_gamePlay"
           and attr(d, "deviceId") not in (None, "0")}
    return sorted(ids, key=int)


# ----------------------------------------------------------------------------
# AVP-style message construction (for the parts not in the capture)
# ----------------------------------------------------------------------------

def avp_wrap(inner_xml, egm_id=EGM_ID):
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        f'<SOAP-ENV:Envelope xmlns:SOAP-ENV="{SOAP_NS}" '
        f'xmlns:g2s="{WSDL_NS}"><SOAP-ENV:Body><g2s:g2sRequest>'
        f"<g2s:g2sRequest>{html.escape(inner_xml)}</g2s:g2sRequest>"
        f"<g2s:g2sEgmId>{egm_id}</g2s:g2sEgmId>"
        f"<g2s:g2sHostId>1</g2s:g2sHostId>"
        f"</g2s:g2sRequest></SOAP-ENV:Body></SOAP-ENV:Envelope>"
    )


def avp_command(command_xml, command_id, session_id, session_type="G2S_request",
                ttl="30000", ts=None, egm_id=EGM_ID):
    ts = ts or now_iso()
    return (
        f'<g2s:g2sMessage xmlns:g2s="{SCHEMA_NS}">\n'
        f'   <g2s:g2sBody g2s:hostId="1" g2s:egmId="{egm_id}" '
        f'g2s:dateTimeSent="{ts}">\n'
        f'      <g2s:communications g2s:deviceId="1" g2s:dateTime="{ts}" '
        f'g2s:commandId="{command_id}" g2s:sessionType="{session_type}" '
        f'g2s:sessionId="{session_id}" g2s:timeToLive="{ttl}">\n'
        f"         {command_xml}\n"
        f"      </g2s:communications>\n"
        f"   </g2s:g2sBody>\n"
        f"</g2s:g2sMessage>"
    )


def avp_class_command(cls, command_xml, command_id, session_id,
                      session_type="G2S_request", ttl="30000", device_id="1",
                      ts=None):
    """Like avp_command but under an ARBITRARY class wrapper (cabinet/meters/
    commConfig...), for exercising the host's non-communications handlers."""
    ts = ts or now_iso()
    return (
        f'<g2s:g2sMessage xmlns:g2s="{SCHEMA_NS}">\n'
        f'   <g2s:g2sBody g2s:hostId="1" g2s:egmId="{EGM_ID}" '
        f'g2s:dateTimeSent="{ts}">\n'
        f'      <g2s:{cls} g2s:deviceId="{device_id}" g2s:dateTime="{ts}" '
        f'g2s:commandId="{command_id}" g2s:sessionType="{session_type}" '
        f'g2s:sessionId="{session_id}" g2s:timeToLive="{ttl}">\n'
        f"         {command_xml}\n"
        f"      </g2s:{cls}>\n"
        f"   </g2s:g2sBody>\n"
        f"</g2s:g2sMessage>"
    )


CMD_URL = "http://127.0.0.1:8081/api/command"
ACCT_URL = "http://127.0.0.1:8081/api/accounts"
DEBUG_LOG_URL = "http://127.0.0.1:8081/api/debug/log"
VOUCHERS_URL = "http://127.0.0.1:8081/api/vouchers"


def post_command(payload):
    """POST /api/command to trigger a host-originated request on demand."""
    req = urllib.request.Request(
        CMD_URL, data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=10) as resp:
        return resp.status, json.loads(resp.read())


def post_command_err(payload):
    """POST /api/command where an HTTP 400 {ok:false} body is an EXPECTED
    outcome (the G2S-41 strict-validation assertions) — surface status +
    body instead of raising (the post_accounts treatment)."""
    req = urllib.request.Request(
        CMD_URL, data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status, json.loads(resp.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())


def get_json(url):
    """GET a host JSON endpoint (/api/accounts, /api/debug/log, ...)."""
    with urllib.request.urlopen(url, timeout=5) as resp:
        return json.loads(resp.read())


def get_raw(url):
    """GET returning (status, content_type, body-str) — 4xx tolerated, for
    the route-fallback assertions (G2S-40)."""
    try:
        with urllib.request.urlopen(url, timeout=5) as resp:
            return (resp.status, resp.headers.get("Content-Type") or "",
                    resp.read().decode("utf-8", errors="replace"))
    except urllib.error.HTTPError as e:
        return (e.code, e.headers.get("Content-Type") or "",
                e.read().decode("utf-8", errors="replace"))


def post_accounts(payload):
    """POST /api/accounts (G2S-39). Unlike post_command, error paths answer
    HTTP 400 with an {ok:false} body — surface both instead of raising."""
    req = urllib.request.Request(
        ACCT_URL, data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status, json.loads(resp.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())


def house_cashable():
    """The house account's cashable balance in millicents (the WAT escrow
    assertions work in DELTAS — balances persist across gate runs)."""
    for a in get_json(ACCT_URL).get("accounts", []):
        if a.get("id") == "house":
            return int(a.get("cashableMillicents") or 0)
    return None


def drain_host_posts(n, timeout=4):
    """Consume up to n host POSTs (best-effort), returning the command names."""
    got = []
    for _ in range(n):
        try:
            got.append(received.get(timeout=timeout).get("command"))
        except queue.Empty:
            break
    return got


def post_to_host(body, soapaction="http://G2S.gamingstandards.com/SendG2SMessage"):
    req = urllib.request.Request(
        HOST_URL, data=body.encode("utf-8"),
        headers={"Content-Type": "text/xml; charset=utf-8",
                 "SOAPAction": f'"{soapaction}"',
                 "User-Agent": "gSOAP/2.7"})
    with urllib.request.urlopen(req, timeout=10) as resp:
        return resp.status, resp.read().decode("utf-8", errors="replace")


def parse_sync_reply(body):
    """Unwrap the host's synchronous SINGLE-wrapped reply -> inner Element.
    The escaped g2sMessage is the TEXT CONTENT of one <g2s:g2sResponse>,
    byte-mirroring the real AVP. (The old double-nested shape was the bug that
    made the machine never register our g2sAck.) A nested g2sResponse element is
    now a FAILURE — that's the regression the fix eliminates."""
    root = ET.fromstring(body)
    outer = root.find(f".//{{{WSDL_NS}}}g2sResponse")
    if outer is None:
        return None, "no wsdl g2sResponse element"
    nested = outer.find(f"{{{WSDL_NS}}}g2sResponse")
    if nested is not None:
        return None, "REGRESSION: sync reply is double-wrapped (nested g2sResponse)"
    if not outer.text or not outer.text.strip():
        return None, "g2sResponse has no escaped payload text"
    return ET.fromstring(html.unescape(outer.text)), None


def voucher_id_items(raw):
    """Return the (validationId, validationSeed) pairs inside a host POST's
    validationData — parse_host_message only surfaces the command element's
    own attributes, not its validationIdItem children."""
    root = ET.fromstring(raw)
    outer = root.find(f".//{{{WSDL_NS}}}g2sRequest")
    inner = ET.fromstring(html.unescape(
        outer.find(f"{{{WSDL_NS}}}g2sRequest").text))
    return [(attr(el, "validationId"), attr(el, "validationSeed"))
            for el in inner.iter()
            if localname(el.tag) == "validationIdItem"]


def meter_sub_selectors(raw):
    """Return (tag, deviceClass, deviceId) triples for the get*Meters
    selectors inside a host POST's setMeterSub — parse_host_message only
    surfaces the command element's own attributes, not its children."""
    root = ET.fromstring(raw)
    outer = root.find(f".//{{{WSDL_NS}}}g2sRequest")
    inner = ET.fromstring(html.unescape(
        outer.find(f"{{{WSDL_NS}}}g2sRequest").text))
    return [(localname(el.tag), attr(el, "deviceClass"), attr(el, "deviceId"))
            for el in inner.iter()
            if localname(el.tag) in ("getDeviceMeters", "getWagerMeters",
                                     "getGameDenomMeters",
                                     "getCurrencyMeters")]


def option_change_details(raw):
    """Deep-parse a host POST's setOptionChange (G2S-27) — the command element's
    own attrs (configurationId/applyCondition/restartAfter), the addressing
    4-tuple on its <option> child, the COMPLETE current-value set as a flat
    paramId -> value map (flat scalars + complexValue children), and the
    authorizeList hostIds. parse_host_message only surfaces the command
    element's own attributes, so this walks the inner tree itself."""
    root = ET.fromstring(raw)
    outer = root.find(f".//{{{WSDL_NS}}}g2sRequest")
    inner = ET.fromstring(html.unescape(
        outer.find(f"{{{WSDL_NS}}}g2sRequest").text))
    setoc = next((el for el in inner.iter()
                  if localname(el.tag) == "setOptionChange"), None)
    if setoc is None:
        return None
    out = {"attrs": {localname(k): v for k, v in setoc.attrib.items()},
           "option": None, "values": {}, "authorizeHosts": []}
    opt = next((el for el in setoc if localname(el.tag) == "option"), None)
    if opt is not None:
        out["option"] = {localname(k): v for k, v in opt.attrib.items()}
        for cv in opt.iter():
            if localname(cv.tag) == "optionCurrentValues":
                for val in cv.iter():
                    if localname(val.tag) in ("booleanValue", "integerValue",
                                              "decimalValue", "stringValue"):
                        pid = attr(val, "paramId")
                        if pid is not None:
                            out["values"][pid] = (val.text or "").strip()
    for item in setoc.iter():
        if localname(item.tag) == "authorizeItem":
            out["authorizeHosts"].append(attr(item, "hostId"))
    return out


# ----------------------------------------------------------------------------
# Fake EGM endpoint (receives the host's outbound POSTs)
# ----------------------------------------------------------------------------

received = queue.Queue()
# Every raw host->EGM POST body, in arrival order — the mediaDisplay
# dormancy gate (#18 P3) scans the WHOLE session's wire for igtMediaDisplay
# bytes, INCLUDING the keepAlive requests the queue filter below drops.
# list.append is atomic under the GIL; the scan snapshots with list().
WIRE_TAPE = []


class FakeEgmHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        pass

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        raw = self.rfile.read(length).decode("utf-8", errors="replace")
        WIRE_TAPE.append(raw)
        parsed = self.parse_host_message(raw)
        # The host runs an always-on keepAlive pinger that POSTs a keepAlive
        # G2S_request every ~10s once onLine. That is durability traffic, NOT
        # part of the choreography we assert, and it would otherwise land in the
        # queue and derail a FIFO/timeout assertion. Drop it here (we still ack
        # it below so the host is happy). Only keepAlive REQUESTS are filtered —
        # keepAliveAck (a G2S_response) and every other message still enqueue.
        is_host_keepalive = (parsed.get("command") == "keepAlive"
                             and parsed.get("sessionType") == "G2S_request")
        if not is_host_keepalive:
            received.put(parsed)
        # Reply like a gSOAP EGM: synchronous message-level g2sAck.
        ack_inner = (
            f'<g2s:g2sMessage xmlns:g2s="{SCHEMA_NS}">\n'
            f'   <g2s:g2sAck g2s:hostId="1" g2s:egmId="{EGM_ID}" '
            f'g2s:dateTimeSent="{now_iso()}"/>\n'
            f"</g2s:g2sMessage>"
        )
        # Reply like the REAL AVP: a SINGLE g2sResponse whose text is the
        # escaped ack (the machine never double-wraps — that was the host bug).
        body = (
            '<?xml version="1.0" encoding="UTF-8"?>\n'
            f'<SOAP-ENV:Envelope xmlns:SOAP-ENV="{SOAP_NS}" '
            f'xmlns:g2s="{WSDL_NS}"><SOAP-ENV:Body><g2s:g2sResponse>'
            f"{html.escape(ack_inner)}"
            f"</g2s:g2sResponse></SOAP-ENV:Body></SOAP-ENV:Envelope>"
        ).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/xml; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    @staticmethod
    def parse_host_message(raw):
        out = {"raw": raw, "error": None, "command": None}
        try:
            root = ET.fromstring(raw)
            outer = root.find(f".//{{{WSDL_NS}}}g2sRequest")
            if outer is None:
                out["error"] = "host POST missing wsdl g2sRequest wrapper"
                return out
            inner_el = outer.find(f"{{{WSDL_NS}}}g2sRequest")
            egm_el = outer.find(f"{{{WSDL_NS}}}g2sEgmId")
            host_el = outer.find(f"{{{WSDL_NS}}}g2sHostId")
            out["g2sEgmId"] = egm_el.text if egm_el is not None else None
            out["g2sHostId"] = host_el.text if host_el is not None else None
            # inner_el.text is ALREADY the once-unescaped inner document
            # (ElementTree unescaped the wsdl wrapper's text on parse) —
            # a second html.unescape here would turn a correctly-escaped
            # &amp; inside an inner attribute (e.g. a query-string
            # mediaUri) into a raw '&' and kill the parse.
            inner = ET.fromstring(inner_el.text)
            body = inner.find(f"{{{SCHEMA_NS}}}g2sBody")
            if body is None:
                out["error"] = "inner message has no g2sBody"
                return out
            out["hostId"] = attr(body, "hostId")
            out["egmId"] = attr(body, "egmId")
            for cls in body:
                out["class"] = localname(cls.tag)
                out["deviceId"] = attr(cls, "deviceId")
                out["commandId"] = attr(cls, "commandId")
                out["sessionType"] = attr(cls, "sessionType")
                out["sessionId"] = attr(cls, "sessionId")
                out["timeToLive"] = attr(cls, "timeToLive")
                for child in cls:
                    out["command"] = localname(child.tag)
                    out["commandAttrs"] = {
                        localname(k): v for k, v in child.attrib.items()}
                break
        except ET.ParseError as e:
            out["error"] = f"parse error: {e}"
        return out


def expect_host_post(label, timeout=10):
    try:
        msg = received.get(timeout=timeout)
        if msg.get("error"):
            check(f"{label} parses", False, msg["error"])
            return None
        return msg
    except queue.Empty:
        check(f"{label} arrives at EGM endpoint", False,
              f"nothing received within {timeout}s — the host is not POSTing "
              f"to egmLocation")
        return None


def expect_no_host_post(label, timeout=1.5):
    """Assert the host does NOT POST anything within `timeout` — used to prove
    a refused /api/command (unknown option, in-flight, local cancel) never
    touches the wire. keepAlive requests are already filtered by the fake EGM."""
    try:
        msg = received.get(timeout=timeout)
        check(f"no host POST for {label}", False,
              f"unexpected {msg.get('class')}.{msg.get('command')} hit the wire")
        return msg
    except queue.Empty:
        check(f"no host POST for {label} (refused before the wire)", True)
        return None


# ----------------------------------------------------------------------------
# The choreography test
# ----------------------------------------------------------------------------

def main():
    global HOST_URL, STATUS_URL, DATA_DIR
    ap = argparse.ArgumentParser(
        description="AVP replay gate for g2s_host.py — LOCAL host only "
                    "(mutates voucher state + associations, see GR-04)")
    ap.add_argument("--host-url", default=HOST_BASE,
                    help="base URL of the g2s_host under test (default "
                         f"{HOST_BASE}). The gate MUTATES host state — "
                         "point this at a local bench instance only, never "
                         "the production Pi.")
    ap.add_argument("--data-dir", default=str(DATA_DIR),
                    help="the target host's data/ dir (voucher_state.json) "
                         "for the persistence checks (default: ../data "
                         "relative to this tool)")
    ap.add_argument("--force", action="store_true",
                    help="run even if the target host shows a live "
                         "production association (commsState onLine/sync/"
                         "offline). DANGEROUS: a forced run against the Pi "
                         "seeds fixture vouchers under the real EGM id.")
    args = ap.parse_args()
    base = args.host_url.rstrip("/")
    HOST_URL = base + "/G2S"
    STATUS_URL = base + "/api/status"
    DATA_DIR = Path(args.data_dir)

    # GR-04 live-host guard: a replay run against the production Pi injected
    # the IGT_00RESTARTGHOST association and permanently persisted fixture
    # vouchers into its voucher_state.json under the real EGM id. Refuse to
    # run when the target already carries an association that has really
    # joined: onLine/sync = live now; offline = a real machine joined this
    # host process and was demoted (GR-01) — production either way. A fresh
    # local bench host has zero associations and always passes.
    try:
        with urllib.request.urlopen(STATUS_URL, timeout=5) as resp:
            pre_snap = json.loads(resp.read())
    except (OSError, ValueError) as e:
        sys.exit(f"FATAL: cannot fetch {STATUS_URL} ({e}) — start a LOCAL "
                 "host first: python3 g2s_host.py (from G2S/)")
    live = {eid: a.get("commsState") for eid, a in pre_snap.items()
            if eid != "_engine" and isinstance(a, dict)
            and (a.get("commsState") in ("onLine", "offline")
                 or str(a.get("commsState", "")).startswith("sync"))}
    if live and not args.force:
        print(f"REFUSING to run against {base}: it looks like a LIVE "
              "production host —")
        for eid, state in live.items():
            print(f"  association {eid}: commsState={state}")
        print("This gate mutates the target's voucher store and association "
              "table (GR-04:\nfixture vouchers were seeded into the Pi's "
              "production voucher_state.json).\nRun it against a local bench "
              "instance instead, or pass --force if you are\nABSOLUTELY sure.")
        sys.exit(3)

    # Preflight: the gate depends on real-AVP wire-capture fixtures under
    # G2S/debug-captures/. Those captures are NOT part of the tester
    # distribution (they are one collector's machine traffic) — without them
    # this gate cannot run, and used to die mid-gate with a raw
    # FileNotFoundError deep inside first_light_reply(). Fail fast with an
    # actionable message instead. The other gates under G2S/tools/ and
    # `pytest SAS/` are fully self-contained and are the tester-facing net.
    missing = [n for n in FIRST_LIGHT_REQUIRED
               if not (FIRST_LIGHT / n).is_file()]
    if missing or not FIRST_LIGHT.is_dir():
        print("ERROR: first-light capture fixtures are missing — this gate "
              "cannot run without them.")
        print(f"  expected dir: {FIRST_LIGHT}")
        for n in (missing or FIRST_LIGHT_REQUIRED):
            print(f"  missing: {n}")
        print("  note: the wire captures this gate replays are not included "
              "in the\n"
              "  tester distribution. The self-contained gates (G2S/tools/"
              "test_*.py and\n"
              "  pytest SAS/) are the regression net for this repo.")
        sys.exit(2)

    # GR-10 deterministic join: the host seeds a NEW association's option
    # inventory from data/config_inventory.json (a previous gate run leaves
    # fixtures there), and the join-time getOptionList one-shot fires only
    # when that inventory is EMPTY. The host reads the file lazily at
    # association creation, so clearing it now — before any traffic —
    # guarantees the fresh-join shape this gate asserts (join extras END
    # with the getOptionList bootstrap). Local bench data only: the
    # live-host guard above already refused production targets.
    inv_file = DATA_DIR / "config_inventory.json"
    try:
        inv_file.unlink()
        print(f"cleared persisted config inventory {inv_file} "
              "(deterministic join shape)")
    except FileNotFoundError:
        pass
    except OSError as e:
        sys.exit(f"FATAL: cannot clear {inv_file} ({e}) — the gate needs a "
                 "fresh config inventory for the deterministic join shape")

    transport_req, comms_online_req = load_captured_requests()
    print(f"Loaded captured AVP requests from {CAPTURE.name}")

    egm_server = ThreadingHTTPServer(("0.0.0.0", EGM_PORT), FakeEgmHandler)
    threading.Thread(target=egm_server.serve_forever, daemon=True).start()
    print(f"Fake EGM endpoint listening on :{EGM_PORT} (capture egmLocation "
          f"is http://192.168.50.100:8080 — host must use the URL we "
          f"advertise below)\n")

    # The captured commsOnLine advertises egmLocation http://192.168.50.100:8080
    # which won't resolve on this box; rewrite ONLY that attribute to point at
    # our fake EGM, keeping every other captured byte identical.
    comms_online_local = comms_online_req.replace(
        "http://192.168.50.100:8080", f"http://127.0.0.1:{EGM_PORT}")

    print("— Step 0: MSX003 rule (commsDisabled before commsOnLine)")
    pre = avp_wrap(avp_command("<g2s:commsDisabled/>", "222", "9"))
    status, body = post_to_host(pre)
    check("HTTP 200", status == 200)
    # Regression guard for the 2026-07-02 ack fix: the sync reply MUST be a
    # SINGLE g2sResponse with the escaped payload as its text content. A nested
    # g2sResponse (the old bug) makes parse_sync_reply return an error here.
    check("sync reply is SINGLE-wrapped (not the double-wrap bug)",
          "<g2s:g2sResponse><g2s:g2sResponse>" not in body
          and body.count("g2sResponse") == 2,
          "double-wrapped g2sResponse detected")
    inner, err = parse_sync_reply(body)
    if check("sync reply single-wraps + parses", inner is not None, err or ""):
        ack = inner.find(f"{{{SCHEMA_NS}}}g2sAck")
        if check("sync reply is a g2sAck", ack is not None):
            check("errorCode=G2S_MSX003 before commsOnLine",
                  attr(ack, "errorCode") == "G2S_MSX003",
                  f"got {attr(ack, 'errorCode')}")

    print("— Step 1: transport version negotiation (captured bytes)")
    status, body = post_to_host(
        transport_req, "http://G2S.gamingstandards.com/getTransportOptions")
    check("HTTP 200", status == 200)
    check("contains g2sTransportAckVersion 1.0",
          "g2sTransportAckVersion>1.0" in body.replace(" ", ""))
    check("NO certificate element (cert-less)", "certificate" not in body)
    check("wsdl namespace on ack", WSDL_NS in body)

    print("— Step 2: commsOnLine (captured bytes, commandId=223)")
    status, body = post_to_host(comms_online_local)
    check("HTTP 200", status == 200)
    check("NO certificate element", "certificate" not in body)
    inner, err = parse_sync_reply(body)
    if check("sync reply parses", inner is not None, err or ""):
        ack = inner.find(f"{{{SCHEMA_NS}}}g2sAck")
        if check("sync reply is ONLY a message-level g2sAck", ack is not None,
                 f"got <{localname(list(inner)[0].tag)}>" if len(inner) else "empty"):
            check("g2sAck egmId echoes AVP", attr(ack, "egmId") == EGM_ID,
                  f"got {attr(ack, 'egmId')}")
            check("g2sAck hostId=1", attr(ack, "hostId") == "1",
                  f"got {attr(ack, 'hostId')}")
            check("g2sAck errorCode clean",
                  (attr(ack, "errorCode") or "G2S_none") == "G2S_none",
                  f"got {attr(ack, 'errorCode')}")

    print("— Step 3: host POSTs commsOnLineAck to egmLocation")
    msg = expect_host_post("commsOnLineAck")
    if msg:
        check("command is commsOnLineAck", msg["command"] == "commsOnLineAck",
              f"got {msg['command']}")
        check("sessionType=G2S_response", msg["sessionType"] == "G2S_response",
              f"got {msg['sessionType']}")
        check("sessionId echoed ('0' — capture has none, default 0)",
              msg["sessionId"] == "0", f"got {msg['sessionId']}")
        check("host commandId starts at 1", msg["commandId"] == "1",
              f"got {msg['commandId']}")
        check("deviceId matches request", msg["deviceId"] == "1",
              f"got {msg['deviceId']}")
        check("timeToLive=0 on response", msg["timeToLive"] == "0",
              f"got {msg['timeToLive']}")
        sync_timer = int(msg.get("commandAttrs", {}).get("syncTimer", "0"))
        check("syncTimer >= 15000", sync_timer >= 15000, f"got {sync_timer}")
        check("wrapper g2sEgmId/g2sHostId present",
              msg["g2sEgmId"] == EGM_ID and msg["g2sHostId"] == "1",
              f"got {msg['g2sEgmId']}/{msg['g2sHostId']}")
        check("no certificate in outbound", "certificate" not in msg["raw"])

    print("— Step 4: EGM enters sync, sends commsDisabled (sessionId=42)")
    status, body = post_to_host(
        avp_wrap(avp_command("<g2s:commsDisabled/>", "224", "42")))
    check("HTTP 200", status == 200)
    inner, err = parse_sync_reply(body)
    if inner is not None:
        ack = inner.find(f"{{{SCHEMA_NS}}}g2sAck")
        check("sync reply g2sAck clean", ack is not None and
              (attr(ack, "errorCode") or "G2S_none") == "G2S_none")

    msg = expect_host_post("commsDisabledAck")
    if msg:
        check("command is commsDisabledAck",
              msg["command"] == "commsDisabledAck", f"got {msg['command']}")
        check("sessionId echoed (42)", msg["sessionId"] == "42",
              f"got {msg['sessionId']}")
        check("host commandId=2", msg["commandId"] == "2",
              f"got {msg['commandId']}")

    print("— Step 5: host sends setCommsState enable=true")
    msg = expect_host_post("setCommsState")
    set_comms_sid = None
    if msg:
        check("command is setCommsState", msg["command"] == "setCommsState",
              f"got {msg['command']}")
        check("enable=true",
              msg.get("commandAttrs", {}).get("enable") == "true",
              f"got {msg.get('commandAttrs')}")
        check("sessionType=G2S_request", msg["sessionType"] == "G2S_request",
              f"got {msg['sessionType']}")
        check("host commandId=3", msg["commandId"] == "3",
              f"got {msg['commandId']}")
        set_comms_sid = msg["sessionId"]
        check("has its own sessionId", bool(set_comms_sid))

    print("— Step 6: EGM responds commsStatus G2S_onLine -> JOIN")
    status_cmd = (
        '<g2s:commsStatus g2s:hostEnabled="true" g2s:egmEnabled="true" '
        'g2s:outboundOverflow="false" g2s:inboundOverflow="false" '
        'g2s:g2sProtocol="true" g2s:commsState="G2S_onLine" '
        'g2s:transportState="G2S_transportUp"/>'
    )
    status, body = post_to_host(avp_wrap(avp_command(
        status_cmd, "225", set_comms_sid or "1001", "G2S_response", "0")))
    check("HTTP 200", status == 200)
    inner, err = parse_sync_reply(body)
    if inner is not None:
        ack = inner.find(f"{{{SCHEMA_NS}}}g2sAck")
        check("commsStatus g2sAck clean", ack is not None and
              (attr(ack, "errorCode") or "G2S_none") == "G2S_none")

    time.sleep(0.5)
    with urllib.request.urlopen(STATUS_URL, timeout=5) as resp:
        snap = json.loads(resp.read())
    egm = snap.get(EGM_ID, {})
    engine = snap.get("_engine", {})
    check("host registers commsState=onLine", egm.get("commsState") == "onLine",
          f"got {egm.get('commsState')}")
    check("joinedAt recorded", bool(egm.get("joinedAt")))
    # G2S-20: the commsStatus above carried a CURRENT dateTimeSent, so the
    # measured skew is ≈0 and the join-time auto clock-sync must NOT fire
    # (Step 6.5's exact commandId arithmetic below re-proves no setDateTime
    # crept into the post-join FIFO).
    check("egmClockSkewSec tracked from inbound dateTimeSent (≈0 here)",
          isinstance(egm.get("egmClockSkewSec"), (int, float))
          and abs(egm["egmClockSkewSec"]) < 10,
          f"got {egm.get('egmClockSkewSec')}")
    check("auto clock-sync NOT fired for a small skew (clockSync empty)",
          egm.get("clockSync") == {}, f"got {egm.get('clockSync')}")
    check("autoClock enabled by default", engine.get("autoClock") is True,
          f"got {engine.get('autoClock')}")

    # — Step 6.5: post-join extras, validated per the host's actual config.
    # ALL queued together at join time, so they arrive in strict FIFO order;
    # drain everything BEFORE replying. Since 2026-07-02 the join also fires
    # the probes that used to hide behind the never-firing post-ownership
    # re-probe (the "ownership gate" was a symptom of the double-wrap ack
    # bug): setMeterSub x2, the cabinet/meters reads, then the event-log
    # backfill sweep. The gamePlay sweep is NOT enqueued here — it starts
    # only when descriptorList reveals the real device list (Step 6.65).
    expected = []  # (command, expected_cid)
    cid = 4        # commsOnLineAck=1, commsDisabledAck=2, setCommsState=3
    if engine.get("keepaliveMs", 0) > 0:
        expected.append(("setKeepAlive", cid)); cid += 1
    if engine.get("harvest"):
        expected.append(("getDescriptor", cid)); cid += 1
        # Harvest now also probes the host table (commConfig.getCommHostList)
        # right after getDescriptor, to explain an empty descriptor harvest.
        expected.append(("getCommHostList", cid)); cid += 1
    if engine.get("subscribe"):
        # RM-04 eventHandler bring-up: profile -> supportedEvents -> setEventSub,
        # enqueued in that order after any harvest commands.
        expected.append(("getEventHandlerProfile", cid)); cid += 1
        expected.append(("getSupportedEvents", cid)); cid += 1
        expected.append(("setEventSub", cid)); cid += 1
    # Un-gated join probes (2026-07-02): meter_subs_desired defaults TRUE so
    # the standing subs install on the FIRST join, then the reads follow
    # unconditionally — setMeterSub, cabinet reads, event-log sweep, in that
    # exact order.
    expected.append(("setMeterSub", cid)); cid += 1      # G2S_onPeriodic
    expected.append(("setMeterSub", cid)); cid += 1      # G2S_onEOD
    expected.append(("getCabinetStatus", cid)); cid += 1
    expected.append(("getMeterInfo", cid)); cid += 1
    expected.append(("getEventHandlerLog", cid)); cid += 1
    # G2S-39 WAT join probe: status+profile for the two wat devices the live
    # AVP exposes (G2S_wat/1 and /2), every epoch, right behind the un-gated
    # reads — so /api/status.wat populates with zero operator action.
    expected.append(("getWatStatus", cid)); cid += 1     # dev 1
    expected.append(("getWatProfile", cid)); cid += 1    # dev 1
    expected.append(("getWatStatus", cid)); cid += 1     # dev 2
    expected.append(("getWatProfile", cid)); cid += 1    # dev 2
    # G2S-40 voucher device join probe: status+profile right behind the WAT
    # probes — /api/status.voucherDevice populates and the profile's
    # maxValIds becomes the getValidationData issuance cap.
    expected.append(("getVoucherStatus", cid)); cid += 1
    expected.append(("getVoucherProfile", cid)); cid += 1
    # GR-10: an EMPTY option inventory (fresh host process + the preflight
    # cleared config_inventory.json) closes the join extras with ONE
    # full-wildcard getOptionList bootstrap. Re-joins skip it — the
    # inventory is populated by then (asserted by steps 6.997/7/7.5's
    # fixed drain lists, which contain NO getOptionList).
    expected.append(("getOptionList", cid)); cid += 1
    extras = {}  # command -> [msgs in arrival order] (setMeterSub comes x2)
    # eventId=7's occurrence time — captured once so Step 6.62's log entry
    # can present the SAME stored record (same eventDateTime): the host's
    # dedupe key is the (eventId, eventDateTime) pair, since eventIds are
    # only unique per log lifetime (§4.1.7) and recycle after a RAM clear.
    ev7_dt = now_iso()
    if expected:
        print(f"— Step 6.5: post-join extras per engine config "
              f"({', '.join(c for c, _ in expected)})")
        for want_cmd, want_cid in expected:
            msg = expect_host_post(want_cmd)
            if not msg:
                continue
            extras.setdefault(msg["command"], []).append(msg)
            check(f"{want_cmd} arrives in FIFO order",
                  msg["command"] == want_cmd, f"got {msg['command']}")
            check(f"{want_cmd} commandId={want_cid}",
                  msg["commandId"] == str(want_cid), f"got {msg['commandId']}")
            check(f"{want_cmd} is G2S_request",
                  msg["sessionType"] == "G2S_request")
        check("WAT join probes address devices 1 and 2 under the wat class "
              "(G2S-39 — the live AVP's G2S_wat/1 + /2)",
              [(m["class"], m["deviceId"])
               for m in (extras.get("getWatStatus") or [])]
              == [("wat", "1"), ("wat", "2")]
              and [(m["class"], m["deviceId"])
                   for m in (extras.get("getWatProfile") or [])]
              == [("wat", "1"), ("wat", "2")],
              f"got status={[(m['class'], m['deviceId']) for m in (extras.get('getWatStatus') or [])]} "
              f"profile={[(m['class'], m['deviceId']) for m in (extras.get('getWatProfile') or [])]}")
        check("voucher device join probe addresses voucher/1 (G2S-40 — no "
              "config-sync yet, so the default device)",
              [(m["class"], m["deviceId"])
               for m in (extras.get("getVoucherStatus") or [])]
              == [("voucher", "1")]
              and [(m["class"], m["deviceId"])
                   for m in (extras.get("getVoucherProfile") or [])]
              == [("voucher", "1")],
              f"got status={[(m['class'], m['deviceId']) for m in (extras.get('getVoucherStatus') or [])]} "
              f"profile={[(m['class'], m['deviceId']) for m in (extras.get('getVoucherProfile') or [])]}")
        # Answer the voucher probes (sessionId echo): the status fold + the
        # PROFILE-DRIVEN issuance cap (maxValIds=40) that step 6.9797
        # asserts getValidationData now honors instead of the 100 fallback.
        vs_m = (extras.get("getVoucherStatus") or [None])[0]
        if vs_m:
            post_to_host(avp_wrap(avp_class_command(
                "voucher",
                '<g2s:voucherStatus g2s:configurationId="0" '
                'g2s:egmEnabled="true" g2s:hostEnabled="true" '
                'g2s:hostLocked="false" g2s:validationListId="0"/>',
                "460", vs_m["sessionId"], "G2S_response")))
        vp_m = (extras.get("getVoucherProfile") or [None])[0]
        if vp_m:
            post_to_host(avp_wrap(avp_class_command(
                "voucher",
                '<g2s:voucherProfile g2s:configurationId="0" '
                'g2s:restartStatus="true" g2s:useDefaultConfig="false" '
                'g2s:requiredForPlay="false" g2s:minLogEntries="35" '
                'g2s:timeToLive="30000" g2s:idReaderId="0" '
                'g2s:combineCashableOut="false" g2s:allowNonCashOut="true" '
                'g2s:maxValIds="40" g2s:minLevelValIds="15" '
                'g2s:valIdListRefresh="43200000" g2s:valIdListLife="86400000" '
                'g2s:voucherHoldTime="15000" g2s:printOffLine="true" '
                'g2s:expireCashPromo="30" g2s:printExpCashPromo="true" '
                'g2s:expireNonCash="30" g2s:printExpNonCash="true" '
                'g2s:propName="CasinoNet" g2s:propLine1="The Game Room" '
                'g2s:propLine2="Home Floor" g2s:titleCash="CASHOUT TICKET" '
                'g2s:titlePromo="PROMO" g2s:titleNonCash="NONCASH" '
                'g2s:titleLargeWin="JACKPOT" g2s:titleBonusCash="BONUS" '
                'g2s:titleBonusPromo="BONUS PROMO" '
                'g2s:titleBonusNonCash="BONUS NC" g2s:titleWatCash="WAT" '
                'g2s:titleWatPromo="WAT PROMO" '
                'g2s:titleWatNonCash="WAT NC"/>',
                "461", vp_m["sessionId"], "G2S_response")))
        ka = (extras.get("setKeepAlive") or [None])[0]
        if ka:
            check("setKeepAlive interval matches engine config",
                  ka.get("commandAttrs", {}).get("interval")
                  == str(engine["keepaliveMs"]),
                  f"got {ka.get('commandAttrs')}")
            # REAL first-light bytes: the machine's own setKeepAliveAck.
            post_to_host(avp_wrap(first_light_reply(
                "setKeepAliveAck.xml", ka["sessionId"])))
            # prove the pulse round-trips
            post_to_host(avp_wrap(avp_command("<g2s:keepAlive/>", "227", "88")))
            resp_ka = expect_host_post("keepAliveAck")
            if resp_ka:
                check("keepAliveAck round-trips (sessionId echoed)",
                      resp_ka["command"] == "keepAliveAck"
                      and resp_ka["sessionId"] == "88",
                      f"got {resp_ka['command']}/{resp_ka['sessionId']}")
        print("— Step 6.52: standing meter subscriptions install AT JOIN "
              "(G2S-16, un-gated 2026-07-02 — no ownership cycle needed)")
        # setMeterSub rides the plain join sequence now: the first-light
        # session proved the live AVP honors it straight after commsStatus
        # G2S_onLine (the old owner-only/post-ownership placement guarded
        # against a refusal that was really the double-wrap ack bug).
        # FIFO order: onPeriodic first, then onEOD.
        subs_msgs = extras.get("setMeterSub") or []
        m = subs_msgs[0] if subs_msgs else None
        periodic_sid = None
        if m:
            check("setMeterSub under meters class as G2S_request",
                  m["class"] == "meters" and m["command"] == "setMeterSub"
                  and m["sessionType"] == "G2S_request",
                  f"got {m.get('class')}.{m.get('command')}/{m.get('sessionType')}")
            check("meters deviceId == hostId (§5.2)", m["deviceId"] == "1",
                  f"got {m['deviceId']}")
            a = m.get("commandAttrs", {})
            check("meterSubType=G2S_onPeriodic",
                  a.get("meterSubType") == "G2S_onPeriodic", f"got {a}")
            check("periodicInterval=60000 (schema minimum, §5.21 Table 5.19)",
                  a.get("periodicInterval") == "60000", f"got {a}")
            check("periodicBase present, NO eodBase on an onPeriodic sub (§5.21)",
                  a.get("periodicBase") == "0" and "eodBase" not in a, f"got {a}")
            sel = meter_sub_selectors(m["raw"])
            check("wildcard getDeviceMeters selector (G2S_all/-1, like the "
                  "on-demand read)",
                  sel == [("getDeviceMeters", "G2S_all", "-1")], f"got {sel}")
            periodic_sid = m["sessionId"]
        m = subs_msgs[1] if len(subs_msgs) > 1 else None
        eod_sid = None
        if m:
            a = m.get("commandAttrs", {})
            check("second setMeterSub is G2S_onEOD: eodBase, NO periodic attrs "
                  "(§5.21)",
                  a.get("meterSubType") == "G2S_onEOD" and a.get("eodBase") == "0"
                  and "periodicInterval" not in a and "periodicBase" not in a,
                  f"got {a}")
            check("onEOD sub has its OWN sessionId",
                  bool(m["sessionId"]) and m["sessionId"] != periodic_sid,
                  f"both {m['sessionId']}")
            eod_sid = m["sessionId"]
        # The EGM accepts each sub with a meterSubList in EXPANDED device form
        # (wildcards resolved, §5.24) — the host must record it per subType.
        if periodic_sid:
            post_to_host(avp_wrap(avp_class_command(
                "meters",
                '<g2s:meterSubList g2s:meterSubType="G2S_onPeriodic" '
                'g2s:periodicInterval="60000" g2s:periodicBase="0">'
                '<g2s:getDeviceMeters g2s:deviceClass="G2S_cabinet" '
                'g2s:deviceId="1" g2s:meterDefinitions="false"/>'
                '<g2s:getDeviceMeters g2s:deviceClass="G2S_gamePlay" '
                'g2s:deviceId="1" g2s:meterDefinitions="false"/>'
                '</g2s:meterSubList>',
                "263", periodic_sid, "G2S_response")))
        if eod_sid:
            post_to_host(avp_wrap(avp_class_command(
                "meters",
                '<g2s:meterSubList g2s:meterSubType="G2S_onEOD" g2s:eodBase="0">'
                '<g2s:getDeviceMeters g2s:deviceClass="G2S_cabinet" '
                'g2s:deviceId="1" g2s:meterDefinitions="false"/>'
                '</g2s:meterSubList>',
                "264", eod_sid, "G2S_response")))
        time.sleep(0.4)
        with urllib.request.urlopen(STATUS_URL, timeout=5) as resp:
            snap65 = json.loads(resp.read())
        subs = snap65.get(EGM_ID, {}).get("meterSubs", {})
        per = subs.get("G2S_onPeriodic", {})
        check("status: onPeriodic sub active with the EXPANDED selector list",
              per.get("active") is True and per.get("periodicInterval") == "60000"
              and len(per.get("selectors", [])) == 2, f"got {per}")
        check("status: onEOD sub active",
              subs.get("G2S_onEOD", {}).get("active") is True,
              f"got {subs.get('G2S_onEOD')}")
        check("status: meterSubsDesired TRUE by default (subs install with "
              "no operator action)",
              snap65.get(EGM_ID, {}).get("meterSubsDesired") is True,
              f"got {snap65.get(EGM_ID, {}).get('meterSubsDesired')}")

        print("— Step 6.53: option-inventory bootstrap — join-time "
              "getOptionList (GR-10)")
        # Pre-GR-10 the inventory only ever arrived via the EGM's unsolicited
        # deviceReset config-sync, so every warm host restart went blind
        # (configOptionCount=0, handpayRemoteCreditAllowed=null) until the
        # machine rebooted. Now a host with an EMPTY inventory closes the
        # join extras with one full-wildcard read, and the EGM answers with
        # the SAME optionList command in RESPONSE form (§9.13 'sent in
        # response to a getOptionList').
        gol = (extras.get("getOptionList") or [None])[0]
        gol_options = (
            '<g2s:optionList>'
            '<g2s:deviceOptions g2s:deviceClass="G2S_handpay" '
            'g2s:deviceId="1">'
            '<g2s:optionGroup g2s:optionGroupId="G2S_handpayOptions" '
            'g2s:optionGroupName="Handpay Options">'
            '<g2s:optionItem g2s:optionId="G2S_handpayOptions" '
            'g2s:securityLevel="G2S_operator">'
            '<g2s:optionCurrentValues>'
            '<g2s:complexValue g2s:paramId="G2S_handpayParams">'
            '<g2s:booleanValue g2s:paramId="G2S_enabledRemoteCredit">false'
            '</g2s:booleanValue>'
            '<g2s:booleanValue g2s:paramId="G2S_enabledRemoteVoucher">false'
            '</g2s:booleanValue>'
            '<g2s:booleanValue g2s:paramId="G2S_enabledLocalHandpay">true'
            '</g2s:booleanValue>'
            '<g2s:booleanValue g2s:paramId="G2S_mixCreditTypes">false'
            '</g2s:booleanValue>'
            '</g2s:complexValue>'
            '</g2s:optionCurrentValues>'
            '</g2s:optionItem>'
            '</g2s:optionGroup>'
            '</g2s:deviceOptions>'
            '<g2s:deviceOptions g2s:deviceClass="G2S_cabinet" '
            'g2s:deviceId="1">'
            '<g2s:optionGroup g2s:optionGroupId="G2S_cabinetOptions" '
            'g2s:optionGroupName="Cabinet Options">'
            '<g2s:optionItem g2s:optionId="G2S_cabinetOptions" '
            'g2s:securityLevel="G2S_operator">'
            '<g2s:optionCurrentValues>'
            '<g2s:integerValue g2s:paramId="G2S_machineNum">42'
            '</g2s:integerValue>'
            '</g2s:optionCurrentValues>'
            '</g2s:optionItem>'
            '</g2s:optionGroup>'
            '</g2s:deviceOptions>'
            '</g2s:optionList>')
        if gol:
            check("getOptionList under optionConfig class as G2S_request "
                  "at deviceId=1 (host-owned class, deviceId==hostId)",
                  gol["class"] == "optionConfig"
                  and gol["sessionType"] == "G2S_request"
                  and gol["deviceId"] == "1",
                  f"got {gol.get('class')}/{gol.get('sessionType')}"
                  f"/{gol.get('deviceId')}")
            check("all five REQUIRED attrs (§9.10 Table 9.6): full wildcards "
                  "G2S_all/-1 + optionDetail=false (values only)",
                  gol.get("commandAttrs") == {
                      "deviceClass": "G2S_all", "deviceId": "-1",
                      "optionGroupId": "G2S_all", "optionId": "G2S_all",
                      "optionDetail": "false"},
                  f"got {gol.get('commandAttrs')}")
            # remoteCredit=false HERE so step 6.94's config-sync flip to true
            # proves the unsolicited push still overwrites the readback.
            post_to_host(avp_wrap(avp_class_command(
                "optionConfig", gol_options, "229", gol["sessionId"],
                "G2S_response")))
            # A response pairs by sessionId and draws NO optionListAck
            # (acks answer requests) — the regression this pins is the host
            # treating the response form as a config-sync request.
            expect_no_host_post("optionList response (no optionListAck for "
                                "the response form)")
            with urllib.request.urlopen(STATUS_URL, timeout=5) as resp:
                snap53 = json.loads(resp.read())
            egm53 = snap53.get(EGM_ID, {})
            check("status: inventory populated from the getOptionList "
                  "READBACK (2 devices / 2 options, was empty)",
                  egm53.get("configDeviceCount") == 2
                  and egm53.get("configOptionCount") == 2
                  and egm53.get("configDevices") == ["G2S_cabinet/1",
                                                     "G2S_handpay/1"],
                  f"got {egm53.get('configDevices')}"
                  f"/{egm53.get('configOptionCount')}")
            check("status: handpay permission lamp VISIBLE from the readback "
                  "(handpayRemoteCreditAllowed=false — a JPX002 refusal is "
                  "predictable BEFORE bench day)",
                  egm53.get("handpayRemoteCreditAllowed") == "false"
                  and egm53.get("handpayOptions", {}).get(
                      "G2S_enabledLocalHandpay") == "true",
                  f"got {egm53.get('handpayRemoteCreditAllowed')}"
                  f"/{egm53.get('handpayOptions')}")
            inv53 = {}
            if inv_file.is_file():
                inv53 = json.loads(inv_file.read_text()).get(
                    "egms", {}).get(EGM_ID, {})
            check("inventory PERSISTED beside the voucher store "
                  "(config_inventory.json — survives a warm host restart)",
                  "G2S_handpay/1/G2S_handpayOptions/G2S_handpayOptions"
                  in inv53.get("configOptions", {})
                  and set(inv53.get("configDevices", {}))
                  == {"G2S_cabinet/1", "G2S_handpay/1"},
                  f"got {sorted(inv53.get('configOptions', {}))}")
            # The on-demand refresh: /api/command getOptionList re-fires the
            # same read; an identical answer must land idempotently.
            cs, cbody = post_command({"action": "getOptionList",
                                      "egmId": EGM_ID})
            check("/api/command getOptionList accepted",
                  cs == 200 and cbody.get("ok"))
            m2 = expect_host_post("getOptionList (on-demand refresh)")
            if m2:
                check("refresh re-sends the identical full-wildcard read "
                      "on a FRESH sessionId",
                      m2["command"] == "getOptionList"
                      and m2.get("commandAttrs") == gol.get("commandAttrs")
                      and m2["sessionId"] != gol["sessionId"],
                      f"got {m2.get('commandAttrs')}"
                      f"/sid={m2.get('sessionId')}")
                post_to_host(avp_wrap(avp_class_command(
                    "optionConfig", gol_options, "231", m2["sessionId"],
                    "G2S_response")))
                time.sleep(0.4)
                with urllib.request.urlopen(STATUS_URL, timeout=5) as resp:
                    snap54 = json.loads(resp.read())
                egm54 = snap54.get(EGM_ID, {})
                check("refresh is idempotent (still 2 devices / 2 options — "
                      "keyed update, no dupes)",
                      egm54.get("configOptionCount") == 2
                      and egm54.get("configDeviceCount") == 2,
                      f"got {egm54.get('configOptionCount')}"
                      f"/{egm54.get('configDeviceCount')}")

        # The eventHandler bring-up answered with the machine's OWN payloads:
        # the real eventHandlerProfile (minLogEntries=100, queueBehavior=
        # G2S_disable — the "Overflow" smoking gun) and the real (truncated
        # to 30) supportedEvents catalog.
        ehp = (extras.get("getEventHandlerProfile") or [None])[0]
        if ehp:
            post_to_host(avp_wrap(first_light_reply(
                "eventHandlerProfile.xml", ehp["sessionId"])))
        gse = (extras.get("getSupportedEvents") or [None])[0]
        if gse:
            post_to_host(avp_wrap(first_light_reply(
                "supportedEvents_first30.xml", gse["sessionId"])))
            time.sleep(0.3)
            with urllib.request.urlopen(STATUS_URL, timeout=5) as resp:
                snap_se = json.loads(resp.read())
            check("status: supported-events catalog recorded from the REAL "
                  "(first-30) payload",
                  snap_se.get(EGM_ID, {}).get("supportedEventCount") == 30,
                  f"got {snap_se.get(EGM_ID, {}).get('supportedEventCount')}")
        es = (extras.get("setEventSub") or [None])[0]
        if es:
            print("— Step 6.6: eventHandler subscription round-trip")
            # The AVP acks setEventSub with setEventSubAck (REAL first-light
            # bytes); the host flips 'subscribed' true on receipt.
            post_to_host(avp_wrap(first_light_reply(
                "setEventSubAck.xml", es["sessionId"])))
            time.sleep(0.3)
            with urllib.request.urlopen(STATUS_URL, timeout=5) as resp:
                snap3 = json.loads(resp.read())
            check("host records subscribed=true after setEventSubAck",
                  snap3.get(EGM_ID, {}).get("subscribed") is True,
                  f"got {snap3.get(EGM_ID, {}).get('subscribed')}")
            # Now the AVP pushes an eventReport (persisted -> G2S_request); the
            # host MUST POST back an eventAck echoing the eventId.
            report = (
                '<g2s:eventReport g2s:deviceClass="G2S_cabinet" '
                'g2s:deviceId="1" g2s:eventCode="G2S_CBE101" '
                'g2s:eventId="7" g2s:eventText="Door Opened" '
                'g2s:eventDateTime="' + ev7_dt + '" '
                'g2s:transactionId="0"/>')
            eh_report = (
                f'<g2s:g2sMessage xmlns:g2s="{SCHEMA_NS}">\n'
                f'   <g2s:g2sBody g2s:hostId="1" g2s:egmId="{EGM_ID}" '
                f'g2s:dateTimeSent="{now_iso()}">\n'
                f'      <g2s:eventHandler g2s:deviceId="1" '
                f'g2s:dateTime="{now_iso()}" g2s:commandId="230" '
                f'g2s:sessionType="G2S_request" g2s:sessionId="55" '
                f'g2s:timeToLive="30000">\n'
                f'         {report}\n'
                f'      </g2s:eventHandler>\n'
                f'   </g2s:g2sBody>\n'
                f'</g2s:g2sMessage>')
            post_to_host(avp_wrap(eh_report))
            ack = expect_host_post("eventAck")
            if ack:
                check("host POSTs eventAck for the eventReport",
                      ack["command"] == "eventAck", f"got {ack['command']}")
                check("eventAck echoes eventId=7",
                      ack.get("commandAttrs", {}).get("eventId") == "7",
                      f"got {ack.get('commandAttrs')}")
                check("eventAck under eventHandler class + sessionId echoed",
                      ack["class"] == "eventHandler" and ack["sessionId"] == "55",
                      f"got {ack['class']}/{ack['sessionId']}")
            time.sleep(0.3)
            with urllib.request.urlopen(STATUS_URL, timeout=5) as resp:
                snap4 = json.loads(resp.read())
            check("status reflects the received event (eventCount=1)",
                  snap4.get(EGM_ID, {}).get("eventCount") == 1,
                  f"got {snap4.get(EGM_ID, {}).get('eventCount')}")

        print("— Step 6.62: eventHandler log backfill (G2S-14 — now fired "
              "from the JOIN sequence)")
        # The join closes its enqueued probes with a getEventHandlerLog
        # sweep: the per-host log (deviceId == hostId, §4.1) holds every
        # event the eventPersist subscription captured — including anything
        # raised while the host was away. lastSequence=0/totalEntries=0 =
        # newest, backward, everything (§1.14.5). The request was drained
        # (and cid/FIFO-checked) with the other join extras above; the reply
        # rides here AFTER the live ev7 delivery so the dedupe is exercised.
        ehl = (extras.get("getEventHandlerLog") or [None])[0]
        if ehl:
            check("getEventHandlerLog under eventHandler class as G2S_request",
                  ehl["class"] == "eventHandler"
                  and ehl["command"] == "getEventHandlerLog"
                  and ehl["sessionType"] == "G2S_request",
                  f"got {ehl.get('class')}.{ehl.get('command')}/"
                  f"{ehl.get('sessionType')}")
            check("full-sweep paging attrs lastSequence=0 totalEntries=0 "
                  "(§1.14.5/§4.20 Table 4.31)",
                  ehl.get("commandAttrs", {}) == {"lastSequence": "0",
                                                  "totalEntries": "0"},
                  f"got {ehl.get('commandAttrs')}")
            check("per-host log addressed at deviceId=1 (deviceId==hostId, §4.1)",
                  ehl["deviceId"] == "1", f"got {ehl['deviceId']}")
            # Reply NEWEST-FIRST (descending logSequence, §1.14.5). Sequence 5 is
            # eventId=7 — the SAME event step 6.6 already delivered live, so the
            # merge must dedupe it (same stored record => same eventDateTime,
            # which is the dedupe key alongside the per-log-lifetime eventId);
            # sequences 3+4 (eventIds 5+6) are new. Entry 4 carries an
            # affectedMeterList POINTER (§4.21.3 — names, no payload).
            # Timestamps are COHERENT with the sequences (a real EGM log is
            # monotone: seq3 < seq4 < seq5), which the GR-26 ring re-sort
            # check below relies on — backfilled entries are OLDER than the
            # live-seen ev7, exactly the overnight live shape.
            ev6_dt = skewed_iso(60)
            ev5_dt = skewed_iso(120)
            log_list = (
                '<g2s:eventHandlerLogList>'
                '<g2s:eventHandlerLog g2s:logSequence="5" '
                'g2s:deviceClass="G2S_cabinet" g2s:deviceId="1" '
                'g2s:eventCode="G2S_CBE101" g2s:eventId="7" '
                f'g2s:eventText="Door Opened" g2s:eventDateTime="{ev7_dt}" '
                'g2s:transactionId="0" g2s:eventAck="true"/>'
                '<g2s:eventHandlerLog g2s:logSequence="4" '
                'g2s:deviceClass="G2S_noteAcceptor" g2s:deviceId="1" '
                'g2s:eventCode="G2S_NAE114" g2s:eventId="6" '
                f'g2s:eventDateTime="{ev6_dt}" g2s:transactionId="12345" '
                'g2s:eventAck="true">'
                '<g2s:affectedMeterList>'
                '<g2s:deviceSelect g2s:deviceClass="G2S_noteAcceptor" '
                'g2s:deviceId="1"/>'
                '</g2s:affectedMeterList>'
                '</g2s:eventHandlerLog>'
                '<g2s:eventHandlerLog g2s:logSequence="3" '
                'g2s:deviceClass="G2S_gamePlay" g2s:deviceId="1" '
                'g2s:eventCode="G2S_GPE104" g2s:eventId="5" '
                f'g2s:eventDateTime="{ev5_dt}" g2s:transactionId="0" '
                'g2s:eventAck="false"/>'
                '</g2s:eventHandlerLogList>')
            post_to_host(avp_wrap(avp_class_command(
                "eventHandler", log_list, "265", ehl["sessionId"],
                "G2S_response")))
            time.sleep(0.4)
            with urllib.request.urlopen(STATUS_URL, timeout=5) as resp:
                snap66 = json.loads(resp.read())
            egm66 = snap66.get(EGM_ID, {})
            check("backfill merged 2 NEW entries, deduped the live-seen one "
                  "(eventCount 1 -> 3)", egm66.get("eventCount") == 3,
                  f"got {egm66.get('eventCount')}")
            check("status: ehBackfilledCount=2 + ehLogLastSequence=5",
                  egm66.get("ehBackfilledCount") == 2
                  and egm66.get("ehLogLastSequence") == 5,
                  f"got {egm66.get('ehBackfilledCount')}"
                  f"/{egm66.get('ehLogLastSequence')}")
            evs66 = egm66.get("recentEvents", [])
            ids66 = [e.get("eventId") for e in evs66]
            check("ring holds eventIds 5/6/7 exactly once each (dedupe by "
                  "eventId+eventDateTime)",
                  all(ids66.count(i) == 1 for i in ("5", "6", "7")),
                  f"got {ids66}")
            check("ring TIME-ordered after the merge — backfilled older "
                  "5/6 precede the live-seen newer 7 (GR-26)",
                  [i for i in ids66 if i in ("5", "6", "7")]
                  == ["5", "6", "7"], f"got {ids66}")
            bf = [e for e in evs66 if e.get("source") == "backfill"]
            check("backfilled entries tagged source=backfill with logSequence "
                  "3+4, merged oldest-first",
                  [e.get("logSequence") for e in bf] == [3, 4],
                  f"got {[(e.get('eventId'), e.get('logSequence')) for e in bf]}")
            check("backfilled entry keeps the friendly label + affected POINTER "
                  "only (no meter payload, §4.21.3)",
                  any(e.get("eventCode") == "G2S_NAE114"
                      and "bill" in (e.get("label") or "").lower()
                      and e.get("affected") == ["affectedMeterList"]
                      and e.get("affectedMeters") == 0 for e in bf),
                  f"got {bf}")
            # Dedupe in the LIVE direction: the AVP retries a persisted event we
            # just merged from the log (same eventId=6, §4.1.4). A retry resends
            # the SAME stored record — same eventDateTime. The host must still
            # eventAck — the EGM's outbound queue cannot drain otherwise — but
            # must NOT double-count it in the ring.
            post_to_host(avp_wrap(avp_class_command(
                "eventHandler",
                '<g2s:eventReport g2s:deviceClass="G2S_noteAcceptor" '
                'g2s:deviceId="1" g2s:eventCode="G2S_NAE114" g2s:eventId="6" '
                f'g2s:eventDateTime="{ev6_dt}" g2s:transactionId="12345"/>',
                "266", "58", "G2S_request")))
            ack = expect_host_post("eventAck (duplicate eventId=6)")
            if ack:
                check("duplicate live eventReport still eventAck'd "
                      "(eventId=6, sessionId=58 echoed)",
                      ack["command"] == "eventAck"
                      and ack.get("commandAttrs", {}).get("eventId") == "6"
                      and ack["sessionId"] == "58",
                      f"got {ack.get('command')}/{ack.get('commandAttrs')}"
                      f"/{ack.get('sessionId')}")
            time.sleep(0.3)
            with urllib.request.urlopen(STATUS_URL, timeout=5) as resp:
                snap67 = json.loads(resp.read())
            egm67 = snap67.get(EGM_ID, {})
            check("duplicate NOT double-counted (eventCount still 3, one "
                  "eventId=6 in the ring)",
                  egm67.get("eventCount") == 3
                  and [e.get("eventId")
                       for e in egm67.get("recentEvents", [])].count("6") == 1,
                  f"got {egm67.get('eventCount')}")

        gd = (extras.get("getDescriptor") or [None])[0]
        if not gd:
            # The engine defaults to --harvest OFF, so the join didn't ask.
            # Trigger the identical read on demand: the gamePlay sweep keys
            # off the descriptorList RESPONSE, not off who asked for it.
            cs, cbody = post_command({"action": "getDescriptor"})
            check("getDescriptor command accepted (harvest off — on-demand "
                  "trigger)", cs == 200 and cbody.get("ok"))
            gd = expect_host_post("getDescriptor (on demand)")
        gp_ids = first_light_gameplay_ids()
        gp_dev1 = gp_ids[0]
        gp_sids = {}
        if gd:
            print("— Step 6.65: descriptor harvest — the REAL first-light "
                  "descriptorList (153 devices) triggers the staggered "
                  "gamePlay sweep")
            check("first-light capture sanity: 119 gamePlay devices",
                  len(gp_ids) == 119, f"got {len(gp_ids)}")
            post_to_host(avp_wrap(first_light_reply(
                "descriptorList_sid1003.xml", gd["sessionId"])))
            time.sleep(0.3)
            with urllib.request.urlopen(STATUS_URL, timeout=5) as resp:
                snap2 = json.loads(resp.read())
            check("status reflects harvested descriptors (the REAL "
                  "153-device inventory)",
                  snap2.get(EGM_ID, {}).get("descriptorCount") == 153,
                  f"got {snap2.get(EGM_ID, {}).get('descriptorCount')}")
            # The gamePlay sweep no longer auto-fires on the descriptorList
            # response (game_sweep defaults OFF — the 119-device × 3-read
            # flood starved the join FIFO). Trigger it on demand, the same
            # way the harvest-off path triggers getDescriptor above: this is
            # the operator "refresh all games" action (/api/command gameSweep).
            cs, cbody = post_command({"action": "gameSweep"})
            check("gameSweep command accepted (sweep off by default — "
                  "on-demand trigger)", cs == 200 and cbody.get("ok"))
            # Bounded FIRST SLICE of the sweep: the first batch (8 devices x
            # status/profile/denoms), strict order — trio per device, devices
            # ascending, every read an EMPTY G2S_request element (ttl=30000)
            # with its own fresh sessionId and increasing commandIds.
            trio = ("getGamePlayStatus", "getGamePlayProfile", "getGameDenoms")
            slice_bad = []
            last_cid = 0
            sids_seen = set()
            for dev, want in [(d, w) for d in gp_ids[:8] for w in trio]:
                m = expect_host_post(f"sweep {want} (dev {dev})")
                if not m:
                    slice_bad.append(f"missing {want}/dev{dev}")
                    break  # fail fast — the sweep never started/stalled
                if not (m["class"] == "gamePlay" and m["command"] == want
                        and m["deviceId"] == dev
                        and m["sessionType"] == "G2S_request"
                        and m.get("commandAttrs", {}) == {}
                        and m["timeToLive"] == "30000"
                        and int(m["commandId"]) > last_cid
                        and m["sessionId"] not in sids_seen):
                    slice_bad.append(
                        f"got {m.get('command')}/dev{m.get('deviceId')}"
                        f"/cid{m.get('commandId')} want {want}/dev{dev}")
                last_cid = int(m["commandId"])
                sids_seen.add(m["sessionId"])
                if dev == gp_dev1:
                    gp_sids[want] = m["sessionId"]
            check("sweep FIRST SLICE in order: 8 devices x3 reads — trio per "
                  "device, ascending devices, empty elements, fresh "
                  "sessionIds, increasing commandIds",
                  not slice_bad, "; ".join(slice_bad[:4]))
            check("each dev-1 gamePlay read has its OWN sessionId",
                  len(gp_sids) == 3 and len(set(gp_sids.values())) == 3,
                  f"got {gp_sids}")
            # The REST of the sweep trickles in staggered batches (FIFO-drain
            # + settle between batches — never one 357-read flood): drain it
            # all, then require exact coverage — every remaining REAL device
            # exactly once, trio order intact per device.
            seen = {}
            for _ in range(len(gp_ids[8:]) * 3):
                m = expect_host_post("sweep read (staggered batches)",
                                     timeout=20)
                if not m:
                    break
                seen.setdefault(m["deviceId"], []).append(m["command"])
            check("staggered sweep covers EXACTLY the remaining 111 REAL "
                  "devices — status->profile->denoms each, none twice",
                  set(seen) == set(gp_ids[8:])
                  and all(v == list(trio) for v in seen.values()),
                  f"got {len(seen)} devices")

            print("— Step 6.66: gamePlay read path round-trip (G2S-21 — now "
                  "fed by the join sweep)")
            # gamePlayStatus (§6.11): themeId/paytableId REQUIRED; hostEnabled
            # OMITTED (optional, default true — never a fault, no re-enable
            # logic); NO playState attribute exists on this element.
            # egmEnabled=false is the legal 0-active-denoms state (§6.2.2).
            if gp_sids.get("getGamePlayStatus"):
                post_to_host(avp_wrap(avp_class_command(
                    "gamePlay",
                    '<g2s:gamePlayStatus g2s:configurationId="0" '
                    'g2s:egmEnabled="false" g2s:themeId="IGT_doubleDiamond" '
                    'g2s:paytableId="IGT_dd0094" g2s:generalTilt="false"/>',
                    "310", gp_sids["getGamePlayStatus"], "G2S_response",
                    device_id=gp_dev1)))
            # gamePlayProfile (§6.13): >=1 wagerCategoryItem (theoPaybackPct
            # has 2 implied decimals) and >=1 winLevelItem.
            if gp_sids.get("getGamePlayProfile"):
                prof = ('<g2s:gamePlayProfile g2s:configurationId="0" '
                        'g2s:themeId="IGT_doubleDiamond" '
                        'g2s:paytableId="IGT_dd0094" '
                        'g2s:maxWagerCredits="3" g2s:progAllowed="false" '
                        'g2s:secondaryAllowed="false">'
                        '<g2s:wagerCategoryList>'
                        '<g2s:wagerCategoryItem g2s:wagerCategory="1" '
                        'g2s:theoPaybackPct="9412" g2s:minWagerCredit="1" '
                        'g2s:maxWagerCredit="3"/>'
                        '</g2s:wagerCategoryList>'
                        '<g2s:winLevelList>'
                        '<g2s:winLevelItem g2s:winLevelIndex="0" '
                        'g2s:winLevelCombo="3 Double Diamonds" '
                        'g2s:progressiveAllowed="false"/>'
                        '<g2s:winLevelItem g2s:winLevelIndex="1" '
                        'g2s:winLevelCombo="Any Bar"/>'
                        '</g2s:winLevelList>'
                        '</g2s:gamePlayProfile>')
                post_to_host(avp_wrap(avp_class_command(
                    "gamePlay", prof, "311", gp_sids["getGamePlayProfile"],
                    "G2S_response", device_id=gp_dev1)))
            # gameDenomList (§6.16): ranges then singles per the §6.24.4.2
            # example; denomIds in millicents (25000 = 25¢); ALL-INACTIVE
            # would be legal too.
            if gp_sids.get("getGameDenoms"):
                post_to_host(avp_wrap(avp_class_command(
                    "gamePlay",
                    '<g2s:gameDenomList>'
                    '<g2s:gameRange g2s:denomMin="5000" g2s:denomMax="25000" '
                    'g2s:denomInterval="5000" g2s:active="false"/>'
                    '<g2s:gameDenom g2s:denomId="25000" g2s:active="true"/>'
                    '<g2s:gameDenom g2s:denomId="100000" g2s:active="false"/>'
                    '</g2s:gameDenomList>',
                    "312", gp_sids["getGameDenoms"], "G2S_response",
                    device_id=gp_dev1)))
            time.sleep(0.4)
            with urllib.request.urlopen(STATUS_URL, timeout=5) as resp:
                snap_gp = json.loads(resp.read())
            gp = snap_gp.get(EGM_ID, {}).get("gamePlay", {}).get(gp_dev1, {})
            check("status: gamePlay/1 theme+paytable captured from "
                  "gamePlayStatus",
                  gp.get("themeId") == "IGT_doubleDiamond"
                  and gp.get("paytableId") == "IGT_dd0094", f"got {gp}")
            check("status: egmEnabled=false honored, ABSENT hostEnabled "
                  "defaults true (§6.11 Table 6.5 — not a fault)",
                  gp.get("egmEnabled") == "false"
                  and gp.get("hostEnabled") == "true",
                  f"got egm={gp.get('egmEnabled')}/host={gp.get('hostEnabled')}")
            gprof = gp.get("profile", {})
            wcs = gprof.get("wagerCategories", [])
            check("status: profile wagerCategory theoPaybackPct=9412 + "
                  "maxWagerCredits=3",
                  gprof.get("maxWagerCredits") == "3" and len(wcs) == 1
                  and wcs[0].get("theoPaybackPct") == "9412"
                  and wcs[0].get("maxWagerCredit") == "3", f"got {gprof}")
            wls = gprof.get("winLevels", [])
            check("status: profile win levels parsed (2, combo text + "
                  "progressiveAllowed default false)",
                  len(wls) == 2
                  and wls[0].get("winLevelCombo") == "3 Double Diamonds"
                  and wls[1].get("winLevelIndex") == "1"
                  and wls[1].get("progressiveAllowed") == "false",
                  f"got {wls}")
            check("status: denoms parsed — activeDenoms=[25000] (25¢), $1 "
                  "single inactive, range 5000-25000 recorded",
                  gp.get("activeDenoms") == ["25000"]
                  and any(d.get("denomId") == "100000"
                          and d.get("active") == "false"
                          for d in gp.get("denoms", []))
                  and gp.get("denomRanges") == [{
                      "denomMin": "5000", "denomMax": "25000",
                      "denomInterval": "5000", "active": "false"}],
                  f"got denoms={gp.get('denoms')} ranges={gp.get('denomRanges')} "
                  f"active={gp.get('activeDenoms')}")

            # Disappearing-games regression fix: the gamePlay inventory now
            # persists to config_inventory.json's gamePlay section (debounced,
            # trailing-edge — one write per sweep, not per parse) so a host
            # restart RESEEDS the Test Panel games card instead of blanking it
            # until a manual re-sweep. The persist is trailing-edge (fires once
            # the sweep goes quiet), so POLL the file rather than sleeping a
            # fixed time — the quiet point drifts with the sweep's batch pacing.
            # Same warm-restart proof as the optionConfig section in Step 6.5.
            gp_saved = {}
            for _ in range(40):  # up to ~12s
                if inv_file.is_file():
                    gp_saved = json.loads(inv_file.read_text()).get(
                        "egms", {}).get(EGM_ID, {}).get("gamePlay", {})
                    if gp_saved.get(gp_dev1, {}).get("themeId"):
                        break
                time.sleep(0.3)
            check("gamePlay inventory PERSISTED to config_inventory.json "
                  "(survives a warm restart — no more blank games card)",
                  gp_saved.get(gp_dev1, {}).get("themeId")
                  == "IGT_doubleDiamond"
                  and gp_saved.get(gp_dev1, {}).get("paytableId")
                  == "IGT_dd0094",
                  f"got {gp_saved.get(gp_dev1)}")

    print("— Step 6.7: cabinet + meters read round-trips (via /api/command)")
    cs, cbody = post_command({"action": "getCabinetStatus"})
    check("getCabinetStatus command accepted", cs == 200 and cbody.get("ok"))
    m = expect_host_post("getCabinetStatus")
    if m:
        check("getCabinetStatus under cabinet class", m["class"] == "cabinet",
              f"got {m['class']}")
        post_to_host(avp_wrap(avp_class_command(
            "cabinet",
            '<g2s:cabinetStatus g2s:egmState="G2S_hostDisabled" '
            'g2s:generalTilt="false" g2s:mainDoorOpen="true" '
            'g2s:hostEnabled="false" g2s:hostLocked="false"/>',
            "250", m["sessionId"], "G2S_response")))
    post_command({"action": "getMeterInfo"})
    m = expect_host_post("getMeterInfo")
    if m:
        check("getMeterInfo under meters class", m["class"] == "meters",
              f"got {m['class']}")
        mi = ('<g2s:meterInfo g2s:meterInfoType="G2S_onDemand" '
              f'g2s:meterDateTime="{now_iso()}">'
              '<g2s:deviceMeters g2s:deviceClass="G2S_cabinet" g2s:deviceId="0">'
              '<g2s:simpleMeter g2s:meterName="G2S_playedCount" '
              'g2s:meterValue="1234"/>'
              '<g2s:simpleMeter g2s:meterName="G2S_wonCount" '
              'g2s:meterValue="567"/>'
              '</g2s:deviceMeters></g2s:meterInfo>')
        post_to_host(avp_wrap(avp_class_command(
            "meters", mi, "251", m["sessionId"], "G2S_response")))
    time.sleep(0.4)
    with urllib.request.urlopen(STATUS_URL, timeout=5) as resp:
        snap5 = json.loads(resp.read())
    egm5 = snap5.get(EGM_ID, {})
    check("cabinet egmState captured",
          egm5.get("cabinet", {}).get("egmState") == "G2S_hostDisabled",
          f"got {egm5.get('cabinet', {}).get('egmState')}")
    check("cabinet mainDoorOpen captured",
          egm5.get("cabinet", {}).get("mainDoorOpen") == "true")
    check("meters parsed (meterCount=2)", egm5.get("meterCount") == 2,
          f"got {egm5.get('meterCount')}")

    print("— Step 6.8: commConfig ownership cycle (claimOwnership)")
    cs, cbody = post_command({"action": "claimOwnership"})
    check("claimOwnership command accepted", cs == 200 and cbody.get("ok"))
    config_id = None
    m = expect_host_post("enterCommConfigMode")
    if m:
        check("enterCommConfigMode under commConfig class",
              m["class"] == "commConfig", f"got {m['class']}")
        check("enterCommConfigMode enable=true",
              m.get("commandAttrs", {}).get("enable") == "true",
              f"got {m.get('commandAttrs')}")
        post_to_host(avp_wrap(avp_class_command(
            "commConfig",
            '<g2s:commConfigModeStatus g2s:enabled="true" '
            'g2s:configurationId="0"/>',
            "260", m["sessionId"], "G2S_response")))
    m = expect_host_post("setCommChange")
    if m:
        check("setCommChange under commConfig class",
              m["class"] == "commConfig", f"got {m['class']}")
        config_id = m.get("commandAttrs", {}).get("configurationId")
        check("setCommChange carries a configurationId", bool(config_id),
              f"got {m.get('commandAttrs')}")
        post_to_host(avp_wrap(avp_class_command(
            "commConfig",
            f'<g2s:commChangeStatus g2s:configurationId="{config_id}" '
            'g2s:transactionId="9001" g2s:changeStatus="G2S_pending" '
            'g2s:applyCondition="G2S_immediate" g2s:changeException="0"/>',
            "261", m["sessionId"], "G2S_response")))
    m = expect_host_post("authorizeCommChange")
    if m:
        check("authorizeCommChange echoes txn 9001",
              m.get("commandAttrs", {}).get("transactionId") == "9001",
              f"got {m.get('commandAttrs')}")
        check("authorizeCommChange echoes the configurationId",
              m.get("commandAttrs", {}).get("configurationId") == config_id,
              f"got {m.get('commandAttrs')}")
        post_to_host(avp_wrap(avp_class_command(
            "commConfig",
            f'<g2s:commChangeStatus g2s:configurationId="{config_id}" '
            'g2s:transactionId="9001" g2s:changeStatus="G2S_applied" '
            'g2s:applyCondition="G2S_immediate" g2s:changeException="0"/>',
            "262", m["sessionId"], "G2S_response")))
    time.sleep(0.4)
    with urllib.request.urlopen(STATUS_URL, timeout=5) as resp:
        snap6 = json.loads(resp.read())
    check("ownership stage = applied",
          snap6.get(EGM_ID, {}).get("commConfigStage") == "applied",
          f"got {snap6.get(EGM_ID, {}).get('commConfigStage')}")
    # The heavy probes that used to ride here (setMeterSub, cabinet reads,
    # the gamePlay sweep, getEventHandlerLog) moved into the JOIN sequence
    # 2026-07-02 (Steps 6.5-6.66) — the branch keeps only the ownership-map
    # re-read, the subscription re-arm, the §8.7 config-mode EXIT (added
    # 2026-07-07 — a completed cycle must release the EGM from its
    # config-mode screen), and the noteAcceptor reads.
    reprobe = drain_host_posts(3)
    check("post-apply re-probe re-reads the ownership map + re-arms the "
          "subscription + exits config mode (moved probes ride the join now)",
          reprobe == ["getCommHostList", "setEventSub", "enterCommConfigMode"],
          f"got {reprobe}")

    print("— Step 6.865: noteAcceptor read path (G2S-23, post-ownership "
          "re-probe)")
    # OWNERSHIP APPLIED also fires the noteAcceptor reads — status then
    # profile, right after the ownership-map re-read + subscription re-arm.
    # Both requests are EMPTY elements (§13.9/§13.11); no config-sync has
    # revealed an acceptor yet, so the default deviceId=1 is used.
    na_sids = {}
    for want in ("getNoteAcceptorStatus", "getNoteAcceptorProfile"):
        m = expect_host_post(want)
        if not m:
            continue
        check(f"{want} under noteAcceptor class as G2S_request at deviceId=1",
              m["class"] == "noteAcceptor" and m["command"] == want
              and m["sessionType"] == "G2S_request" and m["deviceId"] == "1",
              f"got {m.get('class')}.{m.get('command')}/{m.get('sessionType')}"
              f"/dev={m.get('deviceId')}")
        check(f"{want} is an EMPTY element with ttl=30000",
              m.get("commandAttrs", {}) == {} and m["timeToLive"] == "30000",
              f"got {m.get('commandAttrs')}/ttl={m.get('timeToLive')}")
        na_sids[want] = m["sessionId"]
    check("each noteAcceptor read has its OWN sessionId",
          len(na_sids) == 2 and len(set(na_sids.values())) == 2,
          f"got {na_sids}")
    # noteAcceptorStatus (§13.10 Table 13.3): EVERY attribute optional —
    # hostEnabled is OMITTED here and must default TRUE (the repo rule);
    # stackerNearlyFull=true is a status-only warning (NAE117 never
    # disables); every unlisted fault boolean must default false.
    if na_sids.get("getNoteAcceptorStatus"):
        post_to_host(avp_wrap(avp_class_command(
            "noteAcceptor",
            '<g2s:noteAcceptorStatus g2s:configurationId="1234" '
            'g2s:egmEnabled="true" g2s:stackerNearlyFull="true" '
            'g2s:noteValueInEscrow="0"/>',
            "320", na_sids["getNoteAcceptorStatus"], "G2S_response")))
    # noteAcceptorProfile (§13.12 Tables 13.4-13.6): noteEnabled/
    # voucherEnabled required; restartStatus/minLogEntries OMITTED must
    # take their defaults (true/35); noteAcceptorData children carry the
    # per-note table — $1 + $20 active, $100 configured but inactive.
    if na_sids.get("getNoteAcceptorProfile"):
        prof = ('<g2s:noteAcceptorProfile g2s:configurationId="1234" '
                'g2s:requiredForPlay="false" g2s:noteEnabled="true" '
                'g2s:voucherEnabled="true">'
                '<g2s:noteAcceptorData g2s:currencyId="USD" '
                'g2s:denomId="100000" g2s:baseCashableAmt="100000" '
                'g2s:noteActive="true"/>'
                '<g2s:noteAcceptorData g2s:currencyId="USD" '
                'g2s:denomId="2000000" g2s:baseCashableAmt="2000000" '
                'g2s:noteActive="true"/>'
                '<g2s:noteAcceptorData g2s:currencyId="USD" '
                'g2s:denomId="10000000" g2s:baseCashableAmt="10000000" '
                'g2s:noteActive="false"/>'
                '</g2s:noteAcceptorProfile>')
        post_to_host(avp_wrap(avp_class_command(
            "noteAcceptor", prof, "321",
            na_sids["getNoteAcceptorProfile"], "G2S_response")))
    time.sleep(0.4)
    with urllib.request.urlopen(STATUS_URL, timeout=5) as resp:
        snap_na = json.loads(resp.read())
    na = snap_na.get(EGM_ID, {}).get("noteAcceptor", {})
    na_st = na.get("status", {})
    check("status: ABSENT hostEnabled defaults true, egmEnabled honored, "
          "stackerNearlyFull raised (§13.10 Table 13.3)",
          na_st.get("hostEnabled") == "true"
          and na_st.get("egmEnabled") == "true"
          and na_st.get("stackerNearlyFull") == "true",
          f"got {na_st}")
    check("status: unlisted fault/door booleans default false, escrow 0",
          na_st.get("doorOpen") == "false"
          and na_st.get("stackerFull") == "false"
          and na_st.get("acceptorJam") == "false"
          and na_st.get("noteValueInEscrow") == "0", f"got {na_st}")
    na_prof = na.get("profile", {})
    check("status: profile scalars — noteEnabled/voucherEnabled captured, "
          "OMITTED restartStatus/minLogEntries default true/35 (Table 13.4)",
          na_prof.get("noteEnabled") == "true"
          and na_prof.get("voucherEnabled") == "true"
          and na_prof.get("restartStatus") == "true"
          and na_prof.get("minLogEntries") == "35"
          and na_prof.get("requiredForPlay") == "false", f"got {na_prof}")
    check("status: notes table from the profile readback — 3 notes, denoms "
          "in dollars, activeNotes=[$1,$20] ($100 configured-inactive)",
          len(na.get("notes", [])) == 3
          and na.get("activeNotes") == ["$1", "$20"]
          and na.get("notesSource") == "profile"
          and any(n.get("denom") == "$100" and n.get("noteActive") == "false"
                  for n in na.get("notes", [])),
          f"got notes={na.get('notes')} active={na.get('activeNotes')}")
    check("status: top-level noteEnabled mirrors the profile",
          na.get("noteEnabled") == "true", f"got {na.get('noteEnabled')}")

    print("— Step 6.88: gamePlay read via /api/command, explicit deviceId "
          "(G2S-21)")
    # /api/command accepts an optional deviceId (multi-game cabinets expose
    # one gamePlay device per theme+paytable) — parsed into its OWN slot,
    # never clobbering device 1's harvested state.
    cs, cbody = post_command({"action": "getGamePlayStatus", "deviceId": 2})
    check("getGamePlayStatus deviceId=2 command accepted",
          cs == 200 and cbody.get("ok"))
    m = expect_host_post("getGamePlayStatus (dev 2)")
    if m:
        check("read addressed at gamePlay deviceId=2 as G2S_request",
              m["command"] == "getGamePlayStatus" and m["deviceId"] == "2"
              and m["class"] == "gamePlay"
              and m["sessionType"] == "G2S_request",
              f"got {m.get('command')} dev={m.get('deviceId')}")
        post_to_host(avp_wrap(avp_class_command(
            "gamePlay",
            '<g2s:gamePlayStatus g2s:themeId="IGT_bonusPoker" '
            'g2s:paytableId="IGT_bp0090"/>',
            "313", m["sessionId"], "G2S_response", device_id="2")))
        time.sleep(0.3)
        with urllib.request.urlopen(STATUS_URL, timeout=5) as resp:
            snap_gp2 = json.loads(resp.read())
        gp_all = snap_gp2.get(EGM_ID, {}).get("gamePlay", {})
        check("status: device 2 keyed separately (theme=IGT_bonusPoker, "
              "enabled defaults true), device 1 untouched",
              gp_all.get("2", {}).get("themeId") == "IGT_bonusPoker"
              and gp_all.get("2", {}).get("egmEnabled") == "true"
              and gp_all.get("1", {}).get("themeId") == "IGT_doubleDiamond",
              f"got {sorted(gp_all)} dev2={gp_all.get('2')}")

    print("— Step 6.89: gamePlay control — setGamePlayState disable -> "
          "enable round trip, per-device fold (G2S-22)")
    # (a) DISABLE one game. Table 6.4 carries EXACTLY two attributes —
    # enable + disableText (no granular flags, unlike cabinet's Table 3.2)
    # — and the response is gamePlayStatus (§6.9/§6.11), so no chaser read
    # rides the FIFO behind the set.
    cs, cbody = post_command({"action": "setGamePlayState", "deviceId": 2,
                              "enable": False,
                              "disableText": "CasinoNet game hold",
                              "egmId": EGM_ID})
    check("setGamePlayState disable command accepted",
          cs == 200 and cbody.get("ok"))
    m = expect_host_post("setGamePlayState (disable dev 2)")
    if m:
        check("setGamePlayState under gamePlay class as G2S_request at "
              "deviceId=2, ttl=30000",
              m["class"] == "gamePlay" and m["command"] == "setGamePlayState"
              and m["sessionType"] == "G2S_request" and m["deviceId"] == "2"
              and m["timeToLive"] == "30000",
              f"got {m.get('class')}.{m.get('command')}/"
              f"{m.get('sessionType')}/dev={m.get('deviceId')}")
        check("disable sends the Table 6.4 attributes EXACTLY (enable=false "
              "+ disableText, nothing else)",
              m.get("commandAttrs", {}) == {
                  "enable": "false",
                  "disableText": "CasinoNet game hold"},
              f"got {m.get('commandAttrs')}")
    time.sleep(0.3)
    with urllib.request.urlopen(STATUS_URL, timeout=5) as resp:
        snap_gs = json.loads(resp.read())
    gp_all = snap_gs.get(EGM_ID, {}).get("gamePlay", {})
    sc = gp_all.get("2", {}).get("stateChange", {})
    check("status: dev 2 stateChange pending (sent/acked, enable=false) "
          "before the response; dev 1 has NO stateChange",
          sc.get("result") in ("sent", "acked")
          and sc.get("enable") == "false"
          and sc.get("disableText") == "CasinoNet game hold"
          and "stateChange" not in gp_all.get("1", {}),
          f"got dev2={sc} dev1={gp_all.get('1', {}).get('stateChange')}")
    if m:
        # The application response to setGamePlayState is gamePlayStatus
        # (§6.9) echoing hostEnabled — sessionId pairing confirms it and the
        # same handler folds the per-device state.
        post_to_host(avp_wrap(avp_class_command(
            "gamePlay",
            '<g2s:gamePlayStatus g2s:themeId="IGT_bonusPoker" '
            'g2s:paytableId="IGT_bp0090" g2s:hostEnabled="false" '
            'g2s:egmEnabled="true"/>',
            "320", m["sessionId"], "G2S_response", device_id="2")))
        time.sleep(0.3)
        with urllib.request.urlopen(STATUS_URL, timeout=5) as resp:
            snap_gs = json.loads(resp.read())
        gp_all = snap_gs.get(EGM_ID, {}).get("gamePlay", {})
        sc = gp_all.get("2", {}).get("stateChange", {})
        check("status: disable CONFIRMED by the sessionId-matched "
              "gamePlayStatus (hostEnabled=false echoed)",
              sc.get("result") == "confirmed"
              and sc.get("hostEnabled") == "false", f"got {sc}")
        check("status: dev 2 folds hostEnabled=false; dev 1 stays enabled "
              "and stateChange-free",
              gp_all.get("2", {}).get("hostEnabled") == "false"
              and gp_all.get("1", {}).get("hostEnabled") == "true"
              and "stateChange" not in gp_all.get("1", {}),
              f"got dev2={gp_all.get('2', {}).get('hostEnabled')} "
              f"dev1={gp_all.get('1', {}).get('hostEnabled')}")
    # (b) ENABLE — THE RESTORE PATH IS SACRED. enable omitted from the API
    # body defaults true; no disableText may ride on an enable; and the
    # EGM's response omits hostEnabled (ABSENT = TRUE, Table 6.5) so the
    # fold must still restore a clean enabled picture (no stale false).
    cs, cbody = post_command({"action": "setGamePlayState", "deviceId": 2,
                              "egmId": EGM_ID})
    check("setGamePlayState enable (default) command accepted",
          cs == 200 and cbody.get("ok"))
    m = expect_host_post("setGamePlayState (enable dev 2)")
    if m:
        check("enable sends enable=true EXACTLY (no disableText on an "
              "enable)",
              m.get("commandAttrs", {}) == {"enable": "true"}
              and m["deviceId"] == "2",
              f"got {m.get('commandAttrs')} dev={m.get('deviceId')}")
        post_to_host(avp_wrap(avp_class_command(
            "gamePlay",
            '<g2s:gamePlayStatus g2s:themeId="IGT_bonusPoker" '
            'g2s:paytableId="IGT_bp0090"/>',
            "321", m["sessionId"], "G2S_response", device_id="2")))
        time.sleep(0.3)
        with urllib.request.urlopen(STATUS_URL, timeout=5) as resp:
            snap_gs = json.loads(resp.read())
        gp_all = snap_gs.get(EGM_ID, {}).get("gamePlay", {})
        sc = gp_all.get("2", {}).get("stateChange", {})
        check("status: enable CONFIRMED via the ABSENT-hostEnabled default",
              sc.get("result") == "confirmed" and sc.get("enable") == "true"
              and sc.get("hostEnabled") == "true", f"got {sc}")
        check("status: round-trip RESTORED cleanly — dev 2 hostEnabled back "
              "to true (no stale false)",
              gp_all.get("2", {}).get("hostEnabled") == "true",
              f"got {gp_all.get('2', {}).get('hostEnabled')}")
    # (c) a REJECTED set — EXACT attribution needs BOTH the deviceId and the
    # sessionId to echo (a gamePlay-class error response carries NO child
    # command per §1.18.9, so only the pending record can absorb it).
    cs, cbody = post_command({"action": "setGamePlayState", "deviceId": 2,
                              "enable": False, "egmId": EGM_ID})
    check("setGamePlayState disable #2 accepted",
          cs == 200 and cbody.get("ok"))
    m = expect_host_post("setGamePlayState (disable #2)")
    if m:
        check("disable #2 without a disableText sends enable=false only",
              m.get("commandAttrs", {}) == {"enable": "false"},
              f"got {m.get('commandAttrs')}")

        def gp_error(session_id, device_id, error_code):
            # Class-level error response: gamePlay element with errorCode
            # and NO child command (§1.18.9).
            return (
                f'<g2s:g2sMessage xmlns:g2s="{SCHEMA_NS}">\n'
                f'   <g2s:g2sBody g2s:hostId="1" g2s:egmId="{EGM_ID}" '
                f'g2s:dateTimeSent="{now_iso()}">\n'
                f'      <g2s:gamePlay g2s:deviceId="{device_id}" '
                f'g2s:dateTime="{now_iso()}" g2s:commandId="322" '
                f'g2s:sessionType="G2S_response" '
                f'g2s:sessionId="{session_id}" g2s:timeToLive="0" '
                f'g2s:errorCode="{error_code}" '
                f'g2s:errorText="Rejected"/>\n'
                f'   </g2s:g2sBody>\n'
                f'</g2s:g2sMessage>')

        # Wrong sessionId at the right device must NOT flip the record…
        post_to_host(avp_wrap(gp_error("9999", "2", "G2S_GPX002")))
        # …and the right sessionId at the WRONG device must not either.
        post_to_host(avp_wrap(gp_error(m["sessionId"], "1", "G2S_GPX002")))
        time.sleep(0.3)
        with urllib.request.urlopen(STATUS_URL, timeout=5) as resp:
            snap_gs = json.loads(resp.read())
        gp_all = snap_gs.get(EGM_ID, {}).get("gamePlay", {})
        check("status: mismatched errors (wrong sessionId / wrong device) "
              "leave the pending record and dev 1 untouched",
              gp_all.get("2", {}).get("stateChange", {}).get("result")
              in ("sent", "acked")
              and "stateChange" not in gp_all.get("1", {}),
              f"got dev2={gp_all.get('2', {}).get('stateChange')} "
              f"dev1={gp_all.get('1', {}).get('stateChange')}")
        # The exact echo (right device + right sessionId) is the verdict.
        post_to_host(avp_wrap(gp_error(m["sessionId"], "2", "G2S_GPX002")))
        time.sleep(0.3)
        with urllib.request.urlopen(STATUS_URL, timeout=5) as resp:
            snap_gs = json.loads(resp.read())
        sc = snap_gs.get(EGM_ID, {}).get("gamePlay", {}) \
            .get("2", {}).get("stateChange", {})
        check("status: rejection recorded EXACTLY — dev 2 stateChange "
              "error:G2S_GPX002",
              sc.get("result") == "error:G2S_GPX002", f"got {sc}")
    # ...and the restore path still works after a rejection.
    post_command({"action": "setGamePlayState", "deviceId": 2,
                  "enable": True, "egmId": EGM_ID})
    m = expect_host_post("setGamePlayState (enable #2)")
    if m:
        post_to_host(avp_wrap(avp_class_command(
            "gamePlay",
            '<g2s:gamePlayStatus g2s:themeId="IGT_bonusPoker" '
            'g2s:paytableId="IGT_bp0090" g2s:hostEnabled="true"/>',
            "323", m["sessionId"], "G2S_response", device_id="2")))
        time.sleep(0.3)
        with urllib.request.urlopen(STATUS_URL, timeout=5) as resp:
            snap_gs = json.loads(resp.read())
        check("status: restore still confirms after a rejection",
              snap_gs.get(EGM_ID, {}).get("gamePlay", {}).get("2", {})
              .get("stateChange", {}).get("result") == "confirmed",
              f"got {snap_gs.get(EGM_ID, {}).get('gamePlay', {}).get('2')}")

    print("— Step 6.9: web UI + enriched event (labels + affected meters)")
    ui_status, ui_body = None, ""
    try:
        with urllib.request.urlopen("http://127.0.0.1:8081/", timeout=5) as resp:
            ui_status = resp.status
            ui_body = resp.read().decode("utf-8", "replace")
    except Exception as e:  # noqa
        ui_body = f"error {e}"
    check("web console served at /",
          ui_status == 200 and "Casino" in ui_body, f"status={ui_status}")
    # G2S_CBE307 = Cabinet Door Open per the spec's cabinet event table
    # (GR-11 — the old fixture used CBE301, which is really Service Lamp On,
    # under the pre-correction "Main door" label). The deviceMeters group
    # carries its OWN deviceClass/deviceId (cabinet/0), deliberately
    # DIFFERENT from the event's device (cabinet/1): GR-08 requires
    # ride-along meters keyed by the GROUP's device, never the event's.
    ev_xml = (
        '<g2s:eventReport g2s:deviceClass="G2S_cabinet" g2s:deviceId="1" '
        'g2s:eventCode="G2S_CBE307" g2s:eventId="8" '
        f'g2s:eventDateTime="{now_iso()}" g2s:transactionId="0">'
        '<g2s:meterList><g2s:meterInfo>'
        '<g2s:deviceMeters g2s:deviceClass="G2S_cabinet" g2s:deviceId="0">'
        '<g2s:simpleMeter g2s:meterName="G2S_doorOpenCount" g2s:meterValue="42"/>'
        '</g2s:deviceMeters></g2s:meterInfo></g2s:meterList>'
        '</g2s:eventReport>')
    post_to_host(avp_wrap(avp_class_command(
        "eventHandler", ev_xml, "270", "56", "G2S_request")))
    ack = expect_host_post("eventAck")
    if ack:
        check("door-open eventReport acked (eventId=8)",
              ack.get("commandAttrs", {}).get("eventId") == "8",
              f"got {ack.get('commandAttrs')}")
    time.sleep(0.3)
    with urllib.request.urlopen(STATUS_URL, timeout=5) as resp:
        snap7 = json.loads(resp.read())
    evs = snap7.get(EGM_ID, {}).get("recentEvents", [])
    last = evs[-1] if evs else {}
    check("event carries a friendly label",
          "door" in last.get("label", "").lower(), f"got {last.get('label')}")
    check("event categorized as cabinet", last.get("category") == "cabinet",
          f"got {last.get('category')}")
    check("affected meterList folded into meters (affectedMeters>=1)",
          last.get("affectedMeters", 0) >= 1, f"got {last.get('affectedMeters')}")
    mtr7 = snap7.get(EGM_ID, {}).get("meters", {})
    check("ride-along meter keyed by the GROUP's device — G2S_cabinet/0 "
          "from deviceMeters, NOT the event's deviceId=1 (GR-08)",
          mtr7.get("G2S_cabinet/0/G2S_doorOpenCount") == "42"
          and "G2S_cabinet/1/G2S_doorOpenCount" not in mtr7,
          f"got {sorted(mtr7)}")

    print("— Step 6.92: Test Panel static serving (G2S-36) — content types "
          "+ traversal guard")

    # http.client sends the request target VERBATIM (urllib would too, but
    # this makes the raw-dotted-path guarantee explicit for the traversal
    # probes below — no client-side normalization of /../).
    # Derive host/port from the REBASED status url so --host-url isolates
    # these raw probes too (they used to hardcode the default and could
    # mutate the wrong host).
    _raw_u = urllib.parse.urlsplit(STATUS_URL)
    RAW_HOST, RAW_PORT = _raw_u.hostname or "127.0.0.1", _raw_u.port or 8081

    def raw_get(target):
        conn = http.client.HTTPConnection(RAW_HOST, RAW_PORT, timeout=5)
        try:
            conn.request("GET", target)
            resp = conn.getresponse()
            return (resp.status, resp.getheader("Content-Type", ""),
                    resp.read().decode("utf-8", "replace"))
        except Exception as e:  # noqa
            return None, "", f"error {e}"
        finally:
            conn.close()

    # G2S-40 moved the Test Panel's canonical route to /test (+ /testpanel);
    # "/" now prefers the Home UI when webui/home.html exists — the
    # fallback-both-ways assertions live in step 6.9799.
    ui_st, ui_ct, ui_body = raw_get("/test")
    check("GET /test serves the Test Panel (200, text/html)",
          ui_st == 200 and ui_ct.startswith("text/html"),
          f"status={ui_st} type={ui_ct}")
    check("Test Panel is the self-contained cockpit (polls /api/status, "
          "drives /api/command, has the guarded ownership action)",
          "/api/status" in ui_body and "/api/command" in ui_body
          and "claimOwnership" in ui_body and "CabiNet" in ui_body,
          "missing status/command/ownership wiring in served page")
    st2, ct2, body2 = raw_get("/webui/index.html")
    check("GET /webui/index.html serves the same page",
          st2 == 200 and ct2.startswith("text/html") and body2 == ui_body,
          f"status={st2} type={ct2} same={body2 == ui_body}")
    st3, ct3, _ = raw_get("/console")
    check("legacy first-cut console still served at /console (text/html)",
          st3 == 200 and ct3.startswith("text/html"),
          f"status={st3} type={ct3}")
    for probe in ("/webui/../g2s_host.py", "/webui/%2e%2e/g2s_host.py",
                  "/webui/..%2fg2s_host.py", "/webui/foo/../../g2s_host.py"):
        t_st, _, t_body = raw_get(probe)
        check(f"traversal rejected: GET {probe} -> 404, no source leak",
              t_st == 404 and "SCHEMA_NS" not in t_body
              and "TRANSPORT_ACK" not in t_body,
              f"status={t_st} leaked={'SCHEMA_NS' in t_body}")
    m_st, _, m_body = raw_get("/webui/no_such_file.js")
    check("missing webui file -> 404 JSON (not a crash)",
          m_st == 404 and "not found" in m_body, f"status={m_st}")

    print("— Step 6.925: SAS floor registry (/api/sas/report ingest + status)")

    def raw_post(target, body, ctype="application/json"):
        conn = http.client.HTTPConnection(RAW_HOST, RAW_PORT, timeout=5)
        try:
            data = body if isinstance(body, bytes) else body.encode()
            conn.request("POST", target, body=data,
                         headers={"Content-Type": ctype})
            resp = conn.getresponse()
            return resp.status, resp.read().decode("utf-8", "replace")
        except Exception as e:  # noqa
            return None, f"error {e}"
        finally:
            conn.close()

    # A well-formed satellite report registers and round-trips into
    # /api/status under "sas", keyed smibId/address.
    st, body = raw_post("/api/sas/report", json.dumps({
        "smibId": "smib-test", "address": "3", "port": "/dev/ttyAMA0",
        "online": True, "polls": 42, "events": 1,
        "meters": {"0x1A": "1250"},
        "recentEvents": [{"code": "0x12", "label": "slot door closed"}],
        "tickets": {"total": 2, "issued": 2}}))
    check("SAS report accepted (200 ok:true)",
          st == 200 and json.loads(body).get("ok") is True,
          f"status={st} body={body[:120]}")
    sas = get_json(STATUS_URL).get("sas", {})
    entry = sas.get("smib-test/3")
    check("report round-trips into /api/status sas[smib-test/3]",
          bool(entry) and entry.get("online") is True
          and entry.get("polls") == 42, f"got {entry}")
    check("hub stamps report age + not stale immediately",
          entry and entry.get("stale") is False
          and "reportAgeSec" in entry, f"got {entry}")

    # Non-finite numbers are rejected at ingest (would poison every
    # /api/status JSON.parse) — SAS-bridge review F0.
    st, _ = raw_post("/api/sas/report", '{"smibId":"z","polls":NaN}')
    check("NaN in a report -> 400 (never stored, status stays parseable)",
          st == 400, f"status={st}")
    status_valid = True
    try:
        json.loads(urllib.request.urlopen(STATUS_URL, timeout=5).read())
    except ValueError:
        status_valid = False
    check("status still valid JSON after the NaN attempt", status_valid)
    check("the NaN report was never stored",
          "z/1" not in get_json(STATUS_URL).get("sas", {}))

    # Malformed body -> 400, not a crash.
    st, _ = raw_post("/api/sas/report", "{not json")
    check("malformed SAS report -> 400", st == 400, f"status={st}")

    # A non-object payload is refused.
    st, _ = raw_post("/api/sas/report", "[1,2,3]")
    check("non-object SAS report -> 400", st == 400, f"status={st}")

    # The UI pages still serve (the "sas" key must not break the EGM loops).
    s_st, _, s_body = raw_get("/")
    check("Home UI still served with a SAS entry present (200)",
          s_st == 200 and "CabiNet" in s_body, f"status={s_st}")

    print("— Step 6.926: hub.db machine names (/api/names + status 'names')")
    # Set a server-side nickname; it round-trips into /api/status "names"
    # and GET /api/names, and survives the EGM-loop filter.
    st, body = raw_post("/api/names", json.dumps({
        "machineKey": EGM_ID, "name": "Corner cab"}))
    check("set name accepted (200 ok:true)",
          st == 200 and json.loads(body).get("name") == "Corner cab",
          f"status={st} body={body[:120]}")
    names = get_json(STATUS_URL).get("names", {})
    check("name round-trips into /api/status names[EGM]",
          names.get(EGM_ID) == "Corner cab", f"got {names}")
    gn = get_json(STATUS_URL[:-len("/api/status")] + "/api/names")
    check("GET /api/names carries the name + a machines list",
          gn.get("names", {}).get(EGM_ID) == "Corner cab"
          and isinstance(gn.get("machines"), list), f"got {gn}")
    # clearing removes it
    raw_post("/api/names", json.dumps({"machineKey": EGM_ID, "name": ""}))
    check("empty name clears it",
          EGM_ID not in get_json(STATUS_URL).get("names", {}))
    # a missing machineKey is a 400, not a crash
    st, _ = raw_post("/api/names", json.dumps({"name": "x"}))
    check("name POST without machineKey -> 400", st == 400, f"status={st}")
    # hostile inputs stay BOUNDED (hub.db review, live-proven bloat): an
    # oversize key and a wrong-type name are clean 400s — no row created,
    # no raw AttributeError leaked, /api/status never inflates.
    st, _ = raw_post("/api/names", json.dumps({
        "machineKey": "Z" * 300, "name": "x"}))
    check("oversize machineKey -> 400 (registry stays bounded)",
          st == 400, f"status={st}")
    st, body = raw_post("/api/names", json.dumps({
        "machineKey": EGM_ID, "name": 123}))
    check("non-string name -> clean 400 (no attribute-error leak)",
          st == 400 and "strip" not in body, f"status={st} body={body[:120]}")
    check("hostile posts left no name behind",
          EGM_ID not in get_json(STATUS_URL).get("names", {})
          and not any(k.startswith("ZZZ")
                      for k in get_json(STATUS_URL).get("names", {})))
    # the "names" key must not break the Home UI EGM loop
    s_st, _, s_body = raw_get("/")
    check("Home UI still served with a names section present (200)",
          s_st == 200 and "CabiNet" in s_body, f"status={s_st}")
    # F5-less kiosk deploys: /api/status carries the webui build stamp the
    # kiosk watches to reload itself after a UI deploy.
    check("status carries uiStamp (kiosk auto-reload)",
          isinstance(get_json(STATUS_URL).get("uiStamp"), int))

    print("— Step 6.93: noteAcceptor config-sync promotion (G2S-23)")
    # The AVP's optionConfig config-sync pushes the acceptor's real config:
    # G2S_noteAcceptorParams (noteEnabled/voucherEnabled, §13.21.4-5) and the
    # G2S_noteAcceptorDataTable (one complexValue per note: currencyId/
    # denomId/baseCashableAmt/noteActive, §13.21.6-7). The host must promote
    # it to first-class noteAcceptor state — notes table with denoms in
    # dollars — with NO new wire traffic, OVERWRITING the profile-sourced
    # table from step 6.865 (same table, freshest source wins). The note-row
    # sub-values must NOT leak into the flat param map.
    na_options = (
        '<g2s:optionList>'
        '<g2s:deviceOptions g2s:deviceClass="G2S_noteAcceptor" '
        'g2s:deviceId="1">'
        '<g2s:optionGroup g2s:optionGroupId="G2S_noteAcceptorOptions" '
        'g2s:optionGroupName="Note Acceptor Options">'
        '<g2s:optionItem g2s:optionId="G2S_noteAcceptorOptions" '
        'g2s:securityLevel="G2S_operator">'
        '<g2s:optionCurrentValues>'
        '<g2s:complexValue g2s:paramId="G2S_noteAcceptorParams">'
        '<g2s:booleanValue g2s:paramId="G2S_noteEnabled">true'
        '</g2s:booleanValue>'
        '<g2s:booleanValue g2s:paramId="G2S_voucherEnabled">false'
        '</g2s:booleanValue>'
        '</g2s:complexValue>'
        '</g2s:optionCurrentValues>'
        '</g2s:optionItem>'
        '</g2s:optionGroup>'
        '<g2s:optionGroup g2s:optionGroupId="G2S_noteAcceptorDataTable" '
        'g2s:optionGroupName="Note Acceptor Data Table">'
        '<g2s:optionItem g2s:optionId="G2S_noteAcceptorData" '
        'g2s:securityLevel="G2S_operator">'
        '<g2s:optionCurrentValues>'
        '<g2s:complexValue g2s:paramId="G2S_noteAcceptorData">'
        '<g2s:stringValue g2s:paramId="G2S_currencyId">USD</g2s:stringValue>'
        '<g2s:integerValue g2s:paramId="G2S_denomId">100000'
        '</g2s:integerValue>'
        '<g2s:integerValue g2s:paramId="G2S_baseCashableAmt">100000'
        '</g2s:integerValue>'
        '<g2s:booleanValue g2s:paramId="G2S_noteActive">true'
        '</g2s:booleanValue>'
        '</g2s:complexValue>'
        '<g2s:complexValue g2s:paramId="G2S_noteAcceptorData">'
        '<g2s:stringValue g2s:paramId="G2S_currencyId">USD</g2s:stringValue>'
        '<g2s:integerValue g2s:paramId="G2S_denomId">500000'
        '</g2s:integerValue>'
        '<g2s:integerValue g2s:paramId="G2S_baseCashableAmt">500000'
        '</g2s:integerValue>'
        '<g2s:booleanValue g2s:paramId="G2S_noteActive">true'
        '</g2s:booleanValue>'
        '</g2s:complexValue>'
        '<g2s:complexValue g2s:paramId="G2S_noteAcceptorData">'
        '<g2s:stringValue g2s:paramId="G2S_currencyId">USD</g2s:stringValue>'
        '<g2s:integerValue g2s:paramId="G2S_denomId">10000000'
        '</g2s:integerValue>'
        '<g2s:integerValue g2s:paramId="G2S_baseCashableAmt">10000000'
        '</g2s:integerValue>'
        '<g2s:booleanValue g2s:paramId="G2S_noteActive">false'
        '</g2s:booleanValue>'
        '</g2s:complexValue>'
        '</g2s:optionCurrentValues>'
        '</g2s:optionItem>'
        '</g2s:optionGroup>'
        '</g2s:deviceOptions>'
        '</g2s:optionList>')
    post_to_host(avp_wrap(avp_class_command(
        "optionConfig", na_options, "330", "230", "G2S_request",
        ttl="120000")))
    m = expect_host_post("optionListAck (noteAcceptor)")
    if m:
        check("noteAcceptor config-sync answered with optionListAck "
              "(sessionId=230 echoed)",
              m["command"] == "optionListAck"
              and m["class"] == "optionConfig" and m["sessionId"] == "230"
              and m["sessionType"] == "G2S_response",
              f"got {m.get('class')}.{m.get('command')}/{m.get('sessionId')}")
    time.sleep(0.3)
    with urllib.request.urlopen(STATUS_URL, timeout=5) as resp:
        snap_nac = json.loads(resp.read())
    egm_nac = snap_nac.get(EGM_ID, {})
    nac = egm_nac.get("noteAcceptor", {})
    check("status: notes table PROMOTED from the config-sync — 3 rows, "
          "denoms in dollars, activeNotes=[$1,$5], $100 inactive",
          len(nac.get("notes", [])) == 3
          and nac.get("activeNotes") == ["$1", "$5"]
          and nac.get("notesSource") == "config-sync"
          and any(n.get("denomId") == "500000" and n.get("denom") == "$5"
                  and n.get("noteActive") == "true"
                  and n.get("baseCashableAmt") == "500000"
                  and n.get("currencyId") == "USD"
                  for n in nac.get("notes", [])),
          f"got notes={nac.get('notes')} active={nac.get('activeNotes')} "
          f"src={nac.get('notesSource')}")
    check("status: noteEnabled=true / voucherEnabled=false from "
          "G2S_noteAcceptorParams — note-row sub-values kept OUT of the "
          "flat param map",
          nac.get("noteEnabled") == "true"
          and nac.get("voucherEnabled") == "false"
          and "G2S_denomId" not in nac.get("configParams", {})
          and "G2S_currencyId" not in nac.get("configParams", {}),
          f"got noteEnabled={nac.get('noteEnabled')} voucherEnabled="
          f"{nac.get('voucherEnabled')} params={nac.get('configParams')}")
    check("status: config-sync inventory gained G2S_noteAcceptor/1",
          "G2S_noteAcceptor/1" in egm_nac.get("configDevices", []),
          f"got {egm_nac.get('configDevices')}")

    print("— Step 6.94: handpay reset-to-credits (G2S-25)")
    # (a) Config-sync reveals the handpay device's option VALUES — the
    # permission gate for remote key-off (Table 11.46). The host must parse
    # G2S_enabledRemoteCredit & friends out of optionCurrentValues (values,
    # NOT parameter definitions) and surface them in /api/status so a
    # disabled reset-to-credits path is visible before bench day.
    hp_options = (
        '<g2s:optionList>'
        '<g2s:deviceOptions g2s:deviceClass="G2S_handpay" g2s:deviceId="1">'
        '<g2s:optionGroup g2s:optionGroupId="G2S_handpayOptions" '
        'g2s:optionGroupName="Handpay Options">'
        '<g2s:optionItem g2s:optionId="G2S_handpayOptions" '
        'g2s:securityLevel="G2S_operator">'
        '<g2s:optionCurrentValues>'
        '<g2s:complexValue g2s:paramId="G2S_handpayParams">'
        '<g2s:booleanValue g2s:paramId="G2S_enabledRemoteCredit">true'
        '</g2s:booleanValue>'
        '<g2s:booleanValue g2s:paramId="G2S_enabledRemoteVoucher">false'
        '</g2s:booleanValue>'
        '<g2s:booleanValue g2s:paramId="G2S_enabledLocalHandpay">true'
        '</g2s:booleanValue>'
        '<g2s:booleanValue g2s:paramId="G2S_mixCreditTypes">false'
        '</g2s:booleanValue>'
        '</g2s:complexValue>'
        '</g2s:optionCurrentValues>'
        '</g2s:optionItem>'
        '</g2s:optionGroup>'
        '</g2s:deviceOptions>'
        '</g2s:optionList>')
    post_to_host(avp_wrap(avp_class_command(
        "optionConfig", hp_options, "271", "240", "G2S_request",
        ttl="120000")))
    m = expect_host_post("optionListAck")
    if m:
        check("handpay config-sync answered with optionListAck "
              "(optionConfig class, sessionId=240 echoed)",
              m["command"] == "optionListAck" and m["class"] == "optionConfig"
              and m["sessionId"] == "240"
              and m["sessionType"] == "G2S_response",
              f"got {m.get('class')}.{m.get('command')}/{m.get('sessionId')}")
    time.sleep(0.3)
    with urllib.request.urlopen(STATUS_URL, timeout=5) as resp:
        snap_hp = json.loads(resp.read())
    egm_hp = snap_hp.get(EGM_ID, {})
    hpo = egm_hp.get("handpayOptions", {})
    check("status: handpay option VALUES parsed from optionCurrentValues "
          "(remoteCredit=true, remoteVoucher=false, mixCreditTypes=false)",
          hpo.get("G2S_enabledRemoteCredit") == "true"
          and hpo.get("G2S_enabledRemoteVoucher") == "false"
          and hpo.get("G2S_enabledLocalHandpay") == "true"
          and hpo.get("G2S_mixCreditTypes") == "false", f"got {hpo}")
    check("status: handpayRemoteCreditAllowed derived 'true' (the pre-bench "
          "permission lamp)",
          egm_hp.get("handpayRemoteCreditAllowed") == "true",
          f"got {egm_hp.get('handpayRemoteCreditAllowed')}")
    check("status: config-sync inventory gained G2S_handpay/1",
          "G2S_handpay/1" in egm_hp.get("configDevices", []),
          f"got {egm_hp.get('configDevices')}")
    # (b) The lockup: handpayRequest (§11.12) — $1,500 gameWin, remote
    # key-off to credits offered. The host must ack with the bare
    # transactionId echo (§11.13, ack != authorization) and park it pending.
    hp_dt = now_iso()
    hp_req = ('<g2s:handpayRequest g2s:transactionId="777001" '
              'g2s:handpayType="G2S_gameWin" '
              f'g2s:handpayDateTime="{hp_dt}" '
              'g2s:requestCashableAmt="150000000" '
              'g2s:egmPaidCashableAmt="0" '
              'g2s:localHandpay="true" g2s:remoteCredit="true">'
              '<g2s:handpaySourceRef g2s:deviceClass="G2S_gamePlay" '
              'g2s:deviceId="1" g2s:transactionId="4242" '
              'g2s:logSequence="17" g2s:cashableAmt="150000000"/>'
              '</g2s:handpayRequest>')
    post_to_host(avp_wrap(avp_class_command(
        "handpay", hp_req, "272", "241", "G2S_request", ttl="120000")))
    m = expect_host_post("handpayAck")
    if m:
        check("handpayAck echoes transactionId as its ONLY attribute "
              "(§11.13 Table 11.9)",
              m["command"] == "handpayAck"
              and m.get("commandAttrs", {}) == {"transactionId": "777001"},
              f"got {m.get('command')}/{m.get('commandAttrs')}")
        check("handpayAck under handpay class, sessionId=241 echoed, "
              "G2S_response, ttl=0",
              m["class"] == "handpay" and m["sessionId"] == "241"
              and m["sessionType"] == "G2S_response"
              and m["timeToLive"] == "0",
              f"got {m['class']}/{m['sessionId']}/{m['timeToLive']}")
    # The EGM re-sends handpayRequest until acked (§11.12) — a duplicate
    # transactionId must be re-acked WITHOUT double-recording.
    post_to_host(avp_wrap(avp_class_command(
        "handpay", hp_req, "273", "242", "G2S_request", ttl="120000")))
    m = expect_host_post("handpayAck (retry)")
    if m:
        check("duplicate handpayRequest re-acked (EGM retry)",
              m["command"] == "handpayAck"
              and m.get("commandAttrs", {}).get("transactionId") == "777001",
              f"got {m.get('command')}/{m.get('commandAttrs')}")
    time.sleep(0.3)
    with urllib.request.urlopen(STATUS_URL, timeout=5) as resp:
        snap_hp2 = json.loads(resp.read())
    egm_hp2 = snap_hp2.get(EGM_ID, {})
    pend = egm_hp2.get("pendingHandpays", [])
    check("status: ONE pending handpay (retry not double-counted), "
          "handpayCount=1",
          len(pend) == 1 and egm_hp2.get("handpayCount") == 1,
          f"got {len(pend)}/{egm_hp2.get('handpayCount')}")
    check("status: pending record parsed — txn/type/amount/state/"
          "remoteCredit flag",
          pend and pend[0].get("transactionId") == "777001"
          and pend[0].get("handpayType") == "G2S_gameWin"
          and pend[0].get("pendingCashableAmt") == 150000000
          and pend[0].get("state") == "pending"
          and pend[0].get("remoteCredit") == "true"
          and pend[0].get("localHandpay") == "true"
          and pend[0].get("remoteVoucher") == "false",
          f"got {pend}")
    # (c) THE reset: /api/command clearHandpay with NO params — defaults to
    # the oldest pending lockup and keyOffType=G2S_remoteCredit.
    cs, cbody = post_command({"action": "clearHandpay"})
    check("clearHandpay command accepted", cs == 200 and cbody.get("ok"))
    rko_sid = None
    m = expect_host_post("setRemoteKeyOff")
    if m:
        check("setRemoteKeyOff under handpay class as G2S_request at the "
              "lockup's deviceId",
              m["class"] == "handpay" and m["command"] == "setRemoteKeyOff"
              and m["sessionType"] == "G2S_request" and m["deviceId"] == "1"
              and m["timeToLive"] == "30000",
              f"got {m.get('class')}.{m.get('command')}/"
              f"{m.get('sessionType')}/dev={m.get('deviceId')}")
        a = m.get("commandAttrs", {})
        check("keyOffType=G2S_remoteCredit — THE reset-to-credits token "
              "(Table 11.21), default with no override",
              a.get("keyOffType") == "G2S_remoteCredit", f"got {a}")
        check("setRemoteKeyOff echoes txn 777001 + authorizes the FULL "
              "un-paid amount (request − egmPaid, §11.14)",
              a.get("transactionId") == "777001"
              and a.get("keyOffCashableAmt") == "150000000"
              and a.get("keyOffPromoAmt") == "0"
              and a.get("keyOffNonCashAmt") == "0", f"got {a}")
        rko_sid = m["sessionId"]
        time.sleep(0.3)
        with urllib.request.urlopen(STATUS_URL, timeout=5) as resp:
            snap_hp3 = json.loads(resp.read())
        pend3 = snap_hp3.get(EGM_ID, {}).get("pendingHandpays", [])
        check("status: pending entry tracks the send (state=keyOffSent, "
              "keyOffType recorded)",
              pend3 and pend3[0].get("state") == "keyOffSent"
              and pend3[0].get("keyOffType") == "G2S_remoteCredit",
              f"got {pend3}")
        # EGM accepts -> remoteKeyOffAck (JPE103 fires, transfer executing)
        post_to_host(avp_wrap(avp_class_command(
            "handpay", '<g2s:remoteKeyOffAck g2s:transactionId="777001"/>',
            "274", rko_sid, "G2S_response")))
        time.sleep(0.3)
        with urllib.request.urlopen(STATUS_URL, timeout=5) as resp:
            snap_hp4 = json.loads(resp.read())
        pend4 = snap_hp4.get(EGM_ID, {}).get("pendingHandpays", [])
        check("status: remoteKeyOffAck advances state -> keyOffAcked "
              "(JPE103, awaiting keyedOff)",
              pend4 and pend4[0].get("state") == "keyOffAcked",
              f"got {pend4}")
    # (d) Transfer completes: keyedOff (EGM request, §11.16) — the host must
    # keyedOffAck (bare txn echo, §11.17) and close the pending entry out.
    ko = ('<g2s:keyedOff g2s:transactionId="777001" '
          'g2s:keyOffType="G2S_remoteCredit" '
          'g2s:keyOffCashableAmt="150000000" '
          f'g2s:keyOffDateTime="{now_iso()}"/>')
    post_to_host(avp_wrap(avp_class_command(
        "handpay", ko, "275", "243", "G2S_request", ttl="120000")))
    m = expect_host_post("keyedOffAck")
    if m:
        check("keyedOffAck echoes transactionId as its ONLY attribute, "
              "sessionId=243, ttl=0 (§11.17 Table 11.13)",
              m["command"] == "keyedOffAck"
              and m.get("commandAttrs", {}) == {"transactionId": "777001"}
              and m["sessionId"] == "243" and m["class"] == "handpay"
              and m["timeToLive"] == "0",
              f"got {m.get('command')}/{m.get('commandAttrs')}"
              f"/{m.get('sessionId')}")
    time.sleep(0.3)
    with urllib.request.urlopen(STATUS_URL, timeout=5) as resp:
        snap_hp5 = json.loads(resp.read())
    egm_hp5 = snap_hp5.get(EGM_ID, {})
    rec5 = egm_hp5.get("recentHandpays", [])
    check("status: pending CLOSED OUT (pendingHandpays empty), ring holds "
          "the cleared handpay (state=keyedOff, type=G2S_remoteCredit)",
          egm_hp5.get("pendingHandpays") == [] and len(rec5) == 1
          and rec5[0].get("transactionId") == "777001"
          and rec5[0].get("state") == "keyedOff"
          and rec5[0].get("keyOffType") == "G2S_remoteCredit"
          and rec5[0].get("keyOffCashableAmt") == 150000000,
          f"got pend={egm_hp5.get('pendingHandpays')} ring={rec5}")
    # keyedOff retry (§11.16 — EGM re-sends until acked): re-ack, never
    # re-record.
    post_to_host(avp_wrap(avp_class_command(
        "handpay", ko, "276", "244", "G2S_request", ttl="120000")))
    m = expect_host_post("keyedOffAck (retry)")
    if m:
        check("duplicate keyedOff re-acked",
              m["command"] == "keyedOffAck"
              and m.get("commandAttrs", {}).get("transactionId") == "777001",
              f"got {m.get('command')}/{m.get('commandAttrs')}")
    time.sleep(0.3)
    with urllib.request.urlopen(STATUS_URL, timeout=5) as resp:
        snap_hp6 = json.loads(resp.read())
    egm_hp6 = snap_hp6.get(EGM_ID, {})
    check("status: keyedOff retry NOT double-recorded (ring still 1, "
          "handpayCount still 1)",
          len(egm_hp6.get("recentHandpays", [])) == 1
          and egm_hp6.get("handpayCount") == 1,
          f"got {len(egm_hp6.get('recentHandpays', []))}"
          f"/{egm_hp6.get('handpayCount')}")
    # (e) Second lockup: $50 cancelCredit, $10 already EGM-paid, NO remote
    # credit offered. clearHandpay with explicit hpId + keyOffType override;
    # the EGM answers G2S_JPX003 (attendant beat us to it) and the local
    # keyedOff is authoritative (§11.16 — both paths converge on JPE104).
    hp2 = ('<g2s:handpayRequest g2s:transactionId="777002" '
           'g2s:handpayType="G2S_cancelCredit" '
           f'g2s:handpayDateTime="{now_iso()}" '
           'g2s:requestCashableAmt="5000000" '
           'g2s:egmPaidCashableAmt="1000000" '
           'g2s:localHandpay="true" g2s:remoteCredit="false"/>')
    post_to_host(avp_wrap(avp_class_command(
        "handpay", hp2, "277", "245", "G2S_request", ttl="120000")))
    m = expect_host_post("handpayAck #2")
    if m:
        check("second lockup acked (txn 777002)",
              m["command"] == "handpayAck"
              and m.get("commandAttrs", {}).get("transactionId") == "777002",
              f"got {m.get('command')}/{m.get('commandAttrs')}")
    cs, cbody = post_command({"action": "clearHandpay", "hpId": "777002",
                              "keyOffType": "G2S_remoteVoucher"})
    check("clearHandpay accepts explicit hpId + keyOffType override",
          cs == 200 and cbody.get("ok"))
    m = expect_host_post("setRemoteKeyOff (override)")
    if m:
        a = m.get("commandAttrs", {})
        check("override honored: keyOffType=G2S_remoteVoucher at txn 777002",
              a.get("keyOffType") == "G2S_remoteVoucher"
              and a.get("transactionId") == "777002", f"got {a}")
        check("key-off amount = request − egmPaid ($50 − $10 = 4000000 "
              "millicents)",
              a.get("keyOffCashableAmt") == "4000000", f"got {a}")
        # The EGM rejects: class-level G2S_JPX003 (no child command, §1.18.9)
        # — the txn is no longer pending (attendant keyed it off locally).
        err = (
            f'<g2s:g2sMessage xmlns:g2s="{SCHEMA_NS}">\n'
            f'   <g2s:g2sBody g2s:hostId="1" g2s:egmId="{EGM_ID}" '
            f'g2s:dateTimeSent="{now_iso()}">\n'
            f'      <g2s:handpay g2s:deviceId="1" g2s:dateTime="{now_iso()}" '
            f'g2s:commandId="278" g2s:sessionType="G2S_response" '
            f'g2s:sessionId="{m["sessionId"]}" g2s:timeToLive="0" '
            f'g2s:errorCode="G2S_JPX003" '
            f'g2s:errorText="Transaction Is Not Currently Pending"/>\n'
            f'   </g2s:g2sBody>\n'
            f'</g2s:g2sMessage>')
        post_to_host(avp_wrap(err))
        time.sleep(0.3)
        with urllib.request.urlopen(STATUS_URL, timeout=5) as resp:
            snap_hp7 = json.loads(resp.read())
        pend7 = snap_hp7.get(EGM_ID, {}).get("pendingHandpays", [])
        check("status: JPX003 recorded on the in-flight key-off "
              "(state=error:G2S_JPX003) but the entry STAYS pending — "
              "keyedOff is authoritative",
              len(pend7) == 1
              and pend7[0].get("state") == "error:G2S_JPX003",
              f"got {pend7}")
    # The attendant's local key-off arrives — same keyedOff/keyedOffAck
    # convergence as the remote path (§11.1/JPE104), closing the entry.
    ko2 = ('<g2s:keyedOff g2s:transactionId="777002" '
           'g2s:keyOffType="G2S_localHandpay" '
           'g2s:keyOffCashableAmt="4000000" '
           f'g2s:keyOffDateTime="{now_iso()}"/>')
    post_to_host(avp_wrap(avp_class_command(
        "handpay", ko2, "279", "246", "G2S_request", ttl="120000")))
    m = expect_host_post("keyedOffAck #2")
    if m:
        check("local keyedOff still keyedOffAck'd (txn 777002, "
              "sessionId=246)",
              m["command"] == "keyedOffAck"
              and m.get("commandAttrs", {}) == {"transactionId": "777002"}
              and m["sessionId"] == "246",
              f"got {m.get('command')}/{m.get('commandAttrs')}")
    time.sleep(0.3)
    with urllib.request.urlopen(STATUS_URL, timeout=5) as resp:
        snap_hp8 = json.loads(resp.read())
    egm_hp8 = snap_hp8.get(EGM_ID, {})
    rec8 = egm_hp8.get("recentHandpays", [])
    check("status: local key-off closed the second lockup (pending empty, "
          "ring txn 777002 type=G2S_localHandpay, handpayCount=2)",
          egm_hp8.get("pendingHandpays") == [] and len(rec8) == 2
          and rec8[-1].get("transactionId") == "777002"
          and rec8[-1].get("keyOffType") == "G2S_localHandpay"
          and egm_hp8.get("handpayCount") == 2,
          f"got pend={egm_hp8.get('pendingHandpays')} ring={rec8}")
    # (f) Test Panel wiring shipped: the served page drives clearHandpay.
    _, _, hp_ui = raw_get("/test")
    check("Test Panel carries the Handpay card (clearHandpay action + "
          "CLEAR TO CREDITS button)",
          "clearHandpay" in hp_ui and "CLEAR TO CREDITS" in hp_ui,
          "missing clearHandpay/CLEAR TO CREDITS in served page")

    print("— Step 6.95: voucher tier-1 — getValidationData -> validationData")
    # The real AVP sends exactly this ~1s after the optionConfig config-sync
    # on every join (captured 2026-07-01: configurationId=0 validationListId=0
    # numValidationIds=20 valIdListExpired=true, ttl=120000). Pre-feature the
    # host repudiated the class with G2S_APX007; now it must answer a spec-
    # shaped validationData (host-originated POST, sessionId echo, ttl=0).
    post_to_host(avp_wrap(avp_class_command(
        "voucher",
        '<g2s:getValidationData g2s:configurationId="0" '
        'g2s:validationListId="0" g2s:numValidationIds="20" '
        'g2s:valIdListExpired="true"/>',
        "280", "60", "G2S_request", ttl="120000")))
    m = expect_host_post("validationData")
    first_list_id, first_ids = None, []
    if m:
        check("validationData under voucher class",
              m["command"] == "validationData" and m["class"] == "voucher",
              f"got {m.get('class')}.{m.get('command')}")
        check("validationData echoes sessionId=60 as G2S_response",
              m["sessionType"] == "G2S_response" and m["sessionId"] == "60",
              f"got {m['sessionType']}/{m['sessionId']}")
        check("validationData timeToLive=0", m["timeToLive"] == "0",
              f"got {m['timeToLive']}")
        va = m.get("commandAttrs", {})
        first_list_id = va.get("validationListId")
        check("validationListId is a positive integer",
              bool(first_list_id) and first_list_id.isdigit()
              and int(first_list_id) > 0, f"got {first_list_id}")
        check("deleteCurrent=true for an unknown+expired EGM list (§21.16)",
              va.get("deleteCurrent") == "true", f"got {va}")
        items = voucher_id_items(m["raw"])
        first_ids = [v for v, _ in items]
        check("carries the 20 requested validationIdItems",
              len(items) == 20, f"got {len(items)}")
        check("every validationId is exactly 18 digits (Table 21.31)",
              all(v and re.fullmatch(r"\d{18}", v) for v in first_ids))
        check("validationIds unique", len(set(first_ids)) == len(first_ids))
        ints = sorted(int(v) for v in first_ids if v and v.isdigit())
        check("validationIds are not a continuous sequence (§21.3)",
              len(ints) < 2 or any(b - a != 1
                                   for a, b in zip(ints, ints[1:])))
        check("every validationSeed is 1-20 chars (Table 21.31)",
              all(s and len(s) <= 20 for _, s in items))

    print("— Step 6.96: voucher top-up — known current list, fresh ids only")
    second_list_id = None
    if first_list_id:
        post_to_host(avp_wrap(avp_class_command(
            "voucher",
            '<g2s:getValidationData g2s:configurationId="0" '
            f'g2s:validationListId="{first_list_id}" '
            'g2s:numValidationIds="5" g2s:valIdListExpired="false"/>',
            "281", "61", "G2S_request", ttl="120000")))
        m = expect_host_post("validationData #2")
        if m:
            va = m.get("commandAttrs", {})
            second_list_id = va.get("validationListId")
            check("top-up allocates a NEW monotonic validationListId",
                  bool(second_list_id) and second_list_id.isdigit()
                  and int(second_list_id) > int(first_list_id),
                  f"got {second_list_id} after {first_list_id}")
            check("deleteCurrent=false when the EGM list is current (append)",
                  va.get("deleteCurrent") == "false", f"got {va}")
            items2 = voucher_id_items(m["raw"])
            check("top-up carries the 5 requested items", len(items2) == 5,
                  f"got {len(items2)}")
            check("top-up never reuses already-issued ids (§21.3)",
                  not (set(v for v, _ in items2) & set(first_ids)))

    print("— Step 6.97: issueVoucher -> issueVoucherAck (+ retry dedupe, "
          "commitVoucher)")
    # transactionIds must be unique per gate run: the host's voucher store
    # persists across runs and §21.5 dedupe would otherwise swallow a replay.
    iv_txn = str(int(time.time() * 1000))
    if first_ids:
        vid = first_ids[0]
        iv = ('<g2s:issueVoucher '
              f'g2s:transactionId="{iv_txn}" g2s:validationId="{vid}" '
              'g2s:voucherAmt="10000000" g2s:creditType="G2S_cashable" '
              f'g2s:transferDateTime="{now_iso()}" g2s:egmAction="G2S_issued" '
              'g2s:voucherSequence="17"/>')
        post_to_host(avp_wrap(avp_class_command(
            "voucher", iv, "282", "62", "G2S_request", ttl="120000")))
        m = expect_host_post("issueVoucherAck")
        if m:
            check("issueVoucherAck echoes transactionId (only attr, §21.18)",
                  m["command"] == "issueVoucherAck"
                  and m.get("commandAttrs", {}) == {"transactionId": iv_txn},
                  f"got {m.get('command')}/{m.get('commandAttrs')}")
            check("issueVoucherAck under voucher class, sessionId=62 echoed",
                  m["class"] == "voucher" and m["sessionId"] == "62"
                  and m["sessionType"] == "G2S_response",
                  f"got {m['class']}/{m['sessionId']}/{m['sessionType']}")
        # The EGM retries issueVoucher until acked (§21.17); a duplicate txn
        # must be re-acked WITHOUT double-recording (§21.5).
        post_to_host(avp_wrap(avp_class_command(
            "voucher", iv, "283", "63", "G2S_request", ttl="120000")))
        m = expect_host_post("issueVoucherAck (retry)")
        if m:
            check("duplicate issueVoucher re-acked (EGM retry)",
                  m["command"] == "issueVoucherAck"
                  and m.get("commandAttrs", {}).get("transactionId") == iv_txn,
                  f"got {m.get('command')}/{m.get('commandAttrs')}")
        # commitVoucher is ALWAYS sent to close a redemption — even when the
        # host never authorized (voucherHoldTime timeout, egmException=5,
        # §21.21). The host must ack so the EGM's log closes out.
        cv_txn = str(int(iv_txn) + 1)
        post_to_host(avp_wrap(avp_class_command(
            "voucher",
            f'<g2s:commitVoucher g2s:transactionId="{cv_txn}" '
            f'g2s:validationId="{first_ids[1]}" g2s:voucherAmt="0" '
            'g2s:egmException="5"/>',
            "284", "64", "G2S_request", ttl="120000")))
        m = expect_host_post("commitVoucherAck")
        if m:
            check("commitVoucherAck echoes txn + sessionId=64 (§21.22)",
                  m["command"] == "commitVoucherAck"
                  and m.get("commandAttrs", {}).get("transactionId") == cv_txn
                  and m["sessionId"] == "64",
                  f"got {m.get('command')}/{m.get('commandAttrs')}"
                  f"/{m.get('sessionId')}")
        time.sleep(0.3)
        with urllib.request.urlopen(STATUS_URL, timeout=5) as resp:
            snap8 = json.loads(resp.read())
        egm8 = snap8.get(EGM_ID, {})
        check("status: voucherListId tracks the latest list",
              str(egm8.get("voucherListId"))
              == (second_list_id or first_list_id),
              f"got {egm8.get('voucherListId')}")
        check("status: voucherIdsSent matches the latest list size",
              egm8.get("voucherIdsSent") == (5 if second_list_id else 20),
              f"got {egm8.get('voucherIdsSent')}")
        check("status: voucherCount=1 (retry NOT double-counted)",
              egm8.get("voucherCount") == 1, f"got {egm8.get('voucherCount')}")
        recent = egm8.get("recentVouchers", [])
        issued = [v for v in recent if v.get("kind") == "issue"
                  and v.get("transactionId") == iv_txn]
        check("status: recentVouchers carries the issued voucher",
              len(issued) == 1 and issued[0].get("validationId") == vid
              and issued[0].get("voucherAmt") == "10000000",
              f"got {recent}")
        # Persistence: the JSON store must already hold this run's ids + the
        # monotonic list sequence (restart-safe never-reissue guarantee).
        # DATA_DIR follows --data-dir (GR-04) so an isolated host instance
        # can be checked without touching the default ../data store.
        state_path = DATA_DIR / "voucher_state.json"
        st = {}
        if check("voucher_state.json persisted", state_path.exists()):
            st = json.loads(state_path.read_text())
        check("store: validationListSeq >= latest listId",
              st.get("validationListSeq", 0)
              >= int(second_list_id or first_list_id),
              f"got {st.get('validationListSeq')}")
        check("store: every id from this run persisted in issuedIds",
              all(v in st.get("issuedIds", {}) for v in first_ids),
              "missing ids in issuedIds")
        check("store: issued id marked state=issued",
              st.get("issuedIds", {}).get(vid, {}).get("state") == "issued",
              f"got {st.get('issuedIds', {}).get(vid)}")

    print("— Step 6.975: voucher redemption happy path — issue -> redeem -> "
          "authorizeVoucher -> commit (G2S-26 tier 2)")
    # EGM commandIds below use a 28xx block: the host only logs inbound
    # commandIds (no ordering check on the EGM's sequence), and this keeps
    # clear of the 29x/30x ids the later steps already use. transactionIds
    # ride the same per-run-unique base as step 6.97 (§21.5 dedupe persists
    # across gate runs).
    rd_base = int(iv_txn) + 10
    vid2 = first_ids[2] if len(first_ids) > 2 else None
    rd_txn = str(rd_base + 1)
    auth_expected = None
    if vid2:
        # print a second ticket: $25 cashable (the spec example amount)
        iv2_txn = str(rd_base)
        post_to_host(avp_wrap(avp_class_command(
            "voucher",
            f'<g2s:issueVoucher g2s:transactionId="{iv2_txn}" '
            f'g2s:validationId="{vid2}" g2s:voucherAmt="2500000" '
            f'g2s:creditType="G2S_cashable" '
            f'g2s:transferDateTime="{now_iso()}" g2s:egmAction="G2S_issued" '
            f'g2s:voucherSequence="18"/>',
            "2851", "70", "G2S_request", ttl="120000")))
        m = expect_host_post("issueVoucherAck #2")
        if m:
            check("second ticket issued and acked",
                  m["command"] == "issueVoucherAck"
                  and m.get("commandAttrs", {}).get("transactionId")
                  == iv2_txn,
                  f"got {m.get('command')}/{m.get('commandAttrs')}")
        # the ticket goes into the note acceptor -> redeemVoucher (§21.19)
        rv = (f'<g2s:redeemVoucher g2s:transactionId="{rd_txn}" '
              f'g2s:idReaderType="G2S_none" g2s:idNumber="" '
              f'g2s:playerId="" g2s:validationId="{vid2}"/>')
        post_to_host(avp_wrap(avp_class_command(
            "voucher", rv, "2852", "71", "G2S_request", ttl="30000")))
        m = expect_host_post("authorizeVoucher")
        auth_expected = {"transactionId": rd_txn, "validationId": vid2,
                         "voucherAmt": "2500000",
                         "creditType": "G2S_cashable"}
        if m:
            check("authorizeVoucher under voucher class as G2S_response, "
                  "sessionId=71 echoed (§21.30.4 — a response, not a "
                  "request)",
                  m["class"] == "voucher"
                  and m["command"] == "authorizeVoucher"
                  and m["sessionType"] == "G2S_response"
                  and m["sessionId"] == "71",
                  f"got {m.get('class')}.{m.get('command')}/"
                  f"{m.get('sessionType')}/{m.get('sessionId')}")
            check("authorizeVoucher timeToLive=0",
                  m["timeToLive"] == "0", f"got {m['timeToLive']}")
            check("authorized with the ISSUED amount+creditType, NO "
                  "hostException/hostAction/voucherSource (exact attrs, "
                  "§21.20 Table 21.22 defaults)",
                  m.get("commandAttrs", {}) == auth_expected,
                  f"got {m.get('commandAttrs')}")
        # EGM retry while unanswered (§21.5): the SAME transaction must
        # re-draw the IDENTICAL authorization — no state corruption.
        post_to_host(avp_wrap(avp_class_command(
            "voucher", rv, "2853", "71", "G2S_request", ttl="30000")))
        m = expect_host_post("authorizeVoucher (retry)")
        if m:
            check("redeemVoucher retry re-authorized identically "
                  "(idempotent redeemPending)",
                  m["command"] == "authorizeVoucher"
                  and m.get("commandAttrs", {}) == auth_expected,
                  f"got {m.get('command')}/{m.get('commandAttrs')}")
        st = json.loads(state_path.read_text())
        check("store: id held redeemPending with the holder's txn recorded",
              st.get("issuedIds", {}).get(vid2, {}).get("state")
              == "redeemPending"
              and st.get("issuedIds", {}).get(vid2, {})
                    .get("pending", {}).get("transactionId") == rd_txn,
              f"got {st.get('issuedIds', {}).get(vid2)}")
        # stacked -> commitVoucher with the FULL transfer (§21.21: partial
        # transfers are not allowed)
        post_to_host(avp_wrap(avp_class_command(
            "voucher",
            f'<g2s:commitVoucher g2s:transactionId="{rd_txn}" '
            f'g2s:validationId="{vid2}" g2s:voucherAmt="2500000" '
            f'g2s:creditType="G2S_cashable" g2s:transferAmt="2500000" '
            f'g2s:transferDateTime="{now_iso()}" '
            f'g2s:egmAction="G2S_redeemed" g2s:egmException="0"/>',
            "2854", "72", "G2S_request", ttl="120000")))
        m = expect_host_post("commitVoucherAck (redeemed)")
        if m:
            check("redemption commit acked — bare transactionId echo, "
                  "sessionId=72 (§21.22)",
                  m["command"] == "commitVoucherAck"
                  and m.get("commandAttrs", {}) == {"transactionId": rd_txn}
                  and m["sessionId"] == "72",
                  f"got {m.get('command')}/{m.get('commandAttrs')}"
                  f"/{m.get('sessionId')}")
        st = json.loads(state_path.read_text())
        check("store: id CONSUMED — state=redeemed, pending cleared",
              st.get("issuedIds", {}).get(vid2, {}).get("state") == "redeemed"
              and "pending" not in st.get("issuedIds", {}).get(vid2, {}),
              f"got {st.get('issuedIds', {}).get(vid2)}")
        time.sleep(0.3)
        with urllib.request.urlopen(STATUS_URL, timeout=5) as resp:
            snap_rd = json.loads(resp.read())
        egm_rd = snap_rd.get(EGM_ID, {})
        # counts include step 6.97's unsolicited timeout commit (1 rejected)
        check("status: redemption counted ONCE (retry deduped) — "
              "redeemCount=2, redeemedCount=1, redeemRejectedCount=1",
              egm_rd.get("redeemCount") == 2
              and egm_rd.get("redeemedCount") == 1
              and egm_rd.get("redeemRejectedCount") == 1,
              f"got {egm_rd.get('redeemCount')}/"
              f"{egm_rd.get('redeemedCount')}/"
              f"{egm_rd.get('redeemRejectedCount')}")
        rr = [r for r in egm_rd.get("recentRedemptions", [])
              if r.get("transactionId") == rd_txn]
        check("status: recentRedemptions shows the full lifecycle "
              "(authorized -> outcome=redeemed, amounts intact)",
              len(rr) == 1 and rr[0].get("outcome") == "redeemed"
              and rr[0].get("authAction") == "authorized"
              and rr[0].get("hostException") == "0"
              and rr[0].get("voucherAmt") == "2500000"
              and rr[0].get("transferAmt") == "2500000"
              and rr[0].get("validationId") == vid2, f"got {rr}")

    print("— Step 6.976: double-redeem rejected + in-process lock + "
          "rejected commit resets the ticket")
    if vid2:
        # (a) the ALREADY-REDEEMED ticket goes back in -> hostException=2,
        # voucherAmt MUST be 0, creditType still required (§21.20/T21.35)
        rd2_txn = str(rd_base + 2)
        post_to_host(avp_wrap(avp_class_command(
            "voucher",
            f'<g2s:redeemVoucher g2s:transactionId="{rd2_txn}" '
            f'g2s:validationId="{vid2}"/>',
            "2855", "73", "G2S_request", ttl="30000")))
        m = expect_host_post("authorizeVoucher (already redeemed)")
        if m:
            check("double-redeem REJECTED: voucherAmt=0 + hostException=2, "
                  "hostAction left at its G2S_egmAction default "
                  "(exact attrs)",
                  m.get("commandAttrs", {}) == {
                      "transactionId": rd2_txn, "validationId": vid2,
                      "voucherAmt": "0", "creditType": "G2S_cashable",
                      "hostException": "2"},
                  f"got {m.get('commandAttrs')}")
        # the EGM returns the ticket and commits the rejection
        post_to_host(avp_wrap(avp_class_command(
            "voucher",
            f'<g2s:commitVoucher g2s:transactionId="{rd2_txn}" '
            f'g2s:validationId="{vid2}" g2s:voucherAmt="0" '
            f'g2s:transferAmt="0" g2s:transferDateTime="{now_iso()}" '
            f'g2s:egmAction="G2S_rejected" g2s:egmException="3"/>',
            "2856", "74", "G2S_request", ttl="120000")))
        m = expect_host_post("commitVoucherAck (double-redeem)")
        if m:
            check("rejected commit still acked",
                  m["command"] == "commitVoucherAck"
                  and m.get("commandAttrs", {}).get("transactionId")
                  == rd2_txn,
                  f"got {m.get('command')}/{m.get('commandAttrs')}")
        st = json.loads(state_path.read_text())
        check("store: rejected commit did NOT resurrect the redeemed id",
              st.get("issuedIds", {}).get(vid2, {}).get("state")
              == "redeemed", f"got {st.get('issuedIds', {}).get(vid2)}")
    vid3 = first_ids[3] if len(first_ids) > 3 else None
    if vid3:
        # (b) in-process lock: $5 promo ticket, redeemed at "two places"
        iv3_txn, txn_a, txn_b, txn_c = (str(rd_base + 3), str(rd_base + 4),
                                        str(rd_base + 5), str(rd_base + 6))
        post_to_host(avp_wrap(avp_class_command(
            "voucher",
            f'<g2s:issueVoucher g2s:transactionId="{iv3_txn}" '
            f'g2s:validationId="{vid3}" g2s:voucherAmt="500000" '
            f'g2s:creditType="G2S_promo" '
            f'g2s:transferDateTime="{now_iso()}" g2s:egmAction="G2S_issued" '
            f'g2s:voucherSequence="19"/>',
            "2857", "75", "G2S_request", ttl="120000")))
        expect_host_post("issueVoucherAck #3")
        post_to_host(avp_wrap(avp_class_command(
            "voucher",
            f'<g2s:redeemVoucher g2s:transactionId="{txn_a}" '
            f'g2s:validationId="{vid3}"/>',
            "2858", "76", "G2S_request", ttl="30000")))
        m = expect_host_post("authorizeVoucher (promo)")
        if m:
            check("promo ticket authorized with its ISSUED creditType "
                  "(exact attrs)",
                  m.get("commandAttrs", {}) == {
                      "transactionId": txn_a, "validationId": vid3,
                      "voucherAmt": "500000", "creditType": "G2S_promo"},
                  f"got {m.get('commandAttrs')}")
        # a SECOND transaction for the same id while txn_a holds it pending
        # -> hostException=1 (§21.20: no second authorization until commit)
        post_to_host(avp_wrap(avp_class_command(
            "voucher",
            f'<g2s:redeemVoucher g2s:transactionId="{txn_b}" '
            f'g2s:validationId="{vid3}"/>',
            "2859", "77", "G2S_request", ttl="30000")))
        m = expect_host_post("authorizeVoucher (in process)")
        if m:
            check("second site REJECTED while pending: hostException=1 "
                  "(exact attrs)",
                  m.get("commandAttrs", {}) == {
                      "transactionId": txn_b, "validationId": vid3,
                      "voucherAmt": "0", "creditType": "G2S_cashable",
                      "hostException": "1"},
                  f"got {m.get('commandAttrs')}")
        # the loser commits its rejection FIRST — must NOT clobber the
        # winner's redeemPending hold
        post_to_host(avp_wrap(avp_class_command(
            "voucher",
            f'<g2s:commitVoucher g2s:transactionId="{txn_b}" '
            f'g2s:validationId="{vid3}" g2s:voucherAmt="0" '
            f'g2s:transferAmt="0" g2s:transferDateTime="{now_iso()}" '
            f'g2s:egmAction="G2S_rejected" g2s:egmException="3"/>',
            "2860", "78", "G2S_request", ttl="120000")))
        expect_host_post("commitVoucherAck (loser)")
        st = json.loads(state_path.read_text())
        check("store: the loser's rejected commit left the winner's "
              "redeemPending hold intact",
              st.get("issuedIds", {}).get(vid3, {}).get("state")
              == "redeemPending"
              and st.get("issuedIds", {}).get(vid3, {})
                    .get("pending", {}).get("transactionId") == txn_a,
              f"got {st.get('issuedIds', {}).get(vid3)}")
        # the winner ALSO rejects (game state changed etc.) -> the id must
        # RESET to issued: the ticket stays redeemable (§21.21 p.905)
        post_to_host(avp_wrap(avp_class_command(
            "voucher",
            f'<g2s:commitVoucher g2s:transactionId="{txn_a}" '
            f'g2s:validationId="{vid3}" g2s:voucherAmt="500000" '
            f'g2s:creditType="G2S_promo" g2s:transferAmt="0" '
            f'g2s:transferDateTime="{now_iso()}" '
            f'g2s:egmAction="G2S_rejected" g2s:egmException="7"/>',
            "2861", "79", "G2S_request", ttl="120000")))
        expect_host_post("commitVoucherAck (winner rejects)")
        st = json.loads(state_path.read_text())
        check("store: rejected commit RESET the id to issued "
              "(still redeemable, §21.21)",
              st.get("issuedIds", {}).get(vid3, {}).get("state") == "issued"
              and "pending" not in st.get("issuedIds", {}).get(vid3, {}),
              f"got {st.get('issuedIds', {}).get(vid3)}")
        # third try redeems for real — full authorize/commit cycle again
        post_to_host(avp_wrap(avp_class_command(
            "voucher",
            f'<g2s:redeemVoucher g2s:transactionId="{txn_c}" '
            f'g2s:validationId="{vid3}"/>',
            "2862", "80", "G2S_request", ttl="30000")))
        m = expect_host_post("authorizeVoucher (retry after reset)")
        if m:
            check("reset ticket AUTHORIZED again with the original value "
                  "(exact attrs)",
                  m.get("commandAttrs", {}) == {
                      "transactionId": txn_c, "validationId": vid3,
                      "voucherAmt": "500000", "creditType": "G2S_promo"},
                  f"got {m.get('commandAttrs')}")
        post_to_host(avp_wrap(avp_class_command(
            "voucher",
            f'<g2s:commitVoucher g2s:transactionId="{txn_c}" '
            f'g2s:validationId="{vid3}" g2s:voucherAmt="500000" '
            f'g2s:creditType="G2S_promo" g2s:transferAmt="500000" '
            f'g2s:transferDateTime="{now_iso()}" '
            f'g2s:egmAction="G2S_redeemed" g2s:egmException="0"/>',
            "2863", "81", "G2S_request", ttl="120000")))
        expect_host_post("commitVoucherAck (redeemed after reset)")
        st = json.loads(state_path.read_text())
        check("store: id redeemed on the successful third pass",
              st.get("issuedIds", {}).get(vid3, {}).get("state")
              == "redeemed", f"got {st.get('issuedIds', {}).get(vid3)}")
        time.sleep(0.3)
        with urllib.request.urlopen(STATUS_URL, timeout=5) as resp:
            snap_rd2 = json.loads(resp.read())
        egm_rd2 = snap_rd2.get(EGM_ID, {})
        check("status: counts after the dogfight — redeemCount=6, "
              "redeemedCount=2, redeemRejectedCount=4",
              egm_rd2.get("redeemCount") == 6
              and egm_rd2.get("redeemedCount") == 2
              and egm_rd2.get("redeemRejectedCount") == 4,
              f"got {egm_rd2.get('redeemCount')}/"
              f"{egm_rd2.get('redeemedCount')}/"
              f"{egm_rd2.get('redeemRejectedCount')}")

    print("— Step 6.977: unknown / never-issued validationIds rejected "
          "with hostException=4")
    rd7_txn = str(rd_base + 7)
    post_to_host(avp_wrap(avp_class_command(
        "voucher",
        f'<g2s:redeemVoucher g2s:transactionId="{rd7_txn}" '
        f'g2s:validationId="000000000000000000"/>',
        "2864", "82", "G2S_request", ttl="30000")))
    m = expect_host_post("authorizeVoucher (unknown id)")
    if m:
        check("unknown validationId REJECTED: voucherAmt=0 + "
              "hostException=4 'voucher not found' (exact attrs)",
              m.get("commandAttrs", {}) == {
                  "transactionId": rd7_txn,
                  "validationId": "000000000000000000",
                  "voucherAmt": "0", "creditType": "G2S_cashable",
                  "hostException": "4"},
              f"got {m.get('commandAttrs')}")
    if len(first_ids) > 4:
        # an id we handed out in the validation list but NO ticket was ever
        # printed against — no voucher exists to pay (also 4)
        rd8_txn = str(rd_base + 8)
        post_to_host(avp_wrap(avp_class_command(
            "voucher",
            f'<g2s:redeemVoucher g2s:transactionId="{rd8_txn}" '
            f'g2s:validationId="{first_ids[4]}"/>',
            "2865", "83", "G2S_request", ttl="30000")))
        m = expect_host_post("authorizeVoucher (never issued)")
        if m:
            check("never-issued (unused) id also rejected hostException=4",
                  m.get("commandAttrs", {}).get("hostException") == "4"
                  and m.get("commandAttrs", {}).get("voucherAmt") == "0",
                  f"got {m.get('commandAttrs')}")
        time.sleep(0.3)
        with urllib.request.urlopen(STATUS_URL, timeout=5) as resp:
            snap_rd3 = json.loads(resp.read())
        rr3 = [r for r in snap_rd3.get(EGM_ID, {})
               .get("recentRedemptions", [])
               if r.get("transactionId") == rd7_txn]
        check("status: auth-rejected redemption recorded (authAction="
              "rejected, hostException=4, no outcome until commit)",
              len(rr3) == 1 and rr3[0].get("authAction") == "rejected"
              and rr3[0].get("hostException") == "4"
              and rr3[0].get("outcome") is None, f"got {rr3}")

    print("— Step 6.978: /api/command voidValidationId — bench unstick "
          "(void -> redemption rejected 99)")
    vid5 = first_ids[5] if len(first_ids) > 5 else None
    if vid5:
        iv5_txn, rd9_txn = str(rd_base + 9), str(rd_base + 10)
        post_to_host(avp_wrap(avp_class_command(
            "voucher",
            f'<g2s:issueVoucher g2s:transactionId="{iv5_txn}" '
            f'g2s:validationId="{vid5}" g2s:voucherAmt="1000000" '
            f'g2s:creditType="G2S_cashable" '
            f'g2s:transferDateTime="{now_iso()}" g2s:egmAction="G2S_issued" '
            f'g2s:voucherSequence="20"/>',
            "2866", "84", "G2S_request", ttl="120000")))
        expect_host_post("issueVoucherAck #4")
        cs, cbody = post_command({"action": "voidValidationId"})
        check("voidValidationId without an id refused (ok=false)",
              cs == 200 and cbody.get("ok") is False, f"got {cbody}")
        cs, cbody = post_command({"action": "voidValidationId",
                                  "validationId": "123456789012345678"})
        check("voidValidationId for an unknown id refused (ok=false, "
              "'unknown validationId')",
              cs == 200 and cbody.get("ok") is False
              and "unknown" in str(cbody.get("error", "")),
              f"got {cbody}")
        cs, cbody = post_command({"action": "voidValidationId",
                                  "validationId": vid5})
        check("voidValidationId voids the issued ticket "
              "(ok=true, priorState=issued -> void)",
              cs == 200 and cbody.get("ok") is True
              and cbody.get("priorState") == "issued"
              and cbody.get("state") == "void", f"got {cbody}")
        st = json.loads(state_path.read_text())
        check("store: id persisted as void",
              st.get("issuedIds", {}).get(vid5, {}).get("state") == "void",
              f"got {st.get('issuedIds', {}).get(vid5)}")
        post_to_host(avp_wrap(avp_class_command(
            "voucher",
            f'<g2s:redeemVoucher g2s:transactionId="{rd9_txn}" '
            f'g2s:validationId="{vid5}"/>',
            "2867", "85", "G2S_request", ttl="30000")))
        m = expect_host_post("authorizeVoucher (voided)")
        if m:
            check("voided ticket REJECTED: voucherAmt=0 + hostException=99 "
                  "(exact attrs)",
                  m.get("commandAttrs", {}) == {
                      "transactionId": rd9_txn, "validationId": vid5,
                      "voucherAmt": "0", "creditType": "G2S_cashable",
                      "hostException": "99"},
                  f"got {m.get('commandAttrs')}")
        # Test Panel wiring shipped: the served page drives voidValidationId
        # and renders the redemption history.
        _, _, vd_ui = raw_get("/test")
        check("Test Panel carries the voucher redemption wiring "
              "(voidValidationId action + recentRedemptions rendering)",
              "voidValidationId" in vd_ui and "recentRedemptions" in vd_ui,
              "missing voidValidationId/recentRedemptions in served page")

    # =========================================================== wat — G2S-39
    # The "add credits without paper" class (spec ch.22). Balances persist
    # across gate runs, so every balance assertion works in DELTAS and the
    # player-account slices use a FRESH per-run account; transactionIds ride
    # a per-run-unique millisecond base like the voucher steps (§22.31 dedupe
    # persists in the WatStore across runs).

    print("— Step 6.979: /api/accounts — house seeded + create/adjust/"
          "rename/overdraft/delete (G2S-39)")
    acct = get_json(ACCT_URL)
    house_rec = next((a for a in acct.get("accounts", [])
                      if a.get("id") == "house"), None)
    check("GET /api/accounts: house account seeded (kind=house, listed "
          "first)",
          house_rec is not None and house_rec.get("kind") == "house"
          and acct.get("accounts", [{}])[0].get("id") == "house",
          f"got {[a.get('id') for a in acct.get('accounts', [])]}")
    check("GET /api/accounts: account shape carries the contract fields",
          house_rec is not None and all(
              k in house_rec for k in
              ("id", "name", "kind", "cashableMillicents", "promoMillicents",
               "nonCashMillicents", "createdAt", "lastActivity")),
          f"got {sorted(house_rec or {})}")
    check("GET /api/accounts: ledger is a list (last 100)",
          isinstance(acct.get("ledger"), list),
          f"got {type(acct.get('ledger')).__name__}")
    pname = f"Replay Player {int(time.time())}"
    st_a, body_a = post_accounts({"action": "create", "name": pname})
    player = body_a.get("account") or {}
    pid = player.get("id")
    check("create -> ok:true, fresh kind=player account with zero balances",
          st_a == 200 and body_a.get("ok") is True
          and player.get("kind") == "player" and bool(pid)
          and player.get("cashableMillicents") == 0, f"got {body_a}")
    st_a, body_a = post_accounts({"action": "adjust", "id": pid,
                                  "deltaCashableMillicents": 5000000,
                                  "note": "gate seed"})
    check("adjust +$50 cashable -> ok:true, balance 5000000 mc",
          st_a == 200 and body_a.get("ok") is True
          and body_a.get("account", {}).get("cashableMillicents") == 5000000,
          f"got {body_a}")
    st_a, body_a = post_accounts({"action": "adjust", "id": pid,
                                  "deltaCashableMillicents": -6000000})
    check("player overdraft REJECTED (HTTP 400, ok:false, 'negative')",
          st_a == 400 and body_a.get("ok") is False
          and "negative" in str(body_a.get("error", "")),
          f"got {st_a}/{body_a}")
    acct = get_json(ACCT_URL)
    pnow = next((a for a in acct["accounts"] if a.get("id") == pid), {})
    check("balance untouched after the rejected overdraft + the seed "
          "adjustment hit the ledger (kind=cashable, delta 5000000)",
          pnow.get("cashableMillicents") == 5000000
          and any(e.get("accountId") == pid
                  and e.get("deltaMillicents") == 5000000
                  and e.get("kind") == "cashable"
                  and e.get("note") == "gate seed"
                  for e in acct.get("ledger", [])),
          f"got {pnow} / ledger tail {acct.get('ledger', [])[-3:]}")
    st_a, body_a = post_accounts({"action": "rename", "id": pid,
                                  "name": pname + " II"})
    check("rename -> ok:true with the new name",
          st_a == 200 and body_a.get("ok") is True
          and body_a.get("account", {}).get("name") == pname + " II",
          f"got {body_a}")
    st_a, body_a = post_accounts({"action": "delete", "id": "house"})
    check("deleting the house account refused (it is the default WAT "
          "funding account)",
          st_a == 400 and body_a.get("ok") is False, f"got {st_a}/{body_a}")
    hb = house_cashable()
    st_a, body_a = post_accounts({"action": "adjust", "id": "house",
                                  "deltaCashableMillicents": -(hb + 1000000),
                                  "note": "prove the house may go negative"})
    check("house MAY go negative (ok:true, balance -1000000 mc)",
          st_a == 200 and body_a.get("ok") is True
          and body_a.get("account", {}).get("cashableMillicents")
          == -1000000, f"got {body_a}")
    post_accounts({"action": "adjust", "id": "house",
                   "deltaCashableMillicents": hb + 1000000,
                   "note": "restore"})
    st_a, body_a = post_accounts({"action": "explode", "id": "house"})
    check("unknown /api/accounts action refused (HTTP 400)",
          st_a == 400 and body_a.get("ok") is False, f"got {st_a}/{body_a}")

    print("— Step 6.9791: WAT device reads — watStatus/watProfile fold into "
          "/api/status wat.devices + the one-cash-out-device assist")
    cs, cbody = post_command({"action": "watStatus", "deviceId": "2"})
    check("watStatus command accepted", cs == 200 and bool(cbody.get("ok")))
    m = expect_host_post("getWatStatus (on demand)")
    ws_sid = m["sessionId"] if m else "1001"
    if m:
        check("getWatStatus under wat class dev=2 as G2S_request",
              m["class"] == "wat" and m["command"] == "getWatStatus"
              and m["deviceId"] == "2" and m["sessionType"] == "G2S_request",
              f"got {m.get('class')}.{m.get('command')}/dev="
              f"{m.get('deviceId')}")
    # EGM answers watStatus (response, sessionId echo) — cashOutToWat=true
    # on dev 2, so the exclusivity assist below has something to release.
    post_to_host(avp_wrap(avp_class_command(
        "wat",
        '<g2s:watStatus g2s:configurationId="0" g2s:egmEnabled="true" '
        'g2s:hostEnabled="true" g2s:hostLocked="false" '
        'g2s:cashOutToWat="true"/>',
        "500", ws_sid, "G2S_response", device_id="2")))
    cs, cbody = post_command({"action": "watProfile", "deviceId": "1"})
    check("watProfile command accepted", cs == 200 and bool(cbody.get("ok")))
    m = expect_host_post("getWatProfile (on demand)")
    wp_sid = m["sessionId"] if m else "1001"
    post_to_host(avp_wrap(avp_class_command(
        "wat",
        '<g2s:watProfile g2s:configurationId="0" g2s:restartStatus="true" '
        'g2s:requiredForPlay="false" g2s:minLogEntries="35" '
        'g2s:timeToLive="30000" g2s:idReaderId="0" '
        'g2s:interfaceMode="G2S_hostControl" g2s:cashOutMode="G2S_anyDevice" '
        'g2s:cashOutDelay="15000" g2s:authRequired="false" '
        'g2s:mixCreditTypes="true" g2s:allowNonCash="true" '
        'g2s:hashType="G2S_none"/>',
        "501", wp_sid, "G2S_response", device_id="1")))
    time.sleep(0.3)
    with urllib.request.urlopen(STATUS_URL, timeout=5) as resp:
        snap_wat = json.loads(resp.read())
    wat = snap_wat.get(EGM_ID, {}).get("wat", {})
    check("status: wat.devices['2'] folded status (hostEnabled/egmEnabled "
          "true, cashOutToWat=true)",
          wat.get("devices", {}).get("2", {}).get("cashOutToWat") == "true"
          and wat.get("devices", {}).get("2", {}).get("hostEnabled")
          == "true"
          and wat.get("devices", {}).get("2", {}).get("egmEnabled") == "true",
          f"got {wat.get('devices', {}).get('2')}")
    check("status: wat.devices['1'] folded profile (the live AVP's wire "
          "facts: hostControl/anyDevice/no-auth/mix/nonCash/G2S_none)",
          wat.get("devices", {}).get("1", {}).get("interfaceMode")
          == "G2S_hostControl"
          and wat.get("devices", {}).get("1", {}).get("cashOutMode")
          == "G2S_anyDevice"
          and wat.get("devices", {}).get("1", {}).get("authRequired")
          == "false"
          and wat.get("devices", {}).get("1", {}).get("hashType")
          == "G2S_none", f"got {wat.get('devices', {}).get('1')}")
    check("status: wat carries the contract counters",
          all(k in wat for k in ("pendingTransfers", "recentTransfers",
                                 "transferCount", "toEgmOkMillicents",
                                 "fromEgmOkMillicents")),
          f"got {sorted(wat)}")
    wat_count0 = wat.get("transferCount") or 0
    to_egm_ok0 = wat.get("toEgmOkMillicents") or 0
    from_egm_ok0 = wat.get("fromEgmOkMillicents") or 0
    # setWatCashOut exclusivity assist: claiming dev 1 must FIRST release
    # dev 2 (cashOutToWat is true on at most ONE device, G2S_WTX004).
    cs, cbody = post_command({"action": "setWatCashOut", "deviceId": "1",
                              "enable": True})
    check("setWatCashOut command accepted",
          cs == 200 and bool(cbody.get("ok")))
    m = expect_host_post("setWatCashOut (release dev 2)")
    if m:
        check("one-device rule: the RELEASE rides first — "
              "setWatCashOut cashOutToWat=false to dev 2",
              m["command"] == "setWatCashOut" and m["deviceId"] == "2"
              and m.get("commandAttrs", {}).get("cashOutToWat") == "false",
              f"got {m.get('command')}/dev={m.get('deviceId')}/"
              f"{m.get('commandAttrs')}")
        post_to_host(avp_wrap(avp_class_command(
            "wat", '<g2s:watStatus g2s:cashOutToWat="false"/>',
            "502", m["sessionId"], "G2S_response", device_id="2")))
    m = expect_host_post("setWatCashOut (claim dev 1)")
    if m:
        check("then the claim: setWatCashOut cashOutToWat=true to dev 1",
              m["command"] == "setWatCashOut" and m["deviceId"] == "1"
              and m.get("commandAttrs", {}).get("cashOutToWat") == "true",
              f"got {m.get('command')}/dev={m.get('deviceId')}/"
              f"{m.get('commandAttrs')}")
        post_to_host(avp_wrap(avp_class_command(
            "wat", '<g2s:watStatus g2s:cashOutToWat="true"/>',
            "503", m["sessionId"], "G2S_response", device_id="1")))

    print("— Step 6.9792: addCredits happy path — initiateRequest -> "
          "requestPending -> initiateTransfer -> authorizeTransfer (escrow) "
          "-> commitTransfer -> commitTransferAck (G2S-39)")
    wt_base = int(time.time() * 1000) + 500000  # per-run-unique txn block
    h_cash0 = house_cashable()
    cs, cbody = post_command({"action": "addCredits",
                              "cashableMillicents": 2000000})
    check("addCredits accepted — non-zero requestId, house default account",
          cs == 200 and bool(cbody.get("ok"))
          and int(cbody.get("requestId") or 0) > 0
          and cbody.get("accountId") == "house", f"got {cbody}")
    rid1 = str(cbody.get("requestId"))
    m = expect_host_post("initiateRequest")
    ir_sid = m["sessionId"] if m else "1001"
    if m:
        a = m.get("commandAttrs", {})
        check("initiateRequest under wat class dev=1 as G2S_request "
              "(ttl=30000)",
              m["class"] == "wat" and m["command"] == "initiateRequest"
              and m["deviceId"] == "1" and m["sessionType"] == "G2S_request"
              and m["timeToLive"] == "30000",
              f"got {m.get('class')}.{m.get('command')}/"
              f"{m.get('sessionType')}/ttl={m.get('timeToLive')}")
        check("initiateRequest attrs per §22.25: requestId echo, "
              "G2S_toEgm, G2S_payCredit, reqCashableAmt=2000000, "
              "reduceAmts=true, accountId=house",
              a.get("requestId") == rid1
              and a.get("watDirection") == "G2S_toEgm"
              and a.get("payMethod") == "G2S_payCredit"
              and a.get("reqCashableAmt") == "2000000"
              and a.get("reqPromoAmt") == "0"
              and a.get("reqNonCashAmt") == "0"
              and a.get("reduceAmts") == "true"
              and a.get("accountId") == "house", f"got {a}")
    wt_txn = str(wt_base)
    post_to_host(avp_wrap(avp_class_command(
        "wat",
        f'<g2s:requestPending g2s:transactionId="{wt_txn}" '
        f'g2s:requestId="{rid1}"/>',
        "504", ir_sid, "G2S_response")))
    time.sleep(0.3)
    with urllib.request.urlopen(STATUS_URL, timeout=5) as resp:
        snap_wp = json.loads(resp.read())
    pend = [t for t in snap_wp.get(EGM_ID, {}).get("wat", {})
            .get("pendingTransfers", [])
            if str(t.get("requestId")) == rid1]
    check("status: transfer pending with the EGM-assigned transactionId",
          len(pend) == 1 and pend[0].get("state") == "pending"
          and pend[0].get("transactionId") == wt_txn, f"got {pend}")
    check("status: presentation contract on the pending entry — direction/"
          "deviceId strings, normalized requested-amount trio, ISO stamps",
          pend and pend[0].get("direction") == "G2S_toEgm"
          and pend[0].get("deviceId") == "1"
          and pend[0].get("cashableMillicents") == 2000000
          and pend[0].get("promoMillicents") == 0
          and pend[0].get("nonCashMillicents") == 0
          and str(pend[0].get("createdAt", "")).endswith("Z")
          and str(pend[0].get("updatedAt", "")).endswith("Z"),
          f"got {pend}")
    post_to_host(avp_wrap(avp_class_command(
        "wat",
        f'<g2s:initiateTransfer g2s:transactionId="{wt_txn}" '
        f'g2s:requestId="{rid1}" g2s:idReaderType="G2S_none" '
        'g2s:accountId="house" g2s:watDirection="G2S_toEgm" '
        'g2s:payMethod="G2S_payCredit" g2s:reqCashableAmt="2000000" '
        'g2s:reqPromoAmt="0" g2s:reqNonCashAmt="0" g2s:reduceAmts="true" '
        'g2s:maxAmt="2000000"/>',
        "505", "510", "G2S_request")))
    m = expect_host_post("authorizeTransfer")
    if m:
        a = m.get("commandAttrs", {})
        check("authorizeTransfer is the class RESPONSE (sessionId=510 "
              "echoed, ttl=0, §22.30)",
              m["command"] == "authorizeTransfer" and m["class"] == "wat"
              and m["sessionType"] == "G2S_response"
              and m["sessionId"] == "510" and m["timeToLive"] == "0",
              f"got {m.get('command')}/{m.get('sessionType')}/"
              f"{m.get('sessionId')}/ttl={m.get('timeToLive')}")
        check("authorized in full: txn+requestId echo, accountId=house, "
              "authCashableAmt=2000000, hostException=0",
              a.get("transactionId") == wt_txn and a.get("requestId") == rid1
              and a.get("accountId") == "house"
              and a.get("watDirection") == "G2S_toEgm"
              and a.get("authCashableAmt") == "2000000"
              and a.get("authPromoAmt") == "0"
              and a.get("authNonCashAmt") == "0"
              and a.get("hostException") == "0", f"got {a}")
    h_cash1 = house_cashable()
    check("ESCROW: house debited 2000000 mc at authorizeTransfer",
          h_cash1 == h_cash0 - 2000000, f"got {h_cash0} -> {h_cash1}")
    happy_commit = (
        f'<g2s:commitTransfer g2s:transactionId="{wt_txn}" '
        f'g2s:requestId="{rid1}" g2s:accountId="house" '
        'g2s:watDirection="G2S_toEgm" g2s:payMethod="G2S_payCredit" '
        'g2s:transCashableAmt="2000000" g2s:transPromoAmt="0" '
        f'g2s:transNonCashAmt="0" g2s:transDateTime="{now_iso()}" '
        'g2s:egmException="0"/>')
    post_to_host(avp_wrap(avp_class_command(
        "wat", happy_commit, "506", "511", "G2S_request")))
    m = expect_host_post("commitTransferAck")
    if m:
        check("commitTransferAck echoes transactionId + requestId "
              "(§22.32, sessionId=511)",
              m["command"] == "commitTransferAck"
              and m.get("commandAttrs", {})
              == {"transactionId": wt_txn, "requestId": rid1}
              and m["sessionId"] == "511",
              f"got {m.get('command')}/{m.get('commandAttrs')}"
              f"/{m.get('sessionId')}")
    time.sleep(0.3)
    check("no refund due (full amount transferred): house net -2000000",
          house_cashable() == h_cash1, f"got {house_cashable()}")
    led = get_json(ACCT_URL).get("ledger", [])
    check("ledger: the escrow debit carries the per-record idempotency ref "
          "(wat:<egm>:txn<id>:<createdAt>:escrow — the crash-retry dedupe "
          "key)",
          any(str(e.get("ref", "")).startswith(f"wat:{EGM_ID}:txn{wt_txn}:")
              and str(e.get("ref", "")).endswith(":escrow")
              and e.get("deltaMillicents") == -2000000
              for e in led),
          f"got refs {[e.get('ref') for e in led[-6:]]}")
    with urllib.request.urlopen(STATUS_URL, timeout=5) as resp:
        snap_wc = json.loads(resp.read())
    wat = snap_wc.get(EGM_ID, {}).get("wat", {})
    done = [t for t in wat.get("recentTransfers", [])
            if t.get("transactionId") == wt_txn]
    check("status: transfer committed (recentTransfers) and OUT of "
          "pendingTransfers",
          len(done) == 1 and done[0].get("state") == "committed"
          and not any(t.get("transactionId") == wt_txn
                      for t in wat.get("pendingTransfers", [])),
          f"got {done} / pending {wat.get('pendingTransfers')}")
    check("status: toEgmOkMillicents grew by 2000000",
          wat.get("toEgmOkMillicents") == to_egm_ok0 + 2000000,
          f"got {wat.get('toEgmOkMillicents')} want {to_egm_ok0 + 2000000}")

    print("— Step 6.9793: duplicate commitTransfer re-acked WITHOUT "
          "double-apply (§22.31 retry dedupe)")
    post_to_host(avp_wrap(avp_class_command(
        "wat", happy_commit, "507", "512", "G2S_request")))
    m = expect_host_post("commitTransferAck (retry)")
    if m:
        check("duplicate commitTransfer re-acked (EGM retry, sessionId=512)",
              m["command"] == "commitTransferAck"
              and m.get("commandAttrs", {}).get("transactionId") == wt_txn
              and m["sessionId"] == "512",
              f"got {m.get('command')}/{m.get('commandAttrs')}")
    time.sleep(0.3)
    check("house balance UNCHANGED after the commit retry (no double "
          "refund/debit)", house_cashable() == h_cash1,
          f"got {house_cashable()} want {h_cash1}")
    with urllib.request.urlopen(STATUS_URL, timeout=5) as resp:
        snap_dup = json.loads(resp.read())
    wat = snap_dup.get(EGM_ID, {}).get("wat", {})
    check("status: retry not re-counted (toEgmOkMillicents unchanged, one "
          "ring entry)",
          wat.get("toEgmOkMillicents") == to_egm_ok0 + 2000000
          and len([t for t in wat.get("recentTransfers", [])
                   if t.get("transactionId") == wt_txn]) == 1,
          f"got {wat.get('toEgmOkMillicents')}")

    print("— Step 6.9794: deny path — player funds vanish mid-flight -> "
          "authorize all-zero hostException=3, nothing escrowed")
    cs, cbody = post_command({"action": "addCredits", "accountId": pid,
                              "cashableMillicents": 99000000})
    check("addCredits courtesy pre-check refuses an obvious player "
          "overdraft (ok:false)",
          cs == 200 and cbody.get("ok") is False
          and "insufficient" in str(cbody.get("error", "")), f"got {cbody}")
    cs, cbody = post_command({"action": "addCredits", "accountId": "nobody",
                              "cashableMillicents": 100000})
    check("addCredits for an unknown account refused (ok:false)",
          cs == 200 and cbody.get("ok") is False
          and "unknown" in str(cbody.get("error", "")), f"got {cbody}")
    cs, cbody = post_command({"action": "addCredits"})
    check("addCredits with no amounts refused (ok:false, millicents hint)",
          cs == 200 and cbody.get("ok") is False
          and "MILLICENTS" in str(cbody.get("error", "")), f"got {cbody}")
    cs, cbody = post_command({"action": "addCredits", "accountId": pid,
                              "cashableMillicents": 3000000})
    check("addCredits from the funded player account accepted",
          cs == 200 and bool(cbody.get("ok")), f"got {cbody}")
    rid2 = str(cbody.get("requestId"))
    m = expect_host_post("initiateRequest #2")
    ir2_sid = m["sessionId"] if m else "1001"
    # the account drains BETWEEN the request and the EGM's initiateTransfer —
    # the authoritative escrow check at authorizeTransfer must catch it
    post_accounts({"action": "adjust", "id": pid,
                   "deltaCashableMillicents": -5000000,
                   "note": "drained mid-flight"})
    wt_txn2 = str(wt_base + 1)
    post_to_host(avp_wrap(avp_class_command(
        "wat",
        f'<g2s:requestPending g2s:transactionId="{wt_txn2}" '
        f'g2s:requestId="{rid2}"/>',
        "508", ir2_sid, "G2S_response")))
    post_to_host(avp_wrap(avp_class_command(
        "wat",
        f'<g2s:initiateTransfer g2s:transactionId="{wt_txn2}" '
        f'g2s:requestId="{rid2}" g2s:accountId="{pid}" '
        'g2s:watDirection="G2S_toEgm" g2s:payMethod="G2S_payCredit" '
        'g2s:reqCashableAmt="3000000" g2s:reqPromoAmt="0" '
        'g2s:reqNonCashAmt="0" g2s:reduceAmts="true" g2s:maxAmt="3000000"/>',
        "509", "513", "G2S_request")))
    m = expect_host_post("authorizeTransfer (deny)")
    if m:
        a = m.get("commandAttrs", {})
        check("DENY per §22.30: ALL-ZERO amounts + hostException=3 "
              "(insufficient funds)",
              a.get("authCashableAmt") == "0" and a.get("authPromoAmt") == "0"
              and a.get("authNonCashAmt") == "0"
              and a.get("hostException") == "3"
              and a.get("transactionId") == wt_txn2, f"got {a}")
    acct_now = get_json(ACCT_URL)
    p_bal = next((a for a in acct_now["accounts"] if a.get("id") == pid), {})
    check("nothing escrowed on the deny (player balance stays 0)",
          p_bal.get("cashableMillicents") == 0, f"got {p_bal}")
    # the EGM still closes the transaction out (§22.30: commitTransfer in
    # ALL cases once a transactionId exists)
    post_to_host(avp_wrap(avp_class_command(
        "wat",
        f'<g2s:commitTransfer g2s:transactionId="{wt_txn2}" '
        f'g2s:requestId="{rid2}" g2s:watDirection="G2S_toEgm" '
        'g2s:transCashableAmt="0" g2s:transPromoAmt="0" '
        f'g2s:transNonCashAmt="0" g2s:transDateTime="{now_iso()}" '
        'g2s:egmException="1"/>',
        "510", "514", "G2S_request")))
    m = expect_host_post("commitTransferAck (deny close-out)")
    if m:
        check("deny close-out acked",
              m["command"] == "commitTransferAck"
              and m.get("commandAttrs", {}).get("transactionId") == wt_txn2,
              f"got {m.get('command')}/{m.get('commandAttrs')}")
    time.sleep(0.3)
    with urllib.request.urlopen(STATUS_URL, timeout=5) as resp:
        snap_dn = json.loads(resp.read())
    dn = [t for t in snap_dn.get(EGM_ID, {}).get("wat", {})
          .get("recentTransfers", []) if t.get("transactionId") == wt_txn2]
    check("status: denied transfer terminal (state=denied, hostException=3)",
          len(dn) == 1 and dn[0].get("state") == "denied"
          and dn[0].get("hostException") == "3", f"got {dn}")

    print("— Step 6.9795: reduceAmts + maxAmt reconcile — the EGM caps the "
          "transfer, the host authorizes the intersection")
    h_cash2 = house_cashable()
    cs, cbody = post_command({"action": "addCredits",
                              "cashableMillicents": 1000000,
                              "promoMillicents": 1000000})
    check("addCredits cash+promo accepted", cs == 200 and bool(cbody.get("ok")))
    rid3 = str(cbody.get("requestId"))
    m = expect_host_post("initiateRequest #3")
    ir3_sid = m["sessionId"] if m else "1001"
    if m:
        a = m.get("commandAttrs", {})
        check("initiateRequest carries both credit types (mixCreditTypes)",
              a.get("reqCashableAmt") == "1000000"
              and a.get("reqPromoAmt") == "1000000", f"got {a}")
    wt_txn3 = str(wt_base + 2)
    post_to_host(avp_wrap(avp_class_command(
        "wat",
        f'<g2s:requestPending g2s:transactionId="{wt_txn3}" '
        f'g2s:requestId="{rid3}"/>',
        "511", ir3_sid, "G2S_response")))
    # §22.29 reduceAmts semantics: per-type maxima up to the ask, but the
    # TOTAL is capped by maxAmt — the host must trim to fit (promo first
    # here; cashable is kept whole longest).
    post_to_host(avp_wrap(avp_class_command(
        "wat",
        f'<g2s:initiateTransfer g2s:transactionId="{wt_txn3}" '
        f'g2s:requestId="{rid3}" g2s:accountId="house" '
        'g2s:watDirection="G2S_toEgm" g2s:payMethod="G2S_payCredit" '
        'g2s:reqCashableAmt="1000000" g2s:reqPromoAmt="1000000" '
        'g2s:reqNonCashAmt="0" g2s:reduceAmts="true" g2s:maxAmt="1500000"/>',
        "512", "515", "G2S_request")))
    m = expect_host_post("authorizeTransfer (reduced)")
    if m:
        a = m.get("commandAttrs", {})
        check("authorized within maxAmt: cash=1000000 + promo=500000 "
              "(nonCash trimmed first, then promo — cashable kept whole)",
              a.get("authCashableAmt") == "1000000"
              and a.get("authPromoAmt") == "500000"
              and a.get("authNonCashAmt") == "0"
              and a.get("hostException") == "0", f"got {a}")
    acct_mid = get_json(ACCT_URL)
    h_mid = next(a for a in acct_mid["accounts"] if a.get("id") == "house")
    check("escrow spans credit types: house -1000000 cashable and "
          "-500000 promo",
          int(h_mid["cashableMillicents"]) == h_cash2 - 1000000,
          f"got {h_mid}")
    post_to_host(avp_wrap(avp_class_command(
        "wat",
        f'<g2s:commitTransfer g2s:transactionId="{wt_txn3}" '
        f'g2s:requestId="{rid3}" g2s:accountId="house" '
        'g2s:watDirection="G2S_toEgm" g2s:payMethod="G2S_payCredit" '
        'g2s:transCashableAmt="1000000" g2s:transPromoAmt="500000" '
        f'g2s:transNonCashAmt="0" g2s:transDateTime="{now_iso()}" '
        'g2s:egmException="0"/>',
        "513", "516", "G2S_request")))
    expect_host_post("commitTransferAck (reduced)")
    time.sleep(0.3)
    check("full authorized amount transferred — escrow exactly consumed "
          "(house cashable net -1000000)",
          house_cashable() == h_cash2 - 1000000,
          f"got {house_cashable()} want {h_cash2 - 1000000}")
    with urllib.request.urlopen(STATUS_URL, timeout=5) as resp:
        snap_rd = json.loads(resp.read())
    rd = [t for t in snap_rd.get(EGM_ID, {}).get("wat", {})
          .get("recentTransfers", []) if t.get("transactionId") == wt_txn3]
    check("status: committed entry normalizes the amount trio to the "
          "ACTUALS (cash 1000000 / promo 500000), updatedAt >= createdAt",
          len(rd) == 1 and rd[0].get("cashableMillicents") == 1000000
          and rd[0].get("promoMillicents") == 500000
          and rd[0].get("nonCashMillicents") == 0
          and rd[0].get("updatedAt", "") >= rd[0].get("createdAt", ""),
          f"got {rd}")

    print("— Step 6.9796: EGM abort — commitTransfer all-zero + "
          "egmException=8 refunds the WHOLE escrow")
    h_cash3 = house_cashable()
    cs, cbody = post_command({"action": "addCredits",
                              "cashableMillicents": 1000000})
    rid4 = str(cbody.get("requestId"))
    m = expect_host_post("initiateRequest #4")
    ir4_sid = m["sessionId"] if m else "1001"
    wt_txn4 = str(wt_base + 3)
    post_to_host(avp_wrap(avp_class_command(
        "wat",
        f'<g2s:requestPending g2s:transactionId="{wt_txn4}" '
        f'g2s:requestId="{rid4}"/>',
        "514", ir4_sid, "G2S_response")))
    post_to_host(avp_wrap(avp_class_command(
        "wat",
        f'<g2s:initiateTransfer g2s:transactionId="{wt_txn4}" '
        f'g2s:requestId="{rid4}" g2s:accountId="house" '
        'g2s:watDirection="G2S_toEgm" g2s:payMethod="G2S_payCredit" '
        'g2s:reqCashableAmt="1000000" g2s:reqPromoAmt="0" '
        'g2s:reqNonCashAmt="0" g2s:reduceAmts="true" g2s:maxAmt="1000000"/>',
        "515", "517", "G2S_request")))
    expect_host_post("authorizeTransfer #4")
    check("escrow taken (house -1000000)",
          house_cashable() == h_cash3 - 1000000,
          f"got {house_cashable()} want {h_cash3 - 1000000}")
    post_to_host(avp_wrap(avp_class_command(
        "wat",
        f'<g2s:commitTransfer g2s:transactionId="{wt_txn4}" '
        f'g2s:requestId="{rid4}" g2s:watDirection="G2S_toEgm" '
        'g2s:transCashableAmt="0" g2s:transPromoAmt="0" '
        f'g2s:transNonCashAmt="0" g2s:transDateTime="{now_iso()}" '
        'g2s:egmException="8"/>',
        "516", "518", "G2S_request")))
    m = expect_host_post("commitTransferAck (abort)")
    time.sleep(0.3)
    check("EGM abort refunds the full escrow (house back to its "
          "pre-transfer balance)", house_cashable() == h_cash3,
          f"got {house_cashable()} want {h_cash3}")
    with urllib.request.urlopen(STATUS_URL, timeout=5) as resp:
        snap_ab = json.loads(resp.read())
    ab = [t for t in snap_ab.get(EGM_ID, {}).get("wat", {})
          .get("recentTransfers", []) if t.get("transactionId") == wt_txn4]
    check("status: aborted transfer terminal (state=failed, "
          "egmException=8)",
          len(ab) == 1 and ab[0].get("state") == "failed"
          and ab[0].get("egmException") == "8", f"got {ab}")

    print("— Step 6.9797: EGM-initiated cash-out with NO armed wallet — "
          "reject → ticket, NO House fallback (AJ 2026-07-12): DENY at "
          "authorize, all-zero close-out, House never credited")
    h_cash4 = house_cashable()
    wt_txn5 = str(wt_base + 4)
    post_to_host(avp_wrap(avp_class_command(
        "wat",
        f'<g2s:initiateTransfer g2s:transactionId="{wt_txn5}" '
        'g2s:requestId="0" g2s:idReaderType="G2S_none" g2s:accountId="" '
        'g2s:watDirection="G2S_fromEgm" g2s:reqCashableAmt="4567500" '
        'g2s:reqPromoAmt="0" g2s:reqNonCashAmt="0" g2s:reduceAmts="false" '
        'g2s:maxAmt="4567500"/>',
        "517", "519", "G2S_request")))
    m = expect_host_post("authorizeTransfer (cash-out DENY — no wallet home)")
    if m:
        a = m.get("commandAttrs", {})
        check("blank-account fromEgm cash-out DENIED — no armed wallet, so "
              "NO House fallback: accountId='', hostException=2, all-zero "
              "amounts (reject → the machine tickets; the credits stay on it)",
              a.get("transactionId") == wt_txn5 and a.get("requestId") == "0"
              and a.get("watDirection") == "G2S_fromEgm"
              and a.get("accountId") == ""
              and a.get("authCashableAmt") == "0"
              and a.get("hostException") == "2", f"got {a}")
    check("no account movement at authorize for the denied cash-out",
          house_cashable() == h_cash4,
          f"got {house_cashable()} want {h_cash4}")
    # §22.30: the EGM still closes the transaction out — but ALL-ZERO (the
    # host denied; the credits stay on the machine and fall to a ticket). A
    # denied cash-out NEVER credits the House.
    post_to_host(avp_wrap(avp_class_command(
        "wat",
        f'<g2s:commitTransfer g2s:transactionId="{wt_txn5}" '
        'g2s:requestId="0" g2s:accountId="" '
        'g2s:watDirection="G2S_fromEgm" g2s:transCashableAmt="0" '
        'g2s:transPromoAmt="0" g2s:transNonCashAmt="0" '
        f'g2s:transDateTime="{now_iso()}" g2s:egmException="1"/>',
        "518", "520", "G2S_request")))
    m = expect_host_post("commitTransferAck (cash-out deny close-out)")
    if m:
        check("cash-out deny close-out acked (txn + requestId=0 echoed)",
              m["command"] == "commitTransferAck"
              and m.get("commandAttrs", {})
              == {"transactionId": wt_txn5, "requestId": "0"},
              f"got {m.get('commandAttrs')}")
    time.sleep(0.3)
    check("House NOT credited with the denied cash-out (no House fallback — "
          "reject → ticket)", house_cashable() == h_cash4,
          f"got {house_cashable()} want {h_cash4}")
    with urllib.request.urlopen(STATUS_URL, timeout=5) as resp:
        snap_co = json.loads(resp.read())
    wat = snap_co.get(EGM_ID, {}).get("wat", {})
    check("status: fromEgmOkMillicents UNCHANGED (nothing moved on the deny)",
          wat.get("fromEgmOkMillicents") == from_egm_ok0,
          f"got {wat.get('fromEgmOkMillicents')} want {from_egm_ok0}")

    print("— Step 6.9798: watCancel — cancelRequest before authorize, EGM "
          "closes with all-zero egmException=2 (§22.27)")
    cs, cbody = post_command({"action": "watCancel", "requestId": "999999"})
    check("watCancel for an unknown requestId refused (ok:false)",
          cs == 200 and cbody.get("ok") is False, f"got {cbody}")
    h_cash5 = house_cashable()
    cs, cbody = post_command({"action": "addCredits",
                              "cashableMillicents": 500000})
    rid5 = str(cbody.get("requestId"))
    m = expect_host_post("initiateRequest #5")
    ir5_sid = m["sessionId"] if m else "1001"
    wt_txn6 = str(wt_base + 5)
    post_to_host(avp_wrap(avp_class_command(
        "wat",
        f'<g2s:requestPending g2s:transactionId="{wt_txn6}" '
        f'g2s:requestId="{rid5}"/>',
        "519", ir5_sid, "G2S_response")))
    time.sleep(0.3)
    cs, cbody = post_command({"action": "watCancel", "requestId": rid5})
    check("watCancel accepted for the pending push (ok:true, txn echoed)",
          cs == 200 and cbody.get("ok") is True
          and cbody.get("transactionId") == wt_txn6, f"got {cbody}")
    m = expect_host_post("cancelRequest")
    cr_sid = m["sessionId"] if m else "1001"
    if m:
        check("cancelRequest carries transactionId + requestId (§22.27) and "
              "rides the record's device (1)",
              m["class"] == "wat" and m["command"] == "cancelRequest"
              and m["deviceId"] == "1"
              and m.get("commandAttrs", {})
              == {"transactionId": wt_txn6, "requestId": rid5},
              f"got {m.get('command')}/dev={m.get('deviceId')}/"
              f"{m.get('commandAttrs')}")
    post_to_host(avp_wrap(avp_class_command(
        "wat",
        f'<g2s:cancelRequestAck g2s:transactionId="{wt_txn6}" '
        f'g2s:requestId="{rid5}"/>',
        "520", cr_sid, "G2S_response")))
    post_to_host(avp_wrap(avp_class_command(
        "wat",
        f'<g2s:commitTransfer g2s:transactionId="{wt_txn6}" '
        f'g2s:requestId="{rid5}" g2s:watDirection="G2S_toEgm" '
        'g2s:transCashableAmt="0" g2s:transPromoAmt="0" '
        f'g2s:transNonCashAmt="0" g2s:transDateTime="{now_iso()}" '
        'g2s:egmException="2"/>',
        "521", "521", "G2S_request")))
    expect_host_post("commitTransferAck (cancelled)")
    time.sleep(0.3)
    check("no account movement across the whole cancel (house unchanged — "
          "nothing was ever escrowed)", house_cashable() == h_cash5,
          f"got {house_cashable()} want {h_cash5}")
    with urllib.request.urlopen(STATUS_URL, timeout=5) as resp:
        snap_cx = json.loads(resp.read())
    cx = [t for t in snap_cx.get(EGM_ID, {}).get("wat", {})
          .get("recentTransfers", []) if t.get("transactionId") == wt_txn6]
    check("status: cancelled transfer terminal (state=cancelled, "
          "egmException=2, cancel stamps present)",
          len(cx) == 1 and cx[0].get("state") == "cancelled"
          and cx[0].get("egmException") == "2"
          and bool(cx[0].get("cancelRequestedAt"))
          and bool(cx[0].get("cancelAckedAt")), f"got {cx}")
    cs, cbody = post_command({"action": "watCancel",
                              "transactionId": wt_txn6})
    check("watCancel refused once the transfer is closed (ok:false)",
          cs == 200 and cbody.get("ok") is False, f"got {cbody}")

    print("— Step 6.97981: cancelRequest rides the record's OWN wat device, "
          "blank watCancel targets the newest active push, and a cancel "
          "REJECTION never parks the transfer (it proceeds to commit)")
    h_cash5b = house_cashable()
    cs, cbody = post_command({"action": "addCredits", "deviceId": "2",
                              "cashableMillicents": 250000})
    check("addCredits on wat dev 2 accepted",
          cs == 200 and bool(cbody.get("ok")), f"got {cbody}")
    rid6 = str(cbody.get("requestId"))
    m = expect_host_post("initiateRequest #6 (dev 2)")
    ir6_sid = m["sessionId"] if m else "1001"
    if m:
        check("initiateRequest addressed to the requested device 2",
              m["deviceId"] == "2", f"got dev={m.get('deviceId')}")
    wt_txn7 = str(wt_base + 6)
    post_to_host(avp_wrap(avp_class_command(
        "wat",
        f'<g2s:requestPending g2s:transactionId="{wt_txn7}" '
        f'g2s:requestId="{rid6}"/>',
        "531", ir6_sid, "G2S_response", device_id="2")))
    time.sleep(0.3)
    cs, cbody = post_command({"action": "watCancel"})
    check("BLANK watCancel (no requestId, no transactionId) resolves to "
          "the NEWEST active transfer",
          cs == 200 and cbody.get("ok") is True
          and cbody.get("transactionId") == wt_txn7
          and str(cbody.get("requestId")) == rid6, f"got {cbody}")
    m = expect_host_post("cancelRequest (dev 2)")
    if m:
        check("cancelRequest addressed to the record's device (2), ids "
              "echoed — never the hardcoded dev 1",
              m["deviceId"] == "2"
              and m.get("commandAttrs", {})
              == {"transactionId": wt_txn7, "requestId": rid6},
              f"got dev={m.get('deviceId')}/{m.get('commandAttrs')}")
        # the EGM REFUSES the cancel with a wat class-level error (§1.18.9
        # — errorCode, NO child command). The record must NOT park at
        # error:* (the old newest-active misattribution): the error is the
        # CANCEL's verdict, the transfer itself is still live.
        post_to_host(avp_wrap(
            f'<g2s:g2sMessage xmlns:g2s="{SCHEMA_NS}">\n'
            f'   <g2s:g2sBody g2s:hostId="1" g2s:egmId="{EGM_ID}" '
            f'g2s:dateTimeSent="{now_iso()}">\n'
            f'      <g2s:wat g2s:deviceId="2" g2s:dateTime="{now_iso()}" '
            f'g2s:commandId="532" g2s:sessionType="G2S_response" '
            f'g2s:sessionId="{m["sessionId"]}" g2s:timeToLive="0" '
            f'g2s:errorCode="G2S_WTX009" '
            f'g2s:errorText="Unable to cancel"/>\n'
            f'   </g2s:g2sBody>\n'
            f'</g2s:g2sMessage>'))
    time.sleep(0.3)
    with urllib.request.urlopen(STATUS_URL, timeout=5) as resp:
        snap_cr = json.loads(resp.read())
    crx = [t for t in snap_cr.get(EGM_ID, {}).get("wat", {})
           .get("pendingTransfers", [])
           if t.get("transactionId") == wt_txn7]
    check("cancel rejection stamps cancelRejectedAt and leaves the "
          "transfer LIVE (state pending, not error:*)",
          len(crx) == 1 and crx[0].get("state") == "pending"
          and bool(crx[0].get("cancelRejectedAt")), f"got {crx}")
    # the un-cancelled transfer proceeds: initiateTransfer must draw a
    # REAL authorization with escrow, not a stale-error deny
    post_to_host(avp_wrap(avp_class_command(
        "wat",
        f'<g2s:initiateTransfer g2s:transactionId="{wt_txn7}" '
        f'g2s:requestId="{rid6}" g2s:accountId="house" '
        'g2s:watDirection="G2S_toEgm" g2s:payMethod="G2S_payCredit" '
        'g2s:reqCashableAmt="250000" g2s:reqPromoAmt="0" '
        'g2s:reqNonCashAmt="0" g2s:reduceAmts="true" g2s:maxAmt="250000"/>',
        "533", "531", "G2S_request", device_id="2")))
    m = expect_host_post("authorizeTransfer (after failed cancel)")
    if m:
        a = m.get("commandAttrs", {})
        check("transfer authorized normally after the failed cancel "
              "(escrowed, hostException=0)",
              a.get("authCashableAmt") == "250000"
              and a.get("hostException") == "0", f"got {a}")
    post_to_host(avp_wrap(avp_class_command(
        "wat",
        f'<g2s:commitTransfer g2s:transactionId="{wt_txn7}" '
        f'g2s:requestId="{rid6}" g2s:accountId="house" '
        'g2s:watDirection="G2S_toEgm" g2s:payMethod="G2S_payCredit" '
        'g2s:transCashableAmt="250000" g2s:transPromoAmt="0" '
        f'g2s:transNonCashAmt="0" g2s:transDateTime="{now_iso()}" '
        'g2s:egmException="0"/>',
        "534", "532", "G2S_request", device_id="2")))
    expect_host_post("commitTransferAck (after failed cancel)")
    time.sleep(0.3)
    check("the cancel-rejected transfer moved exactly its amount "
          "(house net -250000)", house_cashable() == h_cash5b - 250000,
          f"got {house_cashable()} want {h_cash5b - 250000}")

    print("— Step 6.97982: silent EGM — a 'requested' push with no "
          "requestPending is cancellable LOCALLY (no wire message "
          "possible without a transactionId)")
    h_cash5c = house_cashable()
    cs, cbody = post_command({"action": "addCredits",
                              "cashableMillicents": 100000})
    check("addCredits accepted (the EGM will stay silent)",
          cs == 200 and bool(cbody.get("ok")), f"got {cbody}")
    rid7 = str(cbody.get("requestId"))
    expect_host_post("initiateRequest #7 (never answered)")
    cs, cbody = post_command({"action": "watCancel", "requestId": rid7})
    check("watCancel cancels LOCALLY (ok:true, local:true, no txn)",
          cs == 200 and cbody.get("ok") is True
          and cbody.get("local") is True
          and cbody.get("transactionId") is None, f"got {cbody}")
    expect_no_host_post("local cancel puts NOTHING on the wire")
    with urllib.request.urlopen(STATUS_URL, timeout=5) as resp:
        snap_lc = json.loads(resp.read())
    wat_lc = snap_lc.get(EGM_ID, {}).get("wat", {})
    lc = [t for t in wat_lc.get("recentTransfers", [])
          if str(t.get("requestId")) == rid7]
    check("status: locally-cancelled record terminal (state=cancelled, "
          "cancelledLocally) and OUT of pendingTransfers",
          len(lc) == 1 and lc[0].get("state") == "cancelled"
          and lc[0].get("cancelledLocally") is True
          and not any(str(t.get("requestId")) == rid7
                      for t in wat_lc.get("pendingTransfers", [])),
          f"got {lc}")
    check("no money moved across the local cancel (nothing was escrowed)",
          house_cashable() == h_cash5c,
          f"got {house_cashable()} want {h_cash5c}")

    print("— Step 6.97983: ghost duplicate initiateTransfer AFTER the "
          "close-out re-draws the stored verdict — no re-escrow, no state "
          "regression (the terminal-state re-escrow hole)")
    h_ghost = house_cashable()
    post_to_host(avp_wrap(avp_class_command(
        "wat",
        f'<g2s:initiateTransfer g2s:transactionId="{wt_txn}" '
        f'g2s:requestId="{rid1}" g2s:accountId="house" '
        'g2s:watDirection="G2S_toEgm" g2s:payMethod="G2S_payCredit" '
        'g2s:reqCashableAmt="2000000" g2s:reqPromoAmt="0" '
        'g2s:reqNonCashAmt="0" g2s:reduceAmts="true" g2s:maxAmt="2000000"/>',
        "535", "533", "G2S_request")))
    m = expect_host_post("authorizeTransfer (ghost duplicate)")
    if m:
        a = m.get("commandAttrs", {})
        check("re-drawn verdict: the SAME auth amounts + hostException=0, "
              "account untouched",
              a.get("transactionId") == wt_txn
              and a.get("authCashableAmt") == "2000000"
              and a.get("hostException") == "0", f"got {a}")
    time.sleep(0.3)
    check("NO re-escrow on the ghost duplicate (house unchanged)",
          house_cashable() == h_ghost,
          f"got {house_cashable()} want {h_ghost}")
    with urllib.request.urlopen(STATUS_URL, timeout=5) as resp:
        snap_gh = json.loads(resp.read())
    wat_gh = snap_gh.get(EGM_ID, {}).get("wat", {})
    gh_rec = [t for t in wat_gh.get("recentTransfers", [])
              if t.get("transactionId") == wt_txn]
    check("record STAYS committed — never resurrected into "
          "pendingTransfers (commitSeen latch intact)",
          bool(gh_rec) and gh_rec[0].get("state") == "committed"
          and not any(t.get("transactionId") == wt_txn
                      for t in wat_gh.get("pendingTransfers", [])),
          f"got {gh_rec}")

    print("— Step 6.97984: transactionId REUSE (EGM RAM clear) — a commit "
          "with NEW facts on an old txn id is APPLIED, never swallowed by "
          "the stale commitSeen latch; its own retry still dedupes")
    h_reuse = house_cashable()
    reuse_commit = (
        f'<g2s:commitTransfer g2s:transactionId="{wt_txn}" '
        'g2s:requestId="0" g2s:accountId="house" '
        'g2s:watDirection="G2S_fromEgm" g2s:transCashableAmt="750000" '
        'g2s:transPromoAmt="0" g2s:transNonCashAmt="0" '
        f'g2s:transDateTime="{now_iso()}" g2s:egmException="0"/>')
    post_to_host(avp_wrap(avp_class_command(
        "wat", reuse_commit, "536", "534", "G2S_request")))
    m = expect_host_post("commitTransferAck (reused txn)")
    if m:
        check("reused-txn commit acked",
              m["command"] == "commitTransferAck"
              and m.get("commandAttrs", {}).get("transactionId") == wt_txn,
              f"got {m.get('command')}/{m.get('commandAttrs')}")
    time.sleep(0.3)
    check("the NEW cash-out is APPLIED (house +750000) — not deduped "
          "against the stale committed record",
          house_cashable() == h_reuse + 750000,
          f"got {house_cashable()} want {h_reuse + 750000}")
    post_to_host(avp_wrap(avp_class_command(
        "wat", reuse_commit, "537", "535", "G2S_request")))
    expect_host_post("commitTransferAck (reused-txn retry)")
    time.sleep(0.3)
    check("the reused transaction's OWN retry still dedupes (house "
          "unchanged — ref-idempotent movement)",
          house_cashable() == h_reuse + 750000,
          f"got {house_cashable()} want {h_reuse + 750000}")

    print("— Step 6.9799: EGM->host wat reads answered PIN-less "
          "(getKeyPair/getWatAccounts/getWatBalance) + unknown wat "
          "command -> APX008")
    post_to_host(avp_wrap(avp_class_command(
        "wat", '<g2s:getKeyPair g2s:keyPairId="7" g2s:hashType="G2S_none"/>',
        "522", "522", "G2S_request")))
    m = expect_host_post("keyPair")
    if m:
        check("keyPair echoes keyPairId=7 with hashType=G2S_none "
              "(PIN-less forever, §22.19)",
              m["command"] == "keyPair"
              and m.get("commandAttrs", {}).get("keyPairId") == "7"
              and m.get("commandAttrs", {}).get("hashType") == "G2S_none",
              f"got {m.get('command')}/{m.get('commandAttrs')}")
    post_to_host(avp_wrap(avp_class_command(
        "wat", '<g2s:getWatAccounts g2s:idReaderType="G2S_none"/>',
        "523", "523", "G2S_request")))
    m = expect_host_post("watAccountList")
    if m:
        # the raw SOAP body escapes attribute quotes (&quot;) — unescape
        # before grepping for the watAccount children
        inner_wal = html.unescape(m["raw"])
        check("watAccountList carries the house account, PIN-less "
              "(authRequired=false, withdrawOk=true)",
              m["command"] == "watAccountList"
              and 'g2s:accountId="house"' in inner_wal
              and 'g2s:authRequired="false"' in inner_wal
              and 'g2s:withdrawOk="true"' in inner_wal,
              inner_wal[:300])
    post_to_host(avp_wrap(avp_class_command(
        "wat", '<g2s:getWatBalance g2s:accountId="house"/>',
        "524", "524", "G2S_request")))
    m = expect_host_post("watBalance")
    if m:
        a = m.get("commandAttrs", {})
        check("watBalance reports the live house balances (cashableAmt "
              "matches /api/accounts)",
              m["command"] == "watBalance" and a.get("accountId") == "house"
              and a.get("cashableAmt") == str(house_cashable())
              and a.get("frozen") == "false", f"got {a}")
    post_to_host(avp_wrap(avp_class_command(
        "wat", '<g2s:getWatBalance g2s:accountId="NOBODY99"/>',
        "525", "525", "G2S_request")))
    m = expect_host_post("watBalance (unknown account)")
    if m:
        check("unknown account draws the wat class error G2S_WTX006",
              "G2S_WTX006" in m["raw"], m["raw"][:300])
    post_to_host(avp_wrap(avp_class_command(
        "wat", '<g2s:getWatBogus/>', "526", "526", "G2S_request")))
    m = expect_host_post("wat unhandled-command error")
    if m:
        check("unknown wat command answers G2S_APX008 (wat IS spoken, "
              "GR-21/§1.18.3.3)",
              "G2S_APX008" in m["raw"] and "G2S_APX007" not in m["raw"],
              m["raw"][:300])

    print("— Step 6.97995: WAT EGM-log reads (getWatLogStatus/getWatLog -> "
          "watLogStatus/watLogList)")
    cs, cbody = post_command({"action": "getWatLogStatus"})
    check("getWatLogStatus command accepted",
          cs == 200 and bool(cbody.get("ok")))
    m = expect_host_post("getWatLogStatus")
    wls_sid = m["sessionId"] if m else "1001"
    post_to_host(avp_wrap(avp_class_command(
        "wat",
        '<g2s:watLogStatus g2s:lastSequence="6" g2s:totalEntries="6"/>',
        "527", wls_sid, "G2S_response")))
    cs, cbody = post_command({"action": "getWatLog"})
    check("getWatLog command accepted", cs == 200 and bool(cbody.get("ok")))
    m = expect_host_post("getWatLog")
    wll_sid = m["sessionId"] if m else "1001"
    if m:
        check("getWatLog pages per §1.14.5 (lastSequence=0 totalEntries=0)",
              m.get("commandAttrs", {})
              == {"lastSequence": "0", "totalEntries": "0"},
              f"got {m.get('commandAttrs')}")
    post_to_host(avp_wrap(avp_class_command(
        "wat",
        '<g2s:watLogList>'
        f'<g2s:watLog g2s:logSequence="6" g2s:deviceId="1" '
        f'g2s:transactionId="{wt_txn}" g2s:requestId="{rid1}" '
        'g2s:watState="G2S_commitAcked" g2s:accountId="ouse" '
        'g2s:watDirection="G2S_toEgm" g2s:payMethod="G2S_payCredit" '
        'g2s:transCashableAmt="2000000" g2s:transPromoAmt="0" '
        f'g2s:transNonCashAmt="0" g2s:transDateTime="{now_iso()}" '
        'g2s:egmException="0"/>'
        '</g2s:watLogList>',
        "528", wll_sid, "G2S_response")))
    time.sleep(0.3)
    with urllib.request.urlopen(STATUS_URL, timeout=5) as resp:
        snap_wl = json.loads(resp.read())
    egm_log = snap_wl.get(EGM_ID, {}).get("wat", {}).get("egmLog", {})
    check("status: wat.egmLog carries the high-water AND the parsed entry "
          "(watState=G2S_commitAcked)",
          egm_log.get("lastSequence") == "6"
          and egm_log.get("totalEntries") == "6"
          and egm_log.get("entryCount") == 1
          and (egm_log.get("entries") or [{}])[0].get("watState")
          == "G2S_commitAcked", f"got {egm_log}")

    print("— Step 6.97996: voucher device picture — getVoucherStatus/"
          "getVoucherProfile fold into /api/status voucherDevice")
    cs, cbody = post_command({"action": "getVoucherStatus"})
    check("getVoucherStatus command accepted",
          cs == 200 and bool(cbody.get("ok")))
    m = expect_host_post("getVoucherStatus")
    vs_sid = m["sessionId"] if m else "1001"
    if m:
        check("getVoucherStatus under voucher class as G2S_request",
              m["class"] == "voucher" and m["command"] == "getVoucherStatus"
              and m["sessionType"] == "G2S_request",
              f"got {m.get('class')}.{m.get('command')}")
    post_to_host(avp_wrap(avp_class_command(
        "voucher",
        '<g2s:voucherStatus g2s:configurationId="0" g2s:egmEnabled="true" '
        'g2s:hostEnabled="true" g2s:hostLocked="false" '
        f'g2s:validationListId="{second_list_id or first_list_id or 1}"/>',
        "529", vs_sid, "G2S_response")))
    cs, cbody = post_command({"action": "getVoucherProfile"})
    check("getVoucherProfile command accepted",
          cs == 200 and bool(cbody.get("ok")))
    m = expect_host_post("getVoucherProfile")
    vp_sid = m["sessionId"] if m else "1001"
    post_to_host(avp_wrap(avp_class_command(
        "voucher",
        '<g2s:voucherProfile g2s:configurationId="0" g2s:maxValIds="20" '
        'g2s:minLevelValIds="15" g2s:voucherHoldTime="15000" '
        'g2s:printOffLine="true" g2s:expireCashPromo="30" '
        'g2s:propName="CasinoNet" g2s:propLine1="Home Game Room" '
        'g2s:propLine2="AJ" g2s:titleCash="CASHOUT VOUCHER"/>',
        "530", vp_sid, "G2S_response")))
    cs, cbody = post_command({"action": "setVoucherState", "enable": False,
                              "disableText": "gate test"})
    check("setVoucherState command accepted",
          cs == 200 and bool(cbody.get("ok")))
    m = expect_host_post("setVoucherState")
    if m:
        check("setVoucherState disable carries enable=false + disableText "
              "(Table 21.9)",
              m["command"] == "setVoucherState"
              and m.get("commandAttrs", {}).get("enable") == "false"
              and m.get("commandAttrs", {}).get("disableText") == "gate test",
              f"got {m.get('commandAttrs')}")
        # restore + confirm via the voucherStatus response to the enable
        cs, cbody = post_command({"action": "setVoucherState",
                                  "enable": True})
        m2 = expect_host_post("setVoucherState (enable)")
        if m2:
            check("setVoucherState restore is a bare enable=true",
                  m2.get("commandAttrs", {}).get("enable") == "true"
                  and "disableText" not in m2.get("commandAttrs", {}),
                  f"got {m2.get('commandAttrs')}")
    time.sleep(0.3)
    with urllib.request.urlopen(STATUS_URL, timeout=5) as resp:
        snap_vd = json.loads(resp.read())
    vd = snap_vd.get(EGM_ID, {}).get("voucherDevice", {})
    check("status: voucherDevice.status folded (hostEnabled/egmEnabled/"
          "validationListId)",
          vd.get("status", {}).get("hostEnabled") == "true"
          and vd.get("status", {}).get("egmEnabled") == "true"
          and vd.get("status", {}).get("validationListId")
          == str(second_list_id or first_list_id or 1),
          f"got {vd.get('status')}")
    check("status: voucherDevice.profile folded (maxValIds/voucherHoldTime/"
          "printOffLine verbatim)",
          vd.get("profile", {}).get("maxValIds") == "20"
          and vd.get("profile", {}).get("voucherHoldTime") == "15000"
          and vd.get("profile", {}).get("printOffLine") == "true",
          f"got {vd.get('profile')}")

    print("— Step 6.97997: cold-boot durability — wat_state.json + "
          "account_state.json hold what a restart reloads")
    wat_path = DATA_DIR / "wat_state.json"
    acct_path = DATA_DIR / "account_state.json"
    wat_st = {}
    if check("wat_state.json persisted", wat_path.exists()):
        wat_st = json.loads(wat_path.read_text())
    check("wat store: requestIdSeq at least this run's last requestId "
          "(non-zero ids never reused across restarts)",
          int(wat_st.get("requestIdSeq") or 0) >= int(rid5),
          f"got {wat_st.get('requestIdSeq')} want >= {rid5}")
    wrecs = {r.get("transactionId"): r for r in wat_st.get("transfers", [])}
    check("wat store: the happy-path transfer persisted terminal "
          "(state=committed, commitSeen latch set — the §22.31 dedupe a "
          "cold boot relies on)",
          wrecs.get(wt_txn, {}).get("state") == "committed"
          and wrecs.get(wt_txn, {}).get("commitSeen") is True,
          f"got {wrecs.get(wt_txn)}")
    check("wat store: deny/abort/cancel outcomes persisted",
          wrecs.get(wt_txn2, {}).get("state") == "denied"
          and wrecs.get(wt_txn4, {}).get("state") == "failed"
          and wrecs.get(wt_txn6, {}).get("state") == "cancelled",
          f"got {wrecs.get(wt_txn2, {}).get('state')}/"
          f"{wrecs.get(wt_txn4, {}).get('state')}/"
          f"{wrecs.get(wt_txn6, {}).get('state')}")
    acct_st = {}
    if check("account_state.json persisted", acct_path.exists()):
        acct_st = json.loads(acct_path.read_text())
    check("account store: persisted house balance matches the live "
          "/api/accounts view (what a cold boot reloads)",
          int(acct_st.get("accounts", {}).get("house", {})
              .get("cashableMillicents", "x") or 0) == house_cashable()
          and "house" in acct_st.get("accounts", {}),
          f"got {acct_st.get('accounts', {}).get('house')}")
    # Bound = AccountStore.KEEP_LEDGER (2000). The dev data dir persists
    # ACROSS gate runs and each run appends ~10 ledger movements, so this
    # must assert the store's REAL ring cap — a tighter number is a time
    # bomb (the old <=500 tripped after enough gate runs, 2026-07-09).
    check("account store: ledger bounded (<= 2000, the KEEP_LEDGER ring) "
          "with WAT escrow refs",
          len(acct_st.get("ledger", [])) <= 2000
          and any(str(e.get("ref", "")).startswith("wat:")
                  for e in acct_st.get("ledger", [])),
          f"got {len(acct_st.get('ledger', []))} entries")
    st_a, body_a = post_accounts({"action": "delete", "id": pid})
    check("per-run player account deleted (gate hygiene, delete path "
          "proven)", st_a == 200 and body_a.get("ok") is True
          and body_a.get("account", {}).get("id") == pid,
          f"got {st_a}/{body_a}")

    print("— Step 6.97998: GET /api/debug/log — host-log tail + the "
          "/api/status _engine block")
    dbg = get_json(DEBUG_LOG_URL + "?lines=50")
    check("debug log payload shape: lines list + logFile + engine",
          isinstance(dbg.get("lines"), list) and "logFile" in dbg
          and isinstance(dbg.get("engine"), dict), f"got {sorted(dbg)}")
    check("engine block matches /api/status _engine verbatim",
          dbg.get("engine") == snap_vd.get("_engine"),
          f"got {dbg.get('engine')} want {snap_vd.get('_engine')}")
    check("log tail present and capped (host started via main() writes "
          "the rotating log)",
          dbg.get("logFile") and 0 < len(dbg["lines"]) <= 50,
          f"got logFile={dbg.get('logFile')} lines={len(dbg.get('lines', []))}")
    dbg1 = get_json(DEBUG_LOG_URL + "?lines=1")
    check("?lines= honored (1 -> exactly 1 line)",
          len(dbg1.get("lines", [])) == 1,
          f"got {len(dbg1.get('lines', []))}")

    print("— Step 6.98: cabinet.setDateTime via /api/command (G2S-20)")
    cs, cbody = post_command({"action": "setDateTime"})
    check("setDateTime command accepted", cs == 200 and cbody.get("ok"))
    m = expect_host_post("setDateTime")
    if m:
        check("setDateTime under cabinet class as G2S_request",
              m["class"] == "cabinet" and m["command"] == "setDateTime"
              and m["sessionType"] == "G2S_request",
              f"got {m.get('class')}.{m.get('command')}/{m.get('sessionType')}")
        check("setDateTime timeToLive=30000 (request, not response)",
              m["timeToLive"] == "30000", f"got {m['timeToLive']}")
        attrs = m.get("commandAttrs", {})
        sdt = attrs.get("cabinetDateTime")
        check("cabinetDateTime is the ONLY attribute (§3.13 Table 3.5)",
              list(attrs) == ["cabinetDateTime"], f"got {attrs}")
        good_form = bool(sdt) and bool(re.fullmatch(
            r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z", sdt or ""))
        check("cabinetDateTime is §1.12 UTC form, exactly-3-digit ms",
              good_form, f"got {sdt}")
        if good_form:
            off = abs((datetime.strptime(sdt, "%Y-%m-%dT%H:%M:%S.%fZ")
                       .replace(tzinfo=timezone.utc)
                       - datetime.now(timezone.utc)).total_seconds())
            check("cabinetDateTime carries the host's CURRENT time",
                  off < 5, f"off by {off:.1f}s")
        time.sleep(0.3)
        with urllib.request.urlopen(STATUS_URL, timeout=5) as resp:
            snap9 = json.loads(resp.read())
        cs9 = snap9.get(EGM_ID, {}).get("clockSync", {})
        check("status: clockSync pending (sent/acked) before the response",
              cs9.get("result") in ("sent", "acked"), f"got {cs9}")
        # The EGM answers with cabinetDateTime — the application response to
        # BOTH setDateTime and getDateTime (§3.15, no setDateTimeAck exists).
        egm_now = now_iso()
        post_to_host(avp_wrap(avp_class_command(
            "cabinet",
            f'<g2s:cabinetDateTime g2s:cabinetDateTime="{egm_now}"/>',
            "290", m["sessionId"], "G2S_response", "0")))
        time.sleep(0.3)
        with urllib.request.urlopen(STATUS_URL, timeout=5) as resp:
            snap9 = json.loads(resp.read())
        egm9 = snap9.get(EGM_ID, {})
        cs9 = egm9.get("clockSync", {})
        check("status: clockSync confirmed by cabinetDateTime (sessionId echo)",
              cs9.get("result") == "confirmed"
              and cs9.get("egmReported") == egm_now, f"got {cs9}")
        check("status: cabinet.egmDateTime records the EGM clock",
              egm9.get("cabinet", {}).get("egmDateTime") == egm_now,
              f"got {egm9.get('cabinet', {}).get('egmDateTime')}")

    print("— Step 6.985: setDateTime rejected — G2S_CBX001 other time source")
    post_command({"action": "setDateTime"})
    m = expect_host_post("setDateTime #2")
    if m:
        # Class-level error response: cabinet element with errorCode and NO
        # child command (§1.18.9) — how an NTP-disciplined EGM answers §3.13.
        err = (
            f'<g2s:g2sMessage xmlns:g2s="{SCHEMA_NS}">\n'
            f'   <g2s:g2sBody g2s:hostId="1" g2s:egmId="{EGM_ID}" '
            f'g2s:dateTimeSent="{now_iso()}">\n'
            f'      <g2s:cabinet g2s:deviceId="1" g2s:dateTime="{now_iso()}" '
            f'g2s:commandId="291" g2s:sessionType="G2S_response" '
            f'g2s:sessionId="{m["sessionId"]}" g2s:timeToLive="0" '
            f'g2s:errorCode="G2S_CBX001" '
            f'g2s:errorText="Command Ignored, Other Time Source In Use"/>\n'
            f'   </g2s:g2sBody>\n'
            f'</g2s:g2sMessage>')
        post_to_host(avp_wrap(err))
        time.sleep(0.3)
        with urllib.request.urlopen(STATUS_URL, timeout=5) as resp:
            snap10 = json.loads(resp.read())
        check("status: clockSync records the CBX001 rejection",
              snap10.get(EGM_ID, {}).get("clockSync", {}).get("result")
              == "error:G2S_CBX001",
              f"got {snap10.get(EGM_ID, {}).get('clockSync')}")

    print("— Step 6.986: cabinet control — setCabinetState disable -> enable "
          "round-trip (rest of G2S-20)")
    # (a) DISABLE. Every Table 3.2 attribute defaults TRUE, so a partial send
    # would silently reset omitted flags — the host must send the FULL
    # intended state: enable=false + the three granular flags explicitly true
    # + the disableText. A getCabinetStatus refresh must ride the FIFO right
    # behind the set (both acked-then-ignored pre-ownership on the real AVP).
    cs, cbody = post_command({"action": "setCabinetState", "enable": False,
                              "disableText": "CasinoNet bench hold",
                              "egmId": EGM_ID})
    check("setCabinetState disable command accepted",
          cs == 200 and cbody.get("ok"))
    m = expect_host_post("setCabinetState (disable)")
    if m:
        check("setCabinetState under cabinet class as G2S_request at "
              "deviceId=1, ttl=30000",
              m["class"] == "cabinet" and m["command"] == "setCabinetState"
              and m["sessionType"] == "G2S_request" and m["deviceId"] == "1"
              and m["timeToLive"] == "30000",
              f"got {m.get('class')}.{m.get('command')}/"
              f"{m.get('sessionType')}/dev={m.get('deviceId')}")
        check("disable sends the FULL Table 3.2 state EXACTLY (enable=false, "
              "granular flags true, disableText)",
              m.get("commandAttrs", {}) == {
                  "enable": "false", "enableGamePlay": "true",
                  "enableMoneyIn": "true", "enableMoneyOut": "true",
                  "disableText": "CasinoNet bench hold"},
              f"got {m.get('commandAttrs')}")
    m_refresh = expect_host_post("getCabinetStatus (post-disable refresh)")
    if m_refresh:
        check("getCabinetStatus refresh rides the FIFO right behind the set",
              m_refresh["command"] == "getCabinetStatus"
              and m_refresh["class"] == "cabinet",
              f"got {m_refresh.get('command')}")
    time.sleep(0.3)
    with urllib.request.urlopen(STATUS_URL, timeout=5) as resp:
        snap_cc = json.loads(resp.read())
    csc = snap_cc.get(EGM_ID, {}).get("cabinetStateChange", {})
    check("status: cabinetStateChange pending (sent/acked, enable=false) "
          "before the response",
          csc.get("result") in ("sent", "acked")
          and csc.get("enable") == "false"
          and csc.get("disableText") == "CasinoNet bench hold",
          f"got {csc}")
    if m:
        # The application response to setCabinetState is cabinetStatus (§3.7)
        # echoing the new control state — sessionId pairing confirms it.
        post_to_host(avp_wrap(avp_class_command(
            "cabinet",
            '<g2s:cabinetStatus g2s:egmState="G2S_hostDisabled" '
            'g2s:hostEnabled="false" g2s:egmEnabled="true" '
            'g2s:enableGamePlay="true" g2s:enableMoneyIn="true" '
            'g2s:enableMoneyOut="true" g2s:hostLocked="false"/>',
            "460", m["sessionId"], "G2S_response")))
        time.sleep(0.3)
        with urllib.request.urlopen(STATUS_URL, timeout=5) as resp:
            snap_cc = json.loads(resp.read())
        egm_cc = snap_cc.get(EGM_ID, {})
        csc = egm_cc.get("cabinetStateChange", {})
        check("status: disable CONFIRMED by the sessionId-matched "
              "cabinetStatus (hostEnabled=false echoed)",
              csc.get("result") == "confirmed"
              and csc.get("hostEnabled") == "false"
              and csc.get("egmState") == "G2S_hostDisabled", f"got {csc}")
        check("status: cabinet folds the disabled state (hostEnabled=false, "
              "egmState=G2S_hostDisabled)",
              egm_cc.get("cabinet", {}).get("hostEnabled") == "false"
              and egm_cc.get("cabinet", {}).get("egmState")
              == "G2S_hostDisabled",
              f"got {egm_cc.get('cabinet', {}).get('hostEnabled')}/"
              f"{egm_cc.get('cabinet', {}).get('egmState')}")
    if m_refresh:
        # Answer the refresh too — a plain read response (different
        # sessionId) folds into cabinet WITHOUT touching the confirmed record.
        post_to_host(avp_wrap(avp_class_command(
            "cabinet",
            '<g2s:cabinetStatus g2s:egmState="G2S_hostDisabled" '
            'g2s:hostEnabled="false" g2s:mainDoorOpen="false"/>',
            "461", m_refresh["sessionId"], "G2S_response")))
        time.sleep(0.3)
        with urllib.request.urlopen(STATUS_URL, timeout=5) as resp:
            snap_cc2 = json.loads(resp.read())
        check("refresh response folds (mainDoorOpen=false) without "
              "disturbing the confirmed record",
              snap_cc2.get(EGM_ID, {}).get("cabinetStateChange", {})
              .get("result") == "confirmed"
              and snap_cc2.get(EGM_ID, {}).get("cabinet", {})
              .get("mainDoorOpen") == "false",
              f"got {snap_cc2.get(EGM_ID, {}).get('cabinetStateChange')}")
    # (b) ENABLE — THE RESTORE PATH IS SACRED. enable omitted from the API
    # body defaults true; no disableText may ride on an enable; and the
    # EGM's response omits hostEnabled (ABSENT = TRUE, Table 3.3) so the
    # fold must still restore a clean enabled picture (no stale false).
    cs, cbody = post_command({"action": "setCabinetState", "egmId": EGM_ID})
    check("setCabinetState enable (default) command accepted",
          cs == 200 and cbody.get("ok"))
    m = expect_host_post("setCabinetState (enable)")
    if m:
        check("enable sends the FULL default-true state EXACTLY "
              "(no disableText on an enable)",
              m.get("commandAttrs", {}) == {
                  "enable": "true", "enableGamePlay": "true",
                  "enableMoneyIn": "true", "enableMoneyOut": "true"},
              f"got {m.get('commandAttrs')}")
    m_refresh = expect_host_post("getCabinetStatus (post-enable refresh)")
    if m_refresh:
        check("enable also chased by a getCabinetStatus refresh",
              m_refresh["command"] == "getCabinetStatus",
              f"got {m_refresh.get('command')}")
    if m:
        post_to_host(avp_wrap(avp_class_command(
            "cabinet",
            '<g2s:cabinetStatus g2s:egmState="G2S_enabled" '
            'g2s:egmEnabled="true"/>',
            "462", m["sessionId"], "G2S_response")))
        time.sleep(0.3)
        with urllib.request.urlopen(STATUS_URL, timeout=5) as resp:
            snap_cc3 = json.loads(resp.read())
        egm_cc3 = snap_cc3.get(EGM_ID, {})
        csc3 = egm_cc3.get("cabinetStateChange", {})
        check("status: enable CONFIRMED via the ABSENT-hostEnabled default",
              csc3.get("result") == "confirmed"
              and csc3.get("enable") == "true"
              and csc3.get("hostEnabled") == "true"
              and csc3.get("egmState") == "G2S_enabled", f"got {csc3}")
        check("status: round-trip RESTORED cleanly — cabinet.hostEnabled "
              "back to true (no stale false), egmState=G2S_enabled",
              egm_cc3.get("cabinet", {}).get("hostEnabled") == "true"
              and egm_cc3.get("cabinet", {}).get("egmState") == "G2S_enabled",
              f"got {egm_cc3.get('cabinet', {}).get('hostEnabled')}/"
              f"{egm_cc3.get('cabinet', {}).get('egmState')}")
    # (c) a REJECTED set — exact sessionId attribution. Leave a setDateTime
    # PENDING first: the cabinet-class error that answers the
    # setCabinetState must flip ONLY the state-change record and never
    # bleed into the pending clock-sync (the pre-cabinet-control code
    # attributed ANY cabinet error to it).
    post_command({"action": "setDateTime", "egmId": EGM_ID})
    m_dt = expect_host_post("setDateTime (left pending)")
    cs, cbody = post_command({"action": "setCabinetState", "enable": False,
                              "egmId": EGM_ID})
    check("setCabinetState disable #2 accepted",
          cs == 200 and cbody.get("ok"))
    m = expect_host_post("setCabinetState (disable #2)")
    m_refresh = expect_host_post("getCabinetStatus (refresh #3)")
    if m:
        check("disable #2 without a disableText sends the flags only",
              m.get("commandAttrs", {}) == {
                  "enable": "false", "enableGamePlay": "true",
                  "enableMoneyIn": "true", "enableMoneyOut": "true"},
              f"got {m.get('commandAttrs')}")
        # Class-level error response: cabinet element with errorCode and NO
        # child command (§1.18.9), sessionId echoing the setCabinetState.
        err = (
            f'<g2s:g2sMessage xmlns:g2s="{SCHEMA_NS}">\n'
            f'   <g2s:g2sBody g2s:hostId="1" g2s:egmId="{EGM_ID}" '
            f'g2s:dateTimeSent="{now_iso()}">\n'
            f'      <g2s:cabinet g2s:deviceId="1" g2s:dateTime="{now_iso()}" '
            f'g2s:commandId="463" g2s:sessionType="G2S_response" '
            f'g2s:sessionId="{m["sessionId"]}" g2s:timeToLive="0" '
            f'g2s:errorCode="G2S_APX002" '
            f'g2s:errorText="Command Not Supported"/>\n'
            f'   </g2s:g2sBody>\n'
            f'</g2s:g2sMessage>')
        post_to_host(avp_wrap(err))
        time.sleep(0.3)
        with urllib.request.urlopen(STATUS_URL, timeout=5) as resp:
            snap_cc4 = json.loads(resp.read())
        egm_cc4 = snap_cc4.get(EGM_ID, {})
        check("status: rejection recorded EXACTLY — state change "
              "error:G2S_APX002, pending clockSync untouched",
              egm_cc4.get("cabinetStateChange", {}).get("result")
              == "error:G2S_APX002"
              and egm_cc4.get("clockSync", {}).get("result")
              in ("sent", "acked"),
              f"got {egm_cc4.get('cabinetStateChange')}"
              f"/{egm_cc4.get('clockSync')}")
    if m_dt:
        # Close the pending clock-sync out cleanly (cabinetDateTime, §3.15)
        # so this step leaves no dangling state for the ones after it.
        post_to_host(avp_wrap(avp_class_command(
            "cabinet",
            f'<g2s:cabinetDateTime g2s:cabinetDateTime="{now_iso()}"/>',
            "464", m_dt["sessionId"], "G2S_response", "0")))
    # ...and the restore path still works after a rejection.
    post_command({"action": "setCabinetState", "enable": True,
                  "egmId": EGM_ID})
    m = expect_host_post("setCabinetState (enable #2)")
    m_refresh = expect_host_post("getCabinetStatus (refresh #4)")
    if m:
        post_to_host(avp_wrap(avp_class_command(
            "cabinet",
            '<g2s:cabinetStatus g2s:egmState="G2S_enabled" '
            'g2s:hostEnabled="true"/>',
            "465", m["sessionId"], "G2S_response")))
        time.sleep(0.3)
        with urllib.request.urlopen(STATUS_URL, timeout=5) as resp:
            snap_cc5 = json.loads(resp.read())
        check("status: restore still confirms after a rejection",
              snap_cc5.get(EGM_ID, {}).get("cabinetStateChange", {})
              .get("result") == "confirmed",
              f"got {snap_cc5.get(EGM_ID, {}).get('cabinetStateChange')}")
    # (d) Test Panel wiring shipped: the guarded disable/enable pair + the
    # state-change chip render from the served page.
    _, _, cab_ui = raw_get("/test")
    check("Test Panel carries the cabinet-control wiring (setCabinetState "
          "pair + cabinetStateChange chip)",
          "setCabinetState" in cab_ui and "cabinetStateChange" in cab_ui,
          "missing setCabinetState/cabinetStateChange in served page")
    # ...and the gamePlay-control wiring (G2S-22): the guarded panel pair
    # plus the per-game card toggles' fire path.
    check("Test Panel carries the gamePlay-control wiring (setGamePlayState "
          "pair + per-game card toggles)",
          "setGamePlayState" in cab_ui and "gpFire" in cab_ui,
          "missing setGamePlayState/gpFire in served page")

    # ======================================================= voucher — G2S-40
    # Device commands, the profile-driven issuance cap, the EGM-log
    # reconcile, the ticket API, the WAT owner-guard, and the Home-UI
    # routing. (NO /api/ticket_qr slices: the UI-rendered QR was cut by
    # owner decision 2026-07-07 — machines print barcoded tickets.)

    print("— Step 6.9796: voucher device reads + printing toggle — folds "
          "into /api/status.voucherDevice (G2S-40)")
    cs, cbody = post_command({"action": "getVoucherStatus"})
    check("getVoucherStatus command accepted",
          cs == 200 and bool(cbody.get("ok")))
    m = expect_host_post("getVoucherStatus (on demand)")
    if m:
        check("getVoucherStatus under voucher class as G2S_request",
              m["class"] == "voucher" and m["command"] == "getVoucherStatus"
              and m["sessionType"] == "G2S_request",
              f"got {m.get('class')}.{m.get('command')}")
        # hostEnabled/egmEnabled ABSENT — the schema default is TRUE and the
        # fold must say so (the repo rule)
        post_to_host(avp_wrap(avp_class_command(
            "voucher",
            '<g2s:voucherStatus g2s:configurationId="7" '
            'g2s:hostLocked="false" g2s:validationListId="3"/>',
            "470", m["sessionId"], "G2S_response")))
    time.sleep(0.3)
    vdev = get_json(STATUS_URL).get(EGM_ID, {}).get("voucherDevice", {})
    check("status: voucherDevice.status folded — absent enable flags read "
          "TRUE, validationListId tracked",
          vdev.get("status", {}).get("hostEnabled") == "true"
          and vdev.get("status", {}).get("egmEnabled") == "true"
          and vdev.get("status", {}).get("validationListId") == "3",
          f"got {vdev.get('status')}")
    # Re-read the profile with the FULL Table 21.12 fixture (maxValIds=40 +
    # timers/expiry/titles). The join probe already primed this shape, but
    # step 6.97996's minimal fixture (maxValIds=20) overwrote it — the wire
    # readback is freshest-wins, and the 40 cap is what step 6.9797 asserts
    # getValidationData honors.
    cs, cbody = post_command({"action": "getVoucherProfile"})
    check("getVoucherProfile command accepted",
          cs == 200 and bool(cbody.get("ok")))
    m = expect_host_post("getVoucherProfile (full readback)")
    if m:
        post_to_host(avp_wrap(avp_class_command(
            "voucher",
            '<g2s:voucherProfile g2s:configurationId="0" '
            'g2s:restartStatus="true" g2s:useDefaultConfig="false" '
            'g2s:requiredForPlay="false" g2s:minLogEntries="35" '
            'g2s:timeToLive="30000" g2s:idReaderId="0" '
            'g2s:combineCashableOut="false" g2s:allowNonCashOut="true" '
            'g2s:maxValIds="40" g2s:minLevelValIds="15" '
            'g2s:valIdListRefresh="43200000" g2s:valIdListLife="86400000" '
            'g2s:voucherHoldTime="15000" g2s:printOffLine="true" '
            'g2s:expireCashPromo="30" g2s:printExpCashPromo="true" '
            'g2s:expireNonCash="30" g2s:printExpNonCash="true" '
            'g2s:propName="CasinoNet" g2s:propLine1="The Game Room" '
            'g2s:propLine2="Home Floor" g2s:titleCash="CASHOUT TICKET" '
            'g2s:titlePromo="PROMO" g2s:titleNonCash="NONCASH" '
            'g2s:titleLargeWin="JACKPOT" g2s:titleBonusCash="BONUS" '
            'g2s:titleBonusPromo="BONUS PROMO" '
            'g2s:titleBonusNonCash="BONUS NC" g2s:titleWatCash="WAT" '
            'g2s:titleWatPromo="WAT PROMO" '
            'g2s:titleWatNonCash="WAT NC"/>',
            "478", m["sessionId"], "G2S_response")))
    time.sleep(0.3)
    vdev = get_json(STATUS_URL).get(EGM_ID, {}).get("voucherDevice", {})
    check("status: voucherDevice.profile carries the FULL Table 21.12 facts "
          "(maxValIds/minLevel/timers/expiry/titles)",
          vdev.get("profile", {}).get("maxValIds") == "40"
          and vdev.get("profile", {}).get("minLevelValIds") == "15"
          and vdev.get("profile", {}).get("valIdListRefresh") == "43200000"
          and vdev.get("profile", {}).get("valIdListLife") == "86400000"
          and vdev.get("profile", {}).get("expireCashPromo") == "30"
          and vdev.get("profile", {}).get("printOffLine") == "true"
          and vdev.get("profile", {}).get("voucherHoldTime") == "15000"
          and vdev.get("profile", {}).get("titleCash") == "CASHOUT TICKET",
          f"got {vdev.get('profile')}")
    cs, cbody = post_command({"action": "setVoucherState", "enable": False,
                              "disableText": "Ticketing paused"})
    check("setVoucherState(disable) accepted",
          cs == 200 and bool(cbody.get("ok")))
    m = expect_host_post("setVoucherState (disable)")
    if m:
        a = m.get("commandAttrs", {})
        check("setVoucherState attrs per Table 21.9 — enable=false + "
              "disableText (only sent on a disable)",
              m["class"] == "voucher" and a.get("enable") == "false"
              and a.get("disableText") == "Ticketing paused", f"got {a}")
        post_to_host(avp_wrap(avp_class_command(
            "voucher",
            '<g2s:voucherStatus g2s:hostEnabled="false" '
            'g2s:validationListId="3"/>',
            "471", m["sessionId"], "G2S_response")))
    time.sleep(0.3)
    vdev = get_json(STATUS_URL).get(EGM_ID, {}).get("voucherDevice", {})
    check("status: host-disable folds (hostEnabled=false)",
          vdev.get("status", {}).get("hostEnabled") == "false",
          f"got {vdev.get('status')}")
    cs, cbody = post_command({"action": "setVoucherState", "enable": True})
    check("setVoucherState(enable) accepted",
          cs == 200 and bool(cbody.get("ok")))
    m = expect_host_post("setVoucherState (enable restore)")
    if m:
        a = m.get("commandAttrs", {})
        check("the restore sends enable=true and NO disableText",
              a.get("enable") == "true" and "disableText" not in a,
              f"got {a}")
        post_to_host(avp_wrap(avp_class_command(
            "voucher",
            '<g2s:voucherStatus g2s:validationListId="3"/>',
            "472", m["sessionId"], "G2S_response")))
    cs, cbody = post_command({"action": "getVoucherLogStatus"})
    check("getVoucherLogStatus command accepted",
          cs == 200 and bool(cbody.get("ok")))
    m = expect_host_post("getVoucherLogStatus")
    if m:
        check("getVoucherLogStatus under voucher class as G2S_request",
              m["class"] == "voucher"
              and m["command"] == "getVoucherLogStatus",
              f"got {m.get('command')}")
        post_to_host(avp_wrap(avp_class_command(
            "voucher",
            '<g2s:voucherLogStatus g2s:lastSequence="6" '
            'g2s:totalEntries="6"/>',
            "473", m["sessionId"], "G2S_response")))
    time.sleep(0.3)
    vdev = get_json(STATUS_URL).get(EGM_ID, {}).get("voucherDevice", {})
    check("status: voucherDevice.logStatus high-water folded",
          vdev.get("logStatus", {}).get("lastSequence") == "6"
          and vdev.get("logStatus", {}).get("totalEntries") == "6",
          f"got {vdev.get('logStatus')}")

    print("— Step 6.9797: getValidationData honors the PROFILE'S maxValIds "
          "(40) — the hardcoded 100 cap is gone (G2S-40)")
    post_to_host(avp_wrap(avp_class_command(
        "voucher",
        '<g2s:getValidationData g2s:configurationId="0" '
        'g2s:validationListId="0" g2s:numValidationIds="120" '
        'g2s:valIdListExpired="true"/>',
        "474", "8801", "G2S_request", ttl="120000")))
    m = expect_host_post("validationData (capped)")
    cap_ids = []
    if m:
        check("validationData under voucher class, sessionId=8801 echoed",
              m["command"] == "validationData" and m["class"] == "voucher"
              and m["sessionId"] == "8801",
              f"got {m.get('command')}/{m.get('sessionId')}")
        cap_items = voucher_id_items(m["raw"])
        cap_ids = [v for v, _ in cap_items]
        check("id count capped at the profile's maxValIds=40 (a 120-id ask; "
              "pre-profile the fallback cap was 100)",
              len(cap_items) == 40, f"got {len(cap_items)}")

    print("— Step 6.9798: voucher log reconcile — the EGM's voucherLogList "
          "MERGES into the durable store (G2S-40)")
    vt = int(time.time() * 1000) + 700000  # per-run-unique txn block
    # graceful degradation if the cap slice failed: the merge checks below
    # then fail on their own assertions instead of crashing the gate
    vid_a, vid_b, vid_c = (cap_ids + ["0" * 18] * 3)[:3]
    # vidZ: an 18-digit id this host NEVER minted, shaped like the §21.26
    # mask (last 4 digits, zero-padded to the fixed 18-char type — a digit
    # pad is indistinguishable from a real id). The merge must treat it as
    # MASKED: ring history only, NEVER a money record (the pre-fix leak
    # forged redeemable tickets into /api/vouchers). Re-rolled in case a
    # pre-fix run's synthesis left it in the store.
    rng = random.Random()
    while True:
        vid_z = "0" * 14 + "".join(rng.choice("0123456789")
                                   for _ in range(4))
        if not get_json(f"{VOUCHERS_URL}?vid={vid_z}").get("total"):
            break
    # live issue for vid_a — the log entry for it must DEDUPE, not re-record
    post_to_host(avp_wrap(avp_class_command(
        "voucher",
        f'<g2s:issueVoucher g2s:transactionId="{vt}" '
        f'g2s:validationId="{vid_a}" g2s:voucherAmt="500000" '
        f'g2s:creditType="G2S_cashable" '
        f'g2s:transferDateTime="{now_iso()}" g2s:egmAction="G2S_issued" '
        f'g2s:voucherSequence="21"/>',
        "475", "8802", "G2S_request", ttl="120000")))
    m = expect_host_post("issueVoucherAck (live issue before the merge)")
    if m:
        check("issueVoucherAck for the live issue",
              m["command"] == "issueVoucherAck"
              and m.get("commandAttrs", {}).get("transactionId") == str(vt),
              f"got {m.get('command')}/{m.get('commandAttrs')}")
    va_before = get_json(f"{VOUCHERS_URL}?vid={vid_a}")
    log_dt = now_iso()

    def vlog(seq, txn, action, vid, amt, state, egm_action,
             transfer="0"):
        return (f'<g2s:voucherLog g2s:logSequence="{seq}" '
                f'g2s:deviceId="1" g2s:transactionId="{txn}" '
                f'g2s:voucherState="{state}" '
                f'g2s:voucherAction="{action}" '
                f'g2s:validationId="{vid}" g2s:voucherAmt="{amt}" '
                f'g2s:creditType="G2S_cashable" '
                f'g2s:transferAmt="{transfer}" '
                f'g2s:transferDateTime="{log_dt}" '
                f'g2s:egmAction="{egm_action}" g2s:egmException="0"/>')
    # DELIBERATELY out of order (5,1,2,...): the merge sorts by
    # logSequence so vid_b's issue lands before its redeem.
    log_list = (
        '<g2s:voucherLogList>'
        + vlog(5, vt + 4, "G2S_issue", "345678", "250000",
               "G2S_issueAcked", "G2S_issued")     # masked id: ring-only
        + vlog(1, vt, "G2S_issue", vid_a, "500000",
               "G2S_issueAcked", "G2S_issued")     # known -> dedupe
        + vlog(2, vt + 1, "G2S_issue", vid_b, "750000",
               "G2S_issueAcked", "G2S_issued")     # unseen: unused->issued
        + vlog(3, vt + 2, "G2S_redeem", vid_b, "750000",
               "G2S_commitAcked", "G2S_redeemed", transfer="750000")
        + vlog(4, vt + 3, "G2S_issue", vid_c, "1250000",
               "G2S_issueAcked", "G2S_issued")     # unseen: stays issued
        + vlog(6, vt + 5, "G2S_issue", vid_z, "2000000",
               "G2S_issueAcked", "G2S_issued")     # digit-padded mask:
        + '</g2s:voucherLogList>')                 # ring-only, no record
    cs, cbody = post_command({"action": "getVoucherLog"})
    check("getVoucherLog command accepted",
          cs == 200 and bool(cbody.get("ok")))
    m = expect_host_post("getVoucherLog")
    if m:
        a = m.get("commandAttrs", {})
        check("getVoucherLog under voucher class — full sweep "
              "lastSequence=0/totalEntries=0 (§1.14.5)",
              m["class"] == "voucher" and m["command"] == "getVoucherLog"
              and a.get("lastSequence") == "0"
              and a.get("totalEntries") == "0", f"got {a}")
        post_to_host(avp_wrap(avp_class_command(
            "voucher", log_list, "476", m["sessionId"], "G2S_response")))
    time.sleep(0.4)
    vdev = get_json(STATUS_URL).get(EGM_ID, {}).get("voucherDevice", {})
    check("status: voucherDevice.log carries the 6 EGM entries",
          vdev.get("log", {}).get("entryCount") == 6,
          f"got {vdev.get('log', {}).get('entryCount')}")
    vb = get_json(f"{VOUCHERS_URL}?vid={vid_b}")
    check("merge: issue+redeem while 'down' -> vid_b REDEEMED at $7.50 "
          "(issue merged before redeem despite the shuffled log order)",
          vb.get("total") == 1
          and vb["vouchers"][0].get("state") == "redeemed"
          and vb["vouchers"][0].get("amountMillicents") == 750000
          and bool(vb["vouchers"][0].get("redeemedAt")),
          f"got {vb}")
    vc = get_json(f"{VOUCHERS_URL}?vid={vid_c}")
    check("merge: issue-only while 'down' -> vid_c ISSUED at $12.50 with "
          "issuedAt + the issue transactionId",
          vc.get("total") == 1
          and vc["vouchers"][0].get("state") == "issued"
          and vc["vouchers"][0].get("amountMillicents") == 1250000
          and bool(vc["vouchers"][0].get("issuedAt"))
          and vc["vouchers"][0].get("transactionId") == str(vt + 3),
          f"got {vc}")
    vz = get_json(f"{VOUCHERS_URL}?vid={vid_z}")
    check("merge: an 18-digit id we never minted is a MASK (§21.26 digit "
          "pad) — NO money record synthesized, invisible to /api/vouchers",
          vz.get("total") == 0, f"got {vz}")
    st_mask = json.loads((DATA_DIR / "voucher_state.json").read_text())
    check("merge: the masked entry still lands in the ring as audit-only "
          "history (source=egmLog), just never in issuedIds",
          any(v.get("validationId") == vid_z
              and v.get("source") == "egmLog"
              for v in st_mask.get("vouchers", []))
          and vid_z not in st_mask.get("issuedIds", {}),
          f"ring miss or issuedIds leak for {vid_z}")
    # and it must not be redeemable: a redeemVoucher against the mask draws
    # the unknown-id rejection (voucherAmt=0, hostException=4, §21.20)
    rz_txn = str(vt + 6)
    post_to_host(avp_wrap(avp_class_command(
        "voucher",
        f'<g2s:redeemVoucher g2s:transactionId="{rz_txn}" '
        f'g2s:validationId="{vid_z}"/>',
        "478", "8803", "G2S_request", ttl="30000")))
    m = expect_host_post("authorizeVoucher (masked id)")
    if m:
        a = m.get("commandAttrs", {})
        check("the masked id is NOT redeemable — voucherAmt=0 "
              "hostException=4 (no forged ticket to pay)",
              m["command"] == "authorizeVoucher"
              and a.get("voucherAmt") == "0"
              and a.get("hostException") == "4", f"got {a}")
    va_after = get_json(f"{VOUCHERS_URL}?vid={vid_a}")
    check("merge: the live-issued vid_a DEDUPED (record unchanged)",
          va_after == va_before,
          f"before={va_before} after={va_after}")
    # idempotence: replay the SAME log — nothing may change
    total_before = get_json(f"{VOUCHERS_URL}?limit=1").get("total")
    cs, cbody = post_command({"action": "getVoucherLog"})
    m = expect_host_post("getVoucherLog (re-merge)")
    if m:
        post_to_host(avp_wrap(avp_class_command(
            "voucher", log_list, "477", m["sessionId"], "G2S_response")))
    time.sleep(0.4)
    check("re-merging the same log is a no-op (idempotent, bounded)",
          get_json(f"{VOUCHERS_URL}?limit=1").get("total") == total_before
          and get_json(f"{VOUCHERS_URL}?vid={vid_b}") == vb,
          f"total {total_before} -> "
          f"{get_json(f'{VOUCHERS_URL}?limit=1').get('total')}")

    print("— Step 6.97985: §21.20 escrow hold vs the log fetch + "
          "live-commit/log dedupe + the close_redemption holder guard "
          "(G2S-40 stage B)")
    merge_state_path = DATA_DIR / "voucher_state.json"
    vid_d = cap_ids[3] if len(cap_ids) > 3 else "0" * 18
    it_d, rt_d = str(vt + 10), str(vt + 11)
    # live issue + redeem for vid_d -> the id is held redeemPending
    post_to_host(avp_wrap(avp_class_command(
        "voucher",
        f'<g2s:issueVoucher g2s:transactionId="{it_d}" '
        f'g2s:validationId="{vid_d}" g2s:voucherAmt="300000" '
        f'g2s:creditType="G2S_cashable" '
        f'g2s:transferDateTime="{now_iso()}" g2s:egmAction="G2S_issued" '
        f'g2s:voucherSequence="22"/>',
        "479", "8804", "G2S_request", ttl="120000")))
    expect_host_post("issueVoucherAck (hold-test ticket)")
    post_to_host(avp_wrap(avp_class_command(
        "voucher",
        f'<g2s:redeemVoucher g2s:transactionId="{rt_d}" '
        f'g2s:validationId="{vid_d}"/>',
        "480", "8805", "G2S_request", ttl="30000")))
    m = expect_host_post("authorizeVoucher (hold-test)")
    if m:
        check("hold-test redemption authorized (id now redeemPending)",
              m["command"] == "authorizeVoucher"
              and m.get("commandAttrs", {}).get("voucherAmt") == "300000",
              f"got {m.get('commandAttrs')}")
    # the EGM's log fetched DURING the escrow window: the in-flight
    # redemption logs egmAction=G2S_pending (Table 21.34) — merging it
    # must NOT drop the §21.20 hold (the pre-fix reset-to-issued would
    # have let the ticket authorize a second time mid-redemption)
    pend_list = (
        '<g2s:voucherLogList>'
        + vlog(7, rt_d, "G2S_redeem", vid_d, "300000",
               "G2S_redeemSent", "G2S_pending")
        + '</g2s:voucherLogList>')
    post_command({"action": "getVoucherLog"})
    m = expect_host_post("getVoucherLog (mid-escrow)")
    if m:
        post_to_host(avp_wrap(avp_class_command(
            "voucher", pend_list, "481", m["sessionId"], "G2S_response")))
    time.sleep(0.4)
    st = json.loads(merge_state_path.read_text())
    d_info = st.get("issuedIds", {}).get(vid_d, {})
    check("a G2S_pending log entry left the redeemPending hold INTACT "
          "(holder txn still recorded — §21.20 survives the log fetch)",
          d_info.get("state") == "redeemPending"
          and d_info.get("pending", {}).get("transactionId") == rt_d,
          f"got {d_info}")
    check("the open entry stayed OUT of the ring (its terminal entry must "
          "still merge on a later fetch)",
          not any(v.get("transactionId") == rt_d
                  for v in st.get("vouchers", [])),
          "an egmAction=G2S_pending entry landed in the ring")
    # a STRAY commit claiming G2S_redeemed from a transaction that does
    # NOT hold the pending marker must not consume the ticket (the
    # close_redemption holder guard — the consuming path needs the marker
    # exactly like the reset path)
    post_to_host(avp_wrap(avp_class_command(
        "voucher",
        f'<g2s:commitVoucher g2s:transactionId="{str(vt + 12)}" '
        f'g2s:validationId="{vid_d}" g2s:voucherAmt="300000" '
        f'g2s:creditType="G2S_cashable" g2s:transferAmt="300000" '
        f'g2s:transferDateTime="{now_iso()}" '
        f'g2s:egmAction="G2S_redeemed" g2s:egmException="0"/>',
        "482", "8806", "G2S_request", ttl="120000")))
    expect_host_post("commitVoucherAck (foreign commit)")
    st = json.loads(merge_state_path.read_text())
    d_info = st.get("issuedIds", {}).get(vid_d, {})
    check("a foreign G2S_redeemed commit (non-holder txn) did NOT consume "
          "the ticket — the live hold survives, holder unchanged",
          d_info.get("state") == "redeemPending"
          and d_info.get("pending", {}).get("transactionId") == rt_d,
          f"got {d_info}")
    # now the REAL holder stacks it: live commitVoucher closes it out...
    post_to_host(avp_wrap(avp_class_command(
        "voucher",
        f'<g2s:commitVoucher g2s:transactionId="{rt_d}" '
        f'g2s:validationId="{vid_d}" g2s:voucherAmt="300000" '
        f'g2s:creditType="G2S_cashable" g2s:transferAmt="300000" '
        f'g2s:transferDateTime="{now_iso()}" '
        f'g2s:egmAction="G2S_redeemed" g2s:egmException="0"/>',
        "483", "8807", "G2S_request", ttl="120000")))
    m = expect_host_post("commitVoucherAck (hold-test)")
    if m:
        check("holder's commit acked and the id consumed",
              m["command"] == "commitVoucherAck"
              and m.get("commandAttrs", {}) == {"transactionId": rt_d},
              f"got {m.get('command')}/{m.get('commandAttrs')}")
    # ...and the SAME redemption's terminal log entry must DEDUPE against
    # the live record: the log keys redeem-side entries as kind='commit',
    # the SAME kind commitVoucher recorded — no duplicate ring record, no
    # inflated merge count, no premature KEEP_VOUCHERS eviction
    done_list = (
        '<g2s:voucherLogList>'
        + vlog(8, rt_d, "G2S_redeem", vid_d, "300000",
               "G2S_commitAcked", "G2S_redeemed", transfer="300000")
        + '</g2s:voucherLogList>')
    post_command({"action": "getVoucherLog"})
    m = expect_host_post("getVoucherLog (post-commit)")
    if m:
        post_to_host(avp_wrap(avp_class_command(
            "voucher", done_list, "484", m["sessionId"], "G2S_response")))
    time.sleep(0.4)
    st = json.loads(merge_state_path.read_text())
    ring_d = [v for v in st.get("vouchers", [])
              if v.get("transactionId") == rt_d
              and v.get("kind") in ("commit", "redeem")]
    check("live-committed redemption DEDUPED on the log fetch — exactly "
          "ONE ring record for the txn, the live 'commit' one (kind key "
          "unified)",
          len(ring_d) == 1 and ring_d[0].get("kind") == "commit"
          and ring_d[0].get("source") != "egmLog",
          f"got {ring_d}")
    vd = get_json(f"{VOUCHERS_URL}?vid={vid_d}")
    check("hold-test ticket ends redeemed once (single $3 record)",
          vd.get("total") == 1
          and vd["vouchers"][0].get("state") == "redeemed"
          and vd["vouchers"][0].get("amountMillicents") == 300000,
          f"got {vd}")

    print("— Step 6.9799: ticket API + Home-UI routing (G2S-40 — NO QR: "
          "cut 2026-07-07, machines print barcoded tickets)")
    page = get_json(f"{VOUCHERS_URL}?limit=5&offset=0")
    contract_keys = {"validationId", "state", "amountMillicents",
                     "creditType", "egmId", "issuedAt", "redeemedAt",
                     "expireAt", "voidedAt", "transactionId"}
    check("/api/vouchers page: 5 rows, total >= the merged set, every row "
          "carries the full contract key set",
          len(page.get("vouchers", [])) == 5 and page.get("total", 0) >= 4
          and all(set(v) == contract_keys for v in page["vouchers"]),
          f"total={page.get('total')} "
          f"keys={[sorted(v) for v in page.get('vouchers', [])][:1]}")
    check("/api/vouchers newest-first: the freshly merged/redeemed tickets "
          "lead (the masked vid_z can never appear — it has no record)",
          page["vouchers"] and page["vouchers"][0]["validationId"]
          in {vid_a, vid_b, vid_c, vid_d}
          and all(v["validationId"] != vid_z for v in page["vouchers"]),
          f"got {page['vouchers'][0]['validationId'] if page.get('vouchers') else None}")
    page2 = get_json(f"{VOUCHERS_URL}?limit=5&offset=5")
    both = get_json(f"{VOUCHERS_URL}?limit=10&offset=0")
    check("/api/vouchers pagination: offset pages don't overlap and "
          "concatenate to the limit=10 page",
          [v["validationId"] for v in page["vouchers"]]
          + [v["validationId"] for v in page2["vouchers"]]
          == [v["validationId"] for v in both["vouchers"]],
          "page0+page1 != limit-10 page")
    reds = get_json(f"{VOUCHERS_URL}?state=redeemed&limit=500")
    check("/api/vouchers?state=redeemed filters (vid_b present, every row "
          "redeemed)",
          all(v["state"] == "redeemed" for v in reds.get("vouchers", []))
          and any(v["validationId"] == vid_b
                  for v in reds.get("vouchers", [])),
          f"total={reds.get('total')}")
    check("/api/vouchers unfiltered listing hides unissued buffer ids "
          "(state=unused is deliberate-only)",
          all(v["state"] != "unused" for v in both.get("vouchers", []))
          and get_json(f"{VOUCHERS_URL}?state=unused&limit=1")
          .get("total", 0) > 0,
          "an unused id leaked into the default listing")
    vm = get_json(f"{VOUCHERS_URL}?vid={vid_c}")
    check("/api/vouchers?vid= exact lookup: total=1 + the one record",
          vm.get("total") == 1 and len(vm.get("vouchers", [])) == 1
          and vm["vouchers"][0]["validationId"] == vid_c, f"got {vm}")
    while True:
        vid_missing = "".join(rng.choice("0123456789") for _ in range(18))
        if vid_missing not in (vid_a, vid_b, vid_c, vid_d, vid_z):
            break
    vm0 = get_json(f"{VOUCHERS_URL}?vid={vid_missing}")
    check("/api/vouchers?vid= unknown id: total=0, empty list",
          vm0.get("total") == 0 and vm0.get("vouchers") == [], f"got {vm0}")
    # The QR endpoint was CUT (owner decision 2026-07-07): machines print
    # barcoded tickets phones scan directly, so a UI-rendered QR is
    # redundant. /api/ticket_qr must stay GONE — a plain 404 like any
    # unknown path.
    qr_st, _, _ = get_raw("http://127.0.0.1:8081/api/ticket_qr?vid="
                          + vid_b)
    check("/api/ticket_qr is GONE (owner cut 2026-07-07) — plain 404",
          qr_st == 404, f"status={qr_st}")
    # Home-UI routing: / prefers webui/home.html and FALLS BACK to the Test
    # Panel while it doesn't exist; /test + /testpanel always serve the
    # panel. Asserted for whichever state this checkout is in (home.html is
    # a parallel deliverable) — the missing-home fallback was also proven
    # from an isolated copy without home.html (/ -> Test Panel, /home 404).
    webui_dir = Path(__file__).resolve().parent.parent / "webui"
    home_file = webui_dir / "home.html"
    idx_body = (webui_dir / "index.html").read_text(errors="replace")
    root_st, root_ct, root_body = raw_get("/")
    tp_st, _, tp_body = raw_get("/test")
    tp2_st, _, tp2_body = raw_get("/testpanel")
    check("/test + /testpanel serve the Test Panel verbatim",
          tp_st == 200 and tp2_st == 200 and tp_body == idx_body
          and tp2_body == idx_body,
          f"status={tp_st}/{tp2_st} match={tp_body == idx_body}")
    if home_file.is_file():
        home_body = home_file.read_text(errors="replace")
        check("/ serves the Home UI when webui/home.html exists",
              root_st == 200 and root_ct.startswith("text/html")
              and root_body == home_body,
              f"status={root_st} match={root_body == home_body}")
        h_st, _, h_body = raw_get("/home")
        check("/home serves home.html verbatim",
              h_st == 200 and h_body == home_body, f"status={h_st}")
        check("Home UI ticket modal shows the raw validationId, no QR "
              "(owner cut 2026-07-07)",
              "/api/ticket_qr" not in home_body and "vidCopy" in home_body,
              "home.html still references /api/ticket_qr or lost the "
              "copyable validation number")
    else:
        check("/ FALLS BACK to the Test Panel (home.html not created yet)",
              root_st == 200 and root_body == idx_body,
              f"status={root_st} match={root_body == idx_body}")
        h_st, _, _ = raw_get("/home")
        check("/home 404s while home.html doesn't exist", h_st == 404,
              f"status={h_st}")
    check("Test Panel topbar links the Home UI + carries the new "
          "voucher/WAT wiring (G2S-40) — and NO QR preview",
          'href="/home"' in idx_body and "getVoucherLog" in idx_body
          and "setVoucherState" in idx_body and "addCredits" in idx_body
          and "tkLookup" in idx_body and "/api/ticket_qr" not in idx_body
          and "gameSweep" in idx_body and "getOptionList" in idx_body,
          "missing home link / voucher / WAT wiring in index.html, or a "
          "leftover /api/ticket_qr reference")

    print("— Step 6.97999: WAT owner-guard — config-sync reveals the OWNED "
          "G2S_wat set; owner actions on a guest wat device refused "
          "(G2S-40 research delta: the live AVP's G2S_wat/3 deviceGuest)")
    # The optionConfig config-sync inventory is the ownership truth: it
    # lists only owned/configurable devices. Declare G2S_wat/1 + /2 (the
    # live AVP's owned pair) — wat/3 (deviceGuest=true, the cabinet's
    # internal SAS 6.02 personality surfacing) never appears there, so
    # owner-type actions addressing it must be refused BEFORE any wire
    # traffic. Guest-safe reads stay unrestricted (§22.14 owner+guest).
    post_to_host(avp_wrap(avp_class_command(
        "optionConfig",
        '<g2s:optionList>'
        '<g2s:deviceOptions g2s:deviceClass="G2S_wat" g2s:deviceId="1"/>'
        '<g2s:deviceOptions g2s:deviceClass="G2S_wat" g2s:deviceId="2"/>'
        '</g2s:optionList>',
        "479", "8899", "G2S_request", ttl="120000")))
    m = expect_host_post("optionListAck (wat config-sync)")
    if m:
        check("wat config-sync answered with optionListAck (sessionId=8899 "
              "echoed)",
              m["command"] == "optionListAck" and m["sessionId"] == "8899",
              f"got {m.get('command')}/{m.get('sessionId')}")
    h_guard = house_cashable()
    cs, cbody = post_command({"action": "addCredits", "deviceId": "3",
                              "cashableMillicents": 1000000})
    check("addCredits to the guest wat device REFUSED — ok:false naming "
          "G2S_wat/3 as a GUEST outside the owned set",
          cs == 200 and cbody.get("ok") is False
          and "G2S_wat/3" in str(cbody.get("error", ""))
          and "GUEST" in str(cbody.get("error", ""))
          and "'1'" in str(cbody.get("error", ""))
          and "'2'" in str(cbody.get("error", "")), f"got {cbody}")
    cs, cbody = post_command({"action": "watDisable", "deviceId": "3"})
    check("watDisable (setWatState) on the guest device REFUSED",
          cs == 200 and cbody.get("ok") is False
          and "G2S_wat/3" in str(cbody.get("error", "")), f"got {cbody}")
    cs, cbody = post_command({"action": "setWatCashOut", "deviceId": "3",
                              "enable": True})
    check("setWatCashOut on the guest device REFUSED",
          cs == 200 and cbody.get("ok") is False
          and "G2S_wat/3" in str(cbody.get("error", "")), f"got {cbody}")
    check("guard refusals moved no money (house balance unchanged — no "
          "request record, no escrow)", house_cashable() == h_guard,
          f"got {house_cashable()} want {h_guard}")
    expect_no_host_post("the three guest-device owner-action refusals")
    cs, cbody = post_command({"action": "watStatus", "deviceId": "3"})
    check("guest-safe READ still allowed — watStatus dev=3 accepted "
          "(owner+guest, §22.14)", cs == 200 and bool(cbody.get("ok")),
          f"got {cbody}")
    m = expect_host_post("getWatStatus (guest dev 3)")
    if m:
        check("getWatStatus rode to G2S_wat/3 on the wire (reads are "
              "unrestricted)",
              m["command"] == "getWatStatus" and m["deviceId"] == "3",
              f"got {m.get('command')}/dev={m.get('deviceId')}")
        # the guest device claims it holds the cash-out — folded into
        # wat.devices['3'], which the auto-release step below must SKIP
        post_to_host(avp_wrap(avp_class_command(
            "wat", '<g2s:watStatus g2s:cashOutToWat="true"/>',
            "538", m["sessionId"], "G2S_response", device_id="3")))
    cs, cbody = post_command({"action": "watEnable", "deviceId": "2"})
    check("owner action on an OWNED device still allowed (watEnable dev=2)",
          cs == 200 and bool(cbody.get("ok")), f"got {cbody}")
    m = expect_host_post("setWatState (owned dev 2)")
    if m:
        check("setWatState enable=true rode to G2S_wat/2",
              m["command"] == "setWatState" and m["deviceId"] == "2"
              and m.get("commandAttrs", {}).get("enable") == "true",
              f"got {m.get('command')}/dev={m.get('deviceId')}"
              f"/{m.get('commandAttrs')}")

    print("— Step 6.979995: setWatCashOut auto-release honors the owner "
          "guard — a GUEST cash-out holder (wat/3) is skipped, never poked")
    time.sleep(0.3)
    # State walking in: dev 1 holds cashOutToWat=true (claimed at 6.9792's
    # prelude), dev 3 claims true (guest fold above). Claiming dev 2 must
    # release ONLY the owned holder — setWatCashOut is owner-only
    # (Table 22.2), so poking wat/3 would draw errors that then
    # misattribute onto live transfers.
    cs, cbody = post_command({"action": "setWatCashOut", "deviceId": "2",
                              "enable": True})
    check("setWatCashOut claim on owned dev 2 accepted",
          cs == 200 and bool(cbody.get("ok")), f"got {cbody}")
    m = expect_host_post("setWatCashOut (release dev 1)")
    if m:
        check("release rides to the OWNED holder dev 1 first",
              m["command"] == "setWatCashOut" and m["deviceId"] == "1"
              and m.get("commandAttrs", {}).get("cashOutToWat") == "false",
              f"got {m.get('command')}/dev={m.get('deviceId')}"
              f"/{m.get('commandAttrs')}")
        post_to_host(avp_wrap(avp_class_command(
            "wat", '<g2s:watStatus g2s:cashOutToWat="false"/>',
            "539", m["sessionId"], "G2S_response", device_id="1")))
    m = expect_host_post("setWatCashOut (claim dev 2)")
    if m:
        check("then the claim to dev 2 — the GUEST holder dev 3 was "
              "SKIPPED (owner-only release, G2S-40)",
              m["command"] == "setWatCashOut" and m["deviceId"] == "2"
              and m.get("commandAttrs", {}).get("cashOutToWat") == "true",
              f"got {m.get('command')}/dev={m.get('deviceId')}"
              f"/{m.get('commandAttrs')}")
        post_to_host(avp_wrap(avp_class_command(
            "wat", '<g2s:watStatus g2s:cashOutToWat="true"/>',
            "540", m["sessionId"], "G2S_response", device_id="2")))
    expect_no_host_post("no release was ever sent to the guest wat/3")

    print("— Step 6.99: meter subscription delivery — periodic notification, "
          "EOD push, get/clear (G2S-16)")
    # Periodic meterInfo arrives as a NOTIFICATION ("fire and forget", §5.4):
    # the host folds the meters and must send NOTHING at application level.
    mi_p = ('<g2s:meterInfo g2s:meterInfoType="G2S_onPeriodic" '
            f'g2s:meterDateTime="{now_iso()}">'
            '<g2s:deviceMeters g2s:deviceClass="G2S_gamePlay" '
            'g2s:deviceId="1">'
            '<g2s:simpleMeter g2s:meterName="G2S_playedCount" '
            'g2s:meterValue="2000"/>'
            '<g2s:simpleMeter g2s:meterName="G2S_wonAmt" '
            'g2s:meterValue="4242"/>'
            '</g2s:deviceMeters></g2s:meterInfo>')
    post_to_host(avp_wrap(avp_class_command(
        "meters", mi_p, "295", "0", "G2S_notification", ttl="0")))
    time.sleep(0.8)
    check("periodic notification draws NO app-level response (§5.4)",
          received.empty(),
          f"host posted {drain_host_posts(2, timeout=0.5)}")
    with urllib.request.urlopen(STATUS_URL, timeout=5) as resp:
        snap13 = json.loads(resp.read())
    egm13 = snap13.get(EGM_ID, {})
    check("periodic meters folded into the shared store",
          egm13.get("meters", {}).get("G2S_gamePlay/1/G2S_playedCount")
          == "2000"
          and egm13.get("meters", {}).get("G2S_gamePlay/1/G2S_wonAmt")
          == "4242",
          f"got {egm13.get('meters')}")
    check("status: lastMeterReport stamped with type G2S_onPeriodic",
          bool(egm13.get("lastMeterReport"))
          and egm13.get("lastMeterReportType") == "G2S_onPeriodic",
          f"got {egm13.get('lastMeterReport')}"
          f"/{egm13.get('lastMeterReportType')}")
    # EOD meterInfo arrives as a REQUEST: the EGM re-collects and resends
    # until the host answers a bare meterInfoAck (§5.4, §5.20).
    mi_e = ('<g2s:meterInfo g2s:meterInfoType="G2S_onEOD" '
            f'g2s:meterDateTime="{now_iso()}">'
            '<g2s:deviceMeters g2s:deviceClass="G2S_cabinet" '
            'g2s:deviceId="1">'
            '<g2s:simpleMeter g2s:meterName="G2S_doorOpenCount" '
            'g2s:meterValue="43"/>'
            '</g2s:deviceMeters></g2s:meterInfo>')
    post_to_host(avp_wrap(avp_class_command(
        "meters", mi_e, "296", "77", "G2S_request", ttl="120000")))
    m = expect_host_post("meterInfoAck")
    if m:
        check("EOD push answered with a BARE meterInfoAck (§5.20 — no "
              "attrs/elements)",
              m["command"] == "meterInfoAck"
              and m.get("commandAttrs", {}) == {},
              f"got {m.get('command')}/{m.get('commandAttrs')}")
        check("meterInfoAck under meters class, sessionId=77 echoed, ttl=0",
              m["class"] == "meters" and m["sessionId"] == "77"
              and m["sessionType"] == "G2S_response"
              and m["timeToLive"] == "0",
              f"got {m['class']}/{m['sessionId']}/{m['timeToLive']}")
    time.sleep(0.3)
    with urllib.request.urlopen(STATUS_URL, timeout=5) as resp:
        snap14 = json.loads(resp.read())
    egm14 = snap14.get(EGM_ID, {})
    check("EOD meters folded + lastMeterReportType=G2S_onEOD",
          egm14.get("meters", {}).get("G2S_cabinet/1/G2S_doorOpenCount")
          == "43"
          and egm14.get("lastMeterReportType") == "G2S_onEOD",
          f"got {egm14.get('lastMeterReportType')}")
    # getMeterSub reads back both standing subs (owner+guest, §5.23)...
    cs, cbody = post_command({"action": "getMeterSub"})
    check("getMeterSub command accepted", cs == 200 and cbody.get("ok"))
    for want in ("G2S_onPeriodic", "G2S_onEOD"):
        m = expect_host_post(f"getMeterSub ({want})")
        if m:
            check(f"getMeterSub meterSubType={want} (required attr, §5.23)",
                  m["command"] == "getMeterSub" and m["class"] == "meters"
                  and m.get("commandAttrs", {}) == {"meterSubType": want},
                  f"got {m.get('command')}/{m.get('commandAttrs')}")
    # ...and clearMeterSub drops them; the EGM answers an EMPTIED meterSubList
    # (no selectors, §5.22/§5.24) which must flip the recorded sub inactive.
    cs, cbody = post_command({"action": "clearMeterSub"})
    check("clearMeterSub command accepted", cs == 200 and cbody.get("ok"))
    for want, egm_cid in (("G2S_onPeriodic", "297"), ("G2S_onEOD", "298")):
        m = expect_host_post(f"clearMeterSub ({want})")
        if m:
            check(f"clearMeterSub meterSubType={want} (§5.22)",
                  m["command"] == "clearMeterSub" and m["class"] == "meters"
                  and m.get("commandAttrs", {}) == {"meterSubType": want},
                  f"got {m.get('command')}/{m.get('commandAttrs')}")
            empty = (f'<g2s:meterSubList g2s:meterSubType="{want}"'
                     + (' g2s:periodicInterval="900000" g2s:periodicBase="0"'
                        if want == "G2S_onPeriodic" else ' g2s:eodBase="0"')
                     + '/>')
            post_to_host(avp_wrap(avp_class_command(
                "meters", empty, egm_cid, m["sessionId"], "G2S_response")))
    time.sleep(0.4)
    with urllib.request.urlopen(STATUS_URL, timeout=5) as resp:
        snap15 = json.loads(resp.read())
    subs15 = snap15.get(EGM_ID, {}).get("meterSubs", {})
    check("status: both subs inactive after clearMeterSub (empty "
          "meterSubList)",
          subs15.get("G2S_onPeriodic", {}).get("active") is False
          and subs15.get("G2S_onEOD", {}).get("active") is False,
          f"got {subs15}")

    # GR-10 warm-restart LOAD path, tested without restarting the host: the
    # ghost below is a brand-NEW association for this process, and the host
    # seeds a new association's option inventory from config_inventory.json
    # at creation time (read lazily, so this file edit lands). Craft a
    # persisted record for the ghost id first — read-modify-write preserving
    # the real EGM's entry, exactly the file a previous host process leaves
    # behind — then let step 6.995's first POST create the association; the
    # seed is asserted after the ghost joins.
    inv_all = {"egms": {}}
    if inv_file.is_file():
        inv_all = json.loads(inv_file.read_text())
    inv_all.setdefault("egms", {})["IGT_00RESTARTGHOST"] = {
        "configDevices": {"G2S_handpay/1": {
            "deviceClass": "G2S_handpay", "deviceId": "1",
            "optionGroups": ["G2S_handpayOptions"],
            "optionItems": ["G2S_handpayOptions"], "valueRows": 1}},
        "configOptions": {
            "G2S_handpay/1/G2S_handpayOptions/G2S_handpayOptions": {
                "deviceClass": "G2S_handpay", "deviceId": "1",
                "optionGroupId": "G2S_handpayOptions",
                "optionId": "G2S_handpayOptions",
                "securityLevel": "G2S_operator",
                "currentValues": [
                    {"kind": "complex", "paramId": "G2S_handpayParams",
                     "children": [
                         {"tag": "booleanValue",
                          "paramId": "G2S_enabledRemoteCredit",
                          "value": "true"}]}],
                "paramIds": ["G2S_enabledRemoteCredit"],
            }},
        "at": now_iso(),
    }
    inv_file.write_text(json.dumps(inv_all))

    print("— Step 6.995: strict pre-join gate — post-host-restart traffic "
          "from an UNKNOWN association draws per-POST G2S_MSX003")
    # Simulate the bench sequence after a HOST restart: the EGM still
    # believes it is onLine and keeps posting commsStatus refreshes for an
    # association this host process has never seen. Spec §1.18.1/§1.18.2:
    # each such POST is refused with a message-level G2S_MSX003 and left
    # UNPROCESSED — only commsOnLine passes the gate (§2.3.3). The retired
    # "restart nudge" (G2S-38 -> GR-12) used to service pre-join commsClosing
    # here; live-proven inert (the AVP re-handshakes on its own internal
    # timer), so the strict gate is now the only behavior. A distinct egmId
    # keeps the main association's joined state untouched.
    ghost_id = "IGT_00RESTARTGHOST"
    ghost_status = ('<g2s:commsStatus g2s:transportState="G2S_transportUp" '
                    'g2s:commsState="G2S_onLine"/>')
    status, body = post_to_host(avp_wrap(avp_command(
        ghost_status, "401", "91", "G2S_response", "0", egm_id=ghost_id),
        egm_id=ghost_id))
    check("HTTP 200", status == 200)
    inner, err = parse_sync_reply(body)
    ack = inner.find(f"{{{SCHEMA_NS}}}g2sAck") if inner is not None else None
    check("unknown-assoc commsStatus refused with message-level G2S_MSX003 "
          "(§1.18.1)",
          ack is not None and attr(ack, "errorCode") == "G2S_MSX003",
          err or f"got {attr(ack, 'errorCode') if ack is not None else None}")
    # The refusal is per-POST (mandated) — a second stale POST draws it again.
    status, body = post_to_host(avp_wrap(avp_command(
        ghost_status, "402", "92", "G2S_response", "0", egm_id=ghost_id),
        egm_id=ghost_id))
    inner, err = parse_sync_reply(body)
    ack = inner.find(f"{{{SCHEMA_NS}}}g2sAck") if inner is not None else None
    check("second stale POST still refused with G2S_MSX003 (per-POST, "
          "§1.18.2)",
          ack is not None and attr(ack, "errorCode") == "G2S_MSX003",
          err or f"got {attr(ack, 'errorCode') if ack is not None else None}")
    # Pre-join commsClosing: refused with MSX003 and left unprocessed — NO
    # host-originated commsClosingAck; the EGM rides out its own 30s closing
    # timeout (§2.3.7). This pins the GR-12 retirement: reintroducing pre-
    # join dispatch would put a commsClosingAck on the wire here.
    status, body = post_to_host(avp_wrap(avp_command(
        '<g2s:commsClosing g2s:reason="host restarted"/>', "403", "93",
        egm_id=ghost_id), egm_id=ghost_id))
    inner, err = parse_sync_reply(body)
    ack = inner.find(f"{{{SCHEMA_NS}}}g2sAck") if inner is not None else None
    check("pre-join commsClosing message-ack is G2S_MSX003 (§1.18.2)",
          ack is not None and attr(ack, "errorCode") == "G2S_MSX003",
          err or f"got {attr(ack, 'errorCode') if ack is not None else None}")
    expect_no_host_post("pre-join commsClosing (strict gate: refused "
                        "unprocessed, no commsClosingAck)")
    # closing -> closed -> opening: the ghost re-handshakes with a fresh
    # commsOnLine, which the MSX003 gate passes straight through to a clean
    # join (commsOnLineAck, host commandId restarting at 1 for the epoch).
    status, body = post_to_host(avp_wrap(avp_command(
        f'<g2s:commsOnLine g2s:egmLocation="http://127.0.0.1:{EGM_PORT}"/>',
        "404", "94", egm_id=ghost_id), egm_id=ghost_id))
    inner, err = parse_sync_reply(body)
    ack = inner.find(f"{{{SCHEMA_NS}}}g2sAck") if inner is not None else None
    check("ghost commsOnLine gets a CLEAN message-level ack",
          status == 200 and ack is not None
          and (attr(ack, "errorCode") or "G2S_none") == "G2S_none",
          err or f"got {attr(ack, 'errorCode') if ack is not None else None}")
    m = expect_host_post("ghost commsOnLineAck")
    if m:
        check("ghost commsOnLineAck: sessionId=94 echoed, fresh-epoch "
              "commandId=1, syncTimer present",
              m["command"] == "commsOnLineAck" and m["g2sEgmId"] == ghost_id
              and m["sessionId"] == "94" and m["commandId"] == "1"
              and "syncTimer" in m.get("commandAttrs", {}),
              f"got {m.get('command')}/{m.get('sessionId')}/"
              f"cid={m.get('commandId')}/{m.get('commandAttrs')}")
    time.sleep(0.3)
    with urllib.request.urlopen(STATUS_URL, timeout=5) as resp:
        snap18 = json.loads(resp.read())
    ghost18 = snap18.get(ghost_id, {})
    # commsOnLineAck was consumed above, so the host has already advanced
    # the ghost past "opening" to sync(expected) — awaiting commsDisabled.
    check("status: ghost re-handshake under way (commsState=sync(expected), "
          "commsOnLineSeen=1)",
          ghost18.get("commsState") == "sync(expected)"
          and ghost18.get("commsOnLineSeen") == 1,
          f"got {ghost18.get('commsState')}/"
          f"{ghost18.get('commsOnLineSeen')}")
    check("GR-10: NEW association seeded from the persisted inventory at "
          "creation (1 device / 1 option, handpay lamp re-derived) — the "
          "warm-restart load path, no host restart needed",
          ghost18.get("configDeviceCount") == 1
          and ghost18.get("configOptions")
          == ["G2S_handpay/1/G2S_handpayOptions/G2S_handpayOptions"]
          and ghost18.get("handpayRemoteCreditAllowed") == "true",
          f"got {ghost18.get('configDeviceCount')}"
          f"/{ghost18.get('configOptions')}"
          f"/{ghost18.get('handpayRemoteCreditAllowed')}")

    print("— Step 6.996: EGM RAM/NVRAM clear — restarted event log + "
          "recycled eventIds must not go dedupe-blind")
    # A RAM clear rebuilds the EGM's event log: logSequence AND eventId are
    # only unique per LOG LIFETIME (§4.1.7) and restart near 1 afterwards.
    # Pre-fix, the backfill high-water (5, from step 6.87) silently discarded
    # every fresh post-clear entry, and a live eventReport whose recycled
    # eventId matched a pre-clear ring entry was acked but dropped from the
    # ring/count. State here: eventCount=4 (ids 7,5,6,8), ehLogLastSequence=5.
    ram_dt = now_iso()
    post_to_host(avp_wrap(avp_class_command(
        "eventHandler",
        '<g2s:eventHandlerLogList>'
        '<g2s:eventHandlerLog g2s:logSequence="1" '
        'g2s:deviceClass="G2S_cabinet" g2s:deviceId="1" '
        'g2s:eventCode="G2S_CBE102" g2s:eventId="1" '
        f'g2s:eventText="RAM Cleared" g2s:eventDateTime="{ram_dt}" '
        'g2s:transactionId="0" g2s:eventAck="false"/>'
        '</g2s:eventHandlerLogList>',
        "410", "95", "G2S_response")))
    time.sleep(0.4)
    with urllib.request.urlopen(STATUS_URL, timeout=5) as resp:
        snap20 = json.loads(resp.read())
    egm20 = snap20.get(EGM_ID, {})
    check("post-clear sweep detects the log restart: high-water RESET "
          "5 -> 1, entry MERGED not discarded (eventCount 4 -> 5)",
          egm20.get("ehLogLastSequence") == 1
          and egm20.get("eventCount") == 5
          and egm20.get("ehBackfilledCount") == 3,
          f"got lastSeq={egm20.get('ehLogLastSequence')}"
          f"/count={egm20.get('eventCount')}"
          f"/backfilled={egm20.get('ehBackfilledCount')}")
    # Live direction: the post-clear EGM reuses eventId=7 (still in the ring
    # from the pre-clear lifetime) for a NEW event. Different record =>
    # different eventDateTime => it must be acked AND counted.
    recycle_dt = now_iso()
    recycled = ('<g2s:eventReport g2s:deviceClass="G2S_cabinet" '
                'g2s:deviceId="1" g2s:eventCode="G2S_CBE101" '
                'g2s:eventId="7" g2s:eventText="Door Opened" '
                f'g2s:eventDateTime="{recycle_dt}" g2s:transactionId="0"/>')
    post_to_host(avp_wrap(avp_class_command(
        "eventHandler", recycled, "411", "96", "G2S_request")))
    ack = expect_host_post("eventAck (recycled eventId=7)")
    if ack:
        check("recycled-id eventReport acked (eventId=7, sessionId=96 "
              "echoed)",
              ack["command"] == "eventAck"
              and ack.get("commandAttrs", {}).get("eventId") == "7"
              and ack["sessionId"] == "96",
              f"got {ack.get('command')}/{ack.get('commandAttrs')}"
              f"/{ack.get('sessionId')}")
    time.sleep(0.3)
    with urllib.request.urlopen(STATUS_URL, timeout=5) as resp:
        snap21 = json.loads(resp.read())
    egm21 = snap21.get(EGM_ID, {})
    ids21 = [e.get("eventId") for e in egm21.get("recentEvents", [])]
    check("recycled eventId COUNTED as a new event (eventCount 5 -> 6; "
          "ring holds eventId=7 twice — pre- and post-clear lifetimes)",
          egm21.get("eventCount") == 6 and ids21.count("7") == 2,
          f"got count={egm21.get('eventCount')} ids={ids21}")
    # ...while a genuine RETRY of the post-clear event (same eventId AND the
    # same stored eventDateTime) still dedupes: re-acked, never re-counted.
    post_to_host(avp_wrap(avp_class_command(
        "eventHandler", recycled, "412", "97", "G2S_request")))
    ack = expect_host_post("eventAck (retry of recycled eventId=7)")
    if ack:
        check("post-clear retry still re-acked",
              ack["command"] == "eventAck"
              and ack.get("commandAttrs", {}).get("eventId") == "7",
              f"got {ack.get('command')}/{ack.get('commandAttrs')}")
    time.sleep(0.3)
    with urllib.request.urlopen(STATUS_URL, timeout=5) as resp:
        snap22 = json.loads(resp.read())
    egm22 = snap22.get(EGM_ID, {})
    check("post-clear retry NOT double-counted (eventCount still 6)",
          egm22.get("eventCount") == 6
          and [e.get("eventId")
               for e in egm22.get("recentEvents", [])].count("7") == 2,
          f"got {egm22.get('eventCount')}")

    print("— Step 6.997: meter subs re-armed across a re-handshake "
          "(subscriptionLost, G2S-16) — and a deliberate clear stays cleared")
    # Re-install the standing subs (operator intent). Step 6.99 cleared them,
    # so this also re-raises the desired flag the per-epoch re-arm is gated
    # on. egmId is explicit: the 6.995 ghost makes this a 2-association host.
    cs, cbody = post_command({"action": "setMeterSub", "egmId": EGM_ID})
    check("setMeterSub command accepted", cs == 200 and cbody.get("ok"))
    for want, egm_cid in (("G2S_onPeriodic", "413"), ("G2S_onEOD", "414")):
        m = expect_host_post(f"setMeterSub ({want})")
        if m:
            sub_list = (f'<g2s:meterSubList g2s:meterSubType="{want}"'
                        + (' g2s:periodicInterval="60000" g2s:periodicBase="0"'
                           if want == "G2S_onPeriodic" else ' g2s:eodBase="0"')
                        + '><g2s:getDeviceMeters g2s:deviceClass="G2S_cabinet" '
                        'g2s:deviceId="1" g2s:meterDefinitions="false"/>'
                        '</g2s:meterSubList>')
            post_to_host(avp_wrap(avp_class_command(
                "meters", sub_list, egm_cid, m["sessionId"], "G2S_response")))
    time.sleep(0.4)
    with urllib.request.urlopen(STATUS_URL, timeout=5) as resp:
        snap23 = json.loads(resp.read())
    egm23 = snap23.get(EGM_ID, {})
    check("subs re-installed (both active) + meterSubsDesired raised",
          egm23.get("meterSubs", {}).get("G2S_onPeriodic", {}).get("active")
          is True
          and egm23.get("meterSubs", {}).get("G2S_onEOD", {}).get("active")
          is True and egm23.get("meterSubsDesired") is True,
          f"got {egm23.get('meterSubs')}/desired="
          f"{egm23.get('meterSubsDesired')}")
    # The EGM reboots: commsOnLine with the wire-proven reset flags (the real
    # AVP raises ALL FOUR on every boot commsOnLine — capture 20250725).
    # Table 2.6: subscriptionLost = OUR standing subscriptions are gone.
    reboot_online = (
        f'<g2s:commsOnLine g2s:egmLocation="http://127.0.0.1:{EGM_PORT}" '
        'g2s:deviceReset="true" g2s:deviceChanged="true" '
        'g2s:subscriptionLost="true" g2s:metersReset="true"/>')
    post_to_host(avp_wrap(avp_command(reboot_online, "420", "80")))
    m = expect_host_post("commsOnLineAck #3")
    if m:
        check("reboot commsOnLineAck (fresh epoch commandId=1, sessionId=80 "
              "echoed)",
              m["command"] == "commsOnLineAck" and m["commandId"] == "1"
              and m["sessionId"] == "80",
              f"got {m.get('command')}/cid={m.get('commandId')}"
              f"/sid={m.get('sessionId')}")
    time.sleep(0.3)
    with urllib.request.urlopen(STATUS_URL, timeout=5) as resp:
        snap24 = json.loads(resp.read())
    subs24 = snap24.get(EGM_ID, {}).get("meterSubs", {})
    check("subscriptionLost marks BOTH recorded subs inactive+lost "
          "immediately (no stale-as-live in /api/status)",
          subs24.get("G2S_onPeriodic", {}).get("active") is False
          and subs24.get("G2S_onPeriodic", {}).get("lost") is True
          and subs24.get("G2S_onEOD", {}).get("active") is False
          and subs24.get("G2S_onEOD", {}).get("lost") is True,
          f"got {subs24}")
    # Complete the re-join choreography.
    post_to_host(avp_wrap(avp_command("<g2s:commsDisabled/>", "421", "81")))
    m = expect_host_post("commsDisabledAck #3")
    if m:
        check("re-join commsDisabledAck (cid=2, sessionId=81)",
              m["command"] == "commsDisabledAck" and m["commandId"] == "2"
              and m["sessionId"] == "81",
              f"got {m.get('command')}/cid={m.get('commandId')}")
    m = expect_host_post("setCommsState #3")
    sid3 = m["sessionId"] if m else "1001"
    post_to_host(avp_wrap(avp_command(
        status_cmd, "422", sid3, "G2S_response", "0")))
    # The fresh epoch's join extras must now END with the setMeterSub re-arm
    # (after the eventHandler trio, FIFO order) — THE fix: pre-fix nothing
    # re-armed the subs, so the periodic/EOD meterInfo feed stayed dead after
    # any EGM reboot until someone manually clicked setMeterSub.
    rejoin_pre = []
    if engine.get("keepaliveMs", 0) > 0:
        rejoin_pre.append("setKeepAlive")
    if engine.get("harvest"):
        rejoin_pre += ["getDescriptor", "getCommHostList"]
    if engine.get("subscribe"):
        rejoin_pre += ["getEventHandlerProfile", "getSupportedEvents",
                       "setEventSub"]
    got_pre = drain_host_posts(len(rejoin_pre))
    check("re-join extras precede the re-arm (FIFO order intact)",
          got_pre == rejoin_pre, f"got {got_pre} want {rejoin_pre}")
    m = expect_host_post("re-armed setMeterSub (onPeriodic)")
    rearm_periodic_sid = None
    if m:
        a = m.get("commandAttrs", {})
        check("re-arm #1 is meters.setMeterSub G2S_onPeriodic as G2S_request "
              f"(cid={4 + len(rejoin_pre)}, ttl=30000, interval=60000)",
              m["class"] == "meters" and m["command"] == "setMeterSub"
              and m["sessionType"] == "G2S_request"
              and m["commandId"] == str(4 + len(rejoin_pre))
              and m["timeToLive"] == "30000"
              and a.get("meterSubType") == "G2S_onPeriodic"
              and a.get("periodicInterval") == "60000",
              f"got {m.get('class')}.{m.get('command')}/cid="
              f"{m.get('commandId')}/{a}")
        sel = meter_sub_selectors(m["raw"])
        check("re-arm keeps the wildcard selector (G2S_all/-1)",
              sel == [("getDeviceMeters", "G2S_all", "-1")], f"got {sel}")
        rearm_periodic_sid = m["sessionId"]
    m = expect_host_post("re-armed setMeterSub (onEOD)")
    if m:
        a = m.get("commandAttrs", {})
        check("re-arm #2 is G2S_onEOD (eodBase=0) with its OWN sessionId",
              a.get("meterSubType") == "G2S_onEOD" and a.get("eodBase") == "0"
              and bool(m["sessionId"])
              and m["sessionId"] != rearm_periodic_sid,
              f"got {a}/sid={m.get('sessionId')}")
        # The EGM accepts; the recorded sub must flip back to live.
        post_to_host(avp_wrap(avp_class_command(
            "meters",
            '<g2s:meterSubList g2s:meterSubType="G2S_onEOD" g2s:eodBase="0">'
            '<g2s:getDeviceMeters g2s:deviceClass="G2S_cabinet" '
            'g2s:deviceId="1" g2s:meterDefinitions="false"/>'
            '</g2s:meterSubList>',
            "423", m["sessionId"], "G2S_response")))
        time.sleep(0.4)
        with urllib.request.urlopen(STATUS_URL, timeout=5) as resp:
            snap25 = json.loads(resp.read())
        eod25 = snap25.get(EGM_ID, {}).get("meterSubs", {}).get(
            "G2S_onEOD", {})
        check("accepted re-arm records the sub active again (lost flag gone)",
              eod25.get("active") is True and "lost" not in eod25,
              f"got {eod25}")
    # The un-gated join reads (2026-07-02) trail the re-arm every epoch, in
    # FIFO order behind the setMeterSub pair — the G2S-39 WAT probes
    # (status+profile x devices 1,2) close the list.
    rejoin_reads = ["getCabinetStatus", "getMeterInfo", "getEventHandlerLog",
                    "getWatStatus", "getWatProfile",
                    "getWatStatus", "getWatProfile",
                    "getVoucherStatus", "getVoucherProfile"]
    got_reads = drain_host_posts(len(rejoin_reads))
    check("un-gated join reads trail the setMeterSub re-arm (FIFO order)",
          got_reads == rejoin_reads, f"got {got_reads} want {rejoin_reads}")
    # A deliberate clear must STICK across the next re-handshake:
    # clearMeterSub withdraws the intent, so the following epoch re-arms
    # NOTHING (the re-arm must never resurrect an operator-cleared sub).
    cs, cbody = post_command({"action": "clearMeterSub", "egmId": EGM_ID})
    check("clearMeterSub command accepted", cs == 200 and cbody.get("ok"))
    cleared = drain_host_posts(2)
    check("both clearMeterSub requests sent",
          cleared == ["clearMeterSub", "clearMeterSub"], f"got {cleared}")
    post_to_host(avp_wrap(avp_command(reboot_online, "430", "82")))
    m = expect_host_post("commsOnLineAck #4")
    if m:
        check("post-clear reboot commsOnLineAck (commandId=1)",
              m["command"] == "commsOnLineAck" and m["commandId"] == "1",
              f"got {m.get('command')}/cid={m.get('commandId')}")
    post_to_host(avp_wrap(avp_command("<g2s:commsDisabled/>", "431", "83")))
    expect_host_post("commsDisabledAck #4")
    m = expect_host_post("setCommsState #4")
    sid4 = m["sessionId"] if m else "1001"
    post_to_host(avp_wrap(avp_command(
        status_cmd, "432", sid4, "G2S_response", "0")))
    got_norearm = drain_host_posts(len(rejoin_pre) + len(rejoin_reads) + 2,
                                   timeout=2)
    check("after a deliberate clearMeterSub the re-join re-arms NOTHING "
          "(extras then the un-gated reads, no setMeterSub anywhere)",
          got_norearm == rejoin_pre + rejoin_reads,
          f"got {got_norearm} want {rejoin_pre + rejoin_reads}")
    time.sleep(0.2)
    with urllib.request.urlopen(STATUS_URL, timeout=5) as resp:
        snap26 = json.loads(resp.read())
    check("status: meterSubsDesired withdrawn (False)",
          snap26.get(EGM_ID, {}).get("meterSubsDesired") is False,
          f"got {snap26.get(EGM_ID, {}).get('meterSubsDesired')}")

    print("— Step 6.998: noteAcceptor bill-in event + acceptance toggle "
          "(G2S-23)")
    # (a) 💵 a $20 bill is stacked: G2S_NAE114 (Tables 13.35/13.36) with
    # the device status + transaction record + meters riding along (Table
    # 13.12; our subscription asks for all three). The host must eventAck,
    # fold the noteAcceptor currency meters, fold the ride-along status,
    # and type a bill-in entry (denom + amount) into the notes ring plus
    # the running session total.
    # (Machine-take tracking was REMOVED 2026-07-15 — collector de-cage;
    # this step now only exercises the meter fold + notes ring.)
    bill_dt = now_iso()
    bill_ev = (
        '<g2s:eventReport g2s:deviceClass="G2S_noteAcceptor" '
        'g2s:deviceId="1" g2s:eventCode="G2S_NAE114" g2s:eventId="40" '
        f'g2s:eventDateTime="{bill_dt}" g2s:transactionId="888001">'
        '<g2s:deviceList><g2s:statusInfo>'
        '<g2s:noteAcceptorStatus g2s:egmEnabled="true" '
        'g2s:noteValueInEscrow="0"/>'
        '</g2s:statusInfo></g2s:deviceList>'
        '<g2s:transactionList><g2s:transactionInfo>'
        '<g2s:noteAcceptorLog g2s:logSequence="9" g2s:deviceId="1" '
        'g2s:transactionId="888001" g2s:currencyId="USD" '
        'g2s:denomId="2000000" g2s:baseCashableAmt="2000000" '
        f'g2s:noteDateTime="{bill_dt}"/>'
        '</g2s:transactionInfo></g2s:transactionList>'
        '<g2s:meterList><g2s:meterInfo>'
        '<g2s:deviceMeters g2s:deviceClass="G2S_noteAcceptor" '
        'g2s:deviceId="1">'
        '<g2s:simpleMeter g2s:meterName="G2S_currencyInAmt" '
        'g2s:meterValue="2000000"/>'
        '<g2s:simpleMeter g2s:meterName="G2S_currencyInCnt" '
        'g2s:meterValue="1"/>'
        '</g2s:deviceMeters></g2s:meterInfo></g2s:meterList>'
        '</g2s:eventReport>')
    post_to_host(avp_wrap(avp_class_command(
        "eventHandler", bill_ev, "440", "98", "G2S_request")))
    ack = expect_host_post("eventAck (NAE114)")
    if ack:
        check("bill-in eventReport acked (eventId=40, sessionId=98 echoed)",
              ack["command"] == "eventAck"
              and ack.get("commandAttrs", {}).get("eventId") == "40"
              and ack["sessionId"] == "98",
              f"got {ack.get('command')}/{ack.get('commandAttrs')}"
              f"/{ack.get('sessionId')}")
    time.sleep(0.3)
    with urllib.request.urlopen(STATUS_URL, timeout=5) as resp:
        snap_bi = json.loads(resp.read())
    egm_bi = snap_bi.get(EGM_ID, {})
    bis = egm_bi.get("recentBillIns", [])
    check("status: typed bill-in — denom $20 (from denomId 2000000), "
          "currency/txn from the riding noteAcceptorLog",
          len(bis) == 1 and bis[0].get("denom") == "$20"
          and bis[0].get("denomId") == "2000000"
          and bis[0].get("baseCashableAmt") == "2000000"
          and bis[0].get("currencyId") == "USD"
          and bis[0].get("transactionId") == "888001", f"got {bis}")
    check("status: billInCount=1, session total = $20 (2000000 millicents)",
          egm_bi.get("billInCount") == 1
          and egm_bi.get("billInSessionAmt") == 2000000,
          f"got {egm_bi.get('billInCount')}"
          f"/{egm_bi.get('billInSessionAmt')}")
    check("status: NAE114 ride-alongs folded — currency meters into the "
          "shared store + status (statusSource=event)",
          egm_bi.get("meters", {})
          .get("G2S_noteAcceptor/1/G2S_currencyInAmt") == "2000000"
          and egm_bi.get("meters", {})
          .get("G2S_noteAcceptor/1/G2S_currencyInCnt") == "1"
          and egm_bi.get("noteAcceptor", {}).get("status", {})
          .get("statusSource") == "event",
          f"got {egm_bi.get('noteAcceptor', {}).get('status')}")
    bi_evs = [e for e in egm_bi.get("recentEvents", [])
              if e.get("eventId") == "40"]
    check("status: the event itself rings with the 💵 label under "
          "noteAcceptor",
          len(bi_evs) == 1
          and "bill" in (bi_evs[0].get("label") or "").lower()
          and bi_evs[0].get("category") == "noteAcceptor",
          f"got {bi_evs}")
    # The EGM retries the persisted NAE114 (same eventId + eventDateTime,
    # §4.1.4): re-ack, but NEVER double-count the money.
    post_to_host(avp_wrap(avp_class_command(
        "eventHandler", bill_ev, "441", "99", "G2S_request")))
    ack = expect_host_post("eventAck (NAE114 retry)")
    if ack:
        check("bill-in retry still eventAck'd",
              ack["command"] == "eventAck"
              and ack.get("commandAttrs", {}).get("eventId") == "40",
              f"got {ack.get('command')}/{ack.get('commandAttrs')}")
    time.sleep(0.3)
    with urllib.request.urlopen(STATUS_URL, timeout=5) as resp:
        snap_bi2 = json.loads(resp.read())
    egm_bi2 = snap_bi2.get(EGM_ID, {})
    check("bill-in retry NOT double-counted (billInCount still 1, session "
          "total still $20)",
          egm_bi2.get("billInCount") == 1
          and egm_bi2.get("billInSessionAmt") == 2000000
          and len(egm_bi2.get("recentBillIns", [])) == 1,
          f"got {egm_bi2.get('billInCount')}"
          f"/{egm_bi2.get('billInSessionAmt')}")
    # (b) the acceptance toggle: /api/command setNoteAcceptorState. Disable
    # first — enable=false + disableText must go out EXACTLY (Table 13.2);
    # deviceId resolves to the config-sync-revealed acceptor (device 1).
    # egmId is explicit: the 6.995 ghost makes this a 2-association host.
    cs, cbody = post_command({"action": "setNoteAcceptorState",
                              "enable": False,
                              "disableText": "CasinoNet bench hold",
                              "egmId": EGM_ID})
    check("setNoteAcceptorState disable command accepted",
          cs == 200 and cbody.get("ok"))
    m = expect_host_post("setNoteAcceptorState (disable)")
    if m:
        check("setNoteAcceptorState under noteAcceptor class as G2S_request "
              "at deviceId=1, ttl=30000",
              m["class"] == "noteAcceptor"
              and m["command"] == "setNoteAcceptorState"
              and m["sessionType"] == "G2S_request" and m["deviceId"] == "1"
              and m["timeToLive"] == "30000",
              f"got {m.get('class')}.{m.get('command')}/"
              f"{m.get('sessionType')}/dev={m.get('deviceId')}")
        check("disable carries enable=false + disableText EXACTLY "
              "(Table 13.2)",
              m.get("commandAttrs", {}) == {
                  "enable": "false",
                  "disableText": "CasinoNet bench hold"},
              f"got {m.get('commandAttrs')}")
        # Point-to-point setNoteAcceptorState MUST get a noteAcceptorStatus
        # back (§13.8); the EGM reports hostEnabled=false (NAE003 fired).
        post_to_host(avp_wrap(avp_class_command(
            "noteAcceptor",
            '<g2s:noteAcceptorStatus g2s:hostEnabled="false" '
            'g2s:egmEnabled="true"/>',
            "442", m["sessionId"], "G2S_response")))
        time.sleep(0.3)
        with urllib.request.urlopen(STATUS_URL, timeout=5) as resp:
            snap_tg = json.loads(resp.read())
        tg_st = snap_tg.get(EGM_ID, {}).get("noteAcceptor", {}) \
            .get("status", {})
        check("status: acceptor now hostEnabled=false (disable confirmed "
              "by the response)",
              tg_st.get("hostEnabled") == "false"
              and tg_st.get("egmEnabled") == "true", f"got {tg_st}")
    # Re-enable: enable OMITTED from the body must default TRUE (Table
    # 13.2) and NO disableText may ride on an enable.
    cs, cbody = post_command({"action": "setNoteAcceptorState",
                              "egmId": EGM_ID})
    check("setNoteAcceptorState enable (default) command accepted",
          cs == 200 and cbody.get("ok"))
    m = expect_host_post("setNoteAcceptorState (enable)")
    if m:
        check("enable carries enable=true as its ONLY attribute "
              "(no disableText on an enable)",
              m.get("commandAttrs", {}) == {"enable": "true"},
              f"got {m.get('commandAttrs')}")
        # EGM confirms with hostEnabled ABSENT — the default-true rule again.
        post_to_host(avp_wrap(avp_class_command(
            "noteAcceptor",
            '<g2s:noteAcceptorStatus g2s:egmEnabled="true"/>',
            "443", m["sessionId"], "G2S_response")))
        time.sleep(0.3)
        with urllib.request.urlopen(STATUS_URL, timeout=5) as resp:
            snap_tg2 = json.loads(resp.read())
        tg2_st = snap_tg2.get(EGM_ID, {}).get("noteAcceptor", {}) \
            .get("status", {})
        check("status: acceptor back to hostEnabled=true (absent = true)",
              tg2_st.get("hostEnabled") == "true", f"got {tg2_st}")
    # (c) Test Panel wiring shipped: the served page drives the toggle and
    # renders the bill tape + note table.
    _, _, na_ui = raw_get("/test")
    check("Test Panel carries the note-acceptor wiring "
          "(setNoteAcceptorState toggle + recentBillIns rendering)",
          "setNoteAcceptorState" in na_ui and "recentBillIns" in na_ui
          and "getNoteAcceptorStatus" in na_ui,
          "missing setNoteAcceptorState/recentBillIns in served page")

    print("— Step 6.999: optionConfig change cycle — full apply (G2S-27)")
    # The strategic use: the AVP ships handpay remote-key-off-to-credits
    # DISABLED; the host flips it ON via the setOptionChange -> authorize ->
    # apply choreography. First a config-sync reveals the option's COMPLETE
    # current values (remoteCredit=false). egmId is explicit: the 6.995 ghost
    # makes this a 2-association host. The fixture mirrors the LIVE 2026-07-02
    # inventory shape (a representative 10 of the real 19 booleans, all under
    # ONE complexValue G2S_handpayParams, securityLevel=G2S_administrator —
    # the AVP deviates from the spec's G2S_operator here).
    handpay_fixture_params = {
        "G2S_mixCreditTypes": "false",
        "G2S_requestNonCash": "false",
        "G2S_combineCashableOut": "true",
        "G2S_enabledLocalHandpay": "true",
        "G2S_enabledLocalCredit": "true",
        "G2S_enabledRemoteHandpay": "false",
        "G2S_enabledRemoteCredit": "false",
        "G2S_enabledRemoteVoucher": "false",
        "G2S_disabledRemoteHandpay": "false",
        "G2S_disabledRemoteCredit": "false",
    }

    def handpay_option_list(**overrides):
        vals = dict(handpay_fixture_params, **overrides)
        inner = "".join(
            f'<g2s:booleanValue g2s:paramId="{p}">{v}</g2s:booleanValue>'
            for p, v in vals.items())
        return (
            '<g2s:optionList>'
            '<g2s:deviceOptions g2s:deviceClass="G2S_handpay" g2s:deviceId="1">'
            '<g2s:optionGroup g2s:optionGroupId="G2S_handpayOptions" '
            'g2s:optionGroupName="Handpay Options">'
            '<g2s:optionItem g2s:optionId="G2S_handpayOptions" '
            'g2s:securityLevel="G2S_administrator">'
            '<g2s:optionCurrentValues>'
            '<g2s:complexValue g2s:paramId="G2S_handpayParams">'
            f'{inner}'
            '</g2s:complexValue>'
            '</g2s:optionCurrentValues>'
            '</g2s:optionItem>'
            '</g2s:optionGroup>'
            '</g2s:deviceOptions>'
            '</g2s:optionList>')
    oc_sync_false = handpay_option_list()
    post_to_host(avp_wrap(avp_class_command(
        "optionConfig", oc_sync_false, "469", "249", "G2S_request",
        ttl="120000")))
    expect_host_post("optionListAck (handpay remoteCredit=false)")
    time.sleep(0.3)
    with urllib.request.urlopen(STATUS_URL, timeout=5) as resp:
        snap_o0 = json.loads(resp.read())
    egm_o0 = snap_o0.get(EGM_ID, {})
    OKEY = "G2S_handpay/1/G2S_handpayOptions/G2S_handpayOptions"
    check("status: config-sync option inventory gained the handpay option "
          "4-tuple",
          OKEY in egm_o0.get("configOptions", []),
          f"got {egm_o0.get('configOptions')}")
    check("status: handpayRemoteCreditAllowed is 'false' pre-change (the "
          "disabled reset-to-credits path)",
          egm_o0.get("handpayRemoteCreditAllowed") == "false",
          f"got {egm_o0.get('handpayRemoteCreditAllowed')}")
    # (a) fire the change: flip G2S_enabledRemoteCredit -> true.
    cs, cbody = post_command({
        "action": "setOption", "deviceClass": "G2S_handpay", "deviceId": "1",
        "optionId": "G2S_handpayOptions", "paramId": "G2S_enabledRemoteCredit",
        "value": "true", "egmId": EGM_ID})
    check("setOption accepted + echoes the exact edit (old false -> new true)",
          cs == 200 and cbody.get("ok") is True
          and cbody.get("oldValue") == "false"
          and cbody.get("newValue") == "true"
          and cbody.get("paramId") == "G2S_enabledRemoteCredit"
          and cbody.get("configId"), f"got {cbody}")
    config_id = str(cbody.get("configId"))
    m = expect_host_post("setOptionChange")
    set_sid = None
    if m:
        det = option_change_details(m["raw"])
        set_sid = m["sessionId"]
        check("setOptionChange under optionConfig class as G2S_request at "
              "deviceId=1 (host's own hostId, §9.1), ttl=30000",
              m["class"] == "optionConfig"
              and m["command"] == "setOptionChange"
              and m["sessionType"] == "G2S_request" and m["deviceId"] == "1"
              and m["timeToLive"] == "30000",
              f"got {m.get('class')}.{m.get('command')}/"
              f"{m.get('sessionType')}/dev={m.get('deviceId')}")
        check("setOptionChange carries configurationId + applyCondition="
              "G2S_immediate + restartAfter=false, NO disableCondition "
              "(immediate)",
              det and det["attrs"].get("configurationId") == config_id
              and det["attrs"].get("applyCondition") == "G2S_immediate"
              and det["attrs"].get("restartAfter") == "false"
              and "disableCondition" not in det["attrs"],
              f"got {det['attrs'] if det else None}")
        check("the <option> addresses the exact 4-tuple "
              "(deviceClass/deviceId/optionGroupId/optionId)",
              det and det["option"] == {
                  "deviceClass": "G2S_handpay", "deviceId": "1",
                  "optionGroupId": "G2S_handpayOptions",
                  "optionId": "G2S_handpayOptions"},
              f"got {det['option'] if det else None}")
        check("the COMPLETE current-value set is sent with ONLY remoteCredit "
              "flipped (§9.15 — no single-param deltas)",
              det and det["values"] == dict(handpay_fixture_params,
                                            G2S_enabledRemoteCredit="true"),
              f"got {det['values'] if det else None}")
        check("authorizeList names THIS host (the initiator MUST include "
              "itself, p.362)",
              det and det["authorizeHosts"] == ["1"],
              f"got {det['authorizeHosts'] if det else None}")
    time.sleep(0.3)
    with urllib.request.urlopen(STATUS_URL, timeout=5) as resp:
        snap_o1 = json.loads(resp.read())
    egm_o1 = snap_o1.get(EGM_ID, {})
    check("status: stage=setting, change record in flight (result sent/acked)",
          egm_o1.get("optionConfigStage") == "setting"
          and egm_o1.get("optionChange", {}).get("result") in ("sent", "acked")
          and egm_o1.get("optionChange", {}).get("optionId")
          == "G2S_handpayOptions",
          f"got stage={egm_o1.get('optionConfigStage')} "
          f"oc={egm_o1.get('optionChange')}")
    # (b) the EGM validates -> optionChangeStatus G2S_pending (sessionId echoes
    # the setOptionChange). The host must auto-advance and send
    # authorizeOptionChange (OCE103/104 on the EGM).
    if set_sid:
        post_to_host(avp_wrap(avp_class_command(
            "optionConfig",
            f'<g2s:optionChangeStatus g2s:configurationId="{config_id}" '
            f'g2s:transactionId="55001" g2s:applyCondition="G2S_immediate" '
            f'g2s:disableCondition="G2S_none" g2s:restartAfter="false" '
            f'g2s:changeStatus="G2S_pending" g2s:changeException="0" '
            f'g2s:changeDateTime="{now_iso()}"/>',
            "470", set_sid, "G2S_response")))
    m = expect_host_post("authorizeOptionChange")
    auth_sid = None
    if m:
        auth_sid = m["sessionId"]
        check("authorizeOptionChange under optionConfig class as G2S_request, "
              "echoes configurationId + the EGM-assigned transactionId",
              m["class"] == "optionConfig"
              and m["command"] == "authorizeOptionChange"
              and m["sessionType"] == "G2S_request"
              and m.get("commandAttrs", {}) == {
                  "configurationId": config_id, "transactionId": "55001"},
              f"got {m.get('command')}/{m.get('commandAttrs')}")
    time.sleep(0.3)
    with urllib.request.urlopen(STATUS_URL, timeout=5) as resp:
        snap_o2 = json.loads(resp.read())
    egm_o2 = snap_o2.get(EGM_ID, {})
    check("status: stage=authorizing, txn recorded, result=pending",
          egm_o2.get("optionConfigStage") == "authorizing"
          and egm_o2.get("optionChange", {}).get("transactionId") == "55001"
          and egm_o2.get("optionChange", {}).get("result") == "pending",
          f"got stage={egm_o2.get('optionConfigStage')} "
          f"oc={egm_o2.get('optionChange')}")
    # (c) EGM authorizes -> optionChangeStatus G2S_authorized.
    if auth_sid:
        post_to_host(avp_wrap(avp_class_command(
            "optionConfig",
            f'<g2s:optionChangeStatus g2s:configurationId="{config_id}" '
            f'g2s:transactionId="55001" g2s:applyCondition="G2S_immediate" '
            f'g2s:disableCondition="G2S_none" g2s:restartAfter="false" '
            f'g2s:changeStatus="G2S_authorized" g2s:changeException="0" '
            f'g2s:changeDateTime="{now_iso()}"/>',
            "471", auth_sid, "G2S_response")))
        time.sleep(0.3)
        with urllib.request.urlopen(STATUS_URL, timeout=5) as resp:
            snap_o3 = json.loads(resp.read())
        check("status: result=authorized after the EGM authorizes",
              snap_o3.get(EGM_ID, {}).get("optionChange", {}).get("result")
              == "authorized",
              f"got {snap_o3.get(EGM_ID, {}).get('optionChange')}")
    # (d) EGM applies and PUSHES the terminal optionChangeStatus (G2S_applied)
    # as a REQUEST — the host MUST answer optionChangeStatusAck (§9.18).
    post_to_host(avp_wrap(avp_class_command(
        "optionConfig",
        f'<g2s:optionChangeStatus g2s:configurationId="{config_id}" '
        f'g2s:transactionId="55001" g2s:applyCondition="G2S_immediate" '
        f'g2s:disableCondition="G2S_none" g2s:restartAfter="false" '
        f'g2s:changeStatus="G2S_applied" g2s:changeException="0" '
        f'g2s:changeDateTime="{now_iso()}"/>',
        "472", "251", "G2S_request")))
    m = expect_host_post("optionChangeStatusAck (terminal push)")
    if m:
        check("terminal push answered with optionChangeStatusAck echoing "
              "configurationId + transactionId (sessionId=251, G2S_response)",
              m["command"] == "optionChangeStatusAck"
              and m["class"] == "optionConfig"
              and m["sessionType"] == "G2S_response" and m["sessionId"] == "251"
              and m.get("commandAttrs", {}) == {
                  "configurationId": config_id, "transactionId": "55001"},
              f"got {m.get('command')}/{m.get('commandAttrs')}/"
              f"{m.get('sessionId')}")
    time.sleep(0.3)
    with urllib.request.urlopen(STATUS_URL, timeout=5) as resp:
        snap_o4 = json.loads(resp.read())
    egm_o4 = snap_o4.get(EGM_ID, {})
    check("status: stage=applied, result=applied — the change is LIVE",
          egm_o4.get("optionConfigStage") == "applied"
          and egm_o4.get("optionChange", {}).get("result") == "applied"
          and egm_o4.get("optionChange", {}).get("transactionId") == "55001",
          f"got stage={egm_o4.get('optionConfigStage')} "
          f"oc={egm_o4.get('optionChange')}")
    # (e) §9.3.4 (p.344) mandates the EGM push an optionList after apply to
    # hosts with config authority — but the host verifies WITHOUT depending
    # on this AVP honoring it: an automatic getOptionList scoped to the
    # changed device rides right behind the ack (the optionList handler
    # verifies against either arrival form). Answer the read-back in
    # response form with remoteCredit=true; the permission lamp must flip
    # and the change record must stamp 'verified'.
    m = expect_host_post("getOptionList (post-apply auto read-back)")
    rb_sid = None
    if m:
        rb_sid = m["sessionId"]
        check("read-back is a getOptionList scoped to the CHANGED device "
              "(G2S_handpay/1, groups/options wildcarded, values only)",
              m["class"] == "optionConfig"
              and m["command"] == "getOptionList"
              and m["sessionType"] == "G2S_request"
              and m.get("commandAttrs", {}) == {
                  "deviceClass": "G2S_handpay", "deviceId": "1",
                  "optionGroupId": "G2S_all", "optionId": "G2S_all",
                  "optionDetail": "false"},
              f"got {m.get('command')}/{m.get('commandAttrs')}")
    if rb_sid:
        post_to_host(avp_wrap(avp_class_command(
            "optionConfig",
            handpay_option_list(G2S_enabledRemoteCredit="true"),
            "473", rb_sid, "G2S_response")))
        time.sleep(0.3)
        with urllib.request.urlopen(STATUS_URL, timeout=5) as resp:
            snap_o5 = json.loads(resp.read())
        egm_o5 = snap_o5.get(EGM_ID, {})
        check("status: read-back flips handpayRemoteCreditAllowed to 'true' "
              "(reset-to-credits now ENABLED)",
              egm_o5.get("handpayRemoteCreditAllowed") == "true",
              f"got {egm_o5.get('handpayRemoteCreditAllowed')}")
        check("status: change record VERIFIED against the read-back (stage "
              "stays 'applied', no mismatches)",
              egm_o5.get("optionConfigStage") == "applied"
              and egm_o5.get("optionChange", {}).get("result") == "verified"
              and egm_o5.get("optionChange", {}).get("verify", {})
                  .get("mismatches") == {},
              f"got stage={egm_o5.get('optionConfigStage')} "
              f"oc={egm_o5.get('optionChange')}")
        # An optionList response draws no ack, and verification sends nothing.
        expect_no_host_post("post-verify quiescence")

    print("— Step 6.9992: setOption REFUSES unknown option / paramId — never "
          "hits the wire (G2S-27)")
    cs, cbody = post_command({
        "action": "setOption", "deviceClass": "G2S_handpay", "deviceId": "1",
        "optionId": "G2S_madeUpOption", "value": "true", "egmId": EGM_ID})
    check("unknown optionId refused (ok:false, still HTTP 200)",
          cs == 200 and cbody.get("ok") is False
          and "unknown option" in (cbody.get("error") or ""), f"got {cbody}")
    expect_no_host_post("refused unknown option")
    cs, cbody = post_command({
        "action": "setOption", "deviceClass": "G2S_handpay", "deviceId": "1",
        "optionId": "G2S_handpayOptions", "paramId": "G2S_madeUpParam",
        "value": "true", "egmId": EGM_ID})
    check("unknown paramId refused (ok:false)",
          cs == 200 and cbody.get("ok") is False
          and "unknown paramId" in (cbody.get("error") or ""), f"got {cbody}")
    expect_no_host_post("refused unknown paramId")

    print("— Step 6.9993: optionConfig change ABORT — cancelOptionChange while "
          "pending (G2S-27)")
    cs, cbody = post_command({
        "action": "setOption", "deviceClass": "G2S_handpay", "deviceId": "1",
        "optionId": "G2S_handpayOptions", "paramId": "G2S_enabledRemoteVoucher",
        "value": "true", "egmId": EGM_ID})
    check("second change accepted (prior cycle was terminal/applied)",
          cs == 200 and cbody.get("ok") is True, f"got {cbody}")
    config_id2 = str(cbody.get("configId"))
    m = expect_host_post("setOptionChange #2")
    set_sid2 = m["sessionId"] if m else None
    # EGM validates -> pending; the host auto-sends authorizeOptionChange.
    if set_sid2:
        post_to_host(avp_wrap(avp_class_command(
            "optionConfig",
            f'<g2s:optionChangeStatus g2s:configurationId="{config_id2}" '
            f'g2s:transactionId="55002" g2s:applyCondition="G2S_immediate" '
            f'g2s:disableCondition="G2S_none" g2s:restartAfter="false" '
            f'g2s:changeStatus="G2S_pending" g2s:changeException="0" '
            f'g2s:changeDateTime="{now_iso()}"/>',
            "474", set_sid2, "G2S_response")))
    expect_host_post("authorizeOptionChange #2 (drain)")
    # Operator aborts -> cancelOptionChange for the tracked configId + txn.
    cs, cbody = post_command({"action": "cancelOption", "egmId": EGM_ID})
    check("cancelOption accepted while in flight", cs == 200 and cbody.get("ok"))
    m = expect_host_post("cancelOptionChange")
    cancel_sid = None
    if m:
        cancel_sid = m["sessionId"]
        check("cancelOptionChange under optionConfig class, echoes "
              "configurationId + transactionId",
              m["class"] == "optionConfig"
              and m["command"] == "cancelOptionChange"
              and m["sessionType"] == "G2S_request"
              and m.get("commandAttrs", {}) == {
                  "configurationId": config_id2, "transactionId": "55002"},
              f"got {m.get('command')}/{m.get('commandAttrs')}")
    # EGM confirms the cancel -> optionChangeStatus G2S_cancelled.
    if cancel_sid:
        post_to_host(avp_wrap(avp_class_command(
            "optionConfig",
            f'<g2s:optionChangeStatus g2s:configurationId="{config_id2}" '
            f'g2s:transactionId="55002" g2s:applyCondition="G2S_immediate" '
            f'g2s:disableCondition="G2S_none" g2s:restartAfter="false" '
            f'g2s:changeStatus="G2S_cancelled" g2s:changeException="0" '
            f'g2s:changeDateTime="{now_iso()}"/>',
            "475", cancel_sid, "G2S_response")))
        time.sleep(0.3)
        with urllib.request.urlopen(STATUS_URL, timeout=5) as resp:
            snap_o6 = json.loads(resp.read())
        egm_o6 = snap_o6.get(EGM_ID, {})
        check("status: stage=cancelled, result=cancelled — nothing persisted "
              "(the remoteVoucher edit was rolled back by aborting)",
              egm_o6.get("optionConfigStage") == "cancelled"
              and egm_o6.get("optionChange", {}).get("result") == "cancelled",
              f"got stage={egm_o6.get('optionConfigStage')} "
              f"oc={egm_o6.get('optionChange')}")

    print("— Step 6.9994: local cancel (pre-ownership silence) + in-flight "
          "refusal (G2S-27)")
    cs, cbody = post_command({
        "action": "setOption", "deviceClass": "G2S_handpay", "deviceId": "1",
        "optionId": "G2S_handpayOptions", "paramId": "G2S_mixCreditTypes",
        "value": "true", "egmId": EGM_ID})
    check("third change accepted", cs == 200 and cbody.get("ok"))
    config_id3 = str(cbody.get("configId"))
    expect_host_post("setOptionChange #3 (left un-answered — silence)")
    # A second setOption while one is genuinely in flight is refused (§9.3 —
    # ONE txn at a time) and never hits the wire.
    cs, cbody = post_command({
        "action": "setOption", "deviceClass": "G2S_handpay", "deviceId": "1",
        "optionId": "G2S_handpayOptions", "paramId": "G2S_enabledLocalHandpay",
        "value": "false", "egmId": EGM_ID})
    check("concurrent setOption refused (already in flight)",
          cs == 200 and cbody.get("ok") is False
          and "in flight" in (cbody.get("error") or ""), f"got {cbody}")
    expect_no_host_post("refused concurrent setOption")
    # The EGM never validated the set (no transactionId) — cancelOption resolves
    # it LOCALLY with no cancelOptionChange on the wire (nothing to cancel).
    cs, cbody = post_command({"action": "cancelOption", "egmId": EGM_ID})
    check("local cancel accepted (no EGM txn yet — pre-ownership silence)",
          cs == 200 and cbody.get("ok") and cbody.get("cancelled") == "local",
          f"got {cbody}")
    expect_no_host_post("local cancel (nothing to cancel on the wire)")
    time.sleep(0.3)
    with urllib.request.urlopen(STATUS_URL, timeout=5) as resp:
        snap_o7 = json.loads(resp.read())
    egm_o7 = snap_o7.get(EGM_ID, {})
    check("status: stage=cancelled, result=cancelled after the local abort",
          egm_o7.get("optionConfigStage") == "cancelled"
          and egm_o7.get("optionChange", {}).get("result") == "cancelled"
          and egm_o7.get("optionChange", {}).get("configId") == config_id3,
          f"got stage={egm_o7.get('optionConfigStage')} "
          f"oc={egm_o7.get('optionChange')}")

    print("— Step 6.9995: getOptionChangeStatus reads the change-log high-water "
          "(G2S-27)")
    cs, cbody = post_command({"action": "getOptionChangeStatus",
                              "egmId": EGM_ID})
    check("getOptionChangeStatus command accepted",
          cs == 200 and cbody.get("ok"))
    m = expect_host_post("getOptionChangeLogStatus")
    log_sid = None
    if m:
        log_sid = m["sessionId"]
        check("getOptionChangeLogStatus under optionConfig class as "
              "G2S_request at deviceId=1",
              m["class"] == "optionConfig"
              and m["command"] == "getOptionChangeLogStatus"
              and m["sessionType"] == "G2S_request" and m["deviceId"] == "1",
              f"got {m.get('class')}.{m.get('command')}/dev={m.get('deviceId')}")
    if log_sid:
        post_to_host(avp_wrap(avp_class_command(
            "optionConfig",
            '<g2s:optionChangeLogStatus g2s:lastSequence="7" '
            'g2s:totalEntries="3"/>',
            "476", log_sid, "G2S_response")))
        time.sleep(0.3)
        with urllib.request.urlopen(STATUS_URL, timeout=5) as resp:
            snap_o8 = json.loads(resp.read())
        ocl = snap_o8.get(EGM_ID, {}).get("optionChangeLog", {})
        check("status: optionChangeLog captured (lastSequence=7, "
              "totalEntries=3)",
              ocl.get("lastSequence") == "7" and ocl.get("totalEntries") == "3",
              f"got {ocl}")
    # Test Panel wiring shipped: the Advanced group + setOption fields + the
    # option-change chip render from the served page.
    _, _, oc_ui = raw_get("/test")
    check("Test Panel carries the optionConfig-change wiring (setOption + "
          "cancelOption + getOptionChangeStatus + optionChange render + "
          "fillOption prefill)",
          "setOption" in oc_ui and "cancelOption" in oc_ui
          and "getOptionChangeStatus" in oc_ui and "optionChange" in oc_ui
          and "fillOption" in oc_ui,
          "missing optionConfig-change wiring in served page")

    print("— Step 6.99951: optionConfig change ERROR path — terminal "
          "optionChangeStatus G2S_error (G2S-27)")
    cs, cbody = post_command({
        "action": "setOption", "deviceClass": "G2S_handpay", "deviceId": "1",
        "optionId": "G2S_handpayOptions", "paramId": "G2S_requestNonCash",
        "value": "true", "egmId": EGM_ID})
    check("error-path change accepted (prior cycle terminal)",
          cs == 200 and cbody.get("ok") is True, f"got {cbody}")
    config_id4 = str(cbody.get("configId"))
    m = expect_host_post("setOptionChange #4 (error path)")
    err_sid = m["sessionId"] if m else None
    if err_sid:
        # The EGM refuses at apply: terminal error status, changeException=2
        # ('error applying option changes', Table 9.46) in RESPONSE form.
        post_to_host(avp_wrap(avp_class_command(
            "optionConfig",
            f'<g2s:optionChangeStatus g2s:configurationId="{config_id4}" '
            f'g2s:transactionId="55003" g2s:applyCondition="G2S_immediate" '
            f'g2s:disableCondition="G2S_none" g2s:restartAfter="false" '
            f'g2s:changeStatus="G2S_error" g2s:changeException="2" '
            f'g2s:changeDateTime="{now_iso()}"/>',
            "477", err_sid, "G2S_response")))
        time.sleep(0.3)
        with urllib.request.urlopen(STATUS_URL, timeout=5) as resp:
            snap_e1 = json.loads(resp.read())
        egm_e1 = snap_e1.get(EGM_ID, {})
        check("status: stage=failed, result=error:G2S_error, exception 2 "
              "recorded",
              egm_e1.get("optionConfigStage") == "failed"
              and egm_e1.get("optionChange", {}).get("result")
              == "error:G2S_error"
              and egm_e1.get("optionChange", {}).get("changeException") == "2",
              f"got stage={egm_e1.get('optionConfigStage')} "
              f"oc={egm_e1.get('optionChange')}")
        # A failed change draws NO authorize and NO read-back — the values
        # never changed, so nothing needs verifying.
        expect_no_host_post("failed change (no authorize / no read-back)")

    print("— Step 6.99952: enableRemoteHandpay — the purpose-built "
          "multi-param flip + auto read-back verify (G2S-25/27)")
    # Narrative: an operator re-disabled remote key-off at the machine — a
    # fresh config-sync push resets the inventory to the all-false state
    # (BOTH enabledRemoteCredit and disabledRemoteCredit false).
    post_to_host(avp_wrap(avp_class_command(
        "optionConfig", oc_sync_false, "478", "253", "G2S_request",
        ttl="120000")))
    expect_host_post("optionListAck (operator re-disabled remote key-off)")
    time.sleep(0.3)
    with urllib.request.urlopen(STATUS_URL, timeout=5) as resp:
        snap_r0 = json.loads(resp.read())
    check("status: lamp back to 'false' — the flip's target state",
          snap_r0.get(EGM_ID, {}).get("handpayRemoteCreditAllowed") == "false",
          f"got {snap_r0.get(EGM_ID, {}).get('handpayRemoteCreditAllowed')}")
    # Fire the purpose-built action — no args beyond the target EGM.
    cs, cbody = post_command({"action": "enableRemoteHandpay",
                              "egmId": EGM_ID})
    check("enableRemoteHandpay accepted with the exact edit map (BOTH "
          "enabledRemoteCredit + disabledRemoteCredit false -> true in ONE "
          "transaction) + before/verify surfaced",
          cs == 200 and cbody.get("ok") is True
          and cbody.get("params") == {
              "G2S_enabledRemoteCredit": {"old": "false", "new": "true"},
              "G2S_disabledRemoteCredit": {"old": "false", "new": "true"}}
          and cbody.get("before") == {
              "G2S_enabledRemoteCredit": "false",
              "G2S_disabledRemoteCredit": "false"}
          and cbody.get("configId") and cbody.get("verify"),
          f"got {cbody}")
    config_id5 = str(cbody.get("configId"))
    m = expect_host_post("setOptionChange (enableRemoteHandpay)")
    erh_sid = None
    if m:
        erh_sid = m["sessionId"]
        det = option_change_details(m["raw"])
        check("ONE setOptionChange carries the COMPLETE param set with BOTH "
              "remote-credit params flipped and everything else untouched "
              "(§9.15 multi-param edit), addressed to the handpay 4-tuple, "
              "authorizeList = self",
              det and det["values"] == dict(
                  handpay_fixture_params,
                  G2S_enabledRemoteCredit="true",
                  G2S_disabledRemoteCredit="true")
              and det["option"] == {
                  "deviceClass": "G2S_handpay", "deviceId": "1",
                  "optionGroupId": "G2S_handpayOptions",
                  "optionId": "G2S_handpayOptions"}
              and det["attrs"].get("configurationId") == config_id5
              and det["authorizeHosts"] == ["1"],
              f"got {det['values'] if det else None} / "
              f"{det['option'] if det else None}")
    # EGM validates -> pending (txn 55004); the host auto-authorizes.
    if erh_sid:
        post_to_host(avp_wrap(avp_class_command(
            "optionConfig",
            f'<g2s:optionChangeStatus g2s:configurationId="{config_id5}" '
            f'g2s:transactionId="55004" g2s:applyCondition="G2S_immediate" '
            f'g2s:disableCondition="G2S_none" g2s:restartAfter="false" '
            f'g2s:changeStatus="G2S_pending" g2s:changeException="0" '
            f'g2s:changeDateTime="{now_iso()}"/>',
            "479", erh_sid, "G2S_response")))
    m = expect_host_post("authorizeOptionChange (enableRemoteHandpay)")
    erh_auth_sid = m["sessionId"] if m else None
    if erh_auth_sid:
        post_to_host(avp_wrap(avp_class_command(
            "optionConfig",
            f'<g2s:optionChangeStatus g2s:configurationId="{config_id5}" '
            f'g2s:transactionId="55004" g2s:applyCondition="G2S_immediate" '
            f'g2s:disableCondition="G2S_none" g2s:restartAfter="false" '
            f'g2s:changeStatus="G2S_authorized" g2s:changeException="0" '
            f'g2s:changeDateTime="{now_iso()}"/>',
            "480", erh_auth_sid, "G2S_response")))
    # EGM applies -> terminal push (request form): host must ack AND fire the
    # scoped read-back.
    post_to_host(avp_wrap(avp_class_command(
        "optionConfig",
        f'<g2s:optionChangeStatus g2s:configurationId="{config_id5}" '
        f'g2s:transactionId="55004" g2s:applyCondition="G2S_immediate" '
        f'g2s:disableCondition="G2S_none" g2s:restartAfter="false" '
        f'g2s:changeStatus="G2S_applied" g2s:changeException="0" '
        f'g2s:changeDateTime="{now_iso()}"/>',
        "481", "254", "G2S_request")))
    m = expect_host_post("optionChangeStatusAck (enableRemoteHandpay applied)")
    if m:
        check("terminal push acked (configurationId + transactionId echoed)",
              m["command"] == "optionChangeStatusAck"
              and m.get("commandAttrs", {}) == {
                  "configurationId": config_id5, "transactionId": "55004"},
              f"got {m.get('command')}/{m.get('commandAttrs')}")
    m = expect_host_post("getOptionList (read-back after the flip)")
    erh_rb_sid = m["sessionId"] if m else None
    if m:
        check("flip read-back scoped to G2S_handpay/1",
              m["command"] == "getOptionList"
              and m.get("commandAttrs", {}).get("deviceClass")
              == "G2S_handpay"
              and m.get("commandAttrs", {}).get("deviceId") == "1",
              f"got {m.get('command')}/{m.get('commandAttrs')}")
    if erh_rb_sid:
        post_to_host(avp_wrap(avp_class_command(
            "optionConfig",
            handpay_option_list(G2S_enabledRemoteCredit="true",
                                G2S_disabledRemoteCredit="true"),
            "482", erh_rb_sid, "G2S_response")))
        time.sleep(0.3)
        with urllib.request.urlopen(STATUS_URL, timeout=5) as resp:
            snap_r1 = json.loads(resp.read())
        egm_r1 = snap_r1.get(EGM_ID, {})
        check("status: handpayRemoteCreditAllowed flips to 'true', stage "
              "lands 'applied', record VERIFIED with both params in the "
              "read-back",
              egm_r1.get("handpayRemoteCreditAllowed") == "true"
              and egm_r1.get("optionConfigStage") == "applied"
              and egm_r1.get("optionChange", {}).get("result") == "verified"
              and egm_r1.get("optionChange", {}).get("verify", {})
                  .get("readback") == {"G2S_enabledRemoteCredit": "true",
                                       "G2S_disabledRemoteCredit": "true"},
              f"got stage={egm_r1.get('optionConfigStage')} "
              f"oc={egm_r1.get('optionChange')}")

    print("— Step 6.99953: enableRemoteHandpay is a no-op once enabled — "
          "never hits the wire (G2S-27)")
    cs, cbody = post_command({"action": "enableRemoteHandpay",
                              "egmId": EGM_ID})
    check("second enableRemoteHandpay answers alreadyEnabled (both values "
          "at target)",
          cs == 200 and cbody.get("ok") is True
          and cbody.get("alreadyEnabled") is True
          and cbody.get("before") == {"G2S_enabledRemoteCredit": "true",
                                      "G2S_disabledRemoteCredit": "true"},
          f"got {cbody}")
    expect_no_host_post("no-op enableRemoteHandpay")
    # The served Test Panel carries the purpose-built flip: danger-confirm
    # button on the handpay card + the live permission lamp fed by
    # handpayRemoteCreditAllowed.
    _, _, erh_ui = raw_get("/test")
    check("Test Panel carries the enableRemoteHandpay wiring (danger-confirm "
          "button + handpayRemoteCreditAllowed lamp)",
          "enableRemoteHandpay" in erh_ui and "hpEnableFire" in erh_ui
          and "handpayRemoteCreditAllowed" in erh_ui,
          "missing enableRemoteHandpay wiring in served page")

    print("— Step 6.99954: lossy option — a nested complexValue cannot "
          "round-trip, so setOption refuses instead of pruning (G2S-27 "
          "post-review)")
    # A config-sync push whose option nests complexValue-within-complexValue
    # (legal G2S — §9.13.9 allows complexParameter-within-complexParameter).
    # The round-trip parser keeps only ONE level, so re-emitting this option
    # as the §9.15-mandated COMPLETE set would silently drop the inner block
    # — the host must flag it lossy at parse and refuse the change. Nested
    # shapes are wire-proven absent on THIS AVP (0 of 408 complexValue blocks
    # in the live 2026-07-02 inventory); the guard is for future EGMs.
    lossy_sync = (
        '<g2s:optionList>'
        '<g2s:deviceOptions g2s:deviceClass="G2S_printer" g2s:deviceId="1">'
        '<g2s:optionGroup g2s:optionGroupId="CN_lossyGroup" '
        'g2s:optionGroupName="Lossy Group">'
        '<g2s:optionItem g2s:optionId="CN_lossyOption" '
        'g2s:securityLevel="G2S_operator">'
        '<g2s:optionCurrentValues>'
        '<g2s:complexValue g2s:paramId="CN_outer">'
        '<g2s:booleanValue g2s:paramId="CN_flat">true</g2s:booleanValue>'
        '<g2s:complexValue g2s:paramId="CN_inner">'
        '<g2s:integerValue g2s:paramId="CN_deep">7</g2s:integerValue>'
        '</g2s:complexValue>'
        '</g2s:complexValue>'
        '</g2s:optionCurrentValues>'
        '</g2s:optionItem>'
        '</g2s:optionGroup>'
        '</g2s:deviceOptions>'
        '</g2s:optionList>')
    post_to_host(avp_wrap(avp_class_command(
        "optionConfig", lossy_sync, "483", "255", "G2S_request",
        ttl="120000")))
    expect_host_post("optionListAck (lossy option config-sync)")
    time.sleep(0.3)
    with urllib.request.urlopen(STATUS_URL, timeout=5) as resp:
        snap_l0 = json.loads(resp.read())
    check("status: the lossy option still lands in the inventory (parse "
          "keeps what it can — only the CHANGE path refuses)",
          "G2S_printer/1/CN_lossyGroup/CN_lossyOption"
          in snap_l0.get(EGM_ID, {}).get("configOptions", []),
          f"got {snap_l0.get(EGM_ID, {}).get('configOptions')}")
    cs, cbody = post_command({
        "action": "setOption", "deviceClass": "G2S_printer", "deviceId": "1",
        "optionId": "CN_lossyOption", "paramId": "CN_flat",
        "value": "false", "egmId": EGM_ID})
    check("setOption on the lossy option refused (ok:false, names the "
          "nested-complexValue cause)",
          cs == 200 and cbody.get("ok") is False
          and "nested complexValue" in (cbody.get("error") or ""),
          f"got {cbody}")
    expect_no_host_post("refused lossy option (never hits the wire)")

    print("— Step 6.99955: boolean read-back is case-tolerant — 'True'/'TRUE' "
          "still verifies AND still no-ops the guard (G2S-27 post-review)")
    # Narrative: the operator re-disables at the machine (config-sync back to
    # all-false); the host flips it again — but THIS time the EGM's post-apply
    # read-back answers with capitalized xs:boolean spellings. The shared
    # comparator (option_values_equal) must stamp 'verified', and a follow-up
    # enableRemoteHandpay must read 'True' as already-at-target.
    post_to_host(avp_wrap(avp_class_command(
        "optionConfig", oc_sync_false, "484", "256", "G2S_request",
        ttl="120000")))
    expect_host_post("optionListAck (operator re-disabled once more)")
    cs, cbody = post_command({"action": "enableRemoteHandpay",
                              "egmId": EGM_ID})
    check("third enableRemoteHandpay accepted (values back at false)",
          cs == 200 and cbody.get("ok") is True and cbody.get("configId"),
          f"got {cbody}")
    config_id6 = str(cbody.get("configId"))
    m = expect_host_post("setOptionChange (case-fold run)")
    cf_sid = m["sessionId"] if m else None
    if cf_sid:
        post_to_host(avp_wrap(avp_class_command(
            "optionConfig",
            f'<g2s:optionChangeStatus g2s:configurationId="{config_id6}" '
            f'g2s:transactionId="55005" g2s:applyCondition="G2S_immediate" '
            f'g2s:disableCondition="G2S_none" g2s:restartAfter="false" '
            f'g2s:changeStatus="G2S_pending" g2s:changeException="0" '
            f'g2s:changeDateTime="{now_iso()}"/>',
            "485", cf_sid, "G2S_response")))
    m = expect_host_post("authorizeOptionChange (case-fold run)")
    cf_auth_sid = m["sessionId"] if m else None
    if cf_auth_sid:
        post_to_host(avp_wrap(avp_class_command(
            "optionConfig",
            f'<g2s:optionChangeStatus g2s:configurationId="{config_id6}" '
            f'g2s:transactionId="55005" g2s:applyCondition="G2S_immediate" '
            f'g2s:disableCondition="G2S_none" g2s:restartAfter="false" '
            f'g2s:changeStatus="G2S_authorized" g2s:changeException="0" '
            f'g2s:changeDateTime="{now_iso()}"/>',
            "486", cf_auth_sid, "G2S_response")))
    post_to_host(avp_wrap(avp_class_command(
        "optionConfig",
        f'<g2s:optionChangeStatus g2s:configurationId="{config_id6}" '
        f'g2s:transactionId="55005" g2s:applyCondition="G2S_immediate" '
        f'g2s:disableCondition="G2S_none" g2s:restartAfter="false" '
        f'g2s:changeStatus="G2S_applied" g2s:changeException="0" '
        f'g2s:changeDateTime="{now_iso()}"/>',
        "488", "257", "G2S_request")))
    expect_host_post("optionChangeStatusAck (case-fold applied)")
    m = expect_host_post("getOptionList (case-fold read-back)")
    cf_rb_sid = m["sessionId"] if m else None
    if cf_rb_sid:
        post_to_host(avp_wrap(avp_class_command(
            "optionConfig",
            handpay_option_list(G2S_enabledRemoteCredit="True",
                                G2S_disabledRemoteCredit="TRUE"),
            "489", cf_rb_sid, "G2S_response")))
        time.sleep(0.3)
        with urllib.request.urlopen(STATUS_URL, timeout=5) as resp:
            snap_cf = json.loads(resp.read())
        egm_cf = snap_cf.get(EGM_ID, {})
        check("status: capitalized boolean read-back still VERIFIES (raw "
              "spellings preserved in the readback record)",
              egm_cf.get("optionConfigStage") == "applied"
              and egm_cf.get("optionChange", {}).get("result") == "verified"
              and egm_cf.get("optionChange", {}).get("verify", {})
                  .get("readback") == {"G2S_enabledRemoteCredit": "True",
                                       "G2S_disabledRemoteCredit": "TRUE"},
              f"got stage={egm_cf.get('optionConfigStage')} "
              f"oc={egm_cf.get('optionChange')}")
        # The no-op guard uses the SAME comparator: 'True'/'TRUE' in the
        # inventory reads as already-at-target — nothing hits the wire.
        cs, cbody = post_command({"action": "enableRemoteHandpay",
                                  "egmId": EGM_ID})
        check("enableRemoteHandpay no-ops against the capitalized inventory "
              "(alreadyEnabled via the shared comparator)",
              cs == 200 and cbody.get("ok") is True
              and cbody.get("alreadyEnabled") is True, f"got {cbody}")
        expect_no_host_post("no-op against capitalized booleans")

    print("— Step 6.9996: typed event hooks + activity tape (G2S-12/13)")
    # The semantic-hook layer maps LIVE eventReports onto named hooks that
    # feed the engine-level activity tape at /api/status "activity" (newest
    # first). By this point the run has delivered exactly TWO hookable live
    # events — the CBE307 cabinet-door open (step 6.9, eventId=8) and the
    # $20 NAE114 bill-in (step 6.998, eventId=40) — PLUS the eight G2S-39
    # wat commit close-outs (steps 6.9792-6.97984), whose
    # wat_transfer_completed/wat_transfer_failed hooks fire from the
    # commitTransfer handler (the deduped, amounts-bearing moment — NOT the
    # thin WTE107 event). Everything else was either unmapped (CBE101/102),
    # merged from BACKFILL (the step-6.62 NAE114 log entry — history, not
    # activity), or a deduped retry (the NAE114 retries in 6.62/6.998, the
    # 6.9793 duplicate commitTransfer, the 6.97984 reused-txn retry) — none
    # of which may reach the tape. So the tape right now is the isolation
    # proof in miniature.
    wat_tail = ["wat_transfer_completed",   # 6.97984 reused-txn cash-out
                "wat_transfer_completed",   # 6.97981 commit after the
                #                             rejected cancel
                "wat_transfer_failed",      # 6.9798 cancel (egmException=2)
                "wat_transfer_failed",      # 6.9797 cash-out DENIED (no armed
                #                             wallet -> reject -> ticket, all-
                #                             zero close-out; never House)
                "wat_transfer_failed",      # 6.9796 EGM abort
                "wat_transfer_completed",   # 6.9795 reduced push
                "wat_transfer_failed",      # 6.9794 deny close-out
                "wat_transfer_completed"]   # 6.9792 happy-path push
    with urllib.request.urlopen(STATUS_URL, timeout=5) as resp:
        snap_act = json.loads(resp.read())
    act = snap_act.get("activity")
    check("status exposes the activity tape as a top-level list",
          isinstance(act, list), f"got {type(act).__name__}")
    act = act or []
    check("tape holds EXACTLY the live hookable events (bill_in + the eight "
          "wat close-outs + door_open) — backfills and deduped retries "
          "(incl. the duplicate commitTransfer) never reach it",
          [a.get("hook") for a in act]
          == ["bill_in"] + wat_tail + ["door_open"],
          f"got {[a.get('hook') for a in act]}")
    check("newest first: bill_in tops the tape with its typed denomination "
          "extras ($20 / 2000000 mc / USD / txn)",
          bool(act) and act[0].get("hook") == "bill_in"
          and act[0].get("extras", {}).get("denom") == "$20"
          and act[0].get("extras", {}).get("amt") == 2000000
          and act[0].get("extras", {}).get("currencyId") == "USD"
          and act[0].get("extras", {}).get("transactionId") == "888001",
          f"got {act[0] if act else None}")
    wat_done = next((a for a in act
                     if a.get("hook") == "wat_transfer_completed"), None)
    check("wat_transfer_completed entry carries the money extras "
          "(direction/accountId/amt/state=committed)",
          wat_done is not None and wat_done.get("category") == "wat"
          and wat_done.get("extras", {}).get("accountId") == "house"
          and wat_done.get("extras", {}).get("state") == "committed"
          and int(wat_done.get("extras", {}).get("amt") or 0) > 0,
          f"got {wat_done}")
    door = next((a for a in act if a.get("hook") == "door_open"), None)
    check("door_open entry is normalized: egmId/code/label/category/icon/"
          "dateTime/device + door=cabinet extra",
          door is not None and door.get("egmId") == EGM_ID
          and door.get("code") == "G2S_CBE307"
          and "door" in (door.get("label") or "").lower()
          and door.get("category") == "cabinet"
          and bool(door.get("icon")) and bool(door.get("dateTime"))
          and door.get("device") == "G2S_cabinet/1"
          and door.get("extras", {}).get("door") == "cabinet",
          f"got {door}")
    # Now stream a burst of synthetic eventReports across the hook map:
    # handpay pending (JPE101 — the handpay class is G2S_JPE*, §11.24),
    # game start/end (GPE103/GPE112, §6.23), tilt + clear (CBE309/CBE313)
    # and a ticket out (VCE103). Each is persisted (G2S_request) and must
    # draw its eventAck as usual — hooks ride the existing path, they never
    # replace it.
    hook_dt = now_iso()
    burst = [
        ("G2S_handpay", "G2S_JPE101", "60", "777001", "760"),
        ("G2S_gamePlay", "G2S_GPE103", "61", "0", "761"),
        ("G2S_gamePlay", "G2S_GPE112", "62", "0", "762"),
        ("G2S_cabinet", "G2S_CBE309", "63", "0", "763"),
        ("G2S_cabinet", "G2S_CBE313", "64", "0", "764"),
        ("G2S_voucher", "G2S_VCE103", "65", "888002", "765"),
    ]
    for dev_cls, code, eid, txn, sid in burst:
        post_to_host(avp_wrap(avp_class_command(
            "eventHandler",
            f'<g2s:eventReport g2s:deviceClass="{dev_cls}" '
            f'g2s:deviceId="1" g2s:eventCode="{code}" g2s:eventId="{eid}" '
            f'g2s:eventDateTime="{hook_dt}" g2s:transactionId="{txn}"/>',
            str(480 + int(eid)), sid, "G2S_request")))
    acks = drain_host_posts(len(burst))
    check("all 6 synthetic eventReports eventAck'd (hooks ride the existing "
          "path)", acks == ["eventAck"] * len(burst), f"got {acks}")
    time.sleep(0.3)
    with urllib.request.urlopen(STATUS_URL, timeout=5) as resp:
        snap_act2 = json.loads(resp.read())
    act2 = snap_act2.get("activity") or []
    check("every semantic hook fired, newest first: voucher_issued/"
          "tilt_clear/tilt/game_end/game_start/handpay_pending atop the "
          "earlier bill_in/wat close-outs/door_open",
          [a.get("hook") for a in act2]
          == ["voucher_issued", "tilt_clear", "tilt", "game_end",
              "game_start", "handpay_pending", "bill_in"] + wat_tail
          + ["door_open"],
          f"got {[a.get('hook') for a in act2]}")
    check("handpay_pending carries its transactionId extra + the corrected "
          "JPE label",
          any(a.get("hook") == "handpay_pending"
              and a.get("extras", {}).get("transactionId") == "777001"
              and "handpay" in (a.get("label") or "").lower()
              for a in act2), f"got {act2}")
    # A retry of the persisted JPE101 (same eventId + eventDateTime, §4.1.4)
    # is re-acked but must NOT re-fire the hook — the tape never stutters.
    post_to_host(avp_wrap(avp_class_command(
        "eventHandler",
        '<g2s:eventReport g2s:deviceClass="G2S_handpay" g2s:deviceId="1" '
        'g2s:eventCode="G2S_JPE101" g2s:eventId="60" '
        f'g2s:eventDateTime="{hook_dt}" g2s:transactionId="777001"/>',
        "487", "766", "G2S_request")))
    ack = expect_host_post("eventAck (JPE101 retry)")
    if ack:
        check("JPE101 retry still eventAck'd",
              ack["command"] == "eventAck"
              and ack.get("commandAttrs", {}).get("eventId") == "60",
              f"got {ack.get('command')}/{ack.get('commandAttrs')}")
    time.sleep(0.3)
    with urllib.request.urlopen(STATUS_URL, timeout=5) as resp:
        snap_act3 = json.loads(resp.read())
    act3 = snap_act3.get("activity") or []
    check("deduped retry did NOT re-fire the hook (tape unchanged at 16, "
          "one handpay_pending)",
          len(act3) == 16
          and [a.get("hook") for a in act3].count("handpay_pending") == 1,
          f"got {len(act3)}/{[a.get('hook') for a in act3]}")
    # The served Test Panel carries the tape UI (renderActivity + the
    # actwrap section polling the same /api/status).
    _, _, act_ui = raw_get("/test")
    check("Test Panel carries the activity-tape wiring (renderActivity + "
          "actwrap + act-body)",
          "renderActivity" in act_ui and "actwrap" in act_ui
          and "act-body" in act_ui,
          "missing activity-tape wiring in served page")

    # =========================================================== bonus — G2S-41
    # Spec ch.19 is LOST (missing PDF split) — the wire shapes below follow
    # the recovered dossier: index-backed attribute censuses, the ch.17
    # progressive twin's choreography, live-capture device facts. Money
    # rule under test: the House is debited at commitBonus ONLY
    # (debit-on-confirm, ref g2sbonus:<txn>, §-retry idempotent) — never at
    # award-send or bonusAwardAck, and never on an exception/zero-paid
    # commit. Placed AFTER the hook-burst step (the BNE104 tape entry must
    # not disturb its exact-tape assertions).

    print("— Step 6.995: bonus class (G2S-41) — dossier-reconstructed "
          "ch.19: reads fold into /api/status bonus (attribute census "
          "verbatim)")
    cs, cbody = post_command({"egmId": EGM_ID, "action": "getBonusStatus", "deviceId": "1"})
    check("getBonusStatus command accepted",
          cs == 200 and bool(cbody.get("ok")), f"got {cs}/{cbody}")
    m = expect_host_post("getBonusStatus")
    bs_sid = m["sessionId"] if m else "1001"
    if m:
        check("getBonusStatus under bonus class dev=1 as G2S_request "
              "(ttl=30000, EMPTY element — §19.12)",
              m["class"] == "bonus" and m["command"] == "getBonusStatus"
              and m["deviceId"] == "1" and m["sessionType"] == "G2S_request"
              and m["timeToLive"] == "30000"
              and m.get("commandAttrs", {}) == {},
              f"got {m.get('class')}.{m.get('command')}/"
              f"{m.get('commandAttrs')}")
    # the EGM's bonusStatus — hostActive is the §19.4 watchdog evidence
    post_to_host(avp_wrap(avp_class_command(
        "bonus",
        '<g2s:bonusStatus g2s:configurationId="1" g2s:egmEnabled="true" '
        'g2s:hostEnabled="true" g2s:hostActive="false" '
        'g2s:hostLocked="false" g2s:bonusActive="false"/>',
        "601", bs_sid, "G2S_response")))
    cs, cbody = post_command({"egmId": EGM_ID, "action": "getBonusProfile", "deviceId": "1"})
    check("getBonusProfile command accepted",
          cs == 200 and bool(cbody.get("ok")), f"got {cs}/{cbody}")
    m = expect_host_post("getBonusProfile")
    bp_sid = m["sessionId"] if m else "1001"
    if m:
        check("getBonusProfile under bonus class dev=1 as G2S_request "
              "(EMPTY element — §19.16)",
              m["class"] == "bonus" and m["command"] == "getBonusProfile"
              and m["deviceId"] == "1"
              and m.get("commandAttrs", {}) == {},
              f"got {m.get('command')}/{m.get('commandAttrs')}")
    post_to_host(avp_wrap(avp_class_command(
        "bonus",
        '<g2s:bonusProfile g2s:configurationId="1" g2s:restartStatus="true" '
        'g2s:useDefaultConfig="false" g2s:requiredForPlay="false" '
        'g2s:minLogEntries="35" g2s:timeToLive="30000" '
        'g2s:noMessageTimer="60000" g2s:noHostText="BONUS HOST OFFLINE" '
        'g2s:maxPendingBonus="4" g2s:idReaderId="0"/>',
        "602", bp_sid, "G2S_response")))
    time.sleep(0.3)
    with urllib.request.urlopen(STATUS_URL, timeout=5) as resp:
        snap_bn = json.loads(resp.read())
    bn = snap_bn.get(EGM_ID, {}).get("bonus", {})
    check("status: bonus.status census verbatim (hostActive=false — the "
          "watchdog evidence — hostEnabled/egmEnabled true)",
          bn.get("status", {}).get("hostActive") == "false"
          and bn.get("status", {}).get("hostEnabled") == "true"
          and bn.get("status", {}).get("egmEnabled") == "true"
          and bn.get("status", {}).get("bonusActive") == "false"
          and bn.get("status", {}).get("deviceId") == "1",
          f"got {bn.get('status')}")
    check("status: bonus.profile census verbatim (noMessageTimer/"
          "requiredForPlay/maxPendingBonus/noHostText — the §19.4 "
          "watchdog arm)",
          bn.get("profile", {}).get("noMessageTimer") == "60000"
          and bn.get("profile", {}).get("requiredForPlay") == "false"
          and bn.get("profile", {}).get("maxPendingBonus") == "4"
          and bn.get("profile", {}).get("noHostText")
          == "BONUS HOST OFFLINE", f"got {bn.get('profile')}")
    check("status: bonus block carries the awards-ring contract",
          isinstance(bn.get("awards"), list)
          and isinstance(bn.get("awardCount"), int), f"got {sorted(bn)}")

    cs, cbody = post_command({"egmId": EGM_ID, "action": "setBonusState", "enable": True})
    check("setBonusState command accepted",
          cs == 200 and bool(cbody.get("ok")), f"got {cs}/{cbody}")
    m = expect_host_post("setBonusState")
    sb_sid = m["sessionId"] if m else "1001"
    if m:
        check("setBonusState enable carries ONLY enable=true (§19.11 — "
              "disableText is a disable-side attribute)",
              m["command"] == "setBonusState" and m["deviceId"] == "1"
              and m.get("commandAttrs", {}) == {"enable": "true"},
              f"got {m.get('commandAttrs')}")
    post_to_host(avp_wrap(avp_class_command(
        "bonus",
        '<g2s:bonusStatus g2s:configurationId="1" g2s:egmEnabled="true" '
        'g2s:hostEnabled="true" g2s:hostActive="true" '
        'g2s:hostLocked="false" g2s:bonusActive="false"/>',
        "603", sb_sid, "G2S_response")))
    time.sleep(0.3)
    with urllib.request.urlopen(STATUS_URL, timeout=5) as resp:
        snap_bn2 = json.loads(resp.read())
    check("status: bonusStatus response to setBonusState re-folds "
          "(hostActive flipped true)",
          snap_bn2.get(EGM_ID, {}).get("bonus", {}).get("status", {})
          .get("hostActive") == "true",
          f"got {snap_bn2.get(EGM_ID, {}).get('bonus', {}).get('status')}")

    cs, cbody = post_command({"egmId": EGM_ID, "action": "getOptionList",
                              "deviceClass": "G2S_bonus", "deviceId": "-1"})
    check("getOptionList scoped to G2S_bonus accepted (the EXISTING "
          "action — probe P-3, no duplicate)",
          cs == 200 and bool(cbody.get("ok")), f"got {cs}/{cbody}")
    m = expect_host_post("getOptionList (G2S_bonus)")
    if m:
        check("scoped read: deviceClass=G2S_bonus rides the standard "
              "5-attribute getOptionList (§9.10)",
              m["command"] == "getOptionList"
              and m.get("commandAttrs", {}) == {
                  "deviceClass": "G2S_bonus", "deviceId": "-1",
                  "optionGroupId": "G2S_all", "optionId": "G2S_all",
                  "optionDetail": "false"},
              f"got {m.get('commandAttrs')}")
    # (silence-tolerant read — the fake EGM leaves it unanswered)

    print("— Step 6.9951: bonusAward happy path — setBonusAward -> "
          "bonusAwardAck -> BNE104 -> commitBonus -> commitBonusAck; the "
          "House debited EXACTLY bonusPaidAmt at commit (debit-on-confirm)")
    bn_base = int(time.time() * 1000) + 900000  # clear of the wat txn block
    hb0 = house_cashable()
    cs, cbody = post_command({"egmId": EGM_ID, "action": "bonusAward",
                              "amountMillicents": 250000})
    check("bonusAward accepted — host-assigned PURE-NUMERIC bonusId "
          "(alphanumeric CNB ids drew MSX005 'Invalid Data Type' on the "
          "live AVP 2026-07-09 — the bonusId data type is xs:long-ish), "
          "default G2S_payAny, state=sent",
          cs == 200 and bool(cbody.get("ok"))
          and str(cbody.get("bonusId", "")).isdigit()
          and cbody.get("amountMillicents") == 250000
          and cbody.get("payMethod") == "G2S_payAny", f"got {cbody}")
    bid1 = str(cbody.get("bonusId"))
    m = expect_host_post("setBonusAward")
    if m:
        a = m.get("commandAttrs", {})
        check("setBonusAward attrs EXACTLY the dossier §19.19 census — the "
              "FULL 10-attribute form (live 2026-07-09: the minimal form "
              "drew MSX005; gSOAP wants every attribute present) with the "
              "prose-optional tail at neutral values; NO transactionId "
              "(EGM assigns it), idReader NEUTRAL not targeting",
              m["class"] == "bonus" and m["deviceId"] == "1"
              and m["sessionType"] == "G2S_request"
              and a == {"bonusId": bid1, "bonusAwardAmt": "250000",
                        "creditType": "G2S_cashable",
                        "payMethod": "G2S_payAny",
                        "idReaderType": "G2S_none", "idNumber": "",
                        "playerId": "", "idRank": "0",
                        "textMessage": "", "msgDuration": "0"},
              f"got {a}")
    check("NO debit at award-send (the money rule: never at send)",
          house_cashable() == hb0, f"got {house_cashable()} want {hb0}")
    bn_txn1 = str(bn_base)
    post_to_host(avp_wrap(avp_class_command(
        "bonus",
        f'<g2s:bonusAwardAck g2s:bonusId="{bid1}" '
        f'g2s:transactionId="{bn_txn1}"/>',
        "604", m["sessionId"] if m else "1001", "G2S_response")))
    time.sleep(0.3)
    with urllib.request.urlopen(STATUS_URL, timeout=5) as resp:
        snap_ba = json.loads(resp.read())
    aw = [r for r in snap_ba.get(EGM_ID, {}).get("bonus", {})
          .get("awards", []) if r.get("bonusId") == bid1]
    check("status: award ring entry acked with the EGM-assigned "
          "transactionId (still NO debit)",
          len(aw) == 1 and aw[0].get("state") == "acked"
          and aw[0].get("transactionId") == bn_txn1
          and house_cashable() == hb0, f"got {aw}")
    # the EGM narrates the pay: BNE104 eventReport -> tape 🎁 + ring stamp
    post_to_host(avp_wrap(avp_class_command(
        "eventHandler",
        '<g2s:eventReport g2s:deviceClass="G2S_bonus" g2s:deviceId="1" '
        'g2s:eventCode="G2S_BNE104" g2s:eventId="90" '
        f'g2s:eventDateTime="{now_iso()}" g2s:transactionId="{bn_txn1}"/>',
        "605", "770", "G2S_request")))
    ack = expect_host_post("eventAck (BNE104)")
    if ack:
        check("BNE104 eventReport eventAck'd (eventId=90)",
              ack["command"] == "eventAck"
              and ack.get("commandAttrs", {}).get("eventId") == "90",
              f"got {ack.get('command')}/{ack.get('commandAttrs')}")
    happy_bonus_commit = (
        f'<g2s:commitBonus g2s:transactionId="{bn_txn1}" '
        f'g2s:bonusId="{bid1}" g2s:bonusAwardAmt="250000" '
        'g2s:creditType="G2S_cashable" g2s:payMethod="G2S_payAny" '
        'g2s:bonusPaidAmt="250000" g2s:bonusException="0" '
        f'g2s:bonusDateTime="{now_iso()}"/>')
    st_cb, body_cb = post_to_host(avp_wrap(avp_class_command(
        "bonus", happy_bonus_commit, "606", "771", "G2S_request")))
    inner_cb, perr = parse_sync_reply(body_cb)
    check("sync reply to commitBonus is the SINGLE-wrapped message-level "
          "g2sAck ONLY (the d556ad2 rule — the app response rides the "
          "FIFO)",
          st_cb == 200 and perr is None and inner_cb is not None
          and any(localname(e.tag) == "g2sAck" for e in inner_cb.iter())
          and not any(localname(e.tag) == "commitBonusAck"
                      for e in inner_cb.iter()),
          f"got status={st_cb} err={perr}")
    m = expect_host_post("commitBonusAck")
    if m:
        check("commitBonusAck is the class RESPONSE echoing transactionId "
              "+ bonusId (§19.24; sessionId=771 echoed, ttl=0)",
              m["class"] == "bonus" and m["command"] == "commitBonusAck"
              and m["sessionType"] == "G2S_response"
              and m["sessionId"] == "771" and m["timeToLive"] == "0"
              and m.get("commandAttrs", {})
              == {"transactionId": bn_txn1, "bonusId": bid1},
              f"got {m.get('command')}/{m.get('commandAttrs')}/"
              f"sid={m.get('sessionId')}")
    time.sleep(0.3)
    check("💰 House debited EXACTLY bonusPaidAmt at commit "
          "(debit-on-confirm)", house_cashable() == hb0 - 250000,
          f"got {house_cashable()} want {hb0 - 250000}")
    led = get_json(ACCT_URL).get("ledger", [])
    check("ledger: the debit carries ref g2sbonus:<transactionId> (the "
          "retry-idempotency key)",
          any(e.get("ref") == f"g2sbonus:{bn_txn1}"
              and e.get("deltaMillicents") == -250000
              and e.get("accountId") == "house" for e in led),
          f"got refs {[e.get('ref') for e in led[-6:]]}")

    print("— Step 6.9952: commitBonus RETRY re-acked WITHOUT double-debit "
          "(the §-retry dedupe on the ledger ref)")
    post_to_host(avp_wrap(avp_class_command(
        "bonus", happy_bonus_commit, "607", "772", "G2S_request")))
    m = expect_host_post("commitBonusAck (retry)")
    if m:
        check("duplicate commitBonus re-acked (sessionId=772 echoed, same "
              "txn+bonusId)",
              m["command"] == "commitBonusAck" and m["sessionId"] == "772"
              and m.get("commandAttrs", {})
              == {"transactionId": bn_txn1, "bonusId": bid1},
              f"got {m.get('commandAttrs')}")
    time.sleep(0.3)
    check("house balance UNCHANGED after the commit retry (no double "
          "debit)", house_cashable() == hb0 - 250000,
          f"got {house_cashable()} want {hb0 - 250000}")
    with urllib.request.urlopen(STATUS_URL, timeout=5) as resp:
        snap_bc = json.loads(resp.read())
    aw = [r for r in snap_bc.get(EGM_ID, {}).get("bonus", {})
          .get("awards", []) if r.get("bonusId") == bid1]
    check("status: ONE ring entry — committed, paidAmt=250000, "
          "exception=0, BNE104 stamp riding the typed hook",
          len(aw) == 1 and aw[0].get("state") == "committed"
          and aw[0].get("paidAmt") == 250000
          and bool(aw[0].get("bne104At"))
          and aw[0].get("exception") == "0", f"got {aw}")
    act_bn = snap_bc.get("activity") or []
    check("activity tape: 🎁 bonus_award_paid entry with the txn extra",
          any(a.get("hook") == "bonus_award_paid"
              and (a.get("extras") or {}).get("transactionId") == bn_txn1
              and "🎁" in (a.get("label") or "") for a in act_bn),
          f"got {[a.get('hook') for a in act_bn[:4]]}")

    print("— Step 6.9953: failed/zero-paid commit — payMethod+textMessage "
          "honored on the wire, commitBonusAck still closes it out, the "
          "House NEVER debited")
    hb1 = house_cashable()
    cs, cbody = post_command({"egmId": EGM_ID, "action": "bonusAward",
                              "amountMillicents": 100000,
                              "payMethod": "G2S_payHandpay",
                              "textMessage": "B" * 200})
    check("bonusAward #2 accepted (payMethod whitelisted, textMessage "
          "clamped)", cs == 200 and bool(cbody.get("ok")), f"got {cbody}")
    bid2 = str(cbody.get("bonusId"))
    m = expect_host_post("setBonusAward #2")
    if m:
        a = m.get("commandAttrs", {})
        check("setBonusAward #2: payMethod=G2S_payHandpay, textMessage "
              "clamped to 128 + msgDuration rides along, still NO "
              "transactionId",
              a.get("payMethod") == "G2S_payHandpay"
              and a.get("bonusAwardAmt") == "100000"
              and len(a.get("textMessage") or "") == 128
              and a.get("msgDuration") == "10000"
              and "transactionId" not in a, f"got {a}")
    bn_txn2 = str(bn_base + 1)
    post_to_host(avp_wrap(avp_class_command(
        "bonus",
        f'<g2s:bonusAwardAck g2s:bonusId="{bid2}" '
        f'g2s:transactionId="{bn_txn2}"/>',
        "608", m["sessionId"] if m else "1001", "G2S_response")))
    post_to_host(avp_wrap(avp_class_command(
        "bonus",
        f'<g2s:commitBonus g2s:transactionId="{bn_txn2}" '
        f'g2s:bonusId="{bid2}" g2s:bonusAwardAmt="100000" '
        'g2s:creditType="G2S_cashable" g2s:payMethod="G2S_payHandpay" '
        'g2s:bonusPaidAmt="0" g2s:bonusException="1" '
        f'g2s:bonusDateTime="{now_iso()}"/>',
        "609", "773", "G2S_request")))
    m = expect_host_post("commitBonusAck (failed award)")
    if m:
        check("failed award STILL acked (the close-out echo — txn+bonusId)",
              m["command"] == "commitBonusAck"
              and m.get("commandAttrs", {})
              == {"transactionId": bn_txn2, "bonusId": bid2},
              f"got {m.get('commandAttrs')}")
    time.sleep(0.3)
    check("exception/zero-paid commit debits NOTHING",
          house_cashable() == hb1, f"got {house_cashable()} want {hb1}")
    with urllib.request.urlopen(STATUS_URL, timeout=5) as resp:
        snap_bf = json.loads(resp.read())
    aw2 = [r for r in snap_bf.get(EGM_ID, {}).get("bonus", {})
           .get("awards", []) if r.get("bonusId") == bid2]
    check("status: failed award terminal (state=failed, exception=1, "
          "paidAmt=0)",
          len(aw2) == 1 and aw2[0].get("state") == "failed"
          and aw2[0].get("exception") == "1"
          and aw2[0].get("paidAmt") == 0, f"got {aw2}")
    led = get_json(ACCT_URL).get("ledger", [])
    check("ledger: NO g2sbonus ref for the failed transaction",
          not any(e.get("ref") == f"g2sbonus:{bn_txn2}" for e in led),
          f"got refs {[e.get('ref') for e in led[-6:]]}")

    print("— Step 6.99535: commitBonus WITHOUT a transactionId — no "
          "idempotency key means NO debit (ref g2sbonus:0 would be shared "
          "by every malformed commit), still acked with the echo")
    post_to_host(avp_wrap(avp_class_command(
        "bonus",
        '<g2s:commitBonus g2s:bonusId="CNBGHOST1" '
        'g2s:bonusAwardAmt="50000" g2s:creditType="G2S_cashable" '
        'g2s:payMethod="G2S_payAny" g2s:bonusPaidAmt="50000" '
        f'g2s:bonusException="0" g2s:bonusDateTime="{now_iso()}"/>',
        "6091", "774", "G2S_request")))
    m = expect_host_post("commitBonusAck (no-txn commit)")
    if m:
        check("no-txn commitBonus STILL acked (txn echoes the '0' "
              "fallback + bonusId)",
              m["command"] == "commitBonusAck"
              and m.get("commandAttrs", {})
              == {"transactionId": "0", "bonusId": "CNBGHOST1"},
              f"got {m.get('commandAttrs')}")
    time.sleep(0.3)
    check("no-txn commit debits NOTHING (paid>0 but no idempotency key)",
          house_cashable() == hb1, f"got {house_cashable()} want {hb1}")
    led = get_json(ACCT_URL).get("ledger", [])
    check("ledger: NO g2sbonus:0 ref ever minted",
          not any(e.get("ref") == "g2sbonus:0" for e in led),
          f"got refs {[e.get('ref') for e in led[-6:]]}")

    print("— Step 6.9954: bonusAward strict validation — junk draws HTTP "
          "400 and NOTHING touches the wire")
    for junk_label, junk_payload in (
            ("missing amount",
             {"egmId": EGM_ID, "action": "bonusAward"}),
            ("zero amount",
             {"egmId": EGM_ID, "action": "bonusAward", "amountMillicents": 0}),
            ("over-limit amount",   # 1 mc past the AFT-ceiling twin (wire-max family)
             {"egmId": EGM_ID, "action": "bonusAward",
              "amountMillicents": 9_999_999_999_001}),
            ("string amount",
             {"egmId": EGM_ID, "action": "bonusAward", "amountMillicents": "250000"}),
            ("bogus payMethod",
             {"egmId": EGM_ID, "action": "bonusAward", "amountMillicents": 1000,
              "payMethod": "G2S_payWat"}),
            ("junk deviceId",
             {"egmId": EGM_ID, "action": "bonusAward", "amountMillicents": 1000,
              "deviceId": "abc"})):
        st_v, body_v = post_command_err(junk_payload)
        check(f"bonusAward {junk_label} -> HTTP 400 ok:false",
              st_v == 400 and body_v.get("ok") is False,
              f"got {st_v}/{body_v}")
    expect_no_host_post("the junk bonusAward batch")
    check("house untouched by the junk batch", house_cashable() == hb1,
          f"got {house_cashable()} want {hb1}")

    print("— Step 6.9955: bonus EGM-log reads — getBonusLogStatus/"
          "getBonusLog fold verbatim (bonusState strings = probe P-4 "
          "evidence, never switched on)")
    cs, cbody = post_command({"egmId": EGM_ID, "action": "getBonusLogStatus"})
    check("getBonusLogStatus command accepted",
          cs == 200 and bool(cbody.get("ok")))
    m = expect_host_post("getBonusLogStatus")
    bls_sid = m["sessionId"] if m else "1001"
    post_to_host(avp_wrap(avp_class_command(
        "bonus",
        '<g2s:bonusLogStatus g2s:lastSequence="2" g2s:totalEntries="2"/>',
        "610", bls_sid, "G2S_response")))
    cs, cbody = post_command({"egmId": EGM_ID, "action": "getBonusLog"})
    check("getBonusLog command accepted",
          cs == 200 and bool(cbody.get("ok")))
    m = expect_host_post("getBonusLog")
    bll_sid = m["sessionId"] if m else "1001"
    if m:
        check("getBonusLog pages per §1.14.5 (lastSequence=0 "
              "totalEntries=0)",
              m.get("commandAttrs", {})
              == {"lastSequence": "0", "totalEntries": "0"},
              f"got {m.get('commandAttrs')}")
    post_to_host(avp_wrap(avp_class_command(
        "bonus",
        '<g2s:bonusLogList>'
        f'<g2s:bonusLog g2s:logSequence="2" g2s:deviceId="1" '
        f'g2s:bonusId="{bid1}" g2s:transactionId="{bn_txn1}" '
        'g2s:bonusState="G2S_bonusAck" g2s:payMethod="G2S_payAny" '
        'g2s:bonusAwardAmt="250000" g2s:creditType="G2S_cashable" '
        'g2s:bonusPaidAmt="250000" g2s:bonusException="0" '
        f'g2s:bonusDateTime="{now_iso()}"/>'
        '</g2s:bonusLogList>',
        "611", bll_sid, "G2S_response")))
    time.sleep(0.3)
    with urllib.request.urlopen(STATUS_URL, timeout=5) as resp:
        snap_bl = json.loads(resp.read())
    bn_log = snap_bl.get(EGM_ID, {}).get("bonus", {}).get("egmLog", {})
    check("status: bonus.egmLog carries the high-water AND the parsed "
          "entry with bonusState VERBATIM",
          bn_log.get("lastSequence") == "2"
          and bn_log.get("totalEntries") == "2"
          and bn_log.get("entryCount") == 1
          and (bn_log.get("entries") or [{}])[0].get("bonusState")
          == "G2S_bonusAck", f"got {bn_log}")

    print("— Step 7: re-handshake epoch reset (AVP reboots, commandId=300)")
    status, body = post_to_host(comms_online_local.replace(
        'g2s:commandId="223"', 'g2s:commandId="300"'))
    check("HTTP 200", status == 200)
    msg = expect_host_post("commsOnLineAck #2")
    if msg:
        check("epoch reset: host commandId restarts at 1",
              msg["commandId"] == "1", f"got {msg['commandId']}")
        check("still commsOnLineAck", msg["command"] == "commsOnLineAck")

    print("— Step 7.5: skewed re-join — auto clock-sync fires (G2S-20)")
    # Complete the re-handshake with the EGM clock ~19min behind (the real
    # AVP's observed drift): every inbound dateTimeSent is skewed, so at the
    # moment of join the host must measure the skew and lead the post-join
    # sends with cabinet.setDateTime (clock first — G2S_APX011 expiry risk).
    post_to_host(avp_wrap(avp_command(
        "<g2s:commsDisabled/>", "301", "70", ts=skewed_iso())))
    m = expect_host_post("commsDisabledAck #2")
    if m:
        check("re-join commsDisabledAck (cid=2, sessionId=70)",
              m["command"] == "commsDisabledAck" and m["commandId"] == "2"
              and m["sessionId"] == "70",
              f"got {m.get('command')}/cid={m.get('commandId')}"
              f"/sid={m.get('sessionId')}")
    m = expect_host_post("setCommsState #2")
    sid2 = "1001"
    if m:
        check("re-join setCommsState (cid=3)",
              m["command"] == "setCommsState" and m["commandId"] == "3",
              f"got {m.get('command')}/cid={m.get('commandId')}")
        sid2 = m["sessionId"]
    post_to_host(avp_wrap(avp_command(
        status_cmd, "302", sid2, "G2S_response", "0", ts=skewed_iso())))
    m = expect_host_post("auto setDateTime")
    if m:
        check("auto-sync fired: setDateTime is the FIRST post-join send "
              "(cid=4, ahead of setKeepAlive)",
              m["command"] == "setDateTime" and m["commandId"] == "4",
              f"got {m.get('command')} cid={m.get('commandId')}")
        check("auto setDateTime under cabinet class as G2S_request",
              m["class"] == "cabinet" and m["sessionType"] == "G2S_request",
              f"got {m.get('class')}/{m.get('sessionType')}")
        sdt = m.get("commandAttrs", {}).get("cabinetDateTime") or ""
        good_form = bool(re.fullmatch(
            r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z", sdt))
        off = None
        if good_form:
            off = abs((datetime.strptime(sdt, "%Y-%m-%dT%H:%M:%S.%fZ")
                       .replace(tzinfo=timezone.utc)
                       - datetime.now(timezone.utc)).total_seconds())
        check("auto setDateTime carries the host's CURRENT §1.12 time "
              "(NOT the EGM's drifted clock)",
              good_form and off < 5, f"got {sdt} (off {off})")
        time.sleep(0.3)
        with urllib.request.urlopen(STATUS_URL, timeout=5) as resp:
            snap11 = json.loads(resp.read())
        egm11 = snap11.get(EGM_ID, {})
        skew_val = egm11.get("egmClockSkewSec")
        check(f"status: egmClockSkewSec ≈ -{CLOCK_SKEW_TEST_SEC}s "
              "(the injected drift)",
              isinstance(skew_val, (int, float))
              and -CLOCK_SKEW_TEST_SEC - 60 < skew_val
              < -CLOCK_SKEW_TEST_SEC + 60, f"got {skew_val}")
        cs11 = egm11.get("clockSync", {})
        check("status: clockSync.skewBefore recorded the drift",
              isinstance(cs11.get("skewBefore"), (int, float))
              and -CLOCK_SKEW_TEST_SEC - 60 < cs11["skewBefore"]
              < -CLOCK_SKEW_TEST_SEC + 60, f"got {cs11}")
        # The EGM applies the clock and answers cabinetDateTime — now current.
        egm_now = now_iso()
        post_to_host(avp_wrap(avp_class_command(
            "cabinet",
            f'<g2s:cabinetDateTime g2s:cabinetDateTime="{egm_now}"/>',
            "303", m["sessionId"], "G2S_response", "0")))
        time.sleep(0.3)
        with urllib.request.urlopen(STATUS_URL, timeout=5) as resp:
            snap12 = json.loads(resp.read())
        egm12 = snap12.get(EGM_ID, {})
        check("status: clockSync confirmed after the EGM applies the clock",
              egm12.get("clockSync", {}).get("result") == "confirmed",
              f"got {egm12.get('clockSync')}")
        check("status: egmClockSkewSec back ≈0 after correction",
              isinstance(egm12.get("egmClockSkewSec"), (int, float))
              and abs(egm12["egmClockSkewSec"]) < 10,
              f"got {egm12.get('egmClockSkewSec')}")
    # Drain the rest of the re-join extras (per engine config) so the queue
    # ends clean, and prove the clock-sync did not displace them.
    expected_rest = []
    if engine.get("keepaliveMs", 0) > 0:
        expected_rest.append("setKeepAlive")
    if engine.get("harvest"):
        expected_rest += ["getDescriptor", "getCommHostList"]
    if engine.get("subscribe"):
        expected_rest += ["getEventHandlerProfile", "getSupportedEvents",
                          "setEventSub"]
    # The un-gated join reads close every epoch's extras (the G2S-39 WAT
    # probes, then the G2S-40 voucher probes, last). No setMeterSub here:
    # the deliberate clear from step 6.997 still sticks. No gamePlay sweep
    # either: this epoch's getDescriptor goes unanswered.
    expected_rest += ["getCabinetStatus", "getMeterInfo",
                      "getEventHandlerLog", "getWatStatus", "getWatProfile",
                      "getWatStatus", "getWatProfile",
                      "getVoucherStatus", "getVoucherProfile"]
    rest = drain_host_posts(len(expected_rest))
    check("re-join extras still fire after the clock-sync (FIFO order)",
          rest == expected_rest, f"got {rest} want {expected_rest}")

    print("— Step 7.6: IGT-extension error response uses the right namespace "
          "(licensing.getSecurityData -> APX007, no MSX004 loop)")
    # Wire-proven 2026-07-02: the AVP sends licensing.getSecurityData in its OWN
    # namespace <igtLicensing:licensing> with g2s-prefixed standard attrs. The
    # host had been answering <g2s:licensing> (wrong namespace) -> the AVP's
    # gSOAP failed to deserialize -> MSX004 -> ~30s retry loop forever. The
    # class-level error MUST mirror the extension namespace.
    lic_ns = "http://g2s.igt.com/licensing/v1.0.0"
    lic_inner = (
        f'<g2s:g2sMessage xmlns:g2s="{SCHEMA_NS}" xmlns:igtLicensing="{lic_ns}">\n'
        f'   <g2s:g2sBody g2s:hostId="1" g2s:egmId="{EGM_ID}" '
        f'g2s:dateTimeSent="{now_iso()}">\n'
        f'      <igtLicensing:licensing g2s:deviceId="1" g2s:dateTime="{now_iso()}" '
        f'g2s:commandId="9001" g2s:sessionType="G2S_request" g2s:sessionId="701" '
        f'g2s:timeToLive="30000">\n'
        f'         <igtLicensing:getSecurityData/>\n'
        f'      </igtLicensing:licensing>\n'
        f'   </g2s:g2sBody>\n'
        f'</g2s:g2sMessage>'
    )
    post_to_host(avp_wrap(lic_inner))
    m = expect_host_post("licensing error response")
    if m:
        check("error response class element is igtLicensing-namespaced (not g2s)",
              "igtLicensing:licensing" in m["raw"]
              and "<g2s:licensing" not in m["raw"], m["raw"][:200])
        check("error response carries G2S_APX007 (class not supported)",
              "G2S_APX007" in m["raw"], m["raw"][:200])
        check("error response echoes sessionId=701 as G2S_response",
              m["sessionType"] == "G2S_response" and m["sessionId"] == "701",
              f"got {m.get('sessionType')}/{m.get('sessionId')}")

    print("— Step 7.7: unhandled command in a SPOKEN class draws APX008, "
          "not APX007 (GR-21, §1.18.3.3)")
    # The host actively speaks cabinet (it originates getCabinetStatus/
    # setDateTime/...), so an unknown cabinet request must answer G2S_APX008
    # "command not supported" — G2S_APX007 repudiates the whole CLASS, and
    # this AVP demonstrably reacts to that (the optionConfig APX007 answer
    # left its bring-up unsettled pre-fix). SPOKEN_CLASSES in g2s_host.py is
    # the named source; this check + step 7.6's licensing APX007 pin both
    # sides against drift.
    post_to_host(avp_wrap(avp_class_command(
        "cabinet", '<g2s:getBogusThing/>', "9002", "702", "G2S_request")))
    m = expect_host_post("cabinet unhandled-command error response")
    if m:
        check("unknown cabinet command answered G2S_APX008 (command not "
              "supported — class IS spoken)",
              "G2S_APX008" in m["raw"] and "G2S_APX007" not in m["raw"],
              m["raw"][:200])
        check("APX008 error under the cabinet class, echoes sessionId=702 "
              "as G2S_response",
              m.get("class") == "cabinet"
              and m["sessionType"] == "G2S_response"
              and m["sessionId"] == "702",
              f"got {m.get('class')}/{m.get('sessionType')}"
              f"/{m.get('sessionId')}")

    print("— Step 7.8: remote reboot (RB) — cabinet.resetProcessor plan A + "
          "download reset-script plan B, wire-proven 2026-07-07 refusals")
    # The Test Panel carries the two-list wiring (GROUPS <-> dispatch table)
    # in a "Power" group.
    _, _, rb_ui = raw_get("/test")
    check("Test Panel carries the Power group with the remote-reboot pair "
          "(resetProcessor + rebootEgmScript buttons)",
          'name:"Power"' in rb_ui and '["resetProcessor"' in rb_ui
          and '["rebootEgmScript"' in rb_ui,
          "missing Power group / resetProcessor / rebootEgmScript in page")

    # (a) plan A: resetProcessor -> EXACT cabinet.resetProcessor (§3.16).
    cs, cbody = post_command({"action": "resetProcessor", "egmId": EGM_ID})
    check("resetProcessor command accepted",
          cs == 200 and bool(cbody.get("ok")), f"got {cs} {cbody}")
    m = expect_host_post("resetProcessor")
    if m:
        check("resetProcessor under cabinet class as G2S_request, dev=1, "
              "ttl=60000 (§3.16 generous)",
              m["class"] == "cabinet" and m["command"] == "resetProcessor"
              and m["sessionType"] == "G2S_request" and m["deviceId"] == "1"
              and m["timeToLive"] == "60000",
              f"got {m.get('class')}.{m.get('command')}/"
              f"{m.get('sessionType')}/dev={m.get('deviceId')}"
              f"/ttl={m.get('timeToLive')}")
        check("resetProcessor is EXACTLY the bare element (§3.16 — no "
              "attributes)",
              m.get("commandAttrs") == {}
              and "<g2s:resetProcessor/>" in html.unescape(m["raw"]),
              f"got attrs={m.get('commandAttrs')}")
        # Wire-proven 2026-07-07 (AVP014 R2): G2S_APX008 — resetProcessor
        # is optional and this firmware doesn't implement it. The refusal
        # must be LOGGED and HARMLESS (the association survives).
        err = (
            f'<g2s:g2sMessage xmlns:g2s="{SCHEMA_NS}">\n'
            f'   <g2s:g2sBody g2s:hostId="1" g2s:egmId="{EGM_ID}" '
            f'g2s:dateTimeSent="{now_iso()}">\n'
            f'      <g2s:cabinet g2s:deviceId="1" g2s:dateTime="{now_iso()}" '
            f'g2s:commandId="9101" g2s:sessionType="G2S_response" '
            f'g2s:sessionId="{m["sessionId"]}" g2s:timeToLive="0" '
            f'g2s:errorCode="G2S_APX008" '
            f'g2s:errorText="Command not supported."/>\n'
            f'   </g2s:g2sBody>\n'
            f'</g2s:g2sMessage>')
        post_to_host(avp_wrap(err))
        time.sleep(0.4)
        dbg = get_json(DEBUG_LOG_URL + "?lines=200")
        check("APX008 refusal logged (EGM REJECTED, errorCode surfaced)",
              any("G2S_APX008" in ln and "REJECTED" in ln
                  for ln in dbg.get("lines", [])),
              "no APX008 rejection line in the log tail")
        # The verdict must surface at /api/status.lastResetProbe (§3.16 —
        # APX008 = unsupported), attributed by the probe's sessionId.
        pr = get_json(STATUS_URL).get(EGM_ID, {}).get("lastResetProbe", {})
        check("lastResetProbe stamps the APX008 verdict (kind=resetProcessor, "
              "outcome=error:G2S_APX008)",
              pr.get("kind") == "resetProcessor"
              and pr.get("outcome") == "error:G2S_APX008", f"got {pr}")

    # (b) plan A honored (some OTHER machine would): resetStarted response
    # -> the reset banner + probe outcome. Simulated only — this AVP APX008s.
    post_command({"action": "resetProcessor", "egmId": EGM_ID})
    m = expect_host_post("resetProcessor #2")
    if m:
        post_to_host(avp_wrap(avp_class_command(
            "cabinet", "<g2s:resetStarted/>", "9102", m["sessionId"],
            "G2S_response", "0")))
        time.sleep(0.4)
        dbg = get_json(DEBUG_LOG_URL + "?lines=200")
        check("resetStarted logs the reset banner (EGM RESET STARTED)",
              any("EGM RESET STARTED" in ln for ln in dbg.get("lines", [])),
              "no reset banner in the log tail")
        pr = get_json(STATUS_URL).get(EGM_ID, {}).get("lastResetProbe", {})
        check("lastResetProbe advances to outcome=resetStarted",
              pr.get("kind") == "resetProcessor"
              and pr.get("outcome") == "resetStarted", f"got {pr}")
    with urllib.request.urlopen(STATUS_URL, timeout=5) as resp:
        snap_rb = json.loads(resp.read())
    check("association survives the resetProcessor exchanges (still onLine)",
          snap_rb.get(EGM_ID, {}).get("commsState") == "onLine",
          f"got {snap_rb.get(EGM_ID, {}).get('commsState')}")

    # (c) plan B default: rebootEgmScript -> setDownloadState THEN setScript
    # in FIFO order, one epoch (GR-18: the pair must never split).
    cs, cbody = post_command({"action": "rebootEgmScript", "egmId": EGM_ID})
    check("rebootEgmScript command accepted",
          cs == 200 and bool(cbody.get("ok")), f"got {cs} {cbody}")
    m1 = expect_host_post("setDownloadState")
    m2 = expect_host_post("setScript")
    sid_default = None
    if m1:
        check("setDownloadState rides FIRST under download class as "
              "G2S_request, dev=1, ttl=30000 (§10.7/§10.9: script "
              "execution needs hostEnabled)",
              m1["class"] == "download"
              and m1["command"] == "setDownloadState"
              and m1["sessionType"] == "G2S_request"
              and m1["deviceId"] == "1" and m1["timeToLive"] == "30000",
              f"got {m1.get('class')}.{m1.get('command')}/"
              f"{m1.get('sessionType')}/dev={m1.get('deviceId')}")
        check("setDownloadState carries EXACTLY enable=true",
              m1.get("commandAttrs") == {"enable": "true"},
              f"got {m1.get('commandAttrs')}")
    if m2:
        sc_attrs = m2.get("commandAttrs", {})
        sid_default = sc_attrs.get("scriptId")
        check("setScript under download class as G2S_request behind the "
              "state enable",
              m2["class"] == "download" and m2["command"] == "setScript"
              and m2["sessionType"] == "G2S_request",
              f"got {m2.get('class')}.{m2.get('command')}")
        check("setScript attrs EXACT: fresh integer scriptId + "
              "applyCondition=G2S_immediate + disableCondition=G2S_none "
              "(nothing else)",
              sorted(sc_attrs) == ["applyCondition", "disableCondition",
                                   "scriptId"]
              and (sid_default or "").isdigit()
              and sc_attrs.get("applyCondition") == "G2S_immediate"
              and sc_attrs.get("disableCondition") == "G2S_none",
              f"got {sc_attrs}")
        raw2 = html.unescape(m2["raw"])
        check("setScript body is the reset-only commandList (systemCmd "
              "operation=G2S_resetEgm cmdSequence=1 — NEVER package/module "
              "commands)",
              "<g2s:commandList>" in raw2
              and '<g2s:systemCmd g2s:operation="G2S_resetEgm" '
                  'g2s:cmdSequence="1"/>' in raw2
              and "package" not in raw2 and "module" not in raw2,
              raw2[:300])
        # Wire-proven 2026-07-07: setDownloadState enable=true -> a clean
        # downloadStatus (accepted). The host folds it into the log.
        post_to_host(avp_wrap(avp_class_command(
            "download",
            '<g2s:downloadStatus g2s:configurationId="0" '
            'g2s:hostEnabled="true" g2s:egmEnabled="true" '
            'g2s:downloadEnabled="true" g2s:scriptingEnabled="true"/>',
            "9103", m1["sessionId"] if m1 else "0", "G2S_response", "0")))
        # scriptStatus G2S_inProgress -> the LOUD running banner.
        post_to_host(avp_wrap(avp_class_command(
            "download",
            f'<g2s:scriptStatus g2s:scriptId="{sid_default}" '
            f'g2s:scriptStatus="G2S_inProgress"/>',
            "9104", m2["sessionId"], "G2S_response", "0")))
        time.sleep(0.4)
        dbg = get_json(DEBUG_LOG_URL + "?lines=200")
        rb_lines = dbg.get("lines", [])
        check("downloadStatus response folds into the log (accepted state)",
              any("downloadStatus" in ln and "hostEnabled=true" in ln
                  for ln in rb_lines),
              "no downloadStatus fold in the log tail")
        check("scriptStatus G2S_inProgress logs LOUDLY (reset script "
              "running banner)",
              any("RESET SCRIPT STATUS" in ln and "G2S_inProgress" in ln
                  for ln in rb_lines),
              "no loud in-progress scriptStatus line")
        pr = get_json(STATUS_URL).get(EGM_ID, {}).get("lastResetProbe", {})
        check("lastResetProbe folds the script state (kind=script, "
              "scriptId echoed, outcome=inProgress)",
              pr.get("kind") == "script"
              and pr.get("scriptId") == sid_default
              and pr.get("outcome") == "inProgress"
              and pr.get("scriptStatus") == "G2S_inProgress", f"got {pr}")

    # (d) plan B with {"scriptId": N} override + the wire-proven APX999
    # idle-gate refusal (a HARD-TILTED machine never reaches idle, so the
    # 10-second countdown re-arms forever — CONFIRMED SUPPORTED but gated).
    cs, cbody = post_command({"action": "rebootEgmScript",
                              "scriptId": 424242, "egmId": EGM_ID})
    check("rebootEgmScript with scriptId override accepted",
          cs == 200 and bool(cbody.get("ok")), f"got {cs} {cbody}")
    expect_host_post("setDownloadState #2")
    m2 = expect_host_post("setScript #2")
    if m2:
        check("scriptId override honored EXACTLY (424242 on the wire)",
              m2.get("commandAttrs", {}).get("scriptId") == "424242",
              f"got {m2.get('commandAttrs')}")
        err = (
            f'<g2s:g2sMessage xmlns:g2s="{SCHEMA_NS}">\n'
            f'   <g2s:g2sBody g2s:hostId="1" g2s:egmId="{EGM_ID}" '
            f'g2s:dateTimeSent="{now_iso()}">\n'
            f'      <g2s:download g2s:deviceId="1" '
            f'g2s:dateTime="{now_iso()}" g2s:commandId="9105" '
            f'g2s:sessionType="G2S_response" '
            f'g2s:sessionId="{m2["sessionId"]}" g2s:timeToLive="0" '
            f'g2s:errorCode="G2S_APX999" '
            f'g2s:errorText="EGM is not idle. There are 10 second(s) '
            f'remaining until the EGM is idle."/>\n'
            f'   </g2s:g2sBody>\n'
            f'</g2s:g2sMessage>')
        post_to_host(avp_wrap(err))
        time.sleep(0.4)
        dbg = get_json(DEBUG_LOG_URL + "?lines=200")
        rb_lines = dbg.get("lines", [])
        check("APX999 idle-gate refusal logged with the verbatim errorText",
              any("G2S_APX999" in ln and "EGM is not idle" in ln
                  for ln in rb_lines),
              "no APX999 + errorText line in the log tail")
        check("APX999 draws the actionable reboot-script hint (idle-gated, "
              "retry untilted)",
              any("REBOOT SCRIPT PATH REJECTED" in ln and "idle-gated" in ln
                  for ln in rb_lines),
              "no download-specific rejection hint")
        pr = get_json(STATUS_URL).get(EGM_ID, {}).get("lastResetProbe", {})
        check("lastResetProbe stamps the APX999 refusal (script id 424242, "
              "outcome=error:G2S_APX999)",
              pr.get("kind") == "script" and pr.get("scriptId") == "424242"
              and pr.get("outcome") == "error:G2S_APX999", f"got {pr}")

    # (e) fresh scriptId per attempt (the DLX006 shield): a default-id run
    # >1s later must mint a DIFFERENT scriptId.
    time.sleep(1.1)
    post_command({"action": "rebootEgmScript", "egmId": EGM_ID})
    expect_host_post("setDownloadState #3")
    m2 = expect_host_post("setScript #3")
    if m2:
        sid3 = m2.get("commandAttrs", {}).get("scriptId")
        check("default scriptId is FRESH per attempt (differs from the "
              "first run — DLX006 shield)",
              bool(sid3) and sid3.isdigit() and sid3 != sid_default
              and sid3 != "424242",
              f"got {sid3} vs first {sid_default}")
        # scriptStatus G2S_error -> the loud FAILED banner.
        post_to_host(avp_wrap(avp_class_command(
            "download",
            f'<g2s:scriptStatus g2s:scriptId="{sid3}" '
            f'g2s:scriptStatus="G2S_error" g2s:scriptException="4"/>',
            "9106", m2["sessionId"], "G2S_response", "0")))
        time.sleep(0.4)
        dbg = get_json(DEBUG_LOG_URL + "?lines=200")
        check("scriptStatus G2S_error logs LOUDLY (FAILED + "
              "scriptException)",
              any("RESET SCRIPT STATUS" in ln and "G2S_error" in ln
                  and "FAILED" in ln for ln in dbg.get("lines", [])),
              "no loud error scriptStatus line")

    # (f) download is now a SPOKEN class (GR-21): an unknown download
    # command draws APX008 (command not supported), never APX007 (which
    # would repudiate the whole class the reboot script rides on).
    post_to_host(avp_wrap(avp_class_command(
        "download", "<g2s:getBogusThing/>", "9107", "703", "G2S_request")))
    m = expect_host_post("download unhandled-command error response")
    if m:
        check("unknown download command answered G2S_APX008 (class IS "
              "spoken — the reboot script lives here)",
              "G2S_APX008" in m["raw"] and "G2S_APX007" not in m["raw"],
              m["raw"][:200])
        check("APX008 under the download class, echoes sessionId=703 as "
              "G2S_response",
              m.get("class") == "download"
              and m["sessionType"] == "G2S_response"
              and m["sessionId"] == "703",
              f"got {m.get('class')}/{m.get('sessionType')}"
              f"/{m.get('sessionId')}")

    # (g) EGM-ORIGINATED terminal scriptStatus arrives as a G2S_request
    # (§10.31/Table 10.2 — this is how the machine confirms the reboot
    # script actually ran, typically after it comes back up). The host
    # MUST answer scriptStatusAck (§10.32: no attributes) — pre-fix it
    # fell through to the unknown-request APX008, repudiating the reboot
    # confirmation itself.
    post_to_host(avp_wrap(avp_class_command(
        "download",
        '<g2s:scriptStatus g2s:scriptId="515151" '
        'g2s:scriptStatus="G2S_completed"/>',
        "9108", "704", "G2S_request")))
    m = expect_host_post("scriptStatusAck (unsolicited terminal status)")
    if m:
        check("scriptStatus-as-request draws scriptStatusAck under the "
              "download class — NOT the APX008 repudiation",
              m.get("class") == "download"
              and m["command"] == "scriptStatusAck"
              and "G2S_APX008" not in m["raw"]
              and "g2sError" not in m["raw"],
              f"got {m.get('class')}.{m.get('command')}")
        check("scriptStatusAck echoes sessionId=704 as G2S_response with "
              "NO attributes (§10.32 defines none)",
              m["sessionType"] == "G2S_response" and m["sessionId"] == "704"
              and m.get("commandAttrs", {}) == {},
              f"got {m.get('sessionType')}/{m.get('sessionId')}/"
              f"{m.get('commandAttrs')}")
    time.sleep(0.3)
    dbg = get_json(DEBUG_LOG_URL + "?lines=120")
    check("the unsolicited completed scriptStatus still logs the LOUD "
          "banner (the reboot verdict stays visible)",
          any("RESET SCRIPT STATUS" in ln and "G2S_completed" in ln
              for ln in dbg.get("lines", [])),
          "no loud completed scriptStatus line in the log tail")

    with urllib.request.urlopen(STATUS_URL, timeout=5) as resp:
        snap_rb2 = json.loads(resp.read())
    check("association survives the refusals AND the unsolicited "
          "scriptStatus exchange — still onLine and still serving",
          snap_rb2.get(EGM_ID, {}).get("commsState") == "onLine",
          f"got {snap_rb2.get(EGM_ID, {}).get('commsState')}")

    print("— Step 13: in-process WAT store slices — presentation lazy "
          "timeout, once-ref ledger idempotency, txn-reuse vs retry "
          "discrimination (crash paths a wire gate cannot reach)")
    import tempfile
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    import g2s_host as ghost
    stale = {"requestId": 7, "transactionId": "0", "state": "requested",
             "watDirection": "G2S_toEgm", "deviceId": "1",
             "reqCashableAmt": 100000, "reqPromoAmt": 0,
             "reqNonCashAmt": 0,
             "createdAt": "2026-01-01T00:00:00.000Z"}
    p = ghost.wat_present(stale)
    check("wat_present: a silent 'requested' push presents state="
          "requestTimeout after WAT_REQUEST_ABANDON_SEC, with the "
          "normalized contract fields",
          p.get("state") == "requestTimeout"
          and p.get("cashableMillicents") == 100000
          and p.get("direction") == "G2S_toEgm"
          and p.get("transactionId") == "0"
          and p.get("updatedAt") == "2026-01-01T00:00:00.000Z",
          f"got {p}")
    check("wat_present: a FRESH 'requested' push stays requested (the "
          "timeout stamps only aged copies)",
          ghost.wat_present(dict(stale, createdAt=ghost.now_iso()))
          .get("state") == "requested")
    tmpd = Path(tempfile.mkdtemp(prefix="wat_gate_"))
    acc = ghost.AccountStore(str(tmpd / "acct.json"))
    acc.adjust("house", 500000, note="seed")
    a1, e1 = acc.adjust("house", -200000, note="escrow", ref="r:esc",
                        once=True)
    a2, e2 = acc.adjust("house", -200000, note="escrow", ref="r:esc",
                        once=True)
    check("AccountStore.adjust once=True: the duplicate movement is a "
          "NO-OP returning the current balance (crash-retry safety)",
          e1 is None and e2 is None
          and (a1 or {}).get("cashableMillicents") == 300000
          and (a2 or {}).get("cashableMillicents") == 300000,
          f"got {a1}/{a2} err {e1}/{e2}")
    check("AccountStore.ref_totals recovers what a leg actually moved "
          "(the crashed-escrow refund truth)",
          acc.ref_totals("r:esc") == (-200000, 0, 0),
          f"got {acc.ref_totals('r:esc')}")
    ws = ghost.WatStore(str(tmpd / "wat.json"))
    d1, _ = ws.on_commit("E", "42", "0", "1", "G2S_fromEgm", "house",
                         (100, 0, 0), "0")
    d2, _ = ws.on_commit("E", "42", "0", "1", "G2S_fromEgm", "house",
                         (100, 0, 0), "0")
    d3, r3 = ws.on_commit("E", "42", "0", "1", "G2S_fromEgm", "house",
                          (999, 0, 0), "0")
    check("WatStore.on_commit: an identical §22.31 retry dedupes; a "
          "REUSED transactionId with different facts records FRESH",
          d1 is False and d2 is True and d3 is False
          and r3.get("state") == "committed"
          and r3.get("transCashableAmt") == 999,
          f"got {d1}/{d2}/{d3} r3={r3}")
    r4 = ws.on_authorize("E", "42", "house", (5, 0, 0), 0, False)
    check("WatStore.on_authorize never regresses a commitSeen record "
          "(no resurrection into pendingTransfers)",
          r4 is not None and r4.get("state") == "committed",
          f"got {r4}")

    print("— Step 14: the retired cage is really gone — no hold ledger "
          "movement, no /api/pnl, retired settings keys refused")
    # Machine-take tracking / drift accounting was REMOVED 2026-07-15
    # (collector de-cage). Pin the absence: the replay's whole meter stream
    # moved ZERO House money under a hold: ref, the /api/pnl route is gone
    # (404 body, never a handler crash), and the retired settings keys are
    # strict 400s at the edge (whitelist trim) rather than silent writes.
    acct_snap = get_json(ACCT_URL)
    hold_led = [e for e in acct_snap.get("ledger", [])
                if str(e.get("ref", "")).startswith("hold:")]
    check("no hold: ledger movement during the whole replay (the feature "
          "is gone; historical refs are the only place 'hold:' lives)",
          hold_led == [], f"got {hold_led}")
    _api_base = STATUS_URL.rsplit("/api/status", 1)[0]

    def _post_settings(payload):
        req = urllib.request.Request(
            _api_base + "/api/settings",
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                return resp.status, json.loads(resp.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read())

    try:
        with urllib.request.urlopen(_api_base + "/api/pnl",
                                    timeout=10) as resp:
            pnl_st = resp.status
    except urllib.error.HTTPError as e:
        pnl_st = e.code
    check("/api/pnl route is GONE (404, not a crash)", pnl_st == 404,
          f"got {pnl_st}")
    st, body = _post_settings({"houseHold": True})
    check("retired houseHold key -> 400 at the settings edge",
          st == 400 and body.get("ok") is False, f"{st} {body}")
    st, body = _post_settings({"pnlBaselineReset": True})
    check("retired pnlBaselineReset key -> 400 at the settings edge",
          st == 400 and body.get("ok") is False, f"{st} {body}")
    opts = get_json(STATUS_URL).get("hostOptions") or {}
    check("hostOptions carries NO houseHold (bankroll + allowNegative stay)",
          "houseHold" not in opts and "houseBankrollMc" in opts
          and "houseAllowNegative" in opts, f"got {sorted(opts)}")

    print("— Step 14.2: ticket header (C1-C4) — /api/settings write path, "
          "hostOptions, the SAS reply key + the G2S voucherTextFields "
          "fan-out")
    # (a) hostOptions.ticket is ALWAYS present (the UI's feature-detect
    # key). hub.db persists across gate runs, so RESET the header first
    # (rev is asserted RELATIVELY from here) — then an unset hub answers
    # null fields and a satellite report reply carries NO ticketData: the
    # hub never pushes blank headers over a machine's existing on-glass
    # config (C1).
    t_pre = get_json(STATUS_URL).get("hostOptions", {}).get("ticket")
    check("hostOptions.ticket present (feature-detect key, int rev)",
          isinstance(t_pre, dict) and "propName" in t_pre
          and isinstance(t_pre.get("rev"), int), f"got {t_pre}")
    st, body = _post_settings({"ticketPropName": "", "ticketLine1": "",
                               "ticketLine2": "", "ticketTitleCash": ""})
    rev_base = body.get("ticketRev")
    check("header reset lands (null echoes, a rev bump, EMPTY fan-out — "
          "an unset header never pushes)",
          st == 200 and body.get("ticketPropName") is None
          and body.get("ticketLine1") is None
          and isinstance(rev_base, int)
          and body.get("ticketPush") == [], f"{st} {body}")
    rev_base = rev_base if isinstance(rev_base, int) else 0
    t0 = get_json(STATUS_URL).get("hostOptions", {}).get("ticket", {})
    check("hostOptions.ticket all-null after the reset",
          t0.get("propName") is None and t0.get("line1") is None
          and t0.get("line2") is None and t0.get("titleCash") is None
          and t0.get("rev") == rev_base, f"got {t0}")
    st, body = raw_post("/api/sas/report", json.dumps({
        "smibId": "smib-test", "address": "3", "online": True}))
    check("unset header: report reply carries NO ticketData key",
          st == 200 and "ticketData" not in json.loads(body),
          f"status={st} body={body[:200]}")
    # (b) The EGM advertises its voucher text fields via config-sync (the
    # same arrival form the AVP uses) — the factory G2S_propName "YOUR
    # ESTABLISHMENT" is the whole reason this feature exists. Line2 holds
    # machine-side text the hub does NOT manage (the never-blank proof)
    # and titlePromo is a param the hub never touches (ride-through proof).
    voucher_fixture_params = {
        "G2S_propName": "YOUR ESTABLISHMENT",
        "G2S_propLine1": "",
        "G2S_propLine2": "EXISTING LINE 2",
        "G2S_titleCash": "CASHOUT TICKET",
        "G2S_titlePromo": "PROMO TICKET",
    }

    def voucher_option_list(**overrides):
        vals = dict(voucher_fixture_params, **overrides)
        inner = "".join(
            f'<g2s:stringValue g2s:paramId="{p}">{v}</g2s:stringValue>'
            for p, v in vals.items())
        return (
            '<g2s:optionList>'
            '<g2s:deviceOptions g2s:deviceClass="G2S_voucher" '
            'g2s:deviceId="1">'
            '<g2s:optionGroup g2s:optionGroupId="G2S_voucherOptions" '
            'g2s:optionGroupName="Voucher Options">'
            '<g2s:optionItem g2s:optionId="G2S_voucherTextFields" '
            'g2s:securityLevel="G2S_operator">'
            '<g2s:optionCurrentValues>'
            '<g2s:complexValue g2s:paramId="G2S_voucherTextFields">'
            f'{inner}'
            '</g2s:complexValue>'
            '</g2s:optionCurrentValues>'
            '</g2s:optionItem>'
            '</g2s:optionGroup>'
            '</g2s:deviceOptions>'
            '</g2s:optionList>')
    post_to_host(avp_wrap(avp_class_command(
        "optionConfig", voucher_option_list(), "9620", "9290",
        "G2S_request", ttl="120000")))
    expect_host_post("optionListAck (voucher text fields config-sync)")
    time.sleep(0.3)
    # (c) Strict validation, and validate-ALL-before-write: a junk field
    # anywhere in the POST must leave every tenant untouched (no
    # half-saved header, no rev bump, nothing on the wire).
    st, body = _post_settings({"ticketPropName": 123})
    check("non-string ticketPropName -> 400", st == 400
          and body.get("ok") is False, f"{st} {body}")
    st, body = _post_settings({"ticketPropName": "x" * 65})
    check("65-char ticketPropName -> 400 (the 64-char printed-line bound)",
          st == 400 and body.get("ok") is False, f"{st} {body}")
    st, body = _post_settings({"ticketPropName": "FUNCINO",
                               "ticketLine2": 123})
    t_bad = get_json(STATUS_URL).get("hostOptions", {}).get("ticket", {})
    check("junk in ONE field 400s the whole POST — no half-saved header, "
          "rev unbumped", st == 400 and t_bad.get("propName") is None
          and t_bad.get("rev") == rev_base, f"{st} {body} ticket={t_bad}")
    expect_no_host_post("rejected ticket saves never touch the wire")
    # (d) The real save: propName + line1 + titleCash (line2 deliberately
    # UNSET at the hub). Echo + rev 1 + ONE ok fan-out verdict naming the
    # live assoc and the exact params it edits.
    st, body = _post_settings({"ticketPropName": "FUNCINO",
                               "ticketLine1": "This ain't worth shit",
                               "ticketTitleCash": "CASH TICKET"})
    push = body.get("ticketPush") or []
    check("ticket save lands: echo + a fresh rev bump",
          st == 200 and body.get("ok") is True
          and body.get("ticketPropName") == "FUNCINO"
          and body.get("ticketLine1") == "This ain't worth shit"
          and body.get("ticketTitleCash") == "CASH TICKET"
          and body.get("ticketRev") == rev_base + 1, f"{st} {body}")
    check("the save FANNED OUT to the live G2S assoc (ok verdict, "
          "configId, the 3 edited params — line2 NOT among them)",
          len(push) == 1 and push[0].get("egmId") == EGM_ID
          and push[0].get("ok") is True and push[0].get("configId")
          and push[0].get("params") == ["G2S_propLine1", "G2S_propName",
                                        "G2S_titleCash"], f"got {push}")
    tk_config_id = str(push[0].get("configId")) if push else ""
    # (e) The wire: ONE setOptionChange at the voucher 4-tuple,
    # G2S_immediate, carrying the COMPLETE §9.15 set — hub values for the
    # three managed params, the machine's OWN line2 kept verbatim (unset
    # hub field never blanks it) and the unmanaged titlePromo riding
    # through untouched.
    m = expect_host_post("setOptionChange (ticket header)")
    tk_sid = m["sessionId"] if m else None
    if m:
        det = option_change_details(m["raw"])
        check("setOptionChange addresses G2S_voucher/1/G2S_voucherOptions/"
              "G2S_voucherTextFields with applyCondition=G2S_immediate",
              det and det["option"] == {
                  "deviceClass": "G2S_voucher", "deviceId": "1",
                  "optionGroupId": "G2S_voucherOptions",
                  "optionId": "G2S_voucherTextFields"}
              and det["attrs"].get("configurationId") == tk_config_id
              and det["attrs"].get("applyCondition") == "G2S_immediate",
              f"got {det['option'] if det else None} / "
              f"{det['attrs'] if det else None}")
        check("COMPLETE value set: hub values in, machine line2 KEPT, "
              "titlePromo rides through (C1 never-blank law on the wire)",
              det and det["values"] == {
                  "G2S_propName": "FUNCINO",
                  "G2S_propLine1": "This ain't worth shit",
                  "G2S_propLine2": "EXISTING LINE 2",
                  "G2S_titleCash": "CASH TICKET",
                  "G2S_titlePromo": "PROMO TICKET"},
              f"got {det['values'] if det else None}")
    # Drive the proven cycle to terminal so the record ends verified:
    # pending -> authorize -> authorized -> applied(push) -> read-back.
    if tk_sid:
        post_to_host(avp_wrap(avp_class_command(
            "optionConfig",
            f'<g2s:optionChangeStatus g2s:configurationId="{tk_config_id}" '
            f'g2s:transactionId="66001" g2s:applyCondition="G2S_immediate" '
            f'g2s:disableCondition="G2S_none" g2s:restartAfter="false" '
            f'g2s:changeStatus="G2S_pending" g2s:changeException="0" '
            f'g2s:changeDateTime="{now_iso()}"/>',
            "9621", tk_sid, "G2S_response")))
    m = expect_host_post("authorizeOptionChange (ticket header)")
    tk_auth_sid = m["sessionId"] if m else None
    if tk_auth_sid:
        post_to_host(avp_wrap(avp_class_command(
            "optionConfig",
            f'<g2s:optionChangeStatus g2s:configurationId="{tk_config_id}" '
            f'g2s:transactionId="66001" g2s:applyCondition="G2S_immediate" '
            f'g2s:disableCondition="G2S_none" g2s:restartAfter="false" '
            f'g2s:changeStatus="G2S_authorized" g2s:changeException="0" '
            f'g2s:changeDateTime="{now_iso()}"/>',
            "9622", tk_auth_sid, "G2S_response")))
    post_to_host(avp_wrap(avp_class_command(
        "optionConfig",
        f'<g2s:optionChangeStatus g2s:configurationId="{tk_config_id}" '
        f'g2s:transactionId="66001" g2s:applyCondition="G2S_immediate" '
        f'g2s:disableCondition="G2S_none" g2s:restartAfter="false" '
        f'g2s:changeStatus="G2S_applied" g2s:changeException="0" '
        f'g2s:changeDateTime="{now_iso()}"/>',
        "9623", "9293", "G2S_request")))
    expect_host_post("optionChangeStatusAck (ticket header applied)")
    m = expect_host_post("getOptionList (ticket header read-back)")
    tk_rb_sid = m["sessionId"] if m else None
    if tk_rb_sid:
        post_to_host(avp_wrap(avp_class_command(
            "optionConfig",
            voucher_option_list(
                G2S_propName="FUNCINO",
                G2S_propLine1="This ain't worth shit",
                G2S_titleCash="CASH TICKET"),
            "9624", tk_rb_sid, "G2S_response")))
        time.sleep(0.3)
        egm_tk = get_json(STATUS_URL).get(EGM_ID, {})
        check("ticket-header change VERIFIED against the read-back "
              "(stage applied, result verified)",
              egm_tk.get("optionConfigStage") == "applied"
              and egm_tk.get("optionChange", {}).get("result") == "verified"
              and egm_tk.get("optionChange", {}).get("optionId")
              == "G2S_voucherTextFields",
              f"got stage={egm_tk.get('optionConfigStage')} "
              f"oc={egm_tk.get('optionChange')}")
    # (f) C4/C2 after the save: hostOptions carries the header (line2
    # null — never set), the satellite reply carries ticketData (line2 ""
    # per the fixed reply shape, titleCash set, rev 1), and a satellite's
    # applied-state round-trips into /api/status clamped.
    t1 = get_json(STATUS_URL).get("hostOptions", {}).get("ticket", {})
    check("hostOptions.ticket carries the saved header (line2 null)",
          t1.get("propName") == "FUNCINO"
          and t1.get("line1") == "This ain't worth shit"
          and t1.get("line2") is None
          and t1.get("titleCash") == "CASH TICKET"
          and t1.get("rev") == rev_base + 1, f"got {t1}")
    st, body = raw_post("/api/sas/report", json.dumps({
        "smibId": "smib-test", "address": "3", "online": True,
        "ticketData": {"appliedRev": 1, "detail": "0x7D applied"}}))
    rep = json.loads(body) if st == 200 else {}
    check("report reply carries ticketData (C2 shape: line2 '' when "
          "unset, titleCash set, the bumped rev)",
          st == 200 and rep.get("ticketData", {}).get("propName")
          == "FUNCINO"
          and rep.get("ticketData", {}).get("line1")
          == "This ain't worth shit"
          and rep.get("ticketData", {}).get("line2") == ""
          and rep.get("ticketData", {}).get("titleCash") == "CASH TICKET"
          and rep.get("ticketData", {}).get("rev") == rev_base + 1,
          f"status={st} got {rep.get('ticketData')}")
    sas_tk = get_json(STATUS_URL).get("sas", {}).get("smib-test/3", {})
    check("satellite appliedRev/detail round-trip into /api/status "
          "(the UI's pending-note anchor)",
          sas_tk.get("ticketData", {}).get("appliedRev") == 1
          and sas_tk.get("ticketData", {}).get("detail") == "0x7D applied",
          f"got {sas_tk.get('ticketData')}")
    st, _b = raw_post("/api/sas/report", json.dumps({
        "smibId": "smib-test", "address": "3", "online": True,
        "ticketData": {"appliedRev": "boom", "detail": {"x": 1}}}))
    sas_tk = get_json(STATUS_URL).get("sas", {}).get("smib-test/3", {})
    check("junk appliedRev clamps to null (attacker-typed wire, status "
          "stays parseable)",
          st == 200 and sas_tk.get("ticketData", {}).get("appliedRev")
          is None, f"status={st} got {sas_tk.get('ticketData')}")
    # (g) Clearing the prop name UN-GATES the push: rev bumps (satellites
    # see it), the fan-out is an empty no-op (nothing on the wire — C1),
    # the reply drops ticketData and hostOptions reads unset again.
    st, body = _post_settings({"ticketPropName": ""})
    check("clear lands: null echo, another rev bump, EMPTY fan-out",
          st == 200 and body.get("ticketPropName") is None
          and body.get("ticketRev") == rev_base + 2
          and body.get("ticketPush") == [], f"{st} {body}")
    expect_no_host_post("a cleared header never pushes an option change")
    st, body = raw_post("/api/sas/report", json.dumps({
        "smibId": "smib-test", "address": "3", "online": True}))
    t2 = get_json(STATUS_URL).get("hostOptions", {}).get("ticket", {})
    check("cleared header: reply drops ticketData; hostOptions propName "
          "null again (the rev bump is durable)",
          st == 200 and "ticketData" not in json.loads(body)
          and t2.get("propName") is None and t2.get("rev") == rev_base + 2,
          f"status={st} ticket={t2}")

    # ================================================= mediaDisplay — #18 P3
    # The igtMediaDisplay probe scaffold. An IGT EXTENSION class, so the
    # wire shape is governed by the twice-paid-for laws: the class element
    # carries the EXTENSION prefix with local name mediaDisplay (never
    # <g2s:mediaDisplay> — the MSX004 gSOAP retry loop; never the
    # deviceClass string IGT_mediaDisplay as a tag), structural attributes
    # stay g2s-prefixed, command attributes carry the extension prefix,
    # and every id is pure-numeric (MSX005). DORMANCY IS A GATE: the whole
    # standard suite above ran with zero probes fired, so not one byte of
    # mediaDisplay may exist on the wire before this line.

    print("— Step 6.998: mediaDisplay probe scaffold (#18 P3) — dormancy "
          "gate, wire-shape law, folds, glass ping")
    pre_probe_wire = list(WIRE_TAPE)
    check("DORMANCY: zero igtMediaDisplay bytes across the whole session's "
          f"{len(pre_probe_wire)} host POSTs so far (nothing at join, "
          "nothing in SPOKEN_CLASSES, nothing in the standard exchanges)",
          not any("igtMediaDisplay" in b for b in pre_probe_wire),
          "found igtMediaDisplay in pre-probe wire traffic")
    # (a) bogus rung -> clean error dict at 200 (the actions idiom), and
    # NOTHING on the wire; ownership-blind default -> honest error, never
    # a blind deviceId=1.
    cs, cbody = post_command({"egmId": EGM_ID, "action": "mediaDisplayProbe",
                              "rung": "summonJackpot"})
    check("bogus rung -> clean ok:false error dict (HTTP 200, never a 500)",
          cs == 200 and cbody.get("ok") is False
          and "rung" in str(cbody.get("error", "")), f"got {cs}/{cbody}")
    expect_no_host_post("the bogus-rung probe")
    cs, cbody = post_command({"egmId": EGM_ID, "action": "mediaDisplayProbe",
                              "rung": "status"})
    check("status rung with NO ownership revealed -> honest error dict "
          "(never a blind default device)",
          cs == 200 and cbody.get("ok") is False
          and "owned" in str(cbody.get("error", "")), f"got {cs}/{cbody}")
    expect_no_host_post("the ownership-blind probe")
    # (b) reveal ownership with the REAL first-light commHostList (host 1
    # owns IGT_mediaDisplay/1..6 on the live AVP), then the status rung
    # picks the LOWEST owned device.
    post_to_host(avp_wrap(first_light_reply(
        "commHostList_sid1004.xml", "9901")))
    time.sleep(0.3)
    cs, cbody = post_command({"egmId": EGM_ID, "action": "mediaDisplayProbe",
                              "rung": "status"})
    check("status rung after the commHostList reveal — picks the LOWEST "
          "owned IGT_mediaDisplay device (1 of the live AVP's 1..6)",
          cs == 200 and cbody.get("ok") is True
          and cbody.get("rung") == "status" and cbody.get("deviceId") == "1"
          and cbody.get("ownedMediaDisplays", [])[:3] == ["1", "2", "3"],
          f"got {cs}/{cbody}")
    m = expect_host_post("getMediaDisplayStatus")
    md_sid = m["sessionId"] if m else "1001"
    if m:
        wire_inner = html.unescape(m["raw"])
        check("wire-shape law #1: <igtMediaDisplay:mediaDisplay ...> — "
              "never <g2s:mediaDisplay>, never the deviceClass string "
              "IGT_mediaDisplay as the tag (the MSX004 prefix trap)",
              "<igtMediaDisplay:mediaDisplay " in wire_inner
              and "<g2s:mediaDisplay" not in wire_inner
              and "<igtMediaDisplay:IGT_mediaDisplay" not in wire_inner,
              wire_inner[:400])
        check("wire-shape law: BOTH namespaces declared on g2sMessage; "
              "structural attrs g2s-prefixed; EMPTY adjudicator element "
              "(ttl=30000 G2S_request)",
              'xmlns:igtMediaDisplay="http://g2s.igt.com/mediaDisplay/'
              'v1.0.0"' in wire_inner
              and f'xmlns:g2s="{SCHEMA_NS}"' in wire_inner
              and 'g2s:deviceId="1"' in wire_inner
              and "<igtMediaDisplay:getMediaDisplayStatus/>" in wire_inner
              and m["class"] == "mediaDisplay"
              and m["command"] == "getMediaDisplayStatus"
              and m["sessionType"] == "G2S_request"
              and m["timeToLive"] == "30000"
              and m.get("commandAttrs", {}) == {},
              f"got {m.get('class')}.{m.get('command')} "
              f"attrs={m.get('commandAttrs')} inner={wire_inner[:300]}")
    # (c) the EGM answers in ITS extension namespace -> the fold captures
    # the attribute census verbatim under /api/status mediaDisplay.

    def md_response(command_xml, command_id, session_id, device_id="1",
                    session_type="G2S_response"):
        ts = now_iso()
        return (
            f'<g2s:g2sMessage xmlns:g2s="{SCHEMA_NS}" '
            f'xmlns:igtMediaDisplay="http://g2s.igt.com/mediaDisplay/'
            f'v1.0.0">\n'
            f'   <g2s:g2sBody g2s:hostId="1" g2s:egmId="{EGM_ID}" '
            f'g2s:dateTimeSent="{ts}">\n'
            f'      <igtMediaDisplay:mediaDisplay g2s:deviceId="{device_id}" '
            f'g2s:dateTime="{ts}" g2s:commandId="{command_id}" '
            f'g2s:sessionType="{session_type}" g2s:sessionId="{session_id}" '
            f'g2s:timeToLive="30000">\n'
            f"         {command_xml}\n"
            f"      </igtMediaDisplay:mediaDisplay>\n"
            f"   </g2s:g2sBody>\n"
            f"</g2s:g2sMessage>"
        )

    post_to_host(avp_wrap(md_response(
        '<igtMediaDisplay:mediaDisplayStatus '
        'igtMediaDisplay:egmEnabled="false" '
        'igtMediaDisplay:hostEnabled="true" '
        'igtMediaDisplay:displayState="IGT_hidden"/>',
        "701", md_sid)))
    time.sleep(0.3)
    md = get_json(STATUS_URL).get(EGM_ID, {}).get("mediaDisplay", {})
    check("status: mediaDisplay fold — attribute census verbatim under "
          "dev 1 (egmEnabled=false, the recon's expected blocker) with "
          "lastCommand + lastSeen",
          md.get("1", {}).get("mediaDisplayStatus", {}).get("egmEnabled")
          == "false"
          and md.get("1", {}).get("mediaDisplayStatus", {})
          .get("displayState") == "IGT_hidden"
          and md.get("1", {}).get("lastCommand") == "mediaDisplayStatus"
          and bool(md.get("1", {}).get("lastSeen")), f"got {md}")
    # (d) loadContent: host-allocated PURE-NUMERIC contentId, the default
    # hello.html uri, igtMediaDisplay-prefixed command attributes.
    cs, cbody = post_command({"egmId": EGM_ID, "action": "mediaDisplayProbe",
                              "rung": "load", "deviceId": "3"})
    md_cid = str(cbody.get("contentId", ""))
    check("load rung — host-allocated PURE-NUMERIC contentId (MSX005 law), "
          "the default hello.html uri, explicit deviceId honored",
          cs == 200 and md_cid.isdigit()
          and cbody.get("uri", "").endswith("/webui/hello.html")
          and cbody.get("deviceId") == "3", f"got {cs}/{cbody}")
    m = expect_host_post("loadContent")
    if m:
        wire_inner = html.unescape(m["raw"])
        a = m.get("commandAttrs", {})
        check("loadContent attrs igtMediaDisplay-prefixed on the wire "
              "(law #3): numeric contentId + the default uri, and the "
              "attribute is mediaURI (capital URI — the casing that made "
              "the AVP default to example.swf when we sent 'mediaUri')",
              m["class"] == "mediaDisplay" and m["command"] == "loadContent"
              and m["deviceId"] == "3" and a.get("contentId") == md_cid
              and a.get("mediaURI")
              == "http://192.168.50.2:8081/webui/hello.html"
              and f'igtMediaDisplay:contentId="{md_cid}"' in wire_inner
              and 'igtMediaDisplay:mediaURI="http://192.168.50.2:8081/'
                  'webui/hello.html"' in wire_inner
              and 'mediaUri="' not in wire_inner,
              f"got {a} inner={wire_inner[:300]}")
    cs, cbody = post_command({"egmId": EGM_ID, "action": "mediaDisplayProbe",
                              "rung": "load", "deviceId": "3",
                              "contentId": "77",
                              "uri": "http://192.168.50.2:8081/webui/"
                                     "glass_ping.html?a=1&b=2"})
    check("load rung with explicit contentId + query-string uri accepted",
          cs == 200 and cbody.get("contentId") == "77", f"got {cs}/{cbody}")
    m = expect_host_post("loadContent (explicit contentId + uri)")
    if m:
        wire_inner = html.unescape(m["raw"])
        check("explicit contentId honored; the uri html-ESCAPED inside the "
              "wire attribute (& -> &amp;), mediaURI capital-URI",
              m.get("commandAttrs", {}).get("contentId") == "77"
              and m.get("commandAttrs", {}).get("mediaURI", "")
              .endswith("glass_ping.html?a=1&b=2")
              and 'igtMediaDisplay:mediaURI="http://192.168.50.2:8081/'
                  'webui/glass_ping.html?a=1&amp;b=2"' in wire_inner,
              f"got {m.get('commandAttrs')} inner={wire_inner[:300]}")
    # (e) show = showMediaDisplay, an EMPTY device verb (the recon's
    # 'showContent' with a contentId attribute NEVER existed in the schema —
    # that was the MSX004). No contentId, no transactionId.
    cs, cbody = post_command({"egmId": EGM_ID, "action": "mediaDisplayProbe",
                              "rung": "show", "deviceId": "3"})
    check("show rung accepted (showMediaDisplay is an empty device verb)",
          cs == 200 and cbody.get("deviceId") == "3", f"got {cs}/{cbody}")
    m = expect_host_post("showMediaDisplay")
    if m:
        check("show emits <igtMediaDisplay:showMediaDisplay/> — empty "
              "element, the REAL display verb (not the fictional "
              "showContent), zero command attributes",
              m["command"] == "showMediaDisplay"
              and m.get("commandAttrs", {}) == {},
              f"got {m.get('command')}/{m.get('commandAttrs')}")
    # (e2) a transaction verb without a captured/explicit transactionId is
    # refused at 200 (nothing on the wire); WITH an explicit one it carries
    # g2s:transactionId + igtMediaDisplay:contentId.
    cs, cbody = post_command({"egmId": EGM_ID, "action": "mediaDisplayProbe",
                              "rung": "setactive", "deviceId": "3",
                              "contentId": "77"})
    check("setactive with no captured transactionId -> ok:false at 200 "
          "(the c_baseTransaction txn is required, none seen yet)",
          cs == 200 and cbody.get("ok") is False, f"got {cs}/{cbody}")
    expect_no_host_post("setactive without a transactionId")
    cs, cbody = post_command({"egmId": EGM_ID, "action": "mediaDisplayProbe",
                              "rung": "setactive", "deviceId": "3",
                              "contentId": "77", "transactionId": "9"})
    check("setactive with explicit transactionId accepted",
          cs == 200 and cbody.get("transactionId") == "9"
          and cbody.get("contentId") == "77", f"got {cs}/{cbody}")
    m = expect_host_post("setActiveContent")
    if m:
        wire_inner = html.unescape(m["raw"])
        check("setActiveContent carries g2s:transactionId (inherited, "
              "g2s-qualified) + igtMediaDisplay:contentId",
              m["command"] == "setActiveContent"
              and m.get("commandAttrs", {}).get("transactionId") == "9"
              and m.get("commandAttrs", {}).get("contentId") == "77"
              and 'g2s:transactionId="9"' in wire_inner
              and 'igtMediaDisplay:contentId="77"' in wire_inner,
              f"got {m.get('commandAttrs')} inner={wire_inner[:300]}")
    # (e3) logstatus = getContentLogStatus, empty read (the safe first probe)
    cs, cbody = post_command({"egmId": EGM_ID, "action": "mediaDisplayProbe",
                              "rung": "logstatus", "deviceId": "3"})
    check("logstatus rung accepted (getContentLogStatus empty read)",
          cs == 200 and cbody.get("deviceId") == "3", f"got {cs}/{cbody}")
    m = expect_host_post("getContentLogStatus")
    if m:
        check("getContentLogStatus is an empty element, zero attrs",
              m["command"] == "getContentLogStatus"
              and m.get("commandAttrs", {}) == {},
              f"got {m.get('command')}/{m.get('commandAttrs')}")
    cs, cbody = post_command_err(
        {"egmId": EGM_ID, "action": "mediaDisplayProbe",
         "rung": "contentstatus", "deviceId": "3", "contentId": "DEMO7"})
    check("non-numeric contentId -> HTTP 400 (the MSX005 id law enforced "
          "at the API edge)",
          cs == 400 and cbody.get("ok") is False, f"got {cs}/{cbody}")
    expect_no_host_post("the junk-contentId probe")
    # (f) /api/glass/ping — the path-A interactivity probe's backend.
    st_gp, ct_gp, body_gp = raw_get("/api/glass/ping?src=replay")
    try:
        gp = json.loads(body_gp)
    except ValueError:
        gp = {}
    check("/api/glass/ping answers 200 JSON {ok, pong, src echoed}",
          st_gp == 200 and "json" in ct_gp and gp.get("ok") is True
          and bool(gp.get("pong")) and gp.get("src") == "replay",
          f"got {st_gp}/{ct_gp}/{body_gp[:200]}")
    # (g) the probe pages serve, worse-of-fleet clean: ES5 only (no
    # const/let/arrow/fetch — the AVP's built-in browser is the floor).
    st_h, ct_h, body_h = raw_get("/webui/hello.html")
    check("hello.html served — gameroom-branded glass page (serve-time "
          "{{GAMEROOM}} substitution, neutral fallback), ES5-only inline "
          "script",
          st_h == 200 and "html" in ct_h and "GAME ROOM" in body_h
          and "{{GAMEROOM}}" not in body_h
          and "IT'S ALIVE" in body_h and "XMLHttpRequest" not in body_h
          and "=>" not in body_h and "const " not in body_h
          and "let " not in body_h and "fetch(" not in body_h,
          f"got {st_h}/{ct_h}")
    st_g, ct_g, body_g = raw_get("/webui/glass_ping.html")
    check("glass_ping.html served — the big-button XMLHttpRequest to "
          "/api/glass/ping, ES5 only",
          st_g == 200 and "html" in ct_g and "XMLHttpRequest" in body_g
          and "/api/glass/ping" in body_g
          and "=>" not in body_g and "const " not in body_g
          and "let " not in body_g and "fetch(" not in body_g,
          f"got {st_g}/{ct_g}")

    # ================================================ glass navigation — #18 P4
    # The resident-SPA tier over the probe scaffold above: ONE glassShow
    # pushes webui/glass.html onto the Service Window, the page then POLLS
    # /api/glass/state (~1.5s) and every screen change after that is a
    # client-side view flip — RFID taps mutate HUB-side session state and
    # the next poll flips the glass. The contract's assertions live here:
    # the state shapes (attract/carded), the token discipline (401 unknown
    # sess / 403 off-allowlist / logout = THE shared card-out), the
    # content-push sequencer advancing on the EGM's OWN contentStatus
    # narration (contentId-MATCHED — release chatter is inert), the card-IN
    # recovery push + its 60s throttle, and the inert rule (the SPA is
    # never pushed before an explicit trigger).

    print("— Step 6.999: glass navigation v1 (#18 P4) — state shapes, "
          "session tokens, sequencer, recovery + throttle")
    check("INERT: zero glass.html content pushes anywhere on the wire "
          "before the first card-IN (join + the whole standard suite "
          "stayed SPA-silent — glassShow fires only on demand)",
          not any("glass.html" in b for b in list(WIRE_TAPE)),
          "found a glass.html push in pre-tap wire traffic")
    # (a) the SPA serves, worse-of-fleet clean (the hello.html treatment):
    # ES5 only for the QNX/WebKit-534 cabinet browser, wired to exactly the
    # two glass endpoints, and painting the collector's GAMEROOM name in
    # lights (s.gameroom off the state poll; "GAME ROOM" neutral fallback).
    st_sp, ct_sp, body_sp = raw_get("/webui/glass.html")
    check("glass.html served — resident SPA, polls /api/glass/state "
          "+ posts /api/glass/action, gameroom-branded, ES5-only (no const/"
          "let/arrow/fetch/backtick/classList)",
          st_sp == 200 and "html" in ct_sp
          and "gameroom" in body_sp and "GAME ROOM" in body_sp
          and "/api/glass/state" in body_sp
          and "/api/glass/action" in body_sp
          and "XMLHttpRequest" in body_sp
          and "=>" not in body_sp and "fetch(" not in body_sp
          and "`" not in body_sp and "classList" not in body_sp
          # word-boundary so the CASH OUT view's "walLET"/"conST"-free copy
          # can't false-trip a naive substring (the wallet UI added "wallet")
          and re.search(r"\b(?:let|const)\b", body_sp) is None,
          f"got {st_sp}/{ct_sp}")
    # (a2) the SMIB panel page serves — the BB2 satellite's EXTERNAL-screen
    # player UI (1024x600 landscape, glass.html re-laid). Same endpoints,
    # but its poll self-identifies as src=smib and NEVER as the resident
    # SPA (that brand stamps AVP residency + advances the sequencer).
    st_sm, ct_sm, body_sm = raw_get("/webui/smib.html")
    check("smib.html served — SMIB panel page: polls "
          "/api/glass/state with src=smib (never the spa brand), posts "
          "/api/glass/action, gameroom-branded, backtick-free",
          st_sm == 200 and "html" in ct_sm
          and "gameroom" in body_sm and "GAME ROOM" in body_sm
          and "/api/glass/state" in body_sm
          and "/api/glass/action" in body_sm
          and "src=smib" in body_sm
          and "src=spa" not in body_sm
          and "`" not in body_sm,
          f"got {st_sm}/{ct_sm}")
    # (b) state shapes — attract + unknown egm, both HTTP 200 (the
    # never-500 posture: the poll loop the whole glass UI hangs off must
    # never have to special-case an HTTP error).
    st_gs, ct_gs, body_gs = raw_get(f"/api/glass/state?egm={EGM_ID}")
    try:
        gs = json.loads(body_gs)
    except ValueError:
        gs = {}
    check("state (attract): ok/egmId/nick/carded=false + int uiBuild (the "
          "glass.html mtime — the page's self-reload watermark), and NO "
          "sess token while nobody is carded in",
          st_gs == 200 and "json" in ct_gs and gs.get("ok") is True
          and gs.get("egmId") == EGM_ID and isinstance(gs.get("nick"), str)
          and gs.get("carded") is False
          and isinstance(gs.get("uiBuild"), int) and gs.get("uiBuild") > 0
          and "sess" not in gs and "tier" not in gs,
          f"got {st_gs}/{body_gs[:200]}")
    st_gu, _, body_gu = raw_get("/api/glass/state?egm=IGT_00NOSUCHEGM")
    try:
        gu = json.loads(body_gu)
    except ValueError:
        gu = {}
    check("state (unknown egm): HTTP 200 with ok:false 'unknown egm'",
          st_gu == 200 and gu.get("ok") is False
          and gu.get("error") == "unknown egm",
          f"got {st_gu}/{body_gu[:120]}")
    # (b2) the SMIB panel's poll brand: src=smib answers ok:true exactly
    # like a plain peek — none of the spa-only residency/sequencer
    # machinery is reachable from it (g2s_host gates on src == "spa").
    st_sb, ct_sb, body_sb = raw_get(f"/api/glass/state?egm={EGM_ID}&src=smib")
    try:
        sb = json.loads(body_sb)
    except ValueError:
        sb = {}
    check("state with src=smib (the BB2 SMIB panel's poll) answers ok:true "
          "with the same attract shape as a plain peek",
          st_sb == 200 and "json" in ct_sb and sb.get("ok") is True
          and sb.get("egmId") == EGM_ID and sb.get("carded") is False,
          f"got {st_sb}/{body_sb[:200]}")
    # (c) action edges with NO session minted anywhere: unknown sess -> 401,
    # junk body -> 400 — never a 500.
    st_ga, body_ga = raw_post("/api/glass/action", json.dumps(
        {"sess": "gs_deadbeefdeadbeefdeadbeef", "action": "logout"}))
    check("action with a garbage sess -> 401 {ok:false 'unknown session'}",
          st_ga == 401 and json.loads(body_ga).get("ok") is False
          and json.loads(body_ga).get("error") == "unknown session",
          f"got {st_ga}/{body_ga[:120]}")
    st_gb, body_gb = raw_post("/api/glass/action", "not json {{{")
    check("action with junk JSON -> 400, never a 500",
          st_gb == 400 and json.loads(body_gb).get("ok") is False,
          f"got {st_gb}/{body_gb[:120]}")
    # (d) a synthetic card-IN: register a player fob, then a Companion tap
    # report — the SAME ingest the PN532 daemon drives (RFID Phase 1).
    st_f, body_f = raw_post("/api/fobs", json.dumps(
        {"action": "set", "uid": "A1B2C3D401", "tier": "player",
         "label": "Replay Player"}))
    check("player fob registered via POST /api/fobs",
          st_f == 200 and json.loads(body_f).get("ok") is True,
          f"got {st_f}/{body_f[:120]}")
    glass_comp_started = now_iso()

    def companion_tap(tap_id, uid="A1B2C3D401"):
        return raw_post("/api/companion/report", json.dumps(
            {"companionId": "comp-replay", "startedAt": glass_comp_started,
             "readerOk": True, "g2sEgmId": EGM_ID,
             "taps": [{"tapId": tap_id, "uid": uid, "at": now_iso()}]}))

    st_t, body_t = companion_tap(1)
    check("companion tap report accepted (ackTapId advances to 1)",
          st_t == 200 and json.loads(body_t).get("ackTapId") == 1,
          f"got {st_t}/{body_t[:120]}")
    m = expect_host_post("card-IN setIdValidation")
    if m:
        a = m.get("commandAttrs", {})
        check("card-IN -> idReader.setIdValidation on the LOWEST owned "
              "reader (dev 1): host-sourced ACTIVE identity for the fob",
              m["class"] == "idReader" and m["command"] == "setIdValidation"
              and m["deviceId"] == "1" and a.get("idNumber") == "A1B2C3D401"
              and a.get("idState") == "G2S_active"
              and a.get("idPreferName") == "Replay Player",
              f"got {m.get('class')}.{m.get('command')} "
              f"dev={m.get('deviceId')} attrs={a}")
    m = expect_host_post("card-IN recovery glassShow loadContent")
    gl_cnt = ""
    if m:
        a = m.get("commandAttrs", {})
        gl_cnt = str(a.get("contentId") or "")
        check("card-IN recovery: ONE glassShow auto-fires (loadContent on "
              "dev 1, no manual /api/command) — SPA uri carries ?egm=&dev= "
              "and a pure-numeric contentId (MSX005)",
              m["class"] == "mediaDisplay" and m["command"] == "loadContent"
              and m["deviceId"] == "1" and gl_cnt.isdigit()
              and a.get("mediaURI") == "http://192.168.50.2:8081/webui/"
                                       f"glass.html?egm={EGM_ID}&dev=1",
              f"got {a}")
    # (e) the carded state shape + token discipline at the status endpoint
    st_gc, _, body_gc = raw_get(f"/api/glass/state?egm={EGM_ID}")
    try:
        gc = json.loads(body_gc)
    except ValueError:
        gc = {}
    glass_tok = str(gc.get("sess") or "")
    check("state (carded): sess = gs_ + 24 hex, tier=player, the fob "
          "label as name, sinceIso + uiBuild present",
          st_gc == 200 and gc.get("ok") is True and gc.get("carded") is True
          and re.fullmatch(r"gs_[0-9a-f]{24}", glass_tok) is not None
          and gc.get("tier") == "player"
          and gc.get("name") == "Replay Player" and bool(gc.get("sinceIso"))
          and isinstance(gc.get("uiBuild"), int),
          f"got {st_gc}/{body_gc[:250]}")
    _, _, body_gc2 = raw_get(f"/api/glass/state?egm={EGM_ID}")
    check("polling is read-only: a second state poll returns the SAME "
          "token (minted once at card-IN, never per poll)",
          json.loads(body_gc2).get("sess") == glass_tok,
          f"got {body_gc2[:200]}")
    snap_g = get_json(STATUS_URL).get("glassSessions", {})
    check("/api/status glassSessions: count=1 + tier/name per EGM and NO "
          "token value ever leaks into the wide-audience endpoint",
          snap_g.get("count") == 1
          and snap_g.get("byEgm", {}).get(EGM_ID, {}).get("tier") == "player"
          and glass_tok not in json.dumps(snap_g), f"got {snap_g}")
    # (f) the sequencer advances ONLY on the EGM's own narration for THIS
    # contentId — a stray contentStatus (e.g. the old content's release
    # chatter) is captured but advances NOTHING.
    post_to_host(avp_wrap(md_response(
        '<igtMediaDisplay:contentStatus '
        'igtMediaDisplay:contentId="424242" '
        'igtMediaDisplay:contentState="IGT_contentPending" '
        'g2s:transactionId="500"/>',
        "702", "88801", session_type="G2S_notification")))
    expect_no_host_post("a contentStatus whose contentId does NOT match "
                        "the pending glass push (release narration is "
                        "inert to the sequencer)")
    post_to_host(avp_wrap(md_response(
        f'<igtMediaDisplay:contentStatus '
        f'igtMediaDisplay:contentId="{gl_cnt}" '
        f'igtMediaDisplay:contentState="IGT_contentPending" '
        f'g2s:transactionId="601"/>',
        "703", "88802", session_type="G2S_notification")))
    # BENCH LESSON (2026-07-10 live AVP): pending is TOO EARLY — the EGM
    # answers IGT_MDX005 "Content not loaded." until its browser has
    # fetched the page. Pending only RECORDS the txn; the advance fires
    # from the resident SPA's own state poll (fetch-proof by definition).
    expect_no_host_post("contentStatus(pending) alone — the sequencer "
                        "must NOT fire setActiveContent yet (the MDX005 "
                        "bench lesson)")
    raw_get(f"/api/glass/state?egm={EGM_ID}")   # a PLAIN state peek (hub
    # UI / bench) — proves nothing about the cabinet browser, no advance
    expect_no_host_post("a plain (no src=spa) state peek — only the "
                        "SPA's own self-identifying poll is fetch-proof")
    raw_get(f"/api/glass/state?egm={EGM_ID}&src=smib")   # the SMIB panel's
    # poll brand — an external screen, NOT the cabinet browser
    expect_no_host_post("a src=smib state poll — the SMIB panel is not "
                        "fetch-proof; only the SPA's own poll advances "
                        "the sequencer")
    raw_get(f"/api/glass/state?egm={EGM_ID}&src=spa&dev=1")   # the page's
    # own self-identifying poll
    m = expect_host_post("poll-driven auto-setActiveContent")
    if m:
        a = m.get("commandAttrs", {})
        check("state poll after pending -> auto setActiveContent echoing "
              "the txn the EGM assigned THIS content (601 — never a "
              "stale media_txn)",
              m["command"] == "setActiveContent" and m["deviceId"] == "1"
              and a.get("transactionId") == "601"
              and a.get("contentId") == gl_cnt, f"got {a}")
    raw_get(f"/api/glass/state?egm={EGM_ID}&src=spa&dev=1")  # immediate
    expect_no_host_post("an immediate second spa poll — GLASS_RETRY_"
                        "SPACING_SEC suppresses a duplicate "
                        "setActiveContent")
    post_to_host(avp_wrap(md_response(
        f'<igtMediaDisplay:contentStatus '
        f'igtMediaDisplay:contentId="{gl_cnt}" '
        f'igtMediaDisplay:contentState="IGT_contentExecuting" '
        f'g2s:transactionId="601"/>',
        "704", "88803", session_type="G2S_notification")))
    m = expect_host_post("sequencer auto-showMediaDisplay")
    if m:
        check("contentStatus(executing) -> auto showMediaDisplay "
              "(empty verb, zero attrs)",
              m["command"] == "showMediaDisplay"
              and m.get("commandAttrs", {}) == {},
              f"got {m.get('command')}/{m.get('commandAttrs')}")
    post_to_host(avp_wrap(md_response(
        f'<igtMediaDisplay:mediaDisplayAck '
        f'igtMediaDisplay:contentId="{gl_cnt}" g2s:transactionId="601"/>',
        "705", "88804", session_type="G2S_notification")))
    expect_no_host_post("the show's mediaDisplayAck (stage parks at "
                        "'shown' — the sequencer is done, nothing more "
                        "auto-fires)")
    # (g) tier allowlist: a stub action with a VALID token -> honest 403
    # (the page's stubs send nothing; a future page that does is refused,
    # never 500'd)
    st_g4, body_g4 = raw_post("/api/glass/action", json.dumps(
        {"sess": glass_tok, "action": "wallet"}))
    check("an action outside the tier allowlist -> 403 {'not yet'}",
          st_g4 == 403 and json.loads(body_g4).get("error") == "not yet",
          f"got {st_g4}/{body_g4[:120]}")
    # (h) logout: THE shared card-out (the same path a physical re-tap
    # runs), egmId from the TOKEN record, token dead afterwards
    st_g5, body_g5 = raw_post("/api/glass/action", json.dumps(
        {"sess": glass_tok, "action": "logout"}))
    try:
        g5 = json.loads(body_g5)
    except ValueError:
        g5 = {}
    check("logout -> 200 {ok, action, egmId from the token record, name}",
          st_g5 == 200 and g5.get("ok") is True
          and g5.get("action") == "logout" and g5.get("egmId") == EGM_ID
          and g5.get("name") == "Replay Player",
          f"got {st_g5}/{body_g5[:160]}")
    m = expect_host_post("glass-logout card-OUT setIdValidation")
    if m:
        a = m.get("commandAttrs", {})
        check("glass logout runs THE shared card-out: setIdValidation "
              "present=false (idState G2S_inactive) on the same reader",
              m["command"] == "setIdValidation" and m["deviceId"] == "1"
              and a.get("idState") == "G2S_inactive", f"got {a}")
    # GLASS_FOLLOW_CARD (AJ 2026-07-10): nobody carded in -> the scale
    # window hides so the game plays FULL SCREEN
    m = expect_host_post("follow-card hideMediaDisplay after logout")
    if m:
        check("card-out hides the resident menu window (bare "
              "hideMediaDisplay after the setIdValidation)",
              m["command"] == "hideMediaDisplay" and m["deviceId"] == "1"
              and m.get("commandAttrs", {}) == {},
              f"got {m.get('command')}/{m.get('commandAttrs')}")
    _, _, body_g6 = raw_get(f"/api/glass/state?egm={EGM_ID}")
    check("state after logout: carded=false, and the card session is gone "
          "from /api/status cardSessions too",
          json.loads(body_g6).get("carded") is False
          and EGM_ID not in get_json(STATUS_URL).get("cardSessions", {}),
          f"got {body_g6[:160]}")
    st_g7, body_g7 = raw_post("/api/glass/action", json.dumps(
        {"sess": glass_tok, "action": "logout"}))
    check("the token died with the logout: replaying it -> 401",
          st_g7 == 401, f"got {st_g7}/{body_g7[:120]}")
    # (i) residency gate: with the SPA 'shown' on dev 1, a fresh card-IN
    # re-mints a session but does NOT re-push content
    companion_tap(2)
    m = expect_host_post("re-card-IN setIdValidation")
    if m:
        check("re-tap card-IN accepted (setIdValidation active again)",
              m["command"] == "setIdValidation"
              and m.get("commandAttrs", {}).get("idState") == "G2S_active",
              f"got {m.get('command')}/{m.get('commandAttrs', {})}")
    # GLASS_FOLLOW_CARD: the carded player's menu window comes back
    m = expect_host_post("follow-card showMediaDisplay at card-IN")
    if m:
        check("card-IN shows the resident menu window (bare "
              "showMediaDisplay — NOT a content push)",
              m["command"] == "showMediaDisplay" and m["deviceId"] == "1"
              and m.get("commandAttrs", {}) == {},
              f"got {m.get('command')}/{m.get('commandAttrs')}")
    expect_no_host_post("a recovery push while the SPA is already resident "
                        "('shown' + glass.html uri — the page just flips "
                        "views on its next poll)")
    # (j) the 60s recovery throttle: park a DIFFERENT page at 'shown' (so
    # the residency gate no longer short-circuits), then a re-tap must be
    # THROTTLED — the card-IN recovery above stamped the window <60s ago.
    cs, cbody = post_command({"egmId": EGM_ID, "action": "glassShow",
                              "page": "glass_ping.html"})
    gl_cnt2 = str(cbody.get("contentId") or "")
    check("manual glassShow of another page: accepted, and it RELEASES "
          "the resident content first (maxContentLoaded=1, live-hit law)",
          cs == 200 and cbody.get("ok") is not False
          and cbody.get("releasedOld") is True and gl_cnt2.isdigit(),
          f"got {cs}/{cbody}")
    m = expect_host_post("releaseContent (the old glass.html content)")
    if m:
        a = m.get("commandAttrs", {})
        check("releaseContent carries the OLD contentId + its captured txn",
              m["command"] == "releaseContent"
              and a.get("contentId") == gl_cnt
              and a.get("transactionId") == "601", f"got {a}")
    m = expect_host_post("loadContent (glass_ping.html)")
    if m:
        check("the new load follows the release in FIFO order",
              m["command"] == "loadContent" and "glass_ping.html"
              in m.get("commandAttrs", {}).get("mediaURI", ""),
              f"got {m.get('commandAttrs')}")
    post_to_host(avp_wrap(md_response(
        f'<igtMediaDisplay:contentStatus '
        f'igtMediaDisplay:contentId="{gl_cnt2}" '
        f'igtMediaDisplay:contentState="IGT_contentLoaded" '
        f'g2s:transactionId="801"/>',
        "706", "88805", session_type="G2S_notification")))
    m = expect_host_post("auto-setActiveContent (glass_ping push)")
    if m:
        check("contentStatus(IGT_contentLoaded) advances DIRECTLY (no "
              "poll needed — a non-polling page like glass_ping relies "
              "on the EGM narrating loaded), new txn echoed (801)",
              m["command"] == "setActiveContent"
              and m.get("commandAttrs", {}).get("transactionId") == "801",
              f"got {m.get('commandAttrs')}")
    post_to_host(avp_wrap(md_response(
        f'<igtMediaDisplay:contentStatus '
        f'igtMediaDisplay:contentId="{gl_cnt2}" '
        f'igtMediaDisplay:contentState="IGT_contentExecuting" '
        f'g2s:transactionId="801"/>',
        "707", "88806", session_type="G2S_notification")))
    m = expect_host_post("auto-showMediaDisplay (glass_ping push)")
    post_to_host(avp_wrap(md_response(
        f'<igtMediaDisplay:mediaDisplayAck '
        f'igtMediaDisplay:contentId="{gl_cnt2}" g2s:transactionId="801"/>',
        "708", "88807", session_type="G2S_notification")))
    time.sleep(0.3)
    companion_tap(3)                       # same-uid re-tap = card-OUT
    m = expect_host_post("re-tap card-OUT setIdValidation")
    companion_tap(4)                       # card-IN again, <60s later
    m = expect_host_post("throttled card-IN setIdValidation")
    if m:
        check("tap 4 card-IN accepted (setIdValidation active)",
              m["command"] == "setIdValidation"
              and m.get("commandAttrs", {}).get("idState") == "G2S_active",
              f"got {m.get('command')}")
    expect_no_host_post("a recovery push inside the 60s throttle window "
                        "(GLASS_RECOVERY_THROTTLE_SEC holds — a tap storm "
                        "can never flood the FIFO with 5-8s content "
                        "lifecycles)")
    # (k) glassShow/glassHide edges: page validation (400), unowned device
    # (honest error), hide = bare verb, in-flight refusal, supersede+rerun
    cs, cbody = post_command_err({"egmId": EGM_ID, "action": "glassShow",
                                  "page": "../g2s_host.py"})
    check("glassShow page validation: a traversal-ish page -> HTTP 400 "
          "(must be a bare *.html filename)",
          cs == 400 and cbody.get("ok") is False, f"got {cs}/{cbody}")
    expect_no_host_post("the junk-page glassShow")
    cs, cbody = post_command({"egmId": EGM_ID, "action": "glassShow",
                              "deviceId": "9"})
    check("glassShow at a device outside the revealed owned set -> honest "
          "error dict (probe-ladder honesty), nothing on the wire",
          cs == 200 and cbody.get("ok") is False
          and "owned" in str(cbody.get("error", "")), f"got {cs}/{cbody}")
    expect_no_host_post("the unowned-device glassShow")
    cs, cbody = post_command({"egmId": EGM_ID, "action": "glassHide"})
    check("glassHide accepted (dev 1 default; content stays resident)",
          cs == 200 and cbody.get("deviceId") == "1", f"got {cs}/{cbody}")
    m = expect_host_post("hideMediaDisplay")
    if m:
        check("glassHide -> bare hideMediaDisplay (empty verb, zero attrs)",
              m["command"] == "hideMediaDisplay"
              and m.get("commandAttrs", {}) == {},
              f"got {m.get('command')}/{m.get('commandAttrs')}")
    cs, cbody = post_command({"egmId": EGM_ID, "action": "glassShow"})
    gl_cnt3 = str(cbody.get("contentId") or "")
    check("glassShow back to the SPA supersedes the resident glass_ping "
          "content (release-then-load once more)",
          cs == 200 and cbody.get("releasedOld") is True
          and gl_cnt3.isdigit(), f"got {cs}/{cbody}")
    m = expect_host_post("releaseContent (glass_ping content)")
    if m:
        check("releaseContent names the glass_ping content + txn 801",
              m["command"] == "releaseContent"
              and m.get("commandAttrs", {}).get("contentId") == gl_cnt2
              and m.get("commandAttrs", {}).get("transactionId") == "801",
              f"got {m.get('commandAttrs')}")
    m = expect_host_post("loadContent (glass.html again)")
    cs, cbody = post_command({"egmId": EGM_ID, "action": "glassShow"})
    check("a second glassShow while one is <25s in flight -> refused "
          "(the stage timeout is the honest failure surface), no wire",
          cs == 200 and cbody.get("ok") is False
          and "in flight" in str(cbody.get("error", "")),
          f"got {cs}/{cbody}")
    expect_no_host_post("the in-flight-refused glassShow")
    # walk the supersede to 'shown' so the run ends with the SPA resident
    post_to_host(avp_wrap(md_response(
        f'<igtMediaDisplay:contentStatus '
        f'igtMediaDisplay:contentId="{gl_cnt3}" '
        f'igtMediaDisplay:contentState="IGT_contentPending" '
        f'g2s:transactionId="901"/>',
        "709", "88808", session_type="G2S_notification")))
    raw_get(f"/api/glass/state?egm={EGM_ID}&src=spa&dev=1")  # poll-driven
    # advance (a FRESH push dict has no lastTryTs -> first poll fires)
    m = expect_host_post("auto-setActiveContent (SPA re-push)")
    post_to_host(avp_wrap(md_response(
        f'<igtMediaDisplay:contentStatus '
        f'igtMediaDisplay:contentId="{gl_cnt3}" '
        f'igtMediaDisplay:contentState="IGT_contentExecuting" '
        f'g2s:transactionId="901"/>',
        "710", "88809", session_type="G2S_notification")))
    m = expect_host_post("auto-showMediaDisplay (SPA re-push)")
    post_to_host(avp_wrap(md_response(
        f'<igtMediaDisplay:mediaDisplayAck '
        f'igtMediaDisplay:contentId="{gl_cnt3}" g2s:transactionId="901"/>',
        "711", "88810", session_type="G2S_notification")))
    time.sleep(0.3)
    # (k2) SPA-liveness short-circuit — the hub-restart MDX003 lesson: a
    # glassShow while the SPA's own poll is fresh (<GLASS_SPA_LIVE_SEC)
    # must NOT loadContent over the resident page (maxContentLoaded=1
    # would reject it "Must release loaded content."); it sends a bare
    # showMediaDisplay instead.
    raw_get(f"/api/glass/state?egm={EGM_ID}&src=spa&dev=1")  # heartbeat
    cs, cbody = post_command({"egmId": EGM_ID, "action": "glassShow"})
    check("glassShow with the SPA manifestly alive -> show-only "
          "short-circuit (residentShowOnly, no load cycle)",
          cs == 200 and cbody.get("residentShowOnly") is True
          and "contentId" not in cbody, f"got {cs}/{cbody}")
    m = expect_host_post("short-circuit showMediaDisplay")
    if m:
        check("the short-circuit wire verb is a BARE showMediaDisplay — "
              "never a loadContent at occupied content",
              m["command"] == "showMediaDisplay"
              and m.get("commandAttrs", {}) == {},
              f"got {m.get('command')}/{m.get('commandAttrs')}")
    post_to_host(avp_wrap(md_response(
        f'<igtMediaDisplay:mediaDisplayAck '
        f'igtMediaDisplay:contentId="{gl_cnt3}" g2s:transactionId="901"/>',
        "712", "88811", session_type="G2S_notification")))
    time.sleep(0.3)
    # (k3) the SERVICE BUTTON toggle (live-adjudicated 2026-07-10): with
    # the operator's 'app controls service button' enabled the AVP does
    # NOT drive the window — it narrates each press as G2S_CBE301 and the
    # HUB toggles the resident menu. Visible now (short-circuit show just
    # acked) -> press hides; a burst press debounces; a press after the
    # debounce shows again.
    def service_press(event_id, command_id, session_id):
        # NOTE: the hook's show/hide is enqueued on the eventReport handler
        # thread BEFORE the eventAck, so the toggle verb arrives FIRST.
        post_to_host(avp_wrap(avp_class_command(
            "eventHandler",
            '<g2s:eventReport g2s:deviceClass="G2S_cabinet" '
            'g2s:deviceId="1" g2s:eventCode="G2S_CBE301" '
            f'g2s:eventId="{event_id}" '
            f'g2s:eventDateTime="{now_iso()}" g2s:transactionId="0"/>',
            command_id, session_id, "G2S_request")))

    service_press(9001, "760", "88820")
    m = expect_host_post("service-button toggle: hideMediaDisplay")
    if m:
        check("press #1 with the menu visible -> hideMediaDisplay "
              "(the cabinet's own button closes the menu)",
              m["command"] == "hideMediaDisplay" and m["deviceId"] == "1",
              f"got {m.get('command')}/{m.get('deviceId')}")
    expect_host_post("eventAck (press 9001)")
    service_press(9002, "761", "88821")
    expect_host_post("eventAck (press 9002)")
    expect_no_host_post("press #2 inside GLASS_BUTTON_DEBOUNCE_SEC — a "
                        "burst of CBE301 chirps must not flap the window")
    time.sleep(2.1)
    service_press(9003, "762", "88822")
    m = expect_host_post("service-button toggle: showMediaDisplay")
    if m:
        check("press #3 after the debounce with the menu hidden -> "
              "showMediaDisplay (toggle self-corrects off the belief)",
              m["command"] == "showMediaDisplay" and m["deviceId"] == "1",
              f"got {m.get('command')}/{m.get('deviceId')}")
    expect_host_post("eventAck (press 9003)")
    # (l) cleanup: card OUT, fob deleted, session registry empty again
    companion_tap(5)
    m = expect_host_post("cleanup card-OUT setIdValidation")
    if m:
        check("cleanup card-OUT (idState G2S_inactive)",
              m["command"] == "setIdValidation"
              and m.get("commandAttrs", {}).get("idState")
              == "G2S_inactive", f"got {m.get('commandAttrs')}")
    m = expect_host_post("follow-card hideMediaDisplay (cleanup card-OUT)")
    if m:
        check("cleanup card-out hides the menu window too (follow-card "
              "is every card-out path, not just the logout button)",
              m["command"] == "hideMediaDisplay"
              and m.get("commandAttrs", {}) == {},
              f"got {m.get('command')}/{m.get('commandAttrs')}")
    st_fd, body_fd = raw_post("/api/fobs", json.dumps(
        {"action": "delete", "uid": "A1B2C3D401"}))
    check("cleanup: fob deleted, glassSessions back to 0",
          st_fd == 200 and json.loads(body_fd).get("deleted") is True
          and get_json(STATUS_URL).get("glassSessions", {}).get("count")
          == 0, f"got {st_fd}/{body_fd[:120]}")

    print("— Step 6.9999: Player Maintenance v1 — the /api/players "
          "composite + create->link->fund->carded-credits->remove")
    players_url = STATUS_URL.replace("/api/status", "/api/players")
    # (a) GET shape: the house rides its OWN key (it is the bank, NOT a
    # player) and the unassigned player-fob pool is a list — the Players
    # card's contract.
    pl = get_json(players_url)
    check("GET /api/players: ok:true, players[] list, house under its OWN "
          "key (kind=house, never inside players[]), unassignedFobs[] list",
          pl.get("ok") is True and isinstance(pl.get("players"), list)
          and isinstance(pl.get("house"), dict)
          and pl["house"].get("kind") == "house"
          and not any(p.get("id") == "house" or p.get("kind") == "house"
                      for p in pl["players"])
          and isinstance(pl.get("unassignedFobs"), list),
          f"got {json.dumps(pl)[:220]}")
    pm_house0 = house_cashable()
    # (b) a player-tier fob with NO account lands in the link-me pool
    st_pm, body_pm = raw_post("/api/fobs", json.dumps(
        {"action": "set", "uid": "B4D2C3A902", "tier": "player",
         "label": "Patron Fob"}))
    pl = get_json(players_url)
    check("an unlinked player-tier fob shows in unassignedFobs",
          st_pm == 200 and any(f.get("uid") == "B4D2C3A902"
                               for f in pl.get("unassignedFobs", [])),
          f"got {st_pm}/{json.dumps(pl.get('unassignedFobs'))[:160]}")
    # (c) create -> a fresh zero-balance player row
    st_pm, body_pm = raw_post("/api/players", json.dumps(
        {"action": "create", "name": "Replay Patron"}))
    try:
        pm_row = json.loads(body_pm).get("player") or {}
    except ValueError:
        pm_row = {}
    pm_id = str(pm_row.get("id") or "")
    check("create: 200 {ok, player} — kind=player, all three buckets 0",
          st_pm == 200 and pm_id.startswith("p")
          and pm_row.get("kind") == "player"
          and pm_row.get("cashableMillicents") == 0
          and pm_row.get("promoMillicents") == 0
          and pm_row.get("nonCashMillicents") == 0,
          f"got {st_pm}/{body_pm[:200]}")
    # (d) linkFob moves the fob from the pool onto the player's row
    st_pm, body_pm = raw_post("/api/players", json.dumps(
        {"action": "linkFob", "accountId": pm_id, "uid": "B4D2C3A902"}))
    pl = get_json(players_url)
    pm_view = next((p for p in pl.get("players", [])
                    if p.get("id") == pm_id), {})
    check("linkFob: the fob rides the player's fobs[] and leaves "
          "unassignedFobs",
          st_pm == 200
          and json.loads(body_pm).get("fob", {}).get("accountId") == pm_id
          and any(f.get("uid") == "B4D2C3A902"
                  for f in pm_view.get("fobs", []))
          and not any(f.get("uid") == "B4D2C3A902"
                      for f in pl.get("unassignedFobs", [])),
          f"got {st_pm}/{body_pm[:160]}")
    # (e) fund: cents at the API edge, millicents inside, ONE ref pairing
    # BOTH ledger legs (the house may overdraft — it is the bank)
    st_pm, body_pm = raw_post("/api/players", json.dumps(
        {"action": "fund", "accountId": pm_id, "cents": 2500}))
    try:
        pm_fund = json.loads(body_pm)
    except ValueError:
        pm_fund = {}
    pm_ref = str(pm_fund.get("ref") or "")
    check("fund $25.00: 200 {ok, player, ref, movedMillicents=2500000} — "
          "player cashable 2500000 mc, ref player-fund:<id>:<ts>",
          st_pm == 200 and pm_fund.get("ok") is True
          and pm_fund.get("movedMillicents") == 2500000
          and pm_fund.get("player", {}).get("cashableMillicents") == 2500000
          and pm_ref.startswith(f"player-fund:{pm_id}:"),
          f"got {st_pm}/{body_pm[:200]}")
    legs = [e for e in get_json(ACCT_URL).get("ledger", [])
            if e.get("ref") == pm_ref]
    check("the fund ref pairs in the ledger: player +2500000 / house "
          "-2500000, nothing else under that ref",
          sorted((e.get("accountId"), e.get("deltaMillicents"))
                 for e in legs)
          == [("house", -2500000), (pm_id, 2500000)],
          f"got {legs}")
    check("the house paid exactly the funded amount (-2500000 mc)",
          house_cashable() == pm_house0 - 2500000,
          f"house {house_cashable()} want {pm_house0 - 2500000}")
    # (f) card IN on the linked fob: the glass state poll now carries the
    # credit balance — creditsMc + its pre-formatted display twin
    st_pm, body_pm = companion_tap(6, uid="B4D2C3A902")
    check("companion tap 6 accepted (card-IN on the linked fob)",
          st_pm == 200 and json.loads(body_pm).get("ackTapId") == 6,
          f"got {st_pm}/{body_pm[:120]}")
    m = expect_host_post("player card-IN setIdValidation")
    if m:
        a = m.get("commandAttrs", {})
        check("card-IN identity prefers the LINKED ACCOUNT's name "
              "(fob -> account -> name), not the fob label",
              m["command"] == "setIdValidation"
              and a.get("idNumber") == "B4D2C3A902"
              and a.get("idState") == "G2S_active"
              and a.get("idPreferName") == "Replay Patron", f"got {a}")
    m = expect_host_post("follow-card showMediaDisplay (player card-IN)")
    if m:
        check("card-IN re-shows the resident menu (bare showMediaDisplay "
              "— the SPA is already resident, no content push)",
              m["command"] == "showMediaDisplay"
              and m.get("commandAttrs", {}) == {},
              f"got {m.get('command')}/{m.get('commandAttrs')}")
    _, _, body_gs = raw_get(f"/api/glass/state?egm={EGM_ID}")
    try:
        gs = json.loads(body_gs)
    except ValueError:
        gs = {}
    check("carded state carries the balance: creditsMc=2500000 + the "
          "pre-formatted credits='$25.00' + the ACCOUNT name",
          gs.get("carded") is True and gs.get("creditsMc") == 2500000
          and gs.get("credits") == "$25.00"
          and gs.get("name") == "Replay Patron",
          f"got {body_gs[:250]}")
    # (g) refusals: carded-in remove, the bank, a pull-back past zero
    st_pm, body_pm = raw_post("/api/players", json.dumps(
        {"action": "remove", "accountId": pm_id}))
    check("remove while carded in -> 400 honest refusal naming the "
          "player ('card out first')",
          st_pm == 400 and json.loads(body_pm).get("ok") is False
          and "card out first" in json.loads(body_pm).get("error", "")
          and "Replay Patron" in json.loads(body_pm).get("error", ""),
          f"got {st_pm}/{body_pm[:200]}")
    # unlink-then-remove must ALSO refuse: the refusal resolves the
    # session record's OWN accountId (stamped at card-in), so clearing
    # the fob link cannot blind it — the session IS the wallet link
    raw_post("/api/players", json.dumps(
        {"action": "unlinkFob", "uid": "B4D2C3A902"}))
    st_pm, body_pm = raw_post("/api/players", json.dumps(
        {"action": "remove", "accountId": pm_id}))
    check("unlink-then-remove while carded in STILL refuses (the session "
          "record's accountId survives the fob unlink)",
          st_pm == 400 and json.loads(body_pm).get("ok") is False
          and "card out first" in json.loads(body_pm).get("error", ""),
          f"got {st_pm}/{body_pm[:200]}")
    st_pm, body_pm = raw_post("/api/players", json.dumps(
        {"action": "linkFob", "accountId": pm_id, "uid": "B4D2C3A902"}))
    check("fob re-linked for the rest of the walk (linkFob ok)",
          st_pm == 200 and json.loads(body_pm).get("ok") is True,
          f"got {st_pm}/{body_pm[:160]}")
    st_pm, body_pm = raw_post("/api/players", json.dumps(
        {"action": "remove", "accountId": "house"}))
    check("remove the house -> 400 (the bank cannot be removed)",
          st_pm == 400 and json.loads(body_pm).get("ok") is False,
          f"got {st_pm}/{body_pm[:160]}")
    st_pm, body_pm = raw_post("/api/players", json.dumps(
        {"action": "fund", "accountId": pm_id, "cents": -99999}))
    pl = get_json(players_url)
    pm_view = next((p for p in pl.get("players", [])
                    if p.get("id") == pm_id), {})
    check("a pull-back past zero -> 400 (adjust's ATOMIC player-overdraft "
          "refusal) and the balance is UNTOUCHED",
          st_pm == 400 and json.loads(body_pm).get("ok") is False
          and pm_view.get("cashableMillicents") == 2500000,
          f"got {st_pm}/{body_pm[:160]} "
          f"bal={pm_view.get('cashableMillicents')}")
    check("the players composite shows the carded location "
          f"(cardedAt={EGM_ID!r})",
          pm_view.get("cardedAt") == EGM_ID,
          f"got {pm_view.get('cardedAt')}")
    # money-edge posture: every neighbor REFUSES, none rounds — a float
    # 25.5 must not silently truncate to 25 cents
    st_pm, body_pm = raw_post("/api/players", json.dumps(
        {"action": "fund", "accountId": pm_id, "cents": 25.5}))
    check("fund with float cents (25.5) -> 400 refusal, never a silent "
          "truncation to 25",
          st_pm == 400 and json.loads(body_pm).get("ok") is False
          and "integer" in json.loads(body_pm).get("error", ""),
          f"got {st_pm}/{body_pm[:160]}")
    # the RAW /api/accounts delete keeps the same invariants as the
    # Players remove: refused while carded/funded/linked — no vaporized
    # credits, no dangling fob rows
    st_a2, body_a2 = raw_post("/api/accounts", json.dumps(
        {"action": "delete", "id": pm_id}))
    check("raw /api/accounts delete of a carded+funded+linked player -> "
          "400 (the remove-policy invariants hold at EVERY layer)",
          st_a2 == 400 and json.loads(body_a2).get("ok") is False,
          f"got {st_a2}/{body_a2[:160]}")
    # the fob registry edge validates accountId like linkFob: the bank
    # is not a player, and a typo id must not orphan the fob
    st_f2, body_f2 = raw_post("/api/fobs", json.dumps(
        {"action": "set", "uid": "B4D2C3A902", "accountId": "house"}))
    check("POST /api/fobs linking a fob to 'house' -> 400 (fobs link to "
          "players, not the house)",
          st_f2 == 400 and json.loads(body_f2).get("ok") is False,
          f"got {st_f2}/{body_f2[:160]}")
    st_f2, body_f2 = raw_post("/api/fobs", json.dumps(
        {"action": "set", "uid": "B4D2C3A902", "accountId": "pNOPE"}))
    check("POST /api/fobs linking to an unknown account -> 400 (no "
          "invisible orphan links)",
          st_f2 == 400 and json.loads(body_f2).get("ok") is False,
          f"got {st_f2}/{body_f2[:160]}")
    # (h) card OUT, then remove: the balance FOLDS to the house (paired
    # ledger legs under player-remove:<id>) and the fob unlinks back into
    # the pool — credits are never vaporized
    companion_tap(7, uid="B4D2C3A902")      # same-uid re-tap = card-OUT
    expect_host_post("player card-OUT setIdValidation")
    expect_host_post("follow-card hideMediaDisplay (player card-OUT)")
    _, _, body_gs = raw_get(f"/api/glass/state?egm={EGM_ID}")
    try:
        gs = json.loads(body_gs)
    except ValueError:
        gs = {}
    check("attract state after card-out: creditsMc/credits both OMITTED",
          gs.get("carded") is False and "creditsMc" not in gs
          and "credits" not in gs, f"got {body_gs[:160]}")
    st_pm, body_pm = raw_post("/api/players", json.dumps(
        {"action": "remove", "accountId": pm_id}))
    try:
        pm_rm = json.loads(body_pm)
    except ValueError:
        pm_rm = {}
    check("remove after card-out: 200 {ok, removed, foldedMillicents="
          "2500000, unlinkedFobs=[the fob]}",
          st_pm == 200 and pm_rm.get("ok") is True
          and pm_rm.get("removed") == pm_id
          and pm_rm.get("foldedMillicents") == 2500000
          and pm_rm.get("unlinkedFobs") == ["B4D2C3A902"],
          f"got {st_pm}/{body_pm[:200]}")
    legs = [e for e in get_json(ACCT_URL).get("ledger", [])
            if e.get("ref") == f"player-remove:{pm_id}"]
    check("the fold pairs in the ledger too: player -2500000 / house "
          "+2500000 under player-remove:<id>",
          sorted((e.get("accountId"), e.get("deltaMillicents"))
                 for e in legs)
          == [("house", 2500000), (pm_id, -2500000)],
          f"got {legs}")
    pl = get_json(players_url)
    check("after remove: the player is gone from players[], the fob is "
          "back in unassignedFobs, and the house drifted $0.00",
          not any(p.get("id") == pm_id for p in pl.get("players", []))
          and any(f.get("uid") == "B4D2C3A902"
                  for f in pl.get("unassignedFobs", []))
          and house_cashable() == pm_house0,
          f"house {house_cashable()} want {pm_house0}")
    st_pm, body_pm = raw_post("/api/fobs", json.dumps(
        {"action": "delete", "uid": "B4D2C3A902"}))
    check("cleanup: the patron fob deleted from the registry",
          st_pm == 200 and json.loads(body_pm).get("deleted") is True,
          f"got {st_pm}/{body_pm[:120]}")

    print("— Step 6.99995: account admin flag — the STACKED-glass backend "
          "authority (setAdmin toggle, house refusal, glass admin=false/"
          "true, /api/players carries admin, boot migration)")
    # (a) /api/players GET rows carry admin, defaulting False on a fresh
    # player — the Players card's admin chip/toggle reads this field.
    st_ad, body_ad = raw_post("/api/players", json.dumps(
        {"action": "create", "name": "Admin Walk"}))
    try:
        ad_row = json.loads(body_ad).get("player") or {}
    except ValueError:
        ad_row = {}
    ad_id = str(ad_row.get("id") or "")
    check("create: a fresh player row carries admin=false (the account-level "
          "backend flag, default off)",
          st_ad == 200 and ad_id.startswith("p")
          and ad_row.get("admin") is False, f"got {st_ad}/{body_ad[:200]}")
    pl = get_json(players_url)
    ad_view = next((p for p in pl.get("players", [])
                    if p.get("id") == ad_id), {})
    check("GET /api/players: the row carries the admin field (false here)",
          ad_view.get("admin") is False,
          f"got {json.dumps(ad_view)[:200]}")
    # (b) setAdmin refuses the house — the bank is never carded in, never a
    # backend operator; AccountStore.set_admin owns the refusal.
    st_ad, body_ad = raw_post("/api/players", json.dumps(
        {"action": "setAdmin", "accountId": "house", "admin": True}))
    check("setAdmin house -> 400 (the bank is never admin-toggleable)",
          st_ad == 400 and json.loads(body_ad).get("ok") is False,
          f"got {st_ad}/{body_ad[:160]}")
    st_ad, body_ad = raw_post("/api/players", json.dumps(
        {"action": "setAdmin", "accountId": "pNOPE", "admin": True}))
    check("setAdmin unknown account -> 400 (no invisible admin grants)",
          st_ad == 400 and json.loads(body_ad).get("ok") is False,
          f"got {st_ad}/{body_ad[:160]}")
    # money-edge posture: admin must be a REAL boolean, no truthy coercion
    st_ad, body_ad = raw_post("/api/players", json.dumps(
        {"action": "setAdmin", "accountId": ad_id, "admin": "yes"}))
    check("setAdmin with a non-boolean admin -> 400 'must be a boolean' "
          "(no truthy coercion on an access flag)",
          st_ad == 400 and json.loads(body_ad).get("ok") is False
          and "boolean" in json.loads(body_ad).get("error", ""),
          f"got {st_ad}/{body_ad[:160]}")
    # (c) link a player-tier fob and card IN — a plain player's glass state
    # carries admin=false (the account flag, NOT the fob tier, is the menu
    # authority): the stacked home shows MY WALLET + LOG OUT only.
    st_ad, _ = raw_post("/api/fobs", json.dumps(
        {"action": "set", "uid": "ADAD00AD01", "tier": "player",
         "label": "Admin Walk Fob"}))
    st_ad, body_ad = raw_post("/api/players", json.dumps(
        {"action": "linkFob", "accountId": ad_id, "uid": "ADAD00AD01"}))
    check("admin-walk fob linked to the player (linkFob ok)",
          st_ad == 200 and json.loads(body_ad).get("ok") is True,
          f"got {st_ad}/{body_ad[:160]}")
    companion_tap(8, uid="ADAD00AD01")
    expect_host_post("admin-walk card-IN setIdValidation")
    expect_host_post("follow-card showMediaDisplay (admin-walk card-IN)")
    _, _, body_gs = raw_get(f"/api/glass/state?egm={EGM_ID}")
    try:
        gs = json.loads(body_gs)
    except ValueError:
        gs = {}
    check("glass state (plain player carded): admin=false — the backend "
          "button stack stays hidden, only the wallet shows",
          gs.get("carded") is True and gs.get("admin") is False,
          f"got {body_gs[:200]}")
    # (c.2) the frozen-card-in-stamp fix: a mid-session setAdmin must reach
    # the glass on the very next ~1.5s poll WITHOUT a physical card-out/in.
    # glass_state re-resolves admin LIVE (same cadence as the credits/name
    # resolve) instead of serving the value frozen at card-in, so the flip
    # lands on the still-carded session. Grant then revoke, card untouched.
    raw_post("/api/players", json.dumps(
        {"action": "setAdmin", "accountId": ad_id, "admin": True}))
    _, _, body_gs = raw_get(f"/api/glass/state?egm={EGM_ID}")
    try:
        gs = json.loads(body_gs)
    except ValueError:
        gs = {}
    check("glass state (mid-session admin GRANT): flips to admin=true on the "
          "next poll with NO re-card (live-resolved, not the frozen stamp)",
          gs.get("carded") is True and gs.get("admin") is True,
          f"got {body_gs[:200]}")
    raw_post("/api/players", json.dumps(
        {"action": "setAdmin", "accountId": ad_id, "admin": False}))
    _, _, body_gs = raw_get(f"/api/glass/state?egm={EGM_ID}")
    try:
        gs = json.loads(body_gs)
    except ValueError:
        gs = {}
    check("glass state (mid-session admin REVOKE): flips back to admin=false "
          "on the next poll — a carded operator loses the backend stack "
          "immediately, not only at next card-in",
          gs.get("carded") is True and gs.get("admin") is False,
          f"got {body_gs[:200]}")
    companion_tap(9, uid="ADAD00AD01")        # card-OUT to re-stamp on re-tap
    expect_host_post("admin-walk card-OUT setIdValidation")
    expect_host_post("follow-card hideMediaDisplay (admin-walk card-OUT)")
    # (d) grant admin, then re-card: the fresh card-in stamp resolves the
    # account flag, so the glass state now carries admin=true (the stacked
    # backend menu unlocks). set_admin returns the updated row.
    st_ad, body_ad = raw_post("/api/players", json.dumps(
        {"action": "setAdmin", "accountId": ad_id, "admin": True}))
    check("setAdmin true -> 200 {ok, player} with admin=true on the row",
          st_ad == 200 and json.loads(body_ad).get("ok") is True
          and json.loads(body_ad).get("player", {}).get("admin") is True,
          f"got {st_ad}/{body_ad[:200]}")
    pl = get_json(players_url)
    ad_view = next((p for p in pl.get("players", [])
                    if p.get("id") == ad_id), {})
    check("GET /api/players: the row now carries admin=true",
          ad_view.get("admin") is True, f"got {json.dumps(ad_view)[:200]}")
    companion_tap(10, uid="ADAD00AD01")
    expect_host_post("admin re-card-IN setIdValidation")
    expect_host_post("follow-card showMediaDisplay (admin re-card-IN)")
    _, _, body_gs = raw_get(f"/api/glass/state?egm={EGM_ID}")
    try:
        gs = json.loads(body_gs)
    except ValueError:
        gs = {}
    check("glass state (admin carded): admin=true — the MACHINE/TICKETS/"
          "HANDPAY/GAMES/SETTINGS stack unlocks on top of the wallet",
          gs.get("carded") is True and gs.get("admin") is True,
          f"got {body_gs[:200]}")
    # toggle back off — the flag is a plain two-way switch on the account
    st_ad, body_ad = raw_post("/api/players", json.dumps(
        {"action": "setAdmin", "accountId": ad_id, "admin": False}))
    check("setAdmin false -> 200, admin=false again (a clean two-way toggle)",
          st_ad == 200 and json.loads(body_ad).get("ok") is True
          and json.loads(body_ad).get("player", {}).get("admin") is False,
          f"got {st_ad}/{body_ad[:200]}")
    companion_tap(11, uid="ADAD00AD01")       # card-OUT
    expect_host_post("admin-walk cleanup card-OUT setIdValidation")
    expect_host_post("follow-card hideMediaDisplay (admin-walk cleanup)")
    raw_post("/api/players", json.dumps(
        {"action": "unlinkFob", "uid": "ADAD00AD01"}))
    raw_post("/api/players", json.dumps(
        {"action": "remove", "accountId": ad_id}))
    raw_post("/api/fobs", json.dumps(
        {"action": "delete", "uid": "ADAD00AD01"}))
    # (e) the one-time boot MIGRATION — it promotes accounts linked to
    # operator-tier fobs so today's operators keep backend access before
    # anyone touches the new toggle. It runs ONLY inside G2SHost.__init__
    # (pre-traffic, one-shot) so it can't be re-fired over the wire; drive
    # it directly against a throwaway AccountStore + a synthetic fob source,
    # the in-process posture test_link_demotion uses.
    import tempfile
    import os as _os
    # g2s_host.py lives one dir up from tools/; a script's sys.path[0] is its
    # OWN dir (tools/), so add the package root before importing the host.
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    import g2s_host as _gh          # local import: g2s_host has a __main__
    #                                 guard, so importing it is side-effect-free
    _tmpd = tempfile.mkdtemp(prefix="avp_admin_mig_")
    _astore = _gh.AccountStore(_os.path.join(_tmpd, "accounts.json"))
    _op, _ = _astore.create("Operator Migrated")
    _plain, _ = _astore.create("Plain Player")
    _op_id = _op["id"]
    _plain_id = _plain["id"]

    class _FakeHub:
        """A minimal fob source: an attendant fob linked to a real account,
        a player-tier fob (must NOT promote — the bridge is operator-only),
        a manager fob pointed at the house (set_admin refuses it), and an
        orphan manager fob at an unknown id (silently skipped)."""
        def fobs(self):
            return [
                {"uid": "MIGATT0001", "tier": "attendant",
                 "accountId": _op_id},
                {"uid": "MIGPLY0001", "tier": "player",
                 "accountId": _plain_id},
                {"uid": "MIGMGR0001", "tier": "manager", "accountId": "house"},
                {"uid": "MIGORP0001", "tier": "manager", "accountId": "pNOPE"},
            ]

    class _Shim:
        pass
    _shim = _Shim()
    _shim.hub_store = _FakeHub()
    _shim.account_store = _astore
    _gh.G2SHost._migrate_operator_fob_admins(_shim)
    check("migration: an account linked to an attendant-tier fob is promoted "
          "to admin at boot (operators keep backend access)",
          _astore.get(_op_id).get("admin") is True,
          f"got {_astore.get(_op_id)}")
    check("migration: a player-tier fob does NOT promote its account "
          "(the legacy bridge is attendant/manager only)",
          _astore.get(_plain_id).get("admin") is False,
          f"got {_astore.get(_plain_id)}")
    check("migration: an operator fob pointed at the house is skipped "
          "(set_admin refuses the bank — the sweep never crashes on it)",
          _astore.get("house").get("admin") is False,
          f"got {_astore.get('house')}")
    _gh.G2SHost._migrate_operator_fob_admins(_shim)
    check("migration is idempotent: a re-run leaves the promoted account "
          "admin and silently skips the orphan-account fob",
          _astore.get(_op_id).get("admin") is True,
          f"got {_astore.get(_op_id)}")
    # The one-shot-marker fix: a deliberate demotion must SURVIVE a restart
    # sweep. Demote the promoted operator account, then re-run the
    # migration — the persisted sentinel makes it a no-op, so the account
    # STAYS demoted (before the fix the still-linked operator fob silently
    # re-promoted it on every boot, so admin could never be revoked for
    # exactly this class of account).
    _astore.set_admin(_op_id, False)
    _gh.G2SHost._migrate_operator_fob_admins(_shim)
    check("migration one-shot marker: a deliberate demotion survives a "
          "restart sweep — the still-linked operator fob does NOT re-promote",
          _astore.get(_op_id).get("admin") is False,
          f"got {_astore.get(_op_id)}")

    print("— Step 6.99997: glass destub v1 — /api/glass/data feature views + "
          "the POST /api/glass/action destub (cashout/walletFund reuse the "
          "proven WAT push; the admin views + clearHandpay/refreshGames gate "
          "on the account admin flag; egmId is ALWAYS from the token)")

    def glass_data(tok, v):
        st, _, body = raw_get("/api/glass/data?sess=" + tok + "&view=" + v)
        try:
            return st, json.loads(body)
        except ValueError:
            return st, {}

    def glass_act(tok, obj):
        obj = dict(obj, sess=tok)
        st, body = raw_post("/api/glass/action", json.dumps(obj))
        try:
            return st, json.loads(body)
        except ValueError:
            return st, {}

    def acct_bal(aid):
        for a in get_json(ACCT_URL).get("accounts", []):
            if a.get("id") == aid:
                return int(a.get("cashableMillicents") or 0)
        return None

    # (a) a linked PLAYER (wallet $30, admin=false): cashout is open to any
    # carded session, but every admin data view + admin action answers 403.
    _, body_gp = raw_post("/api/players", json.dumps(
        {"action": "create", "name": "Glass Patron"}))
    gp_id = str((json.loads(body_gp).get("player") or {}).get("id") or "")
    raw_post("/api/fobs", json.dumps(
        {"action": "set", "uid": "6A55D0FB01", "tier": "player",
         "label": "Glass Patron Fob"}))
    raw_post("/api/players", json.dumps(
        {"action": "linkFob", "accountId": gp_id, "uid": "6A55D0FB01"}))
    raw_post("/api/players", json.dumps(
        {"action": "fund", "accountId": gp_id, "cents": 3000}))
    companion_tap(12, uid="6A55D0FB01")
    expect_host_post("glass-destub player card-IN setIdValidation")
    expect_host_post("follow-card showMediaDisplay (glass-destub card-IN)")
    _, _, body_gs = raw_get(f"/api/glass/state?egm={EGM_ID}")
    try:
        gp_tok = str(json.loads(body_gs).get("sess") or "")
    except ValueError:
        gp_tok = ""
    check("glass-destub: linked player carded, session token minted",
          re.fullmatch(r"gs_[0-9a-f]{24}", gp_tok) is not None,
          f"got {body_gs[:160]}")
    # cashout view: open to the carded player, machine credits + the linked
    # wallet fold in, canFund true, egmId echoed from the TOKEN.
    st_co, co = glass_data(gp_tok, "cashout")
    check("data view=cashout (linked player): 200 with machineCreditsMc + "
          "walletMc=3000000 + walletCredits '$30.00' + canFund true, egmId "
          "from the token",
          st_co == 200 and co.get("ok") is True
          and isinstance(co.get("machineCreditsMc"), int)
          and co.get("walletMc") == 3000000
          and co.get("walletCredits") == "$30.00"
          and co.get("canFund") is True and co.get("egmId") == EGM_ID,
          f"got {st_co}/{json.dumps(co)[:220]}")
    # every admin data view refuses a plain player (403), never a 500.
    for v in ("tickets", "handpay", "games", "settings"):
        st_v, dv = glass_data(gp_tok, v)
        check(f"data view={v} (plain player): 403 not authorized (the admin "
              "views gate on the account admin flag, never a 500)",
              st_v == 403 and dv.get("ok") is False
              and dv.get("error") == "not authorized",
              f"got {st_v}/{json.dumps(dv)[:160]}")
    # the two admin ACTIONS also refuse a plain player.
    for verb in ("clearHandpay", "refreshGames"):
        st_v, dv = glass_act(gp_tok, {"action": verb})
        check(f"action {verb} (plain player): 403 not authorized "
              "(_session_is_admin gate)",
              st_v == 403 and dv.get("ok") is False
              and dv.get("error") == "not authorized",
              f"got {st_v}/{json.dumps(dv)[:160]}")
    # walletFund bounds: a positive integer at/under the SAS wire ceiling
    # ($99,999,999.99 = the 5-byte BCD max, raised from the old $10k in
    # aea6cb3), never an overdraw of the player's OWN wallet. The ceiling
    # check fires BEFORE the balance check, so an over-ceiling amount is
    # refused for the ceiling even from a small wallet.
    st_v, dv = glass_act(gp_tok, {"action": "walletFund", "cents": 0})
    check("walletFund cents=0 -> 400 (positive integer required)",
          st_v == 400 and dv.get("ok") is False, f"got {st_v}/{dv}")
    st_v, dv = glass_act(gp_tok,
                         {"action": "walletFund", "cents": 10_000_000_000})
    check("walletFund over the SAS wire ceiling ($99,999,999.99) -> 400 "
          "(reused bound, no new primitive)",
          st_v == 400 and dv.get("ok") is False
          and "ceiling" in str(dv.get("error", "")), f"got {st_v}/{dv}")
    st_v, dv = glass_act(gp_tok, {"action": "walletFund", "cents": 500000})
    check("walletFund $5000 from a $30 wallet -> 400 (never overdraw the "
          "player's OWN account)",
          st_v == 400 and dv.get("ok") is False, f"got {st_v}/{dv}")
    # walletFund happy path — pushes the player's OWN wallet onto THIS machine
    # via the EXACT WAT addCredits path (start_wat_credit_push). A DIFFERENT
    # egmId in the body is IGNORED: the push targets only the token's machine
    # (a body egm of another cabinet would have 404'd 'not connected'). The
    # escrow debit at authorizeTransfer moves the player's ledger, proving the
    # reused money spine is authoritative.
    gp_bal0 = acct_bal(gp_id)
    st_wf, wf = glass_act(gp_tok, {"action": "walletFund", "cents": 1000,
                                   "egmId": "IGT_00NOSUCHEGM"})
    check("walletFund $10 (body egmId=another machine): 200, path=g2s, egmId "
          "forced to the TOKEN's machine, a requestId allocated",
          st_wf == 200 and wf.get("ok") is True and wf.get("path") == "g2s"
          and wf.get("egmId") == EGM_ID and wf.get("accountId") == gp_id
          and str(wf.get("requestId") or "").isdigit(),
          f"got {st_wf}/{json.dumps(wf)[:220]}")
    wf_rid = str(wf.get("requestId"))
    m = expect_host_post("walletFund initiateRequest (WAT addCredits push)")
    wf_sid = m["sessionId"] if m else "1001"
    if m:
        a = m.get("commandAttrs", {})
        check("the push hit ONLY the token machine: wat.initiateRequest on "
              "dev 1 for THIS player's account (never the body egm)",
              m["class"] == "wat" and m["command"] == "initiateRequest"
              and m["deviceId"] == "1" and a.get("accountId") == gp_id
              and a.get("watDirection") == "G2S_toEgm", f"got {a}")
    wf_txn = str(wt_base + 20)
    post_to_host(avp_wrap(avp_class_command(
        "wat",
        f'<g2s:requestPending g2s:transactionId="{wf_txn}" '
        f'g2s:requestId="{wf_rid}"/>',
        "920", wf_sid, "G2S_response")))
    post_to_host(avp_wrap(avp_class_command(
        "wat",
        f'<g2s:initiateTransfer g2s:transactionId="{wf_txn}" '
        f'g2s:requestId="{wf_rid}" g2s:accountId="{gp_id}" '
        'g2s:watDirection="G2S_toEgm" g2s:payMethod="G2S_payCredit" '
        'g2s:reqCashableAmt="1000000" g2s:reqPromoAmt="0" '
        'g2s:reqNonCashAmt="0" g2s:reduceAmts="true" g2s:maxAmt="1000000"/>',
        "921", "920", "G2S_request")))
    m = expect_host_post("walletFund authorizeTransfer (escrow debit)")
    if m:
        a = m.get("commandAttrs", {})
        check("authorizeTransfer echoes the txn + authorizes the $10 "
              "(hostException=0)",
              a.get("authCashableAmt") == "1000000"
              and a.get("hostException") == "0"
              and a.get("transactionId") == wf_txn, f"got {a}")
    check("walletFund DEBITS the player's OWN wallet at escrow (gp "
          "3000000 -> 2000000 mc) — the reused WAT push moved the money",
          acct_bal(gp_id) == gp_bal0 - 1000000,
          f"got {acct_bal(gp_id)} want {gp_bal0 - 1000000}")
    legs = [e for e in get_json(ACCT_URL).get("ledger", [])
            if e.get("accountId") == gp_id
            and str(e.get("ref", "")).startswith(f"wat:{EGM_ID}:")
            and str(e.get("ref", "")).endswith(":escrow")]
    check("walletFund leaves a ledger leg (the WAT escrow ref, -1000000 mc "
          "off the player) — the reused path is the money authority",
          len(legs) == 1 and legs[0].get("deltaMillicents") == -1000000,
          f"got {legs}")
    post_to_host(avp_wrap(avp_class_command(
        "wat",
        f'<g2s:commitTransfer g2s:transactionId="{wf_txn}" '
        f'g2s:requestId="{wf_rid}" g2s:accountId="{gp_id}" '
        'g2s:watDirection="G2S_toEgm" g2s:payMethod="G2S_payCredit" '
        'g2s:transCashableAmt="1000000" g2s:transPromoAmt="0" '
        f'g2s:transNonCashAmt="0" g2s:transDateTime="{now_iso()}" '
        'g2s:egmException="0"/>',
        "922", "921", "G2S_request")))
    expect_host_post("walletFund commitTransferAck (close-out)")

    # ---- cashOutToWallet arm: per-protocol routing + the self-determined gate
    # (still carded as gp on EGM_ID.) The machine->wallet RETURN leg is an ARM
    # (moves no money — the physical CASH OUT button does). On the AVP (plain
    # G2S, unlinked) it routes to the WAT mirror; a SAS-linked cabinet routes
    # to the hub-side AFT host-cashout arm, GATED on the machine's live 0x74
    # from-EGM bit (NOT a manual flag). The SAS branch enqueues to the
    # satellite (never a G2S host post), so the host-post sequence below is
    # undisturbed.
    def _pj(body):
        try:
            return json.loads(body)
        except (ValueError, TypeError):
            return {}
    # (i) AVP routing: the arm reaches the WAT branch, NOT the SAS gate.
    st_ar, dar = glass_act(gp_tok, {"action": "cashOutToWallet",
                                    "enable": False})
    check("cashOutToWallet on the AVP (plain G2S, unlinked): coherent reply, "
          "egmId from the token, NOT the SAS 'operator menu' refusal "
          "(routes to the WAT arm, not the SAS gate)",
          dar.get("egmId") == EGM_ID
          and "operator menu" not in str(dar.get("error", "")),
          f"got {st_ar}/{json.dumps(dar)[:200]}")
    drain_host_posts(3, timeout=1)          # absorb any setWatCashOut posted
    # register a throwaway SAS leg (NO aft block -> not reporting from-EGM) and
    # link EGM_ID to it for the fork test
    raw_post("/api/sas/report", json.dumps(
        {"smibId": "cosmib", "address": "1", "port": "/dev/ttyAMA0",
         "online": True, "polls": 1}))
    st_ln, ln = raw_post("/api/settings", json.dumps(
        {"machineKey": EGM_ID, "sasLink": "cosmib/1"}))
    check("linked the AVP egm to SAS leg cosmib/1 (the cash-out fork under "
          "test)",
          st_ln == 200 and _pj(ln).get("sasLink") == "cosmib/1",
          f"got {st_ln}/{ln[:160]}")
    # (ii) gate self-determines NOT-ready (the leg reports no from-EGM bit):
    # arming refuses HONESTLY, verbatim — never a fake ON.
    st_g0, dg0 = glass_act(gp_tok, {"action": "cashOutToWallet",
                                    "enable": True})
    check("cashOutToWallet on the SAS leg, gate self-determines NOT-ready "
          "(machine not reporting from-EGM): 400 with the honest 'operator "
          "menu' refusal, cashOutToWat false, path=sas",
          st_g0 == 400 and dg0.get("ok") is False
          and dg0.get("cashOutToWat") is False
          and "operator menu" in str(dg0.get("error", "")),
          f"got {st_g0}/{json.dumps(dg0)[:220]}")
    # (iii) the MACHINE now reports from-EGM cash-out on its 0x74 (availXfers
    # bit 0x02 = AVAIL_XFER_FROM_EGM — its operator menu was set to "soft
    # cash-out to host"): the gate self-determines READY, arming lands (SAS
    # enqueue). No manual Settings flip — the wire decides.
    raw_post("/api/sas/report", json.dumps(
        {"smibId": "cosmib", "address": "1", "port": "/dev/ttyAMA0",
         "online": True, "polls": 2,
         "aft": {"registered": True, "availXfers": 0x02, "aftStatus": 0x98}}))
    st_g1, dg1 = glass_act(gp_tok, {"action": "cashOutToWallet",
                                    "enable": True})
    check("cashOutToWallet on the SAS leg, gate READY (machine reports "
          "from-EGM): 200 path=sas, cashOutToWat true (arm enqueued to the "
          "satellite — no host post)",
          st_g1 == 200 and dg1.get("ok") is True
          and dg1.get("path") == "sas" and dg1.get("cashOutToWat") is True,
          f"got {st_g1}/{json.dumps(dg1)[:220]}")
    # (iii-b) THE MONEY LANDS BY THE HUB'S PIN, never the wire (finding #4).
    # gp is armed (pin gp_id on EGM_ID, linked to cosmib/1). The satellite
    # reports a CONFIRMED aft_cashout that NAMES A DIFFERENT wallet (the decoy)
    # — the /api/sas/report channel is unauthenticated + forgeable. The hub
    # must credit gp (what IT armed) and leave the wire-named decoy untouched,
    # else a crafted POST could mint credits into any wallet.
    bal_gp0 = acct_bal(gp_id)
    # per-run-unique cash-out txn ids (the wt_base idiom, line ~3267): the
    # AccountStore's once=True ref ledger PERSISTS across runs, so a fixed txn
    # id would settle once and no-op every re-run — vary it per run.
    co_base = int(time.time() * 1000) + 700000
    co_txn1, co_txn2, co_txn3 = str(co_base), str(co_base + 1), str(co_base + 2)
    _, body_dc = raw_post("/api/players", json.dumps(
        {"action": "create", "name": "Decoy Wallet"}))
    decoy_id = str((json.loads(body_dc).get("player") or {}).get("id") or "")
    bal_decoy0 = acct_bal(decoy_id)
    co_rep = {"smibId": "cosmib", "address": "1", "port": "/dev/ttyAMA0",
              "online": True, "polls": 3,
              "aft": {"registered": True, "availXfers": 0x02,
                      "aftStatus": 0x98},
              "commandResults": [
                  {"type": "aft_cashout", "ok": True, "outcome": "completed",
                   "txnId": co_txn1, "amountCents": 500,
                   "accountId": decoy_id}]}
    raw_post("/api/sas/report", json.dumps(co_rep))
    time.sleep(0.3)
    check("SAS cash-out settle CREDITS THE HUB PIN (gp +$5.00), NOT the wire's "
          "named account — finding #4: the report channel is forgeable, so the "
          "hub credits only what IT armed",
          acct_bal(gp_id) == bal_gp0 + 500000,
          f"got {acct_bal(gp_id)} want {bal_gp0 + 500000}")
    check("the decoy wallet the forged report NAMED was NOT credited (no "
          "wire-named minting)", acct_bal(decoy_id) == bal_decoy0,
          f"got {acct_bal(decoy_id)} want {bal_decoy0}")
    # idempotent: the satellite re-sends every ~1 s — the same txn re-reported
    # must not double-credit (once=True ref + bounded seen-set).
    raw_post("/api/sas/report", json.dumps(dict(co_rep, polls=4)))
    time.sleep(0.3)
    check("re-report of the same cash-out txn does NOT double-credit "
          "(idempotent by ref)", acct_bal(gp_id) == bal_gp0 + 500000,
          f"got {acct_bal(gp_id)} want {bal_gp0 + 500000}")
    # (iii-b2) HOST-CONTROL immediate pull (cashOutNow): the panel button
    # pulls the machine's credits to the wallet NOW — the hub enqueues an
    # aft_cashout_pull + pins the account, and the satellite's completion
    # report credits the PINNED wallet (never the wire's named account).
    bal_gp_hc = acct_bal(gp_id)
    st_cn, dcn = glass_act(gp_tok, {"action": "cashOutNow"})
    check("cashOutNow (host-control) on a from-EGM-ready SAS leg: 200 pending, "
          "path=sas (the hub enqueued a host-pull — no host post, no physical "
          "button)",
          st_cn == 200 and dcn.get("ok") is True and dcn.get("path") == "sas"
          and dcn.get("pending") is True,
          f"got {st_cn}/{json.dumps(dcn)[:220]}")
    # the satellite pulls and reports an aft_cashout completion — again NAMING
    # the decoy; the hub must still credit the pinned gp (credit-by-pin).
    raw_post("/api/sas/report", json.dumps(dict(
        co_rep, polls=5, commandResults=[
            {"type": "aft_cashout", "ok": True, "outcome": "completed",
             "txnId": co_txn3, "amountCents": 750,
             "accountId": decoy_id}])))
    time.sleep(0.3)
    check("cashOutNow completion credits the PINNED player (gp +$7.50), NOT "
          "the wire's decoy — host-control lands to the hub's own arm",
          acct_bal(gp_id) == bal_gp_hc + 750000 and acct_bal(decoy_id) == 0,
          f"got gp {acct_bal(gp_id)} want {bal_gp_hc + 750000}; "
          f"decoy {acct_bal(decoy_id)}")
    # disarm reverts (always allowed even when gated — back to the ticket)
    st_g2, dg2 = glass_act(gp_tok, {"action": "cashOutToWallet",
                                    "enable": False})
    check("cashOutToWallet disarm on the SAS leg: 200, cashOutToWat false "
          "(reverts cash-out to the ticket default)",
          st_g2 == 200 and dg2.get("cashOutToWat") is False,
          f"got {st_g2}/{json.dumps(dg2)[:200]}")
    # (iii-c) the pin is now DISARMED: a satellite cash-out report finds NO
    # hub-armed home -> REJECT. Nobody is credited (no gp, no decoy, and
    # crucially NO House) — the money is a loud unhomed HOLD, never silently
    # banked. This is AJ's "if the account vanished the host rejects and a
    # ticket prints", proven at the settle.
    bal_gp1 = acct_bal(gp_id)
    bal_decoy1 = acct_bal(decoy_id)
    house_pre = house_cashable()
    raw_post("/api/sas/report", json.dumps(
        {"smibId": "cosmib", "address": "1", "port": "/dev/ttyAMA0",
         "online": True, "polls": 5,
         "aft": {"registered": True, "availXfers": 0x02, "aftStatus": 0x98},
         "commandResults": [
             {"type": "aft_cashout", "ok": True, "outcome": "completed",
              "txnId": co_txn2, "amountCents": 700,
              "accountId": decoy_id}]}))
    time.sleep(0.3)
    check("UNARMED SAS cash-out credits NOBODY — no hub pin => reject "
          "(gp/decoy/House all unchanged; a loud unhomed HOLD, never a silent "
          "House credit of a player's cash-out)",
          acct_bal(gp_id) == bal_gp1 and acct_bal(decoy_id) == bal_decoy1
          and house_cashable() == house_pre,
          f"gp {acct_bal(gp_id)} decoy {acct_bal(decoy_id)} "
          f"house {house_cashable()} (pre gp {bal_gp1} decoy {bal_decoy1} "
          f"house {house_pre})")
    post_accounts({"action": "delete", "id": decoy_id})   # tidy the decoy
    # unlink + forget the throwaway leg so EGM_ID is plain G2S again
    raw_post("/api/settings", json.dumps({"machineKey": EGM_ID,
                                          "sasLink": None}))
    raw_post("/api/sas/forget", json.dumps({"key": "cosmib/1"}))

    companion_tap(13, uid="6A55D0FB01")     # card OUT the linked player
    expect_host_post("glass-destub player card-OUT setIdValidation")
    expect_host_post("follow-card hideMediaDisplay (glass-destub card-OUT)")

    # (b) an UNLINKED player-tier session: walletFund refuses (no wallet), and
    # the cashout view is still 200 but canFund=false / walletMc=null.
    raw_post("/api/fobs", json.dumps(
        {"action": "set", "uid": "6A55D0FB02", "tier": "player",
         "label": "No-Wallet Fob"}))
    companion_tap(14, uid="6A55D0FB02")
    expect_host_post("unlinked card-IN setIdValidation")
    expect_host_post("follow-card showMediaDisplay (unlinked card-IN)")
    _, _, body_gs = raw_get(f"/api/glass/state?egm={EGM_ID}")
    try:
        nl_tok = str(json.loads(body_gs).get("sess") or "")
    except ValueError:
        nl_tok = ""
    st_v, dv = glass_act(nl_tok, {"action": "walletFund", "cents": 1000})
    check("walletFund on an UNLINKED session -> 403 (link a player card to "
          "use your wallet — the account is the token's, never the body's)",
          st_v == 403 and dv.get("ok") is False
          and "player card" in str(dv.get("error", "")), f"got {st_v}/{dv}")
    st_co, co = glass_data(nl_tok, "cashout")
    check("data view=cashout (unlinked): 200, machineCreditsMc present but "
          "canFund=false / walletMc=null (no wallet to fund from)",
          st_co == 200 and co.get("ok") is True
          and co.get("canFund") is False and co.get("walletMc") is None,
          f"got {st_co}/{json.dumps(co)[:200]}")
    companion_tap(15, uid="6A55D0FB02")     # card OUT
    expect_host_post("unlinked card-OUT setIdValidation")
    expect_host_post("follow-card hideMediaDisplay (unlinked card-OUT)")

    # (c) an ADMIN session (manager-tier fob, no linked account -> the legacy
    # bridge grants admin): every admin data view answers 200, and both admin
    # actions are ALLOWED (never 403). clearHandpay carries a bogus body egmId
    # to prove the reset keys off the TOKEN's machine (a body egm would 404).
    raw_post("/api/fobs", json.dumps(
        {"action": "set", "uid": "0FF1CE0B01", "tier": "manager",
         "label": "Glass Manager Fob"}))
    companion_tap(16, uid="0FF1CE0B01")
    expect_host_post("admin card-IN setIdValidation")
    expect_host_post("follow-card showMediaDisplay (admin card-IN)")
    _, _, body_gs = raw_get(f"/api/glass/state?egm={EGM_ID}")
    try:
        ad_tok = str(json.loads(body_gs).get("sess") or "")
    except ValueError:
        ad_tok = ""
    check("glass-destub: manager-tier session is admin (legacy bridge), "
          "state carries admin=true",
          bool(ad_tok) and json.loads(body_gs).get("admin") is True,
          f"got {body_gs[:200]}")
    for v, key in (("tickets", "tickets"), ("handpay", "pending"),
                   ("settings", "rows")):
        st_v, dv = glass_data(ad_tok, v)
        check(f"data view={v} (admin): 200 with its list/rows payload + "
              "egmId from the token",
              st_v == 200 and dv.get("ok") is True
              and isinstance(dv.get(key), list) and dv.get("egmId") == EGM_ID,
              f"got {st_v}/{json.dumps(dv)[:200]}")
    # games: grouped ONE ENTRY PER GAME, machine-enabled titles ONLY.
    # Harness state here: dev 1 = doubleDiamond with egmEnabled=FALSE (folded
    # in step 6.2, untouched since), dev 2 = bonusPoker egmEnabled=true +
    # hostEnabled=true (restored after the reject test). egmEnabled is the
    # MACHINE's own state — setGamePlayState only drives hostEnabled — so an
    # egm-disabled title must be ABSENT (a button the menu can't ever light),
    # not rendered as OFF.
    st_v, dv = glass_data(ad_tok, "games")
    gm_names = [str(g.get("name")) for g in (dv.get("games") or [])]
    check("data view=games (admin): grouped by game, machine-enabled only — "
          "bonusPoker present, egm-disabled doubleDiamond ABSENT",
          st_v == 200 and dv.get("ok") is True and dv.get("egmId") == EGM_ID
          and any("bonusPoker" in n for n in gm_names)
          and not any("doubleDiamond" in n for n in gm_names),
          f"got {st_v}/{json.dumps(dv)[:300]}")
    gm_bp = next((g for g in (dv.get("games") or [])
                  if "bonusPoker" in str(g.get("name"))), {})
    check("games entry: count + nested paytables [{id,label,on}] with the "
          "vendor prefix stripped from the paytable label",
          gm_bp.get("count") == 1
          and isinstance(gm_bp.get("paytables"), list)
          and len(gm_bp.get("paytables") or []) == 1
          and (gm_bp.get("paytables") or [{}])[0].get("id") == "2"
          and (gm_bp.get("paytables") or [{}])[0].get("label") == "bp0090"
          and (gm_bp.get("paytables") or [{}])[0].get("on") is True,
          f"got {json.dumps(gm_bp)[:300]}")
    st_v, dv = glass_act(ad_tok, {"action": "clearHandpay",
                                  "egmId": "IGT_00NOSUCHEGM"})
    check("clearHandpay (admin, body egmId=another machine): 200 keyed off "
          "the TOKEN's machine — nothing pending, so an honest ok:false at "
          "200 (NOT a 403/404), egmId forced from the token",
          st_v == 200 and dv.get("ok") is False and dv.get("egmId") == EGM_ID,
          f"got {st_v}/{json.dumps(dv)[:200]}")
    st_v, dv = glass_act(ad_tok, {"action": "refreshGames"})
    check("refreshGames (admin): 200 ok (status-only gamePlay refresh fires, "
          "egmId from the token)",
          st_v == 200 and dv.get("ok") is True and dv.get("egmId") == EGM_ID,
          f"got {st_v}/{json.dumps(dv)[:200]}")
    drain_host_posts(12, timeout=2)         # the async refresh getGameStatus wave
    companion_tap(17, uid="0FF1CE0B01")     # card OUT
    expect_host_post("admin card-OUT setIdValidation")
    expect_host_post("follow-card hideMediaDisplay (admin card-OUT)")
    # cleanup: the carded-out player account + all three destub fobs
    raw_post("/api/players", json.dumps({"action": "remove",
                                         "accountId": gp_id}))
    for uid in ("6A55D0FB01", "6A55D0FB02", "0FF1CE0B01"):
        raw_post("/api/fobs", json.dumps({"action": "delete", "uid": uid}))

    print(f"\n{'=' * 50}\nRESULT: {PASS} passed, {FAIL} failed")
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    main()
