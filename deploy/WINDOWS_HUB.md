# CabiNet hub on Windows — lite mode only

> **The one-sentence version:** a Windows PC can be the CabiNet hub, but only
> the way `LITE_DEPLOY.md` describes it — your router hands out addresses and
> one DHCP setting (option 43) points the machines at this PC. The PC runs the
> G2S host, the web UI and the database. It does **not** run DHCP, DNS, NTP or
> TFTP, and it can't.

Read [`LITE_DEPLOY.md`](LITE_DEPLOY.md) first: the router recipe, the
option-43 payload, what lite mode gives up and why. This page only covers what
is specific to Windows.

## Why Windows is lite-only

The four network-bootstrap services that make full mode's `192.168.50.2`
address work are Linux-only, and on a typical Windows PC they would be
fighting Windows itself:

- UDP 123 (NTP) is held by the Windows Time service.
- UDP 53 (DNS) and 67 (DHCP) are held by Internet Connection Sharing as soon
  as Hyper-V, WSL or a VM product creates a virtual switch.
- The services pin themselves to one network card with a Linux-only socket
  option; without it a DHCP server on Windows answers on *every* adapter —
  your LAN, Hyper-V, VMware — which is the rogue-DHCP scenario the ground
  rules forbid.
- Broadcast replies from an unpinned socket leave the PC over the default
  route, not the card the slot machine is on.

None of that is worth fighting for a mode you don't want on a test PC. So
`g2s_host.py` refuses to start on Windows with the full-mode default and
insists on `--host-base` (the launcher below passes it for you), the
Settings ▸ Updates card says plainly that it can't act here, and the fleet
doctor card is absent. One codebase, no fork: a Linux hub still gets the
full/lite choice.

## What you need

- **Python 3.11 or newer** from python.org (tick *py launcher* in the
  installer). The hub is dependency-free Python — no venv, no pip.
- **git** (git-scm.com) — to clone, and later to update.
- **A DHCP reservation for this PC on your router.** Mandatory, not optional:
  option 43 carries the hub's address as four literal bytes, and every
  machine and satellite that learned it is orphaned if the address changes.
- The router's option-43 entry for the IGT AVP: one TLV, `01 04` + the four
  address bytes. For `192.168.1.50` that is `0x0104C0A80132`; work yours out
  the same way (`LITE_DEPLOY.md` §4 has recipes per router brand).

## Install and run

```bat
git clone https://github.com/ajs1616/CabiNet.git %USERPROFILE%\CabiNet
cd %USERPROFILE%\CabiNet
copy deploy\windows\hub.env.example deploy\windows\hub.env
notepad deploy\windows\hub.env          :: HUB_IP=<the address you reserved>
deploy\windows\run_hub.cmd
```

`run_hub.cmd` (or `run_hub.ps1` from PowerShell) finds a Python 3.11+, sets
the UTF-8 console the hub's logging needs, and starts

```
python -u g2s_host.py --harvest --host-base http://<HUB_IP>:8081 --log-dir logs
```

from the `G2S` folder. Extra flags pass straight through
(`run_hub.cmd --no-seed`). Stop it with Ctrl+C. Logs land in `G2S\logs`,
your floor's data in `G2S\data` — back that folder up if you care about your
wallets.

**Once, in an administrator prompt**, let the machines reach the hub through
Windows Firewall:

```bat
netsh advfirewall firewall add rule name="CabiNet hub" dir=in action=allow protocol=TCP localport=8081
```

Then open `http://<HUB_IP>:8081/` from any browser on the LAN.

## Check it's alive

```bat
curl http://<HUB_IP>:8081/api/status
```

The `_engine` block should show `"platform": "windows"`, your `hostBase`,
and the version. Starting without `--host-base` (or with the full-mode
default) exits with the lite-only message — that is the guard working.

## Satellites (RFID readers, SAS SMIBs)

Exactly as in `LITE_DEPLOY.md` §6: every satellite Pi gets
`--hub http://<HUB_IP>:8081` explicitly. The setup scripts are bash and run
fine from **Git Bash** on this PC, which has `ssh` but not `rsync`; the
scripts fall back to a tar-over-ssh copy when `rsync` is absent, so no extra
install is needed. Run them from the repo root:

```bash
deploy/companion_setup.sh <user>@<satellite-host> --hub http://<HUB_IP>:8081
deploy/smib_setup.sh      <user>@<satellite-host> --hub http://<HUB_IP>:8081
```

## What does NOT work on a Windows hub

- **Full mode** — see above. The `cabinet-dhcp/-dns/-ntp/-tftp` services and
  `hub_setup.sh` are Linux-only.
- **Settings ▸ Updates** — the card shows the running version and says it
  can't update here. `deploy/update.py` restarts systemd units and pushes to
  satellites over ssh/rsync. To update a Windows hub: stop it, `git pull`,
  start it again. Satellites are refreshed by re-running their setup script.
- **The fleet doctor** (`cabinetconfig`) — not offered on Windows.
- **Running as a service.** The launcher runs in a console window. That is
  what a test PC wants; if you ever need it headless, Task Scheduler
  ("run whether user is logged on or not") pointing at `run_hub.cmd` is the
  simplest route.

## Updating a Windows hub

```bat
cd %USERPROFILE%\CabiNet
git pull
deploy\windows\run_hub.cmd
```

The updater's data snapshot and rollback are Linux-only, so copy `G2S\data`
somewhere safe before a big update if the floor's wallets matter to you.
