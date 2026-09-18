# CabiNet lite deploy — host on your existing router's LAN

> **The one-sentence version:** your router does DHCP (with one extra setting
> so machines still find the host), the CabiNet host is a plain server on that
> same network — no dedicated switch, no isolated segment, no DHCP/DNS/NTP/TFTP
> services running on the host itself.

Read `deploy/DEPLOY.md` first if you haven't — this document only
covers what's DIFFERENT in lite mode. Running the host on a **Windows PC**?
That is lite mode by definition — this page plus `deploy/WINDOWS_HUB.md`. Full mode (the host owns its own
isolated network) is the proven default; pick lite only when a dedicated
switch genuinely isn't an option.

## 1. What lite is, and when to choose it

In full mode the CabiNet host **is** the network: it hands out addresses, it
answers DNS, it keeps clocks in sync, and it can serve boot files — all on a
segment nothing else touches. In lite mode none of that is true. Your router
(or a small business access point / firewall) already does DHCP for the
building, and the CabiNet host just joins that network like a printer or a
NAS would. The only thing lite mode needs from the router is **one DHCP
setting** (option 43) so slot machines can still find the host automatically.

Choose lite when:
- You don't have a spare switch/hub to dedicate, or don't want a second
  physical network to manage.
- The floor is small/casual (home game room, a handful of machines) and the
  trust level of "everyone on this LAN" matches "everyone who should be able
  to touch the machines."

**Strongly prefer a dedicated VLAN over a flat LAN if your router supports
it.** Most consumer-to-prosumer gear (UniFi, MikroTik, OpenWrt, pfSense and
similar) can create a separate VLAN, run DHCP + option 43 on ONLY that VLAN,
and keep the slot floor off the same broadcast domain as your laptops and
phones. That gets you most of full mode's isolation with almost none of its
physical-switch overhead — it's the middle ground between full mode and a
flat-LAN lite deployment, and it closes most of the risks in section 7 below.
If your gear can do it, do it.

## 2. Host install

1. **Clone the repo** (adjust the path to taste):

   ```bash
   git clone https://github.com/ajs1616/CabiNet.git ~/CabiNet
   ```

   **Do NOT run `deploy/hub_setup.sh`** — that is the full-mode installer:
   it gives a NIC the fixed `192.168.50.2` address and installs the four
   network-bootstrap services, which is exactly what lite mode must not do.
   Lite is the three steps below, by hand.

2. **Give the host a DHCP reservation (a "static lease") on your router.**
   This is mandatory, not optional. Option 43 embeds the host's IP address as
   a literal 4-byte value inside the DHCP offer — it is not a hostname or a
   DNS lookup. If the host's address ever changes, every machine that
   remembers the old one is orphaned: the AVP's persisted host URI, every
   satellite Pi's cached hub address, all of it. Reserve the address once at
   install time (bind it to the host's MAC in your router's DHCP page) and
   never change it.

3. **Install ONLY `cabinet-g2s`** — none of the network-service units. The
   shipped unit carries PLACEHOLDER user/home values (`owner`,
   `/home/owner/CabiNet`); this pulls your real ones from the shell:

   ```bash
   HOST_IP=192.168.1.50    # the address you reserved in step 2

   cd ~/CabiNet
   mkdir -p /tmp/cab-units && cp deploy/cabinet-g2s.service /tmp/cab-units/
   sed -i "s|/home/owner/CabiNet|$HOME/CabiNet|g; s|^User=owner|User=$USER|; \
           s|^Group=owner|Group=$USER|; \
           s|^ExecStart=.*|ExecStart=/usr/bin/python3 -u g2s_host.py --harvest --host-base http://$HOST_IP:8081|" \
     /tmp/cab-units/cabinet-g2s.service
   sudo cp /tmp/cab-units/cabinet-g2s.service /etc/systemd/system/
   sudo systemctl daemon-reload
   sudo systemctl enable --now cabinet-g2s
   ```

   Set `HOST_IP` at the top to the address you reserved in step 2 — this is
   what feeds the on-glass content URLs (mediaDisplay, the glass SPA) and, by
   default, the URL the host tells the AVP to use for its own comm settings.
   `--host-base` is the ONLY thing that changes between full and lite mode.

   **Do NOT install `cabinet-dhcp`, `cabinet-dns`, `cabinet-ntp`, or
   `cabinet-tftp`.** Your router already does DHCP; running CabiNet's own
   DHCP server alongside it would race your router's and put machines on
   conflicting configuration. See section 3 for why DNS/NTP/TFTP aren't
   needed either.

4. **Check it's alive**, from the host itself and from another device on the
   LAN:

   ```bash
   curl -s http://$HOST_IP:8081/api/status | head -5
   ```

   (`$HOST_IP` only exists in the shell where you set it in step 3 — from
   another device, or a new terminal, use the literal address instead.)

   Then open `http://<host-lan-ip>:8081/` from a browser anywhere on the LAN
   — the CabiNet House Floor.

### If the service won't start

- **`status=216/GROUP`** — the unit's `User=`/`Group=` names an account or
  group that doesn't exist on this box (this happens if the sed above ran
  without the `Group=owner` substitution). Check what the installed unit
  actually says:

  ```bash
  systemctl cat cabinet-g2s | grep -E '^(User|Group|WorkingDirectory|ExecStart)='
  ```

  Fix the group and reload:

  ```bash
  sudo sed -i "s|^Group=.*|Group=$USER|" /etc/systemd/system/cabinet-g2s.service
  sudo systemctl daemon-reload
  sudo systemctl restart cabinet-g2s
  ```

- **`status=200/CHDIR`** — the unit's `WorkingDirectory=` doesn't exist, or
  the service user can't enter it. Classic cause: the repo was cloned as
  root into `/root/CabiNet` (mode 0700) while the unit runs as your normal
  login. Move the checkout under that login's home and fix the path:

  ```bash
  sudo mv /root/CabiNet ~/CabiNet && sudo chown -R "$USER:$USER" ~/CabiNet
  sudo sed -i "s|^WorkingDirectory=.*|WorkingDirectory=$HOME/CabiNet/G2S|" /etc/systemd/system/cabinet-g2s.service
  sudo systemctl daemon-reload && sudo systemctl reset-failed cabinet-g2s && sudo systemctl start cabinet-g2s
  ```

- **`Start request repeated too quickly`** — after 5 failed starts systemd's
  restart limiter latches (the unit's `StartLimitBurst=5` is documented in a
  comment in `deploy/cabinet-g2s.service`); this is deliberate and isn't
  itself the bug, it's just hiding the real error underneath. See the real
  error first:

  ```bash
  journalctl -u cabinet-g2s -n 50 --no-pager
  ```

  Then clear the limiter and start again:

  ```bash
  sudo systemctl reset-failed cabinet-g2s
  sudo systemctl start cabinet-g2s
  ```

## 3. Why no TFTP/NTP

- **AVP discovery is DHCP option 43 only.** This is wire-proven against real
  hardware (see `G2S/docs/DHCP_VENDOR_CONFIG.md`): the AVP takes the host's IP
  from option 43 and joins on its own. The TFTP `g2s.xml` file the full-mode
  install serves is informational only — the AVP never fetches it as part of
  discovery, so a lite deployment loses nothing by not serving it.
- **The WMS TFTP path was never proven on real hardware.** The one BB2E ever
  brought up live joined via **manual host-URL entry in the operator menu**,
  not TFTP boot config — see section 5 and `G2S/BENCH_BRINGUP_WMS.md`. Lite
  mode doesn't regress anything here because full mode's TFTP story for WMS
  was already unverified.
- **EGM clocks don't need NTP from the host.** CabiNet disciplines each
  machine's clock in-band, over G2S itself (`cabinet.setDateTime` at join,
  when the machine's own reported time has drifted) — it never depended on
  the host being the machine's NTP server.

**Safety net:** if you ever run into a specific cabinet that insists on a TFTP
boot file, you can install `cabinet-tftp` on its own (it doesn't need the
DHCP/DNS units) and point your router's DHCP option 66 (TFTP server address)
at the host. That's an isolated addition — nothing else about lite mode
changes.

## 4. Router option-43 recipes

Worked example: host reserved at `192.168.1.50`. The IGT option-43 payload is
always the same shape — `01 04` followed by the host's 4 IPv4 bytes in hex:

```
192.168.1.50  ->  01:04:C0:A8:01:32   (bare hex: 0104C0A80132)
```

**UniFi (UniFi Network / controller UI):** Settings → Networks → (your
network) → DHCP → Custom DHCP Option → add code **43**, type **Hex**, value
`0104C0A80132`.

**MikroTik (RouterOS):**
```
/ip dhcp-server option
add name=g2s-host code=43 value=0x0104C0A80132
/ip dhcp-server network
set [find] dhcp-option=g2s-host
```
(attach the named option to the DHCP server's network entry — the second
command, or the equivalent field in Winbox/WebFig.)

**OpenWrt (dnsmasq), scoped to the AVP's vendor class so other clients never
see it:**
```
dhcp-vendorclass=set:igt,IGT
dhcp-option=tag:igt,43,01:04:c0:a8:01:32
```
Add both lines to `/etc/dnsmasq.conf` (or the LuCI equivalent) and restart
dnsmasq.

**pfSense:** Services → DHCP Server → (your interface) → Additional
BOOTP/DHCP Options → number **43**, type **String**, value
`01:04:c0:a8:01:32`.

**Scoping guidance:** where your gear supports matching on the DHCP vendor
class (option 60), scope option 43 to vendor class `IGT` — that's the AVP's
own vendor class string, so only IGT machines receive it. A global
(unscoped) option 43 is harmless but noisy: every other DHCP client on the
LAN receives an opaque option 43 it doesn't recognize and ignores. Prefer
scoping when you can; don't lose sleep over it when you can't.

**AVP-side checklist** (same as full mode): Override DHCP Configured Host =
**NO**. The port and path (`:8081/G2S`) come from the AVP's own persisted URI
segments, not from DHCP — option 43 only ever carries the bare host IP.

**Warning — the single-TLV format is exact.** `01 04 <4 bytes>` and nothing
else. If your router's UI lets you add multiple DHCP option 43 sub-options
(some enterprise gear presents it that way), do NOT add extras for
port/path/URL/auto-discovery — the AVP's parser treats any additional
sub-option as malformed and discards the WHOLE option, falling back to its
`127.0.0.1` unset default. Full details and the wire-proof are in
`G2S/docs/DHCP_VENDOR_CONFIG.md`.

## 5. WMS machines

WMS BB2E (and most non-IGT G2S cabinets) don't have a proven option-43 URL
format on real hardware. Set the host's URL by hand in the machine's operator
menu instead: `http://<host-lan-ip>:8081/G2S`. This is identical to the
full-mode fallback path — see `G2S/BENCH_BRINGUP_WMS.md` for the menu walk.

## 6. Satellites (SAS SMIBs, Companion RFID readers)

Full mode's satellite Pis self-discover the hub by assuming "the DHCP default
gateway is the hub" — true when the hub IS the gateway (full mode), false on
a router-served LAN (lite mode: the gateway is the router). So in lite mode
the host's address is always passed explicitly — see the commands at the end
of this section. Two things first.

**These setup scripts install onto another machine — not the one you're
typing on.** They run FROM a machine that already has this checkout (usually
the G2S host machine, or your dev box) and push the install onto the
satellite Pi over the network; `<user>@<satellite-host>` below is the TARGET
satellite Pi, not wherever you're sitting. If you're already logged into the
Pi that will carry the reader, you ARE the target — there's no second machine
to point at, and running the script there won't work anyway: it copies the
`Companion/` (or `SAS/`) tree out of this checkout, and a freshly imaged
satellite Pi has no checkout, so there is nothing to copy from. Log out, go
back to a machine that already has this checkout (the G2S host machine is
usually the easiest choice — it's already set up), and run the commands
below from there, using this Pi's own LAN address as the target.

**Prerequisite, once per satellite Pi:** passwordless sudo and your public
key installed. This is REQUIRED, not a convenience — both setup scripts run
entirely non-interactively and cannot ever prompt for a password, so being
able to `ssh` in with a password today does not mean you're ready; the key
step is not skippable. On the Pi:

```bash
echo "$USER ALL=(ALL) NOPASSWD:ALL" | sudo tee /etc/sudoers.d/010-$USER-nopasswd
```

From the dev box:

```bash
ssh-copy-id <user>@<satellite-host>
```

Not sure if you already have this covered?
`ssh -o BatchMode=yes <user>@<satellite-host> true` — silent success means
you're already set up and can leave `-i` off the commands below entirely.
Full fresh-image walkthrough: `deploy/SMIB_FRESH_IMAGE.md`, section 2.

**Then install, always with the host's address explicit** (SAS SMIB first
line, RFID-only Companion second):

```bash
deploy/smib_setup.sh      <user>@<satellite-host> --hub http://<host-lan-ip>:8081
deploy/companion_setup.sh <user>@<satellite-host> --hub http://<host-lan-ip>:8081
```

`<user>` is whatever login already exists on that Pi — it does not have to
match the account the G2S host runs under; the `--hub` URL is the only link
between host and satellite.

For a SMIB player screen (the HDMI panel on a SAS SMIB Pi, `SMIB_FRESH_IMAGE.md`
§3c), edit the `SMIB_URL` line in `cabinet-smibui.service` directly — it
hardcodes the full-mode hub address `192.168.50.2` — and restart the unit; it
isn't driven by a setup-script flag.

If a satellite is ever started WITHOUT `--hub` on a lite LAN, it still tries
the old zero-config guess (the default gateway), notices the guess doesn't
answer like a real CabiNet host, and prints a loud warning in its journal
telling you to pass `--hub http://<host-ip>:8081`. It will NOT silently send
anything to whatever happens to be sitting at your router's address — ALL of
its hub-bound traffic (its periodic status reports AND, on the SAS side, its
ticket calls) is held back until that same check passes, and it just keeps
retrying quietly until you fix the flag.

## 7. What lite does NOT change, and what it costs you

Unchanged: the web UI, the G2S/SAS/Companion protocols, money handling,
ticket/voucher logic, everything about how the floor actually runs. Lite mode
only changes how machines find the host and who else is on the same network.

**Disadvantages and risks — read before choosing lite:**

- **The host's APIs are unauthenticated on a shared LAN.** Anyone who can
  reach the host's IP on your network can drive the same APIs the machines
  and satellites use — including money-adjacent ones (tickets, wallet
  transfers). Full mode's isolated segment is what keeps that surface away
  from anyone but the floor itself. Lite mode is supported ONLY on a LAN you
  actually trust; a dedicated VLAN (section 1) is strongly recommended over a
  flat shared LAN.
- **No protection against a rogue DHCP server.** On a flat LAN, anything else
  offering DHCP (a second router, a misconfigured device, someone's phone in
  hotspot mode) can race your real router and hand a machine bad
  configuration. Full mode's dumb-switch segment structurally can't have this
  problem; lite mode can.
- **Router configuration is now load-bearing.** The option-43 setting from
  section 4 is part of your floor's infrastructure, not a one-time favor. If
  your router gets factory-reset or replaced, machine discovery breaks until
  you redo it.
- **A stable host IP is mandatory**, same reasoning as the DHCP reservation
  in section 2 — plan for it, don't treat it as optional.
- **Satellite onboarding is no longer zero-config.** Full mode's "plug in a
  flashed Pi, it just finds the hub" story requires the hub-is-gateway
  assumption; lite mode needs an explicit `--hub` on every satellite (section
  6). Slightly more setup work per Pi, in exchange for never guessing wrong.
- **WMS machines still need manual per-machine setup** (section 5) — lite
  mode doesn't make this better or worse than full mode.

## 8. Future work

Not built yet, tracked as future improvements:
- A `host_setup.sh --mode full|lite` installer that does the branching this
  document currently asks you to do by hand.
- Optional beacon/mDNS-based satellite discovery for lite mode, so
  `--hub` could eventually become optional again on a lite LAN — it would
  slot into the same "guess, then prove the guess before trusting it"
  structure the identity check in section 6 already uses, just with a
  different way of producing the initial guess.
