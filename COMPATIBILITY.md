# CabiNet Slot Machine Compatibility Matrix

Which slot machines can talk **direct G2S over IP** to CabiNet, and which need the **SMIB hardware bridge** running SAS over RS-232. The machines we haven't seen are the whole point of sharing this — use this file to figure out what YOUR machine speaks, then follow `deploy/DEPLOY.md` § "Slot machines" for the exact settings (some fields are one-shot until a RAM clear — read that section before touching the operator menu).

> **The two golden rules, wherever your machine came from:**
> 1. **G2S machine, any brand:** host URL is `http://192.168.50.2:8081/G2S`, and if the machine offers a G2S **flavor/dialect selector, pick IGT** (IGT machines need neither — they pick everything up from DHCP).
> 2. **SAS machine:** validation = **Secure Enhanced** if offered, otherwise **System** — both tie into the hub. Enable AFT and legacy bonusing if the menu has them.

> **Status: PRELIMINARY**. Cutoffs below are compiled from vendor docs, GSA listings, and NLG / Slottech community knowledge. Bench results from real machines take precedence over any claim here — that's where your machines come in. When in doubt: **test, then open an issue with a support bundle + your row for the bench log**.

> Confidence labels: **[V]** verified by cited source · **[T]** training-data knowledge, not freshly cited · **[U]** uncertain, see Uncertainties section.

---

## How to read this

- **Direct G2S** — EGM speaks G2S over Ethernet natively; CabiNet host connects to the machine without a SMIB.
- **SAS only** — serial SAS over RS-232; CabiNet needs the SMIB hardware bridge.
- **Depends** — same cabinet shipped with multiple CPU boards / OS families; G2S only on the newer ones.

---

## IGT

| Model / Family | G2S Status | Notes |
|---|---|---|
| S-Series, S+, S2000, S3000 | SAS only [T] | 1980s–early 2000s reel/video; no Ethernet stack. SMIB required. |
| Game King (I-Game) | SAS only [V] | Confirmed on NLG / Slottech via SAS RS-232. |
| Vision Series, Reel Touch | SAS only [T] | Pre-AVP. |
| AVP 1.x (early G20/G23) | SAS only [T] | Original AVP launch (~2005–2007), predates IGT's G2S production rollout. |
| AVP 2.0 / 2.5 | Depends — likely SAS in field [U] | Platform supports Ethernet; G2S stack enabled depends on game-package SKU and jurisdiction approval. Most field machines are SAS. |
| **AVP 3.0 (Family 14, OS 014-00305+)** | **Direct G2S** [V/T] | Family 14 quick-reference and IGT sbX training material confirm G2S in this OS family. AVP 3.0 / Crystal Curve / CrystalView ship with this. |
| AVP 4.x / Axxis / CrystalDual | Direct G2S [T] | Modern IGT cabinets, factory G2S. |

**Cutoff for IGT AVP:** look for OS **Family 014** (`AVP014-00xxx`). Family 12/13 = SAS-era; Family 14 = G2S-capable.

---

## WMS / Scientific Games / Light & Wonder

| Model / Family | G2S Status | Notes |
|---|---|---|
| **Bluebird 1 (BB1, CPU-NXT1)** | **SAS only** [V/T] | NLG and Slottech BB1 threads only discuss SAS. NXT1 has no native Ethernet G2S stack. SMIB required. |
| Bluebird 2 (BB2, CPU-NXT2) | Depends — usually SAS [V/U] | BB2 cabinets shipped with CPU-NXT2 (SAS-era). G2S only after CPU-NXT3.2 retrofit. |
| **BB2 / BB2E with CPU-NXT3 / NXT3.2 retrofit** | **Direct G2S** [V] | Part `A-026352-xx` (GETT CPU131) fits BB2/BB2E and BB3. Look for "G2S Diagnostics Menu" in the OS. |
| Bluebird xD (BBxD) | Direct G2S [T] | ~2010, NXT-series CPUs. |
| Helios / Bluebird 3 (BB3) | Direct G2S [V] | Ships with CPU-NXT3.2 by default. |
| Gamefield xD, Twinstar, Kascada | Direct G2S [T] | Modern L&W cabinets, factory G2S. |

**Cutoff for WMS BB2/BB2E:** open the CPU cage. Stamped **`A-017999-xx`** = NXT2, SAS only, SMIB required. Stamped **`A-026352-xx`** = NXT3.2, G2S possible. Confirm in operator menu by looking for the **G2S Diagnostics Menu** entry — its presence means G2S stack is live in that OS.

> "Sentinel" in WMS context = the iVIEW-style player tracking display, **not** a protocol. Don't confuse it.

---

## Bally / Scientific Games

| Model / Family | G2S Status | Notes |
|---|---|---|
| S6000 / S9000 / Game Maker (mechanical & early video) | SAS only [T] | iView SMIB attaches externally; no native G2S. |
| Alpha 1 | SAS only [T] | First-gen Alpha (~2003); pre-G2S. |
| Alpha 2 (Pro Series V22, V32) | Depends — newer CMS-Phase-2 firmware adds G2S [V] | Phase 2 doc enables "web-based communications including G2S" on Alpha and S6000. Most field Alpha 2s are still SAS via iView. |
| Alpha 2 Pro V32 / Pro Wave / Pro Curve | Direct G2S in supported builds [T] | Modern Alpha 2 deployments with current OS. |
| **Alpha Elite (V32 Elite, Pro Slant Elite)** | **Direct G2S** [T] | 2014+ generation, factory G2S. |

---

## Aristocrat

| Model / Family | G2S Status | Notes |
|---|---|---|
| MK5 | SAS only [T] | Late-1990s; serial only. |
| MK6 (XCite, Hyperlink) | SAS only [V/T] | Slottech Jan 2020 thread confirms G2S adoption was minimal. |
| MK7 / Viridian (incl. Viridian WS) | SAS only by default; G2S possible in later OS [V/U] | Same hardware family as MK6 with separate GPU. G2S not standard on MK7. |
| **Helix / Helix+ (MK8 lineage)** | **Direct G2S** [V] | Service manual references "G2S host" for ID Reader, downloads, operator authorization. |
| Helix XT | Direct G2S [V] | 2018+ manual is explicitly G2S-aware. |
| Edge X, ARC Single/Double, MarsX, Neptune | Direct G2S [T] | Modern Aristocrat cabinets. |
| Vertex progressive controller | G2S [V] | Aristocrat used G2S here before main cabinets did. |

---

## Konami

| Model / Family | G2S Status | Notes |
|---|---|---|
| Advantage Series (pre-Podium) | SAS only [T] | |
| Podium KP3 | SAS only at launch; G2S "future" [V] | Konami's KP3 page stated G2S compatibility was a future release. Field KP3s deployed pre-2015 are SAS. |
| **Concerto / Concerto Crescent (KP3+)** | **Direct G2S** [V] | KP3+ is the G2S-era generation, debuted G2E 2015. |
| Concerto Stack / Coda / Opus | Direct G2S [T] | KP3+ derivatives. |
| Dimension 27 / 49 (Dimension XL) | Direct G2S [T] | Modern. |

---

## Other vendors

| Vendor / Family | G2S Status | Notes |
|---|---|---|
| Atronic Cashline / E-motion | SAS only [V] | Pre-G2S era. |
| Spielo (post-Atronic) INTELLIGEN / G3 cabinet | Direct G2S [V] | INTELLIGEN was first G2S-compatible system on government VLTs (2010). |
| AGS (Orion, ICON, Curve, Starwall) | SAS standard; G2S on newer Orion variants [V/U] | |
| Everi / Multimedia Games (Empire, Player Station) | Mixed — G2S on tournament/newer builds [V] | Slottech reports G2S can be enabled for tournament setups. |

---

## Uncertainties (verify on bench before publishing)

1. **Earliest AVP Family 014 OS rev with production G2S stack.** The `00305` lower bound is from the OS quick-reference; the very earliest builds may have G2S disabled or jurisdiction-locked.
2. **BB2 NXT2 → NXT3.2 upgrade reality.** Strong evidence points to a **board swap** (different part numbers) being required, not a re-flash. If your BB2/BB2E came with NXT3.2 from the factory, that's a useful data point — report it.
3. **Bally Alpha 2 G2S firmware revision.** "Phase 2 CMS" is from a 2010 Bally G2E announcement; the specific Alpha 2 OS build that exposes G2S is not nailed down in public docs.
4. **Aristocrat MK7/Viridian G2S.** The 2020 Slottech consensus is "G2S barely used"; whether the MK7 OS even has a G2S stack to enable is unclear.
5. **Konami KP3 retrofit path.** Whether KP3 cabinets ever got a field-upgradable G2S package, or whether you must move to KP3+ hardware, is unclear.

---

## SAS validation modes — CabiNet host support

> Live-proven on the WMS BB2 (SAS 6.02) via the Zero 2W SMIB. Host-side code: `SAS/core/sas_tito_host.py`; ticket ledger: the hub TitoAuthority (`/api/tito`, backed by `G2S/hub_store.py`).

| Mode | Host support | Bench status |
|---|---|---|
| **Secure enhanced** — EGM self-mints 16-digit validation numbers, no per-cashout host gate | **Current primary.** A one-time 0x4C validation-ID seed answers the EGM's 0x3F "validation ID not configured" tilt (auto-seed in `sas_tito_host.py`, cooldown-gated, only fires while unseeded); the host captures each printed ticket via 0x4D and records it to the hub ledger (`record_issued` → `/api/tito/issued`), the same ledger behind cross-machine redemption. | Auto-seed + self-mint live-proven 2026-07-09 (post-RAM-clear pivot); cross-machine TITO loop through the hub live-proven 2026-07-08 (WMS BB2 ticket → IGT AVP and AVP voucher → BB2, on paper) |
| **System** — host mints the validation number in real time (exception 0x57 → 0x58 reply) | **Kept dormant as the compatibility fallback — toggleable.** hub.db `host_settings.sysval_fallback` (absent = on), Settings-tab toggle in the web UI; each satellite flips `SASTITOHost.auto_service` from the hub's report reply. Toggled off, a 0x57 goes unanswered and the cash-out falls to the machine's own handpay path. | Live-proven 2026-07-08 (0x57 → 0x58 → 0x3D → 0x4D handshake on the WMS BB2) |
| **Standard** — machine-only validation | Read-path fallback only: the 0x3D cash-out-ticket read (its validation field is all zeros when the machine is configured enhanced/system). | Guide-cited (Montana §4.6.3); the all-zeros-when-enhanced behavior observed live in the 0x57 handshake chain |

**RAM-clear gotchas (live, 2026-07-09, WMS BB2):** a RAM clear resets the machine's validation setup — the EGM tilts with exception 0x3F until the host re-seeds the validation ID (the auto-seed handles this; re-check the validation-mode selection in the operator menu) — **and silently disables in-house AFT transfers** while leaving AFT registration intact (every 0x72 then answers 0x82 "not a valid transfer function", lock requests draw 0xFF, and the 0x74 status advertises printer-only). Re-enable AFT/cashless in the operator menu after any RAM clear.

---

## SAS money-scale & multidrop portability (untested machines)

> The bench floor is a **penny** WMS BB2 at **SAS address 1**. Two behaviors were calibrated to that machine; both now degrade honestly for a differently-configured SAS cabinet, but a non-penny box needs one operator step.

- **Set the denomination for any non-penny machine.** A linked SAS leg reports its credits as a **raw 0x1A credit count**; the hub converts to money with `credits × denomination × 1000` millicents. The machine *does* carry its own denomination in the 0x1F frame, but the SAS **denomination-code → cents table lives in the full SAS spec, not the on-disk Montana guide**, so CabiNet does **not** decode it — guessing another vendor's code table would swap a visible assumption for a silent misread. When the denomination is unset the hub falls back to **1¢/credit**: correct for a penny machine, but a `$1` / nickel / quarter cabinet then reads its CREDITS value **and tournament score** low by the denom factor (100× on a `$1` box, 5× on a nickel). This is now surfaced, not silent — the floor tile shows **"denom unset · 1¢/credit assumed"** and the tournament seat carries a `denomAssumed` flag — but **you must set the real denomination** in the machine's ⚙ Options (tap its name → *Denomination*, whole cents `1..10000`) before its money and race score are correct. Set a penny machine to `1` to clear the note. Money **transfers** (AFT fund / cash-out) are cents-based and stay correct regardless; only the displayed value and the leaderboard scale off the denomination.
- **Exceptions at address 6/12 on a larger multidrop loop.** A SAS machine answers a general poll with `address | 0x80` as a busy/NACK chirp (live-seen on the addr-1 BB2 as `0x81`). That value collides with two documented general-poll exceptions — **`0x86` "game out of service" (address 6)** and **`0x8C` "game selected" (address 12)**. The poller now **dispatches** the byte as a real exception when it matches a documented code and only folds it as a busy chirp otherwise, so a spec-compliant machine at those addresses reports its status correctly. Addr-1 machines (the common home case) are unaffected: their chirp `0x81` has no documented exception entry and still folds.

---

## Bench-test log (real iron)

> Empirical findings from running CabiNet against actual hardware — the dev bench AND community machines. **This section overrides anything above.** Format: machine + OS rev → result + any quirks/timeouts/workarounds. Running it on a machine not listed here? A row for this table (plus a support bundle if anything fought back) is the single most valuable thing you can send.

| Date | Machine | OS / CPU rev | Result | Notes |
|---|---|---|---|---|
| 2026-07-13 | IGT AVP (`IGT_00012E492815`) | Family 14, direct-G2S (cert-less, DHCP opt43) | **RFID Companion LIVE**: Pi Zero 2W + PN532 (`companion-avp`, G2S-only, no SAS leg) → tap fob `6CB16F06` → hub `setIdValidation` → **carded player menu on the AVP's own glass, functional**. | ⚡ **The Companion Zero powers off the AVP's BUILT-IN USB HUB with NO issues — no separate PSU.** `vcgencmd get_throttled`=**0x0** (zero undervoltage/throttle since boot), core 1.256 V, 36 °C, and the Realtek RTL8152 USB-Ethernet HAT enumerates clean on that same hub — so the cabinet powers both the Zero and its wired-net HAT. Universal i2c-gpio wiring (GPIO23/24 → `/dev/i2c-11`, `delay_us=2`). Bring-up gotcha: an intermittent SDA dupont read as GPIO23 hard-low (a forced pull-up couldn't lift it) — **reseat fixed it**; `pinctrl get 23,24` (both must idle HIGH) is the fast check. The AVP's own built-in `idReader` device returns `APX016 Device Disabled` and is unused — the card is injected host-side via `setIdValidation`. |
| 2026-07-08 | WMS BB2 (bench) | game ID `WM`/`000`, paytable `42B19D`, base 95.99%, SAS **6.02**, serial 1000, 1¢ denom | **SAS FULLY PROVEN via Zero 2W SMIB**: general+long polls, legacy bonus 0x8A credited $0.10 (visually confirmed), AFT 0x73 registration completed (asset 1000, POS 1, key echoed back) | **ROOT-CAUSE LANDMINE**: `tcdrain()`/`flush()` between the wakeup byte and frame body sleeps ~20 ms on Pi kernels → violates spec §2.3.2 (5 ms inter-byte max) → machine silently discards EVERY multi-byte poll while answering 1-byte general polls. Fixed in `sas_serial.send_frame` (busy-wait ~0.86 ms, never drain mid-frame). Other live-proven facts: exception `0x1F` = "no activity, waiting for player" (spec §12.6), streams in attract, flips to `0x00` when credits present; 0x73 response layout `[len][status][asset 4][key 20][POS 4]` confirmed; reg status `0x80`=unregistered `0x01`=registered; Type-R polls bare (no CRC) confirmed; machine ACKs Type-S with bare address byte; wedged-UART recovery = reboot the SMIB (power cycle). |
| 2026-07-16 | WMS BB2E (bench, dual-protocol) | NXT3.2, SAS 6.02 + G2S | **Dual-protocol LIVE**: the same bench BB2E joined G2S with the **flavor/dialect selector = IGT** and host URL `http://192.168.50.2:8081/G2S`, while SAS stays connected via the SMIB — the hub links both legs into **one tile with SAS as the money authority**. G2S carries per-title game control, meters, and events. | The G2S comms fields only open at the **post-RAM-clear boot** config window — have the values ready before you clear. Enter the full URL: the BB2E's URL parser mishandles a bare scheme/port-0 entry. This OS's G2S is partial (its `wat` class is a dead stub), which is exactly why money and the whole-machine lock ride the SAS leg on a linked cabinet. |
| _TBD_ | WMS BB2E #2 | _TBD_ | _TBD_ | _TBD_ |
| _TBD_ | WMS BB1 | _TBD_ | SAS via SMIB | BB1 has no G2S path; baseline. |

---

## Buyer's guide — for hobbyists picking up used machines

**Universal rule of thumb:** if the machine is from before ~2012 and hasn't been re-flashed by a casino IT department in the last 5 years, assume **SAS only** and plan for the SMIB. G2S deployment in the wild is dramatically lower than vendor marketing suggests — even casinos largely run SAS in production.

### What to ask the seller

1. "What CPU board / motherboard part number is in it?" — most informative single question.
2. "What OS / software family / boot chip version is loaded?" — IGT Family 12 vs 14; WMS NXT2 vs NXT3.2; Aristocrat MK6 vs Helix.
3. "Does it have an Ethernet jack populated and labeled for G2S/SBG?" — many G2S-capable cabinets shipped with the port unwired.
4. "Was this machine ever connected to a casino G2S/SBG host?" — if yes, it almost certainly has the stack present.

### Stickers / labels to look for

| Vendor | Where to look | What it tells you |
|---|---|---|
| IGT AVP | Software-package sticker on door interior or CPU cage | `AVP014-xxxxx` = Family 14, G2S-era. `AVP012-xxxxx` / `AVP013-xxxxx` = older, SAS-era. |
| WMS BB2/BB2E | CPU cage stamp | `A-017999-xx` = NXT2, SAS only. `A-026352-xx` = NXT3.2, G2S-capable. |
| Bally Alpha | Backplate sticker | "Alpha", "Alpha 2", "Alpha 2 Pro", "Alpha Elite". Elite = G2S-era. |
| Aristocrat | Cabinet badge | MK6 / Viridian (MK7) = SAS. Helix / Helix XT = G2S. |
| Konami | Cabinet badge | KP3 = SAS. KP3+ / Concerto = G2S. |

### Menu screens to confirm G2S is actually live

| Vendor | Path | What to look for |
|---|---|---|
| IGT AVP | Operator Menu → Setup → Communications | "G2S Host" / "Host Configuration" entries (Family 14 only). |
| WMS BB2/BB3 (NXT3.2) | Audit/Diagnostics menu | **G2S Diagnostics Menu** entry (called out by name in NXT3.2 OS service manual). If absent, you're on NXT1/NXT2 — SAS only. |
| Bally Alpha 2/Elite | Operator Menu → Communications | CMS / SBG protocol selection. |
| Aristocrat Helix/XT | Setup → Comms | "G2S Host" entry (per Helix XT service manual). |
| Konami Concerto/KP3+ | Operator menu → Communications | SAS vs G2S/BoB selection. |

If the machine doesn't expose a G2S menu, treat it as SAS-only regardless of what the seller claims, and route through the CabiNet SMIB.

---

## Bottom line — strategic implication

**SMIB is not optional for CabiNet's launch market.** It covers ~80%+ of what hobbyist buyers actually own (BB1, BB2-NXT2, MK6, Alpha 1/2 pre-Elite, Game King, S2000, KP3, Atronic). Direct G2S is a "nice to have" upsell for the minority running Family-14 AVP, NXT3.2 BB2/BB3, Helix-class Aristocrat, or KP3+ Concerto.

The product spec should lead with "CabiNet works on every SAS machine ever made via the SMIB; if you have a modern G2S-capable cabinet, you can skip the SMIB and connect directly." Not the other way around.

---

## Sources

Cited where possible — preserve confidence labels (V/T/U) when copying claims into spec/marketing material.

- [NLG IGT AVP board](https://newlifegames.com/nlg/index.php?board=99.0)
- [Slottech "Anybody using G2S instead of SAS?" thread](http://forums.delphiforums.com/slottech/messages/18287/1) — primary community ground-truth on real-world G2S adoption
- [WMS CPU-NXT/NXT2/NXT3.2 OS service manual (16-020832)](https://www.scribd.com/document/744435273/16-020832-Nxt1-Nxt2-Nxt3-3-2x-Os-Service-Manual)
- [WMS BB2 operator manual 16-022128-03](https://www.scribd.com/document/341301427/OPERATOR-16-022128-03-Manual-BB2-pdf)
- [WMS NXT3.2 CPU A-026352 (GETT CPU131)](https://get-t.net/product/cpu131/)
- [IGT AVP OS Software Family 014 quick reference](https://www.scribd.com/document/516916236/User-Guide-Quick-Reference-AVP-OS-Software-Family-014)
- [IGT University class descriptions — AVP/sbX/G2S integration](https://www.igt.com/products-and-services/support/igt-university/igt-slot-technical-training/class-descriptions)
- [IGT SuperSAS at G2E (origin of SAS→G2S merger)](https://ir.igt.com/news/news-details/2004/IGT-Debuts-SuperSAS-at-G2E/default.aspx)
- [Aristocrat Helix XT service manual](https://www.manualslib.com/manual/3151385/Aristocrat-Technologies-Helix-Xt.html)
- [Konami KP3 product page](https://www.gaming.konami.com/KP3/)
- [Konami Concerto launch / KP3+](https://www.konamigaming.com/home/2016/08/24/konami-s-all-new-concerto-video-slot-lineup-brings-new-depth-and-diversity-to-g2e-2016)
- [Bally G2E 2010 G2S floor-wide bonusing](https://www.yogonet.com/international/news/2010/11/10/5697-bally-spotlights-floorwide-bonusing-applications-at-upcoming-g2e-2010)
- [INTELLIGEN/Spielo first-G2S VLT system](https://www.intralot.com/newsroom/intralots-vlt-system-certified-by-gsa-first-to-receive-gsa-transport-and-security-certification-/)
- [GSA Game-to-System Committee (IGSA)](https://igsa.org/committees/g2s-game-to-system-committee/)
