#!/usr/bin/env python3
"""
Regression test for the epoch-reset race (found by adversarial review).

The bug: a re-commsOnLine that arrives while an outbound worker is mid-flight
used to reset the host commandId counter under the worker's feet, producing
out-of-order commandIds (e.g. commsOnLineAck cid=2 instead of 1) and a stale
setCommsState sent ahead of the new commsOnLineAck — the exact "loops with no
obvious cause" failure. avp_replay.py can't catch it because it never overlaps
a re-handshake with an in-flight worker.

This test forces the overlap with a deliberately SLOW fake EGM endpoint, then
asserts the invariant the fix guarantees: after any re-commsOnLine, the very
next commsOnLineAck the host emits carries commandId == 1, and no setCommsState
from a superseded epoch is sent before it.

Run from G2S/:
    python3 g2s_host.py --port 19090 &        # or let this script note it
    python3 tools/test_epoch_race.py 19090
Exits 0 if the race is fixed.
"""

import html
import sys
import threading
import time
import queue
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

WSDL_NS = "http://www.gamingstandards.com/wsdl/g2s/v1.0"
SCHEMA_NS = "http://www.gamingstandards.com/g2s/schemas/v1.0.3"
SOAP_NS = "http://schemas.xmlsoap.org/soap/envelope/"
EGM_ID = "IGT_00012E492815"
EGM_PORT = 8080
SLOW_SECONDS = 0.4          # make each host->EGM POST take this long

port = int(sys.argv[1]) if len(sys.argv) > 1 else 19090
HOST_URL = f"http://127.0.0.1:{port}/G2S"

received = queue.Queue()
_slow = {"on": False}


def now_iso():
    dt = datetime.now(timezone.utc)
    return dt.strftime("%Y-%m-%dT%H:%M:%S.") + f"{dt.microsecond // 1000:03d}Z"


def localname(tag):
    return tag.rsplit("}", 1)[-1]


def attr(el, name):
    v = el.get(f"{{{SCHEMA_NS}}}{name}")
    return v if v is not None else el.get(name)


class SlowEgm(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def do_POST(self):
        n = int(self.headers.get("Content-Length", 0))
        raw = self.rfile.read(n).decode("utf-8", "replace")
        if _slow["on"]:
            time.sleep(SLOW_SECONDS)
        # record the command + commandId
        try:
            root = ET.fromstring(raw)
            inner_el = root.find(f".//{{{WSDL_NS}}}g2sRequest/{{{WSDL_NS}}}g2sRequest")
            inner = ET.fromstring(html.unescape(inner_el.text))
            body = inner.find(f"{{{SCHEMA_NS}}}g2sBody")
            cls = list(body)[0]
            cmd = localname(list(cls)[0].tag)
            received.put({"command": cmd, "commandId": attr(cls, "commandId"),
                          "sessionId": attr(cls, "sessionId")})
        except Exception as e:
            received.put({"command": f"PARSE_ERR:{e}", "commandId": None})
        ack = (f'<g2s:g2sMessage xmlns:g2s="{SCHEMA_NS}"><g2s:g2sAck '
               f'g2s:hostId="1" g2s:egmId="{EGM_ID}" '
               f'g2s:dateTimeSent="{now_iso()}"/></g2s:g2sMessage>')
        body = (f'<?xml version="1.0"?><SOAP-ENV:Envelope '
                f'xmlns:SOAP-ENV="{SOAP_NS}" xmlns:g2s="{WSDL_NS}">'
                f"<SOAP-ENV:Body><g2s:g2sResponse><g2s:g2sResponse>"
                f"{html.escape(ack)}</g2s:g2sResponse></g2s:g2sResponse>"
                f"</SOAP-ENV:Body></SOAP-ENV:Envelope>").encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/xml; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def comms_online(command_id):
    ts = now_iso()
    inner = (
        f'<g2s:g2sMessage xmlns:g2s="{SCHEMA_NS}"><g2s:g2sBody g2s:hostId="1" '
        f'g2s:egmId="{EGM_ID}" g2s:dateTimeSent="{ts}">'
        f'<g2s:communications g2s:deviceId="1" g2s:dateTime="{ts}" '
        f'g2s:commandId="{command_id}" g2s:sessionType="G2S_request" '
        f'g2s:sessionId="0" g2s:timeToLive="30000">'
        f'<g2s:commsOnLine g2s:egmLocation="http://127.0.0.1:{EGM_PORT}" '
        f'g2s:deviceReset="true"/></g2s:communications></g2s:g2sBody>'
        f'</g2s:g2sMessage>')
    return wrap(inner)


def comms_disabled(command_id, session_id):
    ts = now_iso()
    inner = (
        f'<g2s:g2sMessage xmlns:g2s="{SCHEMA_NS}"><g2s:g2sBody g2s:hostId="1" '
        f'g2s:egmId="{EGM_ID}" g2s:dateTimeSent="{ts}">'
        f'<g2s:communications g2s:deviceId="1" g2s:dateTime="{ts}" '
        f'g2s:commandId="{command_id}" g2s:sessionType="G2S_request" '
        f'g2s:sessionId="{session_id}" g2s:timeToLive="30000">'
        f'<g2s:commsDisabled/></g2s:communications></g2s:g2sBody>'
        f'</g2s:g2sMessage>')
    return wrap(inner)


def wrap(inner):
    return (f'<?xml version="1.0"?><SOAP-ENV:Envelope xmlns:SOAP-ENV="{SOAP_NS}" '
            f'xmlns:g2s="{WSDL_NS}"><SOAP-ENV:Body><g2s:g2sRequest>'
            f"<g2s:g2sRequest>{html.escape(inner)}</g2s:g2sRequest>"
            f"<g2s:g2sEgmId>{EGM_ID}</g2s:g2sEgmId><g2s:g2sHostId>1</g2s:g2sHostId>"
            f"</g2s:g2sRequest></SOAP-ENV:Body></SOAP-ENV:Envelope>")


def post(body):
    req = urllib.request.Request(HOST_URL, data=body.encode(),
                                 headers={"Content-Type": "text/xml",
                                          "SOAPAction": '"x"'})
    urllib.request.urlopen(req, timeout=10).read()


def drain(timeout=3.0):
    out = []
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        try:
            out.append(received.get(timeout=end - time.monotonic()))
        except queue.Empty:
            break
    return out


def main():
    srv = ThreadingHTTPServer(("0.0.0.0", EGM_PORT), SlowEgm)
    threading.Thread(target=srv.serve_forever, daemon=True).start()

    # 1) Clean join so the association is in sync state.
    post(comms_online(223))
    first = received.get(timeout=3)           # commsOnLineAck cid=1
    assert first["command"] == "commsOnLineAck" and first["commandId"] == "1", first

    # 2) Now force the overlap: turn on the slow EGM, fire a commsDisabled
    #    (kicks off disabledAck + a settled setCommsState worker), then ~100ms
    #    later fire a re-commsOnLine that lands while those are in flight.
    _slow["on"] = True
    post(comms_disabled(224, 42))             # triggers the dual-send worker
    time.sleep(0.1)
    post(comms_online(300))                   # re-handshake mid-flight

    events = drain(4.0)
    _slow["on"] = False
    srv.shutdown()

    # 3) Assert the invariant the fix guarantees.
    print("outbound sequence the host emitted:")
    for e in events:
        print(f"   {e['command']} cid={e['commandId']} sid={e['sessionId']}")

    # Find the commsOnLineAck that answers the re-handshake (the one AFTER the
    # commsDisabled traffic). It MUST carry commandId 1.
    online_acks = [e for e in events if e["command"] == "commsOnLineAck"]
    ok = True
    if not online_acks:
        print("FAIL: no commsOnLineAck emitted for the re-handshake")
        ok = False
    else:
        re_ack = online_acks[-1]
        if re_ack["commandId"] != "1":
            print(f"FAIL: re-handshake commsOnLineAck cid={re_ack['commandId']} "
                  f"(expected 1) — epoch race NOT fixed")
            ok = False
        else:
            print("PASS: re-handshake commsOnLineAck carries commandId=1")

    # The bug also sent a stale-epoch setCommsState AHEAD of the re-handshake
    # commsOnLineAck. Assert no setCommsState precedes the final ack.
    if online_acks:
        idx = events.index(online_acks[-1])
        if any(e["command"] == "setCommsState" for e in events[:idx]):
            print("FAIL: a setCommsState was sent before the re-handshake "
                  "commsOnLineAck — stale-epoch send leaked")
            ok = False
        else:
            print("PASS: no stale setCommsState ahead of the re-handshake ack")

    print("\nRESULT:", "PASS — epoch race fixed" if ok else "FAIL")
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
