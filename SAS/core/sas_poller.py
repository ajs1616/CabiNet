"""
SAS poller engine — the SMIB core loop.

This is the heart of the SMIB: a transport-agnostic loop that keeps one SAS
machine "online" by polling it on a steady cadence, draining its exception
FIFO, and sweeping meters periodically. It is deliberately decoupled from the
wire so it runs identically against:

  * MockSASSerialPort       — dev box / CI, no hardware
  * SASSerialPort           — Pi 5 + RS-232, real BB1/BB2E on a SAS-only game

The loop (per the SAS implementation guide):
  1. General poll (0x80|addr). Machine answers with ONE exception-code byte
     from its FIFO, or 0x00 = no activity. We decode and dispatch it.
  2. If the FIFO had something (non-zero), poll again promptly — drain it.
  3. On a timer, issue meter long polls (e.g. current credits 0x1A) and
     update the cached meter set.
  4. Track online/offline: a machine that misses N consecutive polls is
     declared offline; the first answer after silence is "online".

Callbacks (all optional) let a UI / server layer react without the engine
knowing anything about them:
  on_event(addr, code, name)     — an exception came off the FIFO
  on_typed_event(addr, info)     — same exception as a SASExceptionInfo
                                   (category / event token / denomination),
                                   from core/sas_exceptions.py
  on_meter(addr, code, value)    — a meter reading was refreshed
  on_online(addr)/on_offline(addr)

No asyncio, no threads required — call poll_once() yourself, or run() for a
blocking loop. Pure-Python, SBC-friendly.
"""

import logging
import time
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional

from .sas_protocol import SASProtocol, SAS_MAX_ADDRESS
# The exception-code table (Montana DOJ SAS Implementation Guide v1.5.0 §3.1
# p.9) now lives in core/sas_exceptions.py with categories + typed event
# tokens; EXCEPTION_NAMES / exception_name are re-exported for back-compat.
from .sas_exceptions import (           # noqa: F401 (re-exports)
    EXCEPTION_NAMES, SASExceptionInfo, exception_name, lookup,
)

# Idle general-poll responses (spec §12.6): $00 = no activity, $1F = no
# activity and waiting for player. Both mean "FIFO empty" — neither is an
# event, so neither dispatches nor triggers a FIFO drain (a BB2 in attract
# streams $1F at full poll rate; live-proven 2026-07-08).
IDLE_EXCEPTIONS = frozenset({0x00, 0x1F})
# Exception 0x51 "handpay pending" is a PRIORITY/interactive exception (SAS
# 6.01 §2.2.1 + §7.8.1): the machine re-issues it every 15 s until the host
# reads its paired long poll — LP 0x1B "Send Handpay Information" — to service
# the handpay QUEUE entry. The read is itself the implied ACK that advances the
# queue. Never serviced = the endless 0x51 stream we saw on the BB2 (the
# machine is NOT locked; a stale unserviced queue entry re-asks forever). This
# is NOT cleared by a handpay reset (LP 0x94 returns code 02 "not currently in
# a handpay condition" on an unlocked machine, §7.9). See
# [[reference_casinonet_sas_adjudication]].
HANDPAY_INFO_PENDING = 0x51
CMD_SEND_HANDPAY_INFO = 0x1B
# SAS_POLL_FLOOR: SAS forbids polling a single machine faster than once per
# 200 ms (the 40 ms rate is RTE/ticketing-only). Safe cadence for unknown
# game firmware. Defined in sas_meters (so its sweeps can pace themselves);
# re-exported here for back-compat.
from .sas_meters import (           # noqa: F401 (SAS_POLL_FLOOR re-export)
    SAS_POLL_FLOOR, MeterSnapshot, decode_response, sweep_snapshot,
)
# Two-step handpay reset-to-credits (AJ bench fact: method select MUST
# precede the reset). Poll numbers are TODO(bench) — see the module.
from .sas_handpay_reset import (
    RESET_METHOD_CREDIT_METER, HandpayResetResult,
    reset_handpay_to_credits as _reset_handpay_to_credits,
)


@dataclass
class MeterSpec:
    """A meter to sweep: its long-poll command and a human label.
    decode() turns the response data bytes into a value (default: BCD int)."""
    command: int
    label: str
    decode: Optional[Callable[[bytes], object]] = None


# Default meter sweep — extend per machine/game as we learn what answers.
DEFAULT_METERS: List[MeterSpec] = [
    MeterSpec(0x1A, "current_credits"),
]


@dataclass
class HandpayInfo:
    """One LP 0x1B "Send Handpay Information" response, captured when the poller
    services an exception 0x51. The record byte layout is NOT yet adjudicated on
    real hardware (memory: reference_casinonet_sas_adjudication) — we hand back
    the RAW bytes for capture and never fabricate an amount. `empty` is True when
    the machine returns an all-zero record (queue already reported/drained,
    §7.8.1: "1B returns all zeros if a handpay record has been reported and
    acknowledged")."""
    address: int
    raw: bytes            # full frame as received: [addr][1B][data...][crc]
    data: bytes           # the record payload (between command byte and CRC)
    empty: bool           # data is all zeros -> nothing pending to collect


@dataclass
class MachineState:
    address: int
    online: bool = False
    misses: int = 0
    polls: int = 0
    answers: int = 0
    last_exception: Optional[int] = None
    # Busy/NACK chirps: the machine answering a general poll with its own
    # address ORed with 0x80 (spec Table 7.4b ACK/NACK byte). Counted, never
    # dispatched as an exception — see the guard in _general_poll.
    nacks: int = 0
    meters: Dict[str, object] = field(default_factory=dict)
    # Long-poll results delivered via a GENERAL poll (guide §3 p.8 — e.g. the
    # 0x21 ROM signature, §4.3), keyed by command byte.
    poll_results: Dict[int, object] = field(default_factory=dict)
    last_seen: float = 0.0
    synced: bool = False        # has the machine seen a poll to another addr?
    callback_errors: int = 0


class SASPoller:
    """Polls a single machine over a given transport."""

    def __init__(self, transport, address: int = 1,
                 protocol: Optional[SASProtocol] = None,
                 meters: Optional[List[MeterSpec]] = None,
                 meter_interval: float = 5.0,
                 offline_after: int = 5,
                 on_event: Optional[Callable[[int, int, str], None]] = None,
                 on_typed_event: Optional[
                     Callable[[int, SASExceptionInfo], None]] = None,
                 on_meter: Optional[Callable[[int, int, object], None]] = None,
                 on_poll_result: Optional[
                     Callable[[int, int, object], None]] = None,
                 on_online: Optional[Callable[[int], None]] = None,
                 on_offline: Optional[Callable[[int], None]] = None,
                 on_handpay_reset: Optional[
                     Callable[[int, HandpayResetResult], None]] = None,
                 on_handpay_info: Optional[
                     Callable[[int, "HandpayInfo"], None]] = None,
                 service_handpay: bool = True,
                 clock: Callable[[], float] = time.monotonic):
        if not (SAS_MAX_ADDRESS >= address >= 1):
            raise ValueError(f"address must be 1..{SAS_MAX_ADDRESS}")
        self.transport = transport
        self.address = address
        self.protocol = protocol or SASProtocol()
        self.meters = meters if meters is not None else list(DEFAULT_METERS)
        self.meter_interval = meter_interval
        self.offline_after = offline_after
        self.on_event = on_event
        self.on_typed_event = on_typed_event
        self.on_meter = on_meter
        self.on_poll_result = on_poll_result
        self.on_online = on_online
        self.on_offline = on_offline
        self.on_handpay_reset = on_handpay_reset
        self.on_handpay_info = on_handpay_info
        # When True (default), the poll loop answers exception 0x51 by reading
        # LP 0x1B to drain the machine's handpay queue (§7.8.1). Set False to
        # keep the old passive behavior (log the 0x51, never service it).
        self.service_handpay = service_handpay
        self.clock = clock
        self.state = MachineState(address=address)
        self._last_meter_sweep = float("-inf")  # sweep on the first poll
        # Reset each poll_once: once a 1B read comes back empty (or fails) this
        # cycle, stop re-reading it — a real multi-record queue returns records
        # back-to-back, but a stuck 'reported-but-not-reset' 0x51 (§7.8.1) or a
        # transient read failure should cost ONE 1B read/cycle, not 20.
        self._handpay_empty_this_cycle = False

    # -- one cycle ----------------------------------------------------------

    def poll_once(self) -> Optional[int]:
        """Issue one general poll, handle the response, and (if due) sweep
        meters. Returns the exception code seen, or None on silence.

        Honors two SAS host-side rules the review flagged:
        - POLL-CYCLE SYNC (spec §3.3): after startup or a 5 s comms gap, the
          machine ignores polls to its own address until it sees a poll to a
          DIFFERENT address. We send an address-0 poll (0x80, nobody answers)
          before the first real poll and after any offline period.
        - IMPLIED ACK (spec §3.1-3.2): a general-poll response is acknowledged
          ONLY by a long poll to the same machine OR a poll to a different
          address. Repeating the same general poll is an implied NACK that
          tells the machine to RE-SEND the same exception. So between drained
          exceptions we issue an address-0 poll to ACK, not a bare re-poll.
        """
        if not self.state.synced:
            self._ack_poll()                 # sync to the polling cycle
            self.state.synced = True

        self._handpay_empty_this_cycle = False
        code = self._general_poll()
        if code is not None and code not in IDLE_EXCEPTIONS:
            self._drain_fifo()               # ACK-aware drain
        now = self.clock()
        if code is not None and now - self._last_meter_sweep >= self.meter_interval:
            self._sweep_meters()
            self._last_meter_sweep = now
        return code

    def _ack_poll(self) -> None:
        """Poll address 0 (single byte 0x80). No machine has address 0, so
        nothing answers — but it both resets a desynced machine's poll-state
        counter AND serves as the implied ACK for a prior general-poll
        response (it is a poll to a 'different address')."""
        self.transport.transact(self.protocol.build_general_poll(0))

    def _general_poll(self) -> Optional[int]:
        frame = self.protocol.build_general_poll(self.address)
        resp = self.transport.transact(frame)
        self.state.polls += 1
        if not resp:
            self._register_miss()
            return None
        self._register_answer()
        if len(resp) > 1:
            # Not an exception code: a pending LONG-POLL RESULT delivered in
            # answer to a general poll (guide §3 p.8 / §4.3 p.11 — e.g. the
            # 0x21 ROM signature, computed asynchronously and sent "in
            # response to the next general poll", then ERASED by the VGM once
            # our next poll implies the ACK). Capture it now; reading resp[0]
            # here would dispatch the ADDRESS byte as a phantom exception and
            # lose the result forever.
            self._handle_long_poll_result(resp)
            return 0x00                  # no exception came off the FIFO
        code = resp[0]
        if code == (0x80 | self.address):
            # Busy/NACK chirp, NOT an exception: a machine answers with its
            # address ORed with 0x80 (spec Table 7.4b) when it can't service
            # the poll right now. Live-seen 2026-07-10 on the WMS BB2 (addr 1
            # -> 0x81): a ~1 s run of these at poll cadence, then a 2 s
            # silence, then normal — an internal-busy blip that our dispatch
            # spammed onto the floor UI as 21 phantom "exception 0x81" events.
            # AMBIGUITY, accepted: for addr 1 this byte equals the full-spec
            # A-1 "hopper level low" exception. On hopperless collector
            # cabinets the NACK reading is the correct one; we count every
            # occurrence (state.nacks, edge-logged) so nothing goes silent.
            self.state.nacks += 1
            if self.state.nacks == 1 or self.state.last_exception != code:
                logging.getLogger("sas.poller").info(
                    "addr %d busy/NACK chirp 0x%02X (Table 7.4b; run start, "
                    "%d total) — not dispatched as an exception",
                    self.address, code, self.state.nacks)
            self.state.last_exception = code
            return 0x00                  # treat like idle: no FIFO drain
        self.state.last_exception = code
        if code not in IDLE_EXCEPTIONS:
            self._dispatch_and_service(code)
        return code

    def _drain_fifo(self, max_drains: int = 20) -> None:
        """Drain the exception FIFO. Each iteration ACKs the previous exception
        (address-0 poll) THEN general-polls for the next one — without the ACK
        a conforming machine re-sends the same exception forever (implied
        NACK). Stops on no-activity or the spec's min-20 FIFO bound."""
        for _ in range(max_drains):
            self._ack_poll()                 # ACK the exception just dispatched
            resp = self.transport.transact(
                self.protocol.build_general_poll(self.address))
            self.state.polls += 1
            if not resp:
                self._register_miss()
                return
            self._register_answer()
            if len(resp) > 1:
                # Pending long-poll result instead of an exception (§3 p.8);
                # see _general_poll. Nothing more is in the FIFO this frame.
                self._handle_long_poll_result(resp)
                return
            code = resp[0]
            self.state.last_exception = code
            if code in IDLE_EXCEPTIONS:
                return
            self._dispatch_and_service(code)

    def _handle_long_poll_result(self, resp: bytes) -> None:
        """Route a multi-byte (CRC-bearing) frame that answered a GENERAL
        poll. Guide §3 (p.8): a pending long-poll result — the ROM signature
        (0x21, §4.3) is the guide's documented case — is transmitted
        *instead of* an event exception; the VGM erases it once the next
        poll implies the ACK, so it must be captured HERE, before this
        poller sends anything else. Parsed by command byte via
        sas_meters.decode_response; stored in state.poll_results[command]
        and handed to on_poll_result(addr, command, value).

        TODO(bench): general-poll delivery is cited to guide §3/§4.3 but not
        yet observed on real hardware — confirm a real VGM's result frame
        (addr/cmd echo + CRC) parses through this path.
        """
        packet = self.protocol.parse_packet(resp)
        if packet is None or packet.address != self.address:
            return          # corrupt/foreign frame — drop, don't fabricate
        try:
            value = decode_response(packet.command, packet.data)
        except ValueError:
            value = None
        if value is None:
            value = packet.data          # no/failed parser: keep raw bytes
        self.state.poll_results[packet.command] = value
        self._safe(self.on_poll_result, self.address, packet.command, value)

    def _sweep_meters(self) -> None:
        for spec in self.meters:
            frame = self.protocol.build_short_poll(self.address, spec.command)
            resp = self.transport.transact(frame)
            packet = self.protocol.parse_packet(resp) if resp else None
            if packet is None or not packet.is_valid():
                continue
            # only trust a frame that echoes the address AND command we asked
            # for (a stale/foreign frame can otherwise poison the meter cache)
            if packet.address != self.address or packet.command != spec.command:
                continue
            if spec.decode:
                value = spec.decode(packet.data)
            else:
                value = self.protocol._bcd_to_int(packet.data)
            if value is None:
                continue
            self.state.meters[spec.label] = value
            self._safe(self.on_meter, self.address, spec.command, value)

    def snapshot_meters(self, pace: float = SAS_POLL_FLOOR,
                        sleep: Callable[[float], None] = time.sleep
                        ) -> MeterSnapshot:
        """Issue the full §4.4 accounting sweep (composite 0F + games won/
        lost, games-since, credits, bill meters, bill values) and return a
        MeterSnapshot. Heavier than the per-poll meter list — call it on
        session boundaries (player carded in/out, tournament start/end), not
        every cycle. Long polls double as the implied ACK, so it is safe to
        interleave with general polls. The sweep paces itself at `pace`
        (default SAS_POLL_FLOOR — the 200 ms per-machine floor) between
        polls; pass a no-op `sleep` only in mock tests."""
        return sweep_snapshot(self.transport, self.address, self.protocol,
                              clock=self.clock, pace=pace, sleep=sleep)

    def reset_handpay_to_credits(self, method: int = RESET_METHOD_CREDIT_METER,
                                 pace: float = SAS_POLL_FLOOR,
                                 sleep: Callable[[float], None] = time.sleep,
                                 confirm_polls: int = 5) -> HandpayResetResult:
        """Two-step handpay reset-to-credits (core/sas_handpay_reset.py):
        select reset method (0xA8, REQUIRE ack) -> remote reset (0x94) ->
        confirm via the believed 0x52 'handpay was reset' exception. The
        reset is never sent unless the method selection was acked — AJ's
        bench fact ("send the byte prior to reset") is the whole feature.
        Poll numbers are TODO(bench); the Montana guide does not cite them.

        This is the SMIB-bridge entry point (BRIDGE_DESIGN.md §5.4 action
        `reset_handpay`, bench-gated): call it on an authorized fob tap /
        server command; the typed HandpayResetResult is returned AND handed
        to the on_handpay_reset callback for the bridge's cmd_result.

        Operator-triggered — call it deliberately, not from the poll loop.
        Exceptions drained during the confirm window still dispatch through
        on_event/on_typed_event; a pending long-poll result (e.g. ROM
        signature) arriving in that window is captured, not lost. Paced at
        SAS_POLL_FLOOR; inject a no-op sleep only in mock tests."""
        result = _reset_handpay_to_credits(
            self.transport, self.address, self.protocol, method=method,
            pace=pace, sleep=sleep, confirm_polls=confirm_polls,
            on_exception=self._dispatch_event,
            on_frame=self._handle_long_poll_result)
        self._safe(self.on_handpay_reset, self.address, result)
        return result

    # -- bookkeeping --------------------------------------------------------

    def _safe(self, cb, *args) -> None:
        """Run a user callback without letting it crash the poll loop."""
        if cb is None:
            return
        try:
            cb(*args)
        except Exception:               # noqa: BLE001 — isolate user code
            self.state.callback_errors += 1
            logging.getLogger("sas.poller").exception(
                "callback raised (isolated); poll loop continues")

    def _dispatch_event(self, code: int) -> None:
        info = lookup(code)             # typed: category/event/denomination
        self._safe(self.on_event, self.address, code, info.name)
        self._safe(self.on_typed_event, self.address, info)

    def _dispatch_and_service(self, code: int) -> None:
        """Dispatch an exception, then service it if it is interactive. Only
        the POLL paths (_general_poll/_drain_fifo) call this — the operator
        reset flow calls _dispatch_event directly, so a 0x51 seen during a
        reset confirm window is never double-serviced with a stray 1B read."""
        self._dispatch_event(code)
        if (code == HANDPAY_INFO_PENDING and self.service_handpay
                and not self._handpay_empty_this_cycle):
            if not self._service_handpay_info():
                self._handpay_empty_this_cycle = True

    def _service_handpay_info(self) -> bool:
        """Answer exception 0x51 by reading LP 0x1B (Send Handpay Information),
        the spec-mandated response to the priority handpay exception (§7.8.1).

        WHY: 0x51 is interactive — the machine re-issues it every 15 s until the
        host reads 1B to collect the queued handpay record. The read is itself
        the implied ACK (a long poll to this address), so it advances the queue.
        Our old code only logged the 0x51 and never read 1B, so the BB2 re-asked
        forever (the endless-0x51 root cause). A reset (0x94) does NOT substitute
        for this — on an unlocked machine it returns code 02 "not in a handpay
        condition" (§7.9).

        LP 0x1B is a type-R read poll: bare [addr][1B], no CRC (bench-proven on
        the BB2, same family as 0x1A/0x1F). The response is a CRC-bearing frame
        [addr][1B][record...][crc], or an all-zero record once the queue is
        reported (§7.8.1). We do NOT decode the record fields — the layout is
        bench-unadjudicated — we hand the raw bytes to on_handpay_info for
        capture. Returns True iff a REAL (non-zero) record was collected (the
        caller's drain loop will re-poll and see the next 0x51 if more remain);
        False on an empty record, a foreign/corrupt frame, or silence."""
        frame = self.protocol.build_short_poll(self.address, CMD_SEND_HANDPAY_INFO)
        resp = self.transport.transact(frame)
        if not resp:
            return False
        packet = self.protocol.parse_packet(resp)
        if (packet is None or packet.address != self.address
                or packet.command != CMD_SEND_HANDPAY_INFO):
            # short / CRC-fail / foreign frame — capture nothing, fabricate
            # nothing. Log it LOUD with the raw bytes: both pre-deploy reviews
            # flagged that a transient CRC error here could silently drop a
            # handpay record IF the machine advances its queue on read (vs on
            # ack — bench-unadjudicated, §7.8.1). We don't "recover" blind, but
            # a warning means we SEE it on the wire and can adjudicate the
            # advance timing instead of losing it quietly.
            logging.getLogger("sas.poller").warning(
                "0x51 serviced but LP 0x1B answer did not parse (addr %d): %s "
                "— handpay record not captured this read", self.address,
                resp.hex() if resp else "(silence)")
            return False
        data = packet.data
        empty = not any(data)
        info = HandpayInfo(address=self.address, raw=bytes(resp),
                           data=data, empty=empty)
        if not empty:
            self._safe(self.on_handpay_info, self.address, info)
        return not empty

    def _register_answer(self) -> None:
        self.state.answers += 1
        self.state.misses = 0
        self.state.last_seen = self.clock()
        if not self.state.online:
            self.state.online = True
            self._safe(self.on_online, self.address)

    def _register_miss(self) -> None:
        self.state.misses += 1
        if self.state.online and self.state.misses >= self.offline_after:
            self.state.online = False
            self.state.synced = False   # re-sync (§3.3) when it comes back
            self._safe(self.on_offline, self.address)

    # -- blocking loop ------------------------------------------------------

    def run(self, interval: float = SAS_POLL_FLOOR, max_polls: Optional[int] = None,
            stop: Optional[Callable[[], bool]] = None,
            sleep: Callable[[float], None] = time.sleep) -> None:
        """Poll forever (or until max_polls / stop()). interval is the
        per-machine general-poll cadence. SAS forbids polling a single machine
        faster than once per 200 ms (SAS_POLL_FLOOR); the 40 ms rate is only
        for machines that advertise RTE/ticketing support, so 200 ms is the
        only safe cadence for unknown game firmware on first contact."""
        n = 0
        while True:
            self.poll_once()
            n += 1
            if max_polls is not None and n >= max_polls:
                return
            if stop and stop():
                return
            sleep(interval)


class MultiMachinePoller:
    """Round-robins one transport across several machine addresses (a SAS
    loop is multidrop — one serial line, many machines).

    NOT thread-safe: the shared transport is driven from one thread. Call
    poll_round()/run() from a single thread only."""

    def __init__(self, transport, addresses, **poller_kwargs):
        self.pollers = {a: SASPoller(transport, address=a, **poller_kwargs)
                        for a in addresses}

    def poll_round(self) -> Dict[int, Optional[int]]:
        return {a: p.poll_once() for a, p in self.pollers.items()}

    def run(self, interval: float = SAS_POLL_FLOOR, rounds: Optional[int] = None,
            stop: Optional[Callable[[], bool]] = None,
            sleep: Callable[[float], None] = time.sleep) -> None:
        r = 0
        while True:
            self.poll_round()
            r += 1
            if rounds is not None and r >= rounds:
                return
            if stop and stop():
                return
            sleep(interval)

    @property
    def states(self) -> Dict[int, MachineState]:
        return {a: p.state for a, p in self.pollers.items()}
