# CabiNet deploy — host + companions on their own hub

> **The one-sentence version:** put the CabiNet host and your slot machines on
> a **basic unmanaged Ethernet hub/switch of their own** — no router, no other
> DHCP — because the host IS the network stack (DHCP + DNS + NTP + TFTP + G2S).
> Everything on it is **wired**. The only Wi-Fi anywhere is the host's own
> optional leg onto your home network so you can browse the UI from the couch
> — walkthrough below.

## Why a dumb hub, not a router

The CabiNet host hands every machine its IP address and network settings over
DHCP. (Handing the machine its G2S **host URL** over DHCP too — a true zero-tap
join — is a work in progress; for now you set the host URL once per machine, see
`AVP_SETUP.md`.) A router's own DHCP server would race ours and machines would
join the wrong network. An unmanaged switch/hub has no opinions — plug things in
and the host runs the whole segment:

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
  route through it. See "Browsing from the couch" below for the setup.
- **Advanced users with managed gear:** a dedicated VLAN works exactly the
  same as the dumb switch — that's how the dev floor runs. The requirements
  don't change: an isolated L2 segment, **no other DHCP server on it** (turn
  yours off for that VLAN), and the host's NIC or tagged subinterface static
  at `192.168.50.2/24`. If you know how to do that, you don't need the
  separate switch; if you're not sure, the $15 dumb switch is the path.

## Browsing from the couch

No managed gear needed for this — the host just gets a second leg: Ethernet
stays on the slot switch, and its built-in Wi-Fi joins your home network like
any laptop would. Nothing routes between the two, so the slot segment stays
isolated. On a Pi host:

1. **Join your home Wi-Fi:**

   ```bash
   sudo nmtui        # → "Activate a connection" → pick your network → password
   ```

   (or in one line: `sudo nmcli device wifi connect "YourNetwork" password "YourPassword"`)

2. **Find the address your home network gave the host:**

   ```bash
   hostname -I       # the one NOT starting with 192.168.50. is the home-side IP
   ```

3. **From a phone or laptop on your home Wi-Fi, open:**

   ```
   http://<that address>:8081
   ```

   On most devices `http://<the host's hostname>.local:8081` works too. Some
   Android browsers don't resolve `.local` — use the IP there, and give the
   host a DHCP reservation in your router so it doesn't move.

That's it. The machines and companion Pis don't change at all — they never see
your home network. Two things to know:

- The **wired-only rule is about the slot segment.** The host's home-side leg
  being Wi-Fi is fine — the dev floor runs exactly this shape.
- The page has **no login** — anyone on your home Wi-Fi can open it and play
  banker. In a house full of friends that's a feature; just know it's there.

Non-Pi hosts: join your home network however that box usually does; steps 2–3
are the same.

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

   Then open **http://192.168.50.2:8081/** — the CabiNet House Floor. Any TV
   with a browser can be the spectator board — point it at
   **http://192.168.50.2:8081/board** (the attract show and tournament
   nights: see [`TOURNAMENT.md`](TOURNAMENT.md)).

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

- **IGT (AVP Family 14 etc.) — plug-and-play join:** Certificate Protocols
  **NO** (cert-less is the only supported path), "Override DHCP Configured
  Host" **NO**. The machine takes the host from DHCP option 43 and joins on
  its own — nothing to type. After changing any comm settings, **re-enable
  G2S in the debug menu** or the machine's endpoint stays dark.
  The join is zero-config; the money features are not: **WAT (wallet
  transfers) and TITO need their permissions enabled in the operator menu,
  which takes your eKey.** For the on-glass UI: enable the mediaDisplay
  content areas in the operator menu and give them memory from the media
  pool (it's RAM-capped — enable the ones that fit).
  **Step-by-step with operator-menu photos: [`AVP_SETUP.md`](AVP_SETUP.md).**
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

Wire a SAS SMIB Pi (flash it per [`deploy/SMIB_FRESH_IMAGE.md`](SMIB_FRESH_IMAGE.md),
or run `deploy/zero2w_sas_setup.sh`) between the machine's SAS port and the
switch. Then set these in the operator menu
— **these are the one-shot fields**:

| Field | Set it to | Why |
|---|---|---|
| Validation mode | **Secure Enhanced** if offered; otherwise **System** | Enhanced is the primary path (machine self-mints ticket numbers, host records them). No Enhanced? System mode still ties into the hub — the host answers the machine's cash-out requests in real time. Machine-only/"Standard" validation is the last resort: tickets will print but won't be in the hub ledger. |
| SAS address | **1** | The SMIB polls address 1 by default (`--address` changes it if you must). |
| AFT / cashless transfers | **Enabled** (if offered) | This is how credits move between the wallet and the machine. |
| Legacy bonusing | **Enabled** (if present) | On pre-AFT machines this is the credit-push path; harmless to have on otherwise. |
| Handpay receipts | **Enabled** if desired | The machine prints a receipt when a handpay is keyed off — pure showmanship, your call. |

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

Build the Pi from a fresh SD card — it's just Raspberry Pi OS Lite plus one
setup script; `deploy/SMIB_FRESH_IMAGE.md` walks the whole card, and a
prebuilt image may land in Releases later. Plug the Pi into the same switch,
power it from the cabinet's USB. That's the whole install:

- The Pi self-identifies by its hardware serial and finds the hub via its DHCP
  default gateway (the host) — **no per-device config, no flags**.
- It appears in the UI as an unassigned reader; assign it to a machine from
  that machine's ⚙️ Options. A reader riding a SAS SMIB auto-binds to that
  SMIB's machine.

## Already running CabiNet? Read this first

The updater needs three things that an older install may not have: a **git
clone**, a **branch tracking `origin/main`**, and an **SSH key from the hub to
each satellite**. Plenty of hubs were set up by copying files or unpacking a
download, so this is a one-time adoption. Do it in this order.

### 1. Back up your data — before anything else

```sh
# on the hub
mkdir -p ~/cabinet-backups
python3 - <<'PY'
import os, shutil, sqlite3, time
src = os.path.expanduser("~/CasinoNet/G2S/data")
dst = os.path.expanduser("~/cabinet-backups/pre-adopt-" + time.strftime("%Y%m%d-%H%M%S"))
os.makedirs(dst, exist_ok=True)
for f in sorted(os.listdir(src)):
    p = os.path.join(src, f)
    if not os.path.isfile(p) or f.endswith(("-wal", "-shm")):
        continue
    if f == "hub.db":                       # WAL-safe: a plain cp can tear it
        c = sqlite3.connect(p); o = sqlite3.connect(os.path.join(dst, f))
        c.backup(o); o.close(); c.close()
    else:
        shutil.copy2(p, os.path.join(dst, f))
print("backed up ->", dst)
PY
```

Then copy that folder **off the Pi** (`scp -r`), because an SD card is exactly
the thing that fails while you are busy. Sanity-check it:

```sh
python3 -c "import sqlite3,sys;print(sqlite3.connect(sys.argv[1]).execute('PRAGMA quick_check').fetchone())" \
  ~/cabinet-backups/pre-adopt-*/hub.db
```

### 2. Get the updater (your install predates it)

`deploy/update.py` shipped in July 2026, so **an older install does not have
it**. SSH to the hub and grab the one file — you only ever do this once, because
from then on it updates itself:

```sh
cd ~/CasinoNet                     # wherever your install lives (holds G2S/ and SAS/)
mkdir -p deploy
curl -fsSL https://raw.githubusercontent.com/ajs1616/CabiNet/main/deploy/update.py \
  -o deploy/update.py
python3 deploy/update.py --dry-run
```

No `curl`? Use `wget -O deploy/update.py <same URL>`. Dropping the file in the
install root instead of `deploy/` works too — it looks in both.

The `--dry-run` changes nothing and tells you what a real run would do. When it
looks right, drop the flag.

### 3. Not a git clone? The updater fixes that itself

You do **not** need to know or care. If your install was copied, unpacked from a
download, or set up any way other than `git clone`, just run the updater — it
notices, offers to adopt the tree in place, and only then updates:

```sh
cd ~/CasinoNet
python3 deploy/update.py
```

Before it changes anything it snapshots your data, proves the release tracks
**nothing** under `data/`, and prints exactly which files on disk differ from the
release and will be replaced. Then it asks. Your wallets, tickets, AFT/WAT keys
and registry are never touched — they are not tracked by the repo, and there is
a snapshot besides. Files the release does **not** ship (old logs, a support
bundle, anything you added) are left alone.

One thing to know: if you edited a **tracked** file in place — a service unit, a
TFTP config — adoption replaces it with the released version. It is listed in
the preview before you confirm, and the snapshot has your copy.

### 4. Let the hub reach its satellites

The updater pushes the SAS tree to every satellite Pi itself, so the **hub**
needs a key to them (the setup scripts ran from your laptop, which is a
different machine):

```sh
# on the hub — skip ssh-keygen if ~/.ssh/smib already exists
ssh-keygen -t ed25519 -f ~/.ssh/smib -N ""
ssh-copy-id -i ~/.ssh/smib.pub aj@192.168.50.102     # once per satellite
ssh -i ~/.ssh/smib aj@192.168.50.102 hostname        # must print its name
```

Satellite IPs come from the hub itself — they self-report. If you are not sure:

```sh
curl -s http://127.0.0.1:8081/api/status \
  | python3 -c "import sys,json;[print(v.get('smibId'),v.get('peer')) for v in json.load(sys.stdin).get('sas',{}).values()]"
```

No satellites at all (every machine on G2S)? Then pass `--no-satellites`.

### 5. Dry-run, then do it

```sh
cd ~/CasinoNet
python3 deploy/update.py --dry-run     # changes nothing
python3 deploy/update.py
```

### If it refuses

It is designed to refuse rather than guess. The common ones:

| message | what to do |
|---|---|
| `has no G2S/ and SAS/` | you are in the wrong folder — `cd` to your CabiNet install |
| `on a DETACHED HEAD` | `git checkout main` |
| `no upstream configured` | `git branch --set-upstream-to=origin/main main` |
| `NOT the one the hub runs from` | you are in a second clone — `cd` to the one in `casinonet-g2s.service`'s `WorkingDirectory` |
| `refusing to pull over local edits` | `git stash`, or `--allow-dirty` if the edits are disposable |
| `a tournament is armed/running` | finish or cancel the round |
| `hub.db fails PRAGMA quick_check` | your database was already damaged — restore a snapshot from `G2S/data/_backups/` before updating |
| a gate fails | nothing was restarted and the tree was put back; grab a support bundle and open an issue |

Nothing here is a dead end: every refusal happens **before** anything restarts,
and a failure after that point restores your previous code *and* your data
together.

## Updating

Run this **on the hub, from inside your clone**:

```sh
cd ~/CasinoNet
python3 deploy/update.py --dry-run    # see exactly what would happen
python3 deploy/update.py              # do it, after confirming
```

It updates the **whole fleet** — hub *and* every satellite Pi — because they
are not independent. `SAS/core/*.py` has to be identical on both, and a hub-only
`git pull` breaks a satellite in one of two ways: loudly, with an `ImportError`
crash-loop, or **silently**, where the service reports `active`, logs nothing at
all, and the SAS link is simply dead. The silent one is why this script exists
instead of a line in these docs telling you to `git pull`.

**Your data is protected, and that is not optional.** Before anything is
touched, the updater takes a full snapshot of `G2S/data/` — `hub.db` (via
SQLite's online backup, so a WAL database is captured consistently), plus
wallets, vouchers, transfers, the machine registry and your nicknames — into
`G2S/data/_backups/`. The last 5 snapshots are kept.

That snapshot is not a formality. New code can **migrate** `hub.db` to a newer
schema the moment it starts, so putting old code back on its own would leave it
reading a database it was never written for. A rollback therefore restores
**code and data together**, and the updater tells you when a migration happened
and which snapshot predates it.

What it does, in order:

1. refuses if a tournament is armed, counting down, or running
2. refuses if this clone is not the one your `casinonet-g2s` service runs from
3. snapshots your data, after checking `hub.db` passes `PRAGMA quick_check`
4. pulls, then runs the full gate suite — **if a gate fails, nothing restarts**
5. pushes the SAS tree to every satellite (never `data/`), restarts them, then the hub
6. waits for the machines and SAS links that were up to come back
7. any failure at all → restores the previous commit **and** the data snapshot

Useful flags: `--dry-run`, `--yes` (no prompt), `--gates-only` (just test the
current tree), `--no-satellites` (hub only — leaves satellites stale, so only if
you have none).

If a kiosk or tablet is showing the UI, **hard-reload it** afterwards; the
browser will be holding the old page.

## When something breaks — grab a support bundle

Open a GitHub issue with **what went wrong and roughly when** (clock time
matters — the journals are timestamped). Then grab a support bundle: one
command gathers everything needed to debug it (service journals, CabiNet
logs, state snapshots, network + system info) into a single `.tar.gz` —
read-only, works even when the services are down:

```sh
# on the host box:
python3 deploy/support_bundle.py

# on a satellite Pi (SAS SMIB / reader), same script:
python3 ~/CasinoNet/deploy/support_bundle.py
```

It prints the file it wrote. Run it with `sudo` if it says it couldn't read
the unit journals. If the problem is at one machine, grab bundles from
**both** the host and that machine's Pi.

⚠️ The bundle contains your floor's data (machine ids, player names,
fun-money balances, protocol traffic) — **don't attach it to the public
issue**. Say in the issue that you have one and we'll arrange a private
hand-off.

## Ground rules (the things that break it)

1. **No other DHCP server on the slot segment.** Ever. That's why it's a dumb
   switch and not a router.
2. **The host is always 192.168.50.2.** The segment is always 192.168.50.0/24.
3. **Wired only.** Don't try to Wi-Fi a companion or a machine to the segment.
4. **One host per segment.** Machines are configured for exactly one G2S host.
5. Money data lives in the host's `G2S/data/` — back it up if you care about
   your game room's wallets, and never point test tools at it.
