# Changelog

## v0.1.0
First explicit version number: the host now reports it (with its platform and
its own address) in the `_engine` block of `/api/status`.

- **New install option: lite mode.** The host can now run inside your
  existing network, behind your own router, instead of owning an isolated
  network segment — so you no longer need a dedicated switch. One extra
  setting on the router (a DHCP option) lets the machines still find the host
  automatically. Full mode is unchanged and stays the default. Guide:
  `deploy/LITE_DEPLOY.md`.
- **New setting for the host's own address** (`--host-base`). Lite installs
  set it once so on-glass pages and the machines' comm settings point at the
  right address; full-mode installs never need to touch it. If a machine
  reaches the host on a different address than the one advertised, the host
  logs a warning once per machine.
- **The satellite Pis (card readers, SAS bridges) now verify the address they
  found automatically really is the host.** If it is not (in a lite install
  they would otherwise be talking to the router), they hold back everything
  that normally goes to the host — status reports and, on the SAS side,
  ticket handling — and leave a clear warning in their journal instead of
  quietly sending data to the wrong device. An explicitly configured hub
  address is trusted as before.
- **The hub can now run on a Windows PC**, in lite mode only (your router
  hands out addresses; see `deploy/WINDOWS_HUB.md`). A launcher in
  `deploy/windows/` finds a suitable Python, reads this PC's address from a
  one-line settings file and starts the hub; the guide covers the firewall
  rule and the router setting.
- On Windows the hub refuses to start with the full-mode network address and
  tells you to pass this PC's own address instead, so it can never advertise
  screens the machines cannot reach.
- Settings ▸ Updates on a Windows hub shows the running version and says
  plainly that updating from the card isn't possible there; the fleet doctor
  card is not offered. A Linux hub keeps everything, including the choice
  between full and lite mode.
- Saving wallets, vouchers and settings on a Windows hub no longer logs a
  false "save FAILED" after every successful save.
- The satellite setup scripts (card readers, SAS bridges) no longer require
  `rsync` on the computer you run them from: without it they copy the files
  over the same SSH connection another way. That is what lets you set up
  satellites straight from a Windows hub's Git Bash.
