"""
hub_ticket_client.py — the satellite's handle on the HUB validation
authority (hub.db phase 2, cross-machine TITO).

CasinoNet's ticket truth moved to the hub (the g2s_host.py process on the
Pi 5): a ticket printed on the BB2 must redeem in the AVP and vice versa,
which is only safe with ONE ledger adjudicating every redemption. This
client duck-types the exact TicketStore surface sas_tito_host.py consumes
(mint_validation_number / find_open_pending / record_issued /
authorize_redemption / close_redemption + MAX_TICKET_CENTS and the
.lock/.state the HubReporter snapshot reads), so the live-proven flows in
sas_tito_host.py run UNCHANGED — they just talk to the floor authority
instead of a local JSON file.

Failure posture (the hub being down must never corrupt money state or
strand a player):
  * mint        -> falls back to the LOCAL store under FALLBACK_SYSTEM_ID
                   (02) so the cash-out still prints instead of handpaying;
                   the sid-02 namespace can never collide with the hub's
                   sid-01 mints. Synced to the hub when it returns.
  * record      -> always recorded LOCALLY first (the satellite keeps its
                   own forensic ledger); the hub push is best-effort and
                   re-pushed by sync on failure.
  * authorize   -> REJECT ("hub unreachable — ticket returned"). The 0x71
                   REJECT hands the paper back; it stays redeemable. There
                   is deliberately NO local-authority fallback — a split
                   authority is how double-redeems happen.
  * close       -> journaled to disk and replayed by sync; the hub's
                   redeemPending holder-mark blocks any double-redeem in
                   the meantime.

Sync runs on a daemon thread: once at startup (imports the satellite's
pre-hub ledger and floors the hub's mint sequence past it) and every
SYNC_RETRY_SEC while there is unsynced state. The transport-owning poll
thread NEVER blocks on sync — its HTTP calls carry a short timeout and
every failure path is immediate.

Stdlib only (urllib + json + threading), same as the rest of core/.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
import urllib.error
import urllib.request
import zlib
from typing import Dict, List, Optional

from .sas_ticket_store import TicketStore

log = logging.getLogger("sas.hub_client")

__all__ = ["HubTicketAuthority", "FALLBACK_SYSTEM_ID",
           "derive_fallback_sid"]

#: Base of the fallback system-ID range. Local outage mints must never
#: collide with the hub's sid-01 sequence (vn16 embeds the sid) — NOR with
#: another satellite's fallback mints, so each satellite derives a stable
#: sid in 02..09 from its own smibId (review 2026-07-08: a fleet-global
#: sid 02 collided identical vn16s across two SMIBs minting during the
#: same hub outage).
FALLBACK_SYSTEM_ID = 2


def derive_fallback_sid(smib_id: str) -> int:
    """Stable per-satellite fallback system ID in 02..09."""
    return FALLBACK_SYSTEM_ID + (zlib.crc32(str(smib_id).encode()) % 8)

DEFAULT_TIMEOUT_SEC = 2.5     # per-call ceiling; the EGM holds the ticket
#                               in escrow far longer, but the poll loop
#                               should never stall behind a dead hub
SYNC_RETRY_SEC = 60.0         # dirty-state retry cadence
SYNC_HEARTBEAT_SEC = 1800.0   # clean-state full re-push (heals a hub whose
#                               db was reset/quarantined while we were idle)
MAX_SYNC_TICKETS = 500        # mirror of the hub's ingest bound

DEFAULT_JOURNAL_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "data", "hub_client_journal.json")


class HubTicketAuthority:
    """TicketStore-shaped facade over the hub's /api/tito/* authority."""

    def __init__(self, hub_url: str, smib_id: str,
                 local_store: Optional[TicketStore] = None,
                 timeout: float = DEFAULT_TIMEOUT_SEC,
                 journal_path: str = DEFAULT_JOURNAL_PATH,
                 start_sync_thread: bool = True):
        self.base = hub_url.rstrip("/") + "/api/tito/"
        self.smib_id = smib_id
        self.local = local_store if local_store is not None else TicketStore()
        self.timeout = timeout
        self.MAX_TICKET_CENTS = self.local.MAX_TICKET_CENTS
        self.journal_path = journal_path
        self.fallback_sid = derive_fallback_sid(smib_id)
        self._jlock = threading.Lock()
        # issued-capture journal: a collision print during a hub outage keeps
        # the OLD local row (the conservative local conflict branch), so the
        # fresh capture would otherwise never reach the hub — journal the raw
        # capture and let sync replay it; the hub then applies its re-issue
        # rule. Assigned BEFORE _load_journal (which fills it as a side
        # effect; the missing-file path leaves this empty list).
        self._issued: List[Dict] = []
        self._closes: List[Dict] = self._load_journal()
        # Generation counter (under _jlock): every _mark_dirty bumps it, and
        # sync_now only clears _dirty if the generation it snapshotted is
        # still current — a fallback landing during the unlocked HTTP round
        # trip must not have its dirty mark erased (review 2026-07-08:
        # check-then-clear race orphaned fallback-minted tickets).
        self._gen = 0
        self._last_sync_ok = 0.0
        # dirty => there is local state the hub has not confirmed (fallback
        # mint/record or a journaled close). The sync thread drains it.
        self._dirty = threading.Event()
        if self._closes or self._issued:
            self._dirty.set()
        self._sync_wake = threading.Event()
        self._hub_down = False        # edge-logged, never per-call spam
        log.info("hub ticket authority: %s (fallback sid %02d)",
                 self.base, self.fallback_sid)
        if start_sync_thread:
            threading.Thread(target=self._sync_loop, daemon=True,
                             name="hub-tito-sync").start()

    # -- HubReporter.snapshot compatibility ---------------------------------

    @property
    def lock(self):
        return self.local.lock

    @property
    def state(self):
        return self.local.state

    # -- transport -----------------------------------------------------------

    def _post(self, op: str, payload: Dict) -> Dict:
        """POST one tito op. Raises OSError/ValueError on ANY failure —
        network, HTTP status, or an ok:false hub refusal — so every caller
        goes through its single explicit fallback path."""
        body = json.dumps(payload).encode()
        req = urllib.request.Request(
            self.base + op, data=body,
            headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                out = json.loads(resp.read() or b"{}")
        except urllib.error.HTTPError as e:
            try:
                detail = json.loads(e.read() or b"{}").get("error", "")
            except ValueError:
                detail = ""
            raise OSError(f"hub {op} HTTP {e.code} {detail}".strip()) from e
        if not isinstance(out, dict) or not out.get("ok"):
            raise ValueError(f"hub {op} refused: "
                             f"{out.get('error') if isinstance(out, dict) else out!r}")
        if self._hub_down:
            self._hub_down = False
            log.warning("hub authority RECOVERED (%s)", self.base)
        return out

    def _down(self, op: str, e: Exception) -> None:
        if not self._hub_down:
            self._hub_down = True
            log.warning("hub authority UNREACHABLE on %s (%s) — satellite "
                        "fallbacks engaged (local sid-%02d mints, ticket-in "
                        "rejects, closes journaled)", op, e,
                        self.fallback_sid)

    @staticmethod
    def _canonical_from_raw(validation_raw: bytes) -> str:
        """The barcode digits a machine scanned: the raw escrow field's BCD
        nibbles. Non-BCD padding disqualifies it (fall back to the vn16)."""
        digits = validation_raw.hex() if validation_raw else ""
        return digits if digits.isdigit() and 8 <= len(digits) <= 18 else ""

    # -- the TicketStore surface ----------------------------------------------

    def mint_validation_number(self, amount_cents: int, address: int,
                               system_id: int = 1) -> Dict:
        try:
            r = self._post("mint", {"smibId": self.smib_id,
                                    "address": int(address),
                                    "amountCents": int(amount_cents)})
            return {"validation_number": r["validationNumber"],
                    "system_id": int(r["systemId"]), "seq": r.get("seq"),
                    "amount_cents": int(amount_cents),
                    "minted_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
                    "reused": bool(r.get("reused"))}
        except (OSError, ValueError, KeyError, TypeError) as e:
            self._down("mint", e)
            out = self.local.mint_validation_number(
                amount_cents, address, system_id=self.fallback_sid)
            self._mark_dirty()
            log.warning("FALLBACK mint %s (sid %02d) for %dc at addr %s — "
                        "will sync to the hub", out["validation_number"],
                        self.fallback_sid, amount_cents, address)
            return out

    def find_open_pending(self, address: int,
                          amount_cents: int) -> Optional[Dict]:
        """The hub mint reuses an open mint atomically, so the normal answer
        is None (one round trip instead of two). During an outage the LOCAL
        pending is consulted so a 0x57 re-fire re-sends the same fallback
        number instead of minting another."""
        if self._dirty.is_set():
            return self.local.find_open_pending(address, amount_cents)
        return None

    def record_issued(self, validation_number: str, amount_cents: int,
                      address: int, ticket_number: Optional[int] = None,
                      source: str = "egm", issued_at: Optional[str] = None,
                      extra: Optional[Dict] = None) -> Dict:
        # LOCAL first — the satellite always keeps its own forensic copy and
        # the returned record feeds TicketCaptureResult exactly as before.
        out = self.local.record_issued(
            validation_number, amount_cents, address,
            ticket_number=ticket_number, source=source,
            issued_at=issued_at, extra=extra)
        # issuedAt: the capture's own timestamp when it has one; else the
        # fresh local record's. On a duplicate/conflict the local record is
        # the OLD ticket — its date must not stamp the fresh paper (None
        # lets the hub stamp receipt time).
        payload = {
            "smibId": self.smib_id, "address": int(address),
            "validationNumber": str(validation_number),
            "amountCents": int(amount_cents),
            "ticketNumber": ticket_number, "source": source,
            "issuedAt": issued_at or (None if out["duplicate"]
                                      else out["record"].get("issuedAt"))}
        try:
            r = self._post("issued", payload)
        except (OSError, ValueError) as e:
            self._down("issued", e)
            # JOURNAL the raw capture: on a COLLISION print the local store
            # keeps its old row (its conflict branch can't know the prior
            # paper is spent at another machine), so the ledger sync would
            # never carry this fresh ticket — the journal replay is its only
            # road to the hub's re-issue rule.
            with self._jlock:
                self._issued.append(payload)
                self._save_journal_locked()
            self._mark_dirty()
            log.warning("ticket %s recorded locally; hub push failed — "
                        "journaled, will sync", validation_number)
            return out
        # The hub is the redemption authority and knows cross-machine truth
        # this mirror never learns: when it re-issued a reused validation
        # number to this fresh paper, reflect that locally so the capture
        # result / tape / forensics all tell the same story.
        if r.get("collision") == "reissued":
            out = self.local.apply_hub_reissue(
                validation_number, amount_cents, address,
                ticket_number=ticket_number, source=source,
                issued_at=payload["issuedAt"])
        return out

    def authorize_redemption(self, address: int, validation_number: str,
                             reported_amount_cents: Optional[int] = None,
                             validation_raw: bytes = b"") -> Dict:
        payload = {"smibId": self.smib_id, "address": int(address),
                   "vn16": str(validation_number),
                   "reportedAmountCents": int(reported_amount_cents or 0)}
        canonical = self._canonical_from_raw(validation_raw)
        if canonical:
            payload["canonical"] = canonical
        try:
            r = self._post("authorize", payload)
            return {"authorized": bool(r.get("authorized")),
                    "amount_cents": int(r.get("amountCents") or 0),
                    "reason": str(r.get("reason", "")),
                    "retry": bool(r.get("retry"))}
        except (OSError, ValueError) as e:
            self._down("authorize", e)
            # NO local fallback authority — a split ledger is how a ticket
            # pays twice. Reject: the machine returns the paper, retryable.
            return {"authorized": False, "amount_cents": 0,
                    "reason": "hub unreachable — ticket returned",
                    "retry": False}

    def close_redemption(self, address: int, validation_number: str,
                         redeemed: bool,
                         validation_raw: bytes = b"") -> Optional[str]:
        canonical = self._canonical_from_raw(validation_raw) \
            or str(validation_number)
        try:
            r = self._post("commit", {
                "smibId": self.smib_id, "address": int(address),
                "canonical": canonical, "redeemed": bool(redeemed)})
            return r.get("state")
        except (OSError, ValueError) as e:
            self._down("commit", e)
            with self._jlock:
                self._closes.append({"validation": canonical,
                                     "redeemed": bool(redeemed),
                                     "address": int(address)})
                self._save_journal_locked()
            self._mark_dirty()
            log.warning("close(%s, redeemed=%s) journaled — hub down; the "
                        "hub's pending hold blocks double-redeem until the "
                        "sync replays it", canonical, redeemed)
            return None

    # -- pass-throughs the runner/reporter may touch --------------------------

    def get(self, validation_number: str) -> Optional[Dict]:
        return self.local.get(validation_number)

    def outstanding(self) -> List[Dict]:
        return self.local.outstanding()

    def void_ticket(self, validation_number: str) -> Optional[str]:
        """Bench unstick — voids at the HUB authority (where redemptions
        adjudicate) and mirrors into the local forensic copy."""
        try:
            self._post("void", {"smibId": self.smib_id, "address": 0,
                                "vn16": str(validation_number)})
        except (OSError, ValueError) as e:
            log.warning("hub void(%s) failed (%s) — voided locally only; "
                        "re-run when the hub is back", validation_number, e)
        return self.local.void_ticket(validation_number)

    def pending_validations(self) -> List[Dict]:
        return self.local.pending_validations()

    # -- sync ------------------------------------------------------------------

    def _mark_dirty(self) -> None:
        with self._jlock:
            self._gen += 1
        self._dirty.set()
        self._sync_wake.set()

    def _load_journal(self) -> List[Dict]:
        """Returns the closes list; issued-capture entries load as a side
        effect into self._issued (kept single-return for the __init__ call
        order — _issued is assigned before this runs)."""
        try:
            with open(self.journal_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            issued = data.get("issued", [])
            self._issued = [i for i in issued if isinstance(i, dict)][:100]
            closes = data.get("closes", [])
            return [c for c in closes if isinstance(c, dict)][:100]
        except (OSError, ValueError):
            return []

    def _save_journal_locked(self) -> None:
        tmp = self.journal_path + ".tmp"
        try:
            os.makedirs(os.path.dirname(self.journal_path) or ".",
                        exist_ok=True)
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump({"closes": self._closes[-100:],
                           "issued": self._issued[-100:]}, f)
            os.replace(tmp, self.journal_path)
        except OSError as e:
            log.error("close journal write failed (%s) — a restart before "
                      "sync loses %d journaled close(s) + %d issued",
                      e, len(self._closes), len(self._issued))

    def sync_now(self) -> bool:
        """One sync attempt: push the whole local ledger (idempotent at the
        hub — never-downgrade merge), the local mint sequence (floors the
        hub's sid-01 seq), and any journaled closes. True on success."""
        # Replay journaled issued-captures FIRST: these are collision prints
        # from an outage the ledger sync cannot carry (the local row kept the
        # old ticket); each replays verbatim through /api/tito/issued so the
        # hub applies its re-issue rule. Must precede any close replay — a
        # ticket can't close before it exists.
        with self._jlock:
            issued = list(self._issued)
        for i, item in enumerate(issued):
            try:
                self._post("issued", item)
            except (OSError, ValueError) as e:
                self._down("sync", e)
                with self._jlock:
                    # drop only what landed; the rest retries next pass
                    self._issued = self._issued[i:]
                    self._save_journal_locked()
                return False
        if issued:
            with self._jlock:
                self._issued = self._issued[len(issued):]
                self._save_journal_locked()
        with self._jlock:
            gen0 = self._gen
            closes = list(self._closes)
        with self.local.lock:
            tickets = [dict(t) for t in
                       list(self.local.state.get("tickets", {}).values())
                       [-MAX_SYNC_TICKETS:]]
            seq = self.local.state.get("validationSeq", 0)
        payload = {
            "smibId": self.smib_id,
            # the ledger is per-machine on this satellite; every ticket
            # carries its own address, the top-level one is the default
            "address": tickets[0].get("address", 1) if tickets else 1,
            "validationSeq": seq,
            "tickets": [{
                "validationNumber": t.get("validationNumber"),
                "amountCents": t.get("amountCents"),
                "state": t.get("state"),
                "ticketNumber": t.get("ticketNumber"),
                "issuedAt": t.get("issuedAt"),
                "redeemedAt": t.get("redeemedAt"),
                "redeemedBy": t.get("redeemedBy"),
            } for t in tickets],
            "closes": closes,
        }
        try:
            r = self._post("sync", payload)
        except (OSError, ValueError) as e:
            self._down("sync", e)
            return False
        with self._jlock:
            # only drop what we sent — a close journaled DURING the sync
            # survives for the next pass
            self._closes = self._closes[len(closes):]
            self._save_journal_locked()
            # clear dirty ONLY if nothing new dirtied during the unlocked
            # round trip (generation unchanged) and the journal drained —
            # otherwise the next loop pass syncs again
            if self._gen == gen0 and not self._closes and not self._issued:
                self._dirty.clear()
        self._last_sync_ok = time.monotonic()
        log.info("hub tito sync ok: %s", {k: r[k] for k in
                                          ("inserted", "updated",
                                           "conflicts", "closesApplied")
                                          if k in r})
        return True

    def _sync_loop(self) -> None:
        # Startup import first (also floors the hub mint sequence past this
        # satellite's history — the reason the hub deploys before us).
        time.sleep(1.0)
        self.sync_now()
        while True:
            self._sync_wake.wait(timeout=SYNC_RETRY_SEC)
            self._sync_wake.clear()
            if self._dirty.is_set():
                self.sync_now()
            elif time.monotonic() - self._last_sync_ok \
                    >= SYNC_HEARTBEAT_SEC:
                # clean-state heartbeat: a hub whose db was quarantined/
                # reset while we were idle gets the full ledger back
                self.sync_now()
