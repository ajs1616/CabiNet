#!/usr/bin/env python3
"""
companion_host.py — the Companion RFID daemon (runs on the Zero beside
casinonet-sas).

One job: turn PN532 card taps into hub knowledge. A single thread polls the
reader at ~5Hz, debounces a held card into ONE tap, queues taps, and POSTs
them to the hub's /api/companion/report — immediately on a new tap, else as
a low-rate heartbeat. The hub does everything else (fob lookup, G2S
setIdValidation carded session, reset-tier SAS handpay reset); this daemon
knows nothing about fobs or tiers on purpose — a satellite stays dumb.

Delivery contract with the hub (mirrors HubReporter's posture in
SAS/sas_host.py, the anchor for this file):
  - taps stay queued until the hub's reply acks them ({"ok": true,
    "ackTapId": N} -> drop tapId <= N), so a down hub loses nothing
    (bounded: deque maxlen 64 — better to lose the OLDEST taps of an
    hours-long outage than to grow forever).
  - tapId is monotonic per PROCESS, RAM only; every report carries
    startedAt so a restart (tapIds back to 0) resets the hub's ack
    watermark instead of colliding with it.
  - failures are edge-logged (first failure + recovery), never per-cycle
    spam — a hub that's down must not flood the Zero's journal (GR-01/02).

Zero-config onboarding (2026-07-13): with NO args the daemon self-configures —
it IDs itself by the Pi's hardware serial (companion-<serial-tail>, so one
identical image can be flashed to every card with no per-device edit) and finds
the hub at the DHCP default gateway (the hub IS the gateway + DHCP server on both
the wired slot VLAN and the Wi-Fi AP). The machine binding is then assigned FROM
THE HUB UI — the hub resolves and routes taps by companionId, so no --g2s-egm is
needed here and a re-assign takes effect live with no restart. The flags below
stay as an explicit override for co-located / manual setups.

Usage:
    python3 companion_host.py                # zero-config: serial id + gateway hub
    python3 companion_host.py --companion-id companion-bb2 \\
        --g2s-egm "WMS_00:11:22:33:44:55" --sas-smib smib-bb2 --sas-address 1
    python3 companion_host.py http://127.0.0.1:8081 --mock   # no hardware
"""

import argparse
import collections
import json
import logging
import socket
import struct
import sys
import time
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from reader import MockRfidReader, PN532Reader

logger = logging.getLogger("companion")

POLL_SEC = 0.2            # reader cadence (~5Hz — a tap lands within 200ms)
DEBOUNCE_SEC = 2.0        # same UID inside this window = the SAME tap (a
#                           held card must not machine-gun; re-tap = lift
#                           the card for 2s, which is also the card-out
#                           gesture the hub's session state machine expects)
MAX_QUEUED_TAPS = 64      # unacked-tap bound while the hub is down
HTTP_TIMEOUT_SEC = 2.5    # report POST timeout (LAN hop; hub is one wall away)
HEARTBEAT_MULT = 5        # idle heartbeat = report_sec * 5 (5s at defaults)


def now_iso():
    return time.strftime("%Y-%m-%dT%H:%M:%S")


def _started_at_iso():
    """startedAt with microseconds — it is the hub's restart-detection key
    (ack watermark resets when it changes), so two starts inside the same
    SECOND must still differ."""
    t = time.time()
    return (time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(t))
            + ".%06d" % int((t % 1.0) * 1e6))


def _pi_serial_tail():
    """The last 6 hex of the Pi's hardware serial (/proc/cpuinfo 'Serial'
    line — 16 hex digits, unique per board, needs zero operator input), or
    None off a Pi. This is what lets ONE identical image self-ID on every
    card: the id derives from the silicon, not a hand-set hostname."""
    try:
        with open("/proc/cpuinfo", encoding="ascii", errors="replace") as f:
            for line in f:
                if line.startswith("Serial"):
                    val = line.split(":", 1)[1].strip().lower()
                    val = val.lstrip("0") or "0"
                    return val[-6:]
    except OSError:
        pass
    return None


def default_companion_id():
    """companion-<serial-tail> on a Pi (zero-config, stable across reboots),
    else the hostname (dev box / non-Pi). Kept short + id-charset-safe so it
    round-trips through the hub's _norm_sat_id."""
    tail = _pi_serial_tail()
    return f"companion-{tail}" if tail else socket.gethostname()


def _default_gateway_ip():
    """The IPv4 default-route gateway from /proc/net/route, or None. On the
    CabiNet slot VLAN (and the Wi-Fi AP) the hub IS the DHCP server AND the
    default gateway, so the gateway is the hub — that coupling is what makes
    a flagless image find the hub with no config. (Falls back cleanly to the
    known hub IP when there is no default route.)

    A satellite is often DUAL-HOMED (wired eth0 + Wi-Fi wlan0), so there are
    TWO default routes — pick the LOWEST-METRIC one, which is the kernel's
    preferred egress (wired, the metric-100 route, over the flaky metric-600
    Wi-Fi leg). Picking the first line seen would risk reporting over Wi-Fi."""
    best_ip = None
    best_metric = None
    try:
        with open("/proc/net/route", encoding="ascii") as f:
            for line in f.readlines()[1:]:
                fields = line.split()
                if len(fields) < 7:
                    continue
                dest, gw, flags, metric = (fields[1], fields[2],
                                           int(fields[3], 16), int(fields[6]))
                # default route (dest 0.0.0.0) that IS a gateway (RTF_GATEWAY)
                if dest == "00000000" and (flags & 0x2):
                    if best_metric is None or metric < best_metric:
                        best_metric = metric
                        best_ip = socket.inet_ntoa(struct.pack("<L", int(gw, 16)))
    except (OSError, ValueError):
        return best_ip
    return best_ip


def resolve_hub_url(explicit, port=8081, fallback="http://192.168.50.2:8081"):
    """Pick the hub URL: an explicit --hub wins (co-located passes
    127.0.0.1); else derive http://<default-gateway>:PORT; else the known
    hub IP. Both consumers of self.url (report loop) are late-bound and
    retry-forgiving, so a wrong/late gateway just starts working later."""
    if explicit:
        return explicit
    gw = _default_gateway_ip()
    if gw:
        return f"http://{gw}:{port}"
    return fallback


class CompanionHost:
    """The tap pipeline: poll -> debounce -> queue -> report -> ack-drop.

    Single-threaded on purpose (run() is the only loop); every method takes
    monotonic `now` from the caller so tests drive time deterministically —
    the same injected-clock idiom as the SAS RxWedgeWatchdog."""

    def __init__(self, reader, hub_url, companion_id, g2s_egm=None,
                 sas_smib=None, sas_address=None, report_sec=1.0,
                 monotonic=time.monotonic):
        self.reader = reader
        self.url = hub_url.rstrip("/") + "/api/companion/report"
        self.companion_id = companion_id
        self.g2s_egm = g2s_egm
        self.sas_smib = sas_smib
        # sasAddress rides as a string (hub contract: str|null) — the SAS
        # address is an identity here, not a number to do math on.
        self.sas_address = str(sas_address) if sas_address is not None else None
        self.report_sec = float(report_sec)
        self.monotonic = monotonic
        self.started_at = _started_at_iso()
        self.started_mono = monotonic()
        self.taps = collections.deque(maxlen=MAX_QUEUED_TAPS)
        self._tap_counter = 0             # RAM only — restart resets to 0,
        #                                   startedAt tells the hub so
        self._uid_seen = {}               # uid -> last-sighting monotonic
        #                                   (per-uid debounce windows)
        self.reader_ok = True
        self.last_error = None
        self.failing = False              # edge-logged hub-failure state
        self._rejected = False            # edge-logged hub-REJECTION state
        #                                   (200 reply with ok != true)
        self._last_report_mono = None     # last ATTEMPT (success or not)
        self.stop = False

    # ---- reader side -------------------------------------------------------

    def poll_reader(self, now):
        """One reader poll + debounce. Returns the new tap dict, or None
        (no card / held card / reader trouble). A raising reader flips
        readerOk for the next report; a clean poll flips it back."""
        try:
            uid = self.reader.poll()
        except Exception as e:                       # noqa: BLE001
            if self.reader_ok:
                logger.warning("reader error (%s: %s) — reporting "
                               "readerOk=false until a clean poll",
                               type(e).__name__, e)
            self.reader_ok = False
            self.last_error = f"{type(e).__name__}: {e}"
            # Freeze the debounce windows across reader trouble: a card
            # parked on the antenna through an I2C hiccup + self-heal must
            # stay ONE tap — letting the window lapse would emit a phantom
            # re-tap, which the hub reads as the card-out gesture.
            for u in self._uid_seen:
                self._uid_seen[u] = now
            return None
        self.reader_ok = True
        if uid is None:
            return None
        # Debounce PER UID: every sighting of a uid RE-ARMS its window, so a
        # card parked on the reader is exactly one tap until it leaves the
        # field for DEBOUNCE_SEC. A DIFFERENT uid taps through immediately —
        # but only once per window, so two cards alternating in the field
        # (PN532 anticollision) cannot machine-gun taps at poll rate.
        last = self._uid_seen.get(uid)
        if last is not None and now - last < DEBOUNCE_SEC:
            self._uid_seen[uid] = now
            return None
        # Prune uids that left the field — bounds the dict to cards actually
        # sighted within the window (a handful at most).
        self._uid_seen = {u: t for u, t in self._uid_seen.items()
                          if now - t < DEBOUNCE_SEC}
        self._uid_seen[uid] = now
        tap = {"tapId": self._tap_counter, "uid": uid, "at": now_iso()}
        self._tap_counter += 1
        self.taps.append(tap)
        logger.info("💳 tap uid=%s (tapId %d, %d queued)",
                    uid, tap["tapId"], len(self.taps))
        return tap

    # ---- hub side ----------------------------------------------------------

    def snapshot(self, now):
        return {
            "companionId": self.companion_id,
            "startedAt": self.started_at,
            "uptimeSec": round(now - self.started_mono, 1),
            "readerOk": self.reader_ok,
            "g2sEgmId": self.g2s_egm,
            "sasSmib": self.sas_smib,
            "sasAddress": self.sas_address,
            "taps": list(self.taps),
            "lastError": self.last_error,
        }

    def _take_ack(self, reply_body):
        """Drop taps the hub has confirmed. Malformed replies are ignored
        (a hub answering 200-with-garbage must not shed queued taps)."""
        try:
            data = json.loads(reply_body or b"{}")
        except ValueError:
            return
        if not isinstance(data, dict):
            return
        if data.get("ok") is not True:
            # A well-formed rejection (e.g. the hub's registry-full reply:
            # 200 + {"ok": false, "error": ...}) would otherwise be totally
            # invisible — report() returns True and the taps just retry
            # every cycle. Edge-log it once so the journal says WHY taps
            # never land (same posture as the failing/RECOVERED pair).
            if not self._rejected:
                self._rejected = True
                logger.warning("hub rejected report: %s — taps stay queued",
                               data.get("error") or data)
            return
        if self._rejected:
            self._rejected = False
            logger.info("hub accepting reports again")
        n = data.get("ackTapId")
        if not isinstance(n, int) or isinstance(n, bool):
            return
        while self.taps and self.taps[0]["tapId"] <= n:
            self.taps.popleft()

    def report(self, now):
        """One POST to the hub. Returns True on a 200 (taps <= ackTapId
        dropped), False on failure (taps stay queued for the next cycle)."""
        self._last_report_mono = now
        body = json.dumps(self.snapshot(now)).encode()
        req = urllib.request.Request(
            self.url, data=body,
            headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT_SEC) as resp:
                self._take_ack(resp.read())
            if self.failing:
                self.failing = False
                logger.info("hub reporting RECOVERED (%s)", self.url)
            return True
        except Exception as e:                       # noqa: BLE001
            if not self.failing:
                self.failing = True
                logger.warning("hub report failed (%s: %s) — taps stay "
                               "queued, retrying quietly every %.1fs",
                               self.url, e, self.report_sec)
            return False

    def report_due(self, now, tapped):
        """Cadence gate: a fresh tap posts IMMEDIATELY; queued-but-unacked
        taps retry every report_sec; an idle companion heartbeats every
        report_sec * HEARTBEAT_MULT. Failures count as attempts, so a down
        hub sees report_sec-paced retries, never 5Hz hammering."""
        if tapped:
            return True
        if self._last_report_mono is None:
            return True                   # first report = boot announcement
        age = now - self._last_report_mono
        if self.taps and age >= self.report_sec:
            return True
        return age >= self.report_sec * HEARTBEAT_MULT

    # ---- the loop ----------------------------------------------------------

    def run(self):
        logger.info("companion %s -> %s (g2sEgm=%s sasSmib=%s addr=%s)",
                    self.companion_id, self.url, self.g2s_egm,
                    self.sas_smib, self.sas_address)
        while not self.stop:
            tap = self.poll_reader(self.monotonic())
            if self.report_due(self.monotonic(), tap is not None):
                self.report(self.monotonic())
            time.sleep(POLL_SEC)
        self.reader.close()


def main():
    ap = argparse.ArgumentParser(
        description="Companion RFID daemon (PN532 card taps -> hub)")
    ap.add_argument("hub", nargs="?", default=None,
                    help="hub base URL override; omit for zero-config "
                         "(derive http://<default-gateway>:8081 — the hub is "
                         "the gateway on the slot VLAN + AP). Co-located passes "
                         "http://127.0.0.1:8081")
    ap.add_argument("--hub", dest="hub_opt", default=None,
                    help="same as the positional hub, as a flag")
    ap.add_argument("--companion-id", default=None,
                    help="identity in the hub's companion registry "
                         "(default: companion-<Pi-serial-tail>, else hostname)")
    ap.add_argument("--g2s-egm", default=None,
                    help="OPTIONAL override — egmId this reader binds to for "
                         "carded sessions. Normally omitted: the hub owns the "
                         "binding (assigned in the UI) and routes taps by "
                         "companionId. A value here is the fallback used only "
                         "until/unless the device is assigned in the UI")
    ap.add_argument("--sas-smib", default=None,
                    help="SAS smibId binding for reset-tier fobs (the hub "
                         "routes handpay_reset there); omit = no SAS binding")
    ap.add_argument("--sas-address", default=None,
                    help="SAS machine address behind --sas-smib")
    ap.add_argument("--bus", default="/dev/i2c-11",
                    help="I2C device node (default /dev/i2c-11 = the "
                         "software i2c-gpio bus on GPIO23/24)")
    ap.add_argument("--i2c-addr", type=lambda s: int(s, 0), default=0x24,
                    help="PN532 I2C address (default 0x24)")
    ap.add_argument("--mock", action="store_true",
                    help="scripted MockRfidReader self-test (no hardware)")
    ap.add_argument("--report-sec", type=float, default=1.0,
                    help="report/retry cadence; idle heartbeat is 5x this "
                         "(default 1.0)")
    args = ap.parse_args()

    logging.basicConfig(level=logging.INFO, stream=sys.stderr,
                        format="%(asctime)s | %(levelname)-7s | %(message)s")

    if args.report_sec <= 0:
        ap.error("--report-sec must be > 0")

    hub_url = resolve_hub_url(args.hub_opt or args.hub)
    companion_id = args.companion_id or default_companion_id()

    if args.mock:
        reader = MockRfidReader()
        logger.info("mock reader (self-test, no hardware)")
    else:
        reader = PN532Reader(bus=args.bus, addr=args.i2c_addr)
        if reader.init():
            logger.info("PN532 ready on %s @ 0x%02x", args.bus, args.i2c_addr)
        else:
            # Not fatal: poll() keeps retrying the bring-up lazily and the
            # hub sees readerOk=false meanwhile — a missing board never
            # crash-loops the unit (same posture as sas_host's open_port).
            logger.warning("PN532 not answering on %s @ 0x%02x — will keep "
                           "retrying (board unplugged is fine)",
                           args.bus, args.i2c_addr)

    host = CompanionHost(reader, hub_url, companion_id,
                         g2s_egm=args.g2s_egm, sas_smib=args.sas_smib,
                         sas_address=args.sas_address,
                         report_sec=args.report_sec)
    try:
        host.run()
    except KeyboardInterrupt:
        logger.info("stopped")
    finally:
        reader.close()


if __name__ == "__main__":
    main()
