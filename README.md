# CabiNet 🎰

**Link the slot machines in your home game room.** CabiNet is a hobbyist
"casino system" for collectors of real slot machines — one small Linux host
runs your floor: machines join it over their own protocols (G2S over Ethernet,
SAS via a little Pi bridge), and you get a live floor view, fun-money player
wallets with RFID player cards, TITO tickets that redeem across machines,
credit pushes from a web UI, on-glass bonuses, and handpay clearing — the
whole casino *experience*, at home, for friends.

> **For home game rooms only.** CabiNet is a free hobbyist project for
> personal, non-commercial use with machines you own. It is not certified for
> — and must never be used in — real-money or regulated gaming of any kind.
> The money is fun-money: your bank, your rules, no limits. You wire it into
> your own machines at your own risk — double-check pinouts before powering
> anything; no warranty of any kind (see [LICENSE](LICENSE)).

### 📦 [New install](deploy/DEPLOY.md#host-install)  ·  ⬆️ [**Updating an existing hub**](deploy/DEPLOY.md#updating)  ·  🛠 [Something broken?](deploy/DEPLOY.md#when-something-breaks--grab-a-support-bundle)

## What works today (live-proven on real iron)

- **Direct G2S over Ethernet** — the host is the DHCP server and hands each
  machine its IP + network settings; you point the machine at the host URL once
  (a few taps, see `deploy/AVP_SETUP.md` — automatic host-over-DHCP is a work in
  progress). Then: live meters, events, remote enable/disable — down to
  individual game titles: turn any machine-enabled title on or off from the host.
- **SAS via a small Pi bridge** (a used Pi 3B+ is perfect — Ethernet built
  in) — meters, AFT credit transfers both directions, TITO, legacy
  bonusing, handpay reset.
- **Cross-machine TITO** — print a ticket on one machine, redeem it in another.
- **Player wallets + RFID cards** — tap a fob, the machine knows who's playing;
  fund friends from the House bank; wallet↔machine transfers.
- **On-glass UI** (IGT mediaDisplay) and a touchscreen kiosk for SAS machines —
  both showing *your* game room's name in lights.
- **Dual-protocol cabinets** — a machine that speaks both G2S and SAS is
  linked into one tile with SAS as the money authority.
- **The Gameroom Board** — a wall-TV spectator page at `/board` off the host:
  your game room's name in lights, an attract rotation (now playing,
  standings, record to beat), moment call-outs written as sentences, and
  fireworks on the big hits.
- **Tournament night** — arm the floor and every machine becomes a seat:
  credits funded from the House, one countdown, a wins-only leaderboard
  racing on the board, winner ceremony, leftovers swept back to the House.
  Uncarded players get funny names from an editable roster. See
  [`deploy/TOURNAMENT.md`](deploy/TOURNAMENT.md).
- **Floor lock** — one switch in Settings locks or unlocks every machine on
  the floor at once.

## ⬆️ Already running CabiNet? Updating is one command

```sh
cd ~/CabiNet && python3 deploy/update.py
```

It updates the **whole fleet** — hub *and* every satellite Pi, which are not
independent — snapshots your data first, runs the full test suite before
anything restarts, and puts both code *and* data back if the floor doesn't come
up. There's also an **Updates** card in Settings with a Check-now button if you'd
rather not use a terminal (it never contacts the internet unless you ask).

**Installed before July 2026?** Your copy predates the updater, so grab it once:

```sh
cd ~/CabiNet && mkdir -p deploy
curl -fsSL https://raw.githubusercontent.com/ajs1616/CabiNet/main/deploy/update.py \
  -o deploy/update.py
python3 deploy/update.py --dry-run     # shows what it would do, changes nothing
```

Didn't install with `git clone`? Doesn't matter — it offers to fix that itself.
Full details: **[Updating](deploy/DEPLOY.md#updating)**.

## Getting started

**Read [`deploy/DEPLOY.md`](deploy/DEPLOY.md).** The short
version: the host + your machines + the companion Pis go on a **basic
unmanaged Ethernet switch of their own** (the host runs the whole network),
the host is any Linux box with a spare Ethernet port, and the core stack is
dependency-free Python 3. Standing the host up is one command:

```sh
git clone https://github.com/ajs1616/CabiNet.git ~/CabiNet
cd ~/CabiNet && sudo ./deploy/hub_setup.sh
```

It picks the slot-side port with you, sets the hub address
(`192.168.50.2/24`), and installs + starts the five hub services — it even
retires and carries data over from an old hand-installed hub. Machine-side
setup (G2S flavor, host URL, media display enable) is in the same doc.

- `G2S/` — the host: G2S engine, web UI, DHCP/DNS/NTP/TFTP bootstrap servers,
  the SQLite hub spine, test gates under `G2S/tools/`
- `SAS/` — the SAS bridge stack that runs on the SMIB Pi (3B+ recommended)
  (deps: pyserial, crcmod, loguru — see `SAS/requirements.txt`)
- `Companion/` — the RFID reader daemon (stdlib-only)
- `deploy/` — systemd units, setup scripts, the deploy guide
- `COMPATIBILITY.md` — the machine compatibility matrix (bench-tested)

## Status

Early release. It runs a real two-machine floor daily (IGT AVP on direct
G2S, WMS BB2E dual-protocol) — every other brand and OS rev still needs
proving on real iron, and that's where you come in: run it on your machines
and send back a `COMPATIBILITY.md` row (plus a support bundle when something
fights back — see `deploy/DEPLOY.md`). Expect rough edges; bring your debug
logs. Issues and PRs welcome.

## License

CabiNet is free software: [GPL-3.0](LICENSE). Use it, modify it, share it —
if you distribute a modified version, it stays under the GPL so the next
collector gets the same freedoms you did.

Copyright (C) 2026 AJ Sawaya.

**Not for regulated gaming.** CabiNet is uncertified hobbyist software for
home game rooms and machines you personally own. The GPL's no-warranty terms
apply in full, and nothing here may be deployed in real-money or regulated
gaming environments.
