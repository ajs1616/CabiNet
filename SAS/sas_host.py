#!/usr/bin/env python3
"""
sas_host.py — the SAS backup-channel host service.

The missing production runner for the validated SAS stack: opens the serial
loop, runs SASPoller with the TITO host (ticket capture/redemption ledger)
wired in, and logs machine-state edges to stderr (journald when run as
casinonet-sas.service). Mirrors G2S/g2s_host.py's role for the SAS side —
one file, stdlib + the three pinned deps (pyserial, crcmod, loguru).

Built for the "backup channel" reality: the RS232 HAT and the machine may
not be wired yet. The runner opens the port with retry-forever (a missing
/dev node never crash-loops systemd), polls into the silence at the SAS
floor, and reports ONLINE/OFFLINE edges plus a low-rate heartbeat instead
of per-poll spam (GR-01/GR-02: a dark link is the daily home case — it must
be quiet in the journal and truthful about being offline).

Usage:
    python3 sas_host.py /dev/ttyAMA0                 # Pi 5 PL011 header UART
    python3 sas_host.py /dev/ttyAMA0 --address 3
    python3 sas_host.py --mock --max-polls 50        # no-hardware smoke test

The Pi 5 gotcha this file exists to remember: /dev/serial0 is the DEBUG
connector (ttyAMA10) — the GPIO14/15 header UART behind the RS232 HAT is
/dev/ttyAMA0 (dtparam=uart0=on), and it must be the PL011, never the
mini-UART: the SAS wakeup bit is mark/space parity per byte.
"""

import argparse
import collections
import json
import signal
import socket
import struct
import sys
import threading
import time
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from loguru import logger

from core.sas_protocol import SASProtocol, SASCommandBuilder
from core.sas_poller import MeterSpec, SASPoller
from core.sas_meters import (SAS_POLL_FLOOR, parse_meters_10_15,
                             parse_single_meter)
from core.sas_handpay_reset import (HANDPAY_PENDING_EXCEPTION,
                                    HANDPAY_RESET_EXCEPTION)
from core.hub_ticket_client import HubTicketAuthority
from core.sas_ticket_store import DEFAULT_SYSTEM_ID, TicketStore
from core.sas_tito_host import (SASTITOHost, CMD_SET_EXTENDED_TICKET_DATA,
                                CMD_SET_TICKET_DATA,
                                build_set_extended_ticket_data,
                                build_set_ticket_data,
                                parse_ticket_data_flag)
from core.sas_bonus import (MAX_BONUS_CREDITS, build_legacy_bonus_poll,
                            classify_bonus_reply)
from modules.aft.aft_handler import (
    AFTHost, AFTTransferRequest, TRANSFER_TYPE_HOST_TO_EGM,
    TRANSFER_TYPE_EGM_TO_HOST, TRANSFER_CODE_FULL_ONLY, TRANSFER_FLAGS_NONE,
    make_transaction_id, AFT_ST_IN_HOUSE_ENABLED, AVAIL_XFER_FROM_EGM,
    describe_available_transfers, describe_aft_status)

HEARTBEAT_SEC = 300          # journal proof-of-life cadence while silent
OPEN_RETRY_SEC = 30          # serial-open retry (HAT/device not present yet)
WEDGE_REOPEN_SEC = 15.0      # RX silence -> port reopen (fast phase). The
#                              machine-reboot wedge (live-diagnosed
#                              2026-07-10): a machine power-cycle deafens
#                              the PL011 RX side while TX keeps flowing —
#                              the machine answers unheard until the port
#                              is closed+reopened. This watchdog makes the
#                              manual `systemctl restart casinonet-sas`
#                              cure automatic within one cadence.
WEDGE_SLOW_AFTER = 20        # dry reopens (~5 min) before deciding "machine
#                              is OFF, not wedged" and backing off the churn
WEDGE_SLOW_SEC = 60.0        # slow-phase cadence — worst-case self-heal
#                              after a long machine-off spell = one minute
REPORT_SEC = 1               # hub report + command-pull cadence. Was 5, which
#                              made this the DOMINANT command latency (a command
#                              waits up to REPORT_SEC to be PULLED into the next
#                              report reply before it reaches the wire — 97-98%
#                              of a live-measured ~4s button->wire, 2026-07-09).
#                              At 1s: button->wire ~0.6s avg / ~1.2s worst, and
#                              the verdict round-trip halves too. SAS_STALE_SEC
#                              (hub, 20s) stays valid — just 20 missed reports
#                              instead of ~4. Proper fix is a decoupled command
#                              poll (keeps state reports cheap); this is the
#                              one-line 4-5x win first.
AFT_STATUS_SEC = 30          # poll-thread AFT (73/FF + asset) status cache
#                              refresh cadence — a registration swap is rare,
#                              so read it seldom to keep the journal quiet and
#                              steady-state at ~5 polls/s (GR-01/02)

# The rig's fixed 20-byte AFT registration key. A freshly RAM-cleared machine
# is keyless, so the host must ALWAYS supply a key at 73/00 + 73/01 (the
# register poll requires exactly 20 outbound key bytes); the machine echoes it
# back (BB2 live-proven) and auto_register then ADOPTS the echoed key for
# transfers. Deterministic across the one home rig so a re-register after a RAM
# clear yields the same key. Override with --aft-key (40 hex chars).
DEFAULT_AFT_KEY = b"CasinoNet-AFT-rig001"   # exactly 20 bytes
assert len(DEFAULT_AFT_KEY) == 20

# AFT amount ceiling — the 5-byte BCD wire maximum ($99,999,999.99), NOT an
# artificial spend cap. This is a PROTOCOL-CORRECTNESS guard: cents_to_bcd5()
# can only encode 10 decimal digits, so a value above this can't ride the wire.
# It is DELIBERATELY not a smaller "sane amount" limit — collector machines
# routinely hold well over $10k (big banks, jackpots), and a cash-out-to-wallet
# must never be capped below what the machine actually holds (that was the old
# $10,000 bug — it rejected a full cash-out with "cash out at the machine").
# The machine's own configured max governs; the House bank may go negative, so
# this is not a funds check either. The hub mirrors this ceiling at the
# /api/sas/command edge (g2s_host.SAS_AFT_MAX_CENTS); this re-checks it on the
# poll thread (the last gate before credits move on the wire).
MAX_AFT_CENTS = 9_999_999_999   # 5-byte BCD max = $99,999,999.99

# ---------------------------------------------------------------------------
# Meter poll rotation (house-economy design §A2/§E) — the floor-hold
# formula's SAS inputs. Every meter_interval (5 s default) the poll thread
# sweeps these long polls:
#
#   0x1A  current credits   (existing, live-proven — the hub's Floor term)
#   0x0F  composite         (§4.4.1: cancelled / coin-in / coin-out / drop /
#                            jackpot / games-played — six 4-byte BCD meters
#                            in ONE frame; jackpot has NO single-meter poll)
#   0x46  bills-in          (credit amount of accepted bills — the B term)
#
# All three are type-R frames issued by SASPoller._sweep_meters on the POLL
# thread (the only transport toucher), back-to-back within one sweep —
# back-to-back long polls are live-proven on this BB2 (general->0x1A every
# sweep since day one, 0x51->0x1B, the 0x70/0x71 TITO chain). The park loop
# in stop_or_heartbeat holds the poll thread BEFORE poll_once, so a
# hub-disabled machine sweeps nothing (no wire traffic while parked). Per
# §2.7.4 a machine that doesn't implement 0x0F/0x46 answers dead silence and
# the sweep simply skips those readings (BENCH #5 decides the
# fallback-to-singles question — nothing breaks meanwhile).
#
# REPORT KEY CONTRACT — the hub's hold formula reads these EXACT keys out of
# the report's meters dict. Values are RAW credit-unit counters straight off
# the wire: the HUB owns denom scaling, first-sight baselining, deltas and
# the 8-digit BCD rollover rule; the satellite NEVER does wrap math.
#
#   "0x1A"         current credits           (existing hex key, unchanged)
#   "0x46"         bills-in, credit amount   (existing hex-key convention)
#   "cancelled"    cancelled credits      }
#   "coinIn"       coin in  (wagered)     }  flattened from the 0x0F
#   "coinOut"      coin out (player won)  }  composite — the hub ingest
#   "totalDrop"    total drop             }  clamps every meter VALUE to a
#   "jackpot"      jackpot                }  scalar, so a dict-valued
#   "gamesPlayed"  games played           }  composite would arrive as
#                                            stringified garbage (design §A2)
#
# Label keys stay <=16 chars (the hub clamps meter KEYS to 16 chars).
COMPOSITE_METER_KEYS = {
    "cancelled_credits": "cancelled",
    "coin_in": "coinIn",
    "coin_out": "coinOut",
    "total_drop": "totalDrop",
    "jackpot": "jackpot",
    "games_played": "gamesPlayed",
}


def _lenient(parser):
    """Wrap a sas_meters parser so a wrong-shaped (but CRC-valid) answer
    yields None — the poller's sweep skips a None reading — instead of a
    ValueError that would kill the poll loop. Corrupt-BCD nibbles already
    come back as None from the parsers themselves (a bad read is skipped,
    never fabricated into a number)."""
    def decode(data):
        try:
            return parser(data)
        except ValueError:
            return None
    return decode


#: The satellite's polled meter set, passed to SASPoller in main() (the
#: core's DEFAULT_METERS stays 0x1A-only for bare/bench constructions).
#: 0x1A keeps its default decoder — bit-for-bit the live-proven behavior.
POLLED_METERS = [
    MeterSpec(0x1A, "current_credits"),
    MeterSpec(0x0F, "accounting", decode=_lenient(parse_meters_10_15)),
    MeterSpec(0x46, "credit_amount_of_bills",
              decode=_lenient(parse_single_meter)),
]


def meter_report_key(key):
    """A stats["last_meters"] key -> its report-dict key: label keys (0x0F
    composite components) pass through, int command codes render '0x%02X'."""
    return key if isinstance(key, str) else f"0x{key:02X}"


def ingest_meter_reading(last_meters, addr, meter_code, value):
    """Fold one on_meter delivery into last_meters ({(addr, key): raw}).

    A dict value is the 0x0F composite: each known component flattens to its
    short label key (COMPOSITE_METER_KEYS); None components (corrupt BCD)
    and unknown labels are skipped WITHOUT touching the last good value.
    Scalar deliveries keep the existing (addr, command-int) key. Values are
    stored RAW — a rollover-shaped drop (an 8-digit BCD counter wrapping
    99999999 -> small) passes straight through; the hub adjudicates
    wrap-vs-RAM-clear (design §B2/§B3).

    Returns [(key, old, new)] for every entry that actually changed, so the
    caller owns meterChanges counting + change logging. Single writer: the
    poll thread (the reporter thread only reads — same lock-free
    two-thread pattern as before)."""
    if isinstance(value, dict):
        items = [(COMPOSITE_METER_KEYS.get(label), v)
                 for label, v in value.items()]
    else:
        items = [(meter_code, value)]
    changes = []
    for key, v in items:
        if key is None or v is None:
            continue
        old = last_meters.get((addr, key))
        if old != v:
            last_meters[(addr, key)] = v
            changes.append((key, old, v))
    return changes


def make_on_meter(stats):
    """Build main()'s on_meter callback around a stats dict (module-level so
    tests drive the REAL production path). Keeps the existing meterChanges
    counting: +1 per changed key; the first sight of a key stores + counts
    silently (inventory, not news)."""
    def on_meter(addr, meter_code, value):
        for key, old, new in ingest_meter_reading(
                stats["last_meters"], addr, meter_code, value):
            stats["meter_changes"] += 1
            if old is not None:      # first sweep is inventory, not news
                logger.info("📊 addr {} meter {}: {} -> {}",
                            addr, meter_report_key(key), old, new)
    return on_meter


class HandpayLatch:
    """C3 pendingHandpay latch — the HANDPAY banner state for the hub/UI.

    Latched ON when exception 0x51 (handpay pending) comes off the FIFO;
    latched OFF on exception 0x52 (handpay was reset — key, fob or remote)
    or on a handpay_reset hub command the machine acked (ok=true). It is
    STATE, not an event: it survives report cycles until the handpay is
    actually cleared. Single writer (the POLL thread — exception dispatch
    and run_hub_command both live there) with a lock-free reader (the
    reporter thread snapshots .pending — one dict-slot swap, same
    two-thread pattern as stats["last_meters"]). Edges log ONCE each: a
    re-raised 0x51 while latched (the machine re-asks every 15 s, §7.8.1)
    neither re-logs nor resets the 'since' timestamp."""

    def __init__(self):
        self.pending = None   # {"since": iso-local, "code": "0x51"} | None

    def on_exception(self, code: int) -> None:
        """Feed every dispatched exception code through here (poll thread)."""
        if code == HANDPAY_PENDING_EXCEPTION:
            if self.pending is None:
                self.pending = {
                    "since": time.strftime("%Y-%m-%dT%H:%M:%S"),
                    "code": f"0x{HANDPAY_PENDING_EXCEPTION:02X}"}
                logger.info("🧾 handpay latch ON (0x51) — pending since {}",
                            self.pending["since"])
        elif code == HANDPAY_RESET_EXCEPTION and self.pending is not None:
            self.pending = None
            logger.info("🧾 handpay latch cleared (0x52 handpay was reset)")

    def on_reset_ok(self) -> None:
        """A handpay_reset hub command came back ok=true (poll thread).
        Usually a no-op — the 0x52 drained in the reset's confirm window
        already cleared the latch — but a confirm-window miss must not
        leave a stale HANDPAY banner after a machine-acked reset."""
        if self.pending is not None:
            self.pending = None
            logger.info("🧾 handpay latch cleared (handpay_reset acked)")


def parse_ticket_data_reply(value):
    """C2 exact-shape guard for the report reply's "ticketData" key —
    the ticket-header target the hub pushes to every machine's printed
    tickets. The contract shape is {"propName": str, "line1": str,
    "line2": str, "titleCash": str|null, "expireDays": int, "rev": int};
    anything else — non-dict, junk types, bool/negative rev, an unset/
    blank propName — returns None and changes NOTHING (same discipline as
    the sasEnabled exact-bool gate). titleCash may ride as null or be
    absent (both mean 'no title'). expireDays is the printed-ticket
    expiration in days (0 = never — the collector-right default): ABSENT
    means an old hub and rides as 0, but a PRESENT junk value (bool,
    float, outside the one-byte 0x7D range 0..255) rejects the whole
    dict — partially trusting a malformed push is how wrong bytes reach
    the wire. Strings are stripped, blank line1/line2/titleCash normalize
    to ''/None (the applier omits them — C1: never push empty over
    existing machine config), and every value is clamped to the hub-side
    64-char tenant bound defensively."""
    if not isinstance(value, dict):
        return None
    prop = value.get("propName")
    line1 = value.get("line1")
    line2 = value.get("line2")
    title = value.get("titleCash")
    rev = value.get("rev")
    exp = value.get("expireDays", 0)
    if not isinstance(prop, str) or not prop.strip():
        return None
    if not isinstance(line1, str) or not isinstance(line2, str):
        return None
    if title is not None and not isinstance(title, str):
        return None
    if not isinstance(rev, int) or isinstance(rev, bool) or rev < 0:
        return None
    if exp is None:
        exp = 0
    if not isinstance(exp, int) or isinstance(exp, bool) \
            or not 0 <= exp <= 255:
        return None
    title = (title or "").strip()[:64] or None
    return {"propName": prop.strip()[:64], "line1": line1.strip()[:64],
            "line2": line2.strip()[:64], "titleCash": title,
            "expireDays": exp, "rev": rev}


class TicketHeaderState:
    """C2 ticket-header push state — the rev-gated bridge between the
    hub's report reply and the 0x7C/0x7D wire write.

    Threading (the house two-thread pattern): the REPORTER thread swaps
    .target (one atomic dict-slot write via on_reply) and reads
    applied_rev/detail for the snapshot; everything else — the session
    counter, the attempt gate, the verdict — is POLL-thread-only.

    The gate: an apply is due when the hub's rev differs from the last
    APPLIED rev AND this (online-session, rev) pair has not been
    attempted yet. One attempt per rev per online-session — a failure
    never hot-loops; it retries only on the next rev bump or the next
    machine rejoin (on_online bumps the session). applied_rev lives in
    memory only, so a satellite restart re-applies once — a harmless
    idempotent write of the same text."""

    def __init__(self):
        self.target = None       # validated reply dict | None
        self.applied_rev = None  # last rev the machine ACKed
        self.detail = "no ticket header received from the hub yet"
        self._session = 0        # bumped on every machine-online edge
        self._attempted = None   # (session, rev) of the last wire attempt

    def on_reply(self, value) -> None:
        """Reporter thread: ingest the raw reply value (junk = no-op)."""
        parsed = parse_ticket_data_reply(value)
        if parsed is not None:
            self.target = parsed

    def on_online(self) -> None:
        """Poll thread: machine-online edge — re-arms one attempt for a
        rev that failed in the previous online session."""
        self._session += 1

    def due(self):
        """Poll thread: the target dict if an apply attempt is due now,
        else None. Never due for the already-applied rev (same rev never
        re-sends) or an already-attempted (session, rev)."""
        t = self.target
        if t is None or t["rev"] == self.applied_rev:
            return None
        if self._attempted == (self._session, t["rev"]):
            return None
        return t

    def mark_attempted(self, rev: int) -> None:
        """Poll thread, BEFORE the wire try — so even a raising apply
        burns this (session, rev)'s one attempt instead of hot-looping."""
        self._attempted = (self._session, rev)

    def record_result(self, rev: int, ok: bool, detail: str) -> None:
        """Poll thread: the attempt's verdict (rides the snapshot)."""
        if ok:
            self.applied_rev = rev
        self.detail = detail

    def snapshot(self):
        """Reporter thread: the C2 snapshot block, shape-stable in every
        state: {"appliedRev": int|null, "detail": str}."""
        return {"appliedRev": self.applied_rev, "detail": self.detail}


def _ticket_set_flag(resp, address, command, proto):
    """A 0x7C/0x7D response -> True (ACK flag 01) / False (NACK flag 00) /
    None for silence, a foreign address, a bad CRC or a malformed flag —
    the core classify rule: corruption is silence, never consent."""
    if not resp or len(resp) < 4:
        return None
    packet = proto.parse_packet(resp)
    if packet is None or packet.address != address \
            or packet.command != command:
        return None
    try:
        return parse_ticket_data_flag(packet.data)
    except ValueError:
        return None


def apply_ticket_header(transport, address, target, protocol=None,
                        pace=SAS_POLL_FLOOR, sleep=time.sleep,
                        host_id=DEFAULT_SYSTEM_ID):
    """Push one hub ticket header to the machine. Returns (ok, detail).

    Wire plan (adjudicated 2026-07-09 from the on-disk SAS 6.02 text —
    the full citation lives at the builders in core/sas_tito_host.py):

      1. 0x7C Set Extended Ticket Data (§15.3) — the PRIMARY write:
         code 00 Location = propName, 01 Address 1 = line1, 02 Address 2
         = line2. Extended-validation hosts use 7C, its data takes
         precedence over 7D, and it has no positional side effects.
      1b. On a 7C ACK: 0x7D in its text-less form (host ID + the hub's
         expireDays byte, 00 = never) — 7C has no expiration slot, so
         this is the only host write for the printed-ticket expiration.
         Advisory: its verdict rides the detail but never fails a header
         that 7C already applied.
      2. 0x7D Set Ticket Data (§15.4) — the fallback, ONLY on 7C silence
         (an old/standard-validation machine that never learned 7C). Its
         positional layout forces a host ID (our system id) and the
         expiration byte alongside the same three text lines.

    A 7C NACK (flag 00) means the machine UNDERSTOOD and refused — no 7D
    escalation, honest failure verdict instead. titleCash has NO SAS
    mapping (6.02 offers only the restricted/debit titles, codes 10/20 —
    printing a cash title there would brand the WRONG tickets), so it is
    reported as skipped, never guessed onto the wire. Empty line1/line2
    are OMITTED (C1: never push empty over existing machine config).
    Poll-thread only; one sleep(pace) separates consecutive polls."""
    proto = protocol or SASProtocol()
    loc = target["propName"]
    a1 = target["line1"] or None
    a2 = target["line2"] or None
    days = int(target.get("expireDays") or 0)
    note = (" · titleCash skipped: no SAS field for the cash ticket title "
            "(6.02 §15.3 has only restricted/debit titles)"
            if target.get("titleCash") else "")
    sent = [f for f, v in (("propName", loc), ("line1", a1), ("line2", a2))
            if v is not None]
    fields = "+".join(sent)

    resp = transport.transact(build_set_extended_ticket_data(
        address, location=loc, address1=a1, address2=a2))
    flag = _ticket_set_flag(resp, address, CMD_SET_EXTENDED_TICKET_DATA,
                            proto)
    if flag is True:
        # 0x7C carried the text but has NO expiration slot — 0x7D's
        # host-id+expiration-only form (allow_no_text) is the one way to
        # set it, and §15.4 precedence means this 7D cannot disturb the
        # 7C text. Sent even for 0 (never expires): the hub value must WIN
        # over whatever a previous host once burned into the machine. Its
        # verdict rides the detail but never fails the header apply — an
        # old machine that NACKs/ignores a text-less 7D keeps its header.
        sleep(pace)
        resp = transport.transact(build_set_ticket_data(
            address, host_id=host_id, expiration_days=days,
            allow_no_text=True))
        eflag = _ticket_set_flag(resp, address, CMD_SET_TICKET_DATA, proto)
        edetail = {True: f" · expiration {days}d via 0x7D (ACK)",
                   False: f" · expiration {days}d 0x7D NACK — machine "
                          "refused (expiration unchanged)",
                   None: f" · expiration {days}d 0x7D silent — machine "
                         "may not honor host expiration"}[eflag]
        return True, f"applied {fields} via 0x7C (ACK){edetail}{note}"
    if flag is False:
        return False, ("0x7C NACK — the machine flagged the ticket data "
                       "invalid; no 0x7D fallback (it understood and "
                       f"refused){note}")

    sleep(pace)
    resp = transport.transact(build_set_ticket_data(
        address, host_id=host_id, expiration_days=days,
        location=loc, address1=a1, address2=a2))
    flag = _ticket_set_flag(resp, address, CMD_SET_TICKET_DATA, proto)
    if flag is True:
        return True, (f"applied {fields} + expiration {days}d via 0x7D "
                      f"fallback (0x7C unsupported/silent){note}")
    if flag is False:
        return False, ("0x7D NACK — the machine flagged the ticket data "
                       f"invalid (0x7C was silent){note}")
    return False, ("silence on both 0x7C and 0x7D — ticket header not "
                   "applied (retries on the next rev bump or "
                   f"rejoin){note}")


class HubReporter:
    """POSTs machine-state snapshots to the hub's /api/sas/report every
    REPORT_SEC (the protocol-agnostic floor: the hub's ONE Home UI shows
    G2S and SAS machines side by side). Runs as a daemon thread; failures
    are edge-logged (first failure + recovery), never per-cycle spam — a
    hub that's down must not flood the SMIB's journal (GR-01/02). The
    hub's reply carries a 'commands' list: the report cycle doubles as
    the hub->satellite command channel (legacy_bonus, handpay_reset).
    Since C2 it also carries the hub's switch state — "sasEnabled" (park/
    resume the poll loop) and "sysvalFallback" (answer 0x57 or not) —
    applied via on_settings; old hubs simply omit both keys.

    Threading contract: the SAS transport is NOT thread-safe, so this
    thread NEVER touches it. Commands land in pending_commands (deque —
    same lock-free two-thread pattern as recent_events) and the POLL
    thread drains + executes them between polls (stop_or_heartbeat);
    results ride back to the hub in the next snapshot()."""

    #: hub->SMIB queue bound: a burst beyond this drops OLDEST commands
    #: (deque maxlen) — better to lose a stale queued bonus than to fire
    #: a surprise backlog at the machine minutes later.
    MAX_PENDING = 8

    def __init__(self, hub_url, smib_id, port_path, address, poller, stats,
                 recent_events, store, sas_enabled=None, handpay_latch=None,
                 on_settings=None, ticket_header=None):
        self.url = hub_url.rstrip("/") + "/api/sas/report"
        self.smib_id = smib_id
        self.port_path = port_path
        self.address = address
        self.poller = poller
        self.stats = stats
        self.recent_events = recent_events
        self.store = store
        # C2/C3 wiring (all optional so bare constructions keep today's
        # behavior): sas_enabled is main()'s park Event (set = polling),
        # handpay_latch the HandpayLatch, on_settings the applier for the
        # reply's sasEnabled/sysvalFallback keys (called with RAW values;
        # it alone decides what counts as a real bool), ticket_header the
        # TicketHeaderState (RAW reply "ticketData" in via on_reply,
        # {appliedRev, detail} out via snapshot).
        self.sas_enabled = sas_enabled
        self.handpay_latch = handpay_latch
        self.on_settings = on_settings
        self.ticket_header = ticket_header
        self.started_at = time.strftime("%Y-%m-%dT%H:%M:%S")
        self.failing = False
        self.stop = False
        self.pending_commands = collections.deque(maxlen=self.MAX_PENDING)
        self.command_results = collections.deque(maxlen=20)
        # Dedupe: the hub re-sends a command until it sees its result echoed
        # back, so remember recently-seen ids (bounded).
        self._seen_cmd_ids = collections.deque(maxlen=64)

    def snapshot(self):
        st = self.poller.state
        now = time.monotonic()
        # C3: effective SAS state. While the hub has this machine disabled
        # the poll loop is parked, so st.online is a frozen pre-park value —
        # report offline (honest: we are not talking to the machine).
        enabled = (self.sas_enabled.is_set()
                   if self.sas_enabled is not None else True)
        with self.store.lock:
            tickets = list(self.store.state.get("tickets", {}).values())
        by_state = collections.Counter(t.get("state") for t in tickets)
        return {
            "protocol": "SAS",
            "smibId": self.smib_id,
            "address": str(self.address),
            "port": self.port_path,
            "online": bool(st.online) and enabled,
            "lastSeenAgoSec": round(now - st.last_seen, 1)
                              if st.last_seen else None,
            "polls": self.stats.get("polls", 0),
            "events": self.stats.get("events", 0),
            "meterChanges": self.stats.get("meter_changes", 0),
            # Keys per the REPORT KEY CONTRACT at POLLED_METERS: hex for
            # scalar polls, short labels for the flattened 0x0F composite.
            "meters": {meter_report_key(code): val for (a, code), val
                       in list(self.stats["last_meters"].items())[:64]
                       if a == self.address},
            "recentEvents": list(self.recent_events),
            "tickets": {"total": len(tickets), **dict(by_state)},
            # AFT registration truth, cached by the poll thread (single
            # writer) and read here (single reader) — lock-free like meters.
            # None until the first cache read; the hub clamps + ingests it.
            "aft": self.stats.get("aft"),
            "commandResults": list(self.command_results),
            # C3: both keys are ALWAYS present (pendingHandpay rides as
            # null when clear) so the payload shape is stable for the hub.
            "sasEnabled": enabled,
            "pendingHandpay": (self.handpay_latch.pending
                               if self.handpay_latch is not None else None),
            # C2 ticket header: {"appliedRev": int|null, "detail": str}
            # in every state once wired (null on bare constructions).
            "ticketData": (self.ticket_header.snapshot()
                           if self.ticket_header is not None else None),
            "startedAt": self.started_at,
        }

    def _take_commands(self, reply_body: bytes) -> None:
        """Parse the hub's report reply and queue fresh commands for the
        poll thread. Malformed replies are ignored (a hub that answers
        200-with-garbage must not flip the edge-logged failure state)."""
        try:
            data = json.loads(reply_body or b"{}")
        except ValueError:
            return
        if not isinstance(data, dict):
            return
        # C2: the hub's switch state rides the same reply as the command
        # channel. Values pass through RAW — the applier (main()'s
        # apply_hub_settings) honors EXACT bools only, so junk ("false",
        # 0, null) or an old hub omitting the keys changes nothing. Guarded
        # like the rest of ingest: a raising applier must not flip the
        # edge-logged failure state.
        if self.on_settings is not None:
            try:
                self.on_settings(data.get("sasEnabled"),
                                 data.get("sysvalFallback"))
            except Exception:                       # noqa: BLE001
                pass
        # C2: the ticket-header target rides the same reply (present ONLY
        # when the hub has a propName set). RAW value in — the state's
        # exact-shape guard (parse_ticket_data_reply) decides what counts;
        # junk or an absent key changes nothing, and a raising ingest must
        # not flip the edge-logged failure state.
        if self.ticket_header is not None:
            try:
                self.ticket_header.on_reply(data.get("ticketData"))
            except Exception:                       # noqa: BLE001
                pass
        commands = data.get("commands", [])
        if not isinstance(commands, list):
            return
        for cmd in commands[:self.MAX_PENDING]:
            if not isinstance(cmd, dict):
                continue
            cmd_id = cmd.get("id")
            if not isinstance(cmd_id, str) or not cmd_id:
                continue
            if cmd_id in self._seen_cmd_ids:
                continue                     # hub re-send until result echoed
            self._seen_cmd_ids.append(cmd_id)
            self.pending_commands.append(cmd)

    def run(self):
        while not self.stop:
            try:
                body = json.dumps(self.snapshot()).encode()
                req = urllib.request.Request(
                    self.url, data=body,
                    headers={"Content-Type": "application/json"})
                with urllib.request.urlopen(req, timeout=4) as resp:
                    self._take_commands(resp.read())
                if self.failing:
                    self.failing = False
                    logger.info("hub reporting RECOVERED ({})", self.url)
            except Exception as e:
                if not self.failing:
                    self.failing = True
                    logger.warning("hub report failed ({}: {}) — will keep "
                                   "retrying quietly every {}s",
                                   self.url, e, REPORT_SEC)
            time.sleep(REPORT_SEC)


class RxWedgeWatchdog:
    """Self-heal the machine-reboot serial wedge (live-diagnosed 2026-07-10).

    A BB2 power-cycle deafens the Zero's PL011 RX while TX keeps flowing:
    the machine answers every poll unheard, the tile goes offline, and the
    cabinet LEDs gaslight everyone (fingerprint: /proc/tty/driver/ttyAMA tx
    climbing, rx frozen). This wedge struck three+ times and even
    manufactured a false "one protocol at a time" architecture conclusion
    before it was caught. Cure = close+reopen the port; this class does it
    automatically after WEDGE_REOPEN_SEC of total poll silence.

    Cadence: reopen every reopen_sec while silent; after slow_after dry
    reopens (machine genuinely OFF, not wedged) back off to slow_sec so a
    dead line doesn't churn the device node forever. ANY valid answer
    resets the dry counter and the fast phase. Logging is throttled: dry
    reopens #1, #2, then every 10th — plus a recovery line when the link
    comes back.

    THREADING: tick() runs on the poll thread only (from the run() stop
    callback, between poll_once calls — the same ownership rule as the
    park loop), so the reopen never races a wire exchange. bump() marks
    "silence is expected" (parked / just resumed) and is also poll-thread
    only. Time is injected by the caller (monotonic seconds) — no clock
    reads here, so tests drive it deterministically."""

    def __init__(self, port, state,
                 reopen_sec=WEDGE_REOPEN_SEC,
                 slow_after=WEDGE_SLOW_AFTER,
                 slow_sec=WEDGE_SLOW_SEC):
        self.port = port                  # must expose .reopen()
        self.state = state                # SASPoller MachineState (.answers)
        self.reopen_sec = float(reopen_sec)
        self.slow_after = int(slow_after)
        self.slow_sec = float(slow_sec)
        self.dry_reopens = 0              # consecutive reopens with no answer
        self.reopens_total = 0
        self._answers_seen = int(state.answers)
        self._last_ok = None              # set on first tick()/bump()

    def bump(self, now):
        """Silence is expected right now (poll loop parked, or just
        resumed) — restart the silence clock without touching the port."""
        self._last_ok = now

    def tick(self, now):
        """Call between polls. Returns True if the port was reopened."""
        if self._last_ok is None:
            self._last_ok = now           # arm on first sight, never insta-fire
            return False
        answers = int(self.state.answers)
        if answers != self._answers_seen:  # heard the machine since last tick
            self._answers_seen = answers
            if self.dry_reopens:
                logger.info("🔧 RX watchdog: link recovered after {} port "
                            "reopen(s)", self.dry_reopens)
            self.dry_reopens = 0
            self._last_ok = now
            return False
        cadence = (self.slow_sec if self.dry_reopens >= self.slow_after
                   else self.reopen_sec)
        if now - self._last_ok < cadence:
            return False
        self._last_ok = now
        self.dry_reopens += 1
        self.reopens_total += 1
        loud = self.dry_reopens <= 2 or self.dry_reopens % 10 == 0
        try:
            self.port.reopen()
            if loud:
                logger.info("🔧 RX-silence watchdog: no valid frames for "
                            "{:.0f}s — serial port reopened (dry reopen #{}: "
                            "wedge-heal, or the machine is just off)",
                            cadence, self.dry_reopens)
        except Exception as e:            # noqa: BLE001 — device may be gone
            logger.warning("🔧 RX watchdog: port reopen FAILED ({}: {}) — "
                           "retrying in {:.0f}s", type(e).__name__, e,
                           cadence)
        return True


def open_port(path, mock, address, protocol):
    """Open the transport, retrying forever on failure. A backup channel
    whose HAT isn't installed yet must wait, not crash-loop the unit."""
    if mock:
        from tools.sas_bench_poll import make_mock_machine
        from transport.serial.sas_serial import MockSASSerialPort
        logger.info("mock machine on address {} (self-test, no hardware)",
                    address)
        return MockSASSerialPort(make_mock_machine(protocol, address))
    from transport.serial.sas_serial import SASSerialPort
    while True:
        try:
            port = SASSerialPort(path)
            logger.info("{} open @ 19200 8-mark/space-1, low-latency {}",
                        path, "ON" if port.low_latency else "N/A")
            return port
        except Exception as e:
            logger.warning("cannot open {} ({}) — retrying in {}s "
                           "(HAT not installed yet is fine)",
                           path, e, OPEN_RETRY_SEC)
            time.sleep(OPEN_RETRY_SEC)


def _gateway_hub_url(port=8081, fallback="http://192.168.50.2:8081"):
    """Derive the hub URL from the IPv4 default gateway (/proc/net/route) —
    on the slot VLAN + Wi-Fi AP the hub IS the DHCP server AND the default
    gateway, so a SAS SMIB can find it with no hardcoded IP. Used only when
    --hub is the literal 'auto' (omitting --hub still means NO reporting, the
    dev/smoke default). Falls back to the known hub IP with no default
    route; HubReporter is late-bound + retry-forgiving, so a late gateway
    just starts working."""
    # Dual-homed satellites have TWO default routes (wired + Wi-Fi) — pick the
    # LOWEST-METRIC one (the kernel's preferred egress = wired), never the first
    # line seen, or a Zero could report over the flaky Wi-Fi leg.
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
                if dest == "00000000" and (flags & 0x2):   # default + gateway
                    if best_metric is None or metric < best_metric:
                        best_metric = metric
                        best_ip = socket.inet_ntoa(
                            struct.pack('<L', int(gw, 16)))
    except (OSError, ValueError):
        pass
    return f"http://{best_ip}:{port}" if best_ip else fallback


def _default_smib_id():
    """smib-<pi-serial-tail> on a Pi, else the hostname — the SAS half of the
    ONE-identical-image story (mirrors Companion.default_companion_id). Two
    identical SAS cards share a hostname, so a hostname-based smibId would
    collide their floor legs on the hub; the silicon serial is unique per
    board and needs no operator input.

    ⚠️ MIGRATION: this CHANGED the default from socket.gethostname(). A SMIB
    whose leg identity came from its hostname (e.g. smib-bb2, whose hostname is
    literally "smib-bb2" — NOT a pinned --smib-id) will RENAME its floor leg
    (smib-bb2/1 -> smib-<serial>/1) if this code is deployed there and the
    service restarts, orphaning its sasLink / nickname / AFT registration /
    reader co-location. Such a board must be PINNED with an explicit
    `--smib-id <its-current-name>` in its unit BEFORE any redeploy (an explicit
    --smib-id always wins). New flagless golden-image cards are unaffected."""
    try:
        with open("/proc/cpuinfo", encoding="ascii", errors="replace") as f:
            for line in f:
                if line.startswith("Serial"):
                    val = line.split(":", 1)[1].strip().lower()
                    val = val.lstrip("0") or "0"
                    return f"smib-{val[-6:]}"
    except OSError:
        pass
    return socket.gethostname()


def main():
    ap = argparse.ArgumentParser(
        description="SAS backup-channel host (validated poller + TITO)")
    ap.add_argument("port", nargs="?",
                    help="serial device, e.g. /dev/ttyAMA0 (Pi 5 header UART)")
    ap.add_argument("--address", type=int, default=1,
                    help="machine SAS address (1-127, operator-menu setting)")
    ap.add_argument("--interval", type=float, default=0.2,
                    help="general-poll cadence (default 200ms = the SAS "
                         "floor; never below 0.2 for unknown firmware)")
    ap.add_argument("--mock", action="store_true",
                    help="in-memory machine self-test (no hardware)")
    ap.add_argument("--max-polls", type=int, default=None,
                    help="stop after N polls (smoke tests); default: forever")
    ap.add_argument("--hub", default=None,
                    help="hub base URL to report state to (the G2S host, "
                         "e.g. http://192.168.50.2:8081 from a Zero SMIB or "
                         "http://127.0.0.1:8081 co-located). Pass 'auto' for "
                         "zero-config (derive http://<default-gateway>:8081 — "
                         "the hub is the gateway). Omitted = no reporting")
    ap.add_argument("--smib-id", default=_default_smib_id(),
                    help="identity in the hub's floor registry (default: "
                         "smib-<pi-serial-tail> so one identical SAS image "
                         "self-names per board; override to pin a name)")
    ap.add_argument("--aft-key", default=None,
                    help="AFT registration key as 40 hex chars (20 bytes); "
                         "default: the built-in rig key. A RAM-cleared "
                         "machine is keyless, so the host always supplies it.")
    ap.add_argument("--aft-pos", type=int, default=1,
                    help="AFT POS id (host constant, 4-byte wire field; "
                         "default 1 = the BB2 live-proven value)")
    args = ap.parse_args()

    # Zero-config hub discovery: 'auto' derives the gateway (= the hub). A
    # real URL passes through; omitting --hub still means no reporting.
    if args.hub == "auto":
        args.hub = _gateway_hub_url()
        logger.info("--hub auto -> {}", args.hub)

    if args.aft_key:
        try:
            aft_key = bytes.fromhex(args.aft_key)
        except ValueError:
            ap.error("--aft-key must be hex (40 chars = 20 bytes)")
        if len(aft_key) != 20:
            ap.error(f"--aft-key must be exactly 20 bytes, got {len(aft_key)}")
    else:
        aft_key = DEFAULT_AFT_KEY
    if not 0 <= args.aft_pos < 2 ** 32:
        ap.error("--aft-pos must be 0..4294967295")
    aft_pos = int(args.aft_pos).to_bytes(4, "little")

    if not (1 <= args.address <= 127):
        ap.error(f"--address must be 1..127 (got {args.address})")
    if not args.mock and not args.port:
        ap.error("either a serial port or --mock is required")
    if args.interval < 0.2 and not args.mock:
        logger.warning("--interval {}s is below the 200ms SAS floor; a real "
                       "machine may ignore polls this fast", args.interval)

    protocol = SASProtocol()
    port = open_port(args.port, args.mock, args.address, protocol)

    # ---- state-edge callbacks: log transitions, never per-poll noise ------
    stats = {"events": 0, "meter_changes": 0, "last_meters": {}}
    recent_events = collections.deque(maxlen=20)   # newest-first, for the hub

    # C3 switch state — owned HERE (sas_host), not by the poller core. The
    # Event is the park flag the poll thread checks in stop_or_heartbeat
    # (see the park loop there for why SASPoller needed no change); set =
    # polling. Boots ENABLED until the hub's report reply says otherwise.
    sas_enabled = threading.Event()
    sas_enabled.set()
    hp_latch = HandpayLatch()          # the HANDPAY banner state (C3)
    ticket_header = TicketHeaderState()  # the C2 ticket-header push state

    def on_online(addr):
        logger.info("🎰 SAS machine ONLINE at address {}", addr)
        # C2: a rejoin re-arms ONE ticket-header attempt for a rev that
        # failed in the previous online session (poll-thread callback).
        ticket_header.on_online()

    def on_offline(addr):
        logger.warning("SAS machine OFFLINE at address {} (5+ silent polls)",
                       addr)

    def on_typed_event(addr, info):
        stats["events"] += 1
        hp_latch.on_exception(info.code)   # C3 latch: 0x51 sets, 0x52 clears
        name = getattr(info, "name", "") or ""
        logger.info("🔔 addr {} exception 0x{:02X} {}", addr, info.code, name)
        recent_events.appendleft({
            "code": f"0x{info.code:02X}", "label": name,
            "when": time.strftime("%Y-%m-%dT%H:%M:%S")})

    # Meter ingest: on_meter deliveries (scalars + the 0x0F composite dict)
    # flatten into stats["last_meters"] via ingest_meter_reading — see the
    # REPORT KEY CONTRACT at POLLED_METERS.
    on_meter = make_on_meter(stats)

    def on_handpay_reset(addr, result):
        logger.info("🔑 addr {} handpay reset: {}", addr, result)

    def on_handpay_info(addr, info):
        # The poll loop collected a queued handpay record (LP 0x1B) in response
        # to exception 0x51 — this is what drains the machine's handpay queue
        # and stops the endless 0x51 stream (§7.8.1). Layout is unadjudicated;
        # log the RAW record for bench capture and surface it on the tape.
        logger.info("🧾 addr {} handpay info collected (0x1B): {} "
                    "[{} record bytes]", addr, info.raw.hex(), len(info.data))
        recent_events.appendleft({
            "code": "0x1B", "label": "handpay info collected (queue drained)",
            "when": time.strftime("%Y-%m-%dT%H:%M:%S")})

    poller = SASPoller(port, address=args.address, protocol=protocol,
                       meters=list(POLLED_METERS),
                       on_online=on_online, on_offline=on_offline,
                       on_typed_event=on_typed_event, on_meter=on_meter,
                       on_handpay_reset=on_handpay_reset,
                       on_handpay_info=on_handpay_info)

    # RX-silence watchdog (task #21) — only for a real port that can reopen;
    # the mock transport has no .reopen() and never wedges. None => no-op.
    wedge_watch = (RxWedgeWatchdog(port, poller.state)
                   if not args.mock and hasattr(port, "reopen") else None)

    def on_system_validation(addr, result):
        # Edge-triggered (once per 0x57 serviced/rejected/timeout), not per
        # poll — the responder runs inline on the poll thread's typed-event
        # path, so this only fires when a cash-out is actually validated.
        logger.info("🎟️ addr {} system-validation {}: {}",
                    addr, result.outcome.value, result.detail)
        recent_events.appendleft({
            "code": "0x57", "label": f"system-validation {result.outcome.value}",
            "when": time.strftime("%Y-%m-%dT%H:%M:%S")})

    def on_validation_seeded(addr, result):
        # Edge-triggered: the EGM raised 0x3F "validation ID not configured"
        # (enhanced validation, e.g. after a RAM clear) and we auto-sent the
        # one-time 0x4C seed. ACCEPTED = tilt cleared; the EGM now self-mints
        # its own validation numbers with no per-cashout host round-trip.
        ok = getattr(result, "ok", False)
        logger.info("🎟️ addr {} enhanced validation-ID {}: {}", addr,
                    "SEEDED" if ok else "seed ignored",
                    getattr(result, "detail", result))
        recent_events.appendleft({
            "code": "0x4C",
            "label": f"validation-id {'seeded' if ok else 'ignored'}",
            "when": time.strftime("%Y-%m-%dT%H:%M:%S")})

    # hub.db phase 2: with a hub, the HUB is the ticket authority — mints,
    # adjudications and closes go to /api/tito/* so a ticket printed here
    # redeems on any machine on the floor (and an AVP voucher redeems HERE).
    # The local JSON store stays underneath as the forensic copy + the safe
    # fallback (sid-02 mints, journaled closes) when the hub is unreachable.
    # Without --hub (bench/mock runs) the plain local store is the authority,
    # exactly as before.
    if args.hub:
        store = HubTicketAuthority(args.hub, args.smib_id, TicketStore())
        logger.info("ticket authority: HUB {} (local store is fallback/"
                    "journal)", args.hub)
    else:
        store = TicketStore()   # SAS/data/sas_ticket_state.json (auto-created)
    tito_host = SASTITOHost(
        poller, store,
        on_ticket_issued=lambda a, r: logger.info(
            "🎫 addr {} ticket issued: {}", a, r),
        on_redemption=lambda a, r: logger.info(
            "🎫 addr {} ticket redemption: {}", a, r),
        on_system_validation=on_system_validation,
        on_validation_seeded=on_validation_seeded)

    # AFT host-cashout (EGM_TO_HOST) arm state — the machine->wallet RETURN
    # leg (the peer of the host->EGM fund push). Every field is touched ONLY
    # on the poll thread (the arm command drains there, on_cashout_request
    # fires there off the general-poll FIFO, and the between-polls handler
    # runs there), so no lock is needed — the same single-writer discipline
    # as stats["aft"]/stats["last_meters"]. "armed" gates whether a machine's
    # 0x6A cash-out request is answered (an UNARMED 0x6A is left to the
    # machine's own default — a ticket — never silently pulled); "accountId"
    # is echoed to the hub so the confirmed completion credits the right
    # wallet; "pending" hands a raised 0x6A to handle_cashout_if_pending()
    # between polls (never a re-entrant transact inside the exception cb).
    cashout_state = {"armed": False, "pending": False, "accountId": None}

    def on_cashout_request(addr, code):
        # Machine raised an AFT host-cashout request (believed 0x6A/0x6B) off
        # the general poll — the cash-out button on an armed machine. Only a
        # machine WE armed is answered: flag it for the between-polls handler
        # on THIS poll thread. Setting a flag is the whole job here (the
        # transport is mid-exchange in the exception callback — never transact
        # from inside it).
        if cashout_state["armed"]:
            cashout_state["pending"] = True
            logger.info("🏦 addr {} host-cashout request 0x{:02X} — armed, "
                        "answering EGM->host between polls", addr, code)
        else:
            logger.info("🏦 addr {} host-cashout request 0x{:02X} — NOT armed"
                        " — left to the machine default (ticket)", addr, code)

    # AFT auto-registration host (the 🎫 register path). Built with NO asset —
    # auto_register() READS the operator-set asset off the machine and
    # registers the fixed rig key against it; the response key is then adopted
    # for any later transfer. Constructed AFTER SASTITOHost so both chain
    # poller.on_typed_event (each forwards prev) and TITO events still fire.
    # on_cashout_request wires the machine-initiated host-cashout (0x6A) path.
    aft_host = AFTHost(poller, asset_number=None, registration_key=aft_key,
                       pos_id=aft_pos, sleep=time.sleep,
                       on_cashout_request=on_cashout_request)
    logger.info("AFT register ready: rig key fp {} · POS {}",
                aft_key.hex()[:12], args.aft_pos)

    def apply_hub_settings(enabled, sysval):
        """Apply the C2 report-reply keys. Runs on the REPORTER thread;
        both targets are single atomic writes the poll thread reads (the
        Event and a bool attribute), so no lock is needed. Only an EXACT
        bool applies — absent keys or junk ("false", 0, null) from an old
        or confused hub change NOTHING. Each real flip logs ONCE; the
        steady-state reply echo every REPORT_SEC is silent."""
        if isinstance(enabled, bool) and enabled != sas_enabled.is_set():
            if enabled:
                sas_enabled.set()
                logger.info("🔌 SAS ENABLED by hub — resuming the poll loop")
            else:
                sas_enabled.clear()
                logger.info("🔌 SAS DISABLED by hub — parking the poll loop "
                            "(port stays open, no wire traffic until "
                            "re-enabled in Settings)")
        if isinstance(sysval, bool) and sysval != tito_host.auto_service:
            # SASTITOHost checks auto_service at 0x57 dispatch time, so a
            # runtime attribute flip is the whole sysval-fallback wiring —
            # no core change. False = a legacy system-validation cash-out
            # (0x57) goes unanswered and falls to the machine's own
            # handpay path; the manual service_validation entry point (and
            # every other TITO flow) stays available either way.
            tito_host.auto_service = sysval
            logger.info("🎫 system-validation fallback {} by hub",
                        "ENABLED" if sysval else "DISABLED")

    reporter = None
    if args.hub:
        reporter = HubReporter(args.hub, args.smib_id,
                               args.port or "(mock)", args.address,
                               poller, stats, recent_events, store,
                               sas_enabled=sas_enabled,
                               handpay_latch=hp_latch,
                               on_settings=apply_hub_settings,
                               ticket_header=ticket_header)
        threading.Thread(target=reporter.run, daemon=True,
                         name="hub-reporter").start()
        logger.info("reporting to hub {} every {}s as smibId={}",
                    args.hub, REPORT_SEC, args.smib_id)

    stop = {"flag": False}

    def on_signal(signum, frame):
        logger.info("signal {} — stopping cleanly", signum)
        stop["flag"] = True
        if reporter is not None:
            reporter.stop = True   # let the reporter thread exit its loop

    signal.signal(signal.SIGTERM, on_signal)
    signal.signal(signal.SIGINT, on_signal)

    logger.info("SAS host polling addr {} every {}s (offline is normal "
                "until the machine is wired + SAS enabled)",
                args.address, args.interval)

    # Heartbeat wrapper: proof-of-life in the journal every HEARTBEAT_SEC
    # without logging each of the ~5 polls/second.
    hb = {"last": time.monotonic()}
    aft_next = {"at": 0.0}       # next due time for the AFT status cache read
    stats["polls"] = 0

    def run_hub_command(cmd):
        """Execute one hub command on the POLL thread (the only thread
        allowed to touch the transport). Every outcome — including a
        malformed command — produces a result record so the hub/UI can
        tell the truth; nothing here may raise (an exception would kill
        the poll loop via the bare stop() call in SASPoller.run)."""
        cmd_id = cmd.get("id", "?")
        result = {"id": cmd_id, "type": cmd.get("type"),
                  "when": time.strftime("%Y-%m-%dT%H:%M:%S")}
        if not sas_enabled.is_set():
            # C3: parked. EVERY current command type touches the machine
            # (wire polls), and the park promise is a silent port — refuse
            # with an honest typed record instead of queueing surprise wire
            # traffic. One record per command id (hub dedupe), so no
            # re-send log spam; the operator re-enables in Settings.
            result["ok"] = False
            result["outcome"] = "sas_disabled"
            result["detail"] = "SAS is disabled for this machine (Settings)"
            result["result"] = "rejected: sas disabled"
            logger.info("⛔ hub command {} ({}): refused — SAS disabled",
                        cmd_id, cmd.get("type"))
            return result
        try:
            if cmd.get("type") == "handpay_reset":
                # Two-step reset-to-credits (core/sas_handpay_reset.py:
                # 0xA8 method-select -> 0x94 reset -> 0x52 confirm) via
                # the poller's bridge entry point — same transport, same
                # poll thread; the ~1.4 s worst-case block (paced at the
                # SAS floor) is fine here. No parameters to validate —
                # if the machine is not locked up, the MACHINE says so
                # (typed no_handpay), so the button can fire blind.
                res = poller.reset_handpay_to_credits()
                if res.ok:
                    # C3: a machine-acked reset clears the HANDPAY banner
                    # (no-op if the 0x52 drained in the confirm window
                    # already did — the latch logs once either way).
                    hp_latch.on_reset_ok()
                result["ok"] = res.ok
                result["outcome"] = res.outcome.value
                result["detail"] = res.detail
                # "ack" stays the channel's one success verdict (the UI
                # keys on it, same as the bonus); failures carry the
                # typed outcome as the verdict string — honest, not "ack".
                result["result"] = "ack" if res.ok else res.outcome.value
                logger.info("🔑 hub command {}: handpay reset -> {} ({})",
                            cmd_id, res.outcome.value, res.detail)
                return result
            if cmd.get("type") == "aft_register":
                # Read-current-then-register (modules/aft AFTHost.auto_register):
                # 73/FF status -> already registered? no-op. Else READ the
                # operator asset (0x7B/0x74/73-echo) and register the rig key
                # against it. Idempotent, never assigns an asset, never invents
                # one. Same op as the hub auto-trigger. No parameters to
                # validate — the machine's own asset is the only input.
                ok, reg, detail = aft_host.auto_register()
                if ok:
                    outcome = "registered"
                elif "no asset number" in detail:
                    outcome = "asset_unknown"
                elif reg is not None:
                    outcome = "refused"
                else:
                    outcome = "error"
                result["ok"] = ok
                result["outcome"] = outcome
                result["detail"] = detail
                # SAME field vocabulary as the /api/sas/report aft block
                # (registered/statusCode/asset/posId/keyFp) — one vocabulary,
                # zero drift. The full 20-byte key NEVER rides the wire; only
                # keyFp (its 12-hex-char fingerprint).
                result["registered"] = bool(reg and reg.registered)
                result["statusCode"] = reg.status if reg else None
                result["asset"] = (int.from_bytes(reg.asset_number, "little")
                                   if reg and reg.asset_number
                                   and any(reg.asset_number) else None)
                result["posId"] = (int.from_bytes(reg.pos_id, "little")
                                   if reg and reg.pos_id
                                   and any(reg.pos_id) else None)
                result["keyFp"] = (reg.registration_key.hex()[:12]
                                   if reg and reg.registration_key
                                   and any(reg.registration_key) else None)
                result["result"] = "ack" if ok else outcome
                logger.info("🎫 hub command {}: aft register -> {} ({})",
                            cmd_id, outcome, detail)
                return result
            if cmd.get("type") == "aft_transfer":
                # House->EGM cashable AFT credit push (0x72) — the SAS peer of
                # the G2S WAT addCredits push. LOCK-FIRST by default (74/00
                # cond=bit0 -> LOCKED -> 72; AJ's field practice, and the BB2
                # in fact refuses otherwise); cmd {"lock": false} forces the
                # spec-legal lockless path (6.02 flags bit6=0) for firmware
                # that allows it. Amount in CENTS. The HUB debits the echoed
                # funding account (House by default) on this record's
                # confirmed success — we only move the credits and report
                # the CONFIRMED amount + txnId back.
                # R1 funding parity: echo the hub's funding accountId into
                # EVERY aft_transfer result record — success, refusal
                # (bad_amount / asset_unknown / not_registered), and the
                # exception path (fields set on `result` before a raise
                # survive into the catch-all's record at the bottom of
                # run_hub_command). The satellite never touches accounts —
                # echo only; the hub settles the debit against it.
                acct = cmd.get("accountId")
                if isinstance(acct, str) and acct:
                    result["accountId"] = acct[:64]
                cents = cmd.get("cents")
                if (not isinstance(cents, int) or isinstance(cents, bool)
                        or not 1 <= cents <= MAX_AFT_CENTS):
                    result["ok"] = False
                    result["outcome"] = "bad_amount"
                    result["detail"] = (f"cents must be an integer "
                                        f"1..{MAX_AFT_CENTS}")
                    result["result"] = "rejected: bad cents"
                    logger.warning("💵 hub command {}: aft push rejected "
                                   "(bad cents {})", cmd_id, cents)
                    return result
                result["cents"] = cents
                # Ensure AFT is registered first (idempotent: a REGISTERED
                # machine short-circuits on one 73/FF and ADOPTS its asset, so
                # the transfer uses the machine's real asset+key, never None).
                ok_reg, reg, reg_detail = aft_host.auto_register()
                if not ok_reg:
                    result["ok"] = False
                    result["outcome"] = ("asset_unknown"
                                         if "no asset" in reg_detail
                                         else "not_registered")
                    result["detail"] = f"cannot push — {reg_detail}"
                    result["result"] = result["outcome"]
                    logger.warning("💵 hub command {}: aft push blocked — {}",
                                   cmd_id, reg_detail)
                    return result
                # Lock-first by default: the WMS BB2 refuses a LOCKLESS host->EGM
                # push (status 0x82; 0x74/FF shows availXfers without bit0 while
                # unlocked — bench-proven 2026-07-09). Send lock:false to force
                # the spec-legal lockless path on firmware that supports it.
                require_lock = bool(cmd.get("lock", True))
                logger.info("💵 hub command {}: AFT ready ({}) — pushing {}c "
                            "host->EGM ({})", cmd_id, reg_detail, cents,
                            "lock-first" if require_lock else "lockless")
                req = AFTTransferRequest(
                    transaction_id=make_transaction_id(),
                    transfer_type=TRANSFER_TYPE_HOST_TO_EGM,
                    cashable_cents=cents,
                    transfer_code=TRANSFER_CODE_FULL_ONLY,
                    transfer_flags=TRANSFER_FLAGS_NONE)
                # raw 0x72 req/resp tap — keep the bytes in the journal so any
                # future refusal decodes against Table 8.3e without a re-run.
                res = aft_host.transfer(
                    req, require_lock=require_lock,
                    on_wire=lambda lbl, rq, rp: logger.info(
                        "💵 {} REQ {} | RESP {}", lbl, rq.hex(),
                        rp.hex() if rp else "(silence)"))
                if res.lock is not None:
                    logger.info("💵 lock 0x74: lockStatus=0x{:02X} "
                                "availXfers=0x{:02X} ({}) aftStatus=0x{:02X} "
                                "({})", res.lock.lock_status,
                                res.lock.available_transfers,
                                describe_available_transfers(
                                    res.lock.available_transfers),
                                res.lock.aft_status,
                                describe_aft_status(res.lock.aft_status))
                detail_extra = ""
                if not res.ok:
                    # self-diagnose: a 0x74/FF interrogate (read-only, no lock,
                    # no money) says WHETHER host->EGM is even permitted right
                    # now, decoded via the real Table 8.2b bitmaps. The one
                    # known machine-side trap (live 2026-07-09): a RAM clear
                    # resets the BB2's cashless config, turning OFF in-house
                    # transfers while leaving registration intact — every 0x72
                    # then draws 0x82 and every lock request draws 0xFF.
                    try:
                        ls = res.lock or aft_host.read_lock_status()
                        if ls is not None:
                            logger.info(
                                "💵 diag 0x74/FF: lockStatus=0x{:02X} "
                                "availXfers=0x{:02X} ({}) aftStatus=0x{:02X} "
                                "({}) cashable={}c limit={}c", ls.lock_status,
                                ls.available_transfers,
                                describe_available_transfers(
                                    ls.available_transfers),
                                ls.aft_status,
                                describe_aft_status(ls.aft_status),
                                ls.current_cashable_cents,
                                ls.transfer_limit_cents)
                            result["availXfers"] = ls.available_transfers
                            result["aftStatus"] = ls.aft_status
                            if not ls.aft_status & AFT_ST_IN_HOUSE_ENABLED:
                                detail_extra = (
                                    " — machine config: in-house AFT transfers"
                                    " are DISABLED on the game (a RAM clear"
                                    " resets this); re-enable AFT/cashless in"
                                    " the operator menu")
                        else:
                            logger.info("💵 diag 0x74/FF: silence/corrupt")
                    except Exception as de:       # noqa: BLE001
                        logger.warning("💵 diag lock read failed: {}", de)
                # Authoritative moved amount = what the MACHINE confirmed (full
                # == requested; partial < requested). 0 unless it actually
                # landed — the hub debits House by exactly this.
                confirmed = (res.final.cashable_cents
                             if res.final is not None
                             and res.final.cashable_cents is not None
                             else cents)
                result["ok"] = res.ok
                result["outcome"] = res.outcome.value
                result["detail"] = res.detail + detail_extra
                result["txnId"] = req.transaction_id
                result["amountCents"] = confirmed if res.ok else 0
                result["result"] = "ack" if res.ok else res.outcome.value
                logger.info("💵 hub command {}: aft push {}c -> {} ({})",
                            cmd_id, cents, res.outcome.value,
                            result["detail"])
                return result
            if cmd.get("type") == "aft_cashout_pull":
                # HOST-INITIATED cash-out to wallet ("host control"): the panel
                # button triggers this and the hub pulls the machine's cashable
                # credits machine->host->wallet NOW — no physical CASH-OUT press.
                # It is the funding push in reverse: read the machine's full
                # cashable (0x74/FF), then a LOCK-FIRST EGM_TO_HOST 0x72 (WE
                # initiate, so the machine has NOT self-locked — aft_transfer
                # locks for the FROM_EGM condition). Emits an "aft_cashout"
                # record (the SAME shape as the 0x6A button path) so the hub
                # credits the PINNED wallet on the CONFIRMED amount. accountId
                # is echoed but ADVISORY only — the hub credits its own arm-pin,
                # never the wire. Mirrors handle_cashout_if_pending exactly, but
                # lock-first + host-triggered. (A parked machine already refused
                # at the top of run_hub_command.)
                result["type"] = "aft_cashout"      # the hub settle keys on this
                acct = cmd.get("accountId")
                if isinstance(acct, str) and acct:
                    result["accountId"] = acct[:64]
                ok_reg, reg, reg_detail = aft_host.auto_register()
                if not ok_reg:
                    result["ok"] = False
                    result["outcome"] = ("asset_unknown" if "no asset"
                                         in reg_detail else "not_registered")
                    result["amountCents"] = 0
                    result["detail"] = f"cannot cash out — {reg_detail}"
                    result["result"] = "rejected"
                    logger.warning("🏦 hub command {}: host-pull blocked — {}",
                                   cmd_id, reg_detail)
                    return result
                ls = aft_host.read_lock_status()
                cashable = (ls.current_cashable_cents
                            if ls and ls.current_cashable_cents else 0)
                if ls is not None:
                    result["availXfers"] = ls.available_transfers
                if cashable <= 0:
                    result["ok"] = False
                    result["outcome"] = "no_credits"
                    result["amountCents"] = 0
                    result["detail"] = "machine reports no cashable credits"
                    result["result"] = "rejected"
                    logger.info("🏦 hub command {}: host-pull — no cashable "
                                "credits", cmd_id)
                    return result
                if cashable > MAX_AFT_CENTS:
                    result["ok"] = False
                    result["outcome"] = "over_ceiling"
                    result["amountCents"] = 0
                    result["detail"] = (f"cashable {cashable}c over the "
                                        f"{MAX_AFT_CENTS}c ceiling — cash out "
                                        "at the machine")
                    result["result"] = "rejected"
                    logger.warning("🏦 hub command {}: host-pull over ceiling "
                                   "({}c)", cmd_id, cashable)
                    return result
                req = AFTTransferRequest(
                    transaction_id=make_transaction_id(),
                    transfer_type=TRANSFER_TYPE_EGM_TO_HOST,
                    cashable_cents=cashable,
                    transfer_code=TRANSFER_CODE_FULL_ONLY,
                    transfer_flags=TRANSFER_FLAGS_NONE)
                logger.info("🏦 hub command {}: HOST-PULL {}c EGM->host "
                            "(lock-first, acct {})", cmd_id, cashable,
                            result.get("accountId") or "-")
                res = aft_host.transfer(
                    req, require_lock=True,
                    on_wire=lambda lbl, rq, rp: logger.info(
                        "🏦 {} REQ {} | RESP {}", lbl, rq.hex(),
                        rp.hex() if rp else "(silence)"))
                # Credit ONLY the machine-confirmed amount (never `cashable`) —
                # an unconfirmed completion credits NOTHING (verify meters).
                confirmed = (res.final.cashable_cents
                             if res.final is not None
                             and res.final.cashable_cents is not None
                             else None)
                credited = (confirmed if res.ok and confirmed is not None
                            else 0)
                result["ok"] = res.ok
                result["outcome"] = res.outcome.value
                result["txnId"] = req.transaction_id
                result["amountCents"] = credited
                result["detail"] = (res.detail if credited or not res.ok
                                    else res.detail + " — amount UNCONFIRMED, "
                                    "NOT credited (verify meters)")
                result["result"] = "ack" if res.ok else res.outcome.value
                logger.info("🏦 hub command {}: host-pull {}c -> {} ({})",
                            cmd_id, cashable, res.outcome.value, res.detail)
                return result
            if cmd.get("type") == "aft_set_host_cashout":
                # Arm/disarm AFT host-cashout so the machine's own CASH-OUT
                # BUTTON routes machine->host->wallet (EGM_TO_HOST) instead of
                # a ticket. The arm is PURELY THIS HUB-SIDE FLAG — no wire
                # write to the machine (the CONFIRMED 2026-07-12 model: the
                # machine is menu-set to "soft cash-out to host, fail to
                # ticket" and raises exception 0x6A on the button press). While
                # armed, handle_cashout_if_pending() answers the 0x6A between
                # polls with the EGM_TO_HOST transfer; unarmed, the 0x6A goes
                # unanswered and the machine tickets. set_host_cashout(enable)
                # is now READ-ONLY: on arm it does a 0x74/FF interrogate so the
                # reply can carry the machine's from-EGM readiness (the hub
                # gates on it); on disarm it touches no wire. accountId is
                # echoed back so the completion credits the right wallet (R1
                # parity with the fund push); disarm clears the armed state AND
                # any pending 0x6A so a stale press can't fire.
                enable = bool(cmd.get("enable", True))
                acct = cmd.get("accountId")
                if isinstance(acct, str) and acct:
                    result["accountId"] = acct[:64]
                cashout_state["armed"] = enable
                cashout_state["accountId"] = (result.get("accountId")
                                              if enable else None)
                if not enable:
                    cashout_state["pending"] = False
                ls = None
                try:
                    ls = aft_host.set_host_cashout(enable)
                except Exception as ce:            # noqa: BLE001 — best-effort
                    logger.warning("🏦 hub command {}: host-cashout arm wire "
                                   "error (armed flag still applied): {}",
                                   cmd_id, ce)
                result["ok"] = True
                result["outcome"] = "armed" if enable else "disarmed"
                result["armed"] = enable
                if ls is not None:
                    result["availXfers"] = ls.available_transfers
                    result["hostCashoutStatus"] = ls.host_cashout_status
                    result["fromEgmOk"] = bool(
                        ls.available_transfers & AVAIL_XFER_FROM_EGM)
                result["result"] = "ack"
                logger.info("🏦 hub command {}: AFT host-cashout {} (acct {})",
                            cmd_id, "ARMED" if enable else "disarmed",
                            result.get("accountId") or "-")
                return result
            if cmd.get("type") in ("sas_disable", "sas_enable"):
                # Machine lock/unlock = SAS shutdown 0x01 / startup 0x02
                # (SASCommandBuilder — type-S control polls, CRC even with no
                # data). VERIFY_ON_BENCH: the BB2's exact reply to 0x01/0x02 is
                # unproven — 0x01/0x02 are commonly NO-response, so an empty
                # read is a normal "sent" and a non-empty reply is a clean
                # "ack"; either way the operator sees the machine lock/unlock
                # on the glass. Applies immediately (no credit/idle gate).
                lock = cmd.get("type") == "sas_disable"
                builder = SASCommandBuilder(poller.protocol)
                frame = (builder.shutdown(args.address) if lock
                         else builder.startup(args.address))
                reply = poller.transport.transact(frame)
                result["locked"] = lock
                result["result"] = "ack" if reply else "sent"
                logger.info("{} hub command {}: SAS {} -> {}",
                            "🔒" if lock else "🔓", cmd_id,
                            "disable" if lock else "enable", result["result"])
                return result
            if cmd.get("type") == "set_validation_id":
                # Manual counterpart to the auto-seed off exception 0x3F: force
                # the enhanced-validation ID seed (0x4C) so a tilted (unseeded)
                # EGM can self-mint. Optional "system_id" (1..99) overrides the
                # barcode namespace. Operator-gated because on a mid-life EGM it
                # would reset the starting sequence (fine right after a RAM
                # clear; never fired automatically except off the unseeded-only
                # 0x3F). ACCEPTED = tilt cleared.
                sid = cmd.get("system_id")
                if (isinstance(sid, int) and not isinstance(sid, bool)
                        and 1 <= sid <= 99):
                    tito_host.system_id = sid
                res = tito_host.seed_validation_id(force=True)
                result["ok"] = bool(res and res.ok)
                result["outcome"] = res.outcome.value if res else "error"
                result["detail"] = res.detail if res else "no result"
                result["vgmId"] = (res.vgm_id.hex()
                                   if res and res.vgm_id else None)
                result["seq"] = (res.sequence.hex()
                                 if res and res.sequence else None)
                result["result"] = ("ack" if res and res.ok
                                    else result["outcome"])
                logger.info("🎟️ hub command {}: set validation id (sid={}) "
                            "-> {}", cmd_id, tito_host.system_id,
                            result["result"])
                return result
            if cmd.get("type") != "legacy_bonus":
                result["result"] = "rejected: unknown type"
                return result
            credits = cmd.get("credits")
            if (not isinstance(credits, int) or isinstance(credits, bool)
                    or not 1 <= credits <= MAX_BONUS_CREDITS):
                result["result"] = "rejected: bad credits"
                return result
            result["credits"] = credits
            frame = build_legacy_bonus_poll(args.address, credits)
            reply = poller.transport.transact(frame)
            verdict = classify_bonus_reply(reply, args.address)
            result["result"] = verdict
            logger.info("🎁 hub command {}: legacy bonus {} credits -> {}",
                        cmd_id, credits, verdict)
        except Exception as e:                      # noqa: BLE001
            result["result"] = f"error: {type(e).__name__}"
            if result.get("type") == "handpay_reset":
                # reset contract: the record ALWAYS carries ok/outcome/
                # detail — an exception is an honest ok:false, and the
                # poll thread lives on (nothing here may raise).
                result["ok"] = False
                result["outcome"] = "error"
                result["detail"] = f"{type(e).__name__}: {e}"
            elif result.get("type") in ("aft_register", "aft_transfer",
                                        "aft_set_host_cashout"):
                # same contract as handpay_reset — the AFT records ALWAYS
                # carry ok/outcome/detail even on an exception. For a push,
                # amountCents stays absent/0 so the hub NEVER debits House on
                # an errored transfer (debit-on-CONFIRM only).
                result["ok"] = False
                result["outcome"] = "error"
                result["detail"] = f"{type(e).__name__}: {e}"
                if result.get("type") == "aft_transfer":
                    result["amountCents"] = 0
            logger.warning("hub command {} failed: {}", cmd_id, e)
        return result

    def drain_hub_commands():
        """Drain + execute queued hub commands on THIS (poll) thread. Runs
        both live and parked — a parked satellite still answers commands
        (with the C3 sas_disabled refusal) so the hub/UI is never left
        hanging on a verdict."""
        if reporter is None:
            return
        while reporter.pending_commands:             # popleft is atomic
            try:
                cmd = reporter.pending_commands.popleft()
            except IndexError:
                break
            reporter.command_results.appendleft(run_hub_command(cmd))

    def handle_cashout_if_pending():
        """Answer a machine-initiated host-cashout request (exception 0x6A)
        on THIS (poll) thread, BETWEEN polls — the transport single-writer
        rule (never a re-entrant transact inside the exception callback,
        never a second serial writer, tcdrain timing untouched). Reads the
        machine's cashable balance and pushes it EGM->host (0x80); the hub
        credits the pinned wallet on the confirmed completion (a satellite-
        originated 'aft_cashout' result record). Fires ONLY when armed
        (on_cashout_request gates the flag), so an un-armed machine's
        cash-out is never silently pulled. Never raises — an exception here
        would kill the poll loop via SASPoller.run's bare stop()."""
        if not cashout_state["pending"]:
            return
        cashout_state["pending"] = False
        if not sas_enabled.is_set():
            return                                 # parked — no wire traffic
        rec = {"id": f"cashout-{int(time.time() * 1000)}",
               "type": "aft_cashout",
               "when": time.strftime("%Y-%m-%dT%H:%M:%S"),
               "accountId": cashout_state["accountId"]}
        try:
            # Register + adopt the machine's asset/key (idempotent) so the
            # EGM->host 0x72 can build against the real identity.
            ok_reg, reg, reg_detail = aft_host.auto_register()
            if not ok_reg:
                rec.update(ok=False, amountCents=0,
                           outcome=("asset_unknown" if "no asset" in reg_detail
                                    else "not_registered"),
                           detail=f"cannot cash out — {reg_detail}",
                           result="rejected")
            else:
                # Full cashable off the machine (0x74/FF read-only interrogate).
                ls = aft_host.read_lock_status()
                cashable = (ls.current_cashable_cents
                            if ls and ls.current_cashable_cents else 0)
                if cashable <= 0:
                    rec.update(ok=False, outcome="no_credits", amountCents=0,
                               detail="machine reports no cashable credits",
                               result="rejected")
                elif cashable > MAX_AFT_CENTS:
                    rec.update(ok=False, outcome="over_ceiling", amountCents=0,
                               detail=(f"cashable {cashable}c over the "
                                       f"{MAX_AFT_CENTS}c ceiling — cash out "
                                       "at the machine"), result="rejected")
                else:
                    req = AFTTransferRequest(
                        transaction_id=make_transaction_id(),
                        transfer_type=TRANSFER_TYPE_EGM_TO_HOST,
                        cashable_cents=cashable,
                        transfer_code=TRANSFER_CODE_FULL_ONLY,
                        transfer_flags=TRANSFER_FLAGS_NONE)
                    logger.info("🏦 host-cashout: pushing {}c EGM->host "
                                "(acct {})", cashable, rec["accountId"] or "-")
                    res = aft_host.transfer(
                        req, require_lock=False,
                        on_wire=lambda lbl, rq, rp: logger.info(
                            "🏦 {} REQ {} | RESP {}", lbl, rq.hex(),
                            rp.hex() if rp else "(silence)"))
                    # Credit ONLY what the MACHINE confirmed left the machine —
                    # NEVER the requested `cashable`. If the completion parsed
                    # no amount (res.final missing, or its cashable BCD field
                    # unreadable) credit NOTHING (amountCents=0) even when the
                    # status byte said success: a wallet credit is irreversible,
                    # so an unconfirmed amount is left for a meter check, never
                    # back-filled with the figure we asked for.
                    confirmed = (res.final.cashable_cents
                                 if res.final is not None
                                 and res.final.cashable_cents is not None
                                 else None)
                    credited = (confirmed if res.ok and confirmed is not None
                                else 0)
                    rec.update(ok=res.ok, outcome=res.outcome.value,
                               txnId=req.transaction_id,
                               amountCents=credited,
                               detail=(res.detail if credited or not res.ok
                                       else res.detail + " — amount UNCONFIRMED"
                                       ", NOT credited (verify meters)"),
                               result="ack" if res.ok else res.outcome.value)
                    logger.info("🏦 host-cashout {}c -> {} ({})",
                                cashable, res.outcome.value, res.detail)
        except Exception as e:                     # noqa: BLE001 — never fatal
            rec.update(ok=False, outcome="error", amountCents=0,
                       detail=f"{type(e).__name__}: {e}", result="error")
            logger.warning("🏦 host-cashout failed: {}", e)
        if reporter is not None:
            reporter.command_results.appendleft(rec)

    def heartbeat_if_due(now):
        if now - hb["last"] >= HEARTBEAT_SEC:
            hb["last"] = now
            st = poller.state
            logger.info("heartbeat: polls={} online={} events={} "
                        "meterChanges={} lastSeen={:.0f}s ago{}",
                        stats["polls"], st.online, stats["events"],
                        stats["meter_changes"],
                        (now - st.last_seen) if st.last_seen else -1,
                        "" if sas_enabled.is_set() else " [SAS PARKED]")

    def stop_or_heartbeat():
        stats["polls"] += 1
        drain_hub_commands()
        # C3 PARK POINT — why here and not inside SASPoller: run() only
        # calls poll_once() after this stop callback returns, so HOLDING
        # the poll thread here parks the loop with zero core changes — no
        # polls, no wire bytes, port and thread stay alive. wait(0.2)
        # bounds the resume latency at 200 ms with no busy spin; commands
        # arriving while parked still drain (refused with honest records)
        # and the heartbeat stays alive in the journal. The one poll_once
        # already in flight when the flip lands is unavoidable — the park
        # takes effect within a single poll cycle. When enabled, wait()
        # returns True immediately, so the live path pays nothing.
        parked = False
        while not stop["flag"] and not sas_enabled.wait(0.2):
            parked = True
            drain_hub_commands()
            heartbeat_if_due(time.monotonic())
        if stop["flag"]:
            return True
        now = time.monotonic()
        # RX-silence wedge watchdog (task #21). Runs on the poll thread, in the
        # same window as the AFT cache — never races a wire exchange. A parked
        # stretch is intentional silence: bump() so the reopen clock restarts
        # from resume, not from the machine's last answer. Otherwise tick():
        # if we've been polling with zero valid frames for the cadence, reopen
        # the port (the machine-reboot RX wedge cure).
        if wedge_watch is not None:
            if parked:
                wedge_watch.bump(now)
            else:
                wedge_watch.tick(now)
        # Machine-initiated host-cashout (0x6A) answered here, between polls
        # (poll thread, same window as the AFT cache — never races a wire
        # exchange). No-op unless a 0x6A was flagged while armed. Gated ONLINE
        # (a 0x6A only fires on a live, armed machine).
        if poller.state.online:
            handle_cashout_if_pending()
        elif cashout_state["pending"]:
            # STALE-0x6A guard: a host-cashout request raised while the link
            # was up but not yet answered when the machine dropped OFFLINE is
            # dead — the button press that raised it is long gone. Clear it so
            # an offline->online bounce can't fire a surprise EGM->host against
            # a fresh (or nobody's) session. handle_cashout_if_pending is
            # online-gated, so this is the only place an unanswered pending
            # flag is retired (the machine's own default already ticketed it).
            cashout_state["pending"] = False
            logger.info("🏦 dropped a stale host-cashout request (machine went "
                        "offline before it could be answered)")
        # AFT status cache (poll thread = the only transport toucher). The
        # reporter thread only READS stats["aft"] (single-writer/single-reader,
        # lock-free, like stats["last_meters"]). Throttled to AFT_STATUS_SEC
        # and gated ONLINE so a dark link stays quiet. Only reachable while
        # ENABLED (the park loop above holds first), so a parked port stays
        # wire-silent here too. Feeds the hub's report aft block -> the
        # hub-side auto-register trigger. Never raises.
        if reporter is not None and poller.state.online \
                and now - aft_next["at"] >= AFT_STATUS_SEC:
            aft_next["at"] = now
            try:
                reg = aft_host.registration_status()          # 73/FF
                asset_raw = aft_host.read_asset(reg=reg)       # echo/7B/74
                # 74/FF interrogate (read-only, never changes lock state):
                # availXfers/aftStatus feed the hub's transfer-eligibility
                # truth — a RAM clear turns the in-house class OFF while
                # registration survives (the live 07-09 refusal), and the
                # Wallet should know BEFORE a push, not from a refusal.
                ls = aft_host.read_lock_status()
                stats["aft"] = {
                    "registered": bool(reg and reg.registered),
                    "statusCode": reg.status if reg else None,
                    "asset": int.from_bytes(asset_raw, "little")
                             if asset_raw else None,
                    "posId": int.from_bytes(reg.pos_id, "little")
                             if reg and reg.pos_id and any(reg.pos_id)
                             else None,
                    "keyFp": reg.registration_key.hex()[:12]
                             if reg and reg.registration_key
                             and any(reg.registration_key) else None,
                    "availXfers": ls.available_transfers if ls else None,
                    "aftStatus": ls.aft_status if ls else None,
                    # tri-state on purpose: None = unknown (silent read /
                    # old firmware) — the hub/UI may only act on a REAL
                    # False (never exclude a machine on missing data)
                    "inHouseEnabled": bool(ls.aft_status
                                           & AFT_ST_IN_HOUSE_ENABLED)
                                      if ls else None,
                }
            except Exception as e:                            # noqa: BLE001
                logger.warning("AFT status read failed (ignored): {}", e)
        # C2 ticket-header push (poll thread — the only transport toucher).
        # Gated machine-ONLINE, and only reachable while ENABLED (the park
        # loop above holds first), so a parked or dark machine defers with
        # zero wire traffic. mark_attempted runs BEFORE the wire try, so
        # every (online-session, rev) gets exactly ONE attempt — a failure
        # logs once and waits for the next rev bump or rejoin (no hot
        # loop); the verdict rides every snapshot via ticketData.detail.
        if poller.state.online:
            due = ticket_header.due()
            if due is not None:
                ticket_header.mark_attempted(due["rev"])
                try:
                    ok, detail = apply_ticket_header(
                        poller.transport, args.address, due,
                        protocol=poller.protocol)
                except Exception as e:                        # noqa: BLE001
                    ok, detail = False, (f"apply error: "
                                         f"{type(e).__name__}: {e}")
                ticket_header.record_result(due["rev"], ok, detail)
                (logger.info if ok else logger.warning)(
                    "🎟️ ticket header rev {} {}: {}", due["rev"],
                    "APPLIED" if ok else "NOT applied", detail)
        heartbeat_if_due(now)
        return stop["flag"]

    poller.run(interval=args.interval, max_polls=args.max_polls,
               stop=stop_or_heartbeat)
    st = poller.state
    logger.info("stopped: online={} events={} meterChanges={}",
                st.online, stats["events"], stats["meter_changes"])


if __name__ == "__main__":
    main()
