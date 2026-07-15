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
> The money is fun-money: your bank, your rules, no limits.

## What works today (live-proven on real iron)

- **Direct G2S over Ethernet** — plug-and-play join (the host is the DHCP
  server and hands the machine its host URL; zero machine-side config on IGT
  AVP), live meters, events, remote enable/disable.
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

## Getting started

**Read [`deploy/TESTER_DEPLOY.md`](deploy/TESTER_DEPLOY.md).** The short
version: the host + your machines + the companion Pis go on a **basic
unmanaged Ethernet switch of their own** (the host runs the whole network),
the host is any Linux box at static `192.168.50.2/24` on that segment, and
the core stack is dependency-free Python 3 — clone, install the systemd
units, done. Machine-side setup (G2S flavor, media display enable) is in the
same doc.

- `G2S/` — the host: G2S engine, web UI, DHCP/DNS/NTP/TFTP bootstrap servers,
  the SQLite hub spine, test gates under `G2S/tools/`
- `SAS/` — the SAS bridge stack that runs on the SMIB Pi (3B+ recommended)
  (deps: pyserial, crcmod, loguru — see `SAS/requirements.txt`)
- `Companion/` — the RFID reader daemon (stdlib-only)
- `deploy/` — systemd units, setup scripts, the deploy guide
- `COMPATIBILITY.md` — the machine compatibility matrix (bench-tested)

## Status

Early tester release. It runs a real two-machine floor daily (IGT AVP on
direct G2S, WMS BB2E dual-protocol) — the tester program exists to prove it
on *your* machines and brands. Expect rough edges; bring your debug logs.

## License

CabiNet is free software: [GPL-3.0](LICENSE). Use it, modify it, share it —
if you distribute a modified version, it stays under the GPL so the next
collector gets the same freedoms you did.

Copyright (C) 2026 AJ Sawaya.

**Not for regulated gaming.** CabiNet is uncertified hobbyist software for
home game rooms and machines you personally own. The GPL's no-warranty terms
apply in full, and nothing here may be deployed in real-money or regulated
gaming environments.
