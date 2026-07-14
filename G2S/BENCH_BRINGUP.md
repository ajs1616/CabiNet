# Bench Bring-Up — IGT AVP "Machine Joins" MVP

Goal: the AVP durably joins — reaches `commsState=G2S_onLine` and stays there,
no re-handshake loop. This was **never achieved** in July 2025 (the old server
violated the G2S two-channel architecture); `g2s_host.py` is the rebuilt,
spec-correct host validated against the AVP's captured bytes by
`tools/avp_replay.py` (must end "0 failed"; the assertion count grows —
218 as of 2026-07-01).

## 1. Server box pre-flight (run from `/home/aj/CasinoNet/G2S/`)

**One command checks everything** (NIC/IP, port squatters, legacy-server traps,
firewall, config sanity) and prints GO/NO-GO with fix commands:

```bash
python3 tools/bench_preflight.py
```

Manual equivalents, if you prefer:

```bash
# slot VLAN NIC up with the server IP the whole stack assumes
sudo ip addr add 192.168.50.2/24 dev eno1
sudo ip link set eno1 up
ip addr show eno1                          # verify 192.168.50.2/24, carrier UP

# nothing squatting on our ports
ss -tlnp | grep 8081 || echo "8081 free"   # old webserver/diagnostic servers
ss -ulnp | grep ':67 ' || echo "dhcp free" # (libvirt's virbr0-only dnsmasq is fine)

# firewall — if ufw is active, open what the choreography needs
sudo ufw status
#   sudo ufw allow from 192.168.50.0/24 to any port 8081 proto tcp   # AVP -> us
#   sudo ufw allow from 192.168.50.0/24 to any port 67  proto udp    # DHCP
#   sudo ufw allow from 192.168.50.0/24 to any port 69  proto udp    # TFTP (optional)
# (outbound to AVP:8080 is allowed by default policy)
```

Start order (3 terminals):

```bash
# T1 — DHCP (needs sudo for UDP 67; pins g2s host 192.168.50.2:8081 itself)
sudo python3 start-dhcp-enhanced.py

# T2 — the G2S host. NO SUDO. --harvest pulls the device inventory after the
# join (feeds COMPATIBILITY.md); keepAlive pulses default ON every 15s so a
# joined machine positively proves the link every cycle (--keepalive 0 to
# disable for a strictly-minimal first attempt).
python3 g2s_host.py --harvest

# T3 — evidence capture (gold for COMPATIBILITY.md and future replays)
sudo tcpdump -i eno1 -w logs/bench_$(date +%Y%m%d_%H%M%S).pcap host 192.168.50.100
```

Live status while running:
- console of T2 (join milestones print loudly)
- `curl -s http://192.168.50.2:8081/api/status | python3 -m json.tool`
- raw bytes: `tail -f logs/g2s_wire_*.log`

## 2. AVP operator menu (per AVP-How-To-Guide §3)

| Setting | Value |
|---|---|
| Certificate Management → Enable Certificate Protocols | **NO** (cert-less is permanent) |
| G2STransportG2S Advanced Options → host URI | `http://` `192.168.50.2` `:8081/G2S` |
| └ Override DHCP Config | YES if typing the URI manually; NO to take it from our DHCP |
| Maximum Hosts Allowed | 1 |
| G2S product ID | DEFAULT |
| Feature Control | leave all NONE for the join MVP (assign G2STRANSPORTG2S later) |
| Clock | **AVP clock must NOT run ahead of the server by more than ~30 s.** Our host requests (setCommsState, setKeepAlive, getDescriptor) carry `timeToLive=30000` evaluated against the AVP's clock — an AVP running fast sees them as already expired and rejects with `G2S_APX011`, stalling the join in sync state with no obvious cause. Equal or a couple minutes *behind* the server is safe (the July capture had it 8 min behind — harmless). |

During the run: **Diagnostic → Device → Comm Analyzer** shows the AVP's own
view of RX/TX — keep it open.

## 3. What a successful join looks like (host console)

```
commsOnLine #1 egmLocation=http://192.168.50.100:8080 flags={deviceReset: true, ...}
EGM acked commsOnLineAck(cid=1)
EGM is in SYNC state (commsDisabled heartbeat) — commsOnLineAck WAS ACCEPTED 🎉
EGM acked commsDisabledAck(cid=2)
setCommsState enable=true sent — expecting commsStatus G2S_onLine back
🎰🎰🎰  [IGT_00012E492815] MACHINE JOINED — commsState=G2S_onLine  🎰🎰🎰
```

With `--harvest` and keepAlive on (the recommended flags) you'll additionally see:

```
EGM confirmed setKeepAlive — expect a keepAlive pulse every 15000ms
📋 DESCRIPTOR HARVEST — N devices across M classes   (the AVP's full inventory)
```

**Durable join criteria (record in COMPATIBILITY.md bench log — first real entry!):**
1. `MACHINE JOINED` printed and `/api/status` shows `"commsState": "onLine"`.
2. **No `RE-HANDSHAKE` warnings for 5+ minutes** after the join (the watchdog
   logs loudly if the loop is still happening).
3. **keepAlive pulses keep arriving** (~every 15 s) — positive proof the link
   is alive, not just silent. The watchdog warns if pulses stop.
4. AVP operator screen shows the host link up / comms enabled.
5. Bonus: the descriptor harvest in the wire log = the AVP's device inventory
   (which classes/devices it exposes) — paste the class list into
   COMPATIBILITY.md.

**If the join stalls in sync (commsDisabled heartbeats but never onLine):**
check the wire log for `G2S_APX011` in the EGM's responses — that's the
clock-skew trap (AVP clock ahead of server; see the Clock row above).

## 4. Failure triage

| Symptom | Meaning | Move |
|---|---|---|
| `RE-HANDSHAKE #N` keeps printing, `outbound fail` > 0 | Our POSTs to the AVP's :8080 endpoint can't connect | Check cabling/VLAN; `curl -v http://192.168.50.100:8080/` from the server box; firewall on the path. Last resort: restart host with `--inline` (July-2025-style sync replies) just to harvest behavior data |
| `RE-HANDSHAKE #N`, `outbound ok` > 0 | AVP receives but rejects our commsOnLineAck | The wire log has our exact bytes — diff against a known-good capture + Comm Analyzer view; this is new knowledge either way |
| Reached SYNC (commsDisabled seen) but never onLine | AVP rejects setCommsState | Run with `--no-auto-enable` to confirm stable sync state; inspect the AVP's g2sAck/commsStatus in the wire log |
| MSX003 warnings after restarting g2s_host.py mid-session | Expected — host forgot the association; the AVP re-handshakes on its own within ~30 s | Wait one cycle |
| Nothing arrives at all | AVP has no IP or wrong host URL | Check T1 DHCP console for the lease; verify operator-menu URI; `tcpdump` shows whether SYNs even arrive |

**Whatever happens, keep `logs/g2s_wire_*.log` and the pcap** — every byte from
the real machine is permanent reference material (this is how the July capture
saved this project).

## 5. After the join

- Fill in the first real row of `COMPATIBILITY.md` → "Bench-test log (AJ's lab)".
- Keep the wire log + pcap with your bench notes (they're small and priceless).
- Next milestones from here: getDescriptor harvest (machine capability inventory),
  keepAlive enablement, then the mediaDisplay "hello world" overlay.

## 6. Test Panel (bench cockpit in a browser)

`g2s_host.py` serves its own web UI — no extra process:

- **URL:** `http://192.168.50.2:8081/` (also `/ui`, `/index.html`; the legacy
  first-cut console is kept at `/console`). Single self-contained page, 2 s
  status poll, reply tape, every `/api/command` action grouped as
  Reads / Subs / Clock / Ownership. Renders fine at 800×480 (touchscreen) and desktop.
- ⚠️ **The `claimOwnership` button DISABLES PLAY** — it runs the commConfig
  ownership cycle (`enterCommConfigMode → setCommChange → authorizeCommChange`).
  Only press it with the machine idle at 0 credits, deliberately, with AJ at the
  bench. The UI two-step-confirms it, but treat it as a bench operation, not a
  status poke. Post-join the AVP acks-then-ignores all application reads/subs
  until this cycle succeeds (`OWNERSHIP APPLIED` in the host log).
