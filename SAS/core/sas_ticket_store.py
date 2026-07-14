"""
Durable TITO ticket state for the SAS host — stdlib-JSON, VoucherStore-style.

This is the SAS-side sibling of the G2S host's VoucherStore
(G2S/g2s_host.py): one JSON file, atomic fsync writes, quarantine-on-corrupt,
and a per-ticket redemption state machine

    issued -> redeemPending -> redeemed
                     \\-> (rejected close) -> issued      (ticket stays live)
    any state --void_ticket()--> void                     (bench unstick)

with idempotent retries: while a ticket is redeemPending, only the SAME
(machine address, holder) that opened the redemption can re-draw the SAME
authorization — anything else is rejected until the redemption closes.

TWO validation regimes, TWO id sources:

* ENHANCED / secure (Montana guide §4.6): the EGM generates the validation
  numbers (computed from its validation ID + sequence). Here the store only
  RECORDS tickets as the machine reports them (exception 0x3D -> 4D/3D reads,
  see core/sas_tito_host.py).

* SYSTEM validation (SAS 6.02 §15.8, cross-verified 2026-07-08 — see the
  memory reference reference_casinonet_sas_system_validation): the HOST mints
  the validation number in real time when the EGM raises exception 0x57. For
  that regime this store IS the mint — mint_validation_number() hands out a
  monotonic, persisted, system-ID-prefixed number and tracks it as a pending
  validation. When the resulting cash-out ticket prints (0x3D -> 4D/3D read),
  record_issued RECONCILES against that pending mint so the ticket is recorded
  exactly once. This mint is deliberately host-grade (NOT the mock EGM-side
  core/validation.py simulator) and is designed to later become the single
  validation authority shared across SAS + G2S.

Either way the store adjudicates redemption when a ticket comes back in
(exception 0x67 -> 0x70/0x71 cycle).

UNITS: amounts are CENTS — the SAS wire truth (guide §4.2: 1 credit unit =
1 cent, and every guide amount field is "in units of cents"). The G2S-side
stores keep MILLICENTS; the server layer owns the ×1000 conversion when the
two worlds meet. Field names say `amount_cents` everywhere to keep the
boundary loud.

Stdlib-only. Writes are atomic (tmp + fsync + os.replace + dir fsync); a
failed write is logged, never raised — a full disk must not corrupt
in-memory state or stall the poll loop. An unreadable store file is
quarantined to <path>.corrupt (newest wins) before starting fresh, so a
bench operator can hand-recover outstanding validation numbers.
"""

import json
import logging
import os
import threading
import time
from typing import Dict, List, Optional

log = logging.getLogger("sas.ticket_store")

# Default runtime-state location (gitignored territory; tests pass tmp paths)
DEFAULT_TICKET_STORE_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "data", "sas_ticket_state.json")

# Ticket states
ISSUED = "issued"
REDEEM_PENDING = "redeemPending"
REDEEMED = "redeemed"
VOID = "void"

# System-validation mint (SAS 6.02 §15.8). The validation number is a 16-digit
# decimal string whose first two digits are the system ID (01-99, the 0x4D
# record's validation-system-ID field) and whose remaining 14 digits are a
# monotonic sequence — so a minted number round-trips into the 0x4D capture's
# 8-BCD validation-number + 1-byte system-ID fields for reconciliation.
DEFAULT_SYSTEM_ID = 1
VALIDATION_NUMBER_DIGITS = 16
_SEQ_DIGITS = VALIDATION_NUMBER_DIGITS - 2      # 14 digits after the 2-digit ID

# Ceiling on a single system-validation cash-out the host will mint / approve:
# the 5-byte-BCD WIRE MAX ($99,999,999.99) — protocol physics, not a spend
# policy. Anything past this literally cannot be BCD-encoded in the frame, so
# a corrupted-but-CRC-valid / spoofed 0x57 amount above it is a parse artifact
# by definition. Enforced BOTH in the responder (service_system_validation,
# before any 0x58) and here in the mint, so no code path can hand out an
# out-of-range number. A home game room bets whatever it likes — the old
# $100,000 "sanity" value refused a real $168,250 jackpot to paper (collector
# de-cage, 2026-07-15). Override per store via TicketStore.MAX_TICKET_CENTS.
DEFAULT_MAX_TICKET_CENTS = 9_999_999_999


class ValidationMintError(RuntimeError):
    """The mint could not DURABLY persist the newly-allocated validation
    number, so it was rolled back and NOT handed out. Callers must never send a
    0x58 approve for a number that hit this — a non-persisted number would be
    reissued after a restart and put two physical tickets under one vn."""


def _now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime())


class TicketStore:
    """Durable ticket ledger keyed by validation number (a digit string —
    16 digits enhanced, 8 standard; stored as text because it is an
    identifier, not a quantity — leading zeros are significant)."""

    KEEP_CLOSED = 500     # bounded history of redeemed/void tickets
    KEEP_PENDING_VALIDATIONS = 200   # bounded unreconciled host mints
    MAX_TICKET_CENTS = DEFAULT_MAX_TICKET_CENTS   # mint/approve ceiling

    def __init__(self, path: str = DEFAULT_TICKET_STORE_PATH):
        self.path = path
        self.lock = threading.Lock()
        self.state = {"ticketSeq": 0, "tickets": {},
                      "validationSeq": 0, "pendingValidations": {}}
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            for k in self.state:
                if k in data:
                    self.state[k] = data[k]
            log.info("ticket store loaded: %d tickets (%s)",
                     len(self.state["tickets"]), path)
        except FileNotFoundError:
            log.info("ticket store: fresh state at %s", path)
        except (OSError, ValueError) as e:
            # Same GR-03 lesson as VoucherStore: QUARANTINE, never silently
            # overwrite — an outstanding printed ticket's validation number
            # must stay hand-recoverable.
            quarantine = self.path + ".corrupt"
            try:
                os.replace(self.path, quarantine)
                log.error("ticket store UNREADABLE (%s) — quarantined to %s, "
                          "starting fresh: %s", path, quarantine, e)
            except OSError as qe:
                log.error("ticket store UNREADABLE (%s) — starting fresh "
                          "(quarantine failed: %s): %s", path, qe, e)

    # -- persistence ---------------------------------------------------------

    def _save_locked(self) -> bool:
        """Persist; MUST be called holding self.lock. fsync data AND the
        directory so a wall-plug pull right after a ticket prints cannot
        revert the store and orphan the printed ticket.

        Returns True on a durable write, False if the write failed (swallowed
        OSError). The recorder paths (record_issued/close_redemption) may
        ignore the result and continue in memory, but the MINT must not — it
        rolls back and refuses to hand out a number it could not persist."""
        try:
            tmp = self.path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self.state, f)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, self.path)
            dfd = os.open(os.path.dirname(self.path) or ".", os.O_RDONLY)
            try:
                os.fsync(dfd)
            finally:
                os.close(dfd)
            return True
        except OSError as e:
            log.error("ticket store save FAILED (%s) — continuing with "
                      "in-memory state", e)
            return False

    # -- ticket-out ----------------------------------------------------------

    def record_issued(self, validation_number: str, amount_cents: int,
                      address: int, ticket_number: Optional[int] = None,
                      source: str = "egm", issued_at: Optional[str] = None,
                      extra: Optional[Dict] = None) -> Dict:
        """Record a ticket the machine reports printed. IDEMPOTENT: the same
        validation number reported again (a re-read of the same 4D record, a
        retried capture after a dropped frame) returns the existing record
        with duplicate=True and changes nothing — including its state, so a
        stale re-report can never resurrect a redeemed ticket.

        Returns {"record": <dict>, "duplicate": <bool>}."""
        vn = str(validation_number)
        if not vn.strip("0"):
            raise ValueError("all-zero validation number is the guide's "
                             "'no validation data' sentinel — not recordable")
        if amount_cents is None or amount_cents <= 0:
            raise ValueError(f"ticket amount must be positive cents, "
                             f"got {amount_cents!r}")
        with self.lock:
            existing = self.state["tickets"].get(vn)
            if existing is not None:
                # RECONCILE: the machine confirmed a ticket we already have.
                # If this vn was a host-minted system-validation pending, the
                # print confirms it — clear the pending marker (recorded once).
                self.state["pendingValidations"].pop(vn, None)
                stored = int(existing.get("amountCents") or 0)
                # Same amount = a true idempotent re-read (retried exception /
                # re-poll): change nothing, so a stale re-report can never
                # resurrect a redeemed ticket.
                if stored == int(amount_cents):
                    return {"record": dict(existing), "duplicate": True}
                # COLLISION: same validation number, DIFFERENT amount — a NEW
                # ticket reused the number (enhanced self-mint regenerates
                # numbers after a RAM clear). Never silently drop it.
                prior_state = existing.get("state")
                if prior_state in (REDEEMED, VOID):
                    # Prior paper spent/dead -> RE-ISSUE this row to the fresh
                    # ticket so it is redeemable for its real amount.
                    self.state["ticketSeq"] += 1
                    rec = {
                        "validationNumber": vn,
                        "amountCents": int(amount_cents),
                        "address": int(address),
                        "ticketNumber": ticket_number,
                        "source": source,
                        "state": ISSUED,
                        "seq": self.state["ticketSeq"],
                        "issuedAt": issued_at or _now_iso(),
                        "reissuedFrom": {"state": prior_state,
                                         "amountCents": stored,
                                         "at": _now_iso()},
                    }
                    if extra:
                        rec["extra"] = dict(extra)
                    self.state["tickets"][vn] = rec
                    self._prune_locked()
                    self._save_locked()
                    log.warning("🎫 ticket COLLISION: vn %s reused (%s %d¢ -> "
                                "fresh %d¢) — RE-ISSUED the spent local row.",
                                vn, prior_state, stored, int(amount_cents))
                    return {"record": dict(rec), "duplicate": False,
                            "collision": "reissued"}
                # Live/pending collision of a different amount: keep the live
                # row, flag LOUDLY (never silent) for operator resolution.
                log.error("🎫 ticket COLLISION-CONFLICT: vn %s reused while "
                          "the prior ticket is LIVE (%s %d¢) vs fresh %d¢ — "
                          "kept the live row, flagged.",
                          vn, prior_state, stored, int(amount_cents))
                return {"record": dict(existing), "duplicate": True,
                        "collision": "conflict"}
            self.state["ticketSeq"] += 1
            rec = {
                "validationNumber": vn,
                "amountCents": int(amount_cents),
                "address": int(address),
                "ticketNumber": ticket_number,
                "source": source,
                "state": ISSUED,
                "seq": self.state["ticketSeq"],
                "issuedAt": issued_at or _now_iso(),
            }
            # RECONCILE a host mint: fold its system ID onto the recorded
            # ticket and clear the pending marker so it is recorded once. Only
            # fold when the printing machine is the one we minted for — a ticket
            # minted for address A but reported printed from address B must not
            # be silently attributed to A (it is flagged, not folded).
            pending = self.state["pendingValidations"].pop(vn, None)
            if pending is not None and pending.get("systemId") is not None:
                if pending.get("address") == int(address):
                    rec["systemId"] = pending["systemId"]
                else:
                    log.warning("host mint %s minted for addr %s but reported "
                                "printed from addr %s — NOT folding systemId; "
                                "flagging address mismatch", vn,
                                pending.get("address"), int(address))
                    rec["systemIdMismatch"] = {
                        "mintedAddress": pending.get("address"),
                        "printedAddress": int(address),
                        "systemId": pending["systemId"]}
            if extra:
                rec["extra"] = dict(extra)
            self.state["tickets"][vn] = rec
            self._prune_locked()
            self._save_locked()
            return {"record": dict(rec), "duplicate": False}

    def mint_validation_number(self, amount_cents: int, address: int,
                               system_id: int = DEFAULT_SYSTEM_ID) -> Dict:
        """Mint a host-side system-validation number for a cash-out (SAS 6.02
        §15.8; the SYSTEM regime where the HOST — not the EGM — supplies the
        number in real time on exception 0x57).

        Returns a dict {"validation_number", "system_id", "seq",
        "amount_cents", "minted_at"}. The validation number is the 16-digit
        decimal string [2-digit system ID][14-digit monotonic sequence], so it
        packs into the 0x58 payload as 8-byte BCD AND round-trips into the
        0x4D capture's validation-number + validation-system-ID fields for
        reconciliation. The mint is recorded as a PENDING validation (not yet
        an issued ticket): record_issued reconciles it to a single ticket when
        the cash-out actually prints (exception 0x3D). An unprinted mint (the
        approve never lands, or the machine drops to handpay) simply lingers as
        a bounded pending entry — it never becomes a redeemable ticket.

        This is host-grade and monotonic-persisted; it deliberately does NOT
        reuse core/validation.py (the mock EGM-side simulator)."""
        sid = int(system_id)
        if not 1 <= sid <= 99:
            raise ValueError(f"system_id must be 01..99 (got {system_id})")
        if amount_cents is None or amount_cents <= 0:
            raise ValueError(f"mint amount must be positive cents, "
                             f"got {amount_cents!r}")
        if amount_cents > self.MAX_TICKET_CENTS:
            # Never mint an implausible amount (glitch/spoof guard). The
            # responder should reject to handpay before ever calling mint; this
            # is the defense-in-depth ceiling so NO path can mint out of range.
            raise ValueError(f"mint amount {amount_cents}c exceeds the "
                             f"{self.MAX_TICKET_CENTS}c ceiling")
        with self.lock:
            self.state["validationSeq"] += 1
            seq = self.state["validationSeq"]
            if seq >= 10 ** _SEQ_DIGITS:
                self.state["validationSeq"] = seq - 1        # roll back bump
                raise ValueError("validation sequence space exhausted "
                                 f"({_SEQ_DIGITS} digits)")
            vn = f"{sid:02d}{seq:0{_SEQ_DIGITS}d}"
            entry = {
                "validationNumber": vn,
                "systemId": sid,
                "seq": seq,
                "amountCents": int(amount_cents),
                "address": int(address),
                "mintedAt": _now_iso(),
            }
            self.state["pendingValidations"][vn] = entry
            self._prune_pending_locked()
            if not self._save_locked():
                # DURABILITY FAILED: a validation number we cannot persist must
                # never reach the wire. A restart would reload the last good
                # file (seq-1) and reissue THIS seq for a different cash-out —
                # two physical tickets under one vn = a double-redemption hole.
                # Roll back the increment + pending and refuse to hand it out.
                self.state["pendingValidations"].pop(vn, None)
                self.state["validationSeq"] = seq - 1
                raise ValidationMintError(
                    f"could not durably persist mint {vn} — rolled back, "
                    "no validation number issued")
            return {"validation_number": vn, "system_id": sid, "seq": seq,
                    "amount_cents": int(amount_cents),
                    "minted_at": entry["mintedAt"]}

    def pending_validations(self) -> List[Dict]:
        """Host mints not yet reconciled to a printed ticket, oldest first."""
        with self.lock:
            pend = [dict(e) for e in self.state["pendingValidations"].values()]
        return sorted(pend, key=lambda e: e.get("seq", 0))

    def find_open_pending(self, address: int,
                          amount_cents: int) -> Optional[Dict]:
        """The oldest unreconciled host mint matching this (address, amount)
        cash-out, or None. Lets the responder be IDEMPOTENT per physical
        cash-out: when an EGM re-raises 0x57 because a prior 0x58 (or its ack)
        was lost, the host re-sends the SAME validation number instead of
        minting a second one for the one cash-out."""
        with self.lock:
            matches = [dict(e)
                       for e in self.state["pendingValidations"].values()
                       if e.get("address") == int(address)
                       and e.get("amountCents") == int(amount_cents)]
        if not matches:
            return None
        return sorted(matches, key=lambda e: e.get("seq", 0))[0]

    # -- ticket-in -----------------------------------------------------------

    def apply_hub_reissue(self, validation_number: str, amount_cents: int,
                          address: int, ticket_number: Optional[int] = None,
                          source: str = "egm", issued_at: Optional[str] = None
                          ) -> Dict:
        """Mirror a HUB-side collision re-issue into this local forensic copy.
        The hub is the redemption authority and KNOWS cross-machine truth this
        mirror never learns (a ticket redeemed at another machine stays
        'issued' here) — so when the hub answers a capture with
        collision=reissued, the local row is overwritten to match the fresh
        paper. Only ever called with a hub verdict in hand; never part of the
        local decision path."""
        vn = str(validation_number)
        with self.lock:
            prior = self.state["tickets"].get(vn) or {}
            self.state["ticketSeq"] += 1
            rec = {
                "validationNumber": vn,
                "amountCents": int(amount_cents),
                "address": int(address),
                "ticketNumber": ticket_number,
                "source": source,
                "state": ISSUED,
                "seq": self.state["ticketSeq"],
                "issuedAt": issued_at or _now_iso(),
                "reissuedFrom": {"state": prior.get("state"),
                                 "amountCents": prior.get("amountCents"),
                                 "at": _now_iso(), "by": "hub"},
            }
            self.state["tickets"][vn] = rec
            self._prune_locked()
            self._save_locked()
            log.warning("🎫 local mirror: vn %s re-issued per HUB collision "
                        "verdict (%s -> fresh %d¢)", vn,
                        prior.get("state"), int(amount_cents))
            return {"record": dict(rec), "duplicate": False,
                    "collision": "reissued"}

    def authorize_redemption(self, address: int, validation_number: str,
                             reported_amount_cents: Optional[int] = None,
                             validation_raw: bytes = b"") -> Dict:
        """The authorize-or-reject decision for an inserted ticket, atomic
        under the store lock (mirrors VoucherStore.authorize_redemption).

        validation_raw is accepted for signature parity with the hub
        authority client (which keys cross-machine tickets by the scanned
        barcode digits); the LOCAL store keys by vn and ignores it.

        Success moves the ticket issued -> redeemPending and records WHICH
        machine holds it, so no second machine can redeem the same ticket
        until the redemption closes. Idempotent: a retry from the SAME
        address re-draws the identical authorization without touching state.
        Rejections change nothing and recompute identically on retry.

        The AUTHORIZED amount is always the ISSUED amount (the host is
        authoritative); reported_amount_cents is echoed back in the decision
        for visibility, and a mismatch is noted in `reason` but does not by
        itself reject — barcode-read amounts of 0/None are normal.

        Returns {"authorized", "amount_cents", "reason", "retry"}."""
        vn = str(validation_number)
        with self.lock:
            rec = self.state["tickets"].get(vn)
            if rec is None:
                return self._reject("unknown validation number "
                                    "(never issued here)")
            state = rec.get("state")
            if state == REDEEM_PENDING:
                pend = rec.get("pending") or {}
                if pend.get("address") == int(address):
                    return {"authorized": True,
                            "amount_cents": int(pend.get("amountCents", 0)),
                            "reason": "retry — same machine re-drew the "
                                      "same authorization",
                            "retry": True}
                return self._reject(
                    "redemption already in process at address %s"
                    % pend.get("address"))
            if state == REDEEMED:
                return self._reject("ticket already redeemed")
            if state == VOID:
                return self._reject("ticket voided")
            if state != ISSUED:
                return self._reject(f"unredeemable state {state!r}")
            amount = int(rec.get("amountCents", 0))
            if amount <= 0:
                return self._reject("issued amount unknown/invalid")
            note = "authorized"
            if reported_amount_cents not in (None, 0) and \
                    int(reported_amount_cents) != amount:
                note = ("authorized (issued amount %d overrides machine-"
                        "reported %d)" % (amount, int(reported_amount_cents)))
            rec["state"] = REDEEM_PENDING
            rec["pending"] = {"address": int(address),
                              "amountCents": amount, "at": _now_iso()}
            self._save_locked()
            return {"authorized": True, "amount_cents": amount,
                    "reason": note, "retry": False}

    def close_redemption(self, address: int, validation_number: str,
                         redeemed: bool,
                         validation_raw: bytes = b"") -> Optional[str]:
        """Close a redemption (mirrors VoucherStore.close_redemption):
        redeemed=True consumes the ticket; redeemed=False RESETS a matching
        redeemPending ticket back to issued (the paper stays valid — the
        machine rejected/returned it). A close from a machine that does NOT
        hold the pending marker never clobbers the live redemption — in
        EITHER direction: only the pending holder can consume the ticket
        (authorize_redemption is the only door into redeemPending, so a
        stray redeemed=True from any other address changes nothing).
        Idempotent for duplicate closes. Returns the resulting state, or
        None for a ticket we never issued."""
        vn = str(validation_number)
        with self.lock:
            rec = self.state["tickets"].get(vn)
            if rec is None:
                return None
            pend = rec.get("pending") or {}
            holds_pending = pend.get("address") == int(address)
            changed = False
            if redeemed:
                if holds_pending:
                    rec["state"] = REDEEMED
                    rec["redeemedAt"] = _now_iso()
                    rec["redeemedBy"] = int(address)
                    rec.pop("pending", None)
                    changed = True
            elif rec.get("state") == REDEEM_PENDING and holds_pending:
                rec["state"] = ISSUED
                rec.pop("pending", None)
                changed = True
            if changed:
                self._save_locked()
            return rec.get("state")

    def void_ticket(self, validation_number: str) -> Optional[str]:
        """Bench unstick: park a ticket at 'void' so redemption always
        rejects it. Clears any pending marker. Returns the prior state, or
        None if never issued."""
        vn = str(validation_number)
        with self.lock:
            rec = self.state["tickets"].get(vn)
            if rec is None:
                return None
            prior = rec.get("state")
            rec["state"] = VOID
            rec["voidedAt"] = _now_iso()
            rec.pop("pending", None)
            self._save_locked()
            return prior

    # -- reads ---------------------------------------------------------------

    def get(self, validation_number: str) -> Optional[Dict]:
        with self.lock:
            rec = self.state["tickets"].get(str(validation_number))
            return dict(rec) if rec is not None else None

    def outstanding(self) -> List[Dict]:
        """Tickets still live (issued or redeemPending), oldest first."""
        with self.lock:
            live = [dict(r) for r in self.state["tickets"].values()
                    if r.get("state") in (ISSUED, REDEEM_PENDING)]
        return sorted(live, key=lambda r: r.get("seq", 0))

    # -- internals -----------------------------------------------------------

    @staticmethod
    def _reject(reason: str) -> Dict:
        return {"authorized": False, "amount_cents": 0,
                "reason": reason, "retry": False}

    def _prune_locked(self) -> None:
        """Bound the CLOSED (redeemed/void) history at KEEP_CLOSED, oldest
        out. Live tickets are never pruned — an outstanding piece of paper
        must stay redeemable no matter how old."""
        closed = [(r.get("seq", 0), vn)
                  for vn, r in self.state["tickets"].items()
                  if r.get("state") in (REDEEMED, VOID)]
        if len(closed) <= self.KEEP_CLOSED:
            return
        closed.sort()
        for _, vn in closed[:len(closed) - self.KEEP_CLOSED]:
            del self.state["tickets"][vn]

    def _prune_pending_locked(self) -> None:
        """Bound the unreconciled host-mint set at KEEP_PENDING_VALIDATIONS,
        oldest out. A pending mint that never printed (approve lost, machine
        dropped to handpay) is only a tracking record — never a redeemable
        ticket — so evicting the oldest cannot lose money."""
        pend = self.state["pendingValidations"]
        if len(pend) <= self.KEEP_PENDING_VALIDATIONS:
            return
        by_seq = sorted(pend.items(), key=lambda kv: kv[1].get("seq", 0))
        for vn, entry in by_seq[:len(pend) - self.KEEP_PENDING_VALIDATIONS]:
            # Make orphaned mints OBSERVABLE, not silent: if this mint's 0x58
            # was accepted but the print is merely slow, the eventual ticket
            # will record without a systemId. Warn so it is not lost quietly.
            log.warning("evicting UNRECONCILED host mint %s (addr %s, %sc, "
                        "minted %s) — never reconciled to a printed ticket",
                        vn, entry.get("address"), entry.get("amountCents"),
                        entry.get("mintedAt"))
            del pend[vn]
