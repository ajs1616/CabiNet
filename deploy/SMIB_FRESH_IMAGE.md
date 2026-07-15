# SMIB from a fresh SD card — the runbook

Turn a blank Raspberry Pi into a CabiNet SMIB (satellite by a slot machine).
Two SMIB flavors, same image, same scripts:

- **SAS SMIB** — serial-wired to a SAS machine (WMS Bluebird, etc.). Needs a PL011
  UART + an RS-232 level shifter. `deploy/zero2w_sas_setup.sh`.
- **Companion** — RFID-only, no serial (the slot's own glass is the UI, e.g. the IGT
  AVP). Needs a PN532 reader. `Companion/` + `deploy/casinonet-companion.service`.

One Pi can be **both** at once (proven on the BB2 Zero 2026-07-10: SAS + PN532 together).

---

## 0. Which board?

Any Pi-family board runs the workload (it's stdlib pollers + a 1 s report loop). The
ONE hard rule is **SAS needs a PL011 UART**, which is a Pi-family feature our
`sas_serial.py` 9-bit wakeup depends on — so keep SAS duty on a Raspberry Pi. RFID-only
Companions are I2C/SPI and less fussy, but staying in-family keeps the fleet on one
image.

| Board | SAS? | Notes |
|-------|------|-------|
| **Pi 3B / 3B+** | ✅ | **RECOMMENDED** — cheapest used option **+ built-in Ethernet** (the floor is wired-only, so this skips the HAT entirely) |
| **Pi Zero 2 W** | ✅ | the original reference SMIB (`disable-bt` frees ttyAMA0) — but it has **no Ethernet**, so it needs a USB-Ethernet HAT on the wired floor |
| **Pi 3A+** | ✅ | compact and cheap, same SoC family as the 3B+ — but like the Zero it has no Ethernet |
| **Pi 4B (1–2 GB)** | ✅✅ | native Ethernet + SPARE PL011s (`dtoverlay=uart3` → keep Bluetooth); also the Pro-tier touchscreen board |
| **Pi Zero W (v1)** | ⚠️ | single-core ARMv6, untested, slow to admin — last resort |
| **Pi 5** | ✅ (hub) | UARTs on the RP1; it's the hub already — wasteful as a bare SMIB |
| Orange Pi / Radxa etc. | ❌ SAS | fine for RFID-only, but non-PL011 UART = SAS 9-bit needs new driver work |

The setup script auto-detects the board and prints a note for anything outside the
proven Zero-2-W / 3 / 4 set.

---

## 1. Flash the card (Raspberry Pi Imager)

- **OS:** Raspberry Pi OS **Lite (64-bit)**, Bookworm. No desktop.
- **⚙️ Edit Settings BEFORE writing** (the gear / Ctrl-Shift-X):
  - Hostname: pick a stable one — `smib-bb2`, `smib-avp`, … (it's the SSH target).
  - Username: your pick (a stable one you'll reuse fleet-wide).
  - **Wi-Fi: leave it OFF** — SMIBs are wired-only (Wi-Fi satellites are retired;
    set it only temporarily if you need headless first-boot SSH before the wire).
  - Locale/timezone.
  - Services → **Enable SSH** → *password authentication* (the setup script installs
    your key afterward).
- Write, boot the Pi wired to the slot-segment switch, wait ~60 s for first boot.
  Confirm: `ping <hostname>.local`.

> **Wired only** (the bench lesson of 2026-07-13 — Wi-Fi flakiness dropped report
> legs; the Wi-Fi satellite path is retired). A board with onboard Ethernet (3B+/4B)
> just plugs in; a **Zero 2 W uses an Ethernet/USB HAT** (the HAT rides the USB
> test-points, leaving the I2C GPIOs free for the PN532). Plug it into the
> **slot-segment switch** and the host's DHCP puts it on `192.168.50.x`, reaching the
> host at `--hub http://192.168.50.2:8081` (both setup scripts' default).

---

## 2. One-time on the Pi: passwordless sudo + your key

The setup script drives the Pi over SSH and needs passwordless sudo. Once, on the Pi
(or via `ssh <host>`):

```bash
echo "$USER ALL=(ALL) NOPASSWD:ALL" | sudo tee /etc/sudoers.d/010-$USER-nopasswd
```

Install your key from the dev box so later runs are non-interactive:

```bash
ssh-copy-id <user>@<host>.local
```

(Use one dedicated key for the whole fleet — agent-free with `-i`.)

---

## 3a. SAS SMIB — run the setup script

From the **dev box**, in the repo root:

```bash
deploy/zero2w_sas_setup.sh aj@<host>.local --address 1
```

It is idempotent and does, over SSH:

1. **UART** — `dtoverlay=disable-bt` + `enable_uart=1` (PL011 → `/dev/ttyAMA0`),
   disables `hciuart`, strips the serial console, masks the serial getty, adds `aj` to
   `dialout`. (Board auto-detected; Pi 4 keeping BT: add `dtoverlay=uart3` yourself and
   pass `--port /dev/ttyAMA1`.)
2. **venv** `~/venvs/casinonet` with pyserial / crcmod / loguru / pytest.
3. **rsync** the SAS tree (excludes `/data` — the ticket ledger never travels, GR-04).
4. Installs + enables **`casinonet-sas.service`** (`--port` / `--address` from args;
   `--hub` defaults to the wired host `http://192.168.50.2:8081` — pass `--hub` to
   override for a co-located `127.0.0.1`).
5. **Reboots** (config.txt changed) and verifies `/dev/ttyAMA0` + the service.

**Wire the RS-232 level shifter** to the machine's SAS port: TX→RX, RX→TX, GND↔GND.
(Any "MAX3232 RS232 to TTL" converter board works — ~$5; wire its TTL side to the
Pi's GPIO14/15 + 3.3V/GND, the DB9/screw-terminal side to the SAS harness.)
Double-check pinouts before powering anything — it's your machine, your risk.
Set the SAS **address** (default 1) and **enable the SAS channel** in the machine's
operator menu.

**First contact** (the service holds the port, so stop it first):

```bash
sudo systemctl stop casinonet-sas
~/venvs/casinonet/bin/python tools/sas_bench_poll.py /dev/ttyAMA0 --credits
sudo systemctl start casinonet-sas
journalctl -u casinonet-sas -f
```

A **CRC-BAD** answer is good news — the machine is alive and framing; capture it for
`COMPATIBILITY.md`. (CRC is Kermit, live-settled — a bad CRC now points at wiring or the
machine's own SAS version, not our code.)

---

## 3b. Companion (RFID) — add a reader

For a slot that uses its OWN glass for the UI (the AVP) or to add tap-to-identify to any
cabinet. Runs standalone OR alongside `casinonet-sas` on the same Pi. Stdlib-only — no
venv.

### Zero-config (v11) — the collector path: flash → boot → appears → assign

The golden image ships the Companion unit **flag-free**, so the *same image bytes* boot on
every card and self-configure — no per-device file to edit:

1. **Flash** the golden image to any SD card (identical for every reader).
2. **Boot** it on the slot-VLAN switch. The daemon IDs itself by the **Pi's hardware
   serial** (`companion-<serial-tail>`) and finds the hub at the **DHCP default gateway**
   (the host is the gateway + DHCP server on the slot segment — no host IP
   to configure).
3. **It appears** in the hub UI under **The Players ▸ Readers** as `New Companion
   (<serial-tail>)`, `unassigned`.
4. **Assign it**: click **Assign ▾** on its row, give it a friendly name, and pick the
   machine it *serves*. The hub owns the binding from then on — routing is live, **no
   restart**, and re-assigning to a different machine takes effect on the next tap.

That's it. Wire the PN532 (below), tap a fob, done. `hostname` at flash time no longer
matters for identity — use anything (or nothing); the reader is found by serial + named in
the UI. (Reaching a *specific* card by SSH: its source IP shows next to its id on the
Readers row, since identical images share a hostname.)

### Manual override (co-located / advanced) — bake a binding into the unit

`deploy/companion_setup.sh` still installs onto an existing Pi (enables I2C, rsyncs
`Companion/`, installs + enables the unit, reboots only if config.txt changed). With **no
flags** it installs the zero-config flagless unit; pass flags only to bake a fixed binding:

```bash
# Zero-config (normal): flagless unit, assign the machine in the UI
deploy/companion_setup.sh <user>@<zero-host>

# Manual override: bake the binding (e.g. co-located, or a fixed egm)
deploy/companion_setup.sh <user>@<zero-host>.local \
    --g2s-egm IGT_<egmId> --companion-id companion-avp
```

- `--hub` (override only): omit for zero-config (derive the gateway). Pass
  `http://127.0.0.1:8081` co-located.
- **I2C layout** (auto-configured): the **universal software i2c-gpio bus** on
  `GPIO23/24` → `/dev/i2c-11` — ONE wiring for the whole fleet. Every Companion (SAS+RFID
  like the BB2, or RFID-only like the AVP) wires the PN532 to the **same** pins
  (`Companion/README.md`); the software bus works whether or not a SAS HAT occupies the
  hardware I2C pins, so there is no per-board wiring to remember. `--hw-i2c` opts a board
  into the hardware bus (`GPIO2/3` → `/dev/i2c-1`) instead — rarely needed.
- **Bindings** (override only — normally assigned in the UI): `--g2s-egm <egmId>` makes
  taps G2S `setIdValidation` carded sessions on that machine; `--sas-smib <id>
  --sas-address <n>` routes reset-tier fobs to a SAS handpay reset. Zero-config leaves
  both to the hub UI (Players ▸ Readers ▸ Assign → machine + optional reset-fob leg).

Then set the PN532 DIP switches to I2C (answers at `0x24`), wire it, tap a fob → watch
the HUB journal (`journalctl -u casinonet-g2s -f` on the hub) for `💳 card IN`. A board
that isn't wired yet is fine — the daemon reports `readerOk=false` and retries; the unit
stays active. First-ever tap on our hardware read UID `6CB16F06` (an S50 1K).
`companion_host.py http://127.0.0.1:8081 --mock` is the no-hardware smoke test.

Everything smart (fob→tier lookup, carded sessions, resets) lives in the hub; the
Companion is a dumb tap→POST satellite by design.

---

## 3c. SMIB player screen — the player glass on an HDMI panel

For a SMIB whose cabinet **can't be host-skinned on its own glass** — the WMS BB2E
(Flash-7/CGC, CLOSED 07-10) is the reference case — the full player-glass UI
(attract "TAP YOUR FOB" → carded player screen: wallet, CASH OUT, LOG OUT; admin
adds MACHINE / TICKETS / HANDPAY / GAMES / SETTINGS) runs on the SMIB's **own**
1024×600 landscape HDMI panel. The page (`G2S/webui/smib.html`) is a thin client
of the same `/api/glass/*` hub endpoints the IGT glass uses (it polls `src=smib`,
so it never touches the AVP-only mediaDisplay sequencer) — no new hub logic. This
step parks a browser on the panel; it rides **beside** `casinonet-sas` +
`casinonet-companion` and must never starve them.

**Prereqs:** the SAS SMIB (§3a) and/or Companion (§3b) already live on this Zero; a
1024×600 HDMI panel is connected (`fb0` = `vc4drmfb 1024,600`, HDMI-A-1 connected).
Today the panel shows the Linux login prompt — this replaces it with the attract.

**Engine — `cog` (WPE WebKit), DRM/KMS, no X / no Wayland / no compositor.** Proven
on the real BB2 Zero 2026-07-12: `cog` 0.18.4 / `libwpewebkit-2.0-1` 2.48.1 renders
straight to the vc4 KMS CRTC via GBM/EGL — the lightest option (~68–75MB cog RSS,
~87MB whole tree). One line pulls the whole closure:

```bash
sudo apt-get install -y cog
```

(armhf / trixie build; transitively pulls `libwpewebkit-2.0-1`,
`libwpebackend-fdo-1.0-1`, `libwpe-1.0-1`, `libinput10`, `libegl1`, `libgbm1`,
`libepoxy0`, `libdrm2`, `libsoup-3.0-0`, …). **Do NOT install
seatd/libseat/cage/chromium** — cog-drm needs none of them.

**Install the unit** (`deploy/casinonet-smibui.service`):

1. Edit `Environment=SMIB_URL=` for THIS cabinet — the hub URL + the machine's
   egmId in the `?egm=` param, colons `%`-encoded (`%3A`) and each `%` **doubled**
   to `%%` (systemd specifier escaping — a single `%3A` is dropped as an "Invalid
   slot"; the shipped file already carries the BB2's
   `WMS_00%%3Aa0%%3Aa5%%3A79%%3A2d%%3Aa8` as an example — swap in YOUR machine's id).
   Moving a SMIB to another cabinet is this one line.
2. Copy + enable:
   ```bash
   sudo cp ~/CasinoNet/deploy/casinonet-smibui.service /etc/systemd/system/
   sudo systemctl enable --now casinonet-smibui
   ```
3. Verify — the panel shows the attract, titled with the collector's GAMEROOM NAME
   (the hub substitutes it at serve time; neutral fallback "GAME ROOM"), and from
   the SMIB Pi itself:
   ```bash
   systemctl is-active casinonet-smibui                                     # active
   curl -s -o /dev/null -w '%{http_code}\n' \
     "http://192.168.50.2:8081/webui/smib.html?egm=<egmId>"                # 200
   ```

**Cursor / blanking — nothing to do.** cog-drm draws no pointer (there is no
libinput pointer device; a USB touch lead won't add one), and while it owns the
CRTC it scans out continuously → no DPMS/blank. `consoleblank=0` is already the
kernel default. No `xset` / `setterm` / `unclutter` needed.

**Memory safety — the KIOSK dies before SAS, never the reverse.** Two layers:

- *Works today (no reboot):* the unit ships `OOMScoreAdjust=1000`, making the kiosk
  the kernel OOM-killer's first victim. `oom_score_adj` is honored **independent of
  cgroups**. Protect the money-critical pair with a matching drop-in — it takes
  effect at the next start, so batch it with the cgroup reboot below rather than
  restarting `casinonet-sas` / `-companion` mid-session:
  ```bash
  for u in casinonet-sas casinonet-companion; do
    sudo mkdir -p /etc/systemd/system/$u.service.d
    printf '[Service]\nOOMScoreAdjust=-800\n' | \
      sudo tee /etc/systemd/system/$u.service.d/oom.conf >/dev/null
  done
  sudo systemctl daemon-reload
  ```
- *Enforces the cap (needs a reboot; the machine re-dials on its own):*
  the `MemoryMax=220M` / `MemoryHigh=200M` in the unit are **silent no-ops** until
  the memory cgroup controller is on — the Pi firmware default `/proc/cmdline`
  carries `cgroup_disable=memory` (it is NOT in `cmdline.txt`). Append to the single
  line in `/boot/firmware/cmdline.txt`: `cgroup_enable=memory cgroup_memory=1`, then
  `sudo reboot`. After reboot the 220M cap is real (and the `oom.conf` drop-ins take
  effect). Do this one reboot; skipping it just leaves the cap inert, not unsafe —
  `OOMScoreAdjust` already protects SAS.

**SAS-health soak (hard gate) — re-verify with the kiosk live ≥3 min.** Baseline
2026-07-12 (14 samples over 209s, cog live): satellite `polls` monotonic ~4.3/s (no
counter reset ⇒ no SAS restart), hub `reportAgeSec` 0–1s throughout, `online=true`
`stale=false`, `MemAvailable` 221–250MB (>60MB gate — 3–4×), cog RSS flat over
3.5min (no leak), zero `casinonet-sas` / `-companion` restarts. Read the numbers off
the hub floor registry + `free -m` and `journalctl -u casinonet-sas` on the Zero.

**Redeploy:** edit the page and `sudo systemctl restart casinonet-smibui` (cog
re-reads the URL fresh). The page deliberately **ignores `uiBuild`** — that mtime is
the AVP's `glass.html`, not this file — so redeploys restart the unit; the page
never self-reloads off another file's mtime.

**Rollback:** `sudo systemctl disable --now casinonet-smibui` → `getty@tty1` returns
and the panel shows the login prompt again; `casinonet-sas` + `casinonet-companion`
are untouched throughout. Full removal: `sudo apt-get remove --purge cog`, then
delete the two `oom.conf` drop-ins + `sudo systemctl daemon-reload`.

**Gotchas.** DRM master needs an **active VT** (there is no logind seat here) — the
unit's `Conflicts=getty@tty1` + `TTYPath=/dev/tty1` + `ExecStartPre=chvt 1` handle
it; do not add seatd. Harmless journal noise (not errors): `Renderer 'modeset' does
not support rotation 0 (0 degrees).` and `WPEWebProcess: Can't connect to a11y bus`.
Fallback engine (NOT needed — cog wins decisively): `chromium --kiosk
--ozone-platform=drm <URL>` is ~3× heavier (~250MB) and makes the cgroup cap fix
urgent on the 424Mi board.

---

## 4. Register it on the hub

- The SAS SMIB auto-appears as a floor tile once it reports (the hub's `/api/sas/report`
  registry) — name it and, for a dual-protocol cabinet, link its SAS leg, in the tile's
  Options.
- A **Companion** auto-appears under **The Players ▸ Readers** the moment it reports
  (zero-config: serial id + gateway hub, no flags). Click **Assign ▾** on its row to name
  it and bind the machine it serves — the hub owns the binding from then on (live, no
  restart). This replaces baking `--g2s-egm`/`--sas-*` into the unit.
- Register fobs and link them to players in the Home UI → **The Players ▸ Cards &
  readers** (or the Wallet tab's Players card). An unknown fob tap shows a register-me
  prompt.
- **Dual-protocol cabinet** (BB2 on SAS *and* G2S): link its G2S card to its SAS leg in
  the machine's Settings so meters/credits count once — task #20, `sasLink`.

---

## 5. Networking cheat-sheet

- **Host**: static **192.168.50.2** on the slot NIC = the slot segment.
  Serves DHCP + G2S :8081 on the slot net (see `deploy/DEPLOY.md`).
- **Wired satellite (the ONLY supported path):** plug the Pi into the
  slot-segment switch (a 3B+ goes straight in; a Zero rides its USB-Ethernet
  HAT). The host serves DHCP there, so it comes up on
  **192.168.50.0/24** and reaches the host as one local hop. Zero-config finds the
  host automatically — it IS the default gateway — so no `--hub` is needed
  (SAS `--hub auto`, Companion flagless).
  **Wi-Fi satellites are retired** — the AP proved flaky on the bench (2026-07-13:
  it dropped/delayed the PULL-only report leg → intermittent wallet 400s + stalled
  reset-fob handpay clears). Everything goes on the wire.
- **Co-located satellite** (a SAS host running on the hub Pi itself): `--hub
  http://127.0.0.1:8081`.
- **SSH gotcha:** use the explicit key form in scripts —
  `ssh -o BatchMode=yes aj@<host>` — the bare alias needs an
  ssh-agent that vanishes when you re-SSH into the dev box.

---

## Quick reference

| Thing | Value |
|-------|-------|
| OS | Raspberry Pi OS Lite 64-bit (Bookworm) |
| User | `aj` |
| Key | `~/.ssh/casinonet` |
| venv | `~/venvs/casinonet` |
| SAS port | `/dev/ttyAMA0` (Pi 4 spare PL011: `/dev/ttyAMA1` via `dtoverlay=uart3`) |
| SAS unit | `casinonet-sas.service` |
| Companion unit | `casinonet-companion.service` |
| Player-screen unit | `casinonet-smibui.service` (HDMI-panel SMIBs, e.g. the BB2) |
| Kiosk engine | `cog` (WPE WebKit, DRM/KMS — no X/Wayland); `apt-get install -y cog` |
| Hub URL | **`http://192.168.50.2:8081` (wired slot segment — DEFAULT)** / `http://127.0.0.1:8081` (co-located) |
| Companion (zero-config) | `deploy/companion_setup.sh <user>@<zero-host>` → boot → assign in **Players ▸ Readers ▸ Assign** |
| Companion (manual bind) | `deploy/companion_setup.sh <user>@<zero-host>.local --g2s-egm IGT_<egmId> --companion-id companion-avp` |
| Fob cards | Any ISO14443-A tag with a **fixed UID**: S50 1K "Mifare Classic" or NTAG213/215/216 fobs/cards (the $10-a-bag kind). Blank/"empty" is fine — only the UID is used. Avoid "random UID" privacy tags (UID changes every read, often starts `08`). |
