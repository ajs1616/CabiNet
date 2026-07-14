"""
SAS general-poll exception codes — the typed event table.

Source of authority: Montana DOJ SAS Implementation Guide v1.5.0 §3.1
"Required Event Exceptions" (p.9) — the complete list of exception codes that
guide documents — plus code 0x3E, which §4.6.2 (p.22-23) cites by name
("For exception '3E', hand pay has been validated") even though it is absent
from the §3.1 table.

Every entry in EXCEPTIONS is guide-cited. Codes the FULL IGT SAS spec defines
but the guide does NOT (the on-disk guide is a supplement, not the spec) are
deliberately ABSENT from EXCEPTIONS rather than guessed:

  0x51/0x52: bench 2026-07-08 (BB2), added to BENCH_EXCEPTIONS below.
  0x51 is the EGM ACTIVELY REQUESTING the host complete a handpay reset NOW:
  a real handpay happened, the attendant keyed off the physical LOCKUP, but
  the SAS host never sent the reset — so the EGM re-reports 0x51 until the
  host acknowledges it. The machine stays PLAYABLE (not a live lock), but
  0x51 is a live REQUEST, not a passive flag — the answer is to RUN the reset
  (0xA8 select method -> 0x94 reset, core/sas_handpay_reset.py / the UI 🔑
  button), which is exactly what the machine is asking for; 0x52 is the EGM
  confirming the reset happened. (LP 0x1B "Send Handpay Information" is a
  SEPARATE read poll — do NOT conflate reading the info with resetting the
  handpay; that conflation was the wrong theory.) AJ-corrected 2026-07-08.

  TODO(bench): likewise $50/$100 "bill accepted" exception codes: the guide
  documents only $1/$5/$10/$20 (0x47-0x4A, non-RTE). Larger denominations
  will surface as UNKNOWN until cited.

BENCH_EXCEPTIONS (below) is the ONE sanctioned escape hatch: the ticket-in
redemption and AFT flows (core/sas_tito_host.py, modules/aft/) must dispatch
on exception codes the guide never documents (0x67-0x6C). Those live in a
SECOND, clearly-quarantined table with VERIFY_ON_BENCH provenance — believed
values from the full SAS spec's exception space, NOT cited by anything on
disk. lookup() consults EXCEPTIONS first, then BENCH_EXCEPTIONS, so the
cited table stays a pure transcription of the guide while the funds-transfer
flows still get typed events. If the bench disproves a code, fix it THERE
and in the flow modules — never promote an unproven code into EXCEPTIONS.

Denomination mapping: the guide gives an explicit code->denomination mapping
for 0x47-0x4A only; those entries carry `denom_cents`.

Category assignment is CasinoNet's own layer (the guide has no category
column); the code/name pairs are the cited part.
"""

import enum
from dataclasses import dataclass
from typing import Dict, Optional


class ExceptionCategory(enum.Enum):
    """Coarse buckets for routing/UX (door lamp, tilt banner, bill ticker...)."""
    DOOR = "door"
    POWER = "power"
    BILL = "bill"
    TILT = "tilt"
    TICKET = "ticket"
    HANDPAY = "handpay"
    AFT = "aft"
    GAME = "game"
    SYSTEM = "system"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class SASExceptionInfo:
    """One decoded general-poll exception.

    code        — the raw exception byte off the wire
    name        — human label (guide wording, shortened)
    category    — ExceptionCategory bucket
    event       — stable machine-readable token ('door_open', 'bill_inserted',
                  ...) for callbacks / the SMIB->server bridge
    denom_cents — bill denomination in cents, ONLY for the bill-accepted
                  codes whose denomination the guide documents (0x47-0x4A)
    """
    code: int
    name: str
    category: ExceptionCategory
    event: str
    denom_cents: Optional[int] = None


_C = ExceptionCategory

# Guide §3.1 (p.9) + 0x3E from §4.6.2. Names kept consistent with the
# pre-existing poller table (short forms of the guide's wording).
EXCEPTIONS: Dict[int, SASExceptionInfo] = {e.code: e for e in [
    SASExceptionInfo(0x00, "no activity", _C.SYSTEM, "no_activity"),
    # 0x1F cited by spec §12.6 (no-activity exceptions $00 and $1F): sent
    # instead of $00 while the machine is idle "and waiting for player".
    # Live-proven on the WMS BB2 2026-07-08 (streams in attract mode, flips
    # to $00 once credits are on the meter).
    SASExceptionInfo(0x1F, "no activity, waiting for player", _C.SYSTEM,
                     "no_activity"),
    # -- doors / physical access -------------------------------------------
    SASExceptionInfo(0x11, "slot door opened", _C.DOOR, "door_open"),
    SASExceptionInfo(0x12, "slot door closed", _C.DOOR, "door_closed"),
    SASExceptionInfo(0x13, "drop door opened", _C.DOOR, "door_open"),
    SASExceptionInfo(0x14, "drop door closed", _C.DOOR, "door_closed"),
    SASExceptionInfo(0x15, "card cage opened", _C.DOOR, "door_open"),
    SASExceptionInfo(0x16, "card cage closed", _C.DOOR, "door_closed"),
    SASExceptionInfo(0x19, "cashbox door opened", _C.DOOR, "door_open"),
    SASExceptionInfo(0x1A, "cashbox door closed", _C.DOOR, "door_closed"),
    SASExceptionInfo(0x1B, "cashbox removed", _C.DOOR, "cashbox_removed"),
    SASExceptionInfo(0x1C, "cashbox installed", _C.DOOR, "cashbox_installed"),
    SASExceptionInfo(0x1D, "belly door opened", _C.DOOR, "door_open"),
    SASExceptionInfo(0x1E, "belly door closed", _C.DOOR, "door_closed"),
    # -- power --------------------------------------------------------------
    SASExceptionInfo(0x17, "AC power applied", _C.POWER, "power_on"),
    SASExceptionInfo(0x18, "AC power lost", _C.POWER, "power_lost"),
    # -- bill acceptor faults (tilts) & rejections ---------------------------
    SASExceptionInfo(0x27, "cashbox full", _C.TILT, "cashbox_full"),
    SASExceptionInfo(0x28, "bill jam", _C.TILT, "bill_jam"),
    SASExceptionInfo(0x29, "bill acceptor hardware failure", _C.TILT,
                     "bill_acceptor_failure"),
    SASExceptionInfo(0x2B, "bill rejected", _C.BILL, "bill_rejected"),
    # -- config / system ------------------------------------------------------
    SASExceptionInfo(0x3C, "operator changed configuration", _C.SYSTEM,
                     "config_changed"),
    SASExceptionInfo(0x70, "exception buffer overflow", _C.SYSTEM,
                     "exception_buffer_overflow"),
    # -- tickets / validation -------------------------------------------------
    SASExceptionInfo(0x3D, "cash out ticket printed", _C.TICKET,
                     "ticket_printed"),
    SASExceptionInfo(0x3F, "validation ID not configured", _C.TICKET,
                     "validation_id_not_configured"),
    # -- handpay (0x3E cited by §4.6.2, not the §3.1 table) -------------------
    SASExceptionInfo(0x3E, "hand pay validated", _C.HANDPAY,
                     "handpay_validated"),
    # -- bills accepted, non-RTE, with guide-documented denominations ---------
    SASExceptionInfo(0x47, "$1 bill accepted", _C.BILL, "bill_inserted",
                     denom_cents=100),
    SASExceptionInfo(0x48, "$5 bill accepted", _C.BILL, "bill_inserted",
                     denom_cents=500),
    SASExceptionInfo(0x49, "$10 bill accepted", _C.BILL, "bill_inserted",
                     denom_cents=1000),
    SASExceptionInfo(0x4A, "$20 bill accepted", _C.BILL, "bill_inserted",
                     denom_cents=2000),
    # -- printer ---------------------------------------------------------------
    SASExceptionInfo(0x60, "printer comm error", _C.TILT, "printer_error"),
    SASExceptionInfo(0x61, "printer paper out", _C.TILT, "printer_paper_out"),
    # -- game ----------------------------------------------------------------
    SASExceptionInfo(0x7A, "game soft meter reset", _C.GAME, "meters_reset"),
    SASExceptionInfo(0x86, "game out of service", _C.GAME,
                     "game_out_of_service"),
    SASExceptionInfo(0x8C, "game selected", _C.GAME, "game_selected"),
]}

# --------------------------------------------------------------------------
# VERIFY_ON_BENCH — uncited codes the TITO/AFT flows dispatch on.
#
# Provenance: believed values from the FULL SAS spec's ticket-redemption and
# AFT chapters (not on disk); the Montana guide documents none of them. They
# are quarantined here (never merged into EXCEPTIONS / EXCEPTION_NAMES /
# BILL_DENOM_CENTS — the guide-cited surfaces) and folded in only via
# lookup(), so the flows in core/sas_tito_host.py and modules/aft/ receive
# typed events instead of UNKNOWNs. Bench adjudication: trip each event on a
# real machine (insert a ticket, run an AFT transfer) and confirm the code on
# the wire; record results in COMPATIBILITY.md.
# --------------------------------------------------------------------------
BENCH_EXCEPTIONS: Dict[int, SASExceptionInfo] = {e.code: e for e in [
    # -- handpay queue (SAS 6.01 §7.8.1, spec-read 2026-07-08) ---------------
    # 0x51 = "handpay is pending" — but in the MODERN handpay-queue mode the
    # EGM RE-ISSUES 0x51 every 15s for as long as an unresolved handpay entry
    # sits in its FIFO queue, EVEN WHEN the physical lockup is already keyed
    # off and the machine is playable (AJ, bench-correct: 0x51 spam on an
    # UNLOCKED machine is a stuck/queued handpay, NOT a live lock). The host
    # is expected to answer 0x51 by reading LP 0x1B (handpay info); the queue
    # entry clears through the 1B read+ack lifecycle, and 0x52 is sent only
    # once the handpay is also reset. LP 0x94 does NOT clear this: on an
    # unlocked machine it returns reset-code 02 "not currently in a handpay
    # condition" (§7.9). §7.8.2 gives the operator config to disable the 15s
    # re-issue / enable legacy reporting. See [[reference_casinonet_sas_adjudication]].
    SASExceptionInfo(0x51, "handpay pending (queued; re-issued per §7.8.1)",
                     _C.HANDPAY, "handpay_pending"),
    SASExceptionInfo(0x52, "handpay was reset", _C.HANDPAY,
                     "handpay_was_reset"),
    # -- system-validation ticket-out (core/sas_tito_host.py) ----------------
    # 0x57 "system validation request" is the real-time cash-out trigger and
    # 0x66 "cash out button pressed" is its informational precursor. Neither
    # is in the on-disk Montana guide, so both live here rather than in the
    # guide-cited EXCEPTIONS table — cross-verified 2026-07-08 (saspy,
    # ArduinoTITO, SAS 6.02 §15.8; see the memory reference). 0x57 is serviced
    # by SASTITOHost.service_validation; 0x66 is observe-only.
    SASExceptionInfo(0x57, "system validation request (VERIFY_ON_BENCH)",
                     _C.TICKET, "system_validation_request"),
    SASExceptionInfo(0x66, "cash out button pressed (VERIFY_ON_BENCH)",
                     _C.TICKET, "cashout_button_pressed"),
    # -- ticket-in redemption cycle (core/sas_tito_host.py) ------------------
    SASExceptionInfo(0x67, "ticket inserted (VERIFY_ON_BENCH)", _C.TICKET,
                     "ticket_inserted"),
    SASExceptionInfo(0x68, "ticket transfer complete (VERIFY_ON_BENCH)",
                     _C.TICKET, "ticket_transfer_complete"),
    # -- AFT (modules/aft/aft_handler.py) ------------------------------------
    SASExceptionInfo(0x69, "AFT transfer complete (VERIFY_ON_BENCH)", _C.AFT,
                     "aft_transfer_complete"),
    SASExceptionInfo(0x6A, "AFT request for host cashout (VERIFY_ON_BENCH)",
                     _C.AFT, "aft_host_cashout_request"),
    SASExceptionInfo(0x6B, "AFT request for host to cash out win "
                     "(VERIFY_ON_BENCH)", _C.AFT,
                     "aft_host_cashout_win_request"),
    SASExceptionInfo(0x6C, "AFT request to register (VERIFY_ON_BENCH)",
                     _C.AFT, "aft_register_request"),
    # 0x6F "game locked" — LIVE-PROVEN 2026-07-09 21:27 (BB2): fired the
    # instant our lock-first credit push established the AFT game lock
    # (6.02 §8.2: "If the gaming machine is able to establish the lock, it
    # will issue exception 6F, game locked" — a PRIORITY exception, issued
    # live while locked, never queued). Benign/expected during every
    # lock-first aft_transfer; observe-only (the push flow already reads
    # the 0x74 status itself).
    SASExceptionInfo(0x6F, "game locked (AFT lock established)", _C.AFT,
                     "aft_game_locked"),
]}

# Convenience: bill-accepted exception code -> denomination in cents
# (guide §3.1: 47=$1.00, 48=$5.00, 49=$10.00, 4A=$20.00, all non-RTE mode).
BILL_DENOM_CENTS: Dict[int, int] = {
    c: e.denom_cents for c, e in EXCEPTIONS.items()
    if e.event == "bill_inserted"
}

# Back-compat flat name map (what sas_poller historically exported).
EXCEPTION_NAMES: Dict[int, str] = {c: e.name for c, e in EXCEPTIONS.items()}


def lookup(code: int) -> SASExceptionInfo:
    """Return the table entry for a code — guide-cited EXCEPTIONS first, then
    the quarantined VERIFY_ON_BENCH table — or a synthesized UNKNOWN entry.
    Never raises, so an uncited real-spec code still flows through the event
    path with its raw value visible."""
    info = EXCEPTIONS.get(code)
    if info is None:
        info = BENCH_EXCEPTIONS.get(code)
    if info is not None:
        return info
    return SASExceptionInfo(code, f"exception 0x{code:02X}",
                            ExceptionCategory.UNKNOWN, "unknown_exception")


def exception_name(code: int) -> str:
    return lookup(code).name
