# Bench Bring-Up — WMS BB2E "First Contact" (capture-and-go runbook)

> **Day-one goal is CAPTURE, not join.** Nothing on the WMS side is
> wire-proven yet — every WMS-specific claim in the repo is scaffolding.
> The win condition for the first BB2E session is a complete pcap + wire log
> of whatever the machine actually does, per OS version. A join is a bonus.
>
> Companion to `BENCH_BRINGUP.md` (the proven IGT AVP runbook). Server-side
> steps are shared; the AVP doc's join-milestone section applies verbatim IF
> a BB2E gets that far. Fleet context: 10+ BB2E cabinets across 5+ OS
> versions; protocol path is **game-dependent** (some titles on G2S-capable
> cabinets are SAS-only — see `COMPATIBILITY.md`).

---

## 1. What we KNOW (server side — EGM-agnostic, wire-proven on the AVP)

The whole host stack runs on the Pi 5 (`the host box`, Wi-Fi = management
path, **eth0 = slot net, static 192.168.50.2/24**) as enabled systemd units,
all pinned to eth0:

| Unit | What | Notes |
|---|---|---|
| `casinonet-dhcp` | `start-dhcp-enhanced.py --interface eth0` | leases .100–.200; logs **opt55 param-request + opt60 vendor-class + opt43-requested verdict** on every DISCOVER/REQUEST |
| `casinonet-dns` | discovery DNS on :53 | resolves `g2s.local` / `g2shost.local` / `casinonet.local` → .2 |
| `casinonet-ntp` | NTP on :123, stratum 1 | origin-echo fixed 2026-07-01; disciplined the AVP |
| `casinonet-tftp` | serves `G2S/tftp-root/` on :69 | any fetch is logged — a WMS fetch is a capture event |
| `casinonet-g2s` | `g2s_host.py --harvest` on **:8081**, path `/G2S`, HTTP, cert-less | logs EVERY inbound POST raw to `logs/g2s_wire_*.log` **before** parsing |
| `casinonet-console` | tty1 cockpit on the DSI screen | parses the DHCP host-discovery lines live |

Also known:

- **The join choreography `g2s_host.py` implements is spec-generic** (G2S
  v1.0.3 ch. 2, not IGT-specific): `commsOnLine` → sync-reply `g2sAck` only →
  host-originated POST `commsOnLineAck` to the EGM's `egmLocation` →
  `commsDisabled`/`commsDisabledAck` → `setCommsState enable=true` →
  `commsStatus G2S_onLine`, then a host keepAlive ping every 10 s,
  `optionConfig.optionList → optionListAck`, sessionId-echo pairing. Any
  spec-conformant EGM should walk the same ladder.
- **Only the SOAP *wrapping* is IGT-matched.** The parser expects gSOAP-style
  double-wrap with the inner `g2sMessage` as an **escaped XML string**
  (`g2sRequest > g2sRequest`), wsdl ns `.../wsdl/g2s/v1.0`, schema ns
  `.../g2s/schemas/v1.0.3`, SOAPAction `SendG2SMessage` /
  `getTransportOptions`. That shape came from the IGT capture — the G2S SOAP
  transport binding is NOT in the protocol manual on disk.
- **Capture is guaranteed even on mismatch:** `do_POST` writes the raw body
  + SOAPAction to the wire log before any parsing. If WMS wraps differently,
  the host replies HTTP 500 (`no g2sRequest payload` in the journal) but the
  bytes are already saved. A 500 loop **is a successful capture session**.
- **DHCP option 43 lesson from IGT (wire-proven):** the AVP wants a single
  TLV (`01 04 <ip4>`) and **discards the entire option** if extra sub-options
  are appended. Assume WMS parsers can be equally strict.
- What our DHCP currently sends a WMS-detected client: option 43 = a
  6-sub-option URL blob (**speculative, see §2-U2 — likely wrong**), option
  60 = exact echo of the client's own vendor-class string (fixed
  2026-07-01: the WMS options dict used to carry a literal `60: b'WMS-BB2'`
  that OVERWROTE the echo in both OFFER and ACK — the very landmine the IGT
  bring-up documented; a BB2E validating the echo would silently ignore
  us), option 66 = `192.168.50.2`, option 67 =
  `wms/bluebird.cfg`, option 150 (Cisco-style TFTP IP). WMS detection
  matches opt60 against `WMS-Gaming` / `WMS-BB2` / `WMS-BlueBird`, MAC
  prefixes `00:A0:A5` / `00:1B:3F`, or hostname containing
  wms/bluebird/bb2 — **all unverified for BB2E**.
- **BB2E hardware/OS gate** (`COMPATIBILITY.md`): CPU cage stamp
  `A-026352-xx` = NXT3.2 = G2S-capable; `A-017999-xx` = NXT2 = SAS-only.
  OS tell: a **"G2S Diagnostics Menu"** entry in the Audit/Diagnostics menu.
- **Clock trap (from the AVP; assume it generalizes):** host requests carry
  `timeToLive=30000` evaluated against the EGM clock — an EGM running >30 s
  **ahead** of the Pi silently rejects them and the join stalls in sync.
  Set the machine clock equal to or slightly behind the Pi.

## 2. What we DON'T know (the capture targets)

Every row below is a question the first bench session should answer.
`TODO(bench)` markers in `tftp-root/wms/*` map to these.

| # | Unknown | How we'll know |
|---|---|---|
| U1 | Does BB2E DHCP at all on the G2S config, and does it **request option 43** (opt55 list)? What exact **opt60 vendor-class** string does it send? | `casinonet-dhcp` journal prints opt55/opt60/verdict per DISCOVER; Phase 1 pcap |
| U2 | **WMS option-43 payload format.** Single-TLV IP like IGT? Full URL string? Sub-option numbering? Our current 6-sub-option blob is design fiction — and two of its sub-options (5 and 10) are **malformed by a code bug** (`encode_wms_bluebird_options` clobbers `protocol` with `b'G2S_v1.0.3'` before building the URLs). Per the IGT lesson the whole option may be discarded. | Machine takes/ignores the host; try shrinking to `01 04 <ip4>` as first fallback experiment |
| U3 | **Does BB2E TFTP?** (The AVP never did.) If yes — which filename, and what file format does it expect? Our `wms/{bluebird.cfg,g2s.xml,wms.ini}` are unverified scaffolds. | `casinonet-tftp` journal + Phase 2 pcap (UDP 69 RRQ names) |
| U4 | **WMS SOAP wrapping.** Same gSOAP double-wrap/escaped-inner as IGT? Raw `g2sMessage`? Different SOAPAction / WSDL ns? (WMS also builds on gSOAP-era tooling, but that's an inference, not evidence.) | Phase 3: wire log `IN <<< POST ... SOAPAction=...` + raw body |
| U5 | **Operator-menu host entry.** Where does a BB2E take its G2S host URL — DHCP toggle? manual URL fields? default port (one real IGT field-sheet EXAMPLE used `:65501/g2s_6` — a site-specific config, NOT a documented factory default; AJ's own AVP shows `127.0.0.1` when unset)? Per-OS-version menu differences across the 5+ versions? | Menu walk + photos, §4; SYN-scan watch, Phase 3 |
| U6 | **Identity + callback:** WMS `egmId` format (IGT = `IGT_<12-hex MAC>`; `WMS_...`?) and the `egmLocation` callback URL/port (IGT uses `:8080`). The host POSTs the join responses there — a different scheme shows up as outbound connect failures. | First parsed `commsOnLine`, or raw body if unparsed |
| U7 | Does BB2E use our **DNS discovery names or NTP**? | Phase 2 pcap (UDP 53/123) |
| U8 | **WMS extension namespaces** advertised in `commsOnLine` (parallel to IGT's `igtMediaDisplay`/`igtTourn`/…) — feeds the showmanship roadmap | Wire log of first commsOnLine |
| U9 | Is the WMS join ladder byte-compatible with our IGT-tuned responses (attribute prefixes, `g2s:` attr namespacing, millisecond dateTime)? | Only observable after U4 is solved |
| U10 | **Per-game protocol selection:** how a SAS-only title on a G2S cabinet presents in the comm menus | Menu photos per game, §4 |

## 3. Bench-day capture plan (all commands run ON the Pi)

SSH in over Wi-Fi (SSH to the host) or use the tty1 cockpit. Get the
cabinet's MAC first (label, or it'll show in the DHCP journal).

### Phase 0 — preflight (2 min)

```bash
systemctl is-active casinonet-dhcp casinonet-dns casinonet-ntp casinonet-tftp casinonet-g2s
ip -4 addr show eth0                      # expect 192.168.50.2/24
curl -s http://127.0.0.1:8081/api/status | python3 -m json.tool
```

Note: DNS/NTP/TFTP are `SO_BINDTODEVICE`-pinned to eth0 — they will NOT
answer on 127.0.0.1. Unit status + journals are the health check.

### Umbrella capture — one pcap per cabinet per cold boot (run FIRST, leave running)

```bash
mkdir -p ~/CasinoNet/G2S/logs
sudo tcpdump -i eth0 -s0 \
  -w ~/CasinoNet/G2S/logs/wms_<cab#>_<osver>_$(date +%Y%m%d_%H%M%S).pcap \
  'ether host <MAC> or udp port 67 or udp port 68'
```

The `udp port 67 or 68` clause is REQUIRED: the server sends OFFER/ACK/NAK
to `255.255.255.255:68` (src = the Pi's MAC, dst = broadcast), so a bare
`ether host <MAC>` filter captures the machine's DISCOVER/REQUEST but NONE
of what the host offered back — exactly the opt43/opt60/66/67/150 evidence
U2's post-session analysis needs. (The slot net is quiet; capturing all of
eth0 with no filter is also fine.)

This is the permanent artifact (the IGT equivalent saved the project).
The phase filters below are extra terminals for *live watching* only.

### Phase 1 — DHCP (cold-boot the machine)

```bash
# live view
sudo tcpdump -i eth0 -nn -vvv -e 'udp and (port 67 or port 68)'
# server's own analysis (opt55 / opt60 / opt43-requested verdict)
journalctl -u casinonet-dhcp -f
```

Record: exact opt60 string, opt55 code list, `opt43_requested=yes/no`,
which vendor the server *detected* (WMS vs Generic — if Generic, our match
list needs the real string), leased IP.

### Phase 2 — post-lease service discovery

```bash
sudo tcpdump -i eth0 -nn 'host <EGM-IP> and (udp port 53 or udp port 69 or udp port 123)'
journalctl -u casinonet-tftp -u casinonet-dns -u casinonet-ntp -f
```

Record: DNS names queried (if any), **TFTP RRQ filenames** (if any — served
from `tftp-root/`; an unexpected name = create that file next session),
NTP polling.

### Phase 3 — G2S first contact

```bash
# everything TCP the machine tries against the host — catches a non-8081
# default port (a real IGT field sheet's example URI used :65501 — single
# site-config example, not a documented factory default; scan is port-agnostic)
sudo tcpdump -i eth0 -nn 'host <EGM-IP> and tcp[tcpflags] & tcp-syn != 0'
# the conversation itself, ASCII
sudo tcpdump -i eth0 -nn -A 'host <EGM-IP> and tcp port 8081'
# host's view — raw bytes of every POST land here BEFORE parsing
tail -f ~/CasinoNet/G2S/logs/g2s_wire_*.log     # newest file
journalctl -u casinonet-g2s -f
```

Record: destination port SYN'd, POST path, `SOAPAction`, `User-Agent`
(gSOAP version?), Content-Type, wrapping shape (double-wrap escaped inner
vs raw), `egmId` format, `egmLocation` URL+port.

### Phase 4 — join attempt (only if Phase 3 parses cleanly)

From here `BENCH_BRINGUP.md` §3–4 applies verbatim: watch for
`commsOnLineAck` accepted → sync heartbeats → `MACHINE JOINED`, durable =
no RE-HANDSHAKE for 5+ min with keepAlive pulses flowing. Clock rule from
§1 applies.

## 4. Operator-menu photo checklist (per cabinet, per game)

Photograph — don't transcribe — each of these; filenames like
`wms_<cab#>_<osver>_<screen>.jpg`:

1. **Software/OS version screen** — the exact OS build string keys the
   checklist row and the COMPATIBILITY.md entry.
2. **CPU cage stamp** (open the cage door): `A-026352-xx` vs `A-017999-xx`.
3. **Comm/protocol selection screen(s)** — SAS vs G2S choice, per game.
4. **G2S Diagnostics Menu** (Audit/Diagnostics) — its *existence* is data;
   photograph every sub-screen.
5. **Host configuration screen** — URL/IP/port fields, any "use DHCP"
   toggle, factory-default values *before touching them*.
6. Any **certificate/security screen** — we need the cert-less path; record
   the default and available options, change nothing cert-related.
7. **Game title + version** (protocol path is game-dependent).
8. **Clock screen** — then set it equal to / slightly behind the Pi.
9. Any **network status screen** showing the leased IP / received options.

## 5. Per-OS-version checklist (fill one row per cabinet+OS; feeds `COMPATIBILITY.md` → "Bench-test log (AJ's lab)")

Do NOT copy rows into COMPATIBILITY.md until they hold real bench results.

| Cab # | OS ver | CPU part # | Game title | opt60 string | opt43 req? | Lease OK | DNS/TFTP/NTP used | Port SYN'd | SOAPAction / wrapping | commsOnLine seen | Join result | Artifacts (pcap / wire log / photos) |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| | | | | | | | | | | | | |
| | | | | | | | | | | | | |

Suggested day-one scope: 2–3 cabinets, **one per distinct OS version**, not
all 10 — each cold boot cycle is slow and the per-OS deltas are the data.

## 6. Triage — what each outcome means

| Observation | Meaning | Move |
|---|---|---|
| No packets from the MAC at all | Wrong VLAN/cable, machine on static IP, or comm disabled for the game | Check link lights; walk the comm menu; try a SAS-only title's menu for contrast |
| DHCPs, but server logs vendor = Generic/Unknown | Real opt60 doesn't match our list | Capture the string (that IS the finding); extend `vendor_class` list in `dhcp_dns_server_enhanced.py` next session |
| Lease taken, opt43 requested, but no G2S traffic | Machine ignored our opt43 (blob likely malformed — U2) or host not configured machine-side | Try manual host entry `http://192.168.50.2:8081/G2S` in the operator menu; next-session fallback = single-TLV opt43 |
| SYN to a port ≠ 8081 on .2 | WMS default host port differs | Record the port; either point the machine at 8081 in the menu or run a listener there next session |
| POST arrives, host 500s, journal says `no g2sRequest payload` | **WMS wrapping ≠ IGT wrapping — the expected headline finding (U4)** | Nothing to fix live; the raw body is in the wire log. Session goal achieved — bring it home for a parser extension |
| POSTs parse, join stalls in sync (commsDisabled heartbeats forever) | Clock skew (EGM ahead) or a WMS quirk in our ack bytes | Set clock behind the Pi; diff wire log vs the IGT-known-good exchange |
| TFTP RRQ seen | BB2E DOES bootstrap over TFTP (unlike AVP) — big finding | Journal has the filename; if it's not one of ours, that name defines the real config file to build |
| `MACHINE JOINED` prints | The generic ladder + IGT wrapping happened to fit | Run the durable-join criteria from `BENCH_BRINGUP.md` §3; harvest namespaces (U8) |

## 7. After the session

1. Keep keeper pcaps + `g2s_wire_*.log` files with your bench notes
   (naming: `wms_<cab#>_<osver>_<date>...`) — WMS bytes are priceless,
   exactly like the July-2025 IGT capture was.
2. Fill §5 rows, then promote *confirmed* rows into `COMPATIBILITY.md`'s
   bench-test log table.
3. Resolve the `TODO(bench)` markers in `G2S/tftp-root/wms/*` that the
   session answered (U1–U3 especially); delete or rewrite the scaffold files
   the machine proved irrelevant.
4. File the wrapping sample (U4) for the `g2s_host.py` parser extension —
   that's owned by the host-side workstream, not this doc.

---

*Known code issue logged pre-bench (not fixed here — outside this doc's
area): `encode_wms_bluebird_options()` in
`G2S/python/web/dhcp_dns_server_enhanced.py` reuses the `protocol` variable
after overwriting it with `b'G2S_v1.0.3'`, so opt43 sub-options 5 and 10
carry literal `b'G2S_v1.0.3'://…` URLs. Moot if U2 lands on single-TLV, but
fix before trusting the multi-sub-option blob.*
