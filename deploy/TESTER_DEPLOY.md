# CabiNet tester deploy — host + companions on their own hub

> **The one-sentence version:** put the CabiNet host and your slot machines on
> a **basic unmanaged Ethernet hub/switch of their own** — no router, no other
> DHCP — because the host IS the network stack (DHCP + DNS + NTP + TFTP + G2S).
> Everything is **wired**. There is no Wi-Fi in this deployment, on purpose.

## Why a dumb hub, not a router

The CabiNet host hands every machine its IP address **and** its G2S host URL
(DHCP option 43 — that's how an IGT AVP finds the host with zero machine-side
config). A router's own DHCP server would race ours and machines would join the
wrong network. An unmanaged switch/hub has no opinions — plug things in and the
host runs the whole segment:

```
   slot machine ─┐
   slot machine ─┤                        ┌─ eth0/enpXsY  = the SLOT segment
   companion Pi ─┼── unmanaged switch ────┤   static 192.168.50.2/24
   companion Pi ─┘                        │
                                          └─ (optional) 2nd NIC / your home LAN,
                             CabiNet host      for browsing the UI from a couch
```

- **The slot segment is isolated.** Nothing on it but the host, the machines,
  and the companion Pis. Never bridge it into your home LAN.
- **Wired only.** Wi-Fi legs were nothing but debug pain in bring-up (flaky
  transfers, stalled command channels) — v1 is Ethernet, full stop.
- A second NIC (or the host's Wi-Fi toward your HOME network) is fine for
  reaching the web UI from elsewhere in the house; the slot segment doesn't
  route through it.

## Host hardware — any Linux box

The reference host is a Raspberry Pi 5, but the stack is **stdlib-only Python 3
(3.11+)** with no pip dependencies and no Pi-specific code — any Linux machine
with a spare Ethernet port works (old laptop, mini-PC, NUC, another Pi).

Requirements:
- Linux with systemd, Python 3.11+
- One dedicated Ethernet NIC for the slot segment
- The services bind privileged ports (DHCP 67, DNS 53, NTP 123, TFTP 69), so
  they run as root via systemd (the G2S host itself runs unprivileged on 8081)

## Host install

1. **Clone the repo** (adjust the path to taste — the units below assume
   `/home/<you>/CasinoNet`):

   ```bash
   git clone <the CabiNet repo> ~/CasinoNet
   ```

2. **Give the slot NIC a static IP — it must be `192.168.50.2/24`.** This
   address is baked into the machine-facing configs (DHCP option 43 payload,
   TFTP bootstrap files, the on-glass content URLs). Don't get creative here;
   standardizing it is what makes the rest zero-config. With NetworkManager:

   ```bash
   nmcli con add type ethernet ifname <slotNIC> con-name cabinet-slot \
     ipv4.method manual ipv4.addresses 192.168.50.2/24 ipv6.method disabled
   nmcli con up cabinet-slot
   ```

3. **Install the systemd units** from `deploy/`:

   ```bash
   cd ~/CasinoNet
   # If your user/path/NIC differ from the units' defaults (user aj,
   # /home/aj/CasinoNet, eth0), fix them in one pass:
   mkdir -p /tmp/cab-units && cp deploy/casinonet-{g2s,dhcp,dns,ntp,tftp}.service /tmp/cab-units/
   sed -i "s|/home/aj/CasinoNet|$HOME/CasinoNet|g; s|User=aj|User=$USER|g; s|--interface eth0|--interface <slotNIC>|g" /tmp/cab-units/*.service
   sudo cp /tmp/cab-units/*.service /etc/systemd/system/
   sudo systemctl daemon-reload
   sudo systemctl enable --now casinonet-dhcp casinonet-g2s casinonet-dns casinonet-ntp casinonet-tftp
   ```

   (`casinonet-kiosk` / `casinonet-console` are Pi-5-DSI-touchscreen extras —
   skip them on a generic box; the web UI is the same thing in any browser.)

4. **Check it's alive:**

   ```bash
   systemctl is-active casinonet-dhcp casinonet-g2s
   curl -s http://192.168.50.2:8081/api/status | head -5
   ```

   Then open **http://192.168.50.2:8081/** — the CabiNet House Floor.

## Slot machines

First question: **is your machine G2S or SAS?** `COMPATIBILITY.md` has the
per-vendor cutoffs, the stickers to look for, and the operator-menu screens
that prove G2S is actually present. If the machine doesn't expose a G2S
menu, it's SAS — and SAS is the *normal* case for most hobbyist machines.

> ⚠️ **Read this before touching the operator menu.** On many machines the
> comms/validation fields are **one-shot: once set, they lock until a RAM
> clear** — and a RAM clear wipes your machine's books. Have the right
> values in hand *before* you start. They're all below.

### G2S machines (any brand)

Plug the machine's Ethernet into the slot switch.

- **IGT (AVP Family 14 etc.) — plug-and-play:** Certificate Protocols
  **NO** (cert-less is the only supported path), "Override DHCP Configured
  Host" **NO**. The machine takes the host from DHCP option 43 and joins on
  its own — nothing to type. After changing any comm settings, **re-enable
  G2S in the debug menu** or the machine's endpoint stays dark. For the
  on-glass UI: enable the mediaDisplay content areas in the operator menu
  and give them memory from the media pool (it's RAM-capped — enable the
  ones that fit).
- **Every other brand (or any machine with manual host entry):** point the
  machine's G2S host/server URL at

  ```
  http://192.168.50.2:8081/G2S
  ```

  and if the machine offers a **G2S flavor / dialect selector, choose IGT**
  — the base G2S classes are standard and the IGT flavor is the one CabiNet
  speaks (live-proven on a WMS BB2E this way). Heads-up: some machines only
  open the comms config window at specific moments — on a BB2E it's the
  **post-RAM-clear boot** — so plan the settings before you're standing in
  that window.

### SAS machines (the SMIB path)

Wire a SAS SMIB Pi (golden image or `deploy/zero2w_sas_setup.sh`) between
the machine's SAS port and the switch. Then set these in the operator menu
— **these are the one-shot fields**:

| Field | Set it to | Why |
|---|---|---|
| Validation mode | **Secure Enhanced** if offered; otherwise **System** | Enhanced is the primary path (machine self-mints ticket numbers, host records them). No Enhanced? System mode still ties into the hub — the host answers the machine's cash-out requests in real time. Machine-only/"Standard" validation is the last resort: tickets will print but won't be in the hub ledger. |
| SAS address | **1** | The SMIB polls address 1 by default (`--address` changes it if you must). |
| SAS channel / port | **Enabled**, **19200** baud | SAS standard rate. |
| AFT / cashless transfers | **Enabled** (if offered) | This is how credits move between the wallet and the machine. |
| Legacy bonusing | **Enabled** (if present) | On pre-AFT machines this is the credit-push path; harmless to have on otherwise. |

Host-side: in the web UI **Settings**, leave **System-validation fallback ON**
(it ships on) — that's what answers System-mode cash-outs.

**After any RAM clear:** the machine silently **disables in-house AFT** —
re-enable it in the operator menu (validation re-seeds automatically; just
re-check the validation-mode field survived).

### Both kinds

Machines appear on the floor as they join: **Connecting…** (amber) while the
handshake runs, **LIVE** once joined. Registered machines never disappear —
a powered-off cabinet just shows dark.

## Companion Pis (RFID readers / SAS SMIBs)

Flash the golden image (provided out-of-band with your tester invite — it's
just Raspberry Pi OS Lite with the setup script below already run; you can
equally build your own from a fresh card via `deploy/SMIB_FRESH_IMAGE.md`),
plug the Pi into the same switch, power it from the cabinet's USB. That's
the whole install:

- The Pi self-identifies by its hardware serial and finds the hub via its DHCP
  default gateway (the host) — **no per-device config, no flags**.
- It appears in the UI as an unassigned reader; assign it to a machine from
  that machine's ⚙️ Options. A reader riding a SAS SMIB auto-binds to that
  SMIB's machine.

## When something breaks — send me a support bundle

One command gathers everything I need (service journals, CabiNet logs, state
snapshots, network + system info) into a single `.tar.gz` — read-only, works
even when the services are down:

```sh
# on the host box:
python3 deploy/support_bundle.py

# on a satellite Pi (SAS SMIB / reader), same script:
python3 ~/CasinoNet/deploy/support_bundle.py
```

It prints the file it wrote — attach that to your report along with **what
went wrong and roughly when** (clock time matters; the journals are
timestamped). Run it with `sudo` if it says it couldn't read the unit
journals. If the problem is at one machine, send bundles from **both** the
host and that machine's Pi.

The bundle contains your floor's data (machine ids, player names, fun-money
balances, protocol traffic) — send it to me directly, don't post it publicly.

## Ground rules (the things that break it)

1. **No other DHCP server on the slot segment.** Ever. That's why it's a dumb
   switch and not a router.
2. **The host is always 192.168.50.2.** The segment is always 192.168.50.0/24.
3. **Wired only.** Don't try to Wi-Fi a companion or a machine to the segment.
4. **One host per segment.** Machines are configured for exactly one G2S host.
5. Money data lives in the host's `G2S/data/` — back it up if you care about
   your game room's wallets, and never point test tools at it.
