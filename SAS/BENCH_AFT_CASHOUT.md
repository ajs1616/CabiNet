# Bench Bring-Up — BB2 SAS AFT host-cashout (machine → wallet return leg)

Goal (NARROW): prove that a SAS-linked WMS BB2 — set in its **operator menu**
to **"soft cash-out to host, fail to ticket"** — raises SAS exception **0x6A**
"AFT request for host cashout" when the player presses the cabinet's own **CASH
OUT** button, that an **armed** host answers it with an `EGM_TO_HOST` (0x80) AFT
transfer so `$X` lands in the wallet on paper with the House books flat (drift
`$0.00`), and that when **nobody is armed** the same button press **falls back
to printing a ticket**.

This is the inverse of the already-proven **fund** push (host→EGM 0x72,
live-proven $1 on 07-11). Same serial loop, same enqueue/poll path, same
credit-on-confirm-idempotent-by-ref rigor — only the direction flips.

## The CONFIRMED model (AJ 2026-07-12) — read this first

The mechanism is **LOCKED**, not a guess:

- **Enable = the operator-menu AFT config**, *not* a host long-poll. The BB2 is
  menu-set to "soft cash-out to host, fail to ticket". On the wire that shows as
  `available_transfers & AVAIL_XFER_FROM_EGM (0x02)` in the `0x74` status (real
  6.02 Table 8.2b, already parsed by `aft_handler.py`).
- **The arm is 100% HUB-SIDE.** `AFTHost.set_host_cashout()` sends **NO** game
  lock — it does a **read-only** `0x74/FF` interrogate only (the gate probe).
  The prior build's LP 0x74 REQUEST/CANCEL game-LOCK was the WRONG primitive (it
  locked the cabinet and auto-expired in ~5 s); it is **gone**.
- **The gate self-determines from the wire.** `sasCashoutReady` is **not** a
  manual flag anymore — the hub derives it from the reported from-EGM bit
  (`g2s_host.sas_from_egm_ready`). A machine not reporting from-EGM answers the
  honest refusal *"cash-out to wallet isn't set up on this machine yet (turn on
  soft cash-out to host in its operator menu)"* — never a fake ON. There is **no
  post-bench flag to flip**: enable the operator-menu item and the machine's own
  0x74 arms the gate.
- **Answer-when-armed / lapse-to-ticket.** On 0x6A: armed → answer with a 0x72
  `EGM_TO_HOST`; unarmed → do nothing → the machine's own default prints a
  ticket. Disarm on logout/card-out just clears the hub flag (no wire write).

- **SMIB:** the Zero-2W SAS SMIB `smib-bb2` (see `reference_casinonet_zero_smib_access`).
- **Money safety:** load only a small cashable balance and card in the test
  player before you begin; every press is against that balance only.

---

## 0. What ships (wiring recap)

- **Satellite** (`SAS/sas_host.py`, `SAS/modules/aft/aft_handler.py`):
  - `AFTHost(on_cashout_request=…)` is wired (`sas_host.py`, ~`:919`). A 0x6A/0x6B
    on the general poll sets `cashout_state["pending"]` **only while armed**
    (`on_cashout_request`, ~`:894`) — an un-armed 0x6A is logged, never answered.
  - The 0x6A is answered **between polls** — `handle_cashout_if_pending()`
    (called from `stop_or_heartbeat`, gated on `poller.state.online`): reads the
    full cashable (0x74/FF), builds a **lockless**
    `AFTTransferRequest(transfer_type=EGM_TO_HOST)`, runs the proven
    `aft_host.transfer(require_lock=False)`, reports an `aft_cashout` result.
    **One frame through the poller's single transport writer — tcdrain timing
    untouched, no second writer.** A pending 0x6A that never gets answered while
    the machine bounces **offline** is **dropped** (the stale-0x6A guard), so an
    online bounce can't fire a surprise transfer.
  - `aft_set_host_cashout {enable, accountId}` (`run_hub_command`, ~`:1198`) sets
    the **hub-side flag** (`cashout_state["armed"]/["accountId"]`). It also calls
    `AFTHost.set_host_cashout(enable)` (`aft_handler.py`) — **read-only** `0x74/FF`
    on arm (so the reply can carry `fromEgmOk`), **nothing on the wire** on disarm.
    **No game lock.**
  - **Credited amount = machine-confirmed only.** `handle_cashout_if_pending`
    credits `res.final.cashable_cents`; if the completion parses no amount it
    credits **nothing** (`amountCents=0`, "amount UNCONFIRMED") — it **never**
    back-fills the requested figure.
- **Hub** (`G2S/g2s_host.py`):
  - `glass_cash_out_to_wallet` forks on `sas_links`: SAS-linked → the hub-side
    AFT host-cashout arm, gated by `sas_from_egm_ready(entry)` (the machine's
    live 0x74 from-EGM bit); AVP → the WAT mirror (verbatim).
  - The raw `POST /api/sas/command {"type":"aft_set_host_cashout","enable":true}`
    enforces the **same** self-determined gate (no bypass) — an enable is refused
    unless a machine on that smib reports from-EGM.
  - `_settle_aft_cashouts` CREDITS the echoed wallet on the confirmed completion,
    `ref="aftcashout:<txn>"`, `adjust(once=True)` (idempotent) — the exact inverse
    of the fund debit; no House entry, no hold double-count.
  - `entry["sasCashoutReady"]` on `/api/status` is the **derived** from-EGM state
    (display-only, honest).

## 0.1 Start order (run from `/home/aj/CasinoNet/`)

Three surfaces — the satellite (serial), the hub (the money authority), and the
wire tap. The satellite prints the loud `🏦` lines this runbook keys on; the hub
settles the credit and owns the books.

```bash
# T1 — the SAS satellite on smib-bb2 (serial loop; single writer). It POSTs
# reports to the hub and picks up hub commands in the report reply (~1s).
ssh smib-bb2 'sudo systemctl restart casinonet-sas && journalctl -u casinonet-sas -f'
#   watch this console for:  💵 diag 0x74/FF …   🏦 addr … host-cashout …
#                            🏦 0x72 REQ … | RESP …   🏦 host-cashout Nc -> …

# T2 — the hub / money authority (dev box). Owns wallets, the House ledger,
# the gate, and _settle_aft_cashouts. Serves the glass + /api endpoints.
cd G2S && python3 g2s_host.py --harvest

# T3 — live money truth while you press buttons (all on the hub box):
watch -n1 'curl -s localhost:8081/api/pnl      | python3 -m json.tool | grep -i drift'
#   plus, per step:  curl -s localhost:8081/api/accounts | python3 -m json.tool
#                    curl -s localhost:8081/api/status   | python3 -m json.tool
```

---

## 1. Enable soft cash-out to host in the operator menu (the whole enable)

Host-cashout is **not** a long-poll toggle. Enable it in the BB2 operator menu
(the "AFT cash-out to host" / cashless-to-host item — the same in-house-AFT
family a RAM clear turns OFF, `reference_casinonet_sas_aft_transfer`), set to
**"fail to ticket"** so an unanswered request prints a ticket. Then capture
**`0x74/FF`** (the report cache reads it every `AFT_STATUS_SEC`; the satellite
line is `💵 diag 0x74/FF: … availXfers=0x__ …`).

- **EXPECT `available_transfers` bit1 = `0x02` (from-machine) SET.**
- If from-EGM is **clear**, the gate stays NOT-ready and the MY WALLET toggle
  refuses honestly — nothing can arm it until this bit is set (post-RAM-clear the
  BB2 advertised `0x04` printer-only, bench 07-09). **Record the exact menu
  path** — this is the single most likely first-attempt blocker:

  ```
  BB2 menu path (fill in on the bench): ____________________
  availXfers before: 0x__   after: 0x__   (expect bit1 = 0x02 set)
  ```

- Confirm `/api/status` for this SMIB now shows **`sasCashoutReady: true`**
  (derived, not flipped) once the machine reports `0x02`.

## 2. Fail-to-ticket when UNARMED (prove the default first)

Before arming anything, with the machine menu-enabled but **no** hub arm and
**nobody carded**, press **CASH OUT**.

- Satellite logs `🏦 addr 01 host-cashout request 0x6A — NOT armed — left to the
  machine default (ticket)`. **The machine prints a TICKET.**
- This is the "cash-out to ticket when nobody's logged in" default — the safe
  fallback. Confirm no `aft_cashout` result and no wallet movement.

  ```
  UNARMED press → ticket printed?  Y / N     (0x6A seen + "NOT armed" logged)
  ```

## 3. Arm (hub-side flag only — no wire game-lock)

Card in the test player and toggle **CASH OUT TO WALLET** in MY WALLET (or by
hand on the hub box):

```bash
curl -s localhost:8081/api/sas/command -d \
  '{"smibId":"smib-bb2","type":"aft_set_host_cashout","enable":true,"accountId":"<pXX>"}'
```

- Satellite logs `🏦 hub command <id>: AFT host-cashout ARMED (acct pXX)`. The
  arm is the hub-side `cashout_state` flag — the only wire traffic is the
  **read-only** `0x74/FF` gate probe (**no `🏦 0x72` on_wire line yet**, and **no
  game-lock frame** — confirm on the serial sniff there is NO 0x74 REQUEST/CANCEL).
- The reply rides back in `commandResults` (visible in `/api/status`): watch
  **`fromEgmOk:true`** (derived from the read-only probe).

  ```
  fromEgmOk: ____    (no game-lock frame on the sniff)  Y / N
  ```

## 4. Button press → exception 0x6A → EGM → host transfer (the money leg)

Press the cabinet's **CASH OUT** button.

- Satellite logs `🏦 addr 01 host-cashout request 0x6A — armed, answering
  EGM->host between polls` — **first live proof of `aft_host_cashout_request`**.
  Confirm the code is `0x6A` (or `0x6B` for a win). Confirm the machine
  **locks/waits** for the host, NOT an immediate ticket print.
- Then `🏦 host-cashout: pushing Nc EGM->host (acct pXX)`, the wire tap
  `🏦 0x72 REQ <hex> | RESP <hex>`, then `🏦 host-cashout Nc -> completed (…)`.
- **Capture the 0x72:** transfer type byte = **`0x80`** (EGM_TO_HOST), cashable =
  the loaded balance, response **status `0x00`** full (or `0x01` partial; or
  `0x40` PENDING → exception `0x69` → interrogate `72/FF` → final status). **The
  wallet is credited ONLY on a confirmed `0x00`/`0x01`** (`res.ok`), by
  `res.final.cashable_cents` — never the requested figure.
- **Prove `$X` leaves the machine** (BB2 credit meter drops by `$X`) **and lands
  in the wallet exactly once:**

  ```bash
  curl -s localhost:8081/api/accounts | python3 -m json.tool   # find pXX millicents
  ```

  The credit ledger ref is **`aftcashout:<txn>`** — a pure wallet **credit**,
  **no House entry** (verify by diffing the House balance in /api/accounts
  before/after: it must not move — AFT cashable moves are wallet-only).
- **Idempotency:** the satellite re-sends its last ~20 `commandResults` each
  report (~1 s). Watch two full report cycles → **the wallet credits ONCE** (the
  `_aft_cashouts_settled` seen-set + `adjust(once=True)` guard).

  ```
  0x72 type: 0x__ (expect 0x80)    status: 0x__ (expect 0x00 / 0x40→0x69)
  BB2 credit meter  before: $____  after: $____   (expect −$X)
  wallet (pXX)      before: $____  after: $____   (expect +$X, ONCE)
  House balance     before: $____  after: $____   (expect UNCHANGED)
  ```

## 5. Timing

- Measure **0x6A → 0x72** latency vs. the firmware intercept window (EFT's was
  ~800 ms per the change-log; AFT's is `VERIFY_ON_BENCH`). The 0x6A is answered
  **between polls**, so confirm the responder beats the window. If too tight, a
  fast-path can issue the 0x72 from the cached adopted asset/key on the poll
  thread (still one writer) — note it here if the between-polls path misses:

  ```
  0x6A→0x72 latency: ____ ms    intercept window (ticket fallback at): ____ ms
  between-polls responder in time?  Y / N    (if N, enable the fast-path)
  ```

## 6. Disarm (revert to the ticket default — no wire write)

- Disarm via the hub (and independently via a real **card-out** — the pin pop in
  `_card_out` pairs a protocol-correct disable):

  ```bash
  curl -s localhost:8081/api/sas/command -d \
    '{"smibId":"smib-bb2","type":"aft_set_host_cashout","enable":false}'
  ```

- Satellite logs `🏦 hub command <id>: AFT host-cashout disarmed`. Disarm clears
  `cashout_state` (armed + any pending 0x6A) and touches **NO** wire — there is
  no game-lock to cancel. Confirm on the serial sniff that disarm sends nothing.
- Press CASH OUT → **confirm it prints a TICKET** (the unanswered 0x6A falls to
  the cabinet default). An un-carded machine always tickets.
- **Idempotent:** fire disarm **twice** → the second is a no-op.

---

## What a successful run looks like (satellite console)

```
💵 diag 0x74/FF: lockStatus=0x00 availXfers=0x02 aftStatus=0x…      # bit1 set — eligible
🏦 addr 01 host-cashout request 0x6A — NOT armed — left to the machine default (ticket)  # step 2
🏦 hub command coarm7-…: AFT host-cashout ARMED (acct p03)          # step 3 arm (hub-side)
🏦 addr 01 host-cashout request 0x6A — armed, answering EGM->host between polls  # step 4 press
🏦 host-cashout: pushing 300c EGM->host (acct p03)                   # step 4
🏦 0x72 REQ 017249…80… | RESP 0172…00…                              #   type 0x80, status 0x00
🏦 host-cashout 300c -> completed (FULL_TRANSFER_SUCCESSFUL)         #   confirmed
```

…and on the hub: `/api/accounts` shows the carded wallet **+$3.00**, the
House balance unchanged, the ledger carries one `aftcashout:<txn>` credit
and no duplicate on the next report cycle.

**Pass criteria (record on this page + in `COMPATIBILITY.md`):**
1. Step 1 from-EGM bit `0x02` set (note the operator-menu path); `/api/status`
   `sasCashoutReady:true` derived.
2. Step 2 UNARMED CASH OUT prints a ticket (0x6A "NOT armed").
3. Step 3 arm is hub-side (`fromEgmOk:true`, NO game-lock frame on the sniff).
4. Step 4 exception `0x6A` "armed", `0x72` type `0x80`, status `0x00`/`0x01`,
   `−$X` off the meter, `+$X` in the wallet **once**, House unchanged.
5. Step 6 disarm reverts to a ticket, sends no wire, and is idempotent.

## Failure triage

| Symptom | Meaning | Move |
|---|---|---|
| Step 1 `availXfers` shows no `0x02` (e.g. `0x04` printer-only) | From-EGM disabled in machine config — the enable | Set the operator-menu "AFT cash-out to host / fail to ticket" item; re-capture; record the path. The gate stays NOT-ready until this bit is set. |
| MY WALLET toggle refuses "isn't set up on this machine yet" | The machine isn't reporting from-EGM (0x74 bit 0x02) | Same as above — this is the gate self-determining honestly, not a bug. |
| UNARMED press → no ticket | Machine not menu-set to "fail to ticket" | Fix the operator-menu fallback setting; the whole design relies on it. |
| Press CASH OUT → immediate ticket even when armed | The arm didn't land, or the machine isn't carded | Re-check step 3 landed (`🏦 … ARMED`) and the report carried the command; verify the machine is carded/armed and not tilted. |
| `0x6A` fires but no `0x72` follows | `handle_cashout_if_pending` didn't run (offline) or cashable read failed | Confirm `poller.state.online`; check for `🏦 host-cashout: machine reports no cashable credits`; verify a cashable balance is loaded. |
| `0x72` returns status `0x82` | Invalid-function / from-EGM refused by firmware | Layer-0 not truly enabled, or full-to-host unsupported — try `TRANSFER_CODE_PARTIAL_OK` only if `aft_status` shows partial-to-host OK. |
| Wallet credited but the House balance moved | A House entry crept in (should be a pure wallet credit) | Inspect `_settle_aft_cashouts` — the return leg must NOT touch the House account; the fund debit was the player's own wallet. |
| Wallet credited **twice** on a re-report | Idempotency guard bypassed | Confirm `ref="aftcashout:<txn>"` is stable across reports and `adjust(once=True)` fired; the `<txn>` must come from the satellite's transaction id, not a per-report id. |
| A `0x6A` fires a transfer AFTER an offline bounce | Stale-pending guard missed | Confirm the offline branch in `stop_or_heartbeat` clears `cashout_state["pending"]` while `not poller.state.online`. |

**Keep the satellite journal + any serial capture** — this is the first live
proof of the 0x6A/0x72 EGM_TO_HOST path; every byte is permanent reference for
`SAS/modules/aft/` and the reference memory.

## Regression nets (host-side, no bench)

- `python3 -c "import ast; ast.parse(open('SAS/sas_host.py').read())"` — clean.
- `pytest SAS/tests/test_aft_host_cashout.py` — EGM_TO_HOST byte-5 type, the
  lockless return (no 0x74 dance), the read-only `0x74/FF` gate probe (NO
  REQUEST/CANCEL game-lock), disarm-touches-no-wire, arm-is-a-single-reader (no
  0x72), 0x6A/0x6B surface (0x11 does not).
- `python3 G2S/tools/avp_replay.py` (LOCAL host only): `cashOutToWallet` routes
  AVP→WAT; a linked SAS leg NOT reporting from-EGM refuses honestly ("operator
  menu"); the machine reporting `availXfers=0x02` self-determines READY (path=sas,
  200); disarm reverts; the fund path stays green.
