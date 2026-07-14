# CabiNet G2S Roadmap — full enthusiast functionality

Generated 2026-07-01 via multi-agent research (23 classes mapped, 20 deep-researched, current
code audited). Scope: **home slot-machine enthusiast** control/telemetry/showmanship —
**not** a casino floor. Security/regulatory work is permanently out of scope.

> ~~⚠️ **LIVE-EVIDENCE CAVEAT (2026-07-01):** "nothing application-level works until host 1
> claims ownership via the commConfig cycle (G2S-19) — a hard prerequisite"~~ — **DISPROVEN
> 2026-07-02 (d556ad2):** the clean-ack-then-silence was the double-wrapped sync g2sAck, not an
> ownership gate — the commHostList shows **host 1 already owned ~145 devices**. Post-fix, reads,
> events, and meters all flow with zero commConfig traffic. Never run `claimOwnership` /
> `enterCommConfigMode` as a prerequisite for anything (it disables play for zero benefit).

## Milestones

- **M0 Foundation: durable cert-less join** — DONE, proven on real AVP.
- **M1 Live telemetry** — consume the event + meter stream into structured state.
- **M2 ~~Own the machine~~ — MOOT (2026-07-02):** there was never an ownership gate (see the
  disproven caveat above); host 1 already owns ~145 devices. The commConfig cycle is
  built-and-parked optional tooling (G2S-19, now P3). Cabinet/game control lives on in G2S-20/22.
- **M3 Peripherals & money** — noteAcceptor, printer, handpay, voucher (TITO).
- **M4 Showmanship** — progressive, bonus, player/RFID, IGT-proprietary extensions.
- **M5 UI & operations** — console + Test Panel touchscreen; fleet heterogeneity.

## Task backlog

| ID | Title | Class | Pri | Status | Depends | Eff |
|----|-------|-------|-----|--------|---------|-----|
| G2S-01 | SOAP transport negotiation + cert-less ack | communications | P0 | ✅done | — | M |
| G2S-02 | Two-channel join choreography | communications | P0 | ✅done | 01 | L |
| G2S-03 | sessionId echo + host commandId sequence | communications | P0 | ✅done | 02 | S |
| G2S-04 | Epoch reset on re-handshake + stale-job drop | communications | P0 | ✅done | 02 | M |
| G2S-05 | Host keepAlive durability pinger (10s) | communications | P0 | ✅done | 02 | M |
| G2S-06 | MSX/APX guards + malformed acks + watchdog | communications | P0 | ✅done | 02 | M |
| G2S-07 | optionConfig.optionList config-sync parse + ack | optionConfig | P0 | ✅done | 02 | M |
| G2S-08 | eventHandler subscription bring-up (setEventSub) | eventHandler | P0 | ✅done | 02 | L |
| G2S-09 | eventReport → eventAck + recent-events ring | eventHandler | P0 | ✅done | 08 | M |
| G2S-10 | /api/status snapshot + /api/command trigger (getOptionList + durable option inventory added 2026-07-02, 99ad6d1) | infra | P0 | ✅done | 02 | M |
| G2S-11 | Prove eventReports flow LIVE on real AVP | eventHandler | P0 | ✅done — live-proven 2026-07-02 (events streamed on both overnight joins) | 08,09 | S |
| G2S-12 | Route eventReports to typed app hooks | eventHandler | P0 | ✅code-done 2026-07-02 (8da9bd9 — typed hook registry; live validation tonight) | 11 | M |
| G2S-13 | Parse eventReport affected-data into state | eventHandler | P1 | ✅code-done 2026-07-02 (8da9bd9 — live activity tape; live validation tonight) | 12 | M |
| G2S-14 | getEventHandlerLog backfill (eventHandlerLogList→ring merge deduped by eventId + lastSequence high-water; live eventReport retries re-acked not re-counted) | eventHandler | P2 | ✅done — live-proven 2026-07-02 (backfill ran on both overnight joins) | 12 | S |
| G2S-15 | meters read path (getMeterInfo + parser) | meters | P0 | ✅done — live-proven 2026-07-02 (2,142 meters parsed) | 02 | M |
| G2S-16 | meters subscriptions (setMeterSub onPeriodic 60s + onEOD auto-installed at join, meterSubList sub state, periodic/EOD-push→meterInfoAck, /api/command set/get/clearMeterSub) | meters | P1 | ✅done — live-proven 2026-07-02 (deployed on the Pi, periodic 60s reports firing) | 15 | M |
| G2S-17 | cabinet read path (status/profile/dateTime) | cabinet | P0 | ✅done — live-proven 2026-07-02 | 02 | M |
| G2S-18 | commConfig read: commHostList parse + ack | commConfig | P0 | ✅done — live-proven 2026-07-02 (host 1 already owns ~145 devices) | 02 | M |
| G2S-19 | commConfig ownership cycle (~~THE gate~~ **MOOT as a gate**, d556ad2 — never a prerequisite; disables play while it runs. **NOT parked**: it is config-WRITE machinery alongside G2S-27, and the July-1 'entering' stall predates d556ad2 — likely the same double-wrap blocker bug) | commConfig | P3 | 🔨built* — bench-retest tonight | 18 | L |
| G2S-20 | cabinet control (setDateTime + join-time auto clock-sync with skew tracking; setCabinetState with sacred restore path) | cabinet | P0 | 🔨built* (setCabinetState 7654d45; clock-sync slice live-proven 2026-07-02; live cabinet-disable test pending) | 17 | M |
| G2S-21 | gamePlay read path (getGamePlayStatus/Profile/getGameDenoms builders + parsers; sweep deliberately on-demand since a3b19f8; recall todo) | gamePlay | P1 | 🔨built* — needs only a remote /api/command live-fire | 02 | M |
| G2S-22 | gamePlay control (setGamePlayState/denoms) | gamePlay | P2 | 🔨built* (c2c0ee5; live test pending) | 21 | M |
| G2S-23 | noteAcceptor (bill-in feed + state) | noteAcceptor | P1 | 🔨built* (1d9055e; status parsed live, bill-in feed awaits a physical bill) | 12 | M |
| G2S-24 | printer (templates + printTicket) | printer | P2 | ⬜todo (physically blocked on the replacement Netplex cable) | 12 | M |
| G2S-25 | handpay (request/keyedOff + setRemoteKeyOff) | handpay | P1 | 🔨built* (a9e970e — first-class reset-to-credits; live handpay test pending) | 12 | M |
| G2S-26 | voucher TITO tier-1/2/3 (tier-1: durable VoucherStore id lists, getValidationData/issueVoucher/commitVoucher; tier-2 redeem/authorize; **tier-3 device commands + log reconcile 2026-07-07**: getVoucherStatus/Profile/LogStatus/Log builders + folds into /api/status.voucherDevice, setVoucherState printing toggle, join probe status+profile, getValidationData issuance cap now PROFILE-driven maxValIds — 100 fallback until the profile reveals it; `getVoucherLog` MERGES the EGM's voucherLogList into the durable store: tickets issued/redeemed while the host was down get reconciled onto ids THIS host minted — **stage-B fix 2026-07-07: an id the store never issued is NEVER minted as a money record** (§21.26 masks every logged validationId to its last 4 digits and a digit pad passes any 18-char/isdigit gate, so an unmatched id IS a mask — ring-history/audit-only, invisible to /api/vouchers); (egmId, kind, transactionId) dedupe with redeem-side log entries keyed 'commit' like the live commitVoucher handler (no duplicate ring records on the first fetch after a live redemption); open egmAction=G2S_pending entries skipped whole and only an explicit G2S_rejected close-out releases a redeemPending hold (§21.20 survives a mid-escrow log fetch); close_redemption's CONSUMING path now requires the pending-holder (egmId, txn) — a stray/foreign G2S_redeemed commit can't clobber a live redemption; logSequence-ordered so issue precedes redeem, bounded 200/merge; GET /api/vouchers ticket API — newest-first, vid= exact lookup, state/egmId filters, limit/offset paging, derived 'expired' presentation state. **NO QR** — a UI-rendered ticket QR was built then CUT by owner decision 2026-07-07: the machines print barcoded tickets phones scan directly, so /api/ticket_qr + qr_min.py were removed; the UIs show the 18-digit validationId large/copyable instead) | voucher | P2 | 🔨built* (tier-2 c26890b; tier-3 replay-validated 2026-07-07; validation-data bench test pending) | 12 | M |
| G2S-27 | optionConfig change cycle (full setOptionChange→pending→auto-authorize→applied state machine, multi-param edits in ONE txn, post-apply scoped getOptionList read-back auto-verifies + refreshes inventory/lamp; purpose-built `enableRemoteHandpay` flips G2S_enabledRemoteCredit + disabledRemoteCredit on handpay/1) | optionConfig | P2 | 🔨built* — bench flip tonight | 07 | L |
| G2S-28 | progressive (value broadcast + hit/commit) | progressive | P2 | ⬜todo | 15 | L |
| G2S-29 | bonus (setBonusAward + celebration) | bonus | P2 | ⬜todo | 12 | L |
| G2S-30 | idReader + player (card + greetings) | player | P3 | ⬜todo | 12 | L |
| G2S-31 | coinAcceptor/hopper stubs (present-only) | coinAcceptor | P3 | ⬜todo | 12 | S |
| G2S-32 | IGT-ext capture-decode (igtMediaDisplay/Tourn/PC/SC/cg) | igt-ext | P1 | ⬜todo | 11 | L |
| G2S-33 | igtMediaDisplay on-screen overlay | igt-ext | P2 | ⬜todo | 32 | L |
| G2S-34 | Federated tournaments (sync AFT + leaderboards) | igt-ext | P2 | ⬜todo | 32,15 | L |
| G2S-35 | RFID/NFC player ID + jackpot reset fobs | igt-ext | P2 | ⬜todo | 30,32 | L |
| G2S-36 | Test Panel + console touchscreen UI (bench cockpit built+replay-validated 2026-07-01: webui/index.html self-contained panel — traversal-guarded static serving at / + /webui/*, every /api/command action grouped Reads/Subs/Clock/Ownership with two-step guarded confirm, 2s status poll, reply tape; 800x480 + desktop renders verified; supersedes console.html, legacy kept at /console) | ui | P1 | 🔨built* | 10 | L |
| G2S-37 | Multi-EGM heterogeneous fleet dispatch (AVP+WMS) | infra | P2 | ◐partial | 17,15 | L |
| G2S-38 | Keep avp_replay.py green as regression gate (the restart-nudge sub-feature once tracked here was proven INERT — restartNudges=0 across a real restart and a power-cycle rejoin — and RETIRED in ca75f1c, 2026-07-02; the spec-mandated MSX003 refusal + commsClosing dialog remain; the tool now refuses live hosts unless `--force`) | infra | P0 | ◐ongoing — gate count 667 as of 2026-07-07 (620 + the WAT money-safety slices: crash-idempotent ledger refs, txn-reuse discrimination, terminal-state re-authorize, blank/local watCancel, presentation contract; + the stage-B voucher-log/reboot correctness slices: masked/unknown ids never minted as money records, §21.20 hold survives a mid-escrow log fetch, live-commit vs log-entry dedupe on the unified 'commit' kind, close_redemption holder guard vs foreign redeemed-commits, EGM-originated scriptStatus-as-G2S_request → scriptStatusAck not APX008; the gate's own RESULT line is authoritative — never pin) | — | S |
| G2S-39 | wat class + accounts — "add credits without paper" (spec ch.22: host push initiateRequest→requestPending→initiateTransfer→authorizeTransfer→commitTransfer with escrow-at-authorize/reconcile-at-commit + §22.31 commitSeen retry dedupe; EGM cash-out leg requestId=0 gated by setWatCashOut, one-device rule assisted; setWatState, watStatus/watProfile folds, watLog reads, PIN-less getWatAccounts/getWatBalance/getKeyPair answers hashType=G2S_none; durable WatStore `data/wat_state.json` + AccountStore `data/account_state.json` house-may-go-negative rule; join probe wat/1+/2; /api/command addCredits/watCancel/wat*/setWatCashOut + voucher-device reads, /api/accounts CRUD+ledger, /api/debug/log; WTE labels + wat_transfer_completed/failed hooks off commitTransfer; **money-safety hardening 2026-07-07**: every account movement is ref-idempotent (ledger ref wat:egm:txn:createdAt:leg, adjust once=True) so crash-retries/duplicates can never double-apply or lose a commit; terminal-state initiateTransfer re-draws the stored verdict (no re-escrow); txn-reuse after an EGM RAM clear discriminated by field-match in on_initiate/on_commit (the commsOnLine reset flags fire on EVERY AVP boot and cannot be the fence); cancelRequest rides the record's own device; watCancel blank=newest-active, txn-less records cancel LOCALLY; /api/status wat transfers emit the presentation contract — direction, normalized cashable/promo/nonCash-Millicents trio, ISO createdAt/updatedAt, lazy requestTimeout) | wat | P1 | 🔨built* — replay-validated 2026-07-07, live AVP push pending | 12,15 | L |
| G2S-40 | ticket browser + Home UI routes (GET /api/vouchers off the DURABLE VoucherStore — newest-first, vid= exact lookup, state/egmId filters, limit/offset paging, contract shape per the 2026-07-07 API spec; **NO QR — owner cut it 2026-07-07** (machines print barcoded tickets; `qr_min.py`/`tools/test_qr_min.py`/`/api/ticket_qr` deleted, gate asserts the endpoint stays a plain 404); routing: / prefers webui/home.html and FALLS BACK to the Test Panel until it exists, /test + /testpanel serve index.html, /home serves home.html, /console + /webui/* kept; Test Panel gained voucher-device buttons, gameSweep/getOptionList coverage-gap buttons, an /api/vouchers?vid= ticket-lookup widget, the WAT group w/ two-step ARMED money confirms, /home topbar link; Home UI ticket modal shows the validationId LARGE + copyable; **WAT owner-guard**: owner-type wat actions (setWatState/setWatCashOut/addCredits/cancelRequest) refuse any deviceId outside the config-sync-revealed owned G2S_wat set — the live AVP surfaced G2S_wat/3 deviceGuest=true (its internal SAS 6.02 personality) 2026-07-07; guest-safe reads unrestricted) | ui | P1 | 🔨built* — replay-validated 2026-07-07 (home.html itself is a parallel task) | 26,36,39 | M |

## Out of scope (permanent)

- **X.509 certs / SCEP / OCSP** — cert-less by permanent decision; TRANSPORT_ACK is byte-matched.
- **GAT** (game-authentication/signature) — regulatory integrity check, irrelevant at home.
- **Signed/verified download** — licensed-casino concern; no firmware pushing.
- **Multi-host co-authorization** — single-host topology; self-only authorizeList.
- **REAL casino cashless WAT/AFT accounts w/ PIN** — still out of scope (PIN/auth hardening, hashed authCodes, real-money account backing are casino concerns). **Narrowed 2026-07-07 per AJ:** PIN-less *enthusiast* WAT accounts (hashType=G2S_none, JSON-file AccountStore, house-is-the-bank) are IN scope and built — see G2S-39. AFT credit-push reused inside tournaments remains the SAS-side story.
- **central (class-II/server outcomes)** — AVP/WMS compute RNG locally.
- **deviceConfig (ch.7)** — empty `[In development]` placeholder in v1.0.3; commConfig+optionConfig cover it.
- ~~**DHCP opt-43 host-URL** — superseded; AVP host set manually~~ — **WRONG, corrected 2026-07-01:** plug-and-play DHCP is real and wire-proven — Override DHCP Configured Host=NO + single-TLV Option 43 (`01 04` + host IPv4, `encode_igt_avp_options` in `python/web/dhcp_dns_server_enhanced.py`) delivers the host IP (port/path come from the AVP's persisted URI segments); manual URI also works. It lives in the DHCP server, not the G2S host — in scope and functional, not superseded.

## Current state (2026-07-02)

**🔨built\*** = code-complete + replay-validated (`avp_replay.py` must end **0 failed** — the count
grows, 415 as of 2026-07-02; run against a LOCAL dev host only, the tool now refuses live targets
unless `--force`) but **not yet live-proven** on the machine.

The ownership-gate premise is dead (d556ad2, single-wrapped sync g2sAck): the AVP streams
descriptors, events, and meters with zero commConfig traffic — host 1 already owned ~145 devices.
On top of that, today's fix chain landed: offline demotion on dead links (a878a13), log rotation +
startup sweep + audit micro-batch (b79c0ba), restart-nudge retirement + replay live-guard
(ca75f1c), getOptionList + durable option inventory (99ad6d1), and typed event hooks + the live
activity tape (8da9bd9). Changelog note: the long-standing **"MACHINE JOINED banner spam" open item
is FIXED** — the banner is once-per-epoch (verified across both overnight joins); do not re-fix.
**2026-07-02 discovery: the AVP ships `handpayRemoteCreditAllowed=false`** — the first live
getOptionList (configOptionCount=425, 149 config devices) revealed `G2S_enabledRemoteCredit=false`
on G2S_handpay/1, which blocks the handpay reset-to-credits bench test (setRemoteKeyOff would draw
G2S_JPX002). The wire path to flip it is built (G2S-27): `enableRemoteHandpay` (or generic
`setOption`) → setOptionChange → auto-authorize → applied → scoped getOptionList read-back that
verifies the values and re-lights the lamp. Key off a FRESH lockup after the flip verifies — a
lockup raised before it still carries its own remoteCredit=false (Table 11.6).
Remaining live validation: event hooks/activity tape tonight (G2S-12/13), a remote gamePlay sweep
(G2S-21), then bench tests for the remote-handpay flip (G2S-27) followed by handpay
reset-to-credits (G2S-25), cabinet/gamePlay control (G2S-20/22), bill-in (G2S-23), and the voucher
cold-boot check (G2S-26). Printer work (G2S-24) stays blocked until the replacement Netplex cable
arrives.
